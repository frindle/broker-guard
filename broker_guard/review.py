"""The review folder: the audit trail for real opt-out SUBMISSION attempts.

Why this exists
------------------
Everything else in this codebase either reads a broker's page
(``browser.py``) or hands a removal to the vendored eraser engine. This
module backs the one code path that fills in the person's real PII on a
third party's web form and (optionally) presses Submit --
``optout_submit.py``. An action with that blast radius does not get to be
fire-and-forget: every attempt, whether it submitted, stopped at a bot
check, or failed, leaves a record here that Penn can read after the fact
and say "yes, that is what I wanted sent".

One attempt == two files, sharing a basename::

    <review_dir>/20260922T184233Z-consumer-canvas-9f3a1c2b.json
    <review_dir>/20260922T184233Z-consumer-canvas-9f3a1c2b.png

The name starts with a UTC timestamp so a plain lexicographic sort of the
directory IS chronological order -- ``load_attempts`` relies on that rather
than on stat() mtimes, which a volume copy or a restore would scramble.

Where it lives
-----------------
``Config.review_dir`` / ``BG_REVIEW_DIR``, defaulting to ``data/review``,
which is the ``/data`` volume in the container -- same pinning pattern as
``exposure_cache.json``/``profiles.json``/``settings.json``. It has to be
there and nowhere else: the container runs ``read_only: true`` with
``/tmp`` mounted ``noexec``, so ``/data`` is the only writable location,
and an audit trail that evaporates on redeploy would be worse than none.
Like every other ``BG_*_PATH``/``_DIR`` var this one is env-only and
deliberately NOT UI-editable (see ``settings.py``'s "What is deliberately
NOT here" -- a store cannot relocate itself).

PII discipline
-----------------
A record deliberately contains the exact field values that were typed into
the form, unredacted. That is the entire point of an audit trail Penn owns:
"what did you send on my behalf" is unanswerable if the answer is
``<email>``. The containment is on the OTHER side -- these files are
``0600`` inside a ``0700`` directory, and **nothing in this module or its
callers ever logs a field value, a record body, or a raw exception string**
(exception text from a browser routinely carries the URL, and a form URL
can carry a query string). Log lines get the broker id, the outcome and the
record id; that is all. Same rule ``searx_client._safe_error`` and
``exposure._safe_error`` already follow.
"""
import hashlib
import json
import logging
import os
import tempfile

log = logging.getLogger("broker_guard.review")

DEFAULT_REVIEW_DIR = "data/review"

# --- outcome vocabulary ------------------------------------------------------
#
# Four outcomes, and every attempt is exactly one of them. They are strings
# rather than an enum to match how this codebase already spells its other
# persisted vocabularies (``broker_status.status``, ``progress``'s
# 'hit'/'checked'/'error'/'skipped'), and because they are written verbatim
# into a JSON document that outlives the process that wrote it.
OUTCOME_SUBMITTED = "submitted"            # the form was really sent
OUTCOME_DRY_RUN = "dry_run"                # filled + screenshotted, Submit NOT pressed
OUTCOME_NEEDS_MANUAL = "needs_manual_action"  # a bot check stopped us, by policy
OUTCOME_FAILED = "failed"                  # something broke before any submit

OUTCOMES = (OUTCOME_SUBMITTED, OUTCOME_DRY_RUN, OUTCOME_NEEDS_MANUAL, OUTCOME_FAILED)

# The outcomes that mean "a human has to go and do something". Kept as a
# tuple next to the vocabulary rather than re-derived at each call site, so
# the dashboard's count and the status write can never disagree about which
# outcomes are actionable.
ACTIONABLE_OUTCOMES = (OUTCOME_NEEDS_MANUAL, OUTCOME_FAILED)

_TS_FORMAT = "%Y%m%dT%H%M%SZ"


class ReviewError(RuntimeError):
    """The review folder could not be read or written."""


def review_dir(cfg) -> str:
    """Where *cfg*'s review folder lives.

    Mirrors ``settings.store_path``: a hand-constructed ``Config`` may carry
    ``None``, which means "the module default resolved at use time", which is
    what lets the test suite point the whole folder somewhere hermetic.
    """
    return getattr(cfg, "review_dir", None) or DEFAULT_REVIEW_DIR


def attempt_id(broker_id: str, identity_key: str, started_at: str) -> str:
    """A short, stable id for one attempt.

    A digest of the three things that identify it rather than a random uuid,
    so re-deriving the id for the same attempt (e.g. to find its screenshot)
    never needs the record to be re-read.
    """
    seed = "{}|{}|{}".format(broker_id, identity_key, started_at)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (text or "").lower()]
    return "".join(keep).strip("-") or "broker"


def basename_for(broker_id: str, started_at, record_id: str) -> str:
    """The shared basename of an attempt's ``.json``/``.png`` pair.

    *started_at* is a ``datetime``; it is formatted UTC-first so the
    directory sorts chronologically by name (see the module docstring).
    """
    return "{}-{}-{}".format(started_at.strftime(_TS_FORMAT), _slug(broker_id), record_id)


def _atomic_write(path: str, write) -> None:
    """tmp file + ``os.replace`` + ``chmod 0600``.

    The same durability pattern as ``settings.save_settings`` and
    ``profiles.save_profiles``, for the same two reasons: a reader (the
    dashboard) never observes a half-written file, and these files carry PII
    so they are no more world-readable than ``profiles.json`` is.
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".review-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            write(fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_attempt(directory: str, record: dict, screenshot: bytes | None = None) -> dict:
    """Persist one attempt *record* (+ its screenshot) and return what was written.

    The returned dict is *record* plus a ``screenshot`` key naming the PNG's
    basename (or ``None``), so a caller and the dashboard agree on where the
    image is without re-deriving the name.

    The screenshot is written FIRST. If that fails the record is still
    written, with ``screenshot: None`` and a note -- an attempt that really
    happened must leave a trace even when the image could not be saved,
    because the record is the legally interesting half.
    """
    if record.get("outcome") not in OUTCOMES:
        raise ReviewError("unknown outcome: {!r}".format(record.get("outcome")))
    for required in ("id", "broker_id", "started_at"):
        if not record.get(required):
            raise ReviewError("attempt record must include {}".format(required))

    base = record.get("basename")
    if not base:
        raise ReviewError("attempt record must include basename")

    out = dict(record)
    shot_name = None
    if screenshot:
        shot_name = base + ".png"
        try:
            _atomic_write(os.path.join(directory, shot_name), lambda fh: fh.write(screenshot))
        except OSError:
            # Deliberately no exception text: it would carry the path, and
            # the path carries the broker + timestamp. Record id is enough
            # to find this in the folder.
            log.warning("could not write attempt screenshot",
                        extra={"attempt_id": record["id"], "broker_id": record["broker_id"]})
            shot_name = None
            out["screenshot_error"] = "screenshot could not be written"
    out["screenshot"] = shot_name

    path = os.path.join(directory, base + ".json")
    payload = json.dumps(out, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write(path, lambda fh: fh.write(payload))
    # Outcome + ids only. Never the record body -- it is full of PII.
    log.info("recorded opt-out attempt", extra={
        "attempt_id": out["id"], "broker_id": out["broker_id"], "outcome": out["outcome"],
    })
    return out


def load_attempts(directory: str, limit: int | None = None) -> list[dict]:
    """Every saved attempt, NEWEST FIRST.

    A missing directory is an empty list -- "nothing has been attempted yet",
    the normal state, not an error (same convention as
    ``profiles.load_profiles`` and ``settings.load_settings`` treat a missing
    store). An individual unreadable/corrupt record is SKIPPED with a warning
    rather than raising: one bad file must not take down the review page and
    hide the good records next to it.
    """
    if not os.path.isdir(directory):
        return []
    try:
        names = sorted(
            (n for n in os.listdir(directory)
             if n.endswith(".json") and not n.startswith(".")),
            reverse=True,
        )
    except OSError:
        log.warning("review folder unreadable")
        return []

    out = []
    for name in names:
        if limit is not None and len(out) >= limit:
            break
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            log.warning("skipping unreadable attempt record", extra={"record": name})
            continue
        if not isinstance(record, dict):
            log.warning("skipping non-object attempt record", extra={"record": name})
            continue
        record.setdefault("basename", name[: -len(".json")])
        out.append(record)
    return out


def get_attempt(directory: str, record_id: str) -> dict | None:
    """The saved attempt whose ``id`` is *record_id*, or ``None``.

    Scans the folder rather than deriving a filename: the basename embeds a
    timestamp the caller may not have.
    """
    for record in load_attempts(directory):
        if record.get("id") == record_id:
            return record
    return None


def status_for_outcome(outcome: str) -> str | None:
    """The ``broker_status.status`` an attempt's *outcome* implies, or ``None``.

    This is the "surface it the same way other manual-action items surface
    today" seam, and it deliberately adds NO new vocabulary: an attempt that
    stopped at a bot check, or broke, becomes ``needs_review`` -- the exact
    status ``autopilot.STATUS_NEEDS_REVIEW`` already writes and that
    ``webui._action_needed_count`` already counts into the nav's "Action
    needed" badge. So a CAPTCHA bail-out lights up the existing badge with
    no change to the broker-status model at all.

    ``dry_run`` returns ``None`` -- a rehearsal must not move a broker's real
    status. ``submitted`` also returns ``None``: what a broker does with a
    filed request is the removal pipeline's business (``eraser``'s
    pending/confirmed ladder), and stamping a status here would fight it.

    Pure, so the mapping is testable without a database.
    """
    if outcome in (OUTCOME_NEEDS_MANUAL, OUTCOME_FAILED):
        return "needs_review"
    return None


def attempt_counts(records: list[dict]) -> dict:
    """``{outcome: n}`` over *records*, every known outcome present (0 if none).

    Pure, so the dashboard's summary row can be tested without a folder.
    """
    counts = {outcome: 0 for outcome in OUTCOMES}
    for record in records or []:
        outcome = record.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts
