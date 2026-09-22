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

The "Identity" tab IS the active profile (post-merge)
--------------------------------------------------------
Live feedback after v1 shipped: having a separate "Identity" page and
"Profiles" section was confusing -- two places to manage identity data,
and a profile added on one didn't show up on the other. The fix is NOT a
new third store; it is one invariant on this list: at most one
``NamedProfile.active`` is ``True`` at a time (enforced by every writer
below -- ``add_profile``, ``set_active``, ``remove_profile``), and the
merged ``/identity`` page in ``webui.py`` is just this list with that one
entry pinned to the top and pre-selected for editing. Editing it (still
via ``POST /identity``, unchanged route/validation) upserts the active
entry here too (``upsert_active_profile``), so a save always shows up in
the list. ``migrate_legacy_profile_if_needed`` is the one-time backfill
for a deployment that already had a ``profile.local.json`` before this
merge shipped, so that pre-existing identity appears in the list too,
without the person having to re-enter it.

This still does NOT touch the scan-loop boundary described above:
whichever profile is ``active`` here is mirrored into the same, single
``profile.local.json`` the loop already read before this merge -- the
loop's contract ("read exactly one Identity from ``Config.profile_path``")
is unchanged. Only the UI now offers one place to view/switch it, plus
sync FROM the multi-profile list back to that single legacy file whenever
the active profile changes (edit, add-the-first-one, remove-the-active-
one, or an explicit "Set active"). Concurrently scanning N profiles is
still the same stated follow-up, not part of this change either.
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
    # Exactly one profile in the list this came from is ever ``True`` at a
    # time (see "The Identity tab IS the active profile" above) -- the one
    # whose data is mirrored into the legacy ``profile.local.json`` that
    # ``service``/``autopilot`` actually read. Every writer in this module
    # (add_profile/set_active/remove_profile) maintains that invariant;
    # load_profiles trusts what's on disk rather than re-deriving it.
    active: bool = False

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
            active=bool(entry.get("active", False)),
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

    If *path* has no profiles at all yet, the new one is automatically
    ``active`` -- there is always exactly one active profile once the list
    is non-empty. A caller that already migrated/created an active profile
    (the normal case once the merged UI has been opened once -- see
    ``migrate_legacy_profile_if_needed``) just adds a second, inactive
    entry; switching which one is active is ``set_active``, not this."""
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
        active=not profiles,
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
                # Editing a profile must never change WHICH profile is active
                # -- ``active`` is owned by set_active/add_profile/
                # remove_profile, and *data* (an identity form submission)
                # never carries it. Rebuilding the dataclass without this
                # silently reset it to the ``False`` default, which
                # de-activated the active profile on every save.
                active=existing.active,
            )
            profiles[i] = updated
            save_profiles(path, profiles)
            return updated
    raise ProfileNotFound(profile_id)


def remove_profile(path: str, profile_id: str) -> "NamedProfile | None":
    """Delete a profile from broker-guard's own store. Does NOT touch
    eraser's history.db -- see module docstring's "Removal keeps history"
    section. Raises ``ProfileNotFound`` for an unknown id rather than
    silently no-op'ing, so a caller's "removed" confirmation is honest.

    Maintains the one-active invariant: removing the ACTIVE profile would
    otherwise leave a non-empty list with nothing active (and nothing
    mirrored into ``profile.local.json``), so the first remaining profile
    is promoted. Returns the profile that is active afterwards when that
    promotion happened -- the caller's cue to re-sync the legacy file --
    and ``None`` when the active profile was untouched or the list is now
    empty.
    """
    profiles = load_profiles(path)
    remaining = [p for p in profiles if p.id != profile_id]
    if len(remaining) == len(profiles):
        raise ProfileNotFound(profile_id)

    removed_the_active_one = any(p.id == profile_id and p.active for p in profiles)
    promoted = None
    if removed_the_active_one and remaining:
        remaining = [
            NamedProfile(**{**p.to_dict(), "active": (i == 0)})
            for i, p in enumerate(remaining)
        ]
        promoted = remaining[0]

    save_profiles(path, remaining)
    return promoted


def set_active(path: str, profile_id: str) -> NamedProfile:
    """Make *profile_id* the one active profile, clearing ``active`` on
    every other entry (the invariant in the module docstring). Returns the
    now-active profile so the caller can mirror it into the legacy
    ``profile.local.json`` the scan loop reads -- see
    ``sync_active_to_legacy``."""
    profiles = load_profiles(path)
    if not any(p.id == profile_id for p in profiles):
        raise ProfileNotFound(profile_id)
    updated = [
        NamedProfile(**{**p.to_dict(), "active": (p.id == profile_id)})
        for p in profiles
    ]
    save_profiles(path, updated)
    return next(p for p in updated if p.id == profile_id)


def get_active_profile(path: str) -> "NamedProfile | None":
    """The one profile flagged ``active``, or ``None`` when the list is
    empty. If several are somehow flagged (hand-edited profiles.json), the
    first wins -- ``load_profiles`` trusts the file rather than rewriting
    it, so this resolves the ambiguity read-side without a surprise write."""
    for p in load_profiles(path):
        if p.active:
            return p
    return None


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


def upsert_active_profile(path: str, data: dict) -> NamedProfile:
    """Write the identity form's *data* onto the ACTIVE profile, creating
    that profile if the list has none yet.

    This is what the merged ``POST /identity`` calls so a save on the
    Profile page always shows up in the profiles list, instead of the two
    stores drifting apart (see the module docstring's "The 'Identity' tab
    IS the active profile" section). The active entry's id is preserved,
    same immutable-id rule as ``update_profile``.
    """
    active = get_active_profile(path)
    if active is not None:
        return update_profile(path, active.id, data)
    created = add_profile(path, data)
    if not created.active:
        # Degenerate case: a non-empty list where nothing was flagged
        # active (e.g. a profiles.json written before this field existed
        # and hand-edited since). add_profile only auto-activates into an
        # EMPTY list, so restore the invariant explicitly here.
        created = set_active(path, created.id)
    return created


def to_legacy_profile_dict(profile: NamedProfile) -> dict:
    """The ``profile.local.json`` shape (``profile.load_profile``'s input)
    for *profile*. Deliberately drops ``id``/``active`` -- those are this
    module's bookkeeping, not part of the single-Identity contract the
    scan loop reads."""
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


def sync_active_to_legacy(profiles_path: str, legacy_path: str) -> "NamedProfile | None":
    """Mirror whichever profile is active into *legacy_path*. Returns the
    profile written, or ``None`` when there is no active profile (an empty
    list) -- in which case the legacy file is deliberately left ALONE
    rather than truncated, so removing the last profile never leaves the
    scan loop with an unreadable identity mid-cycle."""
    active = get_active_profile(profiles_path)
    if active is None:
        return None
    write_legacy_profile(legacy_path, active)
    return active


def migrate_legacy_profile_if_needed(profiles_path: str, legacy_path: str) -> "NamedProfile | None":
    """One-time backfill: a deployment that already had a populated
    ``profile.local.json`` before the Identity/Profiles merge shipped gets
    that identity added to the profiles list automatically, as the active
    profile, so it shows up on the merged page without the person
    re-entering anything.

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
