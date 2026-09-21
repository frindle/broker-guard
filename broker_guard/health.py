"""Self-monitoring: heartbeat staleness, failure classification, backoff, reports."""

from datetime import datetime, timedelta, timezone


def _parse_ts(value: str) -> datetime:
    """ISO-8601 parse tolerating 'Z'; naive input is treated as UTC."""
    dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def heartbeat_stale(last_beat_iso: str, now_iso: str, max_age_s: int) -> bool:
    last_beat = _parse_ts(last_beat_iso)
    now = _parse_ts(now_iso)
    age_s = (now - last_beat).total_seconds()
    return bool(age_s > max_age_s)


def classify_failure(error: dict) -> str:
    kind = error.get('kind') if isinstance(error, dict) else None
    if kind == 'http_error':
        status = error.get('http_status')
        if type(status) is int and status >= 500:
            return 'broker_side'
    elif kind in ('captcha', 'timeout'):
        return 'broker_side'
    return 'tool_side'


def update_run_status(prev: dict, ok: bool, now_iso: str, base_backoff_s: int = 60, max_backoff_s: int = 3600) -> dict:
    if ok:
        return {"status": "healthy", "consecutive_failures": 0, "next_retry": None}
    if not isinstance(prev, dict):
        prev = {}
    consecutive_failures = prev.get("consecutive_failures", 0) + 1
    delay = min(base_backoff_s * 2 ** (consecutive_failures - 1), max_backoff_s)
    now = _parse_ts(now_iso)
    next_retry = (now + timedelta(seconds=delay)).isoformat()
    return {"status": "failing", "consecutive_failures": consecutive_failures, "next_retry": next_retry}


def build_report(runs):
    """Aggregate per-broker run outcomes into totals plus a per-broker breakdown.

    Runs missing 'ok' count as failures and runs missing 'broker_id' are
    bucketed under '<unknown>', so a malformed run entry degrades the report
    instead of raising KeyError mid-report.
    """
    runs = list(runs or [])
    ok = 0
    failed = 0
    by_broker = {}
    for run in runs:
        run = run if isinstance(run, dict) else {}
        if run.get('ok'):
            ok += 1
        else:
            failed += 1
        broker_id = run.get('broker_id', '<unknown>')
        counts = by_broker.setdefault(broker_id, {'ok': 0, 'failed': 0})
        if run.get('ok'):
            counts['ok'] += 1
        else:
            counts['failed'] += 1
    return {
        'total': len(runs),
        'ok': ok,
        'failed': failed,
        'by_broker': by_broker,
    }
