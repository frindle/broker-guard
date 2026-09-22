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


# --- the one-active invariant + the merged Identity page's helpers ---------

def test_first_profile_is_automatically_active(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert created.active is True


def test_second_profile_is_added_inactive(tmp_path):
    """Adding a profile must never silently repoint the scan loop at a
    different identity."""
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    second = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    assert second.active is False
    assert [p.active for p in profiles_mod.load_profiles(path)] == [True, False]


def test_active_flag_round_trips_through_disk(tmp_path):
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert profiles_mod.load_profiles(path)[0].active is True


def test_update_profile_preserves_the_active_flag(tmp_path):
    """Regression: update_profile rebuilt the dataclass without `active`,
    so it fell back to the False default -- editing the active profile
    de-activated it and left NOTHING active."""
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert created.active is True
    updated = profiles_mod.update_profile(path, created.id, {"emails": [FAKE_EMAIL]})
    assert updated.active is True
    assert profiles_mod.get_profile(path, created.id).active is True


def test_update_profile_does_not_activate_an_inactive_profile(tmp_path):
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    updated = profiles_mod.update_profile(path, b.id, {"emails": [FAKE_EMAIL]})
    assert updated.active is False
    assert sum(1 for p in profiles_mod.load_profiles(path) if p.active) == 1


def test_set_active_moves_the_flag_and_clears_every_other(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    c = profiles_mod.add_profile(path, {"first_name": "Ccc", "last_name": "Three"})

    now_active = profiles_mod.set_active(path, b.id)
    assert now_active.id == b.id and now_active.active is True

    by_id = {p.id: p.active for p in profiles_mod.load_profiles(path)}
    assert by_id == {a.id: False, b.id: True, c.id: False}


def test_set_active_unknown_id_raises_not_found(tmp_path):
    path = str(tmp_path / "profiles.json")
    profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    with pytest.raises(profiles_mod.ProfileNotFound):
        profiles_mod.set_active(path, "does-not-exist")


def test_get_active_profile_returns_none_for_an_empty_list(tmp_path):
    assert profiles_mod.get_active_profile(str(tmp_path / "profiles.json")) is None


def test_remove_active_profile_promotes_another(tmp_path):
    """Regression: remove_profile just filtered the id out, leaving a
    non-empty list with NOTHING active -- and the scan loop pointing at a
    deleted identity."""
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    promoted = profiles_mod.remove_profile(path, a.id)
    assert promoted is not None and promoted.id == b.id

    remaining = profiles_mod.load_profiles(path)
    assert [p.id for p in remaining] == [b.id]
    assert remaining[0].active is True


def test_remove_inactive_profile_leaves_the_active_one_alone(tmp_path):
    path = str(tmp_path / "profiles.json")
    a = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    promoted = profiles_mod.remove_profile(path, b.id)
    assert promoted is None, "nothing was promoted -- the active profile never moved"
    assert [(p.id, p.active) for p in profiles_mod.load_profiles(path)] == [(a.id, True)]


def test_remove_the_last_profile_promotes_nothing(tmp_path):
    path = str(tmp_path / "profiles.json")
    only = profiles_mod.add_profile(path, {"first_name": FAKE_FIRST, "last_name": FAKE_LAST})
    assert profiles_mod.remove_profile(path, only.id) is None
    assert profiles_mod.load_profiles(path) == []


# --- upsert_active_profile: what POST /identity calls ----------------------

def test_upsert_active_profile_creates_the_first_profile(tmp_path):
    path = str(tmp_path / "profiles.json")
    created = profiles_mod.upsert_active_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    assert created.active is True
    assert profiles_mod.load_profiles(path) == [created]


def test_upsert_active_profile_updates_in_place_rather_than_appending(tmp_path):
    path = str(tmp_path / "profiles.json")
    first = profiles_mod.upsert_active_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST,
    })
    again = profiles_mod.upsert_active_profile(path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL],
    })
    assert again.id == first.id, "the active profile's id is immutable across saves"
    saved = profiles_mod.load_profiles(path)
    assert len(saved) == 1
    assert saved[0].emails == [FAKE_EMAIL]
    assert saved[0].active is True


def test_upsert_active_profile_only_touches_the_active_entry(tmp_path):
    path = str(tmp_path / "profiles.json")
    active = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    other = profiles_mod.add_profile(path, {"first_name": "Bbb", "last_name": "Two"})

    profiles_mod.upsert_active_profile(path, {
        "first_name": "Aaa", "last_name": "One", "emails": [FAKE_EMAIL],
    })
    assert profiles_mod.get_profile(path, active.id).emails == [FAKE_EMAIL]
    assert profiles_mod.get_profile(path, other.id).emails == []


def test_upsert_active_profile_repairs_a_list_with_nothing_active(tmp_path):
    """Degenerate input (a profiles.json written before `active` existed
    and hand-edited since): the invariant is restored rather than leaving
    the list with no active profile at all."""
    path = str(tmp_path / "profiles.json")
    stale = profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    profiles_mod.save_profiles(path, [
        profiles_mod.NamedProfile(**{**p.to_dict(), "active": False})
        for p in profiles_mod.load_profiles(path)
    ])
    assert profiles_mod.get_active_profile(path) is None

    created = profiles_mod.upsert_active_profile(path, {"first_name": "Bbb", "last_name": "Two"})
    assert created.active is True
    by_id = {p.id: p.active for p in profiles_mod.load_profiles(path)}
    assert by_id == {stale.id: False, created.id: True}


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


def test_sync_active_to_legacy_writes_a_loadable_identity(tmp_path):
    """The whole point: whatever the list says is active must be readable
    by profile.load_profile, which is what service.run_once calls."""
    from broker_guard import profile as profile_mod

    path = str(tmp_path / "profiles.json")
    legacy = str(tmp_path / "profile.local.json")
    profiles_mod.add_profile(path, {"first_name": "Aaa", "last_name": "One"})
    b = profiles_mod.add_profile(path, {
        "first_name": "Bbb", "last_name": "Two", "emails": [FAKE_EMAIL],
    })
    profiles_mod.set_active(path, b.id)

    written = profiles_mod.sync_active_to_legacy(path, legacy)
    assert written.id == b.id

    identity = profile_mod.load_profile(legacy)
    assert (identity.first_name, identity.last_name) == ("Bbb", "Two")
    assert identity.emails == [FAKE_EMAIL]


def test_sync_active_to_legacy_leaves_the_file_alone_when_nothing_is_active(tmp_path):
    """Removing the LAST profile must not truncate the file mid-scan-cycle
    into something profile.load_profile can't read."""
    legacy = tmp_path / "profile.local.json"
    legacy.write_text('{"first_name": "Kept", "last_name": "Asis"}', encoding="utf-8")

    assert profiles_mod.sync_active_to_legacy(str(tmp_path / "empty.json"), str(legacy)) is None
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
    assert migrated.active is True
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
