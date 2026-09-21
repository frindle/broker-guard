"""Read-only webUI queries over the presence state db (see broker_guard/state.py).

This module only reads: it never writes, commits or mutates the connection.
The ``presence`` table's schema is owned by ``state.init_db`` -- columns
``identity_key``, ``broker_id``, ``first_seen``, ``last_seen`` (the check-time
column is ``last_seen``).
"""
import sqlite3


def query_presence_history(conn: sqlite3.Connection, broker_id: str | None = None, limit: int = 200) -> list[dict]:
    """Return presence rows as dicts keyed by the four column names.

    One parameterised SELECT over ``presence``, ordered by ``last_seen DESC``
    (newest check first), optionally filtered to a single ``broker_id``, capped
    at ``limit`` rows. An empty table or an unknown broker_id returns ``[]``;
    this never raises for those and never writes through ``conn``.
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
        {
            "identity_key": row[0],
            "broker_id": row[1],
            "first_seen": row[2],
            "last_seen": row[3],
        }
        for row in cur.fetchall()
    ]
