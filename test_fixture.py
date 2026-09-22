"""Adversarial fixture for: bg-dashboard-s3-brokers-matched-removed

Strategy: patch ``broker_guard.state.init_db`` and
``webui_data.query_presence_history`` (the two collaborators the route must go
through), then call the /brokers handler directly. This proves the route is
WIRED to those collaborators -- an implementation that opens its own db or
invents its own query function fails, because the patched fakes are never used
and the real sqlite path blows up (or returns no rows).

Cases:
  * two seeded rows -> every value rendered in the HTML
  * zero rows -> page still renders (header present, no data rows), no crash
  * hostile values -> html-escaped, raw ``<script>`` never reaches the output
  * page skeleton intact -- doctype, title, h1, table open/close, body close
    all present (each is its own line in the reference impl; dropping any one
    must be caught)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui.py')
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

import broker_guard.state as state_mod
import broker_guard.webui_data as webui_data


class _FakeConn:
    def close(self):
        pass


def _render(rows):
    """Patch the two collaborators, render /brokers, restore. Returns (html, paths)."""
    orig_init = state_mod.init_db
    orig_query = webui_data.query_presence_history
    seen = []

    def fake_init(path):
        seen.append(path)
        return _FakeConn()

    state_mod.init_db = fake_init
    webui_data.query_presence_history = lambda conn, broker_id=None, limit=200: list(rows)
    try:
        html_out = target.brokers()
    finally:
        state_mod.init_db = orig_init
        webui_data.query_presence_history = orig_query
    return html_out, seen


CASES = [
    (
        "two seeded rows: every value rendered",
        lambda: (lambda out, seen: all(v in out for v in ("ik-1", "broker-a", "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z")) and len(seen) == 1)(
            *_render([
                {"identity_key": "ik-1", "broker_id": "broker-a",
                 "first_seen": "2024-01-01T00:00:00Z", "last_seen": "2024-06-01T00:00:00Z"},
                {"identity_key": "ik-2", "broker_id": "broker-b",
                 "first_seen": "2023-05-05T00:00:00Z", "last_seen": "2024-07-07T00:00:00Z"},
            ])
        ),
        True,
    ),
    (
        "zero rows: page still renders header, no data rows, no crash",
        lambda: (lambda out, seen: "<th>identity_key</th>" in out and "<td>" not in out)(*_render([])),
        True,
    ),
    (
        "hostile values are html-escaped; raw <script> never reaches output",
        lambda: (lambda out, seen: "<script>alert(1)</script>" not in out and "&lt;script&gt;" in out)(
            *_render([
                {"identity_key": "<script>alert(1)</script>", "broker_id": "b&x",
                 "first_seen": "2024-01-01T00:00:00Z", "last_seen": "2024-06-01T00:00:00Z"},
            ])
        ),
        True,
    ),
]


SKELETON = (
    "<!DOCTYPE html>",
    "<html><head><title>Broker Guard -- Brokers</title></head><body>",
    "<h1>Brokers</h1>",
    '<table border="1">',
    "</table>",
    "</body></html>",
)

CASES = CASES + [
    (
        "page skeleton intact: doctype/title/h1/table open+close/body close all present",
        lambda: (lambda out, seen: all(s in out for s in SKELETON))(*_render([])),
        True,
    ),
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
