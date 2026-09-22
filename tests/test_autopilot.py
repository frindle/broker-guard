"""Tests for broker_guard/autopilot.py -- the kind-aware decision layer and
the scan/confirmation loop. Everything here runs against fakes: no network,
no browser, no subprocess, no real clock (see module docstring's DI seams).
"""
import threading

import pytest

from broker_guard import autopilot
from broker_guard.profile import Identity


class FakeStore:
    """Minimal in-memory stand-in for state.StateStore."""

    def __init__(self):
        self.presence = {}   # (identity_key, broker_id) -> True
        self.status = {}     # (identity_key, broker_id) -> status str

    def is_seen(self, identity_key, broker_id):
        return (identity_key, broker_id) in self.presence

    def record_appearance(self, identity_key, broker_id, seen_at):
        self.presence[(identity_key, broker_id)] = True

    def touch(self, identity_key, broker_id, seen_at):
        self.presence[(identity_key, broker_id)] = True

    def forget(self, identity_key, broker_id):
        self.presence.pop((identity_key, broker_id), None)

    def seen_brokers(self, identity_key):
        return [bid for (idk, bid) in self.presence if idk == identity_key]

    def set_status(self, identity_key, broker_id, status, updated_at):
        self.status[(identity_key, broker_id)] = status

    def get_status(self, identity_key, broker_id):
        return self.status.get((identity_key, broker_id))


@pytest.fixture
def identity():
    return Identity(first_name="Testy", last_name="Mctestface")


@pytest.fixture
def brokers():
    return [
        {"id": "alpha", "name": "Alpha", "url": "https://alpha.invalid", "verification": "automatable"},
        {"id": "beta", "name": "Beta", "url": "https://beta.invalid", "verification": "captcha"},
        {"id": "gamma", "name": "Gamma", "url": "https://gamma.invalid", "verification": "photo_id"},
        {"id": "delta", "name": "Delta", "url": "https://delta.invalid", "verification": "kba"},
        {"id": "epsilon", "name": "Epsilon", "url": "https://epsilon.invalid"},  # no 'verification' key
    ]


def always_present(broker, identity_key):
    return True


def none_present(broker, identity_key):
    return False


class RecordingRemoval:
    def __init__(self, success=True):
        self.calls = []
        self.success = success

    def __call__(self, broker_id, profile):
        self.calls.append((broker_id, profile))
        return {"success": self.success, "detail": "ok", "broker_id": broker_id}


# --- decide_action ------------------------------------------------------------

@pytest.mark.parametrize("kind,expected_action,expected_status", [
    ("automatable", "auto_send", None),
    ("captcha", "auto_send", None),
    ("photo_id", "queue", autopilot.STATUS_NEEDS_DOCUMENT),
    ("kba", "queue", autopilot.STATUS_NEEDS_REVIEW),
    ("manual_review", "queue", autopilot.STATUS_NEEDS_REVIEW),  # unknown kind: fail safe
    ("", "queue", autopilot.STATUS_NEEDS_REVIEW),
])
def test_decide_action_table(kind, expected_action, expected_status):
    decision = autopilot.decide_action(kind)
    assert decision["action"] == expected_action
    assert decision["queue_status"] == expected_status


def test_decide_action_photo_id_queues_even_with_documents_on_file():
    """No documented per-broker eraser attach path exists (see
    eraser.build_eraser_fill_cmd) -- having ID docs on file does not change
    the outcome today. This test pins that as an intentional policy, not a
    bug, so a future accidental change is caught."""
    without_docs = autopilot.decide_action("photo_id", has_id_documents=False)
    with_docs = autopilot.decide_action("photo_id", has_id_documents=True)
    assert without_docs == with_docs == {"action": "queue", "queue_status": autopilot.STATUS_NEEDS_DOCUMENT}


# --- run_scan_cycle ------------------------------------------------------------

def test_scan_cycle_auto_sends_automatable_and_captcha(identity, brokers):
    store = FakeStore()
    removal = RecordingRemoval(success=True)
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=removal)

    result = autopilot.run_scan_cycle(identity, brokers, deps)

    sent_ids = {call[0] for call in removal.calls}
    # 'epsilon' has no 'verification' key; brokers.verification_kind defaults
    # that to 'automatable' (its own default, pinned separately below), so
    # it is sent too.
    assert sent_ids == {"alpha", "beta", "epsilon"}
    assert store.get_status(identity.identity_key, "alpha") == "submitted"
    assert store.get_status(identity.identity_key, "beta") == "submitted"
    assert result["decisions"]["alpha"]["action"] == "auto_send"
    assert result["decisions"]["beta"]["action"] == "auto_send"


def test_scan_cycle_queues_photo_id_and_kba_without_sending(identity, brokers):
    store = FakeStore()
    removal = RecordingRemoval(success=True)
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=removal)

    result = autopilot.run_scan_cycle(identity, brokers, deps)

    sent_ids = {call[0] for call in removal.calls}
    assert "gamma" not in sent_ids and "delta" not in sent_ids
    assert store.get_status(identity.identity_key, "gamma") == autopilot.STATUS_NEEDS_DOCUMENT
    assert store.get_status(identity.identity_key, "delta") == autopilot.STATUS_NEEDS_REVIEW
    assert result["decisions"]["gamma"]["action"] == "queue"
    assert result["decisions"]["delta"]["action"] == "queue"


def test_scan_cycle_never_silently_drops_unknown_kind(identity, brokers):
    """`epsilon` has no 'verification' key at all -- brokers.verification_kind
    defaults that to 'automatable', so it IS auto-sent; this test pins that
    behavior (brokers.py's own default, not autopilot's) rather than assuming
    it. The real "unknown kind" fail-safe is exercised directly in
    test_decide_action_table via a kind string decide_action has never seen."""
    store = FakeStore()
    removal = RecordingRemoval(success=True)
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=removal)

    autopilot.run_scan_cycle(identity, brokers, deps)

    # Every broker that appeared got EITHER sent or queued -- never neither.
    identity_key = identity.identity_key
    for broker in brokers:
        assert store.get_status(identity_key, broker["id"]) is not None


def test_scan_cycle_with_no_removal_engine_records_pending_not_a_fake_success(identity, brokers):
    store = FakeStore()
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=None)

    autopilot.run_scan_cycle(identity, [brokers[0]], deps)

    assert store.get_status(identity.identity_key, "alpha") == "pending"


def test_scan_cycle_removal_exception_does_not_crash_the_cycle(identity, brokers):
    def exploding_removal(broker_id, profile):
        raise RuntimeError("eraser exploded")

    store = FakeStore()
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=exploding_removal)

    result = autopilot.run_scan_cycle(identity, [brokers[0]], deps)

    assert result["decisions"]["alpha"]["action"] == "auto_send"
    # status_after_removal on a failed result leaves it at 'pending', not
    # a crash and not a silently-invented 'submitted'.
    assert store.get_status(identity.identity_key, "alpha") == "pending"


def test_scan_cycle_forgets_resolved_brokers_so_reappearance_is_detected(identity):
    broker = {"id": "alpha", "name": "Alpha", "url": "https://alpha.invalid", "verification": "automatable"}
    store = FakeStore()
    removal = RecordingRemoval(success=True)
    deps = autopilot.AutopilotDependencies(store=store, presence_checker=always_present, submit_removal=removal)

    # Cycle 1: broker appears -> auto-sent.
    result1 = autopilot.run_scan_cycle(identity, [broker], deps)
    assert result1["new_appearances"] == ["alpha"]
    assert store.is_seen(identity.identity_key, "alpha")

    # Cycle 2: broker no longer present -> resolved, and (this is the fix)
    # its presence row is forgotten.
    deps.presence_checker = none_present
    result2 = autopilot.run_scan_cycle(identity, [broker], deps)
    assert result2["resolved"] == ["alpha"]
    assert result2["forgotten"] == ["alpha"]
    assert not store.is_seen(identity.identity_key, "alpha")

    # Cycle 3: broker reappears -- WITHOUT the forget() fix this would be
    # silently swallowed as a touch() rather than surfaced as new.
    deps.presence_checker = always_present
    result3 = autopilot.run_scan_cycle(identity, [broker], deps)
    assert result3["new_appearances"] == ["alpha"]
    assert len(removal.calls) == 2  # sent again on reappearance


# --- run_confirmation_pass -----------------------------------------------------

def test_confirmation_pass_calls_monitor_then_status():
    calls = []
    deps = autopilot.AutopilotDependencies(
        eraser_monitor=lambda: calls.append("monitor") or {"success": True},
        eraser_status=lambda: calls.append("status") or {"success": True},
    )
    result = autopilot.run_confirmation_pass(deps)
    assert calls == ["monitor", "status"]
    assert result["monitor"] == {"success": True}
    assert result["status"] == {"success": True}


def test_confirmation_pass_none_when_eraser_disabled():
    deps = autopilot.AutopilotDependencies(eraser_monitor=None, eraser_status=None)
    result = autopilot.run_confirmation_pass(deps)
    assert result == {"monitor": None, "status": None}


def test_confirmation_pass_survives_exceptions():
    def exploding():
        raise RuntimeError("imap down")

    deps = autopilot.AutopilotDependencies(eraser_monitor=exploding, eraser_status=exploding)
    result = autopilot.run_confirmation_pass(deps)
    assert result["monitor"]["success"] is False
    assert result["status"]["success"] is False


# --- run_forever loop ticking ---------------------------------------------------

def test_run_forever_runs_scan_and_confirmation_immediately_on_start(monkeypatch, tmp_path, profile_file, brokers_file):
    from broker_guard.config import Config

    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"))
    calls = []
    monkeypatch.setattr(autopilot, "run_scan_cycle", lambda *a, **k: calls.append("scan"))
    monkeypatch.setattr(autopilot, "run_confirmation_pass", lambda *a, **k: calls.append("confirmation"))

    stop = threading.Event()

    def fake_sleep(seconds):
        stop.set()  # stop after the first tick -- deterministic, no real waiting

    deps = autopilot.AutopilotDependencies()
    autopilot.run_forever(cfg, deps, autopilot.Intervals(scan_seconds=100, confirmation_seconds=100),
                            stop, sleep=fake_sleep)

    assert calls == ["scan", "confirmation"]


def test_run_forever_respects_independent_confirmation_cadence(monkeypatch, tmp_path, profile_file, brokers_file):
    """confirmation_seconds shorter than scan_seconds -> confirmation runs on
    every tick, scan only once the longer interval has elapsed."""
    from broker_guard.config import Config

    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"))
    calls = []
    monkeypatch.setattr(autopilot, "run_scan_cycle", lambda *a, **k: calls.append("scan"))
    monkeypatch.setattr(autopilot, "run_confirmation_pass", lambda *a, **k: calls.append("confirmation"))

    stop = threading.Event()
    tick_count = {"n": 0}

    def fake_sleep(seconds):
        tick_count["n"] += 1
        if tick_count["n"] >= 3:
            stop.set()

    deps = autopilot.AutopilotDependencies()
    # tick = min(10, 300) = 10s; confirmation every tick (10s >= 10s always
    # true), scan only once 300s have elapsed -- within 3 ticks (30s) that
    # never happens again after the immediate start-up run.
    autopilot.run_forever(cfg, deps, autopilot.Intervals(scan_seconds=300, confirmation_seconds=10),
                            stop, sleep=fake_sleep)

    assert calls.count("scan") == 1  # only the immediate start-up run
    # start-up + 2 more ticks before the 3rd tick's stop.set() ends the loop
    # (the loop exits at the top, before doing a 4th round of work).
    assert calls.count("confirmation") == 3


def test_run_forever_one_bad_cycle_does_not_kill_the_loop(monkeypatch, tmp_path, profile_file, brokers_file):
    from broker_guard.config import Config

    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"))

    def exploding(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(autopilot, "run_scan_cycle", exploding)
    calls = []
    monkeypatch.setattr(autopilot, "run_confirmation_pass", lambda *a, **k: calls.append("confirmation"))

    stop = threading.Event()

    def fake_sleep(seconds):
        stop.set()

    deps = autopilot.AutopilotDependencies()
    # Must not raise -- a scan exception must never take down the loop.
    autopilot.run_forever(cfg, deps, autopilot.Intervals(scan_seconds=100, confirmation_seconds=100),
                            stop, sleep=fake_sleep)
    assert calls == ["confirmation"]


# --- has_id_documents_on_file ---------------------------------------------------

def test_has_id_documents_on_file_requires_both_sides(tmp_path):
    from broker_guard.config import Config

    cfg = Config(id_documents_dir=str(tmp_path / "docs"))
    assert autopilot.has_id_documents_on_file(cfg) is False

    import os

    os.makedirs(cfg.id_documents_dir, exist_ok=True)
    with open(os.path.join(cfg.id_documents_dir, "front.enc"), "w") as fh:
        fh.write("x")
    assert autopilot.has_id_documents_on_file(cfg) is False  # only one side

    with open(os.path.join(cfg.id_documents_dir, "back.enc"), "w") as fh:
        fh.write("x")
    assert autopilot.has_id_documents_on_file(cfg) is True
