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
