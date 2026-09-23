"""The search-form presence leg, and the false negatives it must refuse.

The page texts below are abridged from pages actually captured on
2026-09-22 by loading each site, typing a name into its real search box and
pressing Search -- once with a common name ("John Smith") and once with a
nonsense one ("Zylphrenna Quixbottom"). They are fixtures of what these
brokers really print, not invented prose, which is what makes the
classification assertions worth anything.
"""
import pytest

from broker_guard import playwright_checks, search_forms, search_probe
from broker_guard.profile import Identity
from broker_guard.search_forms import (
    SEARCHPEOPLEFREE, THATSTHEM, USPHONEBOOK, SearchRecipe, SearchField,
)

IDENTITY = Identity(first_name="Testy", last_name="Mctestface",
                    phones=["+1-555-0100"], emails=["testy@example.invalid"])
TERMS = ["Testy Mctestface", "+1-555-0100"]


# --- real page text -----------------------------------------------------

THATSTHEM_HIT = (
    "That'sThem Name Address Phone Email Search Results for Testy Mctestface "
    "Found 10 results That'sThem / People Search / M / Mctestface / Testy "
    "Last updated 2 months ago. BACKGROUND CHECK Testy R. Mctestface "
    "Lives in Bossier City, LA Born June 1963 PHONE NUMBERS: 555-010-0"
)

THATSTHEM_MISS = (
    "That'sThem Name Address Phone Email Search Results for Testy Mctestface "
    "Found 0 results That'sThem / People Search / M / Mctestface / Testy "
    "No Results Found We couldn't find any records matching your search. "
    "Try a different spelling or broader search terms. Access premium results "
    "linked to Testy Mctestface Sponsored By VIEW PREMIUM REPORT"
)

THATSTHEM_INTERSTITIAL = "Searching ... please wait while we look that up"

SPF_HIT = (
    "testy mctestface in the usa Testy Mctestface Free People Search "
    "Showing 1 - 10 of 248 People Testy Mctestface in Brook Park, OH "
    "Age 51 Used to live in: Brookpark OH, Cleveland OH"
)

# The important one: a MISS page that repeats the searched name a dozen
# times. Naive term-matching -- which is exactly what the homepage leg does
# -- reports "found" on this page.
SPF_MISS = (
    "testy mctestface in the usa Page Not Found: Name Free People Search "
    "M Mctestface Testy Mctestface 404 - Not Found We're sorry, the page you "
    "were looking for could not be found. Try one of the popular names below! "
    "Public Records for - Sponsored by TruthFinder.com Testy Mctestface "
    "Aliases, also known as (AKA), maiden name, misspellings and alternate "
    "spellings for Testy Mctestface Home address, vacation, business, rental "
    "and apartment property addresses for Testy Mctestface"
)

USPB_HIT = (
    "Search by phone, name, address Sign In Phone Name Address "
    "Testy Mctestface Success, We've found 247 records for Testy Mctestface "
    "We uncovered 247 results for the name Testy Mctestface. These records "
    "include 74 phone numbers and 141 addresses."
)

USPB_MISS = (
    "Search Search by: Phone Name Address Tap into billions of public records. "
    "USPhonebook.com is not a Consumer Reporting Agency (CRA). "
    "WOW! Searching billions of names we did not return a valid result for "
    "Testy Mctestface Public Records for Testy Mctestface Paid Results "
    "Sponsored by TruthFinder.com VIEW DETAILS"
)

CLOUDFLARE_MID_SEARCH = (
    "Just a moment... Checking your browser before accessing the site. "
    "This process is automatic. Enable JavaScript and cookies to continue."
)


# --- classification -----------------------------------------------------

@pytest.mark.parametrize("recipe,text", [
    (THATSTHEM, THATSTHEM_HIT),
    (SEARCHPEOPLEFREE, SPF_HIT),
    (USPHONEBOOK, USPB_HIT),
])
def test_a_real_listing_is_found(recipe, text):
    assert search_forms.classify_search_page(recipe, text, TERMS) == {"found": True}


@pytest.mark.parametrize("recipe,text", [
    (THATSTHEM, THATSTHEM_MISS),
    (SEARCHPEOPLEFREE, SPF_MISS),
    (USPHONEBOOK, USPB_MISS),
])
def test_a_real_zero_result_page_is_absent(recipe, text):
    """These are the only pages allowed to produce a 'not present'."""
    assert search_forms.classify_search_page(recipe, text, TERMS) == {"found": False}


def test_a_miss_page_that_echoes_the_name_is_not_a_hit():
    """SearchPeopleFree's 404 repeats the searched name a dozen times.

    ``is_people_search_hit`` -- the matcher the homepage leg uses on its own
    -- says True for this page. The recipe's no-results markers are consulted
    first precisely so that echo cannot become a false presence report."""
    from broker_guard.detection import is_people_search_hit

    assert is_people_search_hit({"snippet": SPF_MISS}, TERMS) is True
    assert search_forms.classify_search_page(
        SEARCHPEOPLEFREE, SPF_MISS, TERMS) == {"found": False}


def test_an_unrecognized_page_is_an_error_not_an_absence():
    """The redesign case: selectors still 'worked', page means nothing to us."""
    result = search_forms.classify_search_page(
        THATSTHEM, "Welcome to our brand new website! Sign up today.", TERMS)
    assert "error" in result and "found" not in result


def test_an_empty_page_is_an_error():
    assert "error" in search_forms.classify_search_page(THATSTHEM, "   ", TERMS)


def test_a_results_page_with_no_identity_term_is_an_error():
    """A count>0 with nobody's name on the page is a page we don't understand."""
    text = "Search Results for someone Found 12 results ... unrelated content"
    result = search_forms.classify_search_page(THATSTHEM, text, TERMS)
    assert "error" in result and "found" not in result


def test_a_page_claiming_both_outcomes_is_an_error():
    recipe = SearchRecipe(
        broker_id="x", broker_name="X", search_url="https://x.invalid/",
        fields=(), submit_selector="button", results_host="x.invalid",
        no_results_markers=("no records found",),
        hit_markers=("results for",),
    )
    result = search_forms.classify_search_page(
        recipe, "results for Testy Mctestface -- no records found", TERMS)
    assert "error" in result


def test_printed_zero_outranks_hit_wording():
    """'Search Results for X' + 'Found 0 results' is an absence, not a hit."""
    assert search_forms.printed_result_count(THATSTHEM, THATSTHEM_MISS.lower()) == 0
    assert search_forms.classify_search_page(
        THATSTHEM, THATSTHEM_MISS, TERMS) == {"found": False}


def test_a_page_with_no_printed_count_yields_none_not_zero():
    """None means 'did not say'; treating that as 0 would be the whole bug."""
    assert search_forms.printed_result_count(THATSTHEM, "nothing here") is None


# --- the driver: false-negative discipline ------------------------------

class FakePage:
    """A page that changes what it renders once the form is submitted."""

    def __init__(self, url, form_text="First Name Last Name Search",
                 results_url=None, results_text="", results_title="",
                 fill_error=None, click_error=None):
        self.url = url
        self._text = form_text
        self._title = "search form"
        self._results = (results_url or url, results_text, results_title)
        self.filled = {}
        self.clicked = []
        self.closed = False
        self._fill_error = fill_error
        self._click_error = click_error

    # -- playwright surface used by the driver
    def goto(self, url, **kwargs):
        self.url = url
        return None

    def fill(self, selector, value):
        if self._fill_error:
            raise RuntimeError(self._fill_error)
        self.filled[selector] = value

    def click(self, selector):
        if self._click_error:
            raise RuntimeError(self._click_error)
        self.clicked.append(selector)
        self.url, self._text, self._title = self._results

    def inner_text(self, selector):
        return self._text

    def title(self):
        return self._title

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.closed = False

    def close(self):
        self.closed = True


def drive(page, recipe=THATSTHEM, values=None, pages=None):
    context = FakeContext(pages or [page])
    return search_probe.run_search(
        context, page, recipe,
        values if values is not None else {f.selector: "Testy Mctestface"
                                           for f in recipe.fields},
        TERMS,
        # Short, so the "never settles" cases cost milliseconds. The real
        # default is twenty seconds because ThatsThem genuinely churns.
        ready_timeout_ms=50,
    )


def test_a_bot_wall_mid_search_is_an_error_never_a_not_found():
    """THE test this module exists for.

    The form loads fine, we fill it, we press Search -- and what comes back
    is a Cloudflare challenge. The identity terms are (truthfully!) absent
    from it. Reporting ``{"found": False}`` here would make
    ``interpret_check_result`` say checked=True/present=False, which
    ``run_cycle`` reports as resolved and autopilot acts on with
    ``store.forget()``: the presence row for a broker that is still
    publishing Penn's PII, deleted, because a CAPTCHA said nothing."""
    page = FakePage("https://thatsthem.com/",
                    results_url="https://thatsthem.com/name/Testy-Mctestface",
                    results_text=CLOUDFLARE_MID_SEARCH,
                    results_title="Just a moment...")
    result = drive(page)
    assert "error" in result and "found" not in result
    assert "bot wall" in result["error"]


def test_a_bot_wall_on_the_form_page_is_an_error():
    page = FakePage("https://thatsthem.com/", form_text=CLOUDFLARE_MID_SEARCH)
    page._title = "Just a moment..."
    result = drive(page)
    assert "bot wall" in result.get("error", "")


def test_the_form_is_actually_filled_and_submitted():
    page = FakePage("https://thatsthem.com/",
                    results_url="https://thatsthem.com/name/Testy-Mctestface",
                    results_text=THATSTHEM_HIT, results_title="Testy")
    assert drive(page) == {"found": True}
    assert page.filled == {"form[role='search'] #name": "Testy Mctestface"}
    assert page.clicked == ["form[role='search'] button"]


def test_a_real_zero_result_search_is_a_clean_absence():
    page = FakePage("https://thatsthem.com/",
                    results_url="https://thatsthem.com/name/Testy-Mctestface",
                    results_text=THATSTHEM_MISS, results_title="Testy")
    assert drive(page) == {"found": False}


def test_the_form_page_is_allowed_to_settle_before_anything_is_typed():
    """Regression for a false negative found on the live site.

    USPhonebook's hero form is intercepted by JavaScript that rewrites the
    submit into a slug navigation. Clicking at domcontentloaded, before that
    handler is bound, fires the raw POST and the site answers with a 404 --
    a page that reads exactly like 'no such person'. The settle is what makes
    the same run land on the real results, so its ORDER (before the first
    fill) is the property worth pinning."""
    order = []

    class SettlingPage(FakePage):
        def wait_for_load_state(self, state):
            order.append("load:" + state)

        def wait_for_timeout(self, ms):
            order.append("settle")

        def fill(self, selector, value):
            order.append("fill")
            super().fill(selector, value)

        def click(self, selector):
            order.append("click")
            super().click(selector)

    page = SettlingPage("https://thatsthem.com/",
                        results_url="https://thatsthem.com/name/Testy",
                        results_text=THATSTHEM_HIT)
    assert drive(page) == {"found": True}
    assert order == ["load:load", "settle", "fill", "click"]


def test_selector_drift_is_an_error():
    page = FakePage("https://thatsthem.com/", fill_error="no such element")
    assert "could not fill" in drive(page).get("error", "")


def test_a_submit_that_does_nothing_is_an_error():
    """The click 'worked' but the page never changed: still the form."""
    page = FakePage("https://thatsthem.com/",
                    results_url="https://thatsthem.com/",
                    results_text="First Name Last Name Search")
    result = drive(page)
    assert "error" in result and "did not produce a results page" in result["error"]


def test_a_stuck_interstitial_is_an_error_not_an_absence():
    """ThatsThem really does sit on 'Searching...' for several seconds."""
    page = FakePage("https://thatsthem.com/",
                    results_url="https://thatsthem.com/search?name=x",
                    results_text=THATSTHEM_INTERSTITIAL,
                    results_title="Searching | ThatsThem")
    assert "error" in drive(page)


def test_results_on_another_companys_site_are_never_read():
    """ThatsThem's submit redirects the original tab to spokeo.com.

    Only a page still served by the broker may answer a question about the
    broker -- and when no such page exists, that is an error."""
    page = FakePage("https://thatsthem.com/",
                    results_url="https://www.spokeo.com/Testy-Mctestface",
                    results_text="Spokeo | People Search  Found 3 results",
                    results_title="Spokeo")
    result = drive(page)
    assert "error" in result
    assert "thatsthem.com" in result["error"]


def test_the_results_tab_is_picked_out_of_several_pages():
    """The real target=_blank case: the answer is in the OTHER tab."""
    original = FakePage("https://thatsthem.com/",
                        results_url="https://www.spokeo.com/Testy",
                        results_text="Spokeo", results_title="Spokeo")
    results_tab = FakePage("https://thatsthem.com/name/Testy-Mctestface",
                           form_text=THATSTHEM_HIT)
    assert drive(original, pages=[original, results_tab]) == {"found": True}


def test_host_matching_accepts_www_and_rejects_lookalikes():
    assert search_probe.host_matches("https://www.usphonebook.com/x", "usphonebook.com")
    assert search_probe.host_matches("https://usphonebook.com/x", "usphonebook.com")
    assert not search_probe.host_matches("https://usphonebook.com.evil.invalid/",
                                         "usphonebook.com")
    assert not search_probe.host_matches("https://spokeo.com/", "thatsthem.com")
    assert not search_probe.host_matches("", "thatsthem.com")


# --- read-only boundary --------------------------------------------------

def test_every_shipped_recipe_is_read_only():
    for recipe in search_forms.RECIPES.values():
        search_forms.assert_read_only(recipe)


def test_a_recipe_pointing_at_an_optout_form_is_refused():
    """The boundary with teeth: this leg searches, it never acts."""
    bad = SearchRecipe(
        broker_id="x", broker_name="X",
        search_url="https://x.invalid/opt-out", fields=(),
        submit_selector="button", results_host="x.invalid",
    )
    with pytest.raises(search_forms.UnsafeRecipeError):
        search_forms.assert_read_only(bad)


def test_the_driver_refuses_an_unsafe_recipe_before_typing():
    bad = SearchRecipe(
        broker_id="x", broker_name="X", search_url="https://x.invalid/",
        fields=(SearchField(selector="#removeMe", source="full_name",
                            label="name"),),
        submit_selector="button", results_host="x.invalid",
    )
    page = FakePage("https://x.invalid/")
    with pytest.raises(search_forms.UnsafeRecipeError):
        search_probe.run_search(FakeContext([page]), page, bad,
                                {"#removeMe": "Testy"}, TERMS)
    assert page.filled == {}


# --- wiring --------------------------------------------------------------

BROKER_WITH_RECIPE = {"id": "thatsthem-com", "name": "ThatsThem",
                      "url": "https://thatsthem.com", "verification": "captcha"}
BROKER_WITHOUT = {"id": "alpha", "name": "Alpha", "url": "https://alpha.invalid",
                  "verification": "automatable"}


def test_a_broker_with_a_recipe_gets_a_search_block():
    checks = playwright_checks.build_site_checks(
        [BROKER_WITH_RECIPE], TERMS, IDENTITY)
    assert len(checks) == 1
    assert checks[0]["search"]["broker_id"] == "thatsthem-com"
    assert checks[0]["search"]["values"] == {
        "form[role='search'] #name": "Testy Mctestface"}
    # The homepage url is still there, so a plain PlaywrightChecker is fine.
    assert checks[0]["url"] == "https://thatsthem.com"


def test_a_broker_without_a_recipe_is_untouched():
    checks = playwright_checks.build_site_checks([BROKER_WITHOUT], TERMS, IDENTITY)
    assert checks == [{"broker_id": "alpha", "url": "https://alpha.invalid",
                       "terms": list(TERMS)}]


def test_no_identity_means_exactly_todays_behaviour():
    """The status-quo guarantee: same call, same checks as before."""
    checks = playwright_checks.build_site_checks(
        [BROKER_WITH_RECIPE, BROKER_WITHOUT], TERMS)
    assert all("search" not in c for c in checks)
    assert [c["broker_id"] for c in checks] == ["thatsthem-com", "alpha"]


def test_a_profile_that_cannot_fill_the_form_falls_back():
    """No name -> no search block, and crucially the broker is still checked."""
    class Nameless:
        first_name = last_name = ""
        full_name = ""
        emails = phones = addresses = ()

    checks = playwright_checks.build_site_checks(
        [BROKER_WITH_RECIPE], TERMS, Nameless())
    assert len(checks) == 1 and "search" not in checks[0]


def test_the_same_brokers_are_checked_with_and_without_identity():
    brokers = [BROKER_WITH_RECIPE, BROKER_WITHOUT,
               {"id": "photo", "name": "P", "url": "https://p.invalid",
                "verification": "photo_id"}]
    before = [c["broker_id"] for c in
              playwright_checks.build_site_checks(brokers, TERMS)]
    after = [c["broker_id"] for c in
             playwright_checks.build_site_checks(brokers, TERMS, IDENTITY)]
    assert before == after


def test_search_checker_is_todays_checker_for_a_broker_without_a_recipe():
    """A checker-level guarantee, not just a build_site_checks one:
    a check with no search block runs the inherited homepage path."""
    homepage = FakePage("https://alpha.invalid", form_text=THATSTHEM_HIT)
    homepage.route = lambda *a, **k: None

    class FakeContextWithPage:
        def set_default_timeout(self, ms):
            pass

        def new_page(self):
            return homepage

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContextWithPage()

    checker = search_probe.SearchChecker()
    checker._browser = FakeBrowser()
    result = checker({"broker_id": "alpha", "url": "https://alpha.invalid",
                      "terms": TERMS})
    # The inherited path: plain term matching over the homepage text, with
    # no form ever filled.
    assert result == {"found": True}
    assert homepage.filled == {} and homepage.clicked == []


def test_recipes_are_keyed_by_real_dataset_broker_ids():
    """Recipe keys must match the ids broker_normalize actually produces
    (``<domain-with-dashes>``), or the recipe silently never fires."""
    for broker_id in search_forms.supported_broker_ids():
        assert broker_id == broker_id.lower()
        assert broker_id.endswith("-com")


def test_unsupported_broker_lookup_raises_rather_than_returning_none():
    with pytest.raises(search_forms.SearchRecipeNotFound):
        search_forms.recipe_for("no-such-broker")


def test_brokers_with_no_public_search_surface_are_recorded_with_reasons():
    """gladiknow.com and chexsystems.com were investigated live and have no
    surface to search; the finding is kept so it is not re-investigated."""
    assert set(search_forms.NO_SEARCH_SURFACE) == {"gladiknow-com", "chexsystems-com"}
    for broker_id, reason in search_forms.NO_SEARCH_SURFACE.items():
        assert not search_forms.is_supported(broker_id)
        assert len(reason) > 80
