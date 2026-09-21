#!/usr/bin/env python3
"""Reference impl for: bg-webui-s1-presence-query

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

The target file currently holds only a placeholder stub docstring, so this
writer emits the complete module. The presence table's real schema comes from
broker_guard/state.py: columns identity_key, broker_id, first_seen, last_seen
-- "checked_at" in the intent maps to last_seen (the check-time column).
"""
import pathlib

wt = pathlib.Path(__file__).resolve().parent if len(__import__("sys").argv) < 2 else pathlib.Path(__import__("sys").argv[1])
p = wt / 'broker_guard/webui_data.py'
p.parent.mkdir(parents=True, exist_ok=True)

NEW = '''"""WebUI data access: read-only queries over the presence state db."""
import sqlite3


def query_presence_history(conn: sqlite3.Connection, broker_id: str | None = None, limit: int = 200) -> list[dict]:
    """Return recent presence rows as dicts, newest check first.

    Reads the existing ``presence`` table (columns identity_key, broker_id,
    first_seen, last_seen -- see broker_guard/state.py), optionally filtered to
    one ``broker_id``, ordered by ``last_seen DESC`` and capped at ``limit``
    rows. Each row is returned as a dict keyed by those column names; an empty
    table or an unknown broker_id yields [].
    """
    if broker_id is None:
        cur = conn.execute(
            "SELECT identity_key, broker_id, first_seen, last_seen FROM presence ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        )
    else:
        cur = conn.execute(
            "SELECT identity_key, broker_id, first_seen, last_seen FROM presence WHERE broker_id = ? ORDER BY last_seen DESC LIMIT ?",
            (broker_id, limit),
        )
    return [
        {"identity_key": row[0], "broker_id": row[1], "first_seen": row[2], "last_seen": row[3]}
        for row in cur.fetchall()
    ]
'''

p.write_text(NEW)
print("refimpl applied")
