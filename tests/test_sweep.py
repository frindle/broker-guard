"""Tests for broker_guard/sweep.py -- the broker-outer/profile-inner scan,
its bounded retry of rate-limited pairs, and the cooperative stop.

The invariant every test here protects is the one the whole tool rests on:
a check that did not happen must never be reportable as "clean". The
sweep can end early (stopped), be blocked (bot wall / dead SearXNG) or
simply never reach a broker, and in every one of those cases the presence
checker it hands back must raise PresenceUnknown rather than return False
-- because False flows through orchestrator.run_cycle into `resolved`,
which makes autopilot call store.forget().
"""
import pytest

from broker_guard import profile as profile_mod
from broker_guard import progress as progress_mod
from broker_guard import service
from broker_guard import sweep as sweep_mod
from broker_guard.searx_client import SearxError

from conftest import FAKE_EMAIL, FAKE_PHONE


BROKERS = [
    {"id": "alpha", "name": "Alpha People", "url": "https://alpha.invalid",
     "verification": "captcha"},
    {"id": "beta", "name": "Beta Search", "url": "https://beta.invalid",
     "verification": "captcha"},
    {"id": "gamma", "name": "Gamma Records", "url": "https://gamma.invalid",
     "verification": "captcha"},
]


def _identity(first, last):
    return profile_mod.Identity(first_name=first, last_name=last,
                                emails=[FAKE_EMAIL], phones=[FAKE_PHONE])


class _Deps:
    """The two seams run_sweep actually touches, nothing more. Not
    service.Dependencies: these tests are about loop order and retry, and a
    real Dependencies would drag a sqlite store in for no reason."""

    def __init__(self, searx_search=None, page_action=None):
        self.searx_search = searx_search
        self.page_action = page_action


def _progress():
    return progress_mod.ScanProgress(now=lambda: "T0")


# --- loop order -----------------------------------------------------------

def test_the_outer_loop_is_brokers_and_the_inner_loop_is_profiles():
    """The ordering the retry depends on: a broker is checked for the whole
    household before the sweep moves on, so a mid-broker block is visible
    as "this broker blocked on these profiles" rather than being smeared
    across a per-person pass that ended hours earlier."""
    seen = []
    ann, bob = _identity("Ann", "Example"), _identity("Bob", "Example")

    def search(query):
        seen.append(query)
        return []

    sweep_mod.run_sweep([ann, bob], BROKERS, _Deps(searx_search=search),
                        progress=_progress(), order=BROKERS, sleep=lambda s: None)

    # One (broker, profile) pair issues several queries; collapse each one
    # to the (broker, person) pair it belongs to and drop consecutive
    # duplicates, leaving the order the pairs were visited in.
    visited = []
    for query in seen:
        who = "Ann" if "Ann" in query else "Bob" if "Bob" in query else None
        which = next((b["id"] for b in BROKERS if b["url"].split("//")[1] in query), None)
        if who and which and (not visited or visited[-1] != (which, who)):
            visited.append((which, who))
    assert visited == [
        ("alpha", "Ann"), ("alpha", "Bob"),
        ("beta", "Ann"), ("beta", "Bob"),
        ("gamma", "Ann"), ("gamma", "Bob"),
    ], visited


def test_every_profile_gets_its_own_pair_for_every_broker():
    ann, bob = _identity("Ann", "Example"), _identity("Bob", "Example")
    result = sweep_mod.run_sweep([ann, bob], BROKERS,
                                 _Deps(searx_search=lambda q: []),
                                 progress=_progress(), order=BROKERS,
                                 sleep=lambda s: None)
    assert set(result.pairs) == {
        (i.identity_key, b["id"]) for i in (ann, bob) for b in BROKERS
    }


def test_one_profiles_hit_is_never_another_profiles_hit():
    """The whole reason the progress map is keyed by (identity, broker)."""
    ann, bob = _identity("Ann", "Example"), _identity("Bob", "Example")

    def search(query):
        if "Ann" in query:
            return [{"url": "https://alpha.invalid/p/ann", "title": "Ann Example"}]
        return []

    progress = _progress()
    result = sweep_mod.run_sweep([ann, bob], BROKERS, _Deps(searx_search=search),
                                 progress=progress, order=BROKERS, sleep=lambda s: None)

    assert result.checker_for(ann.identity_key)(BROKERS[0]) is True
    assert result.checker_for(bob.identity_key)(BROKERS[0]) is False

    entries = progress.snapshot(include_brokers=True)["brokers"]
    assert entries[progress_mod.entry_key(ann.identity_key, "alpha")]["outcome"] == "hit"
    assert entries[progress_mod.entry_key(bob.identity_key, "alpha")]["outcome"] == "checked"


# --- the bounded retry ----------------------------------------------------

def _blocking_search(blocked_until_call, broker_host="alpha.invalid"):
    """A SearXNG that rate-limits one broker for the first N calls that
    touch it, then recovers -- the shape searx_client's unresponsive-engine
    threshold produces."""
    state = {"calls": 0}

    def search(query):
        if broker_host in query:
            state["calls"] += 1
            if state["calls"] <= blocked_until_call:
                raise SearxError("majority of engines unresponsive")
        return []

    search.state = state
    return search


def test_a_blocked_pair_is_retried_after_the_rest_of_the_broker_list():
    ann = _identity("Ann", "Example")
    search = _blocking_search(blocked_until_call=1)
    slept = []

    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=_progress(), order=BROKERS,
                                 sleep=slept.append)

    assert result.retried_pairs == 1
    assert slept == [sweep_mod.DEFAULT_RETRY_BACKOFF_S], "one pass, one backoff"
    # The retry succeeded, so the pair is no longer unknown...
    assert result.checker_for(ann.identity_key)(BROKERS[0]) is False
    assert result.unresolved_pairs == 0


def test_only_the_incomplete_profiles_are_retried_not_the_whole_broker():
    """Ann answered cleanly on alpha; only Bob's blocked pair goes back in
    the queue. Retrying Ann too would double the load on a broker that is
    already rate-limiting us."""
    ann, bob = _identity("Ann", "Example"), _identity("Bob", "Example")
    calls = []

    def search(query):
        calls.append(query)
        if "alpha.invalid" in query and "Bob" in query and len(calls) < 20:
            raise SearxError("majority of engines unresponsive")
        return []

    result = sweep_mod.run_sweep([ann, bob], BROKERS, _Deps(searx_search=search),
                                 progress=_progress(), order=BROKERS,
                                 sleep=lambda s: None)

    retried = [p for p in result.pairs.values() if p.attempts]
    assert [p.identity_key for p in retried] == [bob.identity_key]
    assert [p.broker_id for p in retried] == ["alpha"]


def test_the_retry_is_bounded_and_then_falls_back_to_unknown():
    """A persistently blocked broker costs a fixed number of extra checks
    and then exhausts to the existing safety net -- it can never turn one
    cycle into an unbounded loop."""
    ann = _identity("Ann", "Example")

    def search(query):
        if "alpha.invalid" in query:
            raise SearxError("majority of engines unresponsive")
        return []

    slept = []
    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=_progress(), order=BROKERS,
                                 sleep=slept.append)

    assert result.retried_pairs == sweep_mod.DEFAULT_RETRY_PASSES
    assert slept == [sweep_mod.DEFAULT_RETRY_BACKOFF_S,
                     sweep_mod.DEFAULT_RETRY_BACKOFF_S * 2], "backoff doubles"
    assert result.unresolved_pairs == 1

    # ...and the safety net is what it exhausts TO: unknown, never absent.
    with pytest.raises(service.PresenceUnknown):
        result.checker_for(ann.identity_key)(BROKERS[0])


def test_a_successful_retry_replaces_the_error_it_recovered_from():
    """Outcomes are rank-merged within a cycle (error outranks checked), so
    a retry has to REPLACE the entry -- otherwise a broker that recovered
    would still display as a failed check."""
    ann = _identity("Ann", "Example")
    progress = _progress()
    sweep_mod.run_sweep([ann], BROKERS,
                        _Deps(searx_search=_blocking_search(blocked_until_call=1)),
                        progress=progress, order=BROKERS, sleep=lambda s: None)

    entry = progress.snapshot(include_brokers=True)["brokers"][
        progress_mod.entry_key(ann.identity_key, "alpha")]
    assert entry["outcome"] == "checked"
    assert entry["retried"] is True


# --- stopping -------------------------------------------------------------

def test_stop_ends_the_main_pass_between_brokers():
    ann = _identity("Ann", "Example")
    progress = _progress()
    touched = []

    def search(query):
        touched.append(query)
        progress.request_stop()
        return []

    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=progress, order=BROKERS,
                                 sleep=lambda s: None)

    assert result.stopped is True
    # The broker being checked when stop arrived is finished, not abandoned
    # mid-way; the sweep stops BEFORE the next one.
    assert {b for (_key, b) in result.pairs} == {"alpha"}
    assert progress.snapshot()["stopped"] is True


def test_a_stopped_sweep_never_reports_an_unreached_broker_as_clean():
    """The safety-net regression this feature could most easily break: a
    broker the sweep never got to must raise PresenceUnknown, because a
    False here becomes `resolved` and then store.forget()."""
    ann = _identity("Ann", "Example")
    progress = _progress()

    def search(query):
        progress.request_stop()
        return []

    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=progress, order=BROKERS,
                                 sleep=lambda s: None)
    checker = result.checker_for(ann.identity_key)

    for broker in BROKERS[1:]:
        with pytest.raises(service.PresenceUnknown):
            checker(broker)


def test_stop_cancels_the_pending_retry_passes_too():
    """Stopping has to cancel queued retries, not just the main walk --
    otherwise "Stop scan" would sit there for two backoff periods still
    hitting brokers."""
    ann = _identity("Ann", "Example")
    progress = _progress()

    def search(query):
        if "alpha.invalid" in query:
            raise SearxError("majority of engines unresponsive")
        if "gamma.invalid" in query:
            progress.request_stop()   # main pass is done after this broker
        return []

    slept = []
    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=progress, order=BROKERS,
                                 sleep=slept.append)

    assert result.stopped is True
    assert result.retried_pairs == 0, "no retry ran"
    assert slept == [], "and no backoff was waited out"
    with pytest.raises(service.PresenceUnknown):
        result.checker_for(ann.identity_key)(BROKERS[0])


def test_results_recorded_before_a_stop_are_kept():
    ann = _identity("Ann", "Example")
    progress = _progress()

    def search(query):
        if "beta.invalid" in query:
            progress.request_stop()
        if "alpha.invalid" in query:
            return [{"url": "https://alpha.invalid/p/ann", "title": "Ann Example"}]
        return []

    result = sweep_mod.run_sweep([ann], BROKERS, _Deps(searx_search=search),
                                 progress=progress, order=BROKERS,
                                 sleep=lambda s: None)

    assert result.stopped is True
    assert result.checker_for(ann.identity_key)(BROKERS[0]) is True
    entries = progress.snapshot(include_brokers=True)["brokers"]
    assert entries[progress_mod.entry_key(ann.identity_key, "alpha")]["outcome"] == "hit"


# --- degenerate input -----------------------------------------------------

def test_no_profiles_means_no_sweep_and_no_error():
    result = sweep_mod.run_sweep([], BROKERS, _Deps(searx_search=lambda q: []),
                                 progress=_progress(), sleep=lambda s: None)
    assert result.pairs == {} and result.stopped is False


def test_no_brokers_means_no_sweep_and_no_error():
    result = sweep_mod.run_sweep([_identity("Ann", "Example")], [],
                                 _Deps(searx_search=lambda q: []),
                                 progress=_progress(), sleep=lambda s: None)
    assert result.pairs == {}
