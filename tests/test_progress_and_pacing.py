"""Live scan progress, honest error counting, and SearXNG request pacing.

Covers the three defects these features exist to fix:

1. the dashboard could only say "Scan in progress right now." for the whole
   (hour-plus) duration of a cycle;
2. a failed search was silently counted as "checked, found nothing", so a
   total SearXNG outage reported as a clean bill of health; and
3. nothing spaced out successive SearXNG requests, so one cycle fired
   thousands of them back to back and got the instance's upstream engines
   rate-limited for days.

No network, no browser, no real sleeps: every clock, sleep and RNG is
injected, and the full-dataset test at the bottom drives all 827 real
brokers through a fake search backend.
"""
import json
import os
import threading

import pytest

from broker_guard import broker_normalize, playwright_checks, progress as progress_mod
from broker_guard import serpwatch, service, webui_data
from broker_guard.config import ConfigError, load_config
from broker_guard.searx_client import PermanentSearxError, SearxClient, SearxError
from broker_guard.sinks import CompositeAlertSink, FileAlertSink
from broker_guard.state import StateStore


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"results": []}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.requests.append({"url": url, "params": params})
        item = self.responses.pop(0) if self.responses else FakeResponse(200)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """A monotonic clock that only advances when something sleeps.

    This is what makes the pacing assertions exact: real elapsed time never
    leaks in, so a recorded sleep of 2.4s is the pacer's decision and
    nothing else.
    """

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class FixedRng:
    """A stand-in for `random` whose uniform() is deterministic."""

    def __init__(self, value=0.0):
        self.value = value
        self.calls = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return self.value


# --------------------------------------------------------------------------
# ScanProgress
# --------------------------------------------------------------------------

def test_progress_starts_empty_and_reports_no_percent_without_a_total():
    p = progress_mod.ScanProgress(now=lambda: "T0")
    snap = p.snapshot()
    assert snap["active"] is False
    assert snap["total"] == 0 and snap["processed"] == 0
    # Not 0 -- nobody knows the denominator, and "0%" would be a claim.
    assert snap["percent"] is None


def test_progress_counts_each_outcome_into_its_own_bucket():
    p = progress_mod.ScanProgress(now=lambda: "T0")
    p.start(progress_mod.PHASE_SERP, 4)
    p.record("hit")
    p.record("checked")
    p.record("error")
    p.record("skipped")
    snap = p.snapshot()
    assert (snap["hits"], snap["checked"], snap["errors"], snap["skipped"]) == (1, 1, 1, 1)
    assert snap["processed"] == 4
    assert snap["percent"] == 100
    assert snap["active"] is True


def test_progress_keeps_errors_distinct_from_checked():
    """The whole point: 827 failures must not read as 827 clean checks."""
    p = progress_mod.ScanProgress(now=lambda: "T0")
    p.start(progress_mod.PHASE_SERP, 827)
    for _ in range(827):
        p.record("error")
    snap = p.snapshot()
    assert snap["errors"] == 827
    assert snap["checked"] == 0
    assert snap["hits"] == 0


def test_progress_rejects_an_unknown_outcome():
    p = progress_mod.ScanProgress()
    p.start(progress_mod.PHASE_SERP, 1)
    with pytest.raises(ValueError):
        p.record("cheked")  # typo must not silently inflate a bucket


def test_progress_finish_keeps_the_counters_but_clears_active():
    p = progress_mod.ScanProgress(now=lambda: "T0")
    p.start(progress_mod.PHASE_SERP, 2)
    p.record("hit")
    p.finish()
    snap = p.snapshot()
    assert snap["active"] is False
    assert snap["hits"] == 1 and snap["processed"] == 1


def test_progress_start_resets_counters_for_the_new_phase():
    p = progress_mod.ScanProgress(now=lambda: "T0")
    p.start(progress_mod.PHASE_SERP, 100)
    p.record("error")
    p.start(progress_mod.PHASE_BROWSER, 5)
    snap = p.snapshot()
    assert snap["phase"] == progress_mod.PHASE_BROWSER
    assert snap["total"] == 5
    assert snap["errors"] == 0 and snap["processed"] == 0


def test_progress_is_safe_under_concurrent_writers():
    """Both legs run on the autopilot thread while /status reads from a
    request thread -- a lost update here would silently undercount."""
    p = progress_mod.ScanProgress(now=lambda: "T0")
    p.start(progress_mod.PHASE_SERP, 800)

    def worker():
        for _ in range(100):
            p.record("checked")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert p.snapshot()["checked"] == 800


def test_progress_module_singleton_is_shared():
    assert progress_mod.current() is progress_mod.current()
    progress_mod.current().clear()
    assert progress_mod.snapshot()["processed"] == 0


# --------------------------------------------------------------------------
# serpwatch: per-broker outcomes
# --------------------------------------------------------------------------

def _observer_sink():
    seen = []
    return seen, lambda bid, outcome, hits, errors: seen.append((bid, outcome, hits, errors))


def test_serpwatch_reports_a_hit_outcome():
    seen, observer = _observer_sink()
    serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        [], [], ["Testy Mctestface"], [],
        lambda q: [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p",
                    "content": "record"}],
        observer=observer,
    )
    assert [s[:2] for s in seen] == [("alpha", "hit")]


def test_serpwatch_reports_a_checked_outcome_when_nothing_matches():
    seen, observer = _observer_sink()
    serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        [], [], ["Testy Mctestface"], [], lambda q: [], observer=observer,
    )
    assert [s[:2] for s in seen] == [("alpha", "checked")]


def test_serpwatch_reports_an_error_outcome_instead_of_a_clean_zero():
    """THE regression. A backend that raises for every query must not be
    reported as a broker that was successfully searched and came back
    clean -- that is exactly what made a SearXNG outage look like good
    news."""
    seen, observer = _observer_sink()

    def boom(query):
        raise SearxError("searxng unreachable")

    hits = serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        [], [], ["Testy Mctestface"], [], boom, observer=observer,
    )
    assert hits == []                      # still no false positives
    assert [s[:2] for s in seen] == [("alpha", "error")]


def test_serpwatch_reports_skipped_for_a_broker_with_no_usable_url():
    seen, observer = _observer_sink()
    serpwatch.run_serpwatch(
        [{"id": "nourl", "name": "N", "url": ""}],
        [], [], ["Testy Mctestface"], [], lambda q: [], observer=observer,
    )
    assert [s[:2] for s in seen] == [("nourl", "skipped")]


def test_serpwatch_prefers_hit_over_error_when_queries_partly_failed():
    seen, observer = _observer_sink()
    calls = {"n": 0}

    def flaky(query):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SearxError("rate limited")
        return [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p",
                 "content": "record"}]

    serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        ["+1-555-0100"], [], ["Testy Mctestface"], [], flaky, observer=observer,
    )
    # A positive is a positive, even if some evidence never arrived; the
    # error count for the broker is still reported alongside it.
    assert seen[0][1] == "hit"
    assert seen[0][3] == 1


def test_serpwatch_still_continues_past_a_failing_broker():
    """Counting errors must not have cost the "one bad broker never kills
    the cycle" property."""
    seen, observer = _observer_sink()

    def boom_on_alpha(query):
        if "alpha.invalid" in query:
            raise SearxError("down")
        return [{"title": "Testy Mctestface", "url": "https://beta.invalid/p",
                 "content": "record"}]

    hits = serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"},
         {"id": "beta", "name": "B", "url": "https://beta.invalid"}],
        [], [], ["Testy Mctestface"], [], boom_on_alpha, observer=observer,
    )
    assert {h.broker_id for h in hits} == {"beta"}
    assert [s[:2] for s in seen] == [("alpha", "error"), ("beta", "hit")]


def test_serpwatch_does_not_log_the_query_text_on_failure(caplog):
    """Query text IS the person's name/phone/email -- it must never reach a
    log line, even on the error path this change just added."""
    def boom(query):
        raise SearxError("failed for {}".format(query))

    with caplog.at_level("WARNING"):
        serpwatch.run_serpwatch(
            [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
            ["+1-555-0100"], ["testy.mctestface@example.invalid"], [], [], boom,
        )
    logged = "\n".join(r.getMessage() + json.dumps(getattr(r, "error", ""))
                       for r in caplog.records)
    assert "testy.mctestface@example.invalid" not in logged
    assert "555-0100" not in logged


def test_serpwatch_observer_failure_cannot_abort_the_sweep():
    def bad_observer(*args):
        raise RuntimeError("telemetry exploded")

    hits = serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        [], [], ["Testy Mctestface"], [],
        lambda q: [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p",
                    "content": "r"}],
        observer=bad_observer,
    )
    assert {h.broker_id for h in hits} == {"alpha"}


# --------------------------------------------------------------------------
# playwright leg: per-broker outcomes
# --------------------------------------------------------------------------

def test_playwright_checks_report_hit_checked_and_error_outcomes():
    seen, observer = _observer_sink()
    checks = [
        {"broker_id": "hit1", "url": "https://a.invalid", "terms": []},
        {"broker_id": "clean", "url": "https://b.invalid", "terms": []},
        {"broker_id": "broken", "url": "https://c.invalid", "terms": []},
    ]

    def page_action(check):
        if check["broker_id"] == "broken":
            return {"error": "navigation timeout"}
        return {"found": check["broker_id"] == "hit1"}

    playwright_checks.run_playwright_checks(checks, page_action, observer=observer)
    assert [s[:2] for s in seen] == [("hit1", "hit"), ("clean", "checked"), ("broken", "error")]


def test_playwright_checks_report_error_for_a_raising_page_action():
    seen, observer = _observer_sink()

    def page_action(check):
        raise RuntimeError("browser crashed")

    playwright_checks.run_playwright_checks(
        [{"broker_id": "alpha", "url": "https://a.invalid", "terms": []}],
        page_action, observer=observer,
    )
    assert [s[:2] for s in seen] == [("alpha", "error")]


# --------------------------------------------------------------------------
# SearxClient: honest failures
# --------------------------------------------------------------------------

def _client(session, **kwargs):
    kwargs.setdefault("sleep", lambda _: None)
    kwargs.setdefault("min_interval_s", 0)
    kwargs.setdefault("jitter_s", 0)
    return SearxClient("http://searx.invalid:8080", session=session, **kwargs)


def test_searx_client_returns_results_on_success():
    session = FakeSession([FakeResponse(200, {"results": [{"title": "t", "url": "u"}]})])
    assert _client(session)("q") == [{"title": "t", "url": "u"}]


def test_searx_client_raises_searx_error_when_every_attempt_fails():
    session = FakeSession([FakeResponse(503)] * 5)
    with pytest.raises(SearxError):
        _client(session, attempts=3)("q")


def test_searx_client_raises_permanent_error_for_a_4xx():
    session = FakeSession([FakeResponse(403)])
    with pytest.raises(PermanentSearxError):
        _client(session)("q")


def test_searx_client_failure_message_carries_no_query_text():
    session = FakeSession([FakeResponse(503)] * 5)
    try:
        _client(session, attempts=2)("site:alpha.invalid \"Testy Mctestface\"")
    except SearxError as exc:
        assert "Testy Mctestface" not in str(exc)
    else:  # pragma: no cover
        pytest.fail("expected SearxError")


# --------------------------------------------------------------------------
# SearxClient: request pacing
# --------------------------------------------------------------------------

def test_pacing_does_not_delay_the_very_first_request():
    clock = FakeClock()
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(200)]),
        sleep=clock.sleep, monotonic=clock.monotonic, rng=FixedRng(0.0),
        min_interval_s=2.0, jitter_s=1.0,
    )
    client("q1")
    assert clock.sleeps == []


def test_pacing_sleeps_between_successive_requests():
    clock = FakeClock()
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(200)] * 3),
        sleep=clock.sleep, monotonic=clock.monotonic, rng=FixedRng(0.0),
        min_interval_s=2.0, jitter_s=0.0,
    )
    client("q1")
    client("q2")
    client("q3")
    # Two gaps for three requests, each the full minimum interval.
    assert clock.sleeps == [2.0, 2.0]


def test_pacing_adds_jitter_on_top_of_the_minimum_interval():
    clock = FakeClock()
    rng = FixedRng(0.75)
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(200)] * 2),
        sleep=clock.sleep, monotonic=clock.monotonic, rng=rng,
        min_interval_s=2.0, jitter_s=1.0,
    )
    client("q1")
    client("q2")
    assert clock.sleeps == [2.75]
    assert rng.calls == [(0, 1.0)]


def test_pacing_credits_time_that_already_elapsed_rather_than_sleeping_twice():
    """Retry backoff and slow responses already consume wall time; the pacer
    measures the gap instead of sleeping unconditionally, so a slow cycle is
    not penalised on top of being slow."""
    clock = FakeClock()
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(200)] * 2),
        sleep=clock.sleep, monotonic=clock.monotonic, rng=FixedRng(0.0),
        min_interval_s=2.0, jitter_s=0.0,
    )
    client("q1")
    clock.advance(5.0)        # e.g. a slow upstream, or retry backoff
    client("q2")
    assert clock.sleeps == []


def test_pacing_can_be_switched_off_entirely():
    clock = FakeClock()
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(200)] * 3),
        sleep=clock.sleep, monotonic=clock.monotonic,
        min_interval_s=0, jitter_s=0,
    )
    for _ in range(3):
        client("q")
    assert clock.sleeps == []


def test_pacing_also_spaces_out_retry_attempts_of_one_query():
    """Pacing lives at the single point an HTTP request is issued, so it
    covers retries too -- a query that retries 3x must not fire 3 requests
    back to back at a rate-limited instance."""
    clock = FakeClock()
    client = SearxClient(
        "http://searx.invalid:8080", session=FakeSession([FakeResponse(503)] * 3),
        sleep=clock.sleep, monotonic=clock.monotonic, rng=FixedRng(0.0),
        min_interval_s=2.0, jitter_s=0.0, attempts=3, base_delay=0,
    )
    with pytest.raises(SearxError):
        client("q")
    # 3 attempts -> 2 paced gaps (the retry backoff itself is base_delay=0).
    assert clock.sleeps.count(2.0) == 2


def test_pacing_default_is_conservative():
    """Guards the default against being quietly lowered: the instance this
    runs against can take ~2 weeks to clear a CAPTCHA-tier block."""
    client = SearxClient("http://searx.invalid:8080", session=FakeSession([]))
    assert client.min_interval_s >= 1.0
    assert client.jitter_s > 0


def test_config_exposes_the_pacing_knobs(base_env):
    cfg = load_config(base_env)
    assert cfg.searxng_min_interval_s == 2.0
    assert cfg.searxng_jitter_s == 1.0

    tuned = load_config({**base_env, "BG_SEARXNG_MIN_INTERVAL_S": "4.5",
                         "BG_SEARXNG_JITTER_S": "0.5"})
    assert tuned.searxng_min_interval_s == 4.5
    assert tuned.searxng_jitter_s == 0.5


def test_config_rejects_a_negative_pacing_interval(base_env):
    with pytest.raises(ConfigError):
        load_config({**base_env, "BG_SEARXNG_MIN_INTERVAL_S": "-1"})


# --------------------------------------------------------------------------
# webui_data rendering
# --------------------------------------------------------------------------

def test_scan_progress_line_shows_counts_found_and_errors():
    line = webui_data.scan_progress_line({
        "phase": "serp", "total": 827, "processed": 412, "hits": 3, "errors": 0,
    })
    assert line == "Checking brokers: 412/827 -- 3 found, 0 errors."


def test_scan_progress_line_surfaces_a_bad_run_as_a_different_state():
    line = webui_data.scan_progress_line({
        "phase": "serp", "total": 827, "processed": 412, "hits": 0, "errors": 340,
    })
    assert "340 errors" in line


def test_scan_progress_line_is_none_when_nothing_has_started():
    assert webui_data.scan_progress_line(None) is None
    assert webui_data.scan_progress_line({"total": 0, "processed": 0}) is None


def test_scan_status_reports_running_and_a_line_from_a_live_progress():
    result = webui_data.scan_status(None, {}, 86400, progress={
        "active": True, "phase": "serp", "total": 827, "processed": 10,
        "hits": 0, "errors": 0,
    })
    assert result["running"] is True
    assert result["progress_line"] == "Checking brokers: 10/827 -- 0 found, 0 errors."


def test_scan_status_does_not_replay_a_finished_progress_as_live():
    """finish() keeps the counters readable; the dashboard must not go on
    claiming "827/827" hours after the cycle ended."""
    result = webui_data.scan_status(
        {"last_run": "2026-01-01T00:00:00+00:00", "ok": True}, {}, 86400,
        progress={"active": False, "phase": "serp", "total": 827,
                  "processed": 827, "hits": 3, "errors": 0},
    )
    assert result["running"] is False
    assert result["progress_line"] is None


def test_last_scan_detection_line_distinguishes_clean_from_failed():
    clean = webui_data.last_scan_detection_line({
        "detection": {"serp": {"hit": 3, "checked": 824, "error": 0, "skipped": 0},
                      "browser": {"hit": 0, "checked": 0, "error": 0, "skipped": 0}}})
    broken = webui_data.last_scan_detection_line({
        "detection": {"serp": {"hit": 0, "checked": 487, "error": 340, "skipped": 0},
                      "browser": {"hit": 0, "checked": 0, "error": 0, "skipped": 0}}})
    assert clean == "Checked 827 broker(s), 0 error(s)."
    assert broken == "Checked 487 broker(s), 340 error(s)."
    assert clean != broken


def test_last_scan_detection_line_is_none_for_an_older_heartbeat():
    assert webui_data.last_scan_detection_line({"last_run": "2026-01-01T00:00:00+00:00"}) is None


# --------------------------------------------------------------------------
# service.run_once wiring
# --------------------------------------------------------------------------

@pytest.fixture
def cfg(base_env):
    return load_config(base_env)


@pytest.fixture
def deps_factory(cfg):
    made = []

    def make(**overrides):
        deps = service.Dependencies(
            store=StateStore.open(cfg.state_path),
            alert_sink=overrides.pop("alert_sink",
                                      CompositeAlertSink([FileAlertSink(cfg.alert_log_path)])),
            now=lambda: "2026-01-01T00:00:00+00:00",
            **overrides,
        )
        made.append(deps)
        return deps

    yield make
    for deps in made:
        deps.close()


def test_run_once_reports_zero_detection_errors_for_a_healthy_cycle(cfg, deps_factory):
    deps = deps_factory(searx_search=lambda q: [])
    result = service.run_once(cfg, deps)
    assert result["detection_errors"] == 0
    assert result["detection"]["serp"]["checked"] == 3
    assert result["detection"]["serp"]["error"] == 0


def test_run_once_reports_detection_errors_when_every_search_fails(cfg, deps_factory):
    """"0 present" from a working scan and "0 present" from a dead SearXNG
    are now different results, which is the entire point."""
    def boom(query):
        raise SearxError("searxng unreachable")

    deps = deps_factory(searx_search=boom)
    result = service.run_once(cfg, deps)
    assert result["current"] == []                  # unchanged: no false positives
    assert result["detection_errors"] == 3          # ...but no longer silent
    assert result["detection"]["serp"]["error"] == 3
    assert result["detection"]["serp"]["checked"] == 0


def test_run_once_advances_the_shared_progress_counter(cfg, deps_factory):
    progress_mod.current().clear()
    deps = deps_factory(searx_search=lambda q: [])
    service.run_once(cfg, deps)
    snap = progress_mod.snapshot()
    assert snap["total"] == 3
    assert snap["processed"] == 3
    assert snap["active"] is False   # finish() ran


def test_build_presence_checker_accepts_an_isolated_progress(cfg, deps_factory):
    """Tests must be able to avoid touching process-wide state."""
    from broker_guard import brokers as brokers_mod, profile as profile_mod

    isolated = progress_mod.ScanProgress(now=lambda: "T0")
    progress_mod.current().clear()
    identity = profile_mod.load_profile(cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    deps = deps_factory(searx_search=lambda q: [])

    service.build_presence_checker(identity, broker_list, deps, cfg, progress=isolated)

    assert isolated.snapshot()["processed"] == 3
    assert progress_mod.snapshot()["processed"] == 0


# --------------------------------------------------------------------------
# the autopilot re-scans instead of replaying its startup sweep
# --------------------------------------------------------------------------

def test_autopilot_rebuilds_the_presence_checker_every_cycle(cfg):
    """build_dependencies used to capture ONE eagerly-built checker at
    process start, so the deployed container re-reported its boot-time
    results forever."""
    from broker_guard import autopilot, profile as profile_mod

    builds = []

    def factory():
        builds.append(1)

        def checker(broker, identity_key):
            return False

        return checker

    identity = profile_mod.load_profile(cfg.profile_path)
    brokers = [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}]
    deps = autopilot.AutopilotDependencies(
        store=StateStore.open(cfg.state_path),
        presence_checker_factory=factory,
        now=lambda: "2026-01-01T00:00:00+00:00",
    )
    try:
        autopilot.run_scan_cycle(identity, brokers, deps)
        autopilot.run_scan_cycle(identity, brokers, deps)
    finally:
        deps.store.close()
    assert len(builds) == 2


def test_autopilot_still_honours_a_directly_injected_presence_checker(cfg):
    from broker_guard import autopilot, profile as profile_mod

    identity = profile_mod.load_profile(cfg.profile_path)
    brokers = [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}]
    deps = autopilot.AutopilotDependencies(
        store=StateStore.open(cfg.state_path),
        presence_checker=lambda broker, key: True,
        now=lambda: "2026-01-01T00:00:00+00:00",
    )
    try:
        result = autopilot.run_scan_cycle(identity, brokers, deps)
    finally:
        deps.store.close()
    assert result["current"] == ["alpha"]


# --------------------------------------------------------------------------
# the dashboard actually shows it
# --------------------------------------------------------------------------

@pytest.fixture
def web_client(cfg):
    from fastapi.testclient import TestClient

    from broker_guard import webui

    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    webui.app.dependency_overrides[webui.get_jobs] = lambda: {}
    try:
        yield TestClient(webui.app)
    finally:
        webui.app.dependency_overrides.clear()
        progress_mod.current().clear()


def test_dashboard_shows_the_live_counter_instead_of_scan_in_progress(web_client):
    p = progress_mod.current()
    p.clear()
    p.start(progress_mod.PHASE_SERP, 827)
    for _ in range(412):
        p.record("checked")
    for _ in range(3):
        p.record("hit")

    page = web_client.get("/").text
    assert "Checking brokers: 415/827 -- 3 found, 0 errors." in page
    assert "Scan in progress right now." not in page
    assert 'data-scan-running="1"' in page


def test_status_route_feeds_the_poll_loop_with_the_same_line(web_client):
    p = progress_mod.current()
    p.clear()
    p.start(progress_mod.PHASE_SERP, 827)
    for _ in range(340):
        p.record("error")

    scan = web_client.get("/status").json()["scan"]
    assert scan["running"] is True
    assert scan["line"] == "Checking brokers: 340/827 -- 0 found, 340 errors."
    assert scan["progress"]["errors"] == 340


def test_dashboard_distinguishes_a_clean_last_scan_from_a_failed_one(cfg, web_client):
    """"checked 827, 0 errors" and "checked 487, 340 errors" must not render
    as the same sentence."""
    progress_mod.current().clear()
    os.makedirs(cfg.log_dir, exist_ok=True)
    path = os.path.join(cfg.log_dir, "heartbeat.json")

    def write(detection):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"last_run": "2026-01-01T00:00:00+00:00", "ok": True,
                       "status": "done", "detection": detection}, fh)

    write({"serp": {"hit": 3, "checked": 824, "error": 0, "skipped": 0},
           "browser": {"hit": 0, "checked": 0, "error": 0, "skipped": 0}})
    clean = web_client.get("/").text
    assert "Checked 827 broker(s), 0 error(s)." in clean

    write({"serp": {"hit": 0, "checked": 487, "error": 340, "skipped": 0},
           "browser": {"hit": 0, "checked": 0, "error": 0, "skipped": 0}})
    broken = web_client.get("/").text
    assert "Checked 487 broker(s), 340 error(s)." in broken


# --------------------------------------------------------------------------
# FULL DATASET: all ~827 real brokers, mocked SearXNG
# --------------------------------------------------------------------------

@pytest.fixture
def full_brokers_path(tmp_path):
    """The REAL bundled broker dataset, generated into tmp_path.

    Not a handful of fixtures: the point of these tests is to prove the
    counters, error accounting and pacing hold at the actual scale the
    deployment runs at, over the actual entries (missing urls, odd
    domains, every verification kind) it actually contains.
    """
    path = str(tmp_path / "brokers_full.json")
    generated = broker_normalize.ensure_brokers_file(path)
    if not generated or not os.path.exists(path):  # pragma: no cover
        pytest.skip("bundled broker source dataset is not available")
    return path


@pytest.fixture
def full_cfg(base_env, full_brokers_path):
    return load_config({**base_env, "BG_BROKERS_PATH": full_brokers_path})


def _broker_count(path):
    with open(path, encoding="utf-8") as fh:
        return len(json.load(fh)["brokers"])


def test_full_dataset_is_the_real_scale(full_brokers_path):
    assert _broker_count(full_brokers_path) > 800


def test_full_cycle_over_every_broker_counts_all_of_them(full_cfg, full_brokers_path,
                                                          deps_factory):
    """~827 brokers end to end through run_once with a fake backend: every
    broker lands in exactly one bucket, and the buckets add up."""
    progress_mod.current().clear()
    total = _broker_count(full_brokers_path)
    queries = []
    deps = deps_factory(searx_search=lambda q: queries.append(q) or [])

    result = service.run_once(full_cfg, deps)

    serp = result["detection"]["serp"]
    assert serp["hit"] + serp["checked"] + serp["error"] + serp["skipped"] == total
    assert result["detection_errors"] == 0
    # Every broker with a usable domain really was searched.
    assert len(queries) >= serp["checked"] + serp["hit"]
    assert all(q.startswith("site:") for q in queries)

    snap = progress_mod.snapshot()
    assert snap["total"] == total and snap["processed"] == total
    assert snap["active"] is False


def test_full_cycle_detects_a_single_listed_broker_among_all_of_them(full_cfg,
                                                                      full_brokers_path,
                                                                      deps_factory):
    """One needle in the real 827-broker haystack -- proves the counters do
    not drown a genuine hit, and that a hit is attributed to exactly one
    broker."""
    with open(full_brokers_path, encoding="utf-8") as fh:
        brokers = json.load(fh)["brokers"]
    target = next(b for b in brokers if b.get("url"))
    domain = serpwatch.broker_domain(target)

    def search(query):
        if "site:{}".format(domain) in query:
            return [{"title": "Testy Mctestface",
                     "url": "https://{}/p/1".format(domain),
                     "content": "Testy Mctestface profile record"}]
        return []

    deps = deps_factory(searx_search=search)
    result = service.run_once(full_cfg, deps)

    assert result["current"] == [target["id"]]
    assert result["detection"]["serp"]["hit"] == 1
    assert result["detection_errors"] == 0


def test_full_cycle_with_a_dead_backend_reports_every_broker_as_errored(full_cfg,
                                                                        full_brokers_path,
                                                                        deps_factory):
    """The exact production incident, at full scale: SearXNG rate-limited,
    every query failing. Before this change the cycle logged hit_brokers: 0
    and looked healthy. It must now report hundreds of errors and still not
    crash."""
    progress_mod.current().clear()
    total = _broker_count(full_brokers_path)

    def dead(query):
        raise SearxError("searxng returned HTTP 429")

    deps = deps_factory(searx_search=dead)
    result = service.run_once(full_cfg, deps)

    serp = result["detection"]["serp"]
    assert result["current"] == []           # still no false positives
    assert serp["checked"] == 0              # NOTHING is reported as cleanly checked
    assert serp["error"] > 800
    assert serp["error"] + serp["skipped"] == total
    assert result["detection_errors"] == serp["error"]

    snap = progress_mod.snapshot()
    assert snap["errors"] == serp["error"]
    assert webui_data.scan_progress_line({**snap, "active": True}).endswith(
        "{} errors.".format(serp["error"])
    )


def test_full_cycle_paces_every_request_through_the_real_client(full_cfg,
                                                                 full_brokers_path,
                                                                 deps_factory):
    """The pacing fix, exercised over the whole dataset through the REAL
    SearxClient (fake transport, fake clock): thousands of requests, and
    every single gap is at least the configured minimum."""
    clock = FakeClock()
    session = FakeSession([])          # always 200 with no results
    client = SearxClient(
        "http://searx.invalid:8080", session=session,
        sleep=clock.sleep, monotonic=clock.monotonic, rng=FixedRng(0.0),
        min_interval_s=2.0, jitter_s=0.0,
    )
    deps = deps_factory(searx_search=client)

    service.run_once(full_cfg, deps)

    requests = len(session.requests)
    assert requests > 800
    # One paced gap per request after the first, each the full interval.
    assert clock.sleeps == [2.0] * (requests - 1)
    # Sanity: that is a scan measured in hours, not seconds -- which is the
    # entire point of pacing against an instance that CAPTCHA-blocks.
    assert clock.t >= 2.0 * (requests - 1)
