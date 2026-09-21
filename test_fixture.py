"""Adversarial fixture for: bg-dashboard-s1-app-and-status

Each case: (description, callable_returning_actual, expected)
"""
import os
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

from fastapi.testclient import TestClient


def _client():
    return TestClient(target.app)


class _MonkeyPatch:
    """Minimal stand-in for pytest's monkeypatch fixture (this fixture runs
    standalone via CASES, not under pytest)."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append(("attr", obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for kind, obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


def _run_with_mp(fn):
    mp = _MonkeyPatch()
    try:
        return fn(mp)
    finally:
        mp.undo()


def _case_status_200_with_fixture_data(mp):
    mp.setattr(
        target.webui_data, "escalation_countdowns",
        lambda records, now_iso: [
            {"broker_id": "spokeo", "stage": "final_notice", "deadline_iso": now_iso,
             "seconds_remaining": -3600, "overdue": True},
        ])
    mp.setattr(target.webui_data, "load_health_summary",
               lambda lines: {"total": 12, "ok": 9, "failed": 3, "by_broker": {}})
    r = _client().get("/")
    return r.status_code == 200 and "spokeo" in r.text and "total=12 ok=9 failed=3" in r.text


def _case_empty_data_still_200(mp):
    mp.setattr(target.webui_data, "escalation_countdowns", lambda records, now_iso: [])
    mp.setattr(target.webui_data, "load_health_summary",
               lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
    r = _client().get("/")
    return r.status_code == 200


def _case_raising_escalation_still_200(mp):
    def boom(records, now_iso):
        raise RuntimeError("no store yet")
    mp.setattr(target.webui_data, "escalation_countdowns", boom)
    mp.setattr(target.webui_data, "load_health_summary",
               lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
    r = _client().get("/")
    return r.status_code == 200


def _case_raising_health_falls_back_to_zeros(mp):
    mp.setattr(target.webui_data, "escalation_countdowns", lambda records, now_iso: [])

    def boom(lines):
        raise ValueError("bad json")
    mp.setattr(target.webui_data, "load_health_summary", boom)
    r = _client().get("/")
    # Pins the exact fallback values on the exception path (line 28's
    # 0/0/0 dict) -- a mutant that changes any of those zeros must fail this.
    return r.status_code == 200 and "total=0 ok=0 failed=0" in r.text


def _case_kill_test_no_escalation_call(mp):
    """If the route stops calling escalation_countdowns, a distinctive
    fixture value can no longer appear."""
    mp.setattr(
        target.webui_data, "escalation_countdowns",
        lambda records, now_iso: [
            {"broker_id": "ZZZ-CANARY-9f3a", "stage": "warning", "deadline_iso": now_iso,
             "seconds_remaining": 3600, "overdue": False},
        ])
    mp.setattr(target.webui_data, "load_health_summary",
               lambda lines: {"total": 0, "ok": 0, "failed": 0, "by_broker": {}})
    r = _client().get("/")
    return "ZZZ-CANARY-9f3a" in r.text


def _case_real_health_file_read_at_default_path(mp):
    """Does NOT monkeypatch HEALTH_LOG_PATH or load_health_summary: proves the
    route actually reads target.HEALTH_LOG_PATH (its real, default value, not
    a mocked one) with .readlines() and feeds it through the real
    webui_data.load_health_summary. Exercises the open()/readlines() line and
    pins the exact default path string -- a mutated path or a deleted
    readlines() call makes this file invisible and the seeded totals vanish."""
    mp.setattr(target.webui_data, "escalation_countdowns", lambda records, now_iso: [])
    path = target.HEALTH_LOG_PATH
    made_dir = None
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
        made_dir = parent
    with open(path, "w") as f:
        f.write('{"total": 17, "ok": 12, "failed": 5, "by_broker": {}}\n')
    try:
        r = _client().get("/")
        return r.status_code == 200 and "total=17 ok=12 failed=5" in r.text
    finally:
        os.remove(path)
        if made_dir:
            try:
                os.rmdir(made_dir)
            except OSError:
                pass


CASES = [
    ("GET / -> 200 with fixture escalation + health values in the HTML",
     lambda: _run_with_mp(_case_status_200_with_fixture_data), True),
    ("GET / -> 200 when both data functions return empty",
     lambda: _run_with_mp(_case_empty_data_still_200), True),
    ("GET / -> 200 even when escalation_countdowns raises",
     lambda: _run_with_mp(_case_raising_escalation_still_200), True),
    ("GET / falls back to the exact 0/0/0 summary when load_health_summary raises",
     lambda: _run_with_mp(_case_raising_health_falls_back_to_zeros), True),
    ("GET / renders a canary escalation value (kill test for a dropped call)",
     lambda: _run_with_mp(_case_kill_test_no_escalation_call), True),
    ("GET / really reads HEALTH_LOG_PATH and parses it via load_health_summary",
     lambda: _run_with_mp(_case_real_health_file_read_at_default_path), True),
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
