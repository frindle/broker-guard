"""Recipe-rot detection: classification, the ledger, and the alert wiring.

Every error string asserted on below is QUOTED from the module that
produces it -- ``search_probe.run_search``, ``search_forms
.classify_search_page``, ``optout_submit``, ``browser.bot_wall_reason`` --
rather than invented, because a classifier tuned against imagined text is a
classifier that quietly classifies nothing in production. Where a string is
a format template, the test builds it the same way the source does.
"""
import json

import pytest

from broker_guard import alert, orchestrator, recipe_check, recipe_health, sinks


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    # search_probe.run_search, on a field selector that matched nothing.
    "could not fill 'Last Name': TimeoutError: Page.fill: Timeout 30000ms exceeded.",
    "could not submit the search: TimeoutError: Page.click: Timeout 30000ms exceeded.",
    # search_probe.await_results
    "search did not produce a results page: no page served by thatsthem.com "
    "after submitting",
    "search did not produce a results page: results page never settled "
    "(title 'Home | Example')",
    # search_forms.classify_search_page
    "example-com results page matched no known marker (recipe may be stale)",
    "example-com printed 12 result(s) but none of the identity terms appear "
    "on the page -- refusing to guess",
    "example-com results page says both 'no results' and 'results' -- page "
    "shape not understood",
    "example-com rendered a results page with no identity term on it -- "
    "refusing to read that as either present or absent",
    "search results page was empty",
    # optout_submit: a Playwright action against an element that moved.
    "TimeoutError: Page.select_option: Timeout 30000ms exceeded.",
    "Error: strict mode violation: locator resolved to 2 elements",
    # optout_submit's post-submit classification
    "Submit was pressed but the page did not confirm the request; check the "
    "screenshot before re-trying",
    # A host that stopped resolving is a human problem, not a blip.
    "could not open search form: Error: net::ERR_NAME_NOT_RESOLVED",
    # Recipe-integrity interlocks are recipe bugs, and loud ones.
    "search recipe 'x' targets state-changing surface: remove",
    "recipe 'x' targets forbidden input(s): #website",
])
def test_a_page_that_changed_shape_is_structural(text):
    assert recipe_health.classify_failure(text) == recipe_health.STRUCTURAL
    assert recipe_health.is_recipe_drift(text)


@pytest.mark.parametrize("text", [
    "could not open search form: TimeoutError: Page.goto: Timeout 30000ms exceeded.",
    "could not open search form: Error: net::ERR_CONNECTION_REFUSED",
    "could not open search form: Error: net::ERR_CONNECTION_RESET",
    "could not open search form: Error: net::ERR_TIMED_OUT",
])
def test_a_network_blip_is_transient_and_never_alerts(text):
    assert recipe_health.classify_failure(text) == recipe_health.TRANSIENT
    assert not recipe_health.is_recipe_drift(text)


def test_a_navigation_timeout_and_a_fill_timeout_are_not_the_same_thing():
    """The heart of the module: same exception class, opposite meanings.

    ``Page.goto`` timing out says something about the network. ``Page.fill``
    timing out says the element the recipe names is not on the page -- which
    is exactly what a stale selector looks like.
    """
    goto = "TimeoutError: Page.goto: Timeout 30000ms exceeded."
    fill = "TimeoutError: Page.fill: Timeout 30000ms exceeded."
    assert recipe_health.classify_failure(goto) == recipe_health.TRANSIENT
    assert recipe_health.classify_failure(fill) == recipe_health.STRUCTURAL


@pytest.mark.parametrize("text", [
    # browser.bot_wall_reason's three shapes.
    "bot wall: HTTP 403",
    "bot wall: challenge page title 'just a moment...'",
    "bot wall: page says 'verify you are human'",
    # optout_submit's captcha stop.
    "bot check present on the form (.cf-turnstile); form was filled but NOT "
    "submitted -- finish it by hand",
])
def test_a_bot_wall_is_blocked_and_never_alerts(text):
    """ThatsThem's recipe is CORRECT and walled. Calling that 'broken' would
    train the reader to ignore the report."""
    assert recipe_health.classify_failure(text) == recipe_health.BLOCKED
    assert not recipe_health.is_recipe_drift(text)


def test_a_profile_gap_is_not_the_brokers_fault():
    text = "missing required profile fields: Age"
    assert recipe_health.classify_failure(text) == recipe_health.PROFILE
    assert not recipe_health.is_recipe_drift(text)


def test_our_own_missing_browser_is_not_the_brokers_fault():
    text = ("no browser available (is BG_PLAYWRIGHT_ENABLED on, and was the "
            "image built with INSTALL_BROWSERS=true?)")
    assert recipe_health.classify_failure(text) == recipe_health.ENVIRONMENT
    assert not recipe_health.is_recipe_drift(text)


@pytest.mark.parametrize("text", ["", None, "   ", "something nobody has seen"])
def test_an_unrecognized_failure_stays_quiet(text):
    """Wrong-by-omission beats wrong-by-guess for a thing that pages people."""
    assert recipe_health.classify_failure(text) == recipe_health.UNKNOWN
    assert not recipe_health.is_recipe_drift(text)


# --- events ------------------------------------------------------------------

def test_drift_events_keep_the_structural_errors_and_drop_the_rest():
    errors = [
        {"broker_id": "a-com", "error": "a-com: check failed: could not fill "
                                        "'Last Name': TimeoutError: Page.fill: Timeout"},
        {"broker_id": "b-com", "error": "b-com: check failed: bot wall: HTTP 403"},
        {"broker_id": "c-com", "error": "c-com: check failed: could not open "
                                        "search form: Error: net::ERR_CONNECTION_RESET"},
        "not even a dict",
    ]
    events = recipe_health.drift_events_from_errors(errors, at="2026-09-23T00:00:00Z")
    assert [e["broker_id"] for e in events] == ["a-com"]
    event = events[0]
    assert event["kind"] == recipe_health.KIND
    assert event["leg"] == recipe_health.LEG_SEARCH
    assert event["failure_class"] == recipe_health.STRUCTURAL
    # The "<broker>: check failed: " wrapper is stripped: the alert body
    # should read as the broker's own error.
    assert event["detail"].startswith("could not fill 'Last Name'")


def test_a_drift_detail_is_truncated_like_every_other_broker_string():
    event = recipe_health.drift_event("a-com", "search", "could not fill " + "x" * 5000)
    assert len(event["detail"]) <= 300


# --- the ledger --------------------------------------------------------------

def _event(broker_id, leg="search"):
    return recipe_health.drift_event(
        broker_id, leg, "could not fill 'Name': TimeoutError: Page.fill: Timeout")


def test_the_same_breakage_is_reported_once_not_every_cycle(tmp_path):
    ledger = recipe_health.DriftLedger(str(tmp_path / "drift.json"))
    assert ledger.filter_new([_event("a-com")]) != []
    assert ledger.filter_new([_event("a-com")]) == []
    assert ledger.filter_new([_event("a-com")]) == []


def test_the_two_legs_of_one_broker_are_tracked_separately(tmp_path):
    ledger = recipe_health.DriftLedger(str(tmp_path / "drift.json"))
    assert ledger.filter_new([_event("a-com", "search")]) != []
    assert ledger.filter_new([_event("a-com", "optout")]) != []


def test_a_recurrence_after_a_fix_alerts_afresh(tmp_path):
    ledger = recipe_health.DriftLedger(str(tmp_path / "drift.json"))
    ledger.filter_new([_event("a-com")])
    assert ledger.filter_new([_event("a-com")]) == []
    ledger.clear("a-com", "search")
    assert ledger.filter_new([_event("a-com")]) != []


def test_the_ledger_survives_a_restart(tmp_path):
    path = str(tmp_path / "drift.json")
    recipe_health.DriftLedger(path).filter_new([_event("a-com")])
    assert recipe_health.DriftLedger(path).filter_new([_event("a-com")]) == []


def test_a_corrupt_ledger_degrades_to_alerting_again(tmp_path):
    """The safe direction for a rot detector is a duplicate, not a silence."""
    path = tmp_path / "drift.json"
    path.write_text("{ this is not json", encoding="utf-8")
    ledger = recipe_health.DriftLedger(str(path))
    assert ledger.filter_new([_event("a-com")]) != []
    assert json.loads(path.read_text(encoding="utf-8"))


# --- the alert path ----------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, notification):
        self.calls.append(notification)
        return True


def test_a_cycles_structural_errors_become_recipe_drift_events():
    events = alert.events_from_cycle({
        "identity_key": "k", "now_iso": "2026-09-23T00:00:00Z",
        "new_appearances": ["seen-com"], "resolved": [],
        "errors": [
            {"broker_id": "a-com", "error": "a-com: check failed: could not "
                                            "fill 'Last Name': Page.fill: Timeout"},
            {"broker_id": "b-com", "error": "b-com: check failed: bot wall: HTTP 403"},
        ],
    })
    kinds = sorted({e["kind"] for e in events})
    assert kinds == ["new_appearance", "recipe_drift"]
    assert [e["broker_id"] for e in events if e["kind"] == "recipe_drift"] == ["a-com"]


def test_a_ready_made_drift_event_reaches_the_same_digest():
    """How the opt-out leg, which never runs a cycle, gets to the same sinks."""
    event = _event("a-com", recipe_health.LEG_OPTOUT)
    events = alert.events_from_cycle({"recipe_drift": [event], "now_iso": "t"})
    assert events == [event]


def test_the_notification_body_names_the_selector_to_go_and_fix():
    digest = alert.batch_digest([_event("a-com")])
    note = alert.format_notification(digest)
    assert "recipe_drift (a-com)" in note["message"]
    assert "[search]" in note["message"]
    assert "could not fill 'Name'" in note["message"]


def test_a_transient_only_cycle_delivers_nothing(tmp_path):
    """Otherwise every quiet night writes one empty notification per cycle."""
    recorder = _Recorder()
    sink = sinks.CompositeAlertSink([recorder])
    result = sink({"identity_key": "k", "now_iso": "t", "new_appearances": [],
                   "resolved": [], "errors": [
                       {"broker_id": "a-com",
                        "error": "a-com: check failed: bot wall: HTTP 403"}]})
    assert recorder.calls == []
    assert result == {"delivered": 0, "notification": None}


def test_a_structural_error_is_delivered_once_then_suppressed(tmp_path):
    recorder = _Recorder()
    ledger = recipe_health.DriftLedger(str(tmp_path / "drift.json"))
    sink = sinks.CompositeAlertSink([recorder], drift_ledger=ledger)
    payload = {"identity_key": "k", "now_iso": "t", "new_appearances": [],
               "resolved": [], "current": [], "errors": [
                   {"broker_id": "a-com", "error": "a-com: check failed: could "
                                                   "not fill 'Name': Page.fill: Timeout"}]}
    sink(payload)
    assert len(recorder.calls) == 1
    sink(payload)
    assert len(recorder.calls) == 1


def test_a_broker_that_checks_cleanly_is_forgiven_its_past_drift(tmp_path):
    recorder = _Recorder()
    ledger = recipe_health.DriftLedger(str(tmp_path / "drift.json"))
    sink = sinks.CompositeAlertSink([recorder], drift_ledger=ledger)
    broken = {"identity_key": "k", "now_iso": "t", "current": [], "errors": [
        {"broker_id": "a-com",
         "error": "a-com: check failed: could not fill 'Name': Page.fill: Timeout"}]}
    sink(broken)
    sink({"identity_key": "k", "now_iso": "t", "current": ["a-com"],
          "new_appearances": ["a-com"], "resolved": [], "errors": []})
    sink(broken)
    drift_calls = [c for c in recorder.calls
                   if any(i.get("kind") == recipe_health.KIND for i in c["items"])]
    assert len(drift_calls) == 2


# --- run_cycle hands the errors over -----------------------------------------

class _Store:
    def __init__(self):
        self.seen = set()

    def is_seen(self, _identity, broker_id):
        return broker_id in self.seen

    def record_appearance(self, _identity, broker_id, _now):
        self.seen.add(broker_id)

    def seen_brokers(self, _identity):
        return sorted(self.seen)


def test_run_cycle_hands_its_errors_to_the_sink():
    """The bucket a rotted recipe falls into must not be a bucket nobody sees."""
    sent = []
    brokers = [{"id": "a-com"}]

    def checker(_broker, _identity):
        raise RuntimeError("could not fill 'Name': Page.fill: Timeout")

    result = orchestrator.run_cycle("k", brokers, checker, _Store(),
                                    sent.append, "2026-09-23T00:00:00Z")
    assert len(sent) == 1
    assert sent[0]["errors"][0]["broker_id"] == "a-com"
    # ...but an errors-only cycle still did not report anything about a
    # LISTING, and the flag keeps its old meaning.
    assert result["alerts_sent"] is False


def test_a_clean_quiet_cycle_still_calls_nobody():
    sent = []
    orchestrator.run_cycle("k", [], lambda *_: False, _Store(), sent.append, "t")
    assert sent == []


# --- the active probe --------------------------------------------------------

class FakeProbePage:
    """Minimal page: counts matches per selector from a fixed table."""

    def __init__(self, counts, text="a real page", title="Broker", raise_on_goto=None):
        self.counts = counts
        self._text = text
        self._title = title
        self._raise = raise_on_goto
        self.closed = False

    def goto(self, _url, **_kw):
        if self._raise:
            raise self._raise

    def wait_for_timeout(self, _ms):
        pass

    def inner_text(self, _sel):
        return self._text

    def title(self):
        return self._title

    def query_selector_all(self, selector):
        return [object()] * self.counts.get(selector, 0)

    def close(self):
        self.closed = True


def test_the_probe_reports_a_selector_that_matches_nothing():
    page = FakeProbePage({"#first": 1, "#submit": 1})
    out = recipe_check.check_page(
        page, "https://x.invalid/", [("#first", "First"), ("#gone", "Last"),
                                     ("#submit", "Submit button")])
    assert out["status"] == "drift"
    assert out["missing"] == ["Last [#gone]"]
    assert out["checked"] == 3


def test_the_probe_reports_a_selector_that_now_matches_twice():
    """Playwright's strict mode turns a second match into a hard failure --
    the classic outcome of a site adding a mobile copy of its own form."""
    page = FakeProbePage({"#first": 2})
    out = recipe_check.check_page(page, "https://x.invalid/", [("#first", "First")])
    assert out["status"] == "drift"
    assert out["ambiguous"] == ["First [#first] matches 2"]


def test_the_probe_calls_an_intact_page_ok():
    page = FakeProbePage({"#first": 1, "#submit": 1})
    out = recipe_check.check_page(page, "https://x.invalid/",
                                  [("#first", "First"), ("#submit", "Submit button")])
    assert out["status"] == "ok" and out["missing"] == [] and out["ambiguous"] == []


def test_the_probe_does_not_call_a_walled_page_broken():
    page = FakeProbePage({}, text="Please verify you are human to continue.")
    out = recipe_check.check_page(page, "https://x.invalid/", [("#first", "First")])
    assert out["status"] == recipe_health.BLOCKED
    assert out["missing"] == []


def test_the_probe_does_not_call_an_unreachable_page_broken():
    page = FakeProbePage({}, raise_on_goto=TimeoutError(
        "Page.goto: Timeout 30000ms exceeded."))
    out = recipe_check.check_page(page, "https://x.invalid/", [("#first", "First")])
    assert out["status"] == recipe_health.TRANSIENT


def test_the_probe_covers_every_selector_a_recipe_would_touch():
    """Including the submit button, and including a step written as a
    Choice/Select/Check rather than a Field."""
    from broker_guard import optout_forms, search_forms

    for recipe in search_forms.RECIPES.values():
        pairs = recipe_check.selectors_for_search(recipe)
        selectors = {s for s, _ in pairs}
        assert recipe.submit_selector in selectors
        for field in recipe.fields:
            assert field.selector in selectors

    for recipe in optout_forms.RECIPES.values():
        selectors = {s for s, _ in recipe_check.selectors_for_optout(recipe)}
        assert recipe.submit_selector in selectors
        for step in optout_forms.ordered_steps(recipe):
            target = getattr(step, "selector", None) or getattr(step, "container", None)
            if getattr(step, "appears_later", False):
                # Declared conditional: it does not exist on the page this
                # probe opens, so being absent from the sweep is the point.
                assert target not in selectors
                continue
            assert target in selectors


def test_a_conditional_field_is_not_probed_for_and_must_say_so():
    """The false-alarm case that nearly trained Penn to ignore the report.

    Nielsen's request-type, State and Zip controls do not exist in the DOM
    until Country is filled -- which the recipe's own notes said long before
    --check-recipes existed. The probe fills nothing, so it saw three
    perfectly healthy selectors as MISSING and raised recipe_drift for a
    recipe that works. Skipping is therefore not a convenience: it is the
    difference between an alert that means something and one that does not.
    """
    from broker_guard import optout_forms

    nielsen = optout_forms.RECIPES["onetrust-com"]
    conditional = {getattr(s, "selector", None) or getattr(s, "container", None)
                   for s in optout_forms.ordered_steps(nielsen)
                   if getattr(s, "appears_later", False)}
    assert conditional == {"#requestTypesDSARElement", "#stateDSARElement",
                           "#zipDSARElement"}

    probed = {s for s, _ in recipe_check.selectors_for_optout(nielsen)}
    assert not probed.intersection(conditional)
    # ...and the unconditional ones are still every bit as covered.
    assert {"#subjectTypesDSARElement", "#countryDSARElement",
            "#firstNameDSARElement", "#lastNameDSARElement",
            "#emailDSARElement", nielsen.submit_selector} <= probed


def test_the_probe_never_touches_a_forbidden_selector():
    """A honeypot must not be queried into existence by the health check
    either -- the probe only ever looks at what the recipe targets."""
    from broker_guard import optout_forms

    for recipe in optout_forms.RECIPES.values():
        selectors = {s for s, _ in recipe_check.selectors_for_optout(recipe)}
        assert not selectors.intersection(recipe.forbidden_selectors or ())


def test_drift_found_by_the_probe_becomes_the_same_alert_event():
    reports = [
        {"status": "drift", "broker_id": "a-com", "leg": "optout",
         "missing": ["Last [#gone]"], "ambiguous": []},
        {"status": "ok", "broker_id": "b-com", "leg": "search",
         "missing": [], "ambiguous": []},
        {"status": recipe_health.BLOCKED, "broker_id": "c-com", "leg": "search",
         "missing": [], "ambiguous": []},
    ]
    events = recipe_check.drift_events(reports, at="t")
    assert [e["broker_id"] for e in events] == ["a-com"]
    assert events[0]["failure_class"] == recipe_health.STRUCTURAL
    assert "Last [#gone]" in events[0]["detail"]


def test_check_all_closes_every_page_it_opens():
    from broker_guard import search_forms

    opened = []

    def new_page():
        page = FakeProbePage({})
        opened.append(page)
        return page

    reports = recipe_check.check_all(new_page, search_recipes=search_forms.RECIPES,
                                     optout_recipes={})
    assert len(reports) == len(search_forms.RECIPES)
    assert opened and all(p.closed for p in opened)
