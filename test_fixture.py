"""Adversarial fixture for: bg-webui-s2-health-summary

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
import json
import sqlite3
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui_data.py')
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

ZEROED = {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}


def _report(total, ok, failed, by_broker):
    return json.dumps({"total": total, "ok": ok, "failed": failed,
                       "by_broker": by_broker})


R1 = {"total": 3, "ok": 2, "failed": 1, "by_broker": {"a": {"ok": True}}}
R2 = {"total": 5, "ok": 4, "failed": 1, "by_broker": {"b": {"ok": False}}}


CASES = [
    # --- load_health_summary: the contract ---------------------------------
    ("empty lines list returns the zeroed summary",
     lambda: target.load_health_summary([]), ZEROED),

    ("every line unparseable returns the zeroed summary, no raise",
     lambda: target.load_health_summary(["not json at all", "{broken", ""]),
     ZEROED),

    ("lines containing None must not raise and yield the zeroed summary",
     lambda: target.load_health_summary([None, "garbage"]),  # type: ignore[list-item]
     ZEROED),

    ("JSON that parses but is NOT an object (array/string/null) is skipped",
     lambda: target.load_health_summary(["[1, 2]", "\"hello\"", "null"]),
     ZEROED),

    ("returns the LAST valid line, not the first (most recent report wins)",
     lambda: target.load_health_summary([_report(3, 2, 1, R1["by_broker"]),
                                         _report(5, 4, 1, R2["by_broker"])]),
     R2),

    ("a trailing garbage line after a valid one is skipped, not fatal",
     lambda: target.load_health_summary([_report(3, 2, 1, R1["by_broker"]), "oops"]),
     R1),

    ("valid report sandwiched between garbage lines is still found",
     lambda: target.load_health_summary(["junk", _report(5, 4, 1, R2["by_broker"]), "junk"]),
     R2),

    # --- regression half: prior slice's work must keep working --------------
    ("query_presence_history still returns rows newest-first (regression)",
     lambda: _presence_rows(),
     [{"identity_key": "k2", "broker_id": "b1", "first_seen": 2, "last_seen": 9},
      {"identity_key": "k1", "broker_id": "b1", "first_seen": 1, "last_seen": 8}]),

    ("query_presence_history broker filter still works (regression)",
     lambda: _presence_rows(broker_id="other"),
     []),
]


def _presence_rows(broker_id=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE presence (identity_key TEXT, broker_id TEXT,"
                 " first_seen INTEGER, last_seen INTEGER)")
    conn.executemany(
        "INSERT INTO presence VALUES (?, ?, ?, ?)",
        [("k1", "b1", 1, 8), ("k2", "b1", 2, 9)])
    try:
        return target.query_presence_history(conn, broker_id)
    finally:
        conn.close()


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
