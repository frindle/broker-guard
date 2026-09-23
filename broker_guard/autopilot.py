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
    # callable(identity=None) -> presence_checker, invoked ONCE PER PROFILE
    # PER SCAN CYCLE.
    #
    # ``presence_checker`` above is a closure over a set of broker ids
    # computed when it was BUILT (see service.build_presence_checker: it runs
    # the whole SERP sweep eagerly and captures the result). build_dependencies
    # used to build it exactly once, at process start, and hand the same frozen
    # closure to every cycle of run_forever -- so the deployed BG_SERVE_WEB
    # container ran precisely one real scan, at boot, and then re-reported
    # those same startup results every day forever. Rebuilding per cycle is
    # what makes a scheduled re-scan actually re-scan (and what lets the live
    # progress counter advance during a cycle rather than only at boot).
    #
    # Optional: when None, ``presence_checker`` is used as-is, which is what
    # every test that injects a plain predicate relies on.
    presence_checker_factory: object = None
    # callable(identities) -> sweep.SweepResult, invoked ONCE PER SCAN for
    # the whole household: one walk of the broker list checking every
    # profile per broker, with the bounded retry for pairs a rate-limit or
    # bot wall left unknown (see sweep.py). When wired, it is what supplies
    # each cycle's presence checker; ``presence_checker``/
    # ``presence_checker_factory`` above remain the per-identity fallback
    # for callers and tests that inject their own predicate.
    sweep_factory: object = None
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


def _call_presence_factory(factory, identity):
    """Invoke *factory* with the identity when it takes one, without one
    when it does not. See ``run_scan_cycle`` for why this is decided by
    signature rather than by catching TypeError."""
    import inspect

    try:
        takes_identity = bool(inspect.signature(factory).parameters)
    except (TypeError, ValueError):  # builtins / C callables have no signature
        takes_identity = False
    return factory(identity) if takes_identity else factory()


def run_scan_cycle(identity, brokers: list, deps: AutopilotDependencies,
                    has_id_documents: bool = False, presence_checker=None) -> dict:
    """One scan + decide + act pass.

    Returns ``orchestrator.run_cycle``'s result dict plus two extra keys:
    ``decisions`` (``{broker_id: decide_action(...) result}`` for every
    new appearance this pass) and ``forgotten`` (broker ids whose presence
    row was cleared because they resolved -- see module docstring on why
    that is what makes reappearance detectable at all).
    """
    identity_key = identity.identity_key
    now_iso = deps.now()

    # Rebuild the presence checker for THIS cycle when a factory is wired
    # (the real deployment), so each scheduled scan is a genuinely fresh
    # sweep rather than a replay of the one taken at process start. See
    # AutopilotDependencies.presence_checker_factory.
    # A checker handed in by the caller wins: that is the multi-profile
    # sweep (``sweep.run_sweep``) having ALREADY checked every broker for
    # every profile in one interleaved pass, of which this cycle is one
    # person's slice. Rebuilding one here would re-run that person's whole
    # sweep a second time.
    if presence_checker is not None:
        pass
    elif deps.presence_checker_factory is None:
        presence_checker = deps.presence_checker
    else:
        # The factory is per-IDENTITY as well as per-cycle: with every
        # profile scanned each pass, a checker built for profile A's name
        # variants says nothing about profile B. A factory that predates
        # that (or a test's zero-arg fake) is still called with no
        # argument rather than blowing up. The arity is decided by
        # INSPECTING the factory, not by catching TypeError: a TypeError
        # raised from inside a one-argument factory would otherwise be
        # swallowed and the whole sweep silently retried without the
        # identity.
        presence_checker = _call_presence_factory(deps.presence_checker_factory, identity)

    result = run_cycle(
        identity_key, brokers, presence_checker, deps.store,
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

    # Carry the per-leg detection tallies (see service._counting_observer)
    # through to the caller, so run_forever can put a real error count in the
    # heartbeat and the dashboard can distinguish "checked 827, found 0" from
    # "827 checks failed". Absent on a hand-injected test checker, which is
    # fine -- the key is simply omitted rather than faked as zero.
    serp_stats = getattr(presence_checker, "serp_stats", None)
    browser_stats = getattr(presence_checker, "browser_stats", None)
    if serp_stats is not None or browser_stats is not None:
        result["detection"] = {"serp": dict(serp_stats or {}),
                               "browser": dict(browser_stats or {})}
        result["detection_errors"] = ((serp_stats or {}).get("error", 0)
                                      + (browser_stats or {}).get("error", 0))

    log.info("autopilot scan cycle complete", extra={
        "new": len(result["new_appearances"]),
        "auto_sent": sum(1 for d in decisions.values() if d["action"] == "auto_send"),
        "queued": sum(1 for d in decisions.values() if d["action"] == "queue"),
        "forgotten": len(forgotten),
        "detection_errors": result.get("detection_errors"),
    })

    result["decisions"] = decisions
    result["forgotten"] = forgotten
    return result


def run_scan_cycles(identities: list, brokers: list, deps: AutopilotDependencies,
                    has_id_documents: bool = False, progress=None) -> dict:
    """``run_scan_cycle`` for EVERY profile, one after another.

    This is what "there is no active profile" means at the scan loop: the
    whole saved list is swept every pass, each profile's outcomes recorded
    under its own ``identity_key`` (in the state db, which already scopes
    that way, and in the live progress map).

    Detection itself happens ONCE, for everybody, in ``deps.sweep_factory``
    (``sweep.run_sweep``): one walk of the broker list with every profile
    checked per broker, plus its bounded retry of whatever a rate-limit or
    bot wall left unknown. Each identity's cycle below then only diffs that
    profile's slice of the sweep against the state db and acts on it, so
    the expensive network work is not repeated per person.

    Without a sweep factory (every test that injects a plain predicate, and
    any caller predating the sweep) this falls back to the per-identity
    path: each cycle builds its own checker, with ``begin_scan`` opened
    here so profile 2's results accumulate onto profile 1's rather than
    wiping them. Pass an isolated ``ScanProgress`` in a test to stay off
    process-wide state.

    One profile's failure never costs the others their scan: an exception
    is recorded as that profile's ``error`` entry and the loop continues.
    Aggregates (``new_appearances``/``resolved``/``forgotten`` counts) are
    summed for the heartbeat, but the per-identity results are returned
    intact under ``results`` -- "who was found where" is the question, and
    flattening it away would lose the answer.
    """
    from broker_guard import progress as progress_mod

    progress = progress if progress is not None else progress_mod.current()
    sweep_result = None
    if deps.sweep_factory is not None and identities:
        # run_sweep opens the scan itself (it is what knows how many
        # broker x profile pairs there are), so begin_scan is deliberately
        # NOT called here as well -- doing both would clear the map the
        # sweep just filled.
        sweep_result = deps.sweep_factory(identities)
    else:
        progress.begin_scan(identity_keys=[i.identity_key for i in identities])

    results = {}
    try:
        for identity in identities:
            try:
                results[identity.identity_key] = run_scan_cycle(
                    identity, brokers, deps, has_id_documents=has_id_documents,
                    presence_checker=(sweep_result.checker_for(identity.identity_key)
                                      if sweep_result is not None else None),
                )
            except Exception as exc:
                log.exception("scan cycle failed for one profile",
                              extra={"identity_key": identity.identity_key})
                results[identity.identity_key] = {
                    "identity_key": identity.identity_key,
                    "error": "{}: {}".format(type(exc).__name__, exc),
                }
    finally:
        progress.end_scan()

    def _total(key):
        return sum(len(r.get(key) or ()) for r in results.values())

    detection_errors = sum(
        r.get("detection_errors") or 0 for r in results.values()
        if isinstance(r.get("detection_errors"), int)
    )
    return {
        "results": results,
        "identity_keys": [i.identity_key for i in identities],
        "profiles_scanned": len(identities),
        # A user-requested stop cancels the WHOLE multi-profile pass (the
        # sweep's own loop, its pending retries, and therefore every
        # profile's share of it) -- see sweep.py. Surfaced here so the
        # heartbeat and the dashboard can say "stopped early" rather than
        # reporting a truncated pass as a completed one.
        "stopped": bool(sweep_result.stopped) if sweep_result is not None else False,
        "retried_pairs": sweep_result.retried_pairs if sweep_result is not None else 0,
        "unresolved_pairs": (sweep_result.unresolved_pairs
                             if sweep_result is not None else 0),
        "failed_profiles": [k for k, r in results.items() if r.get("error")],
        # {identity_key: message} for the profiles that blew up -- the
        # messages themselves, not just the count, so the heartbeat can
        # say WHAT failed instead of only that something did.
        "errors": {k: r["error"] for k, r in results.items() if r.get("error")},
        "current": _total("current"),
        "new_appearances": _total("new_appearances"),
        "resolved": _total("resolved"),
        "forgotten": _total("forgotten"),
        "detection_errors": detection_errors,
    }


def _merge_detection(results: dict) -> "dict | None":
    """Sum every profile's per-leg detection tallies into one
    ``{'serp': {...}, 'browser': {...}}`` block for the heartbeat.

    Returns ``None`` when not one profile reported a tally (e.g. a
    hand-injected test checker with no ``serp_stats``), so the heartbeat
    omits the field instead of persisting a fabricated all-zero block that
    ``webui_data.last_scan_detection_line`` would read as a real, clean
    scan.
    """
    merged = {"serp": {}, "browser": {}}
    seen = False
    for result in results.values():
        detection = result.get("detection")
        if not isinstance(detection, dict):
            continue
        seen = True
        for leg in ("serp", "browser"):
            for key, value in (detection.get(leg) or {}).items():
                merged[leg][key] = merged[leg].get(key, 0) + value
    return merged if seen else None


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

    *cfg* is the ENVIRONMENT tier (see ``config.py``). Everything below that
    depends on a UI-editable setting re-resolves it through
    ``settings.effective_config`` at the moment it is used, not once here --
    otherwise a setting changed in the dashboard would not take effect until
    the container was restarted, which is precisely the failure mode the
    settings store exists to remove. Concretely:

    * **detection** (SearXNG URL, the pacing pair, the Playwright toggle) --
      the layer is torn down and rebuilt at the start of a scan cycle, but
      only when ``settings.detection_fingerprint`` actually changed, so an
      unchanged setting does not relaunch Chromium every cycle.
    * **removal** (eraser enabled / dry run) -- re-resolved per removal, which
      is free: ``EraserBridge`` builds an argv, it does not spawn anything.
    * **alerting** (webhook URL) -- the composite sink is rebuilt only when
      the URL changes, so its ``last_notification`` survives across cycles.
    * **eraser monitor/status** -- re-resolved per confirmation pass.

    The one knob that is NOT live here is ``interval_seconds``; see
    ``run_forever``.
    """
    from broker_guard import service as service_mod
    from broker_guard import settings as settings_mod
    from broker_guard.eraser_bridge import EraserBridge
    from broker_guard.sinks import build_alert_sink

    def live_cfg() -> Config:
        return settings_mod.effective_config(cfg)

    boot_cfg = live_cfg()
    base = service_mod.build_dependencies(boot_cfg)

    # The detection layer currently in hand, plus the settings fingerprint it
    # was built from and its own teardown. Seeded from `base` (which
    # build_dependencies already constructed) so the common "nothing changed"
    # path builds nothing at all on the first cycle.
    detection = {
        "fingerprint": settings_mod.detection_fingerprint(boot_cfg),
        "searx_search": base.searx_search,
        "page_action": base.page_action,
        # base.closers owns this generation's teardown already (it is closed by
        # base.close(), which is in our closers list below); a REBUILD hands
        # ownership of the superseded generation to us, see below.
        "closers": [],
        "owned": False,
    }

    def _drop_detection():
        """Close the superseded detection layer (today: the Playwright
        browser). Closing it is what stops a settings change from leaking one
        live Chromium per edit."""
        for closer in detection["closers"]:
            try:
                closer()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.warning("detection closer failed", extra={"error": str(exc)})
        detection["closers"] = []
        if not detection["owned"]:
            # First rebuild: the generation being dropped is base's, so close
            # base's detection closers here rather than leaving them to run at
            # process exit against an object we have already replaced.
            for closer in list(base.closers):
                try:
                    closer()
                except Exception as exc:  # pragma: no cover
                    log.warning("detection closer failed", extra={"error": str(exc)})
            base.closers.clear()
            detection["owned"] = True

    def _detection_deps(live: Config):
        fingerprint = settings_mod.detection_fingerprint(live)
        if fingerprint != detection["fingerprint"]:
            log.info("detection settings changed; rebuilding detection layer")
            _drop_detection()
            searx_search, page_action, closers = service_mod.build_detection(live)
            detection.update(fingerprint=fingerprint, searx_search=searx_search,
                             page_action=page_action, closers=closers)
        return service_mod.Dependencies(
            searx_search=detection["searx_search"],
            page_action=detection["page_action"],
            store=base.store,
        )

    def presence_checker_factory(identity=None):
        """Run a FRESH sweep for the profile/cycle that is about to start.

        The broker list is re-read here too, not captured once: a
        regenerated brokers.json takes effect on the next scheduled scan
        instead of requiring a container restart. The same goes for the
        detection settings themselves.

        *identity* is the profile being scanned this pass -- every saved
        profile gets its own sweep, so the checker has to be built from
        that profile's own name variants. ``None`` falls back to the
        legacy single-identity file, which is what the pre-multi-profile
        callers pass.
        """
        live = live_cfg()
        if identity is None:
            identity = profile_mod.load_profile(live.profile_path)
        broker_list = brokers_mod.load_brokers(live.brokers_path)
        return service_mod.build_presence_checker(
            identity, broker_list, _detection_deps(live), live,
        )

    def submit_removal(broker_id, eraser_profile):
        removal = service_mod.build_removal(live_cfg())
        if removal is None:
            # Removals are switched off right now -- report intent without
            # pretending anything was sent (run_scan_cycle's own
            # submit_removal-is-None branch does the same, and this keeps the
            # two paths saying the same thing).
            return {"success": False, "detail": "removal engine disabled"}
        return removal(broker_id, eraser_profile)

    alerting = {"url": boot_cfg.alert_webhook_url, "sink": base.alert_sink}

    def alert_sink(payload):
        live = live_cfg()
        if live.alert_webhook_url != alerting["url"]:
            log.info("alert webhook changed; rebuilding alert sink")
            alerting.update(url=live.alert_webhook_url, sink=build_alert_sink(live))
        return alerting["sink"](payload)

    def _confirmation_bridge():
        live = live_cfg()
        if not live.eraser_enabled:
            return None
        bridge = EraserBridge(live.eraser_bin, live.eraser_timeout_s, live.eraser_dry_run)
        return bridge if bridge.available() else None

    def eraser_monitor():
        bridge = _confirmation_bridge()
        return None if bridge is None else bridge.monitor()

    def eraser_status():
        bridge = _confirmation_bridge()
        return None if bridge is None else bridge.status()

    def sweep_factory(identities):
        """One interleaved sweep for the whole household -- see sweep.py."""
        from broker_guard import sweep as sweep_mod

        live = live_cfg()
        broker_list = brokers_mod.load_brokers(live.brokers_path)
        return sweep_mod.run_sweep(identities, broker_list, _detection_deps(live), live)

    return AutopilotDependencies(
        store=base.store,
        presence_checker=None,
        presence_checker_factory=presence_checker_factory,
        sweep_factory=sweep_factory,
        submit_removal=submit_removal,
        eraser_monitor=eraser_monitor,
        eraser_status=eraser_status,
        alert_sink=alert_sink,
        closers=[_drop_detection, base.close],
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

    Every scan pass sweeps EVERY saved profile (``run_scan_cycles``), not
    one "active" one -- see ``profiles.py``'s module docstring. The profile
    list is re-read at the top of each pass, so an added or edited profile
    joins the next scan without a restart.

    ``sleep`` defaults to ``stop.wait`` (interruptible, matches
    ``service.main``'s pattern) but is an injection seam: a test passes a
    fake that increments a counter and sets ``stop`` after N calls, so the
    state machine can be exercised without real time.

    Scan interval, and why it is "next tick" rather than immediate
    ----------------------------------------------------------------
    ``intervals.scan_seconds`` is re-read from the settings store at the TOP
    OF EVERY TICK (``_live_scan_seconds``), so changing "Scan interval" in the
    dashboard needs no container restart. It does not interrupt a sleep that
    is already in progress: the loop's sleep is ``stop.wait``, the same event
    that carries SIGTERM, and waking it early for a settings poll would mean
    either a second polling thread or shortening every tick for every
    deployment -- real complexity for a knob whose smallest legal value is 60
    seconds and whose realistic value is a day. The practical bound is
    therefore the tick, i.e. ``min(scan_seconds, confirmation_seconds)`` --
    6 hours with the shipped defaults, and immediately at the next tick
    whenever the loop is already ticking faster than that. A change that
    someone wants to see take effect right now is one "Run scan now" click on
    the dashboard, which builds its deps from the live settings anyway.
    """
    from broker_guard import settings as settings_mod

    sleep = sleep or stop.wait
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)

    def _scan_identities() -> list:
        """Every saved profile, re-read at the top of each scan.

        Re-read rather than captured once so a profile added or edited on
        the Profile page joins the very next scheduled scan instead of
        waiting for a container restart -- and so "all profiles are
        scanned" keeps being true as the list changes.
        """
        from broker_guard import profiles as profiles_mod

        try:
            return profiles_mod.load_scan_identities(cfg.profiles_path, cfg.profile_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("profiles unreadable; falling back to the legacy profile",
                        extra={"error": "{}: {}".format(type(exc).__name__, exc)})
            try:
                return [profile_mod.load_profile(cfg.profile_path)]
            except (OSError, ValueError):
                return []

    def _live_scan_seconds() -> int:
        """The scan interval as of right now: the stored setting if the
        dashboard has set one, otherwise whatever this loop was started with
        (which is ``cfg.interval_seconds``, i.e. the env tier) -- never
        silently overriding a caller that passed an explicit, non-config
        interval, which is what every test here does."""
        try:
            live = settings_mod.effective_config(cfg)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("settings re-read failed; keeping current interval",
                        extra={"error": str(exc)})
            return intervals.scan_seconds
        if live.interval_seconds != cfg.interval_seconds:
            return live.interval_seconds
        return intervals.scan_seconds

    scan_seconds = _live_scan_seconds()
    tick_seconds = max(1, min(scan_seconds, intervals.confirmation_seconds))
    elapsed_since_scan = scan_seconds
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
        # Re-read at the top of every tick, so a "Scan interval" change made
        # in the dashboard is picked up without a restart (see the docstring
        # for why this is next-tick rather than mid-sleep).
        scan_seconds = _live_scan_seconds()
        tick_seconds = max(1, min(scan_seconds, intervals.confirmation_seconds))

        if elapsed_since_scan >= scan_seconds:
            started = deps.now()
            # Written BEFORE the cycle runs too, with status=running: without
            # this, the dashboard's scan_status() has no signal at all during
            # a cycle in progress (a scan can take minutes -- SERP + browser
            # checks over the whole broker list) and shows "No scan has run
            # yet", indistinguishable from the loop actually being stuck.
            service_mod.write_heartbeat(cfg, {"last_run": started, "status": "running"})
            try:
                identities = _scan_identities()
                sweep = run_scan_cycles(identities, broker_list, deps,
                                        has_id_documents=has_id_documents)
                payload = {
                    "last_run": started, "ok": not sweep["failed_profiles"], "status": "done",
                    # Aggregated across every profile scanned this pass;
                    # `profiles_scanned` is what makes "0 found" readable
                    # (0 across 3 people, or 0 because nobody was scanned).
                    "present": sweep["current"],
                    "new": sweep["new_appearances"],
                    "profiles_scanned": sweep["profiles_scanned"],
                    "identity_keys": sweep["identity_keys"],
                    # A pass the person stopped is neither "completed" nor
                    # "failed"; it is a real, partial pass, and every
                    # surface that reads this heartbeat says so rather
                    # than implying a full sweep finished.
                    "stopped": sweep["stopped"],
                    "retried_pairs": sweep["retried_pairs"],
                    "unresolved_pairs": sweep["unresolved_pairs"],
                }
                if sweep["failed_profiles"]:
                    payload["failed_profiles"] = sweep["failed_profiles"]
                    # A per-profile failure is contained (the other
                    # profiles still get scanned) but it is still a real
                    # error, and the heartbeat is the only place anything
                    # reads it back from. Keep the SAME `error` string
                    # shape a whole-cycle exception writes, so health
                    # checks have one field to look at instead of two.
                    payload["error"] = "; ".join(sweep["errors"].values())
                # Persisted so the dashboard can render "last scan:
                # checked 827, 0 errors" vs "... 340 errors" AFTER the
                # cycle ends, when the in-memory progress counter has
                # gone inactive. A cycle that errored everywhere is not
                # a successful cycle that found nothing.
                detection = _merge_detection(sweep["results"])
                if detection is not None:
                    payload["detection"] = detection
                    payload["detection_errors"] = sweep["detection_errors"]
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

    from broker_guard import settings as settings_mod

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Same overlay as service.main: stored (dashboard) > env > default.
    cfg = settings_mod.effective_config(cfg)

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
            from broker_guard import profiles as profiles_mod

            identities = profiles_mod.load_scan_identities(cfg.profiles_path, cfg.profile_path)
            broker_list = brokers_mod.load_brokers(cfg.brokers_path)
            run_scan_cycles(identities, broker_list, deps,
                            has_id_documents=has_id_documents_on_file(cfg))
            run_confirmation_pass(deps)
        else:
            run_forever(cfg, deps, Intervals(scan_seconds=cfg.interval_seconds), stop,
                        has_id_documents=has_id_documents_on_file(cfg))
    finally:
        deps.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
