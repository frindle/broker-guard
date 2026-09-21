"""Shared fixtures. All identity data here is FAKE and must stay that way.

Never put a real name, phone, email or address in this repo's tests.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# RFC 2606 / RFC 5737 reserved values -- not routable, not anyone's real data.
FAKE_FIRST = "Testy"
FAKE_LAST = "Mctestface"
FAKE_EMAIL = "testy.mctestface@example.invalid"
FAKE_PHONE = "+1-555-0100"
FAKE_CITY = "Springfield"
FAKE_STATE = "IL"


@pytest.fixture
def fake_profile_dict():
    return {
        "first_name": FAKE_FIRST,
        "last_name": FAKE_LAST,
        "middle_name": "Q",
        "emails": [FAKE_EMAIL],
        "phones": [FAKE_PHONE],
        "city": FAKE_CITY,
        "state": FAKE_STATE,
    }


@pytest.fixture
def profile_file(tmp_path, fake_profile_dict):
    path = tmp_path / "profile.local.json"
    path.write_text(json.dumps(fake_profile_dict), encoding="utf-8")
    return str(path)


@pytest.fixture
def brokers_dict():
    return {
        "brokers": [
            {"id": "alpha", "name": "Alpha People", "url": "https://alpha.invalid",
             "verification": "automatable"},
            {"id": "beta", "name": "Beta Search", "url": "https://beta.invalid",
             "verification": "captcha"},
            {"id": "gamma", "name": "Gamma Records", "url": "https://gamma.invalid",
             "verification": "photo_id"},
        ]
    }


@pytest.fixture
def brokers_file(tmp_path, brokers_dict):
    path = tmp_path / "brokers.json"
    path.write_text(json.dumps(brokers_dict), encoding="utf-8")
    return str(path)


@pytest.fixture
def base_env(tmp_path, profile_file, brokers_file):
    return {
        "BG_PROFILE_PATH": profile_file,
        "BG_BROKERS_PATH": brokers_file,
        "BG_STATE_PATH": str(tmp_path / "state.sqlite"),
        "BG_LOG_DIR": str(tmp_path / "logs"),
        "BG_RUN_ONCE": "1",
    }
