"""Read-only webUI queries over the presence state db (see broker_guard/state.py).

This module only reads: it never writes, commits or mutates the connection.
The ``presence`` table's schema is owned by ``state.init_db`` -- columns
``identity_key``, ``broker_id``, ``first_seen``, ``last_seen`` (the check-time
column is ``last_seen``).

The bucketing/shaping helpers below (``broker_automation_breakdown``,
``broker_kind_breakdown``, ``broker_status_counts``, ``recent_status_changes``,
``submitted_over_time``, ``estimate_next_scan``, ``broker_stepper``) back the
dashboard's charts, stat tiles, notifications feed and per-broker stepper.
Every one of them is a pure function over data this codebase already tracks
(``brokers.json``'s ``verification`` kind, the ``presence``/``broker_status``
rows above) -- none of them invents a metric (e.g. a "compliance score")
this repo has no real data source for.
"""
import hmac
import json
import sqlite3
from datetime import datetime, timedelta

from broker_guard import brokers as brokers_mod


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


def query_broker_status(conn: sqlite3.Connection, identity_key: str | None = None, limit: int = 200) -> list[dict]:
    """Return presence rows LEFT JOINed with their ``broker_status`` row.

    This is the surface no UI read before: ``state.StateStore.set_status``/
    ``get_status`` write ``broker_status`` every cycle, but nothing displayed
    it. One row per (identity_key, broker_id) pair that has a presence
    record, ordered by ``last_seen DESC``, optionally filtered to one
    identity, capped at ``limit``. A broker with presence but no status row
    yet (removal never submitted) gets ``removal_status: None`` -- that is
    itself meaningful ("seen, nothing sent"), not an error. Never writes.
    """
    if identity_key is None:
        cur = conn.execute(
            "SELECT p.identity_key, p.broker_id, p.first_seen, p.last_seen, "
            "s.status, s.updated_at "
            "FROM presence p LEFT JOIN broker_status s "
            "ON p.identity_key = s.identity_key AND p.broker_id = s.broker_id "
            "ORDER BY p.last_seen DESC LIMIT ?",
            (limit,),
        )
    else:
        cur = conn.execute(
            "SELECT p.identity_key, p.broker_id, p.first_seen, p.last_seen, "
            "s.status, s.updated_at "
            "FROM presence p LEFT JOIN broker_status s "
            "ON p.identity_key = s.identity_key AND p.broker_id = s.broker_id "
            "WHERE p.identity_key = ? "
            "ORDER BY p.last_seen DESC LIMIT ?",
            (identity_key, limit),
        )
    return [
        {
            "identity_key": row[0],
            "broker_id": row[1],
            "first_seen": row[2],
            "last_seen": row[3],
            "removal_status": row[4],
            "status_updated_at": row[5],
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


# --- dashboard-shaping helpers -----------------------------------------------

def broker_kind_breakdown(brokers: list[dict]) -> dict:
    """Count *brokers* (as loaded by ``brokers.load_brokers``) by their
    ``brokers.verification_kind()`` -- the SAME kind the autopilot's decision
    table (see ``autopilot`` module docstring) actually keys off. Every key in
    ``brokers.VALID_KINDS`` plus the ``manual_review`` fallback is always
    present in the result (zeroed if unused), so a caller never needs a
    ``.get(..., 0)`` guard. An empty ``brokers`` list returns all-zero
    counts with ``total`` 0.
    """
    counts = {kind: 0 for kind in brokers_mod.VALID_KINDS}
    counts["manual_review"] = 0
    for broker in brokers:
        kind = brokers_mod.verification_kind(broker)
        if kind not in counts:
            kind = "manual_review"
        counts[kind] += 1
    counts["total"] = len(brokers)
    return counts


def broker_automation_breakdown(brokers: list[dict]) -> dict:
    """Bucket *brokers* by the SAME routing decision
    ``autopilot.decide_action`` makes for each one, purely from its
    verification kind: ``auto_send`` (sent with no human in the loop),
    ``needs_document`` (queued, a photo id is required) or ``needs_review``
    (queued, kba or an unrecognized kind). Calls the real
    ``autopilot.decide_action`` rather than re-deriving the policy here, so
    this can never silently drift from what the autopilot actually does. An
    empty ``brokers`` list returns all-zero counts with ``total`` 0.
    """
    from broker_guard import autopilot as autopilot_mod

    counts = {key: 0 for key in ("auto_send", "needs_document", "needs_review")}
    for broker in brokers:
        kind = brokers_mod.verification_kind(broker)
        decision = autopilot_mod.decide_action(kind)
        if decision["action"] == "auto_send":
            counts["auto_send"] += 1
        elif decision["queue_status"] == autopilot_mod.STATUS_NEEDS_DOCUMENT:
            counts["needs_document"] += 1
        else:
            counts["needs_review"] += 1
    counts["total"] = len(brokers)
    return counts


def broker_status_counts(rows: list[dict]) -> dict:
    """Bucket ``query_broker_status`` rows by ``removal_status`` into the
    real vocabulary this codebase ever writes to ``broker_status.status``:
    ``not_sent`` (no status row -- ``removal_status`` is ``None``),
    ``pending``, ``needs_document``, ``needs_review``, ``submitted`` and
    ``confirmed`` (see ``eraser.status_after_removal``/``eraser.needs_reverify``
    and ``autopilot.decide_action`` for where each is written). Any row
    carrying a status this module doesn't recognize also falls back to
    ``not_sent`` rather than raising -- a stat tile must never crash the
    dashboard over an unexpected value. ``total`` is always ``len(rows)``.
    """
    counts = {
        "not_sent": 0, "pending": 0, "needs_document": 0,
        "needs_review": 0, "submitted": 0, "confirmed": 0,
    }
    for row in rows:
        status = row.get("removal_status")
        key = status if status in counts else "not_sent"
        counts[key] += 1
    counts["total"] = len(rows)
    return counts


def recent_status_changes(rows: list[dict], limit: int = 8) -> list[dict]:
    """``query_broker_status`` rows that actually have a ``status_updated_at``,
    newest first, capped at *limit* -- the notifications feed's input.

    A pure re-sort/filter of ``query_broker_status``'s own output: never
    invents a timestamp for a broker that has none (those are simply left
    out), and never re-queries anything itself. ``limit <= 0`` returns ``[]``.
    """
    dated = [row for row in rows if row.get("status_updated_at")]
    dated.sort(key=lambda row: row["status_updated_at"], reverse=True)
    if limit <= 0:
        return []
    return dated[:limit]


def submitted_over_time(rows: list[dict], bucket_chars: int = 10) -> list[dict]:
    """Cumulative count of brokers whose removal reached ``submitted`` or
    ``confirmed``, bucketed by the ISO timestamp's leading *bucket_chars*
    characters (10 == calendar day, ``YYYY-MM-DD``) of ``status_updated_at``.

    Real derived history, not a fabricated series: every point comes from an
    actual ``status_updated_at`` this codebase wrote. Rows with no
    ``status_updated_at`` or a status other than submitted/confirmed are
    excluded. Returns ``[]`` for no qualifying rows; otherwise a list of
    ``{'bucket': str, 'count': int}`` sorted ascending by bucket, ``count``
    cumulative across buckets (a running total, matching a "removal
    requests over time" area chart's shape).
    """
    per_bucket: dict[str, int] = {}
    for row in rows:
        if row.get("removal_status") not in ("submitted", "confirmed"):
            continue
        at = row.get("status_updated_at")
        if not at:
            continue
        per_bucket[at[:bucket_chars]] = per_bucket.get(at[:bucket_chars], 0) + 1
    running = 0
    out = []
    for bucket in sorted(per_bucket):
        running += per_bucket[bucket]
        out.append({"bucket": bucket, "count": running})
    return out


def estimate_next_scan(last_seen_iso: str | None, scan_interval_seconds: int) -> str | None:
    """``last_seen`` plus the configured scan interval -- an ESTIMATE of when
    this broker is next due to be re-checked, not a stored deadline (nothing
    in this codebase persists a per-broker "next scan" timestamp; the
    autopilot loop just re-scans every broker on ``scan_interval_seconds``).
    Returns ``None`` for a missing/unparseable ``last_seen`` rather than
    raising -- the stepper must still render without this one field.
    """
    if not last_seen_iso:
        return None
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
    except ValueError:
        return None
    return (last_seen + timedelta(seconds=scan_interval_seconds)).isoformat()


_STEPPER_NOTES = {
    "needs_document": "Needs a government ID on file before this can be sent.",
    "needs_review": "Needs manual review before this can be sent.",
    "pending": "An opt-out was attempted but is not yet confirmed sent.",
}


def broker_stepper(row: dict, scan_interval_seconds: int) -> dict:
    """The 4-step lifecycle stepper (Scanned -> Removal submitted -> Data
    removed -> Next scan) for one ``query_broker_status`` row, built entirely
    from fields that row already carries:

    * Scanned: always done -- the row exists, so ``first_seen`` is real.
    * Removal submitted: done when ``removal_status`` is ``submitted`` or
      ``confirmed`` (the only two statuses ``eraser.status_after_removal``
      ever advances a broker to after a real send).
    * Data removed: done only when ``removal_status`` is ``confirmed`` --
      today nothing in this codebase auto-sets that (see
      ``autopilot``'s "Confirmation, honestly" docstring section), so this
      step honestly stays un-done for every broker until a future
      confirmation-parsing pass exists to set it.
    * Next scan: never "done" (it is always in the future); its timestamp is
      ``estimate_next_scan``'s estimate, not a stored deadline.

    A broker parked in ``needs_document``/``needs_review``/``pending`` gets a
    note under step 2 explaining why it hasn't moved, instead of silently
    looking stalled.
    """
    status = row.get("removal_status")
    submitted_done = status in ("submitted", "confirmed")
    removed_done = status == "confirmed"

    steps = [
        {"label": "Scanned", "done": True, "at": row.get("first_seen")},
        {
            "label": "Removal submitted", "done": submitted_done,
            "at": row.get("status_updated_at") if submitted_done else None,
            "note": _STEPPER_NOTES.get(status) if not submitted_done else None,
        },
        {
            "label": "Data removed", "done": removed_done,
            "at": row.get("status_updated_at") if removed_done else None,
        },
        {
            "label": "Next scan", "done": False,
            "at": estimate_next_scan(row.get("last_seen"), scan_interval_seconds),
        },
    ]
    current_index = 2 if removed_done else (1 if submitted_done else 0)
    return {"steps": steps, "current_index": current_index}


def scan_progress_line(progress: dict | None) -> str | None:
    """Render a live ``ScanProgress`` snapshot as one honest sentence.

    Examples::

        "Checking brokers: 412/827 -- 3 found, 0 errors."
        "Checking broker sites: 12/95 -- 0 found, 4 errors."

    Returns None when there is nothing truthful to say (no snapshot, or a
    phase that has not started), so the caller falls back to its own text
    rather than rendering "0/0".

    Errors are ALWAYS shown, including when the count is zero: "0 errors"
    is the load-bearing half of the sentence. A line that only appeared
    when something went wrong would leave the normal case looking exactly
    like the old, uninformative "scan in progress", which is the state
    this whole feature exists to replace.
    """
    if not isinstance(progress, dict):
        return None
    total = progress.get("total") or 0
    processed = progress.get("processed") or 0
    if total <= 0 and processed <= 0:
        return None
    label = "Checking broker sites" if progress.get("phase") == "browser" else "Checking brokers"
    counted = "{}/{}".format(processed, total) if total > 0 else str(processed)
    return "{}: {} -- {} found, {} errors.".format(
        label, counted, progress.get("hits") or 0, progress.get("errors") or 0,
    )


# --- per-broker scan results (the /brokers "Scan results" card) --------------

#: Display order: the outcomes a human needs to act on first, then the
#: quiet ones. ``pending`` is LAST and is its own bucket -- a broker the
#: current cycle has not reached yet must never sort or read as "clean".
SCAN_OUTCOME_ORDER = ("hit", "error", "checked", "skipped", "pending")

SCAN_OUTCOME_LABELS = {
    "hit": "Listing found",
    "error": "Check failed",
    "checked": "Checked -- clean",
    "skipped": "Not checkable",
    "pending": "Not yet checked",
}

#: Badge tone per outcome (see webui_style.badge). ``pending`` and
#: ``skipped`` share the neutral tone but NEVER share a label: neutral
#: means "no claim is being made", which is exactly true of both.
SCAN_OUTCOME_TONES = {
    "hit": "escalated",
    "error": "action",
    "checked": "success",
    "skipped": "neutral",
    "pending": "neutral",
}

_SCAN_OUTCOME_RANK = {name: i for i, name in enumerate(SCAN_OUTCOME_ORDER)}


def scan_outcome_rows(brokers: list[dict], progress: dict | None,
                      identity_key: str | None = None) -> list[dict]:
    """One row per broker in the ROSTER, carrying this cycle's outcome.

    The roster -- ``brokers.json`` as loaded by ``brokers.load_brokers`` --
    is the source of rows, not the ``presence`` table. That is the whole
    point: ``query_broker_status`` can only ever show brokers the person
    was FOUND on, so a scan that checked 827 brokers and found nothing had
    nothing to display. Here every broker gets a row, and the ones the
    scan has not reached yet get the explicit ``pending`` outcome rather
    than being quietly rendered as clean.

    *progress* is a ``ScanProgress.snapshot(include_brokers=True)`` dict
    (or None). An entry is only applied to a row when it was recorded for
    *identity_key* -- so filtering the page to profile B can never show
    profile A's results. ``identity_key=None`` means "any identity".

    Sorted by ``SCAN_OUTCOME_ORDER`` then by name, so hits and failures
    are at the top of the list instead of buried under 800 clean rows.
    """
    entries = (progress or {}).get("brokers") or {}
    rows = []
    for broker in brokers:
        broker_id = str(broker.get("id") or "")
        entry = entries.get(broker_id)
        if entry is not None and identity_key is not None \
                and entry.get("identity_key") != identity_key:
            entry = None
        outcome = (entry or {}).get("outcome") or "pending"
        if outcome not in _SCAN_OUTCOME_RANK:
            outcome = "pending"
        name = broker.get("name") or broker_id
        rows.append({
            "broker_id": broker_id,
            "name": name,
            "url": broker.get("url") or "",
            "outcome": outcome,
            "hits": (entry or {}).get("hits") or 0,
            "errors": (entry or {}).get("errors") or 0,
            "checked_at": (entry or {}).get("checked_at"),
            "identity_key": (entry or {}).get("identity_key"),
        })
    rows.sort(key=lambda row: (_SCAN_OUTCOME_RANK[row["outcome"]], row["name"].lower()))
    return rows


def scan_outcome_counts(rows: list[dict]) -> dict:
    """Bucket ``scan_outcome_rows`` output by outcome. Every key in
    ``SCAN_OUTCOME_ORDER`` is always present (zeroed if unused) plus
    ``total``, so a caller never needs a ``.get(..., 0)`` guard."""
    counts = {name: 0 for name in SCAN_OUTCOME_ORDER}
    for row in rows:
        key = row.get("outcome")
        counts[key if key in counts else "pending"] += 1
    counts["total"] = len(rows)
    return counts


def scan_outcome_counts_from_progress(progress: dict | None) -> dict:
    """``scan_outcome_counts``'s shape straight from a ``ScanProgress``
    snapshot, without needing the broker roster.

    Same numbers, different input: the roster-based path backs the page
    render, this one backs the 2s poll, which must not re-read and re-walk
    brokers.json on every tick. ``pending`` comes from the snapshot's own
    ``not_reached`` (cycle_total minus the brokers recorded so far), so
    "not yet checked" is still a first-class count and never folded into
    ``checked``. A snapshot with no per-broker map yields all zeroes.
    """
    counts = {name: 0 for name in SCAN_OUTCOME_ORDER}
    entries = (progress or {}).get("brokers") or {}
    for entry in entries.values():
        outcome = entry.get("outcome")
        counts[outcome if outcome in counts else "pending"] += 1
    counts["pending"] = (progress or {}).get("not_reached") or 0
    counts["total"] = len(entries) + counts["pending"]
    return counts


def scan_outcome_line(counts: dict, active: bool = False) -> str:
    """One honest sentence over ``scan_outcome_counts``.

    Examples::

        "Scan running: 412 of 827 checked -- 3 listings found, 2 checks failed, 415 not yet checked."
        "Most recent scan: 827 of 827 checked -- 0 listings found, 0 checks failed."

    ``not yet checked`` is only mentioned when it is non-zero, and is
    never rolled into the checked count -- the distinction between "we
    looked and found nothing" and "we have not looked yet" is the reason
    this card exists.
    """
    total = counts.get("total", 0)
    pending = counts.get("pending", 0)
    reached = total - pending
    if reached <= 0:
        return ("No per-broker results yet in this process -- every broker below reads "
                "\"not yet checked\" until the next scan runs.")
    line = "{}: {} of {} checked -- {} listing(s) found, {} check(s) failed".format(
        "Scan running" if active else "Most recent scan", reached, total,
        counts.get("hit", 0), counts.get("error", 0),
    )
    if counts.get("skipped", 0):
        line += ", {} not checkable".format(counts["skipped"])
    if pending:
        line += ", {} not yet checked".format(pending)
    return line + "."


def last_scan_detection_line(heartbeat: dict | None) -> str | None:
    """"Checked 827 brokers, 340 errors." for the FINISHED scan, from the
    heartbeat's persisted ``detection`` block (see autopilot.run_forever).

    Returns None for a heartbeat written before this field existed, or by
    the headless ``service.main`` loop -- an older heartbeat honestly has
    nothing to say here rather than implying a clean zero.
    """
    if not isinstance(heartbeat, dict):
        return None
    detection = heartbeat.get("detection")
    if not isinstance(detection, dict):
        return None
    serp = detection.get("serp") if isinstance(detection.get("serp"), dict) else {}
    browser = detection.get("browser") if isinstance(detection.get("browser"), dict) else {}
    checked = sum((leg.get("checked", 0) + leg.get("hit", 0)) for leg in (serp, browser))
    errors = sum(leg.get("error", 0) for leg in (serp, browser))
    skipped = sum(leg.get("skipped", 0) for leg in (serp, browser))
    if checked == 0 and errors == 0 and skipped == 0:
        return None
    return "Checked {} broker(s), {} error(s).".format(checked, errors)


def scan_status(heartbeat: dict | None, jobs_summary: dict, scan_interval_seconds: int,
                 progress: dict | None = None) -> dict:
    """The dashboard's scan-status indicator, from two REAL sources:

    * ``heartbeat`` -- the parsed contents of ``logs/heartbeat.json`` (see
      ``service.write_heartbeat``), written after every autopilot scan
      cycle (headless loop and, since the ``autopilot.run_forever`` fix
      alongside this function, the ``BG_SERVE_WEB=true`` deployed mode too).
      ``None`` when the file doesn't exist yet -- an honest "never run"
      state, not an error.
    * ``jobs_summary`` -- ``{job_id: status}`` for THIS process's in-memory
      ``/scan`` jobs (see ``webui.get_jobs``/``start_scan``): a manual
      "Run scan now" click shows as ``running`` here immediately, even
      before it has written a heartbeat.

    Returns ``{'running': bool, 'last_run_at': str|None, 'last_run_ok':
    bool|None, 'next_run_at': str|None}``. Never fabricates a trend or a
    fake "6 hours ago" string -- ``last_run_at`` is exactly the heartbeat's
    own ``last_run`` timestamp (or ``None``), and ``next_run_at`` is that
    same estimate-from-interval logic ``estimate_next_scan`` already uses,
    not a stored deadline.

    ``running`` is true for a manual "Run scan now" job OR for the
    autopilot background thread's own in-progress cycle (heartbeat's
    ``status`` field, written at cycle start -- see
    ``autopilot.run_forever``). Without the second check, a scan that has
    been running for minutes looked identical to "never run yet" the whole
    time it was in progress, since jobs_summary only ever tracks THIS
    process's manual /scan jobs.

    ``progress`` is an optional ``broker_guard.progress.ScanProgress``
    snapshot (a plain dict -- this function stays pure and imports
    nothing). When a phase is live it supplies ``progress_line``, the
    "412/827 checked, 3 found, 0 errors" granularity the bare "running"
    boolean never had, and it is a third, independent reason to report
    ``running``: it is true the instant a sweep starts, without waiting
    for a heartbeat write.
    """
    running = (
        any(status == "running" for status in jobs_summary.values())
        or bool(heartbeat) and heartbeat.get("status") == "running"
        or bool(progress) and bool(progress.get("active"))
    )
    # Only a LIVE phase may contribute the live counter line. A finished
    # cycle's counters are deliberately kept readable in the snapshot (see
    # ScanProgress.finish), so without this check the dashboard would go on
    # claiming "Checking brokers: 827/827" hours after the scan ended.
    live_progress = progress if (progress and progress.get("active")) else None
    common = {
        "running": running,
        "progress": progress or None,
        "progress_line": scan_progress_line(live_progress),
        "detection_line": last_scan_detection_line(heartbeat),
        "detection_errors": (heartbeat or {}).get("detection_errors"),
    }
    if not heartbeat:
        return {**common, "last_run_at": None, "last_run_ok": None, "next_run_at": None}
    last_run_at = heartbeat.get("last_run")
    return {
        **common,
        "last_run_at": last_run_at,
        "last_run_ok": heartbeat.get("ok"),
        "next_run_at": estimate_next_scan(last_run_at, scan_interval_seconds),
    }
