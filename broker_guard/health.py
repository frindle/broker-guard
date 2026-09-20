"""Stub for broker_guard/health.py -- implement per TASK.md."""

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
