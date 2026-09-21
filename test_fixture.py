"""Adversarial fixture for: bg-webui-s4-auth-token

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


TOKEN = "s3cr3t-token"

CASES = [
    # happy path: exact match returns True (a `provided == expected`-style fix
    # passes this, so the other cases below are what discriminate)
    ("exact match is accepted", lambda: target.verify_token(TOKEN, TOKEN), True),
    # provided=None must return False WITHOUT raising, even with a non-empty expected
    ("provided=None fails closed without raising",
     lambda: target.verify_token(None, TOKEN), False),
    # empty-string provided must return False without raising
    ("empty provided fails closed", lambda: target.verify_token("", TOKEN), False),
    # wrong token is rejected (catches an inverted comparison)
    ("wrong token is rejected", lambda: target.verify_token("other-token", TOKEN), False),
    # prefix of the expected token must be rejected (no partial/startswith match)
    ("prefix of expected is rejected", lambda: target.verify_token(TOKEN[:-1], TOKEN), False),
    # empty expected fails closed even when provided matches nothing meaningful
    ("empty expected fails closed", lambda: target.verify_token("anything", ""), False),
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
