"""Automated opt-out submission: the review folder, the form recipes, and the
Playwright driver.

NOTHING here touches a real broker's form. The driver is exercised against a
fake page object (the same seam ``tests/test_detection_blindness.py`` uses
for ``browser.PlaywrightChecker``), Playwright need not be installed, and the
DRY-RUN path is the default every test runs unless it explicitly asks for a
simulated real submission. A live submission against Consumer Canvas must
only ever happen because a human pressed the button.

All identity data is FAKE -- see tests/conftest.py.
"""
import json
import os
import stat
from datetime import datetime, timezone

import pytest

from broker_guard import optout_forms, optout_submit, review
from broker_guard.config import Config
from broker_guard.profile import Identity
from conftest import FAKE_EMAIL, FAKE_FIRST, FAKE_LAST

RECIPE = optout_forms.CONSUMER_CANVAS
STARTED = datetime(2026, 9, 22, 18, 42, 33, tzinfo=timezone.utc)


@pytest.fixture
def identity():
    return Identity(
        first_name=FAKE_FIRST,
        last_name=FAKE_LAST,
        middle_name="Q",
        emails=[FAKE_EMAIL],
        addresses=["Springfield, IL"],
    )


@pytest.fixture
def cfg(tmp_path):
    """Enabled + dry run: the state Penn is told to start in."""
    return Config(
        review_dir=str(tmp_path / "review"),
        optout_submit_enabled=True,
        optout_submit_dry_run=True,
    )


# --- a fake page -------------------------------------------------------------

class FakePage:
    """Enough of Playwright's Page for the driver, plus a record of calls."""

    def __init__(self, body="Privacy Rights Portal", title="Consumer Canvas",
                 present=(), screenshot=b"PNG", result_body=None):
        self.body = body
        self._title = title
        self.present = set(present)
        self._screenshot = screenshot
        self.result_body = result_body
        self.filled = {}
        self.typed = {}
        self.clicked = []
        self.selected = {}
        self.checked = []
        self.pressed = []
        self.screenshots = 0
        self.submitted = False
        self.closed = False

    # navigation / reading
    def goto(self, url, **kw):
        self.url = url

    def inner_text(self, _selector):
        if self.submitted and self.result_body is not None:
            return self.result_body
        return self.body

    def title(self):
        return self._title

    # filling
    def fill(self, selector, value):
        self.filled[selector] = value

    def type(self, selector, value, delay=0):
        self.typed[selector] = value
        self.filled[selector] = value

    def press(self, selector, key):
        self.pressed.append((selector, key))

    def click(self, selector):
        self.clicked.append(selector)
        if selector == RECIPE.submit_selector:
            self.submitted = True

    def select_option(self, selector, label=None, value=None):
        self.selected[selector] = label if label is not None else value
        self.clicked.append(selector)

    def check(self, selector, force=False):
        self.checked.append(selector)
        self.clicked.append(selector)

    # The one thing a fake page must NEVER be asked to do: a honeypot is
    # only filled through fill/type, and both record into self.filled, so
    # "was the honeypot touched?" is answerable as `"#website" in
    # page.filled` in any test below.
    @property
    def touched(self):
        return set(self.filled) | set(self.typed) | set(self.selected) | set(self.checked)

    def wait_for_selector(self, selector, timeout=None):
        if selector in self.present:
            return object()
        raise RuntimeError("no such element")

    def query_selector(self, selector):
        return object() if selector in self.present else None

    def wait_for_timeout(self, _ms):
        return None

    def screenshot(self, full_page=False):
        self.screenshots += 1
        if self._screenshot is None:
            raise RuntimeError("screenshot failed")
        return self._screenshot

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def set_default_timeout(self, ms):
        pass

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakeSubmitter:
    """Stands in for a started OptOutSubmitter."""

    def __init__(self, page):
        self.page = page
        self._browser = object()      # the "started" marker the driver checks
        self.context = FakeContext(page)

    def new_page(self):
        return self.context, self.page


def combo_ready(**kw):
    """A page whose country/state autocomplete popups resolve."""
    present = set(kw.pop("present", ()))
    present |= {
        "[role='option'][aria-label='United States']",
        "[role='option'][aria-label='Illinois']",
    }
    return FakePage(present=present, **kw)


# --- review folder -----------------------------------------------------------

def _record(outcome=review.OUTCOME_DRY_RUN, **extra):
    rid = review.attempt_id("consumer-canvas-llc", "abc123", STARTED.isoformat())
    base = {
        "id": rid,
        "basename": review.basename_for("consumer-canvas-llc", STARTED, rid),
        "broker_id": "consumer-canvas-llc",
        "outcome": outcome,
        "started_at": STARTED.isoformat(),
    }
    base.update(extra)
    return base


def test_save_attempt_writes_record_and_screenshot(tmp_path):
    saved = review.save_attempt(str(tmp_path), _record(), b"PNGDATA")

    json_path = tmp_path / (saved["basename"] + ".json")
    png_path = tmp_path / (saved["basename"] + ".png")
    assert json.loads(json_path.read_text())["outcome"] == review.OUTCOME_DRY_RUN
    assert png_path.read_bytes() == b"PNGDATA"
    assert saved["screenshot"] == saved["basename"] + ".png"


def test_saved_files_are_owner_only(tmp_path):
    """These files hold the exact PII that was typed into a third party's form."""
    saved = review.save_attempt(str(tmp_path), _record(), b"PNG")

    for suffix in (".json", ".png"):
        mode = stat.S_IMODE(os.stat(tmp_path / (saved["basename"] + suffix)).st_mode)
        assert mode == 0o600, suffix
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700


def test_basename_sorts_chronologically():
    """load_attempts orders by NAME, so the name must carry the clock."""
    earlier = review.basename_for("b", datetime(2026, 1, 1, tzinfo=timezone.utc), "aaaa")
    later = review.basename_for("b", datetime(2026, 6, 1, tzinfo=timezone.utc), "0000")
    assert earlier < later


def test_load_attempts_is_newest_first(tmp_path):
    for month in (1, 6, 3):
        ts = datetime(2026, month, 1, tzinfo=timezone.utc)
        rid = review.attempt_id("b", "k", ts.isoformat())
        review.save_attempt(str(tmp_path), {
            "id": rid, "basename": review.basename_for("b", ts, rid),
            "broker_id": "b", "outcome": review.OUTCOME_DRY_RUN,
            "started_at": ts.isoformat(), "month": month,
        }, None)

    assert [r["month"] for r in review.load_attempts(str(tmp_path))] == [6, 3, 1]


def test_load_attempts_missing_folder_is_empty_not_an_error(tmp_path):
    assert review.load_attempts(str(tmp_path / "nope")) == []


def test_one_corrupt_record_does_not_hide_the_good_ones(tmp_path):
    review.save_attempt(str(tmp_path), _record(), None)
    (tmp_path / "20260101T000000Z-broker-deadbeef.json").write_text("{not json")

    records = review.load_attempts(str(tmp_path))
    assert len(records) == 1
    assert records[0]["outcome"] == review.OUTCOME_DRY_RUN


def test_save_attempt_rejects_an_unknown_outcome(tmp_path):
    with pytest.raises(review.ReviewError):
        review.save_attempt(str(tmp_path), _record(outcome="probably_fine"), None)


def test_record_survives_a_screenshot_that_cannot_be_written(tmp_path, monkeypatch):
    """The record is the legally interesting half; it must not be lost with the image."""
    real = review._atomic_write

    def fail_on_png(path, write):
        if path.endswith(".png"):
            raise OSError("disk full")
        return real(path, write)

    monkeypatch.setattr(review, "_atomic_write", fail_on_png)
    saved = review.save_attempt(str(tmp_path), _record(), b"PNG")

    assert saved["screenshot"] is None
    assert "screenshot_error" in saved
    assert review.load_attempts(str(tmp_path))[0]["id"] == saved["id"]


def test_attempt_counts_reports_every_outcome(tmp_path):
    counts = review.attempt_counts([
        {"outcome": review.OUTCOME_DRY_RUN},
        {"outcome": review.OUTCOME_NEEDS_MANUAL},
        {"outcome": review.OUTCOME_NEEDS_MANUAL},
    ])
    assert counts[review.OUTCOME_NEEDS_MANUAL] == 2
    assert counts[review.OUTCOME_SUBMITTED] == 0
    assert set(counts) == set(review.OUTCOMES)


# --- recipes / field mapping -------------------------------------------------

def test_consumer_canvas_id_matches_the_broker_dataset():
    """The recipe key must equal the id broker_normalize derives, or the
    allow-list silently never matches the broker it was written for."""
    from broker_guard.broker_normalize import slugify

    assert RECIPE.broker_id == slugify("CONSUMER CANVAS LLC")


def test_resolve_fields_maps_the_profile_onto_the_real_selectors(identity):
    resolved = optout_forms.resolve_fields(RECIPE, identity)

    assert resolved["values"]["#firstNameDSARElement"] == FAKE_FIRST
    assert resolved["values"]["#lastNameDSARElement"] == FAKE_LAST
    assert resolved["values"]["#emailDSARElement"] == FAKE_EMAIL
    assert resolved["values"]["#countryDSARElement"] == "United States"
    assert resolved["values"]["#stateDSARElement"] == "Illinois"
    assert resolved["missing"] == []


def test_request_details_is_a_fixed_literal_not_profile_data(identity):
    """What gets sent to a third party in prose is reviewable in the source."""
    details = optout_forms.resolve_fields(RECIPE, identity)["values"][
        "#requestDetailsDSARElement"]
    assert "opt out of the sale" in details
    assert FAKE_FIRST not in details and FAKE_EMAIL not in details


@pytest.mark.parametrize("addresses,expected", [
    (["Springfield, IL"], "Illinois"),
    (["742 Evergreen Terrace, Springfield, IL"], "Illinois"),
    (["Springfield, Illinois"], "Illinois"),
    (["somewhere"], ""),
    ([], ""),
    (None, ""),
])
def test_state_is_expanded_to_the_name_the_form_lists(addresses, expected):
    assert optout_forms.state_from_addresses(addresses) == expected


def test_a_profile_with_no_email_reports_the_missing_field():
    resolved = optout_forms.resolve_fields(
        RECIPE, Identity(first_name="A", last_name="B", addresses=["X, IL"]))
    assert "Email" in resolved["missing"]


def test_recipe_for_unknown_broker_raises(identity):
    with pytest.raises(optout_forms.RecipeNotFound):
        optout_forms.recipe_for("some-other-broker")


def test_exactly_the_hand_verified_brokers_are_turned_on():
    """The allow-list is a hand-written list, and this is the whole of it.

    A broker gets in here only because a person opened its form and read the
    fields off the page. This test failing means someone added one without
    saying so -- which is the failure mode the allow-list exists to prevent.
    """
    assert optout_forms.supported_broker_ids() == [
        "achcoop-com", "advancedbackgroundchecks-com", "bigdbm-com",
        "bolttech", "consumer-canvas-llc", "courtrecords-us", "credit-com",
        "ls-mobile-apps-holdings-ltd", "nielsen", "peopledatalabs-com",
        "recordsfinder-com", "revealphoneowner-com",
        "searchpublicrecords-com", "staterecords-org", "thatsthem-com",
    ]
    # In the dataset, but not hand-verified -> still unsubmittable.
    assert not optout_forms.is_supported("allant-group")
    assert not optout_forms.is_supported("cybba")


def test_brokers_with_no_opt_out_surface_are_recorded_with_reasons():
    """The opt-out-leg twin of search_forms's NO_SEARCH_SURFACE test.

    Each of these was investigated live and has no self-service consumer
    removal surface to submit to (authenticated portal, mailbox-only
    channel, or an affiliate front whose 'opt out' link actually points at
    a different company's own page); the finding is kept so it is not
    re-investigated, and so nobody accidentally builds a recipe that would
    submit a request under the wrong broker's name.
    """
    for broker_id, reason in optout_forms.NO_OPTOUT_SURFACE.items():
        assert not optout_forms.is_supported(broker_id)
        assert len(reason) > 80
    assert "chexsystems-com" in optout_forms.NO_OPTOUT_SURFACE


def test_a_walled_opt_out_form_is_not_filed_as_a_missing_one():
    """'We cannot reach the form' and 'there is no form' are different facts.

    Only one of them can change back: if a wall comes down, a recipe becomes
    possible, whereas a mailbox-only broker never becomes automatable. And a
    blocked broker must still be unsubmittable, which is what absence from
    RECIPES -- not this note -- actually enforces.
    """
    for broker_id, reason in optout_forms.OPTOUT_BLOCKED.items():
        assert not optout_forms.is_supported(broker_id)
        assert broker_id not in optout_forms.NO_OPTOUT_SURFACE
        assert len(reason) > 80
    assert "cyberbackgroundchecks-com" in optout_forms.OPTOUT_BLOCKED


def test_an_undecided_opt_out_is_not_rounded_down_to_a_decided_one():
    """"We don't know yet" must stay sayable.

    Without somewhere to put it, the pressure at the end of a batch is to
    round every unfinished question down to the nearest decided-looking
    bucket -- and a broker filed as blocked or surfaceless stops being
    revisited. So an entry here must be in NONE of the three decided maps,
    and must still be unsubmittable, which absence from RECIPES enforces.
    """
    for broker_id, reason in optout_forms.OPTOUT_UNDECIDED.items():
        assert not optout_forms.is_supported(broker_id)
        assert broker_id not in optout_forms.NO_OPTOUT_SURFACE
        assert broker_id not in optout_forms.OPTOUT_BLOCKED
        assert broker_id not in optout_forms.OPTOUT_OUT_OF_SCOPE
        assert len(reason) > 80


def test_out_of_scope_opt_outs_are_kept_apart_from_the_other_two_nos():
    """A third kind of no, and the one most likely to be mistaken for laziness.

    These brokers have a real, reachable, unwalled removal page -- it just
    asks for something a recipe must not supply on its own: a government-ID
    upload, or a judgement call about which stranger in a result list is
    actually Penn. Recording them as blocked would suggest waiting for a
    wall to fall, and recording them as surfaceless would be false.
    """
    for broker_id, reason in optout_forms.OPTOUT_OUT_OF_SCOPE.items():
        assert not optout_forms.is_supported(broker_id)
        assert broker_id not in optout_forms.NO_OPTOUT_SURFACE
        assert broker_id not in optout_forms.OPTOUT_BLOCKED
        assert len(reason) > 80


# --- the driver: interlocks --------------------------------------------------

def test_refuses_when_the_feature_is_off(identity, tmp_path):
    off = Config(review_dir=str(tmp_path), optout_submit_enabled=False)
    with pytest.raises(optout_submit.SubmissionRefused):
        optout_submit.submit_optout(RECIPE, identity, off,
                                    submitter=FakeSubmitter(combo_ready()))


def test_refusal_leaves_no_record_because_nothing_happened(identity, tmp_path):
    off = Config(review_dir=str(tmp_path), optout_submit_enabled=False)
    with pytest.raises(optout_submit.SubmissionRefused):
        optout_submit.submit_optout(RECIPE, identity, off,
                                    submitter=FakeSubmitter(combo_ready()))
    assert review.load_attempts(str(tmp_path)) == []


def test_refuses_a_broker_that_is_not_on_the_allow_list(identity, cfg):
    rogue = optout_forms.FormRecipe(
        broker_id="not-allow-listed", broker_name="Rogue",
        url="https://rogue.invalid/form", flavor=optout_forms.FLAVOR_ONETRUST_DSAR)
    with pytest.raises(optout_submit.SubmissionRefused):
        optout_submit.submit_optout(rogue, identity, cfg,
                                    submitter=FakeSubmitter(combo_ready()))


def test_missing_profile_fields_fail_without_opening_the_form(cfg):
    """A half-filled DSAR is worse than none: the broker answers it and it is spent."""
    thin = Identity(first_name="A", last_name="B")   # no email, no address
    page = combo_ready()

    saved = optout_submit.submit_optout(RECIPE, thin, cfg, submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert "Email" in saved["reason"]
    assert page.filled == {} and page.clicked == []


def test_no_browser_is_a_recorded_failure_not_a_crash(identity, cfg):
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=None)
    assert saved["outcome"] == review.OUTCOME_FAILED
    assert "browser" in saved["reason"].lower()


# --- the driver: the DRY-RUN default ----------------------------------------

def test_dry_run_fills_everything_and_never_presses_submit(identity, cfg):
    page = combo_ready()

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert saved["dry_run"] is True
    assert page.submitted is False
    assert RECIPE.submit_selector not in page.clicked
    assert page.filled["#firstNameDSARElement"] == FAKE_FIRST
    assert page.filled["#emailDSARElement"] == FAKE_EMAIL
    assert page.screenshots == 1


def test_dry_run_is_the_default_from_config_alone(identity, tmp_path):
    """Enabling the feature must not by itself enable real submission."""
    enabled_only = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True)
    page = combo_ready()

    saved = optout_submit.submit_optout(RECIPE, identity, enabled_only,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert page.submitted is False


def test_choices_are_clicked_before_fields_are_filled(identity, cfg):
    """The request-type listbox does not exist until a subject type is chosen."""
    page = combo_ready()
    optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page))

    subject = next(i for i, s in enumerate(page.clicked) if "subjectTypes" in s)
    request = next(i for i, s in enumerate(page.clicked) if "requestTypes" in s)
    assert subject < request


def test_combobox_fields_are_typed_and_the_option_picked(identity, cfg):
    """A plain fill leaves an Angular autocomplete's model empty."""
    page = combo_ready()
    optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page))

    assert page.typed["#countryDSARElement"] == "United States"
    assert "[role='option'][aria-label='Illinois']" in page.clicked


def test_dry_run_record_lists_exactly_what_was_filled(identity, cfg):
    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(combo_ready()))

    assert saved["fields"]["First Name"] == FAKE_FIRST
    assert saved["fields"]["State"] == "Illinois"
    assert [c["value"] for c in saved["choices"]][0] == "U.S. Consumer"
    assert saved["screenshot"].endswith(".png")


def test_dry_run_override_beats_a_config_that_would_submit(identity, tmp_path):
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready()

    saved = optout_submit.submit_optout(RECIPE, identity, live,
                                        submitter=FakeSubmitter(page), dry_run=True)

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert page.submitted is False


# --- the driver: CAPTCHA is a stop, never a solve ---------------------------

def test_captcha_stops_the_attempt_and_asks_for_a_human(identity, cfg):
    page = combo_ready(present={"#captchaCode"})

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert saved["manual_action_source"] == "captcha_fallback"
    assert page.submitted is False


def test_captcha_bailout_still_leaves_a_filled_form_screenshot(identity, cfg):
    """The screenshot IS the deliverable when a human has to finish by hand."""
    page = combo_ready(present={"#captchaCode"})

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["screenshot"].endswith(".png")
    assert saved["fields"]["First Name"] == FAKE_FIRST
    assert page.screenshots == 1


def test_captcha_stops_a_real_submission_too(identity, tmp_path):
    """Not just a dry-run nicety: this is the interlock that matters."""
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready(present={"#captchaCode"})

    saved = optout_submit.submit_optout(RECIPE, live and identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert page.submitted is False


def test_the_real_consumer_canvas_captcha_is_detected():
    """The live form carries BotDetect; this is the selector set that catches it."""
    page = FakePage(present={"img[src*='botdetectcaptcha']"})
    assert optout_submit.detect_captcha(page, RECIPE) is not None


@pytest.mark.parametrize("selector", [
    "iframe[src*='recaptcha']", ".g-recaptcha", ".cf-turnstile",
    "[data-sitekey]", "input[name*='captcha']",
])
def test_other_vendors_bot_checks_are_detected_too(selector):
    assert optout_submit.detect_captcha(FakePage(present={selector}), RECIPE) == selector


def test_a_clean_form_is_not_reported_as_a_bot_check():
    assert optout_submit.detect_captcha(FakePage(), RECIPE) is None


def test_captcha_module_is_never_used_here():
    """Policy, asserted: we stop at bot checks, we do not solve them."""
    source = open(optout_submit.__file__, encoding="utf-8").read()
    assert "import captcha" not in source
    assert "captcha.solve" not in source


def test_an_interstitial_is_treated_as_a_bot_check(identity, cfg):
    page = combo_ready(body="Checking your browser before accessing", title="Just a moment...")

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert saved["detected"] == "bot_wall"
    assert page.filled == {}


# --- the driver: a simulated REAL submission --------------------------------

def test_a_real_submission_presses_submit_and_records_confirmation(identity, tmp_path):
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready(result_body="Your request has been submitted. Request ID 12345")

    saved = optout_submit.submit_optout(RECIPE, identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_SUBMITTED
    assert saved["dry_run"] is False
    assert page.submitted is True
    assert "12345" in saved["confirmation_text"]


def test_an_unconfirmed_result_page_is_a_failure_not_a_success(identity, tmp_path):
    """Silence must never be counted as a request the broker accepted."""
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready(result_body="Something went wrong.")

    saved = optout_submit.submit_optout(RECIPE, identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert page.submitted is True


def test_classify_submission_needs_a_real_marker():
    assert optout_submit.classify_submission("Thank you for submitting", RECIPE)
    assert not optout_submit.classify_submission("", RECIPE)
    assert not optout_submit.classify_submission(None, RECIPE)


# --- the driver: failures are audited, never crashes ------------------------

def test_a_page_that_explodes_is_recorded_not_raised(identity, cfg):
    class Exploding(FakePage):
        def fill(self, selector, value):
            raise RuntimeError("target closed")

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(Exploding()))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert saved["reason"].startswith("RuntimeError")


def test_the_page_is_closed_even_when_the_attempt_fails(identity, cfg):
    class Exploding(FakePage):
        def fill(self, selector, value):
            raise RuntimeError("boom")

    page = Exploding()
    optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page))
    assert page.closed is True


def test_failure_reasons_never_carry_the_full_exception_text(identity, cfg):
    """Playwright puts the page URL -- and a form URL can carry PII -- in its errors."""
    leak = "https://broker.invalid/form?email=" + FAKE_EMAIL

    class Leaky(FakePage):
        def fill(self, selector, value):
            raise RuntimeError("navigating to " + leak + "\nfull dump here")

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(Leaky()))

    assert "full dump here" not in saved["reason"]
    assert len(saved["reason"]) <= 220


def test_nothing_pii_shaped_reaches_the_logs(identity, cfg, caplog):
    caplog.set_level("DEBUG", logger="broker_guard.optout_submit")
    caplog.set_level("DEBUG", logger="broker_guard.review")

    optout_submit.submit_optout(RECIPE, identity, cfg,
                                submitter=FakeSubmitter(combo_ready()))

    blob = "\n".join(
        str(r.getMessage()) + str(getattr(r, "__dict__", {})) for r in caplog.records)
    for secret in (FAKE_EMAIL, FAKE_FIRST, FAKE_LAST, "Illinois"):
        assert secret not in blob, secret


def test_a_screenshot_failure_does_not_lose_the_attempt(identity, cfg):
    page = combo_ready(screenshot=None)

    saved = optout_submit.submit_optout(RECIPE, identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert saved["screenshot"] is None


# --- config / settings plumbing ---------------------------------------------

def test_both_switches_default_to_the_safe_end():
    blank = Config()
    assert blank.optout_submit_enabled is False
    assert blank.optout_submit_dry_run is True


def test_env_turns_the_switches_over():
    from broker_guard.config import load_config

    cfg = load_config({
        "BG_OPTOUT_SUBMIT_ENABLED": "true",
        "BG_OPTOUT_SUBMIT_DRY_RUN": "false",
        "BG_REVIEW_DIR": "/data/review",
    })
    assert cfg.optout_submit_enabled is True
    assert cfg.optout_submit_dry_run is False
    assert cfg.review_dir == "/data/review"


def test_both_switches_are_ui_editable_and_survive_a_redeploy(settings_store):
    """Stored beats env -- the whole point of the settings store."""
    from broker_guard import settings as settings_mod

    assert "optout_submit_enabled" in settings_mod.SPEC_BY_KEY
    assert "optout_submit_dry_run" in settings_mod.SPEC_BY_KEY

    settings_mod.update_settings(settings_store, {"optout_submit_enabled": True})
    cfg = settings_mod.effective_config(
        Config(settings_path=settings_store, optout_submit_enabled=False),
        settings_store)
    assert cfg.optout_submit_enabled is True


def test_review_dir_is_not_ui_editable():
    """Path plumbing stays env-only: a store cannot relocate itself."""
    from broker_guard import settings as settings_mod

    assert "review_dir" not in settings_mod.SPEC_BY_KEY


# =============================================================================
# The 2026-09-22 additions: Nielsen, bolttech, Credit.com and L.S Mobile Apps.
#
# Every recipe below was written by opening the live form and reading its
# fields; these tests pin down what was read, and -- for the two guards that
# matter (step ORDER and the honeypot) -- prove the guard bites by showing
# the same test failing when the guard is reverted.
# =============================================================================

NIELSEN = optout_forms.NIELSEN
BOLTTECH = optout_forms.BOLTTECH
CREDIT_COM = optout_forms.CREDIT_COM
LSM = optout_forms.LS_MOBILE_APPS


@pytest.fixture
def full_identity():
    """A profile with everything the new forms ask for."""
    return Identity(
        first_name=FAKE_FIRST,
        last_name=FAKE_LAST,
        emails=[FAKE_EMAIL],
        phones=["555-123-4567"],
        addresses=["742 Evergreen Terrace, Springfield, IL 62704"],
    )


class GatedPage(FakePage):
    """A fake page that reproduces the live forms' CONDITIONAL rendering.

    This is the point of the class: on Nielsen's real form the request-type
    listbox and the State field do not exist in the DOM until Country has
    been filled, and on L.S Mobile's the rights dropdown holds nothing but a
    "Select your territory first" placeholder until Territory is chosen.
    Touching a not-yet-rendered element raises here exactly as Playwright
    would time out there, so a recipe whose steps are in the wrong order
    FAILS this test suite instead of only failing in production.

    ``gates`` maps a selector fragment that is gated -> the selector that
    must have been filled/selected first.
    """

    def __init__(self, gates, **kw):
        super().__init__(**kw)
        self.gates = dict(gates)

    def _guard(self, selector):
        for gated, required in self.gates.items():
            if gated in selector and required not in self.touched:
                raise RuntimeError(
                    "element {} is not rendered yet: {} comes first".format(
                        gated, required))

    def click(self, selector):
        self._guard(selector)
        return super().click(selector)

    def fill(self, selector, value):
        self._guard(selector)
        return super().fill(selector, value)

    def type(self, selector, value, delay=0):
        self._guard(selector)
        return super().type(selector, value, delay=delay)

    def select_option(self, selector, label=None, value=None):
        self._guard(selector)
        return super().select_option(selector, label=label, value=value)


def nielsen_page(**kw):
    """A Nielsen form: request type and State appear only after Country."""
    present = set(kw.pop("present", ()))
    present |= {"[role='option'][aria-label='United States']",
                "[role='option'][aria-label='Illinois']"}
    return GatedPage({"#requestTypesDSARElement": "#countryDSARElement",
                      "#stateDSARElement": "#countryDSARElement"},
                     present=present, **kw)


def lsm_page(**kw):
    """An L.S Mobile form: the rights dropdown fills in after Territory."""
    return GatedPage({"#privacyRight": "#territory"}, **kw)


# --- Nielsen -----------------------------------------------------------------

def test_nielsen_fills_country_before_touching_the_gated_request_type(
        full_identity, cfg):
    """The ordering that the live form actually requires."""
    page = nielsen_page()

    saved = optout_submit.submit_optout(NIELSEN, full_identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    country = page.clicked.index("[role='option'][aria-label='United States']")
    request = next(i for i, s in enumerate(page.clicked) if "requestTypes" in s)
    subject = next(i for i, s in enumerate(page.clicked) if "subjectTypes" in s)
    assert subject < country < request


def test_the_old_choices_then_fields_order_would_fail_nielsen(full_identity, cfg):
    """The revert test: prove the ordering guard bites.

    Same recipe, same page, but written the pre-2026-09-22 way (all choices
    first, then all fields). If ``ordered_steps`` did not honour an explicit
    ``steps`` tuple, THIS is what Nielsen would do against the live form --
    click a listbox that has not been rendered.
    """
    reverted = optout_forms.FormRecipe(
        broker_id=NIELSEN.broker_id, broker_name=NIELSEN.broker_name,
        url=NIELSEN.url, flavor=NIELSEN.flavor,
        choices=tuple(s for s in NIELSEN.steps
                      if isinstance(s, optout_forms.Choice)),
        fields=optout_forms.recipe_fields(NIELSEN),
        submit_selector=NIELSEN.submit_selector,
    )
    page = nielsen_page()

    saved = optout_submit.submit_optout(reverted, full_identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert "not rendered yet" in saved["reason"]


def test_nielsen_has_no_us_consumer_subject_type_and_says_so():
    """The live form's vocabulary is panel/employee, not consumer."""
    subject = NIELSEN.steps[0]
    assert subject.option_label == "Other (see description)"
    assert "no consumer option" in NIELSEN.notes.lower()


def test_nielsen_request_type_keeps_the_forms_own_typo():
    """'of of' is Nielsen's aria-label; correcting it would miss the option."""
    request = next(s for s in NIELSEN.steps
                   if isinstance(s, optout_forms.Choice)
                   and s.label == "Request type")
    assert request.option_label == "Right to Opt Out of of Sale or Sharing"


def test_nielsen_sends_no_request_details_because_the_form_has_no_box():
    labels = {f.label for f in optout_forms.recipe_fields(NIELSEN)}
    assert "Request Details" not in labels


def test_nielsen_zip_comes_from_the_profile_address(full_identity):
    resolved = optout_forms.resolve_fields(NIELSEN, full_identity)
    assert resolved["values"]["#zipDSARElement"] == "62704"


def test_nielsen_zip_is_optional_so_a_zipless_profile_still_runs(cfg):
    thin = Identity(first_name="A", last_name="B", emails=[FAKE_EMAIL],
                    addresses=["Springfield, IL"])
    resolved = optout_forms.resolve_fields(NIELSEN, thin)
    assert resolved["missing"] == []


def test_nielsens_botdetect_captcha_stops_a_live_run(full_identity, tmp_path):
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = nielsen_page(present={"#captchaCode"})

    saved = optout_submit.submit_optout(NIELSEN, full_identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert saved["manual_action_source"] == "captcha_fallback"
    assert page.submitted is False


# --- bolttech ----------------------------------------------------------------

def test_bolttech_uses_its_own_option_labels_not_consumer_canvass(
        full_identity, cfg):
    page = combo_ready()

    saved = optout_submit.submit_optout(BOLTTECH, full_identity, cfg,
                                        submitter=FakeSubmitter(page))

    values = [c["value"] for c in saved["choices"]]
    assert values == [
        "Consumer",
        "Request to Opt-Out (Do Not Sell or Share My Personal Information)",
    ]
    # ...and they are NOT Consumer Canvas's, which is the whole reason each
    # form was opened rather than copied.
    canvas = [c.option_label for c in RECIPE.choices]
    assert values != canvas


def test_bolttech_fills_country_before_state(full_identity, cfg):
    """State's autocomplete is populated from Country."""
    page = combo_ready()
    optout_submit.submit_optout(BOLTTECH, full_identity, cfg,
                                submitter=FakeSubmitter(page))

    order = list(page.typed)
    assert order.index("#countryDSARElement") < order.index("#stateDSARElement")


def test_bolttech_does_not_fill_the_optional_phone_field(full_identity):
    """It sits behind a separate country-code combobox; left alone on purpose."""
    resolved = optout_forms.resolve_fields(BOLTTECH, full_identity)
    assert "#phoneNumberDSARElement" not in resolved["values"]


def test_bolttechs_recaptcha_stops_a_live_run(full_identity, tmp_path):
    """Different vendor from the other three: reCAPTCHA v2, not BotDetect."""
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready(present={"iframe[src*='recaptcha']"})

    saved = optout_submit.submit_optout(BOLTTECH, full_identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert page.submitted is False


# --- Credit.com --------------------------------------------------------------

def test_credit_com_is_the_same_widget_despite_the_cdn_url():
    """The finding, pinned: CDN hosting, identical DOM."""
    assert CREDIT_COM.url.startswith("https://privacyportal-cdn.onetrust.com/dsarwebform/")
    assert CREDIT_COM.flavor == optout_forms.FLAVOR_ONETRUST_DSAR
    assert CREDIT_COM.submit_selector == RECIPE.submit_selector


def test_credit_com_asks_for_an_address_not_a_country(full_identity):
    resolved = optout_forms.resolve_fields(CREDIT_COM, full_identity)
    assert resolved["values"]["#addressDSARElement"] == \
        "742 Evergreen Terrace, Springfield, IL 62704"
    assert resolved["values"]["#zipDSARElement"] == "62704"
    assert "#countryDSARElement" not in resolved["values"]
    assert "#stateDSARElement" not in resolved["values"]


def test_credit_com_without_a_zip_is_a_missing_field_not_a_guess(cfg):
    """Zip is REQUIRED on this form; a half-filled DSAR is worse than none."""
    no_zip = Identity(first_name="A", last_name="B", emails=[FAKE_EMAIL],
                      addresses=["Springfield, IL"])
    page = combo_ready()

    saved = optout_submit.submit_optout(CREDIT_COM, no_zip, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert "Zip" in saved["reason"]
    assert page.filled == {}


def test_credit_com_never_offers_an_ssn_or_a_date_of_birth(full_identity, cfg):
    """Both fields exist on the live form, both optional, both declined."""
    page = combo_ready()
    optout_submit.submit_optout(CREDIT_COM, full_identity, cfg,
                                submitter=FakeSubmitter(page))

    assert "#nationalIdDSARElement" not in page.touched
    assert "#dateOfBirthDSARElement" not in page.touched


# --- L.S Mobile Apps: the bespoke flavor ------------------------------------

def test_lsm_is_not_onetrust_and_does_not_claim_to_be():
    assert LSM.flavor == optout_forms.FLAVOR_LSM_BESPOKE
    assert LSM.flavor != optout_forms.FLAVOR_ONETRUST_DSAR
    assert "onetrust" not in LSM.url


def test_lsm_drives_real_selects_by_label_and_ticks_the_confirmation(
        full_identity, cfg):
    page = lsm_page()

    saved = optout_submit.submit_optout(LSM, full_identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert page.selected == {
        "#appUser": "No",
        "#territory": "US",
        "#privacyRight": "Right to Opt-Out of Sale of Personal Information",
    }
    assert page.checked == ["#confirmation"]


def test_lsm_chooses_territory_before_the_rights_dropdown(full_identity, cfg):
    """Until Territory is set, #privacyRight says 'Select your territory first'."""
    page = lsm_page()
    optout_submit.submit_optout(LSM, full_identity, cfg,
                                submitter=FakeSubmitter(page))

    order = list(page.selected)
    assert order.index("#territory") < order.index("#privacyRight")


def test_the_unordered_lsm_recipe_would_hit_the_empty_rights_dropdown(
        full_identity, cfg):
    """Revert test for the ordering guard on this flavor too."""
    steps = list(LSM.steps)
    territory = next(s for s in steps if getattr(s, "container", "") == "#territory")
    rights = next(s for s in steps if getattr(s, "container", "") == "#privacyRight")
    steps.remove(rights)
    steps.insert(steps.index(territory), rights)      # rights BEFORE territory
    scrambled = optout_forms.FormRecipe(
        broker_id=LSM.broker_id, broker_name=LSM.broker_name, url=LSM.url,
        flavor=LSM.flavor, steps=tuple(steps),
        submit_selector=LSM.submit_selector)
    page = lsm_page()

    saved = optout_submit.submit_optout(scrambled, full_identity, cfg,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_FAILED
    assert "not rendered yet" in saved["reason"]


def test_lsm_phone_is_sent_in_the_international_format_the_form_demands(
        full_identity):
    resolved = optout_forms.resolve_fields(LSM, full_identity)
    assert resolved["values"]["#phoneNumber"] == "+15551234567"


def test_lsm_without_a_phone_number_is_a_missing_field(cfg):
    """'We cannot process a request without a country code' -- their words."""
    no_phone = Identity(first_name="A", last_name="B", emails=[FAKE_EMAIL])
    resolved = optout_forms.resolve_fields(LSM, no_phone)
    assert "Phone number" in resolved["missing"]


def test_lsm_sends_one_full_name_not_two_name_fields(full_identity):
    resolved = optout_forms.resolve_fields(LSM, full_identity)
    assert resolved["values"]["#fullName"] == full_identity.full_name


def test_lsms_canvas_captcha_stops_a_live_run(full_identity, tmp_path):
    """Not flagged in the brief; found on the live page and handled."""
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = lsm_page(present={"#captcha"})

    saved = optout_submit.submit_optout(LSM, full_identity, live,
                                        submitter=FakeSubmitter(page))

    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert LSM.submit_selector not in page.clicked


# --- the honeypot guard, and proof that it bites -----------------------------

def test_the_lsm_honeypot_is_never_touched(full_identity, cfg):
    page = lsm_page()
    optout_submit.submit_optout(LSM, full_identity, cfg,
                                submitter=FakeSubmitter(page))
    assert "#website" not in page.touched


def _honeypot_recipe():
    """LSM's recipe as it would look if someone 'helpfully' filled #website."""
    return optout_forms.FormRecipe(
        broker_id=LSM.broker_id, broker_name=LSM.broker_name, url=LSM.url,
        flavor=LSM.flavor,
        steps=tuple(LSM.steps) + (
            optout_forms.Field(selector="#website", source="literal",
                               label="Website", value="example.com"),
        ),
        forbidden_selectors=LSM.forbidden_selectors,
        submit_selector=LSM.submit_selector)


def test_a_recipe_that_targets_the_honeypot_is_refused_before_any_browser(
        full_identity, cfg):
    """Guard 1: the interlock, checked before a browser is ever opened."""
    with pytest.raises(optout_submit.SubmissionRefused) as exc:
        optout_submit.submit_optout(_honeypot_recipe(), full_identity, cfg,
                                    submitter=FakeSubmitter(lsm_page()))
    assert "#website" in str(exc.value)


def test_the_honeypot_guard_also_bites_inside_apply_recipe(full_identity):
    """Guard 2, independently: the function that can actually type refuses too.

    Proven separately from guard 1 on purpose -- a single check that a
    refactor could route around is not a guard, and this is the one that
    holds if a future caller reaches apply_recipe another way.
    """
    recipe = _honeypot_recipe()
    resolved = optout_forms.resolve_fields(recipe, full_identity)
    page = lsm_page()

    with pytest.raises(optout_forms.ForbiddenFieldError):
        optout_submit.apply_recipe(page, recipe, resolved)

    assert "#website" not in page.touched


def test_without_the_forbidden_list_the_same_recipe_would_fill_the_honeypot(
        full_identity, cfg):
    """The revert test: the guard is load-bearing, not decorative.

    Identical recipe with ``forbidden_selectors`` emptied -- i.e. the guard
    reverted -- and the honeypot IS filled. That is what the guard prevents.
    """
    unguarded = optout_forms.FormRecipe(
        broker_id="consumer-canvas-llc",     # allow-listed, so it gets that far
        broker_name=LSM.broker_name, url=LSM.url, flavor=LSM.flavor,
        steps=_honeypot_recipe().steps,
        forbidden_selectors=(),              # <-- the guard, reverted
        submit_selector=LSM.submit_selector)
    page = lsm_page()

    optout_submit.submit_optout(unguarded, full_identity, cfg,
                                submitter=FakeSubmitter(page))

    assert page.filled["#website"] == "example.com"


def test_credit_coms_forbidden_list_bites_the_same_way(full_identity, cfg):
    """The same guard, protecting an SSN box rather than a honeypot."""
    leaky = optout_forms.FormRecipe(
        broker_id=CREDIT_COM.broker_id, broker_name=CREDIT_COM.broker_name,
        url=CREDIT_COM.url, flavor=CREDIT_COM.flavor,
        choices=CREDIT_COM.choices,
        fields=CREDIT_COM.fields + (
            optout_forms.Field(selector="#nationalIdDSARElement",
                               source="literal", label="SSN", value="1234"),
        ),
        forbidden_selectors=CREDIT_COM.forbidden_selectors,
        submit_selector=CREDIT_COM.submit_selector)

    with pytest.raises(optout_submit.SubmissionRefused) as exc:
        optout_submit.submit_optout(leaky, full_identity, cfg,
                                    submitter=FakeSubmitter(combo_ready()))
    assert "nationalId" in str(exc.value)


def test_every_shipped_recipe_passes_its_own_forbidden_check():
    for broker_id in optout_forms.supported_broker_ids():
        optout_forms.assert_no_forbidden(optout_forms.recipe_for(broker_id))


# --- the shared plumbing the new recipes lean on -----------------------------

@pytest.mark.parametrize("addresses,expected", [
    (["742 Evergreen Terrace, Springfield, IL 62704"], "62704"),
    (["Springfield, IL 62704-1234"], "62704"),
    (["Springfield, IL"], ""),
    ([], ""),
    (None, ""),
])
def test_zip_is_read_off_the_address_lines(addresses, expected):
    assert optout_forms.zip_from_addresses(addresses) == expected


@pytest.mark.parametrize("phones,expected", [
    (["555-123-4567"], "+15551234567"),
    (["(555) 123 4567"], "+15551234567"),
    (["15551234567"], "+15551234567"),
    (["+44 20 7123 4567"], "+44 20 7123 4567"),
    ([], ""),
    (None, ""),
])
def test_phone_is_normalized_to_international_format(phones, expected):
    assert optout_forms.phone_for_form(phones) == expected


def test_a_four_digit_extension_is_not_mangled_into_a_phone_number():
    """Unrecognized shapes are passed through, never invented."""
    assert optout_forms.phone_for_form(["ext 4567"]) == "ext 4567"


def test_ordered_steps_preserves_the_old_behaviour_for_old_recipes():
    """Consumer Canvas is untouched by the steps mechanism."""
    assert optout_forms.ordered_steps(RECIPE) == \
        tuple(RECIPE.choices) + tuple(RECIPE.fields)


def test_an_unknown_step_type_is_a_loud_error_not_a_silent_skip(full_identity):
    class Weird:
        pass

    recipe = optout_forms.FormRecipe(
        broker_id="x", broker_name="x", url="https://x.invalid/",
        flavor=optout_forms.FLAVOR_LSM_BESPOKE, steps=(Weird(),))

    with pytest.raises(TypeError):
        optout_submit.apply_recipe(FakePage(), recipe, {"values": {}})


def test_every_shipped_recipe_says_whether_it_has_a_bot_check():
    """No recipe may leave "is there a captcha here?" unanswered.

    Originally this asserted every form HAS a bot check, which was true of
    the first batch and is not a law of nature: InfoPay's shared opt-out
    form (courtrecords.us / staterecords.org / recordsfinder.com) carries
    none at all. An empty ``captcha_selectors`` alone cannot distinguish
    "there is no captcha" from "nobody looked", and the two have opposite
    consequences once submission is enabled with dry-run off -- so a recipe
    must state one or the other, and ``no_captcha_verified`` is the explicit
    way to say "swept, genuinely clean".
    """
    for broker_id in optout_forms.supported_broker_ids():
        recipe = optout_forms.recipe_for(broker_id)
        assert recipe.captcha_selectors or recipe.no_captcha_verified, broker_id
        assert recipe.success_markers, broker_id
        assert recipe.notes.strip(), broker_id


def test_a_captcha_free_recipe_says_so_in_its_notes():
    """A no-bot-check recipe is a loaded gun; the notes must warn about it."""
    for broker_id in optout_forms.supported_broker_ids():
        recipe = optout_forms.recipe_for(broker_id)
        if not recipe.no_captcha_verified:
            continue
        assert "no bot check" in recipe.notes.lower().replace("-", " "), broker_id


@pytest.mark.parametrize("addresses,expected", [
    (["742 Evergreen Terrace, Springfield, IL 62704"], "Illinois"),
    (["Springfield, Illinois 62704-1234"], "Illinois"),
    (["Springfield, IL"], "Illinois"),
    # A two-letter word inside a street name is NOT a state: sending a
    # request to the wrong state's regulator-facing form is worse than
    # reporting a missing field.
    (["12 IN Street, Nowhere"], ""),
    (["Springfield, XX 62704"], ""),
])
def test_a_state_is_read_through_a_trailing_zip(addresses, expected):
    assert optout_forms.state_from_addresses(addresses) == expected
