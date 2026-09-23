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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from broker_guard import brokers as brokers_mod
from broker_guard import eraser as eraser_mod
from broker_guard import eraser_config as eraser_config_mod
from broker_guard import exposure as exposure_mod
from broker_guard import freeze as freeze_mod
from broker_guard import optout_forms
from broker_guard import optout_submit
from broker_guard import profile as profile_mod
from broker_guard import progress as progress_mod
from broker_guard import profiles as profiles_mod
from broker_guard import review as review_mod
from broker_guard import service as service_mod
from broker_guard import settings as settings_mod
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
    """Fresh Config on every request: the environment tier, with the stored
    (UI-editable) settings overlaid on top of it.

    Deliberately NOT cached at import/module time: a webserver process
    outlives a single request, and tests need a clean, isolated Config per
    test without mutating shared process environment. Re-reading the settings
    store per request is also what makes a setting saved on ``/settings``
    apply to the very next page load -- including to a ``/scan`` started from
    the dashboard, which builds its dependencies from this same Config.
    """
    return settings_mod.effective_config(load_config())


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

    A scan the user stopped is reported as "stopped early", never as "ok":
    it did not cover every broker, so calling it ok would make the
    timestamp claim a full pass that never happened. "failed" would be
    just as dishonest in the other direction -- nothing went wrong.
    """
    if scan.get("running"):
        if scan.get("stop_requested"):
            base = scan.get("progress_line") or "Scan in progress right now."
            return base + " Stopping after the current broker..."
        return scan.get("progress_line") or "Scan in progress right now."
    if scan.get("last_run_at"):
        if scan.get("stopped"):
            state = "stopped early"
        elif scan.get("last_run_ok"):
            state = "ok"
        else:
            state = "failed"
        line = "Last scan: {} ({}). Next scan around: {}.".format(
            format_scan_timestamp(scan["last_run_at"]) or scan["last_run_at"],
            state,
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
                # Same human formatting as every other timestamp in the UI,
                # rather than the raw isoformat the row carries.
                when=html.escape(format_scan_timestamp(r["status_updated_at"]) or ""),
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

    # "Stop scan" is enabled from exactly the signal that disables "Run scan
    # now" -- scan.running -- so the pair can never both be live or both be
    # dead. Once a stop is already requested it goes back to disabled, since
    # asking twice does nothing.
    if scan["running"] and not scan.get("stop_requested"):
        stop_attrs = ""
    else:
        stop_attrs = " disabled"
    stop_label = "Stopping..." if scan.get("stop_requested") else "Stop scan"

    body = """
<div class="page-head">
  <div><h1>Removals</h1><div class="muted" id="scanline">{scan_line}</div></div>
  <div class="rowactions">
    <button class="btn secondary" id="runScanBtn" onclick="runScanNow()"{scan_btn_attrs}>{scan_btn_label}</button>
    <button type="button" class="btn secondary" id="stopScanBtn" onclick="stopScan()"{stop_attrs}>{stop_label}</button>
  </div>
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
    var stopBtn = document.getElementById('stopScanBtn');
    if (stopBtn) {{
      stopBtn.disabled = !!scan.stop_requested;
      stopBtn.textContent = scan.stop_requested ? 'Stopping...' : 'Stop scan';
    }}
    setTimeout(pollScan, 1500);
  }}).catch(function () {{ setTimeout(pollScan, 1500); }});
}}

// Cooperative stop: the sweep finishes the broker it is on and then ends,
// so the button says "Stopping..." until /status stops reporting a running
// scan -- it never claims the scan is already over.
function stopScan() {{
  var btn = document.getElementById('stopScanBtn');
  if (btn) {{ btn.disabled = true; btn.textContent = 'Stopping...'; }}
  fetch('/scan/stop', {{method: 'POST'}}).catch(function () {{}});
}}

function runScanNow() {{
  var btn = document.getElementById('runScanBtn');
  btn.disabled = true; btn.textContent = 'Starting...';
  var stopBtn = document.getElementById('stopScanBtn');
  if (stopBtn) {{ stopBtn.disabled = false; stopBtn.textContent = 'Stop scan'; }}
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
        stop_attrs=stop_attrs, stop_label=stop_label,
    )

    return style.render_page("Dashboard", "dashboard", body, action_needed_count=action_needed)


# --- /brokers + /status : presence and removal status -----------------------

def _format_steps(steps: list[dict]) -> list[dict]:
    """The stepper's ``at`` timestamps, rendered the same way every other
    timestamp in the UI is (``format_scan_timestamp``) rather than as the
    raw ``2026-09-22T21:45:02.742110+00:00`` isoformat the db stores."""
    return [{**s, "at": format_scan_timestamp(s.get("at"))} for s in steps]


def _broker_row_html(row: dict, broker_meta: dict, scan_interval_seconds: int,
                      profile_names: dict | None = None) -> str:
    """One expandable ``<details>`` row: summary line (name, profile, kind,
    status, last update) plus the 4-step lifecycle stepper and remove
    action on expand. ``data-*`` attributes carry the values the
    brokers-page's client-side search/status/action-needed filters and the
    last-update sort key off of -- neither ever re-fetches or re-renders,
    they only show/hide and reorder these same rows.

    The profile column exists because every profile is scanned every cycle
    now: a tracked listing is one PERSON's listing, and a page that showed
    "found on Spokeo" without saying who would be ambiguous the moment a
    second profile existed.
    """
    broker_id = str(row["broker_id"])
    meta = broker_meta.get(broker_id, {})
    name = meta.get("name") or broker_id
    kind = brokers_mod.verification_kind(meta) if meta else "manual_review"
    status = row.get("removal_status")
    step = webui_data.broker_stepper(row, scan_interval_seconds)
    action_needed = status in ("needs_document", "needs_review")
    identity_key = str(row.get("identity_key") or "")
    profile_name = (profile_names or {}).get(identity_key) or "unknown profile"
    # The sort/display pair for "last update": the newest real timestamp
    # this row has. data-updated stays the RAW ISO value because ISO-8601
    # sorts correctly as plain text, while the cell shows the formatted one.
    updated_raw = max(
        [t for t in (row.get("last_seen"), row.get("status_updated_at")) if t],
        default="",
    )

    return """
<details class="brokerrow" data-name="{name_lower}" data-status="{status}" data-action-needed="{action_needed}"
         data-profile="{profile_lower}" data-updated="{updated_attr}">
  <summary class="row-summary">
    <span><span class="name">{name}</span><br><span class="sub">{url}</span></span>
    <span class="sub">{profile}</span>
    <span>{kind_label}</span>
    <span>{status_badge}</span>
    <span class="sub">{last_seen}</span>
    <span class="chevron">&#9656;</span>
  </summary>
  <div class="row-detail">
    {stepper}
    <dl class="pii-grid">
      <dt>Broker</dt><dd>{name} &middot; {url}</dd>
      <dt>Profile</dt><dd>{profile}</dd>
      <dt>First seen</dt><dd>{first_seen}</dd>
      <dt>Last checked</dt><dd>{last_seen}</dd>
      <dt>Removal status</dt><dd>{status_text}</dd>
    </dl>
    <form method="post" action="/brokers/{broker_id_attr}/remove">
      <input type="hidden" name="identity_key" value="{identity_attr}">
      <button type="submit" class="btn secondary">Send removal now</button>
    </form>
  </div>
</details>
""".format(
        name_lower=style.escape_attr(name.lower()),
        status=style.escape_attr(status or ""),
        action_needed="true" if action_needed else "false",
        profile_lower=style.escape_attr(profile_name.lower()),
        updated_attr=style.escape_attr(updated_raw),
        name=html.escape(name),
        url=html.escape(str(meta.get("url") or "")),
        profile=html.escape(profile_name),
        kind_label=html.escape(style.KIND_LABELS.get(kind, kind)),
        status_badge=style.status_badge(status),
        last_seen=html.escape(format_scan_timestamp(row.get("last_seen")) or ""),
        stepper=style.stepper(_format_steps(step["steps"])),
        first_seen=html.escape(format_scan_timestamp(row.get("first_seen")) or ""),
        status_text=html.escape(status or "not submitted"),
        broker_id_attr=style.escape_attr(broker_id),
        identity_attr=style.escape_attr(identity_key),
    )


def _identity_options(cfg: Config) -> list[dict]:
    """The profile picker's options: every saved profile, with the
    ``identity_key`` its ``presence``/``broker_status`` rows are scoped by
    (``profiles.identity_key``, which delegates to the one canonical
    derivation in ``profile.Identity``). An unreadable/empty profiles list
    yields ``[]``, which the page renders as "no profiles saved yet"
    rather than 500ing.

    The legacy ``profile.local.json`` is migrated into the list first (a
    cheap no-op once the list is non-empty). Without that, a deployment
    that never opened the Profiles page would be SCANNED -- the scan loop
    falls back to the legacy file, see ``load_scan_identities`` -- while
    showing no profiles at all here, so every one of its results rendered
    as "not yet checked". The set of people shown and the set of people
    swept have to be the same set.
    """
    try:
        profiles_mod.migrate_legacy_profile_if_needed(cfg.profiles_path, cfg.profile_path)
    except (OSError, ValueError):
        pass
    try:
        saved = profiles_mod.load_profiles(cfg.profiles_path)
    except (OSError, ValueError):
        return []
    options = []
    for p in saved:
        try:
            key = profiles_mod.identity_key(p)
        except Exception:  # pragma: no cover - defensive
            continue
        options.append({"id": p.id, "name": p.full_name or p.id, "identity_key": key})
    return options


def _scan_result_row_html(row: dict) -> str:
    """One row of the "Scan results" card: every broker in the roster
    paired with every profile, carrying THIS scan's outcome for that pair.

    ``data-outcome`` is what the client-side filter and the live poll
    update key off; ``data-row-key`` is the progress map's own
    ``identity_key|broker_id`` key, so the poll repaints a row with the
    entry for that row's PROFILE and never another's. ``data-updated``
    carries the raw ISO timestamp for the last-update sort while the cell
    shows the formatted one.
    """
    label = webui_data.SCAN_OUTCOME_LABELS[row["outcome"]]
    tone = webui_data.SCAN_OUTCOME_TONES[row["outcome"]]
    return """
<div class="scanrow" data-broker-id="{bid_attr}" data-row-key="{row_key}" data-name="{name_lower}"
     data-outcome="{outcome}" data-profile="{profile_lower}" data-updated="{updated_attr}">
  <span><span class="name">{name}</span><br><span class="sub">{url}</span></span>
  <span class="sub profile">{profile}</span>
  <span class="outcome">{badge}</span>
  <span class="sub when">{checked_at}</span>
</div>
""".format(
        bid_attr=style.escape_attr(row["broker_id"]),
        row_key=style.escape_attr(
            progress_mod.entry_key(row.get("identity_key"), row["broker_id"])),
        name_lower=style.escape_attr(str(row["name"]).lower()),
        outcome=style.escape_attr(row["outcome"]),
        profile_lower=style.escape_attr(str(row.get("profile_name") or "").lower()),
        updated_attr=style.escape_attr(str(row.get("checked_at") or "")),
        name=html.escape(str(row["name"])),
        url=html.escape(str(row["url"] or "")),
        profile=html.escape(str(row.get("profile_name") or "--")),
        badge=style.badge(html.escape(label), tone),
        checked_at=html.escape(format_scan_timestamp(row.get("checked_at")) or ""),
    )


@app.get("/brokers", response_class=HTMLResponse)
def brokers_page(identity: str = "", cfg: Config = Depends(get_config),
                  jobs: dict = Depends(get_jobs)):
    """Two cards, two different questions -- deliberately not merged:

    * **Tracked listings** (unchanged): the ``presence``/``broker_status``
      history, i.e. "where am I listed and what has been sent". It can
      only ever contain brokers the person was FOUND on, which is why it
      stayed empty through a clean 827-broker scan.
    * **Scan results** (new): every broker in ``brokers.json`` with the
      outcome the most recent scan in THIS process recorded for it --
      found / clean / failed / not checkable / not yet checked.

    Every saved profile is scanned every cycle (there is no "active" one
    -- see ``profiles.py``), so BOTH cards are per-person by default:
    every row says which profile it belongs to, and the page shows every
    profile at once. ``identity=<profile id>`` narrows both cards to one
    person; the default, and ``identity=all``, is the whole household.
    """
    options = _identity_options(cfg)
    selected = next((o for o in options if o["id"] == identity), None) if identity else None
    selected_key = selected["identity_key"] if selected else None
    shown_profiles = [selected] if selected else options
    profile_names = {o["identity_key"]: o["name"] for o in options}

    conn = state_mod.init_db(cfg.state_path)
    try:
        rows = webui_data.query_broker_status(conn, identity_key=selected_key)
    finally:
        conn.close()

    try:
        broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    except (OSError, ValueError):
        broker_list = []
    broker_meta = {b["id"]: b for b in broker_list}

    progress_snapshot = progress_mod.snapshot(include_brokers=True)
    scan_rows = webui_data.scan_outcome_rows(broker_list, progress_snapshot, shown_profiles)
    scan_counts = webui_data.scan_outcome_counts(scan_rows)
    with _JOBS_LOCK:
        jobs_summary = {jid: j.get("status") for jid, j in jobs.items()}
    scan = webui_data.scan_status(_read_heartbeat(cfg), jobs_summary, cfg.interval_seconds,
                                   progress=progress_snapshot)
    scan_line = webui_data.scan_outcome_line(
        scan_counts, active=bool(progress_snapshot.get("active")),
        stopped=bool(scan.get("stopped")),
    )
    profiles_line = webui_data.profiles_checked_line(shown_profiles, progress_snapshot)

    action_needed = _action_needed_count(rows)
    rows_html = "".join(
        _broker_row_html(row, broker_meta, cfg.interval_seconds, profile_names)
        for row in rows
    )
    if not rows:
        rows_html = ('<p class="muted" style="padding:16px;">No listings tracked yet'
                     ' -- this card only fills in when a scan actually FINDS someone somewhere.'
                     ' Every broker that has been checked is in "Scan results" below.</p>')

    if options:
        picker = '<select id="identityPicker" onchange="switchIdentity()">{}{}</select>'.format(
            '<option value="all"{}>All profiles</option>'.format(
                "" if selected else " selected"),
            "".join(
                '<option value="{v}"{sel}>{label}</option>'.format(
                    v=style.escape_attr(o["id"]),
                    sel=" selected" if (selected and o["id"] == selected["id"]) else "",
                    label=html.escape(o["name"]),
                )
                for o in options
            ),
        )
    else:
        picker = '<span class="muted">No profiles saved yet.</span>'

    scan_rows_html = "".join(_scan_result_row_html(row) for row in scan_rows)
    if not scan_rows:
        scan_rows_html = ('<p class="muted" style="padding:16px;">No broker list loaded'
                          ' -- check BG_BROKERS_PATH.</p>')

    body = """
<div class="page-head">
  <div><h1>Brokers</h1><div class="muted">{count} listing(s) tracked{scope_label}</div></div>
  <div style="display:flex;align-items:center;gap:8px;">
    <button type="button" class="btn secondary" id="stopScanBtn" onclick="stopScan()"{stop_attrs}>{stop_label}</button>
    <span class="muted" style="font-size:13px;">Profile</span>{picker}
  </div>
</div>
<div class="card">
  <div class="section-label">Tracked listings -- who was found where, and removal status</div>
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
  <div class="row-summary rowhead">
    <span class="sortable" data-sort="name" onclick="sortBy('rowsContainer','.brokerrow','name',this)">Broker<span class="sortarrow"></span></span>
    <span class="sortable" data-sort="profile" onclick="sortBy('rowsContainer','.brokerrow','profile',this)">Profile<span class="sortarrow"></span></span>
    <span>Verification</span>
    <span>Status</span>
    <span class="sortable" data-sort="updated" onclick="sortBy('rowsContainer','.brokerrow','updated',this)">Last update<span class="sortarrow"></span></span>
    <span></span>
  </div>
  <div id="rowsContainer">{rows_html}</div>
</div>
<div class="card" style="margin-top:18px;" data-scan-running="{scan_running}" id="scanCard">
  <div class="section-label">Scan results -- every broker, every profile, this scan</div>
  <div class="muted" id="scanOutcomeLine">{scan_line}</div>
  <div class="muted" id="profilesCheckedLine" style="margin-top:4px;">{profiles_line}</div>
  <div class="toolbar" style="margin-top:14px;">
    <input type="text" id="scanSearchBox" placeholder="Search brokers..." oninput="filterScanRows()">
    <select id="outcomeFilter" onchange="filterScanRows()">
      <option value="">All outcomes</option>
      {outcome_options}
    </select>
  </div>
  <div class="scanrow rowhead">
    <span class="sortable" onclick="sortBy('scanRowsContainer','.scanrow','name',this)">Broker<span class="sortarrow"></span></span>
    <span class="sortable" onclick="sortBy('scanRowsContainer','.scanrow','profile',this)">Profile<span class="sortarrow"></span></span>
    <span>Outcome</span>
    <span class="sortable" onclick="sortBy('scanRowsContainer','.scanrow','updated',this)">Last update<span class="sortarrow"></span></span>
  </div>
  <div id="scanRowsContainer">{scan_rows_html}</div>
</div>
<script>
// Column sorting, shared by both cards. Purely client side over the rows
// already on the page: it reorders DOM nodes, it never re-queries. The
// key is read from a data-* attribute, and `updated` sorts on the RAW ISO
// timestamp (which sorts correctly as text) while the cell displays the
// formatted one -- so what you see and what you sort by cannot diverge.
// A row with no timestamp always sorts LAST, in both directions: "never
// updated" is not "updated at the beginning of time".
function sortBy(containerId, itemSelector, key, header) {{
  var container = document.getElementById(containerId);
  if (!container) return;
  var asc = header.dataset.dir !== 'asc';
  document.querySelectorAll('.sortable').forEach(function (h) {{
    if (h !== header) {{ h.dataset.dir = ''; h.querySelector('.sortarrow').textContent = ''; }}
  }});
  header.dataset.dir = asc ? 'asc' : 'desc';
  header.querySelector('.sortarrow').textContent = asc ? ' \\u25B2' : ' \\u25BC';
  var items = Array.prototype.slice.call(container.querySelectorAll(itemSelector));
  items.sort(function (a, b) {{
    var av = a.dataset[key] || '', bv = b.dataset[key] || '';
    if (!av && !bv) return 0;
    if (!av) return 1;
    if (!bv) return -1;
    if (av === bv) return 0;
    return (av < bv ? -1 : 1) * (asc ? 1 : -1);
  }});
  items.forEach(function (item) {{ container.appendChild(item); }});
}}

// Stop scan: cooperative, so the button reports "Stopping..." until the
// server confirms the sweep has actually ended rather than claiming the
// scan is over the instant it is clicked.
function stopScan() {{
  var btn = document.getElementById('stopScanBtn');
  if (btn) {{ btn.disabled = true; btn.textContent = 'Stopping...'; }}
  fetch('/scan/stop', {{method: 'POST'}}).catch(function () {{}});
}}

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

function switchIdentity() {{
  var v = document.getElementById('identityPicker').value;
  location.search = '?identity=' + encodeURIComponent(v);
}}

function filterScanRows() {{
  var q = document.getElementById('scanSearchBox').value.toLowerCase();
  var outcome = document.getElementById('outcomeFilter').value;
  document.querySelectorAll('.scanrow').forEach(function(row) {{
    var matches = true;
    if (q && row.dataset.name.indexOf(q) === -1) matches = false;
    if (outcome && row.dataset.outcome !== outcome) matches = false;
    row.style.display = matches ? '' : 'none';
  }});
}}

// Live updates for a scan that is running RIGHT NOW. Same shape as the
// dashboard's pollScan: one fetch of /status on a timer, repaint, and
// reload once the server says the scan is over (so the page ends up on
// the server-rendered truth rather than a JS-patched approximation).
// ?brokers=1 is what adds the per-broker map to the payload -- the
// dashboard's poll deliberately does not ask for it.
var OUTCOME_LABELS = {outcome_labels_json};
var OUTCOME_TONES = {outcome_tones_json};

function applyScanUpdate(entries) {{
  document.querySelectorAll('.scanrow').forEach(function(row) {{
    // Keyed by identity_key|broker_id, so a row can only ever pick up the
    // entry recorded for ITS OWN profile -- one person's hit is never
    // painted onto another person's row.
    var entry = entries[row.dataset.rowKey];
    if (!entry) return;
    var outcome = entry.outcome;
    if (!OUTCOME_LABELS[outcome]) return;
    row.dataset.outcome = outcome;
    var badge = row.querySelector('.outcome .badge');
    if (badge) {{
      badge.textContent = OUTCOME_LABELS[outcome];
      badge.className = 'badge tone-' + OUTCOME_TONES[outcome];
    }}
    var when = row.querySelector('.when');
    // checked_at_display is the SERVER-formatted timestamp (the same
    // format_scan_timestamp the page was rendered with); checked_at is the
    // raw ISO value, kept only as the sort key. Painting the raw one into
    // the cell is exactly the bug this split exists to prevent.
    if (when && entry.checked_at_display) {{ when.textContent = entry.checked_at_display; }}
    if (entry.checked_at) {{ row.dataset.updated = entry.checked_at; }}
  }});
  filterScanRows();
}}

function pollScanResults() {{
  fetch('/status?brokers=1').then(function (r) {{ return r.json(); }}).then(function (s) {{
    var scan = (s && s.scan) || {{}};
    var progress = scan.progress || {{}};
    if (progress.brokers) {{ applyScanUpdate(progress.brokers); }}
    var line = document.getElementById('scanOutcomeLine');
    if (line && scan.outcome_line) {{ line.textContent = scan.outcome_line; }}
    var plines = document.getElementById('profilesCheckedLine');
    if (plines && scan.profiles_line) {{ plines.textContent = scan.profiles_line; }}
    var stopBtn = document.getElementById('stopScanBtn');
    if (stopBtn) {{
      stopBtn.disabled = !scan.running || scan.stop_requested;
      stopBtn.textContent = scan.stop_requested ? 'Stopping...' : 'Stop scan';
    }}
    if (!scan.running) {{ location.reload(); return; }}
    setTimeout(pollScanResults, 2000);
  }}).catch(function () {{ setTimeout(pollScanResults, 2000); }});
}}

(function () {{
  var card = document.getElementById('scanCard');
  if (card && card.dataset.scanRunning === '1') {{ pollScanResults(); }}
}})();
</script>
""".format(
        count=len(rows), rows_html=rows_html, picker=picker,
        scope_label=(" for {}".format(html.escape(selected["name"])) if selected
                     else " across every profile"),
        scan_line=html.escape(scan_line), scan_rows_html=scan_rows_html,
        profiles_line=html.escape(profiles_line),
        scan_running="1" if scan.get("running") else "0",
        stop_attrs="" if scan.get("running") and not scan.get("stop_requested") else " disabled",
        stop_label="Stopping..." if scan.get("stop_requested") else "Stop scan",
        outcome_options="".join(
            '<option value="{}">{}</option>'.format(
                style.escape_attr(name), html.escape(webui_data.SCAN_OUTCOME_LABELS[name]))
            for name in webui_data.SCAN_OUTCOME_ORDER
        ),
        outcome_labels_json=json.dumps(webui_data.SCAN_OUTCOME_LABELS),
        outcome_tones_json=json.dumps(webui_data.SCAN_OUTCOME_TONES),
    )

    return style.render_page("Brokers", "brokers", body, action_needed_count=action_needed)


def _run_scan_job(job_id: str, cfg: Config, jobs: dict, deps_factory) -> None:
    with _JOBS_LOCK:
        jobs[job_id] = {"status": "running", "started_at": _utcnow_iso()}
    deps = None
    try:
        deps = deps_factory(cfg)
        try:
            # run_all, not run_once: a manual "Run scan now" sweeps every
            # saved profile, exactly like the autopilot's scheduled pass.
            result = service_mod.run_all(cfg, deps)
        finally:
            deps.close()
            store = getattr(deps, "store", None)
            if store is not None and hasattr(store, "close"):
                try:
                    store.close()
                except Exception:  # pragma: no cover - best-effort teardown
                    pass
        # "stopped", not "done": a scan the person cancelled is a real,
        # partial pass. Reporting it as done would make the dashboard
        # claim a full sweep finished, which is precisely the kind of lie
        # the unknown/resolved split exists to prevent elsewhere.
        status = "stopped" if (isinstance(result, dict) and result.get("stopped")) else "done"
        with _JOBS_LOCK:
            jobs[job_id] = {"status": status, "result": result, "finished_at": _utcnow_iso()}
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


@app.post("/scan/stop")
def stop_scan(cfg: Config = Depends(get_config), jobs: dict = Depends(get_jobs)):
    """Ask the scan in flight to stop at the next broker boundary.

    Cooperative, and deliberately so (see
    ``progress.ScanProgress.request_stop``): the broker being checked
    right now finishes, everything already recorded STAYS recorded, and
    the sweep -- including its pending retry passes -- ends there. It
    cancels the whole multi-profile pass, not just the profile currently
    being checked: "stop the scan" means the scan, and stopping one
    person's share of a household sweep would be a surprising thing for
    that button to do.

    Never 404s or 409s when nothing is running: the flag is cleared when
    the next scan opens, so a stop that arrives a moment too late is a
    harmless no-op rather than an error the UI has to explain. Works the
    same for a manual /scan job and for the autopilot's own background
    cycle, because both drive the one process-wide ScanProgress.
    """
    progress = progress_mod.current()
    snapshot = progress.snapshot()
    with _JOBS_LOCK:
        running_jobs = any(j.get("status") == "running" for j in jobs.values())
    progress.request_stop()
    log.info("scan stop requested", extra={"was_active": bool(snapshot.get("active"))})
    return {"stop_requested": True,
            "was_running": bool(snapshot.get("active")) or running_jobs}


@app.get("/status")
def get_status(job_id: str | None = None, brokers: bool = False,
                cfg: Config = Depends(get_config), jobs: dict = Depends(get_jobs)):
    """Poll a /scan job by id, or (with no job_id) get the current
    broker_status snapshot -- the table state.StateStore.set_status/
    get_status writes every cycle but that, before this route, no UI ever
    read.

    ``brokers=1`` additionally includes the live per-broker outcome map
    (``scan.progress.brokers``) that the /brokers page's poll repaints its
    rows from. It is opt-in because the dashboard polls this route every
    1.5s and wants five integers, not 827 objects.
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
    snapshot = progress_mod.snapshot(include_brokers=brokers)
    if brokers:
        # The poll paints these straight into the page, so it gets the
        # SAME formatted timestamp the server-rendered row carries. The
        # raw ISO value stays alongside it as the sort key -- see
        # applyScanUpdate.
        for entry in snapshot.get("brokers", {}).values():
            entry["checked_at_display"] = format_scan_timestamp(entry.get("checked_at"))
    scan = webui_data.scan_status(_read_heartbeat(cfg), jobs_summary, cfg.interval_seconds,
                                   progress=snapshot)
    scan["line"] = _scan_line(scan)
    scan["outcome_line"] = webui_data.scan_outcome_line(
        webui_data.scan_outcome_counts_from_progress(snapshot),
        active=bool(snapshot.get("active")), stopped=bool(scan.get("stopped")),
    )
    scan["profiles_line"] = webui_data.profiles_checked_line(_identity_options(cfg), snapshot)
    return {"brokers": rows, "pending_removals": pending, "jobs": jobs_summary,
            "scan": scan}


@app.post("/brokers/{broker_id}/remove")
def remove_broker(broker_id: str, identity_key: str = Form(""),
                   cfg: Config = Depends(get_config),
                   bridge: EraserBridge = Depends(get_eraser_bridge)):
    """Manual, on-demand removal for one broker -- independent of the
    automated new-appearance trigger in service.submit_removals. A human (or
    the /freeze-style UI) can ask for a specific broker to be re-sent at any
    time, whether or not it was just detected as newly present.

    ``identity_key`` says WHOSE listing this is: the /brokers row that
    posts here knows which profile it belongs to, and with every profile
    scanned every cycle, sending Ann's removal and then writing the
    resulting status under Bob's identity_key would corrupt both people's
    history. An unrecognized or absent key falls back to the legacy
    single-identity profile, which is what a deployment with no saved
    profiles (or an old bookmarked form) has.
    """
    identity = None
    if identity_key:
        for p in profiles_mod.load_profiles(cfg.profiles_path):
            if profiles_mod.identity_key(p) == identity_key:
                identity = profile_mod.Identity(**profiles_mod.to_legacy_profile_dict(p))
                break
    if identity is None:
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
    """The ONE identity page: every saved profile, all of them scanned.

    This replaces the old split between a single-identity "Profile" page
    and a separate "Profiles" list -- two disconnected places to manage
    who you are -- and, since the active-profile concept was removed, the
    "Active profile" form that used to sit at the top of it. There is no
    privileged entry any more: the list IS the set of people Broker Guard
    watches, and every one of them is checked on every cycle.

    ``?edit=<id>`` opens a profile for editing in place on this same page
    (posting to ``/profiles/<id>``).
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

    rows_html = "".join(_profile_row_html(p) for p in all_profiles)
    if not all_profiles:
        rows_html = ('<tr><td colspan="4" class="muted" style="padding:16px;">'
                     'No profiles yet -- add one to start scanning for them.</td></tr>')

    edit_card = ""
    if edit:
        try:
            editing = profiles_mod.get_profile(cfg.profiles_path, edit)
        except (profiles_mod.ProfileNotFound, OSError, ValueError):
            raise HTTPException(status_code=404, detail="unknown profile")
        edit_card = _profile_edit_card_html(editing)

    body = """
<div class="page-head"><h1>Profiles</h1></div>
<p class="muted" style="max-width:680px;">Every identity Broker Guard manages, in one place.
<strong>All of them are scanned on every cycle</strong> -- there is no profile to "activate", and
results on the <a href="/brokers" style="text-decoration:underline;">Brokers</a> page are shown per
person. Each profile is also mirrored into eraser's config for <code>--profile &lt;id&gt;</code> on
the CLI.</p>
{edit_card}
<div class="grid-main">
  <div class="card">
    <h2>Profiles</h2>
    <table class="dtable">
      <tr style="text-align:left;font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:var(--faint);">
        <th style="padding-bottom:8px;">Name / id</th><th>Emails</th><th>Phones</th><th></th>
      </tr>
      {rows}
    </table>
  </div>
  <div class="card">
    <h2>Add a profile</h2>
    <p class="muted" style="font-size:13px;">A new profile joins the very next scan -- nothing else to switch on.</p>
    <form method="post" action="/profiles">
      <div class="field"><label>First name</label><input class="inp" type="text" name="first_name" required></div>
      <div class="field"><label>Middle name</label><input class="inp" type="text" name="middle_name"></div>
      <div class="field"><label>Last name</label><input class="inp" type="text" name="last_name" required></div>
      <div class="field"><label>Emails (one per line)</label><textarea class="inp" name="emails" rows="2"></textarea></div>
      <div class="field"><label>Phones (one per line)</label><textarea class="inp" name="phones" rows="2"></textarea></div>
      <div class="field"><label>Addresses (one per line)</label><textarea class="inp" name="addresses" rows="2"></textarea></div>
      <div class="field"><label>Eraser profile id (optional)</label><input class="inp" type="text" name="eraser_profile"></div>
      <button type="submit" class="btn">Add profile</button>
    </form>
  </div>
</div>
<div class="grid-main" style="margin-top:24px;">
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
""".format(rows=rows_html, edit_card=edit_card)
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

    # ...and mirror the same save onto the FIRST entry in the profiles
    # list (creating it if this is the first identity ever saved), so this
    # legacy single-identity route and the profiles list can never drift
    # apart. Deliberately AFTER the validated write above: a bad
    # submission is rejected before either store is touched.
    try:
        profiles_mod.upsert_primary_profile(cfg.profiles_path, data)
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


def _sync_primary_profile_to_legacy(cfg: Config) -> None:
    """Mirror the FIRST profile into cfg.profile_path -- the single
    profile.local.json kept for the entry points that predate
    multi-profile scanning (``service.run_once``, ``POST /identity``, a
    manual removal with no profile attached).

    It is compatibility, not privilege: the scan loops read every profile
    (``profiles.load_scan_identities``), so which profile lands in this
    file no longer decides who gets scanned.
    """
    try:
        profiles_mod.sync_primary_to_legacy(cfg.profiles_path, cfg.profile_path)
    except (OSError, ValueError) as exc:
        log.warning("primary profile sync to legacy file failed", extra={"error": str(exc)})


def _profile_row_html(p: "profiles_mod.NamedProfile") -> str:
    """One profile's row on the /identity page.

    Two actions, both styled with the app's own button tokens (``btn
    secondary small``) rather than the ad-hoc inline heights they used to
    carry -- which was why the "Edit" anchor in particular rendered as a
    bare link: ``.btn`` had no ``display``, so height/padding did nothing
    on an inline ``<a>``. See webui_style.PAGE_CSS.

    There is no "Make active": every profile is scanned every cycle.
    """
    return """
<tr>
  <td><strong>{name}</strong><br><span class="sub faint" style="font-size:12px;">{pid}</span></td>
  <td>{emails}</td>
  <td>{phones}</td>
  <td>
    <div class="rowactions">
      <a class="btn secondary small" href="/identity?edit={pid_attr}">Edit</a>
      <form method="post" action="/profiles/{pid_attr}/remove" onsubmit="return confirm('Remove this profile? It stops being scanned. Its eraser send history is kept and reappears if re-added with the same id.');">
        <button type="submit" class="btn secondary small danger">Remove</button>
      </form>
    </div>
  </td>
</tr>
""".format(
        name=html.escape(p.full_name), pid=html.escape(p.id),
        emails=html.escape(", ".join(p.emails) or "--"),
        phones=html.escape(", ".join(p.phones) or "--"),
        pid_attr=style.escape_attr(p.id),
    )


def _profile_edit_card_html(p: "profiles_mod.NamedProfile") -> str:
    """The in-place edit card for one profile (``/identity?edit=<id>``).

    Every profile is edited the same way now -- there is no separate
    "active profile" form at the top of the page to be the odd one out.
    """
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
      <div class="field"><label>Eraser profile id (optional)</label><input class="inp" type="text" name="eraser_profile" value="{eraser_profile}"></div>
      <div class="rowactions">
        <button type="submit" class="btn">Save</button>
        <a class="btn secondary" href="/identity">Cancel</a>
      </div>
    </form>
  </div>
</div>
""".format(
        name=html.escape(p.full_name or p.id), pid_attr=style.escape_attr(p.id),
        first_name=html.escape(p.first_name), middle_name=html.escape(p.middle_name),
        last_name=html.escape(p.last_name),
        emails=html.escape("\n".join(p.emails)), phones=html.escape("\n".join(p.phones)),
        addresses=html.escape("\n".join(p.addresses)),
        eraser_profile=html.escape(p.eraser_profile or ""),
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
    eraser_profile: str = Form(""),
    cfg: Config = Depends(get_config),
):
    """Add a profile. It is scanned from the next cycle on -- there is
    nothing to activate."""
    email_list = _dedupe_case_insensitive(_split_list(emails))
    invalid_emails = [e for e in email_list if not _EMAIL_RE.match(e)]
    if invalid_emails:
        raise HTTPException(status_code=400, detail="invalid email address(es): " + ", ".join(invalid_emails))
    phone_list = _dedupe_case_insensitive([normalize_phone(p) for p in _split_list(phones)])

    try:
        profiles_mod.add_profile(cfg.profiles_path, {
            "first_name": first_name, "middle_name": middle_name, "last_name": last_name,
            "emails": email_list, "phones": phone_list, "addresses": _split_list(addresses),
            "eraser_profile": eraser_profile.strip() or None,
        })
    except profiles_mod.ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Keep the legacy single-identity file populated for the entry points
    # that still read it (it is compatibility, not "the scanned profile").
    _sync_primary_profile_to_legacy(cfg)
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


# NOTE: there is no POST /profiles/<id>/activate any more. Every profile is
# scanned on every cycle (see profiles.py), so there is nothing to switch
# between -- the route was removed rather than kept as a no-op, because a
# button that silently does nothing is worse than one that is gone.


@app.post("/profiles/{profile_id}")
def profiles_edit_post(
    profile_id: str,
    first_name: str = Form(...),
    middle_name: str = Form(""),
    last_name: str = Form(...),
    emails: str = Form(""),
    phones: str = Form(""),
    addresses: str = Form(""),
    eraser_profile: str = Form(""),
    cfg: Config = Depends(get_config),
):
    email_list = _dedupe_case_insensitive(_split_list(emails))
    invalid_emails = [e for e in email_list if not _EMAIL_RE.match(e)]
    if invalid_emails:
        raise HTTPException(status_code=400, detail="invalid email address(es): " + ", ".join(invalid_emails))
    phone_list = _dedupe_case_insensitive([normalize_phone(p) for p in _split_list(phones)])

    try:
        profiles_mod.update_profile(cfg.profiles_path, profile_id, {
            "first_name": first_name, "middle_name": middle_name, "last_name": last_name,
            "emails": email_list, "phones": phone_list, "addresses": _split_list(addresses),
            "eraser_profile": eraser_profile.strip() or None,
        })
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")
    except profiles_mod.ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # An edit can change the first profile, which is what the legacy
    # single-identity file mirrors -- re-sync so that file does not keep
    # a pre-edit copy of it.
    _sync_primary_profile_to_legacy(cfg)
    _sync_eraser_profiles(cfg)
    return RedirectResponse(url="/identity", status_code=303)


@app.post("/profiles/{profile_id}/remove")
def profiles_remove(profile_id: str, cfg: Config = Depends(get_config)):
    """Delete from broker-guard's own store (and re-sync eraser's list to
    match) -- never touches eraser's history.db, so this profile's send
    history is preserved and reappears if a profile with this same id is
    ever re-added (see profiles.py's module docstring).

    A removed profile simply stops being scanned; nothing is promoted,
    because no profile was privileged in the first place. The legacy
    single-identity file is re-synced to whichever profile is now first,
    and removing the LAST profile leaves that file alone rather than
    truncating it -- see ``sync_primary_to_legacy``.
    """
    try:
        profiles_mod.remove_profile(cfg.profiles_path, profile_id)
    except profiles_mod.ProfileNotFound:
        raise HTTPException(status_code=404, detail="unknown profile")

    _sync_primary_profile_to_legacy(cfg)
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


# --- settings ---------------------------------------------------------------

_SETTINGS_SOURCE_TONE = {
    settings_mod.SOURCE_STORED: "success",
    settings_mod.SOURCE_ENV: "progress",
    settings_mod.SOURCE_DEFAULT: "neutral",
}

_SETTINGS_SOURCE_LABEL = {
    settings_mod.SOURCE_STORED: "saved here",
    settings_mod.SOURCE_ENV: "env var",
    settings_mod.SOURCE_DEFAULT: "built-in default",
}


def _setting_input_html(resolved: "settings_mod.ResolvedSetting") -> str:
    """The editable control for one setting.

    Three shapes, one per need rather than one generic text box:

    * **bool** -> a two-option ``<select>``, NOT a checkbox. An unchecked
      checkbox submits nothing at all, which is indistinguishable from "this
      field wasn't on the form" -- exactly the ambiguity that makes a
      save-everything form silently drop a "turn this off".
    * **secret** -> an empty password field. The stored value is NEVER
      rendered back into the page, matching ``freeze_pin_set``'s rule (no
      GET-the-plaintext route exists for the freeze PIN either); blank on
      submit means "keep what is stored", so a save of the rest of the form
      cannot wipe the key by omission.
    * everything else -> a text box pre-filled with the current EFFECTIVE
      value, so saving an untouched form is a no-op in value terms (it does
      move the source from env/default to stored, which is the point).
    """
    spec = resolved.spec
    name = style.escape_attr(spec.key)

    if spec.secret:
        placeholder = ("stored -- leave blank to keep it"
                       if resolved.value else "not set")
        return ('<input class="inp" type="password" name="{name}" value="" '
                'autocomplete="new-password" placeholder="{ph}">').format(
            name=name, ph=style.escape_attr(placeholder))

    if spec.kind == "bool":
        on = " selected" if resolved.value else ""
        off = "" if resolved.value else " selected"
        return ('<select class="inp" name="{name}">'
                '<option value="true"{on}>on</option>'
                '<option value="false"{off}>off</option></select>').format(
            name=name, on=on, off=off)

    value = "" if resolved.value is None else str(resolved.value)
    return '<input class="inp" type="text" name="{name}" value="{value}">'.format(
        name=name, value=style.escape_attr(value))


def _setting_row_html(resolved: "settings_mod.ResolvedSetting") -> str:
    spec = resolved.spec
    source_note = _SETTINGS_SOURCE_LABEL.get(resolved.source, resolved.source)
    if resolved.source == settings_mod.SOURCE_ENV:
        source_note = "{} ({})".format(source_note, spec.env)

    reset_html = ""
    if resolved.stored:
        # Only offered when there IS a stored override to drop -- a "revert to
        # env" control on a value that is already coming from the env would be
        # a no-op dressed up as an action.
        reset_html = (
            '<label class="muted" style="display:flex;gap:6px;align-items:center;'
            'font-size:12px;margin-top:6px;">'
            '<input type="checkbox" name="reset" value="{key}"> '
            'Forget this override and use {env} again</label>'
        ).format(key=style.escape_attr(spec.key), env=html.escape(spec.env))

    return """
<div class="field" style="border-top:1px solid var(--border);padding-top:14px;">
  <label>{label} {badge}</label>
  <div class="muted" style="font-size:12px;margin:-2px 0 4px;">
    Currently <strong>{current}</strong> &middot; from {source_note} &middot;
    env var <code>{env}</code>
  </div>
  {input_html}
  <div class="muted" style="font-size:12px;">{help}</div>
  {reset_html}
</div>
""".format(
        label=html.escape(spec.label),
        badge=style.badge(resolved.source, _SETTINGS_SOURCE_TONE.get(resolved.source, "neutral")),
        current=html.escape(resolved.display() or "(blank)"),
        source_note=html.escape(source_note),
        env=html.escape(spec.env),
        input_html=_setting_input_html(resolved),
        help=html.escape(spec.help),
        reset_html=reset_html,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_get(saved: str = "", cfg: Config = Depends(get_config)):
    """The runtime settings page.

    Every row shows the effective value AND which tier it came from (saved
    here / env var / built-in default). That source column is not decoration:
    the incidents this page exists to end ("the web UI turned itself off
    again", "Playwright detection turned itself off again") were all a value
    silently coming from a tier nobody was looking at.
    """
    store_path = settings_mod.store_path(cfg)
    resolved = settings_mod.resolve(store_path)
    rows_html = "".join(_setting_row_html(r) for r in resolved)
    saved_note = ""
    if saved:
        saved_note = ('<div class="encnote">Saved to <code>{path}</code>. '
                      'Detection settings apply on the next scan cycle; the scan '
                      'interval applies on the next loop tick.</div>').format(
            path=html.escape(store_path))

    body = """
<div class="page-head"><h1>Settings</h1></div>
<p class="muted" style="max-width:720px;">These are saved to
<code>{path}</code> on the data volume -- <strong>not</strong> to
<code>docker-compose.yml</code>. That is the whole point: the host redeploys with
<code>git reset --hard</code>, which silently reverts any hand-edit to a tracked file,
so a setting changed here survives a rebuild and a redeploy. A setting you have not
touched still comes from its <code>BG_*</code> environment variable exactly as before.</p>
<p class="muted" style="max-width:720px;font-size:13px;">Precedence, per setting:
<strong>saved here</strong> &rarr; <strong>environment variable</strong> &rarr;
<strong>built-in default</strong>. <code>BG_SERVE_WEB</code> is deliberately absent:
it decides whether this dashboard runs at all, so it cannot be turned off from inside
it -- that one stays an environment variable (put it in <code>.env</code>).</p>
{saved_note}
<div class="card" style="max-width:720px;">
  <form method="post" action="/settings">
    {rows}
    <button type="submit" class="btn" style="margin-top:16px;">Save settings</button>
  </form>
</div>
""".format(path=html.escape(store_path), saved_note=saved_note, rows=rows_html)
    return style.render_page("Settings", "settings", body)


@app.post("/settings")
def settings_post(
    playwright_enabled: str = Form(""),
    searxng_url: str = Form(""),
    searxng_min_interval_s: str = Form(""),
    searxng_jitter_s: str = Form(""),
    alert_webhook_url: str = Form(""),
    eraser_enabled: str = Form(""),
    eraser_dry_run: str = Form(""),
    optout_submit_enabled: str = Form(""),
    optout_submit_dry_run: str = Form(""),
    captcha_api_key: str = Form(""),
    interval_seconds: str = Form(""),
    reset: list[str] = Form([]),
    cfg: Config = Depends(get_config),
):
    """Persist the settings form into ``cfg.settings_path``.

    Fields are declared one by one rather than swept out of the raw form body
    so an unknown/renamed field is a 422 from FastAPI rather than a silently
    ignored edit, and so this signature is the readable list of what the page
    can change.

    Validation happens in ``settings.update_settings`` BEFORE anything is
    written, so a bad value (a negative pacing interval, a non-http SearXNG
    URL, a scan interval under the 60s floor) rejects the whole submission
    with a 400 and leaves the stored settings exactly as they were -- the same
    "a bad submission never overwrites good data" rule ``identity_post``
    follows.
    """
    submitted = {
        "playwright_enabled": playwright_enabled,
        "searxng_url": searxng_url,
        "searxng_min_interval_s": searxng_min_interval_s,
        "searxng_jitter_s": searxng_jitter_s,
        "alert_webhook_url": alert_webhook_url,
        "eraser_enabled": eraser_enabled,
        "eraser_dry_run": eraser_dry_run,
        "optout_submit_enabled": optout_submit_enabled,
        "optout_submit_dry_run": optout_submit_dry_run,
        "captcha_api_key": captcha_api_key,
        "interval_seconds": interval_seconds,
    }

    to_reset = {key for key in reset if key in settings_mod.SPEC_BY_KEY}
    unknown_reset = [key for key in reset if key not in settings_mod.SPEC_BY_KEY]
    if unknown_reset:
        raise HTTPException(status_code=400,
                            detail="unknown setting(s): " + ", ".join(sorted(unknown_reset)))

    changes = {}
    for key, raw in submitted.items():
        spec = settings_mod.SPEC_BY_KEY[key]
        if key in to_reset:
            # None == delete the stored override == fall back to the env var.
            changes[key] = None
            continue
        if spec.secret and not raw.strip():
            # Blank secret means "leave the stored one alone", NOT "clear it".
            # Clearing a secret is the reset checkbox, which is explicit.
            continue
        changes[key] = raw

    try:
        settings_mod.update_settings(settings_mod.store_path(cfg), changes)
    except settings_mod.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log.info("runtime settings saved", extra={
        # Names only. The values include a third-party API credential, and
        # config.SECRET_ENV_KEYS exists precisely so these never reach a log.
        "changed": sorted(changes),
        "reset": sorted(to_reset),
    })
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# --- /review : the opt-out submission audit trail -----------------------------
#
# The one place the dashboard answers "what did this tool send in my name, to
# whom, and what did the page look like when it did". Every record here was
# written by broker_guard.optout_submit -- the only code path in this project
# with real third-party side effects (see its module docstring for the four
# interlocks that gate it).

_OUTCOME_TONE = {
    review_mod.OUTCOME_SUBMITTED: "success",
    review_mod.OUTCOME_DRY_RUN: "progress",
    review_mod.OUTCOME_NEEDS_MANUAL: "action",
    review_mod.OUTCOME_FAILED: "escalated",
}

_OUTCOME_LABEL = {
    review_mod.OUTCOME_SUBMITTED: "submitted",
    review_mod.OUTCOME_DRY_RUN: "dry run",
    review_mod.OUTCOME_NEEDS_MANUAL: "needs you",
    review_mod.OUTCOME_FAILED: "failed",
}


def _attempt_identity(cfg: Config, identity_key: str):
    """The Identity an attempt should run as.

    Same resolution order as ``remove_broker``: the named profile when the
    posting row knows one, else the legacy single-identity file. With every
    profile scanned every cycle, submitting Ann's opt-out under Bob's
    identity_key would corrupt both people's history.
    """
    if identity_key:
        for p in profiles_mod.load_profiles(cfg.profiles_path):
            if profiles_mod.identity_key(p) == identity_key:
                return profile_mod.Identity(**profiles_mod.to_legacy_profile_dict(p))
    try:
        return profile_mod.load_profile(cfg.profile_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot load profile: {exc}")


def _attempt_row_html(record: dict) -> str:
    outcome = record.get("outcome") or ""
    fields = record.get("fields") or {}
    field_rows = "".join(
        "<div class='kv'><span>{}</span><strong>{}</strong></div>".format(
            html.escape(str(label)), html.escape(str(value)))
        for label, value in fields.items()
    ) or "<div class='muted'>nothing was filled in</div>"

    shot = ""
    if record.get("screenshot"):
        shot = (
            "<a class='shot' href='/review/{rid}/screenshot' target='_blank'>"
            "view the page as it was at the time of the attempt</a>"
        ).format(rid=html.escape(str(record.get("id", ""))))

    reason = record.get("reason")
    reason_html = (
        "<p class='muted'>{}</p>".format(html.escape(str(reason))) if reason else ""
    )

    return """
<div class="card attempt">
  <div class="attempt-head">
    <div>
      <strong>{broker}</strong>
      <div class="muted">{when}{dry}</div>
    </div>
    {badge}
  </div>
  {reason}
  <div class="kvs">{fields}</div>
  {shot}
</div>
""".format(
        broker=html.escape(str(record.get("broker_name") or record.get("broker_id") or "?")),
        when=html.escape(format_scan_timestamp(record.get("started_at")) or ""),
        dry=" · dry run" if record.get("dry_run") else "",
        badge=style.badge(_OUTCOME_LABEL.get(outcome, outcome),
                          _OUTCOME_TONE.get(outcome, "neutral")),
        reason=reason_html,
        fields=field_rows,
        shot=shot,
    )


@app.get("/review", response_class=HTMLResponse)
def review_page(cfg: Config = Depends(get_config)):
    """Every automated opt-out submission attempt, newest first."""
    directory = review_mod.review_dir(cfg)
    records = review_mod.load_attempts(directory)
    counts = review_mod.attempt_counts(records)

    live_cfg = cfg
    enabled = bool(getattr(live_cfg, "optout_submit_enabled", False))
    dry_default = bool(getattr(live_cfg, "optout_submit_dry_run", True))

    if enabled and dry_default:
        state_note = ("Automated submission is <strong>on, in DRY RUN</strong>: forms are "
                      "filled and photographed, but Submit is never pressed.")
    elif enabled:
        state_note = ("Automated submission is <strong>on and LIVE</strong>: a run will "
                      "really send the request in your name.")
    else:
        state_note = ("Automated submission is <strong>off</strong>. Turn it on in "
                      "<a href='/settings'>Settings</a> to enable the buttons below.")

    buttons = []
    for broker_id in optout_forms.supported_broker_ids():
        recipe = optout_forms.recipe_for(broker_id)
        disabled = "" if enabled else " disabled"
        buttons.append("""
<div class="card">
  <strong>{name}</strong>
  <div class="muted">{url}</div>
  <div class="row gap">
    <form method="post" action="/review/run">
      <input type="hidden" name="broker_id" value="{bid}">
      <input type="hidden" name="mode" value="dry">
      <button class="btn secondary" type="submit"{disabled}>Dry run (fill only)</button>
    </form>
    <form method="post" action="/review/run"
          onsubmit="return confirm('This really submits an opt-out request to {name_js} using your real name, email and state. Continue?');">
      <input type="hidden" name="broker_id" value="{bid}">
      <input type="hidden" name="mode" value="live">
      <button class="btn danger" type="submit"{disabled}>Submit for real</button>
    </form>
  </div>
</div>
""".format(
            name=html.escape(recipe.broker_name),
            name_js=html.escape(recipe.broker_name).replace("'", "\\'"),
            url=html.escape(recipe.url),
            bid=html.escape(broker_id),
            disabled=disabled,
        ))

    rows = "".join(_attempt_row_html(r) for r in records) or (
        "<div class='card muted'>No opt-out submission has been attempted yet.</div>")

    body = """
<div class="page-head"><h1>Opt-out review</h1></div>
<p class="muted">{state_note}</p>
<div class="chips">{chips}</div>
<h2>Run an opt-out</h2>
{buttons}
<h2>Attempts</h2>
<p class="muted">Records and screenshots are kept in <code>{dir}</code>.</p>
{rows}
""".format(
        state_note=state_note,
        chips="".join([
            style.stat_chip(counts[review_mod.OUTCOME_SUBMITTED], "submitted", "success"),
            style.stat_chip(counts[review_mod.OUTCOME_DRY_RUN], "dry runs", "progress"),
            style.stat_chip(counts[review_mod.OUTCOME_NEEDS_MANUAL], "need you", "action"),
            style.stat_chip(counts[review_mod.OUTCOME_FAILED], "failed", "escalated"),
        ]),
        buttons="".join(buttons),
        dir=html.escape(directory),
        rows=rows,
    )
    return style.render_page("Opt-out review", "review", body)


@app.get("/review/{record_id}/screenshot")
def review_screenshot(record_id: str, cfg: Config = Depends(get_config)):
    """Serve one attempt's screenshot.

    The filename is looked up from the RECORD rather than built from
    *record_id*, so a crafted id cannot walk out of the review folder.
    """
    directory = review_mod.review_dir(cfg)
    record = review_mod.get_attempt(directory, record_id)
    if record is None or not record.get("screenshot"):
        raise HTTPException(status_code=404, detail="no screenshot for that attempt")

    name = os.path.basename(str(record["screenshot"]))
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="screenshot file is missing")
    with open(path, "rb") as fh:
        return Response(content=fh.read(), media_type="image/png")


@app.post("/review/run")
def review_run(broker_id: str = Form(...), mode: str = Form("dry"),
               identity_key: str = Form(""), cfg: Config = Depends(get_config)):
    """Run one opt-out attempt now, on demand.

    Synchronous, like ``remove_broker``: this is an occasional human-initiated
    action, and running it inline keeps it inside the one-process/one-writer
    model rather than spawning a second thread with its own DB handle.

    ``mode`` is ``dry`` (fill + screenshot, never submit) or ``live`` (respect
    the configured dry-run setting, i.e. actually submit when it is off).
    ``dry`` is the default and any unrecognized value falls back to it -- the
    safe direction.
    """
    try:
        optout_forms.recipe_for(broker_id)
    except optout_forms.RecipeNotFound:
        raise HTTPException(status_code=404,
                            detail="no verified opt-out form recipe for that broker")

    identity = _attempt_identity(cfg, identity_key)
    dry_run = True if mode != "live" else None   # None == use the configured setting

    try:
        record = optout_submit.run_attempt(broker_id, identity, cfg, dry_run=dry_run)
    except optout_submit.SubmissionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Surface an attempt that needs a human the SAME way every other
    # manual-action item already surfaces: as a needs_review broker_status,
    # which webui._action_needed_count already counts into the nav badge.
    status = review_mod.status_for_outcome(record.get("outcome"))
    if status:
        conn = state_mod.init_db(cfg.state_path)
        try:
            state_mod.StateStore(conn).set_status(
                identity.identity_key, broker_id, status, _utcnow_iso())
        finally:
            conn.close()

    return RedirectResponse(url="/review", status_code=303)
