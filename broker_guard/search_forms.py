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

# --- RECIPE HEALTH, 2026-09-23 -----------------------------------------
#
# thatsthem-com is currently UNREACHABLE: https://thatsthem.com/ answers 403
# from CloudFront ("Request blocked") across the whole site -- homepage,
# search URL and opt-out page alike, on repeated attempts. Its opt-out recipe
# in optout_forms is affected identically; see the longer note above RECIPES
# there, including why it has NOT been demoted on this evidence (a datacenter
# IP is a likely cause, and Penn's deployment runs from a residential address
# that may not be blocked). Re-check from the deployment host before deciding.
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
    # --- batch 23 of 2026-09-24: C sweep ---------------------------------
    #
    # Eleven of the sixteen close here, and the reason is the same one
    # that has dominated this bucket all sweep: these are B2B data
    # products. The data is real and often intimate, but it is sold to
    # businesses through a sales team or a logged-in workbench, and the
    # subject is given no lookup of their own. "No search surface" is a
    # statement about the CONSUMER-FACING web, not about whether the
    # broker can find you -- it certainly can.
    #
    # Two rows in this group, co-ke and com-co, are here for a different
    # and more troubling reason, recorded in full in optout_forms: their
    # broker ids are PUBLIC-SUFFIX MIS-PARSES, not domains. Whatever
    # pipeline produced the dataset treated "co.ke" and "com.co" as bare
    # registrable domains when they are in fact public suffixes, so these
    # rows point at no host at all. They are closed here so the sweep is
    # complete, but the honest verdict is that there is nothing to search
    # because there is no broker -- see the collision warning in the
    # opt-out entries before reusing these keys for anything.
    "clearcompany-com": (
        "Verified 2026-09-24. ClearCompany is an applicant-tracking and "
        "HR-management platform sold to employers. Its public site is "
        "marketing plus a customer login; the records it holds are "
        "candidate and employee data submitted through ITS CUSTOMERS' "
        "career sites, and it holds them as a processor on those "
        "employers' behalf. No consumer-facing lookup exists and none "
        "would be expected -- a candidate's data lives in a particular "
        "employer's tenant, not in a global index this site could search."
    ),
    "clickagy-com": (
        "Verified 2026-09-24. Clickagy is an audience/intent-data vendor "
        "(now part of Claritas). Its data is behavioural and "
        "device-scoped, keyed to advertising identifiers rather than to a "
        "name, and it is delivered to advertisers through DSP "
        "integrations, not through any page a person can query. There is "
        "nothing name-searchable to expose.\n"
        "\n"
        "This is the second row this sweep where the absence of a search "
        "surface is a direct consequence of DEVICE-SCOPED identity rather "
        "than a choice about disclosure (adara-com was the first), which "
        "is the open device-scoped-opt-out question and not something to "
        "settle here."
    ),
    "clientcommand-com": (
        "Verified 2026-09-24. Client Command sells in-market automotive "
        "shopper data to car dealers. Every form on its site -- five of "
        "them, all probed -- is a sales-contact form asking for Company "
        "and Work Email; the recognised B2B-sales-form-as-opt-out tell. "
        "No consumer lookup of any kind is offered, which is consistent "
        "with a product whose buyers are dealerships and whose subjects "
        "are not its audience."
    ),
    "co-ke": (
        "No search surface as of 2026-09-24, because there is no host to "
        "search. 'co.ke' is Kenya's second-level public suffix, not a "
        "registrable domain; the dataset row is a parsing artifact. "
        "Recorded so the sweep is complete. See the optout_forms entry for "
        "the full defect write-up and the key-collision warning."
    ),
    "cognism-com": (
        "Verified 2026-09-24. Cognism sells B2B contact data (work emails, "
        "direct dials, job titles) to sales teams through a logged-in "
        "platform and API. The public site is marketing plus a login; "
        "there is no unauthenticated way to look yourself up, and the "
        "privacy portal is the only subject-facing route.\n"
        "\n"
        "Recorded as no-surface rather than blocked because nothing walled "
        "us off a search page -- there is no search page. Note the probe "
        "limitation that affected the OPT-OUT leg here: Cognism's privacy "
        "portal is a MineOS single-page app and the probe read the DOM "
        "before it rendered (body length zero). That is a tooling gap, not "
        "evidence about this leg."
    ),
    "com-co": (
        "No search surface as of 2026-09-24, for the same reason as "
        "co-ke: 'com.co' is a Colombian second-level public suffix, not a "
        "registrable domain. No host, nothing to search, row closed for "
        "completeness. See optout_forms for the defect write-up."
    ),
    "connectedinvestors-com": (
        "Verified 2026-09-24. Connected Investors is a real-estate "
        "investor network whose property and owner data sits behind "
        "account registration; the public site offers marketing pages and "
        "a sign-up, with no unauthenticated name lookup. The "
        "owner-of-record data it aggregates is searchable only from inside "
        "a paid seat.\n"
        "\n"
        "Recorded here rather than in BLOCKED after weighing it: the "
        "registration wall is real, but unlike the CourtRecords.us case "
        "there is no consumer-facing search route that we were stopped at "
        "-- the lookup is an internal tool feature, not a public surface "
        "with a gate on it. If a recheck finds an addressable public "
        "search behind login, move this row to BLOCKED."
    ),
    "consumerdataprotect-com": (
        "Verified 2026-09-24. Consumer Data Protect is an opt-out "
        "SERVICE, not a data source -- it submits removal requests on a "
        "customer's behalf. It holds no name-keyed index of its own to "
        "search, so a search surface is not merely absent but "
        "inapplicable.\n"
        "\n"
        "Kept in the dataset as a row because it does collect personal "
        "data from its own customers, and its opt-out leg is real; see "
        "optout_forms, including the load-bearing '?ref=' parameter and "
        "the dual timing traps on that form."
    ),
    "cortera-com": (
        "Verified 2026-09-24. Cortera sells BUSINESS credit and trade "
        "payment data, now under Moody's. Its subjects are companies "
        "rather than individuals, and its reports are delivered to "
        "subscribers; there is no consumer name search and, for a "
        "business-credit product, no reason to expect one.\n"
        "\n"
        "Recorded because it is a shared-surface family member worth "
        "remembering: Cortera's subject-request route is an Alchemer "
        "survey (id 8249180) that is the SAME survey alchemer-com uses. "
        "That is a CORPORATE shared surface -- two Moody's properties "
        "pointing at one intake -- not a white-label engine like "
        "optOutLight, and the distinction matters when predicting which "
        "other rows will collapse together."
    ),
    "credit-com": (
        "Verified 2026-09-24. Credit.com is a consumer "
        "credit-education and lead-generation site. It does not publish a "
        "name-keyed index of third parties; what it holds is data on its "
        "OWN registered users, reachable through an account, plus the "
        "leads it passes to lenders. No public lookup surface exists.\n"
        "\n"
        "Only the SEARCH leg was ever missing here: credit-com's opt-out "
        "leg is a shipped recipe (RECIPES in optout_forms, verified "
        "2026-09-22 against its OneTrust DSAR webform). This batch "
        "re-probed that form before noticing, and the re-read agreed with "
        "the recipe in every particular, including its choice to refuse "
        "the optional SSN-last-4 and date-of-birth boxes via "
        "forbidden_selectors. See the batch-23 note in OPTOUT_BLOCKED for "
        "why that duplicate write-up was deleted."
    ),
    "creditreform-de": (
        "Verified 2026-09-24. Creditreform is a German credit reference "
        "agency. Subject access runs through a Selbstauskunft request "
        "under Art. 15 GDPR, handled by the regional Creditreform office "
        "by post or its own portal; there is no public name-search page, "
        "and German law routes the subject to a request rather than a "
        "lookup.\n"
        "\n"
        "Flagging the scope question rather than deciding it: whether "
        "non-US credit bureaus belong in this dataset at all is on the "
        "open list with Penn. Mapped honestly in the meantime."
    ),
    # --- batch 22 of 2026-09-24: B-C sweep, credit bureaus / adtech ------
    #
    # Two of these fourteen close for a reason this bucket has not had
    # before: ca-gov and catalogchoice-org are NOT DATA BROKERS at all --
    # a state regulator's opt-out platform and a nonprofit junk-mail
    # service, swept into the dataset by a harvester. They are recorded
    # here rather than skipped so the sweep's own arithmetic stays honest,
    # but the reason they have no consumer lookup is that there is nothing
    # to look up, not that a broker withheld one. See optout_forms, where
    # both are filed OUT_OF_SCOPE with the defect written up.
    #
    # The four credit bureaus are worth a word too. Buro de Credito,
    # Circulo de Credito, Centrix and CIAL D&B all hold richly
    # name-keyed records, and all four offer the subject a way to SEE
    # them -- but always as a purchased, authenticated credit report, not
    # as a public lookup. That is the correct posture for a bureau and
    # this bucket is the right home, but the reason differs from the
    # adtech rows: not "the data is not keyed to people" but "the data is
    # keyed to people and access is deliberately gated".
    "burodecredito-com-mx": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup of the kind this module means exists here. Buró de "
        "Crédito (Trans Union de México) is a regulated Mexican credit "
        "bureau; a consumer can obtain their own Reporte de Crédito "
        "Especial, but only through an authenticated, identity-verified, "
        "purchased flow -- the only free-text input on the public site is "
        "the site-wide page search. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer publicly, and a credit bureau SHOULD NOT offer an anonymous "
        "self-lookup: the same reasoning that closed locatesmarter-com and "
        "usinfosearch-com applies with more force here."
    ),
    "buyerlink-co": (
        "Verified 2026-09-24 by rendering the site and its privacy policy "
        "in full. No consumer-facing lookup exists here. Buyerlink sells "
        "digital-marketing lead generation for automotive, real estate and "
        "home improvement; every route on the site is 'Request a Demo' or "
        "'Log In'. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "ca-gov": (
        "Verified 2026-09-24 by browser render. No consumer lookup exists "
        "here, and the row should be read with its opt-out leg: ca.gov is "
        "the State of California, and the recorded URL is the California "
        "Privacy Protection Agency's DROP platform -- a regulator's "
        "opt-out tool, not a data broker. There is nothing here to look "
        "oneself up in. The leg is closed on that basis rather than on any "
        "finding about a broker's product."
    ),
    "cadent-com": (
        "Verified 2026-09-24 by rendering the site and its privacy portal. "
        "No consumer-facing lookup exists here. Cadent is a television "
        "advertising platform; its records are keyed to advertising "
        "identifiers and households rather than to names. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "captifytechnologies-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Captify sells search-intent data to "
        "advertisers and publishers; its consumer route is a mailbox. The "
        "leg is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "carpe-io": (
        "Verified 2026-09-24 by rendering the site and its privacy policy "
        "in full. No consumer-facing lookup exists here. Carpe Data sells "
        "claims and underwriting intelligence to P&C insurers -- its "
        "customers are carriers, and the people in its data are claimants "
        "who never dealt with it directly. The leg is closed because a "
        "search recipe cannot be written against a surface the broker does "
        "not offer.\n"
        "\n"
        "Worth noting honestly: this is one of the rows where the absence "
        "matters most. A claimant has no way to see what an insurer was "
        "told about them, and no lookup is offered to make that possible."
    ),
    "cashmereai-com": (
        "Verified 2026-09-24 by rendering the live policy (the recorded "
        "URL 404s -- see optout_forms). No consumer-facing lookup exists "
        "here. Pludo Inc. dba Cashmere sells a client-intelligence "
        "platform to banks; its only public routes are 'Request a Demo' "
        "and 'Login'. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "catalogchoice-org": (
        "Verified 2026-09-24 by browser render. No consumer lookup of a "
        "PERSON exists here, and the row should be read with its opt-out "
        "leg: Catalog Choice is a nonprofit junk-mail opt-out service, not "
        "a data broker. It does offer a search -- /catalogs is a browsable "
        "A-Z of about 920 pages of CATALOG PUBLISHERS -- but that is a "
        "directory of merchants to cancel, not a way to look up a person. "
        "The leg is closed on that basis. Recording the distinction "
        "because a keyword sweep for 'search' would otherwise flag this "
        "site as having a lookup."
    ),
    "cengagegroup-com": (
        "Verified 2026-09-24 by rendering the site and its rights form. No "
        "consumer-facing lookup exists here. Cengage is an education "
        "publisher; the individuals in its systems are learners at "
        "institutions it serves, and it holds much of that data as a "
        "processor. A self-lookup across institutions is not something it "
        "could offer. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "centrix-co-nz": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup of the kind this module means exists here. Centrix is a "
        "New Zealand credit bureau: a consumer can obtain their own credit "
        "report, but through an authenticated, identity-verified flow, and "
        "the site's public 'Check a Consumer' / 'Check a Business' routes "
        "are products sold to CREDIT PROVIDERS, not self-lookups -- an "
        "anonymous search there would be a stranger-lookup tool. The leg "
        "is closed on the permissible-purpose reasoning already applied to "
        "locatesmarter-com and usinfosearch-com.\n"
        "\n"
        "The substantive finding on this row is on the opt-out leg: the "
        "suppression form requires a date of birth, which resolve_fields "
        "cannot supply."
    ),
    "cialdnb-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. CIAL Dun & Bradstreet sells business-credit "
        "and supplier intelligence across Latin America; its subjects are "
        "largely companies, and its lookup products are sold to "
        "subscribers. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer.\n"
        "\n"
        "Caveat carried from the opt-out leg: the page read was the "
        "Spanish-language one, and this is a 43-country network. A "
        "per-country site may differ."
    ),
    "circulodecredito-com-mx": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup of the kind this module means exists here. Círculo de "
        "Crédito is a regulated Mexican credit bureau; the consumer route "
        "to their own Reporte de Crédito Especial runs through "
        "registration and login ('Regístrate', 'Ingresa'). The leg is "
        "closed for the same reason as burodecredito-com-mx: not only is "
        "no public lookup offered, a bureau should not offer one."
    ),
    "citydata-ai": (
        "Verified 2026-09-24 by rendering the live policy (the recorded "
        "URL 404s -- see optout_forms). No consumer-facing lookup exists "
        "here. CityData.AI sells mobility and location analytics for civic "
        "use; it states its data is anonymized, obfuscated and aggregated, "
        "which -- if true -- means there is no individual record to "
        "surface. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "claritas-com": (
        "Verified 2026-09-24 by rendering the site and its OneTrust "
        "webform. No consumer-facing lookup exists here. Claritas sells "
        "consumer segmentation and audience data to marketers; its "
        "consumer route is a rights webform. The leg is closed because a "
        "search recipe cannot be written against a surface the broker does "
        "not offer.\n"
        "\n"
        "Note the asymmetry recorded on the opt-out leg: to make a request "
        "Claritas requires a document upload, while offering the subject "
        "no way to see what it holds in the first place."
    ),
    # --- batch 21 of 2026-09-24: A-B sweep, marketing / people-search ----
    #
    # Fourteen of sixteen close here, and the split inside the batch is
    # clean: every marketing/adtech/B2B row has no consumer lookup, and
    # the only two rows that do are the two people-search sites
    # (backgroundcheckers-net, blockshopper-com), both of which are in
    # SEARCH_UNDECIDED rather than here. That is the pattern the whole
    # sweep keeps reproducing -- a self-lookup exists when, and only when,
    # showing people their own record is the product.
    "adara-com": (
        "Verified 2026-09-24 by rendering the site's privacy pages. No "
        "consumer-facing lookup exists here. Adara (RateGain) sells travel "
        "advertising and analytics; its own opt-out page says its tool is "
        "cookie-based, which is as direct a statement as a broker makes "
        "that its records are keyed to a browser rather than to a person. "
        "The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer. The opt-out leg "
        "carries the finding on this row."
    ),
    "affinity-co": (
        "Verified 2026-09-24 by rendering www.affinity.co in full. No "
        "consumer-facing lookup exists here. Affinity sells relationship-"
        "intelligence CRM software to dealmakers; the only input on the "
        "site is its own content search (GET /search). The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "agedleadstore-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Aged Lead Store sells insurance leads to "
        "agents; its consumer-facing pages are opt-out forms, and its "
        "'BROWSE LEADS' route is an inventory catalogue for buyers, not a "
        "self-lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer.\n"
        "\n"
        "Worth one line of honesty: a lead broker plainly HOLDS "
        "name-keyed records -- its opt-out form asks for exactly those "
        "fields. It simply does not let the subject look at them. The "
        "absence is a choice here, not a consequence of the data's shape."
    ),
    "alchemer-com": (
        "Verified 2026-09-24 by rendering the surface. No consumer-facing "
        "lookup exists here, and note what the domain is: alchemer.com is "
        "a SURVEY PLATFORM, and the page behind this row is a tenant's "
        "request survey. The platform holds nothing about the requester to "
        "look up, and the tenant (Regulatory DataCorp) would offer any "
        "lookup on its own site rather than here. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer. Same shape as privacypillar-com in batch "
        "20; see optout_forms for the domain-attribution note."
    ),
    "alescodata-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Alesco Data sells consumer and B2B marketing "
        "lists; every route is 'Request Quote' or 'Partnerships'. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "applecart-co": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Applecart builds relationship graphs for "
        "advocacy and political campaigns; its consumer-facing page is a "
        "privacy request form only. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer -- and note that its own request form restricts itself to "
        "residents of sixteen states, so even the request route is "
        "narrower than the data it holds."
    ),
    "beeswax-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Beeswax (now FreeWheel/Comcast) is a "
        "bidder/DSP; its records are bid requests and device identifiers, "
        "not names. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer. The opt-out "
        "leg is the open one, and for an unusual reason -- see "
        "optout_forms for the broken certificate on their own opt-out "
        "host."
    ),
    "belardiwong-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Belardi Wong (Belardi Ostroy) is a direct-"
        "marketing agency; its only consumer route is the OneTrust DSAR "
        "webform linked from its privacy policy. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "blackbaud-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Blackbaud sells fundraising and donor-"
        "management software to nonprofits and schools; the individuals in "
        "its systems are its CUSTOMERS' donors, and it holds that data as "
        "a processor on their behalf. A self-lookup across tenants is not "
        "a thing it could offer even in principle. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer -- its own webform, per optout_forms, is "
        "framed around access and correction rather than lookup."
    ),
    "blackbox-email": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. BlackBox (Indiemark LLC) sells email "
        "marketing services; its only consumer route is a HubSpot 'Data "
        "Requests for California Residents' form. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "bolttech-io": (
        "Verified 2026-09-24 by rendering the surface. No consumer-facing "
        "lookup exists here. bolttech is an insurance-technology company; "
        "its consumer route is a OneTrust DSAR webform. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer. Note the dataset files this row under "
        "'risk' rather than 'marketing' -- if that category is right, the "
        "same permissible-purpose reasoning that closed usinfosearch-com "
        "in batch 20 would apply here too."
    ),
    "box-com": (
        "Verified 2026-09-24 by rendering the recorded URL. No consumer-"
        "facing lookup exists there -- but read the opt-out leg before "
        "trusting this row, because the row itself is a dataset defect: "
        "box.com is Box, Inc., a file-sharing host, and the company named "
        "on the row is EAB Global, whose domain is eab.com. What was "
        "actually observed is a PDF viewer showing EAB's privacy policy. "
        "The leg is closed as to box.com, which offers no lookup and is "
        "not a broker; whether EAB offers one at eab.com is a separate "
        "question this row cannot answer."
    ),
    "brandwatch-com": (
        "Verified 2026-09-24 by rendering the site and its /legal/ index. "
        "No consumer-facing lookup exists here. Brandwatch (Crimson "
        "Hexagon / Runtime Collective / Cision) sells social-listening "
        "analytics; the only input on its pages is the site content "
        "search. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer.\n"
        "\n"
        "Note in passing: Brandwatch publishes a separate 'Author Privacy "
        "Statement' for the people whose public posts it indexes, which is "
        "a clearer acknowledgement than most adtech vendors give that "
        "non-customers are in the data. It still offers them no lookup."
    ),
    "bridg-com": (
        "Verified 2026-09-24 by rendering the site and its DataGrail "
        "portal. No consumer-facing lookup exists here. Bridg (a division "
        "of Cardlytics) resolves offline purchase data for retailers; its "
        "consumer route is a privacy request portal, not a search. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    # --- batch 20 of 2026-09-24: adtech / B2B list / identity vendors -----
    #
    # The adtech half of this batch closes for a reason worth stating once:
    # these companies do not hold a record a person could look themselves up
    # IN. Their inventory is bid requests, cookies and device graphs, and
    # the identifier is not a name. "No consumer lookup" here is not a
    # withheld feature; it is a consequence of the data's shape.
    "luc-id": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. luc.id redirects to cint.com -- Lucid was "
        "acquired by Cint -- and cint.com is a market-research marketplace "
        "sold to researchers and brands; its only input is a site search "
        "over its own pages. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer. The "
        "opt-out leg is the open one on this row."
    ),
    "machintel-com": (
        "Verified 2026-09-24 by rendering the site, including its "
        "do-not-sell page. No consumer-facing lookup exists here. Machintel "
        "sells B2B demand generation; the only input anywhere on the site "
        "is the newsletter box. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer. See "
        "optout_forms -- that newsletter box is the whole finding on this "
        "row."
    ),
    "madhive-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Madhive is a connected-TV advertising "
        "platform; its records are keyed to devices and bid requests, not "
        "to names, so there is nothing a person could search by. The leg is "
        "closed because a search recipe cannot be written against a surface "
        "the broker does not offer."
    ),
    "madisonlogic-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Madison Logic sells account-based marketing "
        "and intent data to B2B advertisers; every route on the site is "
        "'Request Demo' or 'Book a Demo'. The leg is closed because a "
        "search recipe cannot be written against a surface the broker does "
        "not offer."
    ),
    "magnite-com": (
        "Verified 2026-09-24 by rendering the site, including its user "
        "choice portal. No consumer-facing lookup exists here. Magnite is a "
        "sell-side advertising platform; the only inputs on the page are a "
        "site search and a newsletter signup. The leg is closed because a "
        "search recipe cannot be written against a surface the broker does "
        "not offer -- and, per the note in optout_forms, its records are "
        "keyed to bid requests rather than to people."
    ),
    "privacypillar-com": (
        "Verified 2026-09-24 by rendering the portal. No consumer-facing "
        "lookup exists here, and the reason is structural rather than a "
        "choice: privacypillar.com is a PRIVACY-PORTAL PRODUCT, and the "
        "page behind this row's URL is the tenant's request form. The "
        "vendor holds nothing about the requester to look up. The leg is "
        "closed because a search recipe cannot be written against a surface "
        "the broker does not offer.\n"
        "\n"
        "Note for whoever reads the dataset row: it is filed under Malvern "
        "Media Inc., which is the tenant. Any lookup Malvern Media might "
        "offer would be on its own site, not on the portal, and that site "
        "was not reached this pass."
    ),
    "marinusanalytics-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here, and it is important that there is not one. "
        "Marinus Analytics sells Traffic Jam, a facial-recognition and "
        "ad-scraping tool licensed to law enforcement for trafficking "
        "investigations. A public self-lookup over that index would be a "
        "search engine for escort advertising, which is the opposite of "
        "what anyone should want built. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer, and it should stay closed.\n"
        "\n"
        "The opt-out leg carries the substantive finding on this row, "
        "including why it warrants a human decision before any automated "
        "submission."
    ),
    "marketforcecorp-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. MarketForce supplies B2B new-business lead "
        "data; its opt-out page is a submission form, not a search. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "matchbookdata-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Matchbook Data's records are DEVICE-KEYED -- "
        "its own privacy form requires a Device ID -- so there is no name a "
        "person could search by, and a name-based search recipe could not "
        "express the query even if a surface existed. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "maxmind-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here -- but this row deserves a sentence more than "
        "the usual, because MaxMind DOES publish a public demo that looks "
        "like one.\n"
        "\n"
        "MaxMind sells IP geolocation, and its records are keyed to IP "
        "ADDRESSES rather than to people. Any 'look up an address' tool it "
        "offers returns an inferred location for a network address, not a "
        "person's record, so it is not a self-lookup in this module's sense "
        "and a hit tells a user nothing about whether they are 'in' the "
        "database. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer.\n"
        "\n"
        "Same reasoning already applied to ipapi-co and ip2location; this "
        "is the third member of the IP-keyed family."
    ),
    "mchdata-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. MCH Strategic Data sells education and "
        "healthcare marketing lists; its database sits behind an account "
        "and a 'Build List' tool for buyers, which is an inventory query "
        "rather than a self-lookup. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "usinfosearch-com": (
        "Verified 2026-09-24 by rendering the site. usinfosearch.com IS a "
        "people-search product -- SSN traces, skip tracing, criminal and "
        "civil records -- but there is no consumer-facing lookup, and the "
        "site says why in its own words: 'This service is for qualified "
        "businesses only', 'Same-Day Credentialing', and 'Our services are "
        "for credentialed businesses only and may not be used for marketing "
        "purposes.' Access is gated behind a paid, credentialed account at "
        "$39.95/month.\n"
        "\n"
        "The leg is closed on the same basis as locatesmarter-com: a "
        "permissible-purpose vendor must NOT offer an anonymous "
        "self-lookup, so the absence is a compliance posture rather than a "
        "withheld feature. See optout_forms for the open question about "
        "whether this row belongs in the FCRA category."
    ),
    "digdevdirect-com": (
        "Verified 2026-09-24 by rendering the site. There is no site: "
        "digdevdirect.com is a GoDaddy parked page. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer -- see optout_forms, including the "
        "distinction between free parking and an auction listing and the "
        "standing warning about confirming ownership if the name ever "
        "serves content again."
    ),
    # --- batch 19 of 2026-09-24: list brokers / B2B data / skip tracing ---
    #
    # Same structural answer as batch 18 and for the same reason: these are
    # wholesale data vendors, and a free consumer lookup would give away the
    # inventory. One row is more interesting than that and is filed under
    # SEARCH_UNDECIDED instead: lusha-com, which advertises a 'control your
    # profile' route that was not rendered this pass.
    #
    # locatesmarter-com is NOT in this batch's additions even though its
    # opt-out leg is -- its search leg was already closed on 2026-09-22 and
    # that entry stands. Worth knowing when reading the counts: a broker can
    # be "unmapped" because one leg is missing, not both.
    "lightboxre-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. LightBox sells commercial-real-estate data and "
        "location intelligence to CRE professionals; the only input on "
        "either page rendered is a WordPress site search over its own "
        "marketing pages. The leg is closed because a search recipe cannot "
        "be written against a surface the broker does not offer.\n"
        "\n"
        "Category note: LightBox's records are keyed to PARCELS AND "
        "BUILDINGS rather than to people, so for most individuals the "
        "honest answer to 'am I in here' is no -- which is a real answer, "
        "not a failed lookup. See optout_forms for the broken do-not-sell "
        "link, which is the substantive finding on this row."
    ),
    "lionsharemarketing-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. LionShare sells direct-marketing lists; the "
        "opt-out page is a submission form, not a search. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer. The opt-out leg is the interesting one on "
        "this row -- it is staged in optout_forms."
    ),
    "lists-inc-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here, and very little else: the whole site is four "
        "pages of marketing copy for healthcare and consumer mailing-list "
        "products. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "listservicedirect-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. ListServiceDirect sells consumer mailing "
        "lists; its 'SIC Code Search' and 'Quick Counts' tools are "
        "inventory queries for BUYERS -- how many records match a segment "
        "-- not a self-lookup, and they return counts rather than people. "
        "The site's only other input is a WordPress site search. The leg is "
        "closed because a search recipe cannot be written against a surface "
        "the broker does not offer."
    ),
    "listsonline-com": (
        "Verified 2026-09-24 by rendering the site, which now trades as "
        "Everleads (see optout_forms for the domain change and why an HTTPS "
        "certificate error nearly buried this row). No consumer-facing "
        "lookup exists on everleads.com. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer.\n"
        "\n"
        "Worth noting for anyone who revisits this: the do-not-sell form "
        "IS, by the company's own description, a search -- 'We will use the "
        "information you provide solely for searching our data to determine "
        "if your information is present in our database.' But it returns "
        "its answer by email and by acting on it, not to the page, so there "
        "is nothing a search recipe could read."
    ),
    "livedatatechnologies-com": (
        "Verified 2026-09-24 by rendering the site (now livedatatech.com). "
        "No consumer-facing lookup exists here. Live Data Technologies "
        "tracks job changes across ~160 million professional profiles and "
        "sells access to sales teams; every route on the site is 'Talk to "
        "Sales' or 'Request a Data Test'. The leg is closed because a "
        "search recipe cannot be written against a surface the broker does "
        "not offer."
    ),
    "logiq-com": (
        "Verified 2026-09-24 by rendering the site. There is no site: "
        "logiq.com is a parked GoDaddy Auctions listing. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer -- see optout_forms, including the warning "
        "about confirming ownership if the name ever resolves to content "
        "again."
    ),
    "lotadata-com": (
        "Verified 2026-09-24: lotadata.com does not resolve, and the "
        "company stated to us directly that it is no longer operational. "
        "The leg is closed on the same basis as the opt-out leg; see "
        "optout_forms for why this row is filed as absence rather than as "
        "an open question."
    ),
    "loopme-com": (
        "Verified 2026-09-24 by rendering the site and its opt-out frame. "
        "No consumer-facing lookup exists here. LoopMe is a mobile "
        "advertising platform whose records are keyed to ADVERTISING "
        "IDENTIFIERS, not names -- its own opt-out asks for an AAID or "
        "IDFA. There is nothing a person could search by, and nothing a "
        "name-based search recipe could express. The leg is closed because "
        "a search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "lsmapps-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. L.S Mobile Apps publishes consumer mobile "
        "apps; its only privacy surface is the data-subject request form "
        "that optout_forms already carries a shipped recipe for. The leg is "
        "closed because a search recipe cannot be written against a surface "
        "the broker does not offer.\n"
        "\n"
        "This row's search leg was the last one outstanding for it, and "
        "reading it turned up drift in the SHIPPED opt-out recipe -- the "
        "live form has grown a security-code field. That finding is "
        "recorded at the head of OPTOUT_UNDECIDED in optout_forms; it is "
        "flagged there rather than acted on here."
    ),
    "localblox-com": (
        "Verified 2026-09-24 by rendering the site. localblox.com now "
        "serves an empty default WordPress installation with no content, "
        "and the recorded consumer subdomain no longer resolves. There is "
        "no lookup, because there is no site. The leg is closed here while "
        "the opt-out leg stays undecided, because 'this placeholder offers "
        "no search' is an observation and 'LocalBlox has no opt-out "
        "surface' would be a claim about a company that has merely gone "
        "quiet."
    ),
    # --- batch 18 of 2026-09-24: lead-generation / B2B contact vendors ----
    #
    # A structurally uniform batch on this leg. Every company here sells
    # contact or audience data to other BUSINESSES, and a consumer lookup is
    # not merely absent but contrary to the product: the people in these
    # databases are the inventory, and exposing a free self-search would let
    # anyone query the inventory for nothing. The one exception is
    # leadiq-com, which does offer a profile-claim route and is filed under
    # SEARCH_UNDECIDED rather than here.
    "koddi-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Koddi sells retail-media and commerce "
        "advertising technology to retailers and brands; its only consumer "
        "surface is the OneTrust DSAR webform embedded at /dsr-form. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "forian-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Forian sells healthcare and life-sciences data "
        "analytics to industry; the only input on its rights page is the "
        "request form itself. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer.\n"
        "\n"
        "Worth noting for the category rather than the leg: much of what "
        "Forian processes is de-identified claims data, where an individual "
        "lookup is impossible by construction and not merely withheld."
    ),
    "lead411-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Lead411 sells B2B contact records by "
        "subscription; its database is behind a login and a free trial, and "
        "the site's only other input is a WordPress site search over its own "
        "marketing pages. The leg is closed because a search recipe cannot "
        "be written against a surface the broker does not offer.\n"
        "\n"
        "ONE THING THAT LOOKS LIKE A SEARCH LEG AND IS NOT, recorded so it "
        "is not rediscovered: the privacy form at /your-privacy-choices/ "
        "offers 'Access My Personal Information' and 'Know What Personal "
        "Information' as request types, which would tell a person what is "
        "held on them. That is a statutory ACCESS REQUEST -- emailed back "
        "after an identity code is verified -- not a lookup, and it belongs "
        "to the opt-out leg's machinery, not this one."
    ),
    "getrev-ai": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. GetRev sells AI-driven demand generation to B2B "
        "sales teams; its only consumer surface is the privacy request form. "
        "The leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "leadloft-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here, and nothing else consumer-facing either: "
        "leadloft.com is a sales-prospecting product whose every form is a "
        "'Start Free Trial' capture. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer. See optout_forms for the full account of what this site does "
        "carry, which is worth reading before anyone submits anything to it."
    ),
    "leadmemedia-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. policy.leadmemedia.com serves a rights-request "
        "form and nothing else; LeadMe Media is a performance-marketing lead "
        "vendor with no public database. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "leadpost-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. LeadPost sells website-visitor identification "
        "to advertisers -- resolving anonymous traffic to named people -- so "
        "its records are keyed to browsing activity a person cannot query. "
        "app.leadpost.com serves the opt-out form and no lookup. The leg is "
        "closed because a search recipe cannot be written against a surface "
        "the broker does not offer."
    ),
    "leadspace-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. Leadspace sells B2B audience data to marketing "
        "teams; the only input on its site outside the do-not-sell form is a "
        "site search over its own pages. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "leidosiq-com": (
        "Verified 2026-09-24 by rendering the site. No consumer-facing "
        "lookup exists here. leidosiq.com is a product marketing site for "
        "Intranet Quorum, the constituent-management system used by "
        "legislative offices; the only inputs on it are two site-search "
        "boxes over its own pages. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer.\n"
        "\n"
        "The category question about whether this row belongs in the dataset "
        "at all is recorded in optout_forms and is not restated here."
    ),
    # --- batch 17 of 2026-09-23: adtech / list / martech vendors ----------
    #
    # Same reasoning as the groups below: sold to businesses, no public name
    # lookup, so the SEARCH leg is terminal. Opt-out legs are separate and
    # mostly open. One URL each was fetched.
    "kochava-com": (
        "Verified 2026-09-23. Mobile attribution/adtech. Its opt-out form "
        "(see optout_forms) is keyed on a MAID, which is the clearest "
        "possible statement that its records are not indexed by name and "
        "that a name-based presence check could not address them."
    ),
    "kargo-com": (
        "Verified 2026-09-23. Mobile advertising; the reachable surface is a "
        "privacy portal, not an index."
    ),
    "kindsight-io": (
        "Verified 2026-09-23. Nonprofit-fundraising data platform sold to "
        "institutions; the reachable page is an opt-out form on a HubSpot "
        "landing domain. No public lookup."
    ),
    "jmr-media-com": (
        "Verified 2026-09-23. Media/advertising; the only forms on the "
        "reachable page are its do-not-sell form and two hidden Netlify "
        "stubs (see optout_forms). No consumer index."
    ),
    "jungroup-com": (
        "Verified 2026-09-23. Marketing agency; the reachable CCPA page "
        "carries request forms only."
    ),
    "kbsynergy-com": (
        "Verified 2026-09-23. Marketing services; the do-not-sell page "
        "embeds a third-party lead-gen iframe and nothing resembling a "
        "consumer index."
    ),
    "knowwho-com": (
        "Verified 2026-09-23, with a caveat. KnowWho sells legislative and "
        "government-official contact data, and its site root is a LOGIN plus "
        "a site keyword search -- so a directory does exist, but it is "
        "behind authentication and its subjects are officials in their "
        "public capacity rather than private individuals. Recorded as "
        "no-surface for the presence check this tool performs; not a claim "
        "that nothing is searchable there."
    ),
    "klarifi-io": (
        "Verified 2026-09-23 on thin evidence: the site root carries only a "
        "generic contact form. No lookup seen, but only one page was read."
    ),
    "jverify-com": (
        "Verified 2026-09-23 on thin evidence: site root returns 200 with no "
        "forms captured. Identity-verification by name, which would be a "
        "gated institutional service rather than a public index -- but only "
        "one page was read."
    ),
    "knowertech-com": (
        "Verified 2026-09-23 on thin evidence: site root returns 200 with no "
        "forms captured and no other path was supplied by the dataset."
    ),
    "jdpower-com": (
        "Verified 2026-09-23. J.D. Power sells automotive and market "
        "research to businesses; the reachable path is a DSAR page. No "
        "consumer-facing person lookup."
    ),
    # --- batch 16 of 2026-09-23: intent-data / enterprise vendors ---------
    #
    # Same reasoning as the B2B group below, restated only where a row needs
    # it. None of these sells a public name lookup; their product is sold to
    # companies. The SEARCH leg is genuinely terminal. Nothing here is a
    # statement about the opt-out leg, which is open for most of them.
    # Limit, as ever: one URL each was fetched.
    "intentiq-com": (
        "Verified 2026-09-23. Identity-graph / advertising company. The "
        "reachable surface is an opt-out page whose only form is a mailing-"
        "list signup (see optout_forms); there is no consumer lookup. If "
        "Intent IQ keys on a cookie or device id rather than a name, a "
        "presence search could not be expressed against it anyway."
    ),
    "intentsify-io": (
        "Verified 2026-09-23. B2B intent-data platform; app.intentsify.io "
        "is a customer application behind a login, not an index."
    ),
    "intentgine-com": (
        "Verified 2026-09-23. B2B intent-data vendor. The only form on the "
        "site root is its own content search (input name='s'), which "
        "searches marketing pages, not people."
    ),
    "intentmacro-com": (
        "Verified 2026-09-23. B2B intent-data vendor; the forms on the "
        "reachable page are a CCPA request form and a sales contact form "
        "(Company, Job Title). No consumer index."
    ),
    "ispot-tv": (
        "Verified 2026-09-23. TV-advertising measurement. The only form "
        "captured is a 'Get A Demo' sales widget; measurement panels are "
        "not publicly searchable."
    ),
    "iqvia-com": (
        "Verified 2026-09-23. Healthcare data and clinical research. Its "
        "reachable privacy surface is a OneTrust DSAR portal (see "
        "optout_forms); health data is emphatically not exposed as a "
        "public lookup, and it would be a serious finding if it were."
    ),
    "irys-us": (
        "Verified 2026-09-23. No consumer lookup on the reachable pages; "
        "the site's own search box (action /search, input name='q') "
        "searches site content."
    ),
    "esiteanalytics-com": (
        "Verified 2026-09-23 on thin evidence, and flagged as such: the "
        "site root returns 200 with no forms at all. Web-analytics vendor "
        "by name, which would put it in this category, but only one page "
        "was seen. Re-probe if this row matters downstream."
    ),
    "ididata-com": (
        "Verified 2026-09-23. ID Insight is a fraud/identity-verification "
        "vendor selling to institutions; the reachable page is a privacy "
        "statement. No public lookup, and by the nature of the product "
        "there should not be one."
    ),
    "idatabasesolutions-com": (
        "Verified 2026-09-23 on thin evidence: only the do-not-sell path "
        "was probed and it carries no visible form (script-built, see "
        "optout_forms). No search surface was seen, but the site root was "
        "not read, so this is weaker than the rows above it."
    ),
    # --- batch of 2026-09-23: B2B data suppliers with no consumer index ---
    #
    # These all resolved the same way and the reasoning is identical, so it is
    # stated once: each is a business-to-business data or marketing-technology
    # company whose product is sold to companies, not a site where a member of
    # the public types a name and gets a record back. There is nothing for a
    # presence check to query -- not because the data is absent (it is very
    # much present, which is why they are in the dataset) but because it is
    # never exposed as a public lookup. That is a genuine terminal state for
    # the SEARCH leg, and it is NOT a statement about the opt-out leg, which
    # is tracked separately and in most of these cases is still open.
    #
    # The honest limit on all of them: one URL each was fetched. A search
    # surface on some other path would not have been seen.
    "infutor-com": (
        "Verified 2026-09-23 to the extent stated above. Identity-"
        "resolution data supplier; /privacy-center/ and the site nav "
        "(Products, Business Need, Industries, Resources, About, Contact) "
        "are entirely B2B. No consumer lookup exists to query."
    ),
    "inmarket-com": (
        "Verified 2026-09-23. Location/advertising data company; the "
        "reachable surface is a 'Privacy Request Center' at "
        "preferences.inmarket.com, not an index. Likely keys on a mobile "
        "advertising ID rather than a name in any case -- see the "
        "identifier-shape cluster noted in optout_forms."
    ),
    "inflection-com": (
        "Verified 2026-09-23. Corporate site for GoodHire and the "
        "SafeDecision/Insight APIs; background-screening results are "
        "delivered to the employer who ordered them, never through a "
        "public search. See also the FCRA category note in optout_forms."
    ),
    "inchecksolutions-com": (
        "Verified 2026-09-23. Employment-screening CRA. The only form on "
        "the homepage is the site's own content search (input name='s'), "
        "which searches InCheck's marketing pages, not people -- worth "
        "saying explicitly because a fill-everything pass would happily "
        "type a name into it and report the resulting page as a hit."
    ),
    "inboundinsight-com": (
        "Verified 2026-09-23. B2B marketing/intent-data vendor (the site "
        "leads with a webinar registration and a 'Schedule Demo' button). "
        "No consumer-facing lookup."
    ),
    "emailindustries-com": (
        "Recorded 2026-09-23 on WEAKER evidence than the others in this "
        "group, and flagged as such: the site timed out and was never "
        "read. Placed here on the strength of the company's name and "
        "category alone -- email-deliverability tooling sold to senders. "
        "If that inference matters to anything downstream, re-probe; the "
        "opt-out leg is separately undecided for the same timeout."
    ),
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
    "crawlbee-com": (
        "Verified 2026-09-23: crawlbee.com is no longer a company site. "
        "It redirects to forsale.godaddy.com -- the domain is parked for "
        "sale -- and even that lands on an Akamai 'Access Denied' page. "
        "There is nothing to search and nobody to ask. Recorded as a "
        "retired domain rather than a reachability failure, because the "
        "redirect target identifies what happened."
    ),
    "creditinfosystems-com": (
        "Verified by browser render 2026-09-23: Credit Information "
        "Systems sells tri-merge credit reports and verification services "
        "to LENDERS; the consumer's route to that data is an FCRA dispute "
        "through the lender or the underlying bureau. "
        "creditinfosystems.com is a marketing site whose only entrance is "
        "CLIENT LOGIN, with no form and no lookup of any kind."
    ),
    "crisil-com": (
        "Verified by browser render 2026-09-23: Crisil is an S&P Global "
        "ratings, research and analytics firm; its subjects are companies "
        "and securities, not consumers. The site's only person-shaped "
        "forms are a login, a password reset and a user registration. No "
        "public people search exists."
    ),
    "crsspxl-com": (
        "Verified by browser render 2026-09-23: Cross Pixel deals in "
        "browser COOKIES for behavioural ad targeting, not named people "
        "-- its own opt-out page reports on 'the status of Behavioral "
        "Targeting against your browser' and told this visitor 'No Cross "
        "Pixel cookie found.' There is no name-keyed record and nothing "
        "to look up."
    ),
    "crunchbase-com": (
        "Verified by browser render 2026-09-23: Crunchbase's database is "
        "about COMPANIES, funding rounds and the executives attached to "
        "them, and querying it is a paid, logged-in product. There is no "
        "free page that takes a person's name and returns a personal "
        "record. No search surface for this tool to read."
    ),
    "smartmove-us": (
        "Verified 2026-09-23: smartmove.us serves a Cloudflare "
        "interstitial ('Performing security verification ... This page is "
        "displayed while the website verifies you are not a bot', Ray ID "
        "logged) on the recorded URL, so nothing renders for an automated "
        "visitor on either leg. SmartMove is TransUnion's landlord-facing "
        "tenant-screening product -- reports are pulled by a landlord "
        "with the applicant's consent, so a public lookup is implausible "
        "anyway -- but the wall is what was actually observed. Note the "
        "dataset's contact for this row, zell@ctam.com, matches neither "
        "TransUnion nor SmartMove and should be doubted."
    ),
    "cuebiq-com": (
        "Verified by browser render 2026-09-23: Cuebiq sells mobile "
        "LOCATION data keyed to device advertising ids, sold to brands "
        "and researchers as aggregated audiences. There is no named- "
        "person record and no public lookup; the only forms on the site "
        "are a content search and a 'Request Live Demo' marketing form."
    ),
    "cyndx-com": (
        "Verified 2026-09-23: Cyndx is winding down. Its california-do- "
        "not-track page now serves a founder's letter -- 'After careful "
        "consideration, we have made the difficult decision to wind down "
        "and dissolve Cyndx' -- in place of any content, and no product "
        "remains. Cyndx searched COMPANIES and capital markets rather "
        "than consumers in any case, so no search surface existed to read "
        "even before the dissolution."
    ),
    "data-axle-com": (
        "Verified by browser render 2026-09-23: Data Axle sells consumer "
        "and business marketing lists and data hygiene to businesses; "
        "delivery is by file, feed or platform under contract. Its public "
        "site has no lookup -- the only name-taking form is the privacy "
        "rights request itself. Note the dataset's opt_out_email for this "
        "row is doba_privacy@donorbase.com, a different brand "
        "(DonorBase), which may indicate several Data Axle brands "
        "collapsed into one row."
    ),
    "datadecisionsgroup-com": (
        "Verified by browser render 2026-09-23: Data Decisions Group "
        "sells B2B and consumer marketing data and appending services to "
        "businesses. The public site has no lookup of any kind; the only "
        "forms on its privacy page are a newsletter subscribe and a 'Talk "
        "to an Expert' contact form."
    ),
    "datadelivers-com": (
        "Verified by browser render 2026-09-23: DataDelivers is a "
        "customer-data platform selling audience data to marketers -- "
        "delivery is by integration, not by a public query. No lookup "
        "exists; the only form on the domain is the WordPress site "
        "search. Recorded alongside a DATASET DEFECT on the opt-out leg: "
        "the row's opt_out_url (/unsubscribe/) 404s."
    ),
    "datafacts-com": (
        "Verified by browser render 2026-09-23: Data Facts is a "
        "background- and tenant-screening CRA plus mortgage verification "
        "services, sold to employers, landlords and lenders. Its consumer "
        "entrances are 'Applicant Support' and 'Client Login', both "
        "authenticated; no public page takes a name and returns a person."
    ),
    "datafy-com": (
        "Verified by browser render 2026-09-23: Datafy sells location- "
        "based attendance and visitation analytics built on mobile device "
        "advertising ids -- its own rights form asks for a Mobile Device "
        "Advertising ID as a REQUIRED field, which is the clearest "
        "possible statement that its records are keyed to devices rather "
        "than names. No public person lookup."
    ),
    "datalinedata-com": (
        "Verified by browser render 2026-09-23: Dataline sells targeted "
        "consumer marketing data to brands and agencies; the site is "
        "marketing plus 'Request a Demo' and exposes no lookup. Recorded "
        "alongside a DATASET DEFECT on the opt-out leg: the row's "
        "opt_out_url (/privacy-portal/) 404s."
    ),
    "businesswatchnetwork-com": (
        "Verified by browser render 2026-09-23: Business Watch Network "
        "publishes B2B webinars, articles and whitepapers and collects "
        "registrant details for its sponsors -- a lead-generation "
        "publisher rather than a records broker. Its forms are a site "
        "search, a newsletter subscribe (user[email]) and a Yotpo review "
        "widget. No person lookup exists."
    ),
    "porchgroupmedia-com": (
        "Verified by browser render 2026-09-23: Porch Group Media sells "
        "consumer marketing data and mover/new-homeowner audiences to "
        "brands; delivery is by list and platform under contract. Neither "
        "porchgroupmedia.com nor its two consumer subdomains offers a "
        "lookup of any kind. No search surface."
    ),
    "deluxe-com": (
        "Verified by browser render 2026-09-23: Deluxe is a payments and "
        "business-services company (checks, payroll, merchant services) "
        "whose consumer data arrives through its business customers. No "
        "public person lookup exists. Recorded alongside a reachability "
        "note on the opt-out leg: the recorded do-not-sell URL failed "
        "with an HTTP/2 protocol error."
    ),
    "datapartners-com": (
        "Verified by browser render 2026-09-23: Data Partners sells "
        "consumer and B2B marketing lists to advertisers and agencies. "
        "Its public site is marketing plus a set of request forms; there "
        "is no lookup and no directory."
    ),
    "dataskip-io": (
        "Verified 2026-09-23: DataSkip is a SKIP-TRACING service, which "
        "makes it a genuine people-lookup business -- but the lookup is a "
        "paid, authenticated product, not a public page. The recorded URL "
        "redirects to /pricing, which advertises '4 cents per hit', a "
        "'98.9% hit rate on typical lists', a dashboard and a developer "
        "API, all behind SIGN IN / SIGN UP. There is no free query "
        "surface to read, and a paid one is a purchase decision rather "
        "than a research one."
    ),
    "datasys-com": (
        "Verified by browser render 2026-09-23: Datasys sells omnichannel "
        "marketing data and audience activation to advertisers. The site "
        "is platform marketing with a 'Talk to an Expert' contact route; "
        "no consumer lookup exists anywhere on it."
    ),
    "datonics-com": (
        "Verified by browser render 2026-09-23: Datonics sells audience "
        "segments built on browser and device identifiers to ad buyers -- "
        "its own rights form offers a Mobile Advertising ID field, which "
        "is the shape of its records. No name-keyed public lookup."
    ),
    "datamasters-org": (
        "Verified by browser render 2026-09-23: DataMasters rents direct- "
        "mail, telephone and email marketing lists by segment, sold by "
        "quote. The only forms on the site are a menu search and a 'GET A "
        "QUOTE' sales form. No person lookup exists."
    ),
    "decide-co": (
        "Verified by browser render 2026-09-23: Decide Technologies "
        "(formerly LockerDome) runs an advertising decision marketplace "
        "for advertisers and publishers. Its records are ad-serving "
        "identifiers and accounts, not a consumer directory, and the site "
        "offers no lookup."
    ),
    "decisionlinks-com": (
        "Verified by browser render 2026-09-23: DecisionLinks sells "
        "compiled consumer marketing data and credit-adjacent products to "
        "businesses; its own opt-out page describes the right as opting "
        "out of MARKETING messages. Products sit behind Login / Book a "
        "demo. No public person lookup."
    ),
    "deeprootanalytics-com": (
        "Verified by browser render 2026-09-23: Deep Root Analytics is a "
        "political media-analytics firm selling audience targeting to "
        "campaigns; its data is delivered inside client engagements. "
        "privacy.deeprootanalytics.com is a request portal, not a "
        "directory, and the main site publishes no lookup."
    ),
    "coresignal-com": (
        "Verified by browser render 2026-09-23: Coresignal sells bulk "
        "public-web datasets and APIs about companies, employees and jobs "
        "to data teams -- delivery is by dataset or API key under "
        "contract, gated behind Log in / Start free. Its site offers no "
        "per-person query page. Note the business does hold person-level "
        "employment records, so the opt-out leg matters even though the "
        "search leg has no surface."
    ),
    "delivr-ai": (
        "Verified by browser render 2026-09-23: Delivr sells "
        "deterministic identity resolution and person-level intent to B2B "
        "marketers. It does put one lookup-shaped control on its homepage "
        "-- an email box labelled 'See your own intent signal' with a "
        "'Look up' button -- but that is a self-service demo keyed to the "
        "VISITOR's own email address, not a name-driven search of a third "
        "party, and it feeds a sales funnel ('Get Started', 'Talk to "
        "Sales'). Not a search surface in the sense this module means, "
        "and recorded explicitly so a future reader does not mistake it "
        "for one."
    ),
    "demandscience-com": (
        "Verified by browser render 2026-09-23: Demand Science sells B2B "
        "demand generation and buying-committee contact data to "
        "marketers, delivered as leads under contract. The public site is "
        "marketing plus a Pardot newsletter form; no lookup exists."
    ),
    "demystdata-com": (
        "Verified by browser render 2026-09-23: Demyst is a data- "
        "orchestration platform for banks and insurers -- it brokers "
        "access to third-party data sources inside a customer's "
        "underwriting workflow rather than publishing anything. No public "
        "lookup. Note the domain has moved: demystdata.com now serves "
        "demyst.com."
    ),
    "diablomedia-com": (
        "Verified by browser render 2026-09-23: Diablo Media is a "
        "performance-marketing and lead-generation network; its consumer "
        "touchpoint is a mailing list, not a directory. The site offers "
        "no lookup of any kind."
    ),
    "dice-com": (
        "Verified by browser render 2026-09-23: Dice is a technology job "
        "board. Candidate profiles are visible to paying employers behind "
        "a recruiter login, and no public page takes a name and returns a "
        "person. Same shape as careerbuilder-com. Note the corporate "
        "parent named on its request endpoint, DHI Group, which may "
        "appear as its own row."
    ),
    "dmsunsub-io": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. dmsunsub.io is a single-purpose "
        "privacy request host for Digital Media Solutions -- it serves a "
        "OneTrust DSAR webform and nothing else, with no product behind "
        "it. The search leg exists to describe where a person can look "
        "themselves up in a broker's product; this broker sells to "
        "businesses and exposes no such surface, so the leg is closed "
        "rather than left open for a recipe that could never be written."
    ),
    "digitalsegment-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. digitalsegment.com markets database- "
        "marketing services to brands; its only input box is a WordPress "
        "site search. The search leg exists to describe where a person "
        "can look themselves up in a broker's product; this broker sells "
        "to businesses and exposes no such surface, so the leg is closed "
        "rather than left open for a recipe that could never be written."
    ),
    "digitalvikingmedia-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. digitalvikingmedia.com is a five-page "
        "agency brochure site (Home, About Us, Our Team, Contact, Privacy "
        "Policy) with no inputs of any kind. The search leg exists to "
        "describe where a person can look themselves up in a broker's "
        "product; this broker sells to businesses and exposes no such "
        "surface, so the leg is closed rather than left open for a recipe "
        "that could never be written."
    ),
    "teamdms-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. teamdms.com sells direct-marketing "
        "services; its only form is the opt-out form itself. The search "
        "leg exists to describe where a person can look themselves up in "
        "a broker's product; this broker sells to businesses and exposes "
        "no such surface, so the leg is closed rather than left open for "
        "a recipe that could never be written."
    ),
    "disconetwork-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. Disco is a transactional-advertising "
        "network reached through a Zendesk help centre; the only search "
        "box indexes help articles. The search leg exists to describe "
        "where a person can look themselves up in a broker's product; "
        "this broker sells to businesses and exposes no such surface, so "
        "the leg is closed rather than left open for a recipe that could "
        "never be written."
    ),
    "dpcoptout-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. dpcoptout.com exists solely to host "
        "Distribution Processing Center's do-not-mail form; the site has "
        "no lookup of any kind. The search leg exists to describe where a "
        "person can look themselves up in a broker's product; this broker "
        "sells to businesses and exposes no such surface, so the leg is "
        "closed rather than left open for a recipe that could never be "
        "written."
    ),
    "dresdendirect-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. dresdendirect.com is a direct-mail "
        "agency site whose only forms are a post search and an 'Ask a "
        "Question' contact form. The search leg exists to describe where "
        "a person can look themselves up in a broker's product; this "
        "broker sells to businesses and exposes no such surface, so the "
        "leg is closed rather than left open for a recipe that could "
        "never be written."
    ),
    "driveniq-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. driveniq.com redirects to visitiq.io, "
        "an identity-resolution product sold to advertisers, and never "
        "reaches a page with a lookup. The search leg exists to describe "
        "where a person can look themselves up in a broker's product; "
        "this broker sells to businesses and exposes no such surface, so "
        "the leg is closed rather than left open for a recipe that could "
        "never be written."
    ),
    "drobu-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. drobu.com is a lead-generation agency "
        "brochure site with no forms at all on the homepage. The search "
        "leg exists to describe where a person can look themselves up in "
        "a broker's product; this broker sells to businesses and exposes "
        "no such surface, so the leg is closed rather than left open for "
        "a recipe that could never be written."
    ),
    "winwithoptimal-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. winwithoptimal.com is a political- "
        "advertising agency site; it carries no lookup, and its privacy "
        "policy now redirects to onemagnify.com. The search leg exists to "
        "describe where a person can look themselves up in a broker's "
        "product; this broker sells to businesses and exposes no such "
        "surface, so the leg is closed rather than left open for a recipe "
        "that could never be written."
    ),
    "dstillery-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. Dstillery sells AI audience targeting "
        "to advertisers; its only input is the WordPress site search. The "
        "search leg exists to describe where a person can look themselves "
        "up in a broker's product; this broker sells to businesses and "
        "exposes no such surface, so the leg is closed rather than left "
        "open for a recipe that could never be written."
    ),
    "thedatatrust-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. The Data Trust is a political data "
        "co-operative that sells to campaigns; no public lookup is "
        "offered. The search leg exists to describe where a person can "
        "look themselves up in a broker's product; this broker sells to "
        "businesses and exposes no such surface, so the leg is closed "
        "rather than left open for a recipe that could never be written."
    ),
    "dtn-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. DTN sells agriculture, energy and "
        "weather analytics to businesses; its inputs are a site search "
        "and the rights-request form. The search leg exists to describe "
        "where a person can look themselves up in a broker's product; "
        "this broker sells to businesses and exposes no such surface, so "
        "the leg is closed rather than left open for a recipe that could "
        "never be written."
    ),
    "dynata-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. Dynata is a survey-panel operator; a "
        "person meets it as a respondent, not through a lookup, and the "
        "site offers none. The search leg exists to describe where a "
        "person can look themselves up in a broker's product; this broker "
        "sells to businesses and exposes no such surface, so the leg is "
        "closed rather than left open for a recipe that could never be "
        "written."
    ),
    "earlywarning-com": (
        "Verified 2026-09-23 by rendering the site: there is no consumer- "
        "facing lookup here at all. Early Warning Services runs bank- "
        "industry reporting products; consumers reach their own file "
        "through a mailed disclosure request, not a lookup, and the only "
        "input is a site search. The search leg exists to describe where "
        "a person can look themselves up in a broker's product; this "
        "broker sells to businesses and exposes no such surface, so the "
        "leg is closed rather than left open for a recipe that could "
        "never be written."
    ),
    "lightcast-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Lightcast sells labour-market analytics to "
        "employers, colleges and government; its public pages carry a "
        "HubSpot newsletter box and nothing else. The leg is closed "
        "rather than left open, because a search recipe could never be "
        "written against a surface the broker does not offer."
    ),
    "edvisors-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Edvisors is a student-finance content site "
        "funded by lead generation -- its forms collect emails for FAFSA "
        "guides, and its only lookup is a site search. The leg is closed "
        "rather than left open, because a search recipe could never be "
        "written against a surface the broker does not offer."
    ),
    "socialgist-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Socialgist sells bulk social-conversation "
        "feeds to AI and intelligence products; socialgist.com now "
        "redirects to socialgist.ai, which carries no form at all. The "
        "leg is closed rather than left open, because a search recipe "
        "could never be written against a surface the broker does not "
        "offer."
    ),
    "worldpay-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Worldpay is a payments processor; its site "
        "has no inputs beyond navigation, and the consumer-facing "
        "reporting product behind this dataset row (ChexSystems) lives on "
        "a different domain entirely. The leg is closed rather than left "
        "open, because a search recipe could never be written against a "
        "surface the broker does not offer."
    ),
    "mastercard-us": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. The recorded URL is Mastercard's data- "
        "subject request portal for Ekata, not a product; it returns HTTP "
        "403 and there is no lookup on it or behind it. The leg is closed "
        "rather than left open, because a search recipe could never be "
        "written against a surface the broker does not offer."
    ),
    "electroniccommerceatoz-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. electroniccommerceatoz.com does not resolve "
        "in DNS, so neither leg has a surface to describe; see the opt- "
        "out entry for the recheck that is owed. The leg is closed rather "
        "than left open, because a search recipe could never be written "
        "against a surface the broker does not offer."
    ),
    "evs7-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. EVS7 sells auto-dialer and call-centre "
        "software; its only forms are a sales contact form and the "
        "privacy request form itself. The leg is closed rather than left "
        "open, because a search recipe could never be written against a "
        "surface the broker does not offer."
    ),
    "eltoro-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. El Toro sells IP-targeted advertising to "
        "marketers; the only form on its privacy pages is an embedded "
        "OneTrust request portal. The leg is closed rather than left "
        "open, because a search recipe could never be written against a "
        "surface the broker does not offer."
    ),
    "emailmovers-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Emailmovers is a UK B2B list vendor selling "
        "to marketers; its only form is a sales enquiry box asking for "
        "company and requirements. The leg is closed rather than left "
        "open, because a search recipe could never be written against a "
        "surface the broker does not offer."
    ),
    "emerges-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. eMerges has ceased operating as a list "
        "broker entirely -- see the opt-out entry -- and its remaining "
        "site is four brochure pages with no inputs. The leg is closed "
        "rather than left open, because a search recipe could never be "
        "written against a surface the broker does not offer."
    ),
    "enformion-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Enformion sells people-search data to "
        "businesses through Tracers.com and Endato.com, all behind a "
        "login; the public site offers a demo request, not a lookup. The "
        "leg is closed rather than left open, because a search recipe "
        "could never be written against a surface the broker does not "
        "offer."
    ),
    "enigma-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Enigma sells business (not consumer) data "
        "through an API; its public pages carry no lookup and its only "
        "form is the do-not-sell request that ships as a staged recipe. "
        "The leg is closed rather than left open, because a search recipe "
        "could never be written against a surface the broker does not "
        "offer."
    ),
    "eprodirect-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. EproDirect sells event and meeting-planner "
        "mailing lists to marketers; its only form is the CCPA request "
        "form. The leg is closed rather than left open, because a search "
        "recipe could never be written against a surface the broker does "
        "not offer."
    ),
    "propstream-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. PropStream is a subscription real-estate "
        "research tool -- the lookup it does have is behind a paid login "
        "and is keyed to PROPERTIES, not to a person looking themselves "
        "up. The leg is closed rather than left open, because a search "
        "recipe could never be written against a surface the broker does "
        "not offer."
    ),
    "force-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. The dataset row is e.Republic, a government- "
        "media publisher, keyed to a Salesforce hosting domain; neither "
        "erepublic.com nor the Salesforce site offers any lookup. The leg "
        "is closed rather than left open, because a search recipe could "
        "never be written against a surface the broker does not offer."
    ),
    "evorra-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Evorra sells audience segments to "
        "advertisers through a platform login; its public pages carry a "
        "newsletter box and a demo request. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "clarityservices-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Clarity is an FCRA credit bureau selling to "
        "lenders -- a consumer reaches their own file by requesting a "
        "report, not by searching, and the only input on the site is a "
        "site search. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "explorium-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Explorium sells external data to data- "
        "science teams via API; its site is behind a Cloudflare challenge "
        "and offers no lookup in any case. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "vdx-tv": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. VDX.TV sells connected-TV advertising; its "
        "only form is a cookie-preferences panel. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "eyeota-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Eyeota sells audience data to advertisers; "
        "the only input on its site is a site search over its own pages. "
        "The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "factori-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Factori sells location and mobility data to "
        "businesses; its consumer-facing page is a request form, not a "
        "lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "fairscreen-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fair Screen is an FCRA background screener "
        "selling to employers -- see the opt-out entry; its site is "
        "login-gated and offers no public lookup. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "faraday-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Faraday sells predictive-audience modelling "
        "to marketers; nothing on the site could be read at all, but no "
        "lookup is advertised anywhere on it. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "fideo-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fideo sells identity and fraud signals to "
        "businesses; its consumer-facing app is a rights wizard, not a "
        "lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "fifty-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fifty sells audience segmentation to "
        "agencies; its only form is a demo request. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "findem-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Findem sells talent-sourcing data to "
        "recruiters, behind a login; its public pages offer a demo "
        "request and the do-not-sell form. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "firstam-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. First American is a title insurer and "
        "property-data provider; consumer-facing search is a site search "
        "over its own content. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "firstdirectmarketing-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. First Direct sells direct-marketing lists to "
        "businesses; its compliance subdomain is a governance portal with "
        "no lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "fmadata-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FMAdata sells sales leads and marketing data "
        "to businesses; its only form is the opt-out request. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "firstorion-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. First Orion supplies caller-identification "
        "data to carriers and businesses; its privacy subdomain is a "
        "request wizard, not a lookup. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "flashintel-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FlashIntel (now trading as FlashLabs) sells "
        "B2B contact enrichment behind a login; its public pages offer a "
        "demo and the request form. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "focus-usa-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Focus USA sells targeted mailing lists to "
        "marketers; its only forms are the privacy request and a docs "
        "feedback box. The leg is closed because a search recipe cannot "
        "be written against a surface the broker does not offer."
    ),
    "instantly-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Instantly sells cold-email tooling to sales "
        "teams; its privacy content is an Intercom help centre that "
        "requires sign-in. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "forager-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Forager sells B2B contact data by API and "
        "platform login; its public site offers sign-in and the removal "
        "form. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "forewarn-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FOREWARN sells pre-meeting risk checks to "
        "real-estate agents, behind a login; there is no public lookup. "
        "The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "fourleafdata-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FourLeaf runs marketing services for "
        "clients; its site is a contact page and a privacy policy, with "
        "no inputs at all. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "foursquare-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Foursquare's consumer app searches PLACES, "
        "not people -- the header search takes a query and a location, "
        "and no person-lookup exists. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "fourthwall-tv": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fourthwall Media sells television-audience "
        "measurement to advertisers; its only forms are the opt-out "
        "request and a newsletter box. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "fraiser-org": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fraiser sells grassroots fundraising tooling "
        "to campaigns and non-profits, behind a login; no public lookup "
        "is offered. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "reachdata-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. reachdata.com has not launched -- the site "
        "reads 'Coming Soon!!' and carries no forms; see the opt-out "
        "entry. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "freewheel-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FreeWheel sells video-advertising "
        "infrastructure to publishers; its only inputs are a site search "
        "and the opt-out form. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "fullcontact-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FullContact sells identity resolution by "
        "API; its consumer-facing page is a rights wizard, not a lookup. "
        "The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "fusedleads-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. fusedleads.com serves an expired certificate "
        "and could not be read at all; no lookup is advertised anywhere "
        "associated with it. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "fushiamedia-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Fuchsia Media sells database enrichment to "
        "marketers; its only form is a three-page site's contact box. The "
        "leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "g2risksolutions-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. G2 Risk Solutions sells bankruptcy and "
        "merchant-risk data to lenders and acquirers; its consumer page "
        "is informational text with no inputs. The leg is closed because "
        "a search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "retention-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Retention.com sells website-visitor "
        "identification to marketers; its only public form is the opt- "
        "out. The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "giantpartners-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Giant Partners is a data-driven marketing "
        "agency selling list services to businesses; its only public form "
        "is the opt-out. The leg is closed because a search recipe cannot "
        "be written against a surface the broker does not offer."
    ),
    "parade-pet": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Pet Parade is a consumer photo-contest game; "
        "its forms are signup, login and phone/email code verification, "
        "none of which look anyone up. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "grassrootsanalytics-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Grassroots Analytics sells donor data to "
        "political campaigns behind a sales process; the CCPA page's form "
        "is a rights request, not a lookup. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "grata-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Grata sells private-company search to "
        "dealmakers -- its product searches COMPANIES, not people, and "
        "requires a paid login. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "grayhairsoftware-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. GrayHair Software sells mail-tracking "
        "analytics to mailers; its site carries no inputs beyond a cookie "
        "banner. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "greatlakeslists-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Great Lakes List Management rents marketing "
        "lists to businesses; its site browses LIST CATEGORIES, not "
        "individuals. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "grin-co": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. GRIN sells influencer-marketing software; "
        "its public pages are behind a Cloudflare interstitial and offer "
        "no lookup in any case. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "forms-gle": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. The row points at a Google Form, which is a "
        "submission surface only; see the opt-out entry for what that "
        "form actually belongs to. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "carneydirect-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Carney Direct does database management for "
        "mailers; the only input on its site is a WordPress site search. "
        "The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "gundir-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Gundir is a direct-mail agency; the only "
        "input on its site is a WordPress site search. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "h1-co": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. H1 sells healthcare-provider intelligence to "
        "life-sciences firms behind a demo request and login; no public "
        "lookup is offered. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "healthcare-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. healthcare.com compares insurance plans, not "
        "people; its quoting flow takes a ZIP to price plans and returns "
        "no records about a person. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "healthlinkdimensions-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. HealthLink Dimensions sells healthcare- "
        "professional contact data to marketers; its consumer page is "
        "prose with no inputs at all. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "granitelists-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. granitelists.com is serving 'Account "
        "Suspended' and offers nothing at all; see the opt-out entry. The "
        "leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "harmonresearch-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Harmon Research runs survey panels and focus "
        "groups for clients; its site is a brochure with a contact form "
        "and a panel-book request. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "pickmedicare-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. PickMedicare is a Medicare lead-generation "
        "landing page whose entire call to action is a phone number; it "
        "has no lookup and in fact no form. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "healthwisedata-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. HealthWise Data sells healthcare audience "
        "data to marketers behind a demo request; its only form is the "
        "privacy request. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "heartbeat-ai": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Heartbeat sells contact enrichment to "
        "recruiters behind a login, and its public pages sit behind a "
        "Cloudflare bot check in any case. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "helixcampaigns-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Helix Campaigns sells political audience "
        "targeting to campaigns; its only public form is the rights "
        "request. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "here-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. HERE sells mapping and location services; "
        "its products search PLACES and routes, never people. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "ip2location-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. IP2Location sells IP geolocation databases "
        "-- its lookups resolve IP ADDRESSES to coarse locations, and "
        "return no person. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "hireez-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. hireez sells recruiting search over "
        "candidate profiles, but only to paying recruiters behind a "
        "login; nothing is public. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "hivestack-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Hivestack sells programmatic out-of-home "
        "advertising to media buyers; its site carries no lookup of any "
        "kind. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "fivestarrated-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. FiveStarRated is a home-services directory: "
        "its ZIP and keyword boxes search CONTRACTORS AND BUSINESSES, not "
        "individuals. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "homeownersmarketingservices-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Homeowners Marketing Services rents "
        "homeowner mailing lists to marketers; its only form is the "
        "removal request. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "exploreatlas-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. exploreatlas.io serves 'This page couldn't "
        "be found' and offers nothing; see the opt-out entry. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "hunter-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. Hunter's email-finder searches by COMPANY "
        "DOMAIN and is gated behind an account; its public page is the "
        "claim/removal form only. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "id5-io": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. ID5 operates an advertising identity graph "
        "consumed by ad tech, not by people; there is no lookup surface "
        "and nothing to query. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "idengine-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. idengine.com is a parked domain listed for "
        "sale; see the opt-out entry. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "idm-us-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. IDM sells marketing data and analytics to "
        "advisers; its only public form is the do-not-sell request. The "
        "leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "ileads-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. iLeads sells mortgage and insurance leads to "
        "lenders; its only public form is the CCPA request. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "acuityads-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. AcuityAds -- now trading as illumin -- sells "
        "programmatic advertising to brands; there is no consumer lookup "
        "anywhere on it. The leg is closed because a search recipe cannot "
        "be written against a surface the broker does not offer."
    ),
    "gumgum-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. GumGum sells contextual advertising to "
        "brands; the only input on its site is a site search over its own "
        "marketing pages. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "backgroundchecks-com": (
        "Verified 2026-09-23 by rendering the site. No consumer-facing "
        "lookup exists here. backgroundchecks.com sells screening reports "
        "to employers behind a credentialed account; the consumer routes "
        "it offers are a file disclosure and a dispute, not a lookup. The "
        "leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "littlebrookmedia-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. Little Brook Media (Big Brook Media, LLC) is "
        "a direct-marketing and lead-generation firm -- co-registration "
        "leads, call-transfer programs -- and the only input on any page "
        "rendered is the footer opt-out form itself (see the optout_forms "
        "entry). There is no people search and no site search. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "oncoreleads-com": (
        "Verified 2026-09-25 by rendering the homepage, /california- "
        "privacy-rights/, /do_not_sell/ and /ccparequest/. No consumer- "
        "facing lookup exists here. OnCore Leads (CO2 Ventures, LLC) "
        "sells lead generation to businesses; not one of those four pages "
        "carries a single form control of any kind, let alone a person "
        "lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "persistent-id": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. Persistent.id sells an identity graph to "
        "ecommerce brands -- its own homepage advertises '275M+ US "
        "Consumers', '4B+ Emails' and '5B+ Web and Intent Signals' as "
        "inventory -- and the rendered page carries no form at all; the "
        "only inputs on the domain are in the embedded rights portal "
        "described in the optout_forms entry. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "crexi-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "PERSON lookup exists here, which is the distinction that matters "
        "for this row. Crexi is a commercial real-estate marketplace and "
        "it does carry a search -- #filter-location-input, 'Enter a "
        "location or keywords', beside a 'Search' button -- but it "
        "searches PROPERTY LISTINGS by location, not people. The 'Comps & "
        "Records' product in its navigation is the only plausible "
        "candidate for records about named individuals and it sits behind "
        "'Sign in' (the guessed path /comps-and-records returns the "
        "site's own 404). Same category as costar-com, minus the Akamai "
        "wall. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "deepsync-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. Deep Sync sells identity resolution and "
        "audience activation to marketers; the rendered homepage has zero "
        "forms and the only search-shaped control is an icon button "
        "labelled 'Search' that opens a site search over its own "
        "marketing pages. Its consumer-facing surface is the privacy "
        "portal on privacy.deepsync.com, which is a request form, not a "
        "lookup. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "creditsafe-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "PERSON lookup exists here. Creditsafe sells business credit and "
        "risk intelligence -- 'fresh company and contact data on more "
        "than 66 million companies in 13 countries' in its own words -- "
        "and the lookup machinery on the homepage is company-shaped: "
        "hidden #companyClaimForm and #business-index inputs, no person "
        "search anywhere. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "crif-com": (
        "Verified 2026-09-25 by rendering the homepage and /privacy- "
        "policy/. No consumer-facing person lookup exists here. CRIF SpA "
        "is a pan-EU credit-bureau and business-information group; the "
        "single form on either page is #js-fv-quicksearch, an "
        "input[name='q'] GETting to /search-results/ -- a site search "
        "over CRIF's own marketing pages, which this module does not "
        "count as a people search (same call as gumgum-com). The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "criteo-com": (
        "Verified 2026-09-25 by rendering the homepage and /privacy/ccpa- "
        "privacy-policy/. No consumer-facing lookup exists here. Criteo "
        "is a retargeting and commerce-media ad-tech company; the only "
        "forms on its homepage are #searchform and #searchformMobile, "
        "both input[name='s'] site searches over criteo.com itself. The "
        "leg is closed because a search recipe cannot be written against "
        "a surface the broker does not offer."
    ),
    "dataaxle-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here, despite Data Axle being one of the larger "
        "compilers in this dataset. The only forms on the homepage are "
        "two copies of a WordPress is-search-form (input[name='s']) over "
        "data-axle.com's own pages, plus a HubSpot 'How can we help?' "
        "sales form requiring a Company name. Its consumer surface is the "
        "rights request form recorded under optout_forms. NOTE for the "
        "dataset: dataaxle.com serves on www.data-axle.com -- the "
        "hyphenated host -- which is where every live URL for this row "
        "lives. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "forddirect-com": (
        "Verified 2026-09-25 by rendering the homepage and /privacy. No "
        "consumer-facing lookup exists here. FordDirect is a joint "
        "venture selling marketing and inventory tooling to Ford dealers "
        "and Lincoln retailers; neither page carries a single input -- "
        "the only controls on the homepage are a nav toggle and two "
        "carousel arrows. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "dealersocket-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. DealerSocket (now a Solera company) sells "
        "dealership CRM and management software; the rendered homepage "
        "has no forms, only navigation, carousel and OneTrust consent "
        "controls. The leg is closed because a search recipe cannot be "
        "written against a surface the broker does not offer."
    ),
    "dealerx-com": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. DealerX sells identity-resolution and "
        "marketing automation to dealerships and OEMs; the homepage "
        "renders no forms at all (its only controls are nav toggles and "
        "its home-grown cookie dialog). The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "pii-ai": (
        "Verified 2026-09-25 by rendering the site. No consumer-facing "
        "lookup exists here. pii.ai serves PieEye, a privacy-compliance "
        "SaaS selling cookie consent and DSAR handling to ecommerce "
        "brands; the forms on its homepage are a 'Send Message' contact "
        "form (with an invisible input[name='phone_number'] honeypot) and "
        "its own cookie widget. The broker listed on this row is "
        "Delivr.ai, whose data is reached only through the request portal "
        "recorded under optout_forms. The leg is closed because a search "
        "recipe cannot be written against a surface the broker does not "
        "offer."
    ),
    "deloitte-com": (
        "Verified 2026-09-25 by rendering deloitte.com/us/en.html and the "
        "Data Analytics Privacy Notice this row records. No consumer- "
        "facing lookup exists here. Deloitte Consulting is a "
        "professional-services firm; the notice covers its "
        "PredictRisk/data-analytics products, which are sold to clients "
        "rather than queried by the public, and the only input anywhere "
        "on either page is the site's own #search-button article search "
        "plus a 'Type location' picker. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "demyst-com": (
        "Verified 2026-09-25 by rendering demyst.com/privacy-policy and "
        "both of its request pages. Same finding as the demystdata-com "
        "row, which now serves from this same domain: Demyst is a data- "
        "orchestration platform that brokers third-party data inside a "
        "customer's underwriting workflow rather than publishing "
        "anything, and no page carries a lookup. The leg is closed "
        "because a search recipe cannot be written against a surface the "
        "broker does not offer."
    ),
    "optout-aboutads-info": (
        "Verified 2026-09-25 by rendering https://optout.aboutads.info/. "
        "No lookup of any kind. This row is not a broker but the Digital "
        "Advertising Alliance's WebChoices browser tool: it enumerates 99 "
        "participating ad companies and reports a per-company cookie "
        "status for the browser that is visiting. There is nothing to "
        "search and nobody to be found. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "disqus-com": (
        "Verified 2026-09-25 by rendering disqus.com and its /data- "
        "sharing-settings/ page. No consumer-facing lookup exists here. "
        "Disqus sells a hosted comment and audience-engagement widget to "
        "publishers; the homepage carries no inputs at all beyond its "
        "cookie banner and a nav dropdown, and anything about a specific "
        "commenter sits behind the publisher or commenter login. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "dmachoice-org": (
        "Verified 2026-09-25 by rendering dmachoice.org and its "
        "registration page. No lookup exists. DMAchoice is the ANA's "
        "mail-preference service: the homepage is a #memberForm login "
        "(email and password, action /login.php) and everything else is "
        "the paid registration flow. It holds a suppression list, not "
        "searchable profiles, and it will not tell a visitor what is on "
        "it. The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "donotcall-gov": (
        "Verified 2026-09-25 by rendering donotcall.gov and "
        "/register.html. No lookup of held data exists. This row is the "
        "FTC's National Do Not Call Registry, a suppression list rather "
        "than a broker: the only lookup on the site is 'verify "
        "registration', which takes a phone number and answers whether "
        "that number is on the Registry -- it returns no record, no name "
        "and no address, so there is nothing for a search recipe to read "
        "out. The leg is closed because a search recipe cannot be written "
        "against a surface the broker does not offer."
    ),
    "domaintools-com": (
        "Verified 2026-09-25 by rendering domaintools.com and "
        "whois.domaintools.com in full. There is a search box, and it is "
        "not a people search: #whois-landing-form takes a single "
        "input[name='q'] labelled 'Enter a domain or IP address...' and "
        "GETs /go/, i.e. the unit of lookup is a domain or an IP, never a "
        "person. The direction this row would actually need -- reverse "
        "WHOIS by registrant name -- is a paid Iris feature, and the only "
        "routes to it on the rendered page are the LOGIN and SIGN UP "
        "buttons. No captcha on the WHOIS landing page itself, though the "
        "marketing homepage does load "
        "challenges.cloudflare.com/turnstile/v0/api.js. The leg is closed "
        "because the broker offers no lookup that takes a person as "
        "input; the dataset's own note is worth keeping, that DomainTools "
        "only redisplays third-party WHOIS data and cannot unpublish a "
        "domain's historical WHOIS records."
    ),
    "dnb-com": (
        "Verified 2026-09-25 by rendering dnb.com/en-us/ and "
        "dnb.com/business-directory.html. The public directory exists and "
        "it is a COMPANY lookup: its input is labelled 'Search by company "
        "name or D-U-N-S Number' and the browse axis below it is NAICS "
        "industry, so a person cannot be looked up by name. Everything "
        "person-shaped is behind 'Log In' or in D&B Hoovers. Worth "
        "recording for anyone who revisits: the business-directory page "
        "carries reCAPTCHA ENTERPRISE (enterprise.js with a grecaptcha- "
        "badge) on an Eloqua/Marketo #eloquaForm lead capture, so even "
        "the marketing furniture on this host is challenged. The leg is "
        "closed because the broker offers no lookup that takes a person "
        "as input."
    ),
    "netwisedata-com": (
        "Verified 2026-09-25 by rendering netwisedata.com. The brand is "
        "gone: www.netwisedata.com/consumer-privacy 404s with and without "
        "a trailing slash, and the host now serves D&B pages -- the apex "
        "landed on https://www.dnb.com/en-us/products/dnb-id-graph- "
        "plus.html, the identity-graph product that absorbed NetWise. "
        "There is no consumer lookup on either the old domain or the page "
        "it resolves to; see the dnb-com row for the company-only "
        "Business Directory. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "trustarc-eu": (
        "Verified 2026-09-25 by rendering the URL this row records. There "
        "is no lookup, and there is no broker site here either: submit- "
        "irm.trustarc.eu is TrustArc's hosted data-subject-request "
        "service, and this row's URL is one client's form on it -- Dun & "
        "Bradstreet's, reached from both dnb.com and netwisedata.com. The "
        "searchable surface question belongs to the dnb-com row. The leg "
        "is closed because a search recipe cannot be written against a "
        "vendor form host."
    ),
    "erepublic-com": (
        "Verified 2026-09-25 by rendering erepublic.com/privacy/ and "
        "following its rights route. No consumer-facing lookup exists "
        "here. e.Republic is a government-market media and research "
        "company (Governing, Government Technology); the privacy page's "
        "only controls are its OneTrust consent dialog, and nothing on "
        "the site invites a visitor to look a person up. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "socialgist-ai": (
        "Verified 2026-09-25 by rendering socialgist.ai and its /privacy- "
        "and-terms in full. No consumer-facing lookup exists here. Effyis "
        "d/b/a Socialgist licenses firehose access to social and forum "
        "content to enterprise buyers; the homepage carries no inputs at "
        "all beyond a nav toggle and its own accept/decline cookie "
        "buttons. Note for the opt-out leg: the policy is explicit that "
        "what Socialgist holds about a person is whatever appears in "
        "collected Content, keyed to handles and usernames rather than to "
        "a name and address. The leg is closed because a search recipe "
        "cannot be written against a surface the broker does not offer."
    ),
    "telephonelists-biz": (
        "Verified 2026-09-25 by rendering telephonelists.biz. There is a "
        "live data surface on the homepage and it is not a person lookup: "
        "a 'LEADS PORTAL: LIVE DEMO' panel of list-building filters -- "
        "country USA/Canada, list type Consumer/Business, a STATE select, "
        "a homeowners checkbox, a monthly-plan-size range slider and '+ "
        "See all 40 additional filters' -- whose output is a COUNT of "
        "matching records for purchase, with the records themselves "
        "behind a paid plan. It takes demographic criteria as input, "
        "never a name, so there is nothing a search recipe could ask it "
        "about one person. Electronic Voice Services sells 189 million US "
        "and Canadian consumer and business phone records this way. The "
        "leg is closed because the broker offers no lookup that takes a "
        "person as input."
    ),
    "endgame-io": (
        "Verified 2026-09-25 by rendering endgame.io and /privacy in "
        "full. No consumer-facing lookup exists here. Endgame Labs sells "
        "AI revenue-intelligence and deal-preparation tooling to sales "
        "teams; both pages render zero forms -- every control is a nav "
        "dropdown, a feature tab or a case-study card -- and the product "
        "itself sits behind 'Go to app'. Its own policy says it processes "
        "contact data as a service provider on its customers' "
        "instructions, which is why the consumer-facing side of this "
        "broker is a mailbox and not a page. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "equativ-com": (
        "Verified 2026-09-25 by rendering privacy.equativ.com/user- "
        "rights-hub/ and its linked request pages. No consumer-facing "
        "lookup exists here. Equativ (formerly Smart AdServer) is a "
        "French ad-serving and supply-side platform whose consumer-facing "
        "estate is a privacy centre and a cookie-sync endpoint; there is "
        "nothing to query about a person. The leg is closed because a "
        "search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "etarget-sk": (
        "Verified 2026-09-25 by rendering etarget.sk and its privacy "
        "policy. No lookup, and almost no site: etarget.sk redirects to "
        "www.etarget.eu/sk/home/, which renders ZERO controls of any "
        "kind, and etarget.sk/privacy.php redirects to "
        "sk.search.etargetnet.com/policy.html, which is equally inert. "
        "ETARGET SE is a Slovak ad network and registered IAB Europe CMP; "
        "its only person-shaped surface is the advertiser login. The leg "
        "is closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "exactcustomer-com": (
        "Verified 2026-09-25 by rendering exactcustomer.com. No consumer- "
        "facing lookup exists here. EXACT OPCO sells performance lead "
        "generation ('we don't just sell leads... we put sales on the "
        "board'); the two forms on the homepage are WordPress Contact "
        "Form 7 instances -- a Name/E-mail/Phone contact form and a one- "
        "field email capture -- both carrying a Cloudflare Turnstile "
        "widget, and neither queries anything. The leg is closed because "
        "a search recipe cannot be written against a surface the broker "
        "does not offer."
    ),
    "remodelyourhome-com": (
        "Verified 2026-09-25 by rendering remodelyourhome.com. No "
        "consumer-facing lookup exists here. Exact Opco's RemodelYourHome "
        "is a home-improvement lead funnel: every control on the page is "
        "a quote call-to-action ('Start a Quote', 'GET AN ESTIMATE', "
        "'REQUEST A QUOTE' into /find-local-pros.html), which collects a "
        "homeowner's details rather than returning anyone's. The leg is "
        "closed because a search recipe cannot be written against a "
        "surface the broker does not offer."
    ),
    "faraday-ai": (
        "Verified 2026-09-25 by rendering https://faraday.ai/ (200, read "
        "in full). Same company as the already-mapped faraday-io row and "
        "the same verdict: it is a B2B predictive-modelling vendor -- "
        "'Faraday provides context on 240 million U.S. adults via MCP, "
        "real-time API, and batch deployment' -- and the only interactive "
        "controls on the page are nav menus, a docs search and a 'Build "
        "predictive model' demo widget. No consumer lookup is offered "
        "anywhere on it, so there is no surface a search recipe could "
        "target."
    ),
    "finthrive-com": (
        "Verified 2026-09-25 by rendering https://finthrive.com/privacy- "
        "policy (200, read in full). FinThrive sells revenue-cycle- "
        "management software to hospitals; the only forms on the page are "
        "two copies of the site-header site-search widget and the "
        "OneTrust cookie preference centre. There is no people lookup, "
        "and nothing on the site advertises one, so this leg is closed "
        "for want of a surface rather than for want of research."
    ),
    "fadv-com": (
        "Verified 2026-09-25 by rendering https://fadv.com/ (200). First "
        "Advantage is an employment background-screening CRA: the site "
        "sells screening to employers and routes individuals to "
        "'Candidates' and 'Check Status', which are per-order status "
        "lookups behind a candidate's own order id, not a public person "
        "search. The only forms on the home page are two copies of the "
        "site-search overlay posting to /search/. No consumer-facing name "
        "lookup exists to write a recipe against."
    ),
    "geniussports-com": (
        "Verified 2026-09-25 by rendering https://www.geniussports.com/ "
        "and its privacy policy (both 200, read in full). Genius Sports "
        "sells sports data, streaming and betting technology to leagues, "
        "sportsbooks and advertisers; the home page's only controls are "
        "nav menus and a carousel. No person lookup is offered or "
        "advertised, so there is no surface for a search recipe."
    ),
    "vector-co": (
        "Verified 2026-09-25 by rendering "
        "https://www.vector.co/legal/privacy and "
        "https://www.vector.co/opt-out (both 200, read in full). "
        "GetVector sells an account-based-marketing platform to B2B "
        "sellers -- 'The ABM platform that shows its work' -- and the "
        "only forms anywhere on those pages are a newsletter subscribe "
        "box and the consumer privacy-request form. No consumer-facing "
        "lookup exists here."
    ),
    "gravyanalytics-com": (
        "Verified 2026-09-25 by render, and the domain has MOVED: "
        "https://gravyanalytics.com/ now redirects to "
        "https://www.unacast.com/ (Gravy Analytics having been folded "
        "into Unacast). Unacast sells enterprise location intelligence; "
        "the only forms on the landing page are two HubSpot lead-capture "
        "forms ('Book a Meeting', 'Submit') and a cookie-preferences box. "
        "Location-data brokers of this kind key their records to mobile "
        "advertising identifiers rather than to names, and nothing on "
        "either domain offers a person lookup, so there is no search "
        "surface to recipe."
    ),
    "graze-social": (
        "Verified 2026-09-25 by rendering https://www.graze.social/ and "
        "its privacy policy (both 200, read in full). Graze is a custom- "
        "feed builder for Bluesky/ATProto -- it processes public Bluesky "
        "content on behalf of feed curators -- and the site offers feed "
        "building and a marketplace, not a person lookup. No search "
        "surface exists to write a recipe against."
    ),
    "growbots-com": (
        "Verified 2026-09-25 by rendering https://www.growbots.com/ "
        "(200). Growbots sells outbound-sales lead generation as a done- "
        "for-you service to B2B sellers; the site's calls to action are "
        "'TALK TO US' and 'BOOK A DEMO' and there is no public lookup of "
        "any kind. Its prospect database is reachable only from inside a "
        "paid account, which is not a surface this codebase can search."
    ),
    "growinglibraries-com": (
        "Verified 2026-09-25 by rendering https://growinglibraries.com/ "
        "(200). Growing Libraries sells a marketing platform to public "
        "libraries for reaching non-users; the home page carries no form "
        "at all beyond a cookie banner, and no consumer lookup is offered "
        "or advertised. The company's consumer-facing surface is the do- "
        "not-sell form only (see optout_forms)."
    ),
    "haines-com": (
        "Verified 2026-09-25 by rendering https://haines.com/ and its "
        "privacy policy (both 200, read in full). Haines publishes the "
        "Criss+Cross Directory and sells property and lead data to "
        "businesses -- 'Delivering Qualified Leads to Grow Your Business "
        "for Over 90 Years' -- with products (Property Connect, "
        "Criss+Cross PLUS) sold by subscription and reached through a "
        "customer login. Nothing on the public site is a consumer lookup, "
        "so this leg closes for want of a surface."
    ),
    "hartehanks-com": (
        "Verified 2026-09-25 by rendering https://www.hartehanks.com/ and "
        "the current privacy highlights page (both 200, read in full). "
        "Harte Hanks is a B2B marketing-services agency that compiles "
        "what it calls a Behavioral Index for clients; the only form on "
        "the site is the WordPress site-search box. It offers no "
        "consumer-facing lookup, so there is no surface a search recipe "
        "could target."
    ),
    "perion-com": (
        "Verified 2026-09-25 by browser render. perion.com is Perion "
        "Network's corporate adtech site (title 'AI-native execution "
        "infrastructure for modern media'): platform, channels, investors "
        "and careers pages, no consumer lookup of any kind, and the only "
        "input on the whole site is a Cookiebot consent dialog. This row "
        "is keyed to perion.com but NAMES Hivestack Inc.; Perion's own "
        "CCPA notice confirms the relationship ('HiveStack (Perion's "
        "Affiliate, a registered data broker)'), so the row is about the "
        "parent's domain rather than hivestack.com -- worth knowing "
        "before anyone re-researches it. Perion's own notice also says it "
        "files people under Mobile Advertising IDs and cannot identify a "
        "person from a name or an email, which is the same reason there "
        "is nothing here to search."
    ),
    "homedata-com": (
        "Verified 2026-09-25. DATASET CLAIM FALSIFIED: the row says "
        "'homedata.com itself now serves generic deepsync.com content', "
        "and it serves nothing at all. homedata.com and www.homedata.com "
        "both RESOLVE (15.197.142.173, an AWS Global Accelerator address) "
        "but the TCP connection never completes: Playwright timed out at "
        "30s on domcontentloaded on both hostnames, and curl -L timed out "
        "at 25s with HTTP 000. Only privacy.homedata.com (54.185.236.12) "
        "answers, and it redirects to DeepSync's privacy portal. So there "
        "is no page under this domain to carry a lookup surface. "
        "Independently, DeepSync is a B2B identity-resolution and "
        "audience vendor that sells to marketers and offers no consumer- "
        "facing people search, so a working apex would not change this "
        "leg."
    ),
    "i-360-com": (
        "Verified 2026-09-25 by browser render. i-360.com is i360's "
        "political data product site (voter and consumer data, models, "
        "survey research, grassroots tools, all sold to campaigns). The "
        "only form on the page is the WordPress site search "
        "(input[name=s], GET to https://www.i-360.com/), and the only "
        "other controls are OneTrust consent-banner and vendor-list "
        "widgets. No consumer lookup of any kind, and the product is sold "
        "by subscription to campaigns rather than by per-record purchase "
        "to the public."
    ),
    "foundryco-com": (
        "Verified 2026-09-25 by browser render. foundryco.com is Foundry "
        "(formerly IDG Communications), a B2B tech-media and intent-data "
        "company: brands (CIO, Computerworld, InfoWorld, Macworld, "
        "PCWorld and the rest), audiences, research and events. The only "
        "forms are two copies of the site search "
        "(input[name=_search_facet], GET to /search-results/). No "
        "consumer lookup. The record it holds is a business contact -- "
        "its own privacy policy enumerates 'first name, last name, "
        "business email address and phone number, company name, business "
        "title, business address' -- which is not a surface a person can "
        "query."
    ),
    "mailinglists-com": (
        "Verified 2026-09-25 by browser render. mailinglists.com "
        "(Infinite Media Concepts) sells marketing lists to mailers; the "
        "site is product and pricing pages. Every form on it is a HubSpot "
        "blog-subscribe widget (email + Instant / Monthly radio, posting "
        "to forms-na2.hsforms.com portal 244059975), served once in an "
        "about:blank injected frame and once in an hs-sites-na2.com "
        "frame. There is no consumer lookup and no way for a person to "
        "ask what is held about them other than the do-not-sell request "
        "form recorded on the opt-out leg."
    ),
    "info": (
        "DATASET DEFECT, verified 2026-09-25, and the leg is closed "
        "because the row identifies no broker. The row's domain field is "
        "the literal string 'info.' -- a trailing-dot fragment, not a "
        "domain -- which slugifies to the meaningless broker_id 'info' "
        "and will collide with any other row whose domain is mangled the "
        "same way. Its name field is 'Info', described in its own notes "
        "as 'company name inferred from domain'. Its recorded opt_out_url "
        "is 'https://optout.aboutads.info.' which, with the stray dot "
        "removed, is the Digital Advertising Alliance's WebChoices tool "
        "-- an industry-wide browser cookie utility, not a broker's site. "
        "There is therefore no company here whose search surface could be "
        "found, and nothing to render beyond the DAA tool itself "
        "(verified live, HTTP 200, title 'WebChoices'). Flagged, not "
        "fixed: this row should be deleted or re-sourced rather than "
        "researched further."
    ),
    "informa-com": (
        "Verified 2026-09-25 by browser render. informa.com is Informa "
        "plc's FTSE-100 corporate site (divisions, investors, "
        "sustainability, Taylor & Francis). The only form is the site "
        "search (input#search-site name=q, GET to /search-results/); the "
        "only child frames are two Investis share-price ticker iframes "
        "carrying nothing but ASP.NET __VIEWSTATE hidden fields. No "
        "consumer lookup exists. Informa's holdings on a person are B2B "
        "event and publishing contact records, reachable only through the "
        "Transcend privacy centre recorded on the opt-out leg."
    ),
    "inmar-com": (
        "Verified 2026-09-25 by browser render. inmar.com is Inmar "
        "Intelligence's corporate site (martech, healthcare, promotions, "
        "settlement). Its three forms are all the same Drupal site search "
        "(input[name=keys], GET to /search/node); there is also a Pardot "
        "'contact us' frame at go.inmar.com asking First Name, Last Name, "
        "Company, Title, Email and Comments, all required -- a B2B sales "
        "enquiry, not a lookup. No consumer search surface. Inmar's "
        "consumer data comes from retailer promotion and loyalty "
        "programmes rather than from a queryable public index."
    ),
    "insurancemarketinghub-com": (
        "Verified 2026-09-25 by browser render. insurancemarketinghub.com "
        "sells exclusive life, health and Medicare leads to agents. Every "
        "form on the site is a Gravity Forms lead-capture form pointed AT "
        "agents (gform_60 asking Name, Email, Business Name, Phone and a "
        "checkbox set of Medicare / Final Expense / Mortgage Protection / "
        "Annuities & IULs / Recruitment / Seminars / Other; gform_116 "
        "asking First Name, Last Name, Agency and Email). There is no "
        "consumer-facing lookup: the consumer is the product here, not "
        "the customer."
    ),
    "kalibrate-com": (
        "Verified 2026-09-25 by browser render. kalibrate.com (Kalibrate "
        "Global, the company behind the row's Intalytics) sells location "
        "intelligence and retail site-selection analytics to businesses. "
        "The only forms are two copies of the WordPress site search "
        "(input[name=s], GET to /) plus a HubSpot newsletter subscribe "
        "frame (email + a 13-option Company Industry select). The site "
        "runs reCAPTCHA v3 sitewide via Contact Form 7 (recaptcha api.js "
        "with render= key, plus a grecaptcha-badge), which is worth "
        "recording but is not itself a search surface. No consumer "
        "lookup: the unit of analysis is a trade area, not a person."
    ),
    "jigyasaanalytics-com": (
        "Verified 2026-09-25 by browser render. jigyasaanalytics.com is "
        "an analytics consulting firm (synthetic data, publishing, "
        "financial services). The home page carries no form and no input "
        "at all. Its /contact-us page states, in its own words, 'Please "
        "note that Jigyasa Analytics LLC no longer operates as a data "
        "broker' -- recorded here as a broker-surface claim the dataset "
        "does not reflect, flagged and not fixed, since the company still "
        "publishes the opt-out form described on the other leg. Either "
        "way there is no consumer lookup surface."
    ),
    "kaspr-io": (
        "Verified 2026-09-25 by browser render. kaspr.io is a LinkedIn "
        "contact-extraction extension sold to sales teams (French "
        "operator, 200m+ profiles claimed). The only forms are two 'Enter "
        "your work email' boxes that GET to app.kaspr.io/signin/ -- "
        "product signup, not a lookup. The product itself is reachable "
        "only from inside a paid account via the browser extension, so "
        "there is no surface a person can query about themselves. "
        "Consistent with that, the DSR form on the opt-out leg asks for a "
        "business email, job title, company and LinkedIn URL: the record "
        "is keyed to a professional profile."
    ),
    "consumercanvas-net": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Row keyed by domain for the first time this batch: it carried "
        "domain=null and collided with three other keyless rows on the "
        "literal slug 'broker'. The domain, consumercanvas.net, comes "
        "from its own opt_out_email and is confirmed (www resolves to "
        "23.185.0.3 and serves a WordPress site titled 'Consumer Canvas "
        "LLC'; the apex has no A record, only MX to inbound-smtp.us- "
        "east-1.amazonaws.com). The only form on the site is WordPress "
        "content search (form.form-searchform, GET to /, single input "
        "name=s) -- site-content search, not a people lookup. Consumer "
        "Canvas is MRI-Simmons' data-enrichment and syndicated-audience "
        "product, sold to marketers; there is no consumer-facing record "
        "search to drive. No captcha."
    ),
    "datadojo-ai": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Another of the four formerly keyless rows; domain datadojo.ai "
        "taken from its own opt_out_email and confirmed. NOTE THE "
        "REBRAND, which the dataset does not record: datadojo.ai 302s to "
        "renshudata.com and every page titles itself 'Renshu Data'. Same "
        "Shopify IP 23.227.38.65. The only forms are Shopify furniture -- "
        "a contact form (contact[name], contact[email], contact[body]) "
        "and a store search (GET /search, name=q, placeholder 'Search our "
        "store...'). No people search, no consumer record lookup, no "
        "captcha. DataDojo CDP is a website-visitor identification pixel "
        "sold to ecommerce brands."
    ),
    "saleseer-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Third of the four formerly keyless rows; domain saleseer.com "
        "from its own opt_out_email, confirmed live (200). Rendered at "
        "12s, not 2.5s, because the first pass reported zero forms on a "
        "2.4KB body. There are genuinely none on the homepage. Saleseer "
        "is a Houston B2B life-sciences data business (oncology clinical "
        "data, ClinicalPath) selling to pharma; the only form anywhere is "
        "the /contact-us/ sales enquiry (WPForms 211, required "
        "First/Last/Company/Email plus a required 'I would like to learn "
        "more about' select), behind invisible reCAPTCHA v3. Nothing "
        "resembling a consumer record search exists."
    ),
    "datalane-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Fourth of the four formerly keyless rows. The dataset row is "
        "named 'TASK GENIE Inc' with no domain and david@datalane.com as "
        "its only identifier; Task Genie Inc does business as DataLane "
        "(confirmed against the Texas data-broker registry and ZoomInfo, "
        "which files DataLane under Task Genie Inc), so the key is "
        "datalane-com. datalane.com serves 200 behind Cloudflare. No "
        "forms at all on the homepage at a 12s settle -- the only call to "
        "action is /book-a-demo. DataLane sells structured contact data "
        "on LOCAL BUSINESSES and their decision makers to sales teams; "
        "there is no consumer-record search surface. No captcha."
    ),
    "keymarketingcorp-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "www.keymarketingcorp.com is a B2B list-services and modelling "
        "shop (CRM Solutions / Customer Acquisition / List Services / "
        "Modeling & Analytics). No search form of any kind on the "
        "homepage; the only two links out are /request_information and "
        "the rights page. reCAPTCHA is loaded site-wide but there is "
        "nothing here to search."
    ),
    "keywordconnects-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "keywordconnects.com sells homeowner leads to home-improvement "
        "advertisers. Rendered at 12s specifically because the 2.5s pass "
        "showed a Marketo form (#mktoForm_1012) with zero controls -- at "
        "12s it is STILL empty, so the Marketo embed never populates; "
        "either way it is a marketing-contact form, not a search. No "
        "other form, no people lookup, no captcha."
    ),
    "rbarrel-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "rbarrel.com (RainBarrel, Knower Tech USA) sells location-based "
        "advertising audiences. The only forms on the homepage are two "
        "copies of the Finsweet cookie preference widget (#cookie- "
        "preferences, three off-layout checkboxes marketing-2 / "
        "personalization-2 / analytics-2). No search surface -- the "
        "product is a MAID and hashed-email audience database, not a "
        "queryable people index."
    ),
    "komodohealth-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "komodohealth.com carries exactly two forms: a site-content "
        "search overlay (form.oms-search--form, GET to /, input oms- "
        "search--input, both controls off-layout until the overlay opens) "
        "and a HubSpot 'Subscribe to Insights' email capture. Komodo "
        "Health sells de-identified healthcare claims analytics to life "
        "sciences; there is no patient or consumer record lookup a person "
        "could run on themselves. No captcha on either form."
    ),
    "kontextdata-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "kontextdata.com is a single-screen site -- 170 characters of "
        "body text, re-rendered at 12s to be sure it was not an SPA "
        "shell, and it is not; that is the whole page. Text reads "
        "'Consented, shopping-focused consumer insights' plus four footer "
        "links. No forms whatsoever, so no search surface. No captcha."
    ),
    "l2-data-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "l2-data.com (Labels & Lists / L2 Political, voter and consumer "
        "data) exposes a WordPress site search (#searchform, GET to /, "
        "input name=s, both controls off-layout) and a Gravity Form "
        "marketing enquiry (#gform_19). Site-content search is not a "
        "record lookup, and the voter files themselves are sold, not "
        "searchable. Invisible reCAPTCHA v3 is wired into the Gravity "
        "forms sitewide (gravityformsrecaptcha 2.2.2, gf_invisible "
        "ginput_recaptchav3, grecaptcha-badge present) but is irrelevant "
        "to the search leg because there is no search to drive."
    ),
    "lbdigitaldata-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "lbdigitaldata.com is a Squarespace brochure site for LB Digital "
        "Data, a registered DBA of Stirista LLC. One form: a newsletter "
        "email capture (form.newsletter-form, POST, input name=email), "
        "behind reCAPTCHA ENTERPRISE (recaptcha/enterprise.js, site key "
        "6LdDFQwjAAAAAPigEvvPgEVbb7QBm-TkVJdDTlAv, badge and a "
        "g-recaptcha-response textarea in the DOM, anchor frame present). "
        "No search surface -- the audience data is sold, not queryable."
    ),
    "monitorbase-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "www.monitorbase.com (Lender Feed LLC, trading as MonitorBase) "
        "sells mortgage borrower-intent alerts to loan officers. Zero "
        "forms on the homepage at a 12s settle; every control is a nav "
        "toggle. No consumer-facing search. No captcha on the homepage."
    ),
    "lighthouselist-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "lighthouselist.com (a registered DBA of Stirista LLC) redirects "
        "to www and serves a brochure page with no forms at all -- one "
        "'OK' cookie button is the only control. No search surface. Its "
        "single privacy link goes to the shared Stirista portal at "
        "unsubscribe.stirista.com."
    ),
    "listkit-io": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "listkit.io sells B2B prospect lists (cold-email contact data). "
        "The public homepage carries no form at all -- controls are a "
        "volume range slider and two embedded video players (Wistia, "
        "YouTube) -- and the searchable contact database is behind the "
        "app login at app.listkit.io. There is therefore no public search "
        "surface to drive, and nothing a person could query about "
        "themselves. No captcha on the public page."
    ),
    "liveintent-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "liveintent.com (now part of Zeta) carries a large Marketo demo- "
        "request form (mktoForm_1380: FirstName, LastName, Email, "
        "Company, Title, Country and Job_Role__c all required, plus 26 "
        "Secondary_Opportunity__c interest checkboxes and 13 hidden UTM "
        "fields) and a WordPress site search (input name=s). The two "
        "loose 'Enter first name' / 'Enter last name' inputs belong to "
        "that same Marketo widget, NOT to a people search -- worth "
        "stating because they read like one in a raw dump. No consumer "
        "record lookup. No captcha."
    ),
    "lob-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "www.lob.com sells direct-mail automation and address "
        "verification to businesses via API. No forms on the homepage at "
        "a 12s settle. Address verification is an authenticated API "
        "product, not a public people search, and there is no consumer- "
        "facing lookup. No captcha."
    ),
    "lotame-com": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "www.lotame.com (now an Epsilon/Publicis property) carries two "
        "HubSpot forms (portal 1867366) -- a newsletter subscribe and a "
        "contact capture -- and nothing else. Lotame sells addressable "
        "audience segments and identity resolution; the data is licensed "
        "to buyers, not searchable by a consumer. No captcha."
    ),
    "mrss-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.mrss.com is M+R's own agency site (title 'M+R', "
        "a fundraising / issue-advocacy consultancy for nonprofits) and "
        "has no people-search surface of any kind. The only two forms on "
        "it are Mailchimp newsletter signups -- #embedded-subscribe-form "
        "posting to /mailchimp_rss_feed/mailchimp-submit.php with "
        "first_name, last_name, email, org and a _mc4wp-style honeypot "
        "named b_21b34a1969d796a966e8260c1_e34d4a711e, and a footer "
        "duplicate with footer_-prefixed names. Nothing looks up a "
        "person. M+R is in the dataset as a compiler of donor/political "
        "data sold to clients, not as a consumer-facing search site."
    ),
    "marriott-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.marriott.com is a hotel booking site and has no "
        "people-search surface. On the dataset's privacy URL the only "
        "forms are the Marriott Bonvoy sign-in (form.s-form posting to "
        "/aries-auth/loginWithCredentials.comp with userID, memberNumber, "
        "password, rememberMe) and a d-none session-timeout stub. Booking "
        "search looks up hotels, not people, and the loyalty account "
        "lookup is a login, not a search of third parties. Marriott is in "
        "this dataset as a CA-registered data broker over its own "
        "guest/loyalty data, which is not queryable by an outsider."
    ),
    "mrginc-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.mrginc.com (Media Resource Group, Inc.) is a B2B "
        "direct-marketing site with no people-search surface. Its only "
        "form, on both the homepage and /opt-out/, is a generic 'Get In "
        "Touch' contact form -- name (required), email (required), phone "
        "(required), message textarea, a tc_accepted 'I accept the Terms "
        "of Service' checkbox and Submit -- behind invisible reCAPTCHA v3 "
        "(api.js?render=6LePGKwqAAAAAMGOL1PtMEQoIc6qhuyvjLw_JgOA, "
        "grecaptcha-badge present). Nothing on the site looks up a "
        "person."
    ),
    "mediasourcesolutions-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.mediasourcesolutions.com IS GONE. Every path on "
        "it, including the bare domain and the do-not-share URL from the "
        "notes, 302s to /cgi-sys/suspendedpage.cgi and serves a "
        "103-character body reading 'Account Suspended / This Account has "
        "been suspended. / Contact your hosting provider for more "
        "information.' -- the cPanel suspension page. Probed twice in the "
        "same batch (root and the rights URL) with the same result. This "
        "is a dead site, not an anti-bot wall: status 200, no captcha, no "
        "challenge, no frames, nothing to render. No search surface can "
        "exist on a suspended host."
    ),
    "media-net": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.media.net is an ad-tech sell-side platform and "
        "has no people-search surface. Its only form is #wf-form-Contact- "
        "Form, a single off-layout required email input with an invisible "
        "Submit -- a Webflow newsletter/contact capture. No captcha on "
        "the page. Nothing looks up a person; Media.net's own opt-out "
        "page states it 'shall never use personally identifiable "
        "information to target any advertising'."
    ),
    "mediamath-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. MediaMath no longer exists as its own site: "
        "www.mediamath.com 301s to infillion.com/products/dsp/, a product "
        "page titled 'Infillion Platform' (Infillion acquired MediaMath, "
        "and the page's own banner announces Infillion acquiring "
        "Foursquare). No people-search surface there: the forms are an "
        "AddSearch site-search box and two Marketo 'Stay Informed' email "
        "captures (mktoForm_1067, Email plus utm_ hidden fields). Nothing "
        "looks up a person."
    ),
    "mediasoftstudio-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. mediasoftstudio.com IS DEAD, in three independent "
        "ways. (1) HTTPS is broken: Playwright refused the navigation "
        "with net::ERR_CERT_DATE_INVALID and curl reports 'certificate "
        "has expired', so nothing on the site can be reached over TLS at "
        "all. (2) Over plain HTTP the root serves a 17-character body "
        "reading 'Under Maintenance' (title 'Under Maintenance') and "
        "nothing else -- no forms, no links, no captcha. (3) The "
        "dataset's opt_out_url path, /unsubscribe.php, returns HTTP 404 "
        "over HTTP. Probed as a single target on its own invocation, "
        "since the URL contains 'unsubscribe'. No search surface: there "
        "is no site left to search."
    ),
    "mediawallah-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. mediawallah.com is an identity-resolution ad-tech "
        "site with no people-search surface. Its only form on both the "
        "homepage and the do-not-sell page is #mc4wp-form-1, a Mailchimp- "
        "for-WordPress newsletter capture (required EMAIL, Submit, an "
        "off-layout _mc4wp_honeypot text input that would need to go in "
        "forbidden_selectors, and the _mc4wp_ "
        "timestamp/form_id/form_element_id hidden fields). Everything "
        "else on the page is the OneTrust cookie preference centre. "
        "Nothing looks up a person."
    ),
    "leadzod-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.leadzod.com is a Wix lead-generation brochure "
        "site (500-character body, title 'Home | leadzod') with no forms "
        "at all on the homepage and no people-search surface -- the only "
        "control is a skip-to-content button. Nothing looks up a person."
    ),
    "medprosystems-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.medprosystems.com sells healthcare-practitioner "
        "licence validation to life-sciences companies and has no "
        "consumer people-search surface. The only form on the site is a "
        "HubSpot embed in a child frame (forms.hsforms.com/.../47075486/6 "
        "83996d6-c985-45e6-a015-c8e3a18b56a6) asking Company*, First "
        "Name*, Last Name*, Title, Email*, 'How did you hear about us?' "
        "plus a hidden honeypot -- a B2B sales enquiry, not a lookup. Its "
        "HCP database is sold to clients, not exposed to the public."
    ),
    "melissa-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. No consumer people-search surface found on "
        "melissa.com. The homepage's only form is a HubSpot 'Clean "
        "Connections' newsletter subscribe (required email plus two "
        "required LEGAL_CONSENT checkboxes), and the live-chat frame's "
        "invisible reCAPTCHA Enterprise is HubSpot's, not a search wall. "
        "Checked the obvious free-lookup path too: "
        "www.melissa.com/lookups/ returns HTTP 404. Melissa's products "
        "are address-verification and data-quality APIs sold to "
        "businesses and gated behind an account or a 'Request Demo', and "
        "its one consumer-facing record tool linked from the nav is "
        "deceased-suppression for mailers, which removes records rather "
        "than returning a person. Nothing here looks up an individual on "
        "request."
    ),
    "meltwater-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.meltwater.com is a media-monitoring / social- "
        "listening platform sold by subscription, with no public people- "
        "search surface. The privacy page's only form is a single-field "
        "Google Forms email capture (POST to docs.google.com/forms/u/0/d/ "
        "e/1FAIpQLScRGK67d9hPTToPT6bmIfg7Vz8VDGNDdSJs01Ksvde2idihpg/formR "
        "esponse, field emailAddress) for privacy-policy update "
        "notifications, and the homepage's is a 'Request Demo' email "
        "capture (#request-form). Everything that would search a person "
        "is behind 'Sign in' or 'Request a demo'. No captcha on either "
        "page."
    ),
    "messagedigital-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.messagedigital.com sells email/text/CRM tooling "
        "to campaigns and nonprofits and has no people-search surface. "
        "Its only form is a HubSpot embed "
        "(hsForm_83edac0b-1a1c-4acb-8515-b212f771a178, POST to "
        "forms.hsforms.com/.../45453246/83edac0b-...) asking First Name*, "
        "Last Name*, Organization*, Job Title*, Email*, Phone number* and "
        "Inquiry* behind invisible reCAPTCHA Enterprise -- a B2B sales "
        "enquiry. Nothing looks up a person."
    ),
    "merkleinc-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. merkleinc.com is not a separate site: it resolves to "
        "www.merkle.com, the dentsu-owned Merkle agency site (title "
        "'Merkle - We Power the Experience Economy'), which has no "
        "people-search surface and in fact no forms at all on its "
        "homepage -- the only child frame is an invisible reCAPTCHA v3 "
        "anchor. Merkle's consumer data is sold to clients, not queryable "
        "by the public. Same finding as merkle-com; the two dataset rows "
        "are one site."
    ),
    "merkle-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.merkle.com (dentsu's Merkle) has no people- "
        "search surface and no forms at all on its homepage -- the only "
        "child frame is an invisible reCAPTCHA v3 anchor. Its consumer "
        "data products are sold to clients rather than exposed as a "
        "lookup. Identical surface to the merkleinc-com row, which "
        "resolves to this same host."
    ),
    "internetbrands-com": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. www.internetbrands.com is MH Sub I, LLC's corporate "
        "holding-company site (a vertical-portfolio brochure page, 1.4k "
        "body, no forms and no captcha) and has no people-search surface. "
        "The consumer data lives in its operating properties (WebMD, "
        "Nolo, Martindale-Hubbell, CarsDirect, Fodor's and the rest), "
        "each of which is its own site; nothing on the corporate domain "
        "looks up a person."
    ),
    "mightyrep-com": (
        "MightyRep sells industrial B2B website-visitor identification -- "
        "it resolves anonymous company traffic into contact details for "
        "its customers' sales teams -- and has no consumer-facing lookup "
        "of any kind. Verified by browser render 2026-09-25: "
        "mightyrep.com carries ZERO forms in the DOM (the only control "
        "anywhere is the invisible g-recaptcha-response textarea that "
        "Contact Form 7's reCAPTCHA v3 integration leaves behind, sitekey "
        "6LfAKWArAAAAAJco-xKg4jAgvStjMDpyRHJ34UAE). Nothing on this leg "
        "to read."
    ),
    "milestonemarketingsolutions-com": (
        "Milestone Marketing Solutions is a direct-mail and data-analysis "
        "agency selling to businesses; there is no consumer lookup. "
        "Verified by browser render 2026-09-25: the homepage's only form "
        "is a Wix contact form (first-name, last-name, company, phone, "
        "email, subject, message, plus a required and computed-invisible "
        "Captcha3940957316__checkbox) whose purpose is sales enquiries, "
        "and the /do-not-sell-my-information page's only form is the opt- "
        "out itself. No search surface of any kind."
    ),
    "minerva-io": (
        "MINERVA BI sells an AI platform to marketing leaders (nav: "
        "Platform, Customers, Pricing, Company, API, Book a Demo). "
        "Verified by browser render 2026-09-25: www.minerva.io carries "
        "ZERO forms and no search input at all -- not even a site search. "
        "The only rights-related link is 'Opt Out' -> "
        "preferences.minerva.io. No consumer lookup exists on this leg."
    ),
    "onspotdata-com": (
        "Mobile Technology Corporation (OnSpot Data) sells location- and "
        "point-of-interest data services to partners; nothing consumer- "
        "facing. Verified by browser render 2026-09-25: "
        "www.onspotdata.com/privacy-policy/ renders 13.8k characters of "
        "policy prose whose single form is the site-wide newsletter "
        "subscribe (input[name=Email-2] plus submit), and that form is "
        "off-layout. There is no lookup of a named person anywhere on the "
        "site."
    ),
    "mobilewalla-com": (
        "Mobilewalla sells device-graph and mobile audience data to "
        "businesses and matches ONLY on mobile advertising identifiers "
        "(see the opt-out leg). Verified by browser render 2026-09-25: "
        "the only forms on www.mobilewalla.com/global-opt-out-request are "
        "the CCPA request form itself (#ccpaForm) and a HubSpot blog- "
        "subscribe form; the site navigation is entirely B2B (WHO WE "
        "SERVE / SOLUTIONS / INDUSTRIES / TALK TO A DATA EXPERT). No "
        "consumer lookup surface."
    ),
    "securiti-ai": (
        "This row is Modernize, Inc. (a QuinStreet home-improvement lead- "
        "generation brand) keyed to the domain of its DSAR vendor, "
        "securiti.ai. Verified by browser render 2026-09-25: "
        "modernize.com is a contractor-matching marketplace whose every "
        "form takes a ZIP code and a trade (header search input[name=s], "
        "sticky-banner input[name=zip], #homeowner-form with 28 "
        "radio[name=trade] values plus input[name=zip], and a newsletter "
        "email). There is no lookup of a named person. CAVEAT worth "
        "recording: www.modernize.com returned a 403 interstitial "
        "('Something Went Wrong !!!', Error Code-a40d0f26197bd51c, and a "
        "nonsense 'Location-JP' for a US IP) on the first render and the "
        "apex modernize.com rendered fully on the retry minutes later, so "
        "this site's WAF fires intermittently on the www host."
    ),
    "modfxlabs-com": (
        "Modfx Labs is a mobile-location SDK aggregator selling event- "
        "planning analytics; it keys everything to advertising "
        "identifiers and has no person lookup. Verified by browser render "
        "2026-09-25: both modfxlabs.com and modfxlabs.com/privacy carry "
        "ZERO forms and zero inputs of any kind. No captcha, and nothing "
        "to search."
    ),
    "mogean-com": (
        "Mogean sells a point-of-interest dataset and geospatial "
        "analytics. Its own opt-out page states the POI dataset 'describe "
        "businesses and places, not people, and carry no device "
        "identifiers or movement traces'. Verified by browser render "
        "2026-09-25: www.mogean.com/opt-out carries ZERO forms and zero "
        "controls. No person lookup on this leg."
    ),
    "monevo-us": (
        "Monevo is a B2B credit-offer distribution platform (it hosts and "
        "distributes pre-qualified credit offers for 150+ lenders) with "
        "no consumer record lookup. Verified 2026-09-25, and see the opt- "
        "out leg for the dataset defect: monevo.us DOES NOT RESOLVE "
        "(ERR_NAME_NOT_RESOLVED for both www.monevo.us and monevo.us), "
        "monevo.com redirects to /uk/ which 404s at the HTTP level while "
        "rendering the UK marketing site, and that site's only links are "
        "'Request demo' and 'Contact'. No search surface exists at any of "
        "the three hosts."
    ),
    "definitivehc-com": (
        "Monocl is now part of Definitive Healthcare, which sells "
        "healthcare-market data and analytics to life-sciences and "
        "provider businesses. Verified by browser render 2026-09-25: "
        "www.definitivehc.com's only form is the Acquia/Drupal site "
        "content search (#views-exposed-form-acquia-search-page, "
        "input[name=search] -> /search), duplicated for the mobile header "
        "and off-layout in both copies. Monocl's expert/HCP profiles are "
        "behind a paid login (nav: Log in, Free trial), so there is no "
        "public lookup of a named person."
    ),
    "moodys-com": (
        "Moody's is a ratings and analytics company; it rates issuers and "
        "instruments, not consumers, and has no people lookup. Verified "
        "by browser render 2026-09-25: "
        "www.moodys.com/web/en/us/legal/privacy-policy.html renders 37.7k "
        "characters of policy and its single form is the site-wide "
        "content search (#search-input, 'Search ratings, research and "
        "more...'). What Moody's holds about an individual is reached "
        "through the request form recorded on the opt-out leg, not by "
        "searching."
    ),
    "movingleads-com": (
        "MovingLeads (First Movers Advantage, LLC / FMAdata) sells "
        "homeowner-mover direct-mail data to movers and home-services "
        "businesses. Verified by browser render 2026-09-25: "
        "www.movingleads.com/opt-out-new's only form is the privacy- "
        "request form itself; the site navigation is entirely B2B "
        "(Services & Pricing, Smart Targeting, Performance Tools, Mail "
        "Design, Sign in, Schedule Demo). No consumer lookup surface."
    ),
    "narvar-com": (
        "Narvar sells post-purchase tracking, returns and delivery-claim "
        "software to retailers; consumers meet it as a retailer's "
        "tracking page, never as a searchable database. Verified by "
        "browser render 2026-09-25: corp.narvar.com/legal/privacy-policy "
        "renders 65.8k characters and carries ZERO forms -- the only "
        "interactive elements are demo-request links and an Osano-style "
        "'Privacy Choices' anchor. No lookup of a named person exists."
    ),
    "nationalopinioninstitute-com": (
        "National Opinion Institute is a survey research agency (a "
        "consumer panel operator) at a P.O. box in Falls Church, VA. "
        "Verified by browser render 2026-09-25: its entire homepage is "
        "456 characters -- nav 'Home Participate', a one-line "
        "description, a postal address and four legal links -- and "
        "carries ZERO forms and zero inputs. The /ca-privacy/ notice "
        "likewise has no controls. No consumer lookup surface exists."
    ),
    "nativo-com": (
        "Nativo sells native-advertising placement and audience data to "
        "advertisers and publishers; nothing consumer-facing. Verified by "
        "browser render 2026-09-25, and note the domain finding on the "
        "opt-out leg: www.nativo.com/interest-based-ads redirects to "
        "ads.life360.com/legal/interest-based-ads (Life360 has acquired "
        "Nativo), and that page's only form is a 'Your Work Email' "
        "newsletter subscribe (input[name=Email-4], off-layout). No "
        "lookup of a named person anywhere."
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
    # --- batch 23 of 2026-09-24: C sweep ---------------------------------
    # Two of these are CourtRecords.us siblings settled by the recheck
    # recorded below; the other two are walls of their own.
    "coloradocourtrecords-us": (
        "Verified 2026-09-24. Blocked by the same registration paywall as "
        "californiacourtrecords-us, and confirmed to be the same surface "
        "rather than merely a similar one: coloradocourtrecords.us carries "
        "the identical #nameSearchForm posting to /search/loading/ with "
        "the same firstName / lastName / city / state controls, observed "
        "in the markup.\n"
        "\n"
        "The network's FCRA notice states the wall in its own words: "
        "'CourtRecords.us will conduct only a preliminary people search "
        "... and a search of any records will only be conducted and made "
        "available after you register for an account or purchase a "
        "report.' The California test drove that flow to its end and got "
        "search theatre -- an abbreviated name and a list of courts, no "
        "results. See californiacourtrecords-us for the full write-up.\n"
        "\n"
        "Dismiss the TrustArc consent overlay first if rechecking."
    ),
    "connecticutcourtrecords-us": (
        "Verified 2026-09-24. Identical to coloradocourtrecords-us and "
        "californiacourtrecords-us: same #nameSearchForm, same "
        "/search/loading/ action, same field set, same registration "
        "paywall on the results, same TrustArc overlay. Recorded "
        "separately only because the dataset lists the fifty network sites "
        "as fifty rows. See californiacourtrecords-us for the full "
        "write-up."
    ),
    "classmates-com": (
        "Verified 2026-09-24 by browser render. Blocked by "
        "AUTHENTICATION: the member-facing routes redirect to "
        "secure.classmates.com/auth/login, which carries password, "
        "magic-link and Facebook sign-in forms and a rendered reCAPTCHA. "
        "No unauthenticated name lookup was reachable.\n"
        "\n"
        "The dataset's own note agrees and adds the detail that matters "
        "for a recipe: 'Name search is not URL-addressable.' Even past the "
        "login there would be no stable URL to drive, which is a second, "
        "independent obstacle -- a search recipe needs an addressable "
        "query, not just a reachable page.\n"
        "\n"
        "Recorded as blocked rather than no-surface because Classmates "
        "plainly DOES hold name-keyed records and plainly does let "
        "somebody search them -- yearbook profiles are the product. What "
        "it does not do is let the subject look without an account. The "
        "opt-out leg is walled the same way and is the more troubling of "
        "the two; see optout_forms."
    ),
    "clustal-org": (
        "Verified 2026-09-24. Blocked before any content was served: "
        "www.clustal.org answered HTTP 403 with a Cloudflare bot-"
        "verification interstitial ('Just a moment...', 'Performing "
        "security verification ... protect against malicious bots', Ray ID "
        "present, __cf_chl_rt_tk challenge token appended to the URL).\n"
        "\n"
        "Worth flagging that a search surface is known to exist even "
        "though it was not seen: the dataset's opt-out note describes it "
        "step by step -- 'Go to Clustal.org and look for your information "
        "in the top search bar. Click on \"view full record\" to grab the "
        "URL.' So there is a top-bar name search and a per-record detail "
        "page behind it. That also makes the two legs coupled: the opt-out "
        "needs a record URL that only this search can produce, so "
        "unblocking the search is a precondition for the opt-out, not "
        "merely a nice-to-have.\n"
        "\n"
        "To resolve: recheck from the deployment host; a residential "
        "address may pass the challenge that this one did not."
    ),
    # --- recheck of 2026-09-24: three rows moved here from UNDECIDED ----
    #
    # These three were left undecided in batches 21 and 22 for an honest
    # reason -- the search form had been found and its selectors recorded,
    # but no result page had been reached, so nothing could be said about
    # what happens on submit. The recheck was cheap and settled all three,
    # and both of the walls it found are worth naming.
    #
    # WALL 1, Cloudflare Turnstile, on the optOutLight white-label engine
    # (backgroundcheckers-net and checksecrets-com). Requesting the search
    # route directly returns a page with a body length of ZERO and a
    # single first-party Turnstile frame. That is the clearest possible
    # answer to the question batch 21 left open, and it retires the note
    # there that a captcha "was not observed": it is observed now.
    #
    # WALL 2, a REGISTRATION PAYWALL, on the CourtRecords.us network. This
    # is a kind of wall this dict has not held before -- not a bot check
    # and not a 403, but a search that runs, answers, and deliberately
    # shows the consumer nothing. It is recorded here rather than as
    # no-surface because a surface plainly exists and was driven; what it
    # withholds is the result. Anyone extending this module should expect
    # more of these: for a paid people-search site, the free search is a
    # sales funnel, and its job is to prove a record EXISTS without
    # showing it.
    "backgroundcheckers-net": (
        "Verified 2026-09-24, then RECHECKED the same day, which changed "
        "the verdict from no-verdict to blocked.\n"
        "\n"
        "The surface is real and fully mapped: on www.backgroundcheckers."
        "net, input#firstName, input#lastName, a state select#state and a "
        "submit #perform-search reading 'FREE SEARCH', behind an FCRA "
        "'I AGREE' notice modal. All three inputs map onto resolve_fields "
        "with nothing left over, so this would be a recipe but for the "
        "wall.\n"
        "\n"
        "BLOCKED by Cloudflare Turnstile. The first pass could only say "
        "that the site served Turnstile on its OPT-OUT route and that "
        "whether it fired on search was unobserved -- which, per the "
        "standing rule, is not a licence to call it unguarded. The "
        "recheck requested the search route directly (/name/search with "
        "fname/lname) and got back a page of length ZERO whose only "
        "content is a frame at /assets/common/captcha/turnstile. The "
        "search is gated.\n"
        "\n"
        "Note for any captcha detector: Turnstile is served from a "
        "FIRST-PARTY path here, not from challenges.cloudflare.com, so "
        "matching on the vendor hostname would miss it.\n"
        "\n"
        "Same engine as checksecrets-com -- see that entry. The opt-out "
        "leg is separately blocked on the same captcha plus an "
        "email-confirmation hop this SMTP-send-only repo cannot read."
    ),
    "checksecrets-com": (
        "Verified 2026-09-24 and rechecked the same day; blocked for "
        "exactly the same reason as backgroundcheckers-net, because it is "
        "exactly the same site under a different name.\n"
        "\n"
        "The surface: #email-form on /optOut/name/landing with "
        "input#First-Name (\"Enter person's First Name\"), "
        "input#Last-Name, a select named 'field', and a submit reading "
        "'FREE SEARCH', behind the same FCRA 'I AGREE' modal.\n"
        "\n"
        "BLOCKED by Cloudflare Turnstile, confirmed the same way: "
        "requesting /name/search directly returns a zero-length body "
        "whose only content is a frame at /assets/common/captcha/"
        "turnstile. Requesting /name/landing with query parameters loads "
        "the same Turnstile frame alongside the form.\n"
        "\n"
        "The white-label identity is established in optout_forms under "
        "checksecrets-com: identical FCRA notice text word for word, "
        "identical 'I AGREE' gate, identical /api/helper/optOutLight/"
        "search removal path, identical #pageForm field list, different "
        "companies on the dataset rows (TRUTH NOW LLC vs "
        "BackgroundCheckers). The recheck confirms the engine extends to "
        "the search leg and to the captcha posture.\n"
        "\n"
        "This is the payoff the engine observation predicted, and it is "
        "worth stating as a result rather than a hope: ONE recheck "
        "settled TWO dataset rows, and would settle any further sibling "
        "domains the same way. Identifying the engine before working "
        "domains individually is now a measured saving, not a guess.\n"
        "\n"
        "One difference to carry: the third control differs per tenant (a "
        "'field' select here, a state select on backgroundcheckers-net), "
        "so a shared recipe would have to read it at runtime rather than "
        "template it."
    ),
    "californiacourtrecords-us": (
        "Verified 2026-09-24 and rechecked the same day. The recheck "
        "settled this row and, with it, the whole fifty-site "
        "CourtRecords.us network.\n"
        "\n"
        "THE SURFACE EXISTS AND WAS DRIVEN. The home page carries "
        "#nameSearchForm, POST to /search/loading/, with input#firstName "
        "and input#lastName required, an optional city, a hidden state, "
        "and a bank of ten unnamed record-type checkboxes (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offences, Small Claims, Lawsuits). A search was submitted and "
        "answered with HTTP 200.\n"
        "\n"
        "BLOCKED by a REGISTRATION PAYWALL, and the shape of it is the "
        "finding. What comes back is a 'Searching...' page: it echoes the "
        "query as an ABBREVIATED name ('Searching All Available Public "
        "Records on John S.') and then lists scores of California "
        "Superior Courts and Courts of Appeal as 'Data Source'. There are "
        "no results on it, no form, and no server-side redirect -- it is "
        "search THEATRE, staged to look like work in progress. The site's "
        "own FCRA notice says what is actually happening: 'CourtRecords.us "
        "will conduct only a preliminary people search of the information "
        "you provide and ... a search of any records will only be "
        "conducted and made available after you register for an account "
        "or purchase a report.'\n"
        "\n"
        "So the question batch 22 left open -- is the free search a usable "
        "self-lookup or a teaser? -- is answered: it is a teaser. A "
        "consumer cannot learn from it whether they are listed, which is "
        "the only thing this module's search leg exists to establish.\n"
        "\n"
        "Recorded as blocked rather than no-surface because the surface is "
        "real, reachable and was successfully driven; the wall is on the "
        "results, not on the form. That is a new wall for this dict and is "
        "described in the header above.\n"
        "\n"
        "Two operational notes. A TrustArc consent overlay ('AGREE & "
        "PROCEED') sits over the site and should be dismissed first. And "
        "this is ONE OF FIFTY per-state sites whose pages are "
        "byte-similar, so this single test stands for all of them -- the "
        "second white-label family settled by one recheck in this pass.\n"
        "\n"
        "The opt-out leg is separately closed as mailbox-only, and the "
        "two legs are coupled: the removal email is per-record, so it "
        "needs a record identity this search will not give up."
    ),
    "zoominfo-com": (
        "Verified 2026-09-23: the probed page answers 403 with no body, so "
        "no surface could be read. Worth a note beyond the usual, because "
        "ZoomInfo DOES operate a public person/company directory that "
        "ordinarily appears in search results -- so unlike the B2B rows "
        "recorded as no-search-surface, a presence check here is probably "
        "MEANINGFUL and simply unreachable by this client. Do not let this "
        "row drift into no-surface on the strength of the 403."
    ),
    "spglobal-com": (
        "Verified 2026-09-23: probed page answers 403 with no body. "
        "Nothing is known about a search surface; S&P Global's consumer-"
        "facing exposure is likely minimal, but that is not established "
        "here."
    ),
    "infocore-com": (
        "Verified 2026-09-23: the whole site answers 403 behind a "
        "Cloudflare 'Confirm you are human' interstitial, so no page -- "
        "search or otherwise -- could be reached. Same evidence as the "
        "opt-out leg; recorded on both so neither looks unexamined."
    ),
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
    "costar-com": (
        "Verified 2026-09-23: privacy.costar.com/DSAR-submission returns "
        "HTTP 403 from Akamai ('Access Denied ... Reference "
        "#18.46a7cb17...'), the same edge wall already recorded for "
        "optoutprescreen-com. Nothing renders on the recorded host, so "
        "neither leg could be read there. What CoStar is argues against a "
        "consumer search surface anyway -- commercial real-estate data "
        "sold by subscription -- but the wall is why this is recorded as "
        "blocked rather than absent."
    ),
    "spyfly-com": (
        "Verified 2026-09-23 by running the search twice, once with a "
        "name that should hit and once with invented nonsense. SpyFly IS "
        "a people-search and its homepage carries three working search "
        "forms -- name (input[name='search-name']), address "
        "(input[name='search-address']) and phone (input[name='search- "
        "phone-number']), each with a 'Search Now' submit. Submitting any "
        "of them lands on /people/ei, an animated progress interstitial "
        "counting National, State and County records, and that page ends "
        "at 'Please verify you are human to continue.'  The reason this "
        "is BLOCKED and not UNDECIDED is that both runs ended "
        "identically. 'Michael Smith' and 'Qzxjv Wrrblfnd' produced the "
        "same interstitial, the same percentages and the same human- "
        "verification prompt, so there is no hit marker and no no-results "
        "marker to tell apart -- the wall arrives before the result does. "
        "A future recipe-writer would need to clear that check first; "
        "there is nothing behind it to transcribe until they do."
    ),
    "dehashed-com": (
        "Verified 2026-09-25. DeHashed IS a lookup -- it indexes breach "
        "corpora and invites you to 'search for usernames, email "
        "addresses, IP addresses, and more' -- and it is walled by "
        "REGISTRATION rather than by a bot check, which is why this is "
        "blocked and not no-surface. Rendering "
        "https://www.dehashed.com/search serves the same unauthenticated "
        "marketing page as the homepage, with the nav offering only "
        "'Login' and 'Register': there is no query input anywhere in the "
        "DOM for a visitor without an account, so there is nothing to "
        "transcribe and no hit/miss pair to tell apart. Same treatment as "
        "the CourtRecords.us paywall rows (californiacourtrecords-us and "
        "siblings). Worth noting for any recheck: the site is mid- "
        "migration ('Welcome to 4.0 -- Please be patient as records are "
        "gradually indexed over the coming weeks') and linked pages are "
        "404ing, so the shape of the gate may change."
    ),
    "delawarecourtrecords-us": (
        "Verified 2026-09-25, and resolved by IDENTITY with the network "
        "rather than by driving the flow again. delawarecourtrecords.us "
        "serves the CourtRecords.us white-label template: the same "
        "#nameSearchForm POSTing to /search/loading/ with the same "
        "firstName / lastName / city controls and a hidden state, under "
        "'Instant Access to Civil and Criminal Court Records', observed "
        "in this render. That is the identical surface already driven to "
        "its end on californiacourtrecords-us and confirmed on "
        "coloradocourtrecords-us and connecticutcourtrecords-us, where "
        "the network's own FCRA notice states the wall: only a "
        "preliminary people search runs, and records 'will only be "
        "conducted and made available after you register for an account "
        "or purchase a report'. Blocked by that registration paywall. See "
        "californiacourtrecords-us for the full write-up; nothing on this "
        "state site differs."
    ),
    "familysearch-org": (
        "Verified 2026-09-25 by browser render. Both "
        "www.familysearch.org/ and the recorded privacy URL "
        "/en/legal/privacy come back HTTP 200 with a COMPLETELY EMPTY "
        "main document (title '', 0 characters of text, zero forms) -- "
        "the real page is an Imperva/Incapsula interstitial served in a "
        "child frame titled 'Captcha Required', reading "
        "'www.familysearch.org Additional security check is required ... "
        "FamilySearch uses security tools to ensure that only real people "
        "can access our services', loading js.hcaptcha.com/1/api.js with "
        "an h-captcha widget and both g-recaptcha-response and h-captcha- "
        "response textareas parked off-layout. The genealogy search "
        "surface certainly exists behind that, but it is walled on every "
        "visit from this browser, so no search recipe can be written "
        "against it. Note the shape for anyone rechecking: a 200 with an "
        "empty body is the tell here, not a 403."
    ),
    "familytreenow-com": (
        "Verified 2026-09-25 by browser render. "
        "https://www.familytreenow.com/ answers 403 and redirects to a "
        "?__cf_chl_rt_tk= challenge page titled 'Just a moment...' "
        "reading 'Performing security verification. This website uses a "
        "security service to protect against malicious bots', loading "
        "challenges.cloudflare.com/turnstile/v0/b/d76008a69eab/api.js "
        "with a cf-turnstile-response hidden input. Cloudflare Turnstile "
        "on the front door, so the name lookup FamilyTreeNow is known for "
        "never rendered at all. Blocked rather than no-surface: the "
        "surface is not in doubt, only reachable."
    ),
    "floridacourtrecords-us": (
        "Verified 2026-09-25, and resolved by IDENTITY with the "
        "CourtRecords.us network rather than by driving the flow again. "
        "floridacourtrecords.us serves the white-label template observed "
        "in this render: form#nameSearchForm, POST to "
        "https://floridacourtrecords.us/search/loading/, with "
        "input#firstName and input#lastName both required, an optional "
        "input#city, a hidden state field and the same bank of twelve "
        "unnamed record-type checkboxes (Felonies, Misdemeanors, "
        "Incarcerations, Arrests & Warrants, Bankruptcies, Judgments, Tax "
        "Liens, Property Liens, Contract Disputes, Traffic Offenses, "
        "Small Claims), under the heading 'Instant Access to Civil and "
        "Criminal Court Records'. That is the identical surface already "
        "driven to its end on californiacourtrecords-us and confirmed on "
        "colorado-, connecticut- and delawarecourtrecords-us, where the "
        "network's own FCRA notice states the wall: only a preliminary "
        "people search runs, and records are 'made available after you "
        "register for an account or purchase a report'. See "
        "californiacourtrecords-us for the full write-up."
    ),
    "georgiacourtrecords-us": (
        "Verified 2026-09-25. Page-for-page identical to "
        "floridacourtrecords-us and the rest of the CourtRecords.us "
        "network: the same form#nameSearchForm POSTing to "
        "/search/loading/ with required #firstName and #lastName, "
        "optional #city, a hidden state and twelve unnamed record-type "
        "checkboxes, observed in this render, behind the same "
        "registration paywall proven on californiacourtrecords-us. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to "
        "Georgia."
    ),
    "hawaiicourtrecords-us": (
        "Verified 2026-09-25. Page-for-page identical to "
        "floridacourtrecords-us and georgiacourtrecords-us -- same "
        "form#nameSearchForm POSTing to /search/loading/, same required "
        "#firstName / #lastName, optional #city, hidden state and twelve "
        "unnamed record-type checkboxes, observed in this render -- and "
        "behind the same registration paywall proven on "
        "californiacourtrecords-us. See that entry for the full write-up."
    ),
    "idcrawl-com": (
        "BLOCKED AT THE EDGE ON EVERY VISIT, verified 2026-09-25 by "
        "browser render with an ordinary desktop Chrome user agent. "
        "https://www.idcrawl.com/ answers HTTP 202 -- not 403 -- with a "
        "thirteen-character body whose entire text is '403 Forbidden' and "
        "whose title is '403 Forbidden'. No form, no input, no captcha "
        "widget and no challenge page: the origin is never reached, so "
        "this is a WAF denial dressed as a success status, and the 202 is "
        "worth recording because a status-code-only check would score "
        "this page as fine. The broker is real (IDCrawl aggregates social "
        "profiles and public records and does publish a name/username "
        "search), but none of it is reachable from here. Both legs of "
        "this row are walled the same way; see the opt-out entry."
    ),
    "inmatessearcher-com": (
        "BLOCKED BY CLOUDFLARE TURNSTILE, verified 2026-09-25 by browser "
        "render. The lookup itself is transcribed and is otherwise "
        "simple: form#email-form (class 'landing-search-form', an Angular "
        "form) GETs https://www.inmatessearcher.com/name/landing with "
        "input#First-Name (name=First-Name, placeholder 'Enter person's "
        "First Name'), input#Last-Name (name=Last-Name), select#field "
        "(name=field, 53 options) and a submit button 'FREE SEARCH'; an "
        "off-layout input#privacy-agree-checkbox sits outside the form. "
        "The wall is a child frame the site serves itself -- /assets/comm "
        "on/captcha/turnstile.html?sitekey=0x4AAAAAAAGIDOWi3uKHrRJK -- "
        "which loads "
        "challenges.cloudflare.com/turnstile/v0/api.js?render=explicit "
        "and holds the hidden cf-turnstile-response input. It is present "
        "on the landing page before any search is attempted, and it is "
        "present again on the dataset's recorded opt-out URL, which "
        "serves this same page. Operated by Truth Now LLC (3746 Foothill "
        "Blvd #7060, Glendale CA), same operator as sealedrecords.net."
    ),
    "idahocourtrecords-us": (
        "Verified 2026-09-25 by browser render. Page-for-page identical "
        "to floridacourtrecords-us / georgiacourtrecords-us / "
        "hawaiicourtrecords-us and the rest of the CourtRecords.us "
        "network, observed in this render: form#nameSearchForm POSTing to "
        "https://idahocourtrecords.us/search/loading/ with required "
        "input#firstName (name=firstName, label 'First Name:'), required "
        "input#lastName (name=lastName, 'Last Name:'), optional "
        "input#city (name=city, 'City:'), a hidden input#state "
        "(name=state) carrying the state, twelve unnamed name=check "
        "checkboxes for the record types the page advertises (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offenses and the rest), and a single input[type=submit] "
        "'Submit'. No captcha script and no captcha widget on the landing "
        "page. Blocked, not undecided, for the reason proven on "
        "californiacourtrecords-us: the POST leads to the network's "
        "registration paywall, so no free response can answer presence. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to Idaho. "
        "See californiacourtrecords-us for the full write-up."
    ),
    "illinoiscourtrecords-us": (
        "Verified 2026-09-25 by browser render. Page-for-page identical "
        "to floridacourtrecords-us / georgiacourtrecords-us / "
        "hawaiicourtrecords-us and the rest of the CourtRecords.us "
        "network, observed in this render: form#nameSearchForm POSTing to "
        "https://illinoiscourtrecords.us/search/loading/ with required "
        "input#firstName (name=firstName, label 'First Name:'), required "
        "input#lastName (name=lastName, 'Last Name:'), optional "
        "input#city (name=city, 'City:'), a hidden input#state "
        "(name=state) carrying the state, twelve unnamed name=check "
        "checkboxes for the record types the page advertises (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offenses and the rest), and a single input[type=submit] "
        "'Submit'. No captcha script and no captcha widget on the landing "
        "page. Blocked, not undecided, for the reason proven on "
        "californiacourtrecords-us: the POST leads to the network's "
        "registration paywall, so no free response can answer presence. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to "
        "Illinois. See californiacourtrecords-us for the full write-up."
    ),
    "indianacourtrecords-us": (
        "Verified 2026-09-25 by browser render. Page-for-page identical "
        "to floridacourtrecords-us / georgiacourtrecords-us / "
        "hawaiicourtrecords-us and the rest of the CourtRecords.us "
        "network, observed in this render: form#nameSearchForm POSTing to "
        "https://indianacourtrecords.us/search/loading/ with required "
        "input#firstName (name=firstName, label 'First Name:'), required "
        "input#lastName (name=lastName, 'Last Name:'), optional "
        "input#city (name=city, 'City:'), a hidden input#state "
        "(name=state) carrying the state, twelve unnamed name=check "
        "checkboxes for the record types the page advertises (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offenses and the rest), and a single input[type=submit] "
        "'Submit'. No captcha script and no captcha widget on the landing "
        "page. Blocked, not undecided, for the reason proven on "
        "californiacourtrecords-us: the POST leads to the network's "
        "registration paywall, so no free response can answer presence. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to "
        "Indiana. See californiacourtrecords-us for the full write-up."
    ),
    "iowacourtrecords-us": (
        "Verified 2026-09-25 by browser render. Page-for-page identical "
        "to floridacourtrecords-us / georgiacourtrecords-us / "
        "hawaiicourtrecords-us and the rest of the CourtRecords.us "
        "network, observed in this render: form#nameSearchForm POSTing to "
        "https://iowacourtrecords.us/search/loading/ with required "
        "input#firstName (name=firstName, label 'First Name:'), required "
        "input#lastName (name=lastName, 'Last Name:'), optional "
        "input#city (name=city, 'City:'), a hidden input#state "
        "(name=state) carrying the state, twelve unnamed name=check "
        "checkboxes for the record types the page advertises (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offenses and the rest), and a single input[type=submit] "
        "'Submit'. No captcha script and no captcha widget on the landing "
        "page. Blocked, not undecided, for the reason proven on "
        "californiacourtrecords-us: the POST leads to the network's "
        "registration paywall, so no free response can answer presence. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to Iowa. "
        "See californiacourtrecords-us for the full write-up."
    ),
    "kansascourtrecords-us": (
        "Verified 2026-09-25 by browser render. Page-for-page identical "
        "to floridacourtrecords-us / georgiacourtrecords-us / "
        "hawaiicourtrecords-us and the rest of the CourtRecords.us "
        "network, observed in this render: form#nameSearchForm POSTing to "
        "https://kansascourtrecords.us/search/loading/ with required "
        "input#firstName (name=firstName, label 'First Name:'), required "
        "input#lastName (name=lastName, 'Last Name:'), optional "
        "input#city (name=city, 'City:'), a hidden input#state "
        "(name=state) carrying the state, twelve unnamed name=check "
        "checkboxes for the record types the page advertises (Felonies, "
        "Misdemeanors, Incarcerations, Arrests & Warrants, Bankruptcies, "
        "Judgments, Tax Liens, Property Liens, Contract Disputes, Traffic "
        "Offenses and the rest), and a single input[type=submit] "
        "'Submit'. No captcha script and no captcha widget on the landing "
        "page. Blocked, not undecided, for the reason proven on "
        "californiacourtrecords-us: the POST leads to the network's "
        "registration paywall, so no free response can answer presence. "
        "Recorded separately only because the dataset lists the fifty "
        "network sites as fifty rows; nothing here is specific to Kansas. "
        "See californiacourtrecords-us for the full write-up."
    ),
    "kentuckycourtrecords-us": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Blocked by the same registration paywall as the rest of the "
        "CourtRecords.us network, and confirmed to be the SAME surface "
        "rather than merely a similar one: kentuckycourtrecords.us "
        "carries the identical #nameSearchForm posting to "
        "/search/loading/ with the same controls in the same order -- "
        "firstName (required), lastName (required), city, a hidden state "
        "prefilled 'KY', twelve off-layout checkboxes all named 'check', "
        "and the Submit input. Same TrustArc consent overlay, same "
        "trustarc-lang-select. See californiacourtrecords-us for the full "
        "write-up of driving that flow to its end and getting search "
        "theatre: an abbreviated name and a list of courts, no results, "
        "with records gated behind account registration or a report "
        "purchase. Dismiss the TrustArc overlay first if rechecking."
    ),
    "louisianacourtrecords-us": (
        "Verified by browser render (Playwright, 12s settle) 2026-09-25. "
        "Blocked by the same registration paywall as the rest of the "
        "CourtRecords.us network, and confirmed to be the SAME surface "
        "rather than merely a similar one: louisianacourtrecords.us "
        "carries the identical #nameSearchForm posting to "
        "/search/loading/ with the same controls in the same order -- "
        "firstName (required), lastName (required), city, a hidden state "
        "prefilled 'LA', twelve off-layout checkboxes all named 'check', "
        "and the Submit input. Same TrustArc consent overlay, same "
        "trustarc-lang-select. See californiacourtrecords-us for the full "
        "write-up of driving that flow to its end and getting search "
        "theatre: an abbreviated name and a list of courts, no results, "
        "with records gated behind account registration or a report "
        "purchase. Dismiss the TrustArc overlay first if rechecking."
    ),
    "mainecourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: mainecourtrecords.us "
        "carries the identical #nameSearchForm posting to "
        "/search/loading/ with the same controls in the same order -- "
        "firstName (required), lastName (required), city, a hidden state "
        "prefilled 'ME', twelve off-layout checkboxes all named 'check', "
        "and the Submit input. Same TrustArc consent overlay, same "
        "trustarc-lang-select, page title 'Maine Court Records | "
        "MaineCourtRecords.us'. Field-by-field signature compared against "
        "the other seven M-state siblings probed in the same batch and "
        "against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "marylandcourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "marylandcourtrecords.us carries the identical #nameSearchForm "
        "posting to /search/loading/ with the same controls in the same "
        "order -- firstName (required), lastName (required), city, a "
        "hidden state prefilled 'MD', twelve off-layout checkboxes all "
        "named 'check', and the Submit input. Same TrustArc consent "
        "overlay, same trustarc-lang-select, page title 'Maryland Court "
        "Records | MarylandCourtRecords.us'. Field-by-field signature "
        "compared against the other seven M-state siblings probed in the "
        "same batch and against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "massachusettscourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "massachusettscourtrecords.us carries the identical "
        "#nameSearchForm posting to /search/loading/ with the same "
        "controls in the same order -- firstName (required), lastName "
        "(required), city, a hidden state prefilled 'MA', twelve off- "
        "layout checkboxes all named 'check', and the Submit input. Same "
        "TrustArc consent overlay, same trustarc-lang-select, page title "
        "'Massachusetts Court Records | MassachusettsCourtRecords.us'. "
        "Field-by-field signature compared against the other seven "
        "M-state siblings probed in the same batch and against "
        "louisianacourtrecords-us: identical. See californiacourtrecords- "
        "us for the full write-up of driving that flow to its end and "
        "getting search theatre: an abbreviated name and a list of "
        "courts, no results, with records gated behind account "
        "registration or a report purchase. Dismiss the TrustArc overlay "
        "first if rechecking."
    ),
    "michigancourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "michigancourtrecords.us carries the identical #nameSearchForm "
        "posting to /search/loading/ with the same controls in the same "
        "order -- firstName (required), lastName (required), city, a "
        "hidden state prefilled 'MI', twelve off-layout checkboxes all "
        "named 'check', and the Submit input. Same TrustArc consent "
        "overlay, same trustarc-lang-select, page title 'Michigan Court "
        "Records | MichiganCourtRecords.us'. Field-by-field signature "
        "compared against the other seven M-state siblings probed in the "
        "same batch and against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "minnesotacourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "minnesotacourtrecords.us carries the identical #nameSearchForm "
        "posting to /search/loading/ with the same controls in the same "
        "order -- firstName (required), lastName (required), city, a "
        "hidden state prefilled 'MN', twelve off-layout checkboxes all "
        "named 'check', and the Submit input. Same TrustArc consent "
        "overlay, same trustarc-lang-select, page title 'Minnesota Court "
        "Records | MinnesotaCourtRecords.us'. Field-by-field signature "
        "compared against the other seven M-state siblings probed in the "
        "same batch and against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "mississippicourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "mississippicourtrecords.us carries the identical #nameSearchForm "
        "posting to /search/loading/ with the same controls in the same "
        "order -- firstName (required), lastName (required), city, a "
        "hidden state prefilled 'MS', twelve off-layout checkboxes all "
        "named 'check', and the Submit input. Same TrustArc consent "
        "overlay, same trustarc-lang-select, page title 'Mississippi "
        "Court Records | MississippiCourtRecords.us'. Field-by-field "
        "signature compared against the other seven M-state siblings "
        "probed in the same batch and against louisianacourtrecords-us: "
        "identical. See californiacourtrecords-us for the full write-up "
        "of driving that flow to its end and getting search theatre: an "
        "abbreviated name and a list of courts, no results, with records "
        "gated behind account registration or a report purchase. Dismiss "
        "the TrustArc overlay first if rechecking."
    ),
    "missouricourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: "
        "missouricourtrecords.us carries the identical #nameSearchForm "
        "posting to /search/loading/ with the same controls in the same "
        "order -- firstName (required), lastName (required), city, a "
        "hidden state prefilled 'MO', twelve off-layout checkboxes all "
        "named 'check', and the Submit input. Same TrustArc consent "
        "overlay, same trustarc-lang-select, page title 'Missouri Court "
        "Records | MissouriCourtRecords.us'. Field-by-field signature "
        "compared against the other seven M-state siblings probed in the "
        "same batch and against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "montanacourtrecords-us": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. Blocked by the same registration paywall as the rest "
        "of the CourtRecords.us network, and confirmed to be the SAME "
        "surface rather than merely a similar one: montanacourtrecords.us "
        "carries the identical #nameSearchForm posting to "
        "/search/loading/ with the same controls in the same order -- "
        "firstName (required), lastName (required), city, a hidden state "
        "prefilled 'MT', twelve off-layout checkboxes all named 'check', "
        "and the Submit input. Same TrustArc consent overlay, same "
        "trustarc-lang-select, page title 'Montana Court Records | "
        "MontanaCourtRecords.us'. Field-by-field signature compared "
        "against the other seven M-state siblings probed in the same "
        "batch and against louisianacourtrecords-us: identical. See "
        "californiacourtrecords-us for the full write-up of driving that "
        "flow to its end and getting search theatre: an abbreviated name "
        "and a list of courts, no results, with records gated behind "
        "account registration or a report purchase. Dismiss the TrustArc "
        "overlay first if rechecking."
    ),
    "multimedialists-com": (
        "Verified by browser render 2026-09-25, twice, at two different "
        "paths: multimedialists.com/ and multimedialists.com/privacy- "
        "policy/ BOTH return HTTP 403 and Cloudflare's hard block page -- "
        "title 'Attention Required! | Cloudflare', body 'Sorry, you have "
        "been blocked. You are unable to access secureservercdn2.net' "
        "with a #cf-footer-ip-reveal button. This is the WAF's terminal "
        "block, not the 'Just a moment...' managed challenge that "
        "namesandfacts-com serves, so there is no challenge to solve and "
        "no fresh Ray ID to retry for: the origin (a GoDaddy "
        "secureservercdn2.net host) refuses this client outright. Nothing "
        "on either leg can be read from this network position. Whether a "
        "search surface exists is therefore unknown, and 'no surface' "
        "would be an unearned claim."
    ),
    "namesandfacts-com": (
        "Verified by browser render 2026-09-25, THREE times across two "
        "paths: namesandfacts.com/ and namesandfacts.com/do-not-sell-my- "
        "info both return HTTP 403 with Cloudflare's managed-challenge "
        "interstitial -- title 'Just a moment...', 265-character body "
        "reading 'Performing security verification', a hidden "
        "input[name=cf-turnstile-response] whose widget id is minted per "
        "load (cf-chl-widget-y41ou, -odzrt) and the Turnstile loader chal "
        "lenges.cloudflare.com/turnstile/v0/b/d76008a69eab/api.js?render= "
        "explicit. Three distinct Ray IDs (a40d05dc8982f1aa, "
        "a40d0d06680df1aa, a40d1281fb39f1aa), including one retry at 22s "
        "settle specifically to let the challenge clear, and the "
        "interstitial never yielded to the real page. The dataset notes "
        "for this row already record that the opt-out page 403s to curl "
        "and was only ever confirmed in a real human browser; that holds "
        "for the search leg too. This is the wall, named: Cloudflare "
        "Turnstile managed challenge on every visit."
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
    # --- batch 23 of 2026-09-24: C sweep ---------------------------------
    "clustrmaps-com": (
        "Undecided as of 2026-09-24. clustrmaps.com refused the TCP "
        "connection on three separate attempts (plain HTTP tried as well "
        "as HTTPS, per the standing rule about not calling a host dead "
        "from one scheme); no page, no status line, no TLS handshake.\n"
        "\n"
        "This is undecided rather than blocked because a refused "
        "connection tells us nothing about whether a search surface "
        "exists. It is not a bot check, a 403 or a paywall -- it is an "
        "absent listener, which could equally be an outage, a "
        "geo/ASN-level drop of this host, or a site that is gone. The "
        "difference matters: BLOCKED asserts we saw a wall, and we did "
        "not.\n"
        "\n"
        "What the dataset asserts, for whoever rechecks: ClustrMaps "
        "publishes name- and address-keyed residence records, so a search "
        "surface is very likely. To resolve: retry from the deployment "
        "host. If it answers there, this row and the opt-out leg both "
        "resolve in one pass; if it refuses there too, the next question "
        "is whether the domain still resolves and serves anyone at all, "
        "which would be a dataset defect rather than a mapping verdict."
    ),
    # --- batch 21 of 2026-09-24: A-B sweep, marketing / people-search ----
    #
    # This batch originally left TWO rows undecided for the same reason:
    # the surface was found and its selectors recorded, but the result
    # page was not reached, so there was no success marker. The recheck
    # was run on 2026-09-24 and settled one of them --
    # backgroundcheckers-net moved to SEARCH_BLOCKED once Cloudflare
    # Turnstile was confirmed on its search route. blockshopper-com
    # remains here because its question is different in kind.
    "blockshopper-com": (
        "NO VERDICT 2026-09-24. A search surface exists and was rendered, "
        "but it is not obviously the right KIND of search, which is why no "
        "verdict is recorded either way.\n"
        "\n"
        "OBSERVED on blockshopper.com: two forms, both GET /search, "
        "carrying input[name='q'] with the placeholder 'Search for Homes "
        "by Address, City or Zip' and a type selector (Places). So the "
        "public entry point is keyed to a PROPERTY, not to a person.\n"
        "\n"
        "That matters for this module's purpose. BlockShopper's product is "
        "publishing home-purchase records under the buyers' NAMES -- that "
        "is why it is filed as people-search and why it appears on "
        "removal lists -- but the search box offered to the public asks "
        "for an address. A recipe could technically drive it: "
        "resolve_fields supplies street, city and zip. What is unknown is "
        "whether the result page then surfaces the person, which is the "
        "only thing that would make this a self-lookup rather than a real "
        "estate search.\n"
        "\n"
        "Not filed as no-surface because a surface plainly exists, and not "
        "filed as a recipe because what it returns was never seen. To "
        "resolve: run one address search for a profile with a known "
        "listing and record whether a name appears in the result, plus the "
        "result-page markers. If it does, this becomes an address-keyed "
        "search recipe -- which would be the first of its kind in this "
        "module and worth flagging as a shape the search leg does not yet "
        "model.\n"
        "\n"
        "Related: the opt-out leg is NO_OPTOUT_SURFACE (mailbox-only), and "
        "the removal email is documented as wanting the URL of the "
        "listing. That URL can only come from this search, so the two legs "
        "are coupled on this row -- resolving this one is worth more than "
        "it looks."
    ),
    # --- batch 20 of 2026-09-24: adtech / B2B list / identity vendors -----
    #
    # All three rows here are the SAME situation, and it is the honest one:
    # the site could not be reached, so nothing about its search surface was
    # observed. These are not "no surface" -- calling a host surfaceless
    # because it refused us would put a guess in the dict that reads like a
    # finding. Each is also in optout_forms.OPTOUT_UNDECIDED and on the
    # UNREACHABLE defects list; a recheck from a different network will
    # settle both legs at once.
    "m1-data-com": (
        "NO VERDICT 2026-09-24. The site was never rendered: it returns a "
        "Cloudflare 403 block page naming 'secureservercdn2.net'. Nothing "
        "about a search surface was observed, so no verdict is recorded on "
        "either direction.\n"
        "\n"
        "Worth knowing before anyone rechecks: this is the SAME GoDaddy-CDN "
        "block page that refused lizdev-com in batch 19. Two unrelated "
        "brokers hitting one shared-hosting edge rule is much better "
        "explained by that edge rule than by either broker, so both rows "
        "should be rechecked together, in one pass, from a different "
        "network -- not chased separately.\n"
        "\n"
        "To resolve: recheck from the deployment host. If the site renders, "
        "look for a consumer lookup and reclassify this and the opt-out leg."
    ),
    "marketops-com": (
        "NO VERDICT 2026-09-24. The host refused the connection "
        "(ERR_CONNECTION_REFUSED) on two separate attempts. Nothing about a "
        "search surface was observed.\n"
        "\n"
        "Refused is not the same as timed out and not the same as a DNS "
        "failure: the name resolved and something answered at the TCP "
        "layer with a rejection, which is consistent with a decommissioned "
        "service on a live name, with an origin that is down, or with an "
        "edge that drops non-browser clients. It does not establish that "
        "the company is gone. See optout_forms for the same note.\n"
        "\n"
        "To resolve: recheck from the deployment host, and try plain HTTP "
        "as well as HTTPS -- the listsonline/Everleads row in batch 19 was "
        "a live broker hiding behind a failure on the HTTPS path alone."
    ),
    "matchandappend-com": (
        "NO VERDICT 2026-09-24. The request timed out on two separate "
        "attempts -- no response at all, not a rejection. Nothing about a "
        "search surface was observed.\n"
        "\n"
        "To resolve: recheck from the deployment host, and try plain HTTP "
        "as well as HTTPS. A timeout is the weakest of the three failure "
        "signatures: it is equally consistent with a dead host and with an "
        "edge silently dropping an automated client, so it justifies no "
        "inference about the company at all."
    ),
    # --- batch 19 of 2026-09-24: list brokers / B2B data / skip tracing ---
    "lusha-com": (
        "NO VERDICT 2026-09-24, and it is the row in batch 19 most likely "
        "to have a real search leg. Lusha's footer links 'Do Not Sell My "
        "Info' to www.lusha.com/privacy_topic/control-your-profile/ -- a "
        "page whose name promises exactly the self-lookup this leg is "
        "about, and which was NOT rendered this pass. Reading it resolves "
        "this row.\n"
        "\n"
        "Two things to carry into that reading. First, Lusha sells B2B "
        "contact data scraped and inferred from the open web, so a profile "
        "on a given person very plausibly exists, and a 'control your "
        "profile' flow is the industry's usual answer -- compare LeadIQ's "
        "'Claim My Profile', already open under this same dict. Second, and "
        "the reason to be careful: claiming or viewing a profile normally "
        "requires handing over the email address the profile is keyed to, "
        "and on a contact-data vendor that VERIFIES a record the company "
        "was previously only guessing at. Use a disposable address and "
        "prefer a route that reads without claiming.\n"
        "\n"
        "The opt-out leg of this row is separately worth reading -- see "
        "optout_forms, where the 'Request Removal' page turns out to lead "
        "with a sales-contact form."
    ),
    "limeleads-com": (
        "NO VERDICT 2026-09-24: limeleads.com serves WP Engine's 'Site Not "
        "Configured' page on every path tried -- the apex and the recorded "
        "opt-out path alike -- so no surface of any kind was observed and "
        "none can be described. UNREACHABLE for the defects list; see "
        "optout_forms for why a hosting-level 'not configured' is a "
        "different animal from a 404 and from a DNS failure.\n"
        "\n"
        "A category expectation, offered as an expectation and not as a "
        "finding: LimeLeads sold B2B contact lists by subscription, and no "
        "wholesale list vendor in this sweep has yet offered a free "
        "consumer self-lookup. That is a reason to think the answer will be "
        "no, not a reason to record one."
    ),
    "lizdev-com": (
        "NO VERDICT 2026-09-24: lizdev.com answered HTTP 403 from "
        "Cloudflare ('Sorry, you have been blocked ... You are unable to "
        "access secureservercdn2.net') and no content was retrieved, so "
        "neither leg could be read. UNREACHABLE for the defects list.\n"
        "\n"
        "The block page names GoDaddy's CDN rather than the site itself, "
        "which suggests a shared-hosting edge rule against this client "
        "rather than a refusal aimed at the public. Recheck from the "
        "deployment host before concluding anything; see optout_forms for "
        "the fuller account."
    ),
    "lookify-io": (
        "NO VERDICT 2026-09-24: www.lookify.io/opt-out never rendered -- "
        "Cloudflare Turnstile's managed-challenge interstitial answered "
        "instead ('Just a moment...', HTTP 403, a __cf_chl_rt_tk token on "
        "the URL). Nothing behind the wall was observed, so nothing can be "
        "said about a lookup surface.\n"
        "\n"
        "Worth a recheck rather than an assumption: the name is a lookup "
        "name, and the dataset says the web form is the ONLY channel this "
        "company honours. Good candidate for the deployment-host recheck "
        "pass."
    ),
    # --- batch 18 of 2026-09-24: lead-generation / B2B contact vendors ---
    "leadiq-com": (
        "NO VERDICT 2026-09-24, and it is the one row in batch 18 where a "
        "search leg plausibly EXISTS. privacy.leadiq.com offers 'Claim My "
        "Profile' -- 'If you've become aware that LeadIQ has a professional "
        "profile on you, you can claim that profile' -- alongside 'Request "
        "Access', described as giving 'a look at what' is held. Either could "
        "function as a self-lookup.\n"
        "\n"
        "Neither was exercised, deliberately. Both routes are gated behind "
        "embedded Typeforms that render one question at a time and exposed "
        "no fields to the probe, and both require handing a real email "
        "address to a B2B contact vendor -- and in the claim case, plausibly "
        "creating an account. That is not a read, and it is exactly the "
        "situation already recorded at listmatch-com: an address given to a "
        "contact-data company to test a form is an address they now have, "
        "and claiming a profile gives them a VERIFIED identity for someone "
        "whose reason for being there was to be less exposed. Whoever takes "
        "this should use a disposable address and run it both ways, and "
        "should prefer 'Request Access' over 'Claim My Profile' for exactly "
        "that reason."
    ),
    "leadsmarket-com": (
        "NO VERDICT 2026-09-24: www.leadsmarket.com did not respond -- two "
        "attempts, both 30-second navigation timeouts with no response at "
        "all. Nothing can be said about any surface. UNREACHABLE for the "
        "defects list; see the optout_forms entry for why two timeouts from "
        "one network still are not proof the host is gone."
    ),
    "leadershipconnect-io": (
        "NO VERDICT 2026-09-24: leadershipconnect.io answers HTTP 403 with "
        "no content, on both the dataset path and /opt-out, so neither leg "
        "could be read. UNREACHABLE for the defects list. Likeliest cause is "
        "an edge rule refusing a headless datacenter client; recheck from "
        "the deployment host, as with thatsthem-com."
    ),
    "l2political-com": (
        "NO VERDICT 2026-09-24: the domain serves, but both the dataset's "
        "recorded rights URL and /privacy-policy/ return HTTP 404, and no "
        "working page was rendered, so no statement about a lookup surface "
        "is supported. Dataset defect flagged in optout_forms, not fixed.\n"
        "\n"
        "Worth a proper look rather than a write-off: L2 is a commercial "
        "voter-file vendor, and voter-file products DO often carry a "
        "self-lookup, since the underlying registration data is public "
        "record. If one exists here it would be a genuine search leg."
    ),
    "keyopinionleaders-com": (
        "NO VERDICT 2026-09-24: www.keyopinionleaders.com does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED), so there is no host to ask. "
        "UNREACHABLE for the defects list."
    ),
    "outlastdfs-com": (
        "NO VERDICT 2026-09-24: outlastdfs.com does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list, and "
        "corroborated independently by the hard-bounced mail recorded in the "
        "dataset a month earlier -- see optout_forms for why this is still "
        "filed as undecided rather than as absence."
    ),
    "lifesight-io": (
        "NO VERDICT 2026-09-24: lifesight.io/opt-out/ was rendered twice and "
        "its body text never settled -- under 900 characters both times, all "
        "of it nav, cookie banner and a rotating promo ticker. The only form "
        "on the page is a HubSpot newsletter signup whose button reads "
        "'Subscribe Now'. Nothing about a lookup surface can be concluded "
        "from a page whose content did not render. See the optout_forms "
        "entry, which is the fuller write-up of this row and of the "
        "marketing-signup hazard it illustrates."
    ),
    "getivydata-com": (
        "NO VERDICT 2026-09-23: getivydata.com does not resolve "
        "(ERR_NAME_NOT_RESOLVED), so nothing can be said about any surface. "
        "UNREACHABLE for the defects list."
    ),
    "kbmg-com": (
        "NO VERDICT 2026-09-23: www.kbmg.com does not resolve "
        "(ERR_NAME_NOT_RESOLVED). UNREACHABLE for the defects list; see "
        "optout_forms for why this one warrants a research pass rather than "
        "being written off as a dead domain."
    ),
    "jdmlistservices-com": (
        "NO VERDICT 2026-09-23: only the dataset's opt-out path was probed "
        "and it 404s on a live host. No search surface was looked for. "
        "DATASET NOTE: stale URL."
    ),
    "keymarketingadvantage-com": (
        "NO VERDICT 2026-09-23: only the dataset's opt-out path was probed "
        "and it 404s on a live host. DATASET NOTE: stale URL."
    ),
    "ipapi-co": (
        "NO VERDICT 2026-09-23: the probed page answers 403 behind Turnstile "
        "so nothing was read. Recorded here rather than as no-surface "
        "because ipapi is IP-geolocation -- any 'lookup' it offers is keyed "
        "to an IP address, which is a different question from a name-based "
        "presence check and is not answered by either bucket. See "
        "optout_forms for the identifier-shape problem."
    ),
    "innovis-com": (
        "NO VERDICT as of 2026-09-23, and deliberately NOT recorded as "
        "no-search-surface despite no form being captured. Innovis is a "
        "nationwide consumer reporting agency -- the fourth credit bureau "
        "-- and it absolutely holds a file on most US adults. It offers a "
        "consumer file disclosure, which is a presence check in every "
        "sense that matters, just not a public one: it is gated behind "
        "identity verification. So 'no search surface' would be false in "
        "substance while true about the probed page, and that distinction "
        "is the reason this entry exists. See optout_forms for why the "
        "opt-out leg is a category question rather than a mechanical one."
    ),
    "intellicorp-net": (
        "NO VERDICT as of 2026-09-23: the dataset's URL 404s and no other "
        "path was probed. A background-screening CRA, so any 'search' is a "
        "file disclosure gated behind identity verification rather than a "
        "public index -- same distinction as innovis-com above. DATASET "
        "NOTE: stale URL."
    ),
    "backgroundsonline-com": (
        "NO VERDICT as of 2026-09-23: only the site root was probed (the "
        "dataset gave no other path) and no form was captured, though "
        "reCAPTCHA loads. Background-screening CRA by name, so the same "
        "file-disclosure-not-public-index reading as innovis-com likely "
        "applies, but nothing was read to confirm it."
    ),
    "integratedmedicaldata-com": (
        "NO VERDICT 2026-09-23: the host does not resolve "
        "(ERR_NAME_NOT_RESOLVED), so nothing can be said about any "
        "surface. UNREACHABLE for the defects list; see optout_forms for "
        "why this particular dead domain deserves a research pass."
    ),
    # --- batch of 2026-09-23: hosts that could not be read at all ---
    #
    # Grouped because the reason is the same and it is a reason about the
    # ATTEMPT, not the broker: nothing was served, so nothing is known about
    # whether a search surface exists. Each is undecided on the opt-out leg
    # too, with the same evidence, recorded in optout_forms.
    "imprintanalytics-io": (
        "NO VERDICT 2026-09-23: TLS handshake fails outright "
        "(ERR_SSL_VERSION_OR_CIPHER_MISMATCH), so no HTTP request is ever "
        "made. Server misconfiguration on their side."
    ),
    "contacts411-com": (
        "NO VERDICT 2026-09-23: the host does not resolve in DNS "
        "(ERR_NAME_NOT_RESOLVED). The name suggests a consumer directory, "
        "which is exactly why no surface is being inferred from it."
    ),
    "trufactor-io": (
        "NO VERDICT 2026-09-23: connection to https://trufactor.io/ timed "
        "out after 30s with no response at all -- not a challenge page, "
        "not an error page, nothing. TruFactor was an SK Telecom-backed "
        "mobile-data venture; a dead host is a plausible end state, but a "
        "single timeout is not evidence of that and no such conclusion is "
        "recorded here. Retry before treating this row as anything."
    ),
    "privacycompliance-biz": (
        "NO VERDICT 2026-09-23: only the dataset's opt-out path was "
        "probed and it 404s. The host is live (it served a WordPress 404 "
        "template), so a root probe would tell us something; it has not "
        "been done. The site appears to be an opt-out PROCESSOR acting for "
        "DatabaseUSA rather than a data holder in its own right, in which "
        "case a presence search against it would be meaningless -- but "
        "that is an inference from a URL slug, not a finding."
    ),
    "infopay-com": (
        "NO VERDICT 2026-09-23: only /privacy was probed, which is prose "
        "with no form. InfoPay operates consumer-facing people-search "
        "brands under OTHER domains, so the search leg for this row may "
        "belong to those rows rather than this one. Needs the site root "
        "read before anything is concluded."
    ),
    "mlxp-com": (
        "NO VERDICT 2026-09-23: the homepage renders a cookie banner and "
        "two HubSpot iframes; no search surface was seen and no other "
        "path was probed, the dataset giving only the root URL."
    ),
    "privatereports-com": (
        "NO VERDICT as of 2026-09-23, and unusually the reason is not "
        "that the form is hard to read -- it is fully transcribed below -- "
        "but that it was reached from the wrong URL.\n"
        "\n"
        "What rendered: form#email-form, action https://www.privatereports"
        ".com/nameSearch/landingPage, with First-Name and Last-Name (text, "
        "name and id identical, labelled 'Enter First Name' / 'Enter Last "
        "Name'), a state <select name='state' id='state'> whose options "
        "begin 'Select States', 'All States' and then the states, and a "
        "submit button reading 'FREE SEARCH'. The form appears twice on "
        "the page with identical ids -- duplicate DOM ids, so a selector "
        "keyed on #First-Name matches two elements and a recipe must "
        "take the first, or scope to a container.\n"
        "\n"
        "Why no verdict: this page was served from the dataset's OPT-OUT "
        "URL (/optOut/name/landing), not from a search path, so it is not "
        "established that this is the site's general search surface rather "
        "than the first step of its removal flow. Those are the same "
        "form in shape and very different in meaning -- see the matching "
        "entry in optout_forms.OPTOUT_UNDECIDED. Writing a SEARCH recipe "
        "from a form that is actually the removal wizard's step one would "
        "make the presence check silently drive an opt-out flow. NEXT "
        "STEP: load the site's own homepage and compare; if the same form "
        "is there, this promotes cleanly.\n"
        "\n"
        "Also noted, not a blocker: the page carries an FCRA disclaimer "
        "and a 'please be cautious when using this tool' notice, but "
        "neither is a gate -- no modal, no I-AGREE button of the kind "
        "recorded against instantcheckmate-com below."
    ),
    "information-com": (
        "NO VERDICT as of 2026-09-23. The page reached (from the "
        "dataset's opt-out URL, which serves the search landing page -- "
        "see optout_forms) carries a search form posting to "
        "https://information.com/privacy-rights/ with firstName, lastName, "
        "city and a state <select>, plus a 'Search' submit.\n"
        "\n"
        "That action URL is the reason this is undecided and not a "
        "recipe. A SEARCH form whose action is /privacy-rights/ is not a "
        "search form in the ordinary sense; the same page also has two "
        "buttons posting to that identical URL reading 'REQUEST A COPY OF "
        "MY DATA' and 'DELETE MY USER DATA'. Whatever /privacy-rights/ "
        "does, it is a privacy-request endpoint, and submitting to it to "
        "answer 'does this broker hold Penn?' would be filing a request "
        "rather than running a query -- a side effect, from the leg of "
        "this tool that is supposed to have none. Not submitted.\n"
        "\n"
        "NEXT STEP: find whether information.com has a plain search "
        "endpoint distinct from /privacy-rights/, and if it does not, "
        "record this as no-search-surface with that reasoning rather than "
        "as a recipe."
    ),
    "publicinfoservices-com": (
        "NO VERDICT as of 2026-09-23. Only the privacy-requests page was "
        "probed (see optout_forms.OPTOUT_BLOCKED for what it contains and "
        "why the opt-out leg is blocked); the site advertises a 'Sample "
        "Report' and a login, so a search surface plausibly exists, but "
        "none was reached and none is described here. Recorded so the "
        "search leg is not silently treated as answered by the opt-out "
        "probe. NEXT STEP: probe the site root.\n"
        "\n"
        "Carry forward from the opt-out side: that page randomises at "
        "least one field's name per render, so if the search form does "
        "the same, it cannot be pinned by name either."
    ),
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
    "listmatch-com": (
        "NO VERDICT as of 2026-09-23, and it is the rare case where a "
        "search leg was found somewhere other than a product page. "
        "listmatch.com/privacy/ carries a self-lookup: "
        "'Check/Manage/Delete your Data Record', a single "
        "input[name='dataaddress'] taking an email address, and the page "
        "states its purpose -- 'Completing the form bellow will give you "
        "the option to view your consumer data record and have your "
        "record deleted' [sic].  So a person CAN look themselves up here. "
        "What is missing is the only thing that would make it a recipe: "
        "this leg requires verifying a hit response and a no-results "
        "response against each other, and both require submitting a real "
        "email address to a list broker. That is not a read, and it is "
        "not this pass's to do unilaterally -- an address handed to an "
        "email-list vendor to test a form is an address they now have. "
        "For whoever takes it: the form GETs to "
        "index.php?action=checkemail, and the two traps are recorded "
        "under the opt-out entry -- a HIDDEN input[name='email'] sitting "
        "beside the visible one (a honeypot, and the name collision is "
        "vicious), and an 'isca' checkbox for California residency. Use a "
        "disposable address, run it twice (a known-present one and "
        "nonsense), and record both markers."
    ),
    "famousbirthdays-com": (
        "NO VERDICT as of 2026-09-23, and this one is a CODEBASE GAP "
        "rather than a research gap: the search was verified live, both "
        "ways, and then could not be shipped.  What was verified. The "
        "homepage form #fb_search takes a single input[name='q']. 'Taylor "
        "Swift' navigated to /people/taylor-swift.html showing the name "
        "beside 'Birthday', 'Birth Sign' and 'Birthplace'. 'Qzxjv "
        "Wrrblfnd' navigated to /notfound/ printing 'Qzxjv Wrrblfnd "
        "wasn't found'. No interstitial, no bot check on either path, no "
        "printed result count. A hit goes STRAIGHT TO THE PROFILE rather "
        "than to a result list, so the hit markers have to be the profile "
        "page's own furniture ('birth sign', 'birthplace') and the miss "
        "marker is the invariant tail of a query-interpolated sentence "
        "('wasn't found'). That is a complete, working set of markers and "
        "it should not have to be rediscovered.  WHY IT IS NOT IN "
        "RECIPES. The form's only submit control is <button type='submit' "
        "class='search-submit' aria-label='Search'> and it measures ZERO "
        "PIXELS WIDE (39.1 high, 0 wide -- an icon button whose glyph is "
        "a background image). Playwright will not click a zero-area "
        "element, so search_probe's page.click(submit_selector) times out "
        "after 30 seconds. The form submits perfectly well on Enter, "
        "which is how the verification above was done, but SearchRecipe "
        "has no way to say 'press Enter' -- it has submit_selector and "
        "nothing else, and run_search always clicks it. A recipe was "
        "written, run through run_search, and failed both ways on 'could "
        "not submit the search', which is exactly the honest outcome the "
        "allow-list is meant to prevent shipping.  The fix is in the "
        "codebase, not here: either a force-click option or a submit_key "
        "field on SearchRecipe. Until one exists this broker cannot be "
        "checked. Recorded in full so that whoever adds it can turn this "
        "entry into a recipe in five minutes.  Two notes on what the "
        "answer would mean. Famous Birthdays is a celebrity and internet- "
        "personality database, so for almost every user the correct "
        "answer is 'not present' -- a real answer, not a failed lookup. "
        "And the submit-control confusion here is the THIRD in this sweep "
        "(checkpeople, convex): the probe reports a control's DOM .type, "
        "in which a <button type=submit> and an <input type=submit> are "
        "indistinguishable. Read the tag, never infer it."
    ),
    "date-detective-app": (
        "Reachability failure, 2026-09-25, and the cause is worth "
        "recording precisely because it is not an anti-bot wall and not a "
        "dead domain. Both https://date-detective.app/ and the host the "
        "dataset records for the opt-out, https://mobile.date- "
        "detective.app/, fail TLS from a real browser with "
        "net::ERR_CERT_COMMON_NAME_INVALID. Reading the certificates "
        "directly explains why: each host answers on 443 with an AZURE "
        "APP SERVICE DEFAULT WILDCARD -- CN=*.msha- "
        "slice-6-dm1-0-ase.p.azurewebsites.net for the apex and "
        "CN=*.msha-slice-6-wus2-1-ase.p.azurewebsites.net for mobile, "
        "both issued to Microsoft Corporation -- so the app is up but no "
        "custom hostname binding or managed certificate was ever attached "
        "to it. Same shape as the optout.prod.bidr.io row: a certificate "
        "whose name does not match the host, distinguished from a DNS "
        "failure. Nothing can be read through it, hence no verdict. The "
        "dataset also records that privacy@date-detective.app hard- "
        "bounced on 2026-08-21, so this row currently has no working "
        "channel at all; a recheck is cheap (one TLS handshake) and "
        "should be redone rather than re-researched."
    ),
    "deutschepost-de": (
        "NO VERDICT as of 2026-09-25, and the honest statement is "
        "'nothing could be read', not 'there is no lookup'. Every "
        "deutschepost.de path tried from a real browser answered HTTP 404 "
        "with Deutsche Post's own bare 'Not Found' body -- the apex "
        "https://www.deutschepost.de/ itself, /de/d.html, and "
        "/de/d/datenschutz.html -- and www.postdirekt.de redirected into "
        "/de/d/deutsche-post-direkt.html which 404s the same way. An apex "
        "that 404s while the domain plainly works for ordinary visitors "
        "reads as an edge rule against automated clients rather than a "
        "missing site, so no claim about what the site offers can be made "
        "from this pass. The broker is Deutsche Post Direkt GmbH, the "
        "group's address and geo-marketing arm; its recorded channel is "
        "the datenschutz@postdirekt.de mailbox. Recheck is one render of "
        "the apex."
    ),
    "fastbackgroundcheck-com": (
        "A REAL, UNWALLED SEARCH FORM, transcribed 2026-09-25 by browser "
        "render, and undecided only because the result page was never "
        "driven. https://www.fastbackgroundcheck.com/ serves three "
        "sibling forms; the name one is form#search-form-people, "
        "METHOD=GET with action on the site root, carrying input#search- "
        "input-name labelled 'Full Name' and input#search-input-address2 "
        "labelled 'City/State/Zip' (neither marked required), with "
        "sibling #search-form-phone and #search-form-address for the "
        "other two tabs. No captcha script and no captcha element "
        "anywhere on the page. What is missing to promote this to a "
        "recipe is only the response side: nobody has submitted a name "
        "and recorded the results host, the hit and no-hit markers or a "
        "count pattern, and the site's own opt-out notice shows it "
        "operates the same infrastructure family as fastpeoplesearch-com, "
        "whose results ARE free and readable. Whoever drives one search "
        "closes this row."
    ),
    "freepeopledirectory-com": (
        "A REAL, UNWALLED, URL-ADDRESSABLE-LOOKING SEARCH FORM, "
        "transcribed 2026-09-25 by browser render, which directly "
        "contradicts this row's dataset note. "
        "https://www.freepeopledirectory.com/ carries form#form- "
        "submit.name-form, METHOD=GET with action on the site root, "
        "holding input#fname 'First Name', input#lname 'Last Name' and "
        "input#address 'City & State' (name attribute address_data) plus "
        "button#submit-button 'SEARCH'. No captcha script, no captcha "
        "element. DATASET DEFECT: the row claims 'Results load behind a "
        "Spokeo-style city/state wizard, so not URL-addressable' -- there "
        "is no wizard on the entry page, just a three-field GET. Left "
        "undecided because the results page was not driven, so the "
        "results host, the hit and no-hit markers and any count pattern "
        "are all still unknown, and this is a Spokeo-network property "
        "whose results may well be gated the way Spokeo's are. The opt- "
        "out leg is already settled (NO_OPTOUT_SURFACE)."
    ),
    "gm-com": (
        "NO VERDICT as of 2026-09-25, and the reason is that the site "
        "could not be read at all. Both https://www.gm.com/ and the "
        "recorded https://www.gm.com/consumer-privacy answer HTTP 403 "
        "with an Akamai 'Access Denied' page ('You don't have permission "
        "to access ... on this server', Reference #18.54c90b17, "
        "errors.edgesuite.net). Two separate attempts, same result. GM is "
        "a vehicle manufacturer on the CA data-broker registry rather "
        "than a people-search site, so the honest expectation is that no "
        "consumer lookup exists -- but that is an expectation, not an "
        "observation, and this leg must not be recorded as no-surface on "
        "the strength of a page nobody has seen. Recheck from a different "
        "network egress."
    ),
    "governmentregistry-org": (
        "A REAL, UNWALLED SEARCH FORM, transcribed 2026-09-25 by browser "
        "render, and undecided only because the result page was never "
        "driven. https://www.governmentregistry.org/ (title 'Search "
        "Government Public Records') carries a METHOD=GET form on the "
        "site root with input#firstName 'First Name *' (required), "
        "input#lastName 'Last Name *' (required), input#city named town "
        "'City' (optional) and select#state 'State *' (required, 52 "
        "options, first is the 'State' placeholder and second is 'All "
        "States'), submitted by a button reading SEARCH. No captcha "
        "script and no captcha element on the page. What is unknown is "
        "what comes back: this is an Accucom / CIS Nationwide property "
        "that disclaims FCRA consumer-reporting-agency status, and the "
        "sibling people-search sites in this dataset routinely put a "
        "registration or purchase wall on the results, so the response "
        "side has to be observed before a recipe can claim hit and no-hit "
        "markers."
    ),
    "idstrong-com": (
        "A SEARCH SURFACE PLAINLY EXISTS and is fully transcribed below, "
        "but the flow past the first POST was not driven, so this is not "
        "a recipe. Verified 2026-09-25 by browser render of "
        "https://www.idstrong.com/. form#nameInputForm POSTs to "
        "https://www.idstrong.com/searching/name-loading/ and holds: "
        "hidden input[name=_csrf-frontend] (a Yii2 CSRF token, so any "
        "driver must GET the page first and carry it forward), hidden "
        "input[name=ltid], hidden input[name=mercSubId], hidden "
        "input[name=type], input#firstName (name=firstName, label 'FIRST "
        "NAME'), input#lastName (name=lastName, 'LAST NAME'), "
        "select[name=state] with 53 options, and button#submitBtn 'RUN "
        "FREE SCAN / Results in 30 Seconds'. A second form#emailFormHome "
        "POSTs to /searching/ with the same four hidden fields plus "
        "input#email. NO captcha script and NO captcha widget on the "
        "landing page -- the only loose control is a TrustArc language "
        "select. What is unresolved is what the POST yields: IDStrong is "
        "an identity-monitoring subscription (InfoPay, Inc., 227 Lewis "
        "Wharf, Boston MA, sibling of staterecords.org, recordsfinder.com "
        "and infotracer.com), the button promises a 'FREE SCAN', and "
        "whether a free response actually answers presence or only "
        "funnels to signup was not established -- resolving that is the "
        "next pass's one job. Left undecided rather than blocked "
        "precisely because nothing blocked the render."
    ),
    "menstoppingviolence-org": (
        "Verified by browser render (Playwright, 11-13s settle) "
        "2026-09-25. RECIPE-READY IN SUBSTANCE: a real, captcha-free "
        "people-search form, not staged only because its controls have no "
        "name attributes. www.menstoppingviolence.org/people/ is titled "
        "'People Search Directory - Find People by Name' and serves an "
        "A-Z surname directory plus the '200 most common names in the US' "
        "as seeded listings. Full transcription -- method GET, action "
        "back to /people/ (the same form also appears on the homepage as "
        "form#search-form): input[type=text] REQUIRED labelled 'First "
        "Name', id 'search-fname', NO name attribute; input[type=text] "
        "REQUIRED labelled 'Last Name', id 'search-lname', NO name "
        "attribute; button[type=submit] reading 'SEARCH'. That is the "
        "entire form. cap[] and widget[] are EMPTY at an 11s settle, "
        "there are no frames, no honeypot and no consent overlay -- "
        "genuinely nothing guarding it. WHY IT IS NOT STAGED: with no "
        "name attributes the query-string keys the search actually uses "
        "cannot be read off the DOM, so the submission would have to be "
        "driven by filling the two ids and clicking, and the result-page "
        "shape has not been characterised. THE BIGGER FINDING, which the "
        "next agent should not have to rediscover: this domain has been "
        "REPURPOSED. The homepage still carries the nonprofit's own "
        "closure notice -- 'the Men Stopping Violence (MSV) initiative "
        "has closed mid-2024 after over four decades of work to end male "
        "violence against women and girls' -- and the same page now also "
        "hosts the people-search form. A domain that used to belong to an "
        "anti-violence nonprofit is now running a people-search "
        "directory, which is why this row sits in a broker dataset at "
        "all. Note the asymmetry with the opt-out leg: the SEARCH pages "
        "are served unchallenged while /opt-out/ is behind a Cloudflare "
        "Turnstile."
    ),
    "microbilt-com": (
        "NO VERDICT as of 2026-09-25, deliberately not no-search-surface, "
        "same reasoning as innovis-com. MicroBilt is an alternative- "
        "credit consumer reporting agency under the FCRA and holds a file "
        "on a large share of US adults. Rendered "
        "www.microbilt.com/consumer-affairs at 12s settle: the only two "
        "forms on it are the site header search (input[name=key] -> "
        "/search, id=search-submit) and an empty 'Get a Demo' wrapper -- "
        "no public lookup of a named person exists. But the page's own "
        "copy offers 'Request your Consumer Report' with unlimited free "
        "copies of your own file, and that IS a presence check in "
        "substance; it is simply not a web surface. The channel is a "
        "downloadable PDF, MicroBilt-Consumer-Report-Request- "
        "Form-03-01-21-.pdf under /Cms_Data/Contents/Microbilt/Media/Docs "
        "/ConsumerAffair/0222_Update/, to be printed and mailed. So 'no "
        "search surface' would be true about the page and false about the "
        "broker, which is the distinction innovis-com exists to preserve. "
        "No captcha script and no captcha widget on the page."
    ),
    "mugshotlook-com": (
        "RECIPE-READY IN SUBSTANCE, filed here because this module cannot "
        "construct a real SearchRecipe object (confirmed by direct "
        "testing across batches). Verified by browser render 2026-09-25 "
        "at 12s settle. The dataset's opt_out_url "
        "(www.mugshotlook.com/optOut/name/landing) 301s to the SEARCH "
        "page, /nameSearch/landingPage -- see the opt-out leg for that "
        "defect -- and the search form transcribes as: form.advanced- "
        "search-form (Angular, class carries ng-untouched/ng-pristine), "
        "method GET, action /nameSearch/landingPage, rendered TWICE "
        "(desktop and mobile copies, identical ids); "
        "input[type=text][name=First-Name][id=First-Name] placeholder "
        "'Enter First Name', not marked required; "
        "input[type=text][name=Last-Name][id=Last-Name] placeholder "
        "'Enter Last Name', not marked required; "
        "select[name=state][id=state] with 53 options ('Select States', "
        "'All States', then the 50 states alphabetically); submit is "
        "button.search-button[type=submit] reading 'FREE SEARCH'. NO "
        "captcha script and NO captcha widget. THE ONE BLOCKER A RECIPE "
        "MUST HANDLE: an FCRA certification interstitial covers the page "
        "-- button.agree-btn 'I AGREE' plus a computed-invisible "
        "#privacy-agree-checkbox -- and it must be dismissed before the "
        "form is usable. Duplicate ids across the two form copies mean a "
        "recipe must select by form, not by id."
    ),
    "mugshots-com": (
        "RECIPE-READY IN SUBSTANCE, filed here because this module cannot "
        "construct a real SearchRecipe object. Verified by browser render "
        "2026-09-25: mugshots.com's search transcribes in full as "
        "form.site-search, method GET, action "
        "https://mugshots.com/search.html, with exactly two controls -- "
        "input[type=text][name=q].site-search-input, marked REQUIRED, no "
        "label or placeholder, and button.site-search-submit[type=submit] "
        "reading 'SEARCH'. No captcha script, no captcha widget, no "
        "consent interstitial, no login. TOOLING NOTE, and the reason "
        "this row needed a second pass: tools/probe_broker_forms.py's JS "
        "THREW on this page -- 'TypeError: Cannot read properties of "
        "undefined (reading trim)' from the links mapper's "
        "a.innerText.trim(), because mugshots.com has an SVG <a> and "
        "SVGAElement has no innerText. The whole page.evaluate aborted "
        "and the target was recorded as an error with zero DOM read. "
        "Worked around with a one-token patch to the same JS "
        "((a.innerText || a.textContent || '')) rather than a fork; the "
        "prober itself needs that guard."
    ),
    "myheritage-com": (
        "NO VERDICT as of 2026-09-25, and the honest statement is that "
        "the page would not render, not that a surface is or is not "
        "there. MyHeritage plainly operates a public historical-records "
        "search engine -- the dataset's own help-article URL is about "
        "removing yourself from it -- but two different search URLs "
        "(www.myheritage.com/research and /research/collection-10182/us- "
        "public-records-index) each returned HTTP 200 with a body "
        "innerText length of ZERO at 12s and 14s settle: no forms, no "
        "controls, no text, no captcha script, no captcha widget, no "
        "frames. An empty 200 that is neither a challenge page nor a "
        "redirect is a distinct shape from the Cloudflare walls elsewhere "
        "in this batch and is worth re-probing headed before anything is "
        "concluded. What a future pass needs: render headed, or find the "
        "surface under a path that serves server-side HTML."
    ),
    "nctue-com": (
        "NO VERDICT as of 2026-09-25, deliberately not no-search-surface, "
        "same reasoning as innovis-com. NCTUE is an FCRA-regulated "
        "telecom and utility payment data exchange -- a consumer "
        "reporting agency in substance -- and it holds a file on a large "
        "share of US adults. Verified by browser render: nctue.com/ and "
        "nctue.com/consumer/ carry no search of any kind; /consumer/ is "
        "an accordion of ten FAQ buttons and the only form in the whole "
        "page is a HubSpot contact form nested in an about:blank frame "
        "(email, firstname, lastname, message, all required, posting to f "
        "orms.hsforms.com/.../46515271/052ce853-6d69-4ca2-a833- "
        "8bacd06aafaa). But the same page names nctueconsumerportal.com "
        "as the place to act on your own file, and the Exchange Service "
        "Center at 1-866-349-3233, so a presence check does exist -- "
        "gated behind identity verification rather than published. "
        "Recording 'no search surface' would be true about the probed "
        "pages and false about the broker. What a future pass needs: "
        "render nctueconsumerportal.com and record what identity proof it "
        "demands."
    ),
    "publicdatacheck-com": (
        "RECIPE-READY IN SUBSTANCE, filed here because this module cannot "
        "construct a real SearchRecipe object. National Data Analytics, "
        "LLC trades as Public Data Check, a full people-search ('People, "
        "Property, Phone and Vehicle Records Search'). Verified by "
        "browser render 2026-09-25 at 12s settle: the homepage carries "
        "FOUR separate single-input GET forms, all with action "
        "https://www.publicdatacheck.com/? and all tab-switched so that "
        "three of the four are off-layout at any moment -- (1) "
        "input[type=search][name=search-name][id=search- "
        "name].people__search plus button.people__search__btn 'Search "
        "Now'; (2) input[type=search][name=search-address][id=search- "
        "address].prop__search plus #btn-search; (3) "
        "input[type=tel][name=search-phone-number][id=search- "
        "number].phone__search plus #phone-search-btn; (4) "
        "input[type=text][name=search-vehicle][id=search- "
        "vehicle].vehicle__search plus #vehicle-search-btn. None is "
        "marked required. NO captcha script and NO captcha widget on the "
        "homepage -- which is notable given that this same site's "
        "privacy-request page runs Cloudflare Turnstile (see the opt-out "
        "leg), so the bot check is on the opt-out and not on the search. "
        "A recipe wants form (1) and must activate the PEOPLE tab first "
        "so the input is on-layout."
    ),
    "studentclearinghouse-org": (
        "NO VERDICT as of 2026-09-25, deliberately not no-search-surface, "
        "same reasoning as innovis-com. The National Student "
        "Clearinghouse holds enrollment and degree records on the large "
        "majority of US post-secondary students and is the authoritative "
        "verifier of them. Verified by browser render: "
        "studentclearinghouse.org's homepage and /privacy-policy/ (42.6k "
        "characters) carry no lookup surface at all -- the only forms are "
        "two copies of the Divi header site search (input[name=s]), both "
        "off-layout behind the magnifier toggle, and the site runs "
        "Gravity Forms with reCAPTCHA (invisible v3, sitekey "
        "6Le81PUrAAAAAEexOQS93MKkbyQf1bFlWHMdRTAQ) on its other pages. "
        "But the privacy policy names a self-service portal at "
        "nscsso.my.site.com/student/s/ for an individual's own records, "
        "which is a presence check gated behind a Salesforce login rather "
        "than published. What a future pass needs: render that portal and "
        "record whether a person can reach their own record without an "
        "institution-issued credential."
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
