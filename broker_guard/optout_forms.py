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

# CareerBuilder/Monster's shared privacy-request page. Unusual enough to
# deserve its own name: there is no ``<form>`` element at all. Five controls
# addressed only by id are read by an inline ``submitRequest()`` which POSTs
# JSON to a per-brand API base (``/privacy/api/requests`` on careerbuilder.com,
# ``/resume/privacy/api/requests`` on monster.com) and reports success by
# opening a modal rather than by navigating.
FLAVOR_CAREERBUILDER_JSON_DSR = "careerbuilder_formless_json_privacy_request"

# Convex's privacy-request page, built as a HubSpot embedded form. Named
# separately because of the trap it carries: the ``<form>`` element's id is
# minted per page load (``hsform-53822860`` one visit, ``hsform-41419905``
# the next), so every selector must key on the field ``name`` and the form
# must be reached through ``form:has(...)`` rather than by its id.
FLAVOR_CONVEX_HUBSPOT = "convex_hubspot_privacy_request_form"

# Porch Group Media's individual opt-out, on its own subdomain. Named for the
# defect that dictates how it must be addressed: several controls SHARE an id
# (``UserData_LastName`` is on last_name, phone AND sign_date), so ids are
# unusable here and every selector keys on the ``name`` attribute.
FLAVOR_PGM_OPTOUT = "porchgroupmedia_individual_optout"

# Enigma's do-not-sell form is the shortest honest surface found so far: four
# boxes, three of them required, one submit, and nothing else on the page.
FLAVOR_ENIGMA_HUBSPOT = "enigma_do_not_sell_hubspot_form"

FLAVOR_EYEOTA_DSR = "eyeota_data_subject_request_hubspot"
FLAVOR_FINDEM_WEBFLOW = "findem_do_not_sell_webflow_form"


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


CAREERBUILDER = FormRecipe(
    broker_id="careerbuilder-com",
    broker_name="CareerBuilder",
    url="https://www.careerbuilder.com/privacy/",
    flavor=FLAVOR_CAREERBUILDER_JSON_DSR,
    fields=(
        Field(selector="#name", source="full_name", label="Full Name"),
        Field(selector="#email", source="email", label="Email Address"),
        # Optional on the page, and the only control the site's own
        # validation does not insist on.
        Field(selector="#address", source="address", label="Address",
              required=False),
        # Both selects are driven by LABEL, not value (optout_submit's
        # kind="select" calls select_option(label=...)), so the literals
        # below are the option texts exactly as rendered -- including the
        # em dash and the emoji. Underlying values, for reference, are
        # "other" and "dns".
        Field(selector="#relation", source="literal", value="Other",
              label="Relation to Organisation", kind="select"),
        Field(selector="#type", source="literal",
              value="\N{NO ENTRY SIGN} Do Not Sell or Share My Data (CCPA)",
              label="Request Type", kind="select"),
    ),
    submit_selector="#submitBtn",
    success_markers=(
        "request submitted",
        "please check your inbox and verify your email address",
    ),
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. THERE IS NO "
        "<form> ELEMENT on this page and not one control carries a name "
        "attribute -- an inline submitRequest() reads five ids (#name, "
        "#email, #type, #address, #relation) and POSTs them as JSON to "
        "getApiBase() + '/requests', which resolves to /privacy/api on "
        "careerbuilder.com and /resume/privacy/api on monster.com. That "
        "makes the ids load-bearing in a way they usually are not, and it "
        "is why every selector here is an id rather than a name.\n"
        "\n"
        "WHY THE URL MUST STAY careerbuilder.com. The same page is served "
        "under both brands (its own footer reads 'Monster'), and an IIFE "
        "on it hides the do-not-sell option outright when the hostname "
        "contains 'monster': 'if (source === monster) { dnsOption.style."
        "display = none }'. Checked live on careerbuilder.com, "
        "#dns-option computes display:block, so the option we need is "
        "present here and absent on the sibling. A recipe pointed at the "
        "Monster host would silently be unable to file this request type.\n"
        "\n"
        "Request type is 'Do Not Sell or Share My Data (CCPA)' rather "
        "than Deletion, and relation is 'Other' rather than 'Job Seeker / "
        "Candidate', because a suppression request from someone who never "
        "held a CareerBuilder account would be misdescribed by the "
        "latter. The remaining options, for a future reader: access / "
        "deletion / dns, and customer / employee / ex-employee / "
        "job-applicant / vendor / other.\n"
        "\n"
        "Success is a MODAL, not a navigation: on HTTP 201 the script "
        "adds .active to #popup-overlay, which reads 'Request Submitted! "
        "Your request has been received. Please check your inbox and "
        "verify your email address to activate your request. Without "
        "verification, your request will not be processed.' The "
        "success_markers above are taken from that text. The script also "
        "handles 409 (a duplicate active request of the same type, with a "
        "due date), 429 (rate limited, retry after an hour) and 400 -- "
        "worth knowing because none of those navigate either, so a "
        "submitter must read the page rather than the URL.\n"
        "\n"
        "No captcha script and no captcha widget on the rendered page; "
        "no_captcha_verified stays False because no human has swept it "
        "and no dry run was performed. No honeypot: every control "
        "computes visible. DATASET NOTE, consistent with what is on the "
        "page: the row records that privacy@careerbuilder.com bounced and "
        "that requests now route through Monster -- they route through "
        "this shared page, and this is the brand's end of it. The request "
        "is NOT complete on submission; it is activated by clicking a "
        "link in a verification email, which is a human step this "
        "codebase does not perform."
    ),
)


PORCHGROUPMEDIA = FormRecipe(
    broker_id="porchgroupmedia-com",
    broker_name="Porch Group Media",
    url="https://optout.porchgroupmedia.com/",
    flavor=FLAVOR_PGM_OPTOUT,
    steps=(
        # A radio, driven by Check because page.check() handles radios too.
        # The other two options are "guardian" and "deceased", and choosing
        # either would be a false statement about who is asking.
        Check(selector="input[name='who'][value='myself']",
              label="I certify this request relates to Myself"),
        Field(selector="input[name='first_name']", source="first_name",
              label="First Name"),
        Field(selector="input[name='last_name']", source="last_name",
              label="Last Name"),
        Field(selector="input[name='address_1']", source="street",
              label="Address 1"),
        Field(selector="input[name='city']", source="city", label="City"),
        # 54 options and the labels ARE the two-letter codes.
        Field(selector="select[name='state']", source="state_code",
              label="State", kind="select"),
        Field(selector="input[name='zip_code']", source="zip",
              label="Zip Code"),
        # Capital E. The only control on the form whose name is not
        # lower_snake_case, and a silent no-match waiting to happen.
        Field(selector="input[name='Email']", source="email", label="Email"),
        Field(selector="input[name='phone']", source="phone", label="Phone",
              required=False),
    ),
    submit_selector="button[name='submit']",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. This is the "
        "broker's real suppression surface and it is unusually explicit "
        "about what it does: 'we will only use [this] to remove your name, "
        "address, phone and/or email information from our databases ... "
        "PGM Solutions will remove your data from PGM Solutions' databases "
        "and will no longer share your data with third parties', within "
        "ten business days. POST to /submit2.action.php.\n"
        "\n"
        "WHY EVERY SELECTOR USES name= AND NOT AN id. The page has "
        "duplicate ids, which is a defect on their side rather than a "
        "style choice: id='UserData_FirstName' is on BOTH first_name and "
        "middle_init, and id='UserData_LastName' is on last_name, phone "
        "AND sign_date. A recipe written against those ids would fill the "
        "wrong boxes and look like it worked. The placeholders lie in the "
        "same way -- phone and sign_date both render placeholder 'Last "
        "Name' -- so placeholder-based selectors are out too.\n"
        "\n"
        "TWO FIELDS ARE DELIBERATELY NOT FILLED. middle_init, because an "
        "identity record here has no middle initial to give. And "
        "sign_date, which is a free-text input (maxlength 50) asking the "
        "requester to date their certification: nothing in resolve_fields "
        "supplies today's date, and hardcoding one into a recipe would age "
        "into a lie. Whoever turns this on must decide how sign_date is "
        "populated -- it is NOT marked required in the DOM, but a request "
        "certifying an identity is exactly where a blank date may get it "
        "rejected. That is the single open question on this recipe.\n"
        "\n"
        "No captcha script, no captcha element, no hidden inputs at all "
        "and no honeypot -- every control computes visible. "
        "no_captcha_verified stays False regardless, because no human has "
        "swept it and no dry run was performed. The only required field "
        "per the DOM is the state select.\n"
        "\n"
        "DATASET FINDING, and the consent-portal trap in a new costume. "
        "The row's recorded opt_out_url (/inbound/do-not-sell-my-personal- "
        "information/) redirects to datarightsrequest.porchgroupmedia.com, "
        "a DSAR form whose four request options are access / correct / "
        "portable copy / delete -- NO opt-out of sale among them -- and "
        "which says so itself: 'To opt out of the sale of your personal "
        "information, please go here'. That link leads here. So the "
        "recorded URL would have filed the wrong request type. Worth "
        "noting about that DSAR form too: it carries no captcha SCRIPT but "
        "does carry a hidden input[name='_captcha'] with value 'true', "
        "which is a FormSubmit-style instruction to challenge on submit -- "
        "a reminder that 'no captcha script on the page' is not the same "
        "finding as 'no captcha'. Second channel for a human: "
        "privacy@porchgroupmedia.com."
    ),
)


_CONVEX_FORM = "form:has(select[name='request_type'])"

CONVEX = FormRecipe(
    broker_id="convex-com",
    broker_name="Convex",
    url="https://www.convex.com/request-removal",
    flavor=FLAVOR_CONVEX_HUBSPOT,
    fields=(
        # Every selector is scoped through _CONVEX_FORM, and that scoping is
        # load-bearing rather than tidy: the page carries a SECOND HubSpot
        # form (a newsletter signup) whose email input is also
        # input[name='email'], so an unscoped "#email" is ambiguous and
        # would be a coin flip between filing the request and subscribing
        # to a mailing list.
        Field(selector=_CONVEX_FORM + " select[name='request_type']",
              source="literal", value="Opt-out Request",
              label="Request Type", kind="select"),
        Field(selector=_CONVEX_FORM + " select[name='convex_user_type']",
              source="literal", value="Customer",
              label="User Type", kind="select"),
        Field(selector=_CONVEX_FORM + " input[name='firstname']",
              source="first_name", label="First Name"),
        Field(selector=_CONVEX_FORM + " input[name='lastname']",
              source="last_name", label="Last Name"),
        Field(selector=_CONVEX_FORM + " input[name='email']",
              source="email", label="Email Address"),
        # 52 options whose labels ARE the two-letter codes ("AK", "AL"),
        # so source="state_code" and not "state".
        Field(selector=_CONVEX_FORM + " select[name='state_helper']",
              source="state_code", label="State", kind="select"),
    ),
    submit_selector=_CONVEX_FORM + " button",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. The page states "
        "the right in its own words -- 'you may have the right to opt-out "
        "of the sale of personal information' -- and the form is the "
        "surface it offers for it.\n"
        "\n"
        "THE FORM ID IS PER-RENDER. Loaded twice in the same session it "
        "came back as #hsform-53822860 and then #hsform-41419905, which is "
        "ordinary HubSpot behaviour and fatal to any recipe written "
        "against the id. Everything here keys on field names instead, and "
        "the form is reached with form:has(select[name='request_type']) -- "
        "the one selector on the page that identifies it by what it "
        "CONTAINS rather than by what it was called this time.\n"
        "\n"
        "Field notes. Nothing is marked required in the DOM (HubSpot "
        "validates client-side), so absence of a required flag here is not "
        "evidence a field is optional. The submit control is a <button> "
        "with no type attribute, so button[type='submit'] does NOT match "
        "it -- the same trap already recorded for checkpeople-com -- hence "
        "the bare 'button' at the end of the submit selector. No captcha "
        "script and no captcha widget were loaded; no_captcha_verified "
        "stays False because no human has swept it and no dry run was "
        "performed. No honeypot: every control computes visible.\n"
        "\n"
        "ONE HONEST WART, for whoever turns this on. convex_user_type "
        "offers only 'Customer' or 'Authorized Agent'. A consumer who has "
        "never dealt with Convex is neither, and 'Customer' is chosen here "
        "because it is the only self-referring option -- filing as an "
        "Authorized Agent would be a false statement about acting for "
        "someone else. The other request types available, for reference, "
        "are Access / Correction / Deletion.\n"
        "\n"
        "No success marker recorded because nothing was submitted; a "
        "future reader should capture one before this leaves "
        "STAGED_RECIPES. Note also that Convex is now part of "
        "ServiceTitan, so this surface may migrate. Second channel for a "
        "human, named on the page itself: support@convexlabs.io."
    ),
)


_ENIGMA_FORM = "form.hs-form-private:has(input[name='firstname'])"

ENIGMA = FormRecipe(
    broker_id="enigma-com",
    broker_name="Enigma Technologies",
    url="https://www.enigma.com/legal/do-not-sell",
    flavor=FLAVOR_ENIGMA_HUBSPOT,
    fields=(
        Field(selector=_ENIGMA_FORM + " input[name='firstname']",
              source="first_name", label="First name"),
        Field(selector=_ENIGMA_FORM + " input[name='lastname']",
              source="last_name", label="Last name"),
        Field(selector=_ENIGMA_FORM + " input[name='email']",
              source="email", label="Email"),
        # The page labels this one "U.S. State (optional)" and it is a plain
        # text box, NOT a select -- so kind stays "text" and the source is
        # "state" (the full name) rather than "state_code". Nothing on the
        # page constrains the format, and a two-letter code in a box asking
        # for a state is a guess about their parser, not a reading of it.
        Field(selector=_ENIGMA_FORM + " input[name='state']",
              source="state", label="U.S. State", required=False),
    ),
    submit_selector=_ENIGMA_FORM + " input[type='submit']",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23, loaded twice. The "
        "page states the right plainly -- 'You may have a right to opt-out "
        "from future \"sales\" or \"sharing\" of personal information' -- "
        "and this form is the surface it offers for exercising it. Four "
        "fields, three required, all four already sourced by "
        "resolve_fields.\n"
        "\n"
        "WHY THE SELECTOR IS STRUCTURAL rather than the form's id. The id "
        "here (#hsForm_7cce78e6-7f9d-4f0e-b3f4-69db6c988d2a) came back "
        "IDENTICAL on both loads, so unlike Convex -- whose HubSpot id was "
        "re-minted per render as #hsform-53822860 then #hsform-41419905 -- "
        "this one looks stable. It is still not used. A GUID that happened "
        "to match twice is weak evidence next to a known HubSpot behaviour "
        "that has already broken one recipe in this module, and the "
        "structural selector costs nothing. Unlike Convex there is no "
        "second form on the page to disambiguate from, which is why the "
        "scoping here is simpler.\n"
        "\n"
        "NOT LIVE-VERIFIED, and staged rather than shipped for the usual "
        "reason: nothing was submitted, so the success markers are unknown "
        "and success_markers is deliberately empty rather than guessed at. "
        "no_captcha_verified stays False. No captcha script, no captcha "
        "element and no hidden challenge input were present on either load "
        "-- but HubSpot can attach a challenge at the submit step, and "
        "porchgroupmedia in this same module is the worked example of a "
        "form that carried no captcha script and a hidden _captcha input "
        "all the same. The absence seen here is real and is still not a "
        "verification.\n"
        "\n"
        "Second channel for a human: privacy@enigma.com."
    ),
)


_EYEOTA_FORM = "form.hs-form-private:has(select[name='regulation'])"

EYEOTA = FormRecipe(
    broker_id="eyeota-com",
    broker_name="Eyeota",
    url="https://www.eyeota.com/data-subject-request",
    flavor=FLAVOR_EYEOTA_DSR,
    steps=(
        # The request TYPE is a checkbox group, not a select, and several may
        # be ticked at once. Only the opt-out is ticked here: a recipe should
        # ask for what it was told to ask for and nothing else, and bundling
        # in a deletion would be making a decision for the user.
        Check(selector=(_EYEOTA_FORM
                        + " input[name='nature_of_request'][value='Opt-out']"),
              label="I would like to opt out"),
        Field(selector=_EYEOTA_FORM + " input[name='firstname']",
              source="first_name", label="First name", required=False),
        Field(selector=_EYEOTA_FORM + " input[name='lastname']",
              source="last_name", label="Last name", required=False),
        Field(selector=_EYEOTA_FORM + " input[name='email']",
              source="email", label="Email"),
        # Options read off the live select, verbatim: "Please Select One",
        # "CCPA (California, Colorado, Connecticut, Virginia, Utah, Nevada
        # Residents)", "GDPR (Non US Requests)", "Other / Generic".
        Field(selector=_EYEOTA_FORM + " select[name='regulation']",
              source="literal", value="Other / Generic",
              label="Regulation", kind="select"),
    ),
    submit_selector=_EYEOTA_FORM + " input[type='submit']",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23. The dataset points "
        "at /how-to-opt-out, which is an EXPLAINER; it links onward to "
        "/data-subject-request, which is the form, and which describes its "
        "own scope: 'This is the form for submitting a request to see what "
        "data Eyeota may have about you ... to ask that Eyeota delete your "
        "data, and to exercise other rights under applicable law.'\n"
        "\n"
        "WHY 'Other / Generic' AND NOT THE CCPA OPTION, which is the one real "
        "judgement call in this recipe. The regulation select is required and "
        "its CCPA option enumerates six states by name. A recipe cannot know "
        "the user's state -- resolve_fields can supply one, but nothing maps "
        "a state to a statute, and picking CCPA for someone in Texas would "
        "assert a jurisdiction they are not in. 'Other / Generic' is true "
        "for every requester, and an opt-out filed under it is still an "
        "opt-out. The cost is that a Californian may get generic handling "
        "instead of statutory handling, which is a real cost and is the thing "
        "a human should weigh before this leaves STAGED_RECIPES. If it is "
        "decided that jurisdiction should be asserted, the option string must "
        "be copied from the live select exactly -- kind='select' matches by "
        "LABEL.\n"
        "\n"
        "The checkbox VALUES are clean and were read off the live page: "
        "'Access Request', 'Deletion', 'Update Request', 'HEM Opt-out', "
        "'Opt-out', 'Questions', 'Complaints'. Note 'HEM Opt-out' sitting "
        "beside 'Opt-out' -- HEM being hashed email, a different request -- so "
        "the value must be matched exactly and not by prefix.\n"
        "\n"
        "THE FORM ID CARRIES A PER-RENDER SUFFIX: it read "
        "#hsForm_c1b0b5d7-7377-412c-a187-c25e06d9e929_4310, and the trailing "
        "_4310 is HubSpot's instance counter. Hence the structural selector, "
        "keyed on the regulation select, which no other form on the page has. "
        "input[name='hidden_field_for_rich_text_'] is a HubSpot rendering "
        "artefact rather than a honeypot and is simply left alone.\n"
        "\n"
        "NOT LIVE-VERIFIED. Nothing was submitted, success_markers is empty "
        "rather than guessed, and no_captcha_verified stays False: no captcha "
        "script, element or hidden challenge input was seen, which is a real "
        "observation and not a verification. Second channel for a human: "
        "privacy@eyeota.com. Note also that the page offers a TrustArc "
        "'Your Privacy Choices' link (submit-irm.trustarc.eu) as a separate "
        "surface, unexamined here."
    ),
)


_FINDEM_FORM = "form#wf-form-Do-not-sell"

FINDEM = FormRecipe(
    broker_id="findem-ai",
    broker_name="Findem",
    url="https://www.findem.ai/privacy-rights",
    flavor=FLAVOR_FINDEM_WEBFLOW,
    fields=(
        Field(selector=_FINDEM_FORM + " input[name='First-Name']",
              source="first_name", label="First Name", required=False),
        Field(selector=_FINDEM_FORM + " input[name='Last-Name']",
              source="last_name", label="Last Name", required=False),
        Field(selector=_FINDEM_FORM + " input[name='Email-Address']",
              source="email", label="Email Address"),
    ),
    submit_selector=_FINDEM_FORM + " input[type='submit']",
    notes=(
        "Transcribed from the rendered DOM on 2026-09-23, loaded twice and "
        "identical both times. A plain Webflow form on a page titled "
        "'Privacy Rights', and the form's own id says what it is for: "
        "wf-form-Do-not-sell. Only the email address is required.\n"
        "\n"
        "Unlike every HubSpot-hosted form in this module the id here is "
        "AUTHORED rather than generated, so it is used directly -- and it has "
        "to be, because the scoping is load-bearing for the usual reason: the "
        "page carries a SECOND form, a HubSpot newsletter box whose only "
        "field is also an email input (input[name='email'], beside ten hidden "
        "UTM and gclid trackers and a 'Subscribe' button). An unscoped email "
        "selector on this page is a coin flip between filing the request and "
        "joining a mailing list.\n"
        "\n"
        "input[name='Other-comments'] is deliberately left unfilled: there is "
        "nothing a recipe could honestly put in a free-text box that the "
        "structured fields have not already said.\n"
        "\n"
        "NOT LIVE-VERIFIED and staged rather than shipped: nothing was "
        "submitted, so success_markers is empty rather than invented, and "
        "no_captcha_verified stays False. No captcha script, element or "
        "hidden challenge field was present on either load. Webflow forms "
        "post to the page itself and swap in a success div, so a future "
        "verification pass should expect an in-place success message rather "
        "than a navigation. taylor@findem.ai is the address the dataset "
        "records."
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
    CAREERBUILDER.broker_id: CAREERBUILDER,
    CONVEX.broker_id: CONVEX,
    PORCHGROUPMEDIA.broker_id: PORCHGROUPMEDIA,
    ENIGMA.broker_id: ENIGMA,
    EYEOTA.broker_id: EYEOTA,
    FINDEM.broker_id: FINDEM,
}


# --- category note: FCRA-regulated background screening ----------------------
#
# A whole class of rows in the dataset are consumer reporting agencies doing
# employment, tenant or mortgage screening. They keep turning up and they all
# end the same way, so the reasoning is written ONCE here and referenced from
# the individual entries instead of being re-derived per broker.
#
# THE FINDING, in the brokers' own words rather than ours. GoodHire's policy
# says, in capitals: "PLEASE NOTE THAT ANY BACKGROUND CHECK DATA REGULATED BY
# THE FAIR CREDIT REPORTING ACT IS EXEMPT FROM CCPA DATA RIGHTS REQUESTS."
# Checkr spells the same thing out with a worked example: "if you sent us a
# CCPA request asking us to delete personal information from your background
# check -- such as criminal history or motor vehicle records -- the CCPA would
# not require us to do so." HireRight's request form opens with "IMPORTANT -
# READ BEFORE SUBMITTING" and argues that "our background screening business
# falls within those exceptions".
#
# WHY THAT MATTERS TO A FORM-FILLING TOOL. These companies DO publish
# "Do Not Sell or Share My Personal Information" links. Those links are
# scoped to the WEBSITE -- cookies, cross-context advertising, a consent
# widget -- and do not touch the report database, which is the only thing a
# person cares about here. Automating one would be automating something that
# accomplishes nothing real, while telling the user their data was suppressed.
# That is worse than doing nothing, so no recipe ships for this class.
#
# WHAT ACTUALLY HELPS, for anyone writing user-facing copy: the recourse is
# the FCRA itself -- a file disclosure, a dispute of inaccurate entries, and
# a security freeze where the agency offers one -- exercised through the
# agency's identity-verified consumer channel, not through a privacy opt-out.
#
# SCOPE, deliberately narrow. This note covers agencies whose business IS
# FCRA-regulated screening. It does NOT cover the marketing arms of the credit
# bureaus, which is why ``experian-com`` and ``transunion-com`` are mapped
# individually and stay that way: the dataset names those rows "Experian
# Marketing Services" and "TransUnion Marketing", and a marketing identity
# graph is exactly the non-exempt side of those companies. A broker is only
# put in this class after its own pages have been read; when in doubt it gets
# an ordinary entry.
FCRA_SCREENING_NOTE = (
    "FCRA-regulated background screening -- see the category note above "
    "NO_OPTOUT_SURFACE in this module. The report database is exempt from "
    "state privacy-rights requests by the broker's own account of it, and "
    "any 'do not sell' control offered is scoped to website cookies and "
    "advertising rather than to the reports. No recipe is written for this "
    "class, because automating the cookie control would tell a user their "
    "records were suppressed when nothing about them changed. The real "
    "recourse is an FCRA file disclosure, dispute or freeze through the "
    "agency's identity-verified consumer channel."
)


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
    "centeda-com": (
        "This broker no longer exists, and the page says so itself. "
        "Verified by browser render 2026-09-23: "
        "centeda.com/ng/control/privacy returns 200 with the title 'This "
        "Domain Has Been Transferred by Court Order' and a notice that, "
        "pursuant to a final judgment of the New Jersey Superior Court in "
        "Atlas Data Privacy Corporation, et al. v. Radaris.com, et al. "
        "(Law Division, Middlesex County, Docket No. MID-L-000847-24), "
        "the domain has been transferred to Atlas Data Privacy "
        "Corporation and is 'no longer under the control of its former "
        "operators'. The page carries no form, no input of any kind and "
        "no contact channel. There is nothing to opt out of here and "
        "nobody left to ask -- the strongest possible no-surface finding, "
        "and a different KIND of no-surface from the rest of this dict, "
        "which is why it is worth spelling out. Related context recorded "
        "on the page: the suit asserts claims assigned by roughly 21,760 "
        "law enforcement officers and other 'covered persons' under New "
        "Jersey's Daniel's Law, and an Amended Complaint was filed "
        "2025-05-27."
    ),
    "liftbasedata-com": (
        "Verified by browser render 2026-09-23, and blocked twice over. "
        "www.liftbasedata.com/request-to-know/ is a HUB, not a form: it "
        "links to five separate per-list request pages (LiftBase, "
        "American WeddingBase, American BabyBase / Family Connexx "
        "Premium, American Pre-Movers, American Movers) and the hub "
        "itself renders no form at all, only a stray g-recaptcha-response "
        "textarea from the Gravity Forms reCAPTCHA plugin the page loads. "
        "First blocker, and the one that lands this row here rather than "
        "under 'blocked': the page states that to respond to a request "
        "LiftEngine requires 'List Name, Full Name, Mailing Address, DATE "
        "OF BIRTH, Marital Status'. A demand for date of birth is the "
        "same line already drawn under chexsystems-com -- this tool does "
        "not hand a broker a DOB to get a removal. Second, the only "
        "channel that is actually an OPT-OUT rather than a right-to-know "
        "is a printed PDF (liftbasedata.com/forms/CCPA-Opt-Out-Form.pdf) "
        "mailed to LiftEngine, 1 Blue Hill Plaza, Box 1509, Pearl River "
        "NY 10965 -- mail-only, so no webform even setting the DOB aside. "
        "Third, the five online forms are per-LIST, so a complete request "
        "means choosing which of LiftEngine's products to name."
    ),
    "cdkglobal-com": (
        "Verified by browser render 2026-09-23: CDK's 'Do Not Sell or "
        "Share My Personal Information' footer link has an EMPTY href -- "
        "it is a JavaScript consent-widget trigger, not a page -- and the "
        "privacy statement scopes it explicitly to tracking technologies: "
        "'You may manage your preferences on the tracking technologies "
        "deployed on the Site by clicking on the Do Not Sell or Share My "
        "Personal Information link at the footer'. Its own text says 'We "
        "do not sell your Personal Information' for other purposes, and "
        "every actual rights request (deletion, access) is directed to a "
        "contact address rather than a form. So there is no web surface "
        "that suppresses a person's records here, only a cookie "
        "preference for this website's visitors. The remaining channel is "
        "mailbox-only, which this codebase cannot represent. Dataset "
        "contact for a human: james.kinzer@cdk.com."
    ),
    "censia-com": (
        "Verified by browser render 2026-09-23: censia.com/privacy- "
        "policy/ carries an 'Opt-Out of the Sale or Sharing of Personal "
        "Information' heading whose body is explicitly cookie-scoped -- "
        "'Our use of certain cookies or other tracking technologies is "
        "deemed a sale or sharing under California law' -- followed by "
        "browser Do Not Track advice. There is no request form anywhere "
        "on the domain: the only controls the rendered page contains are "
        "the Complianz consent-banner checkboxes (cmplz-functional-optin, "
        "cmplz-preferences-optin, cmplz-statistics-optin). Every "
        "substantive right ('If you choose to assert any of these rights "
        "... please contact us at the appropriate address below') routes "
        "to a mailbox: support@censia.com, or 1-888-510-2253. Mailbox- "
        "only, so no surface for a form-filling recipe. Dataset contact "
        "for a human: tgotowka@censia.com."
    ),
    "civitech-io": (
        "Verified by browser render 2026-09-23: the section the dataset "
        "points at (civitech.io/privacy-policy/#section5) is 'Message "
        "Recipient Opt-Out of Customer Messages through TextOut' -- an "
        "SMS unsubscribe handled by replying STOP to a text, and the "
        "policy is explicit that 'Opt-outs are tracked separately for "
        "each TextOut Customer, so unsubscribing from one TextOut "
        "Customer will not unsubscribe you from messages from another'. "
        "That is a per-campaign message opt-out, not a data-broker "
        "suppression, and it cannot be exercised from a web page at all. "
        "The policy's other opt-out references are all third-party ad- "
        "tech links (Google Ads Settings, Google Marketing Platform, the "
        "NAI portal). The policy states Civitech does not sell personal "
        "information for marketing, and directs rights requests to a "
        "contact address. No form exists on the domain -- the only "
        "control on the whole page is the mobile menu toggle. Dataset "
        "contact for a human: jake.london@civitech.io."
    ),
    "cision-com": (
        "Verified by browser render 2026-09-23: the dataset's opt_out_url "
        "IS the right page and it deliberately has no form on it. "
        "www.cision.com/contact-us/opt-out/ ('Cision ID Opt Out') renders "
        "no <form> and no input beyond the site chrome; its entire offer "
        "is two lines of text -- 'Submit via Privacy@cision.com' and "
        "'Call our toll-free number'. The one automated thing on the page "
        "is an invisible tracking-opt-out beacon, an iframe to "
        "c212.net/c/tracking-opt-out/?t=1, which drops a Cision cookie "
        "opt-out in the visitor's own browser and does nothing to the "
        "contact database Cision sells. So the personal-data channel here "
        "is mailbox-only by the broker's own design, which this codebase "
        "has no way to exercise. Dataset contact for a human: "
        "legaldept@cision.com; the page itself names Privacy@cision.com, "
        "which is the one to use."
    ),
    "checkr-com": (
        "Verified by browser render 2026-09-23: Checkr's policy is "
        "explicit that the data a consumer would want suppressed is out "
        "of reach of the request: 'if you sent us a CCPA request asking "
        "us to delete personal information from your background check -- "
        "such as criminal history or motor vehicle records -- the CCPA "
        "would not require us to do so', because FCRA rules apply "
        "instead. The only opt-out the policy does offer is scoped to the "
        "website: a footer 'Do Not Sell or Share My Personal Information' "
        "consent link plus honoring GPC signals, i.e. cross-context "
        "advertising, not the report database. No request form exists "
        "anywhere on checkr.com; rights contact is a mailbox. Note the "
        "policy also covers 'Zethos, Inc. d/b/a Truv', recently acquired. "
        "Dataset contact for a human: hello@checkr.com."
    ),
    "goodhire-com": (
        "Verified by browser render 2026-09-23: goodhire.com/privacy/ "
        "states the exemption in capitals in its own text: 'PLEASE NOTE "
        "THAT ANY BACKGROUND CHECK DATA REGULATED BY THE FAIR CREDIT "
        "REPORTING ACT IS EXEMPT FROM CCPA DATA RIGHTS REQUESTS.' "
        "Everything it does offer is mailbox-only -- 'To exercise your "
        "rights under the CCPA or to contact us about our privacy policy, "
        "send us an email at dpo@inflection.com. You may also submit a "
        "request at legal@inflection.com' -- with privacy@goodhire.com "
        "for general questions and a Nevada address also routed to "
        "dpo@inflection.com. The only controls the rendered page contains "
        "are the OneTrust cookie-preference panel. Worth noting the "
        "parent brand: GoodHire is Inflection, so a separate Inflection "
        "row would be the same company. The dataset has no email for this "
        "row; the page supplies three."
    ),
    "cisive-com": (
        "Verified by browser render 2026-09-23: the dataset's opt_out_url "
        "(cisive.com) has no opt-out page behind it; the privacy notice "
        "lives at /legal-pages/privacy-and-cookies-policy and offers no "
        "form at all. The only form on the whole domain is the HubSpot "
        "marketing signup (hsForm_e3534c86-..., one 'Your work email' "
        "field plus utm_ hidden fields). Its opt-out language is about "
        "communications only -- email unsubscribe links, and 'You can opt "
        "out of receiving notifications to your mobile device, SMS text "
        "messaging ... by contacting us at disputes@cisive.com or typing "
        "STOP'. Background-report access is redirected to the FCRA "
        "dispute channel (disputes@cisive.com) and general privacy "
        "questions to compliance@cisive.com or (855) 881-0716. Mailbox- "
        "only. Dataset contact Mkendrick@cisive.com is a personal "
        "address; compliance@cisive.com is the better one for a human."
    ),
    "bisi-com": (
        "Verified by browser render 2026-09-23: BISI's privacy notice is "
        "at bisi.com/?pg=privacy (NOT /privacy-policy/, which 404s -- "
        "worth recording since the obvious guess fails). It does contain "
        "a real 'E. Right to Opt-Out of Sale or Sharing' clause, but "
        "there is no form on the page or anywhere on the domain: the only "
        "controls rendered are the Cookiebot consent checkboxes, and "
        "every channel the notice names is a mailbox -- service@bisi.com "
        "and compliance@bisi.com. Mailbox-only, which this codebase "
        "cannot exercise. The dataset holds no email for this row; those "
        "two are the ones the site publishes."
    ),
    "birchwoodcreditservices-com": (
        "Verified by browser render 2026-09-23: "
        "birchwoodcreditservices.com/privacy-policy/ carries no rights- "
        "request surface at all -- the only opt-out it mentions is Google "
        "Analytics' browser add-on, and the only contact it publishes is "
        "info@birchwoodcreditservices.com. The two forms on the page are "
        "both the HubSpot content search. This is consistent with what "
        "the company is: a reseller of mortgage credit reports to "
        "LENDERS, so a consumer's route to the data is an FCRA dispute "
        "through the lender or the underlying bureau, not a suppression "
        "request here. Mailbox-only, no surface."
    ),
    "datafacts-com": (
        "FCRA-REGULATED BACKGROUND SCREENING -- see the category note "
        "above NO_OPTOUT_SURFACE in this module for why this whole class "
        "gets no recipe. Verified by browser render 2026-09-23: Data "
        "Facts is a background- and tenant-screening CRA plus mortgage "
        "verification, sold to employers, landlords and lenders. Its site "
        "offers no rights-request form; the consumer entrances are "
        "'Applicant Support' and 'Client Login', both authenticated FCRA "
        "channels, which is precisely the recourse the category note "
        "describes. Dataset contact for a human: "
        "compliance@datafacts.com."
    ),
    "creditinfosystems-com": (
        "FCRA-REGULATED BACKGROUND SCREENING -- see the category note "
        "above NO_OPTOUT_SURFACE in this module for why this whole class "
        "gets no recipe. Verified by browser render 2026-09-23: Credit "
        "Information Systems resells tri-merge credit reports and "
        "verification products to LENDERS. creditinfosystems.com carries "
        "no form at all and no privacy or opt-out link -- its only "
        "entrance is CLIENT LOGIN -- and a consumer's route to the "
        "underlying data is an FCRA dispute through the lender or the "
        "bureau that supplied it. Note the dataset names this row 'Credit "
        "Bureau Of Council Bluffs, Inc', an older corporate name for the "
        "same operation. Dataset contact: heather@creditinfosystems.com, "
        "a personal address."
    ),
    "crsspxl-com": (
        "Verified by browser render 2026-09-23: Cross Pixel's opt-out is "
        "a COOKIE control and says so plainly. privacy.crsspxl.com/optout "
        "is titled 'Behavioral Targeting Opt Out (Do Not Sell My "
        "Information)' and its entire content is a report on the "
        "visitor's own browser -- 'You can view the status of Behavioral "
        "Targeting against your browser below', which for this visit read "
        "'No Cross Pixel cookie found. You have not visited any Cross "
        "Pixel partner sites, or you currently have cookies disabled.' "
        "There is no form, no field, and nothing keyed to a person: the "
        "opt-out is a cookie set in whichever browser visits. Nothing a "
        "form-filling recipe can do, and nothing a named-person request "
        "would attach to. Dataset contact: privacy@crosspixel.net."
    ),
    "crawlbee-com": (
        "Verified 2026-09-23: crawlbee.com no longer belongs to a "
        "company. It redirects to forsale.godaddy.com -- the domain is "
        "parked for sale -- and that in turn serves an Akamai 'Access "
        "Denied' page. There is no opt-out page, no privacy notice and no "
        "operator to address, and the dataset records no email for this "
        "row. Distinct from the DNS failures recorded elsewhere in this "
        "sweep: the redirect target positively identifies the domain as "
        "retired rather than merely unreachable."
    ),
    "cyndx-com": (
        "Verified 2026-09-23: the company is dissolving. The recorded URL "
        "(/california-do-not-track/) now serves a letter from the founder "
        "in place of any content -- 'After careful consideration, we have "
        "made the difficult decision to wind down and dissolve Cyndx' -- "
        "and no form, policy or rights channel remains on the site. There "
        "is no surface and, shortly, no company. Dataset contact "
        "privacy@cyndx.com may still be read during the wind-down but is "
        "mailbox-only regardless."
    ),
    "digitalvikingmedia-com": (
        "Verified 2026-09-23 by rendering the whole site. Digital Viking "
        "Media is a five-page brochure site and the privacy policy is the "
        "only page with any opt-out content on it. Its 'Opt- "
        "Out/Unsubscribe' section is an in-page anchor (#optout) holding "
        "prose, not a form: the site has no form element anywhere, and "
        "the sole interactive control in the whole document is a 'Skip to "
        "Main Content' button.  What the section actually offers is two "
        "channels, both outside a web recipe: the industry cookie opt-out "
        "at optout.aboutads.info, which is the DAA's page and not this "
        "broker's, and mailto:optout@digitalvikingmedia.com, which "
        "matches the address the dataset already records. So the honest "
        "reading is that the email channel IS the opt-out here, and there "
        "is no first-party web surface to automate -- not a form hidden "
        "behind a wall, simply no form."
    ),
    "drobu-com": (
        "Verified 2026-09-23 by rendering the site. drobu.com is a lead- "
        "generation agency brochure (Home, About Us, Lead Generation, "
        "Insurance Agents, Dealers, Our Partners, Contact) and its "
        "homepage contains no form element at all -- the only controls "
        "are a nav toggle and the cookie banner's Accept/Decline. "
        "/privacy-policy/ returns the site's own 404 page: 'Sorry, the "
        "page you're looking for doesn't exist or has been moved'. The "
        "footer still reads (c) 2018.  So there is no privacy policy to "
        "read, no rights page, and no request form to automate. This "
        "matches what the dataset already says -- method email, "
        "dblackman@drobu.com -- and the finding is simply that the web "
        "side is empty rather than walled. A person exercising rights "
        "here has to write to that address."
    ),
    "earlywarning-com": (
        "FLAGGED AS BORDERLINE, 2026-09-23 -- mapped here rather than "
        "folded into the FCRA-screening category note above, "
        "deliberately.  Early Warning Services is a consumer reporting "
        "agency, but a BANKING one: it runs the deposit-account and "
        "payments-history products that banks check when someone opens an "
        "account, which is a different industry from the employment "
        "background screeners the category note covers. It is recorded as "
        "its own entry so that note's scope is not quietly widened; if a "
        "human later decides the note should cover every FCRA consumer "
        "reporting agency rather than screening specifically, this is the "
        "row to revisit first.  The finding itself is not in doubt. "
        "Rendering /consumer-information and /your-early-warning-file- "
        "disclosure shows a site organised entirely around FCRA rights -- "
        "'SUBMIT A DISPUTE', 'YOUR FILE DISCLOSURE', a subpoena process "
        "page -- and no opt-out of any kind is offered, because an FCRA "
        "file is not something a consumer can opt out of. The only form "
        "element on either page is the site search. The disclosure route "
        "is explicitly off-web: 'Print form to request your file "
        "disclosure and then mail, fax or send electronically', or call. "
        "So there is no web opt-out surface to automate, and the real "
        "recourse is the FCRA access and dispute rights the site already "
        "routes people to."
    ),
    "socialgist-com": (
        "Verified 2026-09-23. socialgist.com redirects to socialgist.ai "
        "-- a domain change the dataset does not record, flagged here and "
        "left unfixed in data/source-brokers.json. The site that answers "
        "is a brochure for social-conversation data sold to AI and "
        "intelligence platforms, and it contains no form element at all: "
        "the only controls in the whole document are the cookie banner's "
        "Accept and Decline.  There is no privacy request page, no rights "
        "page and no do-not-sell link anywhere on it. This matches what "
        "the dataset already says -- method email, info@socialgist.com -- "
        "and the finding is that the web side is genuinely empty rather "
        "than walled. Note that info@ is a general enquiries address "
        "rather than a privacy one, so a person writing to it should not "
        "assume it reaches a rights process."
    ),
    "emailmovers-com": (
        "Verified 2026-09-23 by rendering both the homepage and /privacy- "
        "policy/. Emailmovers is a UK B2B list vendor, and its privacy "
        "policy page contains NO form at all -- the only artefacts on it "
        "are the cookie consent manager and a stray reCAPTCHA badge left "
        "by the site-wide Contact Form 7 script. There is no rights page, "
        "no do-not-sell link and no request form anywhere on the site. "
        "The only real form is on the homepage and it is a SALES enquiry: "
        "your-firstname, your-lastname, your-email, company and telephone "
        "all required, a 'Requirements...' box, and a submit button "
        "reading 'Let's Go!'. It is reCAPTCHA-protected and it is not an "
        "opt-out surface; recorded here so nobody mistakes it for one, "
        "which is an error this sweep has had to correct before.  This "
        "matches the dataset -- method email, compliance@emailmovers.com, "
        "which at least is a compliance address rather than a general "
        "one. As a UK company its statutory channel is a UK GDPR request "
        "by email, so the absence of a web form is a gap in convenience "
        "rather than in rights."
    ),
    "emerges-com": (
        "Verified 2026-09-23, and this is the first row in the sweep "
        "where the right answer is that THE BROKER HAS LEFT THE BUSINESS. "
        "emerges.com still serves a site advertising 'watercraft, "
        "aircraft, voter and snowmobile registrations with pilot, hunting "
        "and fishing licenses', and its nav still carries 'REMOVE ME/OPT "
        "OUT' pointing at a Google Form. Following that form to its full "
        "address (docs.google.com/forms/d/e/1FAIpQLSdi3KjEPMsVnXQL- "
        "KllxvgOQWxvLpLfuz30-Z_eqXDHGEbX6w) redirects to /closedform, "
        "which says, in the broker's own words:    'eMerges Opt Out is "
        "now Disabled. *As of July 1, 2025 eMerges ceased   operating as "
        "a List Broker. 1) eMerges is not acquiring, processing, "
        "publishing or selling any lists either directly or indirectly "
        "and   including but not limited to government records. 2) "
        "eMerges has   ceased operating its entire list business "
        "therefore this opt out   resource has been disabled effective "
        "20260223.'  So there is no surface, and uniquely there is "
        "nothing that a surface would accomplish. Recorded as no-surface "
        "rather than blocked or undecided because the absence is "
        "deliberate, dated, and explained by the broker.  DATASET NOTE, "
        "flagged and not acted on: this row is arguably retired rather "
        "than mapped, and a defunct broker in a 969-row checklist is "
        "worth distinguishing from a live one with no form. That is a "
        "decision about the dataset's shape, not about this broker, so it "
        "is left to a human. Note also, in passing, that the surviving "
        "evidence is a Google Form -- the same pattern as clay-com, which "
        "is still open pending aria-labelledby resolution."
    ),
    "clarityservices-com": (
        "FLAGGED AS BORDERLINE-FCRA, 2026-09-23, and mapped as its own "
        "entry rather than folded into the FCRA-screening category note "
        "above -- the same treatment as earlywarning-com and for the same "
        "reason. Clarity Services (Experian Data Corp) is a consumer "
        "reporting agency, but a SUBPRIME LENDING one: it supplies the "
        "alternative-credit data behind payday and instalment lending "
        "decisions. That is neither employment screening, which the "
        "category note covers, nor marketing data, which this tool exists "
        "for.  The recorded /support/opt-out-2/ is titled 'How to Opt-Out "
        "of a Prescreen List' and it contains no form -- the only form "
        "elements on the page are two copies of the theme's site search. "
        "What it describes is the PRESCREEN opt-out, the statutory FCRA "
        "right to stop credit bureaus including you in pre-approved "
        "credit and insurance offers. That right is real and worth "
        "exercising, but it is not exercised here: it is exercised "
        "centrally, through the industry's joint channel and by "
        "telephone, and nothing on this broker's own site can take the "
        "request.  So there is no first-party web surface to automate. "
        "The site's other consumer routes are 'Request Your Clarity "
        "Report' and a dispute process, which are FCRA access and dispute "
        "rights rather than an opt-out, and are the correct recourse for "
        "anyone who wants their file changed. optout@experian.com is the "
        "address the dataset records."
    ),
    "vdx-tv": (
        "Verified 2026-09-23. VDX.TV's privacy page carries exactly one "
        "form and it is #cookie-preferences, a Finsweet consent panel "
        "with three checkboxes (marketing, personalization, analytics). "
        "There is no request form, no rights form and no email address "
        "recorded for this row.  What the page offers instead, under 'Do "
        "Not Sell or Share My Info', is a single link: 'Click here to "
        "Opt-Out' pointing at "
        "a.tribalfusion.com/optout/vdx?participant=... . That is an ad- "
        "industry COOKIE opt-out on the Tribal Fusion (Exponential) ad- "
        "serving host -- per-browser, suppressing targeting rather than "
        "removing a profile, and keyed to a participant parameter rather "
        "than to a person.  IT WAS DELIBERATELY NOT FOLLOWED. Everything "
        "about its shape says it is actuated by the GET itself, which is "
        "precisely the hazard dstillery demonstrated earlier in this "
        "sweep, where merely navigating to an /optout URL performed the "
        "opt-out. The probe tool's docstring now says such URLs are to be "
        "opened deliberately and one at a time, never in a bulk sweep, "
        "and this entry honours that rather than quietly making an "
        "exception. Not following it costs nothing: a cookie opt-out on a "
        "throwaway browser profile would tell us nothing we do not "
        "already know.  Recorded as no-surface rather than blocked "
        "because nothing is defending anything -- there simply is no "
        "first-party request form here."
    ),
    "fairscreen-com": (
        "FCRA_SCREENING_NOTE applies, and this broker states it in its "
        "own words. fairscreen.com/privacy-policy opens: 'Fair Screen, "
        "Inc ('FSI') is a consumer reporting agency governed by the "
        "federal Fair Credit Reporting Act (FCRA), 15 U.S.C. ...'. That "
        "is the exact self-declaration the category note above "
        "NO_OPTOUT_SURFACE describes, so the reasoning is not repeated "
        "here: see it for why a marketing-style opt-out would not touch "
        "the report database and why the real recourse is the FCRA access "
        "and dispute rights instead.  Verified 2026-09-23 by rendering "
        "both the homepage and the privacy policy: there is no request "
        "form of any kind on either, and the only controls on either page "
        "belong to the cookie banner. The dataset already records this "
        "row as email-method with compliance@fairscreen.com, which is the "
        "appropriate channel.  One detail worth keeping because it is the "
        "sort of thing that distinguishes a careful screener from a "
        "careless one: FSI states it is a member of Concerned CRAs, a "
        "group opposed to offshoring sensitive personal information for "
        "processing."
    ),
    "fifty-io": (
        "Verified 2026-09-23. The dataset's /opt-out serves the SAME page "
        "as the homepage: the only form on it is #book-demo-form, a sales "
        "enquiry posting to /assets/php/captcha.php (Full name, Company, "
        "Email, Phone, Message), reCAPTCHA-protected. There is no opt-out "
        "form on the opt-out page.  What the page does carry is an IAB "
        "TCF consent panel -- the [#IABV2SETTINGS#] placeholder and "
        "Necessary/Preferences/Statistics/Marketing toggles with vendor "
        "counts -- so 'opt out' here means cookie consent, per-browser, "
        "and not a request about data the broker holds. Recorded as no- "
        "surface rather than blocked because the captcha guards a demo "
        "form; there is nothing behind it that would serve a person "
        "exercising rights.  One detail worth keeping for the pattern "
        "collection: the demo form's honeypot is input[name='name2'] and "
        "its placeholder reads 'Paste your spam here' -- a honeypot that "
        "announces itself in plain English, which is the opposite of "
        "enformion's 'yourFavoriteNumber'. privacy@fifty.io is the "
        "address on file, and as a UK company its statutory channel is a "
        "UK GDPR request by email."
    ),
    "fourleafdata-com": (
        "Verified 2026-09-23 by rendering the site. fourleafdata.com is a "
        "two-item site -- a CONTACT link and a privacy policy -- and the "
        "policy page contains NO form element at all. There is no request "
        "page, no do-not-sell link, and the dataset records no opt-out "
        "email for this row, which makes the absence more consequential "
        "than usual.  The only opt-out routes the policy offers are the "
        "two industry cookie pages: "
        "networkadvertising.org/managing/opt_out and "
        "optout.aboutads.info. Neither is this broker's surface -- they "
        "suppress ad targeting across participating networks and do "
        "nothing to the data FourLeaf holds -- so pointing a person at "
        "them would be offering a remedy that does not address the "
        "complaint.  So a person exercising rights against FourLeaf has, "
        "on the public evidence, no published channel at all: no form, no "
        "address, no portal. That is itself worth recording rather than "
        "smoothing over."
    ),
    "reachdata-com": (
        "Verified 2026-09-23. reachdata.com has not launched. The site is "
        "a single placeholder page reading 'Coming Soon!!' over a pitch "
        "for a sales-and-recruiting contact-list product, with no form "
        "element on it beyond a menu toggle and a cookie Accept -- the "
        "mailing-list signup the copy invites is not even wired up.  The "
        "dataset records no opt-out URL, no opt-out email and an "
        "opt_out_method of 'unknown' for this row (Freemium Data "
        "Services, LLC), which is consistent: there is nothing to opt out "
        "of yet and nowhere to do it.  Recorded as no-surface rather than "
        "undecided because the state is unambiguous and self-described. "
        "It is worth a recheck if the dataset is ever refreshed, though "
        "-- a pre-launch broker is the one category that can turn into a "
        "live one without warning, which is the opposite of emerges-com "
        "elsewhere in this module, a broker that has shut down."
    ),
    "fushiamedia-com": (
        "Verified 2026-09-23 by rendering the site. Fuchsia Media -- the "
        "dataset spells the domain 'fushiamedia', which is the broker's "
        "own misspelling rather than a dataset error -- is a three-page "
        "brochure (Home, About, Contact) selling database enrichment, "
        "'over 600 demographic and behavioral attributes'.  There is no "
        "privacy policy page, no rights page, no do-not-sell link and no "
        "request form. The only form on the site is a generic contact box "
        "(name, email, phone, message, 'Send Message Now'), which is not "
        "an opt-out surface and is recorded here so it is not mistaken "
        "for one -- the same trap dresdendirect-com sets elsewhere in "
        "this module.  This matches the dataset, which records the row as "
        "email-method with support@fushiamedia.com. The finding is simply "
        "that the web side is empty rather than walled: for a company "
        "advertising 600 attributes per person, publishing no privacy "
        "policy at all is itself the notable part."
    ),
    "g2risksolutions-com": (
        "FLAGGED AS FCRA-ADJACENT, 2026-09-23, and distinct from every "
        "other FCRA row in this module -- which is why it gets its own "
        "entry rather than the screening category note.  G2 Bankruptcy "
        "Risk Solutions is not a consumer reporting agency. It is a "
        "FURNISHER to one, and it says so in its own words on the page "
        "the dataset points at (/consumer-center/, which redirects to "
        "/transunionconsumers/): 'Effective December 1, 2018, G2 "
        "Bankruptcy Risk Solutions became the furnisher of bankruptcy "
        "information to TransUnion ... G2BRS is a furnisher under the "
        "Fair Credit Reporting Act (as amended) and does not maintain or "
        "issue credit reports.'  That distinction has teeth for this "
        "tool. A CRA holds a file about you; a furnisher supplies items "
        "INTO someone else's file. There is nothing here to opt out of "
        "and no file here to request -- the page directs people to "
        "TransUnion for a report or a freeze, and to an email address for "
        "corrections to the bankruptcy information specifically. "
        "Consistent with that, the page carries no form element at all. "
        "So: no web surface, and correctly so. The FCRA dispute right "
        "against a furnisher is real and is the proper recourse for "
        "anyone whose bankruptcy data is wrong, but it is exercised by "
        "writing, not by a form. ramtin.taheri@g2risksolutions.com is the "
        "address the dataset records."
    ),
    "grayhairsoftware-com": (
        "Verified 2026-09-23, and recorded as no-surface DESPITE the site "
        "appearing to offer one -- which is the whole point of the entry. "
        "The privacy policy page presents a consent banner reading 'You "
        "can opt out of these cookies by checking 'Do Not Sell or Share "
        "My Personal Information' and clicking the 'Save My Preferences' "
        "button.' A checkbox with that exact CCPA wording, on a data "
        "broker's privacy page, looks precisely like the surface this "
        "tool is hunting for. It is not one. The banner's own text scopes "
        "it: 'This website or its THIRD-PARTY TOOLS process personal data "
        "... This website stores cookies on your computer.' Ticking it "
        "suppresses ad and analytics cookies in the visitor's browser. It "
        "does nothing to the mail-tracking data GrayHair holds, which is "
        "the reason the company is in this dataset.  Acting on it would "
        "be worse than doing nothing: the tool would report a completed "
        "opt-out while the broker's actual records were untouched. The "
        "consent-banner-is-not-an-opt-out heuristic applies, as it did "
        "for carneydirect-com in this same batch.  The genuine route is "
        "email only. The page's sole substantive link is "
        "mailto:privacy@grayhairsoftware.com, introduced as 'For personal "
        "data deletion requests, please e-mail'. There is no form element "
        "anywhere on the page. The dataset's opt_out_url points at a #Do- "
        "Not-Sell-My-Personal-Information anchor within the policy, which "
        "resolves to prose, not a control."
    ),
    "carneydirect-com": (
        "Verified 2026-09-23. Carney Direct's privacy policy is the "
        "dataset's opt_out_url and carries no request form. The only form "
        "elements on the page are two copies of the WordPress site "
        "search. The opt-out route is a mailto: the policy links 'DO NOT "
        "SELL MY PERSONAL INFORMATION' and 'Do Not Sell My Private "
        "Information' to mailto:privacy@carneydirect.com?Subject=Privacy% "
        "20Policy%20-%20Opt%20Out%20Request, which is a genuine channel "
        "but not a web surface.  THE TRAP HERE IS THE NAMING. The site "
        "has a page at /opt-out-preferences/ and links to it four times, "
        "under labels including 'Opt-out preferences', 'Manage options', "
        "'Manage services' and 'Manage {vendor_count} vendors'. On a data "
        "broker, a page called opt-out-preferences is exactly what this "
        "tool is looking for. It is not one: the cmplz- prefixes and the "
        "#cmplz-tcf-wrapper anchor identify it as Complianz, a WordPress "
        "COOKIE CONSENT plugin, and the vendor list is the IAB TCF ad "
        "framework. It governs tracking in the visitor's own browser and "
        "does nothing to the marketing database Carney Direct sells, "
        "which is the reason the company is in this dataset. Note the "
        "unreplaced {vendor_count} placeholder, incidental evidence that "
        "the widget is stock configuration.  Same call as "
        "grayhairsoftware-com in this batch: a consent banner is not an "
        "opt-out, and treating one as such would report success while the "
        "broker's records went untouched. info@carneydirect.com is the "
        "dataset contact; privacy@carneydirect.com is the better address."
    ),
    "gundir-com": (
        "Verified 2026-09-23. gundir.com/opt-out/ is a real page with a "
        "real heading -- 'Direct mail opt-out' -- and it contains NO opt- "
        "out. The only form on it is the WordPress site search. The "
        "page's entire substance is a paragraph declining to offer one: "
        "'Wait, what? You don't like mail? ... If you're determined to "
        "stop receiving mail solicitations, add your name to the national "
        "do not mail registry, and it will be deleted from future "
        "mailings on a national basis.' The link labelled 'Direct Mail "
        "Opt-Out' goes to consumer.ftc.gov/articles/how-stop-junk-mail -- "
        "the FTC's consumer advice page, on a domain Gundir does not "
        "control.  So the broker's published remedy is to ask a third- "
        "party registry to suppress mailings, which is not the same thing "
        "as Gundir deleting or ceasing to sell what it holds. Pointing a "
        "user here would offer a remedy that does not address the "
        "complaint -- the same reasoning already applied to the NAI and "
        "DAA cookie pages on fourleafdata-com.  Recorded as no-surface "
        "rather than undecided because the page is unambiguous and "
        "complete: there is nothing further to find. The dataset names "
        "jeff@gundir.com, a personal rather than a role address, which is "
        "the only direct channel this company publishes."
    ),
    "healthlinkdimensions-com": (
        "Verified 2026-09-23, and closed on a defect in the BROKER'S OWN "
        "PAGE rather than in the dataset.  /consumerprivacyrights is "
        "headed 'Exercise Your Consumer Data Privacy Rights' and "
        "instructs: 'please FOLLOW THE LINK BELOW to submit a Right to "
        "Opt-Out request or a Right to Know Request.' There is exactly "
        "one such link on the page, labelled 'Do Not Sell or Share My "
        "Personal Information', and it points to "
        "https://healthlinkdimensions.com/consumerprivacyrights -- THE "
        "PAGE ITSELF. Following the instruction returns the visitor to "
        "the instruction. The page contains no form element of any kind. "
        "So the published rights channel is a circular reference: a "
        "person doing exactly what the broker tells them to do arrives "
        "back where they started, with no indication anything went wrong. "
        "This is worth recording as an observed fact rather than smoothed "
        "over as 'no form found', because it is the difference between a "
        "company that offers no web route and one that appears to offer "
        "one and does not.  The page does carry the company's postal "
        "address (1001 Summit Blvd NE, Suite 1125, Atlanta, GA 30319) and "
        "phone (404.250.3900), and the dataset records "
        "nlenyszyn@healthlinkdimensions.com -- again a personal rather "
        "than a role address. Those are the only working channels. "
        "Recorded as no-surface because the absence is established, not "
        "merely unobserved; if the self-link is ever repointed at a real "
        "form, this row should be reopened."
    ),
    "harmonresearch-com": (
        "Verified 2026-09-23 by rendering the site. Harmon Research is a "
        "market research firm -- focus groups, in-depth interviews, "
        "online surveys, and a proprietary respondent panel it advertises "
        "as undergoing 'rigorous quality-control checks'. The site is a "
        "brochure: Home, About Us, Services, a 'NoBot Quality Program' "
        "page, Contact Us and a blog. There is no privacy policy page, no "
        "rights page, no do-not-sell link and no request form. The only "
        "link resembling a submission is 'Request a copy' of the panel "
        "book, a sales asset.  This matches the dataset, which records "
        "the row as email-method with info@harmonresearch.com -- a "
        "general enquiry address, not a privacy one.  Worth a note for "
        "anyone reviewing the category: a company running a standing "
        "PANEL is holding a roster of identified, consenting "
        "participants, which is a materially different relationship from "
        "a list broker's. Panel members usually have an account-based "
        "unsubscribe route that is invisible from the public site. So 'no "
        "public surface' here probably means 'the surface is behind a "
        "panellist login', and that is where a future pass should look "
        "before concluding the company offers nothing."
    ),
    "pickmedicare-com": (
        "Verified 2026-09-23 by rendering the site. pickmedicare.com is a "
        "Medicare lead-generation landing page and it contains NO FORM "
        "ELEMENT AT ALL -- not a request form, not a quote form, not even "
        "a newsletter box. Its entire call to action is a telephone "
        "number repeated down the page: 'Tap Here to Call 1-833-497-2821 "
        "... LICENSED INSURANCE AGENTS AVAILABLE ... TTY: 711 ... M-F 8AM "
        "- 5PM CT'.  That absence is the finding, and it is a purer "
        "example than most. The site's business is collecting Medicare- "
        "eligible people by PHONE, so there is no web form to opt out of "
        "and no web surface on which to offer one. Data enters this "
        "broker through a call centre, where the person is identified by "
        "voice and the record is created by an agent.  The dataset "
        "agrees, recording the row as email-method with "
        "info@pickmedicare.com. Recorded as no-surface rather than "
        "undecided because the page is short, complete and unambiguous -- "
        "there is nothing further on the site to find.  One practical "
        "observation for whoever surfaces this row to a user: the "
        "published phone line is staffed weekdays 8-5 Central and is "
        "answered by licensed agents, so unlike most no-surface rows this "
        "one does have a responsive channel, just not one this tool can "
        "drive."
    ),
    "hivestack-com": (
        "Verified 2026-09-23. /opt-out-california-residents/ contains NO "
        "form element. What it contains is a list of links to OTHER "
        "companies' opt-out pages: undertone.com/opt-out/, "
        "thenai.org/opt-out/, Google's analytics opt-out, "
        "optout.privacyrights.info, and perion.com/ccpa/ -- Perion being "
        "Hivestack's parent.  None of those is Hivestack's surface. The "
        "NAI and Google links suppress ad targeting across participating "
        "networks and do nothing to what Hivestack holds; the Undertone "
        "and Perion links belong to sibling companies. Offering any of "
        "them would be offering a remedy that does not address the "
        "complaint, the same reasoning already applied to fourleafdata- "
        "com and gundir-com.  A DEFECT ON THE PAGE ITSELF, worth "
        "recording: the link labelled 'Your Privacy Choices' -- the one a "
        "person would click first, since it is the statutory phrase -- "
        "has an EMPTY href. It goes nowhere. Clicking it does nothing at "
        "all and gives no error, so a visitor would reasonably assume the "
        "page was broken or that they had already opted out.  So the only "
        "route this company publishes for itself is "
        "privacy@hivestack.com, recorded in the dataset. Closed as no- "
        "surface because the absence is established rather than merely "
        "unobserved."
    ),
    "idengine-com": (
        "Verified 2026-09-23. idengine.com is a PARKED DOMAIN LISTED FOR "
        "SALE. The recorded opt-out path /dnsmpi/ redirects into "
        "GoDaddy's aftermarket and returns an Akamai 'Access Denied' for "
        "'http://forsale.godaddy.com/forsale/idengine.com'. There is no "
        "site behind the name.  This is the most complete form of absence "
        "in the module, and distinct from its neighbours: reachdata-com "
        "had not launched yet, emerges-com had shut down, granitelists- "
        "com is suspended and may return. A domain in a for-sale listing "
        "has been given up by its owner, and may shortly belong to "
        "someone entirely unrelated.  That last point is the reason this "
        "is worth more than one line. The dataset records NO opt-out "
        "email for this row, so the URL was the only channel -- and if "
        "the domain is bought, /dnsmpi/ could later resolve to a live "
        "page belonging to a different company. A tool that retried this "
        "row mechanically could then submit a person's name and address "
        "to a stranger. Any future recheck of parked-domain rows should "
        "confirm OWNERSHIP, not merely that a page has appeared."
    ),
    "backgroundchecks-com": (
        "FCRA CATEGORY, verified 2026-09-23, and folded into the shared "
        "background-screening treatment rather than given a recipe. "
        "backgroundchecks.com sells pre-employment screening reports to "
        "employers. The dataset's /privacy path returns a genuine HTTP "
        "404 ('This is a 404 error, meaning this link doesn't exist'), "
        "and the only frame on the page is a HubSpot chat widget. Flagged "
        "as a dataset defect; not fixed.  More to the point, the routes "
        "the site does offer are the FCRA ones, visible in its own "
        "navigation: 'Get a Copy of Your Background Report' and 'Dispute "
        "Background Report'. Those are the statutory file-disclosure and "
        "dispute rights against a consumer reporting agency, and they are "
        "not opt-outs. A CRA regulated under the FCRA cannot simply "
        "delete a person from its files on request the way a marketing "
        "list broker can -- which is why this module treats the whole "
        "category as no-surface with an explanation rather than as a "
        "broker refusing to cooperate.  The distinction to preserve for "
        "anyone reading this row: unlike g2risksolutions-com, which is a "
        "FURNISHER feeding data into TransUnion's files, "
        "backgroundchecks.com compiles and issues reports itself. Both "
        "are FCRA entities; only the latter holds a file a person can "
        "demand a copy of. support@backgroundchecks.com is published and "
        "is the right channel for a disclosure or dispute request."
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
    "liveramp-com": (
        "NO VERDICT as of 2026-09-23, but the next three URLs are now "
        "known exactly, which is the useful part. The dataset's "
        "opt_out_url (liveramp.com/opt_out/) redirects to "
        "liveramp.com/privacy, whose only form is a site search box; the "
        "opt-out itself is elsewhere and the page names where. Its text "
        "distinguishes two scopes: cookie-level choices, handled by the "
        "Ketch banner on the page (buttons #ketch-banner-button-primary / "
        "-secondary / -tertiary) and browser-specific, versus the broad "
        "one -- 'If you would like to broadly opt out of targeted "
        "advertising or the selling or sharing of personal data (called "
        "Do Not Sell or Share in California), please use the Opt Out "
        "Request form, available on our Your Privacy Choices page.' Three "
        "live links were captured and NOT followed: "
        "liveramp.com/privacy/my-privacy-choices (the Opt Out Request "
        "form, and the one that matters), liveramp.com/opt_out/mobile/ "
        "(mobile identifier opt-out) and "
        "optout.liveramp.com/opt_out_cookies (third-party cookie opt- "
        "out). Next pass: render my-privacy-choices. The dataset URL "
        "should be corrected to it."
    ),
    "nuwber-com": (
        "NO VERDICT as of 2026-09-23, and for an unusual reason worth "
        "recording rather than retrying blindly: nuwber.com would not "
        "RESOLVE. A headless Chromium navigation to the dataset's "
        "opt_out_url (nuwber.com/removal/link) failed with "
        "net::ERR_NAME_NOT_RESOLVED -- a DNS failure, not a timeout, a "
        "certificate problem or an anti-bot block, and distinct from the "
        "HTTP errors recorded on other rows here. One observation from "
        "one host on one day is not enough to call a large and "
        "previously-active people-search site dead, so this is undecided "
        "rather than no-surface. Next pass: resolve the name from a "
        "different network before concluding anything, and if it "
        "resolves, render /removal/link and transcribe. Both legs of this "
        "broker are unresolved for the same reason."
    ),
    "neighborwho-com": (
        "NO VERDICT as of 2026-09-23. The dataset's opt_out_url "
        "(neighborwho.com/remove) 301s to www.neighborwho.com/remove/ and "
        "returns HTTP 404, titled 'Page Not Found | NeighborWho Blog' -- "
        "so the recorded URL is dead and the row's opt-out page, if it "
        "still exists, is somewhere else. The 404 page is not empty: it "
        "carries the site chrome including an address-search box (input "
        "name=address, placeholder 'Enter an address') and Log In / Sign "
        "Up controls, so the service itself is alive. Next pass: find the "
        "live removal path from the site footer or its privacy policy, "
        "then transcribe. The dataset URL should be corrected once it is "
        "found."
    ),
    "bidr-io": (
        "NO VERDICT as of 2026-09-23: the page could not be reached at "
        "all, and the failure is specific enough to be worth recording. A "
        "headless Chromium navigation to the dataset's opt_out_url "
        "(optout.prod.bidr.io/optout) failed with "
        "net::ERR_CERT_COMMON_NAME_INVALID -- the TLS certificate served "
        "on that host does not cover that name, so no browser will load "
        "it without an explicit override, which this tool will not do. "
        "That is a misconfiguration on Beeswax's side rather than an "
        "anti-bot wall, and it means the opt-out is effectively "
        "unavailable to any ordinary consumer using an ordinary browser "
        "-- which is itself the finding. Next pass: recheck whether the "
        "certificate has been fixed; if it has not, this row arguably "
        "belongs under NO_OPTOUT_SURFACE, because an opt-out nobody can "
        "open is not an opt-out."
    ),
    "bombora-com": (
        "NO VERDICT as of 2026-09-23. The dataset's opt_out_url "
        "(bombora.com/opt-out/) returns a hard HTTP 404, titled 'Page not "
        "found - Bombora', with nothing on it but the site's own search "
        "box. The site is otherwise alive. So the recorded URL is dead "
        "and the real surface, if there is one, was not located. Next "
        "pass: read bombora.com's privacy policy for the current path -- "
        "Bombora is a B2B intent-data business, so the plausible outcomes "
        "are a rights form or a mailbox, and it is worth deciding which "
        "rather than leaving the dead URL in place. The dataset should be "
        "corrected either way."
    ),
    "biscred-com": (
        "A REAL form, rendered and transcribed 2026-09-23, held back "
        "because what was transcribed does not make sense yet. "
        "www.biscred.com/do-not-sell-my-information is a Wix (Editor X) "
        "page whose form comp-l5tpyw7t carries: input name=first-name / "
        "#input_comp-l5tpyw961 labelled 'First Name'; input name=last- "
        "name / #input_comp-lbzdz2gk labelled 'Last Name' (required); a "
        "SELECT #collection_comp-lbpjuop7, required, labelled 'Choose an "
        "option' -- which at render time contained exactly ONE option, so "
        "its real choices are populated by JavaScript that had not run or "
        "had nothing to fill it with; and, oddly, a TEXTAREA "
        "#textarea_comp-l5tpyw9x labelled 'Email' and required. A "
        "required single-option select and a textarea for an email "
        "address are both wrong-looking enough that a recipe written "
        "against them would probably submit garbage. No captcha script "
        "and no widget were found, which is the encouraging half. Next "
        "pass: open it interactively, let the select populate, and find "
        "out what it is asking. Note the visible copy promises a "
        "verification email ('You will receive an email to verify your "
        "address'), so a submission here is a request STARTED, not "
        "finished."
    ),
    "blis-com": (
        "NO VERDICT as of 2026-09-23, and the dataset URL is misleading "
        "rather than dead. blis.com/ccpa-opt-out/ renders as a "
        "California-rights EXPLAINER -- it lists the four CCPA rights in "
        "prose -- and the only <form> on the page is a Zoho Campaigns "
        "newsletter signup posting to dgie-zgfl.maillist- "
        "manage.com/weboptin.zc, which is emphatically not an opt-out. "
        "The page does carry an anchor reading 'Do Not Sell My Personal "
        "Information' whose href is EMPTY, i.e. a JavaScript handler, "
        "almost certainly opening the OneTrust preference centre whose "
        "ot-group-id checkboxes are also present in the DOM. Next pass: "
        "click that anchor in a browser and see what it opens; if it is "
        "only the cookie preference centre, then Blis offers cookie-scope "
        "choices and no broad opt-out form, and this row becomes a no- "
        "surface call."
    ),
    "blackpearl-com": (
        "A REAL form that exists but did not render, verified 2026-09-23. "
        "www.blackpearl.com/privacy-opt-out says in its own copy 'Thank "
        "you for visiting our privacy inquiry form. To make a request "
        "regarding your personal and/or company information, complete the "
        "form below' -- but the only <form> actually in the DOM is a "
        "Webflow newsletter Subscribe box (#wf-form-Subscribe, one email "
        "field). The real form is third-party and did not load: the "
        "page's own Cookiebot disclosure lists 'forms.blackpearl.com' as "
        "an embedded origin, so the inquiry form is served from that "
        "subdomain, presumably in an iframe that was blocked or deferred "
        "behind the consent banner. Two bot-check scripts are also "
        "present on the page, reCAPTCHA AND Cloudflare Turnstile, so even "
        "once it renders this may well be a blocked row. Next pass: "
        "accept the cookie banner, let forms.blackpearl.com load, and "
        "transcribe. The page also offers a 'print-at-home option' for a "
        "postal request."
    ),
    "audigent-com": (
        "NO VERDICT as of 2026-09-23. The dataset's opt_out_url is the "
        "homepage with a query string (audigent.com/?optout=1), and "
        "rendering it produces exactly the ordinary marketing homepage -- "
        "no form, no input of any kind anywhere on the page, and no sign "
        "that the ?optout=1 parameter does anything server-side. The "
        "single relevant anchor reads 'Your Privacy Choices' and its href "
        "points back at the same URL, so it is a JavaScript handler for a "
        "consent widget rather than a link to a form. Next pass: click "
        "that control in a browser and find out whether it opens a real "
        "rights form or only cookie toggles; the second outcome would "
        "make this a no-surface row. The dataset's URL should be "
        "corrected to whatever that resolves to."
    ),
    "thebridgecorp-com": (
        "A REAL, unwalled, fully transcribed form -- the best candidate "
        "in this batch -- held out of STAGED_RECIPES for one specific "
        "reason. Verified by browser render 2026-09-23: "
        "www.thebridgecorp.com/opt-out/ carries a form whose fields are "
        "i_am_a (a 3-option select: 'Please Select' / 'California "
        "Resident' / 'Non-California Resident', labelled 'State of "
        "Residency*'), email (required), enter_advertising_id_ (a "
        "textarea, optional), a checkbox group named please_select_the_ty "
        "pe_of_request_or_requests_you_wish_to_submit_ with values "
        "'Request Information', 'Delete My Record' and 'Opt Out', a "
        "required attestation checkbox (value 'true') reading 'By "
        "checking off this box, I hereby declare that the information "
        "submitted in this request is true...', a required text field "
        "write_your_name_below, and a bare button[type=submit] 'Submit'. "
        "NO captcha script and no widget were found. The blocker is "
        "selectors, not substance: every element id on the form is "
        "prefixed with a per-render hash (bcf_a1d7846f_...), and a SECOND "
        "form on the same page -- a blog subscription -- carries its own "
        "email field under a different hash (bcf_d6541a56_email). So the "
        "ids cannot be hardcoded and the submit button cannot be reached "
        "by '#id' either; a recipe must select on the stable NAME "
        "attributes and scope the submit through the form that contains a "
        "named field. That is a real design decision rather than a "
        "transcription gap, which is why this is undecided rather than "
        "staged. Everything else is ready."
    ),
    "big-village-com": (
        "A page that PROMISES a form and does not render one, verified "
        "2026-09-23. big-village.com/do-not-sell-or-share-my-personal- "
        "information/ says 'please submit your mobile advertising "
        "identifier to us using the form below', but the rendered DOM "
        "contains no <form> element at all and no inputs -- only the "
        "site's navigation buttons. So either the embed is broken or it "
        "is gated behind something that did not fire. The substantive "
        "finding matters more than the missing form, though: the only "
        "opt-out Big Village offers is keyed to a MOBILE ADVERTISING "
        "IDENTIFIER, and the page is emphatic -- 'Please enter your "
        "Mobile Advertising ID below. Do not enter your phone number. "
        "Submissions that do not include a valid MAID cannot be "
        "processed. A MAID is 36 characters long.' A MAID is a device "
        "identifier this tool's profile does not hold and cannot derive "
        "from a name and address, so even once the form renders this row "
        "probably belongs under NO_OPTOUT_SURFACE or OPTOUT_OUT_OF_SCOPE "
        "rather than RECIPES. Recorded rather than guessed. The scope is "
        "also narrow by the page's own account: it excludes you from "
        "audiences Big Village creates, nothing more."
    ),
    "datasubject-com": (
        "NO VERDICT as of 2026-09-23, and the row itself needs a second "
        "look. The dataset's opt_out_url is a tokenised per-tenant link "
        "(my.datasubject.com/16CV6iU2K7qFU3Poy/33263) which renders as a "
        "request portal branded 'Blue Action' -- not 'DataSubject' -- so "
        "the recorded URL is one CUSTOMER's form on a privacy-request "
        "vendor's platform, and the broker this row names may be the "
        "vendor rather than the data holder. Worth deciding before more "
        "effort goes into it. What rendered: a jurisdiction notice "
        "('We've automatically detected your jurisdiction... Nevada, US', "
        "behind an Osano location-select button) and a request-type "
        "chooser offering 'Correct my personal information', 'Summarize "
        "my personal information', 'Do Not Sell or Share to a Third "
        "Party' and 'Delete my personal information'. No <form> and no "
        "inputs exist at that point -- it is step one of a wizard, the "
        "fields appear after a type is chosen. If a later pass confirms "
        "the multi-step shape this belongs in OPTOUT_OUT_OF_SCOPE; it is "
        "recorded here because only the first screen was seen."
    ),
    "buyerlink-com": (
        "NO VERDICT as of 2026-09-23. Two dataset corrections first: the "
        "row's domain buyerlink.com redirects to buyerlink.CO, and the "
        "recorded opt_out_url redirects to the homepage. The real page "
        "does exist at www.buyerlink.co/do-not-sell-or-share-my-personal- "
        "information -- it was found from the site footer and rendered -- "
        "but it is nearly empty (672 characters of body text) and the "
        "only <form> on it is a Mailchimp newsletter subscribe box "
        "posting to buyerlink.us8.list-manage.com. So the page that is "
        "supposed to carry the opt-out carries no opt-out. Cloudflare "
        "Turnstile's api.js is loaded site-wide, which would matter if a "
        "form ever appears. Next pass: check whether the content is "
        "behind the consent banner or simply missing, and if it is "
        "missing, this is a no-surface call about a company that "
        "publishes a do-not-sell link with nothing behind it."
    ),
    "corelogic-com": (
        "NO VERDICT as of 2026-09-23, because the company has been "
        "rebranded out from under the dataset. www.corelogic.com/privacy/ "
        "now redirects to cotality.com and returns HTTP 404 at /not-found "
        "-- CoreLogic trades as COTALITY, and every path recorded for the "
        "old brand should be re-derived rather than patched. The 404 page "
        "renders only the site's search bars and a HubSpot newsletter "
        "form, so nothing about a rights surface is known. This matters "
        "more than a typical dead URL: CoreLogic is one of the larger "
        "property-data holders in the dataset, so the row is worth "
        "resolving properly. Next pass: find Cotality's privacy or "
        "consumer-rights page and transcribe from there. Both legs of "
        "this broker are unresolved."
    ),
    "blisspointmedia-com": (
        "NO VERDICT as of 2026-09-23, for a network reason rather than a "
        "research one: www.blisspointmedia.com failed to RESOLVE "
        "(net::ERR_NAME_NOT_RESOLVED) from a headless Chromium on this "
        "host, so no page was reached. This is the second row in the "
        "dataset to fail this way (see nuwber-com), and the two should be "
        "rechecked together from a different network before either is "
        "called dead -- a DNS failure is not a 404 and is not an anti-bot "
        "block. If it does turn out to be gone, note that Bliss Point "
        "Media was acquired and may now trade under another name, the "
        "same trap corelogic-com fell into."
    ),
    "biointelli-com": (
        "NO VERDICT as of 2026-09-23. The dataset's opt_out_url "
        "(biointelli.com/Account/PrivacyPolicy) returns HTTP 404, titled "
        "'Page not found - Biointelli'. The live site was rendered "
        "instead and carries no rights surface at all: the only form on "
        "it is a 'Request Demo' popup (POST to /send.php with first_name, "
        "last_name, email, phone and message). Biointelli describes "
        "itself as 'Scientific Signal Intelligence', mining grants, "
        "papers, patents and conference attendance to tell sales teams "
        "'who's about to buy' -- so it does hold personal data about "
        "named researchers, and the row is plausible, but no consumer- "
        "facing opt-out was found. Next pass: look for a privacy policy "
        "under the live site's own paths and decide between no-surface "
        "and a mailbox."
    ),
    "buxtonco-com": (
        "A REAL, unwalled, COMPLETELY transcribed form, held back by a "
        "gap in this codebase rather than a gap in the research. Verified "
        "by browser render 2026-09-23: the dataset's opt_out_url "
        "(buxtonco.com/privacy/opt-out) redirects to "
        "www.audiense.com/legal/privacy-opt-out/, itself worth flagging, "
        "and that page carries #consumer-request-form, POST to "
        "https://privacy.buxtonco.com/privacy. Fields, all addressable by "
        "NAME (most carry no id): FirstName, AlternativeFirstName, "
        "MiddleInitial, LastName, Suffix, PrimaryEmail, PrimaryPhone "
        "(placeholder XXX-XXX-XXXX), Address, Address2, City, "
        "PersonalState (a 51-option select of two-letter CODES -- AL, AK, "
        "AR, AZ, CA... -- so state_code, not state), Zip, "
        "MobileAdvertisingId, and a required select-MULTIPLE named "
        "Company / #Company offering exactly two options, 'Buxton' and "
        "'Elevar'. Submit is #btnsubmit. Required: FirstName, LastName, "
        "PrimaryEmail, PrimaryPhone, Address, City, Zip, Company. NO "
        "captcha sits on this form -- the reCAPTCHA Enterprise on the "
        "page belongs to a separate HubSpot newsletter form, whose onload "
        "is hsRecaptcha. THE BLOCKER IS THE MULTI-SELECT. An honest opt- "
        "out here has to name BOTH companies, because choosing only "
        "Buxton leaves Elevar holding the data -- and FormRecipe has no "
        "step type that selects more than one option: Field kind='select' "
        "picks a single option by label, and Select/Choice pick a fixed "
        "single literal. So this cannot be staged without either adding a "
        "multi-select step type or shipping a knowingly partial request, "
        "and the second is not acceptable. SECOND mechanism note, for "
        "whoever implements it: posting to privacy.buxtonco.com/privacy "
        "directly returns HTTP 400 with {'errors':{'v':['The "
        "verificationGUID field is required.']}}, so the hosting page "
        "mints a per-load verificationGUID and this is browser-only, "
        "never a canned POST."
    ),
    "experian-com": (
        "Verified by browser render 2026-09-23: the dataset opt_out_url "
        "www.experian.com/privacy/opting_out is a rights EXPLAINER, not a "
        "request surface -- the only form on the rendered page is the "
        "site-wide business search (input[name=q]). An invisible "
        "reCAPTCHA is live on it anyway (recaptcha__en.js plus a "
        ".grecaptcha-badge and a g-recaptcha-response textarea outside "
        "any form), which is the second time this sweep that a static "
        "fetch would have reported a captcha-free page. Experian also "
        "runs several DIFFERENT consumer channels that are easy to "
        "confuse and were not resolved here: the FCRA prescreen opt-out "
        "(optoutprescreen.com, itself blocked -- see its own entry), a "
        "marketing-mail opt-out, and a CCPA/state-rights portal. A future "
        "researcher needs to establish WHICH surface actually suppresses "
        "Experian Marketing Services data (including the acquired Tapad "
        "identity graph, which the dataset notes were merged into this "
        "row) and whether it can be reached without an identity-verified "
        "login, since the credit-file side certainly cannot. Second "
        "channel for a human: privacy@experian.com."
    ),
    "transunion-com": (
        "Verified by browser render 2026-09-23: same finding as experian- "
        "com. www.transunion.com/consumer-privacy renders an FAQ "
        "accordion ('How do I make a data privacy request?', 'Where can I "
        "learn about my consumer rights?') and no request form -- the "
        "only two forms are copies of the header site search. reCAPTCHA "
        "v3 is loaded on the page "
        "(api.js?render=6LfUswssAAAAAEy6MG6LCW72Avmkx2Yohnv2oQfY plus a "
        "recaptcha-cloudservice element), so whatever the accordion links "
        "out to is captcha-backed. Not resolved: which TransUnion "
        "property accepts a marketing-data suppression as opposed to a "
        "credit-file request, and whether the acquired Neustar identity- "
        "graph data the dataset notes mention is covered by the same "
        "request or needs a separate one. Second channel for a human: "
        "privacy@transunion.com."
    ),
    "hightouch-com": (
        "Verified by browser render 2026-09-23: preferences.hightouch.com "
        "is a DataGrail Privacy Request Center that GATES its form behind "
        "two pickers before any request fields exist. The rendered page "
        "contains exactly four controls and no <form> at all: #privacy- "
        "request-center-country-picker and #privacy-request-center- "
        "region-picker (MUI Autocomplete text inputs, each with a "
        "keyboard_arrow_down toggle button), pre-filled from geolocation "
        "as United States / Nevada. No captcha script and no captcha "
        "widget at this stage -- which says nothing about the stage after "
        "it, per the standing rule that a bot check absent before the "
        "form renders is not a bot check absent. What a future recipe- "
        "writer needs: drive both Autocompletes (they are listbox_button- "
        "style, not <select>), record the request-type choices and field "
        "set that appear afterwards, and re-check for a captcha THEN. The "
        "dataset notes are consistent with this being the real surface: "
        "legal@hightouch.com auto-replies requiring identity verification "
        "through this portal."
    ),
    "choreograph-com": (
        "Verified by browser render 2026-09-23: same gated shape as "
        "hightouch-com, differently built. amer- "
        "cpp.choreograph.com/manage-your-data/do-not-sell renders WPP's "
        "own consumer privacy portal, whose entire body is 'Please select "
        "your country of residence to continue.' and three react-select "
        "widgets (#react-select-2-input, -3-, -5-, all css-1hac4vs- "
        "dummyInput and all computed-invisible, which is normal for "
        "react-select rather than a honeypot signal). No <form> exists "
        "until a country is chosen. Page footer reads 'region: amer "
        "version: main-9.3.5', so there are sibling regional portals. No "
        "captcha at this stage. A future recipe-writer must drive the "
        "react-select by clicking the control and choosing from the "
        "rendered listbox -- typing into the dummy input will not do it "
        "-- then transcribe the form that follows and re-check for a "
        "captcha there. Second channel for a human: "
        "adam.little@choreograph.com."
    ),
    "catalyzeai-com": (
        "DATASET DEFECT, verified 2026-09-23: the row's opt_out_url "
        "https://www.catalyzeai.com/opt-out returns HTTP 404 ('Page not "
        "found. The page you are looking for doesn't exist or has been "
        "moved.'), and the dataset's own notes already record that the "
        "broker's email bounced on 2026-08-20. That leaves this row with "
        "NO working contact of any kind. Not fixed in data/source- "
        "brokers.json, only recorded here. What is left to try: find a "
        "current privacy policy on catalyzeai.com and read the rights "
        "section off it, or establish that the company has folded or been "
        "absorbed -- either answer resolves the row, and neither was "
        "established today."
    ),
    "civisanalytics-com": (
        "DATASET DEFECT, verified 2026-09-23: the row's opt_out_url "
        "https://www.civisanalytics.com/privacy-policy/supplemental- "
        "privacy-notice/ returns HTTP 404 ('The page you are looking for "
        "doesn't exist'), serving only a cookie banner. This is a "
        "circular dead end, because the dataset notes record that "
        "dataprotection@civisanalytics.com replied that they cannot "
        "process requests until THIS form is completed -- the form they "
        "point at no longer exists. Not fixed in data/source- "
        "brokers.json, only recorded here. Next step for a researcher: "
        "locate the supplemental notice at its current path under "
        "civisanalytics.com/privacy-policy and quote the real request "
        "surface back to that mailbox."
    ),
    "checkpeople-com": (
        "Verified by browser render 2026-09-23: the opt-out is real and "
        "reachable but only its FIRST step is knowable without "
        "submitting. checkpeople.com/opt-out renders a 'Suppression "
        "Center' whose form.cp-auto-optout__form POSTs to /opt-out and "
        "contains exactly one visible field, input#requestorEmail "
        "(name=requestorEmail, type=email, required), a consent checkbox "
        "#acknowledge that computes invisible (it has no name attribute "
        "and is styled behind a custom control, so it is a styled "
        "checkbox rather than a honeypot -- its label is the Terms of "
        "Service / transactional-email consent), and a submit button "
        "reading Continue. The page states the gate in its own words: "
        "'Upon submission of your email address you will receive a "
        "verification email with a link to proceed.' So the actual "
        "removal form lives behind a one-time emailed link, the same "
        "shape already recorded for fastpeoplesearch-com. No captcha "
        "script or widget on the first step. UNDECIDED rather than out- "
        "of-scope because, unlike fastpeoplesearch, nothing here has "
        "established what is behind that link -- it may be an ordinary "
        "form a recipe could complete once a human has clicked through. A "
        "human with a real mailbox could settle it in one pass. Note also "
        "the footer carries a separate 'Do Not Sell or Share my Personal "
        "Information' link, which was not followed. Second channel: "
        "operations@checkpeople.com."
    ),
    "sterlingcheck-com": (
        "Verified by browser render 2026-09-23: this row has been "
        "absorbed by another company and the trail stops in a cookie "
        "banner. sterlingcheck.com announces 'Sterling Background Check "
        "Solutions is now First Advantage', its footer 'Privacy Policy' "
        "and 'Privacy Center' both point at privacy.sterlingcheck.com, "
        "and THAT redirects to fadv.com/privacy-center/ -- a First "
        "Advantage page whose entire control set is two OneTrust cookie- "
        "preference buttons and their close buttons. No form, no request "
        "link, no rights text surfaced at that URL. Probable duplicate of "
        "a First Advantage row in the dataset. What a future researcher "
        "needs to do: find the actual consumer request entrance under "
        "fadv.com (the homepage also links fadv.com/legal-privacy- "
        "guarantee/), and decide whether Sterling and First Advantage "
        "should be one row. Dataset contact meanwhile: "
        "privacy@sterlingcheck.com."
    ),
    "clay-com": (
        "Verified by browser render 2026-09-23: the dataset URL is "
        "genuine and correct -- a Google Form titled 'Do Not Sell or "
        "Share My Personal Information' whose own text says 'Fill out "
        "this form if you'd like to exercise your rights under CCPA to "
        "stop the sale of your personal information'. It is form#mG61Hd "
        "posting to docs.google.com, with three controls: a required "
        "'Your email', a second required text input and a third optional "
        "one. WHY NO RECIPE YET: Google Forms controls carry NO name and "
        "NO id -- the entry name is on a sibling hidden input "
        "(entry.NNNNNN) and the human-readable label is reached only "
        "through aria-labelledby into a heading element. The two text "
        "fields therefore could not be identified from the control "
        "attributes alone, and guessing which is which would be exactly "
        "the fabrication this module forbids. What a future recipe-writer "
        "needs: read the aria-labelledby targets to learn what the two "
        "text fields ask for, then address them by their listitem "
        "position or by the resolved label. No captcha (Google Forms does "
        "not use one here). Second channel for a human: privacy@clay.com."
    ),
    "saymine-io": (
        "Verified 2026-09-23, and the finding is mostly about the "
        "dataset. The row's URL, cognism.privacy.saymine.io/cognism, "
        "renders a page titled 'Privacy Center' with a body length of "
        "ZERO after a full page settle -- a client-side app that had not "
        "built anything by the time it was read. More importantly the row "
        "looks miskeyed: saymine.io belongs to Mine, a privacy-request "
        "SERVICE, and this URL is Mine's hosted privacy centre for a "
        "different company, COGNISM, which is the actual broker (B2B "
        "contact data). Flagged, not fixed in data/source-brokers.json. "
        "Two things to do next: re-render with a longer settle or a "
        "headed browser to see whether a request form appears, and decide "
        "whether this row should be re-keyed to cognism.com. Dataset "
        "contact, which is consistent with the miskeying: "
        "legal@cognism.com."
    ),
    "ice-com": (
        "Verified by browser render 2026-09-23: the dataset URL is the "
        "corporate homepage of Intercontinental Exchange and has no opt- "
        "out on it. www.ice.com/index carries exactly two forms -- the "
        "site search (input[name=q]) and an email subscription centre "
        "posting to /api/subscription-center/email, which is a newsletter "
        "signup, not a suppression request -- and reCAPTCHA is loaded on "
        "the page and wired to that subscription form. Consumer data at "
        "this company sits with ICE MORTGAGE TECHNOLOGY (the former Ellie "
        "Mae), which is what the dataset's contact "
        "compliancemortgagetech@ice.com points at, and that is a lender- "
        "facing product whose records reach a consumer through their "
        "lender. What a future researcher needs to establish: whether ICE "
        "Mortgage Technology publishes its own consumer privacy request "
        "surface under icemortgagetechnology.com, and whether a "
        "suppression there is even possible given the data arrives under "
        "a lender's contract."
    ),
    "bridgevine-com": (
        "Reachability failure, 2026-09-23 -- same finding as the search "
        "leg. https://bridgevine.com/ does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED from a real browser), so no opt-out "
        "page could be reached. Recorded as undecided rather than absent "
        "because one network's DNS failure is not proof of a dead "
        "company; recheck from elsewhere. Dataset contact "
        "dwayne.landry@bridgevine.com is a personal address, not a "
        "privacy alias, and would be worth verifying before anyone relies "
        "on it."
    ),
    "brightswipe-com": (
        "Reachability failure, 2026-09-23 -- same finding as the search "
        "leg. https://brightswipe.com/ does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED). Recheck from another network "
        "before concluding the domain is retired. Dataset contact: "
        "admin@brightswipe.com."
    ),
    "carmarketsolutions-com": (
        "Reachability failure, 2026-09-23 -- same finding as the search "
        "leg, and a different failure mode from the two "
        "ERR_NAME_NOT_RESOLVED rows beside it: "
        "https://carmarketsolutions.com/ answers DNS but never completes "
        "a page load, timing out at 30s before DOMContentLoaded. That "
        "points at a hung or firewalled host rather than a retired "
        "domain, so it is worth a retry later and from another network. "
        "The dataset records no email for this row, so it presently has "
        "no working channel of any kind."
    ),
    "complementics-com": (
        "Verified by browser render 2026-09-23: a complete, captcha-free "
        "form that this codebase still cannot honestly fill -- a CODEBASE "
        "gap, not a research gap, the same distinction already recorded "
        "for buxtonco-com. complementics.com/opt-out is form#opt-out-form "
        "with exactly two required fields, input#MAID ('Device ID "
        "(MAID)') and input#email, plus a submit input. No captcha "
        "script, no captcha widget, no honeypot. But the identity record "
        "in this repo has no Mobile Advertising ID and never will from a "
        "name and address, so filling it would mean inventing a device "
        "id. Second finding: the dataset's opt_out_url for this row "
        "(/optout-donotsell) is the CCPA policy ADDENDUM, a prose page "
        "with no inputs, and the real form is one link further on at "
        "/opt-out -- the rights-explainer trap again. Note there are two "
        "distinct surfaces here, a device opt-out (this one) and a 'Do "
        "Not Sell My Information' notice; a human would want both. "
        "Contact: hello@complementics.com."
    ),
    "comscore-com": (
        "Verified by browser render 2026-09-23: the dataset URL is the "
        "right page and carries no form. /About/Privacy/Data-Subject- "
        "Rights explains rights and offers a 'Do not sell my personal "
        "information' link that points back at the same page -- a "
        "JavaScript-driven widget that never rendered a control -- while "
        "the only actual form on the page is the site content search "
        "(input[name=keyword]). The other route the page offers is a "
        "popup 'Contact Us' at /layout/set/popup/Request/Contact/Contact- "
        "Us, which was not opened. Two things left to establish: whether "
        "that popup is a real DSR intake, and whether the do-not-sell "
        "control appears after interaction. Note Comscore also operates "
        "PROXIMIC, advertised in its own navigation, which may be a "
        "separate row for the same data. Dataset contact: "
        "privacy@comscore.com."
    ),
    "convergemarketing-com": (
        "Verified by browser render 2026-09-23: the dataset URL "
        "(convergemarketing.com/infoform/?rpgn=&rpurl=) redirects to a "
        "hosted privacy portal at my.datasubject.com/FMy59nP1cQ/53694 "
        "titled 'Data Access Request', which gates on jurisdiction before "
        "showing a form: it auto-detects location ('We've automatically "
        "detected your jurisdiction ... please verify that this is "
        "accurate', showing Nevada, US) and then offers request types, of "
        "which the visible one is 'Don't use my personal information for "
        "advertising'. No form controls existed at that first stage, and "
        "no captcha was loaded yet -- which per the standing rule says "
        "nothing about the stage after it. What a future recipe-writer "
        "needs: confirm the jurisdiction, record the full request-type "
        "list and the field set behind it, and re-check for a captcha "
        "THEN. Note the dataset's contact for this row is "
        "contracts@convergedirect.com, a different brand name (Converge "
        "Direct) for the same company."
    ),
    "cicreports-com": (
        "Verified by browser render 2026-09-23: cicreports.com has no "
        "reachable privacy or opt-out page -- /privacy-policy/ 404s and "
        "the homepage exposes no opt-out link at all, only 'MY REPORT' "
        "and 'CONSUMER ASSISTANCE' entrances that lead to authenticated "
        "FCRA channels. The site is mid-acquisition ('Asurint Enhances "
        "Powerful Data Asset with AMCP's Acquisition of CIC'), which is "
        "the likely reason the legal pages are missing. Two things to do "
        "next: find the live privacy notice under cicreports.com or "
        "asurint.com, and decide whether this row and an Asurint row are "
        "the same company. As a tenant- and employment-screening CRA the "
        "answer may well end up being an FCRA dispute channel rather than "
        "a suppression form, but that has not been established. Dataset "
        "contact: compliance@cicreports.com."
    ),
    "universalcis-com": (
        "Verified by browser render 2026-09-23: universalcis.com no "
        "longer exists as its own site: the domain redirects wholesale to "
        "xactus.com, the brand it was folded into. Nothing resembling an "
        "opt-out is linked from that homepage -- the only links are "
        "'Contact Us', 'Request a Demo' and a customer sign-in. So this "
        "row currently has no surface, but it is undecided rather than "
        "absent because the successor brand's privacy pages were not "
        "enumerated. Next step: look for a consumer request surface under "
        "xactus.com and decide whether this row should be merged into an "
        "Xactus row. Dataset contact ccasey@universalcredit.com is on the "
        "retired brand's domain and should be doubted."
    ),
    "contentgine-com": (
        "Verified by browser render 2026-09-23: contentgine.com now "
        "serves nothing but a rebrand splash -- 'CONTENTgine is now "
        "pharosIQ', a CONTINUE button and marketing@pharosiq.com -- so "
        "there is no opt-out page on the recorded domain at all. "
        "Undecided rather than absent because the successor, "
        "pharosiq.com, was not checked, and a B2B lead-generation company "
        "of this kind usually does publish a do-not-sell page. Next step: "
        "enumerate pharosiq.com and re-key this row. Dataset contact "
        "paul@contentgine.com is a personal address on the retired "
        "domain."
    ),
    "completemailinglists-com": (
        "DATASET DEFECT, verified 2026-09-23: the row's opt_out_url "
        "https://www.completemailinglists.com/node/3697 returns HTTP 404. "
        "What is behind it is worth recording: the 404 page is a half- "
        "finished template whose navigation still reads 'Menu Item One / "
        "Menu Item Two / Menu Item Three', so the site appears to have "
        "been rebuilt without its rights pages being carried across. Its "
        "sibling completemedicallists.com DOES publish a working CCPA "
        "form at /ccpa.php, so the obvious next step is to check whether "
        "completemailinglists.com serves the same /ccpa.php form -- if it "
        "does, this row resolves immediately. Not fixed in data/source- "
        "brokers.json. Dataset contact: ewoolf@completemailinglists.com."
    ),
    "connextdigital-com": (
        "Verified 2026-09-23: the recorded opt_out_url 404s and the "
        "domain itself answers 'This site is currently unavailable' -- "
        "the company's web presence is down rather than merely missing a "
        "page. Undecided rather than no-surface because an unavailable "
        "site is not an established absence; recheck later. Note a "
        "curiosity worth not misreading: reCAPTCHA scripts load even on "
        "the unavailable page, which is a leftover of the site's "
        "WordPress stack and not a bot wall in front of an opt-out. The "
        "dataset records no email for this row, so it has no working "
        "channel at present."
    ),
    "consider-com": (
        "Verified by browser render 2026-09-23: consider.com is a "
        "marketing site with no form of any kind on it and no privacy or "
        "opt-out link in its navigation or footer. Undecided rather than "
        "no-surface because nothing was established about where its "
        "rights requests go -- a talent-intelligence platform holding "
        "candidate profiles almost certainly publishes a privacy policy "
        "somewhere, and it was not found from the homepage. Next step: "
        "locate the policy (try /privacy, /legal, or the Help section) "
        "and read its rights channel. Dataset contact ralph@consider.com "
        "is a personal address rather than a privacy alias and is worth "
        "doubting."
    ),
    "calltruth-com": (
        "Reachability failure, 2026-09-23 -- same finding as the search "
        "leg. https://www.calltruth.com/opt_out.php does not resolve "
        "(net::ERR_NAME_NOT_RESOLVED), so no opt-out page could be "
        "reached. Recheck from another network before concluding the "
        "domain is retired; note the URL shape (/opt_out.php) suggests "
        "the surface did once exist. The dataset records no email for "
        "this row, so it has no working channel at all."
    ),
    "crunchbase-com": (
        "Verified by browser render 2026-09-23: "
        "preferences.crunchbase.com/form/opt_out is a DataGrail Privacy "
        "Request Center that REFUSED this visitor before showing a form: "
        "'Unsupported Location Detected -- The location we have detected "
        "does not support the current legislative right you are trying to "
        "submit a request for.' So the surface exists, is correctly typed "
        "(the URL is /form/opt_out), and is gated on geolocated "
        "jurisdiction. Undecided rather than blocked because this is a "
        "policy gate, not an anti-bot wall, and it may simply open for a "
        "supported state. What a future researcher needs: return the "
        "location picker to a supported jurisdiction, record the field "
        "set and request types behind it, and re-check for a captcha at "
        "that stage -- DataGrail portals elsewhere in this dataset load "
        "one only after the gate. Dataset contact: "
        "privacy@crunchbase.com."
    ),
    "cuebiq-com": (
        "Verified by browser render 2026-09-23: the dataset URL "
        "(cuebiq.com/privacypolicy/) redirects to /privacy-policy/ and "
        "carries NO rights-request form -- the only Gravity Form on the "
        "page, #gform_6, is a 'Request Live Demo' sales form (email, "
        "first, last, title, phone, company), which would be easy to "
        "mistake for a request form and is not one. An invisible "
        "reCAPTCHA v3 is live on the page (the "
        ".gf_invisible.ginput_recaptchav3 wrapper and a "
        "#gfield_recaptcha_response input), attached to that demo form. "
        "Cuebiq sells mobile LOCATION data keyed to device advertising "
        "ids, so the likely shape of any real surface is a MAID-based "
        "opt-out like complementics and datafy in this same sweep -- "
        "which this codebase could not fill anyway -- but that has not "
        "been established. Next step: read the policy's rights section "
        "for the actual channel. Dataset contact: privacy@cuebiq.com."
    ),
    "datadecisionsgroup-com": (
        "Verified by browser render 2026-09-23: "
        "datadecisionsgroup.com/privacy-policy/ carries two Gravity Forms "
        "and NEITHER is a rights request: #gform_3 is a newsletter "
        "subscribe (first, last, email, submit labelled 'Subscribe') and "
        "#gform_7 is a lead form (phone, first, last, email, submit "
        "labelled 'Talk to an Expert'). Recording them explicitly because "
        "a careless reader would see 'Gravity Form on the privacy policy "
        "page' and write a recipe against a marketing signup. No captcha "
        "was loaded on the page. The only other privacy affordance is a "
        "cookie-settings widget. Next step: find whether the policy names "
        "a separate request URL or is mailbox-only. Dataset contact: "
        "privacy@datadecisionsgroup.com."
    ),
    "datadelivers-com": (
        "DATASET DEFECT, verified 2026-09-23: the row's opt_out_url "
        "https://datadelivers.com/unsubscribe/ returns HTTP 404 ('Page "
        "not found'), leaving only the site's WordPress search form. Not "
        "fixed in data/source-brokers.json. Worth noting the URL shape: "
        "/unsubscribe/ suggests the recorded surface was an email "
        "unsubscribe rather than a data suppression, so even if it were "
        "restored it may be the wrong request type -- the trap already "
        "documented on several consent-portal rows. Next step: look for a "
        "privacy or do-not-sell page under datadelivers.com and establish "
        "which request types it accepts. Dataset contact: "
        "supplier@datadelivers.com, which is addressed to data SUPPLIERS "
        "rather than consumers and is itself suspect."
    ),
    "datalinedata-com": (
        "DATASET DEFECT, verified 2026-09-23: the row's opt_out_url "
        "https://datalinedata.com/privacy-portal/ returns HTTP 404, so "
        "the recorded surface is gone. One genuinely useful detail came "
        "out of the render anyway: the site's reCAPTCHA Enterprise is "
        "BROKEN -- its challenge frame reports 'This site is exceeding "
        "reCAPTCHA Enterprise free quota' -- which means any form on this "
        "domain may be unsubmittable for everyone right now, not just for "
        "this tool. Anyone returning here should check that before "
        "concluding a form is walled against them specifically. Next "
        "step: find the live privacy portal (the footer offers only "
        "'Request a Demo'). Dataset contact: psobel@datalinedata.com, a "
        "personal address."
    ),
    "businesswatchnetwork-com": (
        "Verified by browser render 2026-09-23: businesswatchnetwork.com "
        "exposes no privacy or opt-out page from its homepage and carries "
        "no rights form -- its forms are a site search, a newsletter "
        "subscribe (user[email] -> /user/new) and an off-layout Yotpo "
        "review widget. The business is a B2B webinar and whitepaper "
        "publisher that collects registrant details for sponsors, so the "
        "plausible channel is an email unsubscribe rather than a data "
        "suppression, but nothing was established. Next step: look for "
        "/privacy or a footer legal page. Note the dataset's contact for "
        "this row is support@bizwatchnetwork.com -- a DIFFERENT domain "
        "from the row's own businesswatchnetwork.com, which is worth "
        "verifying before relying on it."
    ),
    "datamasters-org": (
        "Verified by browser render 2026-09-23: the page promises an opt- "
        "out form and does not appear to serve one, which is the whole "
        "finding. datamasters.org/opt-out/ says 'DataMasters has two easy "
        "ways the consumers can opt out ... Option 1: Fill in your "
        "information and submit the form below to be opted out from any "
        "future Direct Mail, Telephone or Email Marketing Communications. "
        "Option 2: call our Dedicated Opt Out Line (469) 882-2000'. But "
        "the ONLY form on the page besides the menu search is "
        "#form_contact2, a Formidable SALES form: its fields are Name, "
        "Last, Email, Phone Number, Website, 'Do you know who your target "
        "market is' (Automotive / Consumer / Business / Medical Data), "
        "'What type of data are you interested in' (Direct mailing list / "
        "Email marketing list), a Message textarea and a submit button "
        "reading GET A QUOTE. Either the opt-out form has been replaced "
        "by the quote form by mistake, or Option 1 means typing the "
        "request into that Message box. A recipe must not guess between "
        "those. Note it does carry a honeypot "
        "(input[name='item_meta[86]'], computed-invisible) and a "
        "reCAPTCHA, so it is walled as well as ambiguous. The phone line "
        "is the channel that plainly works. Dataset contact "
        "sales@datamasters.org is a sales alias."
    ),
    "datapartners-com": (
        "Verified by browser render 2026-09-23: the recorded URL is a "
        "correct hub page rather than a form. "
        "datapartners.com/donotsellmyinformation/ ('Manage Your Privacy "
        "Preferences') carries no inputs and instead links out to three "
        "separate request pages: /opt-out-request/, /delete-my- "
        "information-request/ and /information-access-request/. The first "
        "of those is the one this tool would want and it was not opened. "
        "Next step is exactly that: render /opt-out-request/, transcribe "
        "it and check for a captcha there -- nothing about the hub page "
        "predicts what the request page carries. Dataset contact "
        "info@datapartners.com is a general alias."
    ),
    "datasys-com": (
        "Verified by browser render 2026-09-23: the privacy policy at "
        "datasys.com/privacy-policy does not itself take requests; it "
        "links repeatedly to 'Your Privacy Choices' at "
        "datasys.com/privacy/my-privacy-choices, which was not opened, "
        "alongside the usual third-party ad-tech opt-out links "
        "(optout.aboutads.info). So the real surface is one hop away and "
        "identified. Next step: render /privacy/my-privacy-choices, "
        "transcribe the form and check for a captcha. Dataset contact: "
        "privacy@datasys.com."
    ),
    "deeprootanalytics-com": (
        "Verified by browser render 2026-09-23: "
        "privacy.deeprootanalytics.com is a genuine privacy centre -- it "
        "offers 'Access your data' ('We will provide you a report of all "
        "your personal data') and 'Delete your data' -- but the landing "
        "page carries no form and no controls at all; the request flow is "
        "behind whichever tile a visitor picks. No captcha at this stage, "
        "which per the standing rule says nothing about the stage after "
        "it. Next step: click through the Delete (and any suppression) "
        "tile, transcribe the field set and re-check for a captcha there. "
        "The dataset records no email for this row, so this portal is the "
        "only known channel."
    ),
    "diablomedia-com": (
        "Verified by browser render 2026-09-23: the form is EMBEDDED and "
        "did not populate in time, which the new frame-walking made "
        "visible rather than hiding. diablomedia.com/privacy-request/ "
        "('Looking to Opt-Out? ... Please use this form') has zero forms "
        "in its main frame; a child iframe points at "
        "my.datasubject.com/aSCwO6P3vw/60584 -- the same hosted-portal "
        "vendor already recorded for convergemarketing-com, which gates "
        "on jurisdiction before rendering anything. Next step: drive the "
        "jurisdiction gate, record the request types and field set, and "
        "check for a captcha THEN. Note the page's own framing is narrow "
        "-- 'Would you like to opt-out of future mailings?' -- so a "
        "future writer should establish whether this suppresses data or "
        "only mailings. Dataset contact: data@diablomedia.com."
    ),
    "decide-co": (
        "Verified by browser render 2026-09-23: decide.co/privacy carries "
        "a small in-page form (#marketingForm, POST to the same page) "
        "whose select offers only 'Request Account Deletion' and 'Request "
        "Account Information' -- both scoped to an ACCOUNT rather than to "
        "a person's data, which is the wrong request type for a broker "
        "suppression -- plus an Email field and a computed-invisible "
        "'Name' input that is almost certainly a honeypot. There is also "
        "a .recaptcha-signature element on the page with no captcha "
        "script loaded, so what that element does is unresolved. "
        "Undecided because the surface exists but does not offer the "
        "request this tool needs, and because whether Decide holds non- "
        "account data about non-users was not established. Note the "
        "corporate history on the page: Decide Technologies Inc. is 'fka "
        "LockerDome, Inc.', so a LockerDome row would be the same "
        "company. Dataset contact: privacy@decide.co."
    ),
    "demandscience-com": (
        "Verified by browser render 2026-09-23: "
        "demandscience.com/privacy-policy-ccpa/ redirects to the general "
        "/privacy-policy/, which carries no rights-request form -- its "
        "only forms are two copies of a knowledge-base search and a "
        "Pardot newsletter signup (posting to "
        "b2bleadgen.demandscience.com) that a careless reader could "
        "mistake for a request form. Recording that explicitly for the "
        "same reason as cuebiq and datadecisionsgroup in the previous "
        "batch. The company is Demand Science Group, LLC selling B2B "
        "demand generation, so it certainly holds person-level contact "
        "data. Next step: find whether the CCPA page moved or whether the "
        "channel is mailbox-only. Dataset contact: "
        "dataprivacy@demandscience.com."
    ),
    "coresignal-com": (
        "Verified by browser render 2026-09-23: coresignal.com publishes "
        "no opt-out page reachable from its homepage -- the only form on "
        "it is a 'Get a free consultation' contact modal (#wf-form- "
        "contact-us-modal, with a computed-invisible privacy-policy "
        "checkbox). This one is worth someone's time rather than writing "
        "off: Coresignal sells bulk datasets of EMPLOYEE records scraped "
        "from the public web, so it holds person-level data about people "
        "who have never heard of it, and the dataset does record "
        "privacy@coresignal.com. Next step: look for a privacy policy or "
        "GDPR/CCPA request page under coresignal.com and establish "
        "whether the channel is a form or that mailbox."
    ),
    "delivr-ai": (
        "Verified by browser render 2026-09-23: delivr.ai exposes no opt- "
        "out or privacy page from its homepage and carries no rights form "
        "-- the only input is the 'See your own intent signal' demo box. "
        "Delivr sells deterministic identity resolution and person-level "
        "intent, so it holds exactly the kind of data this tool exists to "
        "suppress, and the absence of a visible channel is itself "
        "notable. Next step: check /privacy, /legal and the Company menu "
        "for a rights page before concluding it is mailbox-only. Dataset "
        "contact support@delivr.ai is general support, not a privacy "
        "alias."
    ),
    "dataskip-io": (
        "Verified by browser render 2026-09-23: the dataset's opt_out_url "
        "(dataskip.io/product/start-order/) redirects to /pricing -- a "
        "rate sheet for skip-tracing lookups, not an opt-out of any kind "
        "-- so the recorded surface is wrong. No privacy or opt-out page "
        "is linked from the navigation (PRICING / INDUSTRIES / DEVELOPERS "
        "/ FAQ / CONTACT US / SIGN IN). Worth pursuing rather than "
        "dismissing, because a skip-tracing service holds current address "
        "and phone data on people by design. Next step: check CONTACT US "
        "and any footer legal pages for a suppression channel. Dataset "
        "contact: support@dataskip.io."
    ),
    "deluxe-com": (
        "Reachability failure, 2026-09-23, and of a kind not yet seen in "
        "this sweep: https://www.deluxe.com/policy/donotsell/ fails with "
        "net::ERR_HTTP2_PROTOCOL_ERROR -- the host answers and then the "
        "connection breaks at the protocol level, which is different from "
        "both a DNS failure (bridgevine, brightswipe, calltruth) and a "
        "silent timeout (carmarketsolutions). It may be an anti-bot "
        "measure that rejects this client specifically, or a genuine "
        "server fault. Recorded as undecided and worth a plain retry, "
        "ideally with a different HTTP stack. The URL shape suggests the "
        "surface exists. Dataset contact: "
        "privacyprogramoffice@deluxe.com."
    ),
    "disconetwork-com": (
        "NO VERDICT as of 2026-09-23, and the missing piece is small and "
        "specific. Disco's opt-out notice lives in its Zendesk help "
        "centre (support.disconetwork.com, article 4418121857819) and "
        "that article points at 'this opt-out request form' -- /hc/en- "
        "us/requests/new, the generic Zendesk ticket form.  Rendering "
        "that page shows why it cannot be transcribed yet. The form posts "
        "to /hc/en-us/requests and contains, at rest, exactly two "
        "controls: a hidden request[ticket_form_id] and one text input, "
        "under the instruction 'Please choose your issue below'. Zendesk "
        "builds the real field set only after a ticket form is picked "
        "from that React combobox (class sc-fPXMVe kTRLWL, so the class "
        "names are generated and must not be used as selectors), and the "
        "article does not say which of Disco's ticket forms is the "
        "privacy one.  What a recipe-writer needs to do next, in order: "
        "open /hc/en-us/requests/new, read the options in the issue "
        "combobox and note which one corresponds to the opt-out request, "
        "select it, and only then enumerate the fields that appear -- "
        "they will have stable Zendesk names (request[subject], "
        "request[description], request[custom_fields_NNNN]) but the "
        "custom-field numbers are per-account and must be read off the "
        "live page. Also re-check for a captcha at that stage: none is "
        "loaded on the empty form, but Zendesk attaches one to the submit "
        "step on some accounts and an empty form proves nothing about the "
        "filled one. privacy@disconetwork.com is the fallback channel."
    ),
    "dresdendirect-com": (
        "NO VERDICT as of 2026-09-23, and the finding is that the "
        "advertised surface does not exist. dresdendirect.com shows a "
        "prominent button reading 'SUBMIT A PRIVACY REQUEST'. Inspected "
        "directly, that anchor's href is '#', it carries no onclick and "
        "no handler attribute, and its classes are plain styling (btn "
        "btn-style-default ...). Clicking it goes nowhere. The site's "
        "/privacy-policy/ returns 404, so there is no policy page behind "
        "it either.  The only real form on the site is a generic "
        "WordPress contact form, #wpforms-form-83, whose own heading is "
        "'ASK A QUESTION': Name, Email, Phone, Subject and a message box. "
        "Two of its inputs are HONEYPOTS -- wpforms[fields][1] "
        "(mislabelled 'Message * Email') and wpforms[fields][7] both "
        "compute to hidden -- and would have to go in forbidden_selectors "
        "if this were ever written up.  The open question is a judgement "
        "one, not a research one, which is why this is undecided rather "
        "than closed: does a general-purpose 'Ask a Question' box count "
        "as an opt-out surface when the broker advertises a privacy "
        "request button that does nothing? No other entry in this module "
        "has treated a generic contact form as an opt-out recipe. Someone "
        "should settle that policy once rather than per broker. "
        "phil@dresdendirect.com is the address the dataset records."
    ),
    "winwithoptimal-com": (
        "NO VERDICT as of 2026-09-23, because the company behind this row "
        "has been renamed and the checklist is keyed to the old domain. "
        "DATASET DEFECT, flagged here and deliberately NOT fixed in "
        "data/source-brokers.json: the row is 'Dspolitical, LLC' at "
        "winwithoptimal.com with info@dspolitical.com. Requesting "
        "winwithoptimal.com/privacy-policy/ redirects to "
        "https://www.onemagnify.com/privacy-policy -- DSPolitical / "
        "Optimal is now OneMagnify. The old domain still serves its own "
        "marketing pages, so this is a live rebrand mid-flight rather "
        "than a dead domain, which is why the redirect only shows up on "
        "the policy path.  What the recorded opt_out_url actually is: "
        "/opt-out-of-advertising/ is an EXPLAINER about managing cookies. "
        "Its only first-party form is #footerForm, a single-email Gravity "
        "newsletter box (gf_field_3_1) -- a marketing signup, not an opt- "
        "out, and a careless reader could easily write a recipe against "
        "it. The opt-out links it does give are all third-party: Google's "
        "gaoptout, adsrvr.org, Yahoo's device dashboard. Cookie consent "
        "is OneTrust.  Next step for whoever picks this up: research "
        "OneMagnify, not winwithoptimal. A first pass over "
        "onemagnify.com/privacy-policy found no anchor matching opt-out / "
        "do-not-sell / request / rights at all, so the rights channel "
        "there is probably a mailto or a portal named something else in "
        "the policy prose, and the policy text needs reading rather than "
        "its links scanning. If the surface is found, consider whether "
        "this row should be re-keyed to onemagnify-com."
    ),
    "dstillery-com": (
        "NO VERDICT as of 2026-09-23. Dstillery has two separate surfaces "
        "and neither is straightforwardly automatable; the more useful "
        "finding is about the first one.  dstillery.com/optout IS THE "
        "OPT-OUT, ACTUATED BY THE NAVIGATION ITSELF. Merely requesting "
        "that URL redirected to /ad-choices-optout-thank-you/?success=1 "
        "reading 'You will no longer receive targeted advertisements from "
        "Dstillery on this browser'. No form, no click, no confirmation "
        "-- a GET performs it. That matters beyond this broker: the "
        "reconnaissance tool (tools/probe_broker_forms.py) describes "
        "itself as read-only because it never types or clicks, and on "
        "this page that guarantee did not hold. It opted a throwaway "
        "browser out, which is harmless, but the class of hazard is real "
        "and the tool's docstring now says so.  It is also cookie-scoped "
        "by its own wording -- 'on this browser' -- so it suppresses "
        "targeting, not the profile. Automating it would be easy and "
        "would accomplish very little.  The real data-rights surface is a "
        "separate 'Data Subject Privacy Request' link to privacyportal- "
        "EU.onetrust.com/webform/... . The EU host raises the "
        "jurisdiction-gating question this sweep has hit repeatedly "
        "(hightouch, crunchbase, convergemarketing): a US requester may "
        "be refused or shown a different field set. Somebody needs to "
        "open that portal from a US context, see whether it serves a form "
        "at all, and re-check for a captcha at that stage.  One defect "
        "worth recording: the /do-not-sell-my-information page's own "
        "'click here' opt-out link points at "
        "http://www.dstillery.LOCAL/optout -- a development hostname "
        "leaked into production, so the most prominent link on the do- "
        "not-sell page is dead for every visitor. privacy@dstillery.com "
        "is the fallback."
    ),
    "thedatatrust-com": (
        "NO VERDICT as of 2026-09-23, and honestly so: this page defeated "
        "the reconnaissance tool rather than being read and found "
        "wanting.  Two attempts, at the dataset's /do-not-sell-my- "
        "personal-information/ and at the site root, both ended on "
        "https://thedatatrust.com/privacy-policy/ -- so the do-not-sell "
        "path REDIRECTS to the policy, which normally means there is no "
        "dedicated form at that address. Both then failed to describe the "
        "page: evaluating any DOM-reading script in it throws "
        "'RangeError: Maximum call stack size exceeded' from "
        "cmp.osano.com/dvx9xrLka0/....osano.js. The Osano consent manager "
        "is instrumenting DOM accessors and recursing on them, so "
        "document.title came back null and the body text came back empty. "
        "That is a tool limitation, not a finding about the broker, and "
        "it must not be written up as 'no form found'.  What the next "
        "pass should do: read this one WITHOUT script evaluation -- fetch "
        "the rendered HTML via content() or a static request and parse it "
        "offline, or block cmp.osano.com at the route level before "
        "navigating and then evaluate normally. Blocking the CMP is the "
        "cleaner option and would also apply to any other Osano site this "
        "sweep meets. Until the page has actually been read, nothing can "
        "be said about whether a form exists behind the redirect. "
        "legal@thedatatrust.com is on file."
    ),
    "worldpay-com": (
        "NO VERDICT as of 2026-09-23, and the reason is a tangle in the "
        "dataset row rather than anything the broker has done.  DATASET "
        "DEFECT, flagged and NOT fixed: the row is named 'Efunds "
        "Corporation', keyed to worldpay.com, with "
        "chexsystems.compliance@fisglobal.com as the contact. Those are "
        "three different things. eFunds is the company behind "
        "CHEXSYSTEMS, the banking consumer reporting agency that decides "
        "whether someone can open a checking account. Worldpay is a "
        "payments processor. Both passed through FIS ownership, which is "
        "how they came to share a row, but the consumer-facing reporting "
        "product is not on worldpay.com at all -- and ChexSystems, like "
        "earlywarning-com in this module, is an FCRA agency whose file is "
        "not something a consumer can opt out of.  What was actually "
        "found at the recorded domain: worldpay.com carries 'Do not sell "
        "or share my personal information' pointing at "
        "privacy.worldpay.com, which resolves to /policies and is a "
        "Transcend-powered privacy centre ('Powered by Transcend') "
        "offering 'Make a Privacy Request' and 'View Past Requests'. It "
        "is a single-page app -- /request 404s WITHIN it, so the request "
        "flow opens from the button rather than from a URL, and nothing "
        "could be transcribed without driving it.  Next steps, in order, "
        "because they are two different jobs: (1) drive the Transcend "
        "portal from the button, enumerate the request-type options and "
        "the fields, and re-check for a captcha at that stage -- none is "
        "loaded on the landing page, which proves nothing; (2) decide "
        "whether this row should be re-keyed to chexsystems.com, and if "
        "so whether it belongs with earlywarning-com as an FCRA agency "
        "with no opt-out rather than here."
    ),
    "electroniccommerceatoz-com": (
        "NO VERDICT as of 2026-09-23: electroniccommerceatoz.com does not "
        "resolve. The navigation failed with net::ERR_NAME_NOT_RESOLVED, "
        "i.e. DNS returned nothing -- not a refused connection, not a "
        "timeout, not a certificate mismatch. Nothing about the broker "
        "can be said from that.  It is recorded as undecided rather than "
        "as having no surface because a single DNS failure from one "
        "network is weak evidence. This sweep has already accumulated a "
        "short list of rows failing the same way (nuwber, bridgevine, "
        "brightswipe, carmarketsolutions, calltruth, blisspointmedia) and "
        "they should be rechecked together from a different resolver "
        "before any of them is written off -- a local resolver, a captive "
        "network or an upstream block would produce exactly this result "
        "for a domain that is perfectly alive.  The dataset records no "
        "opt-out email and an opt_out_method of 'unknown' for this row, "
        "so if the domain really is dead there may be no channel at all, "
        "which is itself worth establishing rather than assuming."
    ),
    "listmatch-com": (
        "NO VERDICT as of 2026-09-23, and this is the closest thing to an "
        "unwalled surface in its batch -- which is exactly why it is "
        "written up in full rather than closed.  listmatch.com/privacy/ "
        "carries TWO forms and they are not equally guarded. The second, "
        "a general contact form GETting to index.php?action=contact, is "
        "protected by hCaptcha (newassets.hcaptcha.com frame, .h-captcha "
        "element, h-captcha-response textarea) -- and carries a leftover "
        "g-recaptcha-response textarea beside it, a fossil of a previous "
        "migration. The FIRST form, index.php?action=checkemail, has no "
        "captcha field of any kind: an email box, a checkbox, a submit "
        "reading 'Check/Manage/Delete Data Record'. The hCaptcha SCRIPT "
        "is loaded page-wide, so this is precisely the situation "
        "porchgroupmedia warns about and the absence must not be recorded "
        "as verified.  THE HONEYPOT IS NASTY AND MUST BE RECORDED. Form "
        "one contains a hidden input[name='EMAIL'] sitting beside the "
        "visible input[name='dataaddress'], which is the box that "
        "actually takes the email address. The obvious selector -- the "
        "one any recipe-writer reaches for first -- is the trap, and "
        "filling it would flag the submission as a bot. Any future recipe "
        "needs input[name='dataaddress'] in fields and "
        "input[name='email'] in forbidden_selectors. There is also an "
        "input[name='isca'] checkbox, 'Check this if you are a resident "
        "of California'.  Why undecided and not staged: this form does "
        "not opt anyone out. The page says it 'will give you the option "
        "to view your consumer data record and have your record deleted' "
        "-- so it is step one of at least two, and the deletion happens "
        "on a page that cannot be seen without submitting a real address. "
        "A recipe stopping at step one would report success having done "
        "nothing.  Worth recording verbatim, because it narrows who needs "
        "this at all: 'As of 2024 we do not sell/share/buy data in the "
        "following states: "
        "CA,CO,CT,DE,IA,IN,KY,MD,MT,NE,NH,NJ,OR,TN,TX,UT,VA,VT'."
    ),
    "factori-ai": (
        "NO VERDICT as of 2026-09-23. The surface was found, fully read, "
        "and is blocked by a CODEBASE GAP rather than by the broker. "
        "/do-not-sell-my-information/ embeds a GOOGLE FORM in a child "
        "frame (docs.google.com/forms/d/e/1FAIpQLSenQ43- "
        "rKbEqNWX8jUhKaXWpxzs4G5NLNFaU1-ML9htK-OmLQ). The main frame has "
        "no form at all, so this is another that would have read as 'no "
        "form found' before the probe learned to walk frames. Resolving "
        "the fields through aria-labelledby -- Google Forms give their "
        "inputs no name attribute, only entry.NNNNNNNNN on parallel "
        "hidden inputs -- gives five required questions: Name, Email, "
        "Country, MOBILE ADVERTISING ID, and Message.  The blocker is "
        "Mobile Advertising ID, required. Nothing in resolve_fields "
        "supplies a MAID and nothing could: it is a per-device identifier "
        "the user would have to read out of their phone's settings. This "
        "is the fourth broker in the sweep keyed to one (complementics, "
        "collectivedata, datafy, and datonics optionally), and it is "
        "recorded the same way they were -- undecided, because the form "
        "is honest and reachable and the gap is on our side. If MAID "
        "capture is ever added to resolve_fields, these five brokers "
        "unblock together.  For whoever returns: the hidden entry ids are "
        "entry.1647377565, entry.1929211145, entry.1262164174, "
        "entry.444203481 and entry.38362276, in the same document order "
        "as the five visible inputs, but they should be re-read rather "
        "than trusted -- they change if the form is edited. No captcha "
        "was present. clay-com is the other open Google Form in this "
        "module and has the same aria-labelledby shape; whatever is built "
        "for one will serve both. privacy@factori.ai is the published "
        "channel."
    ),
    "faraday-io": (
        "NO VERDICT as of 2026-09-23, and the reason is a reproducible "
        "failure to read the page at all rather than anything observed on "
        "it.  https://www.faraday.io/privacy-options WEDGES THE BROWSER. "
        "Probed twice, in separate processes, and both attempts exceeded "
        "the 75-second per-target budget with nothing recorded -- no "
        "status, no title, no text. This is the page that exposed the "
        "probe tool's own defect: a child frame whose main thread never "
        "yields makes frame.evaluate hang forever, Playwright offers no "
        "timeout on that call, and the first run lost eight already- "
        "probed targets because results were only written at the end. "
        "Both halves are now fixed (per-target child processes, "
        "incremental writes), which is why this entry exists at all "
        "instead of the run simply dying.  Nothing about the broker "
        "follows from that. It is NOT evidence of an anti-bot wall: a "
        "wall serves a challenge page, which reads perfectly well, and "
        "this served nothing. The likeliest explanations are a busy-loop "
        "in an embedded widget or a consent manager fighting "
        "instrumentation, the way Osano does on thedatatrust-com "
        "elsewhere in this module.  Next step, and it is the same one "
        "thedatatrust needs: read this page WITHOUT script evaluation -- "
        "take page.content() and parse it offline, or block the third- "
        "party frame at the route level before navigating. Two brokers "
        "now need that capability, which makes it worth building once. "
        "privacy@faraday.ai is on file, and note it differs from the "
        "row's domain (faraday.io)."
    ),
    "firstam-com": (
        "NO VERDICT as of 2026-09-23: the surface is one hop further on "
        "and was not opened.  firstam.com/privacy-policy carries no "
        "request form -- its only forms are two site searches -- but it "
        "does carry a 'Do Not Sell or Share My Personal Information' "
        "submit control outside any form, and its prose links out three "
        "separate times to firstam.service-now.com. So First American's "
        "rights requests are handled on a hosted SERVICENOW portal.  That "
        "is recorded as undecided rather than guessed at because "
        "ServiceNow is a known-awkward host for this work: its forms are "
        "keyed by per-instance sys-ids rather than by stable field names, "
        "which this sweep has already met and which makes any "
        "transcription instance-specific. It needs rendering directly "
        "before anything can be said about its fields or whether it "
        "carries a challenge.  The page also offers the usual third-party "
        "cookie opt-outs (Google's gaoptout, optout.aboutads.info) which "
        "are not this broker's surface and should not be mistaken for it. "
        "The dataset's contact for this row, tree-trace- "
        "legal.sna@firstam.com, is specific to the First American Data "
        "Tree subsidiary the row is actually about, which is worth "
        "preserving: a request sent to the parent may not reach the right "
        "database."
    ),
    "firstdirectmarketing-com": (
        "NO VERDICT as of 2026-09-23. compliance.firstdirectmarketing.com "
        "is a hosted 'Governance portal' -- a third-party compliance-page "
        "product, judging by its generic copy ('Empowering you through "
        "absolute transparency') and its Legal/Company chrome -- and it "
        "advertises exactly the right thing: 'Data subject requests: "
        "Exercise and manage your personal data privacy rights'.  It "
        "could not be read. The portal is a JavaScript application that "
        "renders no form element at any URL tried, including /data- "
        "subject-requests, which serves the same landing content as the "
        "root. The only interactive controls in the DOM are the portal's "
        "own chrome plus one telling entry: a submit labelled 'Open "
        "privacy widget.' So the request form is inside a WIDGET opened "
        "by that control, not a page that can be navigated to.  Next step "
        "is concrete: click 'Open privacy widget.', let it render, then "
        "enumerate the fields and check for a captcha at that point. "
        "Worth doing carefully rather than quickly, because a hosted "
        "governance portal is likely to be shared across many brokers -- "
        "identifying the product by name would probably resolve several "
        "dataset rows at once, which is the same leverage the OneTrust "
        "and DataGrail patterns gave. privacy@firstdirectmarketing.com is "
        "the published channel."
    ),
    "instantly-ai": (
        "NO VERDICT as of 2026-09-23. The dataset points at "
        "help.instantly.ai/en/collections/9392788-instantly-privacy- "
        "center, an Intercom help centre, and the request returns HTTP "
        "401 with the page itself saying so: 'Unable to load this "
        "article, you may need to sign in first. You can try sending us a "
        "message or logging in at ...'.  So the privacy centre is behind "
        "authentication, which is a meaningful finding rather than a "
        "wall: a person who has never been an Instantly CUSTOMER -- and "
        "the people who most need to opt out of a cold-email platform's "
        "database are exactly the people who are not its customers -- "
        "cannot read it at all, let alone act on it. That is worth "
        "stating plainly if this row is ever escalated.  It is undecided "
        "rather than closed because a public surface may well exist "
        "elsewhere: instantly.ai's own site was not examined, only the "
        "recorded help-centre URL. Next step is to look for a privacy or "
        "do-not-sell page on the main domain before concluding anything. "
        "privacy@instantly.ai is the address on file. Note also that the "
        "dataset names this row's company 'FOO MONK LLC', which is "
        "unlikely to be the name a person would search for."
    ),
    "forager-ai": (
        "NO VERDICT as of 2026-09-23. The surface was found and read; it "
        "is the out-of-band hop that stops it.  forager.ai/privacy is a "
        "policy page whose only form is a SIGN-IN box (Login-2 / "
        "Password-3) -- not an opt-out, and worth naming so it is not "
        "mistaken for one. The policy links three times to the real "
        "surface at app.forager.ai/privacy/data-removal, headed 'Request "
        "to Remove Your Information from Forager', which asks Full Name "
        "plus either an Email or a Phone (with an international country "
        "select) and offers one button: 'Get Code'.  The page states the "
        "flow itself: 'To delete your information, please enter your "
        "Email or Phone below and we will send you a verification code.' "
        "So step one is trivially automatable and completes nothing; the "
        "request is made at step two, behind a code delivered out of "
        "band. That is the same shape as the shipped "
        "ADVANCEDBACKGROUNDCHECKS recipe, so it is not out of scope in "
        "principle -- which is precisely why this is undecided rather "
        "than closed. Whoever picks it up should check whether the code "
        "arrives by email in a form the tool could be given, and "
        "enumerate step two.  One caution for that pass: the PARENT "
        "domain loads challenges.cloudflare.com, so a challenge may "
        "appear at the step the request is actually submitted even though "
        "none is visible on step one. The policy is candid in a way worth "
        "quoting, incidentally: 'we are committed to protecting the "
        "personal data we collect, process, and sell'. privacy@forager.ai "
        "is published."
    ),
    "foursquare-com": (
        "NO VERDICT as of 2026-09-23, and it is one question short of "
        "resolvable.  foursquare.com/data-requests resolves to "
        "app.foursquare.com/data-requests and carries a genuine, captcha- "
        "free opt-out form: a single text input named 'id', two "
        "checkboxes named dontSellOrShareMyInfo and "
        "limitUseOfSensitiveInfo, and Submit, under the heading 'Opt-out "
        "of sale/sharing or Limit use of personal information'. No "
        "captcha script, no captcha element, no hidden challenge field. "
        "THE BLOCKER IS THAT ONE FIELD. input[name='id'] carries no label "
        "the probe could resolve and no placeholder. On a location-data "
        "company whose product is keyed to devices, 'id' is far more "
        "likely to be an ADVERTISING IDENTIFIER than an email address -- "
        "which would put this with the MAID-keyed cluster (complementics, "
        "collectivedata, datafy, factori) and make it unfillable from "
        "resolve_fields. But it could equally be an email or a Foursquare "
        "account id, and guessing would be exactly the kind of "
        "fabrication this module exists to prevent. Read the surrounding "
        "instructional copy on the live page and this resolves in one "
        "minute.  One curiosity worth recording because it will confuse "
        "the next reader: the form's id attribute reads literally 'ref: "
        "<Node>' -- a React ref object stringified into the DOM by "
        "mistake. It is not a usable selector and should not be treated "
        "as one; scope on the checkbox names instead. Note also the page "
        "localises (fr./de. subdomains) and offers a separate 'Your "
        "Privacy Choices' at location.foursquare.com, unexamined. "
        "legal@foursquare.com is on file."
    ),
    "fourthwall-tv": (
        "NO VERDICT as of 2026-09-23, and it is close. /donotsellorshare- "
        "fourthwall carries a Wix form (#comp-lddc37r6) whose own page "
        "text states the purpose: 'To opt out of the sale or sharing of "
        "your personal information, please complete and submit the form "
        "below'. First Name, Last Name and Email are required; phone, "
        "street address, line 2, city, region/state/province, postal code "
        "and a country select are optional. No captcha script, no captcha "
        "element, no hidden challenge field. Every value it needs has a "
        "source in resolve_fields.  IT IS HELD BACK BY ONE CONTROL: an "
        "unnamed checkbox that is REQUIRED and computes to HIDDEN. That "
        "combination cannot be interpreted from the outside. It might be "
        "a consent box revealed by scrolling or by filling an earlier "
        "field, in which case a recipe checks it and all is well. It "
        "might be a honeypot mis-marked required, in which case checking "
        "it fails the submission. Those two readings call for opposite "
        "actions -- fields versus forbidden_selectors -- and nothing "
        "observed distinguishes them, so no recipe can be written "
        "honestly yet. Resolving it needs the page driven: fill the "
        "required fields, watch whether the checkbox becomes visible, and "
        "read its label if one appears.  Two smaller notes. The field "
        "names are unusually literal and include SPACES and SLASHES -- "
        "'street-address line 2', 'region/state/province', 'postal-/ zip "
        "code' -- so selectors must quote them carefully. And the page "
        "carries a SECOND form, a 'Stay Updated on Trending' newsletter "
        "box whose field is also input[name='email'], so any selector "
        "must be scoped through the request form. misrael@fourthwall.tv "
        "is the address the dataset records."
    ),
    "fraiser-org": (
        "NO VERDICT as of 2026-09-23, and the finding is a vendor rather "
        "than a broker.  fraiser.org/data-request-form renders no form in "
        "its main frame -- which is exactly what it looked like on the "
        "first pass, and would have been written up as 'no form found' by "
        "a less careful read. Enumerating iframes shows why: the form is "
        "embedded from my.datasubject.com/Azq9ITU2sQioIKhOV/44046. Note "
        "that the frame-walking probe did NOT report it either, because "
        "the frame held no controls at the moment it was read -- the "
        "vendor's app had not yet built its form. 'Frame present but "
        "empty' and 'no frame' are different findings and the probe "
        "currently renders them alike; worth fixing if it recurs. "
        "my.datasubject.com is the THIRD broker in this sweep on that "
        "vendor, after convergemarketing-com and diablomedia-com, both of "
        "which are open for the same reason: the portal appears to gate "
        "on jurisdiction before it will show a field set. That makes it "
        "worth solving ONCE rather than three times. Whoever takes it "
        "should drive the portal from a US context, record whether it "
        "serves a form at all, enumerate the fields and re-check for a "
        "captcha at that stage, then apply the answer to all three rows "
        "together.  reCAPTCHA scripts are loaded on the parent page, so a "
        "challenge at the submit step should be expected rather than "
        "assumed absent. privacy@fraiser.org is published."
    ),
    "fusedleads-com": (
        "NO VERDICT as of 2026-09-23: fusedleads.com could not be loaded "
        "at all. The navigation failed with net::ERR_CERT_DATE_INVALID -- "
        "the site's TLS certificate is expired or not yet valid.  That is "
        "worth distinguishing carefully from the other failure modes in "
        "this module. It is NOT a DNS failure (the name resolved), NOT a "
        "refused connection (the handshake got far enough to present a "
        "certificate), and NOT an anti-bot wall (a wall serves a "
        "challenge page, which reads fine). The host is up and answering; "
        "its certificate is simply out of date. Every ordinary visitor is "
        "seeing the same browser interstitial, so this is a broker whose "
        "site is effectively unreachable to the public rather than one "
        "defending itself against automation.  It is undecided rather "
        "than closed because certificates get renewed, often within days, "
        "and the site behind it is unexamined. A retry in a week is the "
        "whole next step. If it is still expired then, that is worth "
        "saying out loud in any escalation: a data broker whose opt-out "
        "channel is unreachable because it has not renewed a certificate "
        "is not offering one. The dataset records this row as email- "
        "method with greg@fusedleads.com, which at least does not depend "
        "on the website."
    ),
    "parade-pet": (
        "NO VERDICT as of 2026-09-23, and the surface was found only "
        "because the site leaks it.  parade.pet is a single-page app: "
        "every path, including ones that return HTTP 404, serves the same "
        "shell, and that shell contains every form the app will ever "
        "show. Enumerating them turns up signUpForm, loginForm, "
        "phoneNumberForm, smsCode, emailCodeForm -- and, decisively, "
        "form#optOutLoginForm, an email box with a Login button. So an "
        "opt-out flow exists and is reachable, which the visible site "
        "never advertises; the homepage FAQ item 'How do I delete my "
        "account and remove my ...' links only to an on-page accordion, "
        "and an 'Opt out of marketing' link points at '#'.  It is "
        "undecided because the flow is GATED ON AN EMAILED CODE. "
        "optOutLoginForm takes an email and logs you in; emailCodeForm "
        "then asks for a code delivered out of band. Nothing beyond that "
        "step was observed, so the fields that carry the actual request "
        "are unknown. Same shape as forager-ai in the previous batch and "
        "as the shipped ADVANCEDBACKGROUNDCHECKS recipe, so not out of "
        "scope in principle.  One caution for whoever continues: because "
        "the SPA serves all forms at all times, PRESENCE OF A FORM IN THE "
        "DOM DOES NOT MEAN IT IS ON SCREEN. A recipe here must assert the "
        "opt-out view is actually displayed before filling anything, or "
        "it will type into a hidden login box and report success. DATASET "
        "NOTE, flagged not fixed: this row's domain is parade.pet but its "
        "contact is hello@goodboystudios.com -- the operator's name, not "
        "the site's."
    ),
    "granitelists-com": (
        "NO VERDICT as of 2026-09-23: granitelists.com returns HTTP 403 "
        "with the page reading 'Account Suspended. This Account has been "
        "suspended. Contact your hosting provider for more information.' "
        "That is the hosting provider's own interstitial, not the "
        "broker's site.  Distinguish this carefully from the other "
        "unreachable rows. It is not a bot wall (403 here is the host "
        "refusing to serve anyone), not DNS, and not a certificate "
        "problem as with fusedleads-com. The account behind the domain "
        "has been suspended, most often for non-payment or a terms "
        "violation.  What makes it consequential rather than merely "
        "inconvenient: the dataset records NO opt-out URL, NO opt-out "
        "email and an opt_out_method of 'unknown' for this row. So there "
        "is no fallback channel to fall back to. If the company still "
        "holds data, there is at present no way whatsoever for a person "
        "to reach it.  Undecided rather than closed because suspensions "
        "are reversible and the site behind it has never been seen. "
        "Recheck in a few weeks; if it is still suspended and still has "
        "no published address, that combination is worth escalating "
        "rather than filing."
    ),
    "grassrootsanalytics-com": (
        "NO VERDICT as of 2026-09-23, and it is one control away from "
        "automatable -- the same single obstacle as fourthwall-tv in the "
        "previous batch, which is starting to look like a Wix pattern "
        "rather than a coincidence.  form#comp-lu8kq90d1 on the CCPA page "
        "is a real rights form and the page states the three rights "
        "plainly (access, deletion, opt-out from sales). Fields, all "
        "REQUIRED: first-name, last-name, email, phone-number, city, "
        "street-address, state, plus an optional 'Additional Information' "
        "textarea. Every one has a source in resolve_fields. No captcha "
        "script, no captcha element, no frames.  WHAT HOLDS IT: four "
        "checkboxes that compute to HIDDEN and carry NO name, NO id and "
        "NO label. Three are pre-checked; the FOURTH IS REQUIRED and is "
        "not checked. A required, unlabelled, invisible checkbox cannot "
        "be interpreted from outside -- it is either a consent box that "
        "appears once earlier fields are filled, in which case a recipe "
        "must check it, or a trap, in which case checking it fails the "
        "submission. Those call for opposite actions and nothing observed "
        "distinguishes them. Because it has no name and no id, it cannot "
        "even be addressed by a stable selector; a recipe would have to "
        "reach it positionally, which is exactly the kind of per-render "
        "fragility this module avoids.  Next step is to drive the page: "
        "fill the seven required fields, then see whether the fourth "
        "checkbox becomes visible and gains a label. Note also a SECOND "
        "form on the page (comp-m2atz5o8) that is a newsletter box with "
        "its own input[name='email'], so any selector must be scoped. The "
        "page usefully publishes CCPA request metrics. "
        "operations@grassrootsanalytics.com is the address on file."
    ),
    "forms-gle": (
        "NO VERDICT as of 2026-09-23, and this row has TWO dataset "
        "defects stacked on each other. Both are flagged here and neither "
        "is fixed.  FIRST: the row's domain is 'forms.gle'. That is "
        "Google's URL shortener, not a broker. Slugged, it becomes the "
        "key forms-gle, which identifies no company and will collide with "
        "any other row whose opt-out happens to be a Google Form. The "
        "row's real subject is whatever company the form belongs to. "
        "SECOND, and worse: the form does not belong to the company the "
        "row names. The dataset's contact is privacy@REALEFLOW.com. The "
        "form at forms.gle/S7vW6zXPwgtnZ9ZF9 is titled 'GROWBOTS OPT-OUT "
        "REQUEST FORM' and its text is Growbots' throughout -- 'we will "
        "remove the profile and business information linked to this email "
        "from our database'. Realeflow sells real-estate investor leads; "
        "Growbots sells B2B sales prospecting. They are unrelated. Either "
        "the URL was copied into the wrong row or the email was, and "
        "there is no way to tell which from here. Acting on it would send "
        "a person's opt-out to a company that may hold nothing about "
        "them, while leaving the company that does hold something "
        "untouched. That alone makes it unsafe to automate.  For the "
        "record, the Growbots form itself is a Google Form (mG61Hd, "
        "entry.1596228221) with a single required text input and the "
        "usual hidden fvv / fbzx / pageHistory / submissionTimestamp "
        "apparatus. Its labels are carried by aria-labelledby rather than "
        "by label elements, the same gap already recorded for clay-com "
        "and factori-ai -- the probe cannot read the question text, so "
        "which field is which is inferred, not observed. The form also "
        "states an out-of-band hop: 'upon filing one and CONFIRMING YOUR "
        "EMAIL, we will remove the profile'. Resolving this row starts "
        "with establishing which company it is actually about."
    ),
    "h1-co": (
        "NO VERDICT as of 2026-09-23, and the reason is unusual enough to "
        "be worth stating: the form is TOO PERMISSIVE to act on safely. "
        "h1.co/legal/privacy-preferences carries Gravity Forms gform_2, "
        "read in full: First Name (input_1), Last Name (input_3), "
        "Business Email (input_4), Public Business Profile URL (input_5), "
        "Title (input_6), Employer Institution (input_7), a full address "
        "group (input_9.1 through 9.5 plus a country select at 9.6), and "
        "three choice checkboxes -- input_11.1 stop processing, "
        "input_13.1 delete, input_12.1 send a summary of data held. No "
        "captcha script, no captcha element, no frames.  NOT ONE FIELD IS "
        "MARKED REQUIRED, including the three checkboxes that select "
        "which right is being exercised. A form that will accept a "
        "submission with no right selected is a form where a recipe can "
        "silently submit nothing at all and receive a success page. "
        "Worse, the three checkboxes all compute to HIDDEN, so which of "
        "them a given visitor is even offered is decided by conditional "
        "logic that was not observed. Choosing one blind would be "
        "guessing at the user's intent; choosing none would be a no-op "
        "dressed as a completed request.  There is a second obstacle of "
        "the kind already recorded for flashintel-ai: the form is keyed "
        "to a PROFESSIONAL identity -- Business Email, Public Business "
        "Profile URL, Title, Employer Institution. H1 sells healthcare- "
        "provider intelligence, so the record it holds is a practitioner "
        "profile, and resolve_fields has no source for a profile URL or "
        "an employer. Next step is to drive the form and see which "
        "checkboxes surface and under what condition. "
        "inquiries@h1insights.com is the dataset contact -- note the "
        "domain differs from the row's."
    ),
    "gladiknow-com": (
        "NO VERDICT as of 2026-09-23, and it must be read together with "
        "this broker's SEARCH leg, which the 2026-09-22 pass already "
        "resolved: gladiknow.com is an AFFILIATE FRONT. Its homepage "
        "'search' renders no results of its own -- it opens a new tab at "
        "truthfinder.com/results with utm_campaign=gladiknow and the "
        "query forwarded.  That finding reframes what is on /opt-out, and "
        "is the reason this is not being written up as a straightforward "
        "search-then-remove flow. The opt-out page does not take a "
        "removal request: it takes first name (required), last name "
        "(required), city, state, and a button reading SEARCH. Given what "
        "the homepage's identically-shaped form does, the first question "
        "is whether THIS form is any different -- whether it searches "
        "records Glad I Know itself holds, or simply hands the visitor on "
        "again. If it forwards, then the page is an opt-out surface in "
        "appearance only, and a person following it would be routed to a "
        "different company's site while believing they had begun a "
        "removal. That would be worth recording as more than a "
        "technicality.  ONE OBSERVED DIFFERENCE SAYS IT MAY BE GENUINE, "
        "and it is interesting on its own. The opt-out form's state "
        "select is NOT the fifty-state list the homepage search uses. It "
        "opens 'Select State...' and offers only California, Colorado, "
        "Connecticut, Delaware, Indiana and the rest of that group -- "
        "states with comprehensive consumer privacy statutes. The SEARCH "
        "side accepts all fifty; the OPT-OUT side accepts only the states "
        "that can compel one. A resident of a state without such a law "
        "appears unable to begin the flow at all while remaining fully "
        "searchable. Jurisdiction gating of that kind implies someone "
        "built this deliberately, which an affiliate landing page would "
        "have no reason to do.  No captcha script, no captcha element, no "
        "frames. Selector hazard: the page renders the form TWICE with "
        "duplicate ids (firstName, lastName, city), the second copy "
        "carrying no name attributes, so scope through the form rather "
        "than the id. Next step is to submit the opt-out form once and "
        "record where the browser ends up -- on gladiknow.com or on "
        "truthfinder.com. support@gladiknow.com is published and is a "
        "viable fallback either way."
    ),
    "here-com": (
        "NO VERDICT as of 2026-09-23, and it is held by a CODEBASE gap "
        "rather than anything the broker does -- the most actionable kind "
        "of finding in this module.  The dataset's /privacy/do-not-sell "
        "is a policy index whose only form is the site's autocomplete "
        "search. The real surface is one hop away at /privacy/here-data- "
        "subject-request, which embeds an iframe from "
        "dsr.legal.here.com/dsr containing the actual form. NO CAPTCHA "
        "SCRIPT AND NO CAPTCHA ELEMENT were seen on either page -- which, "
        "per this module's standing rule, is a statement about what was "
        "rendered and not a claim that none exists at submit time. "
        "Fields: country (select, required), email (required), a Yes/No "
        "radio pair on "
        "are_you_submitting_this_request_on_behalf_of_someone_else, then "
        "first_name, last_name, STREET, HOUSE_NUMBER, city and zip, plus "
        "a hidden intake_method and a Submit button.  THE BLOCKER IS "
        "house_number. HERE is a European company and its form splits the "
        "address the European way -- street name in one box, building "
        "number in another. resolve_fields has a 'street' source that "
        "yields a whole US-style line ('123 Main St') and has NO source "
        "that can produce the number alone. Putting the full line in "
        "'street' and leaving house_number empty may be accepted or may "
        "be rejected, and splitting it by regex would be this module "
        "inventing data. This is the same class of gap already recorded "
        "for MAID-keyed brokers: the identifier shape the broker wants is "
        "one the codebase does not model. Worth fixing once, since any "
        "EU-built form will ask the same way.  Second, smaller issue: the "
        "radios and the name/address inputs carry NO labels in the DOM, "
        "so which radio means Yes is not observable from the outside and "
        "would have to be read from the rendered page. privacy@here.com "
        "is published, and HERE offers a separate Wi-Fi opt-out at "
        "/privacy/here-wi-fi-opt-out not examined here."
    ),
    "fivestarrated-com": (
        "NO VERDICT as of 2026-09-23. /do-not-sell renders its heading -- "
        "'DO NOT SELL MY PERSONAL INFORMATION' -- and the accompanying "
        "prose, but NO REQUEST FORM appears in the DOM. The only forms on "
        "the page are the site's own ZIP and keyword search boxes, "
        "posting to /Home/ZipSearch and /Home/Search, which are the "
        "directory's product and not a rights surface.  It is undecided "
        "rather than closed because the evidence points at a form that "
        "exists but had not appeared when the page was read. reCAPTCHA v3 "
        "is loaded (api.js?render= with site key "
        "6Ldc8_MsAAAAAMWWcB_ioGAAxvosdlqqMk7sgTYI) and an anchor frame is "
        "attached. A site does not attach reCAPTCHA to a page with "
        "nothing to protect -- so the likeliest reading is that the form "
        "is injected on interaction, or by a script that had not "
        "finished. Calling this 'no surface' would be the wrong "
        "conclusion drawn from a real absence.  Next step is to render it "
        "again with a longer settle, and if still empty, to click through "
        "whatever the page offers below the fold. Note that if the form "
        "does appear, the v3 reCAPTCHA already observed would most likely "
        "make this BLOCKED rather than automatable -- v3 is score-based "
        "and invisible, so a submission is not refused but silently "
        "scored down, which is the worst failure mode for this tool "
        "because it looks like success. adminsupport@fivestarrated.com is "
        "published."
    ),
    "exploreatlas-io": (
        "NO VERDICT as of 2026-09-23. www.exploreatlas.io/privacy returns "
        "HTTP 200 whose body reads 'This page couldn't be found. You may "
        "not have access, or it might have been deleted or moved.' That "
        "phrasing is a Notion or Super-style hosted-site message rather "
        "than a web-server 404, so the page was published at some point "
        "and has since been unpublished, deleted or made private. "
        "Recorded as undecided rather than no-surface for two reasons. "
        "The site itself was not examined beyond the recorded privacy "
        "path, so a rights page may exist elsewhere on the domain. And "
        "the row has a bigger question hanging over it than a missing "
        "page.  DATASET DEFECT, flagged not fixed: the row's domain is "
        "exploreatlas.io but its contact is SCOTT@HUNTCLUB.COM -- a "
        "personal address at an unrelated company. Hunt Club sells "
        "recruiting services; Atlas is a separate product name. Either "
        "the row conflates two companies, or Atlas is a Hunt Club "
        "property and the dataset records the parent's contact without "
        "saying so. As with the forms.gle row in the previous batch, "
        "acting on it risks sending a person's opt-out to a company that "
        "holds nothing about them. Establishing which company this row is "
        "about is the first step, before any further page-hunting."
    ),
    "hunter-io": (
        "NO VERDICT as of 2026-09-23, and this is the CLOSEST TO "
        "STAGEABLE of anything in this batch -- recorded carefully so the "
        "next pass can finish it rather than redo it. "
        "hunter.io/claim?ref=donotsell carries form#claim-email-form: a "
        "single required input[name='claim[email]'], a Rails "
        "authenticity_token, and a button reading 'Claim the email "
        "address', posting to hunter.io/claim/authenticate. The page "
        "states the effect plainly: 'Once the email has been claimed, we "
        "immediately process the deletion of all data associated with it, "
        "including its original source.' NO CAPTCHA SCRIPT, no captcha "
        "element, no frames. One field, one source in resolve_fields, no "
        "wall observed.  WHAT STOPS IT BEING STAGED: the action is "
        "/claim/AUTHENTICATE, and 'claim' is the site's word for proving "
        "the address is yours. So step one almost certainly sends a "
        "confirmation email and completes nothing by itself -- the same "
        "shape as forager-ai and parade-pet. A recipe that submitted this "
        "and reported success would be reporting a step, not a removal. "
        "That is precisely the failure this module exists to avoid, and "
        "it is not observable from the form markup: it needs one live "
        "submission to a real address to see what comes back.  The "
        "authenticity_token is per-load but is read from the page at fill "
        "time, so it is not itself an obstacle to a live browser session. "
        "Note the page carries a SECOND form (cookie preferences, posting "
        "to /users/update-cookies) with its own authenticity_token, so "
        "selectors must be scoped through #claim-email-form. Next step: "
        "submit once with a controlled address and record whether a "
        "confirmation arrives and what it asks for. privacy@hunter.io is "
        "published."
    ),
    "id5-io": (
        "NO VERDICT as of 2026-09-23, and the next step carries a HAZARD "
        "that must be read before anyone probes it.  The dataset's URL is "
        "the platform privacy policy, which has no form. It links onward "
        "to id5-sync.com/privacy and id5-sync.com/privacy/do-not-sell, "
        "labelled 'Opt Out and DSAR Portal' and 'Do not sell my personal "
        "information'. The do-not-sell page was rendered: it carries "
        "explanatory prose ('The ID5 - Do Not Sell My Personal "
        "Information page is NOT the same as the ID5 Platform Privacy "
        "Policy') and NO form element was found on it.  THE HAZARD: ID5 "
        "operates an advertising IDENTITY GRAPH, and the standard "
        "mechanism for opting out of one is to SET AN OPT-OUT COOKIE ON "
        "THE VISITOR'S BROWSER -- an action performed by loading the page "
        "or clicking a link, with no form and no submit. dstillery in "
        "this module is already recorded as GET-actuated for exactly this "
        "reason, and the probe tool's docstring carries the warning. So "
        "'no form found' here may mean the opt-out has no form BECAUSE "
        "MERELY VISITING PERFORMS IT. Whoever continues should read the "
        "page's own text before clicking anything, and must not sweep "
        "id5-sync.com URLs in bulk.  A cookie-based opt-out would also be "
        "close to useless for this tool's purpose even if fired "
        "correctly: it binds to one browser profile, not to the person, "
        "and would be lost on the next clear. If that is what this turns "
        "out to be, the honest mapping is probably no-surface with an "
        "explanation, not a recipe. privacy@id5.io is published."
    ),
    "ileads-com": (
        "NO VERDICT as of 2026-09-23, and it is ONE LABEL AWAY from being "
        "stageable -- the strongest near-miss in this batch alongside "
        "hunter-io.  ileads.com/submitrequest/ carries a real CCPA form "
        "with NO CAPTCHA SCRIPT, no captcha element and no frames. "
        "Fields: fname, lname, proaddress ('PROPERTY ADDRESS'), cityname, "
        "zipaddress and phone-number all REQUIRED; a states-list select "
        "and email marked optional. The labels are explicit and every one "
        "maps to a resolve_fields source.  WHAT HOLDS IT: four "
        "checkbox[name='checkbox-datatypes[]'] controls that carry NO "
        "label, NO id and NO value the probe could read. They are "
        "evidently the request-type selection -- which categories of data "
        "the request covers -- and submitting without understanding them "
        "means either sending a request that asks for nothing, or ticking "
        "all four and asserting something on the user's behalf that they "
        "did not choose. Neither is acceptable. Reading the rendered "
        "labels beside those four boxes resolves this row in a minute.  A "
        "HONEYPOT is present: input[name='subject_line'], a text input "
        "with no label that computes to hidden, on a form that never asks "
        "a human for a subject. It belongs in forbidden_selectors.  Two "
        "smaller notes. The form asks for PROPERTY ADDRESS rather than "
        "mailing address -- iLeads sells mortgage and insurance leads, so "
        "the record is keyed to a property; for most people those "
        "coincide, but they need not, and a recipe should not silently "
        "assume it. And the page scopes itself to California residents "
        "only. inquiries@ileads.com is published."
    ),
    "acuityads-com": (
        "NO VERDICT as of 2026-09-23, and there are three separate "
        "reasons to leave it open.  FIRST, A REBRAND THE DATASET DOES NOT "
        "RECORD: privacy.acuityads.com resolves through to illumin.com. "
        "AcuityAds now trades as illumin, and every live URL is on the "
        "new domain. Flagged, not fixed.  SECOND, the form is real but "
        "barely legible from the outside. illumin.com/opt-out/ hosts a "
        "HubSpot form inside an ABOUT:BLANK frame -- injected by script "
        "rather than served from a URL -- containing a single required "
        "input[name='email'] with a per-render id "
        "(email-56f7074b-6a42-488a-abca-2066a9726da2) and a Submit. No "
        "captcha script was seen, but the frame is about:blank, so that "
        "observation covers the parent page and NOT reliably the frame's "
        "own contents. Under this module's standing rule that is not a "
        "finding of 'no captcha'. A recipe would also have to address a "
        "frame with no URL and an id that changes per render -- neither "
        "impossible nor stable.  THIRD, AND THE BEST FINDING HERE: the "
        "consent page carries UNREPLACED COOKIEBOT TEMPLATE PLACEHOLDERS. "
        "Its links include a literal '[#DSR_FORM_URL_TEXT#]' pointing at "
        "'illumin.com/opt-out-success/[#DSR_FORM_URL#]', alongside "
        "'[#IABV2SETTINGS#]'. The DSR form URL -- the data subject "
        "request link, the thing a person on a privacy page is looking "
        "for -- was never configured, so the banner offers a link to a "
        "page that cannot exist. That is a real, checkable defect in the "
        "broker's published rights channel, not a rendering artifact. "
        "itops@acuityads.com is the dataset contact; note it is an "
        "operations address, not a privacy one."
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
    "bdex-com": (
        "Verified by browser render 2026-09-23. www.bdex.com/privacy- "
        "policy/data-sharing-opt-out/ serves a real, short Elementor form "
        "-- id opt_out_form, POST to itself, with "
        "form_fields[email]/#form-field-email (required, the only text "
        "field), two checkboxes form_fields[opt_out_bdex]/#form-field- "
        "opt_out_bdex-0 'Opt out of sharing my information on the BDEX "
        "platform' and form_fields[show_info]/#form-field-show_info-0 "
        "'Show me what information you have on file about me', hidden "
        "Elementor bookkeeping (post_id, form_id, referer_title, "
        "queried_id) and a bare button[type=submit] 'Submit'. It is filed "
        "here because the rendered page carries an element with class "
        "'elementor-g-recaptcha' -- Elementor's reCAPTCHA field -- even "
        "though the widget's script had not fetched by the time of the "
        "snapshot, which is normal for a lazily-loaded reCAPTCHA. Worth a "
        "recheck if this row ever matters enough: if that element turns "
        "out to be an empty placeholder with no site key behind it, this "
        "is a two-line recipe."
    ),
    "bdo-com": (
        "Verified by browser render 2026-09-23. www.bdo.com/do-not-sell- "
        "my-personal-information serves a real Kentico form (id form- "
        "CaliforniaRequestToOpt_Out-981e) posting to "
        "/kentico.components/en-us/kentico.formwidget/..., with a hidden "
        "__RequestVerificationToken minted per load. Blocked by reCAPTCHA "
        "Enterprise: the page loads google.com/recaptcha/enterprise.js "
        "with a render= site key plus BDO's own recaptcha- "
        "enterprise.bundle.js, and the form carries the matching "
        "...ReCaptchaEnterprise.Value textarea. Transcribed anyway: First "
        "Name and Last Name (both required, as ...TextInput_3.Value and "
        "...TextInput_1.Value), a REQUIRED field literally labelled "
        "'Unique Identifier' (...UniqueIdentifier.Value) and an optional "
        "'Additional Identifiers', then Submit. Two things for whoever "
        "revisits. The field names embed a per-form instance hash "
        "('981e') that may not survive a redeploy, so they are not safe "
        "to hardcode. And the required 'Unique Identifier' is unexplained "
        "on the page -- what BDO expects there is unknown, and a recipe "
        "cannot fill a required field it does not understand. Separately, "
        "flagging the row itself: BDO USA is an accounting and advisory "
        "firm, not a data broker in the people-search or audience-data "
        "sense, so this may be a dataset scope artifact."
    ),
    "bestpickreports-com": (
        "Verified by browser render 2026-09-23. "
        "www.bestpickreports.com/do-not-sell serves a real, complete CCPA "
        "form, blocked by an invisible reCAPTCHA v3: the page loads "
        "google.com/recaptcha/api.js with a render= site key and carries "
        "two g-recaptcha-response textareas. Transcribed: firstName, "
        "lastName, emails ('Email Address(es)'), phoneNumbers ('Phone "
        "Number(s)'), streetAddress, a custom listbox BUTTON labelled "
        "'State of Residence / Select state...' backed by a hidden text "
        "input named stateOfResidence, zipCode, and a submit reading "
        "'Submit Privacy Request'. IMPORTANT CAVEAT for any future "
        "recipe-writer: the element ids on this form are framework- "
        "generated positional ids (v-0-1, v-0-2, ... v-0-9) with no "
        "semantic content, so they will silently renumber if a field is "
        "ever added or reordered. A recipe here must select on the NAME "
        "attributes, which are stable and meaningful, never on those ids "
        "-- and the State control is not a <select> at all, so it needs "
        "the listbox_button treatment rather than a select_option."
    ),
    "peoplesmart-com": (
        "Verified by browser render 2026-09-23, and it is the 'wall the "
        "exit specifically' pattern again. The dataset's opt_out_url "
        "(www.peoplesmart.com/svc/optout/search/contact_optouts) returns "
        "HTTP 403 and a Cloudflare interstitial -- title 'Just a "
        "moment...', body 'Performing security verification / This "
        "website uses a security service to protect against malicious "
        "bots', Turnstile script loaded, Ray ID a3fd076fad1231a9 -- so "
        "the opt-out form behind it never renders and there is nothing to "
        "transcribe. The contrast is the finding: the same site's "
        "HOMEPAGE serves normally in the same browser session, with three "
        "working search forms on it. So the search side is open and the "
        "removal side is walled, which is a choice about who is allowed "
        "to leave. Same shape as nationalpublicdata-com and "
        "privatenumberchecker-com."
    ),
    "bbdirect-com": (
        "Verified by browser render 2026-09-23. www.bbdirect.com/privacy- "
        "compliance.html serves a real Cognito Forms embed, blocked by "
        "reCAPTCHA Enterprise: the page loads "
        "google.com/recaptcha/enterprise.js and carries grecaptcha-badge, "
        "grecaptcha-logo and grecaptcha-error elements. Transcribed: "
        "First (#cog-input-auto-0), Last (#cog-input-auto-1), Address "
        "Line 1 (#cog-1-line1), Address Line 2 (#cog-1-line2), City "
        "(#cog-1-city), State (#cog-1-state), Zip Code (#cog-1-zip-code) "
        "and 'Email * (required)' (#cog-2) -- every one of them marked "
        "required, which is worth noting because Address Line 2 being "
        "mandatory will reject a profile that has no unit number. Submit "
        "is a bare button reading 'Submit'. CAVEAT for any future recipe: "
        "Cognito Forms generates these ids from field ORDER (cog-input- "
        "auto-0, cog-1-line1, cog-2), so they renumber silently if a "
        "field is added, and none of the controls carries a name "
        "attribute at all -- the same fragility already recorded on "
        "bestpickreports-com, but worse, because here there is no stable "
        "name to fall back to."
    ),
    "business-com": (
        "Verified by browser render 2026-09-23, and the row is not what "
        "the domain suggests. The dataset's opt_out_url "
        "(business.com/optout/) redirects off-site to "
        "compliance.centerfield.com/v1/, a shared 'Consumer Privacy "
        "Rights Request Form' operated by Centerfield -- so this is a "
        "VENDOR-HOSTED portal, and any other Centerfield property in the "
        "dataset will land on the same form. It is blocked by reCAPTCHA "
        "v2: the page loads recaptcha/api.js and carries a g-recaptcha "
        "widget element plus its response textarea. Transcribed: a "
        "required requester-type radio group (Consumer / InsurancePartner "
        "/ Vendor / AuthorizedPerson / Employee / Applicant) and a "
        "required request-type checkbox group whose values include "
        "DoNotSell, alongside Report, Access, Delete, Limit, "
        "DoNotProfile, DoNotEmail, DoNotCall and Correct. IMPORTANT for a "
        "recipe-writer: every one of those controls computes to invisible "
        "-- they are custom-styled inputs driven by their labels -- so a "
        "driver must click the LABEL, not the input, and none of them "
        "carries a name or id attribute at all. The form is a React app "
        "whose route is a fragment (#/rightsRequest)."
    ),
    "verve-com": (
        "Verified by browser render 2026-09-23. verve.com/data-subject- "
        "request-form/ serves a real, complete Formidable Forms form "
        "(#form_datasubjectaccessrequest, POST to itself) blocked by "
        "reCAPTCHA (api.js loaded with onload=frmRecaptcha, and frm-g- "
        "recaptcha / grecaptcha-badge elements present). Transcribed: a "
        "requester-type radio group item_meta[6] (B2B Customer / Employee "
        "/ Job Applicant / End-User); a request-type checkbox group "
        "item_meta[7][] whose seven values include 'Do Not Sell or Share "
        "My Personal Information' and 'Limit the Use of My Sensitive "
        "Personal Information' alongside Correct Data, Info Request, Data "
        "Deletion, File a Complaint and Data Access; item_meta[8][first] "
        "/ [last] (First and Last Name, required); item_meta[9] Email "
        "(required); item_meta[10] Country (required, a free-text box "
        "rather than a select); and item_meta[11] Request Details (a "
        "required textarea). Field ids carry per-form random suffixes "
        "(field_u1j66, field_ebdv3, field_xr2fa, field_ayz61, "
        "field_zf5gh, field_ck1lr) so a recipe must select on the "
        "item_meta names."
    ),
    "buildertrend-com": (
        "Verified by browser render 2026-09-23, and notable for carrying "
        "TWO bot checks at once. buildertrend.com/privacy-notice/ embeds "
        "Gravity Forms #gform_25 (POST to itself) and the page loads BOTH "
        "google.com/recaptcha/api.js with a render= site key AND "
        "challenges.cloudflare.com/turnstile via the "
        "gravityformsturnstile plugin, with a .cf-turnstile element in "
        "the DOM. Transcribed: input_1 First name, input_11 Last name, "
        "input_2 Email address (all required); cascading geography "
        "selects input_7 Country (32 options), input_6 State (52), "
        "input_15 State (9), input_16 Province (11), input_17 and "
        "input_18 Territory (5 and 15); required input_8 'I am a (an):' "
        "(7 options); required textarea input_14 Request details; submit "
        "#gform_submit_button_25. HONEYPOT: input_24 / #input_25_24 is "
        "labelled 'Instagram', is not required, and sits FIRST in the "
        "field order -- Gravity Forms' standard honeypot shape, and it "
        "would have to go in forbidden_selectors. Flagging the row: "
        "Buildertrend is construction-management SaaS, so this may be a "
        "dataset scope artifact rather than a broker."
    ),
    "700credit-com": (
        "Verified by browser render 2026-09-23. The dataset's opt_out_url "
        "is the privacy POLICY, which names the real surface -- 'this web "
        "form' at www.700credit.com/ccpa-request/ -- and that page serves "
        "Gravity Forms #gform_872, blocked by reCAPTCHA (ginput_recaptcha "
        "element plus the response textarea). Transcribed: input_37 Name "
        "(required, a single full-name box, not split), input_47 Email "
        "Address (required), input_48 'Alternative Email Addressses' "
        "(sic), input_38 Street Address, input_39 City, input_41 State (a "
        "required select of full state names including territories and "
        "'Armed Forces Americas'), input_42 ZIP Code, input_44 Phone (all "
        "required), required select input_46 'I am a' (Customer / "
        "Employee / Job Applicant / Vendor / Ad/Email Recipient / Other), "
        "and a seven-value checkbox group input_51.1-.7 whose options "
        "include 'Do not sell or share my personal information', Delete, "
        "Send Copy, Correct, Explain Sharing Rules, Opt Out of Automated "
        "decision-making and Limit Use of sensitive information. "
        "HONEYPOT: input_55 / #input_872_55 labelled 'Facebook', "
        "unrequired, first in the field order -- the same Gravity Forms "
        "shape as buildertrend-com. Correct the dataset URL to /ccpa- "
        "request/."
    ),
    "brooksim-com": (
        "Verified by browser render 2026-09-23, and the research is "
        "complete except for the wall. www.brooksim.com/privacy-form is "
        "an explainer that embeds a third-party portal and names it in "
        "its own fallback text: dsr.trustsuperset.com/?orgId=2dc76d0a- "
        "78d2-4492-9fc1-39da892fc0d5. Rendering THAT gives a real "
        "'BrooksIM Data Subject Request Form' -- a request-type select "
        "(Right to Erasure / Right to Rectification / Right to Restrict "
        "Processing / Right to Data Portability / Right to Not...), then "
        "first_name, last_name, email (the only required field), phone, "
        "country, address_1, address_2, city, region and zip_code, each "
        "with a matching id, and a 'Submit Request' button. It is blocked "
        "by Cloudflare Turnstile, whose api.js the portal loads. Three "
        "alternate channels the page offers a human, recorded because "
        "they are unusually concrete: privacy@brooksim.com, a dedicated "
        "privacy line at 800-531-2601 x998, and a JSON POST API for bulk "
        "requests (key from datascientist@brooksim.com). The page also "
        "states a submission is confirmed by email, so a completed "
        "submission is a request STARTED. BrooksIM describes itself as a "
        "registered data broker in California and reports opt-out volume "
        "rising from 145 requests in 2023 to 68,593 in 2024."
    ),
    "catalist-us": (
        "Verified by browser render 2026-09-23: the surface is genuine, "
        "complete and DOUBLE-captcha'd. catalist.us/your-privacy-choices/ "
        "renders Gravity Forms #gform_8 posting to itself, with a "
        "conditional request-type select per state of residence "
        "(input_111/105/124/122/132, each offering some subset of 'delete "
        "and opt out of the sale', 'correct', 'access', 'limit the sale', "
        "'appeal the denial by Catalist'), a required state select "
        "(input_46: California, Colorado, Delaware, Indiana, Kentucky "
        "...), self-versus-authorized-agent radios (input_44, input_128), "
        "penalty-of-perjury declaration checkboxes (input_145.1, "
        "input_146.1), a Company text field (input_148) and name fields "
        "(input_62 First Name ...). It is walled twice over: an invisible "
        "reCAPTCHA v3 (script api.js?render=..., element "
        ".gf_invisible.ginput_recaptchav3 and the "
        "#gfield_recaptcha_response input) AND a Cloudflare Turnstile "
        "element (.cf-turnstile) on the same page. Neither appears in the "
        "served HTML. Note the Gravity Forms input_NN names are per-form- "
        "build identifiers, so even solving the challenge would leave a "
        "recipe pinned to this exact form revision. Second channel for a "
        "human: ggruver@catalist.us."
    ),
    "optoutprescreen-com": (
        "Verified 2026-09-23: https://www.optoutprescreen.com/ returns "
        "HTTP 403 from Akamai -- an 'Access Denied' page reading 'You "
        "don't have permission to access http://www.optoutprescreen.com/ "
        "on this server', with an errors.edgesuite.net reference id. "
        "Nothing renders, so there is no form to read. Worth flagging "
        "loudly because this is not an ordinary broker row: "
        "OptOutPrescreen is the official FCRA prescreen opt-out service "
        "operated jointly by the nationwide consumer reporting agencies, "
        "and it is the ONE surface a consumer is statutorily pointed at "
        "for firm-offer suppression. An edge wall in front of it means "
        "this codebase cannot automate the single most standard opt-out "
        "in the United States, and it also means a human using this tool "
        "must be told to visit it by hand (or call 1-888-5-OPTOUT). The "
        "wall is on the edge, not a solvable challenge -- there is no "
        "widget offered. The dataset's opt_out_email for this row, "
        "compliance@ciccredit.com, belongs to a different organization "
        "than the site and should not be treated as its channel."
    ),
    "catalina-com": (
        "Verified by browser render 2026-09-23: and this one is a "
        "correction to what the marketing site suggests. www.catalina.com "
        "itself offers only a OneTrust cookie widget ('Your Privacy "
        "Choices' -> javascript:Optanon.ToggleInfoDisplay()), and the "
        "dataset's opt_out_url (catalina.com/#privacy) plus the obvious "
        "guess /privacy-policy/ both lead nowhere -- the latter 404s. The "
        "real notice is at www.catalina.com/legal#privacy-notice, and "
        "buried in it is a genuine do-not-sell surface: a OneTrust "
        "webform at privacyportal.onetrust.com/webform/7665c53e-aae8- "
        "4a03-9dd3-66fb6bce8f55/991571a9-afd5-406e-b0ac-6fbcb4980df2, "
        "titled for Catalina Marketing and saying in its own words 'If "
        "you wish to opt out of the sale or sharing of your personal "
        "information, please complete the form below.' -- so for once a "
        "consent portal IS the right request type. It is short "
        "(#formField32DSARElement 'State you live in', #emailDSARElement, "
        "an optional file upload, #dsar-webform-submit-button) and it is "
        "captcha-walled: reCAPTCHA loaded explicitly "
        "(api.js?onload=ngx_captcha_onload_callback&render=explicit) with "
        "a live g-recaptcha-response element. Blocked, not absent. Second "
        "channel for a human: privacyteam@catalina.com or "
        "dpo@catalina.com."
    ),
    "clearview-ai": (
        "Verified by browser render 2026-09-23: Clearview does publish "
        "real do-not-sell surfaces, and they are captcha-walled AND "
        "require something this tool cannot supply. The /privacy-and- "
        "requests page links three separate OneTrust webforms under org "
        "1fdd17ee-bd10-4813-a254-de7d5c09360a: DO NOT SELL/SHARE "
        "(2a09e1a7-...), DO NOT SELL (7c79cae5-...) and OPT-OUT OF "
        "PROFILING (...). The DO NOT SELL form was rendered: "
        "#formField78DSARElement (state of residence, required), "
        "#emailDSARElement (required), a REQUIRED file upload (#vt-file- "
        "select-input-1), and #dsar-webform-submit-button -- behind "
        "reCAPTCHA loaded explicitly with a live g-recaptcha-response "
        "element. The required file is the hard part and is not "
        "incidental: Clearview's own text says 'we cannot search by name "
        "or any method other than image - so we need an image', meaning "
        "the request cannot be filed without uploading a PHOTOGRAPH of "
        "the requester. Even with the captcha solved this is outside what "
        "an identity record in this codebase can provide, and uploading a "
        "person's face is a decision a human must make. Alternative "
        "channel given on the form: 1 (866) 637-0257."
    ),
    "datanyze-com": (
        "Verified 2026-09-23: https://www.datanyze.com/privacy-center "
        "returns HTTP 403 with a PerimeterX/HUMAN press-and-hold "
        "challenge -- the body is nothing but 'Press & Hold to confirm "
        "you are a human (and not a bot)' and a reference id. No form "
        "renders, so there is nothing to transcribe. A press-and-hold "
        "challenge is a deliberate human-interaction gate, not a puzzle "
        "this tool should attempt. Note the dataset's opt_out_email for "
        "this row is ccpa@clickagy.com, a DIFFERENT company (Clickagy, "
        "whose data Datanyze/ZoomInfo absorbed) -- flagged as a probable "
        "stale contact; ZoomInfo's own privacy centre is the likelier "
        "live channel and was not checked here."
    ),
    "collectivedata-io": (
        "Verified by browser render 2026-09-23: the surface is real, "
        "narrow and walled. collectivedata.io/opt-out-do-not-sell- "
        "request-process/ posts to itself with exactly ONE field, "
        "input[name=maid], plus #submit-btn -- and an invisible reCAPTCHA "
        "(api.js, .g-recaptcha, a g-recaptcha-response textarea and a "
        ".grecaptcha-badge). Two independent blockers, and the second is "
        "the more interesting: the form does not accept a name, address "
        "or email at all, only a Mobile Advertising ID ('DO NOT enter "
        "your telephone number'), and requests are per-device, so a "
        "person with three devices must file three times. This codebase's "
        "identity record has no MAID field and could not honestly fill "
        "this form even unwalled. Recording it so nobody writes a recipe "
        "for a form that has nothing to say to a named person."
    ),
    "completemedicallists-com": (
        "Verified by browser render 2026-09-23: this is the most "
        "completely transcribed form in the batch and it is still walled. "
        "/ccpa.php ('Remove My Information') posts to /backend/send_ccpa "
        "and is built to defeat scrapers twice over. FIRST, FIVE "
        "HONEYPOTS: input[name=ERA], [name=FIP], [name=BABIP], [name=OPS] "
        "and [name=SLG] -- baseball statistics, all computed-invisible, "
        "all of which must go in forbidden_selectors. SECOND, the real "
        "fields are BASE64-NAMED: Rmlyc3ROYW1l = FirstName, TGFzdE5hbWU= "
        "= LastName, QmlydGhZZWFy = BirthYear, QWRkcmVzczE= = Address1, "
        "QWRkcmVzczI= = Address2, Q2l0eQ== = City, U3RhdGU= = State, Wmlw "
        "= Zip, plus a five-way radio group named ins (unlabelled in the "
        "DOM; a human must read what it selects before anything is "
        "filled). That is all readable. What stops a recipe is a visible "
        "reCAPTCHA -- api.js loaded and a live .g-recaptcha element on "
        "the page. Note the rights notice covers a long list of states, "
        "not just California, so the surface is broader than the file "
        "name suggests. Dataset contact: "
        "tburnell@completemedicallists.com."
    ),
    "contactout-com": (
        "Verified by browser render 2026-09-23: contactout.com/optout "
        "carries a clean one-field first step -- form posting to "
        "/optout/verify/send with input#email (required) and a 'Send "
        "verification link' button -- and it is behind a CLOUDFLARE "
        "TURNSTILE (challenges.cloudflare.com/turnstile script plus a "
        "live .cf-turnstile element). Two separate reasons no recipe "
        "ships: the Turnstile, and the fact that the real removal form is "
        "behind a one-time emailed verification link, the same shape as "
        "checkpeople-com and fastpeoplesearch-com. Second channel for a "
        "human: support@contactout.com."
    ),
    "service-now-com": (
        "Verified by browser render 2026-09-23: the form is real, "
        "correctly typed and captcha-walled. The dataset URL opens a "
        "ServiceNow-hosted page on firstam.service-now.com titled "
        "'Consumer Opt-Out Request Form / Do Not Sell or Share My "
        "Personal Information', explicitly on behalf of Connected "
        "Investors, Inc. -- so for once the recorded URL is exactly the "
        "right request type. It is behind reCAPTCHA ENTERPRISE "
        "(google.com/recaptcha/enterprise loaded, badge and g-recaptcha- "
        "response present). Even unwalled a recipe here would be awkward: "
        "every control is a ServiceNow catalog variable carrying "
        "generated sys-id names (log_variable_actions, "
        "jvar_nested_form_evaluation and friends), so the selectors would "
        "be tied to one form build. Recorded as miskeyed on the search "
        "leg too: the broker is Connected Investors, not ServiceNow. "
        "Dataset contact: support@connectedinvestors.com."
    ),
    "bigidprivacy-cloud": (
        "Verified by browser render 2026-09-23: "
        "crexi.bigidprivacy.cloud/consumer/#/o2TCsG9LJ7 renders a BigID- "
        "hosted 'Crexi Privacy Center' that gates everything behind a "
        "country picker (#userCountry, a MUI autocomplete) before 'Select "
        "an action' and 'Tell us who you are' become reachable -- and "
        "reCAPTCHA is already loaded at that first step (api.js, "
        ".grecaptcha-badge, a g-recaptcha-response textarea). Blocked at "
        "the door, so the request-type list and field set behind it were "
        "never seen. Recorded as probably miskeyed: the broker is Crexi, "
        "bigidprivacy.cloud is BigID's hosting."
    ),
    "hireright-com": (
        "RESOLVED from undecided on 2026-09-23, within the same sweep, by "
        "teaching tools/probe_broker_forms.py to descend into child "
        "frames -- and this row is the case that forced that change. The "
        "trail: the footer offers both /legal/do-not-sell-my-personal- "
        "information (California) and /legal/united-states-opt-out- "
        "rights-outside-of- california, each a notice page with no "
        "inputs, each saying 'click here to fill out the webform' and "
        "both pointing at the same target, http://www.hireright.com/u-s- "
        "state-consumer-privacy-rights-request- form. That page renders, "
        "is titled 'U.S. State Consumer Privacy Rights Request Form' -- "
        "and document.querySelectorAll('form') on its main frame returns "
        "NOTHING, which is what made it look unresolvable on the first "
        "pass. The form is EMBEDDED: a Pardot form at "
        "info.hireright.com/l/650513/2024-10-08/561z6m, posting to "
        "itself, with an initial-request/appeal radio pair, an 'I am:' "
        "select (consumer / agent), agent name and email, first name, "
        "last name, email, a State of Residence select, and two parallel "
        "blocks of request-type checkboxes -- one of which is 'Opt-out of "
        "our sale or sharing of personal information' and another 'Opt- "
        "out of profiling in furtherance of automated decisions'. So the "
        "surface is real and correctly typed. What blocks it is reCAPTCHA "
        "ENTERPRISE with a VISIBLE 'I'm not a robot' checkbox "
        "(enterprise.js, a pardot-recaptcha-wrapper and a .g-recaptcha, "
        "plus the anchor and bframe challenge frames) -- not an invisible "
        "score check, an actual click gate. Note too that every field "
        "name is a generated Pardot identifier of the form "
        "650513_162837pi_650513_162837, which is tied to one form build, "
        "so even unwalled a recipe would need to address controls by "
        "label rather than by name. READ THE PREAMBLE BEFORE WRITING "
        "ANYTHING HERE: the page opens 'IMPORTANT - READ BEFORE "
        "SUBMITTING' and argues in HireRight's own words that 'our "
        "background screening business falls within those exceptions, and "
        "the state consumer privacy laws do not apply', and that outside "
        "California they generally do not apply in an employment context "
        "-- so this is a request the broker may consider inapplicable to "
        "the data a person actually wants suppressed. Dataset contact: "
        "dpo@hireright.com."
    ),
    "intentwave-com": (
        "RESOLVED from undecided on 2026-09-23 by the same frame- "
        "descending change that resolved hireright-com, and it is the "
        "second row in one sweep where 'this page has no form' meant "
        "'this page EMBEDS its form'. intentwave.com/opt-out and its "
        "sibling persistent.id/opt-out render the same two self- "
        "referential choices and no controls in the main frame; the real "
        "surface is a TrustSuperset DSR form in a child frame at "
        "dsr.trustsuperset.com/?orgId=7b774bc1-608f-8c4c-bb90- "
        "453b1540f48b. It is fully readable and correctly typed: "
        "input#first_name, #last_name, #email (the only required one), a "
        "request-type listbox whose options include 'Right to Opt-out of "
        "Sales' and 'Right to Limit Sensitive Personal Information' "
        "(matching the two choices the broker's own page advertises), "
        "#region, a listbox for Individual versus Authorized Agent, and "
        "#country. Both listboxes are button-plus-select pairs rather "
        "than plain selects, so they would need the listbox_button "
        "treatment. What blocks it is a CLOUDFLARE TURNSTILE, and of the "
        "invisible kind this module has been caught by before: "
        "challenges.cloudflare.com/turnstile/v0/api.js is loaded, there "
        "is NO .cf-turnstile element at all, and the only trace in the "
        "DOM is a hidden input[name='cf-turnstile-response'] whose id is "
        "minted per load (cf-chl-widget-0i90u_response on this visit). "
        "The page says 'Please verify you're human' in as many words. "
        "Note another row in this module already points at a different "
        "trustsuperset orgId, so a future reader should expect this "
        "vendor again and can reuse the shape. Dataset contact: "
        "info@persistent.id, which is the sibling brand's address and "
        "consistent with the two domains serving one surface."
    ),
    "smartmove-us": (
        "FCRA-REGULATED BACKGROUND SCREENING -- see the category note "
        "above NO_OPTOUT_SURFACE in this module for why this whole class "
        "gets no recipe. Verified 2026-09-23: the recorded do-not-sell "
        "URL never renders -- smartmove.us serves a Cloudflare "
        "interstitial ('Performing security verification ... This page is "
        "displayed while the website verifies you are not a bot', Ray ID "
        "a3fd3ab4892b31a9) and redirects with a __cf_chl_rt_tk challenge "
        "token. So the page is walled as well as being in the exempt "
        "class. SmartMove is TransUnion's landlord-facing tenant- "
        "screening product; a report is pulled by a landlord with the "
        "applicant's consent, and the consumer's recourse is the FCRA "
        "channel rather than this link. DATASET NOTE: the row is named "
        "'CTAM Leadshare Corp.' with contact zell@ctam.com, which matches "
        "neither TransUnion nor SmartMove -- flagged, not fixed."
    ),
    "crisil-com": (
        "Verified by browser render 2026-09-23: a real, correctly typed "
        "do-not-sell form behind a visible captcha. The recorded URL "
        "redirects to /en/home/crisil-privacy-notice/do-not-sell-my- "
        "personal-information.html, which carries form#do-not-sell-my- "
        "personal-info with input[name=email], [name=firstname], "
        "[name=lastname], [name=companyname] (all required), "
        "[name=designation], [name=streetaddress], [name=city] "
        "(required), [name=country] (required, a text input driving a "
        "picker rather than a select), [name=state] (required), "
        "[name=postalcode], and an input[type=button][name=submit]. "
        "Blocked by reCAPTCHA with a VISIBLE 'I'm not a robot' checkbox "
        "-- api.js loaded, a .g-recaptcha element, and the anchor and "
        "bframe challenge frames both present in child frames. Two "
        "further notes for whoever revisits: the page also carries three "
        "UNRELATED and computed-invisible forms (#loginForm, "
        "#forgotPassword, #registration_form_step1) whose fields would be "
        "easy to mistake for the request form's, so any recipe must scope "
        "to #do-not-sell-my-personal-info; and the required 'company "
        "name' field suggests this surface is aimed at business contacts, "
        "which is consistent with Crisil being an S&P Global ratings and "
        "research firm. Dataset contact: privacy1@crisil.com."
    ),
    "data-axle-com": (
        "Verified by browser render 2026-09-23: the single best-specified "
        "opt-out form in this batch, and walled. The recorded /do-not- "
        "sell-my-data/ leads to /privacy-rights-request/, whose Gravity "
        "Form #gform_4 carries input_1 First Name, input_3 Last Name, an "
        "address group (input_14.1 Street, 14.2 Line 2, 14.3 City, 14.4 a "
        "full 50-state-plus-territories select, 14.5 ZIP), input_9 Email, "
        "input_10 Phone -- all required -- a required 'Privacy Choice' "
        "select whose first option is 'Request to opt out of sale', a "
        "free-text detail box, an acknowledgement checkbox (input_13.1) "
        "and #gform_submit_button_4. It also carries a HONEYPOT: "
        "input_19, labelled 'Instagram', off-layout, which would have to "
        "go in forbidden_selectors. What blocks it is a CLOUDFLARE "
        "TURNSTILE (challenges.cloudflare.com plus a live .cf-turnstile "
        "element). Note the Gravity Forms input_NN names are per-form- "
        "build identifiers, so a recipe would be pinned to this revision "
        "even if the wall came down. DATASET NOTE: the row's "
        "opt_out_email is doba_privacy@donorbase.com, a different brand, "
        "which may mean several Data Axle brands were collapsed into one "
        "row."
    ),
    "datafy-com": (
        "Verified by browser render 2026-09-23: a complete and unusually "
        "thorough rights form, blocked twice over. datafy.com/opt-out "
        "carries #first-name, #last-name, #primary-email-address (all "
        "required), an add-a-secondary-email checkbox, a Myself/Another "
        "Individual radio pair (name=requester), required #country and "
        "#state selects, a five-way action radio group (name=action) "
        "whose first option is 'Opt-out of the sale and use of...', an "
        "#additional-request-notes textarea and a required #signature "
        "field. FIRST BLOCKER: a Cloudflare Turnstile "
        "(challenges.cloudflare.com, and a .cf-turnstile element inside "
        "the form's own styled wrapper). SECOND BLOCKER, and the more "
        "fundamental one: #mobile-device-advertising-id is REQUIRED. "
        "Datafy's records are keyed to device advertising ids rather than "
        "names, so this is the complementics/collectivedata pattern again "
        "-- a codebase gap, since no identity record here holds a MAID, "
        "and filling it would mean inventing one. Note also the controls "
        "carry ids but no name attributes, and the form's action resolves "
        "to DOM node references rather than a URL, so it submits through "
        "JavaScript. Dataset contact: support@datafy.com."
    ),
    "costar-com": (
        "Verified 2026-09-23: privacy.costar.com/DSAR-submission returns "
        "HTTP 403 from Akamai ('Access Denied ... You don't have "
        "permission to access ... Reference "
        "#18.46a7cb17.1790205071.37940b1a'), so the DSAR form never "
        "renders for an automated visitor. Same edge-wall shape as "
        "optoutprescreen-com. The URL naming suggests a genuine request "
        "surface is behind it, which is why this is blocked rather than "
        "absent -- a human on an ordinary connection should try it "
        "directly. The dataset records no email for this row, so the wall "
        "currently leaves no channel at all."
    ),
    "datonics-com": (
        "Verified by browser render 2026-09-23: a thorough, correctly "
        "typed rights form behind a VISIBLE captcha. "
        "datonics.com/privacy/privacy-choices carries form#datonics- "
        "privacy-form with #datonics-email (required), #datonics- "
        "resident-of (required state select), a conditional #datonics- "
        "other-location, #datonics-request-type (required, opening on "
        "'Opt-out of Targeted Advertising' and also offering delete / "
        "access / correct / limit sensitive), an optional #datonics-maid "
        "for a Mobile Advertising ID, a resident-versus-agent "
        "certification radio pair, and #datonics-submit. It ALSO carries "
        "a honeypot: input#datonics-website labelled 'Website', which "
        "would need to go in forbidden_selectors. Blocked by reCAPTCHA "
        "Enterprise rendered as a visible 'I'm not a robot' checkbox -- "
        "the anchor and bframe challenge frames are both present. The "
        "dataset records no email for this row, so the captcha currently "
        "leaves no channel."
    ),
    "decisionlinks-com": (
        "Verified by browser render 2026-09-23: the recorded /opt-out "
        "redirects to /legal-pages/opt-out, a Webflow form (#wf-form-Opt- "
        "Out-Requests) with Opt-Out-First-Name, -Last-Name, -Address, "
        "-City, -State, -Zip all required plus optional -Email and "
        "-Phone. Blocked by reCAPTCHA with a visible 'I'm not a robot' "
        "checkbox. Two things worth carrying forward. First, that "
        "challenge frame ALSO reports 'This site is exceeding reCAPTCHA "
        "Enterprise free quota' -- the second broker in two batches in "
        "that state (see datalinedata-com), which means the form may be "
        "failing for ordinary human visitors too, not just for this tool; "
        "anyone revisiting should check before assuming they are being "
        "singled out. Second, read the scope honestly: the page says "
        "DecisionLinks 'honors your right to opt-out of marketing "
        "messages', which is narrower than a database suppression, so a "
        "future writer should establish what this form actually removes. "
        "Dataset contact: support@decisionlinks.com."
    ),
    "dice-com": (
        "Verified by browser render 2026-09-23: a real and correctly "
        "typed form, walled. dice.com/about/ccpa/ carries form#form-dice- "
        "ccpa posting to ccpa-forwarder.svc.dhigroupinc.com with "
        "input#name and input#email (both required) and a three-way radio "
        "group (name=description) whose options are access / 'I wish to "
        "Opt-out of the sale of my data' / delete. It carries a HONEYPOT, "
        "input[name='user_name'], off-layout and unlabelled, which would "
        "need to go in forbidden_selectors. Blocked by reCAPTCHA "
        "Enterprise with a visible 'I'm not a robot' checkbox (anchor and "
        "bframe frames both present). The rights notice covers "
        "California, Colorado, Connecticut, Utah, Virginia, Montana, "
        "Delaware, Iowa, Nebraska, New Hampshire and New Jersey. Dataset "
        "contact support@dice.com is general support rather than a "
        "privacy alias."
    ),
    "dmsunsub-io": (
        "Verified 2026-09-23 by rendering https://dmsunsub.io/. The page "
        "is a genuine, complete DSAR webform served from dms- "
        "privacy.my.onetrust.com (action .../webform/506f8b15-11ff-4f...) "
        "asking First Name, Last Name, Email, Country, State and a "
        "10-digit mobile number, all required, plus a free-text detail "
        "box and an optional file upload.  It is blocked on two counts "
        "and the second is the one that matters. The first request "
        "returned HTTP 403 and landed on a __cf_chl_rt_tk URL, i.e. "
        "Cloudflare interposed a challenge before the form was reached. "
        "The form then rendered anyway -- but it carries reCAPTCHA: the "
        "page loads recaptcha/api.js, a g-recaptcha-response textarea "
        "sits inside the form, and a google.com/recaptcha/api2/anchor "
        "child frame is attached. Either one alone would stop an honest "
        "recipe.  The dataset's note for this row is worth keeping: the "
        "broker is Digital Media Solutions, listed by Incogni as 'DMS', "
        "and the correct contact is ComplianceDept@dmsgroup.com rather "
        "than the tax@ address once on file."
    ),
    "spyfly-com": (
        "Verified 2026-09-23. The dataset's opt_out_url (/help- "
        "center/privacy) is a readable privacy policy with no inputs; the "
        "real surface is the 'Do Not Sell My Personal Information' link "
        "it repeats five times, pointing at /help-center/privacy- "
        "requests. That is also exactly what SpyFly's own support reply "
        "(recorded in the dataset note, 2026-08-24) told the requester to "
        "use.  Requesting that page returns HTTP 403 and a Cloudflare "
        "interstitial -- 'Performing security verification ... This "
        "website verifies you are not a bot', challenges.cloudflare.com "
        "loaded, Ray ID a3fd4f7d8c2531a9. The form never renders, so "
        "there is nothing to transcribe. Note the shape of it: the policy "
        "page serves fine and only the request page is challenged, which "
        "is the same asymmetry seen on several brokers this pass -- the "
        "reading is open and the acting is walled. "
        "privacyinfo@spyfly.com is on file as a human channel if the wall "
        "holds."
    ),
    "teamdms-com": (
        "Verified 2026-09-23 by rendering https://teamdms.com/opt-out. "
        "Unlike most walled brokers this one has a real, short, well- "
        "built opt-out form sitting in plain view -- form#opt-out- "
        "form.contact-form with a request type select (optOutSelection), "
        "a submitter select, First Name and Last Name, all five required, "
        "under the heading 'Do not sell or share my personal "
        "information'. Everything it asks for has a source in "
        "resolve_fields. It is blocked despite that, on two independent "
        "counts.  Cloudflare Turnstile: the page loads "
        "challenges.cloudflare.com/turnstile/v0/api.js plus a site- "
        "specific turnstile-interop.js, and the form carries a hidden cf- "
        "turnstile-response input waiting to be filled by the widget. "
        "Second, and separately fatal even if the challenge were "
        "solvable: a hidden __RequestVerificationToken (ASP.NET anti- "
        "forgery) is minted per page load, so no stored POST can be "
        "replayed and any future recipe has to drive a real browser. "
        "optout@teamdms.com is published on the same page as a human "
        "channel."
    ),
    "dpcoptout-com": (
        "Verified 2026-09-23 by rendering https://dpcoptout.com/opt-out/. "
        "The form is real and unusually complete -- Gravity Forms "
        "#gform_1 collecting First Name, Last Name, a full address block "
        "(street, line 2, city, a state select, ZIP), an optional phone, "
        "a mail ID from the mailpiece and a free-text reason, described "
        "on the page as adding the person to a Do Not Mail list for the "
        "client mailings this broker processes.  It is walled by "
        "hCaptcha, and the DOM says so twice over: the page loads both "
        "the site's hcaptcha.js and an hcaptcha-gravity-forms.min.js "
        "integration, an .h-captcha element is present, and a hidden "
        "hcaptcha-widget-id sits inside the form. Nothing behind that can "
        "be submitted honestly.  Recorded for whoever revisits it: the "
        "field names are Gravity's POSITIONAL ids (input_4, input_6, "
        "input_8.1 ... input_8.5), which this sweep has repeatedly found "
        "to be re-numbered when a form is edited, so they would have to "
        "be re-read even if the captcha were cleared. The usual per-load "
        "Gravity tokens (gravity_forms_nonce, _wp_http_referer) are "
        "present too. customerservice@distpc.com is the human channel."
    ),
    "driveniq-com": (
        "Verified 2026-09-23. driveniq.com does not serve its own site: "
        "the request returned HTTP 403 and landed on https://visitiq.io/, "
        "the identity-resolution product the company now trades as. That "
        "page is a Cloudflare interstitial -- 'Confirm you are human / We "
        "need to check you're not a robot before you can enter this site' "
        "with an 'I am human' checkbox -- and the site proper is never "
        "reached, so no privacy page, let alone a form, could be read. "
        "Worth separating the two facts for whoever retries: the "
        "challenge is on the WHOLE SITE, not on a request page, which is "
        "a different situation from the brokers walled only at the point "
        "of acting. The dataset already records this row as email-method "
        "with support@driveniq.com, so even behind the wall a web recipe "
        "may not be what is wanted here -- but recording it as blocked is "
        "the honest state, because nobody has yet seen what visitiq.io "
        "offers."
    ),
    "dtn-com": (
        "Verified 2026-09-23 by rendering https://www.dtn.com/do-not- "
        "sell-my-information-form/. The form is genuine and thorough -- "
        "Gravity Forms #gform_47, headed 'DATA RIGHTS EXERCISE REQUEST', "
        "taking First Name, Last Name, Email, Zip (all required), an "
        "optional telephone, an eight-way checkbox set of rights "
        "including 'Do Not Sell or Share My Personal Information', an "
        "'Other Request' box and a required attestation checkbox.  It is "
        "walled by Cloudflare Turnstile: "
        "challenges.cloudflare.com/turnstile/v0/api.js is loaded, a .cf- "
        "turnstile element is in the DOM, and a hidden cf-turnstile- "
        "response_47 sits inside the form awaiting the widget's token. "
        "There is a SECOND challenge that would matter even if the first "
        "were gone, and it is the first of its kind found in this sweep: "
        "a required select labelled 'What is 20+55?'. That is an "
        "arithmetic quiz rendered as a dropdown -- Gravity's CAPTCHA- "
        "alternative field. The operands are regenerated per page load, "
        "so its correct option cannot be stored in a recipe; a browser- "
        "driven recipe would have to parse the question text and compute "
        "the answer, which is a capability nothing in this codebase has. "
        "Recorded so the next reader does not mistake it for an ordinary "
        "select with a fixed value. privacy@dtn.com is the published "
        "channel."
    ),
    "dynata-com": (
        "Verified 2026-09-23, confirming and extending the research "
        "already in the dataset's own note rather than repeating it. "
        "Rendering dynata.com and dynata.com/privacy (which resolves to "
        "/privacy/) found exactly one form on each page: the WordPress "
        "site search. No request-form anchor exists on either, matching "
        "the note's finding that the 'Do Not Sell My Information' control "
        "is a Usercentrics-rendered JavaScript pop-up with no linkable "
        "address.  It is recorded as blocked rather than as having no "
        "surface because a surface does exist -- it simply cannot be "
        "reached or completed. Per the dataset note, the pop-up asks "
        "first name, last name and email, then presents A CAPTCHA, and "
        "then requires clicking a 'Confirm Data Request' link sent by "
        "email. Two walls in series: the challenge, and an out-of-band "
        "confirmation hop. Even setting the captcha aside, there is no "
        "stable URL for a recipe to navigate to.  privacy@dynata.com and "
        "(833) 909-1804 / 833-681-0436 are the channels the published "
        "policy names, and email is the only automatable one."
    ),
    "lightcast-io": (
        "Verified 2026-09-23. Two findings, and the first is the one a "
        "careless reader would get wrong.  THE FORM ON THE PAGE IS NOT "
        "THE FORM. /privacy-request redirects to /legal/privacy-request, "
        "whose main frame contains one HubSpot form "
        "(#hsForm_9cf26c34-...) asking EMAIL and INDUSTRY -- and behind "
        "those, twenty-four hidden marketing fields: newsletter__c, "
        "lead_category, lead_action, annualrevenue, numberofemployees, "
        "company_sector. That is a newsletter signup wearing a privacy "
        "page's URL. Writing a recipe against it would subscribe the user "
        "to marketing while telling them they had opted out.  The real "
        "request form is in a CHILD FRAME -- "
        "privacyportal.onetrust.com/webform/0f61f895-... -- taking "
        "Country, State, First Name, Last Name, Email and phone, all "
        "required. It was invisible to the main-frame read and only the "
        "frame-walking probe found it, which is the same failure mode "
        "that produced two wrong entries earlier in this sweep.  It is "
        "blocked because that frame carries reCAPTCHA: a g-recaptcha- "
        "response textarea inside the OneTrust form, with both "
        "recaptcha/api2/anchor and .../bframe frames attached. "
        "legal@lightcast.io is the published channel."
    ),
    "edvisors-com": (
        "Verified 2026-09-23. The dataset's /third-party-opt-out/ "
        "redirects to /your-privacy-choices/, and the real surface there "
        "is #centralized-privacy-settings -- an ASP.NET form POSTing to "
        "/async/Communication/Centralize... which asks for state of "
        "residence, whether the requester is acting for themselves or as "
        "an authorised agent, what kind of person they are (consumer, "
        "employee, job applicant, business contact, contractor) and an "
        "email, then 'Continue' into a further step.  Blocked on two "
        "independent counts. reCAPTCHA ENTERPRISE: a g-recaptcha-response "
        "textarea sits inside the form and the attached frames are "
        "recaptcha/ENTERPRISE/anchor and /enterprise/bframe, not the "
        "ordinary api2 pair -- worth noting as a distinct variant, since "
        "enterprise mode scores the whole session rather than gating on a "
        "puzzle. Second, a per-load __RequestVerificationToken, so no "
        "stored POST can be replayed.  Also on the page and NOT to be "
        "mistaken for the opt-out: two 'GetGuideModal' forms harvesting "
        "emails for a FAFSA guide and a student loan handbook, each with "
        "its own AgreeToTerms checkbox. The dataset records no opt-out "
        "email for this row."
    ),
    "mastercard-us": (
        "Verified 2026-09-23. The dataset's URL is Mastercard's data- "
        "subject request portal scoped to Ekata (.../dgr-public/personal- "
        "data-request.html#/ekata/request/personalinfo), Ekata being the "
        "identity-verification business Mastercard acquired. Requesting "
        "it returns HTTP 403 and an Akamai edge refusal: 'Access Denied "
        "-- You don't have permission to access ... on this server', "
        "Reference #18.65c90b17.1790206369.1f446d44, served from "
        "errors.edgesuite.net.  Recorded as blocked rather than undecided "
        "because the refusal is at the CDN edge, before any application: "
        "no captcha was offered, no challenge page, no form -- the "
        "request simply is not served. Whether that is geography, the "
        "client fingerprint or a rule against the whole unauthenticated "
        "path cannot be told from outside.  Worth a retry from a "
        "different network before anyone concludes the portal is gone, "
        "since an Akamai deny of this shape is often reputational. "
        "privacysupport@ekata.com is the address on file."
    ),
    "evs7-com": (
        "Verified 2026-09-23 by rendering https://www.evs7.com/personal- "
        "information-request. A real request form is there -- Contact "
        "Form 7, ten visible fields including a select and a three-way "
        "checkbox group, most of them required -- under a page titled "
        "'Personal Information Request' and linked from the site as 'Do "
        "Not Sell My Personal Informaton' [sic].  Blocked by reCAPTCHA, "
        "and it is the invisible v3 variant: the form carries a hidden "
        "_wpcf7_recaptcha_response, the page loads gstatic's "
        "recaptcha__en.js plus a site integration script, a grecaptcha- "
        "badge is rendered and a recaptcha/api2/anchor frame is attached. "
        "There is no checkbox to see, which is exactly why a static fetch "
        "would have called this form clean.  Recorded for anyone who "
        "revisits: the field names are Contact Form 7's auto-generated "
        "ones -- text-163, text-52, text-637, text-566, menu-415, "
        "text-863, tel-877, email-382, text-190 -- carrying no meaning "
        "and re-minted whenever the form is edited in the WordPress "
        "admin. Even past the captcha, they would have to be re-read "
        "against the live page and matched to their labels by position, "
        "which is fragile. richard@evs7.com is the address on file."
    ),
    "eltoro-com": (
        "Verified 2026-09-23. El Toro's /do-not-sell-my-personal- "
        "information/ embeds a OneTrust request portal "
        "(privacyportal.onetrust.com/webform/96e88ef7-...) in a child "
        "frame; the main frame has no form at all, so this is another one "
        "that would have read as 'no form found' before the probe learned "
        "to walk frames. The form itself is thorough -- First/Last name, "
        "Email, Street, City, Country, State and Zip all required, plus "
        "optional IP Address(es) and MAID(s) boxes, under a heading "
        "naming all three rights at once: 'DO NOT SELL OR SHARE MY "
        "PERSONAL INFORMATION / OPTOUT OF TARGETED ADVERTISING / LIMIT "
        "THE USE MY SENSITIVE INFORMATION'.  It is blocked by a captcha "
        "of a kind not yet seen in this sweep: BotDetect, not reCAPTCHA "
        "or hCaptcha. The tell is a cluster of hidden inputs -- "
        "BDC_VCID_angularBasicCaptcha, BDC_BackWorkaround_..., "
        "BDC_Hs_..., BDC_SP_... -- beside a VISIBLE text "
        "input[name='captchaCode'] labelled 'Captcha'. That is a server- "
        "rendered image challenge the user types back, so unlike an "
        "invisible reCAPTCHA there is no token to be granted and no "
        "scoring to pass: it cannot be satisfied by a recipe at all. "
        "Note the optional MAID box as a small piece of good news amid "
        "the wall -- several brokers this sweep REQUIRE a mobile "
        "advertising id, which nothing in this codebase can supply; here "
        "it is optional. privacy@eltoro.com is published."
    ),
    "enformion-com": (
        "Verified 2026-09-23. Enformion is worth reading carefully "
        "because the page the dataset points at is the wrong one and the "
        "right one is heavily defended.  The recorded /do-not-sell/ is a "
        "NOTICE, not a form. Its only form is a HubSpot box asking First "
        "name, Last name, BUSINESS EMAIL and Industry -- a sales lead "
        "capture, and the 'business email' label is the tell. The actual "
        "surface is the 'Opt-Out Form' link to /opt-out/, titled "
        "'Enformion Privacy Portal', which states its own scope usefully: "
        "it covers 'Enformion and our affiliated websites, including "
        "Tracers.com and Endato.com', so one request there reaches three "
        "brokers.  That page renders FOUR copies of the same .enf- "
        "zendesk-form, one per requester type, shown and hidden by script "
        "-- so any recipe must disambiguate by visibility rather than by "
        "selector, and an unscoped match would hit four elements. Two of "
        "the four additionally require DATE OF BIRTH, phone and full "
        "address.  Blocked by reCAPTCHA Enterprise: a g-recaptcha- "
        "response textarea in every copy, with four enterprise/anchor and "
        "four enterprise/bframe frames attached.  One detail worth "
        "carrying forward: the form contains "
        "input[type=number][name='yourFavoriteNumber'], which is a "
        "HONEYPOT wearing a friendly name rather than a hidden one. This "
        "sweep has been detecting honeypots by computed style; this one "
        "would also need to be caught by reading the name, because a "
        "field asking a human for their favourite number on a privacy "
        "form is not a real question. databroker@enformion.com is the "
        "published channel."
    ),
    "eprodirect-com": (
        "Verified 2026-09-23. The form at /do-not-sell-my-personal- "
        "information/ is real and well-scoped -- WPForms #wpforms- "
        "form-208196 with a 'Please do not sell my personal information' "
        "checkbox, a required email, and an optional address block "
        "(address1, address2, city, a state select, postal) -- under copy "
        "saying California residents may use it to opt out of sale or "
        "rental, access their information and delete it.  Blocked by "
        "reCAPTCHA: the page renders a .wpforms-recaptcha-container and a "
        ".g-recaptcha element, a g-recaptcha-response textarea sits in "
        "the form, and both api2/anchor and api2/bframe frames are "
        "attached -- the bframe being the tell that this is the CHECKBOX "
        "variant with a puzzle behind it, not invisible v3.  Recorded for "
        "a future attempt: input[name='wpforms[hp]'], labelled 'Name', is "
        "WPForms' standard honeypot and would have to go in "
        "forbidden_selectors -- it is a plain visible-looking text input, "
        "so the trap here is the opposite of listmatch's, caught by "
        "knowing the framework rather than by computed style. "
        "optout@eprodirect.com is the address on file."
    ),
    "propstream-com": (
        "Verified 2026-09-23. /privacy-request redirects straight out to "
        "a hosted OneTrust portal (privacyportal.onetrust.com/webform/23d "
        "bbccc-a76c-4410-a68e-f247d70e566c/...), titled 'PropStream "
        "Privacy Request', asking a short set: an 'I am a' role picker, "
        "requestor's country of residence, First name, Last name and "
        "Email, all required.  Blocked by reCAPTCHA -- g-recaptcha- "
        "response inside the form, api2/anchor and api2/bframe frames "
        "attached. Short as the form is, there is no honest way past "
        "that.  Two things worth noting for whoever returns. The country- "
        "of-residence field is the jurisdiction-gating pattern this sweep "
        "keeps meeting (hightouch, crunchbase, dstillery): the field set "
        "can change once a country is chosen, so even past the captcha "
        "the form would have to be re-read after that selection rather "
        "than transcribed from its initial state. And PropStream serves a "
        "SEPARATE channel for a specific population -- "
        "redactionrequest@propstream.com, with a 'Public Servant "
        "Redaction Request' FAQ -- which is not the general opt-out but "
        "is the right route for anyone eligible. "
        "privacyinquiry@propstream.com is the general address."
    ),
    "force-com": (
        "Verified 2026-09-23. Two layers of confusion resolved, then a "
        "wall.  DATASET DEFECT, flagged and NOT fixed: the row is "
        "e.Republic (a government-and-education media company) but is "
        "KEYED to force.com, which is Salesforce's hosting domain rather "
        "than anything e.Republic owns. The broker_id 'force-com' is "
        "therefore meaningless, and any future row hosted on Salesforce "
        "would collide with it.  The recorded URL "
        "(erepublic.secure.force.com/PrivacyRequest/) is dead: Salesforce "
        "answers 'URL No Longer Exists'. So does erepublic.com/privacy- "
        "policy/. The live surface was found on the footer of "
        "e.Republic's own 404 page -- erepublic.my.salesforce- "
        "sites.com/PrivacyRequest/ -- i.e. the same app migrated from the "
        "retired *.secure.force.com hostname to the current "
        "*.my.salesforce-sites.com one.  That form is genuine (a request- "
        "type select, name, phone, email, full address, a state select, a "
        "comments box and a declaration 'under penalty of...' checkbox) "
        "and is blocked three times over:  * reCAPTCHA, via a hidden "
        "recaptchaToken input. * A HONEYPOT named almost plausibly: a "
        "hidden text input ending   ':HomeAddressHP' -- the HP suffix "
        "being the only giveaway on a form   that also asks for a real "
        "home address. * A TIMING TRAP, which is new in this sweep and "
        "worth naming: the form   carries formLoadTime and TimeSpent "
        "inputs, so the server judges HOW   LONG the form took to fill. A "
        "recipe that fills instantly is   detectable even with every "
        "field correct and every honeypot avoided.  And even past all "
        "three, the field names are Visualforce's positional auto-ids -- "
        "j_id0:j_id2:j_id3:j_id31:j_id36 and so on -- which renumber "
        "whenever the page is edited. This is the most fragile naming "
        "scheme the sweep has met, worse than Gravity's input_N. "
        "privacy@erepublic.com is the published channel."
    ),
    "evorra-com": (
        "Verified 2026-09-23. The dataset's product-privacy-policy page "
        "is an explainer whose only form is Gravity #gform_2 -- an email "
        "box with 'I would like to subscribe' beside it, i.e. a "
        "NEWSLETTER, not an opt-out. The real surface is the page's own "
        "'opt-out' link, which lands on privacy.evorra.com, and that form "
        "is genuinely good: a requester-type radio, first/last name, "
        "email, country and state of residence, four rights checkboxes, "
        "an authorised-agent block with a file upload, and a details box. "
        "Blocked by Cloudflare Turnstile -- challenges.cloudflare.com "
        "loaded, a .cf-turnstile element present, a hidden cf-turnstile- "
        "response awaiting its token.  Two further obstacles recorded so "
        "nobody thinks clearing the Turnstile would be enough. There is a "
        "HONEYPOT: a hidden input[name='website'] on a form that never "
        "asks a human for a website. And the page states an out-of-band "
        "hop for some request types: 'If you ask to access or correct "
        "your own data, we email you a code to confirm you control the "
        "address' -- there is an input[name='code'] on the form for "
        "exactly that. The opt-out right may or may not be gated the same "
        "way; the wording only names access and correction.  One more "
        "finding on the explainer page, worth carrying forward as a "
        "pattern: gform_2 carries a hidden input named "
        "gform_submission_speeds. That is Gravity Forms measuring HOW "
        "FAST the form was filled, the same class of timing trap found on "
        "e.Republic's Salesforce form. It is becoming common enough to "
        "expect. privacy@evorra.com is the published channel."
    ),
    "explorium-ai": (
        "Verified 2026-09-23. The request returned HTTP 429 and landed on "
        "/do-not-sell-my-personal-info/?__cf_chl_rt_tk=... -- a "
        "Cloudflare challenge page reading 'Performing security "
        "verification ... This website uses a security service to protect "
        "against malicious bots', Ray ID a3fda1ad0f5131a9, with "
        "challenges.cloudflare.com loaded and a stray cf-turnstile- "
        "response input outside any form. The opt-out form behind it "
        "never renders, so there is nothing to transcribe.  The 429 is "
        "worth separating from the challenge itself: it is a RATE-LIMIT "
        "status, not a refusal, so this particular response may say as "
        "much about how recently the host had been asked as about the "
        "client. A retry from a cold address would be a fairer test "
        "before concluding the page is walled against everyone. Note also "
        "that the dataset's path (/do-not-sell-my-personal-information/) "
        "redirects to the shorter -info/ form of the URL. "
        "privacy@explorium.ai does not appear in the dataset for this "
        "row, which records no opt-out email at all."
    ),
    "famousbirthdays-com": (
        "Verified 2026-09-23. Famous Birthdays publishes no privacy "
        "request form: the dataset's /contact is a general contact form "
        "-- Your Name, Your Email, an 'Is this related to an existing "
        "Famous Birthdays URL?' select, a Comments box and Send -- "
        "POSTing to /contact/submit. There is no do-not-sell link "
        "anywhere on the site and the dataset records no opt-out email. "
        "Blocked by reCAPTCHA: a .g-recaptcha element is rendered on the "
        "contact page. So even the general channel cannot be driven.  One "
        "field needs explaining so it is not mistaken for a honeypot: "
        "input[name='url'] computes to hidden at rest, but it is "
        "CONDITIONAL, not a trap -- it is revealed when the 'Is this "
        "related to an existing ... URL?' select is set to 'Yes'. A "
        "recipe would need appears_later=True on it rather than "
        "forbidden_selectors, which is the opposite treatment, and the "
        "distinction is only visible by reading the select beside it. "
        "See the search leg for the more interesting half of this broker: "
        "its search IS verified, both ways, and is held out of RECIPES "
        "only by a zero-width submit button that Playwright cannot click."
    ),
    "fideo-ai": (
        "Verified 2026-09-23 by driving the wizard one step, reading "
        "only. app.fideo.ai/your-privacy-choices is a multi-step SPA: "
        "step one offers five radio choices OUTSIDE any form element -- "
        "Access my data, Correct my data, Do not sell my data, Limit "
        "sharing of my data, Delete my data -- and a CONTINUE button. "
        "Selecting the do-not-sell option and continuing advances to "
        "/your-privacy-choices/country, a second step holding one unnamed "
        "text input and another CONTINUE. How many steps follow is "
        "unknown; the flow was not driven further.  Blocked by reCAPTCHA, "
        "which the app loads explicitly in EXPLICIT RENDER mode "
        "(recaptcha/api.js?onload=googleRecaptchaLoaded&render=explicit). "
        "That is worth naming: in explicit mode the widget is created by "
        "the application at a moment of its choosing, so it will not "
        "appear in the DOM until whichever step calls for it -- an early "
        "step looking clean says nothing at all about the last one.  Two "
        "details that would matter if the captcha were ever cleared. "
        "EVERY RADIO HAS value='on' -- all five of them -- so a recipe "
        "cannot select by value and must key on the surrounding label "
        "text or on position, which is fragile. And no control on either "
        "step carries a name attribute, so selectors would have to be "
        "built from React-generated structure. dpo@fideo.ai is the "
        "published channel, and the page also names an authorised-agent "
        "route by email."
    ),
    "fmadata-com": (
        "Verified 2026-09-23. The form at /opt-out-requests/new is real "
        "and unusually clear about its own scope, offering a request-type "
        "select with 'Opt out', 'Disclosure' and 'Deletion', a name, a "
        "full address block and an authorised-agent yes/no radio with a "
        "conditional agent-name field.  Blocked by hCaptcha: hcaptcha.com "
        "is loaded, an .h-captcha element is present, a "
        "newassets.hcaptcha.com challenge frame is attached, and an "
        "h-captcha-response textarea sits in the form. A leftover "
        "g-recaptcha-response textarea sits beside it -- the same "
        "migration fossil seen on listmatch-com, where a site moved from "
        "reCAPTCHA to hCaptcha and left the old field in place. Two "
        "response fields does not mean two challenges; only the hCaptcha "
        "one is live.  Second blocker regardless: a per-load Rails "
        "authenticity_token, so no stored POST can be replayed.  A small "
        "trap for whoever transcribes this later. The address fields' "
        "PLACEHOLDERS ARE A REAL, COMPLETE ADDRESS -- '4845 Pearl East "
        "Cir Ste', 'Boulder', 'CO', '80301-6112' -- rather than generic "
        "hints. A reader skimming the probe output could easily mistake "
        "those for pre-filled values and conclude the form arrives partly "
        "completed. It does not; they are placeholder text. The broker "
        "also publishes a PDF alternative at /opt-out-requests/new.pdf "
        "for anyone who cannot use the web form, and privacy@fmadata.com "
        "is on file."
    ),
    "firstorion-com": (
        "Verified 2026-09-23. privacy.firstorion.com links 'GET STARTED "
        "ONLINE' to /opt-out/step-one, which is a genuine first step: "
        "First Name, Last Name, Email Address, Phone Number, a two-way "
        "radio (request removal of personal information, or access it), "
        "an attestation checkbox and a 'Send Confirmation' button. "
        "Blocked by reCAPTCHA -- g-recaptcha-response in the form, with "
        "both api2/anchor and api2/bframe frames attached, the bframe "
        "indicating the checkbox-with-puzzle variant rather than "
        "invisible v3.  Two further obstacles behind it. The button says "
        "'Send Confirmation' and the page explains the flow: the request "
        "is not completed here, an email confirmation follows -- the out- "
        "of-band hop that already rules out several brokers in this "
        "module. And NONE OF THE FOUR TEXT INPUTS HAS A NAME ATTRIBUTE; "
        "only the radio group carries one (name='opt-out'). So even past "
        "the captcha, selectors would have to be built from position or "
        "from React-generated structure, which is the same fragility as "
        "fideo-ai in this batch.  Worth recording as a point in the "
        "broker's favour: it states a real prerequisite honestly -- 'you "
        "will need access to the device associated with the phone number "
        "you are opting out' -- and it publishes a postal alternative for "
        "people without an email address. ccparequests@firstorion.com is "
        "the address on file."
    ),
    "flashintel-ai": (
        "Verified 2026-09-23. The dataset's flashintel.ai/dont-sell-my- "
        "information redirects to www.FLASHLABS.ai/dont-sell-my- "
        "information -- a rebrand the dataset does not record, flagged "
        "here and left unfixed. The dataset's contact for the row, "
        "legal@myflashcloud.com, is a third distinct name again.  The "
        "form is real: Full Name, Current Company, Profile URL and "
        "Business Email, all four required, under a 'Submit Request' "
        "button. Blocked by reCAPTCHA -- a g-recaptcha-response textarea "
        "inside the form with both api2/anchor and api2/bframe frames "
        "attached.  Worth recording even past the captcha, because it is "
        "a second, independent obstacle of a kind this sweep keeps "
        "meeting: the form asks for PROFILE URL and CURRENT COMPANY, both "
        "required. This is a B2B contact enrichment product, so the "
        "record it holds is keyed to a professional profile rather than "
        "to a household. resolve_fields has no source for either, and a "
        "profile URL is not something the tool could infer -- the user "
        "would have to supply it. Same shape as the MAID-keyed brokers "
        "(complementics, collectivedata, datafy, factori): the identifier "
        "the broker files you under is not one this codebase collects."
    ),
    "focus-usa-com": (
        "Verified 2026-09-23, and this one is the batch's best argument "
        "for rendering pages rather than fetching them.  THE CAPTCHA "
        "LOADS NO SCRIPT. Scanning script[src] for recaptcha, hcaptcha "
        "and turnstile on /your-privacy-choices/ returns an EMPTY LIST, "
        "and there is no captcha element in the DOM. The form is "
        "nonetheless challenged: WPForms #wpforms-form-32948 carries a "
        "field whose label reads 'Question (Captcha) *', with an answer "
        "box (wpforms[fields][5][a]) beside three hidden inputs -- [n1], "
        "[n2] and [cal]. That is a server-rendered ARITHMETIC challenge "
        "whose operands are minted per page load, so no stored answer can "
        "satisfy it. dtn-com in this module has the same thing rendered "
        "as a select ('What is 20+55?'); here it is free text. Anyone "
        "grepping for captcha scripts would have called this form clean "
        "and shipped a recipe that fails on every submission.  TWO "
        "HONEYPOTS, one of them deliberately cruel. wpforms[fields][1] is "
        "a hidden text input whose label reads 'Last * Name' -- it MIMICS "
        "the real Last Name field (wpforms[fields][17]) closely enough "
        "that a recipe-writer matching on labels rather than on names "
        "would fill the trap and skip the real box. wpforms[fields][2] is "
        "a second, unlabelled hidden text input. Both would have to go in "
        "forbidden_selectors.  Transcribed anyway, because everything "
        "else about it is good and someone may return: First name [16], "
        "Last Name [17], Street [13], City [14], State [12] (select), Zip "
        "[15] all required; Email [10] as a primary/secondary confirm "
        "PAIR, with Email 2 [40] and a hidden Email 3 [41] likewise; "
        "Phone [27] and Phone 2 [28], each rendered as a visible wpf- "
        "temp-* input backed by a hidden real field, so a recipe must "
        "fill the temp one and let the widget populate the other; a "
        "required radio [26] 'Are you submitting request on behalf of "
        "someone?' (Yes/No); a hidden File Upload; and the rights "
        "checkboxes [30][] with the clean values 'Access', 'Correct', "
        "'Delete', 'Opt-Out'. Submit is a BUTTON, not an input.  Finally, "
        "a finding with no parallel elsewhere in this module: Focus USA "
        "PUBLISHES A REST OPT-OUT API, documented at /docs/optout-api/ -- "
        "POST to api.focususaconnect.com/optout, JSON response, x-api-key "
        "header, 600 requests per minute. It is not a route an individual "
        "can use: keys are issued by a sales rep (info@focus-usa.com). "
        "But it is the only programmatic opt-out channel the sweep has "
        "found, and it is worth knowing exists. privacy@focus-usa.com is "
        "the published address."
    ),
    "freewheel-com": (
        "Verified 2026-09-23. /do-not-sell-my-information carries a "
        "short, honest Contact Form 7 -- First Name, Last Name, Email "
        "Address, all required, under 'Complete the form below to opt- "
        "out' -- and it is blocked twice over.  reCAPTCHA, in the "
        "INVISIBLE v3 variant: the form carries a hidden "
        "_wpcf7_recaptcha_response, the page loads gstatic's "
        "recaptcha__en.js plus a site integration script, a grecaptcha- "
        "badge renders and an api2/anchor frame is attached. There is no "
        "checkbox to see, which is precisely why a static fetch would "
        "have called this form clean -- the standing rule in the probe "
        "tool's docstring, demonstrated again.  And a HONEYPOT: "
        "input[name='xv_website_url'], hidden, on a form that never asks "
        "a human for a website. It would need to go in "
        "forbidden_selectors.  Worth noting for whoever revisits: unlike "
        "most Contact Form 7 installs in this sweep, the field names here "
        "are MEANINGFUL (first-name, last-name, email) rather than auto- "
        "generated text-NNN, so the transcription above would survive an "
        "edit of the form. legalnotices@freewheel.com is the published "
        "address."
    ),
    "fullcontact-com": (
        "Verified 2026-09-23. platform.fullcontact.com/your-privacy- "
        "choices is a multi-step rights wizard -- five buttons outside "
        "any form (Access My Data, Correct My Data, Do Not Sell or Share, "
        "Limit Sharing Of My Data, Delete My Data) and no fields until "
        "one is chosen. Structurally identical to fideo-ai in the "
        "previous batch.  Blocked by reCAPTCHA: gstatic's recaptcha "
        "script and google.com/recaptcha are both loaded on the landing "
        "step, before any field exists, so the challenge is attached to "
        "the flow rather than to a particular page.  DATASET NOTE, "
        "flagged and not fixed: the row's contact is "
        "privacy@ziffdavis.com, not a fullcontact.com address. "
        "FullContact was acquired and its rights requests now route to "
        "Ziff Davis. That is correct rather than wrong -- but it means "
        "anyone reconciling this row by domain will think the address is "
        "a mistake, and it is worth knowing it is not. The page also "
        "names an authorised-agent route by email to "
        "privacy@fullcontact.com, which still resolves."
    ),
    "retention-com": (
        "Verified 2026-09-23. app.retention.com/optout/ is a proper, "
        "purpose-built opt-out page -- #verify_opt asking only an Email "
        "Address and a Zipcode, with an 'I am an authorized agent' "
        "checkbox revealing an agent email field, and a single OPT-OUT "
        "button. The scope is stated plainly: 'All US residents may "
        "exercise their right to opt-out by submitting this form or "
        "calling 1-(855) 306-2455 and leaving a voicemail withdrawing "
        "their consent to resell their personal information.'  Blocked by "
        "reCAPTCHA -- a .g-recaptcha element, a g-recaptcha-response "
        "textarea in the form, and both api2/anchor and api2/bframe "
        "frames attached, the bframe indicating the checkbox-with-puzzle "
        "variant. Second and independently, a per-load Rails "
        "authenticity_token, so no stored POST can be replayed.  Two "
        "hidden inputs, optout-e and optout-z, sit beside the visible "
        "email and zipcode boxes. They are NOT honeypots as far as can be "
        "told -- the naming suggests they carry pre-filled values for "
        "people arriving from a personalised unsubscribe link -- but they "
        "are close enough to the honeypot pattern that anyone "
        "transcribing this should establish which they are before filling "
        "or forbidding either.  Note the telephone alternative above: a "
        "voicemail line is an unusually accessible second channel and is "
        "worth surfacing to a user for whom the captcha is the blocker. "
        "support@getemails.com is the address on file, under the row's "
        "registered name GETEMAILS LLC."
    ),
    "giantpartners-com": (
        "Verified 2026-09-23. The form itself is one of the better ones "
        "in this module -- a HubSpot form taking email, first name, last "
        "name, street address, city, state/region and postal code, all "
        "required, with a checkbox whose text is admirably explicit: 'I "
        "am requesting for the removal of my information from databases "
        "used for resale or licensing.' Every value it wants has a source "
        "in resolve_fields.  Blocked by reCAPTCHA ENTERPRISE in invisible "
        "mode: a g-recaptcha-response textarea and hs-recaptcha-response "
        "hidden input in the form, an hs_recaptcha wrapper, a grecaptcha- "
        "badge, and an enterprise/anchor frame attached. Nothing is "
        "visible on the page, which is again why this had to be rendered "
        "rather than fetched.  WORTH RECORDING FOR CONTRAST, because it "
        "is a different species from the arithmetic challenge on focus- "
        "usa and dtn: the form also carries select[name='i_am_human'] "
        "labelled 'I am human*' with options Please Select / Yes / No, "
        "and it is REQUIRED. That is a human-check a recipe COULD answer "
        "honestly -- the user is a human, and answering Yes asserts "
        "nothing false. Filling it would be legitimate. It is the "
        "invisible reCAPTCHA sitting behind it, not this select, that "
        "closes the door. The distinction matters: 'declare you are "
        "human' is automatable, 'prove it by solving a per-load puzzle' "
        "is not.  Two hidden inputs, contact_me_by_fax and "
        "contact_by_fax, are HubSpot property fields rather than "
        "honeypots as far as can be told, but should be left alone "
        "regardless. nikki@giantpartners.com is the dataset contact; the "
        "row separately offers a California-specific page not examined "
        "here."
    ),
    "grata-com": (
        "Verified 2026-09-23. grata.com/legal/do-not-sell-my-information "
        "carries a HubSpot form and the page says what it is for: 'If you "
        "would like to ensure your personal information is not sold, "
        "please complete this form.'  Blocked by reCAPTCHA ENTERPRISE, "
        "invisible: a g-recaptcha-response textarea and hs-recaptcha- "
        "response hidden input inside the form, with an enterprise/anchor "
        "frame attached. Nothing visible on the page says so.  Two things "
        "are worth recording even so. First, the form asks for exactly "
        "ONE piece of information -- 'Enter your email...*' -- while the "
        "page text above it says the request 'should include your contact "
        "information and describe your request with sufficient detail "
        "that allows us to properly understand, evaluate, and respond to "
        "it'. The instructions ask for detail the form provides nowhere "
        "to put. Anyone following the prose would look for fields that do "
        "not exist.  Second, the form is wired for MARKETING ATTRIBUTION: "
        "alongside the email it carries hidden data_source__c, "
        "recent_lead_source, form_level, landing_page_url, "
        "recent_conversion_url and the full utm_source / medium / "
        "campaign / content / term set. A do-not-sell request on this "
        "form is captured with the same lead-tracking apparatus as a demo "
        "enquiry. Not a blocker, and probably just the default HubSpot "
        "template, but it should be left untouched by any future recipe "
        "and it is the sort of detail worth having on record. "
        "hello@grata.com is the published address."
    ),
    "greatlakeslists-com": (
        "Verified 2026-09-23, and the first finding is a DATASET DEFECT, "
        "flagged and deliberately not fixed. The recorded opt_out_url, "
        "greatlakeslists.com/opt_out_request.php, still returns HTTP 200 "
        "but no longer contains a form -- it renders the site's generic "
        "chrome and nothing else. A tool following the dataset would find "
        "an apparently healthy page with nothing on it and could easily "
        "record 'no surface'. The live surfaces are reached only from the "
        "footer: /do-not-sell-ca ('California Consumer Privacy Act "
        "Requests') and /do-not-sell-non-ca ('Opt Out Requests Web "
        "Form'). A 200 that quietly stopped being the page it used to be "
        "is a worse failure than a 404, which would at least announce "
        "itself.  Both live pages are blocked by reCAPTCHA v3 -- "
        "api.js?render= with site key "
        "6LeG5lAbAAAAAB2mnbwCEYHiihwLefn_Udbwksfe, plus the gstatic "
        "runtime and an anchor frame, on each. v3 is score-based and "
        "entirely invisible: there is no checkbox to click and no puzzle, "
        "which means an automated submission is not refused so much as "
        "silently scored down. That failure mode is particularly bad for "
        "this tool, because the request can appear to go through.  Both "
        "are also ROLE-GATED wizards before any fields appear: 'Who is "
        "submitting this request? ... I am the person opting out / I am "
        "an authorized agent'. No field set was reached, so nothing "
        "beyond the choice step is recorded. The pages do offer a 'Check "
        "the status of your opt-out request' route, which is unusual and "
        "useful, and the dataset records no opt-out email for this row, "
        "so the web form is the only channel."
    ),
    "grin-co": (
        "Verified 2026-09-23. grin.co/data-privacy-form/ returns HTTP 200 "
        "but the body is a Cloudflare interstitial -- title 'One moment, "
        "please...', text 'Please wait while your request is being "
        "verified...'. No form, no fields, nothing else in the DOM.  This "
        "is the shape that most deserves care in this module, because it "
        "lies twice. The status code says success. The page renders "
        "without error. A probe that only checked for HTTP 200 and then "
        "counted form elements would report 'page loads fine, no opt-out "
        "form present' and the row would be closed as no-surface -- a "
        "conclusion that is exactly backwards, since the URL is named "
        "data-privacy-form and the form is almost certainly sitting "
        "behind the challenge.  Recorded as blocked rather than undecided "
        "because the obstacle is deliberate and persistent: Cloudflare's "
        "managed challenge is aimed at precisely the kind of headless "
        "automation this tool performs, and waiting longer does not "
        "resolve it. The only honest statement about what is behind it is "
        "that nothing has been seen.  The dataset records no opt-out "
        "email for this row, so there is no fallback channel to offer a "
        "user. A human with an ordinary browser will pass the challenge "
        "without noticing it, so the page is reachable to people and not "
        "to this tool -- worth saying plainly if the row is ever surfaced "
        "in a report."
    ),
    "healthcare-com": (
        "Verified 2026-09-23. /data-request/request-form/ is a proper "
        "'Data Privacy Rights Request Form': a case_type select offering "
        "right to know / right to delete / right to opt-out of sales, "
        "then first_name, last_name and email (all required), zip_code "
        "(optional) and phone_number (required), under a 'continue' "
        "button.  Blocked by INVISIBLE reCAPTCHA -- the gstatic runtime "
        "plus an api2/anchor frame with size=invisible, anchor-ms=120000. "
        "Nothing on the form itself shows it, and notably there is no "
        "g-recaptcha-response element inside the form at all; the only "
        "trace is the frame. A check that looked for a captcha field "
        "WITHIN the form would have missed this one entirely.  Two "
        "further notes. The page states an identity-matching rule that "
        "would constrain any future recipe: 'you must enter the EXACT "
        "email address and phone number previously provided on our "
        "website.' A person who never gave healthcare.com an email cannot "
        "use this form at all, and one who gave a different address than "
        "the tool holds will be rejected -- so even past the captcha, "
        "success depends on knowing which identifiers the broker has, "
        "which this tool does not.  And a curiosity worth cross- "
        "referencing: the form's action attribute reads literally 'ref: "
        "<Node>', a React ref object stringified into the DOM. "
        "foursquare-com has the identical defect in its id attribute. Two "
        "unrelated brokers, same framework bug -- worth knowing the "
        "pattern, because such a value looks like a URL and is not one. "
        "Also note the page title is '2026 Health Insurance Plans', i.e. "
        "the rights form is served inside the marketing site's shell. "
        "info@healthcare.com is published as the fallback."
    ),
    "healthwisedata-com": (
        "Verified 2026-09-23. One of the most complete rights forms in "
        "this module, and blocked twice over.  The Elementor form takes "
        "First Name, Last Name and Email (required), Address Line 1/2, "
        "City, State and ZIP, a Request Type select with unusually clean "
        "values ('Access My Data', 'Delete My Data', 'Correct My Data', "
        "'Opt Out of Sale/Sharing', 'Limit Use of Sensitive Information', "
        "'Appeal a Decision'), an optional details textarea, a REQUIRED "
        "authorized_agent Yes/No radio pair and an authorization-letter "
        "upload. The page also enumerates the nineteen states whose "
        "residents it serves. Every field has a source in resolve_fields. "
        "FIRST WALL -- CLOUDFLARE TURNSTILE: "
        "challenges.cloudflare.com/turnstile is loaded and the form "
        "carries a hidden cf-turnstile-response. Note it is the ?render= "
        "variant, so nothing is visible on the page. Turnstile is the "
        "same class of obstacle as reCAPTCHA and this module treats it "
        "the same way -- stop, do not solve.  SECOND WALL, independent of "
        "the first -- AN EMAILED ONE-TIME CODE. The form carries a 'Send "
        "verification code' button, a REQUIRED input[name='hwd_otp_code'] "
        "labelled 'Email verification code', and hidden hwd_otp_challenge "
        "and hwd_otp_browser companions. So even past Turnstile the "
        "request cannot be completed in one pass. The hwd_ prefix means "
        "this is bespoke, built for this site rather than a plugin "
        "default -- someone deliberately added identity verification on "
        "top of the bot check.  Also present: "
        "input[name='form_fields[field_9fdd253]'], a text input with no "
        "label that computes to hidden -- on the honeypot pattern, and it "
        "would need to go in forbidden_selectors. info@healthwisedata.com "
        "is published."
    ),
    "heartbeat-ai": (
        "Verified 2026-09-23. heartbeat.ai/optout returns HTTP 200 whose "
        "entire body reads 'Checking for any bots ...' with "
        "challenges.cloudflare.com/turnstile loaded. No form, no fields, "
        "nothing else in the DOM.  Same shape as grin-co in the previous "
        "batch and worth the same warning: the status code says success "
        "and the page renders without error, so a check that counted form "
        "elements behind a 200 would report 'no opt-out form present' -- "
        "exactly backwards, since the URL is /optout and the form is "
        "behind the challenge. Recorded as blocked rather than undecided "
        "because the obstacle is deliberate and aimed precisely at "
        "headless automation; waiting does not resolve it.  DATASET NOTE, "
        "flagged not fixed: the row's domain is heartbeat.ai but its "
        "contact is contact@SWORDFISH.ai. That is correct rather than "
        "wrong -- Heartbeat and Swordfish are the same operation, and "
        "Swordfish's own row may exist separately in this dataset. Worth "
        "knowing before anyone 'corrects' the address, and worth checking "
        "whether the two rows are duplicates of one broker."
    ),
    "helixcampaigns-com": (
        "Verified 2026-09-23, and the form is not where it looks. "
        "helixcampaigns.com/mydata renders prose and an empty frame; the "
        "actual request form is embedded from helixcampaigns- "
        "mydata.ZAPIER.APP/_z/embed/page/... -- a Zapier Interfaces page. "
        "That is a vendor not previously seen in this module and worth "
        "naming: a form whose markup, ids and behaviour are Zapier's, not "
        "the broker's, and which can be re-published at any time. "
        "Blocked by reCAPTCHA ENTERPRISE: enterprise.js plus the gstatic "
        "runtime, a g-recaptcha-response textarea in the form, and four "
        "attached frames including a bframe -- the puzzle variant. "
        "Transcribed for whoever revisits, because the shape is "
        "instructive. Fields: Email (required), an international phone "
        "pair, State (required). Then TWO CONTROLS THAT ARE NOT SELECTS "
        "-- 'Requestor Type' and 'Type of Request' are both rendered as "
        "BUTTON elements driving custom dropdowns, and both are required. "
        "A recipe cannot fill them with Select(); it would need the "
        "listbox-button handling this module already has for OneTrust. "
        "There is also a required 'Confirm' control that is a button "
        "fronting a hidden required checkbox[name='confirm'] -- fill the "
        "checkbox directly and the widget's own state never updates. "
        "Note the field naming: the email input is name='field-3', a "
        "positional id that carries no meaning and will shift if the form "
        "is edited. info@helixcampaigns.com is published."
    ),
    "ip2location-com": (
        "Verified 2026-09-23. form#form-do-not-sell is a genuine, well- "
        "built rights form: name, emailAddress, an ipAddresses TEXTAREA, "
        "a requestType select offering correct/delete/stop-using/know/do- "
        "not-sell, a scope select, a Yes/No 'behalf' select, a file "
        "upload, a per-load nonce and a SUBMIT REQUEST button.  Blocked "
        "by CLOUDFLARE TURNSTILE (challenges.cloudflare.com/turnstile, "
        "?render= variant, so invisible). Second and independently, the "
        "hidden nonce is minted per page load, so no stored POST can be "
        "replayed -- though that alone would not stop a live browser "
        "session.  THE MORE INTERESTING OBSTACLE IS THE IDENTIFIER. "
        "IP2Location's product maps IP ADDRESSES to locations; the record "
        "it holds about a person is keyed to an IP, which is why the "
        "form's central field is a textarea for listing them. "
        "resolve_fields has no IP source and could not honestly invent "
        "one: a person's IP address changes, is often shared, and is not "
        "something this tool collects or should guess. This joins the "
        "MAID cluster (complementics, collectivedata, datafy, factori) "
        "and foursquare's unresolved 'id' field as a broker whose filing "
        "key the codebase does not model -- five or six rows now, which "
        "is enough to be worth a deliberate decision rather than case-by- "
        "case notes.  Selector hazard if it is ever revisited: NONE of "
        "the form's inputs carry an id or a label element -- the visible "
        "labels are loose prose -- so every selector must go through name "
        "attributes scoped by #form-do-not-sell. The page also carries a "
        "header LOGIN form and a newsletter form, both with their own "
        "input[name='emailAddress'], so an unscoped selector would hit "
        "the wrong one. support@ip2location.com is published."
    ),
    "hireez-com": (
        "Verified 2026-09-23. app.hireez.com/ownyourdata returns HTTP 403 "
        "with Cloudflare's interstitial: 'Performing security "
        "verification. This website uses a security service to protect "
        "against malicious bots.' Turnstile is loaded. Nothing of the "
        "page itself was reached.  Distinguish this from heartbeat-ai in "
        "the same batch, which serves the same class of challenge behind "
        "a 200: here the status code is honest about it. Both are "
        "blocked, but a 403 at least announces itself, and the contrast "
        "is worth recording since a probe that trusts status codes will "
        "treat these two identically-blocked brokers very differently. "
        "The URL is worth noting for whoever retries: /ownyourdata on the "
        "APP subdomain, not the marketing site. That placement suggests "
        "the form may additionally sit behind a product login, in which "
        "case a person who has never been a hireez customer -- which is "
        "everyone whose data it holds, since it sells candidate profiles "
        "to recruiters -- may not be able to reach it even with a normal "
        "browser. That is speculation and is marked as such; nothing "
        "behind the challenge was seen. privacy@hireez.com is published "
        "and is the honest fallback to offer."
    ),
    "homeownersmarketingservices-com": (
        "Verified 2026-09-23. /list-removal-request-new/ carries an "
        "Elementor form -- Name, Email and Address all required, plus an "
        "optional 'Anything Else You Want To Let Us Know?' textarea and a "
        "Send button.  Blocked by reCAPTCHA: the gstatic runtime, "
        "api.js?render=explicit, a g-recaptcha-response textarea inside "
        "the form and two attached frames.  A HONEYPOT, and an unusually "
        "self-documenting one: input[name='form_fields[privtrue]'], a "
        "text input with no label that computes to hidden. It would need "
        "to go in forbidden_selectors. There is also a hidden "
        "form_fields[field_bf926e6] of the same shape.  WORTH RECORDING "
        "BEYOND THE CAPTCHA, because it bears on whether this row belongs "
        "in the dataset at all: the page opens by DENYING it is a data "
        "broker in the statutory sense -- 'We do not obtain, or sell, "
        "from consumers or from other sources, personal information as "
        "defined in the privacy laws of California or other States'. It "
        "then offers the removal form anyway. Whether that denial is "
        "accurate is not something this sweep can adjudicate, and it is "
        "not a reason to skip the row; it is recorded because a person "
        "submitting here should know the company's stated position is "
        "that the laws they are invoking do not apply to it. The "
        "dataset's dnm@homeownersmarketingservices.com ('do not mail') is "
        "a genuine and probably more effective channel."
    ),
    "idm-us-com": (
        "Verified 2026-09-23. A clean, complete Gravity Forms opt-out -- "
        "gform_2 with Email, First Name, Last Name, Address, City, State "
        "and Zip, ALL SEVEN REQUIRED and all seven plainly labelled, "
        "under a Submit button. Every value has a source in "
        "resolve_fields and there is no honeypot, no hidden required "
        "control and no verification hop. Without the captcha this would "
        "be a staging candidate.  Blocked by reCAPTCHA, and here it is "
        "VISIBLE: the page renders a field labelled 'CAPTCHA', with "
        "api.js loaded, a g-recaptcha-response textarea in the form and "
        "two attached frames. Unlike most of this batch there is no "
        "ambiguity about whether a challenge exists -- the broker shows "
        "it.  One note for whoever writes recipes against Gravity Forms "
        "generally, since this is the third such form in two batches: the "
        "field names here are input_1 through input_7 and the form id is "
        "gform_2 -- THE SAME ids h1-co uses on a completely unrelated "
        "site, with different meanings (input_1 is Email here and First "
        "Name there). Gravity numbers fields per-form, not globally, so "
        "these selectors are meaningful only together with the site, and "
        "they shift if the form is edited. Never copy a Gravity selector "
        "between brokers. ccpa@idm.us.com is published."
    ),
    "gumgum-com": (
        "Verified 2026-09-23. GumGum's privacy policy has no form of its "
        "own; its 'DO NOT SELL', 'Do not Sell' and 'Exercise Your Rights' "
        "links all point to the same OneTrust DSAR webform on "
        "privacyportal-cdn.onetrust.com. That form was rendered and read. "
        "Blocked by reCAPTCHA -- api.js loaded, a g-recaptcha-response "
        "textarea inside the form, and both an anchor and a BFRAME "
        "attached, the latter meaning the checkbox-with-puzzle variant. "
        "THE WRONG-REQUEST-TYPE PROBLEM, and a clear-cut instance of it. "
        "The form is headed 'SUBJECT ACCESS FORM' and its first required "
        "question is 'I am a (an)' with the options: Prospective "
        "Employee, Client, Employee, Visitor, Other. THERE IS NO OPTION "
        "FOR A PERSON WHOSE DATA THE COMPANY COLLECTED THROUGH "
        "ADVERTISING -- which is everyone this dataset is concerned with. "
        "A consumer opting out of GumGum's ad targeting has never been "
        "its employee, client or prospective employee, and calling "
        "themselves a 'Visitor' asserts a relationship to gumgum.com that "
        "they very likely do not have. This is a generic HR-oriented "
        "OneTrust template pressed into service as an advertising-privacy "
        "channel, and picking any of its options would mean a recipe "
        "choosing a characterisation on the user's behalf.  Recorded as "
        "blocked on the captcha, which is decisive on its own, but the "
        "request-type problem would independently keep it out of "
        "STAGED_RECIPES. Remaining fields are conventional: First Name, "
        "Last Name, Email, Country and a required Request Details "
        "textarea, with a request-type multi-select offering Opt out / "
        "Update Data / Info Request / Data Deletion / Object to "
        "Processing. Note the policy also links the NAI consumer opt-out, "
        "which is not GumGum's surface. talbert@gumgum.com is the dataset "
        "contact -- a personal address."
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
    "cadent-tv": (
        "Verified by browser render 2026-09-23, following the dataset URL "
        "through to the actual form. cadent.tv/your-privacy-choices/ "
        "redirects to www.cadent.com/your-privacy-choices, an explainer "
        "with no inputs that links on to privacy.cadent.tv/privacy -- and "
        "that is a multi-step single-page wizard, not a form. It opens on "
        "route #/verify-email with the heading 'Email Verification', a "
        "Yes/No question ('Are you a US based user?') and an offer to "
        "contact Customer Support if you cannot access your email; the "
        "request form itself is behind that gate. Out of scope for the "
        "reason this bucket exists: FormRecipe carries one url and one "
        "flat list of fields, not a sequence of session-gated routes, and "
        "here the sequence begins with an email round-trip that the "
        "driver would have to complete before any field is even visible. "
        "A reCAPTCHA (api.js with render=explicit) is also present, which "
        "is an independent blocker. Recorded for the dataset: the domain "
        "cadent.tv now redirects to cadent.com, while the privacy portal "
        "stays on privacy.cadent.tv."
    ),
    "demystdata-com": (
        "Verified by browser render 2026-09-23: a real, reachable, "
        "captcha-free DSAR form that this codebase must not complete. "
        "demystdata.com now serves demyst.com/data-subject-action- "
        "request, whose form asks for Full name, Former names, Email "
        "address, Phone number, DATE OF BIRTH, Current address, a free- "
        "text 'identify your relationship with Demyst Data, Ltd', and a "
        "set of checkboxes and radios that are computed-invisible in the "
        "DOM (styled controls rather than honeypots, given every one of "
        "them sits under a visible label). Out of scope rather than "
        "staged for two reasons. First, date of birth: this repo's "
        "identity record is a name and address, and DOB is a materially "
        "more sensitive datum to hand a broker than anything a recipe has "
        "been asked to submit so far -- that is a decision for the "
        "person, not for a tool. Second, the 'relationship with Demyst' "
        "free-text has no honest automatic answer for someone who has "
        "never dealt with them. Note also that the form's controls carry "
        "their LABELS as both name and id (name='Date of birth'), spaces "
        "included, so any future recipe would need attribute selectors "
        "with quoted values. No captcha was loaded. Dataset contact: "
        "privacy@demystdata.com."
    ),
    "digitalsegment-com": (
        "FLAGGED FOR A HUMAN DECISION, 2026-09-23 -- this is the first "
        "broker found that forbids in words the exact kind of tool this "
        "repo is.  Digital Segment's opt-out page (/about/opt-out/; the "
        "dataset's /about/consumer-opt-out/ 404s and the site's own links "
        "all point at the shorter path) states twice, in its own words: "
        "'Digital Segment's Online Opt Out Request Process is intended "
        "only for individual consumer use. Third Party Opt Out services "
        "are not authorized to use this process. Third party privacy "
        "right requests on behalf of a individual consumer, must be "
        "submitted via our privacy API' -- elsewhere on the same page, "
        "'via email or mail'.  Whether broker-guard is a 'Third Party Opt "
        "Out service' is genuinely arguable: it is run by the individual, "
        "on their own machine, for their own data, which is nearer to "
        "individual consumer use than to a service. It is recorded as out "
        "of scope rather than blocked because that is a call for a person "
        "to make knowingly, not for a recipe to make by default, and "
        "because filing against a stated prohibition is the kind of thing "
        "that gets an opt-out channel closed for everybody.  Two "
        "technical notes so the decision is made on full information. "
        "There is an independent wall regardless of the policy question: "
        "the page loads reCAPTCHA (gstatic recaptcha__en.js, a "
        "frontend.min.js integration, a grecaptcha-badge and a "
        "g-recaptcha-response) in invisible mode. And the form itself "
        "could not be read at all -- after a full scroll to the footer, "
        "document.querySelectorAll('form') returned NOTHING on the opt- "
        "out page, although the surrounding copy ('Opt Out Request', 'The "
        "information provided on this form...', 'You must provide a valid "
        "email address to use this form') plainly describes one. Either "
        "it is injected behind an accordion toggle or it is broken in "
        "production. If the policy question is ever resolved in favour of "
        "filing, that has to be settled first."
    ),
    "forewarn-com": (
        "Verified 2026-09-23, and recorded as out of scope on the "
        "SENSITIVITY of what it asks rather than on the wall in front of "
        "it -- the same reasoning as demystdata-com in this module, only "
        "stronger.  FOREWARN's #ccpaForm requires LAST 4 DIGITS OF SOCIAL "
        "SECURITY NUMBER (rendered as a password input) and DATE OF BIRTH "
        "(as month/day selects), alongside full name, full address, "
        "mobile phone and email. demystdata was put out of scope for date "
        "of birth alone; this asks for that plus partial SSN. Handing a "
        "broker a partial SSN is a decision with consequences a person "
        "should make deliberately, knowing who they are giving it to -- "
        "it is not a decision a recipe should make on their behalf by "
        "default. The data may well be necessary for the broker to "
        "identify the right record; that does not make it the tool's "
        "call.  There is an independent technical wall regardless: "
        "reCAPTCHA, with a .g-recaptcha element, a g-recaptcha-response "
        "textarea and both api2/anchor and api2/bframe frames attached. "
        "Recorded for completeness, since the form is otherwise "
        "interesting. It is a SALESFORCE WEB-TO-CASE form -- hidden "
        "orgid, retURL, reason and captcha_settings, with "
        "action='javascript: submitForm();' -- and its field names "
        "contain SPACES ('First Name', 'Address Line 1', 'Last 4 Social "
        "Security', 'Lived Here 6 Months'), which any selector would have "
        "to quote. Note also that input[name='Lived Here 6 Months'] "
        "appears TWICE on the form, the duplicate-name hazard already met "
        "on porchgroupmedia. The form additionally offers two conditional "
        "branches -- a legal-right claim and a personal-risk claim, each "
        "with a free-text justification -- which is unusual and suggests "
        "FOREWARN treats opt-outs as exceptions to be argued for. "
        "legal.compliance@forewarn.com is the published address."
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
