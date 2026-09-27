"""Adversarial fixture for: bg-retire-searxng-for-playwright

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
import contextlib
import logging
import sys
import importlib.util
from unittest import mock

spec = importlib.util.spec_from_file_location("target", 'broker_guard/service.py')
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

from broker_guard.config import Config  # noqa: E402


def _cfg(url=None, pw=False):
    return Config(searxng_url=url, playwright_enabled=pw)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _logs():
    cap = _LogCapture()
    lg = logging.getLogger("broker_guard.service")
    old = list(lg.handlers)
    old_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    try:
        yield cap
    finally:
        lg.removeHandler(cap)
        lg.handlers.extend(old)
        lg.setLevel(old_level)


def case_gate_on_url_set():
    """playwright ON + URL set -> NO SearxClient at all (the core gate)."""
    from broker_guard import searx_client as sc
    with mock.patch.object(sc, "SearxClient") as fake:
        searx, _pa, _closers = target.build_detection(_cfg("http://searx.local", pw=True))
        assert not fake.called, "SearxClient was constructed while playwright_enabled"
    return searx is None


def case_gate_off_url_set():
    """playwright OFF + URL set -> SearxClient built exactly as today."""
    from broker_guard.searx_client import SearxClient
    searx, _pa, _closers = target.build_detection(_cfg("http://searx.local", pw=False))
    return isinstance(searx, SearxClient)


def case_gate_off_no_url():
    """playwright OFF + no URL -> None AND the warning log preserved verbatim."""
    with _logs() as cap:
        searx, _pa, _closers = target.build_detection(_cfg(None, pw=False))
    warned = any(
        r.levelno == logging.WARNING and "no SearXNG URL configured" in r.getMessage()
        for r in cap.records
    )
    return (searx is None) and warned


def case_gate_on_no_url():
    """playwright ON + no URL -> still None, gate must not raise."""
    searx, _pa, _closers = target.build_detection(_cfg(None, pw=True))
    return searx is None


def case_permanent_error_still_handled():
    """playwright OFF + invalid URL -> PermanentSearxError caught, error logged."""
    with _logs() as cap:
        searx, _pa, _closers = target.build_detection(_cfg("not-a-url", pw=False))
    errored = any(
        r.levelno == logging.ERROR and "searxng disabled" in r.getMessage()
        for r in cap.records
    )
    return (searx is None) and errored


def case_page_action_unchanged():
    """playwright ON -> page_action still built via make_page_action, closer kept."""
    from broker_guard import search_probe as sp
    sentinel_pa = object()
    sentinel_closer = lambda: None  # noqa: E731
    with mock.patch.object(sp, "make_page_action", return_value=(sentinel_pa, sentinel_closer)):
        _searx, pa, closers = target.build_detection(_cfg("http://searx.local", pw=True))
    return (pa is sentinel_pa) and (closers == [sentinel_closer])



CASES = [
    ("playwright ON + URL set -> no SearxClient built at all", case_gate_on_url_set, True),
    ("playwright OFF + URL set -> SearxClient still built (regression)", case_gate_off_url_set, True),
    ("playwright OFF + no URL -> None and warning log preserved verbatim", case_gate_off_no_url, True),
    ("playwright ON + no URL -> None, gate does not raise", case_gate_on_no_url, True),
    ("playwright OFF + invalid URL -> PermanentSearxError handled as today", case_permanent_error_still_handled, True),
    ("page_action construction untouched when playwright ON", case_page_action_unchanged, True),
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
