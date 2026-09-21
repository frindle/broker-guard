"""Adversarial fixture for: bg-webui-s3-escalation-audit

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

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui_data.py')
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


NOW = "2026-09-20T12:00:00+00:00"


def _rec(broker_id, stage, deadline_iso):
    return {"broker_id": broker_id, "stage": stage, "deadline_iso": deadline_iso}


CASES = [
    # Mixed overdue/future rows come back sorted by seconds_remaining ascending
    # (most overdue first), with correct per-row values and key set. Catches a
    # descending sort, an inverted sign on seconds_remaining, or swapped keys.
    ("mixed rows: sorted most-overdue-first with exact values",
     lambda: target.escalation_countdowns(
         [_rec("b2", "warn", "2026-09-20T12:30:00+00:00"),
          _rec("b1", "critical", "2026-09-20T11:58:30+00:00"),
          _rec("b3", "warn", "2026-09-20T11:45:00+00:00")], NOW),
     [
         {"broker_id": "b3", "stage": "warn", "deadline_iso": "2026-09-20T11:45:00+00:00",
          "seconds_remaining": -900, "overdue": True},
         {"broker_id": "b1", "stage": "critical", "deadline_iso": "2026-09-20T11:58:30+00:00",
          "seconds_remaining": -90, "overdue": True},
         {"broker_id": "b2", "stage": "warn", "deadline_iso": "2026-09-20T12:30:00+00:00",
          "seconds_remaining": 1800, "overdue": False},
     ]),
    # Boundary: deadline exactly at now -> seconds_remaining == 0 and overdue is
    # False (strictly < 0). Catches an `<=` comparison or off-by-one rounding.
    ("deadline equal to now: zero remaining, not overdue",
     lambda: target.escalation_countdowns(
         [_rec("b9", "warn", NOW)], NOW),
     [{"broker_id": "b9", "stage": "warn", "deadline_iso": NOW,
       "seconds_remaining": 0, "overdue": False}]),
    # One second past due is the smallest negative: -1 with overdue True.
    ("one second past deadline: seconds_remaining == -1, overdue True",
     lambda: target.escalation_countdowns(
         [_rec("b4", "critical", "2026-09-20T11:59:59+00:00")], NOW),
     [{"broker_id": "b4", "stage": "critical", "deadline_iso": "2026-09-20T11:59:59+00:00",
       "seconds_remaining": -1, "overdue": True}]),
    # Degenerate input: no records -> empty list, not an error.
    ("empty record list returns []",
     lambda: target.escalation_countdowns([], NOW),
     []),
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
