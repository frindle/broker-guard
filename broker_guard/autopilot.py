"""The autopilot: a continuously-running loop that minimizes human input by
deciding, per newly-appeared broker, whether to auto-send a removal or queue
it for a human -- and by periodically checking for confirmation and
reappearance so a resolved broker gets re-attacked automatically if it comes
back.

This is deliberately a SEPARATE module from ``broker_guard.scheduler``
(which only builds launchd/cron descriptors) and does not change
``service.run_once``/``submit_removals`` -- those still send a removal for
every new appearance unconditionally, with no notion of ``verification``
kind at all. ``run_scan_cycle`` below is where that kind-awareness lives.

Decision table (``decide_action``), by ``brokers.verification_kind(broker)``
------------------------------------------------------------------------------
====================  =========================================  ================================
kind                  action                                      queue_status (if queued)
====================  =========================================  ================================
automatable           auto_send via eraser ``send``               --
captcha               auto_send via eraser ``send``                --
                      (the broker's OWN form may still reject a
                      genuinely-hard captcha; that just becomes a
                      failed/pending status, same low-cost-bounce
                      reasoning ``broker_normalize.classify_kind``
                      already uses -- see that module's docstring)
photo_id              queue                                       needs_document
kba                   queue                                       needs_review
anything else         queue (fail SAFE toward a human, never a     needs_review
(unknown kind)        silent auto-send and never a silent drop)
====================  =========================================  ================================

Why ``photo_id`` always queues, even with an ID document on file
------------------------------------------------------------------
The vendored eraser CLI's ``fill`` command (browser-automates opt-out forms)
takes no per-broker argument and no documented way to attach a specific
file -- see ``eraser.build_eraser_fill_cmd``'s docstring, confirmed against
``vendor/eraser/docs/commands.md``. There is currently nowhere to hand a
stored ID image to eraser automatically for one specific broker, so
``decide_action`` queues every ``photo_id`` broker as ``needs_document``
regardless of whether documents are on file. ``has_id_documents`` is kept as
an explicit parameter (rather than deleted) so this stays visibly a policy
choice tied to a documented CLI limitation, not an oversight -- and so it is
the one line to change if eraser ever grows a per-broker attach flag.

Confirmation, honestly
------------------------
``run_confirmation_pass`` calls eraser's ``monitor`` (IMAP inbox scan) and
``status`` (human-readable text) and returns both, but does NOT attempt to
auto-parse either into a per-broker ``broker_status`` transition: neither
command documents a stable structured (JSON) output today, and guessing a
parser for free text would risk mis-mapping a reply rather than genuinely
confirming anything. Both results are handed to the caller (e.g. the webui
``/status`` route) for a human to read.

Reappearance
-------------
``run_scan_cycle`` calls ``deps.store.forget()`` for every broker
``orchestrator.run_cycle`` reports as ``resolved`` this pass. Before this,
nothing in the codebase ever called ``forget()`` -- a resolved broker's
presence row lived forever, so if it reappeared later, ``is_seen()`` was
already True and the reappearance was silently swallowed as a ``touch()``
rather than surfaced as a new appearance. Forgetting on resolve is what
makes the SAME scan cadence also catch reappearance: a separate
"reappearance interval" distinct from the main scan interval is not
mechanically necessary once this is in place, so ``Intervals`` has exactly
two knobs (``scan_seconds``, ``confirmation_seconds``), not three.
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from broker_guard import brokers as brokers_mod
from broker_guard import profile as profile_mod
from broker_guard.config import Config
from broker_guard.eraser import status_after_removal
from broker_guard.orchestrator import run_cycle

log = logging.getLogger("broker_guard.autopilot")

STATUS_NEEDS_DOCUMENT = "needs_document"
STATUS_NEEDS_REVIEW = "needs_review"

# Kinds sent automatically via eraser `send`, no human in the loop.
AUTO_SEND_KINDS = ("automatable", "captcha")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide_action(kind: str, has_id_documents: bool = False) -> dict:
    """Pure decision: verification kind (+ whether ID docs are on file) ->
    ``{'action': 'auto_send' | 'queue', 'queue_status': str | None}``.

    Never raises for an unrecognized kind -- an unknown kind is queued as
    ``needs_review`` rather than either auto-sent (unsafe) or silently
    dropped (loses the broker entirely). See the module docstring's
    decision table for the full policy and reasoning.
    """
    if kind in AUTO_SEND_KINDS:
        return {"action": "auto_send", "queue_status": None}
    if kind == "photo_id":
        return {"action": "queue", "queue_status": STATUS_NEEDS_DOCUMENT}
    if kind == "kba":
        return {"action": "queue", "queue_status": STATUS_NEEDS_REVIEW}
    return {"action": "queue", "queue_status": STATUS_NEEDS_REVIEW}


@dataclass
class AutopilotDependencies:
    """Everything the autopilot loop touches that is not a pure function --
    same injection-seam pattern as ``service.Dependencies``, so the whole
    state machine can be exercised with fakes: no network, no browser, no
    subprocess, no real clock.
    """

    store: object = None            # StateStore-like (is_seen/record_appearance/seen_brokers/touch/forget/set_status/get_status)
    presence_checker: object = None  # callable(broker, identity_key) -> bool
    submit_removal: object = None   # callable(broker_id, eraser_profile_dict) -> result dict
    eraser_monitor: object = None   # callable() -> result dict, or None if eraser is disabled
    eraser_status: object = None    # callable() -> result dict, or None if eraser is disabled
    alert_sink: object = None       # callable(cycle_payload) -> anything
    now: object = utcnow_iso
    closers: list = field(default_factory=list)

    def close(self):
        for closer in self.closers:
            try:
                closer()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.warning("closer failed", extra={"error": str(exc)})


def run_scan_cycle(identity, brokers: list, deps: AutopilotDependencies,
                    has_id_documents: bool = False) -> dict:
    """One scan + decide + act pass.

    Returns ``orchestrator.run_cycle``'s result dict plus two extra keys:
    ``decisions`` (``{broker_id: decide_action(...) result}`` for every
    new appearance this pass) and ``forgotten`` (broker ids whose presence
    row was cleared because they resolved -- see module docstring on why
    that is what makes reappearance detectable at all).
    """
    identity_key = identity.identity_key
    now_iso = deps.now()

    result = run_cycle(
        identity_key, brokers, deps.presence_checker, deps.store,
        deps.alert_sink or (lambda payload: None), now_iso,
    )

    by_id = {b["id"]: b for b in brokers}
    decisions = {}
    for broker_id in result["new_appearances"]:
        broker = by_id.get(broker_id, {})
        kind = brokers_mod.verification_kind(broker)
        decision = decide_action(kind, has_id_documents)
        decisions[broker_id] = decision

        if decision["action"] == "auto_send":
            if deps.submit_removal is not None:
                try:
                    removal_result = deps.submit_removal(broker_id, identity.to_eraser_profile())
                except Exception as exc:
                    removal_result = {"success": False,
                                       "detail": "{}: {}".format(type(exc).__name__, exc)}
                prior = deps.store.get_status(identity_key, broker_id) or "pending"
                new_status = status_after_removal(prior, removal_result)
            else:
                # No removal engine configured at all -- record intent
                # without pretending a send happened.
                new_status = "pending"
            deps.store.set_status(identity_key, broker_id, new_status, now_iso)
        else:
            deps.store.set_status(identity_key, broker_id, decision["queue_status"], now_iso)

    forgotten = []
    for broker_id in result["resolved"]:
        if hasattr(deps.store, "forget"):
            deps.store.forget(identity_key, broker_id)
            forgotten.append(broker_id)

    log.info("autopilot scan cycle complete", extra={
        "new": len(result["new_appearances"]),
        "auto_sent": sum(1 for d in decisions.values() if d["action"] == "auto_send"),
        "queued": sum(1 for d in decisions.values() if d["action"] == "queue"),
        "forgotten": len(forgotten),
    })

    result["decisions"] = decisions
    result["forgotten"] = forgotten
    return result


def run_confirmation_pass(deps: AutopilotDependencies) -> dict:
    """Best-effort 'did anything get confirmed' step: ``eraser monitor``
    (IMAP inbox scan) then ``eraser status`` (human-readable text). Neither
    result is auto-parsed into a broker_status transition -- see the module
    docstring's "Confirmation, honestly" section for why. Never raises: a
    disabled/unavailable eraser (``deps.eraser_monitor``/``eraser_status``
    is None) simply yields ``None`` for that half rather than an error.
    """
    monitor_result = None
    if deps.eraser_monitor is not None:
        try:
            monitor_result = deps.eraser_monitor()
        except Exception as exc:
            monitor_result = {"success": False, "detail": "{}: {}".format(type(exc).__name__, exc)}

    status_result = None
    if deps.eraser_status is not None:
        try:
            status_result = deps.eraser_status()
        except Exception as exc:
            status_result = {"success": False, "detail": "{}: {}".format(type(exc).__name__, exc)}

    log.info("autopilot confirmation pass complete", extra={
        "monitor_ran": monitor_result is not None,
        "status_ran": status_result is not None,
    })
    return {"monitor": monitor_result, "status": status_result}


def build_dependencies(cfg: Config) -> AutopilotDependencies:
    """Construct the REAL implementations, reusing service.py's existing
    wiring (searx/playwright presence detection, eraser availability, alert
    sink, state store) rather than duplicating it -- this module only adds
    the kind-aware decision layer on top.
    """
    from broker_guard import service as service_mod
    from broker_guard.eraser_bridge import EraserBridge

    base = service_mod.build_dependencies(cfg)
    identity = profile_mod.load_profile(cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    presence_checker = service_mod.build_presence_checker(identity, broker_list, base, cfg)

    eraser_monitor = eraser_status = None
    if cfg.eraser_enabled:
        bridge = EraserBridge(cfg.eraser_bin, cfg.eraser_timeout_s, cfg.eraser_dry_run)
        if bridge.available():
            eraser_monitor = bridge.monitor
            eraser_status = bridge.status

    return AutopilotDependencies(
        store=base.store,
        presence_checker=presence_checker,
        submit_removal=base.removal,
        eraser_monitor=eraser_monitor,
        eraser_status=eraser_status,
        alert_sink=base.alert_sink,
        closers=list(base.closers) + [base.close],
    )


def has_id_documents_on_file(cfg: Config) -> bool:
    """True only when BOTH front and back ID images are stored (see
    webui.upload_id_document) -- a single side is not enough to attempt
    anything with, were an attach path to ever exist."""
    import os

    return all(
        os.path.exists(os.path.join(cfg.id_documents_dir, f"{side}.enc"))
        for side in ("front", "back")
    )


@dataclass
class Intervals:
    scan_seconds: int = 86400
    confirmation_seconds: int = 21600  # check for replies more often than a full re-scan


def run_forever(cfg: Config, deps: AutopilotDependencies, intervals: "Intervals",
                 stop: threading.Event, sleep=None, has_id_documents: bool = False) -> None:
    """The actual long-lived loop: scan+decide+act on ``scan_seconds``,
    confirmation on ``confirmation_seconds``, until ``stop`` is set. Both
    passes run once immediately on start.

    ``sleep`` defaults to ``stop.wait`` (interruptible, matches
    ``service.main``'s pattern) but is an injection seam: a test passes a
    fake that increments a counter and sets ``stop`` after N calls, so the
    state machine can be exercised without real time.
    """
    sleep = sleep or stop.wait
    identity = profile_mod.load_profile(cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)

    tick_seconds = max(1, min(intervals.scan_seconds, intervals.confirmation_seconds))
    elapsed_since_scan = intervals.scan_seconds
    elapsed_since_confirmation = intervals.confirmation_seconds

    # service.main()'s headless loop calls service.write_heartbeat every
    # cycle, so health.heartbeat_stale has real data to read -- this loop
    # (the one actually running in the deployed BG_SERVE_WEB=true container)
    # never did, so heartbeat.json was never written in production and the
    # dashboard had no real "is a scan running / when did it last run" signal
    # to show. Lazy import: service imports nothing from autopilot, but
    # importing it at module scope here would be a needless coupling for a
    # single helper call.
    from broker_guard import service as service_mod

    while not stop.is_set():
        if elapsed_since_scan >= intervals.scan_seconds:
            started = deps.now()
            # Written BEFORE the cycle runs too, with status=running: without
            # this, the dashboard's scan_status() has no signal at all during
            # a cycle in progress (a scan can take minutes -- SERP + browser
            # checks over the whole broker list) and shows "No scan has run
            # yet", indistinguishable from the loop actually being stuck.
            service_mod.write_heartbeat(cfg, {"last_run": started, "status": "running"})
            try:
                result = run_scan_cycle(identity, broker_list, deps, has_id_documents=has_id_documents)
                payload = {"last_run": started, "ok": True, "status": "done"}
                if isinstance(result, dict):
                    payload["present"] = len(result.get("current", []))
                    payload["new"] = len(result.get("new_appearances", []))
                service_mod.write_heartbeat(cfg, payload)
            except Exception as exc:
                log.exception("autopilot scan cycle failed",
                               extra={"error": "{}: {}".format(type(exc).__name__, exc)})
                service_mod.write_heartbeat(cfg, {
                    "last_run": started, "ok": False, "status": "done",
                    "error": "{}: {}".format(type(exc).__name__, exc),
                })
            elapsed_since_scan = 0

        if elapsed_since_confirmation >= intervals.confirmation_seconds:
            try:
                run_confirmation_pass(deps)
            except Exception as exc:
                log.exception("autopilot confirmation pass failed",
                               extra={"error": "{}: {}".format(type(exc).__name__, exc)})
            elapsed_since_confirmation = 0

        if stop.is_set():
            break
        sleep(tick_seconds)
        elapsed_since_scan += tick_seconds
        elapsed_since_confirmation += tick_seconds


def main(argv=None) -> int:  # pragma: no cover - thin CLI wrapper, exercised manually
    import argparse
    import signal
    import sys

    from broker_guard.config import ConfigError, load_config, validate_runtime_paths

    parser = argparse.ArgumentParser(
        prog="python -m broker_guard.autopilot",
        description="Continuously scan, decide and act on broker removals with minimal human input.",
    )
    parser.add_argument("--once", action="store_true", help="run one scan + one confirmation pass, then exit")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    problems = validate_runtime_paths(cfg)
    if problems:
        for problem in problems:
            print(f"config problem: {problem}", file=sys.stderr)
        return 2

    deps = build_dependencies(cfg)
    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("shutdown signal", extra={"signal": signum})
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        if args.once:
            identity = profile_mod.load_profile(cfg.profile_path)
            broker_list = brokers_mod.load_brokers(cfg.brokers_path)
            run_scan_cycle(identity, broker_list, deps, has_id_documents=has_id_documents_on_file(cfg))
            run_confirmation_pass(deps)
        else:
            run_forever(cfg, deps, Intervals(scan_seconds=cfg.interval_seconds), stop,
                        has_id_documents=has_id_documents_on_file(cfg))
    finally:
        deps.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
