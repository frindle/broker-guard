"""Broker Guard web dashboard -- first page: escalation countdowns + health summary."""
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import broker_guard.webui_data as webui_data

app = FastAPI(title="Broker Guard")

HEALTH_LOG_PATH = "logs/health.jsonl"


@app.get("/", response_class=HTMLResponse)
def index():
    # Escalation countdowns: no persisted escalation-records store yet, so an
    # empty list is the honest input ("no escalations recorded yet").
    try:
        countdowns = webui_data.escalation_countdowns([], datetime.now(timezone.utc).isoformat())
    except Exception:
        countdowns = []

    lines = []
    if os.path.exists(HEALTH_LOG_PATH):
        with open(HEALTH_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    try:
        summary = webui_data.load_health_summary(lines)
    except Exception:
        summary = {"total": 0, "ok": 0, "failed": 0, "by_broker": {}}

    rows_html = "\n".join(
        "<tr><td>{broker_id}</td><td>{stage}</td><td>{deadline_iso}</td>"
        "<td>{seconds_remaining}</td><td>{overdue}</td></tr>".format(**row)
        for row in countdowns
    )

    return (
        "<!DOCTYPE html>\n"
        "<html><head><title>Broker Guard</title></head><body>\n"
        "<h1>Broker Guard</h1>\n"
        "<table border=\"1\">\n"
        "<tr><th>broker_id</th><th>stage</th><th>deadline_iso</th>"
        "<th>seconds_remaining</th><th>overdue</th></tr>\n"
        + rows_html + "\n"
        "</table>\n"
        "<p>health: total={total} ok={ok} failed={failed}</p>\n"
        "</body></html>"
    ).format(total=summary.get("total", 0), ok=summary.get("ok", 0), failed=summary.get("failed", 0))
