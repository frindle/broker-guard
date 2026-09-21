#!/usr/bin/env python3
"""Reference impl for: bg-dashboard-s1-app-and-status

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/webui.py'
t = p.read_text()

OLD = '''"""Stub for broker_guard/webui.py -- implement per TASK.md."""'''
NEW = '''"""Broker Guard web dashboard."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import broker_guard.webui_data as webui_data

app = FastAPI(title="Broker Guard")

HEALTH_LOG_PATH = "logs/health.jsonl"


@app.get("/", response_class=HTMLResponse)
def status():
    try:
        rows = webui_data.escalation_countdowns([], datetime.now(timezone.utc).isoformat())
    except Exception:
        rows = []
    try:
        with open(HEALTH_LOG_PATH) as f:
            lines = f.readlines()
    except OSError:
        lines = []
    try:
        health = webui_data.load_health_summary(lines)
    except Exception:
        health = {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}

    row_html = "".join(
        f"<tr><td>{r['broker_id']}</td><td>{r['stage']}</td><td>{r['deadline_iso']}</td>"
        f"<td>{r['seconds_remaining']}</td><td>{r['overdue']}</td></tr>"
        for r in rows
    ) or "<tr><td colspan=5>No escalations.</td></tr>"

    return f"""<html><body>
<h1>Broker Guard</h1>
<table>
<tr><th>broker_id</th><th>stage</th><th>deadline</th><th>seconds_remaining</th><th>overdue</th></tr>
{row_html}
</table>
<div>total={health['total']} ok={health['ok']} failed={health['failed']}</div>
</body></html>"""'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
