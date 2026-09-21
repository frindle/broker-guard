"""Adversarial fixture for: bg-dashboard-s1-app-and-status

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

from fastapi.testclient import TestClient


def _client():
    return TestClient(target.app)


def _case_status_200_with_fixture_data():
    def run(monkeypatch):
        monkeypatch.setattr(
            target.webui_data, "escalation_countdowns",
            lambda records, now_iso: [
                {"broker_id": "spokeo", "stage": "final_notice", "deadline_iso": now_iso,
                 "seconds_remaining": -3600, "overdue": True},
            ])
        monkeypatch.setattr(
            target.webui_data, "load_health_summary",
            lambda lines: {"total": 12, "ok": 9, "failed": 3, "by_broker": {}})
        r = _client().get("/")
        return r.status_code == 200 and "spokeo" in r.text and "12" in r.text
    return run


def _case_empty_data_still_200():
    def run(monkeypatch):
        monkeypatch.setattr(target.webui_data, "escalation_countdowns", lambda records, now_iso: [])
        monkeypatch.setattr(target.webui_data, "load_health_summary",
                             lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
        r = _client().get("/")
        return r.status_code == 200
    return run


def _case_raising_data_fn_still_200():
    def run(monkeypatch):
        def boom(records, now_iso):
            raise RuntimeError("no store yet")
        monkeypatch.setattr(target.webui_data, "escalation_countdowns", boom)
        monkeypatch.setattr(target.webui_data, "load_health_summary",
                             lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
        r = _client().get("/")
        return r.status_code == 200
    return run


def _case_health_raising_still_200():
    def run(monkeypatch):
        monkeypatch.setattr(target.webui_data, "escalation_countdowns", lambda records, now_iso: [])

        def boom(lines):
            raise ValueError("bad json")
        monkeypatch.setattr(target.webui_data, "load_health_summary", boom)
        r = _client().get("/")
        return r.status_code == 200
    return run


def _case_kill_test_no_escalation_call():
    """If the route stops calling escalation_countdowns, a distinctive fixture
    value can no longer appear -- this must FAIL against a mutant that drops
    the call (i.e. it must currently PASS against the real, correct impl)."""
    def run(monkeypatch):
        monkeypatch.setattr(
            target.webui_data, "escalation_countdowns",
            lambda records, now_iso: [
                {"broker_id": "ZZZ-CANARY-9f3a", "stage": "warning", "deadline_iso": now_iso,
                 "seconds_remaining": 3600, "overdue": False},
            ])
        monkeypatch.setattr(target.webui_data, "load_health_summary",
                             lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
        r = _client().get("/")
        return "ZZZ-CANARY-9f3a" in r.text
    return run


class _MonkeyPatch:
    """Minimal stand-in for pytest's monkeypatch fixture (this fixture runs
    standalone via CASES, not under pytest)."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


def _run_with_mp(fn):
    mp = _MonkeyPatch()
    try:
        return fn(mp)
    finally:
        mp.undo()


CASES = [
    ("GET / -> 200 with fixture escalation + health values in the HTML",
     lambda: _run_with_mp(_case_status_200_with_fixture_data()), True),
    ("GET / -> 200 when both data functions return empty",
     lambda: _run_with_mp(_case_empty_data_still_200()), True),
    ("GET / -> 200 even when escalation_countdowns raises",
     lambda: _run_with_mp(_case_raising_data_fn_still_200()), True),
    ("GET / -> 200 even when load_health_summary raises",
     lambda: _run_with_mp(_case_health_raising_still_200()), True),
    ("GET / renders a canary escalation value (kill test for a dropped call)",
     lambda: _run_with_mp(_case_kill_test_no_escalation_call()), True),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
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
