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


def classify_failure(error: dict) -> str:
    kind = error.get('kind') if isinstance(error, dict) else None
    if kind == 'http_error':
        status = error.get('http_status')
        if type(status) is int and status >= 500:
            return 'broker_side'
    elif kind in ('captcha', 'timeout'):
        return 'broker_side'
    return 'tool_side'
