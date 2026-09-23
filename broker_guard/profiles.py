"""Multi-profile identity management -- the "Profiles" section of the web
UI, backing what used to require running ``eraser profile add/edit/remove``
by hand against ``~/.eraser/config.yaml``.

Scope
--------
This module is the profile STORE (CRUD + eraser-sync). A broker-guard
"profile" here is a named identity, stored in ``data/profiles.json``
(path: ``Config.profiles_path``), and mirrored into eraser's
``~/.eraser/config.yaml`` ``profiles:`` list (see
``eraser_config.sync_profiles``) so ``eraser send --profile <id>`` etc.
keep working for any of them from the CLI.

Every profile in this list is scanned on every cycle -- see "Every
profile is scanned" below for how that reaches the scan loop, and
``load_scan_identities`` for the one function that hands it the set of
identities to sweep.

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

Every profile is scanned, every cycle -- there is no "active" one
------------------------------------------------------------------
There USED to be an ``active`` flag on this list: exactly one profile was
``active``, it was the only identity the scan/autopilot loop ever ran
against, and the UI had a "Make active" button to switch it. That is
gone. A data-broker monitor whose whole job is scrubbing a household's
personal data has no reason to watch one person at a time, and the flag
made "who is actually being scanned" a piece of hidden state that had to
be remembered and toggled.

The rule now: **every saved profile is scanned on every cycle**
(``autopilot.run_scan_cycles`` / ``service.run_all``), and every result
is tagged with that profile's own ``identity_key`` -- the same key
``state.py`` already scopes ``presence``/``broker_status`` by, and the
same key ``progress.ScanProgress`` now tags each per-broker outcome with
(it holds outcomes for MANY identities per scan, keyed by
``identity_key|broker_id``). Results are therefore per person, end to
end, with no global "which profile is this?" to get wrong.

``profile.local.json`` (``Config.profile_path``) still exists as a
COMPATIBILITY artifact, not as "the active profile": it is kept mirrored
to the FIRST entry in this list (``sync_primary_to_legacy``) so single-
identity entry points that predate multi-profile scanning
(``service.run_once``, ``POST /identity``, a deployment whose
``profiles.json`` has not been created yet) keep working unchanged.
``load_scan_identities`` is what the scan loops actually call: every
saved profile, falling back to that legacy file when the list is empty.
``migrate_legacy_profile_if_needed`` is the one-time backfill for a
deployment that had a ``profile.local.json`` before the list existed.
"""
import json
import os
import logging
import re
import tempfile
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

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
    # NOTE: there is deliberately no ``active`` field. Every profile is
    # scanned every cycle -- see the module docstring. A profiles.json
    # written before that change still carries an ``active`` key per entry;
    # ``load_profiles`` simply ignores it, so an old file loads cleanly and
    # the flag disappears the next time the list is saved.

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
    never supplies one, so two profiles can never collide.

    Every profile added here is scanned from the next cycle onward; there
    is nothing to activate (see the module docstring)."""
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


def remove_profile(path: str, profile_id: str) -> NamedProfile:
    """Delete a profile from broker-guard's own store. Does NOT touch
    eraser's history.db -- see module docstring's "Removal keeps history"
    section. Raises ``ProfileNotFound`` for an unknown id rather than
    silently no-op'ing, so a caller's "removed" confirmation is honest.

    Returns the profile that was removed. Nothing is promoted and nothing
    else changes: with no "active" profile there is no invariant to
    restore -- the remaining profiles all go on being scanned exactly as
    they were.
    """
    profiles = load_profiles(path)
    removed = next((p for p in profiles if p.id == profile_id), None)
    if removed is None:
        raise ProfileNotFound(profile_id)
    save_profiles(path, [p for p in profiles if p.id != profile_id])
    return removed


def primary_profile(path: str) -> "NamedProfile | None":
    """The FIRST profile in the list, or ``None`` when the list is empty.

    "Primary" here means one thing only: which profile is mirrored into
    the legacy single-identity ``profile.local.json`` for the entry points
    that still read it (see ``sync_primary_to_legacy``). It confers no
    scanning privilege whatsoever -- every profile is scanned every cycle.
    """
    profiles = load_profiles(path)
    return profiles[0] if profiles else None


def identity_key(profile: NamedProfile) -> str:
    """The ``identity_key`` the state db scopes *profile*'s rows by.

    Delegates to ``profile.Identity.identity_key`` -- the ONE derivation
    (a digest of first|middle|last) that ``service.run_once``,
    ``state.py``'s ``presence``/``broker_status`` tables and
    ``progress.ScanProgress``'s per-broker map all already use. Never
    re-implemented here: a second copy of this formula would silently
    split one person's history across two keys the day either changed.

    Imported inside the function on purpose, to keep this module free of
    an import-time dependency on ``profile.py`` (see the NamedProfile
    docstring).
    """
    from broker_guard import profile as profile_mod

    return profile_mod.Identity(**to_legacy_profile_dict(profile)).identity_key


def upsert_primary_profile(path: str, data: dict) -> NamedProfile:
    """Write *data* onto the FIRST profile in the list, creating it if the
    list is empty.

    This backs the legacy single-identity ``POST /identity`` route, which
    predates multi-profile scanning and has no profile id to work with: it
    has to mean SOME one profile, and "the first one" is the same entry
    ``sync_primary_to_legacy`` mirrors into ``profile.local.json``, so the
    two stores cannot drift. The entry's id is preserved, same immutable-id
    rule as ``update_profile``. Per-profile editing in the UI goes through
    ``update_profile`` (``POST /profiles/<id>``) instead, and does not care
    about ordering at all.
    """
    first = primary_profile(path)
    if first is not None:
        return update_profile(path, first.id, data)
    return add_profile(path, data)


def to_legacy_profile_dict(profile: NamedProfile) -> dict:
    """The ``profile.local.json`` shape (``profile.load_profile``'s input)
    for *profile*. Deliberately drops ``id`` -- that is this module's
    bookkeeping, not part of the ``Identity`` contract."""
    return {
        "first_name": profile.first_name,
        "middle_name": profile.middle_name,
        "last_name": profile.last_name,
        "emails": list(profile.emails),
        "phones": list(profile.phones),
        "addresses": list(profile.addresses),
        "eraser_profile": profile.eraser_profile,
    }


def write_legacy_profile(legacy_path: str, profile: NamedProfile) -> None:
    """Mirror *profile* into the single legacy ``profile.local.json`` that
    ``service.run_once``/``autopilot`` read. Atomic (tmp + ``os.replace``)
    and owner-only, same as ``save_profiles`` -- same class of PII."""
    parent = os.path.dirname(os.path.abspath(legacy_path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".profile-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(to_legacy_profile_dict(profile), fh, indent=2)
        os.replace(tmp_path, legacy_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(legacy_path, 0o600)
    except OSError:
        pass


def sync_primary_to_legacy(profiles_path: str, legacy_path: str) -> "NamedProfile | None":
    """Mirror the FIRST profile into *legacy_path*. Returns the profile
    written, or ``None`` when the list is empty -- in which case the
    legacy file is deliberately left ALONE rather than truncated, so
    removing the last profile never leaves a single-identity entry point
    with an unreadable identity mid-cycle.

    This file is compatibility only (``service.run_once``, ``POST
    /identity``, ``POST /brokers/<id>/remove`` without a profile). The
    scan loops read ``load_scan_identities`` -- every profile -- not this.
    """
    first = primary_profile(profiles_path)
    if first is None:
        return None
    write_legacy_profile(legacy_path, first)
    return first


def load_scan_identities(profiles_path: str, legacy_path: str | None = None) -> list:
    """Every identity a scan cycle should sweep, as ``profile.Identity``
    objects -- one per saved profile, in list order.

    This is the ONE place "who gets scanned" is decided, and the answer is
    "everyone": there is no active-profile filter here, by design (see the
    module docstring). A profile whose stored fields don't satisfy
    ``profile.Identity``'s validation is SKIPPED with the rest of the list
    still returned, so one half-filled profile can never cost the others
    their scan.

    Falls back to the single legacy ``profile.local.json`` at
    *legacy_path* when the profiles list is empty or unreadable -- a
    deployment that never opened the Profile page still scans the identity
    it has. Returns ``[]`` when there is genuinely nothing to scan;
    callers must treat that as "nothing to do", never as an error.
    """
    from broker_guard import profile as profile_mod

    try:
        saved = load_profiles(profiles_path)
    except (OSError, ValueError):
        saved = []

    identities = []
    for p in saved:
        # A profile with no name at all is not scannable: the search terms
        # would collapse to the address/email fields alone, and a
        # name-less query is exactly the kind of thing that matches
        # everybody. Skipped rather than scanned badly.
        if not (p.first_name or "").strip() and not (p.last_name or "").strip():
            log.warning("skipping a profile with no name", extra={"profile_id": p.id})
            continue
        try:
            identities.append(profile_mod.Identity(**to_legacy_profile_dict(p)))
        except (TypeError, ValueError):
            log.warning("skipping an unusable profile", extra={"profile_id": p.id})
            continue
    if identities:
        return identities

    if not legacy_path or not os.path.exists(legacy_path):
        return []
    try:
        return [profile_mod.load_profile(legacy_path)]
    except (OSError, ValueError):
        return []


def migrate_legacy_profile_if_needed(profiles_path: str, legacy_path: str) -> "NamedProfile | None":
    """One-time backfill: a deployment that already had a populated
    ``profile.local.json`` before the profiles list existed gets that
    identity added to the list automatically, so it shows up on the
    Profile page -- and gets scanned like every other profile -- without
    the person re-entering anything.

    Runs only when the profiles list is EMPTY -- once there is at least one
    profile the list is the source of truth and this is a no-op, so it is
    safe (and cheap) to call on every page load. A missing or unparseable
    legacy file is also a no-op: there is simply nothing to migrate, which
    is not an error.

    Returns the migrated profile, or ``None`` when nothing was migrated.
    """
    if load_profiles(profiles_path):
        return None
    if not os.path.exists(legacy_path):
        return None

    # Imported lazily, not at module scope: this module deliberately has no
    # import-time dependency on profile.py (see NamedProfile's docstring).
    # load_profile is reused rather than re-parsed here so the migrated
    # entry gets the exact same normalization the scan loop already sees --
    # notably profile.py's city/state -> address-line synthesis.
    from broker_guard import profile as _profile_mod

    try:
        identity = _profile_mod.load_profile(legacy_path)
    except (OSError, ValueError):
        return None

    return add_profile(profiles_path, {
        "first_name": identity.first_name,
        "middle_name": identity.middle_name,
        "last_name": identity.last_name,
        "emails": list(identity.emails),
        "phones": list(identity.phones),
        "addresses": list(identity.addresses),
        "eraser_profile": identity.eraser_profile,
    })


def get_profile(path: str, profile_id: str) -> NamedProfile:
    for p in load_profiles(path):
        if p.id == profile_id:
            return p
    raise ProfileNotFound(profile_id)
