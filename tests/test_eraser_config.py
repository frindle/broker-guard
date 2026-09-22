"""Tests for broker_guard.eraser_config.

All identity data here is FAKE (reuses conftest.py's RFC 2606/5737 reserved
values) -- never a real name, phone, email or address.
"""
import stat

import yaml

from broker_guard.eraser_config import (
    build_eraser_config,
    provision_eraser_config,
)
from broker_guard.profile import Identity

FAKE_FIRST = "Testy"
FAKE_LAST = "Mctestface"
FAKE_MIDDLE = "Q"
FAKE_EMAIL = "testy.mctestface@example.invalid"
FAKE_EMAIL_2 = "old.testy@example.invalid"
FAKE_PHONE = "+1-555-0100"
FAKE_ADDRESS = "123 Fake Street, Springfield, IL"


def _dummy_identity(**overrides):
    kwargs = dict(
        first_name=FAKE_FIRST,
        last_name=FAKE_LAST,
        middle_name=FAKE_MIDDLE,
        emails=[FAKE_EMAIL, FAKE_EMAIL_2],
        phones=[FAKE_PHONE],
        addresses=[FAKE_ADDRESS],
    )
    kwargs.update(overrides)
    return Identity(**kwargs)


# --- build_eraser_config: pure mapping logic ------------------------------

def test_fresh_config_has_expected_profile_fields():
    cfg = build_eraser_config(_dummy_identity(), existing=None)
    profile = cfg["profile"]
    assert profile["first_name"] == FAKE_FIRST
    assert profile["last_name"] == FAKE_LAST
    assert profile["middle_name"] == FAKE_MIDDLE
    assert profile["email"] == FAKE_EMAIL
    assert profile["additional_emails"] == [FAKE_EMAIL_2]
    assert profile["phone"] == FAKE_PHONE
    assert profile["address"] == FAKE_ADDRESS


def test_fresh_config_defaults_to_manual_send_mode_with_no_smtp_creds():
    cfg = build_eraser_config(_dummy_identity(), existing=None)
    assert cfg["options"]["send_mode"] == "manual"
    assert "email" not in cfg


def test_multiple_phones_and_addresses_split_into_additional_fields():
    identity = _dummy_identity(phones=[FAKE_PHONE, "+1-555-0199"],
                                addresses=[FAKE_ADDRESS, "Old Fake Street, Old Town, IL"])
    cfg = build_eraser_config(identity, existing=None)
    profile = cfg["profile"]
    assert profile["phone"] == FAKE_PHONE
    assert profile["additional_phones"] == ["+1-555-0199"]
    assert profile["address"] == FAKE_ADDRESS
    assert profile["previous_addresses"] == ["Old Fake Street, Old Town, IL"]


def test_blank_identity_fields_do_not_blank_out_existing_profile_values():
    existing = {"profile": {"first_name": "Existing", "last_name": "Person",
                             "city": "Somewhere", "date_of_birth": "1990-01-01"}}
    identity = _dummy_identity(middle_name="")  # no middle name supplied
    cfg = build_eraser_config(identity, existing=existing)
    profile = cfg["profile"]
    # Identity's own first/last overwrite (both non-blank)...
    assert profile["first_name"] == FAKE_FIRST
    # ...but fields Identity doesn't carry at all survive untouched.
    assert profile["city"] == "Somewhere"
    assert profile["date_of_birth"] == "1990-01-01"
    assert "middle_name" not in profile or profile.get("middle_name") in (None, "")


def test_merge_preserves_existing_email_and_options_blocks_untouched():
    existing = {
        "profile": {"first_name": "Old", "last_name": "Name"},
        "email": {"provider": "smtp", "from": "old@example.invalid",
                   "smtp": {"host": "smtp.example.invalid", "port": 465,
                            "username": "old@example.invalid", "password": "SECRET",
                            "use_tls": True}},
        "options": {"template": "gdpr", "rate_limit_ms": 3000, "daily_send_limit": 200},
        "inbox": {"enabled": True, "provider": "gmail"},
    }
    cfg = build_eraser_config(_dummy_identity(), existing=existing)
    assert cfg["email"] == existing["email"]
    assert cfg["options"]["template"] == "gdpr"
    assert cfg["options"]["rate_limit_ms"] == 3000
    # Merge must not force send_mode: manual onto an existing config that
    # already has real SMTP creds configured.
    assert "send_mode" not in cfg["options"]
    assert cfg["inbox"] == existing["inbox"]


def test_merge_preserves_named_profiles_list():
    existing = {"profile": {"first_name": "Old", "last_name": "Name"},
                "profiles": [{"id": "spouse", "first_name": "Other", "last_name": "Person"}]}
    cfg = build_eraser_config(_dummy_identity(), existing=existing)
    assert cfg["profiles"] == existing["profiles"]


# --- provision_eraser_config: end-to-end round trip -----------------------

def test_provision_writes_loadable_yaml_with_expected_fields(tmp_path):
    path = tmp_path / "eraser" / "config.yaml"
    result_path = provision_eraser_config(_dummy_identity(), str(path))
    assert result_path == str(path)
    assert path.exists()

    with open(path, "r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    assert loaded["profile"]["first_name"] == FAKE_FIRST
    assert loaded["profile"]["last_name"] == FAKE_LAST
    assert loaded["profile"]["email"] == FAKE_EMAIL
    assert loaded["options"]["send_mode"] == "manual"


def test_provisioned_file_has_owner_only_permissions(tmp_path):
    path = tmp_path / "config.yaml"
    provision_eraser_config(_dummy_identity(), str(path))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_reprovisioning_merges_onto_the_file_it_just_wrote(tmp_path):
    path = tmp_path / "config.yaml"
    provision_eraser_config(_dummy_identity(), str(path))
    # Simulate a hand-edited SMTP block being added between runs.
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data["email"] = {"provider": "smtp", "from": FAKE_EMAIL,
                      "smtp": {"host": "smtp.example.invalid", "port": 465,
                               "username": FAKE_EMAIL, "password": "SECRET", "use_tls": True}}
    data["options"]["send_mode"] = None
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh)

    provision_eraser_config(_dummy_identity(first_name="Updated"), str(path))

    with open(path, "r", encoding="utf-8") as fh:
        reloaded = yaml.safe_load(fh)
    assert reloaded["profile"]["first_name"] == "Updated"
    assert reloaded["email"]["smtp"]["host"] == "smtp.example.invalid"
