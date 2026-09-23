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

UNITEDSTATESPHONEBOOK = SearchRecipe(
    broker_id="unitedstatesphonebook-com",
    broker_name="UnitedStatesPhoneBook",
    search_url="https://www.unitedstatesphonebook.com/",
    fields=(
        SearchField(selector="input[name='first']", source="first_name",
                    label="First name"),
        SearchField(selector="input[name='last']", source="last_name",
                    label="Surname"),
    ),
    # Scoped to the name form on purpose: input[name='Search'] alone matches
    # EIGHT controls on this homepage (the reverse-phone form's own Search,
    # plus six promo buttons named the same). page.click would still have
    # worked -- Page-level selectors are not strict, so it takes the first
    # match, which happens to be the right one -- but a recipe that relies
    # on DOM order is a recipe that breaks silently the day the site
    # reorders its homepage. --check-recipes is what caught this.
    submit_selector="form[action='search.php'] input[name='Search']",
    results_host="unitedstatesphonebook.com",
    no_results_markers=("there is no match in our free white pages database",),
    hit_markers=("results from our white pages database",),
    count_pattern=r"here are your ([\d,]+) results",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. A plain POST to search.php: "
        "a hit prints 'Here are your 200 results from our White Pages "
        "database: (200 is the max)' over name/address/phone rows, a "
        "nonsense name prints 'There is no match in our free White Pages "
        "database.'. Read that printed count as a CEILING, not a census -- "
        "the site caps at 200 and says so -- which costs nothing here, "
        "because the only question asked is whether the count is zero. The "
        "optional City and State boxes are left empty, per this module's "
        "rule about narrowing fields. This broker's OPT-OUT leg remains "
        "deliberately undecided (removal is a per-result 'Remove' button "
        "next to each listing rather than a form, which FormRecipe cannot "
        "express) -- see the note in optout_forms."
    ),
)

JUDYRECORDS = SearchRecipe(
    broker_id="judyrecords-com",
    broker_name="judyrecords",
    search_url="https://www.judyrecords.com/",
    fields=(
        SearchField(selector="input[name='search']", source="full_name",
                    label="Search"),
    ),
    submit_selector="form button[type='submit']",
    results_host="judyrecords.com",
    no_results_markers=("did not match any records",),
    hit_markers=("total cases for",),
    count_pattern=r"page 1 of ([\d,]+) total cases",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. One search box over 770m "
        "US court cases: a hit prints 'Page 1 of 558,065 total cases for: "
        "michael thompson' above case extracts, a nonsense name prints "
        "'Your search - <name> - did not match any records.'. Note what "
        "the count counts: CASES mentioning the name, not people, and the "
        "extracts are court filings rather than a broker's own profile of "
        "a person. That is still the right answer to this tool's question "
        "-- is this person's name published on this site -- but a nonzero "
        "count here means 'a court record naming them is indexed', not "
        "'this broker sells a dossier on them'. Its opt-out leg is "
        "recorded as infeasible: removal is court-order-only and by "
        "e-mail, see optout_forms.NO_OPTOUT_SURFACE."
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

SPOKEO = SearchRecipe(
    broker_id="spokeo-com",
    broker_name="Spokeo",
    search_url="https://www.spokeo.com/",
    fields=(
        # One box, whole name. The homepage offers Name/Email/Phone/Address
        # tabs; the default (Name) tab's form is the one addressed here, by
        # its id, because the page carries a SECOND, visually identical name
        # form further down whose input is named "name-search".
        SearchField(selector="#homepage_hero_form input[name='q']",
                    source="full_name", label="Name"),
    ),
    submit_selector="#homepage_hero_form button[type='submit']",
    results_host="spokeo.com",
    no_results_markers=(
        "did not match any results",
        "results not found for",
    ),
    hit_markers=("people named",),
    # "16,204 people named Michael Thompson found in California, Texas and
    # 49 other states." A one-hit page says "person named", hence the
    # alternation.
    count_pattern=r"([\d,]+)\s+(?:people|person)\s+named",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting the hero form "
        "navigates the SAME tab to https://www.spokeo.com/<First>-<Last> on "
        "the broker's own host -- no interstitial, no new tab. The miss page "
        "is titled 'Results Not Found for Zylphrenna Quixbottom - Spokeo' "
        "and reads 'Your search - Zylphrenna Quixbottom - did not match any "
        "results.'; the hit page is titled 'Michael Thompson (16,204 "
        "matches): ...' and prints '16,204 people named Michael Thompson "
        "found in California, Texas and 49 other states.' over real listing "
        "rows (name, age, city of residence, relatives, aliases), so the "
        "identity terms are genuinely on the page rather than only echoed. "
        "Rule (1) applies here too -- the MISS page also repeats the "
        "searched name, in its heading and its title -- so the no-results "
        "markers must be, and are, consulted first.\n"
        "\n"
        "Two live observations about driving it, neither of which changes "
        "the recipe but both of which cost time: an Osano consent banner "
        "renders over the hero area and steals focus from a raw coordinate "
        "click (a page.fill is unaffected), and a few seconds AFTER the "
        "miss page renders the site opens an affiliate pop-up tab at "
        "beenverified.com/lp/...?fn=Zylphrenna&ln=Quixbottom. That pop-up "
        "is why results_host is load-bearing on this broker as well as on "
        "ThatsThem: a driver reading 'whichever page is in hand' could end "
        "up reading BeenVerified's landing page as Spokeo's answer. The "
        "optional narrowing controls on the results page (First/Middle/"
        "Last/Age/State filters) are not touched."
    ),
)

WHITEPAGES = SearchRecipe(
    broker_id="whitepages-com",
    broker_name="Whitepages",
    search_url="https://www.whitepages.com/",
    fields=(
        SearchField(selector="#search-name", source="full_name",
                    label="Person name"),
    ),
    # The sibling "City, State, or ZIP" box (#search-location) is
    # deliberately NOT filled -- see the module docstring on narrowing.
    submit_selector="#wp-search",
    results_host="whitepages.com",
    no_results_markers=(
        "didn't find any results for",
    ),
    hit_markers=("people found",),
    # "John Smith 1000+ people found".
    count_pattern=r"([\d,]+)\+?\s+people\s+found",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting navigates the "
        "same tab to /name/<First>-<Last> on the broker's own host. The "
        "miss page reads 'Sorry, we didn't find any results for Zylphrenna "
        "Quixbottom' (plain ASCII apostrophe, confirmed by reading the "
        "character codes off the live page rather than assuming) and "
        "contains the string 'people found' NOWHERE, which is what makes "
        "that the safe hit marker. The hit page prints 'John Smith 1000+ "
        "people found' over real listing rows carrying UNMASKED full names, "
        "cities, aliases and relatives, so identity terms genuinely "
        "corroborate the count.\n"
        "\n"
        "The one thing worth knowing before touching this recipe: the HIT "
        "page (not the miss page) renders a consent gate -- 'Please accept "
        "to view results ... I agree to the Terms of Service and Privacy "
        "Policy / Continue to Results'. It is NOT clicked, and does not "
        "need to be: the result rows are already in the DOM and in "
        "inner_text('body') behind it, verified by counting 27 occurrences "
        "of the searched name and reading four full listing rows without "
        "touching the gate. That matters twice over -- accepting a "
        "broker's terms is a state-changing act this leg has no licence "
        "for, and a recipe that depended on clicking it would be one "
        "redesign away from reporting a false absence."
    ),
)

TRUEPEOPLESEARCH = SearchRecipe(
    broker_id="truepeoplesearch-com",
    broker_name="TruePeopleSearch",
    search_url="https://www.truepeoplesearch.com/",
    fields=(
        # The homepage carries five tabbed search forms (Name / Phone /
        # Address / Email / Neighbors) plus a mobile duplicate of each
        # ("id-m-*"), so the selector has to name the desktop NAME box
        # specifically rather than "the first text input".
        SearchField(selector="#id-d-n", source="full_name",
                    label="Enter name, phone or address"),
    ),
    # The sibling "City, State or Zip" box (#id-d-loc-name) is left empty.
    submit_selector="#btnSubmit-d-n",
    results_host="truepeoplesearch.com",
    no_results_markers=(
        "could not find any records for that search criteria",
    ),
    hit_markers=("records found for",),
    # "247 records found for John Smith".
    count_pattern=r"([\d,]+)\s+records\s+found\s+for",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting navigates the "
        "same tab to /results?name=... on the broker's own host, with no "
        "interstitial and no bot wall on either outcome. The miss page "
        "prints 'We could not find any records for that search criteria.' "
        "and is titled with the bare searched name; the hit page is titled "
        "'John Smith - Records Found' and prints '247 records found for "
        "John Smith' above ten free, UNMASKED rows (full name, age, city, "
        "previous cities, relatives).\n"
        "\n"
        "The two markers were chosen to be disjoint on purpose, because "
        "they are one word apart: the MISS page contains 'any records for' "
        "and the HIT page contains 'records found for', so a lazier hit "
        "marker of 'records for' would fire on both. Checked against the "
        "real text of both pages rather than reasoned about.\n"
        "\n"
        "Both result pages carry BeenVerified and InstantCheckmate "
        "'Sponsored Links' tables that repeat the searched name -- on the "
        "MISS page too ('Search current phone of Zylphrenna Quixbottom'). "
        "That is rule (1) in a new costume: the identity terms are present "
        "on a page that means 'not listed', which is why no-results markers "
        "are consulted before terms and why a term match alone can never "
        "produce a found."
    ),
)

MYLIFE = SearchRecipe(
    broker_id="mylife-com",
    broker_name="MyLife",
    search_url="https://www.mylife.com/",
    fields=(
        # One box, whole name. The page carries a mobile duplicate of this
        # form (#search-form-mobile / #single-search-input-mobile), so the
        # desktop one is named explicitly.
        SearchField(selector="#single-search-input", source="full_name",
                    label="Enter any name"),
    ),
    submit_selector="#search-form input[type='submit']",
    results_host="mylife.com",
    no_results_markers=(
        "we didn't find",
    ),
    hit_markers=("results for",),
    # "We Found 100 Results for John Smith".
    count_pattern=r"we\s+found\s+([\d,]+)\s+results",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting navigates the "
        "same tab to /pub-multisearch.pubview?... on the broker's own host. "
        "The miss page says 'We didn't find Zylphrenna Quixbottom. Please "
        "check the spelling and other information and search again' "
        "(plain ASCII apostrophe, read off the live page); the hit page "
        "says 'We Found 100 Results for John Smith' over free, UNMASKED "
        "rows carrying full name, age, city, ZIP+4, aliases and places "
        "lived. Both markers were checked against BOTH pages: the miss "
        "page does not contain 'results for' and the hit page does not "
        "contain \"we didn't find\".\n"
        "\n"
        "The hidden companions of the visible box (searchFirstName, "
        "searchLastName, searchLocation) are populated by the site's own "
        "JS from what is typed, so the recipe fills only the visible "
        "input. searchLocation is one of them, and it stays empty -- the "
        "usual narrowing rule.\n"
        "\n"
        "Its OPT-OUT leg is out of scope; see optout_forms, and note that "
        "the dataset's opt-out URL for this broker (/ccpa/index.pubview) "
        "is a hard 404 today."
    ),
)

FASTPEOPLESEARCH = SearchRecipe(
    broker_id="fastpeoplesearch-com",
    broker_name="FastPeopleSearch",
    search_url="https://www.fastpeoplesearch.com/",
    fields=(
        # One box, whole name. The form's second input (#search-name-address,
        # "City, State or ZIP") is the optional narrowing box the module
        # docstring refuses to fill.
        SearchField(selector="#search-name-name", source="full_name",
                    label="Name"),
    ),
    # The name form carries TWO buttons -- a "Close" button[type=button] and
    # the real one -- so it is addressed by the submit button's own class
    # inside that one form. Both selectors resolve to exactly 1 element,
    # checked live on 2026-09-23.
    submit_selector="#form-search-name button.search-form-button-submit",
    results_host="fastpeoplesearch.com",
    no_results_markers=(
        "no results found for",
        "we could not find any results based on your search criteria",
    ),
    # Deliberately NOT "results found": the MISS page says "No results found
    # for <name>", so that marker would fire on both pages. "records found
    # for" appears only on the hit page.
    hit_markers=("records found for",),
    # "Over 100+ FREE public records found for John Smith." The whitespace is
    # ragged in the HTML ("public  records found\n\t\t\tfor"), which does not
    # matter because search_probe reads inner_text("body") and the browser
    # collapses it -- but the pattern is written tolerantly anyway.
    count_pattern=r"([\d,]+)\s*\+?\s*free\s+public\s+records\s+found",
    verified_on="2026-09-23",
    notes=(
        "Verified live both ways on 2026-09-23. Submitting is a plain GET to "
        "/search which lands on /name/<first>-<last> on the broker's own "
        "host. 'John Smith' answers HTTP 200 titled 'John Smith in | Fast "
        "and Free People Search of Public Records' and prints 'Over 100+ "
        "FREE public records found for John Smith.' above real listing cards "
        "(name, age, city, past addresses, relatives) with no paywall in "
        "front of them. 'Zylphrenna Quixbottom' answers HTTP 404 titled "
        "'Free People Search | FastPeopleSearch.com' and prints 'No results "
        "found for Zylphrenna Quixbottom' plus 'We could not find any "
        "results based on your search criteria:' over an echo of the parsed "
        "query (First Name: zylphrenna / Last Name: quixbottom).\n"
        "\n"
        "Two traps on that miss page, both handled by the existing ordering "
        "rather than by anything new here. (1) It echoes the searched name "
        "more than a dozen times, in 'Get current phone number for "
        "Zylphrenna Quixbottom' teaser rows -- rule (1) of the module "
        "docstring, and the no-results markers are consulted first. (2) "
        "Those teaser rows are third-party paid placements ('Paid Results "
        "Sponsored by TruthFinder.com', then BeenVerified.com, then "
        "InstantCheckMate.com) that appear on the MISS page and not on the "
        "hit page, which is the opposite of the usual shape; they carry no "
        "marker of either kind, so they change nothing. The 404 status is "
        "the broker's own 'not listed' answer, not a bot wall, and "
        "browser.bot_wall_reason does not treat 404 as one.\n"
        "\n"
        "Its opt-out leg is NOT automatable -- the removal form is behind a "
        "one-time emailed link; see optout_forms.OPTOUT_OUT_OF_SCOPE."
    ),
)

RECIPES = {
    THATSTHEM.broker_id: THATSTHEM,
    MYLIFE.broker_id: MYLIFE,
    TRUEPEOPLESEARCH.broker_id: TRUEPEOPLESEARCH,
    SPOKEO.broker_id: SPOKEO,
    WHITEPAGES.broker_id: WHITEPAGES,
    SEARCHPEOPLEFREE.broker_id: SEARCHPEOPLEFREE,
    USPHONEBOOK.broker_id: USPHONEBOOK,
    ADVANCEDBACKGROUNDCHECKS.broker_id: ADVANCEDBACKGROUNDCHECKS,
    SEARCHPUBLICRECORDS.broker_id: SEARCHPUBLICRECORDS,
    CYBERBACKGROUNDCHECKS.broker_id: CYBERBACKGROUNDCHECKS,
    JUDYRECORDS.broker_id: JUDYRECORDS,
    NATIONALPUBLICDATA.broker_id: NATIONALPUBLICDATA,
    PRIVATENUMBERCHECKER.broker_id: PRIVATENUMBERCHECKER,
    REVEALPHONEOWNER.broker_id: REVEALPHONEOWNER,
    UNITEDSTATESPHONEBOOK.broker_id: UNITEDSTATESPHONEBOOK,
    FASTPEOPLESEARCH.broker_id: FASTPEOPLESEARCH,
}


# Brokers that were investigated for this pilot and found to have NO usable
# public presence-search surface. Recorded as data, with the reason, so the
# next person does not spend the evening re-discovering it -- and so nobody
# "fixes" the gap by writing a recipe against a page that cannot answer the
# question. These are notes, not behaviour: nothing reads this at runtime.
NO_SEARCH_SURFACE = {
    "onetrust-com": (
        "The dataset files this one under the NAME 'Nielsen' but gives the "
        "url onetrust.com, and onetrust.com is not Nielsen -- it is the "
        "privacy-portal SaaS vendor whose webform Nielsen's opt-out is "
        "hosted on. The search leg has to be decided against the site the "
        "dataset actually points at, and that site is a B2B marketing "
        "site with no people lookup. Verified 2026-09-23: www.onetrust.com "
        "carries one search form, GET /search/ with a single "
        "input[name='q'] whose placeholder is 'Search keyword'. Submitting "
        "the nonsense name 'Zylphrenna Quixbottom' returned the string "
        "'928 results(s) found', and the results are OneTrust's own "
        "corporate pages -- 'Zendesk (Integrations)', 'Red Clover "
        "Advisors (Partner Locator)', 'TrustWeek 2026' -- not person "
        "records. That 928 is the reason this is NO_SEARCH_SURFACE and "
        "not an UNDECIDED: a content search that returns most of the site "
        "for a name nobody has would report a confident HIT for every "
        "person alive. Absence here can never be stated by the broker, so "
        "there is nothing to build a recipe against. Its opt-out leg is a "
        "real OneTrust-hosted webform and is recorded separately."
    ),
    "lexisnexis-com": (
        "LexisNexis Risk Solutions sells risk/identity data to licensed "
        "businesses; it has no public people lookup. Verified 2026-09-23 "
        "across both of its domains, because the dataset's url "
        "(lexisnexis.com) is not even the right site: lexisnexis.com "
        "redirects to /en-us/gateway.page, a 'Choose Your Path' splash "
        "whose only two inputs are a 'remember my choice' checkbox and its "
        "hidden partner -- no search of any kind. The actual broker's site, "
        "risk.lexisnexis.com, carries one search box and it is a site "
        "search ('Search for Products, Resources, and More'), the rest "
        "being OneTrust cookie-consent controls. Same shape as "
        "equifax-com and chexsystems-com: what this company holds about a "
        "person is reached through an identity-verified consumer "
        "disclosure request, not a name box."
    ),
    "equifax-com": (
        "Equifax is a nationwide consumer reporting agency under the FCRA, "
        "and the dataset carries it as 'Equifax Marketing Services'. "
        "Either way there is no public people lookup. Verified "
        "2026-09-23: equifax.com's entire homepage carries exactly ONE "
        "input, and it is the site's own content search -- "
        "input[name='efxNavSiteSearchQuery'], placeholder 'Search "
        "Personal' -- which searches Equifax's web pages, not people. "
        "Same finding as chexsystems-com: what Equifax holds about a "
        "person is reached by requesting your own file with "
        "identity-verified credentials, which this tool must not "
        "automate, not by typing a stranger's name into a box."
    ),
    "epsilon-com": (
        "Epsilon Data Management is a B2B marketing-services and "
        "consumer-data licensing business, not a consumer people-search "
        "site. Verified 2026-09-23: epsilon.com/us carries 19 form inputs "
        "and not one of them is a people lookup -- they are a 'Contact us' "
        "lead form (firstName, lastName, emailAddress, title, company, "
        "comments, a marketing opt-in and reCAPTCHA) plus OneTrust cookie-"
        "consent checkboxes and that widget's own vendor-search box. There "
        "is no name/email/phone lookup anywhere on the domain to build a "
        "recipe against. Its opt-out leg is a real DSAR form and is "
        "recorded separately."
    ),
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
    "searchusapeople-com": (
        "Verified 2026-09-23: an InfoTracer front, on both legs. Its only "
        "search form is <form name='nameForm' "
        "action='https://infotracer.com/loading/' method='get'> carrying "
        "hidden affiliate parameters (source=somecotton, addPixel=yes), so "
        "pressing Search asks InfoTracer, not this site -- and InfoTracer's "
        "own search leg is separately undecided above. Its "
        "/data-removal-request/ page says the same thing in words: 'For "
        "removal requests / opt-out, please visit InfoTracer and follow "
        "their instructions.'\n"
        "\n"
        "Worth recording separately, because it wasted an hour and will "
        "waste somebody else's: every Playwright visit to this domain "
        "returned HTTP 429 from Cloudflare with an EMPTY body, which reads "
        "like self-inflicted rate limiting. It is not. The 429 is keyed to "
        "the User-Agent: curl with this tool's production UA ('X11; Linux "
        "x86_64') gets 429 every time, while the same request with a "
        "Windows Chrome UA, or with plain curl/8.0, gets 200. The response "
        "even carries 'vary: ... User-Agent'. So this domain refuses "
        "broker-guard's UA outright, which also means its plain homepage "
        "presence check can only ever error. The production UA was "
        "deliberately NOT changed to work around this: every other shipped "
        "recipe's host was re-checked under both UAs and none behaves "
        "differently -- except thatsthem.com, which answers the Linux UA "
        "with 200 and the Windows one with 403, i.e. the current UA is the "
        "better of the two."
    ),
    "addresses-com": (
        "Verified 2026-09-23: an Intelius front, on both legs, and it does "
        "not even pretend otherwise once you press the button. Its "
        "<form name='people-search'> (firstName, lastName, an optional "
        "state <select>) carries no action attribute -- the handler is "
        "JavaScript -- and submitting it navigates the tab straight off "
        "the domain to https://www.intelius.com/results/?utm_source=ADDRS&"
        "traffic[source]=ADDRS&...&traffic[funnel]=bg&firstName=zylphrenna&"
        "lastName=quixbottom. The affiliate tag (ADDRS) is baked into the "
        "destination. Nothing on addresses.com answers whether "
        "addresses.com lists anyone, and Intelius's own search leg is "
        "separately undecided above. Its footer agrees about ownership: "
        "every privacy link on the page points at intelius.com."
    ),
    "pipl-com": (
        "Verified 2026-09-23: Pipl no longer sells a consumer people "
        "search, and its own homepage says so -- it is titled 'Fraud "
        "intelligence for enterprise risk decisions' and carries ZERO "
        "<form> elements and zero inputs of any kind, checked both in a "
        "browser after the page's JavaScript had run and in the raw HTML. "
        "Every 'Search' link on it goes to marketing copy "
        "(/solutions/search), API documentation "
        "(docs.pipl.com/docs/welcome-to-the-pipl-search-api) or the "
        "login-gated customer product at search.pipl.com/accounts/login/, "
        "which serves no form to an unauthenticated visitor either. There "
        "is nothing on this domain that answers whether it holds a record "
        "on someone. Its OPT-OUT leg is a different story and ships as a "
        "working recipe; see optout_forms.PIPL."
    ),
    "peoplefinder-com": (
        "Verified 2026-09-23: PeopleFinder.com is an Intelius front, and "
        "its own page says so ('PeopleFinder.com powered by Intelius'). "
        "Its search form's action is https://tracking.intelius.com/ with "
        "affiliate codes baked in as hidden inputs, and submitting it "
        "(after the FCRA modal that otherwise blocks the button) lands on "
        "intelius.com/search/?affid=1117...&s1=www.peoplefinder.com, "
        "titled 'Searching for Michael Thompson in ALL - Intelius'. "
        "Nothing on peoplefinder.com itself can answer whether "
        "peoplefinder.com lists a person. Its opt-out link goes to "
        "Intelius's privacy centre for the same reason."
    ),
    "easyoptouts-com": (
        "Not a data broker at all -- the dataset's row name is literally "
        "'Additional Options (Paid and Free)', a section heading from an "
        "opt-out guide that got swept in as if it were a company. "
        "Verified 2026-09-23: easyoptouts.com is a PAID REMOVAL SERVICE "
        "($19.99/yr) that removes people FROM brokers; the page is sign- "
        "up CTAs, a comparison table and press quotes, with no name box "
        "anywhere. There is nothing here that could hold a record about "
        "anyone, so there is nothing to search."
    ),
    "epic-org": (
        "Not a data broker -- EPIC is the Electronic Privacy Information "
        "Center, a Washington DC privacy-advocacy nonprofit, and the "
        "dataset row is named 'Additional Resources' because a link to "
        "one of EPIC's reports was scraped as though it were a broker "
        "entry. Verified 2026-09-23: epic.org and "
        "epic.org/issues/consumer-privacy/data-brokers/ carry issue "
        "pages, litigation/amicus content, a surveillance campaign banner "
        "and donation asks. No person-search input exists anywhere on the "
        "site."
    ),
    "adelement-com": (
        "AdElement Vast, LLC is a B2B programmatic DSP. Verified "
        "2026-09-23: adelement.com fronts itself as an 'AI-Powered DSP "
        "for Advertisers' and sells to advertisers and app developers -- "
        "case studies, eCPM figures, publisher logos (Audiomack, "
        "TrueCaller, Zynga). There is no individual-lookup input of any "
        "kind, so no presence check is possible here."
    ),
    "adept-id-com": (
        "AdeptID is a B2B talent-matching / workforce-hiring vendor, not "
        "a people-search site. Verified 2026-09-23: www.adept-id.com is "
        "an AI hiring-match platform page with SOC2/GDPR/CCPA compliance "
        "messaging, 'Schedule a demo' CTAs and partner logos (UKG, Year "
        "Up, Avionte). No name or person search input exists."
    ),
    "adikteev-com": (
        "Adikteev is a B2B mobile-app retargeting/growth adtech vendor. "
        "Verified 2026-09-23: adikteev.com sells churn-based bidding and "
        "app retargeting to brands (McDonald's, Blizzard, Fanatics, King, "
        "Playtika are named as clients) and its footer's only privacy "
        "affordance is a generic 'Your personal data' link. No person- "
        "lookup input exists."
    ),
    "adsquare-com": (
        "Adsquare is a B2B 'real-world data intelligence' adtech "
        "platform. Verified 2026-09-23: adsquare.com is organised around "
        "an 'Outcomes Loop' marketing framework and names 2,500 "
        "brand/agency/platform customers (IKEA, Coca-Cola, McDonald's, "
        "WPP, Trade Desk, DV360). Nothing on it accepts a person's name; "
        "the data it holds is reached by its buyers, not by the public."
    ),
    "4-eyes-ai": (
        "Verified 2026-09-23, and the first thing to know is that the "
        "domain moved: www.4-eyes.ai 301-redirects to delivr.ai, the B2B "
        "intent-data / identity-resolution vendor that absorbed 4Eyes "
        "after its 2025 acquisition. The only input on the landing page "
        "is a 'Look up' box that takes the VISITOR'S OWN work email and "
        "previews that visitor's own intent profile -- it cannot be "
        "pointed at a third party's name, so it is not a people-search "
        "surface. No name lookup exists on either domain."
    ),
    "4legalleads-com": (
        "4LegalLeads is a B2B legal-lead-generation marketplace that "
        "sells case leads to attorneys. Verified 2026-09-23: "
        "www.4legalleads.com is organised by practice area (auto "
        "accident, DUI, bankruptcy) and its only interactive elements are "
        "attorney login/signup and quote-request buttons. There is no "
        "search box of any kind, so there is no way to ask whether a "
        "person is in its lead pool."
    ),
    "5x5data-com": (
        "5X5 US, LLC runs a member-driven marketing data cooperative / "
        "identity graph sold to marketing, sales, HR and fraud-detection "
        "clients. Verified 2026-09-23: the only input on 5x5data.com is a "
        "generic WordPress content search labelled 'Search for:', which "
        "searches the company's own pages. No person lookup exists."
    ),
    "abovedata-io": (
        "Above Data describes itself as a 'signal layer for consumer data "
        "infrastructure' sold to enterprise data owners, platforms and "
        "brands. Verified 2026-09-23: the only input on www.abovedata.io "
        "is an email-capture box labelled 'Leave us your email' under a "
        "'Get Access' heading. No person-name search exists."
    ),
    "accudata-com": (
        "Verified 2026-09-23, and the domain has moved: accudata.com "
        "301-redirects to deepsync.com/accudata/, a Deep Sync "
        "product/leadership page selling identity resolution to brands, "
        "agencies and platforms. The page carries nav menus, executive "
        "LinkedIn links and 'Book a demo' / 'Talk with us' CTAs and no "
        "input that takes a person's name. Its opt-out leg redirects into "
        "Deep Sync's privacy portal for the same reason and is recorded "
        "separately."
    ),
    "accurateappend-com": (
        "Accurate Append is a B2B data-append and contact-verification "
        "vendor. Verified 2026-09-23: accurateappend.com's only "
        "interactive elements are Contact Us, Self-Service Sign Up, Self- "
        "Service Login and an API Trial Key Request. There is no person- "
        "name input anywhere on the page -- the data is reached through "
        "the client API, not a public box."
    ),
    "activimpact-ai": (
        "Activimpact is a B2B 'AI-enabled multi-channel performance "
        "platform for independent agencies' -- adtech sold to agencies, "
        "not a consumer site. Verified 2026-09-23: activimpact.ai's only "
        "CTAs are 'Get started' and 'Book a Demo', both pointing at "
        "/contact. No person-name search input exists."
    ),
    "idology-com": (
        "Verified 2026-09-23, and the domain has moved: www.idology.com "
        "301-redirects to www.gbg.com/en-us/?rd=ido, IDology having been "
        "absorbed into GBG's brand. The landing page sells enterprise "
        "identity verification, KYC and fraud prevention (IBM, Mastercard "
        "and HSBC are named as customers) and carries only a generic "
        "site-content search box. No person lookup exists on either "
        "domain."
    ),
    "adadapted-com": (
        "AdAdapted is a CPG shopper-marketing adtech platform ('The "
        "Action Layer') selling to brands, agencies and retailers. "
        "Verified 2026-09-23: the only form on www.adadapted.com is an "
        "email newsletter signup. No person-name search input exists -- "
        "and per its own opt-out page it does not even hold email "
        "addresses, only mobile advertising identifiers, so a name could "
        "never be the key here."
    ),
    "addefend-com": (
        "AdDefend GmbH is a German (Hamburg) anti-adblock advertising "
        "vendor, selling to publishers (Der Spiegel, Welt, Finanzen.net) "
        "and advertisers (Panasonic, Babbel, A.T.U.). Verified "
        "2026-09-23: www.addefend.com/en/ carries no search inputs or "
        "lookup forms of any kind. Its business is served ads, not person "
        "records, so there is nothing to look a person up in."
    ),
    "adstradata-com": (
        "Adstra sells identity resolution and audience licensing to "
        "agencies, brands and publishers -- 'Marketing solutions for "
        "identity, activation, and everything in between', built on its "
        "Conexa identity graph. Verified 2026-09-23: adstradata.com's "
        "homepage carries no name-search or lookup input, only a 'Get "
        "Started' CTA that leads to a B2B contact form."
    ),
    "advcredit-com": (
        "Advantage Credit, Inc. is a B2B mortgage-credit-report and "
        "verification vendor for lenders and loan officers. Verified "
        "2026-09-23: www.advcredit.com carries no person-search input at "
        "all -- the only paths off the homepage are a customer login and "
        "credential-gated mortgage-credit / background-screening portals. "
        "Nothing public can answer whether it holds a given person."
    ),
    "take5mg-com": (
        "Verified 2026-09-23: the domain is effectively gone. "
        "take5mg.com, www.take5mg.com and the plain-http form all fail "
        "identically with an EXPIRED TLS CERTIFICATE, and a search result "
        "for the domain is titled 'take5mg.com Domain for sale', so the "
        "host is parked rather than serving Take 5 Media Group content "
        "(Take 5 was acquired by Advantage Solutions in 2018, which is "
        "why the dataset files it under Advantage Sales & Marketing LLC). "
        "A dead, parked domain has no search surface."
    ),
    "smartsheet-com": (
        "The dataset's domain is not this broker. Verified 2026-09-23: "
        "smartsheet.com is Smartsheet, the SaaS work-management platform "
        "(project management, workflow automation, dashboards, "
        "Salesforce/Microsoft/Slack integrations) -- AdvisorTarget "
        "appears nowhere on it. The row exists only because "
        "AdvisorTarget's opt-out happens to be a Smartsheet-HOSTED form, "
        "and that form is recorded on the opt-out leg. Smartsheet's own "
        "site has no person lookup, and the search leg has to be decided "
        "against the site the dataset actually points at."
    ),
    "finsum-com": (
        "Verified 2026-09-23: www.finsum.com is a financial-news and "
        "insights content site ('Your go-to source for the latest "
        "insights and trends in finance' -- equities, bonds, wealth "
        "management), which is AdvisorTarget's publishing front rather "
        "than a lookup product. The only form on it is a newsletter "
        "signup; the nav carries a search ICON but no rendered search "
        "input, and in any case a content search of a news site cannot "
        "answer whether a person is in AdvisorTarget's advisor data."
    ),
    "affinityanswers-com": (
        "AffinityAnswers sells 'Affinity Verified Data' -- audience- "
        "targeting segments built from social engagement signals across "
        "Facebook, Reddit, X, YouTube, Instagram and TikTok -- to "
        "programmatic, CTV, social and DOOH advertisers. Verified "
        "2026-09-23: www.affinityanswers.com carries no person-search "
        "input, only a 'Let's Chat' CTA and the address "
        "cs@affinityanswers.com."
    ),
    "affinity-solutions": (
        "Affinity Solutions sells consumer-purchase analytics (it claims "
        "195M+ consumer transaction records used for marketing "
        "measurement) to banks, brands, retailers and agencies. Verified "
        "2026-09-23: www.affinity.solutions carries no person-lookup "
        "input; its only form is B2B lead capture (name, email, company, "
        "industry, job title). Its data is reached by its buyers, never "
        "by the public."
    ),
    "agrmarketingsolutions-com": (
        "AGR Marketing Solutions is a B2B intent-data and marketing "
        "vendor -- its own products are named Digital Intent, Financial "
        "Will & Means, SmartMailBox and data appending -- and it sells to "
        "businesses, not consumers. Verified 2026-09-23: "
        "agrmarketingsolutions.com carries no person-search or lookup "
        "input. (The homepage DOES carry an opt-out form; that is a "
        "separate finding on the opt-out leg.)"
    ),
    "aidentified-com": (
        "Aidentified is a B2B sales-intelligence and prospecting platform "
        "aimed at wealth managers and financial advisors. Verified "
        "2026-09-23: www.aidentified.com advertises '300M+ profiles' but "
        "every route to them is gated -- the homepage's only controls are "
        "'Start your free trial', 'Request demo', 'Log in' and 'Start for "
        "free'. There is no public name-search box, so no unauthenticated "
        "presence check is possible."
    ),
    "arccorp-com": (
        "Airlines Reporting Corporation is a B2B ticket-settlement and "
        "distribution platform for airlines and travel agencies. Verified "
        "2026-09-23: arccorp.com 302-redirects to www2.arccorp.com, an "
        "industry site about NDC distribution and ticket settlement with "
        "no people-search feature of any kind."
    ),
    "aisinfo-com": (
        "AIS Portfolio Services is a B2B financial-services operations "
        "vendor -- bankruptcy and deceased-data services, loan-servicing "
        "operations, staffing and automation -- sold to banks and "
        "lenders. Verified 2026-09-23: www.aisinfo.com carries no person "
        "lookup; the only non-marketing route off it is a client-only "
        "'AIS ONLINE LOGIN' and a general Contact Us link."
    ),
    "alikeaudience-com": (
        "AlikeAudience is a B2B adtech vendor selling AI-driven audience "
        "segmentation, onboarding and activation to agencies, brands, "
        "marketers and platforms, with DSP integrations (The Trade Desk, "
        "Amazon) as its distribution. Verified 2026-09-23: "
        "alikeaudience.com carries no public person-lookup input."
    ),
    "agrgroupinc-com": (
        "Verified 2026-09-23: the domain does not resolve. Both "
        "agrgroupinc.com and www.agrgroupinc.com fail DNS with "
        "getaddrinfo ENOTFOUND, so no page of this broker exists to carry "
        "a search surface. The company is real -- CA data-broker "
        "registration 186616, All Global Resources, LLC, Henderson NV, "
        "privacy@agrgroupinc.com -- but it is a registration with no live "
        "website, which is also why the dataset lists it as email-only. "
        "If the domain ever comes back this call should be revisited."
    ),
    "termly-io": (
        "The dataset files this row under the NAME '01Advertising Inc.' "
        "but gives the url termly.io, and termly.io is not 01Advertising "
        "-- it is the Termly SaaS consent-management vendor whose hosted "
        "DSAR widget 01Advertising's opt-out link points at. Same shape "
        "as the onetrust-com row. The search leg has to be decided "
        "against the site the dataset actually points at, and verified "
        "2026-09-23 that site is a B2B compliance suite (policy "
        "generators, cookie banners, DSAR tooling sold to other "
        "businesses) with no search bar or person-lookup input anywhere "
        "in its nav or homepage. 01Advertising's own domain is a separate "
        "row, 01advertising-com."
    ),
    "remodeling-com": (
        "Verified 2026-09-23: remodeling.com is a contractor-matching and "
        "lead-generation directory ('Find a Pro', cost guides, browse by "
        "project category), and its only search surface matches "
        "homeowners to CONTRACTORS by project type and location. There is "
        "no name-based person input, so nothing here can answer whether a "
        "given person is in 33 Mile Radius / EverCommerce's lead data. "
        "Its opt-out leg is a real CCPA form and is recorded separately."
    ),
    "33mileradius-com": (
        "Verified 2026-09-23: www.33mileradius.com is a contractor lead- "
        "generation network that routes homeowner phone calls to "
        "contractors, and its only form is a CONTRACTOR intake ('Start "
        "Booking Jobs Today!' -- First/Last Name, Email, Phone Number, "
        "Company Name, a consent checkbox). That form creates a business "
        "account; it does not look anyone up. No person-search surface "
        "exists."
    ),
    "attribits-com": (
        "All Good Media's attribits.com is a B2B ad/data-services vendor "
        "selling audience expansion, identity resolution and second-party "
        "data to marketers. Verified 2026-09-23: the homepage's only call "
        "to action is 'contact us'; there is no name-lookup or people- "
        "search input of any kind."
    ),
    "allwebleads-com": (
        "Verified 2026-09-23, and the domain has moved: allwebleads.com "
        "301-redirects to awl.com, a B2B insurance lead-generation and "
        "agent marketplace that connects consumers with licensed agents "
        "and claims 15,000+ agents. No person-lookup form appears on the "
        "homepage -- consumers arrive through quote funnels, they are not "
        "searched for."
    ),
    "allantgroup-com": (
        "Allant Group sells an enterprise 'audience management platform' "
        "and advertises '11B+ composable consumer data points' to "
        "marketers. Verified 2026-09-23: the only form on allantgroup.com "
        "is a newsletter signup (First Name, Last Name, Work Email). "
        "There is no person-search surface; the data is reached by its "
        "enterprise buyers."
    ),
    "alliantinsight-com": (
        "Alliant Cooperative Data Solutions sells marketing-data products "
        "-- its own names for them are PeopleCore, PurchaseCore and "
        "ProfessionalsCore -- to brands, agencies and publishers. "
        "Verified 2026-09-23: alliantinsight.com has no consumer person- "
        "lookup feature on its homepage. Note this is the same corporate "
        "family as analytics-iq-com, whose opt-out routes to the same "
        "OneTrust portal."
    ),
    "alphonso-tv": (
        "Alphonso (now LG Ads) is a B2B adtech and CTV measurement "
        "company built on automatic content recognition data from smart "
        "TVs. Verified 2026-09-23: alphonso.tv carries no person-search "
        "feature on its homepage. What it holds is keyed to TV devices, "
        "not to names, which is also why its consumer privacy page offers "
        "only device-scoped choices."
    ),
    "altairdata-com": (
        "Altair Data Resources is a B2B credit and marketing-data company "
        "-- tri-bureau credit data and a 'DataCloud' platform -- selling "
        "to financial institutions and agencies. Verified 2026-09-23: "
        "altairdata.com carries no person-lookup form on its homepage."
    ),
    "altisource-com": (
        "Altisource is a B2B mortgage and real-estate services provider "
        "(Hubzu auctions, Trelix fulfillment, Equator workflow, Premium "
        "Title) serving institutional servicers, originators and "
        "investors. Verified 2026-09-23: altisource.com carries no "
        "person-search feature. Its consumer-facing obligations are "
        "FCRA/GLBA-shaped affiliate-sharing choices, not a public lookup."
    ),
    "altrata-com": (
        "Altrata (WealthEngine, Wealth-X, BoardEx) sells wealth "
        "intelligence to fundraisers, banks and sales teams. Verified "
        "2026-09-23: altrata.com is a B2B platform page with nav, 'Let's "
        "connect' / 'Get started' demo CTAs and a generic site-search "
        "icon. It advertises '100M+ people profiles' -- and every route "
        "to them is gated behind a login or a demo request, with nothing "
        "exposed to an anonymous visitor. No public presence check is "
        "possible."
    ),
    "aspire-north-com": (
        "American Spirit Data Solutions describes itself on www.aspire- "
        "north.com as 'hybrid strategists, marketers, and experts' "
        "running B2B audience and campaign services on licensed Experian "
        "data. Verified 2026-09-23: the homepage carries no public search "
        "box for an individual. Note the company's live privacy content "
        "actually lives on americanspiritcorp.com, which is where its "
        "opt-out leg had to be chased."
    ),
    "amerilist-com": (
        "Amerilist is a mailing-list broker -- targeted mailing, "
        "telemarketing and email lists, data processing, data "
        "enhancement. Verified 2026-09-23: the one tool on "
        "www.amerilist.com is '24/7 Interactive List Counts', which "
        "builds BULK lists by selection criteria and returns counts. A "
        "count tool cannot be asked about a named individual, so there is "
        "no presence check here even though the site is plainly full of "
        "people."
    ),
    "amplemarket-com": (
        "Amplemarket is a B2B sales-intelligence 'AI Sales Copilot' for "
        "sales teams. Verified 2026-09-23: prospect data is reachable "
        "only behind a login or a business free-trial signup, and "
        "www.amplemarket.com exposes no public person-lookup surface to a "
        "consumer."
    ),
    "analytics-iq-com": (
        "AnalyticsIQ (now part of Alliant) is a B2B people-based "
        "marketing-data vendor whose flagship dataset, PeopleCore, it "
        "advertises as covering 264M+ individuals, licensed to "
        "enterprises through Snowflake and LiveRamp. Verified 2026-09-23: "
        "analytics-iq.com has no public interface for looking up an "
        "individual record. Same corporate family as alliantinsight-com."
    ),
    "anchorcomputer-com": (
        "Anchor Computer is a B2B marketing-data-services provider -- "
        "data validation and cleansing, customer profiling and "
        "segmentation, enrichment, database design. Verified 2026-09-23: "
        "anchorcomputer.com routes business clients to 'Contact Us' or a "
        "client portal login and exposes no public person-name lookup. "
        "(Its opt-out surface, by contrast, is a real and unusually "
        "detailed form; see the opt-out leg.)"
    ),
    "01advertising-com": (
        "Verified as far as this tool may go, 2026-09-23. Both https and "
        "http fetches of www.01advertising.com failed outright with no "
        "response, so the homepage was never rendered here -- but the "
        "call is still no-surface rather than undecided for a reason the "
        "search leg can stand on: every independent description of "
        "01Advertising is of an AI-driven B2B audience builder that "
        "constructs ad-targeting segments from CLIENTS' own "
        "CRM/CDP/first-party data (it is on the Texas data-broker "
        "registry on that basis). A vendor whose input is its customers' "
        "data has nothing for a stranger to search, and no source "
        "anywhere references a person-lookup feature. If a future pass "
        "renders the site and finds a name box, overturn this. Its opt- "
        "out leg is genuinely undecided and is recorded separately."
    ),
    "33across-com": (
        "Verified as far as this tool may go, 2026-09-23: every 33Across "
        "URL tried returned HTTP 403 to this fetcher, including the "
        "homepage. The call rests on what 33Across unambiguously is -- a "
        "supply-side programmatic advertising platform (an SSP, with "
        "cookieless identity resolution branded 'Lexicon') selling to "
        "publishers and DSPs -- a category that has no consumer lookup, "
        "and no source references one. This is recorded as no-surface "
        "rather than undecided because the business model settles it; the "
        "opt-out leg, where the mechanics actually matter, is recorded as "
        "undecided precisely because the 403 blocks what needs to be "
        "seen."
    ),
    "180bytwo-com": (
        "Verified 2026-09-23, and the domain has been absorbed: "
        "180bytwo.com's root 302-redirects wholesale to anteriad.com, "
        "180byTwo having been folded into Anteriad. anteriad.com is a B2B "
        "marketing and demand-generation platform (the 'Anteriad "
        "Marketing Cloud', BDR-as-a-service, audience identification sold "
        "to marketers) whose only search affordance is a generic nav "
        "placeholder -- no person lookup anywhere. Note for anyone re- "
        "checking: the root redirects, but the dataset's deeper privacy "
        "path still resolves on the old domain, which is how the opt-out "
        "leg was read."
    ),
    "andrewswharton-com": (
        "Andrews Wharton is a B2B data-driven marketing vendor -- "
        "audience targeting, data enhancement, email marketing, analytics "
        "-- and its own footer brands it 'A Stirista Solution'. Verified "
        "2026-09-23: www.andrewswharton.com carries no public people- "
        "search tool; the only form on it is a newsletter signup for "
        "product news. Its sibling domain stirista.com is walled and "
        "recorded separately."
    ),
    "verinext-com": (
        "The dataset files this row as 'Anexinet Corp.' and Verinext is "
        "the merged successor, but either way it is not a people-search "
        "company: verified 2026-09-23, Verinext is a business technology "
        "services firm (enterprise AI, data protection, infrastructure, "
        "automation, networking, security, managed services). The only "
        "form reached on the site is a B2B sales contact form -- Name, "
        "Business Email, Phone Number, Job Title, Company Name, two "
        "dropdowns and a Message -- with no lookup of any kind. Caveat "
        "for the record: this was read off /contact/ rather than the "
        "homepage, so if a future pass finds a consumer product here, "
        "revisit."
    ),
    "missionwired-com": (
        "MissionWired (the trade name of Anne Lewis Strategies, LLC) is a "
        "fundraising and marketing firm for nonprofits and political "
        "organisations -- digital, email, SMS and direct mail, plus a "
        "donor-acquisition product it calls The Digital Co-Op. Verified "
        "2026-09-23: missionwired.com's only input is a newsletter email "
        "signup. There is no person-lookup surface; the donor data it "
        "holds is reached by its client organisations."
    ),
    "anteriad-com": (
        "Anteriad is a B2B marketing and demand-generation platform -- "
        "the 'Anteriad Marketing Cloud', BDR-as-a-service, audience "
        "identification sold to marketers. Verified 2026-09-23: its only "
        "search affordance is a generic nav placeholder and there is no "
        "person lookup anywhere. Note this is the same site the 180bytwo- "
        "com row now redirects into, 180byTwo having been absorbed into "
        "Anteriad; the two rows are one company and should be decided "
        "together."
    ),
    "hubspot-com": (
        "The dataset files this row under the NAME 'Apihub, Inc.' but "
        "gives the url hubspot.com, and whatever Apihub is, hubspot.com "
        "is HubSpot's own CRM and marketing-software site. Same shape as "
        "the termly-io and smartsheet-com rows: the search leg has to be "
        "decided against the site the dataset actually points at. "
        "Verified 2026-09-23: that site is product pages, case studies, "
        "integration showcases and 'Get a demo' / 'Get started free' "
        "buttons, with no input anywhere that retrieves person records by "
        "name. The name/domain mismatch should be reconciled in the "
        "dataset."
    ),
    "apollointeractive-com": (
        "Apollo Interactive is a performance-marketing lead vendor -- it "
        "sells 'Data Leads', 'Clicks' and 'Calls' in the health, auto, "
        "insurance, mortgage and home-services verticals. Verified "
        "2026-09-23: the only inputs on www.apollointeractive.com are a "
        "contact form (Name, Last Name, Email, Message and a captcha) and "
        "a newsletter signup. There is no consumer lookup; its leads are "
        "delivered to buyers, not searched by the public."
    ),
    "appsci-io": (
        "Verified 2026-09-23: appsci.io 301-redirects to appscience.ai -- "
        "the two dataset rows are one company. App Science sells cross- "
        "platform media analytics and measurement (Insights, Attribution, "
        "Political + Advocacy, Audience Intelligence) built on a "
        "proprietary household graph. Its homepage's only controls are "
        "LOGIN, REQUEST DEMO and nav links; there is no person-lookup "
        "input. See appscience-ai for the identical finding on the other "
        "row."
    ),
    "appscience-ai": (
        "Same company and same finding as appsci-io, which 301-redirects "
        "here. Verified 2026-09-23: www.appscience.ai sells advanced "
        "analytics and measurement for media planning to advertisers and "
        "political/advocacy buyers, and the homepage offers only LOGIN, "
        "REQUEST DEMO, nav and a LinkedIn link. No person-lookup input "
        "exists."
    ),
    "arity-com": (
        "Arity (an Allstate company) is a mobility data and analytics "
        "vendor -- driving-behaviour telematics, crash detection, a "
        "marketing platform targeting on driving behaviour, traffic "
        "analytics -- sold to auto insurers, marketers, retailers and the "
        "public sector, on what it calls the world's largest driving "
        "dataset tied to insurance claims (40M+ drivers, 3T+ miles). "
        "Verified 2026-09-23: arity.com carries no search box, text input "
        "or lookup form of any kind. What it holds is keyed to driving "
        "and devices, not to a name box."
    ),
    "aslmarketing-com": (
        "Verified 2026-09-23, and the domain has moved: "
        "www.aslmarketing.com 301-redirects to deepsync.com/asl-marketing "
        "-- ASL Marketing having been absorbed into Deep Sync, the same "
        "parent as the accudata-com row. Worth recording that the "
        "redirect TARGET itself now answers 404, so the old brand has no "
        "live page at all. Deep Sync's own site sells identity resolution "
        "to brands, agencies and platforms with no public person lookup "
        "(verified on the accudata-com row), so there is no search "
        "surface on either end of this redirect."
    ),
    "atlanticfox-com": (
        "Atlantic Fox Technologies builds enterprise identity-resolution "
        "and data infrastructure -- its own product names are Quanta "
        "Pattern (identity resolution) and Qbits Pattern (a self-service "
        "query interface for its CUSTOMERS), sold with the pitch "
        "'infrastructure you actually control'. Verified 2026-09-23: "
        "www.atlanticfox.com carries no public-facing lookup; the only "
        "form on it is a business contact form, and the query interface "
        "it sells is something a customer runs on their own data, not a "
        "public box."
    ),
    "attomdata-com": (
        "ATTOM sells property data and intelligence -- 160M+ US "
        "properties, with characteristics, foreclosure records, mortgage "
        "data, ownership and neighbourhood detail -- delivered by API, "
        "bulk licensing and cloud to real-estate, mortgage, insurance and "
        "financial buyers. Verified 2026-09-23: www.attomdata.com carries "
        "no search bar or name input of any kind. Note that PROPERTY data "
        "keyed to an address is still personal data about its owner, "
        "which is why the opt-out leg matters here even though the search "
        "leg is empty."
    ),
    "audienceacuity-com": (
        "Audience Acuity sells deterministic identity infrastructure -- "
        "identity resolution, data enrichment, customer profiling, "
        "audience activation and measurement, through products it names "
        "Realink (an API), Identity Authority (a Snowflake application), "
        "Onsight (on-premise) and syndicated segments. Verified "
        "2026-09-23: www.audienceacuity.com carries no person-lookup "
        "input; its whole pitch is that customers resolve identity "
        "without moving their data, which is the opposite of a public "
        "box."
    ),
    "astoriacompany-com": (
        "Astoria Company runs lead generation, pay-per-call, marketplace "
        "and SaaS businesses -- it matches and routes consumer inquiries "
        "to lenders, insurers, attorneys and contractors, buys and sells "
        "inquiry and call data between commercial partners, and sells "
        "PingPost.Exchange for managing that flow. Verified 2026-09-23: "
        "astoriacompany.com's homepage carries no search or lookup field "
        "of any kind; its calls to action are 'Buy Leads' and 'Sell "
        "Leads', which is the whole business."
    ),
    "atdata-com": (
        "AtData sells email-address intelligence: validation "
        "(SafeToSend), email/postal identity matching and append, "
        "consumer enrichment, fraud scoring, and data licensing including "
        "what it calls its Email Identity File. Verified 2026-09-23: "
        "www.atdata.com carries no public person-lookup input -- "
        "everything it offers runs against a customer's own list through "
        "an API. Its key is an email address rather than a name, which "
        "also shapes its opt-out form."
    ),
    "arkeero-com": (
        "Arkeero (Rock Internet, S.L., a Spanish company) sells an "
        "omnichannel advertising platform -- it builds audiences from "
        "what it calls 100% declarative data and activates them in "
        "campaigns for advertisers and publishers. Verified 2026-09-23: "
        "arkeero.com carries no person-lookup input; the only interactive "
        "element on the page is a first-party cookie notice with an "
        "Accept button."
    ),
    "audiencepoint-com": (
        "AudiencePoint sells email-engagement intelligence that sits on "
        "top of a marketer's existing systems -- its three capabilities "
        "are Data Health (real people vs bots, deliverability), Audience "
        "Clarity (segments from engagement signals) and Activation "
        "Intelligence (who receives mail vs spam). Verified 2026-09-23: "
        "audiencepoint.com carries no person-lookup input; everything it "
        "does is audience-level analysis over a CUSTOMER'S own list, not "
        "a record anyone can query."
    ),
    "audiencerate-com": (
        "Audiencerate Ltd sells a 'Marketing Data Platform' in four "
        "modules -- unified customer profiles and segmentation, Google "
        "DV360 activation, omnichannel email/SMS/WhatsApp campaigns, and "
        "AI market analysis. Verified 2026-09-23: www.audiencerate.com "
        "carries no person-lookup input. It aggregates a customer's own "
        "CRM, CSV, web and app data rather than exposing anything to "
        "search, and the site is plainly live (ISO 27001:2022, 2026 "
        "copyright, client testimonials)."
    ),
    "automotivemastermind-com": (
        "automotiveMastermind sells analytics and customer-engagement "
        "software to car dealers and OEMs -- loyalty and retention, "
        "service-drive sales, customer acquisition, dealer-group "
        "management, and a Recall Connect product. Verified 2026-09-23: "
        "nothing on the site offers a person lookup; its audience is "
        "dealerships, and the consumer-facing page it does publish is a "
        "request page, not a search. Caveat for the record: this was read "
        "off the do-not-sell request page and its navigation rather than "
        "the homepage."
    ),
    "autoweb-com": (
        "AutoWeb sells performance-based marketing to the automotive "
        "industry -- it connects car shoppers with dealers rather than "
        "selling cars. Verified 2026-09-23: every input on "
        "www.autoweb.com is about VEHICLES, not people -- Make and Model "
        "dropdowns, a Year dropdown, body-type and brand filters, and a "
        "Zip Code field for local pricing. There is no name box and no "
        "person lookup of any kind."
    ),
    "awl-com": (
        "AWL Holdings runs an insurance lead-generation marketplace "
        "connecting consumers with licensed agents. Verified 2026-09-23: "
        "there is no person-lookup tool on the site -- consumers arrive "
        "through quote funnels and are sold onward as leads, never "
        "searched for. Note this is the same company as the allwebleads- "
        "com row, whose domain 301-redirects here; the two rows are one "
        "business and their opt-out legs point at different URLs, which "
        "is worth reconciling in the dataset."
    ),
    "az-direct-com": (
        "AZ Direct GmbH (a Bertelsmann company) sells cross-channel "
        "marketing in the German-speaking market -- direct mail, email, "
        "digital advertising and analytics over a database it advertises "
        "as ~70 million consumers and 40 million households in Germany. "
        "Verified 2026-09-23: www.az-direct.com carries no person-lookup "
        "input. See EU-NOTES.md: this is a German GDPR-governed broker "
        "whose data is about German residents, so a US subject's presence "
        "here is unlikely to be the question anyway."
    ),
    "hybridtheory-com": (
        "Hybrid Theory (Azerion US Inc.) is an advertising network -- its "
        "own opt-out page describes its business as delivering 'ads "
        "tailored to your interests'. Verified 2026-09-23: no person- "
        "lookup surface exists; what it holds is keyed to cookies and "
        "devices, which is exactly why the only control it offers is a "
        "cookie toggle. Caveat: this was read off /opt-out/ rather than "
        "the homepage."
    ),
    "azira-com": (
        "Azira sells location and mobility audiences, and its own privacy "
        "policy settles this leg better than any homepage could: it "
        "states Azira 'does not maintain direct identifiers of consumers. "
        "It only maintains indirect identifiers, in the form of unique "
        "codes assigned to mobile devices (MAIDs) plus location data.' "
        "Verified 2026-09-23. A company that holds no names cannot offer "
        "a name lookup, and there is nothing on www.azira.com that takes "
        "one."
    ),
    "trustarc-com": (
        "The dataset files this row under the NAME 'AZIRA LLC' but gives "
        "the url trustarc.com, and TrustArc is not Azira -- it is the "
        "privacy-compliance vendor whose submit-irm.trustarc.com webform "
        "hosts Azira's opt-out (and, as it happens, the CourtRecords.us "
        "network's too). Same shape as the termly-io, smartsheet-com, "
        "hubspot-com and networkadvertising-org rows. Azira has its own "
        "row at azira-com, where the substantive finding lives -- that it "
        "holds only MAIDs and location, never names. There is no people- "
        "search surface on either company's site, and this row should be "
        "folded into azira-com rather than mapped as a broker in its own "
        "right."
    ),
    "biscience-com": (
        "B.I Science (2009) Ltd sells identifiers and internet-activity "
        "data to third-party ad networks and analytics partners, and "
        "insights containing unique identifiers to marketing and data- "
        "analytics companies -- its own CCPA notice calls both a 'sale' "
        "and a 'share'. Verified 2026-09-23: there is no person-lookup "
        "surface; its data is keyed to device and app identifiers "
        "collected through its and its affiliates' apps, not to names."
    ),
    "cybba-com": (
        "Verified 2026-09-23: cybba.com is a business-facing advertising "
        "agency ('Turning Ads Into Outcomes'), selling campaign services "
        "to brands. There is no consumer-facing lookup of any kind -- no "
        "name, phone, address or email search -- so there is nothing for "
        "a presence check to query. Cybba holds personal data as an "
        "adtech intermediary, which is why the broker row is legitimate, "
        "but the only consumer-visible surface it operates is a rights "
        "request form; see optout_forms.OPTOUT_BLOCKED for that leg."
    ),
    "cardlytics-com": (
        "Verified 2026-09-23: cardlytics.com is a B2B card-linked-offers "
        "platform whose customers are banks and advertisers, and the site "
        "carries no consumer lookup at all -- the homepage's only "
        "privacy-adjacent links are its Privacy Policy and Candidate "
        "Privacy Notice. Cardlytics' data reaches consumers only through "
        "their own bank's offers feed, which its policy confirms ('you "
        "can do so through the Publishing Partner directly'). Nothing to "
        "search, so nothing to detect."
    ),
    "demandbase-com": (
        "Verified 2026-09-23: Demandbase is an account-based B2B "
        "marketing platform, and says so on its own rights form "
        "('Demandbase is a business to business (B2B) company'). All "
        "lookup functionality sits behind a paid customer login and is "
        "keyed to companies rather than to individuals; there is no "
        "public people-search to probe. See "
        "optout_forms.OPTOUT_OUT_OF_SCOPE for the opt-out leg."
    ),
    "centeda-com": (
        "Verified by browser render 2026-09-23: centeda.com no longer "
        "operates. Every path on it now serves a notice titled 'This "
        "Domain Has Been Transferred by Court Order' -- the domain was "
        "transferred to Atlas Data Privacy Corporation under a final "
        "judgment of the New Jersey Superior Court in Atlas Data Privacy "
        "Corporation, et al. v. Radaris.com, et al. (Docket No. "
        "MID-L-000847-24) and is 'no longer under the control of its "
        "former operators'. The page carries no input of any kind. There "
        "is no people-search here to probe, and there will not be one "
        "again."
    ),
    "liveramp-com": (
        "Verified 2026-09-23 against liveramp.com/privacy: LiveRamp is a "
        "B2B identity-resolution and data-collaboration company whose "
        "customers are brands and platforms, and the only form anywhere "
        "on its public site is a site-content search box (GET "
        "liveramp.com/search, input name=query). There is no consumer "
        "lookup -- no name, phone, address or email search -- so a "
        "presence check has nothing to query. LiveRamp's consumer-facing "
        "surfaces are all opt-out surfaces rather than search ones; see "
        "optout_forms.OPTOUT_UNDECIDED, which records the three of them."
    ),
    "bdex-com": (
        "Verified by browser render 2026-09-23: BDEX runs a business-to- "
        "business data exchange, and its public site offers only a site- "
        "content search box (Elementor's, GET to bdex.com with name=s) "
        "plus a 'Try for Free' funnel into a customer account. No "
        "consumer-facing people lookup exists to probe. Its consumer "
        "surface is the opt-out form alone; see "
        "optout_forms.OPTOUT_BLOCKED."
    ),
    "bdo-com": (
        "Verified by browser render 2026-09-23: bdo.com's only forms are "
        "two site-content search boxes (#search-form2 and #search-form4, "
        "both unnamed inputs posting to the current page) and the "
        "California opt-out form. BDO USA is an accounting, tax and "
        "advisory firm; it holds personal data as an employer and service "
        "provider, not as a searchable people directory, and there is no "
        "consumer lookup of any kind on the site. Flagging the row itself "
        "as a probable dataset scope artifact: a professional-services "
        "firm is a different animal from the people-search and audience- "
        "data businesses this pilot is built around."
    ),
    "bestpickreports-com": (
        "Verified by browser render 2026-09-23: bestpickreports.com "
        "publishes vetted home-services CONTRACTOR ratings -- the thing "
        "you search for there is a plumber, not a person. The only other "
        "form on its do-not-sell page is a newsletter subscribe box. "
        "There is no consumer people-search to probe, which is worth "
        "stating explicitly because the row's opt-out form asks for name, "
        "address, phone and email and could easily be mistaken for a "
        "people-search broker's."
    ),
    "biscred-com": (
        "Verified by browser render 2026-09-23: Biscred sells commercial- "
        "real-estate prospecting data to sales and marketing teams "
        "('where sales and marketing professionals discover new prospects "
        "in commercial real estate'), and its lookup sits entirely behind "
        "Login / Set Up A Demo. Nothing on the public site accepts a "
        "person's name. No consumer-visible search surface to detect."
    ),
    "blis-com": (
        "Verified by browser render 2026-09-23: Blis is a B2B location- "
        "based advertising platform -- its public site is marketing copy "
        "plus a 'Book a demo' funnel, and the only form on the "
        "California-rights page is a Zoho newsletter signup. There is no "
        "consumer lookup of any kind. See optout_forms.OPTOUT_UNDECIDED "
        "for the opt-out leg."
    ),
    "bombora-com": (
        "Verified by browser render 2026-09-23: Bombora sells B2B intent "
        "data to sales and marketing teams; its public site carries only "
        "a site-content search box (GET bombora.com with name=s). There "
        "is no consumer people-search to probe. Note the opt-out leg is a "
        "separate problem -- the dataset's opt-out URL 404s; see "
        "optout_forms.OPTOUT_UNDECIDED."
    ),
    "thebridgecorp-com": (
        "Verified by browser render 2026-09-23: BRIDGE "
        "(thebridgecorp.com) sells audience data and media activation to "
        "advertisers -- the public site is marketing copy behind a 'Get a "
        "Demo' funnel, and the only forms on it are the opt-out form and "
        "a blog subscription. No consumer lookup exists to query. Its "
        "opt-out form is transcribed in full; see "
        "optout_forms.OPTOUT_UNDECIDED."
    ),
    "liftbasedata-com": (
        "Verified by browser render 2026-09-23: LiftBase Data "
        "(LiftEngine) is a postal and email LIST broker -- it rents "
        "audience lists such as 'American WeddingBase' and 'American Pre- "
        "Movers' to marketers, and its own rights pages are organised per "
        "LIST rather than per person. There is no consumer-facing lookup "
        "on the site at all. The consumer surface is a right-to-know hub "
        "and a mail-in PDF; see optout_forms.NO_OPTOUT_SURFACE."
    ),
    "blackpearl-com": (
        "Verified by browser render 2026-09-23: Blackpearl Group "
        "(Wellington, NZ) sells website-visitor identification to B2B "
        "marketers; its public site is investor and product marketing "
        "with no consumer lookup, and the only form that actually "
        "rendered on its opt-out page was a newsletter subscribe box. "
        "Nothing to search. The opt-out leg is unresolved for a different "
        "reason -- the real form is served from forms.blackpearl.com and "
        "did not load; see optout_forms.OPTOUT_UNDECIDED."
    ),
    "audigent-com": (
        "Verified by browser render 2026-09-23: Audigent is a data- "
        "activation, curation and identity platform selling to publishers "
        "and advertisers (Hadron ID, SmartPMP, ContextualPMP). Its "
        "homepage renders no input of any kind, let alone a people "
        "lookup. There is no consumer search surface to probe; see "
        "optout_forms.OPTOUT_UNDECIDED for the opt-out leg, which is also "
        "unresolved."
    ),
    "bidr-io": (
        "Verified 2026-09-23: bidr.io is Beeswax's bidder infrastructure "
        "-- a programmatic DSP endpoint, not a consumer-facing site. "
        "There is no people-search surface, and the only consumer-facing "
        "host on the domain is the opt-out one, which currently serves a "
        "certificate that does not match its name; see "
        "optout_forms.OPTOUT_UNDECIDED."
    ),
    "bbdirect-com": (
        "Verified by browser render 2026-09-23: BB Direct is a postal and "
        "email list broker selling to marketers, and its public site "
        "carries no consumer lookup -- the only form on its compliance "
        "page is the opt-out itself. Nothing accepts a person's name for "
        "searching. See optout_forms.OPTOUT_BLOCKED for that leg."
    ),
    "brooksim-com": (
        "Verified by browser render 2026-09-23: Brooks Integrated "
        "Marketing (BrooksIM) states on its own privacy page that it is "
        "'a registered data broker in California and other states' and "
        "that it does 'not engage directly with individual consumers, nor "
        "do we compile data independently'. Its public site is agency "
        "marketing plus a SaaS data platform pitch, with no consumer "
        "lookup of any kind. Nothing to search; the consumer surface is "
        "the request form alone, and that is Turnstile-walled (see "
        "optout_forms.OPTOUT_BLOCKED)."
    ),
    "business-com": (
        "Verified by browser render 2026-09-23: business.com is a B2B "
        "content and lead-generation publisher, not a people directory, "
        "and nothing on it accepts a person's name for lookup. Its rights "
        "surface is not even its own -- the opt-out redirects to a shared "
        "Centerfield-operated portal; see optout_forms.OPTOUT_BLOCKED, "
        "which also notes that other Centerfield properties in the "
        "dataset will land on that same form."
    ),
    "buxtonco-com": (
        "Verified by browser render 2026-09-23: Buxton sells customer- "
        "analytics and site-selection work to retailers and "
        "municipalities; its public presence now redirects into "
        "audiense.com, and neither site exposes a consumer lookup. There "
        "is no people-search to probe. The opt-out form is transcribed in "
        "full; see optout_forms.OPTOUT_UNDECIDED."
    ),
    "buyerlink-com": (
        "Verified by browser render 2026-09-23: Buyerlink (now at "
        "buyerlink.CO) runs a real-time auction marketplace matching "
        "consumer demand to service providers -- its site is marketing "
        "copy plus 'Request a Demo' and 'Log In', with no lookup that "
        "accepts a person's name. Nothing to search. See "
        "optout_forms.OPTOUT_UNDECIDED, where the do-not-sell page turns "
        "out to be effectively empty."
    ),
    "big-village-com": (
        "Verified by browser render 2026-09-23: Big Village sells "
        "audience products to advertisers and its public site offers no "
        "consumer lookup at all. Consistent with that, the only opt-out "
        "it offers is keyed to a mobile advertising identifier rather "
        "than to a person -- there is no name-keyed surface here in "
        "either direction. See optout_forms.OPTOUT_UNDECIDED."
    ),
    "cadent-tv": (
        "Verified by browser render 2026-09-23: Cadent sells TV and video "
        "advertising technology to media buyers; its site is platform "
        "marketing behind Login and Contact, with no consumer lookup. "
        "There is nothing to query. Note for the dataset: cadent.tv "
        "redirects to cadent.com. The opt-out leg is a multi-step "
        "verification wizard; see optout_forms.OPTOUT_OUT_OF_SCOPE."
    ),
    "verve-com": (
        "Verified by browser render 2026-09-23: Verve is a mobile "
        "advertising and audience platform selling to publishers and "
        "advertisers, and its public site exposes no consumer lookup -- "
        "the only form on the relevant page is the data-subject request "
        "form itself. Nothing to search. See optout_forms.OPTOUT_BLOCKED."
    ),
    "buildertrend-com": (
        "Verified by browser render 2026-09-23: Buildertrend is "
        "construction project-management SaaS sold to builders and "
        "remodelers. Its only public forms are its privacy request form "
        "and marketing captures; there is no people lookup of any kind. "
        "Flagging the row itself as a probable dataset scope artifact -- "
        "a construction SaaS vendor holds customer data as a service "
        "provider, which is a different thing from the people-search and "
        "audience-data businesses this pilot targets."
    ),
    "700credit-com": (
        "Verified by browser render 2026-09-23: 700Credit sells credit "
        "reports, soft pulls, identity verification and lead generation "
        "to AUTO DEALERS -- every entry point on the site is Dealer "
        "Login, Agents or Sign Up, and there is no consumer-facing "
        "lookup. Consumers interact with it only through the CCPA request "
        "form. Worth noting for anyone revisiting: as a credit-report "
        "reseller this row sits close to the FCRA-regulated territory "
        "already flagged under chexsystems-com, so its data is not simply "
        "opt-out-able. See optout_forms.OPTOUT_BLOCKED."
    ),
    "datasubject-com": (
        "Verified by browser render 2026-09-23: my.datasubject.com serves "
        "privacy-request portals on behalf of OTHER companies -- the "
        "tokenised URL this dataset records renders branded as 'Blue "
        "Action', not as DataSubject -- so datasubject.com is a rights- "
        "request VENDOR and has no people-search surface of its own. "
        "Flagging the row as a probable scrape artifact: what was "
        "captured is one customer's form on a vendor platform, and the "
        "broker that actually holds the data is whoever that tenant is. "
        "See optout_forms.OPTOUT_UNDECIDED."
    ),
    "biointelli-com": (
        "Verified by browser render 2026-09-23: Biointelli ('Scientific "
        "Signal Intelligence') mines grants, publications, patents and "
        "conference attendance to tell life-science sales teams which "
        "researchers are about to buy. Everything is behind Login or "
        "Request Demo -- the only public form is a demo-request popup -- "
        "so there is no consumer-facing lookup to probe, even though the "
        "company plainly profiles named individuals. See "
        "optout_forms.OPTOUT_UNDECIDED; its recorded privacy URL 404s."
    ),
    "experian-com": (
        "Verified by browser render 2026-09-23: Experian is a nationwide "
        "consumer reporting agency, not a people-search site. The dataset "
        "URL www.experian.com/privacy/opting_out renders a prose rights "
        "page whose ONLY form is the site-wide business search box "
        "(input[name=q] -> /search/business). There is no place for a "
        "member of the public to look themselves or anyone else up; a "
        "consumer's own file is reachable only through an authenticated, "
        "identity-verified account. Nothing to read on this leg."
    ),
    "transunion-com": (
        "Verified by browser render 2026-09-23: Same shape as experian- "
        "com and for the same reason. www.transunion.com/consumer-privacy "
        "renders an FAQ accordion about consumer rights; the only forms "
        "on it are two copies of the site header search "
        "(input[name=searchQuery] -> /consumer-search-results.html), both "
        "off-layout behind the magnifier toggle. TransUnion is a credit "
        "bureau; there is no public lookup of a named person, and a "
        "consumer's own file sits behind an identity-verified login."
    ),
    "careerbuilder-com": (
        "Verified by browser render 2026-09-23: CareerBuilder is a job "
        "board, not a people-search. Candidate profiles are visible only "
        "to paying employers behind a recruiter login, and no public page "
        "accepts a name and returns a person. The privacy page itself "
        "carries no search control of any kind. Its opt-out leg, by "
        "contrast, is a complete and readable form -- see "
        "optout_forms.STAGED_RECIPES."
    ),
    "hightouch-com": (
        "Verified by browser render 2026-09-23: Hightouch sells a B2B "
        "Composable CDP / reverse-ETL product; it moves a CUSTOMER's own "
        "warehouse data to that customer's SaaS tools and publishes no "
        "consumer-facing directory. preferences.hightouch.com renders a "
        "DataGrail Privacy Request Center with a country picker and "
        "nothing resembling a person lookup. No search surface exists to "
        "read."
    ),
    "catalist-us": (
        "Verified by browser render 2026-09-23: Catalist is a closed B2B "
        "political-data cooperative: its voter file is licensed to "
        "progressive campaigns and nonprofits under contract, and every "
        "product page sits behind a sales conversation. catalist.us "
        "offers only a WordPress site search (input[name=s]). There is no "
        "public place to look a voter up."
    ),
    "catalyzeai-com": (
        "Verified by browser render 2026-09-23: CatalyzeAI sells "
        "predictive seller-lead scores to real-estate agents through a "
        "subscription product; its consumer-facing footprint is a "
        "marketing site. No public person lookup exists. Recorded "
        "alongside a DATASET DEFECT on the opt-out leg: the row's "
        "opt_out_url returns 404."
    ),
    "cdkglobal-com": (
        "Verified by browser render 2026-09-23: CDK Global sells "
        "dealership management software to auto retailers. Its data about "
        "a consumer arrives through that consumer's own dealership and is "
        "exposed only inside the dealer's authenticated DMS. "
        "www.cdkglobal.com is a corporate marketing site with no lookup "
        "of any kind."
    ),
    "censia-com": (
        "Verified by browser render 2026-09-23: Censia sells Talent "
        "Intelligence -- candidate profiles delivered inside an "
        "employer's ATS under a B2B contract. The public site is "
        "marketing plus a privacy policy; there is no public candidate "
        "search, and the only controls on the policy page are the "
        "Complianz cookie-consent toggles."
    ),
    "choreograph-com": (
        "Verified by browser render 2026-09-23: Choreograph is WPP's data "
        "and technology arm; its audience segments are sold to agencies "
        "and brands, never queried by the public. The consumer-facing "
        "property amer-cpp.choreograph.com is a privacy portal, not a "
        "directory: its landing page asks only for a country of "
        "residence. No search surface."
    ),
    "optoutprescreen-com": (
        "Verified by browser render 2026-09-23: OptOutPrescreen.com is "
        "the joint FCRA prescreen opt-out service run by the nationwide "
        "consumer reporting agencies, not a broker that publishes data. "
        "It only ACCEPTS opt-outs; it has no lookup of any person, and by "
        "design could not have one. Flagging as a probable non-broker row "
        "in the source dataset: it is the industry's opt-out mechanism "
        "rather than a data seller."
    ),
    "cision-com": (
        "Verified by browser render 2026-09-23: Cision sells a media- "
        "contact database to PR teams by subscription. Journalist records "
        "are queried inside the paid CisionOne platform behind a login; "
        "www.cision.com exposes no public lookup. The public-facing "
        "consumer artifact is a cookie/Cision-ID opt-out page, which "
        "carries no search of any kind."
    ),
    "civisanalytics-com": (
        "Verified by browser render 2026-09-23: Civis Analytics sells "
        "data science software and consulting to campaigns, nonprofits "
        "and enterprises. No public person lookup exists on the marketing "
        "site. Recorded alongside a DATASET DEFECT on the opt-out leg: "
        "the row's opt_out_url returns 404."
    ),
    "civitech-io": (
        "Verified by browser render 2026-09-23: Civitech is a public- "
        "benefit corporation selling campaign tooling (TextOut, "
        "Districter, RunningStart) to Democratic campaigns and "
        "organizations. Its voter data is reached only inside those "
        "licensed products; civitech.io publishes no lookup. The privacy "
        "policy page carries a single control, the mobile menu toggle."
    ),
    "catalina-com": (
        "Verified by browser render 2026-09-23: Catalina Marketing sells "
        "shopper-purchase-based targeting to CPG brands and retailers; "
        "its data comes from retailer loyalty programs and is never "
        "publicly queryable. www.catalina.com is a Svelte marketing site "
        "whose only privacy affordance is a OneTrust cookie widget. No "
        "search surface."
    ),
    "checkr-com": (
        "Verified by browser render 2026-09-23: Checkr is a consumer "
        "reporting agency selling background screening to employers. "
        "Reports are ordered by an employer against a named candidate "
        "WITH that candidate's FCRA authorization and delivered inside "
        "the employer's account; nothing on the public site accepts a "
        "name and returns a person. A consumer's own file is reachable "
        "only through an identity-verified applicant portal, which this "
        "tool must not automate. checkr.com is a marketing site (its only "
        "consumer-facing links are 'My background check' and 'Contact "
        "support', both authenticated). Note the site now advertises the "
        "acquisition of Truv, and the privacy policy covers 'Checkr and "
        "Zethos, Inc. d/b/a Truv' -- worth knowing if Truv appears as its "
        "own row."
    ),
    "goodhire-com": (
        "Verified by browser render 2026-09-23: GoodHire is a consumer "
        "reporting agency selling background screening to employers. "
        "Reports are ordered by an employer against a named candidate "
        "WITH that candidate's FCRA authorization and delivered inside "
        "the employer's account; nothing on the public site accepts a "
        "name and returns a person. A consumer's own file is reachable "
        "only through an identity-verified applicant portal, which this "
        "tool must not automate. www.goodhire.com offers PERSONAL CHECKS, "
        "but that is a product a visitor buys to screen themselves or a "
        "nanny/tenant, gated behind purchase and consent -- not a free "
        "directory lookup. No public search surface."
    ),
    "sterlingcheck-com": (
        "Verified by browser render 2026-09-23: Sterling is a consumer "
        "reporting agency selling background screening to employers. "
        "Reports are ordered by an employer against a named candidate "
        "WITH that candidate's FCRA authorization and delivered inside "
        "the employer's account; nothing on the public site accepts a "
        "name and returns a person. A consumer's own file is reachable "
        "only through an identity-verified applicant portal, which this "
        "tool must not automate. The domain now announces 'Sterling "
        "Background Check Solutions is now First Advantage', so this row "
        "is a brand that has been merged into another company -- worth "
        "flagging as a probable duplicate of a First Advantage row. The "
        "only form on the page is the site-wide content search "
        "(input[name=swpquery] -> /search/)."
    ),
    "hireright-com": (
        "Verified by browser render 2026-09-23: HireRight is a consumer "
        "reporting agency selling background screening to employers. "
        "Reports are ordered by an employer against a named candidate "
        "WITH that candidate's FCRA authorization and delivered inside "
        "the employer's account; nothing on the public site accepts a "
        "name and returns a person. A consumer's own file is reachable "
        "only through an identity-verified applicant portal, which this "
        "tool must not automate. The homepage carries only a content "
        "search; the consumer entrances are 'My Background Check' and a "
        "support portal, both authenticated."
    ),
    "cisive-com": (
        "Verified by browser render 2026-09-23: Cisive is a consumer "
        "reporting agency selling background screening to employers. "
        "Reports are ordered by an employer against a named candidate "
        "WITH that candidate's FCRA authorization and delivered inside "
        "the employer's account; nothing on the public site accepts a "
        "name and returns a person. A consumer's own file is reachable "
        "only through an identity-verified applicant portal, which this "
        "tool must not automate. The only form on www.cisive.com is a "
        "HubSpot marketing signup (hsForm_e3534c86-..., a single 'Your "
        "work email' field posting to forms.hsforms.com) -- a newsletter, "
        "not a lookup."
    ),
    "bisi-com": (
        "Verified by browser render 2026-09-23: BISI (Background "
        "Information Services, Inc.) is a consumer reporting agency "
        "selling background screening to employers. Reports are ordered "
        "by an employer against a named candidate WITH that candidate's "
        "FCRA authorization and delivered inside the employer's account; "
        "nothing on the public site accepts a name and returns a person. "
        "A consumer's own file is reachable only through an identity- "
        "verified applicant portal, which this tool must not automate. "
        "bisi.com is a small corporate site whose only consumer-facing "
        "control is the cookie banner; every other entrance is 'CARE "
        "Login' or 'Client Access'. Note it fronts several sibling brands "
        "(BIS, ChemScreen and others listed under 'Our Companies') which "
        "may appear as separate rows."
    ),
    "birchwoodcreditservices-com": (
        "Verified by browser render 2026-09-23: Birchwood Credit Services "
        "resells mortgage credit reports and verification products to "
        "LENDERS; the consumer never interacts with it directly and it "
        "publishes no directory. The only forms on the site are two "
        "copies of the HubSpot content search (input[name=term] -> /hs- "
        "search-result)."
    ),
    "clearview-ai": (
        "Verified by browser render 2026-09-23: Clearview AI is "
        "searchable ONLY by image and only by its vetted law-enforcement "
        "customers -- its own privacy page states plainly that 'Clearview "
        "AI does not maintain any sort of information other than publicly "
        "available photos' and that 'we cannot search by name or any "
        "method other than image'. There is no public lookup of any kind, "
        "by name or otherwise, so there is nothing for a name-driven "
        "search recipe to read."
    ),
    "datanyze-com": (
        "Verified by browser render 2026-09-23: Datanyze sells B2B "
        "contact and technographic data to sales teams by subscription; "
        "records are queried inside the paid product and in a Chrome "
        "extension, never from a public page. The domain is additionally "
        "walled (see the opt-out leg), so nothing renders to read either "
        "way."
    ),
    "saymine-io": (
        "Verified 2026-09-23: this row's domain is not a broker at all. "
        "saymine.io is Mine, a privacy-request SERVICE; the dataset URL "
        "cognism.privacy.saymine.io/cognism is Mine's hosted privacy "
        "centre for a DIFFERENT company, Cognism. Flagging as a probable "
        "dataset defect: the row should almost certainly be keyed on "
        "cognism.com, with saymine.io as the opt-out host. Either way "
        "Mine publishes no people-search surface, and the Cognism "
        "database is a paid B2B product queried behind a login."
    ),
    "ice-com": (
        "Verified by browser render 2026-09-23: Intercontinental Exchange "
        "runs exchanges (NYSE) and, through ICE Mortgage Technology, "
        "lender software. Neither publishes a consumer lookup: "
        "www.ice.com is a corporate/investor site whose only forms are "
        "the site search (input[name=q] -> /site-search) and an email "
        "subscription centre. The dataset's contact for this row, "
        "compliancemortgagetech@ice.com, points at the mortgage- "
        "technology arm, which is where any consumer data sits -- behind "
        "a lender's account."
    ),
    "clay-com": (
        "Verified by browser render 2026-09-23: Clay sells a GTM data- "
        "enrichment product that queries third-party providers on a "
        "CUSTOMER's instruction; its own do-not-sell page says so "
        "explicitly ('Clay acts as a data processor working on the "
        "instruction of its customers'). Nothing on clay.com accepts a "
        "name from the public and returns a person -- enrichment happens "
        "inside a paid workspace."
    ),
    "collectivedata-io": (
        "Verified by browser render 2026-09-23: The Collective deals in "
        "MOBILE ADVERTISING IDs, not named people: its own opt-out page "
        "asks for a MAID and warns 'DO NOT enter your telephone number'. "
        "There is no name-keyed record to look up and no public search "
        "surface of any kind."
    ),
    "intentwave-com": (
        "Verified by browser render 2026-09-23: IntentWave sells B2B "
        "intent data and identity resolution to marketers; the public "
        "site is products and a contact form. Its consumer page (/opt- "
        "out) offers only two privacy choices and no lookup, and even "
        "those hand off to its sibling brand persistent.id. No public "
        "person search."
    ),
    "bigidprivacy-cloud": (
        "Verified 2026-09-23: this row is keyed on a PRIVACY-SERVICE "
        "domain, not a broker. bigidprivacy.cloud is BigID's hosted "
        "privacy-centre product, and the dataset URL "
        "crexi.bigidprivacy.cloud is the tenant belonging to CREXI, a "
        "commercial-real-estate marketplace, which is the actual company. "
        "Flagged as a probable dataset miskeying (compare saymine-io in "
        "the same sweep). Neither BigID nor the privacy centre publishes "
        "a people search, and Crexi's own product searches PROPERTIES "
        "rather than people."
    ),
    "complementics-com": (
        "Verified by browser render 2026-09-23: Complementics deals in "
        "mobile device identifiers and app audiences, not named people -- "
        "its own opt-out asks for a Device ID (MAID). There is no name- "
        "keyed record and no public lookup of any kind."
    ),
    "completemailinglists-com": (
        "Verified by browser render 2026-09-23: Complete Mailing Lists "
        "sells direct-mail list rentals to marketers by segment, not per- "
        "person lookups; the public site is a list catalogue and a quote "
        "request. No consumer-facing search. Recorded alongside a DATASET "
        "DEFECT on the opt-out leg: the row's opt_out_url (/node/3697) "
        "404s, and the site it lands on is a half-built template still "
        "carrying 'Menu Item One/Two/Three' placeholders in its "
        "navigation."
    ),
    "completemedicallists-com": (
        "Verified by browser render 2026-09-23: Same business as its "
        "sibling completemailinglists-com: rented direct-mail lists of "
        "healthcare professionals, sold by segment. The only form "
        "resembling a search is a mailing-LIST search (input[name=search] "
        "-> /mailing_lists_search) that looks up products, not people. No "
        "person lookup."
    ),
    "comscore-com": (
        "Verified by browser render 2026-09-23: Comscore does cross- "
        "platform audience MEASUREMENT -- panels and census tags "
        "producing aggregate ratings -- and publishes no per-person "
        "records. www.comscore.com's only form is the site content search "
        "(input[name=keyword]). No search surface."
    ),
    "service-now-com": (
        "Verified 2026-09-23, and this row is keyed on the wrong thing: "
        "service-now.com is ServiceNow, a workflow-software vendor, while "
        "the dataset URL firstam.service-now.com/... is a ServiceNow- "
        "hosted form belonging to FIRST AMERICAN and serving CONNECTED "
        "INVESTORS (the page is titled 'Consumer Opt-Out Request Form' "
        "and names 'Connected Investors, Inc.'). Flagged as a probable "
        "miskeying -- the broker is Connected Investors, the host is "
        "incidental. Neither ServiceNow nor the form offers a people "
        "search."
    ),
    "connextdigital-com": (
        "Verified by browser render 2026-09-23: Connext Digital is a "
        "BPO/outsourced-staffing provider rather than a records "
        "publisher, and its site is down regardless -- "
        "connextdigital.com/opt-out/ 404s and the domain serves 'This "
        "site is currently unavailable'. No search surface exists to "
        "read, and none is plausible for the business."
    ),
    "consider-com": (
        "Verified by browser render 2026-09-23: Consider sells a talent- "
        "intelligence platform to VCs, hiring companies and staffing "
        "agencies; candidate data is reached inside a paid, logged-in "
        "workspace. consider.com is pure marketing with no form on it at "
        "all. No public lookup."
    ),
    "universalcis-com": (
        "Verified by browser render 2026-09-23: universalcis.com now "
        "redirects wholesale to xactus.com -- Universal Credit Services "
        "was folded into the Xactus brand, so this row is a retired brand "
        "of a mortgage-verification provider. Its data reaches consumers "
        "only through a lender, and the site publishes no lookup. "
        "Probable duplicate of any Xactus row. Dataset contact "
        "ccasey@universalcredit.com belongs to the retired brand."
    ),
    "contactout-com": (
        "Verified by browser render 2026-09-23: ContactOut sells "
        "recruiter-facing contact data ('Find Anyone's Email & Phone') "
        "but only through an authenticated Search Portal and a Chrome "
        "extension sold by seat -- there is no public page that takes a "
        "name and returns a person. The only form on the public opt-out "
        "page is the email-verification step. No free search surface to "
        "read."
    ),
    "cicreports-com": (
        "Verified by browser render 2026-09-23: CIC (cicreports.com) is a "
        "tenant- and employment-screening CRA, now announcing its "
        "acquisition by Asurint via AMCP. Reports are ordered by "
        "landlords and employers with FCRA authorization; the consumer "
        "entrance is 'MY REPORT' / 'CONSUMER ASSISTANCE', both "
        "authenticated. No public lookup. Note the likely duplicate: an "
        "Asurint row would be the same data."
    ),
    "contentgine-com": (
        "Verified by browser render 2026-09-23: contentgine.com now "
        "serves only a rebrand splash -- 'CONTENTgine is now pharosIQ' "
        "with a CONTINUE button and marketing@pharosiq.com. The business "
        "is B2B content-syndication lead generation, which publishes no "
        "consumer lookup, and the domain no longer hosts a product site "
        "at all. Probable rename to be re-keyed on pharosiq.com; dataset "
        "contact paul@contentgine.com is a personal address on the "
        "retired domain."
    ),
    "convergemarketing-com": (
        "Verified by browser render 2026-09-23: Converge Direct is a "
        "media-buying agency; its consumer-facing surface is a hosted "
        "privacy portal (my.datasubject.com) rather than any directory. "
        "No public person lookup exists."
    ),
    "convex-com": (
        "Verified by browser render 2026-09-23: Convex sells a "
        "commercial-services sales-intelligence platform (now part of "
        "ServiceTitan) whose records are about BUSINESSES and properties, "
        "reached inside a paid login. www.convex.com publishes no person "
        "lookup; its only forms are the privacy-request form and a "
        "newsletter signup."
    ),
}


# Brokers whose SEARCH exists and is fine -- and sits behind an anti-bot wall
# on every visit. The search-leg twin of ``optout_forms.OPTOUT_BLOCKED``, and
# a different finding from both of the maps above: there is a surface, it is
# not an affiliate's, and the only thing between us and it is a challenge this
# tool will not solve. Kept apart so that "walled today" is never read as
# "hopeless forever" -- and so nobody writes a recipe here and then wonders why
# it reports a bot wall for eternity.
#
# Notes, not behaviour: nothing reads this at runtime.
SEARCH_BLOCKED = {
    "voterrecords-com": (
        "Verified 2026-09-23: the site's own HOMEPAGE is a Cloudflare "
        "Turnstile interstitial ('Performing security verification', a "
        "cf-turnstile-response input and nothing else). There is no search "
        "form to read, let alone submit, so both legs of this broker are "
        "walled rather than absent."
    ),
    "uspeoplesearch-com": (
        "Verified 2026-09-23: same shape as voterrecords-com -- the "
        "homepage itself serves a Cloudflare Turnstile challenge, so the "
        "search form never renders for an automated visitor."
    ),
    "freepeoplesearch-com": (
        "Verified 2026-09-23, and one step harder than the two above: this "
        "domain does not serve a solvable challenge at all but Cloudflare's "
        "terminal block page ('Sorry, you have been blocked. You are unable "
        "to access FreePeopleSearch.com'). Nothing renders, nothing is "
        "offered to solve, and no recipe can change that."
    ),
    "stirista-com": (
        "Verified 2026-09-23: every page of this domain serves an anti- "
        "bot interstitial. www.stirista.com and www.stirista.com/opt-out- "
        "preferences/ both return nothing but a loader and the string "
        "'Please wait while your request is being verified...' -- the "
        "real page never renders for an automated visitor, so there is no "
        "form to read on either leg. Same shape as voterrecords-com. What "
        "the company is can be read off its sibling domain "
        "andrewswharton.com, which brands itself 'A Stirista Solution' "
        "and sells B2B audience targeting and data enhancement, so a "
        "search surface is unlikely -- but unlikely is not verified, and "
        "this wall is why."
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
    "instantcheckmate-com": (
        "Verified as far as this tool may go, 2026-09-23. The search form "
        "itself is ordinary and readable -- <form id='form-search' "
        "action='/search/' method='get'> with firstName, lastName, an "
        "optional city and an optional state <select> -- and filling first "
        "and last and pressing Search does fire. What comes back is not a "
        "results page but a blocking 'Notice' modal (div.modal-overlay."
        "open) whose only way forward is a button labelled I AGREE "
        "(button#yes), carrying the FCRA disclaimer and an agreement to "
        "the site's Terms of Use and Privacy Policy.\n"
        "\n"
        "That button was deliberately NOT clicked, so this leg has no "
        "verdict. Pressing it is accepting a third party's terms on Penn's "
        "behalf, which is a different act from typing a name into a public "
        "index and is not something the read-only search leg has a licence "
        "for. And even if it were clicked, SearchRecipe still has no way "
        "to EXPRESS a consent step -- the same structural gap already "
        "recorded against privaterecords-net, peoplesearcher-com and "
        "courtrecords-us -- so a recipe could not be written from what is "
        "behind it either. Finishing this leg needs two things that do not "
        "exist today: a consent step in the recipe format, and a decision "
        "by a human about whether this tool may agree to broker terms at "
        "all. Recorded as undecided rather than blocked because nothing "
        "here is an anti-bot wall: the page is served normally and the "
        "form works."
    ),
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
    "courtrecords-us": (
        "Verified end to end on 2026-09-23, and it WORKS -- which is why "
        "this sits here rather than under NO_SEARCH_SURFACE. Fill "
        "first/last, choose the required State, press Submit, click the "
        "FCRA 'I Agree' in the notice that appears (that link IS the "
        "submit: its onclick calls the form's own submitForm()), wait "
        "about 25 seconds, and the site prints either '100 RESULTS FOUND "
        "IN ILLINOIS' or, on /search/results/?noHit=1, 'Although we didn't "
        "find an exact match in our preliminary database'. The blocker is "
        "what those pages SAY: every name on them is masked to its first "
        "letter ('M****** T*******', 'Search Completed for Z********* "
        "Q*********'), so not one identity term ever appears. "
        "classify_search_page would read the miss correctly and then "
        "refuse the hit -- a printed count with no corroborating term is "
        "an error by design -- which means shipping this recipe would "
        "report 'unknown' for every person the site actually lists, and "
        "recipe_health would classify each of those as structural drift "
        "and raise an alert about a recipe that is working exactly as "
        "written. Deciding otherwise needs a model for masked results, "
        "not a recipe. Its OPT-OUT leg is a shipped, working recipe -- "
        "see optout_forms.COURTRECORDS_US."
    ),
    "peoplewhiz-com": (
        "Verified 2026-09-23. Its search does submit (after dismissing an "
        "FCRA warning dialog that otherwise intercepts the click) and, "
        "about sixty seconds later, lands on /hflow/<id>/<uuid> -- but "
        "that page contains no names at all. Searching a common real name "
        "and a nonsense name produced the SAME page: eight 'Get Report' "
        "rows, no person named anywhere in the rendered text or the HTML "
        "(checked by counting the searched terms in the page source: "
        "zero). There is no state on that page from which presence or "
        "absence could be read, by this tool or by a human."
    ),
    "courtrec-com": (
        "Verified 2026-09-23, and the failure is a strange one worth "
        "writing down rather than re-deriving. The homepage search box "
        "(#peopleSearch-input) cannot be driven at all: a page heading "
        "sits over the input so a real click is intercepted, typing after "
        "focusing leaves input_value() EMPTY, a page.fill() that does set "
        "the value is ignored by the app, and the 'SEARCH COURT RECORDS' "
        "button is a no-op in every combination -- including a forced "
        "click and pressing Enter. Whether that is deliberate hardening or "
        "simply broken, there is no answer to read. publicrecords-us and "
        "publicrecords-info serve the same template (same furniture, same "
        "dashboard.<domain>/opt-out wizard) and are recorded with it."
    ),
    "publicrecords-us": (
        "Same template and same finding as courtrec-com, recorded "
        "separately because each dataset broker gets its own verdict: the "
        "search box cannot be driven and the search button does nothing. "
        "Confirmed as one family by its own live pages -- identical "
        "layout, identical 404 page, and a REMOVE MY INFO link to the same "
        "dashboard.<domain>/opt-out wizard -- rather than assumed from a "
        "shared look."
    ),
    "publicrecords-info": (
        "Same template and same finding as courtrec-com and "
        "publicrecords-us, and recorded separately for the same reason. "
        "Its own live pages carry the same layout and the same "
        "dashboard.publicrecords.info/opt-out removal wizard."
    ),
    "phonenumbers-org": (
        "Verified 2026-09-23: the site's only search is a phone box whose "
        "SEARCH control does nothing. Its React onClick handler is bound "
        "and does run -- it reads the number straight out of the DOM, so a "
        "typed value reaches it -- and then awaits an internal call that "
        "produces no navigation and no network request beyond an analytics "
        "beacon. Reproduced with a typed (not just filled) value, on the "
        "state subdomains as well as the apex, and in a HEADED browser as "
        "well as headless, so this is the site's behaviour rather than an "
        "artefact of automation. Nothing to classify means no recipe. Its "
        "opt-out is handled on infotracer.com, a different company's "
        "site -- see optout_forms."
    ),
    "infotracer-com": (
        "Verified 2026-09-23, and it fails twice over. (1) Every search "
        "box a visitor can actually see on infotracer.com is an affiliate "
        "handoff: each visible form carries a "
        "data-affiliate-url-template pointing at "
        "htrk1.beenverified.com/aff_c?offer_id=...&aff_id=3135, and "
        "pressing SEARCH opens beenverified.com in a new tab with the name "
        "as query parameters while the original tab never moves. Name, "
        "phone, email and username all behave this way. (2) The site does "
        "keep non-affiliate forms of its own (the criminal/court/arrest/ "
        "public-records tabs post to /loading/), and that path completes: "
        "an FCRA notice appears, its 'I Agree' is a.disclaimer-yes calling "
        "approveForm(), a staged progress meter runs for about 42 seconds, "
        "and the answer lands on /name/nohit/ titled 'Results Not Found'. "
        "But BOTH 'John Smith' and 'Michael Johnson' answered nohit, and "
        "the interstitial says why: it 'will conduct only a preliminary "
        "people search ... a search of any records will only be conducted "
        "and made available after you register for an account'. A leg that "
        "answers 'no' for the two commonest names in the country is a "
        "false-negative factory, which is the one failure this module "
        "exists to prevent."
    ),
    "recordsfinder-com": (
        "Verified 2026-09-23, and the flow itself is solved: fill "
        "#firstname/#lastname, press SEARCH NOW (which is swallowed on its "
        "own -- the site's jQuery submit handler pops an FCRA notice and "
        "rewrites #searchDisclaimerYes's href), then click "
        "#searchDisclaimerYes and the browser lands on "
        "/search/name/loader/. The blocker is that the landing page is not "
        "an answer. It says 'Multiple Results Found for the Name You "
        "Searched' and asks you to pick an age or city to refine -- and it "
        "says exactly that for 'Zylphrenna Quixbottom' as well as for "
        "'John Smith'. A page that claims multiple results for a name that "
        "cannot exist carries no information about whether this broker "
        "lists anybody, and picking an age out of the refinement list to "
        "get past it is a judgement call, not a recipe."
    ),
    "staterecords-org": (
        "Verified 2026-09-23. Driving it takes three steps (fill "
        "first/last, choose the required state, press SEARCH -- which a "
        "React handler intercepts to open an FCRA modal -- then click that "
        "modal's I AGREE button, which is what actually calls "
        "router.push), followed by about 48 seconds on /loader before "
        "/result renders. The blocker is the InfoPay masking already "
        "recorded for courtrecords-us: the answer page reads 'Search "
        "Completed for J*** S****' and 'Although we didn't find an exact "
        "match in our preliminary database', for John Smith in Illinois. "
        "No identity term can ever appear on that page, so no hit could be "
        "confirmed and its miss wording cannot be trusted either. Its "
        "OPT-OUT leg is a shipped, working recipe."
    ),
    "arrestwarrant-org": (
        "Verified 2026-09-23: its search does not answer on its own site. "
        "The homepage form (#form1, target=_blank) submits to "
        "/reg6/loader.php, shows 'Searching Database Registry for John "
        "Smith in IL' for a few seconds, and then redirects that tab to "
        "verifyrecords.com -- another domain, where the visitor is handed "
        "a fresh, empty search form. Both a real name and a nonsense name "
        "end at the same place. This is the affiliate-front finding "
        "(a different fact from a capability gap), but it is recorded here "
        "rather than under NO_SEARCH_SURFACE because arrestwarrant.org "
        "does serve its own branded search and its own loader before "
        "handing the visitor off; what it never serves is a result."
    ),
    "searchbug-com": (
        "Verified 2026-09-23: the search runs and then stops at a "
        "registration wall. #quick takes a NAME and, as its own hint "
        "insists ('When searching by name, please also specify some "
        "Location'), a location; supply both and SEARCH lands on "
        "/services/pay.aspx?TYPE=ppl2&FNAME=..&LNAME=..&STATE=IL titled "
        "'People Search John Smith in IL | Order on Searchbug', whose "
        "whole content is 'Please Login or Create New Account to "
        "Continue' and 'Credit card is needed to create an account'. "
        "'Zylphrenna Quixbottom' produces the identical page, so there is "
        "no signal in it at all. Worth distinguishing from the entries in "
        "SEARCH_BLOCKED: nothing here is fighting automation -- the answer "
        "is simply sold rather than shown, and this tool will not open an "
        "account or enter a card to read it."
    ),
    "freebackgroundcheck-org": (
        "Verified 2026-09-23: this broker has no search form anywhere -- "
        "the homepage and every category and state subdomain render zero "
        "<form> elements. What it actually publishes is a LINK DIRECTORY: "
        "'Total Record(s) Found: 40' counts outbound links to other "
        "people-search sites ('Search For People in the Military', "
        "'Amateur Radio Callsign Lookups', 'Face Search'), not records. "
        "Its own privacy policy agrees: 'If you want to correct or remove "
        "your original public record ... you will need to contact the "
        "custodian of that information.' Recorded here rather than under "
        "NO_SEARCH_SURFACE only because the dataset lists it as a broker "
        "and a future reader deserves the evidence that it is not one; if "
        "that reading holds, this broker could reasonably be dropped."
    ),
    "acxiom-com": (
        "No verdict as of 2026-09-23, and the reason is an OUTAGE rather "
        "than anything about the broker -- which is exactly why it is "
        "written here instead of being rounded to a decided-looking "
        "bucket. www.acxiom.com answers HTTP 500 on every path tried "
        "(/, /privacy/, /about-us/privacy/, /optout/), rendering only "
        "'Something went wrong / Try again' with zero <form> elements, and "
        "the 500 is not User-Agent-keyed: curl/8.7.1, a Windows Chrome UA "
        "and this tool's own Linux Chrome UA all get it. The separate "
        "application host isapps.acxiom.com answers 200 but its body is "
        "'Request unsuccessful. Incapsula incident ID: "
        "430000170140468063-...', i.e. an Imperva block page, so neither "
        "host could be read.\n"
        "\n"
        "Note what is NOT being claimed. Acxiom is widely understood to be "
        "a B2B marketing-data company with no consumer lookup, and "
        "NO_SEARCH_SURFACE would probably turn out to be the right answer "
        "-- but 'probably' read off a 500 page is exactly the kind of "
        "confident-looking answer to an unasked question this module "
        "exists to refuse. Whether a search surface exists here has not "
        "been established at all. The next step is a single visit on "
        "another day."
    ),
    "beenverified-com": (
        "Verified live both ways on 2026-09-23, and the reason it cannot "
        "ship is that the ANSWER IS ASYMMETRIC. The homepage form is a plain "
        "GET (fn=, ln=) to https://www.beenverified.com/lp/a19763/2/loading "
        "on the broker's own host, so getting a query in is easy. What "
        "comes back is not one page but a funnel: 'Beginning Your Search!', "
        "then 'Thank you. Where Do They Live?' (city + state, with an 'I'm "
        "not sure.' button.js-skip), then 'Can you share the following "
        "details to help narrow down our results?' (age + middle name, same "
        "skip button). 'Zylphrenna Quixbottom' falls out of that funnel "
        "after the FIRST skip with the heading 'Sorry, we have 0 results'. "
        "'John Smith' does not: it walks the whole funnel and lands on "
        "'Please confirm before we continue ... Please check the box below "
        "to see the results of your search' (input#fcra-checkbox + an 'I "
        "Agree' button), and step 4 of the same funnel id, "
        "/lp/a19763/4/subscribe, is titled 'Final Step | BeenVerified.com' "
        "and carries subscription_plan_name radios, 'Your membership "
        "automatically renews' and a PayPal control. So a MISS is published "
        "for free and a HIT is sold. A recipe here would print a confident "
        "'not present' on exactly the queries the site has nothing for and "
        "an unreadable paywall on the queries it does -- and this tool will "
        "not check an FCRA use-restriction box or open a paid membership to "
        "find out which. Independently of that, the answer is two "
        "intermediate skip clicks deep and SearchRecipe carries one url, "
        "one flat field list and one submit selector, so there is nothing "
        "to point it at even if the paywall were not there."
    ),
    "intelius-com": (
        "Verified 2026-09-23. The form itself is trivially drivable -- "
        "<form name='people-search-form' action='/search/' method='GET'> "
        "with firstName/lastName/city/state on the broker's own host, so "
        "https://www.intelius.com/search/?firstName=..&lastName=.. IS the "
        "results URL. What is served there is not a result. It is a "
        "twelve-screen engagement wizard: 'Let's Narrow This Down' -> "
        "'QUESTION 1 OF 5' (middle initial, city, more than one city or "
        "state, age range, only child) -> three unskippable interstitials "
        "('Public Records May Reveal More Than Expected', 'Why This Might "
        "Matter', \"You're Not the Only One Checking\") -> a 'VIEW MY "
        "RESULTS' button. Every screen was walked with 'Zylphrenna "
        "Quixbottom', and the site answered a name that cannot exist "
        "exactly as it would answer a real one -- the wizard never says "
        "'no results', never prints a count, and the page title stays "
        "'Searching for Zylphrenna Quixbottom in ALL - Intelius' "
        "throughout. So there is no page state here that means absence, "
        "and a recipe could only ever report 'unknown'. Two further "
        "blockers even if there were: the answer is a dozen clicks deep "
        "and SearchRecipe carries one submit selector, and 'VIEW MY "
        "RESULTS' was not pressed because Intelius sells its reports and "
        "this tool will not buy one to read an answer. Its opt-out leg is "
        "separately out of scope; see optout_forms."
    ),
    "truthfinder-com": (
        "Verified live both ways on 2026-09-23, and it is Intelius's shape "
        "with a nastier twist. <form id='form-search' name='search' "
        "action='/search/' method='GET'> (firstName, lastName, state) "
        "submits to a wizard, not to an answer: 'What else do you know "
        "about Zylphrenna?' -> a staged progress meter that counts 22%, "
        "40% with its own countdown ('1: 10s') -> 'Has Zylphrenna "
        "Quixbottom lived their whole life in ALL?'. What that meter "
        "prints while it runs is the twist: for a name that cannot exist "
        "it ticks off 'Location Confirmed', 'Age Range Confirmed' and "
        "'Relative Confirmed'. Anything on that page reading as "
        "corroboration is theatre.\n"
        "\n"
        "Following it to the end lands on /results/?firstName=..&lastName="
        "..&previewSearchQuestion=true&state=ALL, and BOTH terminal pages "
        "were read. 'John Smith' gives 'Incredible! 24 Matches Found - "
        "Select Your Result Now' with '21 Matches / Verified Name "
        "Matches'. 'Zylphrenna Quixbottom' gives 'Your search for "
        "\"Zylphrenna Quixbottom\" may be too broad. Add more details like "
        "city and state to help you find the correct Zylphrenna Quixbottom "
        "in our background check database.' That is this broker's zero "
        "page and it is phrased as its own opposite -- it blames breadth, "
        "i.e. TOO MANY results, for having none. Adopting it as a "
        "no_results_marker would mean teaching this tool to read 'too many "
        "to show you' as 'we do not have this person', which is the "
        "false-negative this module exists to prevent; the broadest "
        "possible real query (John Smith, no state) returns a COUNT rather "
        "than that page, which is suggestive but is not the broker stating "
        "absence. Undecided on that alone -- and independently unshippable "
        "because the answer is only reachable through the wizard, while "
        "SearchRecipe submits one form and reads what comes back, and "
        "string-building the /results/ URL by hand is exactly what this "
        "module forbids. Its opt-out leg is separately out of scope; see "
        "optout_forms."
    ),
    "adform-com": (
        "NO VERDICT as of 2026-09-23, and the reason is our fetcher, not "
        "the site. Every attempt at adform.com, www.adform.com and "
        "site.adform.com returned an empty response, so the homepage was "
        "never actually read. Everything known about Adform from outside "
        "-- a Danish DSP/SSP selling to advertisers -- points at "
        "NO_SEARCH_SURFACE, but that call has to be made against a page "
        "somebody has seen, and nobody here has. A future pass with a "
        "real browser should load site.adform.com and check whether any "
        "input on it takes a person's name."
    ),
    "adrearubin-com": (
        "NO VERDICT as of 2026-09-23. adrearubin.com and "
        "www.adrearubin.com/privacy-policy/ both failed with TLS "
        "handshake errors on repeated attempts, so no page of this broker "
        "was ever rendered. Search snippets describe Adrea Rubin "
        "Marketing, Inc. (CA data-broker registration 186558, also "
        "registered as 'Adrea Rubin Media, Inc. dba Calibrant Digital') "
        "as a PR/marketing agency, which would ordinarily be a no-surface "
        "shape, but a TLS failure is not evidence about a search box. "
        "Next pass: retry with a browser that negotiates the site's TLS, "
        "or check whether the domain has moved."
    ),
    "mediaocean-com": (
        "NO VERDICT as of 2026-09-23, and the reason is transport, not "
        "the site: every attempt at www.mediaocean.com over both https "
        "and http failed with 'unable to verify the first certificate', "
        "repeatably, so the homepage was never read. A site: search "
        "returns only marketing and privacy-policy pages, consistent with "
        "4C Insights / Mediaocean being an ad-tech and martech platform "
        "rather than a consumer people-search brand -- but that is an "
        "inference from result titles, not from a page anyone loaded. "
        "Next pass needs a fetcher that tolerates this certificate chain."
    ),
    "6sense-com": (
        "NO VERDICT as of 2026-09-23. 6sense is plainly a B2B revenue- "
        "intelligence / ABM platform, and no public unauthenticated "
        "person-search form appears on 6sense.com -- which would normally "
        "settle it as no-surface. What holds the call is that its own "
        "navigation advertises a 'Company and People Search' feature "
        "under the Sales Intelligence product, and that tool is behind a "
        "login, so it was never observed. Until someone can say whether "
        "that gated search is a person lookup over 6sense's own contact "
        "data, calling this no-surface would be asserting an absence "
        "nobody checked."
    ),
    "bookyourdata-com": (
        "NO VERDICT as of 2026-09-23. bookyourdata.com sells B2B contact "
        "lists (verified business emails, decision-makers) and exposes "
        "two tools -- a 'Prospector Tool' that filters by job title, "
        "industry and company, and an 'Email Finder' that searches by "
        "company, domain or contact details -- but both sit behind a 'Get "
        "10 Free Leads' signup and were never exercised. Neither is "
        "framed as a name-based people-search, yet an Email Finder that "
        "takes contact details is close enough to a presence check that "
        "no-surface cannot be asserted from outside the login. Next pass: "
        "determine whether either tool answers 'is this named individual "
        "in your database'."
    ),
    "degree-me": (
        "NO VERDICT as of 2026-09-23: the domain serves nothing. "
        "degree.me has NS delegation to AWS Route53 "
        "(ns-1086.awsdns-07.org, ns-1685.awsdns-18.co.uk, "
        "ns-236.awsdns-29.com, ns-918.awsdns-50.net) but NO A or AAAA "
        "record resolves, confirmed by dig both ways, and curl returns "
        "http_code 000. This looks like a dormant registration tied to "
        "ACE Agents Inc. / academixdirect.com. It sits here rather than "
        "under NO_SEARCH_SURFACE because a domain that does not resolve "
        "today may resolve tomorrow; recheck resolution before deciding."
    ),
    "acronymix-com": (
        "NO VERDICT as of 2026-09-23: the whole site answers HTTP 500. "
        "acronymix.com returns a server error on both WebFetch and a "
        "direct curl, which confirms it is the origin failing and not a "
        "fetcher artifact, so no page was ever rendered and nothing can "
        "be said about a search surface either way. Same shape as the "
        "acxiom.com entry. Recheck when the site is back up."
    ),
    "acutraq-com": (
        "NO VERDICT as of 2026-09-23. ACUTRAQ is primarily B2B background "
        "screening sold to employers, landlords, nonprofits and "
        "healthcare organisations, and its homepage carries no open name- "
        "search box -- but it links to two places nobody walked through: "
        "an 'Online Application' on a separate quickleasepro.com login "
        "portal, and a 'Personal Background Check' self-request path. "
        "Until someone establishes whether either exposes a lookup rather "
        "than an authenticated intake, no-surface would be a guess. Note "
        "the opt-out leg IS decided (mail/fax/email only) and is recorded "
        "separately."
    ),
    "alabamacourtrecords-us": (
        "A SEARCH SURFACE PLAINLY EXISTS, and that is exactly why this is "
        "not no-surface -- but it is not a recipe yet either. Verified "
        "2026-09-23: alabamacourtrecords.us serves a public person lookup "
        "with fields labelled 'First Name:', 'Last Name:' and 'City:' "
        "under 'Instant Access to Civil and Criminal Court Records', and "
        "the site states it is 'a private entity independent of any state "
        "government agency' and disclaims FCRA use. What is missing is "
        "mechanical: the form's element names, its action/method and its "
        "submit control did not render to the fetcher, and the sibling "
        "site alaskacourtrecords.us warns that results are 'only a "
        "preliminary people search' requiring registration and purchase "
        "-- so whether a free response can even answer presence is "
        "unsettled. Next pass needs a JS-capable browser on this "
        "template."
    ),
    "alaskacourtrecords-us": (
        "Same template and same open question as alabamacourtrecords-us; "
        "both are the CourtRecords.us / StateRecords.org network. "
        "Verified 2026-09-23: alaskacourtrecords.us serves a person "
        "lookup with 'First Name', 'Last Name' and 'City' fields "
        "promising civil and criminal record access, self-describes as "
        "private and non-governmental, and disclaims FCRA use. It states "
        "outright that results are 'only a preliminary people search' "
        "requiring registration and purchase for a full report, and the "
        "form's element names and submit control did not render to the "
        "fetcher. Resolve the network's template once and both state "
        "sites resolve together."
    ),
    "mydataprivacy-com": (
        "NO VERDICT as of 2026-09-23, and this one is genuinely a hybrid. "
        "mydataprivacy.com is Alesco Data's CCPA compliance PRODUCT, sold "
        "to businesses and list owners, which would point at no-surface "
        "-- except that it also exposes a public lookup: a form taking "
        "either an 'Email Address' or a 'Name & Postal Address' with a "
        "'Search' button, whose own copy says 'If your name is found in "
        "the database, you will have the option to opt-out... and/or "
        "deleting your name from national databases'. That is a database- "
        "membership check rather than a dossier people-search, and it "
        "answers for PARTICIPATING databases rather than for Alesco "
        "itself, so what a HIT here would actually mean has to be settled "
        "before a recipe can claim anything from it."
    ),
    "192-com": (
        "A SEARCH SURFACE PLAINLY EXISTS -- this is the one broker in its "
        "batch that is unambiguously a people-search site -- but it is "
        "not a recipe yet. Verified 2026-09-23: www.192.com serves a "
        "'Search People' form with a Name input and a Location input, "
        "alongside promoted Electoral Roll 2026, background-check and "
        "property-ownership lookups. Two things block a recipe. First, "
        "mechanics: the form's element names, action and method were "
        "never captured. Second, scope: 192.com's records are UK-only, "
        "and the dataset's own notes record that 192.com replied to a "
        "request from a non-UK address asking why it had been contacted "
        "-- so a HIT or MISS here may mean nothing for a US profile. See "
        "EU-NOTES.md before writing anything against this one."
    ),
    "411-com": (
        "A SEARCH SURFACE PLAINLY EXISTS. Verified 2026-09-23: "
        "www.411.com serves a genuine white-pages homepage -- a Name + "
        "state/location 'Name Location Search' form under the tagline "
        "'Find contact information on yourself or anyone else', plus a "
        "separate reverse phone lookup. It is undecided only because the "
        "form's element names, action and method were not captured, and "
        "because 411.com is a Whitepages property (its opt-out is "
        "Whitepages' own suppression flow), so a recipe-writer should "
        "first check whether its results are served by whitepages.com "
        "under the hood -- if so, the two rows share one surface and "
        "should be resolved together rather than twice."
    ),
    "allpeople-com": (
        "A SEARCH SURFACE PLAINLY EXISTS. Verified 2026-09-23: "
        "allpeople.com calls itself the 'largest free directory of "
        "business contacts for America' and its homepage carries a real "
        "search form with four labelled fields -- Name, Email, Phone, "
        "Industry -- plus browse-by-state links for all 50 states. "
        "Undecided for the usual mechanical reason (no element names, "
        "action or method captured) and one substantive one: this is a "
        "BUSINESS-contact directory, so what a hit means for a private "
        "individual needs deciding before a recipe reports presence or "
        "absence."
    ),
    "acbj-com": (
        "NO VERDICT as of 2026-09-23, and the reason is a fetch wall. "
        "www.acbj.com, acbj.com and www.bizjournals.com all failed with a "
        "host-level 'unable to fetch' -- not a 404, so the sites are up "
        "and refusing this fetcher. What is known from outside: ACBJ is "
        "the parent of bizjournals.com (business news across 44 local "
        "markets), it is CA-registered as a data broker, and bizjournals' "
        "article search can be 'refined by people' -- which is an "
        "article-search FACET over news content, not a person-record "
        "lookup, but that distinction was never confirmed against the "
        "live page. Next pass needs a fetcher these hosts will serve."
    ),
    "ancestry-com": (
        "NO VERDICT as of 2026-09-23, and the interesting question here "
        "is what the surface MEANS, not whether it exists. "
        "www.ancestry.com/search/ does expose an anonymous form -- First "
        "& Middle Name(s), Last Name, 'Place your ancestor might have "
        "lived', Birth Year, a Search button and 'Show more options' -- "
        "but it is framed entirely around historical record collections "
        "(birth/marriage/death, census, military, immigration, wills), "
        "i.e. genealogy about the dead, not a lookup of living people. "
        "Two things must be settled before any recipe: whether a non- "
        "logged-in visitor sees result RECORDS at all or only a "
        "paywall/signup gate, and whether a hit in a census index is even "
        "the kind of presence this tool is meant to report. The homepage "
        "itself shows no anonymous search box and pushes a 14-day trial."
    ),
    "networkadvertising-org": (
        "NO VERDICT as of 2026-09-23, and the first problem is that the "
        "domain is not the broker. The dataset names this row 'Anne Lewis "
        "Strategies, LLC' (i.e. MissionWired, which has its own row at "
        "missionwired-com) but points at networkadvertising.org, which is "
        "the Network Advertising Initiative -- the ad-industry "
        "association whose optout.networkadvertising.org tool is the "
        "shared member opt-out page MissionWired merely links to. So this "
        "row is a link, not a company, the same mistake as the termly-io "
        "row. It is undecided rather than no-surface only because "
        "optout.networkadvertising.org/?c=1 and the bare host both failed "
        "to return anything to this fetcher, so nothing was observed "
        "first-hand. Next pass: render the NAI tool, confirm it is the "
        "industry cookie opt-out it is universally described as, and then "
        "fold this row into missionwired-com rather than mapping it as a "
        "broker."
    ),
    "apollo-io": (
        "NO VERDICT as of 2026-09-23. Apollo.io is a B2B sales- "
        "intelligence platform and its homepage exposes no "
        "unauthenticated lookup -- every route is 'Sign up for free', "
        "'Log in' or 'Request a demo'. What holds the call is the product "
        "behind that wall: Apollo advertises access to '240M contacts & "
        "30M companies', and a contact database of that shape may well "
        "answer 'is this named person listed', which is exactly the "
        "question this tool asks. Signup is free, so a future pass can "
        "settle it without paying. Same shape as the bookyourdata-com and "
        "6sense-com entries."
    ),
    "archives-com": (
        "NO VERDICT as of 2026-09-23, and it is the same question as the "
        "ancestry-com row -- Archives.com is an Ancestry property (its "
        "privacy and terms links point at Ancestry.com, and the dataset's "
        "contact is usprivacyrequests@ancestry.com). A real anonymous "
        "search form exists on www.archives.com: First name, Last name "
        "(required), a Country select (United States, United Kingdom, "
        "England, Wales), a US state Location dropdown, and Birth year "
        "and Death year with +/- ranges. But it searches 11.8 billion "
        "HISTORICAL records -- photos, newspapers, vital records, the "
        "1950 census -- explicitly framed as finding ancestors, and full "
        "access is behind a paid subscription after a 7-day trial. Two "
        "things to settle before a recipe: whether an anonymous visitor "
        "sees result records or only a paywall, and whether a hit in a "
        "census index is the kind of presence this tool should report at "
        "all. Decide ancestry-com first; this row should inherit that "
        "answer."
    ),
    "aristotle-com": (
        "NO VERDICT as of 2026-09-23: blocked. "
        "www.aristotle.com/privacy/do-not-sell-my-personal-info/ returned "
        "HTTP 403 to this fetcher and no page of the site was rendered on "
        "either leg. Aristotle International is known as a political-data "
        "and voter-file vendor, which would ordinarily point at no- "
        "surface, but a voter-file company is exactly the kind that "
        "sometimes exposes a registration lookup, so the guess is not "
        "safe. Next pass needs a fetcher Aristotle will serve."
    ),
    "arizonacourtrecords-us": (
        "Third site of the CourtRecords.us network in this dataset, same "
        "template and same open question as alabamacourtrecords-us and "
        "alaskacourtrecords-us. Verified 2026-09-23: "
        "arizonacourtrecords.us serves a person-search form with fields "
        "labelled First Name, Last Name and City, states that "
        "'CourtRecords.us is not a consumer reporting agency' and "
        "operates 'as a private entity independent of any state "
        "government agency', and -- the decisive sentence -- says 'a "
        "search of any records will only be conducted and made available "
        "after you register for an account or purchase a report', with "
        "only 'a preliminary people search' performed before that. So the "
        "surface is real, and whether a free response can answer presence "
        "at all is exactly what has not been established. Resolve the "
        "network template once; four rows here fall together."
    ),
    "arkansascourtrecords-us": (
        "Fourth CourtRecords.us row; see arizonacourtrecords-us for the "
        "transcribed template, which this site shares. It is recorded as "
        "undecided on the same grounds -- a real First Name / Last Name / "
        "City lookup whose results are gated behind registration or "
        "purchase, with no element names captured. Honest caveat for the "
        "next pass: this specific domain's HOMEPAGE was not individually "
        "rendered this session (its /optout page was), so the template is "
        "inferred from three verified siblings rather than observed here. "
        "Whoever cracks the network should spot-check it."
    ),
    "arrakis-ai": (
        "NO VERDICT as of 2026-09-23, and the reason is transport. Both "
        "www.arrakis.ai and www.arrakis.ai/optout fail with a TLS "
        "internal error (TLSV1_ALERT_INTERNAL_ERROR) -- the handshake is "
        "refused before any HTTP happens, so no page of this broker was "
        "rendered on either leg and nothing can be said about a search "
        "surface. Same class as the adrearubin-com entry. Next pass: a "
        "client that negotiates this host's TLS."
    ),
    "issgovernance-com": (
        "NO VERDICT as of 2026-09-23. The domain has moved -- "
        "www.issgovernance.com/privacy-legal/ccpa/ 301-redirects to "
        "www.iss-stoxx.com/privacy-legal/ccpa/ following the ISS STOXX "
        "rebrand, which matches the dataset's own contact, "
        "dataprotectionofficer@iss-stoxx.com -- and the redirect target "
        "returned HTTP 403 to this fetcher, so nothing was rendered. ISS "
        "is a governance-research and proxy-advisory firm and the "
        "dataset's row name, Asset International, is its former "
        "publishing arm; that shape would ordinarily point at no-surface, "
        "but nothing was seen. Next pass: a fetcher iss-stoxx.com will "
        "serve."
    ),
    "assurance-com": (
        "NO VERDICT as of 2026-09-23: assurance.com serves an EXPIRED TLS "
        "CERTIFICATE on both the homepage and the dataset's privacy- "
        "practices URL, so no page was rendered on either leg. Assurance "
        "IQ is a Prudential-owned insurance-quoting marketplace, which "
        "would ordinarily point at no-surface, but nothing was observed. "
        "Note this is the second row in this batch behind an expired "
        "certificate (see take5mg-com, which turned out to be a parked "
        "domain) -- so check first whether assurance.com is still a live "
        "business or a lapsed one."
    ),
    "propertychecker-com": (
        "The form is fully transcribed; what is undecided is the half a "
        "search recipe needs beyond it. Verified by browser render "
        "2026-09-23: propertychecker.com's homepage carries four separate "
        "Yii2 search forms, of which the name-search one is the relevant "
        "surface -- POST to propertychecker.com/loader?ltid=home&mercSubI "
        "d=name&searchTab=name, fields "
        "NameSearchForm[firstname]/#namesearchform-firstname, "
        "NameSearchForm[lastname]/#namesearchform-lastname, "
        "NameSearchForm[city]/#namesearchform-city, "
        "NameSearchForm[state]/#namesearchform-state (select), a hidden "
        "NameSearchForm[type], and a hidden _csrf-frontend token minted "
        "per page load (so this can only be driven in a browser, never as "
        "a canned POST). No captcha script and no widget on the page. The "
        "other three tabs are address search (/loader-address2), parcel "
        "search (/loader-parcel) and a zip-code area search (/area- "
        "search/search-loader). Undecided because nothing is yet known "
        "about what the results page looks like or whether it is "
        "paywalled at the point a presence check would need to read it -- "
        "that, not the form, is the remaining work."
    ),
    "courtcasefinder-com": (
        "Same shape as propertychecker-com: form captured, results side "
        "unknown. Verified by browser render 2026-09-23: "
        "courtcasefinder.com's homepage carries three search forms, all "
        "with id 'searchForm' -- a name search POSTing to /search/loader "
        "with firstName/#firstName, lastName/#lastName, city/#city and a "
        "52-option state/#state select behind button#search-button; an "
        "address search GETting /search/address/loader with "
        "houseNumber/#addressHouseNumber, streetName/#addressStreetName, "
        "aptUnit/#addressAptUnit, city/#cityAddress and state; and a "
        "phone search GETting /search/loader with phone/#phoneNumber. No "
        "captcha script and no widget. Two cautions for the next pass: "
        "the three forms share a DOM id, so any selector must be scoped "
        "by the field it contains rather than by '#searchForm', and the "
        "site is a subscription lookup (it carries LOGIN and SIGN UP), so "
        "whether an unauthenticated result page reveals enough for a "
        "presence check is exactly the open question. The opt-out leg is "
        "transcribed and staged; see optout_forms.STAGED_RECIPES."
    ),
    "neighborwho-com": (
        "NO VERDICT as of 2026-09-23, but the surface plainly exists. "
        "Rendering the dataset's (dead, 404) removal URL still returned "
        "the site chrome, which carries an ADDRESS search box -- form "
        "class js-navbar-address-search, a single input name=address with "
        "placeholder 'Enter an address', and a bare submit button -- "
        "alongside Log In and Sign Up controls. So NeighborWho is an "
        "address-keyed property/people lookup that is at least partly "
        "account-gated. Undecided rather than transcribed because the box "
        "was captured from a 404 page rather than from the real search "
        "page, its form action was therefore not meaningful, and nothing "
        "is known about what an unauthenticated result page shows. Next "
        "pass: render the homepage properly and run a search."
    ),
    "nuwber-com": (
        "NO VERDICT as of 2026-09-23 for a network reason, not a research "
        "one: nuwber.com failed to RESOLVE (net::ERR_NAME_NOT_RESOLVED) "
        "from a headless Chromium on this host, so no page was ever "
        "reached. Nuwber is a well-known people-search site, so a single "
        "DNS failure is not grounds for a no-surface call in either leg. "
        "Next pass: resolve the name from a different network first. See "
        "the opt-out leg's entry, which is unresolved for the same "
        "reason."
    ),
    "peoplesmart-com": (
        "The forms are transcribed; what is undecided is whether their "
        "URLs are stable. Verified by browser render 2026-09-23: "
        "www.peoplesmart.com serves THREE search surfaces. A people "
        "search (form.js-people-form, GET) with name/#input-name, "
        "company/#input-company, state/#input-state (select), job/#input- "
        "job-title, industry/#input-industry and seniority/#input- "
        "seniority behind a SEARCH button; a phone search (form.js-phone- "
        "form) with phone/#input-phone; and an email search (form.js- "
        "email-form) with email/#input-email. The problem is the actions: "
        "they point at /lp/88dce3/2/loading, /lp/2310c2/2/building-report "
        "and /lp/5b2812/2/building-report -- opaque landing-page variant "
        "ids that look like campaign routing rather than stable "
        "endpoints, and a recipe pinned to them would rot silently. Note "
        "the positioning too: PeopleSmart now markets itself as a B2B "
        "contact-search tool ('Search over 100M targeted leads', "
        "Recruitment / Sales / B2B Lead Gen), so what it returns for a "
        "private individual is unknown. Next pass: run a search and see "
        "what an unauthenticated result page shows. The opt-out leg is "
        "Cloudflare-walled; see optout_forms.OPTOUT_BLOCKED."
    ),
    "blisspointmedia-com": (
        "NO VERDICT as of 2026-09-23: www.blisspointmedia.com did not "
        "RESOLVE (net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on "
        "this host, so nothing about either leg can be stated. Recheck "
        "from a different network before calling it dead, and check "
        "whether the company now trades under another name. Same "
        "situation as nuwber-com; see the opt-out leg's entry."
    ),
    "corelogic-com": (
        "NO VERDICT as of 2026-09-23. corelogic.com now redirects to "
        "COTALITY.com and the recorded path 404s, so the site that was "
        "going to be examined no longer exists under that brand. What "
        "rendered on the 404 page was only a content search bar (GET "
        "cotality.com/search, input name=query) and a HubSpot newsletter "
        "form, neither of which is a people or property lookup. CoreLogic "
        "is a large property-data holder, so whether Cotality exposes any "
        "consumer-facing lookup is worth establishing properly rather "
        "than assuming. Next pass: examine cotality.com directly."
    ),
    "checkpeople-com": (
        "Verified live both ways 2026-09-23, and the verdict is that this "
        "site cannot be probed honestly. The form itself is clean and "
        "fully transcribed: form#heroTabPeople (class checkhero__tab), "
        "POST to https://checkpeople.com/landing, carrying #heroFirstName "
        "(name=firstName, required), #heroLastName (name=lastName, "
        "required), #heroCity (name=city, optional), #heroState "
        "(name=state, a select the site pre-fills from the visitor's "
        "geolocation -- it came back as nv unbidden), plus per-load "
        "hidden _token (Laravel) and aid inputs, so browser-driven only. "
        "Its submit is a bare <button> with NO type attribute, so "
        "button[type='submit'] does not match it and the selector has to "
        "be #heroTabPeople button. A duplicate of the same form "
        "(navTabPeople/ navFirstName...) lives in the header, plus a "
        "phone variant (navTabPhone -> /landing/phone/rp1e/searching). No "
        "captcha script and no captcha widget on the rendered homepage. "
        "WHY NO RECIPE: submitting does not produce a results page at "
        "all. Both John Smith and the nonsense name Zylphrenna Quixbottom "
        "land on the SAME url shape "
        "/landing/people/<code>/searching?...&firstName=...&lastName= "
        "..., both titled 'Searching for <name> - CheckPeople.com', and "
        "both render an identical theatrical progress funnel -- a ticking "
        "percentage, 'We are Checking Federal/State/County Data Sources', "
        "rows for Criminal Records / Relatives / Arrest Records / "
        "Mugshots each reading 'Loading...', and interstitial questions "
        "('Do you think <first> has ever had a DUI?'). Nothing "
        "distinguishes a hit from a miss, because the funnel is a paywall "
        "lead-in rather than a report. For a recipe to become possible "
        "somebody would have to establish what the funnel terminates in "
        "for a real versus an absent person, and whether that terminus is "
        "reachable without paying -- which is a purchase decision, not a "
        "research one."
    ),
    "bridgevine-com": (
        "Reachability failure, 2026-09-23: https://bridgevine.com/ does "
        "not resolve (net::ERR_NAME_NOT_RESOLVED from a real browser). "
        "Recorded as UNDECIDED rather than no-surface because a DNS "
        "failure from one network is not proof the company is gone -- it "
        "should be rechecked from a different network before anyone "
        "concludes the domain is dead. The dataset carries an "
        "opt_out_email for it (dwayne.landry@bridgevine.com), which is a "
        "personal address rather than a privacy alias and is itself worth "
        "doubting."
    ),
    "brightswipe-com": (
        "Reachability failure, 2026-09-23: https://brightswipe.com/ does "
        "not resolve (net::ERR_NAME_NOT_RESOLVED). Same caveat as "
        "bridgevine-com -- recheck from another network before calling "
        "the domain dead. Dataset contact: admin@brightswipe.com."
    ),
    "carmarketsolutions-com": (
        "Reachability failure, 2026-09-23: "
        "https://carmarketsolutions.com/ resolves but never completes a "
        "connection -- the browser timed out after 30s without reaching "
        "DOMContentLoaded. Distinct from the two ERR_NAME_NOT_RESOLVED "
        "rows in this batch: something answers DNS here, so this is more "
        "likely a hung or firewalled host than a retired domain. Recheck "
        "later and from another network. The dataset holds no email for "
        "this row, so it currently has no working channel at all."
    ),
    "calltruth-com": (
        "Reachability failure, 2026-09-23: "
        "https://www.calltruth.com/opt_out.php does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED from a real browser). Same treatment "
        "as the bridgevine/brightswipe rows -- a DNS failure on one "
        "network is not proof the domain is retired, so this is undecided "
        "pending a recheck from elsewhere. The dataset holds no email for "
        "this row, so it currently has no working channel at all."
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
