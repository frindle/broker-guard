"""The opt-out state machine: one durable row per (identity, broker).

Why this exists
---------------
Until this module an opt-out "attempt" was only a review JSON + PNG pair
(``review.py``), and the autopilot deduped against those files: one attempt
per broker per identity, forever, whatever happened. There was no notion of
"submitted and waiting", "needs you to click an emailed link", "removed" or
"came back", so nothing could retry with backoff, re-verify, or tell Penn
what is actually waiting on him.

States
------
::

    queued -> submitted -> awaiting_user_confirm -> removed
                   |               |                  |
                   v               v                  v
                 failed         needs_user         relisted -> queued ...

* ``queued``                -- nothing sent yet (or a retry is wanted).
* ``submitted``             -- the request is with the broker; nothing is
                               needed from Penn. Waits for the listing to
                               disappear (``removed``) or the re-verify date.
* ``awaiting_user_confirm`` -- the broker emailed (or will email) Penn a
                               confirmation link. broker-guard is send-only
                               and never reads his inbox, so it asks HIM,
                               naming the sender domain to look for.
* ``removed``               -- the listing is gone (the scan's ``forget()``).
* ``failed``                -- an attempt broke; retried with backoff until
                               ``MAX_ATTEMPTS``, then ``needs_user``.
* ``needs_user``            -- automation has stopped; a human must act.
* ``relisted``              -- a removed listing reappeared; due again.

The table lives in the same SQLite file as ``presence``/``broker_status`` and
is written through ``StateStore`` hooks (``forget`` -> removed,
``record_appearance`` -> relisted), so the existing scan loop drives it with
no new plumbing.

Nothing here logs or stores a field value. ``last_reason`` is the short
reason string review records already carry (no PII by construction).
"""
import threading
from datetime import datetime, timedelta, timezone

QUEUED = "queued"
SUBMITTED = "submitted"
AWAITING_USER_CONFIRM = "awaiting_user_confirm"
REMOVED = "removed"
FAILED = "failed"
NEEDS_USER = "needs_user"
RELISTED = "relisted"

STATES = (QUEUED, SUBMITTED, AWAITING_USER_CONFIRM, REMOVED, FAILED,
          NEEDS_USER, RELISTED)

# Legal moves. A same-state "move" is always allowed (it updates columns only).
TRANSITIONS = {
    QUEUED: {SUBMITTED, AWAITING_USER_CONFIRM, FAILED, NEEDS_USER},
    SUBMITTED: {AWAITING_USER_CONFIRM, REMOVED, FAILED, NEEDS_USER, RELISTED},
    AWAITING_USER_CONFIRM: {SUBMITTED, REMOVED, FAILED, NEEDS_USER},
    REMOVED: {RELISTED, QUEUED},
    FAILED: {QUEUED, SUBMITTED, AWAITING_USER_CONFIRM, NEEDS_USER},
    NEEDS_USER: {QUEUED, SUBMITTED, AWAITING_USER_CONFIRM, REMOVED},
    RELISTED: {QUEUED, SUBMITTED, AWAITING_USER_CONFIRM, FAILED, NEEDS_USER},
}

MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 6 * 3600
BACKOFF_CAP_S = 3 * 86400
REVERIFY_DAYS = 60

_LOCK = threading.RLock()


class OptoutStateError(ValueError):
    """An illegal state change was requested."""


def backoff_seconds(attempts: int) -> int:
    """Delay before retry number *attempts* (1-based): 6h, 12h, 24h, 48h, 3d cap."""
    n = max(1, int(attempts))
    return min(BACKOFF_BASE_S * (2 ** (n - 1)), BACKOFF_CAP_S)


def _parse(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _plus(now_iso: str, seconds: float) -> str:
    return (_parse(now_iso) + timedelta(seconds=seconds)).isoformat()


def _plus_days(now_iso: str, days: int) -> str:
    return _plus(now_iso, days * 86400)


def init_tables(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS optout_attempts ("
        "identity_key TEXT NOT NULL, broker_id TEXT NOT NULL, "
        "channel TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "last_attempt_id TEXT, last_reason TEXT, "
        "last_dry_run INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at TEXT, submitted_at TEXT, removed_at TEXT, "
        "verify_after TEXT, expected_sender TEXT, user_confirmed_at TEXT, "
        "relist_count INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT, updated_at TEXT, sla_due_at TEXT, "
        "PRIMARY KEY (identity_key, broker_id))"
    )
    # A db created before sla_due_at existed: add the column in place.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(optout_attempts)")}
    if "sla_due_at" not in cols:
        conn.execute("ALTER TABLE optout_attempts ADD COLUMN sla_due_at TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS optout_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT, broker_id TEXT, "
        "at TEXT, from_state TEXT, to_state TEXT, detail TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS optout_meta ("
        "key TEXT PRIMARY KEY, value TEXT)"
    )


_COLS = ("identity_key", "broker_id", "channel", "state", "attempts",
         "last_attempt_id", "last_reason", "last_dry_run", "next_attempt_at",
         "submitted_at", "removed_at", "verify_after", "expected_sender",
         "user_confirmed_at", "relist_count", "created_at", "updated_at",
         "sla_due_at")


def _row(cur_row) -> dict:
    return dict(zip(_COLS, cur_row))


class OptoutStore:
    """State-machine operations over a sqlite connection (see module docstring)."""

    def __init__(self, conn):
        self.conn = conn
        init_tables(conn)

    # -- reads ---------------------------------------------------------------

    def get(self, identity_key: str, broker_id: str):
        cur = self.conn.execute(
            "SELECT {} FROM optout_attempts WHERE identity_key = ? AND broker_id = ?"
            .format(", ".join(_COLS)), (identity_key, broker_id))
        row = cur.fetchone()
        return _row(row) if row else None

    def list(self, identity_key: str | None = None, states=None) -> list:
        sql = "SELECT {} FROM optout_attempts".format(", ".join(_COLS))
        clauses, args = [], []
        if identity_key is not None:
            clauses.append("identity_key = ?")
            args.append(identity_key)
        if states:
            clauses.append("state IN ({})".format(",".join("?" * len(states))))
            args.extend(states)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, broker_id"
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def counts(self, identity_key: str | None = None) -> dict:
        out = {s: 0 for s in STATES}
        for row in self.list(identity_key):
            out[row["state"]] = out.get(row["state"], 0) + 1
        return out

    def events(self, identity_key: str, broker_id: str) -> list:
        cur = self.conn.execute(
            "SELECT at, from_state, to_state, detail FROM optout_events "
            "WHERE identity_key = ? AND broker_id = ? ORDER BY id",
            (identity_key, broker_id))
        return [dict(zip(("at", "from_state", "to_state", "detail"), r))
                for r in cur.fetchall()]

    # -- writes --------------------------------------------------------------

    def _event(self, ik, bid, now, old, new, detail):
        self.conn.execute(
            "INSERT INTO optout_events (identity_key, broker_id, at, from_state, "
            "to_state, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ik, bid, now, old, new, (detail or "")[:300]))

    def _set(self, ik, bid, now, to_state, detail="", **cols) -> bool:
        """Move (ik, bid) to *to_state*, creating the row if needed.

        Returns True when the STATE changed (that is what callers notify on).
        Raises ``OptoutStateError`` for an illegal move; the row is left alone.
        """
        if to_state not in STATES:
            raise OptoutStateError("unknown state: {!r}".format(to_state))
        with _LOCK:
            row = self.get(ik, bid)
            if row is None:
                self.conn.execute(
                    "INSERT INTO optout_attempts (identity_key, broker_id, state, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (ik, bid, QUEUED, now, now))
                self._event(ik, bid, now, None, QUEUED, "created")
                row = self.get(ik, bid)
            old = row["state"]
            if to_state != old and to_state not in TRANSITIONS[old]:
                raise OptoutStateError(
                    "illegal transition {} -> {} for {}".format(old, to_state, bid))
            cols["updated_at"] = now
            sets = ", ".join("{} = ?".format(k) for k in ["state"] + list(cols))
            self.conn.execute(
                "UPDATE optout_attempts SET {} WHERE identity_key = ? AND broker_id = ?"
                .format(sets), [to_state] + list(cols.values()) + [ik, bid])
            if to_state != old:
                self._event(ik, bid, now, old, to_state, detail)
            self.conn.commit()
            return to_state != old

    def ensure_queued(self, ik, bid, now, channel="") -> dict:
        with _LOCK:
            if self.get(ik, bid) is None:
                self._set(ik, bid, now, QUEUED, "queued", channel=channel)
            return self.get(ik, bid)

    def requeue(self, ik, bid, now, detail="requeued") -> bool:
        return self._set(ik, bid, now, QUEUED, detail, attempts=0,
                         next_attempt_at=None, last_dry_run=0)

    def record_attempt(self, ik: str, record: dict, now: str, *,
                       channel: str = "form", confirm_expected: bool = False,
                       expected_sender: str = "", sla_days: int | None = None) -> str:
        """Fold one review record (``review.save_attempt`` output) into the table.

        Returns the resulting state. ``record['outcome']`` mapping:

        * ``submitted``            -> ``awaiting_user_confirm`` when the broker
          is known to email a confirmation link (*confirm_expected*), else
          ``submitted``; sets ``submitted_at`` and the 60-day re-verify date.
        * ``dry_run``              -> stays ``queued`` but remembers the
          rehearsal so the pass does not re-run it every cycle.
        * ``needs_manual_action``  -> ``needs_user``.
        * ``failed`` after Submit was pressed (fields filled, so the request
          may already be spent) -> ``needs_user`` rather than a blind resend.
        * ``failed`` before anything was sent -> ``failed`` with exponential
          backoff, ``needs_user`` once ``MAX_ATTEMPTS`` is reached.
        """
        bid = record.get("broker_id")
        outcome = record.get("outcome")
        rid = record.get("id")
        reason = (record.get("reason") or "")[:300]
        with _LOCK:
            row = self.ensure_queued(ik, bid, now, channel)
            common = {"channel": channel, "last_attempt_id": rid,
                      "last_reason": reason or None}
            if outcome == "submitted":
                state = AWAITING_USER_CONFIRM if confirm_expected else SUBMITTED
                self._set(ik, bid, now, state, "submitted via " + channel,
                          submitted_at=now, verify_after=_plus_days(now, REVERIFY_DAYS),
                          attempts=row["attempts"] + 1, last_dry_run=0,
                          next_attempt_at=None,
                          expected_sender=expected_sender or row["expected_sender"],
                          sla_due_at=(_plus_days(now, sla_days) if sla_days else None),
                          **common)
                return state
            if outcome == "dry_run":
                self._set(ik, bid, now, row["state"], "dry run",
                          last_dry_run=1, **common)
                return row["state"]
            if outcome == "needs_manual_action":
                self._set(ik, bid, now, NEEDS_USER, "needs manual action",
                          attempts=row["attempts"] + 1, next_attempt_at=None,
                          last_dry_run=1 if record.get("dry_run") else 0, **common)
                return NEEDS_USER
            if outcome == "failed":
                spent = bool(record.get("fields")) and not record.get("dry_run")
                attempts = row["attempts"] + 1
                if spent or attempts >= MAX_ATTEMPTS:
                    self._set(ik, bid, now, NEEDS_USER,
                              "submit unconfirmed" if spent else "retries exhausted",
                              attempts=attempts, next_attempt_at=None, **common)
                    return NEEDS_USER
                self._set(ik, bid, now, FAILED, "attempt failed", attempts=attempts,
                          next_attempt_at=_plus(now, backoff_seconds(attempts)),
                          last_dry_run=1 if record.get("dry_run") else 0, **common)
                return FAILED
            raise OptoutStateError("unknown outcome: {!r}".format(outcome))

    def mark_user_confirmed(self, ik, bid, now) -> bool:
        """Penn says he clicked the emailed link. Waits for the listing to go."""
        row = self.get(ik, bid)
        if row is None or row["state"] != AWAITING_USER_CONFIRM:
            return False
        return self._set(ik, bid, now, SUBMITTED, "user confirmed",
                         user_confirmed_at=now,
                         verify_after=_plus_days(now, REVERIFY_DAYS))

    def mark_done_by_user(self, ik, bid, now) -> bool:
        """Penn handled a needs_user item by hand: treat as submitted."""
        row = self.get(ik, bid)
        if row is None or row["state"] != NEEDS_USER:
            return False
        return self._set(ik, bid, now, SUBMITTED, "done by user",
                         submitted_at=now, user_confirmed_at=now,
                         verify_after=_plus_days(now, REVERIFY_DAYS))

    def mark_removed(self, ik, bid, now) -> bool:
        """The listing is gone (scan ``forget()``). Only meaningful once a
        request was actually sent; a listing vanishing before any request is
        not credited to us."""
        row = self.get(ik, bid)
        if row is None or row["state"] not in (SUBMITTED, AWAITING_USER_CONFIRM):
            return False
        return self._set(ik, bid, now, REMOVED, "listing gone", removed_at=now,
                         verify_after=_plus_days(now, REVERIFY_DAYS))

    def mark_relisted(self, ik, bid, now) -> bool:
        """The listing reappeared after a removal; it is due again."""
        row = self.get(ik, bid)
        if row is None or row["state"] != REMOVED:
            return False
        return self._set(ik, bid, now, RELISTED, "listing reappeared",
                         relist_count=row["relist_count"] + 1, attempts=0,
                         next_attempt_at=None, last_dry_run=0)

    # -- scheduling ----------------------------------------------------------

    def is_due(self, ik, bid, now, dry_run: bool = True) -> bool:
        row = self.get(ik, bid)
        if row is None:
            return True
        state = row["state"]
        if state == RELISTED:
            return True
        if state == QUEUED:
            return not (row["last_dry_run"] and dry_run)
        if state == FAILED:
            nxt = _parse(row["next_attempt_at"])
            return nxt is None or nxt <= _parse(now)
        return False

    def reverify(self, ik, now, present) -> dict:
        """Re-check rows whose 60-day date has passed.

        *present* is ``callable(broker_id) -> bool`` (the scan's presence
        record). Still listed -> ``relisted`` (a resubmit is due); gone ->
        credited as removed. Either way the next check is 60 days out.
        Returns ``{'relisted': [...], 'removed': [...]}``.
        """
        out = {"relisted": [], "removed": []}
        for row in self.list(ik, states=(SUBMITTED, REMOVED)):
            due = _parse(row["verify_after"])
            if due is None or due > _parse(now):
                continue
            bid = row["broker_id"]
            if present(bid):
                if row["state"] == REMOVED:
                    if self.mark_relisted(ik, bid, now):
                        out["relisted"].append(bid)
                else:
                    self._set(ik, bid, now, RELISTED, "still listed at re-verify",
                              relist_count=row["relist_count"] + 1, attempts=0,
                              next_attempt_at=None, last_dry_run=0)
                    out["relisted"].append(bid)
            else:
                if row["state"] == SUBMITTED:
                    self._set(ik, bid, now, REMOVED, "absent at re-verify",
                              removed_at=now)
                    out["removed"].append(bid)
                self.conn.execute(
                    "UPDATE optout_attempts SET verify_after = ? "
                    "WHERE identity_key = ? AND broker_id = ?",
                    (_plus_days(now, REVERIFY_DAYS), ik, bid))
                self.conn.commit()
        return out

    def overdue(self, now: str) -> list:
        """Submitted rows whose statutory response window has passed.

        Uses ``escalation.is_overdue`` so the SLA arithmetic has one home.
        """
        from broker_guard import escalation

        out = []
        for row in self.list(states=(SUBMITTED, AWAITING_USER_CONFIRM)):
            if row["sla_due_at"] and row["submitted_at"]:
                days = (_parse(row["sla_due_at"]) - _parse(row["submitted_at"])).days
                if escalation.is_overdue(row["submitted_at"], now, days):
                    out.append(row)
        return out

    def escalate_overdue(self, now: str) -> list:
        """Move overdue rows to ``needs_user`` (a regulator complaint /
        follow-up is Penn's call: ``escalation.route_escalation`` never
        auto-files those). Returns the rows moved."""
        moved = []
        for row in self.overdue(now):
            self._set(row["identity_key"], row["broker_id"], now, NEEDS_USER,
                      "no response within the statutory window",
                      last_reason="no response within the statutory window; "
                                  "follow up or file a complaint",
                      sla_due_at=None)
            moved.append(self.get(row["identity_key"], row["broker_id"]))
        return moved

    # -- migration / digest --------------------------------------------------

    def import_review_records(self, records: list, now: str) -> int:
        """Seed rows from existing review JSON (newest first, as
        ``review.load_attempts`` returns them). Idempotent: a pair that already
        has a row is left alone. Returns how many rows were created."""
        created = 0
        seen = set()
        for rec in records or []:
            ik, bid = rec.get("identity_key"), rec.get("broker_id")
            if not bid or ik is None or (ik, bid) in seen:
                continue
            seen.add((ik, bid))
            if self.get(ik, bid) is not None:
                continue
            if rec.get("outcome") not in ("submitted", "dry_run",
                                          "needs_manual_action", "failed"):
                continue
            at = rec.get("finished_at") or rec.get("started_at") or now
            self.record_attempt(ik, rec, at, channel=rec.get("channel") or "form")
            created += 1
        return created

    def awaiting_digest(self, identity_key: str | None = None) -> list:
        return self.list(identity_key, states=(AWAITING_USER_CONFIRM,))

    def get_meta(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM optout_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with _LOCK:
            self.conn.execute(
                "INSERT INTO optout_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self.conn.commit()

    def digest_due(self, now: str, every_s: int = 86400) -> bool:
        last = _parse(self.get_meta("last_digest_at"))
        return last is None or _parse(now) - last >= timedelta(seconds=every_s)


def expected_sender_domain(broker: dict | None, recipe=None) -> str:
    """The domain Penn should look for on the confirmation email.

    Recipe's explicit ``confirmation_sender`` wins, then the broker's own
    domain (from its ``url``), then the form URL's host. Never an address
    scraped from anywhere else.
    """
    from urllib.parse import urlsplit

    explicit = getattr(recipe, "confirmation_sender", "") if recipe else ""
    if explicit:
        return explicit
    for candidate in ((broker or {}).get("url"), getattr(recipe, "url", "")):
        host = urlsplit(candidate or "").hostname or ""
        if host:
            return host[4:] if host.startswith("www.") else host
    return ""
