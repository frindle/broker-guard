"""Privacy-request platforms: detect which one a broker's page is built on.

Dozens of brokers do not run their own opt-out form; they embed a privacy-
request product (OneTrust, DataGrail, MineOS, TrustArc, Osano, Ketch,
Transcend ...). Knowing the platform lets the assisted filler (assisted.py)
accept the right consent banner, know that the request will be email-verified,
and group brokers so one fix covers many rows.

This module deliberately carries NO per-platform field selectors. A selector
nobody has read off a live page is a fabrication, and optout_forms' own
rule is that a fabricated recipe is worse than an honest "undecided". The
assisted filler reads the actual form instead and a human approves it.

Pure and browser-free: ``detect_platform`` takes page HTML and/or URLs.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    name: str
    signatures: tuple          # lowercase substrings of the page HTML / frame URLs
    consent_selectors: tuple = ()   # cookie-banner accept buttons known for this vendor
    verifies_email: bool = True     # these products email the requester a link


PLATFORMS = (
    Platform("onetrust", ("cdn.cookielaw.org", "privacyportal", "onetrust.com", "ot-dsar",
                          "otsdkstub", "optanon"),
             ("#onetrust-accept-btn-handler",)),
    Platform("datagrail", ("datagrail.io", "datagrail-consent", "dg-privacy"),
             ("button[data-testid='accept-all']",)),
    Platform("mineos", ("mineos.ai", "app.mineos", "mineos-"),),
    Platform("trustarc", ("trustarc.com", "truste-", "consent.trustarc", "teconsent"),
             ("#truste-consent-button",)),
    Platform("osano", ("osano.com", "cmp.osano", "osano-cm"),
             (".osano-cm-accept-all",)),
    Platform("ketch", ("ketchcdn.com", "ketch.com", "ketch_consent", "lanyard_root"),),
    Platform("transcend", ("transcend.io", "cdn.transcend", "transcend-consent"),),
    Platform("securiti", ("securiti.ai",),),
    Platform("cognism", ("cognism.com/privacy", "cognism-"),),
    Platform("cookiebot", ("cookiebot.com", "cybot"),
             ("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",), False),
    Platform("hubspot_form", ("hsforms.net", "hbspt.forms", "hs-form"), (), False),
    Platform("wpforms", ("wpforms", "gform_wrapper", "elementor-form"), (), False),
)

BY_NAME = {p.name: p for p in PLATFORMS}

# Cookie-banner accept buttons by visible text, used when no vendor selector hit.
GENERIC_ACCEPT_TEXTS = ("accept all", "accept", "allow all", "i agree", "agree", "got it", "ok")


def detect_platform(html: str | None = "", urls=()) -> str | None:
    """The platform name whose signatures appear most often, else ``None``.

    ``urls`` are the page URL plus any frame/script URLs. Ties go to the
    earlier entry in ``PLATFORMS`` (the dedicated request products come before
    the generic form builders), so a OneTrust portal that also uses HubSpot
    scripts is still OneTrust.
    """
    haystack = ((html or "") + " " + " ".join(urls or ())).lower()
    best, best_hits = None, 0
    for platform in PLATFORMS:
        hits = sum(1 for sig in platform.signatures if sig in haystack)
        if hits > best_hits:
            best, best_hits = platform.name, hits
    return best


def verifies_email(name: str | None) -> bool:
    platform = BY_NAME.get(name or "")
    return bool(platform and platform.verifies_email)


def consent_selectors(name: str | None) -> tuple:
    platform = BY_NAME.get(name or "")
    vendor = platform.consent_selectors if platform else ()
    every = tuple(s for p in PLATFORMS for s in p.consent_selectors)
    return tuple(dict.fromkeys(vendor + every))
