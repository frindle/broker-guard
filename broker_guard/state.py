"""Presence state: pure snapshot diffs plus the SQLite store behind them.

``new_appearances`` and ``resolved`` are pure: they depend only on their two
arguments, perform no I/O and touch no module-level state. Inputs may be empty
or contain duplicates; outputs are always plain lists of str, deduplicated and
sorted lexicographically.

The remaining helpers (``init_db``, ``record_presence``, ``get_present``) own
the SQLite side, and ``StateStore`` wraps them in the object interface that
``orchestrator.run_cycle`` expects.
"""
import os
import sqlite3


def init_db(path: str):
    """Open (creating if needed) the sqlite db at ``path`` and return the connection.

    Idempotent: safe to call more than once on the same path -- CREATE TABLE IF
    NOT EXISTS means no exception and no data loss. The ``presence`` table's
    composite primary key (identity_key, broker_id) is what
    ``record_presence``'s ON CONFLICT upsert relies on.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    # The state db holds PII (which brokers list this person): keep it
    # owner-only on disk, not whatever the process umask happened to be.
    try:
        if path != ":memory:" and os.path.exists(path):
            os.chmod(path, 0o600)
    except OSError:
        pass
    # WAL keeps a reader (e.g. a status query) from blocking the writer, and
    # the busy timeout turns a concurrent-run collision into a wait rather
    # than an immediate "database is locked".
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS presence ("
        "identity_key TEXT, "
        "broker_id TEXT, "
        "first_seen TEXT, "
        "last_seen TEXT, "
        "PRIMARY KEY (identity_key, broker_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS broker_status ("
        "broker_id TEXT, "
        "identity_key TEXT, "
        "status TEXT, "
        "updated_at TEXT)"
    )
    return conn


def new_appearances(prev: list[str], current: list[str]) -> list[str]:
    """Names present in ``current`` but absent from ``prev`` (forward diff).

    Returns exactly ``sorted(set(current) - set(prev))`` -- deduplicated,
    sorted lexicographically. Empty or duplicate inputs never raise.
    """
    return sorted(set(current) - set(prev))


def resolved(prev: list[str], current: list[str]) -> list[str]:
    """Names present in ``prev`` but no longer in ``current`` (reverse diff).

    Returns exactly ``sorted(set(prev) - set(current))`` -- deduplicated,
    sorted lexicographically. Empty or duplicate inputs never raise.
    """
    return sorted(set(prev) - set(current))


def record_presence(conn, identity_key: str, broker_id: str, seen_at: str):
    """Record that ``identity_key`` was seen by ``broker_id`` at ``seen_at``.

    Upserts exactly one row in the existing ``presence`` table keyed by the
    (identity_key, broker_id) pair: on insert both first_seen and last_seen
    are set to ``seen_at``; on conflict only last_seen is advanced and the
    original first_seen is left untouched. Commits through ``conn`` so the
    row survives closing and reopening the connection.
    """
    conn.execute(
        "INSERT INTO presence (identity_key, broker_id, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(identity_key, broker_id) DO UPDATE SET last_seen = excluded.last_seen",
        (identity_key, broker_id, seen_at, seen_at),
    )
    conn.commit()


def get_present(conn, identity_key: str) -> list[str]:
    """Broker ids that currently have a presence row for ``identity_key``.

    Runs one parameterised query selecting ``broker_id`` from the existing
    ``presence`` table where ``identity_key = ?`` and returns a plain Python
    ``list[str]`` with exactly one entry per distinct broker_id. An identity
    with no rows returns ``[]``; brokers belonging to other identities never
    appear, and repeated upserts of the same (identity_key, broker_id) pair
    do not duplicate the result.
    """
    cur = conn.execute(
        "SELECT broker_id FROM presence WHERE identity_key = ?",
        (identity_key,),
    )
    return [bid for (bid,) in cur.fetchall()]


class StateStore:
    """Object wrapper over the module-level SQLite helpers.

    ``orchestrator.run_cycle`` takes a ``state_conn`` and calls
    ``is_seen`` / ``record_appearance`` / ``seen_brokers`` on it. A raw
    ``sqlite3.Connection`` has none of those methods, so the orchestrator and
    this module could not actually be wired together -- the two slices were
    authored against different interfaces. This adapter is that missing seam;
    it delegates to the existing pure-contract functions and does not change
    their behaviour.
    """

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def open(cls, path: str) -> "StateStore":
        return cls(init_db(path))

    def seen_brokers(self, identity_key: str) -> list[str]:
        return get_present(self.conn, identity_key)

    def is_seen(self, identity_key: str, broker_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM presence WHERE identity_key = ? AND broker_id = ? LIMIT 1",
            (identity_key, broker_id),
        )
        return cur.fetchone() is not None

    def record_appearance(self, identity_key: str, broker_id: str, seen_at: str) -> None:
        record_presence(self.conn, identity_key, broker_id, seen_at)

    def touch(self, identity_key: str, broker_id: str, seen_at: str) -> None:
        """Advance last_seen for a broker that was already known."""
        record_presence(self.conn, identity_key, broker_id, seen_at)

    def forget(self, identity_key: str, broker_id: str) -> None:
        """Drop a presence row once a removal is confirmed."""
        self.conn.execute(
            "DELETE FROM presence WHERE identity_key = ? AND broker_id = ?",
            (identity_key, broker_id),
        )
        self.conn.commit()

    def set_status(self, identity_key: str, broker_id: str, status: str, updated_at: str) -> None:
        self.conn.execute(
            "DELETE FROM broker_status WHERE identity_key = ? AND broker_id = ?",
            (identity_key, broker_id),
        )
        self.conn.execute(
            "INSERT INTO broker_status (broker_id, identity_key, status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (broker_id, identity_key, status, updated_at),
        )
        self.conn.commit()

    def get_status(self, identity_key: str, broker_id: str):
        cur = self.conn.execute(
            "SELECT status FROM broker_status WHERE identity_key = ? AND broker_id = ?",
            (identity_key, broker_id),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
