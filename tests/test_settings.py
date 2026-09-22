"""Tests for broker_guard/settings.py -- the UI-editable, /data-persisted
settings overlay that exists so a redeploy's ``git reset --hard`` can no
longer silently revert a runtime setting.

The precedence rule (stored > env > built-in default) is the load-bearing
claim of the whole feature, so it is asserted in BOTH directions here: a
stored value must beat an env var that says something else, and an env var
must still win when nothing is stored (that second one is what guarantees
first-boot behavior is unchanged for a deployment that never opens the page).
"""
import json
import os
import threading

import pytest

from broker_guard import autopilot
from broker_guard import service as service_mod
from broker_guard import settings as settings_mod
from broker_guard.config import Config, load_config


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "settings.json")


def write_store(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


# --- precedence: the whole point -------------------------------------------

def test_env_wins_when_nothing_is_stored(store):
    """First boot / never-opened-the-page: the BG_* variables still decide
    everything, byte for byte as before this module existed."""
    env = {"BG_PLAYWRIGHT_ENABLED": "true",
           "BG_SEARXNG_URL": "http://searx.invalid:8080",
           "BG_INTERVAL_SECONDS": "3600"}
    assert not os.path.exists(store)

    by_key = {r.key: r for r in settings_mod.resolve(store, env=env)}

    assert by_key["playwright_enabled"].value is True
    assert by_key["playwright_enabled"].source == settings_mod.SOURCE_ENV
    assert by_key["searxng_url"].value == "http://searx.invalid:8080"
    assert by_key["searxng_url"].source == settings_mod.SOURCE_ENV
    assert by_key["interval_seconds"].value == 3600
    assert by_key["interval_seconds"].source == settings_mod.SOURCE_ENV


def test_stored_wins_over_env(store):
    """The actual bug fix: the env var says one thing (that is what the
    tracked docker-compose.yml keeps resetting it to) and the stored value
    says another -- the stored value must win."""
    write_store(store, {"playwright_enabled": True,
                        "searxng_url": "http://stored.invalid:9090",
                        "interval_seconds": 1800})
    env = {"BG_PLAYWRIGHT_ENABLED": "false",
           "BG_SEARXNG_URL": "http://env.invalid:8080",
           "BG_INTERVAL_SECONDS": "86400"}

    by_key = {r.key: r for r in settings_mod.resolve(store, env=env)}

    assert by_key["playwright_enabled"].value is True
    assert by_key["playwright_enabled"].source == settings_mod.SOURCE_STORED
    assert by_key["searxng_url"].value == "http://stored.invalid:9090"
    assert by_key["searxng_url"].source == settings_mod.SOURCE_STORED
    assert by_key["interval_seconds"].value == 1800
    assert by_key["interval_seconds"].source == settings_mod.SOURCE_STORED


def test_default_wins_when_neither_stored_nor_env(store):
    by_key = {r.key: r for r in settings_mod.resolve(store, env={})}
    blank = Config()
    for resolved in by_key.values():
        assert resolved.source == settings_mod.SOURCE_DEFAULT
        assert resolved.value == getattr(blank, resolved.key)


def test_stored_empty_string_is_an_explicit_disable_not_a_fallthrough(store):
    """"" is stored, not absent: it must turn the setting OFF rather than let
    the env var take over -- that is how the UI disables SERP detection or the
    alert webhook without deleting the store."""
    write_store(store, {"searxng_url": "", "alert_webhook_url": ""})
    env = {"BG_SEARXNG_URL": "http://env.invalid:8080",
           "BG_ALERT_WEBHOOK_URL": "https://hook.invalid/x"}

    by_key = {r.key: r for r in settings_mod.resolve(store, env=env)}

    assert by_key["searxng_url"].value is None
    assert by_key["searxng_url"].source == settings_mod.SOURCE_STORED
    assert by_key["alert_webhook_url"].value is None
    assert by_key["alert_webhook_url"].source == settings_mod.SOURCE_STORED


def test_effective_config_overlays_stored_onto_the_env_config(store, base_env):
    """End to end through the real thing: load_config reads the env, then
    effective_config overlays the store. Both tiers asserted on one Config."""
    env = {**base_env,
           "BG_SETTINGS_PATH": store,
           "BG_PLAYWRIGHT_ENABLED": "false",
           "BG_SEARXNG_URL": "http://env.invalid:8080",
           "BG_SEARXNG_MIN_INTERVAL_S": "2.0"}
    cfg = load_config(env)
    assert cfg.playwright_enabled is False          # env tier, before overlay
    assert cfg.searxng_url == "http://env.invalid:8080"

    write_store(store, {"playwright_enabled": True, "searxng_min_interval_s": 7.5})
    live = settings_mod.effective_config(cfg)

    assert live.playwright_enabled is True          # stored beat the env var
    assert live.searxng_min_interval_s == 7.5
    # Untouched keys still come from the env, and the input Config is not
    # mutated -- callers hold the env baseline and must keep it.
    assert live.searxng_url == "http://env.invalid:8080"
    assert cfg.playwright_enabled is False


def test_effective_config_ignores_a_setting_it_does_not_own(store):
    """BG_SERVE_WEB is a bootstrap paradox (no UI to toggle it from when it is
    off), so it must never become overlayable -- not even by hand-editing the
    store. Same for the store's own path."""
    assert "serve_web" not in settings_mod.SPEC_BY_KEY
    assert "settings_path" not in settings_mod.SPEC_BY_KEY

    write_store(store, {"serve_web": True, "settings_path": "/tmp/elsewhere.json"})
    cfg = Config(settings_path=store, serve_web=False)

    live = settings_mod.effective_config(cfg)

    assert live.serve_web is False
    assert live.settings_path == store


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"interval_seconds": "30"},                 # below the documented 60s floor
    {"searxng_min_interval_s": "-1"},
    {"searxng_jitter_s": "-0.5"},
    {"searxng_url": "not-a-url"},
    {"searxng_url": "ftp://searx.invalid"},
    {"alert_webhook_url": "javascript:alert(1)"},
    {"interval_seconds": "soon"},
    {"playwright_enabled": "maybe"},
])
def test_update_settings_rejects_a_bad_value_without_writing_anything(store, changes):
    settings_mod.update_settings(store, {"interval_seconds": 3600})
    before = open(store, encoding="utf-8").read()

    with pytest.raises(settings_mod.SettingsError):
        settings_mod.update_settings(store, changes)

    assert open(store, encoding="utf-8").read() == before


def test_update_settings_rejects_an_unknown_key(store):
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.update_settings(store, {"serve_web": True})


def test_none_deletes_the_override_so_env_takes_over_again(store):
    settings_mod.update_settings(store, {"playwright_enabled": True})
    env = {"BG_PLAYWRIGHT_ENABLED": "false"}
    assert settings_mod.resolve(store, env=env)[0].source == settings_mod.SOURCE_STORED

    settings_mod.update_settings(store, {"playwright_enabled": None})

    by_key = {r.key: r for r in settings_mod.resolve(store, env=env)}
    assert by_key["playwright_enabled"].source == settings_mod.SOURCE_ENV
    assert by_key["playwright_enabled"].value is False
    assert "playwright_enabled" not in json.load(open(store, encoding="utf-8"))


def test_one_unusable_stored_key_degrades_to_env_without_killing_the_rest(store):
    """A hand-edited file with one bad value must cost exactly that one key --
    this file is read on the request path and on every autopilot cycle."""
    write_store(store, {"interval_seconds": -5, "nonsense_key": 1,
                        "playwright_enabled": True})

    by_key = {r.key: r for r in settings_mod.resolve(store, env={"BG_INTERVAL_SECONDS": "7200"})}

    assert by_key["interval_seconds"].value == 7200
    assert by_key["interval_seconds"].source == settings_mod.SOURCE_ENV
    assert by_key["playwright_enabled"].value is True
    assert by_key["playwright_enabled"].source == settings_mod.SOURCE_STORED


def test_a_corrupt_store_never_takes_down_the_process(store):
    with open(store, "w", encoding="utf-8") as fh:
        fh.write("[not, an, object")

    with pytest.raises(ValueError):
        settings_mod.load_settings(store)

    # ...but the overlay and the page-facing resolver both degrade to env-only.
    cfg = Config(settings_path=store, playwright_enabled=True)
    assert settings_mod.effective_config(cfg).playwright_enabled is True
    assert all(r.source != settings_mod.SOURCE_STORED
               for r in settings_mod.resolve(store, env={}))


# --- durability / concurrency ------------------------------------------------

def test_save_is_atomic_a_failed_write_leaves_the_old_file_intact(store, tmp_path, monkeypatch):
    """tmp-file + os.replace, not truncate-in-place: a write that dies part
    way through must leave the PREVIOUS settings readable, not a half file."""
    settings_mod.update_settings(store, {"interval_seconds": 3600,
                                         "playwright_enabled": True})
    original = open(store, encoding="utf-8").read()

    def exploding_dump(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(settings_mod.json, "dump", exploding_dump)
    with pytest.raises(OSError):
        settings_mod.save_settings(store, {"interval_seconds": 60})

    assert open(store, encoding="utf-8").read() == original
    assert settings_mod.load_settings(store)["interval_seconds"] == 3600
    # ...and no .settings-*.tmp debris is left behind in the data directory.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".settings-")] == []


def test_save_is_owner_only(store):
    settings_mod.update_settings(store, {"captcha_api_key": "topsecret"})
    assert oct(os.stat(store).st_mode & 0o777) == "0o600"


def test_concurrent_updates_do_not_lose_each_others_writes(store):
    """The web request thread and the autopilot thread share one process (see
    webapp.py), so a read-modify-write has to be serialized -- otherwise two
    saves interleave and one edit vanishes. Also asserts a concurrent READER
    never observes a partial/invalid document."""
    changes = [
        {"interval_seconds": 3600},
        {"playwright_enabled": True},
        {"searxng_url": "http://a.invalid:8080"},
        {"searxng_min_interval_s": 3.0},
        {"searxng_jitter_s": 1.5},
        {"alert_webhook_url": "https://hook.invalid/x"},
        {"eraser_enabled": True},
        {"eraser_dry_run": False},
        {"captcha_api_key": "k"},
    ]
    start = threading.Barrier(len(changes) + 1)
    reader_errors = []
    stop_reading = threading.Event()

    def writer(change):
        start.wait()
        for _ in range(20):
            settings_mod.update_settings(store, change)

    def reader():
        start.wait()
        while not stop_reading.is_set():
            try:
                settings_mod.load_settings(store)
            except Exception as exc:  # pragma: no cover - the failure we assert against
                reader_errors.append(repr(exc))
                return

    threads = [threading.Thread(target=writer, args=(c,)) for c in changes]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads[:-1]:
        t.join()
    stop_reading.set()
    threads[-1].join()

    assert reader_errors == []
    final = settings_mod.load_settings(store)
    for change in changes:
        for key, value in change.items():
            assert final[key] == value, f"{key} was lost by a concurrent write"


# --- liveness: does a change actually take effect without a restart? ---------

def _autopilot_cfg(tmp_path, store, profile_file, brokers_file):
    return Config(
        profile_path=profile_file, brokers_path=brokers_file,
        state_path=str(tmp_path / "state.sqlite"), log_dir=str(tmp_path / "logs"),
        alert_log_path=str(tmp_path / "alerts.jsonl"),
        settings_path=store,
    )


def test_detection_layer_is_rebuilt_when_a_detection_setting_changes(
        tmp_path, store, profile_file, brokers_file, monkeypatch):
    """The live-toggle claim, proven: change BG_PLAYWRIGHT_ENABLED/SEARXNG_URL
    through the store between two autopilot cycles and the SECOND cycle must
    build its detection layer from the new values -- no process restart."""
    built = []
    closed = []

    def fake_build_detection(cfg):
        built.append((cfg.playwright_enabled, cfg.searxng_url,
                      cfg.searxng_min_interval_s))
        return None, None, [lambda: closed.append(len(built))]

    monkeypatch.setattr(service_mod, "build_detection", fake_build_detection)
    monkeypatch.setattr(service_mod, "build_presence_checker",
                        lambda identity, brokers, deps, cfg: deps)

    cfg = _autopilot_cfg(tmp_path, store, profile_file, brokers_file)
    deps = autopilot.build_dependencies(cfg)
    try:
        first = deps.presence_checker_factory()
        assert built == [(False, None, 2.0)]        # the Config/env baseline
        assert first.searx_search is None and first.page_action is None

        # ...someone flips the toggles on /settings mid-process.
        settings_mod.update_settings(store, {"playwright_enabled": True,
                                             "searxng_url": "http://new.invalid:8080",
                                             "searxng_min_interval_s": 4.0})
        deps.presence_checker_factory()

        assert built[-1] == (True, "http://new.invalid:8080", 4.0)
        assert closed, "the superseded detection layer must be torn down, not leaked"
    finally:
        deps.close()


def test_detection_layer_is_not_rebuilt_when_nothing_changed(
        tmp_path, store, profile_file, brokers_file, monkeypatch):
    """The other half of the same property: an unchanged setting must NOT
    relaunch Chromium on every single cycle."""
    built = []
    monkeypatch.setattr(service_mod, "build_detection",
                        lambda cfg: (built.append(cfg.searxng_url), (None, None, []))[1])
    monkeypatch.setattr(service_mod, "build_presence_checker",
                        lambda identity, brokers, deps, cfg: deps)

    cfg = _autopilot_cfg(tmp_path, store, profile_file, brokers_file)
    deps = autopilot.build_dependencies(cfg)
    try:
        for _ in range(3):
            deps.presence_checker_factory()
        # Exactly the one build_dependencies did at boot; the three cycles
        # reused it.
        assert built == [None]
    finally:
        deps.close()


def test_removal_engine_toggle_takes_effect_without_a_restart(
        tmp_path, store, profile_file, brokers_file, monkeypatch):
    monkeypatch.setattr(service_mod, "build_detection", lambda cfg: (None, None, []))

    sent = []

    def fake_build_removal(cfg):
        if not cfg.eraser_enabled:
            return None
        return lambda broker_id, profile: sent.append((broker_id, cfg.eraser_dry_run))

    cfg = _autopilot_cfg(tmp_path, store, profile_file, brokers_file)
    deps = autopilot.build_dependencies(cfg)
    monkeypatch.setattr(service_mod, "build_removal", fake_build_removal)
    try:
        off = deps.submit_removal("alpha", {})
        assert off["success"] is False and sent == []

        settings_mod.update_settings(store, {"eraser_enabled": True,
                                             "eraser_dry_run": False})
        deps.submit_removal("alpha", {})

        assert sent == [("alpha", False)]
    finally:
        deps.close()


def test_run_forever_picks_up_a_changed_scan_interval_on_the_next_tick(
        tmp_path, store, profile_file, brokers_file, monkeypatch):
    """interval_seconds is the one knob that is next-TICK rather than
    immediate (see run_forever's docstring). Prove it is at least that: a
    stored interval shorter than the one the loop started with must make the
    scan fire again without the process being restarted."""
    cfg = _autopilot_cfg(tmp_path, store, profile_file, brokers_file)
    scans = []
    monkeypatch.setattr(autopilot, "run_scan_cycle", lambda *a, **k: scans.append(1))
    monkeypatch.setattr(autopilot, "run_confirmation_pass", lambda *a, **k: None)
    monkeypatch.setattr(service_mod, "write_heartbeat", lambda cfg, payload: None)

    stop = threading.Event()
    ticks = {"n": 0}

    def fake_sleep(seconds):
        ticks["n"] += 1
        if ticks["n"] == 1:
            # Between tick 1 and tick 2, someone shortens the interval on
            # /settings. cfg.interval_seconds (86400, the env tier) is
            # unchanged; only the store moved.
            settings_mod.update_settings(store, {"interval_seconds": 60})
        if ticks["n"] >= 3:
            stop.set()

    autopilot.run_forever(
        cfg, autopilot.AutopilotDependencies(),
        autopilot.Intervals(scan_seconds=86400, confirmation_seconds=600),
        stop, sleep=fake_sleep,
    )

    # Start-up scan, then at least one more once the stored 60s interval was
    # picked up -- with the original 86400s interval there would be exactly 1.
    assert len(scans) > 1
