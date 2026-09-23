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

# InfoPay's shared "Do Not Sell or Share My Personal Information" form, served
# on several of its properties (courtrecords.us, staterecords.org,
# recordsfinder.com) at ``/do-not-sell-share-my-personal-information``. The
# giveaway is the field naming, which is the vendor's own PHP model path:
# ``InfoPay_Core_Components_OptOuts_DataRemovalServiceModel[fname]``. Four
# fields (First/Last/State/City), one ``button[type=submit].form-btn``, and --
# unusually for this pilot -- NO bot check at all.
#
# "Same flavor" is a FINDING here, not an assumption: each of the three sites
# was opened separately and its form enumerated element by element (identical
# ids, identical field set, identical submit button, no honeypot), because
# ``optout_forms``'s own docstring warns that a shared HOSTING shape is not a
# shared SCHEMA.
FLAVOR_INFOPAY_DNS = "infopay_do_not_sell_form"

# ThatsThem's own /optout page. A single server-rendered Tailwind form --
# Full Name, Street Address, City, State (<select>), ZIP, Email, Phone, all
# seven starred required -- posting to itself, with no wizard, no emailed
# link and no account. Its bot check is an INVISIBLE Cloudflare Turnstile:
# there is no .cf-turnstile div and no iframe on the page, only a hidden
# input[name='cf-turnstile-response'] whose id carries Cloudflare's
# cf-chl-widget- prefix.
FLAVOR_THATSTHEM_BESPOKE = "thatsthem_bespoke_form"

# Pipl's /personal-information-removal-request page. Not Pipl's own form at
# all: a Salesforce Web-to-Case form (action=webto.salesforce.com/servlet/
# servlet.WebToCase, orgid=00D5e000003ToGq) embedded in their marketing
# site, so the visible fields sit alongside Salesforce's hidden bookkeeping
# (subject="Information Removal Request", priority="Medium", retURL to a
# thank-you page) and two custom-field ids instead of names. It carries TWO
# bot checks: reCAPTCHA, and a required arithmetic question.
FLAVOR_PIPL_WEBTOCASE = "pipl_salesforce_webtocase_form"

# RevealPhoneOwner's own /data-removal/ page. The plainest form in this
# pilot: a server-rendered Bootstrap POST form with five visible inputs
# (name, last name, phone, e-mail, free-text reason), a hidden form-name
# marker, no JavaScript widget of any kind, no honeypot -- and no bot check.
FLAVOR_REVEALPHONEOWNER_BESPOKE = "revealphoneowner_bespoke_form"

# AGR Marketing Solutions' opt-out, embedded directly in its privacy policy
# rather than on a page of its own. A plain WordPress form (#agr-optout-form)
# posting to wp-admin/admin-post.php with action=agr_optout and a per-load
# nonce, ten visible fields, no bot check -- and a honeypot: a hidden
# "Website" box (name=agr_website) inside a display:none wrapper.
FLAVOR_AGR_WP_OPTOUT = "agr_wordpress_admin_post_optout"

# PropertyChecker's own do-not-sell form. A Yii2 server-rendered POST form
# (#w0) whose fields carry the framework's model-array names,
# ``OptOutForm[fullName]`` and friends, with a per-load ``_csrf-frontend``
# token. Notable because PropertyChecker sits in the same corporate family
# as the InfoPay sites but does NOT serve the shared InfoPay form.
FLAVOR_PROPERTYCHECKER_YII = "propertychecker_yii_optout_form"


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
    # True when this element does not EXIST on the freshly-loaded page and
    # is rendered only once an earlier step has been answered. See Field's
    # copy of this flag for why it has to be declared rather than guessed.
    appears_later: bool = False


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
    # True when this element does not EXIST on the freshly-loaded page and
    # is rendered only after an earlier step is answered -- Nielsen's State
    # and request-type controls appear only once Country is filled.
    #
    # It is here for the benefit of ``--check-recipes``, which opens the
    # page and looks for every selector WITHOUT filling anything in: a
    # conditional field is legitimately absent there, and reporting it as
    # missing raises a drift alert about a recipe that is perfectly
    # healthy. That is the exact failure mode the alerting design set out
    # to avoid (noise Penn learns to ignore), so it is worth a declared
    # flag rather than a heuristic like "anything after the first Choice".
    # The cost is explicit: a conditional selector is NOT covered by the
    # active check, and its rot is caught only when the recipe actually
    # runs -- where ``recipe_health`` classifies the Playwright failure as
    # structural and alerts anyway.
    appears_later: bool = False


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
    # Set ONLY when a human swept this form for a bot check and found none.
    # It exists because "captcha_selectors is empty" is ambiguous -- it could
    # mean "there is no captcha" or "nobody looked" -- and those two have
    # opposite safety consequences: with submission enabled and dry-run off,
    # a form with no bot check is one this tool really will submit
    # unattended. Every recipe must say which it is (proven by
    # ``tests/test_optout_submit.py``), and the reason belongs in ``notes``.
    no_captcha_verified: bool = False
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
    # The dataset's id for Nielsen, not a slug of its name. See the
    # "Keyed by the DATASET's id" note above RECIPES.
    broker_id="onetrust-com",
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
               label="Request type", appears_later=True),
        Field(selector="#stateDSARElement", source="state", label="State",
              kind="combo", appears_later=True),
        Field(selector="#firstNameDSARElement", source="first_name", label="First Name"),
        Field(selector="#lastNameDSARElement", source="last_name", label="Last Name"),
        Field(selector="#emailDSARElement", source="email", label="Email"),
        Field(selector="#zipDSARElement", source="zip", label="Zip",
              required=False, appears_later=True),
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
    broker_id="bolttech-io",
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
    broker_id="lsmapps-com",
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


# The vendor's own model-path prefix on every field id of the shared InfoPay
# form. Written once rather than five times per recipe, because the string is
# long enough that a typo in one copy would be invisible in review.
_INFOPAY = "#InfoPay_Core_Components_OptOuts_DataRemovalServiceModel_"

# Scoped to the FORM that carries the opt-out's own First Name box. The
# button's bare class (``.form-btn``) is reused by every search form on some
# of these sites -- recordsfinder.com's homepage alone has fourteen of them --
# so an unscoped ``button.form-btn`` would be ambiguous the moment this form
# moved onto a page that also carries a search box.
_INFOPAY_SUBMIT = "form:has({}fname) button[type='submit']".format(_INFOPAY)


def _infopay_steps() -> tuple:
    """The four steps of InfoPay's shared opt-out form.

    City is offered without the form's own ``*`` required marker, so it is
    ``required=False``: a profile with no parseable city still produces a
    submittable First/Last/State request rather than a refusal.
    """
    return (
        Field(selector=_INFOPAY + "fname", source="first_name", label="First Name"),
        Field(selector=_INFOPAY + "lname", source="last_name", label="Last Name"),
        # Full state NAMES ("Illinois"), chosen by label, so source="state"
        # rather than BigDBM's bare-code "state_code".
        Field(selector=_INFOPAY + "state", source="state", label="State",
              kind="select"),
        Field(selector=_INFOPAY + "city", source="city", label="City",
              required=False),
    )


_INFOPAY_SUCCESS = (
    "thank you",
    "your request has been received",
    "request has been submitted",
)

COURTRECORDS_US = FormRecipe(
    broker_id="courtrecords-us",
    broker_name="CourtRecords.us",
    url="https://courtrecords.us/do-not-sell-share-my-personal-information/",
    flavor=FLAVOR_INFOPAY_DNS,
    steps=_infopay_steps(),
    submit_selector=_INFOPAY_SUBMIT,
    # Swept and confirmed clean -- see the recipe's notes.
    no_captcha_verified=True,
    success_markers=_INFOPAY_SUCCESS,
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity): all four fields fill correctly and "
        "the run stops before Submit, screenshot confirms it. The dataset's "
        "recorded opt-out URL for this broker (courtrecords.us/optout) is "
        "NOT this form -- it is a 'Your Privacy Choices' rights-explainer "
        "page with zero inputs on it, which links on to this one; a recipe "
        "written against the dataset URL would have filled nothing. "
        "IMPORTANT, and unusual for this pilot: this form carries NO bot "
        "check of any kind (no reCAPTCHA, hCaptcha, Turnstile, BotDetect or "
        "honeypot -- swept for and confirmed absent on all three InfoPay "
        "sites). Every other recipe in this module stops at 'needs manual "
        "action' on a captcha; this one would NOT, so flipping "
        "BG_OPTOUT_SUBMIT_ENABLED with dry-run off really would press "
        "Submit here unattended. success_markers are a plausible guess at "
        "the confirmation wording, NOT read off a real submitted page -- "
        "same caveat as ACHCOOP's recipe, and the reason a run reporting "
        "'submitted' via these markers should be spot-checked. SCOPE "
        "CAVEAT, printed under the form itself on all three InfoPay sites "
        "and confirmed in the dry-run screenshot: 'Submitting this form "
        "will result in the removal of only the specific records you "
        "select ... each record must be submitted separately.' So this "
        "form is step 1 of a select-your-records flow, the same shape of "
        "caveat as AdvancedBackgroundChecks' magic link: a completed "
        "submission here is a request STARTED, not a person removed, and "
        "somebody still has to pick their records on the page that "
        "follows."
    ),
)

STATERECORDS_ORG = FormRecipe(
    broker_id="staterecords-org",
    broker_name="StateRecords.org",
    url="https://staterecords.org/do-not-sell-share-my-personal-information",
    flavor=FLAVOR_INFOPAY_DNS,
    steps=_infopay_steps(),
    submit_selector=_INFOPAY_SUBMIT,
    # Swept and confirmed clean -- see the recipe's notes.
    no_captcha_verified=True,
    success_markers=_INFOPAY_SUCCESS,
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity), and enumerated element by element "
        "rather than assumed from the shared flavor: this site's markup is "
        "Tailwind-themed where CourtRecords.us's is plain Bootstrap, but "
        "the four field ids, the field set and the submit button are "
        "identical. Same no-bot-check caveat as COURTRECORDS_US. Note the "
        "URL has no trailing slash: with one, the site 301s, and the "
        "settled form is recorded here so the driver does not depend on a "
        "redirect staying in place."
    ),
)

RECORDSFINDER = FormRecipe(
    broker_id="recordsfinder-com",
    broker_name="RecordsFinder",
    url="https://recordsfinder.com/do-not-sell-share-my-personal-information/",
    flavor=FLAVOR_INFOPAY_DNS,
    steps=_infopay_steps(),
    submit_selector=_INFOPAY_SUBMIT,
    # Swept and confirmed clean -- see the recipe's notes.
    no_captcha_verified=True,
    success_markers=_INFOPAY_SUCCESS,
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity). Same InfoPay form as CourtRecords.us "
        "and StateRecords.org, enumerated separately; the only difference "
        "is that this site's State <select> carries an extra 'All States' "
        "option above the real ones, which a label match on a real state "
        "name cannot hit. The dataset's recorded opt-out URL "
        "(recordsfinder.com/optout) is the rights-explainer page, not this "
        "form -- see COURTRECORDS_US's note. Same no-bot-check caveat."
    ),
)


REVEALPHONEOWNER = FormRecipe(
    broker_id="revealphoneowner-com",
    broker_name="RevealPhoneOwner",
    url="https://www.revealphoneowner.com/data-removal/",
    flavor=FLAVOR_REVEALPHONEOWNER_BESPOKE,
    fields=(
        # Selected by NAME, not by the ids the page also carries. Its ids are
        # positional and generated ("removal-element-1".."-6"), so inserting
        # one field anywhere above would silently renumber every selector
        # below it and this recipe would quietly type the last name into the
        # phone box. The name attributes say what the field IS.
        Field(selector="#removal input[name='first_name']",
              source="first_name", label="Name"),
        Field(selector="#removal input[name='last_name']",
              source="last_name", label="Last Name"),
        # Required by the form and genuinely required by the broker: this is
        # a reverse-phone directory, so the phone number IS the listing key.
        Field(selector="#removal input[name='phone_number']",
              source="phone", label="Phone Number"),
        # The page warns in its own help text that a request with no valid
        # e-mail address is rejected.
        Field(selector="#removal input[name='email']",
              source="email", label="Email"),
        Field(selector="#removal textarea[name='message']", source="literal",
              label="Removal reason", value=_OPT_OUT_DETAILS),
    ),
    submit_selector="#removal input[type='submit']",
    # Swept and confirmed clean -- see the recipe's notes.
    no_captcha_verified=True,
    success_markers=("thank you", "request has been received",
                     "your request has been submitted"),
    notes=(
        "Verified against the live page on 2026-09-22 (dry run, real "
        "browser, synthetic identity): fills correctly and stops before "
        "Submit, screenshot confirms it. A plain server-rendered POST form "
        "-- no JavaScript widgets, no honeypot input, and NO BOT CHECK of "
        "any kind: the whole form was enumerated element by element and it "
        "is five visible inputs, one hidden form-name marker (form=removal, "
        "not a honeypot: it carries a value the server expects and is left "
        "untouched), and a submit. Treat that the way the other no-bot-check "
        "recipes are treated -- with submission enabled and dry-run off, "
        "this one really will send. success_markers are a plausible guess, "
        "NOT read off a real submitted page."
    ),
)


# A second standing request text, for forms whose own request-type control
# says DELETION rather than do-not-sell. Kept separate from
# ``_OPT_OUT_DETAILS`` and equally literal, for the same reason: it is sent
# verbatim to a third party in Penn's name, so it is reviewable here. Using
# the do-not-sell wording on a form whose Type field says "Deletion" would
# be filing one request while asking for another, and a broker that answers
# the wrong one has still spent the request.
_DELETION_DETAILS = (
    "I am exercising my right to have my personal information deleted. "
    "Please delete all personal information you hold about me, and instruct "
    "any service providers and third parties to whom you have disclosed it "
    "to do the same. Please confirm in writing once this request has been "
    "processed."
)

PIPL = FormRecipe(
    broker_id="pipl-com",
    broker_name="Pipl",
    url="https://pipl.com/personal-information-removal-request",
    flavor=FLAVOR_PIPL_WEBTOCASE,
    steps=(
        Field(selector="#full-name", source="full_name", label="Full Name"),
        Field(selector="#email-support", source="email", label="Email Address"),
        # The two Salesforce custom fields. Their ids ARE their names --
        # there is no friendlier handle -- and each was tied to its visible
        # label by reading the label text wrapping the input, not guessed
        # from the id.
        Field(selector="#00NUc000000TNu5", source="phone", label="Phone Number",
              required=False),
        Field(selector="#00N4U000009AIoj", source="address", label="Address",
              required=False),
        # A real <select>; its options are "Please Select", "Deletion",
        # "Disclosure", "Disclosure + Deletion". A fixed choice, not a
        # profile-varying one, so Select rather than Field(kind='select').
        Select(container="#direct_your_support_question",
               option_label="Deletion", label="Type"),
        Field(selector="#message_desc", source="literal", label="Message",
              value=_DELETION_DETAILS),
    ),
    forbidden_selectors=(
        # The arithmetic bot check. Listed as forbidden as well as declared
        # below, so that no future edit can "helpfully" teach the driver to
        # compute it: solving a bot check is the one thing this module does
        # not do.
        "#00NUc000002Porh",
    ),
    submit_selector="#submit-btn",
    captcha_selectors=(
        # (1) reCAPTCHA. The generic sweep's .g-recaptcha already catches
        # this one (three matching elements on the page), and it is named
        # here for the record.
        ".g-recaptcha",
        # (2) The one the generic sweep would MISS. A required number input
        # labelled "Solve this math problem to continue*" whose prompt is
        # rendered next to it ("10 + 1 ="). Its id is a Salesforce custom
        # field key, so it matches neither input[name*='captcha'] nor
        # input[id*='captcha'].
        "#00NUc000002Porh",
    ),
    success_markers=(
        # The form's own retURL is https://pipl.com/lp/customer-support-
        # thank-you, so a successful POST lands on a thank-you page. Read
        # off the hidden field, NOT off a page reached by submitting --
        # nothing was submitted.
        "thank you",
    ),
    notes=(
        "Verified against the live page on 2026-09-23, read-only: the form "
        "was enumerated element by element (every visible control plus "
        "Salesforce's six hidden inputs) and each visible field matched to "
        "its own label. Nothing was typed and nothing was submitted.\n"
        "\n"
        "It is a Salesforce Web-to-Case form posting to "
        "webto.salesforce.com, not to pipl.com, which is worth knowing "
        "before anyone reads a failed POST as Pipl being down. Two fields "
        "are Salesforce custom keys rather than names "
        "(00NUc000000TNu5 = Phone Number, 00N4U000009AIoj = Address); both "
        "are optional on the form and are marked required=False here so a "
        "profile without them still files.\n"
        "\n"
        "TWO bot checks, and only one of them is detectable generically. "
        "The reCAPTCHA is caught by the shared sweep. The other is a "
        "REQUIRED arithmetic question -- 'Solve this math problem to "
        "continue*' over a rendered '10 + 1 =' and a number input -- whose "
        "id is a Salesforce custom key, so none of the generic "
        "input[name*='captcha'] / input[id*='captcha'] patterns touch it. "
        "It is declared in captcha_selectors so the run stops, and ALSO in "
        "forbidden_selectors so that nobody later implements the two-line "
        "arithmetic solver that would quietly turn this into the first "
        "bot-check bypass in the codebase. The expected outcome of a live "
        "run is therefore 'needs manual action' with a screenshot of a "
        "fully filled form -- which is the useful outcome, since the only "
        "things left to do by hand are the sum and the reCAPTCHA.\n"
        "\n"
        "Request type is set to 'Deletion' and the message body is "
        "_DELETION_DETAILS rather than the do-not-sell _OPT_OUT_DETAILS "
        "every other recipe uses, because this form makes the request type "
        "explicit and the two must agree. Pipl has no consumer search "
        "surface at all; see search_forms.NO_SEARCH_SURFACE."
    ),
)

THATSTHEM = FormRecipe(
    broker_id="thatsthem-com",
    broker_name="ThatsThem",
    url="https://thatsthem.com/optout",
    flavor=FLAVOR_THATSTHEM_BESPOKE,
    fields=(
        # One box for the whole name; this form has no first/last split.
        Field(selector="#name", source="full_name", label="Full Name"),
        Field(selector="#street", source="street", label="Street Address"),
        Field(selector="#city", source="city", label="City"),
        # A real <select> whose options are full state names (value="NV",
        # text="Nevada"), so kind="select" picking by the resolved LABEL --
        # confirmed live: selectOption('Nevada') set the value to NV.
        Field(selector="#state", source="state", label="State", kind="select"),
        Field(selector="#zip", source="zip", label="ZIP Code"),
        Field(selector="#email", source="email", label="Email"),
        Field(selector="#phone", source="phone", label="Phone"),
    ),
    submit_selector="form button[type='submit']",
    captcha_selectors=(
        # An INVISIBLE Cloudflare Turnstile, which is why this is declared
        # here at all: optout_submit's generic sweep looks for
        # .cf-turnstile and iframe[src*='turnstile'], and on this page
        # BOTH are absent (checked live: 0 matches each, and zero iframes
        # on the document). The only trace of it is the hidden response
        # input Cloudflare injects. Without these two selectors the generic
        # detection would report "no bot check" on a form that has one --
        # exactly the miss captcha_selectors exists to cover.
        "input[name='cf-turnstile-response']",
        "input[id^='cf-chl-widget-']",
    ),
    success_markers=(
        # NOT read off a real submitted page -- nothing was submitted. Kept
        # deliberately plain; see notes.
        "your request has been received",
        "thank you",
    ),
    notes=(
        "Verified against the live form on 2026-09-23, fill-only: every "
        "selector was resolved (all ten, including the submit button, match "
        "exactly ONE element on the page), the whole form was filled with "
        "obviously fictitious data (Zzyzx Testperson, 400 Nonexistent Way, "
        "Elko NV 89801) to confirm each control accepts a value and the "
        "State <select> maps a full-name label onto its two-letter value, "
        "and then the page was navigated away from. Submit was never "
        "pressed, so success_markers are a plausible guess rather than "
        "wording read off a confirmation page -- the same caveat the "
        "L.S Mobile recipe carries.\n"
        "\n"
        "The page's own copy states the shape: 'Request removal of your "
        "personal information from our database', 'Requests are typically "
        "processed within 72 hours. You'll receive a confirmation email "
        "once complete.' No account, no emailed link before the form, no "
        "record-picking -- which is what separates this from BeenVerified "
        "and Intelius, both of whose removals are out of scope.\n"
        "\n"
        "One finding worth carrying forward, because it cuts against the "
        "usual reassurance that a captcha stops us anyway: this Turnstile "
        "is INVISIBLE and it self-issued. Immediately after filling the "
        "form, input[name='cf-turnstile-response'] already held a token "
        "(a '1.PjBI_...' string) with nothing clicked and no widget shown. "
        "So this is a form a fully-enabled, dry-run-off run really could "
        "submit unattended -- it is listed in captcha_selectors so that it "
        "will not, and so that the first live attempt stops at 'needs "
        "manual action' with a screenshot for a human to look at. Do not "
        "remove those selectors on the grounds that 'there is no visible "
        "captcha'; that is precisely the observation that makes them "
        "necessary. Its SEARCH leg is a shipped recipe in search_forms "
        "(THATSTHEM), whose results endpoint separately serves headless "
        "clients an Access Denied page.\n"
        "\n"
        "One known mismatch, recorded rather than papered over: "
        "resolve_fields feeds #phone through phone_for_form, which yields "
        "E.164 ('+17755550142'), while the field's own placeholder shows "
        "'(206) 555-1234'. It is a plain type=tel text input with no "
        "pattern attribute, so nothing rejects E.164 client-side -- but "
        "whether ThatsThem's SERVER accepts that shape is not something a "
        "fill-only verification can answer, and it will only be known from "
        "the first dry-run screenshot and the reply that follows a real "
        "submission."
    ),
)

# Keyed by the DATASET's broker id -- the value of ``id`` in
# ``data/brokers.json`` -- and by nothing else. That is not a style note, it
# is the only thing that makes the allow-list reachable: every lookup comes
# in holding an id the dataset produced, so a recipe filed under a slug of
# the broker's NAME is not a stricter allow-list, it is an absent one.
#
# Found the hard way on 2026-09-23, while cross-checking every key in this
# dict against data/brokers.json. THREE of the fourteen recipes -- Nielsen
# ("nielsen"), bolttech ("bolttech") and L.S Mobile Apps
# ("ls-mobile-apps-holdings-ltd") -- were filed under ids that appear
# nowhere in the dataset, whose real ids are "onetrust-com", "bolttech-io"
# and "lsmapps-com". All three had been unreachable since they were
# written, including all three the README advertises by name under
# "Automated opt-out submission". The remap is not a guess: Nielsen's and
# bolttech's recipe urls are byte-identical to their dataset entries'
# optout_url, and L.S Mobile's is the redirect target this module's own
# docstring already documents for lsmapps-com's dataset url. The names
# match exactly in all three cases too.
#
# Nothing asserts this today. A test that every key here (and in
# search_forms.RECIPES, which was checked at the same time and is clean)
# resolves to a real dataset broker would have caught it the day it landed,
# and is the obvious next piece of work.
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
    COURTRECORDS_US.broker_id: COURTRECORDS_US,
    STATERECORDS_ORG.broker_id: STATERECORDS_ORG,
    RECORDSFINDER.broker_id: RECORDSFINDER,
    REVEALPHONEOWNER.broker_id: REVEALPHONEOWNER,
    THATSTHEM.broker_id: THATSTHEM,
    PIPL.broker_id: PIPL,
}


# --- staged recipes ----------------------------------------------------------
#
# Transcribed element by element against the live page in a real browser on
# 2026-09-23, every selector read off the rendered DOM rather than inferred,
# and every one of them swept for a bot check that turned out to be absent.
# They are staged rather than shipped for one honest reason: no human has
# dry-run them, and ``no_captcha_verified`` is the flag this module reserves
# for a HUMAN sweep. Each carries False accordingly.

AGR_MARKETING = FormRecipe(
    broker_id="agrmarketingsolutions-com",
    broker_name="AGR Marketing Solutions",
    url="https://agrmarketingsolutions.com/privacy-policy/",
    flavor=FLAVOR_AGR_WP_OPTOUT,
    fields=(
        Field(selector="#optout-first-name", source="first_name",
              label="First Name"),
        Field(selector="#optout-last-name", source="last_name",
              label="Last Name"),
        Field(selector="#optout-email", source="email", label="Email"),
        Field(selector="#optout-phone", source="phone", label="Phone Number"),
        Field(selector="#optout-address", source="street", label="Address"),
        Field(selector="#optout-city", source="city", label="City"),
        # A plain text box, NOT a <select>, so nothing on the page settles
        # whether it wants "Nevada" or "NV". See the notes.
        Field(selector="#optout-state", source="state_code", label="State"),
        Field(selector="#optout-zip", source="zip", label="ZIP"),
    ),
    forbidden_selectors=("#optout-website",),
    submit_selector="#optout-submit-btn",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. Form "
        "#agr-optout-form posts to /wp-admin/admin-post.php with "
        "action=agr_optout and a WordPress nonce (agr_optout_nonce) minted "
        "per page load, so it can only ever be driven in a browser, never "
        "as a canned POST. HONEYPOT, and the reason forbidden_selectors is "
        "populated: input[name=agr_website]/#optout-website is labelled "
        "'Website' and sits inside <p class='agr-hp hidden' "
        "style='display:none !important' aria-hidden='true' "
        "tabindex='-1'> -- filling it is how the form identifies a bot. "
        "NO bot check of any kind was found: no reCAPTCHA, hCaptcha, "
        "Turnstile or BotDetect script, and no widget element. "
        "no_captcha_verified stays False regardless, because no human has "
        "swept it. TWO OPEN QUESTIONS, and why this is staged rather than "
        "shipped: (1) #optout-state is a free-text box, so state_code "
        "above is a judgement call from the form's Address/City/State/ZIP "
        "mailing shape, not something the page states; (2) no surrounding "
        "text ties the form to a named legal right, so whether this is a "
        "CCPA opt-out of sale or a narrower do-not-mail suppression is "
        "still unsettled. A dry run should settle (1); reading AGR's "
        "policy should settle (2)."
    ),
)

# CourtCaseFinder is a FOURTH InfoPay property carrying the shared opt-out
# form -- same vendor model-path field ids as courtrecords.us,
# staterecords.org and recordsfinder.com, enumerated separately rather than
# assumed, per this module's own warning that shared hosting is not a shared
# schema. What differs: it posts OFF-SITE, to
# members.courtcasefinder.com/removeMyData/, and it carries no
# _csrf-frontend hidden field.
COURTCASEFINDER = FormRecipe(
    broker_id="courtcasefinder-com",
    broker_name="CourtCaseFinder.com",
    url=("https://courtcasefinder.com/"
         "do-not-sell-share-my-personal-information"),
    flavor=FLAVOR_INFOPAY_DNS,
    steps=_infopay_steps(),
    submit_selector=_INFOPAY_SUBMIT,
    success_markers=_INFOPAY_SUCCESS,
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. Form #yw0 posts "
        "to https://members.courtcasefinder.com/removeMyData/ with the "
        "four familiar InfoPay fields -- "
        "InfoPay_Core_Components_OptOuts_DataRemovalServiceModel[fname], "
        "[lname], [state] (a 52-option select) and [city] -- and a bare "
        "button[type=submit] reading 'SUBMIT'. Field ids match the three "
        "shipped InfoPay recipes exactly, which is why _infopay_steps() is "
        "reused verbatim; city is again the one field without the form's "
        "required marker. No captcha script and no widget on the page, "
        "matching the no-bot-check finding on its three siblings -- but "
        "no_captcha_verified is False here because no human has swept it "
        "and no dry run was performed. Inherits the siblings' SCOPE "
        "CAVEAT: the page says a submission removes only the specific "
        "records the requester then selects, so a completed submission is "
        "a request STARTED, not a person removed. Note also that the "
        "dataset's opt_out_url for this row is a TrustArc form that "
        "explicitly refuses do-not-sell requests (see "
        "propertychecker-com's entry) -- this URL, off the site's own "
        "footer, is the real surface."
    ),
)

PROPERTYCHECKER = FormRecipe(
    broker_id="propertychecker-com",
    broker_name="PropertyChecker",
    url=("https://propertychecker.com/"
         "do-not-sell-share-my-personal-information"),
    flavor=FLAVOR_PROPERTYCHECKER_YII,
    fields=(
        Field(selector="#optoutform-fullname", source="full_name",
              label="Full Name"),
        Field(selector="#optoutform-street", source="street", label="Street"),
        Field(selector="#optoutform-city", source="city", label="City"),
        # 52 options, full state NAMES, so source="state" not "state_code".
        Field(selector="#optoutform-state", source="state", label="State",
              kind="select"),
        Field(selector="#optoutform-zip", source="zip", label="Zip Code"),
        Field(selector="#optoutform-email", source="email",
              label="Email Address"),
        Field(selector="#optoutform-phone", source="phone", label="Phone",
              required=False),
    ),
    submit_selector="form#w0 button[type='submit']",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. Form #w0 posts "
        "to itself with Yii2-style names OptOutForm[fullName], [street], "
        "[city], [state] (52-option select of full state names), [zip], "
        "[email] and [phone] (the only optional one), plus a "
        "_csrf-frontend token minted per page load -- so browser-driven "
        "only, never a canned POST. No captcha script, no widget, no "
        "honeypot found; no_captcha_verified is still False because no "
        "human has swept it and no dry run was performed. IMPORTANT "
        "DATASET FINDING: the recorded opt_out_url for this row is a "
        "TrustArc IRM form that REFUSES this request type in its own "
        "words -- 'To submit a Do Not Sell or Share request, please use "
        "the Do Not Sell or Share My Personal Information link located in "
        "the website footer' -- so a recipe written against the dataset "
        "URL would have filed the wrong kind of request. The footer link "
        "leads here. Note too that /optout, which the footer also offers "
        "as 'Your Privacy Choices', is a rights-explainer page with zero "
        "inputs on it, the same trap already documented on the InfoPay "
        "rows. Unlike its sibling courtcasefinder-com, this site does NOT "
        "use the shared InfoPay form -- same corporate family, different "
        "opt-out implementation, which is exactly why each one is "
        "enumerated separately. Second channel for a human: "
        "privacy@propertychecker.com."
    ),
)


# Brokers whose form has been WRITTEN DOWN but which are not yet turned on.
# It exists so that "we transcribed the form" and "we are willing to submit
# to it" stay two separate decisions. Nothing reads this at runtime: a broker
# here is absent from RECIPES, which is what actually stops a submission.
STAGED_RECIPES: dict = {
    AGR_MARKETING.broker_id: AGR_MARKETING,
    COURTCASEFINDER.broker_id: COURTCASEFINDER,
    PROPERTYCHECKER.broker_id: PROPERTYCHECKER,
}


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
    "locateplus-com": (
        "LocatePLUS sells skip-tracing data to licensed businesses through "
        "a closed customer portal. Verified 2026-09-22: the public "
        "marketing domain has no opt-out webform anywhere -- the only "
        "removal channel referenced in the privacy policy is a plain "
        "mailto to customerservice@locateplus.com, which this codebase has "
        "no mail-sending capability for (and mailbox-only channels are "
        "outside what a form-filling recipe can represent)."
    ),
    "golookup-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: this domain no "
        "longer belongs to the original broker (transferred by court "
        "order) and serves no broker content, including no opt-out page."
    ),
    "arrestfacts-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: arrestfacts.com "
        "redirects entirely to an unrelated third party (nkreeger.com); "
        "there is no page on the original domain to open an opt-out form "
        "against."
    ),
    "publicrecords360-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: the domain "
        "302-redirects entirely to ussearch.com; it serves no page of its "
        "own, including no opt-out page."
    ),
    "publicrecordsnow-com": (
        "Verified 2026-09-22: the 'Do Not Sell' link on publicrecordsnow."
        "com's own homepage points to https://www.peoplefinders.com/"
        "do-not-sell, PeopleFinders' own opt-out page, not anything hosted "
        "on publicrecordsnow.com. Same affiliate-front finding as this "
        "broker's search_forms.NO_SEARCH_SURFACE entry: submitting a "
        "removal request through PeopleFinders' form would be recorded "
        "under the wrong broker's name."
    ),
    "facecheck-id": (
        "FaceCheck's 'Removal Request' page (/en/RemoveMyPhotos) renders "
        "no form of any kind -- verified 2026-09-22, the only inputs "
        "anywhere on that page are dark-mode toggle checkboxes. Removing a "
        "face match appears to require a separate, unautomated channel "
        "(uploading or referencing the specific photo/URL to take down), "
        "which is also not a request this tool should make on Penn's "
        "behalf without a specific photo of him to identify."
    ),
    "freepeopledirectory-com": (
        "Free People Directory has its own name-search UI (see "
        "search_forms), but its 'Do Not Sell My Info' link, verified "
        "2026-09-22, points straight to https://www.spokeo.com/privacy/"
        "control/ -- Spokeo's OWN privacy-control page, not anything "
        "hosted on freepeopledirectory.com. That confirms this is a "
        "Spokeo-network property for opt-out purposes; there is no "
        "independent removal surface on freepeopledirectory.com itself to "
        "build a recipe against."
    ),
    "judyrecords-com": (
        "Verified 2026-09-22: judyrecords.com has no opt-out webform "
        "anywhere (/privacy 404s and its footer links to only 'terms', "
        "'info', and 'API'). Its /info page states the only removal "
        "channel is a plain mailto to removal@judyrecords.com, which this "
        "codebase has no mail-sending capability for -- and that page also "
        "states requests are honored only 'if the record has been sealed "
        "or expunged by a court order', not on request, which is a "
        "different thing from a discretionary consumer opt-out anyway."
    ),
    "cocofinder-com": (
        "Verified 2026-09-22: CocoFinder's 'Do Not Sell My Information' "
        "link goes to a Google Form (docs.google.com/forms), and Google "
        "has DISABLED that form -- the page returns Google's own notice "
        "that the form was taken down for violating Google's Terms of "
        "Service, so there are no inputs to fill and nothing to submit. "
        "The only other channel named on the site is a mailto to "
        "support@cocofinder.com, which this codebase cannot send. Recorded "
        "under no-surface rather than blocked: nothing is standing between "
        "us and the form, the form is gone."
    ),
    "peoplefinder-com": (
        "Verified 2026-09-23: peoplefinder.com's 'Do Not Sell Or Share My "
        "Personal Information' link points at "
        "https://www.intelius.com/privacy-center/ -- Intelius's own page, "
        "on Intelius's own domain -- and the site describes itself as "
        "'PeopleFinder.com powered by Intelius'. Its search form posts to "
        "tracking.intelius.com with affiliate codes, so this is a front "
        "rather than a broker with records of its own. Same finding as "
        "publicrecordsnow-com: there is no independent removal surface "
        "here to build a recipe against, and submitting to Intelius under "
        "this broker's name would be filing a request with the wrong "
        "company."
    ),
    "searchusapeople-com": (
        "Verified 2026-09-23: its own /data-removal-request/ page, titled "
        "'Data Removal Request - Opt Out', contains no removal form at "
        "all -- the only form on it is the site's InfoTracer-affiliate "
        "search box -- and its removal instructions are one sentence: 'For "
        "removal requests / opt-out, please visit InfoTracer and follow "
        "their instructions.' Same affiliate-front finding as "
        "publicrecordsnow-com and peoplefinder-com: filing here would mean "
        "filing with a different company. See "
        "search_forms.NO_SEARCH_SURFACE for the User-Agent block that made "
        "this domain look rate-limited rather than hostile."
    ),
    "arrestwarrant-org": (
        "Verified 2026-09-23: the only removal channel is a PRINTABLE PDF. "
        "arrestwarrant.org/privacy.html links 'our data removal policy' to "
        "a javascript popup of '/InfoPay Opt-Out New.pdf' (200, 198 KB), a "
        "fill-in form asking for full legal name, aliases, date of birth, "
        "current address and a reason drawn from a fixed list (law "
        "enforcement officer at risk, identity-theft victim, at risk of "
        "physical harm, record demonstrably incorrect), to be sent to "
        "support@verifyrecords.com. There is no web form to fill, this "
        "codebase cannot send mail, and the reason list is a claim about "
        "Penn's circumstances that no tool should make on his behalf. "
        "The site's /do-not-sell-share-my-personal-information/ path 404s."
    ),
    "freebackgroundcheck-org": (
        "Verified 2026-09-23: nothing to opt out OF, and the site says so. "
        "It publishes no records of its own -- it is a directory of links "
        "to other people-search sites (see the same finding in "
        "search_forms.SEARCH_UNDECIDED) -- and its privacy policy states "
        "'If you want to correct or remove your original public record ... "
        "you will need to contact the custodian of that information.' The "
        "only opt-outs it offers are for marketing email and browser push "
        "notifications. Its 'privacy' tab under members."
        "freebackgroundcheck.org redirects to that same policy page."
    ),
    "addresses-com": (
        "Verified 2026-09-23. The dataset's opt-out URL for this broker, "
        "https://www.addresses.com/optout.php, is DEAD: it answers HTTP "
        "404 titled '404 Sorry, not found | Addresses.com' with the site's "
        "own 'Oops! NOT FOUND / This page may have been moved or doesn't "
        "exist.' copy. What replaced it is not on this domain. Every "
        "privacy link in the live footer -- 'Privacy Policy' and 'Exercise "
        "My Data Privacy Rights' alike -- points at intelius.com "
        "(/privacy-policy/ and /privacy-center/ respectively), and its "
        "search form hands off to intelius.com/results/ with an ADDRS "
        "affiliate tag. Same affiliate-front finding as peoplefinder-com: "
        "there is no independent removal surface here, and filing through "
        "Intelius's wizard under this broker's name would be filing with a "
        "different company -- one whose own opt-out leg is separately "
        "recorded as out of scope. Worth flagging to whoever maintains the "
        "public dataset: that optout_url is stale."
    ),
    "phonenumbers-org": (
        "Verified 2026-09-23: the dataset's opt-out URL for this broker is "
        "infotracer.com/optout/ -- another company's site -- and "
        "phonenumbers.org's own footer carries only a privacy policy, no "
        "removal page of its own. Recorded as no independent surface. Note "
        "this is a different finding from the search leg's, which failed "
        "for its own reason; see search_forms.SEARCH_UNDECIDED."
    ),
    "easyoptouts-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: this row is not "
        "a broker. easyoptouts.com is the paid removal SERVICE, and the "
        "dataset's opt_out_url is simply its homepage. Verified "
        "2026-09-23: the page carries subscription sign-up CTAs and "
        "nothing resembling a data-removal request form, because there is "
        "no record here to remove. 'Opting out' of EasyOptOuts means "
        "declining to subscribe."
    ),
    "epic-org": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: EPIC is a "
        "privacy-advocacy nonprofit, not a broker. Verified 2026-09-23: "
        "the dataset's opt_out_url is a link to EPIC's May 2026 report "
        "'Good Luck Opting Out: Manipulative Design Patterns in Opt-Out "
        "Processes' -- a PDF documenting OTHER companies' broken opt-out "
        "flows. A policy paper is not an opt-out surface, and EPIC holds "
        "nothing to opt out of."
    ),
    "adelement-com": (
        "Verified 2026-09-23: adelement.com/opt-out.html is a COOKIE opt- "
        "out, not a record removal. The page is a status display with one "
        "mutually-exclusive toggle button ('Opt Out Now' / 'Opt In Now') "
        "that sets a browser opt-out cookie for behavioural advertising, "
        "and the page itself warns the setting resets if you change "
        "browsers or devices or clear cookies. Nothing on it accepts an "
        "identity or asks the company to delete anything, so there is no "
        "removal request a recipe could send."
    ),
    "adikteev-com": (
        "Verified 2026-09-23 (adikteev.com/privacy would not render to "
        "the fetcher; this rests on the policy text as published). "
        "Adikteev's own privacy policy routes end users to the opt-out "
        "link in the emails its merchant partners send, or to the device "
        "OS ad-tracking setting -- and redirects CCPA/GDPR requests to "
        "'the relevant advertiser or application' rather than handling "
        "them itself. The dataset agrees: method 'email', "
        "dpo@adikteev.com. No consumer-facing removal form exists on the "
        "site."
    ),
    "adform-com": (
        "Verified 2026-09-23 as far as the fetcher allowed "
        "(site.adform.com would not render). Adform's published opt-out "
        "is cookie-based -- it sets an anonymous 'opt-out' cookie that, "
        "in its own words, 'remains in effect only as long as this cookie "
        "is present in your browser' -- and a real data-subject request "
        "goes by downloading an Identity Verification Form PDF and "
        "emailing it to dpo@adform.com. A cookie toggle plus a mailed PDF "
        "is not a form this tool can fill: matches the dataset's method "
        "'email'."
    ),
    "4-eyes-ai": (
        "Verified 2026-09-23: every 4Eyes path is dead. The dataset's URL "
        "(www.4-eyes.ai/privacy-policy?cord_section=privacy_options_dns) "
        "404s, as do www.4-eyes.ai/privacy-policy and both spellings of "
        "/privacy-dashboard/ -- the company's own pages were retired "
        "after the 2025 Delivr.ai acquisition. The live successor, "
        "www.delivr.ai/privacy, embeds no opt-out form either: it carries "
        "a cookie-preference banner, a California phone number, "
        "info@delivr.ai and a bare reference to an external 'Data Subject "
        "Request Form' with nothing rendered behind it. Nothing here is a "
        "form a recipe could fill."
    ),
    "degree-me": (
        "Verified 2026-09-23: there cannot be a form, because there is no "
        "site. The dataset gives no opt_out_url and declares the channel "
        "email-only (admin@academixdirect.com), and independently "
        "degree.me resolves to no A or AAAA record at all (see the search "
        "leg). A mailbox-only channel is outside what a form-filling "
        "recipe can represent, and here even the mailbox belongs to a "
        "different domain than the row."
    ),
    "acutraq-com": (
        "Verified 2026-09-23: acutraq.com/privacy-policy/ carries no web "
        "form for CCPA, opt-out or deletion requests. The only channels "
        "it declares are email (info@acutraq.com), postal mail (P.O. Box "
        "766, Elkins, AR 72727) and fax (479-439-9156); the single link "
        "on the page is a marketing-email unsubscribe, which is not a "
        "data-deletion request. Mail, fax and a mailbox are all outside "
        "what a form-filling recipe can do."
    ),
    "adadapted-com": (
        "Verified 2026-09-23: www.adadapted.com/legal/do-not-sell is CCPA "
        "policy prose with no interactive form -- only "
        "hello@adadapted.com and +1 (313) 744-3383. The deeper problem is "
        "the key, not the channel: the page states AdAdapted 'does not "
        "store or receive email address, only the mobile advertising "
        "identifier', so a request must carry an Apple IDFA or Android "
        "AAID. This codebase has no such identifier to offer and no mail "
        "capability, so there is nothing here it could submit."
    ),
    "addefend-com": (
        "Verified 2026-09-23: www.addefend.com/en/opt-out/ is a one-click "
        "COOKIE toggle, not a data request. The page carries 'Opt-Out' "
        "and 'Opt-In' buttons with prose explaining they set a flag "
        "governing whether AdDefend may collect data for that visitor -- "
        "no named fields, no labels, no dropdowns, no checkboxes, and "
        "(worth noting) no OneTrust/Osano/Termly/Ketch widget behind it. "
        "A browser-local flag is not a removal request, so there is no "
        "record-deletion surface here."
    ),
    "advcredit-com": (
        "Verified 2026-09-23: the dataset gives no opt_out_url and "
        "declares the channel email-only (evon@advcredit.com), and "
        "nothing resembling an opt-out form is present on or linked from "
        "www.advcredit.com -- every non-marketing route off the homepage "
        "is a credentialed lender portal. A mailbox-only channel is "
        "outside what a form-filling recipe can represent."
    ),
    "take5mg-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: verified "
        "2026-09-23, the domain is unreachable behind an expired TLS "
        "certificate and appears to be parked for sale, so no web form "
        "can exist to evaluate. The dataset declares the channel email- "
        "only (alex@take5mg.com) and gives no opt_out_url, which is "
        "consistent. If Advantage Solutions ever publishes a real surface "
        "for the former Take 5 Media Group data, this should be "
        "revisited."
    ),
    "agrgroupinc-com": (
        "Same finding as search_forms.NO_SEARCH_SURFACE: verified "
        "2026-09-23, agrgroupinc.com fails DNS on both apex and www, so "
        "no web form can exist. The dataset gives no opt_out_url and "
        "declares the channel email-only (privacy@agrgroupinc.com), which "
        "matches a company that is a CA data-broker registration with no "
        "live website."
    ),
    "altisource-com": (
        "Verified 2026-09-23: www.altisource.com/us-consumer-privacy- "
        "notice carries no web form at all. The only channel is email -- "
        "send your name and address to OptOutRequest@Altisource.com "
        "naming which of three choices you want (no sharing of "
        "creditworthiness information with affiliates, no affiliate "
        "marketing, no sharing with non-affiliates, the last of which the "
        "notice says it does not currently do anyway). A mailbox-only "
        "channel with a hand-composed body is outside what a form-filling "
        "recipe can represent."
    ),
    "altrata-com": (
        "Verified 2026-09-23: altrata.com/online-privacy-notice is policy "
        "prose with no fillable fields and no submit button. California "
        "residents are told to opt out 'by contacting us [email] or by "
        "calling our Toll free number (+1 877 314 5147)', and the only "
        "link on the page is a marketing-email unsubscribe, which is not "
        "a record removal. Email and a phone line are both outside what "
        "this tool can do."
    ),
    "aspire-north-com": (
        "Verified 2026-09-23, after chasing the row's URL to its real "
        "home. The dataset's URL (www.aspire-north.com/do-not-sell-my- "
        "personal-information--out-out-request-form.html) 404s, and so "
        "did every guessed sibling path. The live footer link instead "
        "points to americanspiritcorp.com/data-services-and-website- "
        "privacy-policy.html, which names an opt-out page at "
        "americanspiritcorp.com/right-to-deletion.html -- and THAT page, "
        "fetched directly, contains no fillable form either: just a phone "
        "number (952.886.3400) and an email contact link. So the surface "
        "was found, and it is a phone-and-email channel, not a form. The "
        "dataset's dead URL should be corrected."
    ),
    "amplemarket-com": (
        "Verified 2026-09-23: www.amplemarket.com/legal/do-not-sell-my- "
        "info is prose only -- no fields, no submit button. It instructs "
        "the reader to email privacy@amplemarket.com with 'full name, "
        "business email address and phone number'. A hand-composed "
        "mailbox request is outside what a form-filling recipe can "
        "represent."
    ),
    "verinext-com": (
        "Verified 2026-09-23: the dataset's URL, verinext.com/contact/, "
        "is a general SALES contact form, not a privacy surface -- Name, "
        "Business Email, Phone Number, Job Title, Company Name, 'How did "
        "you hear about us?', 'What solution are you most interested in?' "
        "and Message, submitting to 'Submit', with an SMS-consent "
        "disclaimer. Nothing on it exercises a data right, and the page "
        "carries no privacy-request mechanism at all. The dataset's own "
        "fallback, dpo@verinext.com, is a mailbox, which is outside what "
        "a form-filling recipe can represent."
    ),
    "atlanticfox-com": (
        "Verified 2026-09-23: the dataset gives no opt_out_url and "
        "declares the channel email-only (privacy@atlanticfox.com), and "
        "www.atlanticfox.com carries no privacy-request form anywhere. "
        "The only form on the site is a business-inquiry contact form "
        "alongside a sales address (ceo@atlanticfox.com) and a phone "
        "number -- a sales channel, not a data-rights one. A mailbox-only "
        "channel is outside what a form-filling recipe can represent."
    ),
    "hybridtheory-com": (
        "Verified 2026-09-23: www.hybridtheory.com/opt-out/ is a COOKIE "
        "opt-out, not a record removal. The page offers an opt-out button "
        "that stops ad personalisation, and otherwise points at the "
        "browser's own privacy settings and at the industry portal "
        "youronlinechoices.com. Nothing on it accepts an identity or asks "
        "the company to delete anything, and a browser-local flag is not "
        "a removal request -- the same finding as adelement-com and "
        "addefend-com. The dataset carries no opt_out_email for this row "
        "either."
    ),
    "cardlytics-com": (
        "Verified by browser render 2026-09-23, and this row is also a "
        "dataset defect. The recorded opt_out_url, "
        "datagrail.cardlytics.com, returns a hard HTTP 404 ('Page not "
        "found') -- the DataGrail portal is gone, not merely JS-rendered. "
        "www.cardlytics.com/privacy-notice and /privacy also 404; the "
        "live policy is www.cardlytics.com/privacy-policy, which was read "
        "in full (about 50k characters) and offers NO web form of any "
        "kind: every rights path it names is a mailbox. Its California, "
        "Colorado and Connecticut sections each say rights are exercised "
        "'by emailing us at privacy@cardlytics.com', and appeals go to "
        "the same address with the subject 'Appeal of Consumer Rights "
        "Request'. Separately, the policy says opting out of a card- "
        "linked marketing program is done through the Publishing Partner "
        "(the consumer's own bank), not through Cardlytics. Mailbox-only "
        "with no webform is the definition of this bucket. Note the "
        "dataset carries legalnotices@cardlytics.com while the policy "
        "names privacy@cardlytics.com -- the dataset should be corrected "
        "on both the dead URL and the address."
    ),
}


# Opt-out legs with NO verdict yet, and the reason there is none.
#
# The opt-out twin of ``search_forms.SEARCH_UNDECIDED``, and it exists for
# the same reason: an honest "we do not know" has to be writable, or the
# pressure at the end of a batch is to round every open question down to the
# nearest decided-looking bucket. Nothing here is a finding about the broker;
# each entry is a finding about the ATTEMPT, and says what would have to be
# true to finish it.
#
# Notes, not behaviour: nothing reads this at runtime.
OPTOUT_UNDECIDED = {
    "epsilon-com": (
        "NO VERDICT as of 2026-09-23, and of the four undecided opt-out "
        "legs in this batch it is the one closest to shipping: the form is "
        "single-page, fully transcribed, and every value it needs already "
        "has a source in resolve_fields. It is recorded here rather than "
        "in RECIPES only because this pass ran no dry run -- see the "
        "correction note under truepeoplesearch-com for why that line is "
        "being held.\\n"
        "\\n"
        "Where it is: the dataset's /privacy/consumer-preference-center "
        "is a marketing page whose 'Your Privacy Choices' link goes to "
        "https://legal.epsilon.com/dsr/, titled 'Epsilon - Data Subject "
        "Request'. That is the real surface.\\n"
        "\\n"
        "Its shape is progressive, so it would need FormRecipe's ordered "
        "``steps`` rather than choices+fields -- the later controls do not "
        "exist until the earlier ones are answered, exactly the Nielsen "
        "problem. Observed order, driven live with no personal data "
        "typed: pick country (select[name='country'] -> 'United States'), "
        "which reveals a requestType radio group of eight "
        "(sales='Do not sell my Personal Information', share, access, "
        "correct, delete, profile, sensitive, appeal); choosing 'sales' "
        "and then select[name='user'] -> 'Consumer' reveals the rest, "
        "which is flat: input[name='email'], [name='first'], "
        "[name='last'], [name='address'], [name='city'], "
        "select[name='state'], input[name='zip'], and "
        "button[name='Submit'].\\n"
        "\\n"
        "Two things a recipe author must weigh first. (1) The bot check is "
        "INVISIBLE reCAPTCHA v3 -- a grecaptcha-badge and a "
        "g-recaptcha-response textarea, no widget to solve -- which is the "
        "same hazard already written up at length on "
        "ADVANCEDBACKGROUNDCHECKS: there is nothing for a human to do, so "
        "the captcha-stop safety net may not fire and this form could "
        "reach a real unattended Submit. (2) 'Do not sell' and 'Delete' "
        "are separate radio options, and which one is filed on Penn's "
        "behalf is his call, not a recipe author's. Nothing was submitted; "
        "only the two gating selects and one radio were touched, with no "
        "identity data of any kind entered. Its SEARCH leg is recorded "
        "under search_forms.NO_SEARCH_SURFACE."
    ),
    "truepeoplesearch-com": (
        "NO VERDICT as of 2026-09-23, and this entry exists to correct one "
        "I made earlier the same day. The form was read accurately: "
        "/removal POSTs to /removal/beginremovalidv with "
        "RightsExerciseType, FirstName, MiddleName, LastName, Email, an "
        "agent block, an AuthorizeContact checkbox and hCaptcha, and the "
        "page's own four numbered steps say it only MAILS you a link -- "
        "'We will send a link to your email address that will take you to "
        "the opt-out form', expiring in 24 hours -- with the record "
        "details entered on that later page.\n"
        "\n"
        "I first filed that under OPTOUT_OUT_OF_SCOPE, reasoning that a "
        "form reached only through a mailbox has no URL to point a "
        "FormRecipe at. That reasoning is refuted by this file's own "
        "shipped work: ADVANCEDBACKGROUNDCHECKS is the SAME magic-link "
        "shape and is a live recipe, with success_markers documented to "
        "mean 'the link request was accepted' rather than 'the opt-out is "
        "complete'. So the shape is in scope, and calling it out of scope "
        "was wrong.\n"
        "\n"
        "What is actually missing is verification, not scope. Every shipped "
        "recipe here was proven by a DRY RUN through optout_submit -- real "
        "browser, synthetic identity, fill, screenshot, stop before Submit "
        "-- and this batch was explicitly a data-only pass that runs no "
        "automation, so no dry run was performed and none of these "
        "selectors has ever been driven. Shipping a recipe on a "
        "hand-transcription alone would put a form this tool can really "
        "submit behind an allow-list entry nobody has watched fill in. The "
        "honest state is therefore 'transcribed, not verified'. To finish: "
        "one dry-run fill with the screenshot checked, plus success_markers "
        "read off the resulting page. Its SEARCH leg is a shipped, working "
        "recipe; see search_forms.TRUEPEOPLESEARCH."
    ),
    "usphonebook-com": (
        "NO VERDICT as of 2026-09-23, for exactly the reason given under "
        "truepeoplesearch-com above, and corrected out of "
        "OPTOUT_OUT_OF_SCOPE alongside it. The dataset's /opt-out 301s to "
        "/removal, whose 'US Phone Book Opt-Out Form' POSTs to /removal "
        "with _token, user-type, subject-firstname/middlename/lastname, "
        "subject-email, an agent block, an agreement checkbox and "
        "reCAPTCHA, and mails a one-time link expiring in 24 hours before "
        "showing the form that names a person. Read off this site's own "
        "page rather than inherited from its sibling: different field "
        "names, different action path, reCAPTCHA here against hCaptcha "
        "there. Same magic-link shape as the shipped "
        "ADVANCEDBACKGROUNDCHECKS recipe, so in scope; unverified because "
        "this pass ran no dry run. Its SEARCH leg is a shipped, working "
        "recipe; see search_forms.USPHONEBOOK."
    ),
    "searchpeoplefree-com": (
        "NO VERDICT as of 2026-09-23, same correction as the two above. "
        "/opt-out carries o_first, o_middle, o_last, o_email, an o_terms "
        "checkbox, a Cloudflare Turnstile response field and "
        "button#o_submit, under the same four numbered magic-link steps. "
        "Read off this site's own page: the o_* field names match neither "
        "sibling's, and the bot check is Turnstile where they use hCaptcha "
        "and reCAPTCHA. In scope by the ADVANCEDBACKGROUNDCHECKS "
        "precedent, unverified for want of a dry run. Its SEARCH leg is a "
        "shipped, working recipe; see search_forms.SEARCHPEOPLEFREE."
    ),
    "instantcheckmate-com": (
        "NO VERDICT as of 2026-09-23, corrected out of "
        "OPTOUT_OUT_OF_SCOPE with the three above, and the one of the four "
        "with a second open question.\n"
        "\n"
        "Where it goes: /privacy-center/ redirects to "
        "app.instantcheckmate.com/privacy-center/, whose Public Data Tools "
        "link off-site to https://suppression.peopleconnect.us/?brand="
        "InstantCheckmate. That hop is NOT the affiliate-front finding "
        "that sends publicrecordsnow-com and peoplefinder-com to "
        "NO_OPTOUT_SURFACE: PeopleConnect is this broker's own parent, and "
        "the page states its scope itself -- it suppresses the Background "
        "Report 'on all people search sites in the PeopleConnect family "
        "including TruthFinder.com, InstantCheckmate.com, Intelius.com and "
        "USSearch.com'. A request filed there is filed for this broker. It "
        "is also free, and says so.\n"
        "\n"
        "What it asks: 'Step 1 Enter your email address. Upon submission "
        "of your email address you will receive a verification email with "
        "a link to proceed.' One email box beside a required 'I agree to "
        "the Terms of Use and Privacy Policy' checkbox. The magic-link half "
        "is in scope per ADVANCEDBACKGROUNDCHECKS. The checkbox is the open "
        "question, and it is not a captcha or a honeypot: ticking it is "
        "ACCEPTING A THIRD PARTY'S TERMS in Penn's name, which is a "
        "different act from supplying his name and email, and no shipped "
        "recipe currently does it (L.S Mobile's Check step is a 'this "
        "information is accurate' attestation about the request itself, "
        "not an agreement to the broker's terms). Nothing was submitted "
        "and the box was not ticked. To finish: a decision from Penn on "
        "whether a recipe may agree to broker terms, and then a dry run. "
        "Its SEARCH leg is separately undecided; see "
        "search_forms.SEARCH_UNDECIDED."
    ),
    "unitedstatesphonebook-com": (
        "Left undecided on purpose, and carried forward from batch 1 "
        "rather than re-opened: removal there is a per-search-result "
        "'Remove' button, not a fixed form, so there is nothing for a "
        "FormRecipe to point at without first running a search and "
        "choosing a row. Confirmed first-hand on 2026-09-23 that the "
        "SECOND channel is a mailbox: its privacy policy says 'We will "
        "remove your data from this site upon your request, please use "
        "the e-mail below to request including what SPECIFIC data to "
        "remove, and from which site', and there is no removal form "
        "linked anywhere on the site. Both shapes are outside a recipe, "
        "but the entry stays here rather than moving to out-of-scope or "
        "no-surface because leaving this leg alone was an explicit "
        "instruction, and quietly converting somebody's 'don't decide "
        "this' into a decision is how a note becomes a fact. Its SEARCH "
        "leg is a shipped, working recipe."
    ),
    "acxiom-com": (
        "No verdict as of 2026-09-23, for the same outage recorded in "
        "search_forms.SEARCH_UNDECIDED, observed from the opt-out side. "
        "The dataset's opt-out URL, https://isapps.acxiom.com/optout/"
        "optout.aspx, does still exist in the sense that it 302s -- to "
        "https://www.acxiom.com/optout/ -- but that destination answers "
        "HTTP 500 with 'Something went wrong / Try again' and no form of "
        "any kind, as does every other path on the domain. Requesting "
        "isapps.acxiom.com directly gets an Imperva/Incapsula block page "
        "instead. Neither observation supports a finding: a 500 is not "
        "'no surface', an Incapsula block on a DIFFERENT host is not a "
        "wall in front of THIS form, and Acxiom's opt-out has historically "
        "been a real self-service webform, so recording it as absent or "
        "blocked would both be unsupported and would teach the next "
        "reader something false. Retry cold on another day."
    ),
    "infotracer-com": (
        "No verdict as of 2026-09-23, and deliberately not guessed. "
        "infotracer.com/optout/ answers HTTP 200 with the body 'Sorry this "
        "page was requested too many times. If you feel this is an error, "
        "please go back and reach out to support.' -- while the same "
        "domain's homepage and its whole search flow serve normally in the "
        "same session, so this is a per-path throttle rather than a wall "
        "or an outage. It is most likely self-inflicted (this batch drove "
        "the search leg hard), and it did not clear over several hours or "
        "for any User-Agent, so it is an IP-scoped cooldown. The right "
        "next step is a single cold visit on another day, NOT more "
        "attempts today: the page is almost certainly a normal opt-out "
        "form, and recording it as blocked would both be a claim I cannot "
        "support and quietly teach the next reader that InfoPay walls its "
        "removal pages, which the three sibling sites with shipped "
        "recipes (courtrecords.us, staterecords.org, recordsfinder.com) "
        "show it does not."
    ),
    "adept-id-com": (
        "NO VERDICT as of 2026-09-23. www.adept-id.com/opt-out- "
        "preferences/ does serve a real form, in two parts: a cookie "
        "section (a checkbox labelled 'Opt out of cookies (apart from "
        "necessary cookies)' with a Submit) and an email-marketing "
        "section (an 'Email *' field, radios 'Opt out of marketing "
        "communications' / 'Opt in to marketing communications', and a "
        "Submit). What stops a recipe is that a consent-management widget "
        "(Functional/Preferences/Statistics/Marketing with "
        "Accept/Deny/Save preferences) renders over the same page and the "
        "page needs JavaScript, so it could not be established whether "
        "the visible form posts to AdeptID or is intercepted by the "
        "consent platform. Also unsettled: whether an email-preference "
        "opt-out is a RECORD removal at all, which matters more here than "
        "the selectors. Next pass needs a real browser and a look at "
        "where Submit actually posts."
    ),
    "adrearubin-com": (
        "NO VERDICT as of 2026-09-23, and blocked at the same wall as the "
        "search leg: adrearubin.com refused TLS on every attempt, so no "
        "page was read. Second-hand sources (optoutindex.com and the "
        "company's own CCPA disclosure as quoted elsewhere) say consumers "
        "may opt out 'by either calling a number listed on our website or "
        "filling out a form on our website', which would mean a form "
        "exists -- but that claim has not been seen on the live site, and "
        "the dataset's one contact, jenniferv@adrearubin.com, hard- "
        "bounced on 2026-08-23. Next pass: reach the site at all, then "
        "check whether the promised form is real."
    ),
    "adsquare-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL "
        "(adsquare.com/privacy/us_privacy_supplement/#goto-contact-us) "
        "404s. The live neighbour, adsquare.com/privacy/, does carry a "
        "genuine privacy-request form -- observed controls: a "
        "Website/Country dropdown, request-type checkboxes including "
        "'Object or Opt-out from Processing', First name, Last name, a "
        "required Email, a 5000-character request-details textarea, and "
        "an optional MAID (mobile advertising ID) field -- plus a 'Do Not "
        "Sell or Share My Personal Information' footer link and the "
        "contact privacy@adsquare.com (the dataset carries "
        "legal@adsquare.com instead). It sits here rather than in RECIPES "
        "because those controls were read off /privacy/ as prose, with no "
        "element names, no submit-button text and no dry run, and because "
        "the dataset's own URL has to be corrected first."
    ),
    "mediaocean-com": (
        "NO VERDICT as of 2026-09-23, blocked at the same wall as the "
        "search leg: www.mediaocean.com/your-privacy-rights and its "
        "trailing-slash variant both failed with 'unable to verify the "
        "first certificate', so the page was never read. A result titled "
        "'Privacy request | Mediaocean' and adjacent snippets say rights "
        "may be exercised by emailing datasecurity@mediaocean.com 'or by "
        "form' -- which suggests a form exists but names nothing on it. "
        "Note the dataset carries ccpa@mediaocean.com instead. Next pass: "
        "a fetcher that tolerates the certificate chain, then transcribe "
        "the form."
    ),
    "5x5data-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL (5x5data.com/dsr- "
        "out/) 404s. The live path is 5x5data.com/privacy-policy/, whose "
        "'Do Not Sell or Share My Personal Information' link points at "
        "privacy.5x5data.com/5x5/opt-out -- and that page, fetched twice, "
        "returned only its title ('Opt Out - 5x5 Data') with no body at "
        "all, i.e. a JS-rendered app shell, most likely a consent- "
        "management portal. A real surface is there; nothing about its "
        "fields is known. Next pass needs a JS-capable browser, and the "
        "dataset's dead URL should be corrected to the "
        "privacy.5x5data.com one."
    ),
    "6sense-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL "
        "(6sense.com/privacy-center/) 301-redirects to "
        "privacy.6sense.com, a DataGrail-powered 'Privacy Request "
        "Center'. Two fetches returned only the page title ('Privacy "
        "Request Center | DataGrail') and no body, the familiar signature "
        "of a third-party JS consent portal that does not render to a "
        "static fetcher. So a real request surface exists and not one "
        "field of it is known. Next pass: a JS-capable browser against "
        "privacy.6sense.com."
    ),
    "accudata-com": (
        "A REAL and unusually complete form, held back from RECIPES for a "
        "substantive reason as well as the usual one. Verified "
        "2026-09-23: optout.accudata.com 302-redirects to "
        "privacy.deepsync.com (Accudata now being part of Deep Sync), "
        "which serves radios for 'Who is this request for?' (Myself / A "
        "deceased individual / Someone I'm authorized to represent), then "
        "'First name', 'Last name', repeatable 'Email address' and 'Phone "
        "number', 'Primary address' with a 'State' dropdown, up to five "
        "'Additional address' blocks each with its own state dropdown, "
        "and a 'What would you like us to do?' checkbox group (do not "
        "sell/share, do not use for targeted advertising, do not use for "
        "profiling, limit sensitive information use, delete personal "
        "information); submit reads 'Submit request.' The substantive "
        "blocker: the page states identity is verified by 'dynamically- "
        "generated questions' AFTER submission, so the flow does not end "
        "at the form and a recipe that stopped there would report a "
        "success it did not achieve."
    ),
    "acronymix-com": (
        "NO VERDICT as of 2026-09-23, for the same reason as the search "
        "leg: acronymix.com/privacy-policy/ returns HTTP 500 on both "
        "WebFetch and a direct curl, so the page was never rendered and "
        "neither the presence nor the absence of a form can be stated. "
        "The fallback channel the dataset carries is "
        "privacy@acronymix.com. Recheck when the origin stops erroring."
    ),
    "activimpact-ai": (
        "NO VERDICT as of 2026-09-23. The dataset's URL "
        "(www.activimpact.ai/dsar?hsLang=en) is live and redirects 200 to "
        "activimpact.ai/dsar, but two separate fetches rendered only nav "
        "and footer text ('Your Privacy Choices', a 'DSAR' link) with no "
        "form fields. The hsLang=en parameter is a HubSpot convention, so "
        "this is almost certainly a JS-rendered HubSpot embedded form "
        "that a static fetcher cannot materialise. Next pass needs a JS- "
        "capable browser to name the fields."
    ),
    "idology-com": (
        "NO VERDICT as of 2026-09-23, and this one is unfinished research "
        "rather than a wall. The dataset declares email-only "
        "(compliance@gbgplc.com) with no URL, and IDology is now GBG, so "
        "the question is whether GBG publishes a web DSAR surface "
        "alongside that mailbox. One guessed path (www.gbg.com/en- "
        "us/privacy-notice/) 404'd and the real privacy/DSAR page was not "
        "located in the time available. Next pass: find GBG's actual "
        "privacy page from its own footer and decide the leg there -- do "
        "not assume email-only just because the dataset says so."
    ),
    "adstradata-com": (
        "NO VERDICT as of 2026-09-23. www.adstradata.com/privacy-policy/ "
        "embeds no form of its own; it repeatedly links out to a OneTrust "
        "consent-portal widget for CA/CO/CT/NV/UT/VA state requests and "
        "promotional opt-out, alongside DAA and bluecava ad-choices links "
        "and a direct channel (privacy.officer@adstradata.com, plus a "
        "Princeton NJ postal address). The real mechanism is therefore "
        "the OneTrust widget, and it did not render its field structure "
        "to the fetcher. Next pass: a JS-capable browser to capture the "
        "OneTrust webform's URL and fields."
    ),
    "smartsheet-com": (
        "NO VERDICT as of 2026-09-23, and note this leg is about "
        "AdvisorTarget, not Smartsheet. The dataset's URL, "
        "app.smartsheet.com/b/form/48a44f05a4644a1f824601e77ad1719d, is "
        "live and is a real Smartsheet-hosted form page (fetched twice; "
        "the markdown carried only the shell and the title 'Smartsheet "
        "Forms'), whose fields render client-side and never surfaced. "
        "Corroboration that it is the right form: finsum.com's own 'Your "
        "Privacy Choices' page links to this exact URL for "
        "AdvisorTarget/Finsum requests. Next pass needs a JS-capable "
        "browser; Smartsheet forms have stable field markup once "
        "rendered."
    ),
    "finsum-com": (
        "NO VERDICT as of 2026-09-23, and it resolves to the same form as "
        "smartsheet-com -- these two rows are one company. The dataset's "
        "URL (www.finsum.com/index.php/what-we-do/privacy-policy) 404s; "
        "the working equivalent is finsum.com/what-we-do/privacy-policy, "
        "which embeds no form and links to finsum.com/what-we-do/your- "
        "privacy-choices, which also embeds none and sends the reader to "
        "an external 'Privacy Request Form' at "
        "app.smartsheet.com/b/form/48a44f05a4644a1f824601e77ad1719d. "
        "Undecided for the identical JS-rendering reason recorded under "
        "smartsheet-com; whoever cracks that form closes both legs at "
        "once."
    ),
    "affinityanswers-com": (
        "A REAL form, and closer to shippable than most here. Verified "
        "2026-09-23: www.affinityanswers.com/your-privacy-choices/ serves "
        "a consumer opt-out form with fields for LinkedIn, Residency (a "
        "country/state dropdown), a required Email, Social Media Handles, "
        "and privacy-rights checkboxes whose options VARY BY RESIDENCY "
        "(access / deletion / correction / opt-out-of-sale). That "
        "conditional behaviour, not just the missing submit-button label "
        "and element names, is what holds it out of RECIPES: a flat "
        "FormRecipe cannot express controls that appear only after the "
        "residency answer. A recipe-writer should check whether the "
        "US/California branch alone is flat enough to encode."
    ),
    "affinity-solutions": (
        "NO VERDICT as of 2026-09-23. www.affinity.solutions/data- "
        "privacy-notice/ embeds no form; it directs the reader to a "
        "third-party OneTrust portal at affinitysolutions- "
        "privacy.my.onetrust.com ('Filling out this online form'), "
        "alongside a postal address (112 West 34th Street, 18th Fl, New "
        "York, NY) and a toll-free number (877-218-7776). The OneTrust "
        "widget did not render its structure to the fetcher, so a real "
        "surface exists with none of its fields known. Next pass: a JS- "
        "capable browser against the my.onetrust.com host."
    ),
    "aidentified-com": (
        "NO VERDICT as of 2026-09-23. www.aidentified.com/delete-my-data "
        "is live and is plainly the right page -- it renders a 'Delete My "
        "Data' heading and the consent sentence 'By submitting this form, "
        "you are agreeing to aidentified Privacy Policy and Terms of "
        "Service' with links to both -- but not one field name, label or "
        "submit control surfaced, which is the signature of a client- "
        "rendered embed (HubSpot/Formstack or similar). The form exists; "
        "its mechanics are unknown. Next pass needs a JS-capable browser."
    ),
    "arccorp-com": (
        "NO VERDICT as of 2026-09-23. www2.arccorp.com/site-privacy- "
        "policy/ is policy prose that hands off to an external subject- "
        "access portal: 'please fill out this form: "
        "https://my.datasubject.com/Uw03R7mCGS/60313', alongside "
        "privacy@arccorp.com and a phone number. That portal was followed "
        "and rendered only a 'Data Access Request' heading with no "
        "fields, dropdowns or submit control -- another JS-rendered "
        "third-party widget. Note the dataset carries no opt_out_email "
        "for this row; my.datasubject.com/Uw03R7mCGS/60313 is the URL a "
        "future pass should attack."
    ),
    "aisinfo-com": (
        "NO VERDICT as of 2026-09-23. www.aisinfo.com/privacy (the "
        "#california-privacy-rights section) tells CA and TX residents to "
        "use 'Webform: www.aisinfo.com/contact-us' alongside "
        "contactnow@aisinfo.com and a phone number -- note that is a "
        "different address from the dataset's arsllc@aisinfo.com. The "
        "contact-us page does reference an 'INQUIRY FORM' (an #inquiry- "
        "form anchor) and carries 'Do Not Sell / Share My Personal Info' "
        "and 'CA Notice at Collection' links, but its fields and submit "
        "control did not render, so it is a JS-rendered form of unknown "
        "shape. A second question for the next pass: whether a general "
        "inquiry form is an opt-out surface at all, or just a mailbox "
        "with a skin."
    ),
    "alabamacourtrecords-us": (
        "NO VERDICT as of 2026-09-23, though a real network-level form "
        "was found. alabamacourtrecords.us/optout embeds nothing itself; "
        "it routes to (1) courtrecords.us/do-not-sell-share-my-personal- "
        "information/ and (2) an external TrustArc form at submit- "
        "irm.trustarc.com, plus privacy@courtrecords.us for 'Covered "
        "Person Removal' (which wants a specific subject line plus name, "
        "residence and email). The courtrecords.us page WAS fetched and "
        "does embed a genuine form titled 'Request to Opt-Out of the Sale "
        "and/or Sharing of Your Personal Information' with 'First Name "
        "*', 'Last Name *', a 'State *' dropdown, 'City', and a 'Submit' "
        "button. It stays undecided because the form lives on the NETWORK "
        "domain rather than this row's domain, its element names were "
        "never captured, the TrustArc branch was never opened, and it is "
        "unsettled whether a do-not-sell request actually delists a "
        "record from the state site."
    ),
    "alaskacourtrecords-us": (
        "Identical structure and identical open questions as "
        "alabamacourtrecords-us -- one network, one shared form. Verified "
        "2026-09-23: alaskacourtrecords.us/optout embeds no form and "
        "delegates to courtrecords.us/do-not-sell-share-my-personal- "
        "information/ (the confirmed First Name*, Last Name*, State* "
        "dropdown, City, Submit form) plus a TrustArc data-subject-rights "
        "form and privacy@courtrecords.us for Covered Person and "
        "expungement removals. Whoever resolves the network form should "
        "decide both state rows, and any others of this family in the "
        "dataset, in one pass."
    ),
    "mydataprivacy-com": (
        "NO VERDICT as of 2026-09-23, and the interesting failure is that "
        "the opt-out is not where the dataset points. The given URL, "
        "www.mydataprivacy.com/upload-csv, rendered only marketing copy "
        "('Drag and drop file uploads', 'Easily Manage & Upload Data "
        "Files') and no actual file-picker -- that is the BUSINESS side "
        "of Alesco's compliance product and almost certainly needs a "
        "login. The consumer path is the homepage's own form ('Email "
        "Address' or 'Name & Postal Address', with a 'Search' button), "
        "but that is a LOOKUP: the opt-out is whatever it offers after a "
        "hit, and none of those next-step mechanics were observed. A "
        "recipe here would have to walk search-then-act, which is not a "
        "flat form, and the dataset's URL should be corrected."
    ),
    "alikeaudience-com": (
        "NO VERDICT as of 2026-09-23, and the doubt is about meaning "
        "rather than mechanics. privacy-optout.alikeaudience.com renders "
        "what looks like a real form -- 'Please select your Advertising "
        "Type here:' with email/ios/android options, 'Please enter your "
        "email address:', and a 'Submit' button -- but that is a "
        "device/advertising-identifier opt-out rather than a PII deletion "
        "request, so it is unclear what submitting it would actually "
        "remove. Also unverified: whether those controls are first-party "
        "markup or a consent widget's rendering, since only a single "
        "fetch saw them."
    ),
    "termly-io": (
        "NO VERDICT as of 2026-09-23, and note this leg IS "
        "01Advertising's even though the domain is Termly's. The "
        "dataset's URL "
        "(app.termly.io/notify/d1321341-0e82-4d8c-afaf-c4940d46c171) is "
        "live and is plainly the right page -- it renders the heading "
        "'Data Subject Access Request Form' -- but two separate fetches "
        "returned only a static shell with no field markup, the familiar "
        "signature of a third-party JS-rendered widget. So a real DSAR "
        "surface exists behind a Termly-hosted form whose fields are "
        "entirely unknown. Next pass needs a JS-capable browser; solving "
        "the Termly widget once would likely unlock every Termly-hosted "
        "row in this dataset."
    ),
    "01advertising-com": (
        "NO VERDICT as of 2026-09-23. www.01advertising.com/legal/ failed "
        "with no response on repeated attempts over both https and http, "
        "so nothing was rendered. Outside sources describe that path as a "
        "'Legal Portal' listing a Privacy Policy, Terms, Cookie Policy, "
        "Consent Preferences and a 'Do Not Sell My Information' option, "
        "with legal@01advertising.com as the contact -- but whether 'Do "
        "Not Sell' is a real form, a consent-widget toggle or just a link "
        "is exactly what could not be seen. Note the dataset carries "
        "privacy@01advertising.com on the sibling termly-io row and no "
        "email here. Next pass needs a fetcher the host will serve."
    ),
    "180bytwo-com": (
        "NO VERDICT as of 2026-09-23, with one useful discovery about the "
        "domain. 180bytwo.com's ROOT now 302-redirects wholesale into "
        "anteriad.com (180byTwo having been absorbed into Anteriad), but "
        "the dataset's deeper path, "
        "180bytwo.com/privacycaliforniaresidents/, still resolves "
        "independently and rendered fine. That page embeds no form; it "
        "offers four channels -- a toll-free line, a link to an external "
        "'OneTrust Privacy Management Webform', privacy@180bytwo.com, and "
        "a postal address. The real form is on the OneTrust portal, which "
        "was not followed, so no field is known. Next pass: capture the "
        "OneTrust webform URL from that page and attack it with a JS- "
        "capable browser."
    ),
    "192-com": (
        "NO VERDICT as of 2026-09-23. www.192.com/misc/privacy-policy/ is "
        "a standard GDPR privacy policy with data-subject-rights language "
        "and NO embedded suppression or removal form. The only actionable "
        "controls found are an account-settings 'change e-mail "
        "preferences' toggle, which governs marketing email rather than "
        "the record, and contact by post (192.com Customer Services, 36 "
        "Southwark Bridge Rd, London SE1 9EU) or email. It sits here "
        "rather than under NO_OPTOUT_SURFACE because a UK people-search "
        "of this size is required to offer a suppression route and one "
        "was probably not found rather than absent -- look for an "
        "Electoral Roll / edited-register opt-out path, and read EU- "
        "NOTES.md on whether a non-UK subject can use it at all."
    ),
    "33mileradius-com": (
        "NO VERDICT as of 2026-09-23. www.33mileradius.com/privacy- "
        "policy/ embeds no form; it says opt-out requests go through a "
        "'Do Not Sell or Share My Personal Information' link elsewhere on "
        "the site, or by email, phone or mail (EverConnect, 7700 Irvine "
        "Center Drive Suite 430, Irvine CA, with two numbers listed). "
        "That referenced Do-Not-Sell page was never located or fetched, "
        "so the mechanics are unknown. Worth noting the sibling row "
        "remodeling-com is the same corporate family "
        "(EverCommerce/EverConnect) and DOES have a transcribed CCPA form "
        "-- a future pass should check whether 33 Mile Radius routes to "
        "that same form."
    ),
    "33across-com": (
        "NO VERDICT as of 2026-09-23: fully blocked. Every relevant "
        "33Across URL returned HTTP 403 to this fetcher -- the homepage, "
        "/privacy-policy/ccpa-notice, /user-data-portal, and the "
        "dataset's own opt_out_url (a CCPA notice PDF) -- so no content "
        "was retrieved anywhere. Outside references point at a 'Do Not "
        "Sell My Information' link and a 'user-data-portal' as the "
        "intended mechanism, and www.33across.com/user-data-portal is the "
        "URL a future pass should attack, with a fetcher that is not "
        "403'd. Note the dataset's opt_out_url is a PDF, which would not "
        "be a submittable surface even if it loaded."
    ),
    "411-com": (
        "NO VERDICT as of 2026-09-23, and the wall is on Whitepages, not "
        "411. 411.com's own privacy policy routes removals through "
        "Whitepages' suppression process, and the dataset points at "
        "www.whitepages.com/suppression-requests -- which this fetcher "
        "cannot reach at all ('unable to fetch', no content, on every "
        "attempt). Multiple independent opt-out guides describe a "
        "consistent multi-step flow (paste your Whitepages profile URL, "
        "confirm the profile, click 'Remove Me', pick a reason from a "
        "dropdown, then verify by PHONE via a 'Call now to verify' button "
        "and a confirmation code), but none of that was observed first- "
        "hand, so it is recorded as secondary-source hearsay rather than "
        "fact. Two things for the next pass: get a fetcher Whitepages "
        "will serve, and note that a per-PROFILE flow plus phone "
        "verification may land this in OPTOUT_OUT_OF_SCOPE rather than "
        "RECIPES. The whitepages-com row should be decided first and this "
        "one inherited from it."
    ),
    "attribits-com": (
        "A REAL but very thin form. Verified 2026-09-23: "
        "www.attribits.com/do-not-sell carries a single input under "
        "'Please enter your email address below', used to request removal "
        "from All Good Media's databases and to stop the sale of that "
        "email; compliance@allgoodmediagroup.com and a postal address are "
        "listed as alternates. It is undecided rather than a recipe "
        "because the submit control was never captured, no element names "
        "were seen, and -- more importantly -- an email-keyed removal can "
        "only act on records already keyed to that address, so what it "
        "actually removes is unsettled."
    ),
    "allwebleads-com": (
        "NO VERDICT as of 2026-09-23, and the doubt is about meaning "
        "rather than reachability. dnc.allwebleads.com/Unsubscribe is "
        "live and is a real form with two fields: 'Email' (unsubscribe "
        "from communications) and 'Phone' (add to a Do Not Contact list). "
        "But that is a SUPPRESSION list for contact, not a deletion of "
        "the lead record, so submitting it would not be the removal this "
        "tool means; the submit control was also never captured. A "
        "recipe-writer should decide whether a do-not-contact flag counts "
        "here before encoding it, and check whether AWL offers a separate "
        "CCPA deletion path under awl.com."
    ),
    "allantgroup-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL is a OneTrust "
        "privacy-portal webform "
        "(privacyportal.onetrust.com/webform/cbbe21b6-d675-445f-9c24-f625c01dafb3/bebf975c-f540-4f99-8b73-116ca8cb28be), "
        "it is live and is the right surface, and the fetch returned only "
        "the bare title 'OneTrust | Privacy Management Software' with no "
        "fields -- the standard JS-rendered consent-widget failure. Next "
        "pass needs a JS-capable browser. OneTrust webforms share a "
        "markup family, so whoever cracks one of them should sweep every "
        "OneTrust row in this dataset at once."
    ),
    "alliantinsight-com": (
        "NO VERDICT as of 2026-09-23. alliantinsight.com/data-services- "
        "and-website-privacy-policy/ embeds no form of its own; it links "
        "out to SEPARATE OneTrust-hosted forms for 'Opt Out of "
        "Sale/Sharing', 'Deletion', 'Records Access' and 'Correction', "
        "plus a phone number carrying AnalyticsIQ's name. None of those "
        "linked forms rendered their fields to the fetcher. Note the "
        "branching: a recipe here has to choose WHICH of the four forms "
        "is the opt-out, which is a decision, not a detail. Same "
        "corporate family and same portal as analytics-iq-com; resolve "
        "them together."
    ),
    "allpeople-com": (
        "A REAL removal surface -- and the flow, not the fields, is what "
        "holds it back. Verified 2026-09-23: allpeople.com/removal serves "
        "a form with the same four fields as the site search (Name, "
        "Email, Phone, Industry) and a 'Begin Removal Process' submit "
        "button, and the page spells out a five-step flow: agree to terms "
        "and complete a CAPTCHA, click Begin Removal Process, SEARCH FOR "
        "AND LOCATE YOUR OWN RECORD in the results, click Remove on it, "
        "then confirm through an emailed link, with removal completing "
        "within 72 hours. That is a per-RESULT flow behind a CAPTCHA, "
        "which is the shape the codebase files under OPTOUT_OUT_OF_SCOPE "
        "(see the spokeo-com entry) -- it is recorded here instead only "
        "because no one has yet confirmed the CAPTCHA is unavoidable. "
        "Decide that first; if it is, move this entry."
    ),
    "alphonso-tv": (
        "NO VERDICT as of 2026-09-23. alphonso.tv/privacy/consumer/ "
        "embeds no form; it offers two buttons linking to "
        "choice.alphonso.tv/donotsell ('Do Not Sell My Personal "
        "Information') and choice.alphonso.tv/otherccparequests ('Other "
        "Requests'). The do-not-sell link was followed and turned out to "
        "be a React single-page app that rendered only a 'React.js "
        "Boilerplate' placeholder to the fetcher, so no field surfaced. "
        "Note the dataset carries compliance@lgads.tv, reflecting the LG "
        "Ads acquisition. Next pass: a JS-capable browser against "
        "choice.alphonso.tv, and a check of whether the opt-out is keyed "
        "to a TV device identifier rather than a person."
    ),
    "altairdata-com": (
        "NO VERDICT as of 2026-09-23, and this one is an unusual host. "
        "altairdata.com/consumer-info/ embeds no form; it directs "
        "consumers to an external Atlassian Jira Service Desk portal at "
        "datacloudhome.atlassian.net/servicedesk/customer/portal/12, or "
        "to postal mail. That portal is anonymously accessible and lists "
        "five request types -- Individual Opt-Out/Deletion, Authorized "
        "Agent Opt-Out/Deletion, Automation Request, Correction, Access "
        "Personal Information -- but the fields behind each option never "
        "rendered. Two things for the next pass: name the fields behind "
        "the Individual Opt-Out request type, and note that a Jira "
        "Service Desk form is a different markup family from the OneTrust "
        "widgets elsewhere in this dataset, so it needs its own handling."
    ),
    "acbj-com": (
        "NO VERDICT as of 2026-09-23, blocked at the same wall as the "
        "search leg: www.acbj.com/privacy failed with a host-level "
        "'unable to fetch' over both https and http, as did every other "
        "ACBJ host tried. Outside sources confirm ACBJ is CA-registered "
        "as a data broker and that a privacy/CCPA page exists at that "
        "exact path, but no content, no field and no submit mechanism was "
        "ever observed. The dataset carries legal@bizjournals.com. Next "
        "pass needs a fetcher these hosts will serve."
    ),
    "amerilist-com": (
        "A REAL, transcribed CCPA form -- and a CAPTCHA is what actually "
        "decides this one. Verified 2026-09-23: www.amerilist.com/optout "
        "serves a privacy-request form with Name*, Email*, Phone*, "
        "Address*, a Request Type* dropdown whose options include "
        "'Request to opt out of the sale of personal information' "
        "alongside know/access/delete, AND a verification/CAPTCHA field; "
        "the submit button reads 'SUBMIT'. It is recorded here rather "
        "than in RECIPES because a CAPTCHA this tool cannot solve sits on "
        "the form, and because no element names, action or method were "
        "captured. If the CAPTCHA proves unavoidable this belongs with "
        "the search-leg's blocked-style findings rather than as a pending "
        "recipe."
    ),
    "analytics-iq-com": (
        "NO VERDICT as of 2026-09-23. analytics-iq.com/privacy-policy/ "
        "embeds no form; it carries three links -- 'Do Not Sell Or Share "
        "My Personal Information', 'Opt-Out of Targeted Advertising' and "
        "'Opt-Out of Personal Data Use' -- all pointing at an external "
        "OneTrust-hosted portal whose widget did not render to the "
        "fetcher. Alternate channels given: (833) 533-1388 and "
        "Compliance@AlliantData.com, the latter confirming this is the "
        "same corporate family as alliantinsight-com and routing to the "
        "same portal. Resolve the two rows together, and note the same "
        "which-of-three-links problem."
    ),
    "ancestry-com": (
        "NO VERDICT as of 2026-09-23. www.ancestry.com/legal/ccpa- "
        "donotshare-sell is live and correctly titled 'Do Not Sell or "
        "Share My Personal Information and Opt Out of Targeted "
        "Advertising', but two fetches rendered only nav and footer with "
        "no inputs, checkboxes or buttons, and -- unusually -- no "
        "identifiable consent-vendor branding either (no OneTrust, Osano, "
        "Termly or Ketch marker surfaced), so even the widget family is "
        "unknown. Next pass needs a JS-capable browser. Worth pairing "
        "with the search leg's open question: what Ancestry would be "
        "opting a living person out OF, given its product is historical "
        "records."
    ),
    "anchorcomputer-com": (
        "A REAL and unusually detailed form -- and the reason it is not a "
        "recipe is a hard rule, not a missing selector. Verified "
        "2026-09-23: ecom2.anchorcomputer.com/privacyrequest serves First "
        "Name, Last Name, Suffix, Address Line 1, Address Line 2, City, a "
        "State dropdown, Zip Code, SSN (LAST 4 DIGITS), Date of Birth, "
        "Phone Number and Email, plus checkboxes 'Request full report (CA "
        "residents only)', 'Delete my data (Delete any data found)' and "
        "'Do not sell my data (Keep data, but mark as do not sell)'. The "
        "SSN and date-of-birth fields are the point: this codebase does "
        "not hand a broker a partial SSN or a DOB, which is the same line "
        "drawn under chexsystems-com. If those fields turn out to be "
        "optional a recipe becomes possible; until someone confirms that, "
        "this stays undecided, and if they are required it belongs under "
        "NO_OPTOUT_SURFACE. The submit control was also never captured."
    ),
    "andrewswharton-com": (
        "NO VERDICT as of 2026-09-23. The dataset's opt_out_url for this "
        "row is just the HOMEPAGE, www.andrewswharton.com, which is not "
        "an opt-out page -- but the footer does carry a 'Your Privacy "
        "Choices' link (repeated three times), so a real surface is being "
        "pointed at and was never followed. Nothing rendered of whatever "
        "sits behind it. Next pass: follow that footer link, and expect "
        "it to land on Stirista infrastructure, which is currently behind "
        "the anti-bot wall recorded under stirista-com. Correct the "
        "dataset's URL once the real one is known."
    ),
    "networkadvertising-org": (
        "NO VERDICT as of 2026-09-23, and see the search leg first: this "
        "row's domain is the Network Advertising Initiative, not Anne "
        "Lewis Strategies. The dataset's URL, "
        "optout.networkadvertising.org/?c=1, and the bare host both "
        "returned nothing to this fetcher, so nothing was observed. What "
        "the NAI tool is universally described as -- an industry-wide "
        "COOKIE opt-out that sets browser opt-out cookies across member "
        "companies -- would make it a no-surface of the adelement-com "
        "kind rather than a record removal, but that has to be seen "
        "before it is written down. Next pass: render it, then fold this "
        "row into missionwired-com."
    ),
    "missionwired-com": (
        "NO VERDICT as of 2026-09-23. missionwired.com/privacy-policy/ "
        "embeds no form; its California section offers three routes -- a "
        "'Your Privacy Choices' footer link, an email address, and an "
        "external request form at info.missionwired.com/personal- "
        "information-request. That external page WAS fetched and is "
        "plainly the right one (it renders the heading 'Personal "
        "Information Rights Request Form' and refers to 'the form "
        "below'), but not one field, label or button rendered -- a JS- "
        "embedded form, and the info.* subdomain suggests HubSpot. Next "
        "pass needs a JS-capable browser against that URL, and the "
        "dataset's opt_out_url should be corrected to it."
    ),
    "anteriad-com": (
        "NO VERDICT as of 2026-09-23. anteriad.com/privacy-center embeds "
        "no form; it is a hub linking to policy documents and to a 'Do "
        "Not Sell My Personal Information' privacy portal whose vendor it "
        "does not name on the page. Note the sibling row 180bytwo-com "
        "resolved to a OneTrust webform through the same company, so this "
        "is very likely the same OneTrust portal -- worth confirming and "
        "then deciding both rows at once. The dataset carries no "
        "opt_out_email for this row."
    ),
    "hubspot-com": (
        "NO VERDICT as of 2026-09-23, and note the row's name/domain "
        "mismatch recorded on the search leg. The dataset's URL, "
        "preferences.hubspot.com, resolves to a DataGrail-powered "
        "'Privacy Request Center' -- so it is a data-rights request "
        "surface rather than the email-preference centre its hostname "
        "suggests, which is the useful finding here. It rendered only its "
        "title with no fields, the same JS-widget failure as the 6sense- "
        "com row, which is also DataGrail. Solving the DataGrail form "
        "once should close both. Next pass needs a JS-capable browser."
    ),
    "apollo-io": (
        "NO VERDICT as of 2026-09-23, and the doubt is about what it "
        "removes, not whether it works. www.apollo.io/privacy- "
        "policy/remove is live and carries a single input labelled 'Work "
        "email' and a button reading 'Get verification information'; the "
        "page promises 'we will honor your request by removing your "
        "profile from our services, and we will retain the email address "
        "you enter solely for purposes of storing and respecting your "
        "opt-out preference'. Three things are unresolved: the flow does "
        "not end at the button (a verification step follows and was never "
        "walked), an email-keyed removal only reaches records already "
        "keyed to that address, and the field asks specifically for a "
        "BUSINESS email, which a consumer may not have. No CAPTCHA was "
        "seen and no record-picking step is mentioned, so this is a "
        "promising candidate once the verification step is understood."
    ),
    "appsci-io": (
        "NO VERDICT as of 2026-09-23. appsci.io/do-not-sell-my-info "
        "301-redirects to www.appscience.ai/do-not-sell-my-info -- the "
        "two dataset rows share one opt-out page, recorded identically "
        "under appscience-ai. The page says to opt out 'by providing your "
        "email and mobile advertising ID via the form below', and that "
        "form did not render to the fetcher. The mobile advertising ID is "
        "the substantive problem: like adadapted-com, this company's "
        "records are keyed to a MAID this codebase does not hold, so even "
        "a rendered form may be unfillable. Next pass: render it and find "
        "out whether the MAID is required or optional."
    ),
    "appscience-ai": (
        "Same page and same open questions as appsci-io, which redirects "
        "here. Verified 2026-09-23: www.appscience.ai/do-not-sell-my-info "
        "states outright that App Science engages in 'profiling, targeted "
        "advertising and the sale/sharing of Personal Information' and "
        "may be considered to have sold data in the past 12 months, then "
        "offers three routes -- mobile OS opt-out signals, CTV operating- "
        "system signals, and a direct opt-out 'via the form below' taking "
        "an email and a mobile advertising ID. The form itself did not "
        "render. Whether a recipe is possible turns on whether the MAID "
        "field is required."
    ),
    "archives-com": (
        "NO VERDICT as of 2026-09-23, and it inherits the ancestry-com "
        "row's answer. The dataset's URL, "
        "www.archives.com/support/privacy-policy, returns HTTP 404 -- so "
        "the recorded path is simply wrong. Archives.com is an Ancestry "
        "property (its own privacy and terms links point at Ancestry.com) "
        "and the dataset's contact, usprivacyrequests@ancestry.com, "
        "agrees, so the real surface is almost certainly Ancestry's own "
        "ccpa-donotshare-sell page, which is itself recorded as undecided "
        "because its widget renders nothing. Next pass: confirm the "
        "routing, correct the dead URL, and decide both rows together."
    ),
    "aristotle-com": (
        "NO VERDICT as of 2026-09-23: blocked, same as the search leg. "
        "www.aristotle.com/privacy/do-not-sell-my-personal-info/ returned "
        "HTTP 403 and no content was retrieved, so neither the presence "
        "nor the shape of a form can be stated. The page's NAME says it "
        "should be a do-not-sell surface, and the dataset's fallback is "
        "info@aristotle.com -- a general mailbox, not a privacy one. Next "
        "pass needs a fetcher Aristotle will serve."
    ),
    "arity-com": (
        "NO VERDICT as of 2026-09-23. arity.com/yourprivacychoices/ "
        "embeds no form; it directs the reader to an external webform at "
        "arityoptout.consumerprivacyinfo.com, which was not followed. "
        "That host is the useful finding -- it is a distinct third-party "
        "privacy vendor, not one of the OneTrust/DataGrail/TrustArc "
        "families already seen in this dataset, so it will need its own "
        "handling. Also worth noting for whoever writes the recipe: Arity "
        "frames its activity as 'sharing' for targeted advertising rather "
        "than a sale, and its data is keyed to driving and devices, so "
        "what an opt-out here removes is not obvious."
    ),
    "arizonacourtrecords-us": (
        "Same network mechanism and same open questions as "
        "alabamacourtrecords-us and alaskacourtrecords-us. "
        "arizonacourtrecords.us/optout embeds no form of its own and "
        "delegates to courtrecords.us/do-not-sell-share-my-personal- "
        "information/ -- the shared network form confirmed on the Alabama "
        "pass (First Name *, Last Name *, a State * dropdown, City, and a "
        "Submit button) -- plus an external TrustArc form and "
        "privacy@courtrecords.us for covered-person and expungement "
        "removals. Undecided because the form lives on the network domain "
        "rather than this row's, no element names were captured, the "
        "TrustArc branch was never opened, and it is unsettled whether a "
        "do-not-sell request actually delists a record from the state "
        "site."
    ),
    "arkansascourtrecords-us": (
        "Verified directly 2026-09-23, and it confirms the network "
        "pattern on a fourth site: arkansascourtrecords.us/optout embeds "
        "no form and offers exactly three routes -- 'the form on our Do "
        "Not Sell or Share My Personal Information page' at "
        "courtrecords.us/do-not-sell-share-my-personal-information/, "
        "privacy@courtrecords.us for covered-person removal, expungement "
        "records and appeals, and a 'contact us HERE' link into an "
        "external TrustArc validation form. Same open questions as the "
        "three sibling rows; all four should be closed by one piece of "
        "work on the network form."
    ),
    "arrakis-ai": (
        "NO VERDICT as of 2026-09-23, blocked at the same wall as the "
        "search leg: www.arrakis.ai/optout fails with a TLS internal "
        "error before any HTTP response, so the page -- which by its URL "
        "is plainly meant to be an opt-out surface -- was never rendered. "
        "The dataset carries no opt_out_email for this row either, so "
        "there is no fallback channel recorded. Next pass: a client that "
        "negotiates this host's TLS, then transcribe /optout."
    ),
    "aslmarketing-com": (
        "NO VERDICT as of 2026-09-23, and it is the SAME Deep Sync form "
        "as the accudata-com row -- these two dataset rows share one "
        "surface. Verified directly: privacy.deepsync.com/request/opt-out "
        "serves 'Who is this request for?' (Myself / A deceased "
        "individual / Someone I'm authorized to represent), First name, "
        "Last name, Email address, Phone number, Primary address with a "
        "state dropdown, Additional address 1-5 each with its own state "
        "dropdown, a 'What would you like us to do?' checkbox group, and "
        "a 'Submit request' button. The blocker is the same and is "
        "substantive, not cosmetic: the page states 'For your protection, "
        "we verify identity through dynamically-generated questions "
        "before completing requests', so the flow does not end at Submit "
        "and a recipe stopping there would report a success it never "
        "achieved. Decide accudata-com and this row together."
    ),
    "issgovernance-com": (
        "NO VERDICT as of 2026-09-23, blocked at the same wall as the "
        "search leg: the dataset's URL 301-redirects to www.iss- "
        "stoxx.com/privacy-legal/ccpa/ after the ISS STOXX rebrand, and "
        "that page returned HTTP 403, so nothing was observed. The "
        "dataset's contact, dataprotectionofficer@iss-stoxx.com, already "
        "reflects the new entity and is the one fact here that checks "
        "out. The dataset's URL should be updated to the iss-stoxx.com "
        "path regardless of how the leg resolves."
    ),
    "assurance-com": (
        "NO VERDICT as of 2026-09-23: assurance.com serves an expired TLS "
        "certificate, so the dataset's URL (assurance.com/assurance- "
        "privacy-practices.html#CRPDYourConsumerRightsRightToOptOut) was "
        "never rendered and the 'Right to Opt Out' section it anchors to "
        "was never read. Note the dataset's opt_out_email for this row, "
        "swenson@assurance.com, is an individual's personal address "
        "rather than a privacy alias, which is usually a sign the record "
        "is stale. Next pass: check whether the business is still live "
        "before spending effort on the form."
    ),
    "astoriacompany-com": (
        "NO VERDICT as of 2026-09-23, and the useful finding is that real "
        "forms exist but were never named. astoriacompany.com/privacy- "
        "policy/ embeds no form itself; it points to a 'Your Privacy "
        "Choices' homepage link and to THREE separate dedicated URLs -- a "
        "Privacy Request form, a Do Not Sell/Share form, and an "
        "Authorized Agent Request form -- alongside "
        "privacy@astoriacompany.com and a Fort Worth TX postal address "
        "(note the dataset carries bizdev@astoriacompany.com instead, a "
        "sales alias). Those three URLs were not captured or followed. "
        "Next pass: get the Do Not Sell/Share URL off the policy page and "
        "transcribe it; a recipe here also has to CHOOSE among the three "
        "forms, which is a decision rather than a detail."
    ),
    "attomdata-com": (
        "A REAL and fully transcribed CCPA form, held back for two "
        "reasons. Verified 2026-09-23: the dataset's URL 301-redirects to "
        "ccpa.attomdata.com, which serves Company, First Name*, Last "
        "Name*, Email*, 'First Name of person filling out the form'*, "
        "'Last Name of person filling out the form'*, Phone, an Address "
        "block (Street Address, Address Line 2, City, State, ZIP Code), "
        "Prior Phone Numbers, Prior Last Names and Prior Addresses, a "
        "request-type choice among 'Request for access (give me my "
        "data)', 'Request for deletion (delete my data)' and 'Request for "
        "opt out (do not sell my data)', and a 'Submit' button. The two "
        "blockers: it requires an attestation 'under penalty of perjury "
        "that I am a California resident', which is a claim this tool "
        "must not make on behalf of a non-Californian, and the page "
        "states that access and deletion requests 'must have proper "
        "identity verification', a step that happens after submit and was "
        "never walked. If the opt-out branch turns out to skip "
        "verification, a California resident could use this. Missing as "
        "usual: element names, action/method, a dry run."
    ),
    "audienceacuity-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL, "
        "www.audienceacuity.com/opt-out-of-database, 301-redirects to "
        "optout.audienceacuity.com -- a dedicated opt-out host, which is "
        "a good sign -- and that page rendered nothing but the heading "
        "'Audience Acuity Opt-Out': no fields, no button, no vendor "
        "branding. Another client-rendered form. Next pass needs a JS- "
        "capable browser against optout.audienceacuity.com, and the "
        "dataset's URL should be updated to it."
    ),
    "arkeero-com": (
        "NO VERDICT as of 2026-09-23. The dataset gives no opt_out_url "
        "and declares the channel email-only (dpd@arkeero.com -- 'dpd' "
        "being the Spanish abbreviation for data protection officer, "
        "consistent with Rock Internet, S.L. being a Spanish GDPR- "
        "governed company). arkeero.com's footer does link a Privacy "
        "Policy and a Cookie Policy, but an attempt to read the privacy "
        "policy returned only the site shell and the cookie banner, so "
        "its actual erasure/opt-out channels were never read. It sits "
        "here rather than under NO_OPTOUT_SURFACE precisely because the "
        "one document that would settle it was not retrieved. Next pass: "
        "render arkeero.com's privacy policy and read its rights section; "
        "see EU-NOTES.md on whether a US subject can use a Spanish DPO "
        "channel at all."
    ),
    "audiencepoint-com": (
        "NO VERDICT as of 2026-09-23, and the dataset's URL is simply "
        "dead: audiencepoint.com/cpra/ returns HTTP 404. The homepage's "
        "footer carries only a Privacy Policy (/privacy-policy) and an "
        "End-User License Agreement -- no privacy portal and no 'Do Not "
        "Sell' link surfaced, and the dataset records no opt_out_email "
        "either, so there is currently no known channel at all. It sits "
        "here rather than under NO_OPTOUT_SURFACE because the privacy "
        "policy itself was never read, and that is where a CPRA rights "
        "section would be. Next pass: read audiencepoint.com/privacy- "
        "policy and decide the leg from its rights section."
    ),
    "audiencerate-com": (
        "NO VERDICT as of 2026-09-23, and the dataset's URL is dead: "
        "www.audiencerate.com/optout returns HTTP 404. The live site "
        "carries three policy pages instead -- privacy-policy.html, "
        "cookie-policy.html and data-regulation.html -- none of which was "
        "read, and the dataset's fallback is gl@audiencerate.com, which "
        "looks like an individual's initials rather than a privacy alias. "
        "The company is plainly still trading, so a rights channel should "
        "exist. Next pass: read data-regulation.html first, then the "
        "privacy policy, and correct the dead opt_out_url."
    ),
    "automotivemastermind-com": (
        "NO VERDICT as of 2026-09-23. The dataset's URL is live and is "
        "the right page by name, but it embeds no form: it offers a phone "
        "number (1-800-447-2273) and a footer 'Your Privacy Choices' link "
        "into a OneTrust privacy-management portal. The real mechanism is "
        "therefore the OneTrust webform, which was not followed and did "
        "not render. Next pass: capture the OneTrust webform URL from "
        "that footer link and attack it with a JS-capable browser -- and "
        "note that whoever solves the OneTrust family closes a dozen rows "
        "in this dataset at once."
    ),
    "autoweb-com": (
        "NO VERDICT as of 2026-09-23. www.autoweb.com/privacy embeds no "
        "form; it carries a 'Do Not Sell or Share My Personal "
        "Information' link into a OneTrust privacy portal, which was not "
        "followed and whose fields are unknown. Worth recording that the "
        "privacy page itself does not disclose AutoWeb's data-sale "
        "practices in any detail, so what an opt-out here covers is also "
        "unclear. Next pass: capture the OneTrust webform URL and "
        "transcribe it."
    ),
    "awl-com": (
        "NO VERDICT as of 2026-09-23, and this row's research produced "
        "the most concrete URL of the batch. awl.com/privacy/ states "
        "plainly that 'we may share or sell your personal information "
        "with third parties that may not be insurance-related', naming "
        "names, addresses, phone numbers, email addresses and MEDICAL "
        "information among the categories -- and it gives two channels: a "
        "OneTrust webform for California, Nevada and Virginia residents "
        "at privacyportal- "
        "eu.onetrust.com/webform/031dc37f-2093-4055-9d04-22f83329fe9f/a4fb8ed6-8389-4a17-a3be- "
        "ca808c6b300e, and a general unsubscribe at "
        "dnc.allwebleads.com/Unsubscribe. That second URL is the surface "
        "already recorded under the allwebleads-com row, which reinforces "
        "that these two rows are one company: the unsubscribe is do-not- "
        "CONTACT, and this OneTrust webform is the actual rights channel. "
        "Undecided because the OneTrust form was not rendered. Next pass: "
        "that exact URL, JS-capable browser, and fold allwebleads-com in."
    ),
    "az-direct-com": (
        "NO VERDICT as of 2026-09-23. The dataset gives no opt_out_url "
        "and declares the channel email-only (datenschutz@az-direct.com), "
        "and no request form is visible on www.az-direct.com -- but the "
        "footer links two German data-protection pages, /site/datenschutz "
        "and /site/datenschutz-website, neither of which was read, and a "
        "German broker of this size is obliged to publish an "
        "Auskunft/Widerspruch route. It sits here rather than under "
        "NO_OPTOUT_SURFACE precisely because the two pages that would "
        "settle it were not opened. Note the site also publishes country- "
        "specific DPO addresses (datenschutz@bertelsmann.at for Austria, "
        "datenschutzbeauftragter@az-direct.ch for Switzerland). Next "
        "pass: read /site/datenschutz, and see EU-NOTES.md on whether a "
        "US subject has standing here at all."
    ),
    "azira-com": (
        "NO VERDICT as of 2026-09-23, and the two Azira rows in this "
        "dataset point at the two halves of one answer. "
        "www.azira.com/privacy-policy/ embeds no form; it offers a 'Do "
        "Not Sell or Share My Personal Information' homepage button, "
        "privacy@azira.com, and a direct 'Opt-Out & Data Rights Form' on "
        "TrustArc at submit-irm.trustarc.com -- the same host the "
        "trustarc-com row points at, and the same host the "
        "CourtRecords.us network delegates to. The TrustArc widget was "
        "not rendered, so no field is known. One substantive caution for "
        "the recipe-writer: Azira states it holds only MAIDs and "
        "location, no direct identifiers, so an opt-out submitted with a "
        "name may have nothing to match against. Next pass: render the "
        "TrustArc form once and close azira-com, trustarc-com and the "
        "four CourtRecords.us rows together."
    ),
    "trustarc-com": (
        "NO VERDICT as of 2026-09-23, and see the search leg: this row's "
        "domain is TrustArc, the privacy vendor, not Azira. The dataset's "
        "URL is a specific TrustArc webform instance (submit- "
        "irm.trustarc.com/services/validation/0a80503b-1d56-4d50-a898-4377a0227dab) "
        "which is presumably Azira's, and it was not rendered -- "
        "TrustArc's forms are JS-rendered like every other consent portal "
        "in this dataset. The dataset's opt_out_email for the row, "
        "sonal@azira.com, is an individual's personal address. Next pass: "
        "render that webform id; if it is Azira's, fold this row into "
        "azira-com rather than maintaining it separately."
    ),
    "biscience-com": (
        "NO VERDICT as of 2026-09-23, and the reason is an unusual one "
        "worth recording. www.biscience.com/ccpa/ embeds no form and "
        "offers three routes, none of which is a web form: a cookie "
        "manager in the page footer, a per-app 'opt out from sharing Raw "
        "Data' toggle inside each of its and its affiliates' products, "
        "and -- the only record-level channel -- a Data Subject Request "
        "Form that must be DOWNLOADED and then emailed to "
        "privacy@biscience.com. A downloaded document emailed to a "
        "mailbox is the same shape as adform-com's identity-verification "
        "PDF and would normally be a no-surface call; it is held here "
        "only because the form itself was never opened, so whether it "
        "demands an ID or a device identifier is unknown. Next pass: "
        "download that form and decide. Note the dataset records no "
        "opt_out_email for this row even though the page names one."
    ),
}


# A DIFFERENT finding from NO_OPTOUT_SURFACE, kept separate on purpose.
#
# These brokers DO host a real, self-service opt-out form. We simply cannot
# reach it: an anti-bot wall stands in front of it on every visit, and this
# tool's standing rule is that a bot check is where automation STOPS -- it is
# never solved, worked around, or waited out. Lumping these in with "there is
# no surface" would be a false statement about the broker and would also hide
# the one thing that distinguishes them: if the wall ever comes down, a recipe
# becomes possible here, whereas a mailbox-only broker will never become
# automatable no matter what changes.
#
# Notes, not behaviour: nothing reads this at runtime. A broker listed here is
# simply absent from RECIPES, which is what actually stops a submission.
OPTOUT_BLOCKED = {
    "nationalpublicdata-com": (
        "Verified 2026-09-23: /optout.html serves a Cloudflare Turnstile "
        "challenge page ('Performing security verification') and nothing "
        "else -- the form behind it never renders, so there is nothing to "
        "transcribe. The contrast is inside one broker again: its SEARCH "
        "side is not challenged at all and ships as a working recipe in "
        "search_forms."
    ),
    "privatenumberchecker-com": (
        "Verified 2026-09-23: /removalrequest/ is behind a Cloudflare "
        "Turnstile challenge on every visit, while the rest of the site "
        "(including the reverse-phone search this pilot DOES automate) "
        "serves normally. Worth noting as a pattern rather than a "
        "one-off: three brokers in this batch wall their removal page "
        "specifically, which is a choice about who is allowed to leave."
    ),
    "voterrecords-com": (
        "Verified 2026-09-23: the whole site, homepage included, is behind "
        "a Cloudflare Turnstile challenge, so the opt-out page cannot be "
        "reached to be read. Recorded as blocked rather than absent "
        "because nothing here says the surface does not exist -- only that "
        "an automated visitor never gets to see it. Same for its search "
        "leg; see search_forms.SEARCH_BLOCKED."
    ),
    "uspeoplesearch-com": (
        "Verified 2026-09-23: same as voterrecords-com -- a Cloudflare "
        "Turnstile challenge stands in front of the whole site, including "
        "the /purge-my-data/ page the dataset records as its opt-out."
    ),
    "freepeoplesearch-com": (
        "Verified 2026-09-23: Cloudflare's terminal block page ('Sorry, "
        "you have been blocked'), not a solvable challenge, on the domain "
        "as a whole. Nothing of this broker's own content renders."
    ),
    "cyberbackgroundchecks-com": (
        "Verified 2026-09-22 across repeated visits, including with the "
        "production browser user-agent: the opt-out page is served behind a "
        "Cloudflare MANAGED CHALLENGE every time ('Verifying you are human' "
        "/ 'Just a moment...'), and the form itself never renders, so there "
        "is nothing to transcribe. This is a wall in front of a real "
        "surface, not an absent surface -- and per this tool's policy a bot "
        "check is a full stop, never something to solve. Note the contrast "
        "with the SEARCH leg, which is not challenged at all and ships as a "
        "working recipe in search_forms."
    ),
    "stirista-com": (
        "Same wall as search_forms.SEARCH_BLOCKED. Verified 2026-09-23: "
        "the dataset's URL, www.stirista.com/opt-out-preferences/, is "
        "plainly the right page by name and serves only the interstitial "
        "'Please wait while your request is being verified...' -- no "
        "fields, no buttons, nothing. Note the dataset's opt_out_email "
        "for this row (robert@lighthouselist.com) is a personal address "
        "at a THIRD company, which is worth re-checking whenever the wall "
        "lifts."
    ),
    "accurateappend-com": (
        "CORRECTION to an earlier entry, and the reason this pass exists. "
        "A WebFetch-based look on 2026-09-23 transcribed this form's "
        "labels and reported no CAPTCHA; rendering the same page in a "
        "real browser the same day shows that was wrong. "
        "clients.accurateappend.com/Public/OptOut is form #optOutForm, "
        "POST to /Public/OptOut, and it carries a Cloudflare Turnstile "
        "widget -- the rendered DOM contains an input named cf-turnstile- "
        "response with id #cf-chl-widget-4embv_response (the widget id "
        "suffix is per-load). The form itself is otherwise complete and "
        "was transcribed while it was open: "
        "Name.FirstName/#Name_FirstName, Name.LastName/#Name_LastName, "
        "three hidden OtherNames[0..2].FirstName|LastName pairs revealed "
        "by an 'ADD OTHER NAMES' control, Address/#Address, City/#City, "
        "State/#State (a 73-option select), PostalCode/#PostalCode, "
        "Phone.Number/#Phone_Number, Email.Address/#Email_Address, "
        "repeatable OtherPhones[0..2].Number and "
        "OtherEmails[0..2].Address, submit #submitBtn reading 'SUBMIT'. "
        "All of that is recorded so nobody re-does it: if the Turnstile "
        "ever comes down this becomes a recipe in an afternoon. Until "
        "then the standing rule applies -- a bot check is where "
        "automation stops."
    ),
    "atdata-com": (
        "CORRECTION to an earlier entry. The 2026-09-23 WebFetch pass "
        "said 'No CAPTCHA was seen'; a browser render the same day shows "
        "a reCAPTCHA on the form. instantdata.atdata.com/optout is form "
        "#new_opt_out, POST to /optout#opt_form, Rails-shaped with a "
        "hidden authenticity_token that is minted per page load, and the "
        "rendered DOM carries the usual hidden <textarea "
        "name='g-recaptcha-response'>. Transcribed while open, so the "
        "work is not lost: opt_out[email]/#opt_out_email, "
        "opt_out[first_name]/#opt_out_first_name, "
        "opt_out[last_name]/#opt_out_last_name, "
        "opt_out[street]/#opt_out_street, opt_out[city]/#opt_out_city, "
        "opt_out[state]/#opt_out_state, "
        "opt_out[country_code]/#opt_out_country_code (a 251-option "
        "select), opt_out[zip]/#opt_out_zip, and a submit named 'commit'. "
        "Two notes for whoever revisits: the per-load authenticity_token "
        "means this can only ever be driven in a browser, never as a "
        "canned POST, and AtData keys on the EMAIL address, so what one "
        "submission actually reaches is worth confirming before trusting "
        "a success."
    ),
    "4legalleads-com": (
        "CORRECTION to an earlier entry that called this 'a real form... "
        "a recipe-writer with a browser should be able to finish this one "
        "quickly'. A browser did, on 2026-09-23, and found a wall. "
        "www.4legalleads.com/removal POSTs to "
        "/fsg?pageId=5364fa9a-a3b7-4758-b8e3-d5d78a29fd93&variant=c and "
        "loads challenges.cloudflare.com/turnstile/v0/api.js with "
        "render=explicit; the rendered DOM contains cf-turnstile-response "
        "(#cf-chl-widget-rjq13_response, suffix per-load). It also "
        "carries ActiveProspect TrustedForm consent-certificate fields "
        "(trustedform_cert_url, xxTrustedFormToken, "
        "xxTrustedFormPingUrl), which are lead-industry session receipts "
        "rather than a bot check, but are worth knowing about. The form "
        "is fully transcribed: first_name/#first_name, "
        "last_name/#last_name, subscribers_phone_number, "
        "subscribers_email_address, a REQUIRED radio pair (not the "
        "dropdown the earlier note described) named "
        "you_are_requesting_removal_and_opt_out_from_our_system_because, "
        "an optional textarea comments_not_required, and submit button "
        "#lp-pom-button-934 'Submit Your Request for Removal'."
    ),
    "bookyourdata-com": (
        "CORRECTION to an earlier entry. Verified by browser render "
        "2026-09-23: optout.bookyourdata.com is form #dns-form and it "
        "loads challenges.cloudflare.com/turnstile/v0/api.js, with a cf- "
        "turnstile-response input in the rendered DOM -- so the bot check "
        "the label-only pass could not see is there. Everything else was "
        "captured: email/#email (type=email, required), name/#name, "
        "state/#state, a checkbox pair both named 'rt', a bare text input "
        "named 'website' with no label at all (almost certainly a "
        "honeypot, and it would belong in forbidden_selectors if this "
        "ever became a recipe), and submit #submit 'Submit my request'. "
        "Also confirmed: the dataset's opt_out_url for this row "
        "(www.bookyourdata.com/ccpa-ready) is NOT the form -- it only "
        "points at optout.bookyourdata.com -- and should be corrected."
    ),
    "abovedata-io": (
        "A REAL, native, fully transcribed form that is nonetheless "
        "blocked, verified by browser render 2026-09-23. "
        "www.abovedata.io/opt-out is form #dsr-form and the page loads "
        "google.com/recaptcha/enterprise.js with a render= site key -- "
        "reCAPTCHA Enterprise in its invisible, score-based mode, so "
        "there is no visible widget but every submission is scored. Filed "
        "here rather than in RECIPES because this module's standing rule "
        "is that a bot check of any kind is where automation stops; the "
        "honest caveat is that an invisible v3-style check is a weaker "
        "wall than a Turnstile interstitial, and if the project ever "
        "decides score-based checks are acceptable this row is ready to "
        "ship. Transcribed: a honeypot FIRST -- input name=_gotcha "
        "class=hp, computed position:absolute left:-9999px opacity:0, "
        "which must go in forbidden_selectors -- then firstName/#dsr- "
        "first, lastName/#dsr-last, company/#dsr-company, email/#dsr- "
        "email, country/#dsr-country, four checkboxes all named "
        "'requests' with values 'Opt out of targeted advertising', "
        "'Request access to information about me', 'Request deletion of "
        "information about me', 'Correction of information about me', and "
        "button.dsr-submit 'Submit Request'. Correct the dataset URL to "
        "/opt-out."
    ),
    "remodeling-com": (
        "A REAL, fully transcribed WPForms form, blocked for the same "
        "reason as abovedata-io, verified by browser render 2026-09-23. "
        "remodeling.com/do-not-sell/ is form #wpforms-form-42452, POST to "
        "the same URL, and the page loads google.com/recaptcha/api.js "
        "with a render= site key (invisible reCAPTCHA v3); the form "
        "carries the matching hidden wpforms[recaptcha] field alongside "
        "wpforms[time_token], a WPForms anti-replay token minted per page "
        "load. Transcribed: "
        "wpforms[fields][1][first]/#wpforms-42452-field_1 and "
        "[1][last]/#wpforms-42452-field_1-last (Name), [2] Emails, [3] "
        "Phone Numbers, [4] Street Address (optional), [5] a SELECT whose "
        "only option is 'California', [6] Zip Code, and checkbox pair "
        "[7][] with values 'Do Not Sell My Personal Information' and "
        "'Delete My Personal Information'; submit is button#wpforms- "
        "submit-42452. The California-only select is the substantive "
        "finding: this is not a nationwide surface, and a non-CA subject "
        "has nothing to submit here at all. Same corporate family as "
        "33mileradius-com."
    ),
    "cybba-com": (
        "Verified by browser render 2026-09-23. The dataset's OneTrust "
        "webform (cybba- "
        "requests.my.onetrust.com/webform/a4a1351e-.../81d37a05-...) "
        "renders fine and is a genuine rights form, but it is gated by a "
        "BotDetect image CAPTCHA -- a visible 'Captcha' text box "
        "(#captchaCode) backed by hidden BDC_VCID_angularBasicCaptcha / "
        "BDC_Hs_angularBasicCaptcha / BDC_SP_angularBasicCaptcha fields "
        "-- which is an OCR puzzle and squarely where this tool stops. "
        "Transcribed anyway: OneTrust's element ids are stable across "
        "tenants, so countryDSARElement, stateDSARElement, "
        "firstNameDSARElement, lastNameDSARElement, emailDSARElement, "
        "formField78DSARElement, requestDetailsDSARElement and #dsar- "
        "webform-submit-button are all present, alongside a file picker "
        "and a required request-type checkbox group. SECOND AND MORE "
        "IMPORTANT FINDING, which generalises beyond this row: this DSAR "
        "form explicitly EXCLUDES the opt-out of sale. Its own text reads "
        "'To opt out of the sale, sharing, or use of personal information "
        "for targeted advertising... visit Your Privacy Choices', so the "
        "surface the dataset records for this broker is the wrong one for "
        "a do-not-sell request. The Your Privacy Choices widget was not "
        "opened; that is the next step here."
    ),
}


# A THIRD kind of no, and the one most likely to be mistaken for laziness.
#
# These brokers host a real, reachable, unwalled self-service removal page.
# What they ask for is outside what this codebase may or can do:
#
#   * a government ID or driver's licence upload, which this tool will not
#     send anywhere on anyone's behalf -- a DSAR is worth less than a scan of
#     Penn's passport sitting in a broker's ticket queue; or
#   * a per-RESULT removal flow: search yourself, pick your record out of the
#     list, then confirm. ``FormRecipe`` describes a fixed form, and picking
#     the right stranger out of a result list is a judgement call, not a
#     recipe. Getting it wrong means asking a broker to delete somebody else.
#   * a multi-PAGE, modal-driven wizard, where the form is not at a URL at
#     all: it is reached by walking several session-gated pages, and each
#     datum is added through its own dialog. ``FormRecipe`` carries one url
#     and one flat list of fields, so there is nothing to point it at.
#
# All three are decided "no"s rather than open questions, which is why they are
# not in a pending list -- but they are decided for a REASON THAT COULD
# CHANGE (a broker adds a plain form; this codebase grows a result-picking
# model with a human in the loop), and that is why they are not filed under
# "no surface" either.
#
# Notes, not behaviour: nothing reads this at runtime.
OPTOUT_OUT_OF_SCOPE = {
    "spokeo-com": (
        "Verified 2026-09-23. spokeo.com/optout is a real, reachable, "
        "unwalled page with a two-field form (POST to /optout; an "
        "<input type=url name=url placeholder='Enter URL here'> and an "
        "<input type=email name=email>) and no captcha of any kind -- and "
        "it still cannot be a FormRecipe, because the URL it wants is the "
        "URL of ONE listing. The page states the constraint itself: 'you "
        "may have multiple listings on Spokeo. Each one is identified by a "
        "unique URL and must be opted out individually', and its worked "
        "example is 'https://www.spokeo.com/Smith-Sample/Houston/TX/"
        "p12345678'. So a submission has to be preceded by a search and by "
        "picking which of the 16,204 people named Michael Thompson is the "
        "right one -- the per-RESULT shape this bucket exists for, and "
        "exactly the judgement call that risks asking a broker to delete a "
        "stranger. Nothing was submitted. Its SEARCH leg is a shipped, "
        "working recipe; see search_forms.SPOKEO. Second channel for a "
        "human: the page also offers privacy@spokeo.com and a link to "
        "spokeo.com/privacy/control/all-categories, and it warns that a "
        "confirmation email must be clicked before a request takes effect."
    ),
    "whitepages-com": (
        "Verified 2026-09-23. /suppression-requests is a FIVE-step wizard "
        "whose first step is, in the page's own words, 'STEP 1 OF 5 ... You "
        "can opt out and have your listing removed from the Whitepages "
        "website. Copy and paste the URL of your profile.' The only field "
        "on it is a single textbox with that sentence as its placeholder, "
        "plus a Next button. Same shape as spokeo-com, and out of scope for "
        "the same two reasons: FormRecipe carries one url and one flat list "
        "of fields (not five session-gated pages), and the value it would "
        "have to supply is the URL of ONE listing chosen out of a result "
        "list -- a judgement call about which stranger is Penn. Nothing was "
        "submitted; step 2 was never reached. Its SEARCH leg is a shipped, "
        "working recipe; see search_forms.WHITEPAGES."
    ),
    "mylife-com": (
        "Verified 2026-09-23, and it is the closest call in this bucket so "
        "far -- a real, reachable, unwalled, SINGLE-PAGE form with one "
        "Submit button, which is exactly the shape FormRecipe was built "
        "for. First, a correction to the dataset: its recorded opt-out URL "
        "/ccpa/index.pubview is a hard 404 ('The requested URL "
        "/ccpa/index.pubview was not found on this server'). The live "
        "removal page is /privacyrequest, reached from the privacy "
        "policy's 'Do Not Sell My Personal Information' link, and titled "
        "'DELETION AND OPT OUT OF SALE OF PERSONAL INFORMATION'.\n"
        "\n"
        "Two things on it put it out of scope, and neither is the "
        "reCAPTCHA v2 at the bottom (a bot check alone would only mean "
        "'needs you', which this tool already handles):\n"
        "\n"
        "  * 'Birth Year (YYYY)' is REQUIRED (red asterisk), captioned "
        "    'Your birth year helps us locate your correct profile.' There "
        "    is no birth-year source in resolve_fields -- the supported "
        "    sources are name/email/phone/address/city/state/zip/country -- "
        "    and date of birth is named in FormRecipe.forbidden_selectors' "
        "    own docstring as a datum this codebase declines to volunteer. "
        "    A recipe could not fill it, and resolve_fields would return it "
        "    under 'missing', so optout_submit would refuse the form "
        "    anyway. Adding a DOB field to the identity model to satisfy "
        "    one broker is a decision for a human, not a recipe.\n"
        "  * 'Email Validator' is required and is not a plain email box: it "
        "    is an input beside a 'Verify Email' button, i.e. an "
        "    out-of-band confirmation step before the form can be "
        "    completed. It was not pressed -- doing so would send mail to "
        "    whatever address was typed.\n"
        "\n"
        "For the record, the rest of the form is ordinary and fillable: a "
        "required State <select>, First/Last name, City and Postal/Zip, "
        "plus optional Middle Initial, Maiden/Alias name and a profile-URL "
        "box (optional here, unlike Spokeo's, so the per-listing problem "
        "does NOT apply to this broker). Nothing was filled and nothing "
        "was submitted. Its SEARCH leg is a shipped, working recipe; see "
        "search_forms.MYLIFE."
    ),
    "equifax-com": (
        "Verified 2026-09-23. First, the dataset is stale: its opt-out URL "
        "(equifax.com/personal/education/identity/opt-out-prescreen/) is a "
        "hard 404 -- the page renders Equifax's own '404 Error / Oops! We "
        "can't seem to find the page you're looking for.' The live "
        "surface, reached from that 404 page's own 'Your Privacy Choices' "
        "footer link, is https://myprivacy.equifax.com/opt-in-opt-out/"
        "personal-info, titled 'myPrivacy'.\n"
        "\n"
        "It is a real, unwalled, self-service form -- and out of scope on "
        "shape, like whitepages-com. The page's own stepper reads '1 Info "
        "/ 2 Verify': step 1 takes First name, Last name, Address line "
        "1/2, City, State and Zip, and step 2 is an identity "
        "verification stage that was NOT entered. FormRecipe carries one "
        "url and one flat field list, so a two-stage session-gated wizard "
        "is not something it can express, and for a CRA the stage it "
        "cannot express is precisely the one that proves who you are.\n"
        "\n"
        "Worth recording for whoever revisits it: the SSN/ITIN and date-of-"
        "birth boxes on step 1 are genuinely OPTIONAL, and the page says "
        "so in as many words -- 'You do not have to provide your date of "
        "birth, SSN or ITIN, email, or mobile number, but this "
        "information makes it easier for us to locate your information "
        "and complete your request.' If this ever does become a recipe, "
        "those two belong in forbidden_selectors for the same reason "
        "Credit.com's do: an optional SSN box is not an invitation. "
        "Nothing was filled and nothing was submitted. Its SEARCH leg is "
        "recorded under search_forms.NO_SEARCH_SURFACE."
    ),
    "lexisnexis-com": (
        "Verified 2026-09-23, and it is the clearest SSN case in this "
        "file. optout.lexisnexis.com redirects to "
        "consumer.risk.lexisnexis.com/opt, whose 'Request Opt-Out/Opt-In' "
        "link leads to /optrequest -- a real, reachable, single-page form "
        "POSTing to /optSubmit, with no HTML `required` attributes at all "
        "(so the browser will not tell you what is mandatory; the page's "
        "prose does).\n"
        "\n"
        "What the page says, verbatim: 'You may be required to submit "
        "proof of your identity for certain requests to be processed. Such "
        "information may include your First Name, Last Name, Street "
        "Address, City, Zip, and Date of Birth, and either your Social "
        "Security Number or your Driver's License Number and State.' The "
        "form matches that prose exactly -- alongside FirstName, "
        "MiddleName, LastName, Residence_StreetAddress1/City/State/Zip5 "
        "and Phone/Email it carries SSN, DOB, DLNumber and Issuer, and a "
        "duplicate auth_* set of all of them for an authorized agent.\n"
        "\n"
        "This codebase will not put a Social Security number, a date of "
        "birth or a driver's licence number into a third party's form on "
        "anybody's behalf -- the same line FormRecipe.forbidden_selectors "
        "draws around Credit.com's optional SSN/DOB boxes, except that "
        "here they are not decorative: they are how the request gets "
        "honoured. A recipe that omitted them would be filing a request "
        "the broker has told us in advance it may refuse, which is worse "
        "than not filing (a spent request that looks filed). This one "
        "belongs to a human with the form open. There is also a bot check "
        "on the page, but it is not the reason. Nothing was filled and "
        "nothing was submitted. Its SEARCH leg is recorded under "
        "search_forms.NO_SEARCH_SURFACE."
    ),
    "peoplewhiz-com": (
        "Verified 2026-09-23. /remove-my-info is a search box, not a form: "
        "its own instructions say to search your name, select your record, "
        "confirm, and then -- in the page's own words -- 'IMPORTANT: ID "
        "required. You'll need to provide proof of your identity before we "
        "can complete your request.' Both halves are out of scope: this "
        "tool does not choose which stranger's record is Penn's, and it "
        "does not upload identity documents."
    ),
    "privaterecords-net": (
        "Verified 2026-09-23. The 'Do Not Sell Or Share My Personal Info' "
        "link goes to /api/helper/optOutLight/search, whose own heading "
        "states the shape: 'Enter the name and state in the form below to "
        "locate the record you would like to remove'. The fields on it "
        "(fname, lname, city, state, zip, phone, email) are SEARCH fields "
        "for finding somebody's record, not a removal request -- so a "
        "FormRecipe pointed at them would be filling in a lookup and "
        "calling it a filed opt-out. There is a second problem on top of "
        "the shape: submitting that search POSTs, answers 200, and "
        "re-renders the same empty form with no result list, no 'nothing "
        "found' copy and no error, which is the same never-settles "
        "behaviour its SEARCH leg shows (see "
        "search_forms.SEARCH_UNDECIDED)."
    ),
    "peoplesearcher-com": (
        "Verified 2026-09-23: the same page, at the same path, on the same "
        "codebase as privaterecords-net -- /api/helper/optOutLight/search, "
        "same heading, same seven search fields, and the same 200-with-no-"
        "result-list when submitted. Recorded separately because each "
        "dataset broker gets its own verdict, and the finding is "
        "identical."
    ),
    "openpeoplesearch-com": (
        "Verified 2026-09-23, and this closes the question batch 1 left "
        "open: the START button on /Consumer IS locatable -- it is an "
        "anchor, a.btn-bigger, not a button, which is why a button-shaped "
        "search for it failed twice. The blocker is what it starts. The "
        "removal form lives at /Consumer/OptOut behind a three-page "
        "session-gated wizard (START, then 'what state do you live in', "
        "then a state-privacy-law notice with GOT IT, CONTINUE); "
        "navigating straight to /Consumer/OptOut or /Consumer/Privacy in a "
        "fresh session redirects back to /Consumer. And the form itself is "
        "not a form: it is four ADD buttons (ADD NAME, ADD ADDRESS, ADD "
        "PHONE NUMBER, ADD EMAIL ADDRESS), each opening a modal with its "
        "own Add/Cancel, and opening one blocks the others. FormRecipe "
        "carries one url and one flat field list, so there is nothing here "
        "for it to point at. Recorded as a shape problem rather than a "
        "wall: the page is reachable, unauthenticated and, checked "
        "explicitly, carries no captcha of any kind."
    ),
    "searchbug-com": (
        "Verified 2026-09-23. Searchbug's CCPA page has no removal form of "
        "its own; it instructs consumers to use the generic Contact Us "
        "form with 'CCPA Request' as the reason and, in its own numbered "
        "steps, to \"Click 'Attach files' to upload an image of your "
        "Driver's License, or Government ID and/or legal document "
        "(Required)\". A free-text support ticket carrying a scan of "
        "Penn's ID is not something this codebase will submit unattended."
    ),
    "courtrec-com": (
        "Verified 2026-09-23. dashboard.courtrec.com/opt-out is a "
        "per-result wizard: search by name, address or phone, pick your "
        "record from the results, then type 'I AGREE' into a confirmation "
        "box. There is no fixed form to fill -- the fields on the page are "
        "SEARCH fields -- so a FormRecipe cannot express it, and the "
        "record-picking step is exactly the judgement call this tool must "
        "not make on its own."
    ),
    "publicrecords-us": (
        "Verified 2026-09-23: dashboard.publicrecords.us/opt-out is the "
        "same per-result wizard as courtrec.com's, down to the 'Type I "
        "AGREE to confirm' box. Recorded separately rather than by "
        "reference because each dataset broker gets its own verdict."
    ),
    "publicrecords-info": (
        "Verified 2026-09-23: dashboard.publicrecords.info/opt-out is the "
        "same per-result wizard again. Same reason for a separate entry."
    ),
    "beenverified-com": (
        "Verified 2026-09-23. The dataset's opt-out URL "
        "(/app/optout/search) 302s to /svc/optout/search/optouts, which "
        "renders a SEARCH box, not a removal form: its only inputs are "
        "fname, ln and state, and the page's own instructions say what the "
        "rest of the flow is -- 'Just search our database using the form "
        "above and select the record you would like to opt-out. We will "
        "then send you a verification email. Next you simply need to "
        "confirm the request by clicking on the link in the verification "
        "email.' That is the per-RESULT shape this bucket exists for: "
        "FormRecipe describes a fixed form, and choosing which stranger's "
        "record is Penn's is a judgement call whose failure mode is asking "
        "a broker to delete somebody else. The email round-trip is a "
        "second, independent blocker -- this codebase has no mailbox to "
        "click the confirmation link from. Worth recording as well: the "
        "first request to this path answers HTTP 403 with 'Just a moment...' "
        "and the page that eventually renders still carries two "
        "cf-turnstile-response inputs, so a Cloudflare challenge sits in "
        "front of it -- but the per-result shape is the deciding fact, "
        "which is why this is out-of-scope rather than OPTOUT_BLOCKED."
    ),
    "intelius-com": (
        "Verified 2026-09-23. intelius.com/privacy-center/ 302s to "
        "app.intelius.com/privacy-center/, which is real and reachable and "
        "carries exactly one plain form -- firstName, middleInitial, "
        "lastName, month/day/year of birth, city, state, email, Submit -- "
        "but that form is NOT the listing removal. Its own section heading "
        "is 'Right to Opt Out' and its copy says it exists to 'prevent "
        "your name from appearing as a possible relative or associate in "
        "other persons' reports'. The actual removal is the 'Suppress your "
        "Background Report' section, whose only control is a 'Manage My "
        "Suppression Rules' button, and pressing it opens a NEW TAB at "
        "https://suppression.peopleconnect.us/login -- a different domain "
        "(PeopleConnect, Intelius's parent) serving 'Suppression Center / "
        "Free - Start Here / Step 1 / Enter your email address ... you "
        "will receive a verification email with a link to proceed', with a "
        "Terms-of-Use consent checkbox. That is the multi-PAGE, "
        "session-gated wizard shape this bucket names: the form is not at "
        "a URL, the flow is gated on clicking a link in a mailbox this "
        "codebase does not have, and Intelius's own page says the later "
        "steps require 'your full name, date of birth, and a phone or "
        "email address that you can verify'. Nothing was submitted. Note "
        "for whoever revisits: search_forms.NO_SEARCH_SURFACE already "
        "records peoplefinder-com as pointing its opt-out here, so this "
        "single wizard is the terminus for more than one dataset broker."
    ),
    "fastpeoplesearch-com": (
        "Verified 2026-09-23. /removal 302s to /optout, which is reachable "
        "and renders a real form -- but that form is only STEP ONE of four, "
        "and the page numbers the steps itself: 'Enter your email address "
        "and name and complete the captcha below. We will send a link to "
        "your email address that will take you to the opt-out form.' -> "
        "'Click the link sent to your email ... If you wait more than 24 "
        "hours to click this link you will need to request a new one.' -> "
        "'Enter your information on the form.' -> a confirmation page. So "
        "the actual removal form is not at a URL at all: it is behind a "
        "one-time token mailed to an address this codebase cannot read, "
        "which is the multi-page/session-gated shape this bucket names. The "
        "step-one form itself (action=/optout-start-submitted, method=post, "
        "fields am, agentFirstname, agentLastname, agentEmail, firstname, "
        "middlename, lastname, email, a 'legal' checkbox) also carries a "
        "cf-turnstile-response input, i.e. a Cloudflare Turnstile captcha -- "
        "but the captcha is not the deciding fact here, since this tool "
        "already screenshots-and-stops on captchas for five shipped "
        "recipes. The mailbox is. Nothing was submitted. Its SEARCH leg is "
        "a shipped, working recipe."
    ),
    "truthfinder-com": (
        "Verified 2026-09-23: the same PeopleConnect wizard already "
        "recorded for intelius-com, served under TruthFinder's own "
        "branding. truthfinder.com/privacy-center/ 302s to "
        "app.truthfinder.com/privacy-center/, whose single plain form "
        "(firstName, middleInitial, lastName, month/day/year, city, state, "
        "email) is again only the relative/associate suppression under "
        "'Right to Opt Out', not the listing removal. The listing removal "
        "is 'Suppress your Background Report', its only control is 'Manage "
        "My Suppression Rules', and pressing it opens a new tab at "
        "https://suppression.peopleconnect.us/login -- the identical "
        "email-token wizard on the identical third-party domain. "
        "TruthFinder's own copy carries the identity bar too: 'You will "
        "need to confirm your identity by providing your full name, date "
        "of birth, and a phone or email address that you can verify.' "
        "Recorded separately rather than as 'see intelius-com' because "
        "each dataset broker gets its own verdict, and because the two "
        "pages were opened separately rather than assumed identical from "
        "a shared parent company. Nothing was submitted."
    ),
    "apollointeractive-com": (
        "A real, reachable, unwalled form -- and it asks for a government "
        "ID, which is the line this codebase does not cross. Verified "
        "2026-09-23: www.apollointeractive.com/data-rights.php serves a "
        "full state-privacy request form -- a 'Select State' dropdown, "
        "First Name, Last Name, Address, City, State, Zip, Phone, Email, "
        "a request-category choice among 'Opt-Out of Sale and Use', 'Opt- "
        "Out of Use of Sensitive Personal Information', 'Right to "
        "Delete', 'Right to Know' and 'Right to Correct', a correction "
        "free-text box, an agent-authorisation Yes/No with its own "
        "'Upload proof of authorization', a residency attestation, and a "
        "'Submit' button. The blocker is the field 'Upload a valid "
        "government issued photo ID': this tool will not send a scan of "
        "Penn's ID into a broker's ticket queue, which is the same rule "
        "recorded in this dict's header and applied to adform-com's "
        "mailed identity-verification PDF. If Apollo Interactive ever "
        "makes that upload optional, this becomes a recipe candidate."
    ),
    "demandbase-com": (
        "Verified by browser render 2026-09-23, including clicking into "
        "the second step. demandbase.com/privacy- "
        "center.html?ketch_preferences_tab=rightsTab is a Ketch consent- "
        "management widget, and the rights flow is a MODAL WIZARD: step "
        "one is a 'Select a Request Type' screen with four buttons "
        "(Delete your data / Withdraw consent / Access your data / "
        "Correct your data) and no fields at all; only after clicking one "
        "does the form appear, in the same modal, with a back-navigation "
        "control and no URL change. That is the multi-page-modal shape "
        "this bucket exists for -- FormRecipe carries one url and one "
        "flat list of fields, not a session-gated sequence of panels. The "
        "step-two form was transcribed anyway: text-field-firstName, "
        "text-field-lastName, text-field-email, select-field-country, "
        "text-field-stateRegion, select-field-typeCode ('I am a (an)'), "
        "text-field-typeRelationshipDetails, a Submit button, and a "
        "hidden g-recaptcha-response textarea (so there is an invisible "
        "reCAPTCHA here too, an independent reason this could not ship). "
        "Worth recording for the pilot's own purposes: NONE of the four "
        "request types is an opt-out of sale or sharing. Demandbase's own "
        "preamble says it is a B2B company, and the nearest thing on "
        "offer is 'Withdraw consent'."
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
