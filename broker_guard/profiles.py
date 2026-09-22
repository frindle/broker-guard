"""Multi-profile identity management -- the "Profiles" section of the web
UI, backing what used to require running ``eraser profile add/edit/remove``
by hand against ``~/.eraser/config.yaml``.

Scope (v1), stated explicitly per the feature request this implements
------------------------------------------------------------------------
This module is CRUD + eraser-sync ONLY. A broker-guard "profile" here is a
named identity, stored in ``data/profiles.json`` (path: ``Config.
profiles_path``), and mirrored into eraser's ``~/.eraser/config.yaml``
``profiles:`` list (see ``eraser_config.sync_profiles``) so ``eraser send
--profile <id>`` etc. keep working for any of them from the CLI.

It does NOT wire multiple profiles through broker-guard's own scan /
removal-detection / autopilot loop -- that loop (``service.run_once`` /
``autopilot.run_scan_cycle``) still reads exactly one identity, from the
single legacy ``profile.local.json`` (``Config.profile_path``), same as
before this feature. Scanning/removal-tracking N profiles concurrently
(separate ``presence``/``broker_status`` rows per profile, a profile
switcher on the dashboard, etc.) is real additional work -- a stated
follow-up, not something silently half-wired here.

Why a *separate* store rather than replacing ``profile.local.json``
---------------------------------------------------------------------
Every other module (``service``, ``autopilot``, ``webui``'s non-Profiles
routes, the state db's ``identity_key`` scoping) reads exactly one
``Identity`` from ``Config.profile_path``. Changing that to "one of N" is
the multi-profile-scan follow-up above, not this change -- so this module
adds a parallel list store instead of touching that contract.

ID stability, ported from eraser's own Go rule
-------------------------------------------------
``vendor/eraser/internal/config/config.go``'s ``SlugifyID``/
``SlugifyProfileID`` are the reference: lowercase, ``[^a-z0-9]+`` collapsed
to a single hyphen, leading/trailing hyphens trimmed, empty input falls
back to the literal ``"profile"``. ``slugify_id``/``slugify_profile_id``
below are byte-for-byte ports of those two functions (same regex class,
same trim, same collision-suffix loop) so a broker-guard-assigned id
round-trips through eraser's own ``--profile <id>`` and ``eraser profile
edit/remove`` without eraser ever re-deriving a different slug for the same
name. An id, once assigned, is NEVER changed by ``update_profile`` --
same rule ``cmd_profile.go``'s ``runProfileEdit`` enforces, and for the same
reason: it is stored verbatim in eraser's ``history.db``, so changing it
would orphan that profile's existing send history.

Removal keeps history, same as eraser's own ``profile remove``
------------------------------------------------------------------
``remove_profile`` only deletes the entry from broker-guard's
``profiles.json`` and (via the caller, ``eraser_config.sync_profiles``)
from eraser's ``profiles:`` list. It never touches eraser's ``history.db``
-- those rows stay tagged with the now-unreferenced profile id and become
reachable again if a profile with that same id is ever re-added, exactly
matching ``cmd_profile.go``'s documented behavior.
"""
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def slugify_id(text: str) -> str:
    """Port of eraser's ``config.SlugifyID`` (Go): lowercase, collapse every
    run of non-``[a-z0-9]`` characters to a single hyphen, trim leading/
    trailing hyphens. Empty/all-punctuation input returns ``"profile"``
    rather than an empty string, matching the Go function exactly."""
    base = _NON_SLUG_CHARS.sub("-", text.strip().lower())
    base = base.strip("-")
    return base or "profile"


def slugify_profile_id(first_name: str, last_name: str, existing_ids) -> str:
    """Port of eraser's ``config.SlugifyProfileID``: derive an id from
    first+last name, appending ``-2``, ``-3``, ... until it doesn't collide
    (case-insensitively) with any id in *existing_ids*."""
    base = slugify_id(f"{first_name}-{last_name}")
    taken = {pid.lower() for pid in existing_ids}
    candidate = base
    n = 2
    while candidate.lower() in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


@dataclass
class NamedProfile:
    """One entry in the profiles.json list -- an Identity plus a stable id.

    Deliberately a plain dataclass distinct from ``profile.Identity``
    (rather than subclassing it) so this module has no import-time
    dependency on ``profile.py``'s validation rules; ``load_profiles``
    below does its own minimal validation, tailored to "a saved profile
    list entry" rather than "the one active profile.local.json".
    """

    id: str
    first_name: str
    last_name: str
    middle_name: str = ""
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    addresses: list = field(default_factory=list)
    eraser_profile: str | None = None

    @property
    def full_name(self) -> str:
        parts = [self.first_name.strip(), self.middle_name.strip(), self.last_name.strip()]
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return asdict(self)


class ProfileNotFound(KeyError):
    pass


class ProfileValidationError(ValueError):
    pass


def _as_str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    raise ProfileValidationError("expected a list of strings")


def load_profiles(path: str) -> list[NamedProfile]:
    """Every profile currently on disk at *path*. A missing file is an
    empty list (no profiles created yet), not an error -- same convention
    as ``profile.load_profile`` treats a missing ``profile.local.json``,
    except this store is allowed to legitimately be empty."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ProfileValidationError(f"{path} must contain a JSON list")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProfileValidationError("each profile entry must be a JSON object")
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise ProfileValidationError("every stored profile needs a non-blank id")
        out.append(NamedProfile(
            id=pid,
            first_name=entry.get("first_name") or "",
            last_name=entry.get("last_name") or "",
            middle_name=entry.get("middle_name") or "",
            emails=_as_str_list(entry.get("emails")),
            phones=_as_str_list(entry.get("phones")),
            addresses=_as_str_list(entry.get("addresses")),
            eraser_profile=entry.get("eraser_profile"),
        ))
    return out


def save_profiles(path: str, profiles: list[NamedProfile]) -> None:
    """Atomic write (tmp file + ``os.replace``), owner-only permissions --
    same pattern as ``webui.identity_post``'s ``profile.local.json`` write,
    since this file carries the same class of PII."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".profiles-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([p.to_dict() for p in profiles], fh, indent=2)
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


def _validate_names(first_name: str, last_name: str) -> None:
    if not first_name or not first_name.strip():
        raise ProfileValidationError("first_name is required")
    if not last_name or not last_name.strip():
        raise ProfileValidationError("last_name is required")


def add_profile(path: str, data: dict) -> NamedProfile:
    """Create a new profile from *data* (same keys as the identity form:
    first_name/middle_name/last_name/emails/phones/addresses/eraser_profile).
    The id is ALWAYS derived here via ``slugify_profile_id`` -- a caller
    never supplies one, so two profiles can never collide."""
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    _validate_names(first_name, last_name)

    profiles = load_profiles(path)
    new_id = slugify_profile_id(first_name, last_name, [p.id for p in profiles])
    profile = NamedProfile(
        id=new_id,
        first_name=first_name,
        last_name=last_name,
        middle_name=(data.get("middle_name") or "").strip(),
        emails=_as_str_list(data.get("emails")),
        phones=_as_str_list(data.get("phones")),
        addresses=_as_str_list(data.get("addresses")),
        eraser_profile=(data.get("eraser_profile") or None),
    )
    profiles.append(profile)
    save_profiles(path, profiles)
    return profile


def update_profile(path: str, profile_id: str, data: dict) -> NamedProfile:
    """Update an existing profile's fields. The id itself is immutable --
    *data* is never consulted for one -- matching eraser's own
    ``profile edit`` (see module docstring)."""
    profiles = load_profiles(path)
    for i, existing in enumerate(profiles):
        if existing.id == profile_id:
            first_name = (data.get("first_name") or existing.first_name).strip()
            last_name = (data.get("last_name") or existing.last_name).strip()
            _validate_names(first_name, last_name)
            updated = NamedProfile(
                id=profile_id,
                first_name=first_name,
                last_name=last_name,
                middle_name=(data.get("middle_name") if "middle_name" in data else existing.middle_name) or "",
                emails=_as_str_list(data.get("emails")) if "emails" in data else existing.emails,
                phones=_as_str_list(data.get("phones")) if "phones" in data else existing.phones,
                addresses=_as_str_list(data.get("addresses")) if "addresses" in data else existing.addresses,
                eraser_profile=(data.get("eraser_profile") if "eraser_profile" in data else existing.eraser_profile) or None,
            )
            profiles[i] = updated
            save_profiles(path, profiles)
            return updated
    raise ProfileNotFound(profile_id)


def remove_profile(path: str, profile_id: str) -> None:
    """Delete a profile from broker-guard's own store. Does NOT touch
    eraser's history.db -- see module docstring's "Removal keeps history"
    section. Raises ``ProfileNotFound`` for an unknown id rather than
    silently no-op'ing, so a caller's "removed" confirmation is honest."""
    profiles = load_profiles(path)
    remaining = [p for p in profiles if p.id != profile_id]
    if len(remaining) == len(profiles):
        raise ProfileNotFound(profile_id)
    save_profiles(path, remaining)


def get_profile(path: str, profile_id: str) -> NamedProfile:
    for p in load_profiles(path):
        if p.id == profile_id:
            return p
    raise ProfileNotFound(profile_id)
