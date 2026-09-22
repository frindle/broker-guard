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
"""
import base64
import html
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from broker_guard import eraser as eraser_mod
from broker_guard import exposure as exposure_mod
from broker_guard import freeze as freeze_mod
from broker_guard import profile as profile_mod
from broker_guard import service as service_mod
from broker_guard import state as state_mod
from broker_guard import webui_data
from broker_guard.config import Config, load_config
from broker_guard.crypto import encrypt_field
from broker_guard.eraser_bridge import EraserBridge

log = logging.getLogger("broker_guard.webui")

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


def get_exposure_client() -> exposure_mod.XposedOrNotClient:
    return exposure_mod.XposedOrNotClient()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- / : health + escalations -----------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(cfg: Config = Depends(get_config)):
    # Escalation countdowns: no persisted escalation-records store yet, so an
    # empty list is the honest input ("no escalations recorded yet").
    try:
        countdowns = webui_data.escalation_countdowns([], _utcnow_iso())
    except Exception:
        countdowns = []

    health_log_path = os.path.join(cfg.log_dir, "health.jsonl")
    lines = []
    if os.path.exists(health_log_path):
        with open(health_log_path, "r", encoding="utf-8") as f:
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
        '<p><a href="/brokers">brokers</a> | <a href="/identity">identity</a> | '
        '<a href="/exposure">exposure</a> | <a href="/freeze">credit freeze</a></p>\n'
        "<table border=\"1\">\n"
        "<tr><th>broker_id</th><th>stage</th><th>deadline_iso</th>"
        "<th>seconds_remaining</th><th>overdue</th></tr>\n"
        + rows_html + "\n"
        "</table>\n"
        "<p>health: total={total} ok={ok} failed={failed}</p>\n"
        "</body></html>"
    ).format(total=summary.get("total", 0), ok=summary.get("ok", 0), failed=summary.get("failed", 0))


# --- /brokers + /status : presence and removal status -----------------------

def _broker_rows_html(rows: list[dict]) -> str:
    out = []
    for row in rows:
        out.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            '<td><form method="post" action="/brokers/{bid_attr}/remove">'
            '<button type="submit">send removal</button></form></td></tr>'.format(
                html.escape(str(row["identity_key"])),
                html.escape(str(row["broker_id"])),
                html.escape(str(row["first_seen"])),
                html.escape(str(row["last_seen"])),
                html.escape(str(row["removal_status"] or "not submitted")),
                html.escape(str(row["status_updated_at"] or "")),
                bid_attr=html.escape(str(row["broker_id"]), quote=True),
            )
        )
    return "\n".join(out)


@app.get("/brokers", response_class=HTMLResponse)
def brokers_page(cfg: Config = Depends(get_config)):
    conn = state_mod.init_db(cfg.state_path)
    try:
        rows = webui_data.query_broker_status(conn)
    finally:
        conn.close()

    lines = [
        "<!DOCTYPE html>",
        "<html><head><title>Broker Guard -- Brokers</title></head><body>",
        "<h1>Brokers</h1>",
        '<table border="1">',
        "<tr><th>identity_key</th><th>broker_id</th><th>first_seen</th><th>last_seen</th>"
        "<th>removal_status</th><th>status_updated_at</th><th>action</th></tr>",
        _broker_rows_html(rows),
        "</table>",
        "</body></html>",
    ]
    return "\n".join(lines)


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
    return {"brokers": rows, "pending_removals": pending, "jobs": jobs_summary}


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


@app.get("/identity", response_class=HTMLResponse)
def identity_get(cfg: Config = Depends(get_config)):
    try:
        loaded = profile_mod.load_profile(cfg.profile_path)
    except Exception:
        loaded = None

    first_name = loaded.first_name if loaded is not None else ""
    middle_name = loaded.middle_name if loaded is not None else ""
    last_name = loaded.last_name if loaded is not None else ""
    emails = "\n".join(loaded.emails) if loaded is not None else ""
    phones = "\n".join(loaded.phones) if loaded is not None else ""
    addresses = "\n".join(loaded.addresses) if loaded is not None else ""
    eraser_profile = (loaded.eraser_profile or "") if loaded is not None else ""

    lines = [
        "<!DOCTYPE html>",
        "<html><head><title>Broker Guard -- Identity</title></head><body>",
        "<h1>Identity</h1>",
        '<form method="post" action="/identity">',
        '<label>First name <input type="text" name="first_name" value="%s"></label>' % html.escape(first_name),
        '<label>Middle name <input type="text" name="middle_name" value="%s"></label>' % html.escape(middle_name),
        '<label>Last name <input type="text" name="last_name" value="%s"></label>' % html.escape(last_name),
        '<label>Emails <textarea name="emails">%s</textarea></label>' % html.escape(emails),
        '<label>Phones <textarea name="phones">%s</textarea></label>' % html.escape(phones),
        '<label>Addresses <textarea name="addresses">%s</textarea></label>' % html.escape(addresses),
        '<label>Eraser profile id <input type="text" name="eraser_profile" value="%s"></label>' % html.escape(eraser_profile),
        '<button type="submit">Save</button>',
        "</form>",
        '<h2>Government ID (encrypted at rest)</h2>',
        '<form method="post" action="/identity/id-document" enctype="multipart/form-data">',
        '<label>Side <select name="side"><option value="front">front</option>'
        '<option value="back">back</option></select></label>',
        '<input type="file" name="file">',
        '<button type="submit">Upload</button>',
        "</form>",
        "</body></html>",
    ]
    return "\n".join(lines)


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
    """
    data = {
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "emails": _split_list(emails),
        "phones": _split_list(phones),
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

    lines = [
        "<!DOCTYPE html>",
        "<html><head><title>Broker Guard -- Exposure</title></head><body>",
        "<h1>Breach Exposure</h1>",
    ]
    for email, breaches in results.items():
        lines.append(f"<h2>{html.escape(email)}</h2>")
        if breaches:
            lines.append("<ul>" + "".join(f"<li>{html.escape(name)}</li>" for name in breaches) + "</ul>")
        else:
            lines.append("<p>no known breaches</p>")
    if not results:
        lines.append("<p>no emails on the profile to check</p>")
    lines.append(f"<p><em>{html.escape(exposure_mod.XPOSEDORNOT_ATTRIBUTION)}</em></p>")
    lines.append("</body></html>")
    return "\n".join(lines)


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
            '<td><a href="{url}" rel="noopener noreferrer">{url}</a></td></tr>'.format(
                html.escape(bureau["display_name"]),
                html.escape(state.status),
                "yes" if state.has_pin() else "no",
                html.escape(confidence),
                url=html.escape(bureau["freeze_url"], quote=True),
            )
        )

    lines = [
        "<!DOCTYPE html>",
        "<html><head><title>Broker Guard -- Credit Freeze</title></head><body>",
        "<h1>Credit Freeze Tracker</h1>",
        '<table border="1">',
        "<tr><th>bureau</th><th>status</th><th>has_pin</th><th>confidence</th><th>freeze_url</th></tr>",
        "\n".join(rows),
        "</table>",
        "</body></html>",
    ]
    return "\n".join(lines)


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
