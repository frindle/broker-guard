"""Frozen test: broker-guard-removal-dormant.

Removal is handled outside broker-guard now. A single env-only master switch,
BG_REMOVAL_ENABLED (Config.removal_enabled, default False), must force every
removal / opt-out / email path off in settings.effective_config, even when the
dashboard settings store turns one on. Removal code stays in place (dormant).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from broker_guard import settings as settings_mod  # noqa: E402
from broker_guard.config import Config, load_config  # noqa: E402

REMOVAL = ("eraser_enabled", "optout_submit_enabled",
           "optout_email_enabled", "assist_enabled")


def _store():
    d = tempfile.mkdtemp()
    return os.path.join(d, "settings.json")


def test_default_is_off():
    assert Config().removal_enabled is False
    assert load_config({}).removal_enabled is False
    assert load_config({"BG_REMOVAL_ENABLED": "true"}).removal_enabled is True


def test_removal_keys_constant():
    assert tuple(settings_mod.REMOVAL_KEYS) == REMOVAL
    for key in REMOVAL:
        assert hasattr(Config(), key)


def test_store_cannot_enable_removal_when_dormant():
    store = _store()
    settings_mod.update_settings(store, {k: True for k in REMOVAL})
    cfg = settings_mod.effective_config(Config(settings_path=store), store)
    for key in REMOVAL:
        assert getattr(cfg, key) is False, key


def test_env_flags_forced_off_when_dormant():
    store = _store()
    raw = Config(settings_path=store, **{k: True for k in REMOVAL})
    cfg = settings_mod.effective_config(raw, store)
    assert [getattr(cfg, k) for k in REMOVAL] == [False] * len(REMOVAL)
    assert cfg.removal_enabled is False


def test_master_switch_restores_store_control():
    store = _store()
    settings_mod.update_settings(store, {"optout_submit_enabled": True,
                                         "eraser_enabled": True})
    cfg = settings_mod.effective_config(
        Config(settings_path=store, removal_enabled=True), store)
    assert cfg.optout_submit_enabled is True
    assert cfg.eraser_enabled is True
    assert cfg.optout_email_enabled is False


TESTS = [test_default_is_off, test_removal_keys_constant,
         test_store_cannot_enable_removal_when_dormant,
         test_env_flags_forced_off_when_dormant,
         test_master_switch_restores_store_control]

if __name__ == "__main__":
    order = list(reversed(TESTS)) if os.environ.get("TEST_ORDER") == "reverse" else TESTS
    failed = 0
    for t in order:
        try:
            t()
            print(f"RESULT {t.__name__} PASS")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"RESULT {t.__name__} FAIL {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
