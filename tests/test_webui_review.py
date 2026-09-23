"""The /review page and the on-demand opt-out run button.

Routes go through FastAPI's TestClient with ``app.dependency_overrides``,
and ``optout_submit.run_attempt`` is monkeypatched in every test that
presses a button -- no browser is ever launched and no broker's form is
ever contacted from this suite.

All identity data is the shared FAKE_* fixtures from conftest.py.
"""
import json
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from broker_guard import optout_submit, review, state as state_mod, webui
from broker_guard.config import Config

from conftest import FAKE_EMAIL, FAKE_FIRST


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
        profiles_path=str(tmp_path / "profiles.json"),
        settings_path=str(tmp_path / "settings.json"),
        eraser_config_path=str(tmp_path / "eraser-config.yaml"),
        review_dir=str(tmp_path / "review"),
        crypto_key=Fernet.generate_key().decode("ascii"),
        optout_submit_enabled=True,
        optout_submit_dry_run=True,
    )


@pytest.fixture
def client(cfg):
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {}
    return TestClient(webui.app)


def seed(cfg, outcome=review.OUTCOME_DRY_RUN, screenshot=b"PNGBYTES", **extra):
    ts = datetime(2026, 9, 22, 18, 42, 33, tzinfo=timezone.utc)
    rid = review.attempt_id("consumer-canvas-llc", "key", ts.isoformat())
    record = {
        "id": rid,
        "basename": review.basename_for("consumer-canvas-llc", ts, rid),
        "broker_id": "consumer-canvas-llc",
        "broker_name": "CONSUMER CANVAS LLC",
        "outcome": outcome,
        "started_at": ts.isoformat(),
        "dry_run": outcome == review.OUTCOME_DRY_RUN,
        "fields": {"First Name": FAKE_FIRST, "Email": FAKE_EMAIL},
    }
    record.update(extra)
    return review.save_attempt(review.review_dir(cfg), record, screenshot)


# --- the page ----------------------------------------------------------------

def test_review_page_renders_when_empty(client):
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "No opt-out submission has been attempted yet" in resp.text


def test_review_page_lists_an_attempt(client, cfg):
    seed(cfg)
    resp = client.get("/review")
    assert "CONSUMER CANVAS LLC" in resp.text
    assert "dry run" in resp.text
    assert FAKE_FIRST in resp.text     # Penn's own audit trail, shown to Penn


def test_review_page_is_in_the_nav(client):
    assert 'href="/review"' in client.get("/").text


def test_page_says_when_submission_is_off(cfg):
    cfg.optout_submit_enabled = False
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {}

    text = TestClient(webui.app).get("/review").text
    assert "off" in text
    assert "disabled" in text          # both buttons unusable


def test_page_warns_when_submission_is_live(cfg):
    cfg.optout_submit_dry_run = False
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {}

    assert "LIVE" in TestClient(webui.app).get("/review").text


def test_live_button_asks_for_confirmation(client):
    """A real third-party submission does not happen on a single stray click."""
    assert "confirm(" in client.get("/review").text


def test_only_allow_listed_brokers_get_a_button(client):
    text = client.get("/review").text
    assert "CONSUMER CANVAS LLC" in text
    assert "Nielsen" in text                  # allow-listed since 2026-09-22
    # A broker with a dataset entry but no hand-verified recipe stays off the
    # page: presence in the 970-entry dataset is NOT what grants a button.
    assert "Allant Group" not in text


# --- screenshots -------------------------------------------------------------

def test_screenshot_is_served(client, cfg):
    saved = seed(cfg)
    resp = client.get("/review/{}/screenshot".format(saved["id"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"PNGBYTES"


def test_unknown_attempt_screenshot_is_404(client):
    assert client.get("/review/deadbeef/screenshot").status_code == 404


def test_attempt_without_a_screenshot_is_404(client, cfg):
    saved = seed(cfg, screenshot=None)
    assert client.get("/review/{}/screenshot".format(saved["id"])).status_code == 404


def test_a_crafted_id_cannot_walk_out_of_the_review_folder(client, cfg):
    """The filename comes from the record, never from the URL."""
    resp = client.get("/review/..%2F..%2Fetc%2Fpasswd/screenshot")
    assert resp.status_code == 404


def test_a_record_naming_a_file_elsewhere_cannot_read_it(client, cfg, tmp_path):
    """The served filename is basenamed, so a record cannot point out of the folder.

    The decoy is a file that really exists one level up, so dropping the
    ``os.path.basename`` would genuinely serve it -- a traversal target that
    does not exist would make this test pass either way.
    """
    decoy = tmp_path / "secret.txt"
    decoy.write_bytes(b"TOP SECRET")

    seed(cfg, screenshot=b"X")
    directory = review.review_dir(cfg)
    name = [n for n in os.listdir(directory) if n.endswith(".json")][0]
    path = os.path.join(directory, name)
    record = json.loads(open(path).read())
    record["screenshot"] = "../secret.txt"
    open(path, "w").write(json.dumps(record))

    resp = client.get("/review/{}/screenshot".format(record["id"]))
    assert resp.status_code == 404
    assert b"TOP SECRET" not in resp.content


# --- the run button ----------------------------------------------------------

def test_dry_run_button_forces_dry_run(client, cfg, monkeypatch):
    seen = {}

    def fake_run(broker_id, identity, config, dry_run=None):
        seen["broker_id"] = broker_id
        seen["dry_run"] = dry_run
        return {"outcome": review.OUTCOME_DRY_RUN, "id": "x"}

    monkeypatch.setattr(optout_submit, "run_attempt", fake_run)
    resp = client.post("/review/run", data={"broker_id": "consumer-canvas-llc",
                                            "mode": "dry"}, follow_redirects=False)

    assert resp.status_code == 303
    assert seen == {"broker_id": "consumer-canvas-llc", "dry_run": True}


def test_live_mode_defers_to_the_configured_setting(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, i, c, dry_run=None: seen.update(dry_run=dry_run)
                        or {"outcome": review.OUTCOME_DRY_RUN, "id": "x"})

    client.post("/review/run", data={"broker_id": "consumer-canvas-llc",
                                     "mode": "live"}, follow_redirects=False)
    assert seen["dry_run"] is None


@pytest.mark.parametrize("mode", ["", "DRY", "whatever", "liv"])
def test_an_unrecognized_mode_falls_back_to_dry_run(client, monkeypatch, mode):
    """The safe direction, always."""
    seen = {}
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, i, c, dry_run=None: seen.update(dry_run=dry_run)
                        or {"outcome": review.OUTCOME_DRY_RUN, "id": "x"})

    client.post("/review/run", data={"broker_id": "consumer-canvas-llc",
                                     "mode": mode}, follow_redirects=False)
    assert seen["dry_run"] is True


def test_a_broker_with_no_recipe_is_404(client):
    resp = client.post("/review/run", data={"broker_id": "allant-group"},
                       follow_redirects=False)
    assert resp.status_code == 404


def test_a_refused_attempt_is_409_not_a_500(client, monkeypatch):
    def refuse(*a, **kw):
        raise optout_submit.SubmissionRefused("it is off")

    monkeypatch.setattr(optout_submit, "run_attempt", refuse)
    resp = client.post("/review/run", data={"broker_id": "consumer-canvas-llc"},
                       follow_redirects=False)
    assert resp.status_code == 409


# --- surfacing a bail-out the way manual actions already surface -------------

def test_a_captcha_bailout_lights_up_the_existing_action_needed_badge(
        client, cfg, monkeypatch):
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, i, c, dry_run=None: {
                            "outcome": review.OUTCOME_NEEDS_MANUAL, "id": "x"})

    client.post("/review/run", data={"broker_id": "consumer-canvas-llc"},
                follow_redirects=False)

    conn = state_mod.init_db(cfg.state_path)
    try:
        identity = webui.profile_mod.load_profile(cfg.profile_path)
        status = state_mod.StateStore(conn).get_status(
            identity.identity_key, "consumer-canvas-llc")
    finally:
        conn.close()
    assert status == "needs_review"
    assert webui._action_needed_count([{"removal_status": status}]) == 1


def test_a_dry_run_does_not_move_the_brokers_real_status(client, cfg, monkeypatch):
    """A rehearsal must not change what the dashboard says about a broker."""
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, i, c, dry_run=None: {
                            "outcome": review.OUTCOME_DRY_RUN, "id": "x"})

    client.post("/review/run", data={"broker_id": "consumer-canvas-llc"},
                follow_redirects=False)

    conn = state_mod.init_db(cfg.state_path)
    try:
        identity = webui.profile_mod.load_profile(cfg.profile_path)
        status = state_mod.StateStore(conn).get_status(
            identity.identity_key, "consumer-canvas-llc")
    finally:
        conn.close()
    assert status is None


@pytest.mark.parametrize("outcome,expected", [
    (review.OUTCOME_NEEDS_MANUAL, "needs_review"),
    (review.OUTCOME_FAILED, "needs_review"),
    (review.OUTCOME_DRY_RUN, None),
    (review.OUTCOME_SUBMITTED, None),
])
def test_status_for_outcome_adds_no_new_vocabulary(outcome, expected):
    assert review.status_for_outcome(outcome) == expected


# --- settings page -----------------------------------------------------------

def test_both_switches_appear_on_the_settings_page(client):
    text = client.get("/settings").text
    assert "Automated opt-out submission" in text
    assert "DRY RUN" in text


def test_the_settings_form_actually_saves_the_switches(client, cfg):
    from broker_guard import settings as settings_mod

    resp = client.post("/settings", data={
        "playwright_enabled": "", "searxng_url": "", "searxng_min_interval_s": "2.0",
        "searxng_jitter_s": "1.0", "alert_webhook_url": "", "eraser_enabled": "",
        "eraser_dry_run": "on", "optout_submit_enabled": "on",
        "optout_submit_dry_run": "on", "captcha_api_key": "",
        "interval_seconds": "86400",
    }, follow_redirects=False)

    assert resp.status_code == 303
    stored = settings_mod.load_settings(cfg.settings_path)
    assert stored["optout_submit_enabled"] is True
    assert stored["optout_submit_dry_run"] is True
