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
