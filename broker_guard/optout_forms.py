"""Declarative recipes for the opt-out webforms we know how to drive.

This module is pure data + pure functions: no browser, no I/O, no network.
``optout_submit.py`` is the thing that actually drives a page; everything it
needs to know about a particular broker's form lives HERE, as a
``FormRecipe``. That split is what makes adding the next broker a data
change rather than a code change, and it is what lets the whole mapping be
unit-tested without Playwright installed.

The allow-list is the safety boundary
----------------------------------------
``RECIPES`` is an explicit, hand-written allow-list keyed by broker id.
A broker that is not in it CANNOT be submitted to, full stop -- there is no
"guess the form from the URL" fallback and there must never be one. Filling
someone's real name, email and home state into an arbitrary page that a
970-entry community-maintained dataset happened to label ``web-form`` is
exactly the failure mode this design refuses. Adding a broker means a human
opened its form, read its fields, and wrote them down here.

The OneTrust family is not one form
--------------------------------------
"Hosted on OneTrust" is a hosting fact, not a schema. The dataset currently
carries several OneTrust-flavored URLs and they are NOT interchangeable:

* ``privacyportal-XX.onetrust.com/webform/<org>/<form>`` -- the Angular
  "DSAR webform" this module's ``FLAVOR_ONETRUST_DSAR`` describes. Consumer
  Canvas, Nielsen and bolttech are all this shape.
* ``privacyportal-cdn.onetrust.com/dsarwebform/<org>/<form>.html`` --
  Credit.com. A *statically hosted* variant of the same widget. Opened and
  checked on 2026-09-22: the DOM really is the same Angular widget, same
  element ids, same submit button, same BotDetect CAPTCHA, so it needs no
  flavor of its own. What it does NOT have is country/state -- it asks for
  Address/City/Zip instead. The hosting shape was the red herring; the
  FIELD SET is what differs.
* ``https://www.lsmapps.com/onetrust-opt-out`` -- L.S Mobile Apps. Despite
  the "onetrust" in the path this is not a OneTrust form at all: it
  redirects to ``https://lsmapps.com/opt-out``, a bespoke page with real
  ``<select>`` elements, a confirmation checkbox and a honeypot. It gets
  its own flavor, ``FLAVOR_LSM_BESPOKE``.

So flavor is recorded per recipe, and a recipe is only ever written after
someone has looked at that specific URL.

Order is part of the form
----------------------------
Two of these forms render fields only once an earlier answer is given --
Nielsen's request-type listbox and State field do not exist until Country
is filled, and L.S Mobile's rights dropdown is empty until Territory is
chosen. "All choices, then all fields" cannot express that, so a recipe may
instead give an explicit ordered ``steps`` tuple mixing every step type.
``ordered_steps`` is the single place that decides what runs when.

Some fields must never be filled
-----------------------------------
``forbidden_selectors`` names inputs on a form that this codebase refuses
to touch: L.S Mobile's ``#website`` honeypot (filling it marks the request
as a bot), and Credit.com's SSN and date-of-birth boxes (optional fields
that a "do not sell my data" request has no business volunteering). The
rule is enforced, not documented -- ``assert_no_forbidden`` raises, the
driver calls it before it opens a browser AND again before it types, and
``tests/test_optout_submit.py`` proves both guards bite.
"""
from dataclasses import dataclass, field
import re

# --- flavors -----------------------------------------------------------------

# OneTrust's Angular-rendered DSAR webform (the ``privacyportal-*.onetrust.com
# /webform/<org>/<form>`` shape). Characteristic markers, all verified
# against Consumer Canvas's live form:
#   * text inputs with ids like ``firstNameDSARElement``
#   * "listbox" groups of ``div[role=option][aria-label=...]`` instead of
#     real <select>/<radio> elements -- so they are CLICKED, not selected
#   * country/state are ``role=combobox`` autocompletes: typing alone leaves
#     the model empty, the matching option has to be picked from the popup
#   * a submit button that starts ``disabled`` until the form validates
FLAVOR_ONETRUST_DSAR = "onetrust_dsar_webform"

# L.S Mobile Apps' own page (``lsmapps.com/opt-out``). Nothing OneTrust about
# it, verified against the live page:
#   * plain ``<input id=...>``/``<textarea>``, no Angular listboxes
#   * real ``<select>`` elements, chosen by OPTION LABEL
#   * a required confirmation ``<input type=checkbox>``
#   * a ``#website`` honeypot that must stay empty
#   * a canvas-drawn security code (``<canvas id=canv>`` + ``#captcha``)
FLAVOR_LSM_BESPOKE = "lsmapps_bespoke_form"

# AdvancedBackgroundChecks' own opt-out page (``/opt-out``). A React SPA form
# with no ``<form>`` element at all -- the submit button is wired up in JS --
# plain inputs by id, a ``mode`` <select> ("subject" vs "authorized agent"),
# a ``company`` honeypot, and invisible reCAPTCHA.
FLAVOR_ABGC_BESPOKE = "advancedbackgroundchecks_bespoke_form"

# A Wix-built "DNSMPI" page (achcoop.com): Wix's own generated field ids
# (``form-field-input-<uuid>-comp-...``), a group of request-type checkboxes,
# and a visible captcha checkbox with no id/name at all (only a
# ``Captcha<digits>__checkbox`` class).
FLAVOR_WIX_DNSMPI = "wix_dnsmpi_form"

# BigDBM's dedicated opt-out subdomain (optout.bigdbm.com): a plain
# server-rendered Bootstrap form, an "All / Address only / Phone only /
# Email only" checkbox group, a required attestation checkbox, and reCAPTCHA
# bound directly to the submit button.
FLAVOR_BIGDBM_BESPOKE = "bigdbm_bespoke_form"

# People Data Labs' "Do Not Sell or Share" marketing-site page: just Name +
# Email + a "State of origin" <select>, reCAPTCHA.
FLAVOR_PDL_BESPOKE = "peopledatalabs_bespoke_form"


# --- step types --------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    """Click one option in a OneTrust ``role=listbox`` group.

    Matched by the option's ``aria-label``, which is the visible label and is
    what OneTrust puts on the clickable ``div`` (there is no <select> to
    select and no <input value> to set).
    """

    container: str      # CSS selector for the listbox
    option_label: str   # exact aria-label of the option to click
    label: str          # human name, for the audit record


@dataclass(frozen=True)
class Field:
    """One value to type into the form.

    ``source`` names WHERE the value comes from, resolved by
    ``resolve_fields`` below:

    * ``first_name`` / ``last_name`` / ``email`` -- straight off the profile
    * ``state`` / ``country`` -- derived from the profile's address
    * ``literal``   -- a fixed string from ``value`` (the request-details
      body), never profile data

    ``kind`` says HOW to type it: ``text`` is a plain fill; ``combo`` is the
    type-then-pick-from-the-popup dance the country/state autocompletes
    require; ``select`` is a real ``<select>`` element chosen by the
    resolved value as the option's visible LABEL -- for a plain HTML state
    dropdown that (unlike L.S Mobile's ``Select``/``Choice`` steps, which
    always pick a FIXED literal) has to vary with the profile; ``listbox_button``
    is a collapsed ``role=combobox`` button (ACHCOOP's State field) that must
    be clicked open before its matching ``role=option`` exists to click.
    """

    selector: str
    source: str
    label: str
    kind: str = "text"
    value: str = ""
    required: bool = True


@dataclass(frozen=True)
class Select(Choice):
    """Choose an option in a REAL ``<select>`` element.

    Separate from ``Choice`` because the page action is different: a
    ``<select>`` is driven by ``select_option``, not by clicking a div.
    Inherits ``Choice``'s ``container``/``option_label``/``label`` shape --
    ``container`` is the select's own selector -- so a recipe reads the same
    way whichever widget the broker happened to use.
    """


@dataclass(frozen=True)
class Check:
    """Tick a checkbox (L.S Mobile's required "this is accurate" box)."""

    selector: str
    label: str


@dataclass(frozen=True)
class FormRecipe:
    """Everything needed to drive one broker's opt-out form."""

    broker_id: str
    broker_name: str
    url: str
    flavor: str
    choices: tuple = ()
    fields: tuple = ()
    # An explicit ordered run of steps, for forms where a later field does
    # not EXIST until an earlier one is answered. When set it replaces
    # choices+fields entirely; see ``ordered_steps``.
    steps: tuple = ()
    # Inputs on this form that must never be touched: honeypots, and
    # optional fields we decline to volunteer (SSN, date of birth).
    forbidden_selectors: tuple = ()
    submit_selector: str = ""
    # Selectors whose PRESENCE means "there is a bot check on this page".
    # Purely additive to optout_submit's generic detection -- a form-specific
    # widget that the generic sweep would miss goes here.
    captcha_selectors: tuple = ()
    # Text that appears on the page after a successful submission. Used only
    # to classify the result AFTER a real submit; never to decide whether to
    # submit.
    success_markers: tuple = ()
    notes: str = ""


# --- the allow-list ----------------------------------------------------------

# The standing request text. Deliberately a fixed literal and not composed
# from profile data: it is sent verbatim to a third party, so what it says is
# reviewable here rather than assembled at runtime.
_OPT_OUT_DETAILS = (
    "I am exercising my right to opt out of the sale, sharing and targeted-"
    "advertising use of my personal information, and I request that you do "
    "not sell or share my personal information. Please confirm in writing "
    "once this request has been processed."
)

CONSUMER_CANVAS = FormRecipe(
    broker_id="consumer-canvas-llc",
    broker_name="CONSUMER CANVAS LLC",
    url=(
        "https://privacyportal-eu.onetrust.com/webform/"
        "07a2e8c2-346a-4f58-865f-0d56c9baaff5/e6013ecc-3719-4f58-adb1-5121ea4797f8"
    ),
    flavor=FLAVOR_ONETRUST_DSAR,
    choices=(
        # Order matters: the request-type listbox is not rendered until a
        # subject type has been chosen.
        Choice(container="#subjectTypesDSARElement",
               option_label="U.S. Consumer",
               label="I am a (an)"),
        Choice(container="#requestTypesDSARElement",
               option_label="Opt Out of Sales, Sharing, or Targeted Advertising of "
                            "My Personal Information",
               label="Request type"),
    ),
    fields=(
        Field(selector="#firstNameDSARElement", source="first_name", label="First Name"),
        Field(selector="#lastNameDSARElement", source="last_name", label="Last Name"),
        Field(selector="#emailDSARElement", source="email", label="Email"),
        Field(selector="#countryDSARElement", source="country", label="Country", kind="combo"),
        Field(selector="#stateDSARElement", source="state", label="State", kind="combo"),
        Field(selector="#requestDetailsDSARElement", source="literal",
              label="Request Details", value=_OPT_OUT_DETAILS, required=False),
    ),
    submit_selector="#dsar-webform-submit-button",
    captcha_selectors=(
        # BotDetect. Confirmed present and VISIBLE on this form: a 6-character
        # image CAPTCHA (`captcha.onetrust.com/bc/botdetectcaptcha`) plus four
        # hidden BDC_* bookkeeping inputs. Its presence is why a real
        # submission to this broker bails to "needs manual action" by design
        # -- see optout_submit's module docstring.
        "#captchaCode",
        "input[name^='BDC_VCID']",
        "img[src*='botdetectcaptcha']",
    ),
    success_markers=(
        "your request has been submitted",
        "thank you for submitting",
        "request id",
    ),
    notes=(
        "Verified against the live form on 2026-09-22: subject-type and "
        "request-type are click-only listboxes, country/state are combobox "
        "autocompletes, and the form carries a mandatory BotDetect image "
        "CAPTCHA."
    ),
)


# A BotDetect image CAPTCHA, the OneTrust default. Same widget on Consumer
# Canvas, Nielsen and Credit.com, so the selectors are named once.
_BOTDETECT_SELECTORS = (
    "#captchaCode",
    "input[name^='BDC_VCID']",
    "img[src*='botdetectcaptcha']",
)

# Every OneTrust DSAR webform confirms with the same wording.
_ONETRUST_SUCCESS = (
    "your request has been submitted",
    "thank you for submitting",
    "request id",
)

NIELSEN = FormRecipe(
    broker_id="nielsen",
    broker_name="Nielsen",
    url=(
        "https://privacyportal-de.onetrust.com/webform/"
        "70b0083d-d519-4ad2-84ca-96b7c5f8e1a9/9810a8bc-e54d-4d70-bac0-5e4d781ef5b9"
    ),
    flavor=FLAVOR_ONETRUST_DSAR,
    # ORDERED, and the order is load-bearing: on the live form the
    # request-type listbox and the State field do not exist in the DOM at
    # all until Country has been filled. Running Consumer Canvas's
    # choices-then-fields order here clicks a selector that is not there.
    steps=(
        Choice(container="#subjectTypesDSARElement",
               option_label="Other (see description)",
               label="I am a (an)"),
        Field(selector="#countryDSARElement", source="country",
              label="Country", kind="combo"),
        Choice(container="#requestTypesDSARElement",
               # Nielsen's own typo ("of of"). Matched verbatim on purpose:
               # this is an aria-label lookup, not prose.
               option_label="Right to Opt Out of of Sale or Sharing",
               label="Request type"),
        Field(selector="#stateDSARElement", source="state", label="State", kind="combo"),
        Field(selector="#firstNameDSARElement", source="first_name", label="First Name"),
        Field(selector="#lastNameDSARElement", source="last_name", label="Last Name"),
        Field(selector="#emailDSARElement", source="email", label="Email"),
        Field(selector="#zipDSARElement", source="zip", label="Zip", required=False),
    ),
    submit_selector="#dsar-webform-submit-button",
    captcha_selectors=_BOTDETECT_SELECTORS,
    success_markers=_ONETRUST_SUCCESS,
    notes=(
        "Verified against the live form on 2026-09-22. Two surprises versus "
        "Consumer Canvas: (1) the subject-type labels are Nielsen's own "
        "panel/employee vocabulary and there is NO consumer option at all -- "
        "'Other (see description)' is the only honest pick for someone who is "
        "just a US resident; (2) the request-type listbox and the State field "
        "are rendered only after Country is filled, which is why this recipe "
        "is ordered. There is no Request Details box on this form, so the "
        "standing request text has nowhere to go. Carries the BotDetect image "
        "CAPTCHA, so a real run always stops at 'needs manual action'."
    ),
)

BOLTTECH = FormRecipe(
    broker_id="bolttech",
    broker_name="bolttech (Boltech)",
    url=(
        "https://privacyportal-de.onetrust.com/webform/"
        "644d2a38-e3d6-43db-99be-9b758a433b86/64d8a391-f017-4626-bc66-17101e4eeb49"
    ),
    flavor=FLAVOR_ONETRUST_DSAR,
    choices=(
        Choice(container="#subjectTypesDSARElement",
               option_label="Consumer",
               label="I am a (an)"),
        Choice(container="#requestTypesDSARElement",
               option_label="Request to Opt-Out (Do Not Sell or Share My Personal "
                            "Information)",
               label="Request type"),
    ),
    fields=(
        Field(selector="#firstNameDSARElement", source="first_name", label="First name"),
        Field(selector="#lastNameDSARElement", source="last_name", label="Last name"),
        Field(selector="#emailDSARElement", source="email", label="Email"),
        # Country before State: the State autocomplete is populated from it.
        Field(selector="#countryDSARElement", source="country",
              label="Country or Location of Residence", kind="combo"),
        Field(selector="#stateDSARElement", source="state", label="State", kind="combo"),
    ),
    submit_selector="#dsar-webform-submit-button",
    captcha_selectors=(
        # reCAPTCHA v2, NOT BotDetect -- the generic sweep in optout_submit
        # already catches the iframe; these are belt and braces.
        "#g-recaptcha-response",
        "iframe[src*='recaptcha']",
    ),
    success_markers=_ONETRUST_SUCCESS,
    notes=(
        "Verified against the live form on 2026-09-22. Closest of the four to "
        "Consumer Canvas: both listboxes are present up front, so no ordered "
        "steps are needed. Labels are bolttech's own ('Consumer', 'Request to "
        "Opt-Out (Do Not Sell or Share My Personal Information)') and differ "
        "from Consumer Canvas's, which is exactly why they were read off the "
        "live page. No Request Details box. Phone number is offered but "
        "optional and sits behind a separate country-code combobox, so it is "
        "deliberately not filled. Bot check is Google reCAPTCHA v2."
    ),
)

CREDIT_COM = FormRecipe(
    broker_id="credit-com",
    broker_name="Credit.com",
    url=(
        "https://privacyportal-cdn.onetrust.com/dsarwebform/"
        "e5972974-adf3-405e-b919-62b20ae438a0/eb0adcee-1fed-4068-9801-85b289d900b6.html"
    ),
    # Same flavor as the rest, and that is a FINDING, not an assumption: the
    # CDN-hosted page was opened and its DOM is the same Angular widget with
    # the same element ids. The hosting path differs; the driver does not
    # need to.
    flavor=FLAVOR_ONETRUST_DSAR,
    choices=(
        Choice(container="#subjectTypesDSARElement",
               option_label="Consumer",
               label="I am a (an)"),
        Choice(container="#requestTypesDSARElement",
               option_label="Do Not Sell or Share My Personal Information",
               label="Request type"),
    ),
    fields=(
        Field(selector="#firstNameDSARElement", source="first_name", label="First Name"),
        Field(selector="#lastNameDSARElement", source="last_name", label="Last Name"),
        Field(selector="#emailDSARElement", source="email", label="Email"),
        # This form has no country/state. It wants a postal address instead,
        # and makes Address and Zip required.
        Field(selector="#addressDSARElement", source="address", label="Address"),
        Field(selector="#zipDSARElement", source="zip", label="Zip"),
        Field(selector="#requestDetailsDSARElement", source="literal",
              label="Request Details", value=_OPT_OUT_DETAILS, required=False),
    ),
    forbidden_selectors=(
        # Both optional on the form, and neither is any of Credit.com's
        # business for an opt-out request. Enforced, see assert_no_forbidden.
        "#nationalIdDSARElement",
        "#dateOfBirthDSARElement",
    ),
    submit_selector="#dsar-webform-submit-button",
    captcha_selectors=_BOTDETECT_SELECTORS,
    success_markers=_ONETRUST_SUCCESS,
    notes=(
        "Verified against the live CDN-hosted form on 2026-09-22. The prior "
        "agent's worry that the 'privacyportal-cdn.../dsarwebform/....html' "
        "shape might need its own flavor was checked and is not borne out: "
        "identical widget, identical ids, identical submit button, BotDetect "
        "CAPTCHA. What genuinely differs is the field set -- no Country or "
        "State, but required Address and Zip, plus OPTIONAL SSN-last-4 and "
        "date-of-birth boxes that this recipe refuses to fill."
    ),
)


LS_MOBILE_APPS = FormRecipe(
    broker_id="ls-mobile-apps-holdings-ltd",
    broker_name="L.S Mobile Apps Holdings Ltd",
    # The dataset records https://www.lsmapps.com/onetrust-opt-out, which
    # 301s to this. The settled URL is recorded here so the driver does not
    # depend on a redirect staying in place.
    url="https://lsmapps.com/opt-out",
    flavor=FLAVOR_LSM_BESPOKE,
    steps=(
        Field(selector="#fullName", source="full_name", label="Full name"),
        Field(selector="#email", source="email", label="Email address"),
        Field(selector="#phoneNumber", source="phone", label="Phone number"),
        # "Are you a user of our App?" -- answered No because that is the
        # true answer for someone who is opting out of a data holding they
        # never signed up for. If Penn DOES use one of their apps, this is
        # the line to change to "Yes"; it is a factual claim made in his
        # name, which is why it is a hand-written literal and not a guess.
        Select(container="#appUser", option_label="No",
               label="Are you a user of our App?"),
        # Territory must be chosen BEFORE the rights dropdown: until it is,
        # #privacyRight holds a single placeholder option reading "Select
        # your territory first".
        Select(container="#territory", option_label="US", label="Territory"),
        Select(container="#privacyRight",
               option_label="Right to Opt-Out of Sale of Personal Information",
               label="Which right do you wish to exercise?"),
        Field(selector="#details", source="literal", label="Additional details",
              value=_OPT_OUT_DETAILS, required=False),
        Check(selector="#confirmation", label="Confirmation of accuracy"),
    ),
    forbidden_selectors=(
        # The honeypot. Offscreen (-left-[9999px]), aria-hidden, tabindex=-1,
        # autocomplete=off: a bot trap, and the one field on this form whose
        # correct value is "untouched".
        "#website",
    ),
    submit_selector="button[type='submit']",
    captcha_selectors=(
        # A canvas-drawn security code. The generic sweep's
        # input[id*='captcha'] already matches #captcha; the canvas is named
        # here so detection does not hinge on one id spelling.
        "#captcha",
        "canvas#canv",
    ),
    success_markers=(
        "thank you",
        "your request has been received",
        "request has been submitted",
    ),
    notes=(
        "Verified against the live page on 2026-09-22. NOT a OneTrust form "
        "despite the 'onetrust-opt-out' path in the dataset URL, which "
        "redirects to lsmapps.com/opt-out. Real <select> elements, a required "
        "confirmation checkbox, a #website honeypot, and -- not flagged in "
        "the brief -- its OWN CAPTCHA: a canvas-drawn security code with a "
        "#captcha input. So this form also stops at 'needs manual action' on "
        "a real run. The rights dropdown is populated from Territory, hence "
        "ordered steps. Requires a phone number in international format."
    ),
)


ADVANCEDBACKGROUNDCHECKS = FormRecipe(
    broker_id="advancedbackgroundchecks-com",
    broker_name="AdvancedBackgroundChecks",
    url="https://www.advancedbackgroundchecks.com/opt-out",
    flavor=FLAVOR_ABGC_BESPOKE,
    steps=(
        # Already the default option, but chosen explicitly rather than
        # relied upon -- the same reasoning as L.S Mobile's territory pick.
        Select(container="#mode", option_label="The subject of the request",
               label="I am"),
        Field(selector="#sfn", source="first_name", label="First name"),
        Field(selector="#sln", source="last_name", label="Last name"),
        Field(selector="#semail", source="email", label="Email address"),
    ),
    forbidden_selectors=(
        # A honeypot: offscreen (-left-[9999px], height/width 0, opacity 0),
        # named "company" the way a lead-gen form would to bait a scraper
        # into filling in an organization name.
        "input[name='company']",
    ),
    submit_selector="button:has-text('Submit')",
    captcha_selectors=(
        # Invisible reCAPTCHA v3: no visible widget to solve, scored
        # silently. The iframe/response-textarea are injected by Google's
        # async script and were NOT yet in the DOM at the point
        # optout_submit.detect_captcha runs in a real dry-run attempt (see
        # notes) -- named here anyway, belt and braces, for whenever the
        # script does win the race.
        "textarea[name='g-recaptcha-response']",
    ),
    success_markers=(
        "we have received your request",
        "check your email",
    ),
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity): the form fills correctly and stops "
        "before Submit, screenshot confirms it. NOT a OneTrust form: a "
        "React SPA with no <form> element at all, plain input ids, one "
        "real <select> ('I am' -- subject vs. authorized agent), and a "
        "'company' honeypot. TWO surprises versus the OneTrust family: (1) "
        "this page is only step 1 of a two-step MAGIC-LINK flow -- its own "
        "copy says submitting here only emails a link to a SECOND page "
        "that carries the actual opt-out form, which nothing in this "
        "codebase can click through unattended, so success_markers "
        "describes 'the link request was accepted', not 'the opt-out is "
        "complete'; (2) the reCAPTCHA here is invisible v3 (no widget to "
        "solve), and in the dry-run verification run its iframe/response "
        "textarea had not yet been injected by Google's async script at "
        "the point optout_submit.detect_captcha runs, so THIS FORM CAN "
        "REACH A REAL, UNATTENDED SUBMIT without ever tripping the "
        "captcha-stop safety net -- worth Penn's attention before ever "
        "flipping BG_OPTOUT_SUBMIT_ENABLED for this broker, since 'stop and "
        "ask a human' is the intended behavior for any bot check, visible "
        "or not. Middle name (#smn) is optional and is not filled -- "
        "profile.Identity carries one, but no existing Field source "
        "resolves it and this form does not require it."
    ),
)


# Keyed by the broker id. Every entry here has been opened, read and
# transcribed by hand; see each recipe's ``notes`` for the date and the
# surprises. Being listed here is necessary but NOT sufficient for a real
# submission -- the enabled flag, the dry-run flag and Playwright are three
# further interlocks, and as it happens most of these forms carry a
# CAPTCHA, so a live run stops at "needs manual action" by design.
SEARCHPUBLICRECORDS = FormRecipe(
    broker_id="searchpublicrecords-com",
    broker_name="Search Public Records (Civil Data Research, LLC)",
    url="https://www.searchpublicrecords.com/help-center/privacy-requests",
    flavor="searchpublicrecords_bespoke_form",
    steps=(
        Select(container="#requestType", option_label="Do Not Sell My Info",
               label="Request Type"),
        Field(selector="#firstName", source="first_name", label="First Name"),
        # The real "Last Name" input -- confirmed via its <label for=...>,
        # not a honeypot -- but this form builder gave it a randomized id
        # and name attribute instead of "lastName".
        Field(selector="#Eeb8d156aa7a02436", source="last_name", label="Last Name"),
        Field(selector="#city", source="city", label="City"),
        # A real <select> whose correct choice varies with the profile
        # (unlike L.S Mobile's fixed-literal Select/Choice steps), hence
        # kind="select" rather than a Choice.
        Field(selector="#states", source="state", label="State", kind="select"),
        Field(selector="#zip", source="zip", label="ZIP"),
        # Required by the form and NOT resolvable: this codebase collects no
        # age/date-of-birth anywhere on Identity, and will not start
        # fabricating one to hand to a data broker under Penn's name (the
        # same refusal Credit.com's recipe makes for SSN/DOB, just for a
        # field this form does not let us skip). A literal empty value
        # means resolve_fields always reports "Age" missing and
        # submit_optout refuses BEFORE opening a browser -- see notes.
        Field(selector="#age", source="literal", label="Age", value="",
              required=True),
        Field(selector="#email", source="email", label="Confirmation Email"),
    ),
    submit_selector="#submit-button",
    captcha_selectors=(
        # Cloudflare Turnstile, explicit data-sitekey. The generic sweep's
        # .cf-turnstile / iframe[src*='turnstile'] already catch it.
        ".cf-turnstile",
    ),
    success_markers=(
        "your request has been received",
        "thank you",
    ),
    notes=(
        "Verified against the live page on 2026-09-22 (page structure read "
        "by hand; NOT dry-run-verified end to end, because the form's own "
        "Age select is REQUIRED and this codebase has nowhere to source an "
        "age or date of birth from -- the literal empty value on #age makes "
        "resolve_fields report it missing for every identity, which is "
        "confirmed by test, and submit_optout's own missing-field guard "
        "(proven for Credit.com's zip) stops the attempt before a browser "
        "ever opens. Also carries a mandatory Cloudflare Turnstile, so even "
        "a hypothetical future run (Identity extended with an age) would "
        "still stop at 'needs manual action'. The State <select> lists full "
        "state names matching US_STATES's values exactly, chosen by a new "
        "Field(kind='select') (added for this recipe -- see optout_submit's "
        "_select_field) rather than a fixed-literal Select/Choice, because "
        "the correct state varies with the profile the way a combo field's "
        "typed text does. The 'Last Name' input's id/name is a form-"
        "builder-randomized string, confirmed real via its own <label for>, "
        "not a honeypot. #email's container is hidden at page load and may "
        "only "
        "become visible after the required fields above it validate -- "
        "moot in practice since Age is always missing first."
    ),
)


ACHCOOP = FormRecipe(
    broker_id="achcoop-com",
    broker_name="ACH, Address Clearing House",
    url="https://www.achcoop.com/do-not-sell-my-personal-info",
    flavor=FLAVOR_WIX_DNSMPI,
    steps=(
        Field(selector="#form-field-input-5858d39a-427c-455d-56b0-fc7bec83fd9d-comp-mhuo5c7k-",
              source="first_name", label="First name"),
        Field(selector="#form-field-input-201e217e-9a38-4058-d1bd-6e4d601bfc60-comp-mhuo5c7k-",
              source="last_name", label="Last name"),
        Field(selector="#form-field-input-3bf1bced-9e0f-4f9f-0a88-6d3a2a043ef1-comp-mhuo5c7k-",
              source="street", label="Address 1"),
        Field(selector="#form-field-input-57b8968e-f5e4-4f12-09e3-db9f625a7a15-comp-mhuo5c7k-",
              source="city", label="City"),
        # Required (marked with *) and NOT a native <select> -- a collapsed
        # Wix combobox BUTTON whose options only render into its
        # aria-controls target once opened. Under this codebase's actual
        # production browser context (OptOutSubmitter's own configured
        # Chrome/124 user agent) that target element never appears in the
        # DOM at all after the open click, confirmed by direct DOM
        # inspection (not a timing issue -- waited up to several seconds);
        # it DOES appear under Playwright's own default (much newer) UA.
        # Rather than ship a recipe that reliably times out in the exact
        # configuration that will actually run it, this is a literal empty
        # required value, same refuse-before-opening-a-browser reasoning as
        # SEARCHPUBLICRECORDS's Age field -- an honest, fast "missing
        # required field" beats a 30-second timeout into a "failed" record.
        # Revisit if OptOutSubmitter's default UA is ever modernized.
        Field(selector="[role='combobox'][aria-label='State']", source="literal",
              label="State", value="", required=True),
        Field(selector="#form-field-input-641bda48-c9a3-4499-9f8e-eea0047b582c-comp-mhuo5c7k-",
              source="zip", label="Zip Code"),
        # "Select your request" is a group of 7 independent checkboxes, not
        # a listbox -- these two are the ones that add up to a full removal
        # ("stop selling my data" + "delete me"). The other five (access,
        # correction, ad-personalization opt-out, profiling opt-out) are
        # different CCPA rights this tool is not asked to exercise.
        Check(selector="#checkbox-18", label="Do Not Sell My Personal Information"),
        Check(selector="#checkbox-21", label="Remove Me From Your Database"),
    ),
    # Address 2 (Apt/Unit) is explicitly optional on the page ("ONLY enter
    # if applicable") and this tool has no unit-number field to source it
    # from; left unfilled rather than guessed.
    submit_selector="button:has-text('Submit')",
    captcha_selectors=(
        # No id/name at all -- only this class -- so the generic sweep's
        # id/name-based captcha patterns would miss it.
        "[class*='Captcha' i]",
    ),
    success_markers=("thank you", "request has been received", "we'll be in touch"),
    notes=(
        "Investigated live on 2026-09-22. A Wix site -- text-input "
        "selectors are Wix's own generated ids, confirmed IDENTICAL across "
        "separate page loads minutes apart, so treated as stable rather "
        "than per-request-random. SURPRISE #1, caught only by reading the "
        "dry-run screenshot rather than trusting the JSON record: the "
        "first pass missed the required State field entirely, because it "
        "is not an <input>/<select>/<textarea> at all -- a collapsed Wix "
        "combobox BUTTON (role=combobox) whose options only render into "
        "its aria-controls target once opened -- so a page-wide "
        "input/select/textarea sweep finds nothing there. SURPRISE #2: "
        "under Playwright's own default browser UA that target renders "
        "fine (51 two-letter-code options), but under THIS codebase's "
        "actual production UA (OptOutSubmitter's configured Chrome/124 "
        "string) it never appears in the DOM at all after the open click, "
        "confirmed by direct inspection, not merely a slow render -- so "
        "State is deliberately a literal empty required value (self-"
        "refuses before opening a browser), same reasoning as "
        "SEARCHPUBLICRECORDS's Age field, rather than shipping something "
        "that reliably times out in the one configuration that will "
        "actually run it. The rest of the form (name/street/city/zip, the "
        "checkbox pair, the captcha) IS dry-run-verified end to end -- see "
        "the driver's own new listbox_button/_pick_listbox_button code, "
        "added for this field and left in place for any future broker "
        "whose UA-sensitivity turns out to be more forgiving. Also carries "
        "a visible captcha widget with neither id nor name (only a "
        "'Captcha<digits>__checkbox' class), which the generic captcha "
        "sweep's id/name patterns would NOT have caught -- added as an "
        "explicit captcha_selectors entry for that reason. The 'Select "
        "your request' checkbox group visually LOOKED like only one box "
        "took on an early screenshot despite both being recorded checked; "
        "confirmed by reading .checked directly on both inputs after the "
        "same two check() calls this recipe uses -- both are true, the "
        "second box's checkmark icon simply had not repainted yet at the "
        "instant that screenshot was captured. success_markers are a "
        "plausible guess at a Wix form's default confirmation wording, NOT "
        "read off a real submitted page (this recipe never reaches "
        "Submit, now permanently, on the missing State field) -- do not "
        "treat a run that reports 'confirmed' via this marker as gospel."
    ),
)

BIGDBM = FormRecipe(
    broker_id="bigdbm-com",
    broker_name="BIGDBM",
    url="https://optout.bigdbm.com/",
    flavor=FLAVOR_BIGDBM_BESPOKE,
    steps=(
        Field(selector="#horizontal-firstname-input", source="first_name", label="First Name"),
        Field(selector="#horizontal-lastname-input", source="last_name", label="Last Name"),
        Field(selector="#horizontal-email-input", source="email", label="Email"),
        Field(selector="#horizontal-phone-input", source="phone", label="Phone"),
        Field(selector="#horizontal-address1-input", source="street", label="Address1"),
        Field(selector="#horizontal-city-input", source="city", label="City"),
        # This <select>'s options are bare two-letter codes ("IL", not
        # "Illinois") -- source="state_code", not "state".
        Field(selector="#horizontal-state-input", source="state_code", label="State", kind="select"),
        Field(selector="#horizontal-zip-input", source="zip", label="ZipCode"),
        # "All" (vs. Address/Phone/Email Only) -- a full opt-out, not one
        # narrowed to a single data category.
        Check(selector="#formCheckAll", label="All"),
        Check(selector="#inlineFormCheck", label="I confirm this information is accurate"),
    ),
    # Address2 and the optional supporting-document file upload are left
    # blank -- no source for either, and a document upload is not something
    # this tool volunteers.
    submit_selector="#main-submit-button",
    captcha_selectors=(
        # The submit button itself carries the g-recaptcha class (invisible
        # v3/v2-invisible bound to the button), on top of the page's own
        # #g-recaptcha-response textarea that the generic sweep already
        # matches via .g-recaptcha.
        "#main-submit-button.g-recaptcha",
    ),
    success_markers=("thank you", "request has been received", "successfully submitted"),
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity): fills correctly and stops before "
        "Submit, screenshot confirms it. Reached this optout.bigdbm.com "
        "subdomain only after a 429 rate-limit wall on the FIRST attempt "
        "with a Linux Chrome UA -- retrying (this codebase's default UA) "
        "succeeded, suggesting a velocity/UA heuristic rather than a "
        "persistent block; worth remembering if this recipe ever starts "
        "erroring in production. success_markers are a plausible guess, "
        "NOT read off a real submitted page -- see the same caveat on "
        "ACHCOOP's recipe."
    ),
)

PEOPLEDATALABS = FormRecipe(
    broker_id="peopledatalabs-com",
    broker_name="People Data Labs",
    url="https://www.peopledatalabs.com/do-not-sell-or-share",
    flavor=FLAVOR_PDL_BESPOKE,
    steps=(
        Field(selector="#name", source="full_name", label="Full Name"),
        Field(selector="#email", source="email", label="Email"),
        # The <select> only enumerates NY/California/Oregon/Montana by name;
        # every other US state is meant to be filed under "Other US State".
        # This tool has no per-state mapping table, so it always answers
        # "Other US State" rather than fabricating a match to one of the
        # four named ones -- true for anyone not in those four states, and
        # merely LESS PRECISE (never false) for anyone who is. See notes.
        Select(container="#origin-location", option_label="Other US State",
               label="State of origin"),
    ),
    submit_selector="#submit",
    captcha_selectors=(
        # Redundant with the generic sweep's own #g-recaptcha-response
        # pattern (this page carries that textarea already) -- named
        # explicitly anyway so this recipe is never read as "nobody
        # checked for a bot wall here".
        "#g-recaptcha-response",
    ),
    success_markers=("thank you", "request has been received", "we've received your request"),
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity): fills correctly and stops before "
        "Submit, screenshot confirms it. The simplest form seen in this "
        "pilot: only Full Name + Email are real Identity-sourced fields. "
        "'State of origin' is a fixed literal ('Other US State'), not "
        "identity-derived, for the reason in the field comment above -- "
        "flag for a human if People Data Labs is ever the priority broker "
        "for someone who IS in NY/California/Oregon/Montana, since a more "
        "specific answer would be available but is not sent. "
        "success_markers are a plausible guess, NOT read off a real "
        "submitted page -- see the same caveat on ACHCOOP's recipe."
    ),
)


RECIPES = {
    CONSUMER_CANVAS.broker_id: CONSUMER_CANVAS,
    NIELSEN.broker_id: NIELSEN,
    BOLTTECH.broker_id: BOLTTECH,
    CREDIT_COM.broker_id: CREDIT_COM,
    LS_MOBILE_APPS.broker_id: LS_MOBILE_APPS,
    ADVANCEDBACKGROUNDCHECKS.broker_id: ADVANCEDBACKGROUNDCHECKS,
    SEARCHPUBLICRECORDS.broker_id: SEARCHPUBLICRECORDS,
    ACHCOOP.broker_id: ACHCOOP,
    BIGDBM.broker_id: BIGDBM,
    PEOPLEDATALABS.broker_id: PEOPLEDATALABS,
}


# Brokers whose form has been WRITTEN DOWN but which are not yet turned on.
# Empty today; it exists so that "we transcribed the form" and "we are willing
# to submit to it" stay two separate decisions.
STAGED_RECIPES: dict = {}


# Brokers investigated for this pilot and found to have NO self-service
# consumer opt-out surface at all -- the opt-out-leg twin of
# ``search_forms.NO_SEARCH_SURFACE``. Recorded as data, with the reason, so
# the gap is not silently re-investigated or "fixed" by writing a recipe
# against a page that cannot actually honor a removal request (an
# authenticated portal requiring SSN/DOB, a mailbox-only channel with no
# webform, etc). These are notes, not behaviour: nothing reads this at
# runtime.
NO_OPTOUT_SURFACE = {
    "chexsystems-com": (
        "ChexSystems is a nationwide specialty CRA under the FCRA. Verified "
        "2026-09-22: there is no self-service opt-out webform at all -- "
        "every consumer path (security freeze, dispute, disclosure) requires "
        "logging into the authenticated Consumer Portal at "
        "chexsystems-ds.fiscloudservices.com with identity-verified "
        "credentials (which can require SSN + DOB), which this tool must "
        "not automate."
    ),
}


class RecipeNotFound(KeyError):
    """No verified form recipe exists for this broker."""


class ForbiddenFieldError(ValueError):
    """A recipe tried to target an input this codebase refuses to fill."""


def ordered_steps(recipe: FormRecipe) -> tuple:
    """Every step of *recipe*, in the order the driver must run them.

    A recipe with an explicit ``steps`` tuple is run exactly as written --
    that is the whole point of it, because on Nielsen's form the
    request-type listbox does not exist until Country has been filled.
    Otherwise the historical order applies: all choices, then all fields
    (Consumer Canvas's request-type listbox is not rendered until a subject
    type is chosen, and nothing else on that form is order-sensitive).
    """
    if recipe.steps:
        return tuple(recipe.steps)
    return tuple(recipe.choices) + tuple(recipe.fields)


def recipe_fields(recipe: FormRecipe) -> tuple:
    """The ``Field`` steps of *recipe*, whichever way it was written."""
    return tuple(s for s in ordered_steps(recipe) if isinstance(s, Field))


def targeted_selectors(recipe: FormRecipe) -> tuple:
    """Every selector *recipe* would touch, including its submit button."""
    out = []
    for step in ordered_steps(recipe):
        if isinstance(step, Field):
            out.append(step.selector)
        elif isinstance(step, Check):
            out.append(step.selector)
        elif isinstance(step, Choice):     # covers Select
            out.append(step.container)
    if recipe.submit_selector:
        out.append(recipe.submit_selector)
    return tuple(out)


def assert_no_forbidden(recipe: FormRecipe) -> None:
    """Raise if *recipe* targets one of its own ``forbidden_selectors``.

    The honeypot rule with teeth. ``#website`` on L.S Mobile's form is an
    offscreen, ``aria-hidden``, ``tabindex=-1`` bot trap: a request that
    arrives with it filled is a request that gets binned, and worse, it is
    this tool announcing itself as a bot on the person's behalf. Credit.com
    likewise asks for SSN-last-4 and date of birth, both optional, and a
    "do not sell my data" request does not need to hand over either.

    A recipe is data, and data gets edited; this is the check that turns
    "we wrote it down correctly" into "it cannot be wrong at runtime".
    """
    forbidden = {s for s in (recipe.forbidden_selectors or ())}
    if not forbidden:
        return
    clashes = sorted(forbidden.intersection(targeted_selectors(recipe)))
    if clashes:
        raise ForbiddenFieldError(
            "recipe {!r} targets forbidden input(s): {}".format(
                recipe.broker_id, ", ".join(clashes)))


def recipe_for(broker_id: str) -> FormRecipe:
    """The recipe for *broker_id*, or raise ``RecipeNotFound``.

    Raises rather than returning ``None`` so a caller cannot accidentally
    treat "we have no idea what this form looks like" as "nothing to fill".
    """
    recipe = RECIPES.get((broker_id or "").strip())
    if recipe is None:
        raise RecipeNotFound(broker_id)
    return recipe


def supported_broker_ids() -> list[str]:
    """Every broker id that can currently be submitted to, sorted."""
    return sorted(RECIPES)


def is_supported(broker_id: str) -> bool:
    return (broker_id or "").strip() in RECIPES


# --- profile -> field values -------------------------------------------------

# Two-letter US state codes, so an address line ending in ", IL" can be turned
# into the "Illinois" that a OneTrust state autocomplete actually lists. Only
# the expansion direction is needed: the combo fill types the full name and
# picks the matching option.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

DEFAULT_COUNTRY = "United States"


def state_from_addresses(addresses) -> str:
    """The full US state name implied by a profile's address lines, or "".

    ``profile.load_profile`` synthesizes a ``"City, ST"`` line from the
    example profile's ``city``/``state`` keys, so the last comma-separated
    token of an address line is where the state code lives. Both a bare code
    (``IL``) and an already-spelled-out name (``Illinois``) are accepted; the
    full name is returned either way, because that is what the form's
    autocomplete lists.

    A trailing ZIP is tolerated: a real address line ends ``", IL 62704"``,
    not ``", IL"``, and reading that as "no state" used to make Nielsen's and
    bolttech's required State field come back missing. Only the exact shapes
    ``IL``, ``Illinois`` and ``IL 62704`` are accepted -- a two-letter word
    that merely happens to sit inside a street name is NOT treated as a
    state, because a wrong state is sent to a third party under the person's
    name.

    Returns "" when no address line yields a recognizable state -- the caller
    reports that as a missing required field rather than guessing.
    """
    by_name = {name.lower(): name for name in US_STATES.values()}
    for line in reversed(list(addresses or [])):
        if not isinstance(line, str):
            continue
        for token in reversed([t.strip() for t in line.split(",") if t.strip()]):
            code = token.upper()
            if code in US_STATES:
                return US_STATES[code]
            if token.lower() in by_name:
                return by_name[token.lower()]
            # "IL 62704" / "Illinois 62704-1234"
            with_zip = re.match(r"^(.*?)\s+\d{5}(?:-\d{4})?$", token)
            if with_zip:
                head = with_zip.group(1).strip()
                if head.upper() in US_STATES:
                    return US_STATES[head.upper()]
                if head.lower() in by_name:
                    return by_name[head.lower()]
    return ""


def state_code_from_addresses(addresses) -> str:
    """The two-letter US state CODE implied by a profile's address, or "".

    ``state_from_addresses`` returns the full name because that is what
    most brokers' state widgets show; BigDBM's plain ``<select>`` instead
    lists bare codes (``<option value="IL">IL</option>``, not "Illinois"),
    so a recipe needs the code as the value handed to ``kind="select"``'s
    label match. Reuses ``state_from_addresses``'s parsing rather than
    duplicating it, then looks the code back up from the name.
    """
    name = state_from_addresses(addresses)
    if not name:
        return ""
    for code, full in US_STATES.items():
        if full == name:
            return code
    return ""


def _state_token_index(tokens: list) -> int:
    """Index in *tokens* of the one that names a US state, or -1.

    Shared anchor logic for ``city_from_addresses`` and
    ``street_only_from_addresses``: both need to know WHICH comma-separated
    token is the state (accepting a trailing zip glued onto it, e.g.
    ``"IL 62704"``) so the tokens on either side of it can be read off as
    city / street.
    """
    by_name = {name.lower(): name for name in US_STATES.values()}
    for idx in range(len(tokens) - 1, -1, -1):
        token = tokens[idx]
        code = token.upper()
        is_state = code in US_STATES or token.lower() in by_name
        if not is_state:
            with_zip = re.match(r"^(.*?)\s+\d{5}(?:-\d{4})?$", token)
            if with_zip:
                head = with_zip.group(1).strip()
                is_state = head.upper() in US_STATES or head.lower() in by_name
        if is_state:
            return idx
    return -1


def city_from_addresses(addresses) -> str:
    """The city named in a profile's address lines, or "".

    Looks for the same ``"..., City, ST"`` / ``"..., City, ST ZIP"`` shape
    ``state_from_addresses`` reads, and returns the token immediately before
    the recognized state -- ``"123 Main St, Springfield, IL 62704"`` ->
    ``"Springfield"``. Returns "" rather than guessing when no line has a
    recognizable state token to anchor on, for the same reason
    ``state_from_addresses`` does: a wrong city is sent to a third party
    under the person's name, and a missing-field refusal is the safe
    direction.
    """
    for line in reversed(list(addresses or [])):
        if not isinstance(line, str):
            continue
        tokens = [t.strip() for t in line.split(",") if t.strip()]
        idx = _state_token_index(tokens)
        if idx > 0:
            return tokens[idx - 1]
    return ""


def street_only_from_addresses(addresses) -> str:
    """Just the street-address token(s) of a profile's address, or "".

    Unlike ``street_from_addresses`` (deliberately the WHOLE line, for a
    single free-text Address box such as Credit.com's), this is for a form
    that gives the street its OWN input separate from City/State/Zip ones
    (BigDBM's Address1, ACHCOOP's Address 1) -- handing that box the whole
    ``"123 Main St, Springfield, IL 62704"`` line would duplicate the city
    and state into a field meant to hold neither. Returns everything before
    the recognized city+state pair, joined back with ", " (so a street that
    itself contains a comma, e.g. a suite line, survives); "" when no state
    token anchors the split, same refuse-rather-than-guess reasoning as
    ``city_from_addresses``.
    """
    for line in reversed(list(addresses or [])):
        if not isinstance(line, str):
            continue
        tokens = [t.strip() for t in line.split(",") if t.strip()]
        idx = _state_token_index(tokens)
        if idx > 1:
            return ", ".join(tokens[:idx - 1])
    return ""


_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def zip_from_addresses(addresses) -> str:
    """The 5-digit US ZIP in a profile's address lines, or "".

    Credit.com makes Zip a REQUIRED field, so "" here is what makes that
    attempt stop as a recorded missing-field failure instead of a
    half-filled request. Only the 5-digit form is returned: ZIP+4 is
    accepted as input but the +4 is dropped, because a wrong +4 is worse
    than no +4 for matching.
    """
    for line in reversed(list(addresses or [])):
        if not isinstance(line, str):
            continue
        found = _ZIP_RE.search(line)
        if found:
            return found.group(1)
    return ""


def street_from_addresses(addresses) -> str:
    """The most specific address line, verbatim, or "".

    Deliberately NOT parsed into street/city/state parts. Credit.com's
    Address box is free text; handing it the line the person actually
    wrote is more faithful than this module guessing where a street name
    ends, and a wrong guess is sent to a third party under their name.
    """
    for line in reversed(list(addresses or [])):
        if isinstance(line, str) and line.strip():
            return line.strip()
    return ""


def phone_for_form(phones) -> str:
    """The first profile phone in the international format forms ask for.

    L.S Mobile's form says outright: "We cannot process a request without a
    country code". A bare 10-digit US number is therefore rendered as
    ``+1XXXXXXXXXX``. Anything already starting with ``+`` is passed
    through untouched, and anything that is neither is returned as-is
    rather than being mangled into a number that is not the person's.
    """
    for raw in list(phones or []):
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        if text.startswith("+"):
            return text
        digits = "".join(c for c in text if c.isdigit())
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return text
    return ""


def resolve_fields(recipe: FormRecipe, identity) -> dict:
    """Map *identity* onto *recipe*'s fields.

    Returns ``{"values": {selector: text}, "labels": {selector: label},
    "missing": [label, ...]}``.

    ``missing`` lists the labels of REQUIRED fields with no usable value.
    ``optout_submit`` refuses to fill a form with a non-empty ``missing``
    rather than submitting a half-filled request under Penn's name -- a
    partial DSAR is worse than no DSAR, because the broker answers it and
    the request is spent.

    Pure: takes an ``Identity``-shaped object, returns plain data, touches no
    browser. This is the function that the "is the mapping right?" test
    suite actually asserts on.
    """
    values, labels, missing = {}, {}, []
    for f in recipe_fields(recipe):
        labels[f.selector] = f.label
        if f.source == "literal":
            text = f.value
        elif f.source == "full_name":
            text = (getattr(identity, "full_name", "") or "").strip()
        elif f.source == "zip":
            text = zip_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "address":
            text = street_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "street":
            text = street_only_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "city":
            text = city_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "phone":
            text = phone_for_form(getattr(identity, "phones", None))
        elif f.source == "first_name":
            text = (getattr(identity, "first_name", "") or "").strip()
        elif f.source == "last_name":
            text = (getattr(identity, "last_name", "") or "").strip()
        elif f.source == "email":
            emails = list(getattr(identity, "emails", None) or [])
            text = emails[0].strip() if emails else ""
        elif f.source == "state":
            text = state_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "state_code":
            text = state_code_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "country":
            text = DEFAULT_COUNTRY
        else:
            raise ValueError("unknown field source: {!r}".format(f.source))

        if text:
            values[f.selector] = text
        elif f.required:
            missing.append(f.label)
    return {"values": values, "labels": labels, "missing": missing}
