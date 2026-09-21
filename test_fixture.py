"""Adversarial fixture for: bg-webui-s1-presence-query

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


def _conn_with_rows():
    """In-memory db with the exact presence schema from broker_guard/state.py,
    seeded so last_seen ordering is unambiguous (ISO timestamps sort lexically)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE presence ("
        "identity_key TEXT, "
        "broker_id TEXT, "
        "first_seen TEXT, "
        "last_seen TEXT, "
        "PRIMARY KEY (identity_key, broker_id))"
    )
    rows = [
        # (identity_key, broker_id, first_seen, last_seen)
        ("alice", "b1", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"),
        ("bob",   "b1", "2026-01-02T00:00:00Z", "2026-01-05T00:00:00Z"),
        ("alice", "b2", "2026-01-04T00:00:00Z", "2026-01-06T00:00:00Z"),
        ("carol", "b3", "2026-01-05T00:00:00Z", "2026-01-07T00:00:00Z"),
    ]
    conn.executemany("INSERT INTO presence VALUES (?,?,?,?)", rows)
    conn.commit()
    return conn


def _empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE presence ("
        "identity_key TEXT, broker_id TEXT, first_seen TEXT, last_seen TEXT, "
        "PRIMARY KEY (identity_key, broker_id))"
    )
    return conn



CASES = [
    # Empty table: must be [] and must not raise.
    ("empty table returns []", lambda: target.query_presence_history(_empty_conn()), []),

    # Unfiltered: all 4 rows, newest last_seen first (carol/b3 -> alice/b2 -> bob/b1 -> alice/b1).
    ("unfiltered returns every row ordered by last_seen DESC as dicts",
     lambda: target.query_presence_history(_conn_with_rows()),
     [
         {"identity_key": "carol", "broker_id": "b3", "first_seen": "2026-01-05T00:00:00Z", "last_seen": "2026-01-07T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b2", "first_seen": "2026-01-04T00:00:00Z", "last_seen": "2026-01-06T00:00:00Z"},
         {"identity_key": "bob",   "broker_id": "b1", "first_seen": "2026-01-02T00:00:00Z", "last_seen": "2026-01-05T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b1", "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-01-03T00:00:00Z"},
     ]),

    # Filtered to one broker_id: only b1's rows, still newest-first. A wrong fix
    # that filters on identity_key or forgets the WHERE clause fails this.
    ("broker_id filter returns only that broker's rows",
     lambda: target.query_presence_history(_conn_with_rows(), "b1"),
     [
         {"identity_key": "bob",   "broker_id": "b1", "first_seen": "2026-01-02T00:00:00Z", "last_seen": "2026-01-05T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b1", "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-01-03T00:00:00Z"},
     ]),

    # Unknown broker_id: [] not an error.
    ("unknown broker_id returns []",
     lambda: target.query_presence_history(_conn_with_rows(), "nope"), []),

    # limit boundary: exactly `limit` rows, the NEWEST ones (a fix that caps
    # without ordering, or orders ASC, fails this).
    ("limit=2 keeps only the two newest rows",
     lambda: target.query_presence_history(_conn_with_rows(), None, 2),
     [
         {"identity_key": "carol", "broker_id": "b3", "first_seen": "2026-01-05T00:00:00Z", "last_seen": "2026-01-07T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b2", "first_seen": "2026-01-04T00:00:00Z", "last_seen": "2026-01-06T00:00:00Z"},
     ]),

    # limit larger than the table: all rows, no padding.
    ("limit=99 returns all 4 rows when fewer exist",
     lambda: target.query_presence_history(_conn_with_rows(), None, 99),
     [
         {"identity_key": "carol", "broker_id": "b3", "first_seen": "2026-01-05T00:00:00Z", "last_seen": "2026-01-07T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b2", "first_seen": "2026-01-04T00:00:00Z", "last_seen": "2026-01-06T00:00:00Z"},
         {"identity_key": "bob",   "broker_id": "b1", "first_seen": "2026-01-02T00:00:00Z", "last_seen": "2026-01-05T00:00:00Z"},
         {"identity_key": "alice", "broker_id": "b1", "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-01-03T00:00:00Z"},
     ]),

    # Default limit is 200 (signature default, not a required argument).
    ("default limit=200 is applied without passing it",
     lambda: target.query_presence_history(_conn_with_rows(), "b1") == target.query_presence_history(_conn_with_rows(), "b1", 200),
     True),
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
