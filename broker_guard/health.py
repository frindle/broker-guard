"""Stub for broker_guard/health.py -- implement per TASK.md."""

from datetime import datetime, timedelta


def heartbeat_stale(last_beat_iso: str, now_iso: str, max_age_s: int) -> bool:
    last_beat = datetime.fromisoformat(last_beat_iso.replace('Z', '+00:00'))
    now = datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
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
    now = datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
    next_retry = (now + timedelta(seconds=delay)).isoformat()
    return {"status": "failing", "consecutive_failures": consecutive_failures, "next_retry": next_retry}


def build_report(runs):
    ok = 0
    failed = 0
    by_broker = {}
    for run in runs:
        if run['ok']:
            ok += 1
        else:
            failed += 1
        broker_id = run['broker_id']
        counts = by_broker.setdefault(broker_id, {'ok': 0, 'failed': 0})
        if run['ok']:
            counts['ok'] += 1
        else:
            counts['failed'] += 1
    return {
        'total': len(runs),
        'ok': ok,
        'failed': failed,
        'by_broker': by_broker,
    }
