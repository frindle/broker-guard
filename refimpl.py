#!/usr/bin/env python3
"""Reference impl for: bg-webui-s3-escalation-audit

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Appends escalation_countdowns to broker_guard/webui_data.py, preserving every
function already landed in this slice chain.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui_data.py'
t = p.read_text()

ANCHOR = '''    return {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}'''

NEW = ANCHOR + '''


def escalation_countdowns(records: list[dict], now_iso: str) -> list[dict]:
    """Return one countdown dict per record, sorted by seconds_remaining ascending.

    Each input record has ``{'broker_id': str, 'stage': str,
    'deadline_iso': str}``. The output row is the same three fields plus
    ``seconds_remaining`` (int, deadline minus now in whole seconds -- negative
    when past due) and ``overdue`` (bool, true exactly when
    ``seconds_remaining < 0``). Ties keep input order.
    """
    from datetime import datetime

    now = datetime.fromisoformat(now_iso)
    rows = []
    for rec in records:
        deadline = datetime.fromisoformat(rec["deadline_iso"])
        seconds_remaining = int((deadline - now).total_seconds())
        rows.append({
            "broker_id": rec["broker_id"],
            "stage": rec["stage"],
            "deadline_iso": rec["deadline_iso"],
            "seconds_remaining": seconds_remaining,
            "overdue": seconds_remaining < 0,
        })
    return sorted(rows, key=lambda r: r["seconds_remaining"])
'''

assert ANCHOR in t, "refimpl anchor not found -- did the target change?"
if "def escalation_countdowns" in t:
    print("refimpl already applied; nothing to do")
else:
    p.write_text(t.replace(ANCHOR, NEW, 1))
    print("refimpl applied")
