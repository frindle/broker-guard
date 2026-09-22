"""Light spy-based tests for service.main's dispatch logic:

- ``--serve-web`` / ``BG_SERVE_WEB`` routes to ``webapp.run_web_server``
  instead of falling through to the headless loop.
- the broker-dataset auto-generation hook runs before path validation, and
  a failure there is logged but non-fatal (the ordinary missing-brokers.json
  config-validation error still fires as before).

No real uvicorn/autopilot loop is started here -- ``run_web_server`` itself
is replaced with a spy so these stay fast, network-free unit tests.
"""
import pytest

from broker_guard import broker_normalize, service


@pytest.fixture
def env(base_env, monkeypatch):
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    return base_env


def test_serve_web_flag_dispatches_to_run_web_server_instead_of_the_loop(env, monkeypatch):
    calls = []

    def fake_run_web_server(cfg):
        calls.append(cfg)
        return 0

    monkeypatch.setattr("broker_guard.webapp.run_web_server", fake_run_web_server)

    assert service.main(["--serve-web"]) == 0
    assert len(calls) == 1


def test_bg_serve_web_env_var_also_dispatches_to_run_web_server(env, monkeypatch):
    calls = []
    monkeypatch.setenv("BG_SERVE_WEB", "true")
    monkeypatch.setattr("broker_guard.webapp.run_web_server", lambda cfg: calls.append(cfg) or 0)

    assert service.main([]) == 0
    assert len(calls) == 1


def test_without_serve_web_the_headless_once_path_still_runs_unchanged(env, monkeypatch):
    """Regression guard: the default (BG_SERVE_WEB unset, no --serve-web) must
    still take the pre-existing headless loop path, not the web path."""
    calls = []
    monkeypatch.setattr("broker_guard.webapp.run_web_server", lambda cfg: calls.append(cfg) or 0)

    assert service.main(["--once"]) == 0
    assert calls == []  # run_web_server never touched


def test_ensure_brokers_file_is_invoked_before_path_validation(env, monkeypatch):
    calls = []
    original = broker_normalize.ensure_brokers_file

    def spy(brokers_path, *a, **kw):
        calls.append(brokers_path)
        return original(brokers_path, *a, **kw)

    monkeypatch.setattr(broker_normalize, "ensure_brokers_file", spy)

    assert service.main(["--check-config"]) == 0
    assert len(calls) == 1
    assert calls[0] == env["BG_BROKERS_PATH"]


def test_ensure_brokers_file_failure_is_logged_not_fatal_and_validation_still_runs(env, monkeypatch, tmp_path):
    """If auto-generation itself raises, service.main must not crash -- it
    logs the error and lets validate_runtime_paths report the (still
    missing) brokers.json as the ordinary config problem it already is."""
    missing_path = str(tmp_path / "never-created-brokers.json")
    monkeypatch.setenv("BG_BROKERS_PATH", missing_path)

    def boom(*a, **kw):
        raise OSError("disk exploded")

    monkeypatch.setattr(broker_normalize, "ensure_brokers_file", boom)

    # Does not raise, and correctly falls through to the normal
    # "brokers.json missing" config-validation failure (exit code 2).
    assert service.main(["--check-config"]) == 2


def test_ensure_brokers_file_never_touches_an_existing_brokers_file_via_main(env):
    """End-to-end confirmation through service.main: an existing
    BG_BROKERS_PATH file is left byte-for-byte untouched."""
    with open(env["BG_BROKERS_PATH"], "r", encoding="utf-8") as fh:
        before = fh.read()

    assert service.main(["--check-config"]) == 0

    with open(env["BG_BROKERS_PATH"], "r", encoding="utf-8") as fh:
        after = fh.read()
    assert after == before
