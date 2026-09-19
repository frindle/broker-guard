"""Stub for broker_guard/health.py -- implement per TASK.md."""


def classify_failure(error: dict) -> str:
    kind = error.get('kind') if isinstance(error, dict) else None
    if kind == 'http_error':
        status = error.get('http_status')
        if type(status) is int and status >= 500:
            return 'broker_side'
    elif kind in ('captcha', 'timeout'):
        return 'broker_side'
    return 'tool_side'
