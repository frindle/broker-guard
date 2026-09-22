"""Process wiring for ``BG_SERVE_WEB=true`` / ``--serve-web``: runs the
FastAPI dashboard (``webui.py``) AND the autopilot scan/confirmation loop in
ONE process, in ONE container, on ONE port -- rather than as two separate
services in docker-compose.

One process, not two -- why
-----------------------------
The autopilot loop and the web routes both read/write the same profile file
and the same state db. A second container running the loop alongside a web
container would mean either duplicating all of that wiring, or two
independent processes racing to write the SAME sqlite state db and decide
independently whether to auto-send the SAME broker's removal. SQLite's WAL
mode tolerates concurrent readers with a single writer -- it does not make
two independent writers' business decisions safe. Running the loop as a
background thread inside the same interpreter that serves the routes keeps
exactly one writer and lets the web UI's ``/scan`` route and the autopilot's
own scheduled scan share the identical ``AutopilotDependencies``/state-db
wiring rather than two divergent copies of it.

fastapi/uvicorn are imported LAZILY inside ``run_web_server``, not at
``broker_guard.service`` import time -- so the default headless deployment
(``BG_SERVE_WEB`` unset/false) never needs either package installed at all.
This matches the existing lazy-import convention for optional dependencies
(``browser.py``'s Playwright import, ``searx_client.py``'s ``requests``
import, ``service.build_dependencies``'s eraser/browser/searx imports).
"""
import logging
import threading

from broker_guard.config import Config

log = logging.getLogger("broker_guard.webapp")


def run_web_server(cfg: Config) -> int:
    """Start the autopilot loop as a background thread, then serve the
    FastAPI app in the foreground until interrupted. Returns an exit code
    (0 on a clean shutdown) so ``service.main`` can just ``return`` it.
    """
    import uvicorn

    from broker_guard import autopilot
    from broker_guard.webui import app

    deps = autopilot.build_dependencies(cfg)
    stop = threading.Event()
    loop_thread = threading.Thread(
        target=autopilot.run_forever,
        args=(cfg, deps, autopilot.Intervals(scan_seconds=cfg.interval_seconds), stop),
        kwargs={"has_id_documents": autopilot.has_id_documents_on_file(cfg)},
        name="autopilot-loop",
        daemon=True,
    )
    loop_thread.start()
    log.info("autopilot loop started as a background thread",
              extra={"scan_interval_seconds": cfg.interval_seconds})

    try:
        uvicorn.run(app, host="0.0.0.0", port=cfg.web_port, log_level=cfg.log_level.lower())
    finally:
        stop.set()
        loop_thread.join(timeout=10)
        deps.close()
        log.info("web server + autopilot loop stopped")
    return 0
