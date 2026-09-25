"""Health-state tracking and failure classification for broker runs."""
from datetime import datetime, timedelta


def update_run_status(prev: dict, ok: bool, now_iso: str, base_backoff_s: int = 60, max_backoff_s: int = 3600) -> dict:
    if ok:
        return {'status': 'healthy', 'consecutive_failures': 0, 'next_retry': None}
    prev = prev if isinstance(prev, dict) else {}
    failures = prev.get('consecutive_failures', 0) + 1
    delay = min(base_backoff_s * (2 ** (failures - 1)), max_backoff_s)
    next_retry = (datetime.fromisoformat(now_iso) + timedelta(seconds=delay)).isoformat()
    return {'status': 'failing', 'consecutive_failures': failures, 'next_retry': next_retry}

from datetime import datetime


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
