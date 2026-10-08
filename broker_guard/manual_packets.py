"""Pre-filled packets for the rows only a human can finish (the manual tail).

Nothing here is hidden or auto-sent. For each ``needs_you`` row of
``parity`` it assembles what Penn needs so the task takes minutes: the exact
page, the values to type, the steps, and for mailed or notarized requests a
printable letter built from the same statute-specific text the email channel
uses (``optout_email.TEMPLATES``) via ``escalation.render_letter``.

The packets contain Penn's PII by design, so they are rendered only on the
authenticated local UI and never logged, stored or sent anywhere.
"""
from __future__ import annotations

from datetime import date

from broker_guard import escalation, optout_email, parity

KIND_TITLES = {
    parity.PHONE: "Phone or SMS verification",
    parity.MAIL: "Mailed letter",
    parity.NOTARY: "Notarized letter",
    parity.KBA: "Knowledge-based authentication",
    parity.ID_UNREDACTED: "Wants an unredacted ID or proof of status",
    parity.SSN_DOB: "Wants an SSN or date of birth",
    parity.ACCOUNT: "Needs an account",
    parity.SELECTION: "You must pick your own record",
    parity.OTHER_SCOPE: "Form shape broker-guard cannot drive",
}

KIND_STEPS = {
    parity.PHONE: [
        "Open the opt-out page below and start the request.",
        "Answer the verification call or text with the code the page sends you.",
        "Mark it done in Opt-out status when the confirmation appears.",
    ],
    parity.MAIL: [
        "Print the letter, sign it, and mail it to the broker's privacy/data-rights address "
        "(listed on the opt-out page).",
        "Keep the receipt; the response clock starts when they receive it.",
    ],
    parity.NOTARY: [
        "Print the letter and take it, unsigned, to a notary with your photo ID.",
        "Sign in front of the notary, then mail it to the address on the opt-out page.",
        "Keep a copy and the mailing receipt.",
    ],
    parity.KBA: [
        "Start the request on the opt-out page.",
        "Answer the identity questions yourself; they are built from your credit file.",
    ],
    parity.ID_UNREDACTED: [
        "This broker wants more of your ID than a redacted copy shows (number, photo or proof "
        "of status). broker-guard will not send that.",
        "Decide whether the listing is worth it; if so, submit it yourself on the opt-out page.",
    ],
    parity.SSN_DOB: [
        "This broker asks for an SSN or date of birth. broker-guard never volunteers either.",
        "Decide whether to give it; if so, submit on the opt-out page yourself.",
    ],
    parity.ACCOUNT: [
        "Create or log in to your account on the site.",
        "Use the privacy or account-deletion setting there; broker-guard cannot hold a login.",
    ],
    parity.SELECTION: [
        "Search for yourself on the site and pick your own record(s) from the results.",
        "Use the values below on the removal form.",
    ],
    parity.OTHER_SCOPE: [
        "Open the opt-out page and complete it by hand with the values below.",
    ],
}

LETTER_KINDS = (parity.MAIL, parity.NOTARY)

_NOTARY_BLOCK = """\

--------------------------------------------------------------------
Notary acknowledgment (complete in front of the notary)

State of ____________________   County of ____________________

Subscribed and sworn before me on ______________ by {full_name}, who proved
to me on the basis of satisfactory evidence to be the person who appeared
before me.

Notary signature: ______________________________   Seal:
"""

_LETTER_FRAME = """\
{today}

{broker_name}
Attn: Privacy / Data Rights Department
(address: see {optout_url})

{body}
Sincerely,

______________________________
{full_name}
{address_line}
{notary}"""


def _address_line(identity) -> str:
    addresses = list(getattr(identity, "addresses", None) or [])
    return addresses[0] if addresses else ""


def render_letter_text(row: dict, identity, kind: str = parity.MAIL, today: date | None = None) -> str:
    """The printable letter for *row*, in the statute text matching Penn's state."""
    _subject, body, _sla = optout_email.TEMPLATES[optout_email.template_for(identity)]
    body_text = escalation.render_letter(body, {
        "identity_block": optout_email._identity_block(identity),
        "full_name": identity.full_name,
    })
    context = {
        "today": (today or date.today()).strftime("%B %d, %Y").replace(" 0", " "),
        "broker_name": row.get("name") or row.get("domain") or "",
        "optout_url": row.get("opt_out_url") or row.get("domain") or "the broker's website",
        "body": body_text,
        "full_name": identity.full_name,
        "address_line": _address_line(identity),
        "notary": escalation.render_letter(_NOTARY_BLOCK, {"full_name": identity.full_name})
        if kind == parity.NOTARY else "",
    }
    return escalation.render_letter(_LETTER_FRAME, context)


def build_packet(row: dict, identity, kind: str) -> dict:
    return {
        "broker_id": parity.row_id(row),
        "name": row.get("name") or row.get("domain") or "",
        "kind": kind,
        "title": KIND_TITLES.get(kind, kind),
        "url": row.get("opt_out_url") or "",
        "steps": list(KIND_STEPS.get(kind, [])),
        "prefilled": {
            "Name": identity.full_name,
            "Addresses": list(getattr(identity, "addresses", None) or []),
            "Emails": list(getattr(identity, "emails", None) or []),
            "Phones": list(getattr(identity, "phones", None) or []),
        },
        "has_letter": kind in LETTER_KINDS,
    }


def build_packets(rows, identity, live_recipes=None) -> list:
    """One packet per ``needs_you`` row, grouped by kind in a stable order."""
    out = []
    for row in rows:
        bucket, why = parity.classify_row(row, live_recipes)
        if bucket == parity.NEEDS_YOU:
            out.append(build_packet(row, identity, why))
    order = {k: i for i, k in enumerate(KIND_TITLES)}
    out.sort(key=lambda p: (order.get(p["kind"], 99), p["broker_id"]))
    return out
