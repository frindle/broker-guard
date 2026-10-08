"""Learned (assisted) opt-out recipes, and the approval ladder they climb.

``optout_forms.RECIPES`` is a hand-verified allow-list in source. The assisted
filler (``assisted.py``) produces recipes for brokers nobody has transcribed;
those live HERE, in a small owner-only JSON file under /data, and move through
explicit states so an unverified recipe can never submit anything:

    candidate  -> built from the live page; may only be DRY-RUN filled
    approved   -> Penn looked at the dry-run screenshot and approved; now in
                  ``optout_forms.RECIPES``, so the normal pass may attempt it
                  (live only if the global dry-run switch is off)
    live       -> one real submission succeeded; promoted, same as a shipped
                  recipe from then on
    rejected   -> Penn declined it; never registered, never retried

Only ``approved`` and ``live`` are ever registered into the allow-list.
"""
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict

from broker_guard import optout_forms

log = logging.getLogger("broker_guard.recipe_store")

CANDIDATE, APPROVED, LIVE, REJECTED = "candidate", "approved", "live", "rejected"
UNBUILDABLE = "unbuildable"   # the page could not be turned into a recipe (wall, no form)
STATES = (CANDIDATE, APPROVED, LIVE, REJECTED, UNBUILDABLE)
REGISTERED_STATES = (APPROVED, LIVE)

_LOCK = threading.Lock()
_STEP_TYPES = {"Field": optout_forms.Field, "Choice": optout_forms.Choice,
               "Select": optout_forms.Select, "Check": optout_forms.Check}
_TUPLE_KEYS = ("choices", "fields", "steps", "forbidden_selectors", "captcha_selectors",
               "success_markers")


def recipe_to_dict(recipe) -> dict:
    data = asdict(recipe)
    for key in ("choices", "fields", "steps"):
        data[key] = [dict(asdict(step), _type=type(step).__name__)
                     for step in getattr(recipe, key)]
    return data


def recipe_from_dict(data: dict):
    data = dict(data)
    for key in ("choices", "fields", "steps"):
        steps = []
        for raw in data.get(key) or []:
            raw = dict(raw)
            cls = _STEP_TYPES[raw.pop("_type")]
            steps.append(cls(**raw))
        data[key] = tuple(steps)
    for key in _TUPLE_KEYS:
        if key not in ("choices", "fields", "steps"):
            data[key] = tuple(data.get(key) or ())
    return optout_forms.FormRecipe(**data)


def store_path(cfg) -> str:
    explicit = getattr(cfg, "learned_recipes_path", None)
    if explicit:
        return explicit
    state = getattr(cfg, "state_path", None) or "data/state.sqlite"
    return os.path.join(os.path.dirname(os.path.abspath(state)), "learned-recipes.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("learned recipes unreadable; treating as empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".recipes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class RecipeStore:
    def __init__(self, path: str):
        self.path = path

    def list(self, state: str | None = None) -> list:
        rows = []
        for bid, entry in sorted(_load(self.path).items()):
            if state is None or entry.get("state") == state:
                rows.append(dict(entry, broker_id=bid))
        return rows

    def get(self, broker_id: str):
        entry = _load(self.path).get(broker_id)
        return dict(entry, broker_id=broker_id) if entry else None

    def recipe(self, broker_id: str):
        entry = _load(self.path).get(broker_id)
        return recipe_from_dict(entry["recipe"]) if entry and entry.get("recipe") else None

    def mark_unbuildable(self, broker_id: str, reason: str, now=None) -> None:
        with _LOCK:
            data = _load(self.path)
            if data.get(broker_id, {}).get("state") in (APPROVED, LIVE):
                return
            data[broker_id] = {"state": UNBUILDABLE, "reason": reason, "created_at": now}
            _save(self.path, data)

    def add_candidate(self, recipe, platform=None, unmapped=(), screenshot=None, now=None) -> bool:
        """Store a new candidate. Never overwrites a recipe that is further along."""
        with _LOCK:
            data = _load(self.path)
            existing = data.get(recipe.broker_id)
            if existing and existing.get("state") in (APPROVED, LIVE, REJECTED, CANDIDATE):
                return False
            data[recipe.broker_id] = {
                "state": CANDIDATE, "platform": platform, "unmapped": list(unmapped),
                "screenshot": screenshot, "created_at": now,
                "recipe": recipe_to_dict(recipe)}
            _save(self.path, data)
            return True

    def set_dry_run(self, broker_id: str, record_id: str | None, screenshot: str | None) -> None:
        with _LOCK:
            data = _load(self.path)
            if broker_id in data:
                data[broker_id]["dry_run_record"] = record_id
                data[broker_id]["screenshot"] = screenshot
                _save(self.path, data)

    def forget(self, broker_id: str) -> bool:
        """Drop an entry that never produced a recipe (unbuildable), to retry it."""
        with _LOCK:
            data = _load(self.path)
            if data.get(broker_id, {}).get("state") != UNBUILDABLE:
                return False
            del data[broker_id]
            _save(self.path, data)
            return True

    def transition(self, broker_id: str, new_state: str, now=None) -> bool:
        """candidate->approved|rejected, approved->live|rejected. Anything else is refused."""
        allowed = {CANDIDATE: (APPROVED, REJECTED), APPROVED: (LIVE, REJECTED),
                   REJECTED: (CANDIDATE,), UNBUILDABLE: (CANDIDATE,)}
        with _LOCK:
            data = _load(self.path)
            entry = data.get(broker_id)
            if not entry or new_state not in allowed.get(entry.get("state"), ()):
                return False
            entry["state"] = new_state
            entry[new_state + "_at"] = now
            _save(self.path, data)
            return True


def register_all(store: RecipeStore) -> list:
    """Make approved/live learned recipes part of the allow-list. Returns ids.

    Idempotent, and also REMOVES a learned recipe whose state fell back out of
    the registered set (rejected), so the allow-list always equals the store.
    """
    want = {}
    for row in store.list():
        if row["state"] in REGISTERED_STATES and row.get("recipe"):
            try:
                want[row["broker_id"]] = recipe_from_dict(row["recipe"])
            except Exception as exc:
                log.warning("learned recipe unusable", extra={
                    "broker_id": row["broker_id"], "error": type(exc).__name__})
    for bid in list(optout_forms.LEARNED):
        if bid not in want:
            optout_forms.unregister_recipe(bid)
    for bid, recipe in want.items():
        optout_forms.register_recipe(recipe)
    return sorted(want)


def note_outcome(cfg, broker_id: str, record: dict, now=None) -> bool:
    """Promote an approved learned recipe to live after a real, confirmed submit."""
    if broker_id not in optout_forms.LEARNED:
        return False
    if record.get("outcome") != "submitted" or record.get("dry_run"):
        return False
    return RecipeStore(store_path(cfg)).transition(broker_id, LIVE, now=now)
