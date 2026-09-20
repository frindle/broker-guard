"""Pure diff helpers for broker name snapshots.

Both functions are pure: they depend only on their two arguments, perform no
I/O and touch no module-level state. Inputs may be empty or contain duplicates;
outputs are always plain lists of str, deduplicated and sorted lexicographically.
"""


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
