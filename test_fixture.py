"""Adversarial fixture for: bg-eraser-cmd

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

spec = importlib.util.spec_from_file_location("target", 'broker_guard/eraser.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with the
# task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


PII_PROFILE = {"name": "Jane Q. Doe", "email": "jane@example.com"}


def _has_config(argv, path):
    return argv[-2:] == ["--config", path] and argv.count("--config") == 1


CASES = [
    # --- regression: both keywords absent -> byte-identical to today's argv --
    ("send baseline unchanged (no config/profile)",
     lambda: target.build_eraser_cmd("acme", PII_PROFILE, dry_run=True),
     ["eraser", "send", "--broker", "acme", "--dry-run"]),

    # --- the defect itself: --config emitted as exactly two argv items -------
    ("send emits exactly ['--config', path] when config_path given",
     lambda: _has_config(target.build_eraser_cmd("acme", {}, config_path="/etc/eraser/config.yaml"), "/etc/eraser/config.yaml"),
     True),

    ("status baseline unchanged (no config)",
     lambda: target.build_eraser_status_cmd(limit=10),
     ["eraser", "status", "--limit", "10"]),

    ("status emits exactly ['--config', path] when config_path given",
     lambda: _has_config(target.build_eraser_status_cmd(config_path="/opt/eraser/c.yaml"), "/opt/eraser/c.yaml"),
     True),

    ("monitor emits exactly ['--config', path] when config_path given",
     lambda: _has_config(target.build_eraser_monitor_cmd(config_path="/opt/eraser/c.yaml"), "/opt/eraser/c.yaml"),
     True),

    ("fill emits exactly ['--config', path] when config_path given",
     lambda: _has_config(target.build_eraser_fill_cmd(config_path="/opt/eraser/c.yaml"), "/opt/eraser/c.yaml"),
     True),

    # --- boundary: empty-string config_path must NOT emit the flag -----------
    ("empty-string config_path emits no --config (send)",
     lambda: "--config" in target.build_eraser_cmd("acme", {}, config_path=""),
     False),

    ("None config_path emits no --config (status)",
     lambda: "--config" in target.build_eraser_status_cmd(config_path=None),
     False),

    # --- profile_id keyword precedence over the dict's eraser_profile -------
    ("profile_id kw wins over dict eraser_profile",
     lambda: target.build_eraser_cmd("acme", {"eraser_profile": "dictprof"}, profile_id="kwprof"),
     ["eraser", "send", "--broker", "acme", "--profile", "kwprof"]),

    ("dict eraser_profile still used when kw absent (regression)",
     lambda: target.build_eraser_cmd("acme", {"eraser_profile": "dictprof"}),
     ["eraser", "send", "--broker", "acme", "--profile", "dictprof"]),

    # --- no-PII rule: profile dict values never leak into argv --------------
    ("no PII from profile dict appears in send argv",
     lambda: any("Jane Q. Doe" in a or "jane@example.com" in a for a in target.build_eraser_cmd("acme", PII_PROFILE, config_path="/etc/eraser/config.yaml")),
     False),

    # --- combined: both keywords together ------------------------------------
    ("send with both config_path and profile_id",
     lambda: target.build_eraser_cmd("acme", {"eraser_profile": "dictprof"}, config_path="/c.yaml", profile_id="kw"),
     ["eraser", "send", "--broker", "acme", "--profile", "kw", "--config", "/c.yaml"]),
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
