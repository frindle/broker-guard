"""Declarative recipes for the on-site PEOPLE-SEARCH forms we know how to read.

Why this exists
------------------
The browser presence leg (``browser.PlaywrightChecker``) points Chromium at a
broker's BARE HOMEPAGE -- ``https://<domain>`` straight out of the dataset --
and scans whatever renders for identity terms. A data broker does not publish
a person's listing on its homepage; it publishes it behind the site's own
search form. So the homepage check is a near-guaranteed ``{"found": false}``
for every broker, which is worse than useless: it is a confident-looking
answer to a question that was never actually asked. This module is the other
half -- the site's own search box, filled in and read.

It is the SEARCH sibling of ``optout_forms.py`` and deliberately mirrors its
shape: pure data plus pure functions here, browser driving in
``search_probe.py``. Same allow-list safety property, too -- a broker with no
hand-verified recipe simply keeps today's homepage behaviour, and there is no
"guess the search form from the URL" fallback.

READ-ONLY, and that boundary is narrower than it looks
---------------------------------------------------------
``browser.py``'s docstring promises this leg "only ever reads". Typing a name
into a SEARCH box and pressing SEARCH is still reading: it asks the broker's
own public index a question and reads the answer back. What is out of scope,
permanently, is anything that changes state on the broker's side -- opt-out
and removal forms, account creation, logins, payments, contact/"request my
data" forms. Those belong to ``optout_submit.py``, which has four interlocks
and an audit trail precisely because it has side effects. A recipe here
targets a search field and a search button, nothing else, and
``assert_read_only`` refuses one whose selectors or URL say otherwise.

False negatives are the whole game
-------------------------------------
Re-read ``browser.py``'s bot-wall comment block: a page that is not really
the broker's answer, silently read as "found: false", becomes
``resolved`` -> ``store.forget()`` -- the presence row for a broker that is
still publishing the person's PII is deleted, and the person stops chasing a
live listing. A search driver has MORE ways to produce that lie than a plain
homepage fetch:

* the form never submitted (selector drift after a redesign)
* the results page is still an interstitial ("Searching | ThatsThem")
* the click landed us on somebody else's page entirely (see below)
* a bot wall appeared between the form and the results

So ``classify_search_page`` never infers absence from the ABSENCE of
evidence. "Not present" has to be affirmatively stated by the broker -- a
zero count it printed itself, or wording from the recipe's
``no_results_markers`` that was read off that broker's real zero-result
page. Anything else is ``{"error": ...}``, which ``interpret_check_result``
turns into checked=False -> ``PresenceUnknown`` -> excluded from
``resolved``. Unknown is cheap; a false "absent" is not.

Two live findings that shaped the design
-------------------------------------------
1. **A zero-result page echoes the name you searched for.** SearchPeopleFree's
   miss page says "404 - Not Found ... Aliases, also known as (AKA) ... for
   Zylphrenna Quixbottom" -- the searched name, many times over. A naive
   "are the identity terms on this page?" match (which is exactly what the
   homepage leg does) reports FOUND on a page that means the opposite. That
   is why the no-results markers are consulted BEFORE any term matching, and
   why a term match alone can never produce a "found".
2. **Submitting can navigate you off the broker's site.** ThatsThem's search
   form is ``target="_blank"``: clicking Search opens the real results in a
   new tab AND redirects the original tab to ``spokeo.com``. Reading "the
   page we clicked on" would have classified Spokeo's homepage as
   ThatsThem's answer. Hence ``results_host``: the driver only ever reads a
   page still served by the broker, and reports an error when no such page
   exists.

Optional narrowing fields are left EMPTY on purpose
------------------------------------------------------
Several of these forms offer an optional "City, State" box. Filling it from a
profile address line is how you manufacture a false negative: a slightly-off
locality turns a real listing into a truthful-looking "Found 0 results". A
recipe therefore fills only what the form REQUIRES, which is the broadest
query the site will answer. Over-broad costs a false positive at worst (a
presence row that stays), and that is the safe direction.
"""
from dataclasses import dataclass
import re

from broker_guard.detection import is_people_search_hit

# Fields a search recipe may ask for. Deliberately a short list: everything
# here comes straight off ``profile.Identity`` with no inference. "state" is
# the one exception -- it is derived the same way
# ``optout_forms.state_from_addresses`` derives it, and belongs here only
# because a broker's search form can make it flatly REQUIRED to search at
# all (a disabled Search button with no state chosen), not a narrowing
# field this tool is choosing to fill. See ``resolve_search_fields``.
FIELD_SOURCES = ("first_name", "last_name", "full_name", "email", "phone", "state")


@dataclass(frozen=True)
class SearchField:
    """One identity value to type into the broker's search form.

    ``source`` names where the value comes from (see ``FIELD_SOURCES``);
    ``required`` False means the recipe tolerates an empty profile value and
    submits without it. No recipe currently fills an optional NARROWING
    field -- see the module docstring on why that is a false-negative
    factory -- so ``required=False`` is for genuinely alternative inputs.
    ``kind`` says how to apply the value: ``text`` (default) is a plain
    fill; ``select`` is a real ``<select>`` element chosen by label.
    """

    selector: str
    source: str
    label: str
    required: bool = True
    kind: str = "text"


@dataclass(frozen=True)
class SearchRecipe:
    """Everything needed to ask ONE broker's site whether it lists someone."""

    broker_id: str
    broker_name: str
    # The page carrying the search form. Not a results URL: results are
    # reached by submitting the form, never by string-building a URL.
    search_url: str
    fields: tuple
    submit_selector: str
    # Host that a legitimate results page must still be served from. See
    # finding (2) above -- ThatsThem's submit navigates the original tab to
    # spokeo.com, and reading that would be reading the wrong company's site.
    results_host: str
    # Wording this broker prints when it has NOTHING for the query. Read off
    # its real zero-result page. This is the ONLY thing (besides a printed
    # zero count) that may produce a "not present".
    no_results_markers: tuple = ()
    # Wording this broker prints when it DOES have results. Necessary but not
    # sufficient for a "found" -- the identity terms must appear too.
    hit_markers: tuple = ()
    # Optional regex whose group 1 is the number of results the broker itself
    # printed. A printed 0 is the strongest possible "not present" evidence;
    # a printed N>0 still needs the identity terms to appear.
    count_pattern: str = ""
    # Extra wording that means "the results page has settled". Defaults to
    # hit_markers + no_results_markers, which is usually exactly right.
    ready_markers: tuple = ()
    # How long THIS broker may churn before printing an answer, in ms. 0
    # means "use search_probe's default". Set it only with a measurement to
    # point at: RevealPhoneOwner runs a ~26-second staged progress animation
    # ("Initiating phone lookup...", "Finding social profiles...") before
    # every HIT, while its misses answer instantly -- so the default 20s
    # deadline would time out on precisely the pages that contain a person
    # and never on the pages that do not. That is not a slow broker, it is a
    # recipe that reports "unknown" for every hit.
    ready_timeout_ms: int = 0
    verified_on: str = ""
    notes: str = ""


# --- the allow-list ----------------------------------------------------------
#
# Every entry was opened in a real browser, filled in by hand with a common
# name ("John Smith") and with a nonsense name ("Zylphrenna Quixbottom"), and
# BOTH result pages were read. Nothing here was inferred from a URL, from
# documentation or from memory. Penn's own PII was never typed into any of
# these sites during authoring.

THATSTHEM = SearchRecipe(
    broker_id="thatsthem-com",
    broker_name="ThatsThem",
    search_url="https://thatsthem.com/",
    fields=(
        # One box, whole name. The optional "City, State" box (#address) is
        # deliberately NOT filled.
        SearchField(selector="form[role='search'] #name", source="full_name",
                    label="Full Name"),
    ),
    # The button carries only utility classes and no type attribute, so it is
    # addressed as "the button inside the search form".
    submit_selector="form[role='search'] button",
    results_host="thatsthem.com",
    no_results_markers=(
        "no results found",
        "we couldn't find any records matching your search",
        "found 0 results",
    ),
    hit_markers=("search results for",),
    # "Found 10 results" / "Found 0 results".
    count_pattern=r"found\s+([\d,]+)\s+results",
    verified_on="2026-09-22",
    notes=(
        "Verified live. Submitting goes through a 'Searching | ThatsThem' "
        "interstitial for several seconds before the real results render -- "
        "reading the page too early is a false negative, which is why the "
        "driver waits for a ready marker rather than for load. The form is "
        "target=_blank: the results open in a NEW tab and the ORIGINAL tab is "
        "redirected to spokeo.com, so results_host is load-bearing here. The "
        "zero-result page is served with HTTP 404, which bot_wall_reason "
        "correctly does not treat as a wall. KNOWN STATE (2026-09-22): driven "
        "from headless Chromium this recipe currently gets an 'Access Denied' "
        "interstitial on /search -- the form page loads, the submit goes "
        "through, and the RESULTS endpoint refuses an automated client "
        "(reproduced with and without resource blocking). That is reported as "
        "an error, never as 'not listed', which is the entire point; the "
        "recipe is kept because it is correct and the block is per-client, so "
        "a run from another host or a relaxation on their side starts working "
        "with no code change. Do not 'fix' this by evading the wall."
    ),
)

SEARCHPEOPLEFREE = SearchRecipe(
    broker_id="searchpeoplefree-com",
    broker_name="SearchPeopleFree",
    search_url="https://www.searchpeoplefree.com/",
    fields=(
        # Alpine.js form: no ids and no name attributes on the name boxes, so
        # the x-model bindings are the only stable handles. page.fill emits
        # the input event Alpine listens for.
        SearchField(selector="input[x-model='first']", source="first_name",
                    label="First Name"),
        SearchField(selector="input[x-model='last']", source="last_name",
                    label="Last Name"),
    ),
    # Two buttons exist on the page; the other is "scroll-up".
    submit_selector="button.btn",
    results_host="searchpeoplefree.com",
    no_results_markers=(
        "404 - not found",
        "the page you were looking for could not be found",
    ),
    hit_markers=("showing 1 -",),
    # "Showing 1 - 10 of 248 People".
    count_pattern=r"showing\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)\s+people",
    verified_on="2026-09-22",
    notes=(
        "Verified live both ways. This is the site that proves rule (1): its "
        "miss page is a 404 titled 'Page Not Found' that nonetheless repeats "
        "the searched name a dozen times ('Aliases ... for Zylphrenna "
        "Quixbottom'), so term-matching alone reports a hit on a page that "
        "means the opposite. No-results markers are checked first for exactly "
        "this reason. The optional 'City, State' box (#input_location) is not "
        "filled."
    ),
)

USPHONEBOOK = SearchRecipe(
    broker_id="usphonebook-com",
    broker_name="USPhonebook",
    # The site's default form is a REVERSE PHONE box; the name form lives on
    # /people-search. Recorded settled rather than clicking the tab, because
    # the tab is itself a redirect to this URL.
    search_url="https://www.usphonebook.com/people-search",
    fields=(
        SearchField(selector="#pfHeroInput", source="full_name",
                    label="First and last name"),
    ),
    submit_selector="#searchHeroForm button[type='submit']",
    results_host="usphonebook.com",
    no_results_markers=(
        "we did not return a valid result for",
    ),
    # Deliberately narrow. The obvious "records for" is NOT usable: the
    # zero-result page also says "Public Records for <name> -- Paid Results
    # Sponsored by TruthFinder.com", so that marker fires on a miss and the
    # page reads as self-contradictory. "We uncovered N results for the name
    # ..." appears only when there really are results.
    hit_markers=("we uncovered",),
    # "Success, We've found 247 records for John Smith". The apostrophe is
    # optional/typographic-tolerant because the page is Cloudflare-rewritten.
    count_pattern=r"we['’]?ve\s+found\s+([\d,]+)\s+records",
    verified_on="2026-09-22",
    notes=(
        "Verified live both ways through the /people-search hero form. The "
        "zero-result page is also HTTP 404 and also echoes the searched name "
        "(under 'Paid Results Sponsored by TruthFinder.com'), so the same "
        "markers-before-terms rule applies. Cloudflare fronts this site "
        "(Rocket Loader is present), so a challenge page is a realistic "
        "outcome -- which bot_wall_reason turns into an error, not a 'no'. "
        "The optional City/State inputs are not filled."
    ),
)


ADVANCEDBACKGROUNDCHECKS = SearchRecipe(
    broker_id="advancedbackgroundchecks-com",
    broker_name="AdvancedBackgroundChecks",
    search_url="https://www.advancedbackgroundchecks.com/",
    fields=(
        SearchField(selector="input[placeholder='First Name']", source="first_name",
                    label="First Name"),
        SearchField(selector="input[placeholder='Last Name']", source="last_name",
                    label="Last Name"),
    ),
    # The only element on the page with type=submit; the optional
    # City/State inputs are deliberately not filled.
    submit_selector="button[type='submit']",
    results_host="advancedbackgroundchecks.com",
    no_results_markers=(
        "didn't find an exact match",
    ),
    # Only present on a card-bearing results page; the miss page (which also
    # echoes the searched name repeatedly, in the same style as
    # SearchPeopleFree) never renders one.
    hit_markers=("view details",),
    verified_on="2026-09-22",
    notes=(
        "Verified live both ways: a real name navigates to "
        "/find/name/<slug> with result cards and 'View Details' links; a "
        "nonsense name lands on the SAME url shape but says 'We searched our "
        "public records database ... but didn't find an exact match', while "
        "still echoing the searched name a dozen times in the FAQ boilerplate "
        "-- the no-results marker is checked first for exactly that reason. "
        "No usable printed count: the site prints an approximate '260+ "
        "people with a similar name', which is not a precise 'this person' "
        "count, so no count_pattern is set and the markers alone decide it."
    ),
)

SEARCHPUBLICRECORDS = SearchRecipe(
    broker_id="searchpublicrecords-com",
    broker_name="Search Public Records (Civil Data Research, LLC)",
    search_url="https://www.searchpublicrecords.com/",
    fields=(
        SearchField(selector="#search-name", source="full_name", label="Full Name"),
    ),
    submit_selector=".people__search__btn",
    results_host="searchpublicrecords.com",
    # Deliberately empty: every attempt (2026-09-22, both a common and a
    # nonsense name, waited out to 15s) reaches a "Search Complete!" results
    # page that ALSO always says "Please verify you are human to continue." -
    # a bot wall this codebase's own bot_wall_reason recognizes (the strong
    # marker "verify you are human"), so run_search reports {"error": ...}
    # before classify_search_page ever runs. No hand-verified hit/no-results
    # wording exists because the real results page has never been seen --
    # writing markers now would be inventing prose, which this module's own
    # docstring forbids. See notes.
    no_results_markers=(),
    hit_markers=(),
    verified_on="2026-09-22",
    notes=(
        "Verified live both ways (2026-09-22): the form page itself is "
        "clean (no wall text on /), filling #search-name and clicking "
        "Search navigates to /people/<token> which runs a 'Searching for "
        "<name> ... National/State/County Records' progress animation to "
        "100% and then shows 'Search Complete!' -- but the results "
        "themselves are always behind 'Please verify you are human to "
        "continue.', for a common name (Michael Thompson) exactly as much "
        "as a nonsense one. This recipe is kept anyway, same reasoning as "
        "ThatsThem's: with NO recipe, this broker gets the homepage check, "
        "which is a near-guaranteed confident 'not found' on a page that "
        "never even asked the question. With this recipe, every run is an "
        "honest {'error': 'bot wall: ...'} -> PresenceUnknown -> excluded "
        "from resolved. Do not fill in no_results_markers/hit_markers from "
        "guesswork if the wall ever lifts; read them off a real page first."
    ),
)

CYBERBACKGROUNDCHECKS = SearchRecipe(
    broker_id="cyberbackgroundchecks-com",
    broker_name="Cyber Background Checks",
    search_url="https://www.cyberbackgroundchecks.com/",
    fields=(
        SearchField(selector="#SearchCriteriaViewModel_FirstName",
                    source="first_name", label="First Name"),
        SearchField(selector="#SearchCriteriaViewModel_LastName",
                    source="last_name", label="Last Name"),
    ),
    # The homepage carries FOUR search forms in tabs (name/address/phone/
    # email), each with its own submit button, so the button is addressed by
    # the id of the NAME one specifically.
    submit_selector="#button-search-by-name",
    results_host="cyberbackgroundchecks.com",
    no_results_markers=(
        "unfortunately we did not find any results",
    ),
    hit_markers=("view details",),
    # "250 results for Michael Thompson" / "0 results for Zylphrenna
    # Quixbottom" -- this broker prints its own zero, which is the strongest
    # possible evidence of absence.
    count_pattern=r"([\d,]+)\s+results\s+for",
    verified_on="2026-09-22",
    notes=(
        "Verified live both ways on 2026-09-22 from headless Chromium with "
        "this codebase's production UA. A plain server-rendered ASP.NET MVC "
        "form: submitting navigates to /people/<first>-<last> and prints "
        "'250 results for Michael Thompson' with 'VIEW DETAILS' cards; the "
        "nonsense name lands on the same URL shape and prints '0 results "
        "for Zylphrenna Quixbottom' plus 'Unfortunately we did not find any "
        "results'. Note the miss page ALSO echoes the searched name eight "
        "times under 'Sponsored by Truthfinder.com'/'Sponsored by "
        "Spokeo.com' partner blocks, each with a bare 'DETAILS' link -- "
        "hence hit_markers is the two-word 'view details', which those "
        "affiliate blocks do not contain. The optional 'City & State' box "
        "(#SearchByName_AddressLine2) is deliberately not filled. Contrast "
        "this broker's OPT-OUT page, which is permanently behind a "
        "Cloudflare managed challenge -- see optout_forms."
    ),
)

PRIVATENUMBERCHECKER = SearchRecipe(
    broker_id="privatenumberchecker-com",
    broker_name="PrivateNumberChecker",
    search_url="https://www.privatenumberchecker.com/",
    fields=(
        SearchField(selector="#search-form input[name='phone-search']",
                    source="phone", label="Phone Number"),
    ),
    submit_selector="#search-form button[type='submit']",
    results_host="privatenumberchecker.com",
    no_results_markers=("no result found",),
    # "Result for <number>" on the settled hit page. Distinct from the miss
    # page's "No Result found!" -- checked, not assumed: the two strings
    # share the word "result" and nothing else.
    hit_markers=("result for",),
    # Measured: the hit page walks through a staged animation ("Initiating
    # reverse phone lookup", "Get location", "Querying service provider",
    # "Generating result") and lands on /phone-number/<number>/ at about
    # eleven seconds. Under the shared 20s default that fits, but only just,
    # so this recipe buys margin rather than reporting "unknown" on a slow
    # night.
    ready_timeout_ms=30000,
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Built on the same template "
        "as RevealPhoneOwner (same page furniture, same 'No Result found!' "
        "miss page, same staged progress animation, same 2022 copyright "
        "line), but recorded as its own finding from its own live run "
        "rather than inherited: a shared look is not a shared codebase, and "
        "the two do differ -- this one settles in ~11s and lands on a "
        "different URL shape (/phone-number/<number>/), and its hit wording "
        "is 'Result for <number>', not 'Data result found for'. Like its "
        "sibling it is a reverse-phone directory, so this asks about the "
        "profile's PHONE, and the owner name sits behind a 'Full Report' "
        "paywall -- which does not matter, because the only question asked "
        "here is whether a record exists. Its REMOVAL page, unlike the rest "
        "of the site, is behind a Cloudflare Turnstile challenge -- see "
        "optout_forms.OPTOUT_BLOCKED."
    ),
)

NATIONALPUBLICDATA = SearchRecipe(
    broker_id="nationalpublicdata-com",
    broker_name="National Public Data",
    search_url="https://nationalpublicdata.com/",
    fields=(
        # The page carries TWO name boxes -- a compact one in the header
        # (#search-name) and the main hero form (#main-search-name) -- so
        # the selector names the hero one explicitly. A generic
        # "input[type=text]" here would match both and Playwright's strict
        # mode would refuse to type into either.
        SearchField(selector="#main-search-name", source="full_name",
                    label="Name"),
    ),
    submit_selector="#page-search-form button",
    results_host="nationalpublicdata.com",
    no_results_markers=("we could not find people named",),
    hit_markers=("people found",),
    count_pattern=r"([\d,]+)\s+people\s+found",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. A hit navigates to "
        "/people/<letter>/<first>-<last>/ and prints '9352 people found' "
        "above per-person cards; a nonsense name lands on /search/?name=... "
        "titled 'Results Not Found' and prints 'We could not find people "
        "named <name>'. The miss page echoes the searched name (the usual "
        "trap) but carries no count and no 'people found' wording at all, "
        "checked by reading its full text. The optional 'City & State' box "
        "is deliberately left empty. Note the contrast with this broker's "
        "OPT-OUT page, which is behind a Cloudflare Turnstile challenge -- "
        "see optout_forms.OPTOUT_BLOCKED."
    ),
)

REVEALPHONEOWNER = SearchRecipe(
    broker_id="revealphoneowner-com",
    broker_name="RevealPhoneOwner",
    search_url="https://www.revealphoneowner.com/",
    fields=(
        # The only search this site has. It is a reverse-phone directory:
        # there is no name box anywhere on it, so presence here is a
        # question about the profile's PHONE NUMBER, and a profile with no
        # phone gets "unknown" rather than a guess (resolve_search_fields
        # reports the missing field and the check never runs).
        SearchField(selector="#phone-query", source="phone",
                    label="Phone Number"),
    ),
    submit_selector="#query-form button[type='submit']",
    results_host="revealphoneowner.com",
    no_results_markers=("no result found",),
    hit_markers=("data result found for",),
    # See the field comment on SearchRecipe.ready_timeout_ms: measured, and
    # the measurement is the whole reason this field exists.
    ready_timeout_ms=45000,
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting navigates to "
        "/search/<dashed-number>/ on the broker's own host. A number it has "
        "nothing for answers instantly with 'No Result found!'. A number it "
        "DOES have runs a staged progress animation ('Initiating phone "
        "lookup ...', then 'Finding social profiles ...') for about 26 "
        "seconds and then prints 'Data result found for (312) 222-3232' "
        "over a teaser card (Owner Name: Available, Address, Zip Code, "
        "Phone Type) behind a 'Get Full Report' paywall. The teaser is "
        "enough: it states that a record for that number exists, which is "
        "the only question this tool asks, and the searched number is "
        "printed on the page so the identity-term check corroborates it "
        "(detection matches phone terms on digits, so formatting does not "
        "matter). The 26-second animation is why this recipe carries its "
        "own ready_timeout_ms -- under the shared 20s default every HIT "
        "would time out into 'unknown' while every MISS answered "
        "instantly, which is the most misleading failure mode available. "
        "Its opt-out leg is a working recipe too; see optout_forms."
    ),
)

RECIPES = {
    THATSTHEM.broker_id: THATSTHEM,
    SEARCHPEOPLEFREE.broker_id: SEARCHPEOPLEFREE,
    USPHONEBOOK.broker_id: USPHONEBOOK,
    ADVANCEDBACKGROUNDCHECKS.broker_id: ADVANCEDBACKGROUNDCHECKS,
    SEARCHPUBLICRECORDS.broker_id: SEARCHPUBLICRECORDS,
    CYBERBACKGROUNDCHECKS.broker_id: CYBERBACKGROUNDCHECKS,
    NATIONALPUBLICDATA.broker_id: NATIONALPUBLICDATA,
    PRIVATENUMBERCHECKER.broker_id: PRIVATENUMBERCHECKER,
    REVEALPHONEOWNER.broker_id: REVEALPHONEOWNER,
}


# Brokers that were investigated for this pilot and found to have NO usable
# public presence-search surface. Recorded as data, with the reason, so the
# next person does not spend the evening re-discovering it -- and so nobody
# "fixes" the gap by writing a recipe against a page that cannot answer the
# question. These are notes, not behaviour: nothing reads this at runtime.
NO_SEARCH_SURFACE = {
    "gladiknow-com": (
        "Glad I Know is not a search engine over its own records -- it is an "
        "affiliate front. Verified 2026-09-22: filling its first/last/city/"
        "state form and pressing SEARCH never renders results on gladiknow."
        "com; it opens a new tab at truthfinder.com/results?...&utm_campaign="
        "gladiknow with the query forwarded. There is nothing on this site "
        "that can say whether it lists a person, so any recipe here would be "
        "reporting TruthFinder's paywall as Glad I Know's answer."
    ),
    "chexsystems-com": (
        "ChexSystems is a nationwide specialty CRA under the FCRA, not a "
        "people-search site, and it has no public consumer lookup at all. "
        "Verified 2026-09-22: every consumer path (disclosure report, score, "
        "security freeze lookup, dispute) is behind the authenticated "
        "Consumer Portal at chexsystems-ds.fiscloudservices.com, and the "
        "'Lookup your Security Freeze' page contains no form whatsoever -- "
        "just 'log in to the Consumer Portal'. Presence there is established "
        "by requesting your own consumer disclosure with SSN + DOB, which is "
        "an identity-verified request this tool must not automate."
    ),
    "achcoop-com": (
        "ACH, Address Clearing House is a B2B address-hygiene/ACH-clearing "
        "data vendor, not a consumer people-search site. Verified "
        "2026-09-22: its homepage carries zero <input>/<select>/<textarea> "
        "elements anywhere -- no search box exists on this domain at all, "
        "only a DNSMPI (opt-out) page."
    ),
    "bigdbm-com": (
        "BIGDBM is a B2B identity-data vendor, not a consumer people-search "
        "site. Verified 2026-09-22: its homepage carries zero form inputs "
        "anywhere -- no search box exists on this domain at all, only its "
        "opt-out subdomain."
    ),
    "peopledatalabs-com": (
        "People Data Labs is a B2B people-data API vendor, not a consumer "
        "people-search site. Verified 2026-09-22: its homepage carries no "
        "name-search UI anywhere, only a marketing email-capture box and a "
        "Do Not Sell or Share page."
    ),
    "locateplus-com": (
        "LocatePLUS is a skip-tracing/investigative-data platform sold to "
        "licensed businesses through a closed, login-gated customer portal. "
        "Verified 2026-09-22: the public marketing domain carries no "
        "consumer search UI of any kind -- no inputs anywhere on the "
        "homepage -- and no opt-out webform either, only a "
        "customerservice@locateplus.com mailbox referenced in the privacy "
        "policy."
    ),
    "locatesmarter-com": (
        "LocateSmarter's homepage (www.locatesmarter.com) redirects "
        "straight to portal.locatesmarter.com, a login-gated ASP.NET "
        "customer portal (the only inputs on the landing page are "
        "__EVENTTARGET/__VIEWSTATE-style hidden ASP.NET fields, no visible "
        "search box). Verified 2026-09-22: there is no public, "
        "unauthenticated consumer search surface anywhere on this domain."
    ),
    "openpeoplesearch-com": (
        "Open People Search's own homepage is API/developer marketing copy "
        "(\"The Official Open People Search API\") with zero consumer-"
        "facing search inputs anywhere on the page. Verified 2026-09-22: "
        "there is no name-search box on this domain at all, only a "
        "'Remove My Info' consumer opt-out flow."
    ),
    "golookup-com": (
        "golookup.com no longer belongs to the original broker at all. "
        "Verified 2026-09-22: the homepage itself now reads 'This Domain "
        "Has Been Transferred by Court Order' and serves press coverage of "
        "the Atlas Data Privacy Corp / Daniel's Law litigation against data "
        "brokers (Radaris et al.) -- there is no search box, no broker "
        "content, and nothing to build a recipe against."
    ),
    "arrestfacts-com": (
        "arrestfacts.com no longer serves its own site. Verified "
        "2026-09-22: the domain now redirects entirely to a different, "
        "unrelated third party (nkreeger.com), which happens to run a "
        "copy of the same arrest-records search template under its own "
        "branding ('US Official Arrest & Criminal Records ... | "
        "nkreeger.com'). A recipe here would be searching someone else's "
        "site and reporting it as arrestfacts.com's answer."
    ),
    "facecheck-id": (
        "FaceCheck is a reverse FACE-IMAGE search engine, not a name-based "
        "people-search site -- verified 2026-09-22, its only search input "
        "anywhere on the domain is a photo file-upload control. This tool "
        "has no photo of the person to search with, and would not use one "
        "without separate, explicit consent even if it did; there is no "
        "name/email/phone search surface to build a recipe against."
    ),
    "publicrecords360-com": (
        "publicrecords360.com no longer serves its own content. Verified "
        "2026-09-22: even the bare homepage 302-redirects straight to "
        "https://www.ussearch.com/?feeder=publicrecords360 -- it is purely "
        "an affiliate feeder for US Search, with nothing of its own to "
        "search."
    ),
    "publicrecordsnow-com": (
        "PublicRecordsNow's homepage is independently served, but its "
        "search action is not: verified 2026-09-22, filling the name box "
        "and pressing Search navigates the tab to https://www.peoplefinders."
        "com/ (PeopleFinders' own homepage, not a results page on either "
        "site), and its own 'Do Not Sell' link on the same page points to "
        "peoplefinders.com/do-not-sell, not to anything on publicrecordsnow."
        "com. This is an affiliate front for PeopleFinders; there is "
        "nothing on publicrecordsnow.com itself that can answer whether it "
        "lists a person."
    ),
    "cocofinder-com": (
        "Same shape as gladiknow-com. Verified 2026-09-22: CocoFinder's own "
        "name form does not produce results on cocofinder.com -- pressing "
        "SEARCH hands the query to truthfinder.com's results page with a "
        "cocofinder affiliate campaign tag. It is an affiliate front, so a "
        "recipe here would be reporting TruthFinder's answer (and "
        "TruthFinder's paywall) as CocoFinder's, which is exactly the "
        "mistake the allow-list exists to prevent."
    ),
}


# A search surface EXISTS here, but we could not get a trustworthy verdict
# out of it, so no recipe ships and no verdict is guessed.
#
# This is deliberately a third category, separate from RECIPES ("we can read
# this broker") and NO_SEARCH_SURFACE ("there is nothing here to read").
# Collapsing it into either one would be a lie in a different direction: into
# RECIPES and the tool reports absence it never established, into
# NO_SEARCH_SURFACE and a future reader stops looking at a site that plainly
# has a search box. Each entry says what was observed and what would have to
# change for a recipe to become possible.
#
# Notes, not behaviour: nothing reads this at runtime. A broker listed here is
# simply absent from RECIPES, which is what actually prevents a search.
SEARCH_UNDECIDED = {
    "privaterecords-net": (
        "Verified 2026-09-22. The site is a single-page app that first "
        "shows an FCRA consent interstitial; the search form underneath "
        "cannot be submitted until 'I AGREE' is clicked, and clicking the "
        "search button beforehand fails with the consent overlay "
        "intercepting the pointer (which is how the interstitial was found "
        "-- the Playwright error named the intercepting element rather than "
        "just timing out). After agreeing, the form does submit, but the "
        "results view never leaves its loading state: no results, no "
        "'no results' copy, and no error, across repeated attempts and "
        "waits of well over a minute. There is therefore no page state this "
        "tool could read as either 'present' or 'absent', and "
        "classify_search_page would (correctly) refuse it. Left undecided "
        "rather than called infeasible: the surface is real, and a recipe "
        "would become possible if the results view ever completes. Note "
        "also that SearchRecipe has no way to express the consent click "
        "even if it did -- see the same finding on peoplesearcher-com and "
        "on courtrecords.us, where the consent click DOES lead to a "
        "readable results page."
    ),
    "peoplesearcher-com": (
        "Verified 2026-09-22: the same Angular single-page application as "
        "privaterecords-net, down to the FCRA consent interstitial, the "
        "markup of the search form, and the loading view that never "
        "resolves. Recorded separately rather than as 'see privaterecords' "
        "because they are separate dataset brokers and each leg gets its "
        "own independent verdict, but the finding and the condition for "
        "revisiting it are identical."
    ),
}


class SearchRecipeNotFound(KeyError):
    """No hand-verified search recipe exists for this broker."""


class UnsafeRecipeError(ValueError):
    """A recipe looks like it would do something other than search."""


def recipe_for(broker_id: str) -> SearchRecipe:
    """The recipe for *broker_id*, or raise.

    Raises rather than returning None so no caller can read "we have never
    looked at this broker's form" as "this broker has no form".
    """
    recipe = RECIPES.get((broker_id or "").strip())
    if recipe is None:
        raise SearchRecipeNotFound(broker_id)
    return recipe


def is_supported(broker_id: str) -> bool:
    return (broker_id or "").strip() in RECIPES


def supported_broker_ids() -> list[str]:
    return sorted(RECIPES)


# Words that have no business in a READ-ONLY presence check. A selector or
# URL carrying one of these is a recipe that has drifted from "ask the site's
# index a question" towards "act on the person's behalf".
_STATE_CHANGING_WORDS = (
    "opt-out", "optout", "opt_out", "unsubscribe", "remove", "removal",
    "delete", "suppress", "signup", "sign-up", "register", "login",
    "log-in", "signin", "sign-in", "checkout", "payment", "subscribe",
)


def assert_read_only(recipe: SearchRecipe) -> None:
    """Raise if *recipe* targets anything that looks state-changing.

    The rule with teeth, in the same spirit as
    ``optout_forms.assert_no_forbidden``. This module's entire licence to
    type into a third party's page rests on "it is a search box"; a recipe
    edit that started pointing at a removal form or a signup flow would
    quietly spend that licence. The driver calls this before it opens a
    browser AND again before it types.

    Substring matching is crude on purpose: it is allowed to be annoying
    (rename your selector) and it must never be bypassable by a plausible
    -looking recipe.
    """
    haystack = " ".join(
        [recipe.search_url or "", recipe.submit_selector or ""]
        + [f.selector for f in recipe.fields]
    ).lower()
    hits = sorted({w for w in _STATE_CHANGING_WORDS if w in haystack})
    if hits:
        raise UnsafeRecipeError(
            "search recipe {!r} targets state-changing surface: {}".format(
                recipe.broker_id, ", ".join(hits)))


# --- profile -> field values -------------------------------------------------

def resolve_search_fields(recipe: SearchRecipe, identity) -> dict:
    """Map *identity* onto *recipe*'s fields.

    Returns ``{"values": {selector: text}, "labels": {...},
    "missing": [label, ...]}``, the same shape
    ``optout_forms.resolve_fields`` returns, so the two drivers read alike.

    A non-empty ``missing`` means this profile cannot drive this form; the
    caller falls back to today's homepage check rather than submitting a
    half-filled search (a search for a first name alone would answer a
    different question from the one asked).
    """
    values, labels, missing = {}, {}, []
    for f in recipe.fields:
        labels[f.selector] = f.label
        if f.source == "full_name":
            text = (getattr(identity, "full_name", "") or "").strip()
        elif f.source == "first_name":
            text = (getattr(identity, "first_name", "") or "").strip()
        elif f.source == "last_name":
            text = (getattr(identity, "last_name", "") or "").strip()
        elif f.source == "email":
            emails = list(getattr(identity, "emails", None) or [])
            text = emails[0].strip() if emails else ""
        elif f.source == "phone":
            phones = list(getattr(identity, "phones", None) or [])
            text = phones[0].strip() if phones else ""
        elif f.source == "state":
            # Local import: this is the only search field source that needs
            # optout_forms's address parsing, and importing it at module
            # level would be an unused coupling for every broker that does
            # not need it.
            from broker_guard.optout_forms import state_from_addresses
            text = state_from_addresses(getattr(identity, "addresses", None))
        else:
            raise ValueError("unknown search field source: {!r}".format(f.source))
        if text:
            values[f.selector] = text
        elif f.required:
            missing.append(f.label)
    return {"values": values, "labels": labels, "missing": missing}


# --- reading the results page ------------------------------------------------

def ready_markers(recipe: SearchRecipe) -> tuple:
    """Wording whose appearance means the results page has settled.

    Defaults to "anything this broker says about having results or not
    having them". Until one of these shows up, whatever is on screen is an
    interstitial, a stale form, or somebody else's page -- and reading it is
    how a false negative gets made.
    """
    if recipe.ready_markers:
        return tuple(recipe.ready_markers)
    return tuple(recipe.hit_markers) + tuple(recipe.no_results_markers)


def page_is_ready(recipe: SearchRecipe, text: str | None) -> bool:
    haystack = (text or "").lower()
    return any(marker.lower() in haystack for marker in ready_markers(recipe))


def printed_result_count(recipe: SearchRecipe, text: str | None) -> int | None:
    """The result count the broker printed itself, or None.

    None means "this page did not state a count", which is NOT zero. The
    distinction is the whole safety property: a page that never printed a
    count cannot be used to conclude absence.
    """
    if not recipe.count_pattern:
        return None
    found = re.search(recipe.count_pattern, (text or "").lower())
    if not found:
        return None
    try:
        return int(found.group(1).replace(",", ""))
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return None


def _says(markers, text: str) -> bool:
    return any(m.lower() in text for m in (markers or ()))


def classify_search_page(recipe: SearchRecipe, text: str | None,
                         terms: list) -> dict:
    """``{"found": bool}`` or ``{"error": str}`` for one results page.

    Pure and browser-free, so every branch below is unit-tested against real
    page text captured from the live sites.

    The order is the safety argument, and it is not arbitrary:

    1. **A printed count wins.** "Found 0 results" is the broker stating
       absence in its own words; nothing else on the page can outrank that.
       A printed N>0 still has to be corroborated by the identity terms --
       if the broker says it has records but the person's name is nowhere on
       the page, the page is not the page we think it is, and that is an
       error, not a "yes" and certainly not a "no".
    2. **Then the no-results wording**, read off this broker's real miss
       page. Checked BEFORE any term matching because a miss page echoes the
       searched name (see the module docstring).
    3. **Then the hit wording**, and only together with a term match, does a
       page become ``{"found": True}``.
    4. **Everything else is an error.** Notably: a page with neither kind of
       marker. That is the "the site got redesigned and our selectors now
       mean nothing" case, and the whole point of this module is that it
       must not be reported as "not present".
    """
    haystack = (text or "").lower()
    if not haystack.strip():
        return {"error": "search results page was empty"}

    says_none = _says(recipe.no_results_markers, haystack)
    says_hit = _says(recipe.hit_markers, haystack)
    matched = is_people_search_hit({"snippet": text or ""}, list(terms or []))

    count = printed_result_count(recipe, haystack)
    if count is not None:
        if count == 0:
            return {"found": False}
        if matched:
            return {"found": True}
        return {"error": (
            "{} printed {} result(s) but none of the identity terms appear on "
            "the page -- refusing to guess".format(recipe.broker_id, count))}

    if says_none:
        if says_hit:
            return {"error": (
                "{} results page says both 'no results' and 'results' -- "
                "page shape not understood".format(recipe.broker_id))}
        return {"found": False}

    if says_hit:
        if matched:
            return {"found": True}
        return {"error": (
            "{} rendered a results page with no identity term on it -- "
            "refusing to read that as either present or absent".format(
                recipe.broker_id))}

    return {"error": (
        "{} results page matched no known marker (recipe may be stale)".format(
            recipe.broker_id))}
