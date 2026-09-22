"""End-to-end tests for the wired pipeline.

These drive ``service.run_once`` (and ``service.main``) over the FULL chain --
profile -> brokers -> serpwatch -> playwright_checks -> state -> alert ->
eraser_bridge -- with every I/O boundary faked via dependency injection. No
network, no browser, no subprocess, no real PII.
"""
import json
import os

import pytest

from broker_guard import service
from broker_guard.config import load_config
from broker_guard.eraser_bridge import EraserBridge, EraserUnavailable
from broker_guard.searx_client import SearxClient
from broker_guard.sinks import CompositeAlertSink, FileAlertSink, WebhookAlertSink
from broker_guard.state import StateStore


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def make_searx(listed_domains, name="Testy Mctestface"):
    """A fake SearXNG returning a hit only for the listed broker domains."""
    def search(query):
        for domain in listed_domains:
            if "site:{}".format(domain) in query:
                return [{"title": name, "url": "https://{}/p/1".format(domain),
                         "content": "{} profile record".format(name)}]
        return []
    return search


def make_page_action(found_ids, errored_ids=()):
    def page_action(check):
        if check["broker_id"] in errored_ids:
            return {"error": "navigation failed"}
        return {"found": check["broker_id"] in found_ids}
    return page_action


class RecordingRemoval:
    def __init__(self, succeed=True):
        self.calls = []
        self.succeed = succeed

    def __call__(self, broker_id, profile):
        self.calls.append((broker_id, dict(profile)))
        return {"success": self.succeed, "broker_id": broker_id, "detail": "", "timed_out": False}


@pytest.fixture
def cfg(base_env):
    return load_config(base_env)


@pytest.fixture
def deps_factory(cfg):
    made = []

    def make(**overrides):
        sink = CompositeAlertSink([FileAlertSink(cfg.alert_log_path)])
        deps = service.Dependencies(
            store=StateStore.open(cfg.state_path),
            alert_sink=overrides.pop("alert_sink", sink),
            now=lambda: "2026-01-01T00:00:00+00:00",
            **overrides,
        )
        made.append(deps)
        return deps

    yield make
    for deps in made:
        deps.close()


# --------------------------------------------------------------------------
# end-to-end
# --------------------------------------------------------------------------

def test_full_cycle_detects_records_alerts_and_submits_removal(cfg, deps_factory):
    removal = RecordingRemoval()
    deps = deps_factory(searx_search=make_searx(["alpha.invalid"]), removal=removal)

    result = service.run_once(cfg, deps)

    assert result["current"] == ["alpha"]
    assert result["new_appearances"] == ["alpha"]
    assert result["resolved"] == []
    # state actually persisted
    assert deps.store.seen_brokers(result["identity_key"]) == ["alpha"]
    # alert actually written to the sink file
    lines = open(cfg.alert_log_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["counts"] == {"new_appearance": 1}
    # removal actually invoked, with the eraser profile shape (no raw PII keys)
    assert [c[0] for c in removal.calls] == ["alpha"]
    assert set(removal.calls[0][1]) == {"full_name", "eraser_profile"}
    # status recorded
    assert deps.store.get_status(result["identity_key"], "alpha") == "submitted"


def test_second_cycle_reports_no_new_appearance(cfg, deps_factory):
    searx = make_searx(["alpha.invalid"])
    deps = deps_factory(searx_search=searx)
    first = service.run_once(cfg, deps)
    assert first["new_appearances"] == ["alpha"]

    second = service.run_once(cfg, deps)
    assert second["current"] == ["alpha"]
    assert second["new_appearances"] == []
    assert second["resolved"] == []
    # only the first cycle should have alerted
    assert len(open(cfg.alert_log_path, encoding="utf-8").read().strip().splitlines()) == 1


def test_broker_disappearing_is_reported_as_resolved(cfg, deps_factory):
    deps = deps_factory(searx_search=make_searx(["alpha.invalid"]))
    service.run_once(cfg, deps)
    deps.searx_search = make_searx([])  # listing gone
    result = service.run_once(cfg, deps)
    assert result["current"] == [] and result["resolved"] == ["alpha"]


def test_serp_and_browser_sources_are_unioned(cfg, deps_factory):
    deps = deps_factory(
        searx_search=make_searx(["alpha.invalid"]),
        page_action=make_page_action({"beta"}),
    )
    result = service.run_once(cfg, deps)
    assert sorted(result["current"]) == ["alpha", "beta"]


def test_photo_id_broker_is_not_browser_checked(cfg, deps_factory):
    """gamma requires photo_id -> not automatable -> no browser check built."""
    seen = []

    def page_action(check):
        seen.append(check["broker_id"])
        return {"found": False}

    deps = deps_factory(searx_search=lambda q: [], page_action=page_action)
    service.run_once(cfg, deps)
    assert "gamma" not in seen and sorted(seen) == ["alpha", "beta"]


def test_browser_error_does_not_look_like_a_removal(cfg, deps_factory):
    """A broker known present, whose recheck errors, must NOT be 'resolved'."""
    deps = deps_factory(searx_search=make_searx(["alpha.invalid"]))
    service.run_once(cfg, deps)

    # Now SERP finds nothing and the browser check for alpha errors out.
    deps.searx_search = make_searx([])
    deps.page_action = make_page_action(set(), errored_ids={"alpha"})
    result = service.run_once(cfg, deps)
    # errored -> unknown, not absent: an outage must not be read as success.
    assert result["resolved"] == []
    assert [e["broker_id"] for e in result["errors"]] == ["alpha"]
    # and the stored presence row is still there for the next cycle
    assert deps.store.seen_brokers(result["identity_key"]) == ["alpha"]


def test_presence_checker_error_is_captured_not_raised(cfg, deps_factory):
    deps = deps_factory(searx_search=make_searx([]))
    checker_calls = []

    def exploding(broker, identity_key):
        checker_calls.append(broker["id"])
        raise RuntimeError("boom")

    identity_key = "k"
    from broker_guard.orchestrator import run_cycle
    result = run_cycle(identity_key, [{"id": "alpha"}], exploding, deps.store,
                       lambda p: None, "2026-01-01T00:00:00+00:00")
    assert result["current"] == [] and len(result["errors"]) == 1
    assert result["errors"][0]["broker_id"] == "alpha"


def test_removal_failure_leaves_status_pending(cfg, deps_factory):
    removal = RecordingRemoval(succeed=False)
    deps = deps_factory(searx_search=make_searx(["alpha.invalid"]), removal=removal)
    result = service.run_once(cfg, deps)
    assert deps.store.get_status(result["identity_key"], "alpha") == "pending"


def test_removal_raising_does_not_abort_the_cycle(cfg, deps_factory):
    def boom(broker_id, profile):
        raise OSError("eraser exploded")

    deps = deps_factory(searx_search=make_searx(["alpha.invalid"]), removal=boom)
    result = service.run_once(cfg, deps)
    assert result["removals"][0]["success"] is False
    assert "eraser exploded" in result["removals"][0]["detail"]


def test_cycle_runs_with_no_sources_configured(cfg, deps_factory):
    """Nothing configured -> a clean, empty cycle rather than a crash."""
    result = service.run_once(cfg, deps_factory())
    assert result["current"] == [] and result["new_appearances"] == []


# --------------------------------------------------------------------------
# main() / config gate
# --------------------------------------------------------------------------

def test_main_check_config_passes_with_valid_inputs(base_env, monkeypatch):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    assert service.main(["--check-config"]) == 0


def test_main_check_config_fails_on_missing_profile(base_env, monkeypatch, tmp_path):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BG_PROFILE_PATH", str(tmp_path / "nope.json"))
    assert service.main(["--check-config"]) == 2


def test_main_rejects_a_bad_interval(base_env, monkeypatch):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BG_INTERVAL_SECONDS", "5")
    assert service.main(["--check-config"]) == 2


def test_exposure_cache_and_profiles_paths_are_env_configurable(tmp_path):
    """New Config fields (exposure_cache_path, profiles_path,
    eraser_config_path) added for the /exposure 500 fix and the Profiles
    feature -- each has a real default and is overridable via its own
    BG_* env var, same convention as every other path field."""
    from broker_guard.config import DEFAULT_EXPOSURE_CACHE_PATH, DEFAULT_PROFILES_PATH

    default_cfg = load_config({})
    assert default_cfg.exposure_cache_path == DEFAULT_EXPOSURE_CACHE_PATH
    assert default_cfg.profiles_path == DEFAULT_PROFILES_PATH
    assert default_cfg.eraser_config_path.endswith(".eraser/config.yaml")

    custom_exposure = str(tmp_path / "custom_exposure.json")
    custom_profiles = str(tmp_path / "custom_profiles.json")
    custom_eraser = str(tmp_path / "custom_eraser.yaml")
    cfg = load_config({
        "BG_EXPOSURE_CACHE_PATH": custom_exposure,
        "BG_PROFILES_PATH": custom_profiles,
        "BG_ERASER_CONFIG_PATH": custom_eraser,
    })
    assert cfg.exposure_cache_path == custom_exposure
    assert cfg.profiles_path == custom_profiles
    assert cfg.eraser_config_path == custom_eraser


def test_main_runs_one_real_cycle_and_exits(base_env, monkeypatch, tmp_path):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    built = {}
    real_build = service.build_dependencies

    def fake_build(cfg):
        deps = service.Dependencies(
            store=StateStore.open(cfg.state_path),
            searx_search=make_searx(["alpha.invalid"]),
            alert_sink=CompositeAlertSink([FileAlertSink(cfg.alert_log_path)]),
        )
        built["deps"] = deps
        return deps

    monkeypatch.setattr(service, "build_dependencies", fake_build)
    assert service.main(["--once"]) == 0
    assert real_build is not None and "deps" in built

    # main() closes its store, so verify persistence by reopening the db.
    from broker_guard.profile import load_profile
    identity_key = load_profile(os.environ["BG_PROFILE_PATH"]).identity_key
    reopened = StateStore.open(os.environ["BG_STATE_PATH"])
    try:
        assert reopened.seen_brokers(identity_key) == ["alpha"]
    finally:
        reopened.close()

    # heartbeat written for the dead-man's switch
    hb = json.load(open(os.path.join(os.environ["BG_LOG_DIR"], "heartbeat.json")))
    assert hb["ok"] is True and hb["present"] == 1


def test_main_survives_a_cycle_that_raises(base_env, monkeypatch):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)

    def fake_build(cfg):
        return service.Dependencies(store=StateStore.open(cfg.state_path))

    def boom(cfg, deps):
        raise RuntimeError("cycle exploded")

    monkeypatch.setattr(service, "build_dependencies", fake_build)
    monkeypatch.setattr(service, "run_once", boom)
    # The loop must log and exit cleanly, not propagate.
    assert service.main(["--once"]) == 0


# --------------------------------------------------------------------------
# real I/O adapters, with the transport faked
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.requests.append({"url": url, "params": params, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None, headers=None):
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_searx_client_feeds_serpwatch_end_to_end(cfg, deps_factory):
    payload = {"results": [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p",
                            "content": "record"}]}
    session = FakeSession([FakeResponse(200, payload)] * 50)
    client = SearxClient("http://searx.invalid:8080", session=session, sleep=lambda _: None)
    deps = deps_factory(searx_search=client)
    result = service.run_once(cfg, deps)
    # Only alpha's own domain matches, even though every query returns the hit.
    assert result["current"] == ["alpha"]
    assert all(r["timeout"] == 20 for r in session.requests)
    assert all(r["url"] == "http://searx.invalid:8080/search" for r in session.requests)


def test_searx_client_returns_empty_on_total_failure_rather_than_raising():
    session = FakeSession([FakeResponse(503)] * 5)
    client = SearxClient("http://searx.invalid:8080", session=session, sleep=lambda _: None)
    assert client("anything") == []


def test_searx_client_never_retries_a_4xx():
    session = FakeSession([FakeResponse(403)])
    client = SearxClient("http://searx.invalid:8080", session=session, sleep=lambda _: None)
    assert client("anything") == []
    assert len(session.requests) == 1


def test_eraser_bridge_invokes_without_a_shell_and_times_out_safely():
    seen = {}

    class Completed:
        returncode = 0
        stdout = "sent 1 request"
        stderr = ""

    def runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return Completed()

    bridge = EraserBridge("eraser", timeout_s=7, dry_run=True, runner=runner)
    result = bridge.submit_removal("alpha", {"full_name": "Testy Mctestface"})
    assert result["success"] is True and result["dry_run"] is True
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 7
    assert isinstance(seen["cmd"], list)
    assert seen["cmd"][:4] == ["eraser", "send", "--broker", "alpha"]


def test_eraser_bridge_reports_a_timeout_instead_of_hanging():
    import subprocess

    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    bridge = EraserBridge(runner=runner, timeout_s=3)
    result = bridge.submit_removal("alpha", {})
    assert result["success"] is False and result["timed_out"] is True


def test_eraser_bridge_raises_a_clear_error_when_the_binary_is_missing():
    def runner(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(EraserUnavailable):
        EraserBridge(runner=runner).submit_removal("alpha", {})


def test_eraser_bridge_refuses_a_malicious_broker_id_before_spawning():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("must not be reached")

    with pytest.raises(ValueError):
        EraserBridge(runner=runner).submit_removal("alpha; rm -rf /", {})
    assert calls == []


def test_webhook_sink_posts_and_reports_failure_without_logging_the_url(caplog):
    session = FakeSession([FakeResponse(200)])
    sink = WebhookAlertSink("https://hook.invalid/secret-token", session=session,
                            sleep=lambda _: None)
    assert sink({"title": "x"}) is True
    assert session.requests[0]["url"] == "https://hook.invalid/secret-token"

    failing = FakeSession([FakeResponse(500)] * 5)
    sink2 = WebhookAlertSink("https://hook.invalid/secret-token", session=failing,
                             sleep=lambda _: None)
    with caplog.at_level("ERROR"):
        assert sink2({"title": "x"}) is False
    assert "secret-token" not in caplog.text


def test_composite_sink_survives_one_failing_sink(tmp_path):
    def bad(notification):
        raise OSError("disk full")

    path = tmp_path / "alerts.jsonl"
    composite = CompositeAlertSink([bad, FileAlertSink(str(path))])
    out = composite({"identity_key": "k", "new_appearances": ["alpha"], "now_iso": "t"})
    assert out["delivered"] == 1
    assert path.read_text(encoding="utf-8").strip()


def test_alert_file_is_owner_only(tmp_path):
    path = tmp_path / "alerts.jsonl"
    FileAlertSink(str(path))({"title": "x"})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
