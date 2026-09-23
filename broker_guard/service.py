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
import random
import signal
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from broker_guard import broker_normalize, brokers as brokers_mod
from broker_guard import health, profile as profile_mod, scheduler, serpwatch
from broker_guard import playwright_checks, profiles as profiles_mod, progress as progress_mod
from broker_guard import settings as settings_mod
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


def _empty_stats() -> dict:
    """A zeroed per-leg outcome tally.

    Pre-seeded with every key rather than built up lazily so a consumer can
    read ``stats["error"]`` unconditionally -- a missing key defaulting to 0
    reads identically to a real zero, which is the exact ambiguity this
    whole error-counting change exists to remove.
    """
    return {"hit": 0, "checked": 0, "error": 0, "skipped": 0}


def _counting_observer(stats: dict, progress, error_ids=None):
    """Fan one per-broker outcome out to *stats* and to the live *progress*.

    ``record_outcome`` (not ``record``) is what keeps the broker_id: the
    aggregate tally alone could say "827 checked, 0 found" but could not
    name a single clean broker, which is why ``/brokers`` showed nothing
    at all during a clean scan.
    """

    def _observe(broker_id, outcome, hits=0, errors=0):
        if outcome in stats:
            stats[outcome] += 1
        if error_ids is not None and outcome == "error":
            error_ids.add(broker_id)
        progress.record_outcome(broker_id, outcome, hits, errors)

    return _observe


def order_brokers_for_scan(brokers: list[dict], known_ids, rng=None) -> list[dict]:
    """The order ONE cycle walks the broker list: unknown brokers first,
    then everything else shuffled.

    Two properties, both deliberate:

    * **Unknown first.** A broker this identity has no information about
      (no ``presence`` row ever -- ``StateStore.seen_brokers``) is where a
      new, undiscovered listing can actually turn up; a broker already
      known to list the person is re-confirmation. A cycle that is slow,
      rate-limited or killed half way therefore spends its budget on the
      brokers most likely to produce something new.
    * **Never the same order twice.** The remainder is shuffled with a
      fresh ``random.Random()`` per call (seeded from OS entropy at
      construction), NOT from a fixed seed or from the broker id. With a
      stable order, a scan that dies or gets rate-limited two thirds of
      the way through starves the SAME tail of the list every single
      cycle -- those brokers would never be checked at all. The unknown
      group is shuffled too, for exactly the same reason.

    Pure and total: never drops, duplicates or mutates a broker (the
    result is a permutation of *brokers*), and *rng* is injectable so a
    test can assert the ordering without flaking on real randomness.
    """
    known = {str(bid) for bid in (known_ids or ())}
    rng = rng if rng is not None else random.Random()
    unknown = [b for b in brokers if str(b.get("id") or "") not in known]
    rest = [b for b in brokers if str(b.get("id") or "") in known]
    rng.shuffle(unknown)
    rng.shuffle(rest)
    return unknown + rest


def _known_broker_ids(deps, identity_key: str) -> set:
    """Brokers this identity already has a presence record for.

    Best-effort: a store that is missing, closed or raising must not cost
    the cycle -- the scan just falls back to treating every broker as
    unknown, which is a worse ORDER, never a wrong result.
    """
    store = getattr(deps, "store", None)
    if store is None or not hasattr(store, "seen_brokers"):
        return set()
    try:
        return {str(bid) for bid in (store.seen_brokers(identity_key) or ())}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not read seen brokers for scan ordering",
                    extra={"error": "{}: {}".format(type(exc).__name__, exc)})
        return set()


def build_presence_checker(identity, brokers, deps, cfg, progress=None):
    """Fuse SERP hits and browser checks into ``presence_checker(broker, key)``.

    This is the join the per-slice modules never had: ``run_serpwatch``
    returns a list of PresenceResult, ``run_playwright_checks`` returns a dict
    keyed by broker_id, and ``run_cycle`` wants a per-broker predicate. A
    broker counts as present if EITHER source says so; a leg that errored is
    not evidence of absence, so it defers to the other leg, and a broker
    neither leg could actually check raises ``PresenceUnknown`` rather than
    reporting a false "absent" (which ``run_cycle`` would report as
    ``resolved`` and autopilot would act on with ``store.forget()``).

    ``progress`` is a ``broker_guard.progress.ScanProgress`` (defaulting to
    the process-wide one) that both legs feed per broker, so the dashboard
    can show "412/827 checked, 3 found, 0 errors" WHILE the cycle runs
    instead of a bare "scan in progress" for the next hour. Pass an
    isolated instance in a test to avoid touching process-wide state.

    The returned checker also carries ``serp_stats``/``browser_stats``
    attributes -- the per-leg outcome tallies -- so ``run_once`` can put
    real error counts in the cycle result rather than only in a log line.

    Both legs walk ``order_brokers_for_scan``'s order -- brokers with no
    presence record for this identity first, the rest freshly shuffled --
    computed ONCE here rather than separately per leg, so the two legs
    agree and the policy has a single home. ``scan_order`` is the scan's
    order only; ``run_cycle``'s bookkeeping still sees the caller's list.
    """
    name_variants = profile_mod.name_variants(identity)
    progress = progress if progress is not None else progress_mod.current()
    scan_order = order_brokers_for_scan(
        brokers, _known_broker_ids(deps, identity.identity_key)
    )
    # One cycle, two legs. This is the per-broker map's reset point (see
    # progress.begin_cycle); progress.start() below still resets the
    # aggregate counters per leg, because their denominators differ.
    # ``identity.identity_key`` is the SAME key state.py scopes
    # presence/broker_status by -- the results are tagged with it so a
    # /brokers page filtered to one profile can never show another's.
    progress.begin_cycle(identity_key=identity.identity_key, total=len(brokers))

    serp_ids = set()
    serp_error_ids = set()
    serp_stats = _empty_stats()
    if deps.searx_search is not None:
        progress.start(progress_mod.PHASE_SERP, len(brokers))
        hits = serpwatch.run_serpwatch(
            scan_order, identity.phones, identity.emails, name_variants,
            identity.addresses, deps.searx_search,
            observer=_counting_observer(serp_stats, progress, serp_error_ids),
        )
        serp_ids = {hit.broker_id for hit in hits}
        progress.finish()
        # `errors` is the whole point of this line: a cycle that logged
        # `hit_brokers: 0` used to be indistinguishable from one where every
        # single broker's search failed. Now it is not.
        log.info("serpwatch complete", extra={
            "brokers": len(brokers),
            "hit_brokers": len(serp_ids),
            "checked": serp_stats["checked"],
            "errors": serp_stats["error"],
            "skipped": serp_stats["skipped"],
        })

    browser_results = {}
    browser_stats = _empty_stats()
    if deps.page_action is not None:
        terms = name_variants + identity.phones + identity.emails
        checks = playwright_checks.build_site_checks(scan_order, terms, identity)
        progress.start(progress_mod.PHASE_BROWSER, len(checks))
        browser_results = playwright_checks.run_playwright_checks(
            checks, deps.page_action,
            observer=_counting_observer(browser_stats, progress),
        )
        progress.finish()
        errored = sum(1 for r in browser_results.values() if not r["checked"])
        log.info("browser checks complete",
                 extra={"checks": len(checks), "errored": errored,
                        "hits": browser_stats["hit"], "checked": browser_stats["checked"]})

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
        if broker_id in serp_error_ids:
            # The SERP leg FAILED for this broker (every query against it
            # errored -- SearXNG unreachable, or answering 200s with nothing
            # because its upstream engines are all walled) and the browser leg
            # has no opinion either. That is unknown, not absent.
            #
            # This used to fall through to `return False`. serpwatch counted
            # the failure in serp_stats, but that tally is only a log line and
            # a dashboard number -- it never reached run_cycle, so the broker
            # was still reported as "not present" and, if it had a presence
            # row, as `resolved` -> store.forget(). Raising is what puts the
            # SERP leg behind the same safety net the browser leg already had.
            #
            # Note the ordering: a broker the browser CHECKED cleanly returns
            # above, on the strength of a successful direct read of the
            # broker's own site, which is better evidence than the search
            # index. A SERP failure only decides brokers the browser leg did
            # not resolve.
            raise PresenceUnknown(
                "{}: SERP check failed; presence unknown".format(broker_id)
            )
        return False

    presence_checker.serp_ids = serp_ids
    presence_checker.serp_error_ids = serp_error_ids
    presence_checker.browser_results = browser_results
    presence_checker.serp_stats = serp_stats
    presence_checker.browser_stats = browser_stats
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
    """One full cycle for the single legacy ``profile.local.json`` identity.

    Kept as-is for the entry points that predate multi-profile scanning
    (the headless ``service.main`` loop, tests). ``run_all`` is the one
    that sweeps every saved profile -- see its docstring.
    """
    identity = profile_mod.load_profile(cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)
    return run_once_for(cfg, deps, identity, broker_list)


def run_all(cfg: Config, deps: Dependencies) -> dict:
    """One full cycle for EVERY saved profile, in list order.

    There is no "active" profile (see ``profiles.py``): a scan sweeps the
    whole household, and each profile's findings are recorded under that
    profile's own ``identity_key`` -- in the state db, which already scopes
    ``presence``/``broker_status`` that way, and in the live progress map.

    Detection is ONE interleaved pass for everybody (``sweep.run_sweep``:
    outer loop brokers, inner loop profiles, plus its bounded retry of the
    pairs a rate-limit or bot wall left unknown). Each profile's cycle then
    diffs its own slice of that sweep, so the network work is not repeated
    per person.

    Returns ``{"identities": [...], "results": {identity_key: result},
    "stopped": bool, "ran_at": iso}``. Per-profile results are NOT merged
    into one blob: "which person was found where" is the question this tool
    exists to answer, so the per-identity shape is preserved all the way
    out.

    A profile whose cycle RAISES does not cost the remaining profiles
    their scan -- the failure is recorded as that profile's ``error`` and
    the loop continues. Nothing about the resolved/unknown safety net
    changes: each profile's own ``run_cycle`` applies it as before, and a
    pair the sweep never reached (stopped early, or blocked through every
    retry) is UNKNOWN, so it is excluded from ``resolved`` too.
    """
    from broker_guard import sweep as sweep_mod

    started = deps.now()
    identities = profiles_mod.load_scan_identities(cfg.profiles_path, cfg.profile_path)
    broker_list = brokers_mod.load_brokers(cfg.brokers_path)

    sweep_result = sweep_mod.run_sweep(identities, broker_list, deps, cfg)
    results = {}
    try:
        for identity in identities:
            try:
                results[identity.identity_key] = run_once_for(
                    cfg, deps, identity, broker_list, started,
                    presence_checker=sweep_result.checker_for(identity.identity_key),
                )
            except Exception as exc:
                log.exception("cycle failed for one profile",
                              extra={"identity_key": identity.identity_key})
                results[identity.identity_key] = {
                    "identity_key": identity.identity_key,
                    "error": "{}: {}".format(type(exc).__name__, exc),
                }
    finally:
        progress_mod.current().end_scan()

    return {
        "identities": [
            {"identity_key": i.identity_key, "full_name": i.full_name} for i in identities
        ],
        "results": results,
        "stopped": sweep_result.stopped,
        "retried_pairs": sweep_result.retried_pairs,
        "unresolved_pairs": sweep_result.unresolved_pairs,
        "ran_at": started,
    }


def run_once_for(cfg: Config, deps: Dependencies, identity, broker_list: list,
                 started: str | None = None, presence_checker=None) -> dict:
    """One full cycle for ONE identity: detect, diff, alert, submit removals.

    *presence_checker* lets a caller supply detection that has ALREADY
    happened -- ``run_all`` passes this identity's slice of the one
    household-wide sweep. Left None, this builds its own single-identity
    checker exactly as before.
    """
    started = started or deps.now()
    identity_key = identity.identity_key
    log.info("cycle start", extra={"brokers": len(broker_list), "identity_key": identity_key})

    if presence_checker is None:
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
    # Detection health, kept SEPARATE from result["errors"].
    #
    # result["errors"] is orchestrator.run_cycle's list of brokers whose
    # presence_checker RAISED (i.e. browser check errored -> PresenceUnknown).
    # It has never included a SERP failure, because a failed SERP query was
    # silently turned into "no hit" long before the checker ran. These two
    # fields are that missing signal -- a cycle can now say "827 brokers,
    # 340 of them errored" instead of an unqualified "0 found".
    result["detection"] = {
        "serp": dict(presence_checker.serp_stats),
        "browser": dict(presence_checker.browser_stats),
    }
    result["detection_errors"] = (
        presence_checker.serp_stats["error"] + presence_checker.browser_stats["error"]
    )
    log.info("cycle complete", extra={
        "present": len(result["current"]),
        "new": len(result["new_appearances"]),
        "resolved": len(result["resolved"]),
        "errors": len(result["errors"]),
        "detection_errors": result["detection_errors"],
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


def build_detection(cfg: Config) -> tuple:
    """The two presence-detection legs for *cfg*: ``(searx_search,
    page_action, closers)``.

    Split out of ``build_dependencies`` so the autopilot can tear this layer
    down and rebuild it MID-PROCESS when the SearXNG URL / pacing pair /
    Playwright toggle change through the dashboard, without also rebuilding
    (and re-opening) the state db, the alert sink and the removal bridge. See
    ``settings.detection_fingerprint`` for the exact set of values that
    invalidates a previously-built layer.

    ``closers`` is this layer's own teardown (today: the Playwright browser),
    and is the caller's to run -- exactly once, when it drops the layer.
    """
    # The search-form-aware page action: identical to browser.make_page_action
    # for every broker without a hand-verified search recipe, and the site's
    # own people-search form for the few that have one.
    from broker_guard.search_probe import make_page_action
    from broker_guard.searx_client import PermanentSearxError, SearxClient

    closers = []

    searx_search = None
    if cfg.searxng_url:
        try:
            searx_search = SearxClient(
                cfg.searxng_url, timeout_s=cfg.searxng_timeout_s, auth=cfg.searxng_auth,
                engines=cfg.searxng_engines, attempts=max(1, cfg.max_retries),
                base_delay=cfg.retry_base_delay_s,
                min_interval_s=cfg.searxng_min_interval_s,
                jitter_s=cfg.searxng_jitter_s,
            )
            log.info("searxng enabled", extra={
                "min_interval_s": cfg.searxng_min_interval_s,
                "jitter_s": cfg.searxng_jitter_s,
            })
        except PermanentSearxError as exc:
            log.error("searxng disabled", extra={"error": str(exc)})
    else:
        log.warning("no SearXNG URL configured; SERP detection disabled")

    page_action = None
    if cfg.playwright_enabled:
        page_action, closer = make_page_action(cfg.playwright_timeout_ms, cfg.playwright_headless)
        closers.append(closer)

    return searx_search, page_action, closers


def build_removal(cfg: Config):
    """The removal callable for *cfg*, or ``None`` when removals are off.

    Also split out of ``build_dependencies`` (same reason as
    ``build_detection``): the autopilot re-resolves it per scan cycle, so
    flipping "Removal engine enabled"/"DRY RUN" in the dashboard takes effect
    on the next cycle rather than on the next container restart. Cheap to
    build -- ``EraserBridge`` is argv construction, not a subprocess.
    """
    from broker_guard.eraser_bridge import EraserBridge, noop_bridge

    if not cfg.eraser_enabled:
        return None
    bridge = EraserBridge(cfg.eraser_bin, cfg.eraser_timeout_s, cfg.eraser_dry_run)
    if bridge.available():
        log.info("eraser enabled", extra={"dry_run": cfg.eraser_dry_run})
        return bridge.submit_removal
    log.error("eraser binary not found; removals disabled",
              extra={"eraser_bin": cfg.eraser_bin})
    return noop_bridge


def build_dependencies(cfg: Config) -> Dependencies:
    """Construct the REAL implementations from config."""
    from broker_guard.sinks import build_alert_sink

    searx_search, page_action, closers = build_detection(cfg)

    removal = build_removal(cfg)

    return Dependencies(
        searx_search=searx_search,
        page_action=page_action,
        removal=removal,
        alert_sink=build_alert_sink(cfg),
        store=StateStore.open(cfg.state_path),
        closers=closers,
    )


def run_recipe_check(cfg) -> int:
    """``--check-recipes``: probe every recipe's page, print, alert, exit code.

    Exit code 1 when any recipe shows drift, so this is usable from cron or
    a CI job as well as by hand. A blocked or unreachable page is NOT drift
    and does not fail the run -- see ``recipe_check``'s docstring.
    """
    from broker_guard import recipe_check
    from broker_guard.optout_submit import OptOutSubmitter
    from broker_guard.sinks import build_alert_sink

    submitter = OptOutSubmitter(timeout_ms=cfg.playwright_timeout_ms,
                                headless=cfg.playwright_headless)
    try:
        submitter.start()
    except Exception as exc:
        print("cannot open a browser for the recipe check: {}: {}".format(
            type(exc).__name__, exc), file=sys.stderr)
        return 2

    pages = []

    def new_page():
        _context, page = submitter.new_page()
        pages.append(_context)
        return page

    try:
        reports = recipe_check.check_all(new_page)
    finally:
        for context in pages:
            try:
                context.close()
            except Exception:
                pass
        submitter.close()

    print(recipe_check.format_report(reports))
    events = recipe_check.drift_events(reports, at=utcnow_iso())
    if events:
        build_alert_sink(cfg)({"now_iso": utcnow_iso(), "recipe_drift": events})
    return 1 if events else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="broker-guard", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--check-config", action="store_true",
                        help="validate config and inputs, then exit")
    parser.add_argument("--diagnose-broker", metavar="NAME",
                        help="run the REAL detection legs against ONE broker "
                             "(matched by id, name, or unambiguous substring) for "
                             "every identity a scan would cover, print the full "
                             "un-collapsed outcome including any exception the "
                             "sweep would have swallowed, and exit. Read-only: "
                             "nothing is written to the state db or the dashboard")
    parser.add_argument("--check-recipes", action="store_true",
                        help="open every hand-verified search and opt-out form "
                             "recipe's page and report any selector that no "
                             "longer matches (or now matches twice), then exit. "
                             "Read-only: nothing is typed, clicked or submitted. "
                             "Drift found here raises the same recipe_drift "
                             "alert a scan would")
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

    # Overlay whatever was set through the dashboard on top of the environment
    # tier (see settings.py's precedence section). Applied here rather than
    # inside load_config so a Config stays "purely what the env said" and the
    # overlay is a visible, single step. BG_SERVE_WEB is deliberately NOT part
    # of the overlay: it is read from `cfg` below, off the environment, because
    # a toggle for it could only live in a UI that BG_SERVE_WEB=false means
    # isn't running.
    cfg = settings_mod.effective_config(cfg)

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

    if args.diagnose_broker:
        # One-shot, read-only live diagnosis of a single broker. Placed after
        # path validation (it needs brokers.json) and before every long-running
        # branch below, because it must never start the scan loop or the web
        # server -- see broker_guard/diagnose.py for why it bypasses
        # sweep._check_pair (and therefore progress/state.sqlite) entirely.
        from broker_guard import diagnose as diagnose_mod

        return diagnose_mod.run(cfg, args.diagnose_broker)

    if args.check_recipes:
        # Active rot detection, one-shot and read-only. Placed beside
        # --diagnose-broker for the same reason: it must never reach the scan
        # loop or the web server.
        return run_recipe_check(cfg)

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
