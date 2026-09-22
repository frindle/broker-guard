"""Tests for broker_guard/webui.py -- routes are exercised through FastAPI's
TestClient with app.dependency_overrides, never against a real network,
browser or subprocess. All identity data is the shared FAKE_* fixtures from
conftest.py.
"""
import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from broker_guard import freeze as freeze_mod
from broker_guard import profile as profile_mod
from broker_guard import state as state_mod
from broker_guard import webui
from broker_guard.config import Config

from conftest import FAKE_CITY, FAKE_EMAIL, FAKE_FIRST, FAKE_LAST, FAKE_STATE


@pytest.fixture(autouse=True)
def _clear_overrides():
    webui.app.dependency_overrides.clear()
    yield
    webui.app.dependency_overrides.clear()


@pytest.fixture
def cfg(tmp_path, profile_file, brokers_file):
    return Config(
        profile_path=profile_file,
        brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"),
        log_dir=str(tmp_path / "logs"),
        id_documents_dir=str(tmp_path / "id_documents"),
        freeze_state_path=str(tmp_path / "freeze_state.json"),
        crypto_key=Fernet.generate_key().decode("ascii"),
    )


@pytest.fixture
def client(cfg):
    # A single shared dict instance for the whole test, NOT `lambda: {}`
    # (that would hand every request a fresh empty dict and /scan's job
    # would never be found by a later /status poll).
    job_store: dict = {}
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: job_store
    return TestClient(webui.app)


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Broker Guard" in resp.text


def test_brokers_page_reads_real_state_path_not_hardcoded_one(client, cfg):
    """Regression: the pre-fix draft hardcoded data/presence.sqlite3, which
    never matched cfg.state_path. Writing through state_mod at cfg.state_path
    must be what /brokers renders."""
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance("idkey1", "alpha", "2026-01-01T00:00:00+00:00")
        store.set_status("idkey1", "alpha", "submitted", "2026-01-02T00:00:00+00:00")
    finally:
        conn.close()

    resp = client.get("/brokers")
    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "submitted" in resp.text


def test_identity_get_empty_profile_does_not_crash(tmp_path, brokers_file):
    cfg = Config(
        profile_path=str(tmp_path / "missing_profile.json"),
        brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"),
        log_dir=str(tmp_path / "logs"),
    )
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    client = TestClient(webui.app)
    resp = client.get("/identity")
    assert resp.status_code == 200


def test_identity_post_writes_validated_profile_to_configured_path(client, cfg):
    resp = client.post(
        "/identity",
        data={
            "first_name": FAKE_FIRST,
            "middle_name": "",
            "last_name": FAKE_LAST,
            "emails": FAKE_EMAIL,
            "phones": "",
            "addresses": f"{FAKE_CITY}, {FAKE_STATE}",
            "eraser_profile": "my-eraser-id",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved = profile_mod.load_profile(cfg.profile_path)
    assert saved.first_name == FAKE_FIRST
    assert saved.eraser_profile == "my-eraser-id"  # not dropped


def test_identity_post_rejects_invalid_without_touching_existing_profile(client, cfg):
    before = open(cfg.profile_path, encoding="utf-8").read()

    # A whitespace-only name: present (so FastAPI's Form(...) required-field
    # check is satisfied) but rejected by profile.load_profile's own
    # `not name.strip()` validation, which is the path this test exercises.
    resp = client.post(
        "/identity",
        data={"first_name": "   ", "last_name": FAKE_LAST},
    )
    assert resp.status_code == 400

    after = open(cfg.profile_path, encoding="utf-8").read()
    assert before == after  # untouched by the rejected submission


def test_identity_post_preserves_eraser_profile_round_trip(client, cfg):
    client.post(
        "/identity",
        data={"first_name": FAKE_FIRST, "last_name": FAKE_LAST, "eraser_profile": "abc123"},
        follow_redirects=False,
    )
    html_page = client.get("/identity").text
    assert 'value="abc123"' in html_page


def test_id_document_upload_requires_crypto_key(tmp_path, profile_file, brokers_file):
    cfg = Config(
        profile_path=profile_file, brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"), log_dir=str(tmp_path / "logs"),
        id_documents_dir=str(tmp_path / "id_documents"), crypto_key=None,
    )
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    client = TestClient(webui.app)

    resp = client.post(
        "/identity/id-document",
        data={"side": "front"},
        files={"file": ("id.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert resp.status_code == 500


def test_id_document_upload_stores_encrypted_not_plaintext(client, cfg):
    raw = b"totally-a-real-id-photo"
    resp = client.post(
        "/identity/id-document",
        data={"side": "front"},
        files={"file": ("id.jpg", raw, "image/jpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"side": "front", "stored": True}

    import os

    dest = os.path.join(cfg.id_documents_dir, "front.enc")
    on_disk = open(dest, encoding="utf-8").read()
    assert b"totally-a-real-id-photo" not in on_disk.encode("utf-8")

    import base64

    from broker_guard.crypto import decrypt_field

    decrypted = base64.b64decode(decrypt_field(on_disk, cfg.crypto_key.encode("utf-8")))
    assert decrypted == raw


def test_id_document_rejects_bad_side(client):
    resp = client.post(
        "/identity/id-document",
        data={"side": "sideways"},
        files={"file": ("id.jpg", b"x", "image/jpeg")},
    )
    assert resp.status_code == 400


def test_scan_returns_job_id_and_status_reaches_done(client):
    resp = client.post("/scan")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"

    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = client.get("/status", params={"job_id": job_id}).json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status
    assert "current" in status["result"]


def test_status_unknown_job_id_is_404(client):
    resp = client.get("/status", params={"job_id": "does-not-exist"})
    assert resp.status_code == 404


def test_status_without_job_id_surfaces_broker_status_table(client, cfg):
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance("idkey1", "beta", "2026-01-01T00:00:00+00:00")
    finally:
        conn.close()

    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert any(row["broker_id"] == "beta" for row in body["brokers"])
    assert body["pending_removals"] >= 1  # seen, no removal submitted yet


class _FakeBridge:
    def __init__(self, available=True, success=True):
        self._available = available
        self._success = success
        self.calls = []

    def available(self):
        return self._available

    def submit_removal(self, broker_id, profile):
        self.calls.append((broker_id, profile))
        return {"success": self._success, "detail": "ok" if self._success else "nope",
                "timed_out": False, "broker_id": broker_id, "dry_run": True}


def test_manual_remove_submits_and_updates_broker_status(client, cfg):
    fake = _FakeBridge(available=True, success=True)
    webui.app.dependency_overrides[webui.get_eraser_bridge] = lambda: fake

    resp = client.post("/brokers/alpha/remove")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert fake.calls and fake.calls[0][0] == "alpha"

    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        identity_key = profile_mod.load_profile(cfg.profile_path).identity_key
        assert store.get_status(identity_key, "alpha") == "submitted"
    finally:
        conn.close()


def test_manual_remove_when_eraser_unavailable_is_503(client):
    webui.app.dependency_overrides[webui.get_eraser_bridge] = lambda: _FakeBridge(available=False)
    resp = client.post("/brokers/alpha/remove")
    assert resp.status_code == 503


class _FakeExposureClient:
    def __init__(self, breaches):
        self.breaches = breaches

    def check_email(self, email):
        return self.breaches.get(email, [])


def test_exposure_page_renders_breaches_and_attribution(client):
    webui.app.dependency_overrides[webui.get_exposure_client] = lambda: _FakeExposureClient(
        {FAKE_EMAIL: ["SomeBreach2019"]}
    )
    resp = client.get("/exposure")
    assert resp.status_code == 200
    assert "SomeBreach2019" in resp.text
    assert "XposedOrNot" in resp.text  # attribution line present


def test_freeze_page_flags_needs_verification_bureaus(client):
    resp = client.get("/freeze")
    assert resp.status_code == 200
    assert "NEEDS VERIFICATION" in resp.text
    # Equifax must NOT be flagged the same way.
    equifax_idx = resp.text.index("Equifax")
    innovis_idx = resp.text.index("Innovis")
    equifax_row_end = resp.text.index("</tr>", equifax_idx)
    assert "NEEDS VERIFICATION" not in resp.text[equifax_idx:equifax_row_end]
    innovis_row_end = resp.text.index("</tr>", innovis_idx)
    assert "NEEDS VERIFICATION" in resp.text[innovis_idx:innovis_row_end]


def test_freeze_status_transition_valid_then_invalid(client, cfg):
    resp = client.post("/freeze/equifax/status", data={"new_status": freeze_mod.STATUS_PENDING})
    assert resp.status_code == 200
    assert resp.json()["status"] == freeze_mod.STATUS_PENDING

    # not_started/pending -> thawed directly is not a valid transition.
    resp = client.post("/freeze/equifax/status", data={"new_status": freeze_mod.STATUS_THAWED})
    assert resp.status_code == 400


def test_freeze_status_unknown_bureau_404(client):
    resp = client.post("/freeze/not-a-bureau/status", data={"new_status": freeze_mod.STATUS_PENDING})
    assert resp.status_code == 404


def test_freeze_pin_set_requires_crypto_key(tmp_path, profile_file, brokers_file):
    cfg = Config(
        profile_path=profile_file, brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"), log_dir=str(tmp_path / "logs"),
        freeze_state_path=str(tmp_path / "freeze_state.json"), crypto_key=None,
    )
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    client = TestClient(webui.app)
    resp = client.post("/freeze/equifax/pin", data={"pin": "1234"})
    assert resp.status_code == 500


def test_freeze_pin_set_then_has_pin_true_no_plaintext_route(client, cfg):
    resp = client.post("/freeze/equifax/pin", data={"pin": "1234"})
    assert resp.status_code == 200
    assert resp.json() == {"bureau_key": "equifax", "has_pin": True}

    identity_key = profile_mod.load_profile(cfg.profile_path).identity_key
    states = freeze_mod.load_freeze_states(cfg.freeze_state_path, identity_key)
    assert states["equifax"].has_pin()
    # The stored token is never the plaintext PIN.
    assert states["equifax"].pin_token != "1234"


# --- dashboard + brokers-page redesign: real data, not decoration ----------

def test_index_dashboard_shows_real_stat_chips_and_kind_breakdown(client, cfg):
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance("idkey1", "alpha", "2026-01-01T00:00:00+00:00")
        store.set_status("idkey1", "alpha", "submitted", "2026-01-02T00:00:00+00:00")
        store.record_appearance("idkey1", "gamma", "2026-01-01T00:00:00+00:00")
        store.set_status("idkey1", "gamma", "needs_document", "2026-01-02T00:00:00+00:00")
    finally:
        conn.close()

    resp = client.get("/")
    assert resp.status_code == 200
    # 2 tracked, 1 action-needed (gamma is needs_document) -- both real counts.
    assert ">2<" in resp.text
    assert ">1<" in resp.text
    assert "No scan has run yet" in resp.text


def test_index_dashboard_reflects_heartbeat_scan_status(client, cfg):
    import json as _json
    import os as _os

    _os.makedirs(cfg.log_dir, exist_ok=True)
    with open(_os.path.join(cfg.log_dir, "heartbeat.json"), "w", encoding="utf-8") as fh:
        _json.dump({"last_run": "2026-01-01T00:00:00+00:00", "ok": True}, fh)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "2026-01-01T00:00:00" in resp.text
    assert "ok" in resp.text


def test_brokers_page_shows_stepper_labels(client, cfg):
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance("idkey1", "alpha", "2026-01-01T00:00:00+00:00")
    finally:
        conn.close()

    resp = client.get("/brokers")
    assert resp.status_code == 200
    assert "Scanned" in resp.text
    assert "Removal submitted" in resp.text
    assert "Data removed" in resp.text
    assert "Next scan" in resp.text


def test_brokers_page_action_needed_hash_filter_script_present(client):
    resp = client.get("/brokers")
    assert "action-needed" in resp.text


# --- identity email/phone textarea validation + normalization --------------

def test_identity_post_dedupes_emails_case_insensitively(client, cfg):
    resp = client.post(
        "/identity",
        data={
            "first_name": FAKE_FIRST, "last_name": FAKE_LAST,
            "emails": f"{FAKE_EMAIL}\n{FAKE_EMAIL.upper()}\n",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = profile_mod.load_profile(cfg.profile_path)
    assert saved.emails == [FAKE_EMAIL]


def test_identity_post_rejects_malformed_email_without_touching_existing_profile(client, cfg):
    before = open(cfg.profile_path, encoding="utf-8").read()
    resp = client.post(
        "/identity",
        data={"first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": "not-an-email"},
    )
    assert resp.status_code == 400
    after = open(cfg.profile_path, encoding="utf-8").read()
    assert before == after


def test_identity_post_normalizes_phone_to_hyphenated_us_format(client, cfg):
    resp = client.post(
        "/identity",
        data={
            "first_name": FAKE_FIRST, "last_name": FAKE_LAST,
            "phones": "(555) 010-0100\n1-555-010-0101\n",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = profile_mod.load_profile(cfg.profile_path)
    assert saved.phones == ["+1-555-010-0100", "+1-555-010-0101"]


def test_normalize_phone_leaves_non_10_digit_input_alone():
    assert webui.normalize_phone("+44 20 7946 0958") == "+44 20 7946 0958"


# --- /exposure 500 regression: cache path must come from Config -----------

def test_get_exposure_client_uses_configured_cache_path_not_hardcoded_default(cfg, tmp_path):
    cfg.exposure_cache_path = str(tmp_path / "custom_exposure_cache.json")
    client_dep = webui.get_exposure_client(cfg)
    assert client_dep.cache.path == cfg.exposure_cache_path


def test_exposure_page_does_not_500_when_cache_dir_is_fresh(client, cfg, tmp_path):
    # Regression for the live bug: a brand-new deployment has no
    # exposure_cache.json and no parent dir for it yet -- this must never
    # 500. Points cfg.exposure_cache_path at a nested tmp_path directory
    # that does not exist yet, so ExposureCache._save() must create it.
    import os as _os

    cfg.exposure_cache_path = str(tmp_path / "fresh_data_dir" / "exposure_cache.json")
    assert not _os.path.exists(cfg.exposure_cache_path)
    resp = client.get("/exposure")
    assert resp.status_code == 200


# --- /profiles : CRUD + eraser-sync (v1 scope) ------------------------------

def test_profiles_page_empty_state(client):
    resp = client.get("/profiles")
    assert resp.status_code == 200
    assert "No profiles yet" in resp.text


def test_profiles_add_edit_remove_round_trip(client, cfg, tmp_path):
    eraser_cfg_path = tmp_path / "eraser-config.yaml"
    profiles_path = tmp_path / "profiles.json"
    cfg.eraser_config_path = str(eraser_cfg_path)
    cfg.profiles_path = str(profiles_path)

    resp = client.post(
        "/profiles",
        data={"first_name": "Jane", "last_name": "Doe", "emails": FAKE_EMAIL},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from broker_guard import profiles as profiles_mod

    saved = profiles_mod.load_profiles(str(profiles_path))
    assert len(saved) == 1
    profile_id = saved[0].id
    assert profile_id == "jane-doe"

    # eraser's config.yaml got the same profile synced into it.
    import yaml as _yaml

    with open(eraser_cfg_path, "r", encoding="utf-8") as fh:
        eraser_cfg = _yaml.safe_load(fh)
    assert any(p["id"] == "jane-doe" for p in eraser_cfg["profiles"])

    resp = client.post(f"/profiles/{profile_id}", data={
        "first_name": "Jane", "last_name": "Doe", "emails": FAKE_EMAIL, "phones": "555 010 0100",
    }, follow_redirects=False)
    assert resp.status_code == 303
    updated = profiles_mod.get_profile(str(profiles_path), profile_id)
    assert updated.id == profile_id  # id never changes on edit
    assert updated.phones == ["+1-555-010-0100"]

    resp = client.post(f"/profiles/{profile_id}/remove", follow_redirects=False)
    assert resp.status_code == 303
    assert profiles_mod.load_profiles(str(profiles_path)) == []

    # Removal is mirrored into eraser's list too, but that sync never
    # touches eraser's history.db -- this route has no history store to
    # even reach, by design (see profiles.py's module docstring).
    with open(eraser_cfg_path, "r", encoding="utf-8") as fh:
        eraser_cfg = _yaml.safe_load(fh)
    assert eraser_cfg["profiles"] == []


def test_profiles_edit_unknown_id_is_404(client):
    resp = client.get("/profiles/does-not-exist/edit")
    assert resp.status_code == 404
