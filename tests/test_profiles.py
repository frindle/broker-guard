"""Tests for broker_guard/profiles.py -- multi-profile CRUD and the
slugify_id/slugify_profile_id ports of eraser's Go SlugifyID/
SlugifyProfileID (vendor/eraser/internal/config/config.go).

All identity data is FAKE (RFC 2606/5737 reserved values).
"""
import pytest

from broker_guard import profiles as profiles_mod

FAKE_FIRST = "Testy"
FAKE_LAST = "Mctestface"
FAKE_EMAIL = "testy.mctestface@example.invalid"


# --- slugify_id / slugify_profile_id: byte-for-byte port of the Go rule ----

@pytest.mark.parametrize("raw,expected", [
    ("Maris", "maris"),
    ("  spouse  ", "spouse"),
    ("Jane Doe", "jane-doe"),
    ("O'Brien!!", "o-brien"),
    ("---", "profile"),
    ("", "profile"),
    ("Māris", "m-ris"),  # non-ASCII collapses like any other non-[a-z0-9] run
])
def test_slugify_id_matches_go_charset_rule(raw, expected):
    assert profiles_mod.slugify_id(raw) == expected


def test_slugify_profile_id_derives_from_first_last():
    assert profiles_mod.slugify_profile_id("Jane", "Doe", []) == "jane-doe"


def test_slugify_profile_id_appends_suffix_on_collision():
    existing = ["jane-doe"]
    assert profiles_mod.slugify_profile_id("Jane", "Doe", existing) == "jane-doe-2"
    existing = ["jane-doe", "jane-doe-2"]
    assert profiles_mod.slugify_profile_id("Jane", "Doe", existing) == "jane-doe-3"


def test_slugify_profile_id_collision_check_is_case_insensitive():
    existing = ["Jane-Doe"]
    assert profiles_mod.slugify_profile_id("jane", "doe", existing) == "jane-doe-2"


# --- CRUD round trip --------------------------------------------------------

def test_load_profiles_missing_file_is_empty_list(tmp_path):
    assert profiles_mod.load_profiles(str(tmp_path / "nope.json")) == []


def test_add_profile_assigns_a_slug_id_and_persists(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    assert created.id == "testy-mctestface"

    reloaded = profiles_mod.load_profiles(path)
    assert len(reloaded) == 1
    assert reloaded[0].id == created.id
    assert reloaded[0].emails == [FAKE_EMAIL]


def test_add_profile_requires_first_and_last_name(tmp_path):
    path = str(tmp_path / "profiles.json")
    with pytest.raises(profiles_mod.ProfileValidationError):
        profiles_mod.add_profile(path, {"first_name": "", "last_name": FAKE_LAST})


def test_add_profile_twice_same_name_gets_distinct_ids(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    b = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert a.id != b.id
    assert b.id == "testy-mctestface-2"


def test_update_profile_changes_fields_but_never_the_id(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    updated = profiles_mod.update_profile(path, created.id, {"emails": [FAKE_EMAIL]})
    assert updated.id == created.id
    assert updated.emails == [FAKE_EMAIL]

    reloaded = profiles_mod.get_profile(path, created.id)
    assert reloaded.emails == [FAKE_EMAIL]


def test_update_profile_unknown_id_raises_not_found(tmp_path):
    path = str(tmp_path / "profiles.json")
    with pytest.raises(profiles_mod.ProfileNotFound):
        profiles_mod.update_profile(path, "does-not-exist", {"first_name": "X"})


def test_remove_profile_deletes_it(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    profiles_mod.remove_profile(path, created.id)
    assert profiles_mod.load_profiles(path) == []


def test_remove_profile_unknown_id_raises_not_found(tmp_path):
    path = str(tmp_path / "profiles.json")
    with pytest.raises(profiles_mod.ProfileNotFound):
        profiles_mod.remove_profile(path, "does-not-exist")


def test_remove_one_profile_leaves_others_untouched(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    profiles_mod.remove_profile(path, a.id)
    remaining = profiles_mod.load_profiles(path)
    assert [p.id for p in remaining] == [b.id]


# --- every profile is scanned: no active flag, no promotion ---------------
#
# There used to be a "one active profile" invariant here (add sets the
# first one active, update preserves it, remove promotes a replacement).
# That whole concept is gone: every saved profile is scanned every cycle,
# so there is nothing to activate and nothing to promote. What replaces
# those tests is the assertion that the flag really is gone, plus the
# FIRST-entry rules the legacy profile.local.json mirror now rests on.

def test_profiles_have_no_active_flag_at_all(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert not hasattr(created, "active")
    assert "active" not in created.to_dict()


def test_a_legacy_active_key_on_disk_is_ignored_not_fatal(tmp_path):
    """profiles.json written by an older build still has `active` in every
    entry. Loading must drop it silently rather than blowing up on an
    unexpected keyword."""
    import json as _json

    path = tmp_path / "profiles.json"
    path.write_text(_json.dumps([
        {"id": "one", "first_name": "Aaa", "last_name": "One", "active": True},
        {"id": "two", "first_name": "Bbb", "last_name": "Two", "active": False},
    ]), encoding="utf-8")

    loaded = profiles_mod.load_profiles(str(path))
    assert [p.id for p in loaded] == ["one", "two"]
    assert not any(hasattr(p, "active") for p in loaded)


def test_adding_a_profile_appends_without_disturbing_the_others(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    assert [p.id for p in profiles_mod.load_profiles(path)] == [a.id, b.id]


def test_primary_profile_is_just_the_first_entry(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    assert profiles_mod.primary_profile(path).id == a.id


def test_primary_profile_returns_none_for_an_empty_list(tmp_path):
    assert profiles_mod.primary_profile(str(tmp_path / "profiles.json")) is None


def test_remove_profile_returns_what_it_removed_and_promotes_nothing(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    removed = profiles_mod.remove_profile(path, a.id)
    assert removed.id == a.id
    assert [p.id for p in profiles_mod.load_profiles(path)] == [b.id]


def test_remove_unknown_profile_raises_not_found(tmp_path):
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    with pytest.raises(profiles_mod.ProfileNotFound):
        profiles_mod.remove_profile(path, "does-not-exist")


def test_removing_the_last_profile_empties_the_list(tmp_path):
    path = str(tmp_path / "profiles.json")
    only = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert profiles_mod.remove_profile(path, only.id).id == only.id
    assert profiles_mod.load_profiles(path) == []


# --- load_scan_identities: WHO gets scanned -------------------------------
#
# The single answer to that question, and the reason no profile needs to
# be "active": the scan loop asks for the whole list.

def test_load_scan_identities_returns_every_saved_profile(tmp_path):
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    identities = profiles_mod.load_scan_identities(path)
    assert [(i.first_name, i.last_name) for i in identities] == [("Aaa", "One"), ("Bbb", "Two")]
    assert len({i.identity_key for i in identities}) == 2


def test_load_scan_identities_falls_back_to_the_legacy_file(tmp_path):
    """A deployment that never opened the Profiles page still has only
    profile.local.json -- it must not silently stop being scanned."""
    legacy = tmp_path / "profile.local.json"
    legacy.write_text('{"first_name": "Kept", "last_name": "Asis"}', encoding="utf-8")

    identities = profiles_mod.load_scan_identities(str(tmp_path / "profiles.json"), str(legacy))
    assert [(i.first_name, i.last_name) for i in identities] == [("Kept", "Asis")]


def test_load_scan_identities_is_empty_when_there_is_nothing_to_scan(tmp_path):
    assert profiles_mod.load_scan_identities(str(tmp_path / "profiles.json")) == []


def test_load_scan_identities_skips_an_unusable_entry_not_the_whole_list(tmp_path):
    """One malformed profile must cost only that person their scan -- the
    others still get swept."""
    import json as _json

    path = tmp_path / "profiles.json"
    path.write_text(_json.dumps([
        {"id": "bad", "first_name": "", "last_name": ""},
        {"id": "good", "first_name": "Bbb", "last_name": "Two"},
    ]), encoding="utf-8")

    identities = profiles_mod.load_scan_identities(str(path))
    assert [i.first_name for i in identities] == ["Bbb"]


# --- upsert_primary_profile: what POST /identity calls ---------------------

def test_upsert_primary_profile_creates_the_first_profile(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.upsert_primary_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    assert profiles_mod.load_profiles(path) == [created]


def test_upsert_primary_profile_updates_in_place_rather_than_appending(tmp_path):
    path = str(tmp_path / "profiles.json")
    first = profiles_mod.upsert_primary_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST,
    })
    again = profiles_mod.upsert_primary_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    assert again.id == first.id, "the primary profile's id is immutable across saves"
    saved = profiles_mod.load_profiles(path)
    assert len(saved) == 1
    assert saved[0].emails == [FAKE_EMAIL]


def test_upsert_primary_profile_only_touches_the_first_entry(tmp_path):
    path = str(tmp_path / "profiles.json")
    primary = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    other = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    profiles_mod.upsert_primary_profile(path, {
        "first_name": "Aaa", "last_name": "One", "emails": [FAKE_EMAIL],
    })
    assert profiles_mod.get_profile(path, primary.id).emails == [FAKE_EMAIL]
    assert profiles_mod.get_profile(path, other.id).emails == []


# --- the legacy profile.local.json mirror (the scan loop's contract) -------

def test_to_legacy_profile_dict_drops_bookkeeping_fields(tmp_path):
    path = str(tmp_path / "profiles.json")
    p = profiles_mod.add_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    legacy = profiles_mod.to_legacy_profile_dict(p)
    assert "id" not in legacy and "active" not in legacy
    assert legacy["first_name"] == FAKE_FIRST
    assert legacy["emails"] == [FAKE_EMAIL]


def test_sync_primary_to_legacy_writes_a_loadable_identity(tmp_path):
    """profile.local.json is now a COMPATIBILITY artifact mirroring the
    first profile, not "the active one" -- but it must still be readable
    by profile.load_profile, which is what service.run_once calls."""
    from broker_guard import profile as profile_mod

    path = str(tmp_path / "profiles.json")
    legacy = str(tmp_path / "profile.local.json")
    a = profiles_mod.add_profile(path, {
        "first_name": "Aaa", "last_name": "One", "emails": [FAKE_EMAIL],
    })
    profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    written = profiles_mod.sync_primary_to_legacy(path, legacy)
    assert written.id == a.id

    identity = profile_mod.load_profile(legacy)
    assert (identity.first_name, identity.last_name) == ("Aaa", "One")
    assert identity.emails == [FAKE_EMAIL]


def test_sync_primary_to_legacy_follows_a_removal(tmp_path):
    """Remove the first profile and the mirror follows whoever is first
    now -- there is no promotion, just "first entry"."""
    from broker_guard import profile as profile_mod

    path = str(tmp_path / "profiles.json")
    legacy = str(tmp_path / "profile.local.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    profiles_mod.sync_primary_to_legacy(path, legacy)

    profiles_mod.remove_profile(path, a.id)
    profiles_mod.sync_primary_to_legacy(path, legacy)
    assert profile_mod.load_profile(legacy).first_name == "Bbb"


def test_sync_primary_to_legacy_leaves_the_file_alone_when_the_list_is_empty(tmp_path):
    """Removing the LAST profile must not truncate the file mid-scan-cycle
    into something profile.load_profile can't read."""
    legacy = tmp_path / "profile.local.json"
    legacy.write_text('{"first_name": "Kept", "last_name": "Asis"}', encoding="utf-8")

    assert profiles_mod.sync_primary_to_legacy(str(tmp_path / "empty.json"), str(legacy)) is None
    assert "Kept" in legacy.read_text(encoding="utf-8")


# --- migrate_legacy_profile_if_needed: the one-time backfill ---------------

def test_migrate_backfills_an_existing_legacy_profile(tmp_path):
    import json as _json

    legacy = tmp_path / "profile.local.json"
    legacy.write_text(_json.dumps({
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    }), encoding="utf-8")
    path = str(tmp_path / "profiles.json")

    migrated = profiles_mod.migrate_legacy_profile_if_needed(path, str(legacy))
    assert migrated is not None
    assert migrated.emails == [FAKE_EMAIL]
    assert [p.id for p in profiles_mod.load_profiles(path)] == [migrated.id]


def test_migrate_carries_over_profile_pys_city_state_address_synthesis(tmp_path):
    import json as _json

    legacy = tmp_path / "profile.local.json"
    legacy.write_text(_json.dumps({
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST,
        "city": "Springfield", "state": "IL",
    }), encoding="utf-8")

    migrated = profiles_mod.migrate_legacy_profile_if_needed(
        str(tmp_path / "profiles.json"), str(legacy))
    assert migrated.addresses == ["Springfield, IL"]


def test_migrate_is_a_noop_once_the_list_is_non_empty(tmp_path):
    import json as _json

    legacy = tmp_path / "profile.local.json"
    legacy.write_text(_json.dumps({"first_name": "Legacy", "last_name": "Person"}), encoding="utf-8")
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})

    assert profiles_mod.migrate_legacy_profile_if_needed(path, str(legacy)) is None
    assert len(profiles_mod.load_profiles(path)) == 1


def test_migrate_is_a_noop_without_a_legacy_file(tmp_path):
    assert profiles_mod.migrate_legacy_profile_if_needed(
        str(tmp_path / "profiles.json"), str(tmp_path / "nope.json")) is None


def test_migrate_is_a_noop_for_an_unreadable_legacy_file(tmp_path):
    """A half-written or invalid profile.local.json is "nothing to
    migrate", not a 500 on every page load."""
    legacy = tmp_path / "profile.local.json"
    legacy.write_text("{not json", encoding="utf-8")
    path = str(tmp_path / "profiles.json")

    assert profiles_mod.migrate_legacy_profile_if_needed(path, str(legacy)) is None
    assert profiles_mod.load_profiles(path) == []
