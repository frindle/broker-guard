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


def test_only_consumer_canvas_is_turned_on():
    """The other OneTrust brokers are a deliberate follow-up, not an oversight."""
    assert optout_forms.supported_broker_ids() == ["consumer-canvas-llc"]
    assert not optout_forms.is_supported("nielsen")


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
