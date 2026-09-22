"""The runnable service: wires the pure modules to real implementations.

Before this module the pipeline was a bag of pure functions with no way to run
end to end. ``run_once`` performs one full cycle; ``main`` loops it on
``BG_INTERVAL_SECONDS`` until SIGTERM/SIGINT.

Every external dependency is injected through ``Dependencies``, so the whole
loop can be exercised with fakes -- no network, no browser, no subprocess.
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from broker_guard import broker_normalize, brokers as brokers_mod
from broker_guard import health, profile as profile_mod, scheduler, serpwatch
from broker_guard import playwright_checks
from broker_guard.config import Config, ConfigError, load_config, validate_runtime_paths
from broker_guard.eraser import needs_reverify, status_after_removal
from broker_guard.logging_setup import setup_logging
from broker_guard.orchestrator import run_cycle
from broker_guard.state import StateStore

log = logging.getLogger("broker_guard.service")


class PresenceUnknown(RuntimeError):
    """Presence could not be determined for a broker this cycle.

    Distinct from "absent": run_cycle treats it as an error, so the broker is
    left in whatever state it already had rather than being reported resolved.
    """


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Dependencies:
    """Everything the cycle touches that is not a pure function."""

    # Annotations are required here: without them these would be plain class
    # attributes rather than dataclass fields, and the constructor would take
    # no arguments at all.
    searx_search: object = None   # callable(query) -> list[raw result dict]
    page_action: object = None    # callable(check) -> {'found': bool} | {'error': str}
    removal: object = None        # callable(broker_id, profile_dict) -> result dict
    alert_sink: object = None     # callable(cycle_payload) -> anything
    store: object = None          # StateStore-like
    now: object = utcnow_iso      # callable() -> iso str
    closers: list = field(default_factory=list)

    def close(self):
        for closer in self.closers:
            try:
                closer()
            except Exception as exc:  # pragma: no cover
                log.warning("closer failed", extra={"error": str(exc)})


def build_presence_checker(identity, brokers, deps, cfg):
    """Fuse SERP hits and browser checks into ``presence_checker(broker, key)``.

    This is the join the per-slice modules never had: ``run_serpwatch``
    returns a list of PresenceResult, ``run_playwright_checks`` returns a dict
    keyed by broker_id, and ``run_cycle`` wants a per-broker predicate. A
    broker counts as present if EITHER source says so; a browser check that
    errored is not evidence of absence, so it defers to the SERP result.
    """
    name_variants = profile_mod.name_variants(identity)

    serp_ids = set()
    if deps.searx_search is not None:
        hits = serpwatch.run_serpwatch(
            brokers, identity.phones, identity.emails, name_variants,
            identity.addresses, deps.searx_search,
        )
        serp_ids = {hit.broker_id for hit in hits}
        log.info("serpwatch complete", extra={"brokers": len(brokers), "hit_brokers": len(serp_ids)})

    browser_results = {}
    if deps.page_action is not None:
        terms = name_variants + identity.phones + identity.emails
        checks = playwright_checks.build_site_checks(brokers, terms)
        browser_results = playwright_checks.run_playwright_checks(checks, deps.page_action)
        errored = sum(1 for r in browser_results.values() if not r["checked"])
        log.info("browser checks complete",
                 extra={"checks": len(checks), "errored": errored})

    def presence_checker(broker, identity_key):
        broker_id = broker["id"]
        if broker_id in serp_ids:
            return True
        result = browser_results.get(broker_id)
        if result is not None:
            if result["checked"]:
                return result["present"]
            # The browser check ERRORED. That is "unknown", not "absent".
            # Returning False here would let a site outage be recorded as a
            # successful removal, which is the worst failure this tool can
            # have -- the person stops chasing a listing that is still live.
            # Raising makes run_cycle bucket the broker under `errors`, which
            # excludes it from `resolved`.
            raise PresenceUnknown(
                "{}: check failed: {}".format(broker_id, result["error"])
            )
        return False

    presence_checker.serp_ids = serp_ids
    presence_checker.browser_results = browser_results
    return presence_checker


def submit_removals(identity, broker_ids, deps, now_iso) -> list[dict]:
    """Hand each newly-appeared broker to the removal engine."""
    if deps.removal is None or not broker_ids:
        return []
    eraser_profile = identity.to_eraser_profile()
    results = []
    for broker_id in broker_ids:
        try:
            result = deps.removal(broker_id, eraser_profile)
        except Exception as exc:
            result = {"success": False, "broker_id": broker_id,
                      "detail": "{}: {}".format(type(exc).__name__, exc)}
        results.append(result)
        if deps.store is not None and hasattr(deps.store, "set_status"):
            prior = deps.store.get_status(identity.identity_key, broker_id) or "pending"
            deps.store.set_status(
                identity.identity_key, broker_id,
                status_after_removal(prior, result), now_iso,
            )
    return results


def run_once(cfg: Config, deps: Dependencies) -> dict:
    """One full cycle: load inputs, detect, diff, alert, submit removals."""
    started = deps.now()
    identity = profile_mod.load_profile(cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    identity_key = identity.identity_key
    log.info("cycle start", extra={"brokers": len(broker_list), "identity_key": identity_key})

    presence_checker = build_presence_checker(identity, broker_list, deps, cfg)
    result = run_cycle(
        identity_key, broker_list, presence_checker, deps.store,
        deps.alert_sink or (lambda payload: None), started,
    )
    result["removals"] = submit_removals(identity, result["new_appearances"], deps, started)
    result["reverify"] = [
        bid for bid in result["current"]
        if needs_reverify(
            (deps.store.get_status(identity_key, bid) if hasattr(deps.store, "get_status") else None)
            or "", True,
        )
    ]
    log.info("cycle complete", extra={
        "present": len(result["current"]),
        "new": len(result["new_appearances"]),
        "resolved": len(result["resolved"]),
        "errors": len(result["errors"]),
        "removals": len(result["removals"]),
    })
    return result


def write_heartbeat(cfg: Config, payload: dict) -> None:
    """Write the dead-man's-switch file that ``health.heartbeat_stale`` reads."""
    path = os.path.join(cfg.log_dir, "heartbeat.json")
    try:
        os.makedirs(cfg.log_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, path)  # atomic: a reader never sees a half-written file
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("heartbeat write failed", extra={"path": path, "error": str(exc)})


def build_dependencies(cfg: Config) -> Dependencies:
    """Construct the REAL implementations from config."""
    from broker_guard.browser import make_page_action
    from broker_guard.eraser_bridge import EraserBridge, noop_bridge
    from broker_guard.searx_client import PermanentSearxError, SearxClient
    from broker_guard.sinks import build_alert_sink

    closers = []

    searx_search = None
    if cfg.searxng_url:
        try:
            searx_search = SearxClient(
                cfg.searxng_url, timeout_s=cfg.searxng_timeout_s, auth=cfg.searxng_auth,
                engines=cfg.searxng_engines, attempts=max(1, cfg.max_retries),
                base_delay=cfg.retry_base_delay_s,
            )
        except PermanentSearxError as exc:
            log.error("searxng disabled", extra={"error": str(exc)})
    else:
        log.warning("BG_SEARXNG_URL not set; SERP detection disabled")

    page_action = None
    if cfg.playwright_enabled:
        page_action, closer = make_page_action(cfg.playwright_timeout_ms, cfg.playwright_headless)
        closers.append(closer)

    removal = None
    if cfg.eraser_enabled:
        bridge = EraserBridge(cfg.eraser_bin, cfg.eraser_timeout_s, cfg.eraser_dry_run)
        if bridge.available():
            removal = bridge.submit_removal
            log.info("eraser enabled", extra={"dry_run": cfg.eraser_dry_run})
        else:
            log.error("eraser binary not found; removals disabled",
                      extra={"eraser_bin": cfg.eraser_bin})
            removal = noop_bridge

    return Dependencies(
        searx_search=searx_search,
        page_action=page_action,
        removal=removal,
        alert_sink=build_alert_sink(cfg),
        store=StateStore.open(cfg.state_path),
        closers=closers,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="broker-guard", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--check-config", action="store_true",
                        help="validate config and inputs, then exit")
    parser.add_argument("--serve-web", action="store_true",
                        help="serve the web dashboard (FastAPI/uvicorn) instead of the "
                             "headless loop; the autopilot scan/confirmation loop still "
                             "runs, as a background thread in the same process "
                             "(same effect as BG_SERVE_WEB=true)")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(cfg.log_level, cfg.log_dir, cfg.log_pii)
    logger.info("broker-guard starting", extra={"config": cfg.redacted()})

    # First-boot convenience: fill in a missing broker dataset from the
    # bundled source BEFORE path validation runs, so a fresh deploy with no
    # brokers.json yet doesn't fail validate_runtime_paths below. Never
    # touches an existing file -- see broker_normalize.ensure_brokers_file.
    try:
        broker_normalize.ensure_brokers_file(cfg.brokers_path)
    except (OSError, ValueError, KeyError) as exc:
        logger.error("broker dataset auto-generation failed", extra={
            "error": "{}: {}".format(type(exc).__name__, exc),
        })
        # Not fatal here -- validate_runtime_paths below will report the
        # still-missing brokers.json as the ordinary config problem it is.

    problems = validate_runtime_paths(cfg)
    if problems:
        for problem in problems:
            logger.error("config problem", extra={"problem": problem})
        return 2
    if args.check_config:
        logger.info("config ok")
        return 0

    if args.serve_web or cfg.serve_web:
        from broker_guard.webapp import run_web_server

        logger.info("serving web dashboard", extra={"port": cfg.web_port})
        return run_web_server(cfg)

    stop = threading.Event()

    def handle_signal(signum, _frame):
        logger.info("shutdown signal", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    deps = build_dependencies(cfg)
    status = {}
    exit_code = 0
    try:
        while not stop.is_set():
            started = utcnow_iso()
            try:
                result = run_once(cfg, deps)
                status = health.update_run_status(status, True, started)
                write_heartbeat(cfg, {"last_run": started, "ok": True,
                                      "present": len(result["current"]),
                                      "new": len(result["new_appearances"])})
            except Exception as exc:
                # One bad cycle must never kill a long-running monitor.
                logger.exception("cycle failed", extra={"error": "{}: {}".format(
                    type(exc).__name__, exc)})
                status = health.update_run_status(status, False, started,
                                                  base_backoff_s=60,
                                                  max_backoff_s=cfg.interval_seconds)
                write_heartbeat(cfg, {"last_run": started, "ok": False,
                                      "consecutive_failures": status["consecutive_failures"]})

            if args.once or cfg.run_once:
                break

            delay = cfg.interval_seconds
            if status.get("status") == "failing" and status.get("next_retry"):
                # Back off after a failure instead of hammering on the interval.
                delay = min(delay, max(60, 60 * 2 ** (status["consecutive_failures"] - 1)))
            logger.info("sleeping", extra={
                "seconds": delay,
                "next_run": scheduler.next_run_time(utcnow_iso(), int(delay)),
            })
            stop.wait(delay)
    finally:
        deps.close()
        store = getattr(deps, "store", None)
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:
                pass
        logger.info("broker-guard stopped")
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
