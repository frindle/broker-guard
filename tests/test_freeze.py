"""Tests for broker_guard.freeze.

PINs used here are dummy test values, never a real security-freeze PIN.
"""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from broker_guard.freeze import (
    BUREAUS,
    REQUIRED_BUREAU_FIELDS,
    STATUS_FROZEN,
    STATUS_NOT_STARTED,
    STATUS_PENDING,
    STATUS_THAWED,
    BureauFreezeState,
    is_valid_transition,
    semi_automate_freeze,
    transition,
    validate_registry,
)

EXPECTED_BUREAUS = {"equifax", "experian", "transunion", "innovis",
                     "chexsystems", "nctue", "lexisnexis"}


# --- registry integrity ---------------------------------------------------

def test_registry_has_every_expected_bureau():
    assert EXPECTED_BUREAUS.issubset(BUREAUS.keys())


def test_registry_validates_clean():
    assert validate_registry() == []


@pytest.mark.parametrize("bureau_key", sorted(BUREAUS.keys()))
def test_every_bureau_has_required_fields(bureau_key):
    entry = BUREAUS[bureau_key]
    for field_name in REQUIRED_BUREAU_FIELDS:
        assert field_name in entry, f"{bureau_key} missing {field_name}"
    assert isinstance(entry["online_selfserve"], bool)
    assert entry["freeze_url"].startswith("https://")
    assert entry["thaw_url"].startswith("https://")
    assert isinstance(entry["display_name"], str) and entry["display_name"]


def test_validate_registry_catches_a_missing_field():
    broken = {"acme": {"display_name": "Acme", "freeze_url": "https://acme.invalid",
                        "thaw_url": "https://acme.invalid", "online_selfserve": True}}
    problems = validate_registry(broken)
    assert any("notes" in p for p in problems)


def test_validate_registry_catches_a_non_https_url():
    broken = {"acme": {"display_name": "Acme", "freeze_url": "http://acme.invalid",
                        "thaw_url": "https://acme.invalid", "online_selfserve": True, "notes": ""}}
    problems = validate_registry(broken)
    assert any("freeze_url" in p for p in problems)


def test_validate_registry_catches_wrong_type_for_online_selfserve():
    broken = {"acme": {"display_name": "Acme", "freeze_url": "https://acme.invalid",
                        "thaw_url": "https://acme.invalid", "online_selfserve": "yes", "notes": ""}}
    problems = validate_registry(broken)
    assert any("online_selfserve" in p for p in problems)


# --- status transitions ---------------------------------------------------

def test_default_state_is_not_started():
    state = BureauFreezeState(bureau_key="equifax")
    assert state.status == STATUS_NOT_STARTED
    assert state.last_updated is None


@pytest.mark.parametrize("current,new,expected", [
    (STATUS_NOT_STARTED, STATUS_PENDING, True),
    (STATUS_NOT_STARTED, STATUS_FROZEN, True),
    (STATUS_PENDING, STATUS_FROZEN, True),
    (STATUS_FROZEN, STATUS_THAWED, True),
    (STATUS_THAWED, STATUS_FROZEN, True),
    (STATUS_THAWED, STATUS_PENDING, True),
    (STATUS_FROZEN, STATUS_NOT_STARTED, False),
    (STATUS_PENDING, STATUS_THAWED, False),
    ("bogus", STATUS_FROZEN, False),
    (STATUS_FROZEN, "bogus", False),
])
def test_is_valid_transition(current, new, expected):
    assert is_valid_transition(current, new) is expected


def test_transition_updates_status_and_timestamp():
    state = BureauFreezeState(bureau_key="experian")
    transition(state, STATUS_PENDING, "2026-01-01T00:00:00+00:00")
    assert state.status == STATUS_PENDING
    assert state.last_updated == "2026-01-01T00:00:00+00:00"
    transition(state, STATUS_FROZEN, "2026-01-05T00:00:00+00:00")
    assert state.status == STATUS_FROZEN
    assert state.last_updated == "2026-01-05T00:00:00+00:00"


def test_transition_rejects_invalid_move_and_leaves_state_unchanged():
    state = BureauFreezeState(bureau_key="transunion", status=STATUS_FROZEN,
                               last_updated="2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError):
        transition(state, STATUS_NOT_STARTED, "2026-02-01T00:00:00+00:00")
    assert state.status == STATUS_FROZEN
    assert state.last_updated == "2026-01-01T00:00:00+00:00"


# --- PIN encryption: never plaintext ---------------------------------------

def test_pin_is_never_stored_in_plaintext():
    key = Fernet.generate_key()
    state = BureauFreezeState(bureau_key="innovis")
    state.set_pin("1234-DUMMY", key)
    assert state.pin_token is not None
    assert state.pin_token != "1234-DUMMY"
    assert "1234-DUMMY" not in state.pin_token
    assert "1234-DUMMY" not in repr(state)
    assert "1234-DUMMY" not in str(vars(state))


def test_pin_round_trips_with_the_right_key():
    key = Fernet.generate_key()
    state = BureauFreezeState(bureau_key="chexsystems")
    state.set_pin("dummy-pin-9999", key)
    assert state.has_pin() is True
    assert state.get_pin(key) == "dummy-pin-9999"


def test_pin_decrypt_fails_with_the_wrong_key():
    key = Fernet.generate_key()
    wrong_key = Fernet.generate_key()
    state = BureauFreezeState(bureau_key="nctue")
    state.set_pin("dummy-pin-0000", key)
    with pytest.raises(InvalidToken):
        state.get_pin(wrong_key)


def test_get_pin_without_a_stored_pin_raises():
    state = BureauFreezeState(bureau_key="lexisnexis")
    with pytest.raises(ValueError):
        state.get_pin(Fernet.generate_key())


def test_set_pin_rejects_empty_pin():
    state = BureauFreezeState(bureau_key="equifax")
    with pytest.raises(ValueError):
        state.set_pin("", Fernet.generate_key())


# --- semi_automate_freeze stub ---------------------------------------------

def test_semi_automate_freeze_is_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        semi_automate_freeze("equifax")


def test_semi_automate_freeze_rejects_unknown_bureau():
    with pytest.raises(ValueError):
        semi_automate_freeze("not-a-real-bureau")
