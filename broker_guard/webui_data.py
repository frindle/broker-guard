"""Read-only webUI queries over the presence state db (see broker_guard/state.py).

This module only reads: it never writes, commits or mutates the connection.
The ``presence`` table's schema is owned by ``state.init_db`` -- columns
``identity_key``, ``broker_id``, ``first_seen``, ``last_seen`` (the check-time
column is ``last_seen``).
"""
import hmac
import json
import sqlite3
from datetime import datetime


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


def escalation_countdowns(records: list[dict], now_iso: str) -> list[dict]:
    """Turn escalation records into webUI countdown rows.

    Each input record is ``{'broker_id': str, 'stage': str,
    'deadline_iso': str}``. Returns one dict per record with exactly the keys
    ``broker_id``, ``stage``, ``deadline_iso`` (copied through unchanged),
    ``seconds_remaining`` (int whole seconds of deadline minus now; negative
    once the deadline has passed) and ``overdue`` (bool, true exactly when
    ``seconds_remaining < 0`` -- a deadline equal to now is NOT overdue).
    Rows are sorted by ``seconds_remaining`` ascending, so the most overdue /
    most urgent rows come first. An empty ``records`` list returns ``[]``.
    """
    now = datetime.fromisoformat(now_iso)
    rows = []
    for record in records:
        deadline = datetime.fromisoformat(record["deadline_iso"])
        seconds_remaining = int((deadline - now).total_seconds())
        rows.append({
            "broker_id": record["broker_id"],
            "stage": record["stage"],
            "deadline_iso": record["deadline_iso"],
            "seconds_remaining": seconds_remaining,
            "overdue": seconds_remaining < 0,
        })
    return sorted(rows, key=lambda row: row["seconds_remaining"])


def verify_token(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def load_recent_alerts(lines: list[str], limit: int = 50) -> list[dict]:
    """Return the LAST ``limit`` valid alert digests, newest first.

    Each element of ``lines`` is one JSON object as appended to
    ``logs/alerts.jsonl`` (an alert digest). Blank lines and lines that fail
    to parse are skipped without raising; non-object JSON is not a valid
    entry either. The file's natural append order is oldest-to-newest, so the
    result is the last ``limit`` valid entries in REVERSED (newest-first)
    order. An empty list or an all-invalid one returns ``[]``.
    """
    valid = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            valid.append(entry)
    if limit <= 0:
        return []
    return list(reversed(valid[-limit:]))
