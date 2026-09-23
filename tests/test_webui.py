"""Tests for broker_guard/webui.py -- routes are exercised through FastAPI's
TestClient with app.dependency_overrides, never against a real network,
browser or subprocess. All identity data is the shared FAKE_* fixtures from
conftest.py.
"""
import json
import os
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from broker_guard import freeze as freeze_mod
from broker_guard import profile as profile_mod
from broker_guard import profiles as profiles_mod
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
        # Pinned to tmp_path, NOT left on Config's data/profiles.json
        # default: /identity now migrates-and-writes the profiles list, so
        # an unpinned path would have tests writing real PII-shaped files
        # into the repo's data/ directory.
        profiles_path=str(tmp_path / "profiles.json"),
        # Same reasoning as profiles_path above: /settings WRITES this file,
        # so an unpinned path would have tests dropping a real settings.json
        # into the repo's data/ directory -- and, worse, would make every
        # other test's effective config depend on whatever a previous local
        # run happened to leave there.
        settings_path=str(tmp_path / "settings.json"),
        eraser_config_path=str(tmp_path / "eraser-config.yaml"),
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
        profiles_path=str(tmp_path / "profiles.json"),
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
    # The primary profile's eraser id is on its EDIT card now, not in a
    # standalone "active profile" form at the top of the page -- that form
    # went away with the active-profile concept.
    saved = profiles_mod.load_profiles(cfg.profiles_path)
    assert [p.eraser_profile for p in saved] == ["abc123"]
    html_page = client.get("/identity?edit=" + saved[0].id).text
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
    # A manual scan is a whole-household sweep now (service.run_all), so
    # the job result is per-identity rather than one flat cycle result.
    result = status["result"]
    assert result["identities"], "the sweep must report who it scanned"
    assert result["stopped"] is False
    for key in result["identities"]:
        assert "current" in result["results"][key["identity_key"]]


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
    assert "2026-01-01 00:00 UTC" in resp.text
    assert "2026-01-01T00:00:00" not in resp.text
    assert "ok" in resp.text


def test_format_scan_timestamp_renders_human_readable_utc():
    assert webui.format_scan_timestamp("2026-09-22T14:32:07.481932+00:00") == "2026-09-22 14:32 UTC"


def test_format_scan_timestamp_passes_through_unparseable_and_none():
    assert webui.format_scan_timestamp("not-a-timestamp") == "not-a-timestamp"
    assert webui.format_scan_timestamp(None) is None
    assert webui.format_scan_timestamp("") == ""


def test_index_dashboard_shows_running_during_autopilot_cycle(client, cfg):
    """A heartbeat written mid-cycle (status=running, from
    autopilot.run_forever) must show "in progress", not "No scan has run
    yet" -- the dashboard's only signal that the background loop, not just
    a manual /scan job, is actually working."""
    import json as _json
    import os as _os

    _os.makedirs(cfg.log_dir, exist_ok=True)
    with open(_os.path.join(cfg.log_dir, "heartbeat.json"), "w", encoding="utf-8") as fh:
        _json.dump({"last_run": "2026-01-01T00:00:00+00:00", "status": "running"}, fh)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Scan in progress right now" in resp.text
    assert "No scan has run yet" not in resp.text


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

def test_profiles_url_redirects_to_the_merged_identity_page(client):
    """/identity IS the profiles page now -- /profiles is a thin alias for
    old bookmarks, not a second, divergent identity UI."""
    resp = client.get("/profiles", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/identity"


def test_identity_page_empty_state_when_there_is_nothing_to_migrate(tmp_path, brokers_file):
    cfg = Config(
        profile_path=str(tmp_path / "missing_profile.json"),
        brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"),
        log_dir=str(tmp_path / "logs"),
        profiles_path=str(tmp_path / "profiles.json"),
    )
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    resp = TestClient(webui.app).get("/identity")
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


# --- the merged Identity page: one surface for "who am I" -------------------

def test_identity_page_migrates_an_existing_legacy_profile_into_the_list(client, cfg):
    """A deployment that already had a populated profile.local.json before
    this merge shipped must see that identity in the list automatically --
    without re-entering it."""
    assert profiles_mod.load_profiles(cfg.profiles_path) == []

    resp = client.get("/identity")
    assert resp.status_code == 200

    migrated = profiles_mod.load_profiles(cfg.profiles_path)
    assert len(migrated) == 1
    assert migrated[0].first_name == FAKE_FIRST
    assert migrated[0].last_name == FAKE_LAST
    # ...and it is listed as an ordinary profile. There is no "Active
    # profile" form pinned above the list any more: no profile is
    # privileged, so nothing gets pinned.
    assert FAKE_FIRST in resp.text
    assert "Active profile" not in resp.text
    assert "Make active" not in resp.text


def test_identity_migration_runs_once_and_does_not_duplicate(client, cfg):
    client.get("/identity")
    client.get("/identity")
    assert len(profiles_mod.load_profiles(cfg.profiles_path)) == 1


def test_identity_post_upserts_the_primary_profile_in_the_list(client, cfg):
    """The bug this merge fixes: a save on the Profile page used to touch
    only profile.local.json, so it never showed up in the profiles list."""
    client.get("/identity")  # migrate the legacy profile in
    original = profiles_mod.load_profiles(cfg.profiles_path)[0]

    resp = client.post("/identity", data={
        "first_name": FAKE_FIRST, "middle_name": "", "last_name": FAKE_LAST,
        "emails": "updated@example.invalid", "phones": "", "addresses": "",
        "eraser_profile": "",
    }, follow_redirects=False)
    assert resp.status_code == 303

    saved = profiles_mod.load_profiles(cfg.profiles_path)
    assert len(saved) == 1, "a save must UPDATE the first profile, not append a new one"
    assert saved[0].id == original.id, "the profile id is immutable"
    assert saved[0].emails == ["updated@example.invalid"]
    # and the legacy compatibility mirror the single-identity path reads got it too
    assert profile_mod.load_profile(cfg.profile_path).emails == ["updated@example.invalid"]


def test_identity_post_with_no_profiles_yet_creates_one(client, cfg, tmp_path):
    cfg.profile_path = str(tmp_path / "fresh_profile.local.json")
    resp = client.post("/identity", data={
        "first_name": "Jane", "middle_name": "", "last_name": "Doe",
        "emails": FAKE_EMAIL, "phones": "", "addresses": "", "eraser_profile": "",
    }, follow_redirects=False)
    assert resp.status_code == 303

    saved = profiles_mod.load_profiles(cfg.profiles_path)
    assert [p.id for p in saved] == ["jane-doe"]


def test_adding_a_profile_leaves_the_others_and_the_legacy_mirror_alone(client, cfg):
    """Adding a second person must not repoint anything: both are scanned,
    and the legacy mirror still tracks the FIRST entry."""
    client.get("/identity")  # migrates the legacy profile in
    first = profiles_mod.load_profiles(cfg.profiles_path)[0]

    resp = client.post("/profiles", data={
        "first_name": "Jane", "last_name": "Doe", "emails": FAKE_EMAIL,
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/identity"

    assert [p.id for p in profiles_mod.load_profiles(cfg.profiles_path)] == [
        first.id, "jane-doe"]
    assert profile_mod.load_profile(cfg.profile_path).first_name == FAKE_FIRST


def test_both_profiles_are_scanned_with_no_activation_step(client, cfg):
    """The feature this change exists for: two saved profiles means two
    identities swept, without anyone having to make one 'active'."""
    client.get("/identity")
    client.post("/profiles", data={"first_name": "Jane", "last_name": "Doe"})

    identities = profiles_mod.load_scan_identities(cfg.profiles_path, cfg.profile_path)
    assert {(i.first_name, i.last_name) for i in identities} == {
        (FAKE_FIRST, FAKE_LAST), ("Jane", "Doe")}


def test_the_activate_route_is_gone(client, cfg):
    """Regression guard: the endpoint was removed, not just unlinked from
    the page -- an old bookmark must not silently re-privilege a profile."""
    client.get("/identity")
    resp = client.post("/profiles/jane-doe/activate")
    assert resp.status_code in (404, 405)


def test_removing_a_profile_resyncs_the_legacy_mirror_without_promoting(client, cfg):
    client.get("/identity")
    first = profiles_mod.load_profiles(cfg.profiles_path)[0]
    client.post("/profiles", data={"first_name": "Jane", "last_name": "Doe"})

    resp = client.post(f"/profiles/{first.id}/remove", follow_redirects=False)
    assert resp.status_code == 303

    remaining = profiles_mod.load_profiles(cfg.profiles_path)
    assert [p.id for p in remaining] == ["jane-doe"]
    # Nothing was "promoted" -- the mirror simply follows whoever is first.
    assert profile_mod.load_profile(cfg.profile_path).first_name == "Jane"


def test_editing_a_non_active_profile_leaves_the_legacy_file_alone(client, cfg):
    from broker_guard import profiles as profiles_mod

    client.get("/identity")
    client.post("/profiles", data={"first_name": "Jane", "last_name": "Doe"})

    resp = client.post("/profiles/jane-doe", data={
        "first_name": "Jane", "last_name": "Doe", "emails": "jane@example.invalid",
    }, follow_redirects=False)
    assert resp.status_code == 303

    assert profiles_mod.get_profile(cfg.profiles_path, "jane-doe").emails == ["jane@example.invalid"]
    # The scan loop still reads the ACTIVE profile, untouched by that edit.
    assert profile_mod.load_profile(cfg.profile_path).first_name == FAKE_FIRST


def test_identity_edit_query_param_renders_that_profile_in_place(client, cfg):
    client.get("/identity")
    client.post("/profiles", data={"first_name": "Jane", "last_name": "Doe"})

    resp = client.get("/identity?edit=jane-doe")
    assert resp.status_code == 200
    assert 'action="/profiles/jane-doe"' in resp.text
    assert "Edit profile -- Jane Doe" in resp.text


def test_identity_edit_query_param_unknown_id_is_404(client):
    client.get("/identity")
    resp = client.get("/identity?edit=does-not-exist")
    assert resp.status_code == 404


def test_old_profiles_edit_url_redirects_into_the_merged_page(client):
    client.get("/identity")
    client.post("/profiles", data={"first_name": "Jane", "last_name": "Doe"})
    resp = client.get("/profiles/jane-doe/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/identity?edit=jane-doe"


def test_nav_has_a_single_identity_entry(client):
    """Two nav tabs ("Profile" and "Profiles") is what made people think
    there were two places to manage identity."""
    from broker_guard import webui_style

    keys = [key for key, _href, _label in webui_style.NAV_ITEMS]
    assert "identity" in keys
    assert "profiles" not in keys


# --- dashboard: "Run scan now" reflects a scan already in flight -----------

def test_run_scan_button_renders_disabled_when_a_scan_is_already_running(cfg):
    """Regression: the #scanline text said "Scan in progress right now."
    but the button still rendered plain/enabled, so returning to / mid-scan
    looked identical to nothing happening."""
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {"job-1": {"status": "running"}}
    resp = TestClient(webui.app).get("/")

    assert resp.status_code == 200
    assert "Scan in progress right now." in resp.text
    assert 'id="runScanBtn" onclick="runScanNow()" disabled data-scan-running="1">Scanning...' in resp.text


def test_run_scan_button_is_enabled_when_no_scan_is_running(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="runScanBtn" onclick="runScanNow()">Run scan now' in resp.text
    assert 'data-scan-running="1"' not in resp.text


def test_running_scan_page_polls_the_jobs_summary_branch_of_status(cfg):
    """The page-load poll has no job_id to poll (the job was started from a
    page we navigated away from), so it must use /status's jobs summary."""
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {"job-1": {"status": "running"}}
    resp = TestClient(webui.app).get("/")

    assert "fetch('/status')" in resp.text
    assert "location.reload()" in resp.text


def test_autopilot_only_scan_attaches_the_poll_and_status_agrees_it_is_running(cfg, tmp_path):
    """An autopilot background cycle (heartbeat status=running, empty jobs
    map) must now BOTH disable the button and attach the poll.

    Previously the poll was withheld here: it decided "is it still running?"
    from /status's jobs summary, which is empty during an autopilot cycle,
    so it would have concluded "finished" and reloaded in a loop. The poll
    now reads /status's `scan.running` -- the server's own view, which
    covers the autopilot thread -- so the live counter works in exactly the
    case it was previously unavailable. This asserts the reload-loop is
    still impossible, by checking /status reports running: True here."""
    os.makedirs(cfg.log_dir, exist_ok=True)
    with open(os.path.join(cfg.log_dir, "heartbeat.json"), "w", encoding="utf-8") as fh:
        json.dump({"status": "running", "last_run": "2026-01-01T00:00:00+00:00", "ok": True}, fh)

    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {}
    client = TestClient(webui.app)
    resp = client.get("/")

    assert "Scan in progress right now." in resp.text
    assert 'id="runScanBtn" onclick="runScanNow()" disabled data-scan-running="1">Scanning...' \
        in resp.text

    # The poll's own source of truth says "still running" -- so it keeps
    # polling instead of reloading forever.
    scan = client.get("/status").json()["scan"]
    assert scan["running"] is True
    assert scan["line"] == "Scan in progress right now."


# --- /brokers: per-broker scan results, per identity -------------------------
#
# The gap these cover: /brokers listed ONLY brokers with a presence row --
# brokers the person was found on -- so watching a live scan tick from 0/827
# to 7/827 with no hits showed literally nothing new on this page. "Checked,
# nothing found" was never recorded per broker anywhere.

from broker_guard import progress as progress_mod  # noqa: E402


@pytest.fixture
def clean_progress():
    progress_mod.current().clear()
    yield progress_mod.current()
    progress_mod.current().clear()


def _badge(label, tone):
    """The rendered BADGE, not just the word: every outcome name also
    appears in the filter dropdown, so asserting on the bare string would
    pass even if no row rendered it."""
    return '<span class="badge tone-{}">{}</span>'.format(tone, label)


def _deployment_identity_key(cfg):
    """The identity_key /brokers will actually render rows for.

    Every saved profile is scanned now, and a deployment whose profiles
    list is still empty gets its legacy profile.local.json migrated in on
    page load -- so the rows belong to THAT identity, not to an arbitrary
    made-up key. A test planting scan outcomes has to plant them under the
    same key or it is asserting on rows that were never going to match.
    """
    return profile_mod.load_profile(cfg.profile_path).identity_key


def _record_scan(progress, identity_key, outcomes):
    progress.begin_cycle(identity_key=identity_key, total=3)
    progress.start(progress_mod.PHASE_SERP, 3)
    for broker_id, outcome in outcomes.items():
        progress.record_outcome(broker_id, outcome,
                                1 if outcome == "hit" else 0,
                                1 if outcome == "error" else 0)
    progress.finish()


def test_brokers_page_lists_every_broker_not_only_the_found_ones(client, clean_progress):
    resp = client.get("/brokers")
    assert resp.status_code == 200
    # All three roster brokers appear even though none has a presence row.
    for name in ("Alpha People", "Beta Search", "Gamma Records"):
        assert name in resp.text
    assert resp.text.count(_badge("Not yet checked", "neutral")) == 3


def test_brokers_page_renders_each_outcome_distinctly(client, cfg, clean_progress):
    _record_scan(clean_progress, _deployment_identity_key(cfg),
                 {"alpha": "checked", "beta": "hit"})
    resp = client.get("/brokers")
    assert _badge("Checked -- clean", "success") in resp.text
    assert _badge("Listing found", "escalated") in resp.text
    # gamma was never reached -- and must not read like a clean check.
    assert resp.text.count(_badge("Not yet checked", "neutral")) == 1


def test_a_failed_check_never_renders_as_clean(client, cfg, clean_progress):
    _record_scan(clean_progress, _deployment_identity_key(cfg), {"alpha": "error"})
    resp = client.get("/brokers")
    assert _badge("Check failed", "action") in resp.text
    assert _badge("Checked -- clean", "success") not in resp.text


def test_brokers_page_keeps_the_removal_tracking_view(client, cfg, clean_progress):
    """Additive, not a replacement: the presence-history rows, the status
    filter, the search box and the action-needed filter all stay."""
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance("idkey1", "alpha", "2026-01-01T00:00:00+00:00")
        store.set_status("idkey1", "alpha", "needs_review", "2026-01-02T00:00:00+00:00")
    finally:
        conn.close()

    resp = client.get("/brokers")
    assert ">needs_review</span>" in resp.text
    assert 'id="statusFilter"' in resp.text
    assert 'id="searchBox"' in resp.text
    assert 'id="actionOnly"' in resp.text
    assert "Scanned" in resp.text          # the lifecycle stepper
    assert 'id="scanRowsContainer"' in resp.text   # ...alongside the new card


def _two_profiles(cfg):
    a = profiles_mod.add_profile(cfg.profiles_path, {
        "first_name": FAKE_FIRST, "last_name": FAKE_LAST, "emails": [FAKE_EMAIL]})
    b = profiles_mod.add_profile(cfg.profiles_path, {
        "first_name": "Otherfirst", "last_name": "Otherlast", "emails": []})
    return a, b


def test_brokers_page_scopes_scan_results_to_the_selected_profile(client, cfg, clean_progress):
    a, b = _two_profiles(cfg)
    _record_scan(clean_progress, profiles_mod.identity_key(a), {"alpha": "hit"})

    mine = client.get("/brokers?identity=" + a.id)
    assert _badge("Listing found", "escalated") in mine.text

    theirs = client.get("/brokers?identity=" + b.id)
    # Profile B was never scanned -- its rows must not inherit A's results.
    assert _badge("Listing found", "escalated") not in theirs.text
    assert theirs.text.count(_badge("Not yet checked", "neutral")) == 3


def test_brokers_page_scopes_tracked_listings_to_the_selected_profile(client, cfg, clean_progress):
    a, b = _two_profiles(cfg)
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        store.record_appearance(profiles_mod.identity_key(a), "alpha", "2026-01-01T00:00:00+00:00")
        store.set_status(profiles_mod.identity_key(a), "alpha", "submitted",
                         "2026-01-02T00:00:00+00:00")
        store.record_appearance(profiles_mod.identity_key(b), "beta", "2026-01-03T00:00:00+00:00")
        store.set_status(profiles_mod.identity_key(b), "beta", "confirmed",
                         "2026-01-04T00:00:00+00:00")
    finally:
        conn.close()

    mine = client.get("/brokers?identity=" + a.id)
    assert ">submitted</span>" in mine.text
    assert ">confirmed</span>" not in mine.text

    # A profile that is NOT the active one still shows its own history --
    # that is persisted per identity_key and never merged away.
    theirs = client.get("/brokers?identity=" + b.id)
    assert ">confirmed</span>" in theirs.text
    assert ">submitted</span>" not in theirs.text


def test_brokers_page_offers_a_profile_picker_defaulting_to_all_profiles(client, cfg,
                                                                        clean_progress):
    """No profile is privileged any more, so the page opens on EVERY
    profile's results rather than picking one for you. Narrowing to a
    single person stays available; it is just no longer the default."""
    a, b = _two_profiles(cfg)
    resp = client.get("/brokers")
    assert 'id="identityPicker"' in resp.text
    assert '<option value="all" selected>All profiles</option>' in resp.text
    for profile in (a, b):
        assert '<option value="{}"'.format(profile.id) in resp.text
    assert "selected>" not in resp.text.split('value="all" selected')[1].split("</select>")[0]


def test_status_includes_the_per_broker_map_only_when_asked(client, clean_progress):
    _record_scan(clean_progress, "idkey1", {"alpha": "checked"})

    lean = client.get("/status").json()
    assert "brokers" not in lean["scan"]["progress"]

    full = client.get("/status?brokers=1").json()
    key = progress_mod.entry_key("idkey1", "alpha")
    assert full["scan"]["progress"]["brokers"][key]["outcome"] == "checked"
    assert "not yet checked" in full["scan"]["outcome_line"]


# --- /settings ---------------------------------------------------------------

SETTINGS_FORM = {
    "playwright_enabled": "false",
    "searxng_url": "",
    "searxng_min_interval_s": "2.0",
    "searxng_jitter_s": "1.0",
    "alert_webhook_url": "",
    "eraser_enabled": "false",
    "eraser_dry_run": "true",
    "captcha_api_key": "",
    "interval_seconds": "86400",
}


def test_settings_page_renders_every_setting_with_its_source(client, cfg):
    from broker_guard import settings as settings_mod

    resp = client.get("/settings")

    assert resp.status_code == 200
    for spec in settings_mod.SETTING_SPECS:
        assert spec.label in resp.text
        assert spec.env in resp.text          # the env var is named, not hidden
    # Nothing has been saved yet, so every row must say so rather than
    # implying the dashboard owns the value.
    assert "built-in default" in resp.text
    # The per-row source badge renders the bare tier name; none should say
    # "stored" yet. (The prose above the form mentions "saved here", so match
    # the badge itself rather than that phrase.)
    assert ">stored<" not in resp.text
    # BG_SERVE_WEB is the one deliberate omission (bootstrap paradox).
    assert "BG_SERVE_WEB" not in resp.text.split("Precedence")[0]


def test_settings_page_shows_stored_values_and_labels_them_stored(client, cfg):
    from broker_guard import settings as settings_mod

    settings_mod.update_settings(cfg.settings_path, {
        "playwright_enabled": True, "searxng_url": "http://searx.invalid:8080"})

    resp = client.get("/settings")

    assert "http://searx.invalid:8080" in resp.text
    assert ">stored<" in resp.text
    # ...and offers to drop the override and go back to the env var.
    assert 'name="reset" value="playwright_enabled"' in resp.text


def test_settings_post_persists_to_the_store(client, cfg):
    from broker_guard import settings as settings_mod

    resp = client.post("/settings", data={**SETTINGS_FORM,
                                          "playwright_enabled": "true",
                                          "searxng_url": "http://searx.invalid:8080",
                                          "interval_seconds": "3600"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=1"
    stored = settings_mod.load_settings(cfg.settings_path)
    assert stored["playwright_enabled"] is True
    assert stored["searxng_url"] == "http://searx.invalid:8080"
    assert stored["interval_seconds"] == 3600
    assert json.loads(open(cfg.settings_path, encoding="utf-8").read())["interval_seconds"] == 3600


def test_settings_post_rejects_a_bad_value_and_keeps_the_old_store(client, cfg):
    from broker_guard import settings as settings_mod

    client.post("/settings", data={**SETTINGS_FORM, "interval_seconds": "3600"},
                follow_redirects=False)

    for bad in ({"interval_seconds": "10"}, {"searxng_url": "nope"},
                {"searxng_min_interval_s": "-4"}):
        resp = client.post("/settings", data={**SETTINGS_FORM, **bad},
                           follow_redirects=False)
        assert resp.status_code == 400, bad

    assert settings_mod.load_settings(cfg.settings_path)["interval_seconds"] == 3600


def test_settings_reset_checkbox_removes_the_stored_override(client, cfg):
    from broker_guard import settings as settings_mod

    settings_mod.update_settings(cfg.settings_path, {"playwright_enabled": True})

    client.post("/settings", data={**SETTINGS_FORM, "reset": "playwright_enabled"},
                follow_redirects=False)

    assert "playwright_enabled" not in settings_mod.load_settings(cfg.settings_path)


def test_settings_never_renders_the_captcha_key_back_into_the_page(client, cfg):
    """Same rule as the credit-freeze PIN: a secret goes in, it never comes
    back out over HTTP. A blank secret field means 'keep the stored one', so
    saving the rest of the form cannot silently wipe the key either."""
    from broker_guard import settings as settings_mod

    client.post("/settings", data={**SETTINGS_FORM, "captcha_api_key": "super-secret-key"},
                follow_redirects=False)
    assert settings_mod.load_settings(cfg.settings_path)["captcha_api_key"] == "super-secret-key"

    resp = client.get("/settings")
    assert "super-secret-key" not in resp.text
    assert 'type="password"' in resp.text
    assert "leave blank to keep it" in resp.text

    # Save the form again with the secret field blank -> still stored.
    client.post("/settings", data=SETTINGS_FORM, follow_redirects=False)
    assert settings_mod.load_settings(cfg.settings_path)["captcha_api_key"] == "super-secret-key"


def test_settings_nav_entry_is_present_on_every_page(client):
    for path in ("/", "/brokers", "/identity", "/settings"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'href="/settings"' in resp.text, path


def test_get_config_overlays_the_stored_settings(tmp_path, monkeypatch):
    """The dependency every route uses must return the OVERLAID config, or a
    setting saved on /settings would not reach /scan, the eraser bridge or
    anything else that reads Config."""
    from broker_guard import settings as settings_mod

    store = str(tmp_path / "settings.json")
    monkeypatch.setenv("BG_SETTINGS_PATH", store)
    monkeypatch.setenv("BG_PLAYWRIGHT_ENABLED", "false")
    monkeypatch.setenv("BG_INTERVAL_SECONDS", "86400")

    assert webui.get_config().playwright_enabled is False

    settings_mod.update_settings(store, {"playwright_enabled": True,
                                         "interval_seconds": 3600})

    live = webui.get_config()
    assert live.playwright_enabled is True
    assert live.interval_seconds == 3600


# --- /brokers: per-profile rows, honest timestamps, sorting, stop ----------

def test_brokers_page_names_the_profiles_the_scan_checked(client, cfg, clean_progress):
    """Penn's ask: the page has to say WHO was checked, not just what was
    found -- and a saved profile the scan has not reached yet is named as
    such rather than being implied to be done."""
    a, b = _two_profiles(cfg)
    _record_scan(clean_progress, profiles_mod.identity_key(a), {"alpha": "checked"})

    text = client.get("/brokers").text
    assert "Profiles checked: {}".format(a.full_name) in text
    assert "Not yet reached this scan: {}".format(b.full_name) in text


def test_brokers_page_shows_one_row_per_profile_per_broker(client, cfg, clean_progress):
    a, b = _two_profiles(cfg)
    text = client.get("/brokers").text
    # 3 roster brokers x 2 profiles, and every row says whose it is.
    assert text.count('class="scanrow" data-broker-id=') == 6
    for profile in (a, b):
        assert 'data-profile="{}"'.format(profile.full_name.lower()) in text


def test_brokers_page_never_renders_a_raw_isoformat_timestamp(client, cfg, clean_progress):
    """The reported bug: cells showed 2026-09-22T21:45:02.742110+00:00.
    Every timestamp on the page goes through format_scan_timestamp now."""
    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        key = _deployment_identity_key(cfg)
        store.record_appearance(key, "alpha", "2026-09-22T21:45:02.742110+00:00")
        store.set_status(key, "alpha", "submitted", "2026-09-22T21:45:02.742110+00:00")
    finally:
        conn.close()

    text = client.get("/brokers").text
    assert "2026-09-22 21:45 UTC" in text
    # The raw form survives only in the data-updated sort key, never in a cell.
    for chunk in text.split("2026-09-22T21:45:02.742110+00:00")[:-1]:
        assert chunk.endswith('data-updated="'), "a raw isoformat leaked into the page"


def test_brokers_page_offers_a_last_update_column_sort(client, cfg, clean_progress):
    """Sorting is client side over data-updated, which holds the RAW ISO
    string -- it sorts correctly as text, so what is displayed and what is
    sorted on cannot drift apart."""
    text = client.get("/brokers").text
    assert "sortBy('rowsContainer','.brokerrow','updated'" in text
    assert "sortBy('scanRowsContainer','.scanrow','updated'" in text
    assert 'class="sortable"' in text
    assert "function sortBy(" in text


def test_scan_stop_requests_a_stop_without_pretending_one_was_running(client,
                                                                     clean_progress):
    """POST /scan/stop is idempotent and never 404s: the button can be
    clicked as a cycle is ending, and a stop nobody needed is not an
    error."""
    resp = client.post("/scan/stop")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_requested"] is True
    assert body["was_running"] is False
    assert progress_mod.current().should_stop() is True


def test_a_stopped_scan_is_reported_as_stopped_not_ok(client, cfg, clean_progress):
    """A stopped pass did not cover every broker, so calling it 'ok' would
    make the timestamp claim a full sweep that never happened."""
    os.makedirs(cfg.log_dir, exist_ok=True)
    with open(os.path.join(cfg.log_dir, "heartbeat.json"), "w", encoding="utf-8") as fh:
        json.dump({"last_run": "2026-09-22T21:45:02.742110+00:00", "ok": True,
                   "status": "done", "stopped": True}, fh)

    text = client.get("/").text
    assert "stopped early" in text
    assert "(ok)" not in text
