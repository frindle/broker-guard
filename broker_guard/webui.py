"""Broker Guard web dashboard -- identity, brokers/status, on-demand scan and
removal, breach-exposure panel, and the credit-freeze tracker.

Every route reads its runtime paths from ``broker_guard.config.Config``
rather than a hardcoded constant (the previous draft of this file hardcoded
``profile.json`` and ``data/presence.sqlite3`` -- neither matches what
``service.py``/``config.py`` actually read/write, so the dashboard was
silently looking at the wrong files). ``get_config`` is a FastAPI dependency
so tests can override it with a tmp-path Config via
``app.dependency_overrides`` instead of mutating process environment.

Nothing here fills or submits a broker's opt-out form directly -- that still
only ever happens through the vendored ``eraser`` engine via
``EraserBridge``, same rule as ``browser.py``.

Presentation (layout/CSS/components) lives in ``webui_style``; the
chart/stat-tile/stepper DATA it renders is shaped by pure helpers in
``webui_data`` -- every number on the dashboard traces back to a real
``brokers.json``/``presence``/``broker_status`` read, never a placeholder.
"""
import base64
import html
import json
import logging
import os
import re
import tempfile
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from broker_guard import brokers as brokers_mod
from broker_guard import eraser as eraser_mod
from broker_guard import eraser_config as eraser_config_mod
from broker_guard import exposure as exposure_mod
from broker_guard import freeze as freeze_mod
from broker_guard import profile as profile_mod
from broker_guard import progress as progress_mod
from broker_guard import profiles as profiles_mod
from broker_guard import service as service_mod
from broker_guard import state as state_mod
from broker_guard import webui_data
from broker_guard import webui_style as style
from broker_guard.config import Config, load_config
from broker_guard.crypto import encrypt_field
from broker_guard.eraser_bridge import EraserBridge

log = logging.getLogger("broker_guard.webui")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_DIGITS_RE = re.compile(r"\d+")

app = FastAPI(title="Broker Guard")

# The 4 bureaus in freeze.BUREAUS whose freeze_url/thaw_url are the bureau's
# general security-freeze landing page rather than a confirmed deep link
# (see freeze.py's module docstring) -- flagged here too so the dashboard
# never presents them with the same confidence as Equifax/Experian/
# TransUnion.
NEEDS_VERIFICATION_BUREAUS = frozenset({"innovis", "chexsystems", "nctue", "lexisnexis"})

ID_DOC_SIDES = ("front", "back")


# --- dependencies (overridable in tests) ------------------------------------

def get_config() -> Config:
    """Fresh Config from the environment on every request.

    Deliberately NOT cached at import/module time: a webserver process
    outlives a single request, and tests need a clean, isolated Config per
    test without mutating shared process environment.
    """
    return load_config()


# In-memory /scan job store. Module-level so a job survives past the request
# that created it (that's the whole point), but overridable per-test via
# dependency_overrides so tests never share state across each other.
_JOBS_LOCK = threading.Lock()
_JOBS: dict = {}


def get_jobs() -> dict:
    return _JOBS


def get_deps_factory():
    """Real dependency-builder for a /scan job. Tests override this to avoid
    touching the network/browser/subprocess for a background scan."""
    return service_mod.build_dependencies


def get_eraser_bridge(cfg: Config = Depends(get_config)) -> EraserBridge:
    return EraserBridge(cfg.eraser_bin, cfg.eraser_timeout_s, cfg.eraser_dry_run)


def get_exposure_client(cfg: Config = Depends(get_config)) -> exposure_mod.XposedOrNotClient:
    """Root-cause fix for the live ``/exposure`` 500: this used to construct
    ``XposedOrNotClient()`` with no arguments, which built an
    ``ExposureCache`` at its own hardcoded, ``Config``-independent
    ``exposure.DEFAULT_CACHE_PATH`` -- a relative path that may not be
    writable (or even exist as a directory) in the deployed container's
    actual working directory, and ``ExposureCache._save()`` had no
    try/except around that write. Routing the path through
    ``cfg.exposure_cache_path`` (now a real, overridable Config field) is
    the actual fix; ``exposure.py``'s hardened ``_save()`` is additional
    defense-in-depth on top of it, not a substitute for it.
    """
    cache = exposure_mod.ExposureCache(cfg.exposure_cache_path)
    return exposure_mod.XposedOrNotClient(cache=cache)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_scan_timestamp(iso_str: str | None) -> str | None:
    """Human-readable rendering of a heartbeat/estimate ISO timestamp for the
    dashboard's scan_line -- e.g. '2026-09-22 14:32 UTC' instead of the raw
    '2026-09-22T14:32:07.481932+00:00' _utcnow_iso()/estimate_next_scan()
    produce internally. Returns the input unchanged if it doesn't parse --
    this is presentation only, never allowed to blank out or raise on a
    valid-but-unexpected value."""
    if not iso_str:
        return iso_str
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    return dt.strftime("%Y-%m-%d %H:%M")


def _action_needed_count(rows: list[dict]) -> int:
    """How many ``query_broker_status`` rows are parked in a state that
    needs a human -- ``needs_document`` or ``needs_review`` -- the same
    real count the nav's "Action needed" badge and the dashboard's stat
    chip both show. Never a placeholder: 0 when nothing is actually
    waiting on a human."""
    return sum(1 for row in rows if row.get("removal_status") in ("needs_document", "needs_review"))


def _read_heartbeat(cfg: Config) -> dict | None:
    """Best-effort parse of ``logs/heartbeat.json`` (see
    ``service.write_heartbeat`` / the ``autopilot.run_forever`` fix) --
    ``None`` for "never written yet" or any read/parse failure, never a
    raised exception (a dashboard render must not 500 over a missing or
    momentarily-half-written heartbeat file)."""
    path = os.path.join(cfg.log_dir, "heartbeat.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# --- / : dashboard -----------------------------------------------------------

def _scan_line(scan: dict) -> str:
    """The one-line scan indicator, as PLAIN TEXT.

    Shared by the ``/`` template and the ``/status`` JSON the poll loop
    consumes, so the text a page is first served and the text it refreshes
    itself to are produced by the SAME function rather than two copies that
    can drift.

    Returned unescaped on purpose, because those two consumers have
    opposite needs: the template escapes it on the way into HTML, while
    the poll loop assigns it to ``textContent`` (already inert, and where
    pre-escaped entities would render literally as "&amp;"). Escaping here
    would be wrong for one of them whichever way it went, so each
    insertion point does its own.

    Three honest states, in priority order:

    1. A sweep is live -> the running counter ("Checking brokers: 412/827
       -- 3 found, 0 errors."). Falls back to the old bare sentence only
       when no counter exists (e.g. a /scan job that has not started its
       first phase yet).
    2. A scan has finished -> timestamp, ok/failed, next run, AND the
       per-broker outcome tally when the heartbeat carries one. A run that
       errored on every broker used to render identically to a clean one;
       now "827 broker(s), 340 error(s)" is right there in the line.
    3. Nothing has ever run.
    """
    if scan.get("running"):
        return scan.get("progress_line") or "Scan in progress right now."
    if scan.get("last_run_at"):
        line = "Last scan: {} ({}). Next scan around: {}.".format(
            format_scan_timestamp(scan["last_run_at"]) or scan["last_run_at"],
            "ok" if scan.get("last_run_ok") else "failed",
            format_scan_timestamp(scan.get("next_run_at")) or "unknown",
        )
        detection_line = scan.get("detection_line")
        if detection_line:
            line += " " + detection_line
        return line
    return "No scan has run yet in this deployment."


@app.get("/", response_class=HTMLResponse)
def index(cfg: Config = Depends(get_config), jobs: dict = Depends(get_jobs)):
    """The Removals dashboard. Every number below is wired to a real,
    already-tracked value -- see webui_data's docstrings for exactly which
    presence/broker_status/brokers.json field each one reads. Nothing here
    is a fabricated metric or a fake historical trend.
    """
    try:
        brokers = brokers_mod.load_brokers(cfg.brokers_path)
    except (OSError, ValueError):
        brokers = []

    conn = state_mod.init_db(cfg.state_path)
    try:
        rows = webui_data.query_broker_status(conn)
    finally:
        conn.close()

    status_counts = webui_data.broker_status_counts(rows)
    kind_breakdown = webui_data.broker_kind_breakdown(brokers)
    automation_breakdown = webui_data.broker_automation_breakdown(brokers)
    submitted_series = webui_data.submitted_over_time(rows)
    recent = webui_data.recent_status_changes(rows)
    action_needed = _action_needed_count(rows)

    # Escalated: no persisted escalation-records store exists yet (see
    # webui_data.escalation_countdowns), so this is honestly always 0 today
    # -- a real computation over a real (currently empty) input, not a
    # fabricated number. It will start reporting non-zero the moment an
    # escalation-record store is added, with no change needed here.
    try:
        escalated = sum(1 for c in webui_data.escalation_countdowns([], _utcnow_iso()) if c["overdue"])
    except Exception:
        escalated = 0

    heartbeat = _read_heartbeat(cfg)
    with _JOBS_LOCK:
        jobs_summary = {jid: j.get("status") for jid, j in jobs.items()}
    scan = webui_data.scan_status(heartbeat, jobs_summary, cfg.interval_seconds,
                                   progress=progress_mod.snapshot())

    in_progress = status_counts["pending"] + status_counts["submitted"]
    chips = "".join([
        style.stat_chip(status_counts["total"], "Tracked", "neutral"),
        style.stat_chip(status_counts["confirmed"], "Removed", "success"),
        style.stat_chip(in_progress, "In progress", "progress"),
        style.stat_chip(action_needed, "Action needed", "action" if action_needed else "neutral"),
        style.stat_chip(escalated, "Escalated", "escalated" if escalated else "neutral"),
    ])

    if submitted_series:
        area = style.area_chart(
            "submittedChart",
            [row["bucket"] for row in submitted_series],
            [row["count"] for row in submitted_series],
            "Removals submitted (cumulative)",
        )
    else:
        area = '<p class="muted">No removals have been submitted yet -- this fills in once the ' \
               "first one is sent (real data only, never a placeholder trend).</p>"

    kind_segments = [
        (style.KIND_COLORS[k], kind_breakdown.get(k, 0), style.KIND_LABELS[k])
        for k in style.KIND_ORDER
    ]
    kind_legend = "".join(
        style.legend_row(style.KIND_COLORS[k], style.KIND_LABELS[k], kind_breakdown.get(k, 0))
        for k in style.KIND_ORDER if kind_breakdown.get(k, 0) > 0
    )

    donut = style.donut_canvas(
        "automationDonut",
        [style.ACTION_LABELS[k] for k in style.ACTION_ORDER],
        [automation_breakdown[k] for k in style.ACTION_ORDER],
        [style.ACTION_COLORS[k] for k in style.ACTION_ORDER],
        "brokers tracked",
    )
    action_legend = "".join(
        style.legend_row(style.ACTION_COLORS[k], style.ACTION_LABELS[k], automation_breakdown[k])
        for k in style.ACTION_ORDER
    )

    if recent:
        notif_html = "".join(
            '<div class="notif"><span>{bid}: status changed to <strong>{status}</strong></span>'
            '<span class="when">{when}</span></div>'.format(
                bid=html.escape(str(r["broker_id"])),
                status=html.escape(str(r["removal_status"] or "not submitted")),
                when=html.escape(str(r["status_updated_at"] or "")),
            )
            for r in recent
        )
    else:
        notif_html = '<p class="muted">No status changes recorded yet.</p>'

    scan_line = html.escape(_scan_line(scan))

    # The #scanline text already reflected an in-flight scan on page load,
    # but the button itself always rendered plain-and-enabled -- so coming
    # BACK to the dashboard mid-scan (click "Run scan now", navigate to
    # /brokers, navigate back) looked identical to no scan running at all,
    # even though the server already knew one was. Render the same
    # disabled/"Scanning..." state the click handler produces.
    if scan["running"]:
        # The poll loop below now asks /status for the SERVER's own view of
        # whether a scan is running (scan.running), rather than inferring it
        # from this process's in-memory /scan job map. That map is empty
        # during an autopilot background cycle, which used to make the poll
        # conclude "finished" and reload immediately, over and over, for the
        # whole cycle -- so the auto-refresh had to be withheld in exactly
        # the case a live counter is most useful. With the authoritative
        # signal available it can always attach.
        scan_btn_attrs = ' disabled data-scan-running="1"'
        scan_btn_label = "Scanning..."
    else:
        scan_btn_attrs = ""
        scan_btn_label = "Run scan now"

    body = """
<div class="page-head">
  <div><h1>Removals</h1><div class="muted" id="scanline">{scan_line}</div></div>
  <button class="btn secondary" id="runScanBtn" onclick="runScanNow()"{scan_btn_attrs}>{scan_btn_label}</button>
</div>
<div class="chips">{chips}</div>
<div class="grid-main">
  <div class="card"><h2>Removals submitted over time</h2>{area}</div>
  <div class="card">
    <h2>Notifications</h2>
    <div>{notif_html}</div>
  </div>
</div>
<div class="grid-main" style="margin-top:18px;">
  <div class="card">
    <div class="section-label">Broker overview -- by verification kind</div>
    {stackbar}
    {kind_legend}
  </div>
  <div class="card">
    <h2>Automation mix</h2>
    {donut}
    {action_legend}
  </div>
</div>
<script>
// ONE poll loop, used by both entry points below: the "Run scan now"
// click and the page-load resume for a scan that was already in flight.
// It reads /status (no job_id), whose `scan` block is the server's own
// authoritative view -- covering the autopilot background thread's cycle
// as well as this process's /scan jobs -- and repaints #scanline from
// scan.line every tick, which is what turns the old static "Scan in
// progress right now." into a live "Checking brokers: 412/827 -- 3
// found, 0 errors."
//
// Deliberately NOT a second mechanism alongside a job_id poll: the two
// used to answer "is it done?" from different sources and could disagree.
function pollScan() {{
  var btn = document.getElementById('runScanBtn');
  var line = document.getElementById('scanline');
  fetch('/status').then(function (r) {{ return r.json(); }}).then(function (s) {{
    var scan = (s && s.scan) || {{}};
    if (!scan.running) {{ location.reload(); return; }}
    if (btn) {{ btn.disabled = true; btn.textContent = 'Scanning...'; }}
    if (line && scan.line) {{ line.textContent = scan.line; }}
    setTimeout(pollScan, 1500);
  }}).catch(function () {{ setTimeout(pollScan, 1500); }});
}}

function runScanNow() {{
  var btn = document.getElementById('runScanBtn');
  btn.disabled = true; btn.textContent = 'Starting...';
  // /scan registers the job as 'queued' BEFORE responding, so scan.running
  // is already true by the time this resolves -- no race where the first
  // poll sees "not running" and reloads the page instantly.
  fetch('/scan', {{method: 'POST'}}).then(function () {{ pollScan(); }})
    .catch(function () {{ pollScan(); }});
}}

// Resume the counter for a scan that was already running when this page
// was served (an autopilot cycle, or a /scan started before navigating
// away and back).
(function () {{
  var btn = document.getElementById('runScanBtn');
  if (!btn || btn.getAttribute('data-scan-running') !== '1') {{ return; }}
  pollScan();
}})();
</script>
""".format(
        scan_line=scan_line, chips=chips, area=area, notif_html=notif_html,
        stackbar=style.stacked_bar(kind_segments), kind_legend=kind_legend,
        donut=donut, action_legend=action_legend,
        scan_btn_attrs=scan_btn_attrs, scan_btn_label=scan_btn_label,
    )

    return style.render_page("Dashboard", "dashboard", body, action_needed_count=action_needed)


# --- /brokers + /status : presence and removal status -----------------------

def _broker_row_html(row: dict, broker_meta: dict, scan_interval_seconds: int) -> str:
    """One expandable ``<details>`` row: summary line (name, kind, status,
    last update) plus the 4-step lifecycle stepper and remove action on
    expand. ``data-*`` attributes carry the values the brokers-page's
    client-side search/status/action-needed filters key off of -- filtering
    never re-fetches or re-renders, it only shows/hides these same rows.
    """
    broker_id = str(row["broker_id"])
    meta = broker_meta.get(broker_id, {})
    name = meta.get("name") or broker_id
    kind = brokers_mod.verification_kind(meta) if meta else "manual_review"
    status = row.get("removal_status")
    step = webui_data.broker_stepper(row, scan_interval_seconds)
    action_needed = status in ("needs_document", "needs_review")

    stepper_note = ""
    for s in step["steps"]:
        if s.get("note"):
            stepper_note = s["note"]

    return """
<details class="brokerrow" data-name="{name_lower}" data-status="{status}" data-action-needed="{action_needed}">
  <summary class="row-summary">
    <span><span class="name">{name}</span><br><span class="sub">{url}</span></span>
    <span>{kind_label}</span>
    <span>{status_badge}</span>
    <span class="sub">{last_seen}</span>
    <span class="chevron">&#9656;</span>
  </summary>
  <div class="row-detail">
    {stepper}
    <dl class="pii-grid">
      <dt>Broker</dt><dd>{name} &middot; {url}</dd>
      <dt>First seen</dt><dd>{first_seen}</dd>
      <dt>Last checked</dt><dd>{last_seen}</dd>
      <dt>Removal status</dt><dd>{status_text}</dd>
    </dl>
    <form method="post" action="/brokers/{broker_id_attr}/remove">
      <button type="submit" class="btn secondary">Send removal now</button>
    </form>
  </div>
</details>
""".format(
        name_lower=style.escape_attr(name.lower()),
        status=style.escape_attr(status or ""),
        action_needed="true" if action_needed else "false",
        name=html.escape(name),
        url=html.escape(str(meta.get("url") or "")),
        kind_label=html.escape(style.KIND_LABELS.get(kind, kind)),
        status_badge=style.status_badge(status),
        last_seen=html.escape(str(row.get("last_seen") or "")),
        stepper=style.stepper(step["steps"]),
        first_seen=html.escape(str(row.get("first_seen") or "")),
        status_text=html.escape(status or "not submitted"),
        broker_id_attr=style.escape_attr(broker_id),
    )


@app.get("/brokers", response_class=HTMLResponse)
def brokers_page(cfg: Config = Depends(get_config)):
    conn = state_mod.init_db(cfg.state_path)
    try:
        rows = webui_data.query_broker_status(conn)
    finally:
        conn.close()

    try:
        broker_meta = {b["id"]: b for b in brokers_mod.load_brokers(cfg.brokers_path)}
    except (OSError, ValueError):
        broker_meta = {}

    action_needed = _action_needed_count(rows)
    rows_html = "".join(_broker_row_html(row, broker_meta, cfg.interval_seconds) for row in rows)
    if not rows:
        rows_html = '<p class="muted" style="padding:16px;">No brokers tracked yet -- run a scan.</p>'

    body = """
<div class="page-head"><h1>Brokers</h1><div class="muted">{count} tracked</div></div>
<div class="card">
  <div class="toolbar">
    <input type="text" id="searchBox" placeholder="Search brokers..." oninput="filterRows()">
    <select id="statusFilter" onchange="filterRows()">
      <option value="">All statuses</option>
      <option value="">Not sent</option>
      <option value="pending">Pending</option>
      <option value="needs_document">Needs document</option>
      <option value="needs_review">Needs review</option>
      <option value="submitted">Submitted</option>
      <option value="confirmed">Confirmed</option>
    </select>
    <label style="display:flex;align-items:center;gap:6px;font-size:13px;">
      <input type="checkbox" id="actionOnly" onchange="filterRows()"> Action needed only
    </label>
  </div>
  <div id="rowsContainer">{rows_html}</div>
</div>
<script>
function filterRows() {{
  var q = document.getElementById('searchBox').value.toLowerCase();
  var statusVal = document.getElementById('statusFilter').value;
  var actionOnly = document.getElementById('actionOnly').checked;
  document.querySelectorAll('.brokerrow').forEach(function(row) {{
    var matches = true;
    if (q && row.dataset.name.indexOf(q) === -1) matches = false;
    if (statusVal && row.dataset.status !== statusVal) matches = false;
    if (actionOnly && row.dataset.actionNeeded !== 'true') matches = false;
    row.style.display = matches ? '' : 'none';
  }});
}}
if (location.hash === '#action-needed') {{
  document.getElementById('actionOnly').checked = true;
  filterRows();
}}
</script>
""".format(count=len(rows), rows_html=rows_html)

    return style.render_page("Brokers", "brokers", body, action_needed_count=action_needed)


def _run_scan_job(job_id: str, cfg: Config, jobs: dict, deps_factory) -> None:
    with _JOBS_LOCK:
        jobs[job_id] = {"status": "running", "started_at": _utcnow_iso()}
    deps = None
    try:
        deps = deps_factory(cfg)
        try:
            result = service_mod.run_once(cfg, deps)
        finally:
            deps.close()
            store = getattr(deps, "store", None)
            if store is not None and hasattr(store, "close"):
                try:
                    store.close()
                except Exception:  # pragma: no cover - best-effort teardown
                    pass
        with _JOBS_LOCK:
            jobs[job_id] = {"status": "done", "result": result, "finished_at": _utcnow_iso()}
    except Exception as exc:
        with _JOBS_LOCK:
            jobs[job_id] = {
                "status": "error",
                "error": "{}: {}".format(type(exc).__name__, exc),
                "finished_at": _utcnow_iso(),
            }


@app.post("/scan")
def start_scan(cfg: Config = Depends(get_config), jobs: dict = Depends(get_jobs),
               deps_factory=Depends(get_deps_factory)):
    """Trigger one scan cycle in a background thread; return a pollable job id.

    Never blocks the request on the scan itself -- the thread is started and
    this returns immediately with ``status: queued``. Poll ``GET
    /status?job_id=<id>`` for completion.
    """
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        jobs[job_id] = {"status": "queued"}
    thread = threading.Thread(
        target=_run_scan_job, args=(job_id, cfg, jobs, deps_factory), daemon=True,
    )
    thread.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/status")
def get_status(job_id: str | None = None, cfg: Config = Depends(get_config),
                jobs: dict = Depends(get_jobs)):
    """Poll a /scan job by id, or (with no job_id) get the current
    broker_status snapshot -- the table state.StateStore.set_status/
    get_status writes every cycle but that, before this route, no UI ever
    read.
    """
    if job_id is not None:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return job

    conn = state_mod.init_db(cfg.state_path)
    try:
        rows = webui_data.query_broker_status(conn)
    finally:
        conn.close()
    pending = sum(1 for r in rows if r["removal_status"] in (None, "pending"))
    with _JOBS_LOCK:
        jobs_summary = {jid: j.get("status") for jid, j in jobs.items()}
    # `scan` is what the dashboard's poll loop actually consumes: the live
    # per-broker counter plus the same rendered line the page was served
    # with. `jobs` stays for backward compatibility with anything already
    # polling it, but it is no longer the signal the UI decides on -- it is
    # blind to the autopilot background thread's own cycle.
    scan = webui_data.scan_status(_read_heartbeat(cfg), jobs_summary, cfg.interval_seconds,
                                   progress=progress_mod.snapshot())
    scan["line"] = _scan_line(scan)
    return {"brokers": rows, "pending_removals": pending, "jobs": jobs_summary,
            "scan": scan}


@app.post("/brokers/{broker_id}/remove")
def remove_broker(broker_id: str, cfg: Config = Depends(get_config),
                   bridge: EraserBridge = Depends(get_eraser_bridge)):
    """Manual, on-demand removal for one broker -- independent of the
    automated new-appearance trigger in service.submit_removals. A human (or
    the /freeze-style UI) can ask for a specific broker to be re-sent at any
    time, whether or not it was just detected as newly present.
    """
    try:
        identity = profile_mod.load_profile(cfg.profile_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot load profile: {exc}")

    if not bridge.available():
        raise HTTPException(status_code=503, detail="eraser binary not available")

    try:
        result = bridge.submit_removal(broker_id, identity.to_eraser_profile())
    except Exception as exc:
        raise HTTPException(status_code=502, detail="{}: {}".format(type(exc).__name__, exc))

    conn = state_mod.init_db(cfg.state_path)
    try:
        store = state_mod.StateStore(conn)
        prior = store.get_status(identity.identity_key, broker_id) or "pending"
        store.set_status(
            identity.identity_key, broker_id,
            eraser_mod.status_after_removal(prior, result), _utcnow_iso(),
        )
    finally:
        conn.close()
    return result


# --- /identity : profile view + edit -----------------------------------------

def _split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split("\n") if part.strip()]


def _dedupe_case_insensitive(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for v in values:
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def normalize_phone(raw: str) -> str:
    """Normalize *raw* toward the ``+1-XXX-XXX-XXXX`` shape
    ``profile.example.json`` uses for its own (fictitious) phone field and
    ``eraser_config._profile_block`` writes verbatim into eraser's
    ``profile.phone``/``additional_phones`` -- so what's stored is
    consistent regardless of how a person typed it in (with parens,
    dots, spaces, a leading 1, etc), not just cosmetically reformatted
    for display.

    A 10-digit US number (optionally with a leading country-code 1, 11
    digits total) becomes ``+1-AAA-EEE-LLLL``. Anything else (a shorter
    number, an already-international number, garbage) is returned with
    only whitespace collapsed -- this never invents digits or guesses a
    country code it wasn't given.
    """
    digits = "".join(_PHONE_DIGITS_RE.findall(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return "+1-{}-{}-{}".format(digits[0:3], digits[3:6], digits[6:10])
    return " ".join(raw.split())


@app.get("/identity", response_class=HTMLResponse)
def identity_get(edit: str = "", cfg: Config = Depends(get_config)):
    """The ONE identity page: the profiles list with the active profile
    pinned to the top and pre-selected in the editable identity form.

    This replaces the old split between a single-identity "Profile" page
    and a separate "Profiles" list -- two disconnected places to manage
    who you are, where a profile added on one never showed up on the
    other. There is still exactly one identity store per surface: the
    profiles list (``cfg.profiles_path``) is the source of truth for the
    UI, and whichever entry is active is mirrored into the single
    ``cfg.profile_path`` the scan/autopilot loop reads -- that loop's
    contract is unchanged (see ``profiles.py``'s module docstring).

    ``?edit=<id>`` opens a non-active profile for editing in place on this
    same page (posting to ``/profiles/<id>``); the active profile is
    always edited through the main form at the top (``POST /identity``).
    """
    # One-time backfill for a deployment that already had a populated
    # profile.local.json before this merge shipped -- a no-op once the
    # profiles list is non-empty, so it is safe on every page load.
    try:
        profiles_mod.migrate_legacy_profile_if_needed(cfg.profiles_path, cfg.profile_path)
    except (OSError, ValueError) as exc:
        log.warning("legacy profile migration skipped", extra={"error": str(exc)})

    try:
        all_profiles = profiles_mod.load_profiles(cfg.profiles_path)
    except (OSError, ValueError) as exc:
        log.warning("profiles list unreadable", extra={"error": str(exc)})
        all_profiles = []

    active = next((p for p in all_profiles if p.active), None)
    others = [p for p in all_profiles if active is None or p.id != active.id]

    if active is not None:
        first_name, middle_name, last_name = active.first_name, active.middle_name, active.last_name
        emails = "\n".join(active.emails)
        phones = "\n".join(active.phones)
        addresses = "\n".join(active.addresses)
        eraser_profile = active.eraser_profile or ""
        active_heading = "Active profile -- {}".format(active.full_name or active.id)
    else:
        # No profiles at all (and nothing to migrate): a blank form that
        # creates the first -- which add_profile marks active for us.
        first_name = middle_name = last_name = ""
        emails = phones = addresses = eraser_profile = ""
        active_heading = "Active profile"

    rows_html = "".join(_profile_row_html(p) for p in others)
    if not others:
        empty_note = (
            "No profiles yet." if active is None
            else "No other profiles yet -- add one below."
        )
        rows_html = '<tr><td colspan="4" class="muted" style="padding:16px;">{}</td></tr>'.format(empty_note)

    edit_card = ""
    if edit:
        try:
            editing = profiles_mod.get_profile(cfg.profiles_path, edit)
        except (profiles_mod.ProfileNotFound, OSError, ValueError):
            raise HTTPException(status_code=404, detail="unknown profile")
        edit_card = _profile_edit_card_html(editing)

    body = """
<div class="page-head"><h1>Profile</h1></div>
<p class="muted" style="max-width:680px;">Every identity Broker Guard manages, in one place. The
<strong>active</strong> profile is the one the dashboard, scan and autopilot loop run against, and
it is the one mirrored into eraser's config for <code>--profile &lt;id&gt;</code> on the CLI.
Scanning several profiles at once is a planned follow-up -- switching the active one here is what
changes who gets scanned today.</p>
<div class="grid-main">
  <div class="card">
    <h2>{active_heading}</h2>
    <form method="post" action="/identity">
      <div class="field"><label>First name</label>
        <input class="inp" type="text" name="first_name" value="{first_name}"></div>
      <div class="field"><label>Middle name</label>
        <input class="inp" type="text" name="middle_name" value="{middle_name}"></div>
      <div class="field"><label>Last name</label>
        <input class="inp" type="text" name="last_name" value="{last_name}"></div>
      <div class="field"><label>Emails (one per line)</label>
        <textarea class="inp" name="emails" rows="3">{emails}</textarea></div>
      <div class="field"><label>Phones (one per line)</label>
        <textarea class="inp" name="phones" rows="3">{phones}</textarea></div>
      <div class="field"><label>Addresses (one per line)</label>
        <textarea class="inp" name="addresses" rows="3">{addresses}</textarea></div>
      <div class="field"><label>Eraser profile id (optional)</label>
        <input class="inp" type="text" name="eraser_profile" value="{eraser_profile}"></div>
      <button type="submit" class="btn">Save</button>
    </form>
  </div>
  <div class="card">
    <h2>Government ID</h2>
    <div class="encnote">Encrypted at rest with BG_CRYPTO_KEY -- never stored or transmitted in plaintext.</div>
    <form method="post" action="/identity/id-document" enctype="multipart/form-data">
      <div class="field"><label>Side</label>
        <select class="inp" name="side"><option value="front">front</option><option value="back">back</option></select></div>
      <div class="field"><input type="file" name="file"></div>
      <button type="submit" class="btn secondary">Upload</button>
    </form>
  </div>
</div>
{edit_card}
<div class="grid-main" style="margin-top:24px;">
  <div class="card">
    <h2>Other profiles</h2>
    <table class="dtable">
      <tr style="text-align:left;font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:var(--faint);">
        <th style="padding-bottom:8px;">Name / id</th><th>Emails</th><th>Phones</th><th></th>
      </tr>
      {rows}
    </table>
  </div>
  <div class="card">
    <h2>Add a profile</h2>
    <p class="muted" style="font-size:13px;">Added profiles are inactive until you make one active.</p>
    <form method="post" action="/profiles">
      <div class="field"><label>First name</label><input class="inp" type="text" name="first_name" required></div>
      <div class="field"><label>Middle name</label><input class="inp" type="text" name="middle_name"></div>
      <div class="field"><label>Last name</label><input class="inp" type="text" name="last_name" required></div>
      <div class="field"><label>Emails (one per line)</label><textarea class="inp" name="emails" rows="2"></textarea></div>
      <div class="field"><label>Phones (one per line)</label><textarea class="inp" name="phones" rows="2"></textarea></div>
      <div class="field"><label>Addresses (one per line)</label><textarea class="inp" name="addresses" rows="2"></textarea></div>
      <button type="submit" class="btn">Add profile</button>
    </form>
  </div>
</div>
""".format(
        active_heading=html.escape(active_heading),
        first_name=html.escape(first_name), middle_name=html.escape(middle_name),
        last_name=html.escape(last_name), emails=html.escape(emails), phones=html.escape(phones),
        addresses=html.escape(addresses), eraser_profile=html.escape(eraser_profile),
        rows=rows_html, edit_card=edit_card,
    )
    return style.render_page("Profile", "identity", body)


@app.post("/identity")
def identity_post(
    first_name: str = Form(...),
    middle_name: str = Form(""),
    last_name: str = Form(...),
    emails: str = Form(""),
    phones: str = Form(""),
    addresses: str = Form(""),
    eraser_profile: str = Form(""),
    cfg: Config = Depends(get_config),
):
    """Save the identity form -- fixed to (1) write to cfg.profile_path
    (config.DEFAULT_PROFILE_PATH == profile.local.json, NOT profile.json),
    (2) validate through profile.load_profile before ever touching the real
    file (an invalid submission never overwrites a good profile), and
    (3) round-trip eraser_profile instead of dropping it.

    Emails/phones are one-per-line textareas (not per-row add/remove
    buttons): split on newline, trim, drop blanks, case-insensitive dedupe.
    Emails are additionally format-validated (a malformed line rejects the
    whole submission with a 400, same "never touch a good profile with a
    bad submission" rule as first_name/last_name below); phones are
    additionally normalized -- see ``normalize_phone``.
    """
    email_list = _dedupe_case_insensitive(_split_list(emails))
    invalid_emails = [e for e in email_list if not _EMAIL_RE.match(e)]
    if invalid_emails:
        raise HTTPException(
            status_code=400,
            detail="invalid email address(es): " + ", ".join(invalid_emails),
        )
    phone_list = _dedupe_case_insensitive([normalize_phone(p) for p in _split_list(phones)])

    data = {
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "emails": email_list,
        "phones": phone_list,
        "addresses": _split_list(addresses),
        "eraser_profile": eraser_profile.strip() or None,
    }

    target = cfg.profile_path
    parent = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".profile-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        profile_mod.load_profile(tmp_path)  # validates; raises ValueError on bad input
        os.replace(tmp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except ValueError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    # ...and mirror the same save onto the ACTIVE entry in the profiles
    # list (creating it if this is the first identity ever saved), so the
    # merged page's list and the legacy file the scan loop reads can never
    # drift apart. Deliberately AFTER the validated write above: a bad
    # submission is rejected before either store is touched.
    try:
        profiles_mod.upsert_active_profile(cfg.profiles_path, data)
    except (OSError, ValueError) as exc:
        log.warning("profiles list upsert failed", extra={"error": str(exc)})
    else:
        _sync_eraser_profiles(cfg)

    return RedirectResponse(url="/identity", status_code=303)


@app.post("/identity/id-document")
async def upload_id_document(
    side: str = Form(...),
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
):
    """Store a front/back government-ID image, encrypted at rest.

    The key is always cfg.crypto_key (BG_CRYPTO_KEY), never hardcoded and
    never generated by this module. Only the side and the byte count are
    ever logged -- never the filename (it can carry PII), never the raw or
    decrypted bytes.
    """
    if side not in ID_DOC_SIDES:
        raise HTTPException(status_code=400, detail="side must be 'front' or 'back'")
    if not cfg.crypto_key:
        raise HTTPException(
            status_code=500,
            detail="BG_CRYPTO_KEY is not configured; refusing to store an ID document unencrypted",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    encoded = base64.b64encode(raw).decode("ascii")
    try:
        token = encrypt_field(encoded, cfg.crypto_key.encode("utf-8"))
    except Exception as exc:
        log.error("id-document encryption failed", extra={"side": side, "error": type(exc).__name__})
        raise HTTPException(status_code=500, detail="encryption failed") from exc

    os.makedirs(cfg.id_documents_dir, exist_ok=True)
    dest = os.path.join(cfg.id_documents_dir, f"{side}.enc")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass

    log.info("id-document stored", extra={"side": side, "bytes": len(raw)})
    return {"side": side, "stored": True}


# --- /exposure : breach-exposure panel --------------------------------------

@app.get("/exposure", response_class=HTMLResponse)
def exposure_page(cfg: Config = Depends(get_config),
                   client: exposure_mod.XposedOrNotClient = Depends(get_exposure_client)):
    try:
        identity = profile_mod.load_profile(cfg.profile_path)
    except (OSError, ValueError) as exc:
        return HTMLResponse(
            "<!DOCTYPE html><html><body><p>no profile yet: {}</p></body></html>".format(
                html.escape(str(exc))
            )
        )

    results = exposure_mod.profile_exposure(identity.emails, client=client)

    cards = []
    for email, breaches in results.items():
        if breaches:
            list_html = "".join('<li>{}</li>'.format(html.escape(name)) for name in breaches)
            body_html = '<ul style="margin:8px 0 0;padding-left:18px;">{}</ul>'.format(list_html)
            tone = "action"
        else:
            body_html = '<p class="muted" style="margin:8px 0 0;">No known breaches.</p>'
            tone = "success"
        cards.append(
            '<div class="card" style="margin-bottom:14px;">'
            '<div style="display:flex;align-items:center;justify-content:space-between;">'
            '<strong>{email}</strong>{badge}</div>{body}</div>'.format(
                email=html.escape(email),
                badge=style.badge("{} breach(es)".format(len(breaches)), tone),
                body=body_html,
            )
        )
    if not cards:
        cards.append('<p class="muted">No emails on the profile to check -- add one on the '
                      '<a href="/identity" style="color:var(--teal-ink);font-weight:600;">Profile</a> page.</p>')

    body = """
<div class="page-head"><h1>Breach Exposure</h1></div>
{cards}
<p class="faint" style="font-size:12px;"><em>{attribution}</em></p>
""".format(cards="".join(cards), attribution=html.escape(exposure_mod.XPOSEDORNOT_ATTRIBUTION))
    return style.render_page("Exposure", "exposure", body)


# --- /freeze : credit-freeze bureau tracker ---------------------------------

@app.get("/freeze", response_class=HTMLResponse)
def freeze_page(cfg: Config = Depends(get_config)):
    try:
        identity_key = profile_mod.load_profile(cfg.profile_path).identity_key
    except (OSError, ValueError):
        identity_key = None

    states = freeze_mod.load_freeze_states(cfg.freeze_state_path, identity_key) if identity_key else {}

    rows = []
    for key, bureau in freeze_mod.BUREAUS.items():
        state = states.get(key) or freeze_mod.BureauFreezeState(bureau_key=key)
        confidence = (
            "NEEDS VERIFICATION -- confirm this URL before relying on it"
            if key in NEEDS_VERIFICATION_BUREAUS else "confirmed"
        )
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            '<td><a href="{url}" rel="noopener noreferrer" style="color:var(--teal-ink);font-weight:600;">{url}</a></td></tr>'.format(
                html.escape(bureau["display_name"]),
                style.badge(html.escape(state.status), "success" if state.status == "frozen" else "neutral"),
                "yes" if state.has_pin() else "no",
                html.escape(confidence),
                url=html.escape(bureau["freeze_url"], quote=True),
            )
        )

    body = """
<div class="page-head"><h1>Credit Freeze Tracker</h1></div>
<div class="card">
  <table class="dtable">
    <tr style="text-align:left;font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:var(--faint);">
      <th style="padding-bottom:10px;">Bureau</th><th>Status</th><th>Has PIN</th><th>Confidence</th><th>Freeze URL</th>
    </tr>
    {rows}
  </table>
</div>
""".format(rows="\n".join(rows))
    return style.render_page("Credit Freeze", "freeze", body)


@app.post("/freeze/{bureau_key}/status")
def freeze_status_update(bureau_key: str, new_status: str = Form(...),
                          cfg: Config = Depends(get_config)):
    if bureau_key not in freeze_mod.BUREAUS:
        raise HTTPException(status_code=404, detail="unknown bureau")
    try:
        identity = profile_mod.load_profile(cfg.profile_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot load profile: {exc}")

    states = freeze_mod.load_freeze_states(cfg.freeze_state_path, identity.identity_key)
    state = states.get(bureau_key) or freeze_mod.BureauFreezeState(bureau_key=bureau_key)
    try:
        freeze_mod.transition(state, new_status, _utcnow_iso())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    freeze_mod.save_freeze_state(cfg.freeze_state_path, identity.identity_key, state)
    return {"bureau_key": bureau_key, "status": state.status}


# --- /profiles : multi-profile CRUD, synced into eraser's config.yaml ------
#
# v1 SCOPE (stated explicitly, not silently half-wired): this is profile
# CRUD + eraser-sync ONLY. Adding, editing or removing a profile here keeps
# eraser's ~/.eraser/config.yaml `profiles:` list in sync (so `eraser send
# --profile <id>` etc. work for any of them from the CLI), but broker-
# guard's OWN scan/removal-detection/autopilot loop still reads exactly one
# identity, from the separate, pre-existing profile.local.json
# (Config.profile_path) -- unchanged by anything below. Wiring N profiles
# through that loop concurrently (a profile switcher on the dashboard,
# per-profile presence/broker_status rows) is real additional work, and is
# a stated follow-up, not part of this change.

def _sync_eraser_profiles(cfg: Config) -> None:
    """Best-effort: mirror every broker-guard profile into eraser's
    profiles: list. Never lets a filesystem/YAML problem on the eraser
    side turn a successful CRUD write into a 500 -- the broker-guard-side
    write (the source of truth for this UI) already succeeded by the time
    this runs."""
    try:
        current = profiles_mod.load_profiles(cfg.profiles_path)
        eraser_config_mod.sync_profiles(current, path=cfg.eraser_config_path)
    except (OSError, ValueError) as exc:
        log.warning("eraser profiles sync failed", extra={"error": str(exc)})


def _sync_active_profile_to_legacy(cfg: Config) -> None:
    """Mirror whichever profile is active into cfg.profile_path -- the
    single profile.local.json that service.run_once/autopilot read.

    This is the ONLY direction the merged UI writes that file outside
    ``POST /identity`` (which writes it directly, after validating). The
    scan loop's contract is unchanged by the merge: it still reads exactly
    one Identity from cfg.profile_path; this just keeps that one file
    pointing at whichever profile the list says is active.
    """
    try:
        profiles_mod.sync_active_to_legacy(cfg.profiles_path, cfg.profile_path)
    except (OSError, ValueError) as exc:
        log.warning("active profile sync to legacy file failed", extra={"error": str(exc)})


def _profile_row_html(p: "profiles_mod.NamedProfile") -> str:
    """One non-active profile's row on the merged /identity page. "Make
    active" is what switches which identity the scan loop runs against;
    "Edit" opens this profile in place on the same page rather than
    navigating to a second, divergent profile UI."""
    return """
<tr>
  <td><strong>{name}</strong><br><span class="sub faint" style="font-size:12px;">{pid}</span></td>
  <td>{emails}</td>
  <td>{phones}</td>
  <td style="display:flex;gap:8px;">
    <form method="post" action="/profiles/{pid_attr}/activate">
      <button type="submit" class="btn secondary" style="height:32px;padding:0 12px;font-size:12px;">Make active</button>
    </form>
    <a class="btn secondary" style="height:32px;padding:0 12px;font-size:12px;" href="/identity?edit={pid_attr}">Edit</a>
    <form method="post" action="/profiles/{pid_attr}/remove" onsubmit="return confirm('Remove this profile? Its eraser send history is kept and reappears if re-added with the same id.');">
      <button type="submit" class="btn secondary" style="height:32px;padding:0 12px;font-size:12px;">Remove</button>
    </form>
  </td>
</tr>
""".format(
        name=html.escape(p.full_name), pid=html.escape(p.id),
        emails=html.escape(", ".join(p.emails) or "--"),
        phones=html.escape(", ".join(p.phones) or "--"),
        pid_attr=style.escape_attr(p.id),
    )


def _profile_edit_card_html(p: "profiles_mod.NamedProfile") -> str:
    """The in-place edit card for a NON-active profile (``/identity?edit=
    <id>``). The active profile is edited through the main form at the top
    of the page instead, which posts to /identity."""
    return """
<div class="grid-main" style="margin-top:24px;">
  <div class="card">
    <h2>Edit profile -- {name}</h2>
    <form method="post" action="/profiles/{pid_attr}">
      <div class="field"><label>First name</label><input class="inp" type="text" name="first_name" value="{first_name}" required></div>
      <div class="field"><label>Middle name</label><input class="inp" type="text" name="middle_name" value="{middle_name}"></div>
      <div class="field"><label>Last name</label><input class="inp" type="text" name="last_name" value="{last_name}" required></div>
      <div class="field"><label>Emails (one per line)</label><textarea class="inp" name="emails" rows="3">{emails}</textarea></div>
      <div class="field"><label>Phones (one per line)</label><textarea class="inp" name="phones" rows="3">{phones}</textarea></div>
      <div class="field"><label>Addresses (one per line)</label><textarea class="inp" name="addresses" rows="3">{addresses}</textarea></div>
      <button type="submit" class="btn">Save</button>
      <a class="btn secondary" href="/identity" style="margin-left:8px;">Cancel</a>
    </form>
  </div>
</div>
""".format(
        name=html.escape(p.full_name or p.id), pid_attr=style.escape_attr(p.id),
        first_name=html.escape(p.first_name), middle_name=html.escape(p.middle_name),
        last_name=html.escape(p.last_name),
        emails=html.escape("\n".join(p.emails)), phones=html.escape("\n".join(p.phones)),
        addresses=html.escape("\n".join(p.addresses)),
    )


@app.get("/profiles")
def profiles_page(cfg: Config = Depends(get_config)):
    """Thin alias -- /identity IS the profiles page now.

    Kept (rather than deleted) so bookmarks, the old nav entry and any
    external link still land somewhere useful instead of 404ing. It
    renders nothing of its own: keeping a second copy of this UI is
    exactly the two-divergent-pages problem the merge removed.
    """
    return RedirectResponse(url="/identity", status_code=303)


@app.post("/profiles")
def profiles_add(
    first_name: str = Form(...),
    middle_name: str = Form(""),
    last_name: str = Form(...),
    emails: str = Form(""),
    phones: str = Form(""),
    addresses: str = Form(""),
    cfg: Config = Depends(get_config),
):
    email_list = _dedupe_case_insensitive(_split_list(emails))
    invalid_emails = [e for e in email_list if not _EMAIL_RE.match(e)]
    if invalid_emails:
        raise HTTPException(status_code=400, detail="invalid email address(es): " + ", ".join(invalid_emails))
    phone_list = _dedupe_case_insensitive([normalize_phone(p) for p in _split_list(phones)])

    try:
        created = profiles_mod.add_profile(cfg.profiles_path, {
            "first_name": first_name, "middle_name": middle_name, "last_name": last_name,
            "emails": email_list, "phones": phone_list, "addresses": _split_list(addresses),
        })
    except profiles_mod.ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # add_profile auto-activates the very first profile -- which means it
    # is now the identity the scan loop runs against, so mirror it into
    # the legacy file that loop reads.
    if created.active:
        _sync_active_profile_to_legacy(cfg)
    _sync_eraser_profiles(cfg)
    return RedirectResponse(url="/identity", status_code=303)


@app.get("/profiles/{profile_id}/edit")
def profiles_edit_get(profile_id: str, cfg: Config = Depends(get_config)):
    """Thin alias for the old standalone edit page -- editing now happens
    in place on /identity. Still 404s an unknown id rather than bouncing
    to a page that would silently show something else."""
    try:
        p = profiles_mod.get_profile(cfg.profiles_path, profile_id)
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")
    return RedirectResponse(
        url="/identity?edit={}".format(urllib.parse.quote(p.id, safe="")),
        status_code=303,
    )


@app.post("/profiles/{profile_id}/activate")
def profiles_activate(profile_id: str, cfg: Config = Depends(get_config)):
    """Switch which profile is active -- i.e. which single identity the
    dashboard/scan/autopilot loop runs against. Mirrors the newly active
    profile into cfg.profile_path (the loop's unchanged one-Identity
    contract) and re-syncs eraser's list."""
    try:
        profiles_mod.set_active(cfg.profiles_path, profile_id)
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")

    _sync_active_profile_to_legacy(cfg)
    _sync_eraser_profiles(cfg)
    return RedirectResponse(url="/identity", status_code=303)


@app.post("/profiles/{profile_id}")
def profiles_edit_post(
    profile_id: str,
    first_name: str = Form(...),
    middle_name: str = Form(""),
    last_name: str = Form(...),
    emails: str = Form(""),
    phones: str = Form(""),
    addresses: str = Form(""),
    cfg: Config = Depends(get_config),
):
    email_list = _dedupe_case_insensitive(_split_list(emails))
    invalid_emails = [e for e in email_list if not _EMAIL_RE.match(e)]
    if invalid_emails:
        raise HTTPException(status_code=400, detail="invalid email address(es): " + ", ".join(invalid_emails))
    phone_list = _dedupe_case_insensitive([normalize_phone(p) for p in _split_list(phones)])

    try:
        updated = profiles_mod.update_profile(cfg.profiles_path, profile_id, {
            "first_name": first_name, "middle_name": middle_name, "last_name": last_name,
            "emails": email_list, "phones": phone_list, "addresses": _split_list(addresses),
        })
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")
    except profiles_mod.ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Editing the active profile has to reach the legacy file too, or the
    # scan loop would keep running against the pre-edit identity.
    if updated.active:
        _sync_active_profile_to_legacy(cfg)
    _sync_eraser_profiles(cfg)
    return RedirectResponse(url="/identity", status_code=303)


@app.post("/profiles/{profile_id}/remove")
def profiles_remove(profile_id: str, cfg: Config = Depends(get_config)):
    """Delete from broker-guard's own store (and re-sync eraser's list to
    match) -- never touches eraser's history.db, so this profile's send
    history is preserved and reappears if a profile with this same id is
    ever re-added (see profiles.py's module docstring).

    Removing the ACTIVE profile promotes another one (remove_profile keeps
    the one-active invariant); that promoted profile is mirrored into the
    legacy file so the scan loop is never left pointing at a deleted
    identity. Removing the LAST profile leaves the legacy file alone
    rather than truncating it -- see ``sync_active_to_legacy``.
    """
    try:
        promoted = profiles_mod.remove_profile(cfg.profiles_path, profile_id)
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")

    if promoted is not None:
        _sync_active_profile_to_legacy(cfg)
    _sync_eraser_profiles(cfg)
    return RedirectResponse(url="/identity", status_code=303)


@app.post("/freeze/{bureau_key}/pin")
def freeze_pin_set(bureau_key: str, pin: str = Form(...), cfg: Config = Depends(get_config)):
    """Set (encrypt + store) the PIN for one bureau.

    Deliberately no matching GET-the-plaintext-PIN route: serving a
    decrypted PIN back over this HTTP interface would defeat the point of
    encrypting it at rest. ``has_pin`` (bool) is all any route ever reports.
    """
    if bureau_key not in freeze_mod.BUREAUS:
        raise HTTPException(status_code=404, detail="unknown bureau")
    if not cfg.crypto_key:
        raise HTTPException(
            status_code=500,
            detail="BG_CRYPTO_KEY is not configured; refusing to store a PIN unencrypted",
        )
    try:
        identity = profile_mod.load_profile(cfg.profile_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot load profile: {exc}")

    states = freeze_mod.load_freeze_states(cfg.freeze_state_path, identity.identity_key)
    state = states.get(bureau_key) or freeze_mod.BureauFreezeState(bureau_key=bureau_key)
    state.set_pin(pin, cfg.crypto_key.encode("utf-8"))
    freeze_mod.save_freeze_state(cfg.freeze_state_path, identity.identity_key, state)
    log.info("freeze pin stored", extra={"bureau_key": bureau_key})
    return {"bureau_key": bureau_key, "has_pin": True}
