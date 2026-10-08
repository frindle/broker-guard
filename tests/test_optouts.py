"""The opt-out state machine, its scan hooks, retry/backoff, re-verify, the
daily digest, ntfy delivery and the /optouts page.

Fakes only: an in-memory sqlite db, a fake requests session, a recording
notifier. No network, no browser.
"""
import pytest
from fastapi.testclient import TestClient

from broker_guard import autopilot, notify, optouts, review, sinks, state, webui
from broker_guard.config import Config

T0 = "2026-10-01T00:00:00+00:00"
IK = "ik-1"


@pytest.fixture
def store(tmp_path):
    return state.StateStore.open(str(tmp_path / "s.sqlite"))


def rec(outcome, **kw):
    base = {"id": "rid-" + outcome, "broker_id": "b1", "identity_key": IK,
            "outcome": outcome, "fields": {}, "reason": "r"}
    base.update(kw)
    return base


# --- transitions -------------------------------------------------------------

def test_submitted_without_email_confirmation_is_submitted(store):
    st = store.optouts.record_attempt(IK, rec("submitted", fields={"a": "b"}), T0)
    assert st == optouts.SUBMITTED
    row = store.optouts.get(IK, "b1")
    assert row["submitted_at"] == T0 and row["verify_after"].startswith("2026-11-30")


def test_submitted_with_emailed_link_awaits_user(store):
    st = store.optouts.record_attempt(
        IK, rec("submitted"), T0, confirm_expected=True, expected_sender="x.com")
    assert st == optouts.AWAITING_USER_CONFIRM
    assert store.optouts.get(IK, "b1")["expected_sender"] == "x.com"


def test_user_confirm_moves_to_submitted_and_only_from_awaiting(store):
    o = store.optouts
    o.record_attempt(IK, rec("submitted"), T0)
    assert o.mark_user_confirmed(IK, "b1", T0) is False      # not awaiting
    o.record_attempt(IK, rec("submitted"), T0, confirm_expected=True)
    assert o.mark_user_confirmed(IK, "b1", T0) is True
    assert o.get(IK, "b1")["state"] == optouts.SUBMITTED
    assert o.get(IK, "b1")["user_confirmed_at"] == T0


def test_illegal_transition_is_refused_and_row_untouched(store):
    o = store.optouts
    o.record_attempt(IK, rec("submitted"), T0)               # submitted
    with pytest.raises(optouts.OptoutStateError):
        o._set(IK, "b1", T0, optouts.QUEUED)                 # submitted -> queued
    assert o.get(IK, "b1")["state"] == optouts.SUBMITTED


def test_captcha_stop_needs_user(store):
    st = store.optouts.record_attempt(IK, rec("needs_manual_action"), T0)
    assert st == optouts.NEEDS_USER


def test_failed_before_send_backs_off_exponentially_then_needs_user(store):
    o = store.optouts
    seen = []
    for i in range(optouts.MAX_ATTEMPTS - 1):
        assert o.record_attempt(IK, rec("failed"), T0) == optouts.FAILED
        row = o.get(IK, "b1")
        seen.append(optouts.backoff_seconds(row["attempts"]))
        assert row["next_attempt_at"] == optouts._plus(T0, seen[-1])
    assert seen == sorted(seen) and seen[0] == 6 * 3600 and seen[1] == 12 * 3600
    assert o.record_attempt(IK, rec("failed"), T0) == optouts.NEEDS_USER


def test_failed_after_fields_were_sent_is_never_auto_retried(store):
    st = store.optouts.record_attempt(IK, rec("failed", fields={"Email": "x"}), T0)
    assert st == optouts.NEEDS_USER
    assert not store.optouts.is_due(IK, "b1", "2999-01-01T00:00:00+00:00")


def test_dry_run_is_remembered_so_it_is_not_repeated_until_live(store):
    o = store.optouts
    o.record_attempt(IK, rec("dry_run", dry_run=True), T0)
    assert o.get(IK, "b1")["state"] == optouts.QUEUED
    assert o.is_due(IK, "b1", T0, dry_run=True) is False
    assert o.is_due(IK, "b1", T0, dry_run=False) is True     # Penn went live


def test_backoff_gates_due(store):
    o = store.optouts
    o.record_attempt(IK, rec("failed"), T0)
    assert o.is_due(IK, "b1", optouts._plus(T0, 3600)) is False
    assert o.is_due(IK, "b1", optouts._plus(T0, 6 * 3600)) is True


# --- scan hooks: forget -> removed, record_appearance -> relisted -------------

def test_forget_credits_a_sent_request_as_removed_then_reappearance_relists(store):
    store.record_appearance(IK, "b1", T0)
    store.optouts.record_attempt(IK, rec("submitted"), T0)
    store.forget(IK, "b1")
    row = store.optouts.get(IK, "b1")
    assert row["state"] == optouts.REMOVED and row["removed_at"]
    store.record_appearance(IK, "b1", "2026-12-01T00:00:00+00:00")
    row = store.optouts.get(IK, "b1")
    assert row["state"] == optouts.RELISTED and row["relist_count"] == 1
    assert store.optouts.is_due(IK, "b1", T0) is True


def test_listing_vanishing_before_any_request_is_not_credited(store):
    store.record_appearance(IK, "b1", T0)
    store.optouts.ensure_queued(IK, "b1", T0)
    store.forget(IK, "b1")
    assert store.optouts.get(IK, "b1")["state"] == optouts.QUEUED


def test_first_appearance_creates_no_optout_row(store):
    store.record_appearance(IK, "b9", T0)
    assert store.optouts.get(IK, "b9") is None


# --- 60-day re-verify -----------------------------------------------------------

def test_reverify_relists_a_still_listed_broker_and_credits_an_absent_one(store):
    o = store.optouts
    o.record_attempt(IK, rec("submitted", broker_id="still"), T0)
    o.record_attempt(IK, rec("submitted", broker_id="gone"), T0)
    later = optouts._plus(T0, 61 * 86400)
    out = o.reverify(IK, later, lambda bid: bid == "still")
    assert out == {"relisted": ["still"], "removed": ["gone"]}
    assert o.get(IK, "still")["state"] == optouts.RELISTED
    assert o.get(IK, "gone")["state"] == optouts.REMOVED
    assert o.get(IK, "gone")["verify_after"] > later          # next check pushed out


def test_reverify_before_the_date_does_nothing(store):
    o = store.optouts
    o.record_attempt(IK, rec("submitted"), T0)
    assert o.reverify(IK, optouts._plus(T0, 30 * 86400), lambda b: True) == {
        "relisted": [], "removed": []}


# --- review import --------------------------------------------------------------

def test_import_is_idempotent_and_takes_newest_per_pair(store):
    o = store.optouts
    newest_first = [rec("submitted", id="new"), rec("failed", id="old")]
    assert o.import_review_records(newest_first, T0) == 1
    assert o.get(IK, "b1")["state"] == optouts.SUBMITTED
    assert o.import_review_records(newest_first, T0) == 0


# --- events / counts --------------------------------------------------------------

def test_events_trail_records_each_state_change(store):
    o = store.optouts
    o.record_attempt(IK, rec("submitted"), T0, confirm_expected=True)
    o.mark_user_confirmed(IK, "b1", T0)
    trail = [(e["from_state"], e["to_state"]) for e in o.events(IK, "b1")]
    assert trail == [(None, "queued"), ("queued", "awaiting_user_confirm"),
                     ("awaiting_user_confirm", "submitted")]
    assert o.counts()["submitted"] == 1


# --- the autopilot pass -----------------------------------------------------------

class Rec:
    def __init__(self):
        self.calls = []

    def state_changed(self, kind, row, detail=""):
        self.calls.append((kind, row["broker_id"]))
        return 1

    def digest(self, rows):
        self.calls.append(("digest", len(rows)))
        return 1


def _pass(monkeypatch, store, outcome, notifier, cfg=None, brokers=None):
    from broker_guard import optout_forms, optout_submit

    monkeypatch.setattr(review, "load_attempts", lambda d: [])
    monkeypatch.setattr(optout_forms, "supported_broker_ids", lambda: ["advancedbackgroundchecks-com"])
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, i, c: {"id": "r1", "broker_id": b, "outcome": outcome,
                                         "fields": {"x": "y"} if outcome == "submitted" else {}})
    ident = type("I", (), {"identity_key": IK})()
    return autopilot.run_optout_submission_pass(
        [ident], cfg or Config(optout_submit_dry_run=False), store=store,
        notifier=notifier, brokers=brokers, now=T0)


def test_pass_records_state_and_notifies_awaiting_with_sender(monkeypatch, store):
    n = Rec()
    counts = _pass(monkeypatch, store, "submitted", n,
                   brokers=[{"id": "advancedbackgroundchecks-com", "url": "https://www.abgc.example"}])
    row = store.optouts.get(IK, "advancedbackgroundchecks-com")
    assert row["state"] == optouts.AWAITING_USER_CONFIRM
    assert row["expected_sender"] == "abgc.example"
    assert n.calls == [("awaiting_user_confirm", "advancedbackgroundchecks-com")]
    assert counts["attempted"] == 1 and counts["notified"] == 1


def test_pass_notifies_on_captcha_stop_once(monkeypatch, store):
    n = Rec()
    _pass(monkeypatch, store, "needs_manual_action", n)
    assert n.calls == [("needs_user", "advancedbackgroundchecks-com")]


def test_pass_does_not_rerun_a_submitted_broker(monkeypatch, store):
    n = Rec()
    _pass(monkeypatch, store, "submitted", n)
    counts = _pass(monkeypatch, store, "submitted", n)
    assert counts["attempted"] == 0 and counts["skipped_existing"] == 1


def test_pass_reverify_notifies_relisted(monkeypatch, store):
    n = Rec()
    store.optouts.record_attempt(IK, rec("submitted", broker_id="zzz"), T0)
    store.record_appearance(IK, "zzz", T0)
    from broker_guard import optout_forms, optout_submit
    monkeypatch.setattr(review, "load_attempts", lambda d: [])
    monkeypatch.setattr(optout_forms, "supported_broker_ids", lambda: [])
    ident = type("I", (), {"identity_key": IK})()
    counts = autopilot.run_optout_submission_pass(
        [ident], Config(), store=store, notifier=n, now=optouts._plus(T0, 90 * 86400))
    assert counts["relisted"] == 1 and n.calls == [("relisted", "zzz")]


# --- digest -------------------------------------------------------------------------

def test_digest_lists_awaiting_items_with_sender_domain_once_a_day(store):
    store.optouts.record_attempt(IK, rec("submitted"), T0, confirm_expected=True,
                                 expected_sender="mail.example")
    sent = []
    notifier = notify.OptoutNotifier([lambda n: sent.append(n) or True],
                                     base_url="https://bg.example", names={"b1": "Broker One"})
    cfg = Config()
    assert autopilot.run_optout_digest(cfg, store=store, notifier=notifier, now=T0) == 1
    assert "Broker One" in sent[0]["message"] and "mail.example" in sent[0]["message"]
    assert sent[0]["ntfy"]["click"] == "https://bg.example/optouts"
    # within the day: nothing more
    assert autopilot.run_optout_digest(cfg, store=store, notifier=notifier,
                                       now=optouts._plus(T0, 3600)) == 0
    assert autopilot.run_optout_digest(cfg, store=store, notifier=notifier,
                                       now=optouts._plus(T0, 86400)) == 1


def test_digest_is_silent_when_nothing_awaits(store):
    sent = []
    notifier = notify.OptoutNotifier([lambda n: sent.append(n) or True])
    assert autopilot.run_optout_digest(Config(), store=store, notifier=notifier, now=T0) == 0
    assert sent == []


def test_digest_not_marked_sent_when_delivery_fails(store):
    store.optouts.record_attempt(IK, rec("submitted"), T0, confirm_expected=True)
    notifier = notify.OptoutNotifier([lambda n: False])
    assert autopilot.run_optout_digest(Config(), store=store, notifier=notifier, now=T0) == 0
    assert store.optouts.get_meta("last_digest_at") is None


# --- notifications -----------------------------------------------------------------

def test_notification_deep_links_to_the_attempt_record():
    row = {"broker_id": "b1", "last_attempt_id": "abc123", "state": "needs_user",
           "last_reason": "bot check"}
    n = notify.build_notification("needs_user", row, "Broker One", "https://bg.example/")
    assert n["ntfy"]["click"] == "https://bg.example/review#attempt-abc123"
    assert "Broker One" in n["title"]


def test_notification_has_no_link_without_a_public_url():
    n = notify.build_notification("needs_user", {"broker_id": "b1"}, "B", None)
    assert "click" not in n["ntfy"]


def test_notification_never_carries_profile_values():
    row = {"broker_id": "b1", "last_reason": "r", "state": "needs_user"}
    n = notify.build_notification("needs_user", row, "B", "https://x")
    assert set(n) == {"kind", "title", "message", "broker_id", "ntfy"}


class FakeSession:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, headers))
        return type("R", (), {"status_code": self.status})()


def test_ntfy_sink_posts_json_with_click_priority_and_bearer():
    sess = FakeSession()
    sink = sinks.NtfySink("http://ntfy.local/", "topic-x", token="tok", session=sess)
    ok = sink({"title": "T", "message": "M",
               "ntfy": {"priority": 4, "tags": ["warning"], "click": "https://bg/r"}})
    assert ok
    url, body, headers = sess.calls[0]
    assert url == "http://ntfy.local"
    assert body == {"topic": "topic-x", "title": "T", "message": "M", "priority": 4,
                    "tags": ["warning"], "click": "https://bg/r"}
    assert headers["Authorization"] == "Bearer tok"


def test_ntfy_sink_failure_returns_false_after_retries():
    sess = FakeSession(status=500)
    sink = sinks.NtfySink("http://n", "t", session=sess, attempts=2, sleep=lambda s: None)
    assert sink({"title": "T", "message": "M"}) is False
    assert len(sess.calls) == 2


def test_ntfy_needs_url_and_topic():
    assert sinks.build_ntfy_sink(Config(ntfy_url="http://n")) is None
    assert sinks.build_ntfy_sink(Config(ntfy_topic="t")) is None
    assert sinks.build_ntfy_sink(Config(ntfy_url="http://n", ntfy_topic="t")) is not None


def test_alert_sink_includes_ntfy_when_configured(tmp_path):
    cfg = Config(alert_log_path=str(tmp_path / "a.jsonl"), ntfy_url="http://n", ntfy_topic="t")
    assert any(isinstance(s, sinks.NtfySink) for s in sinks.build_alert_sink(cfg).sinks)


# --- the /optouts page ----------------------------------------------------------------

@pytest.fixture
def client(tmp_path, brokers_file):
    cfg = Config(brokers_path=brokers_file, state_path=str(tmp_path / "w.sqlite"),
                 settings_path=str(tmp_path / "set.json"), review_dir=str(tmp_path / "rv"))
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    yield TestClient(webui.app), cfg
    webui.app.dependency_overrides.clear()


def _seed_web(cfg, outcome, **kw):
    s = state.StateStore.open(cfg.state_path)
    s.optouts.record_attempt(IK, rec(outcome), T0, **kw)
    s.close()


def test_optouts_page_lists_awaiting_with_confirm_button(client):
    c, cfg = client
    _seed_web(cfg, "submitted", confirm_expected=True, expected_sender="mail.example")
    html = c.get("/optouts").text
    assert "awaiting user confirm" in html and "mail.example" in html
    assert "/confirm" in html


def test_confirm_route_advances_state(client):
    c, cfg = client
    _seed_web(cfg, "submitted", confirm_expected=True)
    r = c.post("/optouts/{}/b1/confirm".format(IK), follow_redirects=False)
    assert r.status_code == 303
    s = state.StateStore.open(cfg.state_path)
    assert s.optouts.get(IK, "b1")["state"] == optouts.SUBMITTED


def test_confirm_route_409_when_not_awaiting_and_404_when_unknown(client):
    c, cfg = client
    _seed_web(cfg, "submitted")
    assert c.post("/optouts/{}/b1/confirm".format(IK)).status_code == 409
    assert c.post("/optouts/{}/nope/confirm".format(IK)).status_code == 404


def test_retry_route_requeues_a_needs_user_item(client):
    c, cfg = client
    _seed_web(cfg, "needs_manual_action")
    assert c.post("/optouts/{}/b1/retry".format(IK), follow_redirects=False).status_code == 303
    s = state.StateStore.open(cfg.state_path)
    assert s.optouts.get(IK, "b1")["state"] == optouts.QUEUED


def test_scheduled_pass_runs_on_the_live_config_not_the_boot_env(monkeypatch, tmp_path,
                                                               profile_file, brokers_file):
    """/settings flips optout_submit_enabled; the loop must see it without a
    container restart (it used to be handed the boot-time env Config)."""
    import threading
    from broker_guard import settings as settings_mod

    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"),
                 settings_path=str(tmp_path / "set.json"), optout_submit_enabled=False)
    settings_mod.update_settings(cfg.settings_path, {"optout_submit_enabled": True})
    seen = []
    monkeypatch.setattr(autopilot, "run_scan_cycles", lambda *a, **k: {
        "failed_profiles": [], "current": [], "new_appearances": [], "profiles_scanned": 0,
        "identity_keys": [], "stopped": False, "retried_pairs": 0, "unresolved_pairs": 0,
        "results": {}, "detection_errors": 0, "errors": {}})
    monkeypatch.setattr(autopilot, "run_confirmation_pass", lambda *a, **k: None)
    monkeypatch.setattr(autopilot, "run_optout_digest", lambda *a, **k: 0)
    monkeypatch.setattr(autopilot, "run_optout_submission_pass",
                        lambda ids, c, **k: seen.append(c.optout_submit_enabled))
    stop = threading.Event()
    autopilot.run_forever(cfg, autopilot.AutopilotDependencies(), autopilot.Intervals(),
                          stop, sleep=lambda s: stop.set())
    assert seen == [True]
