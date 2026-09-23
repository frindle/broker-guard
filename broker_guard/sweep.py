"""The detection sweep: every broker checked against EVERY profile, with a
bounded retry pass for the (broker, profile) pairs a rate-limit or bot wall
left unknown.

Why this module exists, and why the loop is nested THIS way
-----------------------------------------------------------
The old sweep (``service.build_presence_checker``) was per-identity: run
the whole SERP leg over all ~827 brokers for one person, then the whole
browser leg, then diff. With every profile now scanned every cycle (see
``profiles.py`` -- there is no "active profile" any more), the naive
extension is to run that entire per-identity sweep N times, once per
person. This module deliberately does NOT do that. The loop here is:

    for broker in brokers:          # outer
        for identity in profiles:   # inner

so a broker is checked for the whole household before the sweep moves
on. That ordering is what makes the retry below possible and cheap. Two
signals this codebase already computes tell us a check failed because the
BROKER is pushing back, not because the person is absent:

* ``browser.bot_wall_reason`` -- the broker's own page answered with a
  challenge/429/403 instead of content, and
* ``searx_client``'s ``unresponsive_engines`` threshold -- the search
  backend answered "nothing" while the majority of its engines were
  unresponsive, i.e. nothing was actually searched.

Both already resolve to "unknown, not absent" (``PresenceUnknown`` ->
``run_cycle`` ``errors`` -> excluded from ``resolved``, so a block can
never be mistaken for a removal). That safety net is unchanged and still
the final word. What was missing is that a block hit on profile B, one
second after profile A checked cleanly, was accepted as this cycle's
answer for B and never looked at again until tomorrow -- even though the
rate limit that caused it is typically over in minutes.

Retry policy (bounded, deliberately modest)
--------------------------------------------
Pairs whose presence came back UNKNOWN are queued, per broker, as
``(broker, [identity, ...])`` -- only the incomplete profiles, never the
ones that already answered. When the main pass ends, the queue is
re-checked at most ``DEFAULT_RETRY_PASSES`` (2) times, with a doubling
backoff starting at ``DEFAULT_RETRY_BACKOFF_S`` (60s), so the rest of the
broker list -- hundreds of other domains -- has run in between and any
per-domain limiter has had real time to drain. Anything still unknown
after that exhausts to the existing safety net, which is already correct;
it is simply reached after a genuine retry instead of immediately.

The bound is a hard count, not a deadline: a persistently blocked broker
costs at most two extra checks per incomplete profile and then stops,
so a broker that is down all day cannot turn one cycle into an infinite
loop.

Stopping
--------
``progress.ScanProgress``'s cooperative stop flag is polled between
brokers in the main pass, between brokers in each retry pass, and before
each retry pass starts -- so "Stop scan" cancels the pending retries too,
not just the main walk. Everything already checked stays recorded;
everything never reached stays UNKNOWN (the checkers below raise
``PresenceUnknown`` for a pair that was never attempted), which is what
keeps a stopped scan from ever reading as "these brokers are clean now".
"""
import logging
import time
from dataclasses import dataclass, field

from broker_guard import playwright_checks, profile as profile_mod
from broker_guard import progress as progress_mod, serpwatch

log = logging.getLogger("broker_guard.sweep")

#: How many extra passes the incomplete queue gets after the main walk.
DEFAULT_RETRY_PASSES = 2

#: Seconds before the FIRST retry pass; doubled for each pass after it.
#: Injectable via ``run_sweep(sleep=...)`` so tests never really wait.
DEFAULT_RETRY_BACKOFF_S = 60


@dataclass
class PairResult:
    """What this cycle learned about one (broker, profile) pair."""

    broker_id: str
    identity_key: str
    #: False until the pair has actually been attempted. A pair the sweep
    #: never reached (stopped early) must read as UNKNOWN, never as clean.
    reached: bool = False
    serp_hit: bool = False
    serp_error: bool = False
    #: None when the browser leg had no opinion at all (disabled, or this
    #: broker is not automatable); otherwise the normalized check result.
    browser: dict | None = None
    #: Which retry pass last touched this pair (0 = the main pass).
    attempts: int = 0

    @property
    def unknown(self) -> bool:
        """Would the presence checker raise ``PresenceUnknown`` for this
        pair? Mirrors ``presence_checker`` below exactly -- the retry queue
        must be built from what the CHECKER concludes, not from the
        display outcome, or a broker whose browser leg answered cleanly
        would be retried forever over an irrelevant SERP error."""
        if not self.reached:
            return True
        if self.serp_hit:
            return False
        if self.browser is not None:
            return not self.browser["checked"]
        return self.serp_error


@dataclass
class SweepResult:
    """Everything one sweep produced, for every profile it covered."""

    pairs: dict = field(default_factory=dict)   # (identity_key, broker_id) -> PairResult
    stats: dict = field(default_factory=dict)   # identity_key -> per-leg tallies
    stopped: bool = False
    retried_pairs: int = 0
    unresolved_pairs: int = 0

    def checker_for(self, identity_key: str):
        """The ``presence_checker(broker, identity_key)`` predicate for one
        profile, in exactly the shape ``orchestrator.run_cycle`` wants.

        Precedence is identical to the single-identity
        ``service.build_presence_checker``: a SERP hit is a hit; a browser
        leg that CHECKED the site decides (a successful direct read beats
        the search index); a browser leg that errored is unknown; a SERP
        failure with no browser opinion is unknown; anything else is
        absent. The one addition is the first clause -- a pair this sweep
        never attempted is unknown, which is what keeps a stopped or
        exhausted sweep from reporting untouched brokers as resolved.
        """
        from broker_guard.service import PresenceUnknown

        def presence_checker(broker, _identity_key=None):
            broker_id = broker["id"]
            pair = self.pairs.get((identity_key, broker_id))
            if pair is None or not pair.reached:
                raise PresenceUnknown(
                    "{}: not checked this cycle; presence unknown".format(broker_id)
                )
            if pair.serp_hit:
                return True
            if pair.browser is not None:
                if pair.browser["checked"]:
                    return pair.browser["present"]
                raise PresenceUnknown(
                    "{}: check failed: {}".format(broker_id, pair.browser["error"])
                )
            if pair.serp_error:
                raise PresenceUnknown(
                    "{}: SERP check failed; presence unknown".format(broker_id)
                )
            return False

        leg_stats = self.stats.get(identity_key, {})
        presence_checker.serp_stats = leg_stats.get("serp", _empty_stats())
        presence_checker.browser_stats = leg_stats.get("browser", _empty_stats())
        return presence_checker


def _empty_stats() -> dict:
    return {"hit": 0, "checked": 0, "error": 0, "skipped": 0}


def _check_serp(broker: dict, identity, deps) -> tuple:
    """The SERP leg for ONE (broker, profile) pair -> ``(outcome, hits)``.

    ``serpwatch.run_serpwatch`` is called with a one-broker list rather
    than reimplemented: the query building, the domain scoping, the
    per-query error accounting and the PII-free logging are all decisions
    that already live there and must not get a second, drifting copy.
    """
    if deps.searx_search is None:
        return None, 0

    seen = {}

    def _observe(broker_id, outcome, hits=0, errors=0):
        seen["outcome"] = outcome
        seen["hits"] = hits

    hits = serpwatch.run_serpwatch(
        [broker], identity.phones, identity.emails,
        profile_mod.name_variants(identity), identity.addresses,
        deps.searx_search, observer=_observe,
    )
    return seen.get("outcome"), len(hits)


def _check_browser(broker: dict, identity, deps) -> "dict | None":
    """The browser leg for ONE (broker, profile) pair, or None when it has
    no opinion (leg disabled, or this broker has no automatable check)."""
    if deps.page_action is None:
        return None
    terms = (profile_mod.name_variants(identity) + list(identity.phones)
             + list(identity.emails))
    checks = playwright_checks.build_site_checks([broker], terms)
    if not checks:
        return None
    results = playwright_checks.run_playwright_checks(checks, deps.page_action)
    return results.get(broker["id"])


def _display_outcome(pair: PairResult, serp_outcome: "str | None") -> str:
    """The single per-broker outcome shown on /brokers for this pair.

    Merged across the two legs by ``progress.OUTCOME_RANK`` -- the same
    precedence the per-leg merge used when the legs ran as separate
    phases, so nothing about what the page displays changes here.
    """
    candidates = []
    if serp_outcome:
        candidates.append(serp_outcome)
    if pair.browser is not None:
        if not pair.browser["checked"]:
            candidates.append("error")
        elif pair.browser["present"]:
            candidates.append("hit")
        else:
            candidates.append("checked")
    if not candidates:
        return "skipped"
    return max(candidates, key=lambda o: progress_mod.OUTCOME_RANK[o])


def _check_pair(broker: dict, identity, deps, progress, stats: dict,
                replace: bool = False) -> PairResult:
    """Check one (broker, profile) pair and record it against *progress*.

    *replace* is the retry path: the pair already has an entry and an
    aggregate tick from an earlier attempt, so the entry is OVERWRITTEN
    (a retry that now succeeds must not stay outranked by the error it
    replaces) and the aggregate counters are left alone (the pair is not
    a second broker to check, it is the same one re-examined).
    """
    pair = PairResult(broker_id=str(broker.get("id") or ""),
                      identity_key=identity.identity_key, reached=True)
    serp_outcome, serp_hits = _check_serp(broker, identity, deps)
    if serp_outcome is not None:
        stats["serp"][serp_outcome] = stats["serp"].get(serp_outcome, 0) + 1
        pair.serp_hit = serp_outcome == "hit"
        pair.serp_error = serp_outcome == "error"

    pair.browser = _check_browser(broker, identity, deps)
    if pair.browser is not None:
        browser_outcome = ("error" if not pair.browser["checked"]
                           else "hit" if pair.browser["present"] else "checked")
        stats["browser"][browser_outcome] = stats["browser"].get(browser_outcome, 0) + 1

    outcome = _display_outcome(pair, serp_outcome)
    progress.set_identity(identity.identity_key)
    progress.record_outcome(
        pair.broker_id, outcome,
        hits=max(serp_hits, 1) if outcome == "hit" else 0,
        errors=1 if outcome == "error" else 0,
        replace=replace,
    )
    return pair


def _scan_order(brokers: list, identities: list, deps) -> list:
    """``service.order_brokers_for_scan`` extended to a household.

    A broker counts as "known" only when EVERY profile already has a
    presence record for it -- a broker one person has never been checked
    against is still somewhere a new listing can turn up, so it belongs in
    the unknown-first head of the list. Same single ordering policy,
    applied to the intersection rather than to one person's set.
    """
    from broker_guard.service import _known_broker_ids, order_brokers_for_scan

    known = None
    for identity in identities:
        ids = _known_broker_ids(deps, identity.identity_key)
        known = ids if known is None else (known & ids)
    return order_brokers_for_scan(brokers, known or set())


def run_sweep(identities: list, brokers: list, deps, cfg=None, progress=None,
              retry_passes: int = DEFAULT_RETRY_PASSES,
              backoff_s: float = DEFAULT_RETRY_BACKOFF_S, sleep=None,
              order=None) -> SweepResult:
    """Check every broker against every identity, then retry what was left
    unknown. See the module docstring for the loop order and retry policy.

    *deps* is a ``service.Dependencies``-shaped object (``searx_search``,
    ``page_action``, ``store``). *progress* defaults to the process-wide
    ``ScanProgress``; pass an isolated one in a test. *sleep* and *order*
    are injection seams for the retry backoff and the broker ordering, so
    a test neither waits a real minute nor depends on a shuffle.
    """
    sleep = sleep if sleep is not None else time.sleep
    progress = progress if progress is not None else progress_mod.current()
    identities = list(identities)
    broker_list = list(order) if order is not None else list(brokers)

    result = SweepResult()
    if not identities or not broker_list:
        return result

    if order is None:
        broker_list = _scan_order(broker_list, identities, deps)

    stats = {i.identity_key: {"serp": _empty_stats(), "browser": _empty_stats()}
             for i in identities}
    result.stats = stats

    progress.begin_scan(identity_keys=[i.identity_key for i in identities],
                        total=len(broker_list) * len(identities))
    progress.start(progress_mod.PHASE_SWEEP, len(broker_list) * len(identities))

    # (broker, [identity, ...]) -- only the profiles this broker left
    # unknown, never the ones that already answered.
    retry_queue = []
    try:
        for broker in broker_list:
            if progress.should_stop():
                result.stopped = True
                break
            incomplete = []
            for identity in identities:
                pair = _check_pair(broker, identity, deps, progress,
                                   stats[identity.identity_key])
                result.pairs[(identity.identity_key, pair.broker_id)] = pair
                if pair.unknown:
                    incomplete.append(identity)
            if incomplete:
                retry_queue.append((broker, incomplete))

        delay = backoff_s
        for attempt in range(1, retry_passes + 1):
            if not retry_queue or progress.should_stop():
                if retry_queue and progress.should_stop():
                    result.stopped = True
                break
            log.info("retrying brokers that blocked mid-cycle", extra={
                "pass": attempt, "brokers": len(retry_queue),
                "pairs": sum(len(ids) for _, ids in retry_queue),
                "delay_s": delay,
            })
            sleep(delay)
            delay *= 2
            still_blocked = []
            for broker, idents in retry_queue:
                if progress.should_stop():
                    result.stopped = True
                    break
                remaining = []
                for identity in idents:
                    pair = _check_pair(broker, identity, deps, progress,
                                       stats[identity.identity_key], replace=True)
                    pair.attempts = attempt
                    result.pairs[(identity.identity_key, pair.broker_id)] = pair
                    result.retried_pairs += 1
                    if pair.unknown:
                        remaining.append(identity)
                if remaining:
                    still_blocked.append((broker, remaining))
            retry_queue = still_blocked
    finally:
        progress.finish()
        if result.stopped:
            progress.mark_stopped()

    result.unresolved_pairs = sum(1 for p in result.pairs.values() if p.unknown)
    log.info("sweep complete", extra={
        "brokers": len(broker_list), "profiles": len(identities),
        "retried_pairs": result.retried_pairs,
        "unresolved_pairs": result.unresolved_pairs,
        "stopped": result.stopped,
    })
    return result
