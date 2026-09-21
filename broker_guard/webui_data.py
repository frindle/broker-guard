"""Read-only webUI queries over the presence state db (see broker_guard/state.py).

This module only reads: it never writes, commits or mutates the connection.
The ``presence`` table's schema is owned by ``state.init_db`` -- columns
``identity_key``, ``broker_id``, ``first_seen``, ``last_seen`` (the check-time
column is ``last_seen``).
"""
import json
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


def load_health_summary(lines: list[str]) -> dict:
    """Return the most recent ``build_report`` JSON line from a rolling log.

    Each element of ``lines`` is one JSON object as written by
    ``broker_guard/health.py``'s ``build_report`` (keys ``total``, ``ok``,
    ``failed``, ``by_broker``). Only the LAST line that parses to a dict is
    returned; unparseable lines, non-object JSON and ``None`` entries are
    skipped without raising. An empty list or an all-invalid one yields the
    zeroed summary ``{'total': 0, 'ok': 0, 'failed': 0, 'by_broker': {}}``.
    """
    for line in reversed(lines):
        try:
            report = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(report, dict):
            return report
    return {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}
