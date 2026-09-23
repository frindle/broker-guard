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
  Credit.com. A *statically hosted* variant of the same widget; the field
  ids are the same family but it is served from the CDN host and has to be
  eyeballed before being trusted, not assumed.
* ``https://www.lsmapps.com/onetrust-opt-out`` -- L.S Mobile Apps. This is
  the broker's OWN page that presumably embeds or links to a OneTrust
  widget. It is NOT a raw OneTrust webform URL and must not be assumed to
  share this flavor at all.

So flavor is recorded per recipe, and a recipe is only ever written after
someone has looked at that specific URL.
"""
from dataclasses import dataclass, field

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
    require.
    """

    selector: str
    source: str
    label: str
    kind: str = "text"
    value: str = ""
    required: bool = True


@dataclass(frozen=True)
class FormRecipe:
    """Everything needed to drive one broker's opt-out form."""

    broker_id: str
    broker_name: str
    url: str
    flavor: str
    choices: tuple = ()
    fields: tuple = ()
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


# Keyed by the broker id as it appears in the broker dataset. ONE entry today,
# on purpose: Consumer Canvas ships working end to end first (see this task's
# brief). The other OneTrust-hosted brokers in the dataset -- Nielsen,
# Credit.com, bolttech, L.S Mobile Apps -- are a follow-up, and each needs its
# own verified FormRecipe here before it can ever be submitted to.
RECIPES = {
    CONSUMER_CANVAS.broker_id: CONSUMER_CANVAS,
}


# Brokers whose form has been WRITTEN DOWN but which are not yet turned on.
# Empty today; it exists so that "we transcribed the form" and "we are willing
# to submit to it" stay two separate decisions.
STAGED_RECIPES: dict = {}


class RecipeNotFound(KeyError):
    """No verified form recipe exists for this broker."""


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
    for f in recipe.fields:
        labels[f.selector] = f.label
        if f.source == "literal":
            text = f.value
        elif f.source == "first_name":
            text = (getattr(identity, "first_name", "") or "").strip()
        elif f.source == "last_name":
            text = (getattr(identity, "last_name", "") or "").strip()
        elif f.source == "email":
            emails = list(getattr(identity, "emails", None) or [])
            text = emails[0].strip() if emails else ""
        elif f.source == "state":
            text = state_from_addresses(getattr(identity, "addresses", None))
        elif f.source == "country":
            text = DEFAULT_COUNTRY
        else:
            raise ValueError("unknown field source: {!r}".format(f.source))

        if text:
            values[f.selector] = text
        elif f.required:
            missing.append(f.label)
    return {"values": values, "labels": labels, "missing": missing}
