"""Tests for broker_guard.webapp.run_web_server -- the BG_SERVE_WEB entrypoint
that runs the autopilot loop as a background thread and serves webui.py's
FastAPI app in the foreground.

uvicorn.run is monkeypatched so nothing actually binds a port; autopilot's
build_dependencies/run_forever are monkeypatched to fakes so no real state
db, subprocess, or network I/O happens. This only tests the wiring/teardown
in webapp.py itself -- autopilot's own decision logic has its own test
suite (test_autopilot.py).
"""
import threading

import pytest

from broker_guard import autopilot, webapp
from broker_guard.config import load_config


@pytest.fixture
def cfg(base_env, monkeypatch):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BG_WEB_PORT", "8123")
    return load_config()


class FakeDeps:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_run_web_server_starts_autopilot_thread_and_serves_then_tears_down(cfg, monkeypatch):
    fake_deps = FakeDeps()
    monkeypatch.setattr(autopilot, "build_dependencies", lambda c: fake_deps)

    run_forever_calls = []

    def fake_run_forever(cfg_arg, deps_arg, intervals_arg, stop_arg, **kwargs):
        run_forever_calls.append((cfg_arg, deps_arg, intervals_arg, stop_arg, kwargs))
        # A real run_forever blocks on stop.wait(); a thread here just
        # returns immediately once started, which is fine for this test.

    monkeypatch.setattr(autopilot, "run_forever", fake_run_forever)
    monkeypatch.setattr(autopilot, "has_id_documents_on_file", lambda c: False)

    uvicorn_calls = []

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            uvicorn_calls.append({"host": host, "port": port, "log_level": log_level})

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)

    rc = webapp.run_web_server(cfg)

    assert rc == 0
    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0]["port"] == 8123
    assert uvicorn_calls[0]["host"] == "0.0.0.0"
    assert fake_deps.closed is True  # torn down in the finally block

    # give the daemon thread a moment to actually have run
    assert len(run_forever_calls) == 1
    _, deps_arg, intervals_arg, stop_arg, kwargs = run_forever_calls[0]
    assert deps_arg is fake_deps
    assert intervals_arg.scan_seconds == cfg.interval_seconds
    assert isinstance(stop_arg, threading.Event)
    assert kwargs == {"has_id_documents": False}


def test_run_web_server_binds_without_waiting_for_build_dependencies(cfg, monkeypatch):
    """build_dependencies() (which launches Playwright/Chromium when enabled)
    must run on the background thread, not the main thread -- a slow/stalled
    browser launch must never delay uvicorn.run() from being reached. This is
    the exact regression: BG_PLAYWRIGHT_ENABLED=true was observed to leave
    the port unbound indefinitely because build_dependencies() used to run
    BEFORE uvicorn.run() on the main thread."""
    release = threading.Event()
    fake_deps = FakeDeps()

    def slow_build_dependencies(c):
        # Simulates a slow/stalled Playwright launch. If webapp.py still
        # called this on the main thread, uvicorn.run() below would never
        # be reached before the test's own timeout.
        assert release.wait(timeout=5), "build_dependencies was awaited before uvicorn.run()"
        return fake_deps

    monkeypatch.setattr(autopilot, "build_dependencies", slow_build_dependencies)
    monkeypatch.setattr(autopilot, "run_forever", lambda *a, **kw: None)
    monkeypatch.setattr(autopilot, "has_id_documents_on_file", lambda c: False)

    uvicorn_calls = []

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            uvicorn_calls.append(True)
            # Only unblock the "slow" build_dependencies once uvicorn has
            # already been reached, so the teardown join() doesn't hang.
            release.set()

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)

    rc = webapp.run_web_server(cfg)

    assert rc == 0
    assert uvicorn_calls == [True]
    assert fake_deps.closed is True


def test_run_web_server_stops_the_loop_and_closes_deps_even_if_uvicorn_raises(cfg, monkeypatch):
    fake_deps = FakeDeps()
    monkeypatch.setattr(autopilot, "build_dependencies", lambda c: fake_deps)
    monkeypatch.setattr(autopilot, "run_forever", lambda *a, **kw: None)
    monkeypatch.setattr(autopilot, "has_id_documents_on_file", lambda c: False)

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            raise RuntimeError("bind failed")

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)

    with pytest.raises(RuntimeError):
        webapp.run_web_server(cfg)

    assert fake_deps.closed is True  # finally block still ran
