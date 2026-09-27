"""Adversarial fixture for: bg-eraser-bridge-config

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/eraser_bridge.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


class _Completed:
    stdout = "ok"
    stderr = ""
    returncode = 0


def _bridge(**kw):
    """Build an EraserBridge with a recording fake runner; returns (bridge, calls)."""
    calls = []

    def fake(cmd, **run_kw):
        calls.append((list(cmd), dict(run_kw)))
        return _Completed()

    kw.setdefault("runner", fake)
    b = target.EraserBridge(**kw)
    return b, calls


CFG = "/home/guard/.eraser/config.yaml"


def case_submit_with_config():
    """config_path set -> submit_removal argv ends with ['--config', path]."""
    b, calls = _bridge(eraser_bin="/opt/eraser", timeout_s=42, dry_run=True, config_path=CFG)
    r = b.submit_removal("acme_broker", {"eraser_profile": "main"})
    assert len(calls) == 1, "submit_removal must invoke the runner exactly once"
    cmd, run_kw = calls[0]
    assert cmd[-2:] == ["--config", CFG], f"argv must end with --config {CFG}: {cmd}"
    assert cmd[:4] == ["/opt/eraser", "send", "--broker", "acme_broker"], f"prefix broken: {cmd}"
    assert "--dry-run" in cmd, f"dry_run=True must still land in argv: {cmd}"
    assert run_kw["timeout"] == 42 and run_kw["shell"] is False
    assert r["success"] is True and r["broker_id"] == "acme_broker" and r["dry_run"] is True
    return b.config_path


def case_submit_without_config_byte_identical():
    """config_path None (default) -> argv byte-identical to today's: no --config."""
    b, calls = _bridge(eraser_bin="eraser", timeout_s=300, dry_run=False)
    assert b.config_path is None, "default config_path must be None"
    b.submit_removal("acme_broker", {})
    cmd, run_kw = calls[0]
    want = ["eraser", "send", "--broker", "acme_broker"]
    assert cmd == want, f"argv with config_path=None must be byte-identical to today's: {cmd} != {want}"
    assert "--config" not in cmd


def case_status_with_config():
    """status forwards config_path -> argv ends with ['--config', path]."""
    b, calls = _bridge(config_path=CFG)
    r = b.status(limit=7)
    cmd, run_kw = calls[0]
    assert cmd == ["eraser", "status", "--limit", "7", "--config", CFG], f"status argv: {cmd}"
    assert r["success"] is True
    return True

def case_status_without_config():
    """status with config_path None -> no --config, byte-identical to today's."""
    b, calls = _bridge()
    b.status(limit=50)
    cmd, _ = calls[0]
    assert cmd == ["eraser", "status", "--limit", "50"], f"status argv: {cmd}"
    return True

def case_monitor_with_config():
    """monitor forwards config_path -> argv ends with ['--config', path]."""
    b, calls = _bridge(config_path=CFG)
    r = b.monitor("main")
    cmd, _ = calls[0]
    assert cmd == ["eraser", "monitor", "--profile", "main", "--config", CFG], f"monitor argv: {cmd}"
    assert r["success"] is True
    return True

def case_monitor_without_config():
    """monitor with config_path None -> no --config, byte-identical to today's."""
    b, calls = _bridge()
    b.monitor(None)
    cmd, _ = calls[0]
    assert cmd == ["eraser", "monitor"], f"monitor argv: {cmd}"
    return True

def case_fill_with_config():
    """fill forwards config_path -> argv ends with ['--config', path]."""
    b, calls = _bridge(config_path=CFG)
    r = b.fill("main")
    cmd, _ = calls[0]
    assert cmd == ["eraser", "fill", "--profile", "main", "--config", CFG], f"fill argv: {cmd}"
    assert r["success"] is True
    return True

def case_fill_without_config():
    """fill with config_path None -> no --config, byte-identical to today's."""
    b, calls = _bridge()
    b.fill(None)
    cmd, _ = calls[0]
    assert cmd == ["eraser", "fill"], f"fill argv: {cmd}"
    return True

def case_positional_and_kw_regressions():
    """Existing positional args (eraser_bin, timeout_s, dry_run) and the
    runner/cwd/env keywords keep working alongside config_path."""
    env = {"PATH": "/usr/bin"}
    b, calls = _bridge(eraser_bin="/opt/eraser", timeout_s=99, dry_run=True,
                       cwd="/tmp/work", env=env, config_path=CFG)
    assert (b.eraser_bin, b.timeout_s, b.dry_run) == ("/opt/eraser", 99, True)
    assert b.cwd == "/tmp/work" and b._env is env
    b.submit_removal("acme_broker", {})
    cmd, run_kw = calls[0]
    assert cmd[0] == "/opt/eraser" and "--dry-run" in cmd and cmd[-2:] == ["--config", CFG], f"{cmd}"
    assert run_kw["cwd"] == "/tmp/work" and run_kw["env"] is env, "runner kwargs regressed: %r" % (run_kw,)
    return True

def case_failure_result_shape_preserved():
    """A failing eraser (non-zero exit) still yields the structured failure dict."""
    class _Fail(_Completed):
        stdout = "failed to load config"
        returncode = 1

    calls = []

    def fake(cmd, **run_kw):
        calls.append(list(cmd))
        return _Fail()

    b = target.EraserBridge(runner=fake, config_path=CFG)
    r = b.submit_removal("acme_broker", {})
    assert r["success"] is False and "failed to load config" in r["detail"], f"{r}"
    assert calls[0][-2:] == ["--config", CFG]
    return True
CASES = [
    ("submit_removal with config_path ends argv with --config <path>", case_submit_with_config, CFG),
    ("submit_removal with default (None) is byte-identical to today's argv", case_submit_without_config_byte_identical, None),
    ("status forwards config_path; argv ends with --config <path>", case_status_with_config, True),
    ("status without config_path stays byte-identical", case_status_without_config, True),
    ("monitor forwards config_path; argv ends with --config <path>", case_monitor_with_config, True),
    ("monitor without config_path stays byte-identical", case_monitor_without_config, True),
    ("fill forwards config_path; argv ends with --config <path>", case_fill_with_config, True),
    ("fill without config_path stays byte-identical", case_fill_without_config, True),
    ("positional args + runner/cwd/env keywords keep working", case_positional_and_kw_regressions, True),
    ("failure result shape preserved with config_path set", case_failure_result_shape_preserved, True),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
