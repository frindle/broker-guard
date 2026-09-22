"""Detection failures must not be able to look like a verified absence.

Two real failure modes used to sail past every safety net in the pipeline by
returning a perfectly ordinary "not present":

* SearXNG answering ``200 OK`` with ``results: []`` for every query because its
  upstream engines are all rate-limited or CAPTCHA-walled; and
* a broker's site serving a Cloudflare/CAPTCHA challenge page, which navigates
  fine and simply does not contain the identity terms.

Neither raised, so ``orchestrator.run_cycle``'s error-exclusion (which is what
keeps a transient failure from being reported as ``resolved``) never saw them,
and ``autopilot.run_scan_cycle`` went on to ``store.forget()`` the presence row
of a broker that is still publishing the person's PII.

These tests are deliberately written at two levels: the detectors in isolation,
AND the whole chain through ``run_cycle`` / ``run_scan_cycle`` with a store that
records whether ``forget`` was called -- because "the client raises" is only
half the property; "the raise actually reaches the safety net" is the other.

All identity data here is FAKE (see tests/conftest.py).
"""
import pytest

from broker_guard import autopilot, browser, playwright_checks, service
from broker_guard.browser import PlaywrightChecker, bot_wall_reason
from broker_guard.orchestrator import run_cycle
from broker_guard.profile import Identity
from broker_guard.searx_client import (
    SearxClient,
    SearxError,
    unresponsive_engine_names,
)

from tests.test_autopilot import FakeStore


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """Returns the same payload for every request; records the calls."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        return FakeResponse(self.payload, self.status_code)


def make_client(payload, **kwargs):
    kwargs.setdefault("attempts", 1)
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("min_interval_s", 0)
    kwargs.setdefault("jitter_s", 0)
    return SearxClient("https://searx.invalid", session=FakeSession(payload), **kwargs)


ENGINES = "brave,google,duckduckgo,startpage,wikipedia"


def outage_payload(engines=ENGINES.split(",")):
    """What SearXNG returns when its upstreams are all walled: a valid 200
    with nothing in it, and every engine listed as unresponsive."""
    return {
        "query": "redacted",
        "number_of_results": 0,
        "results": [],
        # SearXNG serializes UnresponsiveEngine via get_translated_errors as
        # (engine, translated message) pairs -> JSON arrays of two strings.
        "unresponsive_engines": [[e, "Too many requests"] for e in engines],
    }


def healthy_empty_payload():
    """A genuine "this person is not listed anywhere" answer."""
    return {"query": "redacted", "number_of_results": 0,
            "results": [], "unresponsive_engines": []}


# --------------------------------------------------------------------------
# 1. SearXNG systemic blindness -- the detector
# --------------------------------------------------------------------------

def test_empty_results_with_all_configured_engines_down_raises():
    client = make_client(outage_payload(), engines=ENGINES)
    with pytest.raises(SearxError) as excinfo:
        client("site:alpha.invalid Testy")
    assert "unresponsive" in str(excinfo.value)


def test_empty_results_with_healthy_engines_does_not_raise():
    """The regression that matters most: a real clean result must still work.

    If this ever starts raising, every not-listed broker becomes an error and
    nothing can ever be reported resolved again.
    """
    client = make_client(healthy_empty_payload(), engines=ENGINES)
    assert client("site:alpha.invalid Testy") == []


def test_one_flaky_engine_of_five_is_not_an_outage():
    """SearXNG is designed to survive a single dead engine by falling back to
    the others -- treating that as an outage would be its own signal-destroying
    false positive."""
    payload = outage_payload(["brave"])
    client = make_client(payload, engines=ENGINES)
    assert client("site:alpha.invalid Testy") == []


def test_partial_outage_below_threshold_with_engines_unset_does_not_raise():
    payload = outage_payload(["brave", "google"])
    client = make_client(payload)  # engines unset -> count threshold (3)
    assert client("site:alpha.invalid Testy") == []


def test_threshold_many_engines_down_with_engines_unset_raises():
    payload = outage_payload(["brave", "google", "duckduckgo"])
    client = make_client(payload)
    with pytest.raises(SearxError):
        client("site:alpha.invalid Testy")


def test_non_empty_results_never_raise_however_many_engines_are_down():
    """If SOMETHING answered and found something, that is real signal."""
    payload = outage_payload()
    payload["results"] = [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p/1"}]
    client = make_client(payload, engines=ENGINES)
    assert client("site:alpha.invalid Testy") == payload["results"]


def test_blindness_error_carries_no_query_text():
    """The query IS the person's name/phone/email; it must never reach a log
    line or an exception message."""
    client = make_client(outage_payload(), engines=ENGINES)
    with pytest.raises(SearxError) as excinfo:
        client("site:alpha.invalid Testy Mctestface 555-0100")
    message = str(excinfo.value)
    assert "Testy" not in message and "555" not in message


def test_blindness_is_retried_like_any_other_transient_failure():
    session = FakeSession(outage_payload())
    client = SearxClient("https://searx.invalid", session=session, engines=ENGINES,
                         attempts=3, sleep=lambda _s: None,
                         min_interval_s=0, jitter_s=0)
    with pytest.raises(SearxError):
        client("site:alpha.invalid Testy")
    assert session.calls == 3


@pytest.mark.parametrize("raw, expected", [
    ([["brave", "Too many requests"]], ["brave"]),          # SearXNG's real shape
    (["brave", "google"], ["brave", "google"]),             # bare strings
    ([{"engine": "brave", "error": "x"}], ["brave"]),       # object form
    ([None, 7, [], ""], []),                                # junk is ignored
    ("not a list", []),
    (None, []),
])
def test_unresponsive_engine_names_parses_every_known_shape(raw, expected):
    assert unresponsive_engine_names({"unresponsive_engines": raw}) == expected


# --------------------------------------------------------------------------
# 2. Browser bot walls -- the detector
# --------------------------------------------------------------------------

CLOUDFLARE_PAGE = (
    "Just a moment...\n"
    "Checking your browser before accessing alpha.invalid.\n"
    "This process is automatic. Your browser will redirect shortly.\n"
    "Please enable JavaScript and cookies to continue."
)

CAPTCHA_PAGE = "Verify you are human by completing the action below.\nalpha.invalid"

REAL_NO_RESULTS_PAGE = (
    "Alpha People Search\n"
    "No results found for your search. We could not find any records matching "
    "that name in our database. Try searching by phone number or email address "
    "instead, or browse our directory by state. Alpha People Search indexes "
    "public records from county, state and federal sources. "
    "About us | Privacy policy | Do not sell my information | Opt out\n"
    + "Browse by state: " + ", ".join(["Illinois"] * 100)
)

REAL_LISTING_WITH_CAPTCHA_WIDGET = (
    "Testy Mctestface, 40, Springfield IL. Known addresses, relatives and "
    "phone numbers. View full report.\n"
    + "Related records. " * 200 +
    "\nTo request removal, complete the form below and confirm you are human: "
    "I'm not a robot"
)


@pytest.mark.parametrize("text, title, status", [
    (CLOUDFLARE_PAGE, "Just a moment...", 200),
    (CAPTCHA_PAGE, "", 200),
    ("Sorry, you have been blocked", "Attention Required! | Cloudflare", 200),
    ("Pardon Our Interruption\nWe noticed unusual activity.", "", 200),
    ("Request unsuccessful. Incapsula incident ID: 123-456", "", 200),
    ("", "", 403),
    ("", "", 429),
])
def test_bot_wall_signatures_are_detected(text, title, status):
    assert bot_wall_reason(text, title, status) is not None


@pytest.mark.parametrize("text, title, status", [
    (REAL_NO_RESULTS_PAGE, "No results - Alpha People Search", 200),
    (REAL_LISTING_WITH_CAPTCHA_WIDGET, "Testy Mctestface - Alpha", 200),
    ("No records found.", "Alpha People Search", 404),
    ("Testy Mctestface, Springfield IL", "Testy Mctestface", 200),
])
def test_ordinary_broker_pages_are_not_bot_walls(text, title, status):
    """False positives here turn a usable check into a permanent error for
    that broker, so the signature list has to stay conservative. Note the
    third case: a 404 on a search URL is a legitimate 'no such record'."""
    assert bot_wall_reason(text, title, status) is None


def test_generic_captcha_wording_only_counts_on_a_near_empty_page():
    """A broker's opt-out page may legitimately embed a reCAPTCHA widget next
    to real content; a challenge interstitial has nothing else on it."""
    assert bot_wall_reason("I'm not a robot", "", 200) is not None
    assert bot_wall_reason(REAL_LISTING_WITH_CAPTCHA_WIDGET, "", 200) is None


# --------------------------------------------------------------------------
# 3. Browser bot walls -- through PlaywrightChecker and run_playwright_checks
# --------------------------------------------------------------------------

class FakePage:
    def __init__(self, text, title="", status=200):
        self._text = text
        self._title = title
        self._status = status

    def route(self, *a, **k):
        pass

    def goto(self, *a, **k):
        return type("Resp", (), {"status": self._status})()

    def inner_text(self, selector):
        return self._text

    def title(self):
        return self._title

    def close(self):
        pass


class FakeContext:
    def __init__(self, page):
        self._page = page

    def set_default_timeout(self, ms):
        pass

    def new_page(self):
        return self._page

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_context(self, **kwargs):
        return FakeContext(self._page)


def checker_for(text, title="", status=200):
    checker = PlaywrightChecker()
    checker._browser = FakeBrowser(FakePage(text, title, status))
    return checker


CHECK = {"broker_id": "alpha", "url": "https://alpha.invalid/search",
         "terms": ["Testy Mctestface", "+1-555-0100"]}


def test_playwright_checker_reports_a_bot_wall_as_an_error():
    result = checker_for(CLOUDFLARE_PAGE, "Just a moment...")(CHECK)
    assert "error" in result and "found" not in result


def test_playwright_checker_still_reports_a_real_clean_page_as_not_found():
    result = checker_for(REAL_NO_RESULTS_PAGE, "No results - Alpha")(CHECK)
    assert result == {"found": False}


def test_playwright_checker_still_reports_a_real_hit():
    result = checker_for("Testy Mctestface, Springfield IL", "Testy")(CHECK)
    assert result == {"found": True}


def test_run_playwright_checks_counts_a_bot_wall_as_errored_not_checked():
    results = playwright_checks.run_playwright_checks(
        [CHECK], checker_for(CLOUDFLARE_PAGE, "Just a moment...")
    )
    assert results["alpha"]["checked"] is False
    assert results["alpha"]["present"] is False
    assert "bot wall" in results["alpha"]["error"]


def test_run_playwright_checks_observer_sees_error_not_checked():
    seen = []
    playwright_checks.run_playwright_checks(
        [CHECK], checker_for(CLOUDFLARE_PAGE, "Just a moment..."),
        observer=lambda bid, outcome, hits, errors: seen.append(outcome),
    )
    assert seen == ["error"]


# --------------------------------------------------------------------------
# 4. End to end: does the raise actually reach run_cycle / forget()?
# --------------------------------------------------------------------------

IDENTITY = Identity(first_name="Testy", last_name="Mctestface",
                    phones=["+1-555-0100"], emails=["testy@example.invalid"])

BROKERS = [
    {"id": "alpha", "name": "Alpha", "url": "https://alpha.invalid",
     "verification": "automatable"},
]


def seeded_store():
    """A store that already knows alpha lists this person -- the state in which
    a false 'absent' becomes a false 'resolved' and a destroyed presence row."""
    store = FakeStore()
    store.presence[(IDENTITY.identity_key, "alpha")] = True
    return store


def presence_checker_for(searx_search=None, page_action=None):
    from broker_guard import progress as progress_mod

    deps = service.Dependencies(searx_search=searx_search, page_action=page_action,
                                store=seeded_store())
    return service.build_presence_checker(
        IDENTITY, BROKERS, deps, None, progress=progress_mod.ScanProgress(),
    )


def test_searxng_outage_is_excluded_from_resolved_end_to_end():
    """The whole point. SearXNG answers 200-with-nothing for every query; the
    broker must land in `errors`, NOT in `resolved`."""
    client = make_client(outage_payload(), engines=ENGINES)
    checker = presence_checker_for(searx_search=client)
    store = seeded_store()

    result = run_cycle(IDENTITY.identity_key, BROKERS, checker, store,
                       lambda payload: None, "2026-01-01T00:00:00+00:00")

    assert result["resolved"] == []
    assert [e["broker_id"] for e in result["errors"]] == ["alpha"]
    assert result["current"] == []


def test_searxng_outage_never_reaches_store_forget():
    client = make_client(outage_payload(), engines=ENGINES)
    store = seeded_store()
    deps = autopilot.AutopilotDependencies(
        store=store, presence_checker=presence_checker_for(searx_search=client),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    result = autopilot.run_scan_cycle(IDENTITY, BROKERS, deps)

    assert result["forgotten"] == []
    # The presence row -- the record that this broker is still listing the
    # person -- survived the outage.
    assert store.is_seen(IDENTITY.identity_key, "alpha")


def test_healthy_empty_searxng_still_resolves_and_forgets():
    """The other half of the property: a genuine removal must still be
    detected. If this fails, the fix has turned every clean scan into an
    outage and the tool can never confirm a removal again."""
    client = make_client(healthy_empty_payload(), engines=ENGINES)
    store = seeded_store()
    deps = autopilot.AutopilotDependencies(
        store=store, presence_checker=presence_checker_for(searx_search=client),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    result = autopilot.run_scan_cycle(IDENTITY, BROKERS, deps)

    assert result["errors"] == []
    assert result["resolved"] == ["alpha"]
    assert result["forgotten"] == ["alpha"]
    assert not store.is_seen(IDENTITY.identity_key, "alpha")


def test_bot_wall_is_excluded_from_resolved_end_to_end():
    checker = presence_checker_for(
        page_action=checker_for(CLOUDFLARE_PAGE, "Just a moment...")
    )
    store = seeded_store()

    result = run_cycle(IDENTITY.identity_key, BROKERS, checker, store,
                       lambda payload: None, "2026-01-01T00:00:00+00:00")

    assert result["resolved"] == []
    assert [e["broker_id"] for e in result["errors"]] == ["alpha"]


def test_bot_wall_never_reaches_store_forget():
    store = seeded_store()
    deps = autopilot.AutopilotDependencies(
        store=store,
        presence_checker=presence_checker_for(
            page_action=checker_for(CLOUDFLARE_PAGE, "Just a moment...")
        ),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    result = autopilot.run_scan_cycle(IDENTITY, BROKERS, deps)

    assert result["forgotten"] == []
    assert store.is_seen(IDENTITY.identity_key, "alpha")


def test_real_no_results_broker_page_still_resolves_and_forgets():
    store = seeded_store()
    deps = autopilot.AutopilotDependencies(
        store=store,
        presence_checker=presence_checker_for(
            page_action=checker_for(REAL_NO_RESULTS_PAGE, "No results - Alpha")
        ),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    result = autopilot.run_scan_cycle(IDENTITY, BROKERS, deps)

    assert result["errors"] == []
    assert result["forgotten"] == ["alpha"]


def test_browser_confirmation_outranks_a_serp_outage():
    """A successful direct read of the broker's OWN site is better evidence
    than the search index, so a SERP failure does not turn a browser-confirmed
    result into 'unknown' -- in either direction."""
    client = make_client(outage_payload(), engines=ENGINES)

    present = presence_checker_for(
        searx_search=client,
        page_action=checker_for("Testy Mctestface, Springfield IL", "Testy"),
    )
    assert present(BROKERS[0], IDENTITY.identity_key) is True

    absent = presence_checker_for(
        searx_search=make_client(outage_payload(), engines=ENGINES),
        page_action=checker_for(REAL_NO_RESULTS_PAGE, "No results - Alpha"),
    )
    assert absent(BROKERS[0], IDENTITY.identity_key) is False
