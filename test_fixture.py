"""Adversarial fixture for: bg-webui-s5-alerts-feed

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
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


def _digest(n):
    return {"seq": n}


CASES = [
    # limit smaller than the valid count: LAST `limit` entries, newest first.
    ("returns the last 3 of 5 digests in newest-first order",
     lambda: target.load_recent_alerts(
         ['{"seq": %d}' % n for n in range(1, 6)], limit=3),
     [_digest(5), _digest(4), _digest(3)]),

    # blank lines and unparseable lines are skipped; non-object JSON is not a
    # valid entry either (a plausible wrong fix keeps lists/strings).
    ("skips blank, garbage and non-object lines without raising",
     lambda: target.load_recent_alerts(
         ["", "   ", "{not json", "[1, 2]", '"just a string"',
          '{"seq": 7}', 'null', '{"seq": 8}'], limit=50),
     [_digest(8), _digest(7)]),

    # fewer valid entries than limit: all of them, still reversed.
     ("fewer valid lines than limit returns all, newest first",
     lambda: target.load_recent_alerts(['oops', '{"seq": 2}', '{"seq": 1}'], limit=50),
     [_digest(1), _digest(2)]),

    # default limit is 50: with 60 valid entries the FIRST 10 (oldest) drop.
    ("default limit=50 keeps only the newest 50 of 60",
     lambda: target.load_recent_alerts(['{"seq": %d}' % n for n in range(1, 61)]),
     [_digest(n) for n in range(60, 10, -1)]),

    # degenerate inputs must not raise.
    ("empty list returns []", lambda: target.load_recent_alerts([]), []),
    ("all-invalid lines return []",
     lambda: target.load_recent_alerts(["", "nope", "{x"], limit=50), []),
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
