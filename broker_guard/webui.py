"""Broker Guard web dashboard -- first page: escalation countdowns + health summary."""
import html
import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import HTMLResponse

import broker_guard.profile
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


PROFILE_PATH = "profile.json"


def _split_list(text):
    return [part.strip() for part in text.split("\n") if part.strip()]


@app.get("/identity", response_class=HTMLResponse)
def identity_get():
    try:
        profile = broker_guard.profile.load_profile(PROFILE_PATH)
    except Exception:
        profile = None

    first_name = profile.first_name if profile is not None else ""
    middle_name = profile.middle_name if profile is not None else ""
    last_name = profile.last_name if profile is not None else ""
    emails = "\n".join(profile.emails) if profile is not None else ""
    phones = "\n".join(profile.phones) if profile is not None else ""
    addresses = "\n".join(profile.addresses) if profile is not None else ""

    lines = []
    lines.append("<!DOCTYPE html>")
    lines.append("<html><head><title>Broker Guard -- Identity</title></head><body>")
    lines.append("<h1>Identity</h1>")
    lines.append('<form method="post" action="/identity">')
    lines.append('<label>First name <input type="text" name="first_name" value="%s"></label>' % html.escape(first_name))
    lines.append('<label>Middle name <input type="text" name="middle_name" value="%s"></label>' % html.escape(middle_name))
    lines.append('<label>Last name <input type="text" name="last_name" value="%s"></label>' % html.escape(last_name))
    lines.append('<label>Emails <textarea name="emails">%s</textarea></label>' % html.escape(emails))
    lines.append('<label>Phones <textarea name="phones">%s</textarea></label>' % html.escape(phones))
    lines.append('<label>Addresses <textarea name="addresses">%s</textarea></label>' % html.escape(addresses))
    lines.append('<button type="submit">Save</button>')
    lines.append("</form>")
    lines.append("</body></html>")
    return "\n".join(lines)


@app.post("/identity")
def identity_post(first_name: str = Form(...), middle_name: str = Form(""), last_name: str = Form(...), emails: str = Form(""), phones: str = Form(""), addresses: str = Form("")):
    data = {
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "emails": _split_list(emails),
        "phones": _split_list(phones),
        "addresses": _split_list(addresses),
    }
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return RedirectResponse(url='/identity', status_code=303)
