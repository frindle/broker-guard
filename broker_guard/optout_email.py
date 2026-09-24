"""Opt-out by EMAIL -- the channel for brokers whose web form cannot be driven.

Why this exists
---------------
``optout_submit.py`` can only act on a broker with a hand-verified recipe, and
84 brokers mapped so far are in ``optout_forms.OPTOUT_BLOCKED`` -- almost all of
them behind a CAPTCHA this codebase will not solve. But most of those brokers
publish an email address for exactly this purpose, and the state privacy
statutes treat a designated email channel as a valid way to make a request. So
for a large part of the blocked set there is an honest route that needs no
form-filling at all, and it leaves a timestamped record that is better evidence
than a form post which returns no receipt.

THE HARD PRECONDITION, AND WHY IT IS HERE AND NOT IN A CHECKLIST
----------------------------------------------------------------
A request composed here contains the person's real name, postal address and
phone number. Sending that to the wrong inbox is not a failed opt-out; it is a
disclosure of PII to a stranger, caused by us.

The dataset makes that a live risk rather than a theoretical one. Of the 84
blocked brokers, the recorded ``opt_out_email`` is:

    40  a role address on the broker's own domain   (privacy@, ccpa@, dnm@...)
    13  a role address on some OTHER domain         (acquisitions, mostly)
    18  a personal-looking address                  -- including one GMAIL
                                                       account serving as a
                                                       broker's statutory
                                                       opt-out channel
    13  absent entirely

Only the first group is safe to send to without a human first establishing who
is on the other end. So ``eligible`` is enforced INSIDE ``compose_request`` and
again inside ``send_request``: an ineligible broker cannot be composed against
at all, let alone sent to. It is deliberately not a flag a caller can pass, not
a reviewer checklist item and not a warning in a UI -- a reviewer approving
forty well-formatted emails in a row will approve the forty-first, which is
precisely how the gmail address would have gone out.

The other 44 are not abandoned. They are waiting on the address-verification
research driven by ``DATASET_DEFECTS.md``; once a row's address is confirmed to
belong to the right company, it becomes eligible by the same rule as the rest.

TWO MODES, AND WHY DRAFT IS THE DEFAULT
---------------------------------------
``prepare_draft`` builds a ``mailto:`` URL that opens pre-populated in a mail
client. Nothing is sent; a human reads it and presses Send, from their own
address, into their own Sent folder.

``send_request`` will really send, unattended, through a caller-supplied
transport -- but only with BOTH ``Config.optout_email_enabled`` (default False)
and ``not Config.optout_email_dry_run`` (default True). Turning the first on and
the second off are two separate deliberate acts, which is the same interlock
``optout_submit.py`` puts in front of its one side-effecting click, and it is
here for the same reason: email cannot be recalled.

The default is draft-first because of what the validation can and cannot
establish. ``eligibility`` is a SYNTACTIC check -- role local-part, broker's own
domain. It proves the address is shaped like a designated privacy channel. It
cannot prove the inbox exists, is monitored, or belongs to the company the
dataset row is actually about. That last gap is not hypothetical here: the
sweep has already catalogued rows where the recorded contact belonged to an
entirely different company, and one row (worldpay.com) that conflates three
separate legal entities, where a ``privacy@`` address at any one of them would
pass this check and still be the wrong recipient.

So the first run of any new template should go out by hand, a handful at a
time, and be read. A flaw in the body -- wrong tone, a legal claim that does not
apply, a formatting bug that discloses more than intended -- is cheap to catch
in the first three and irreversible across forty. Once a template has been seen
working, flipping the two flags is a reasonable thing for the operator to do,
and this module is built to support that rather than to prevent it.

What is NOT overridable, in either mode, is ``eligibility``.
"""
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import quote

from broker_guard import review

log = logging.getLogger(__name__)


class EmailRefused(RuntimeError):
    """The request was refused before anything was composed or sent."""


# Local-parts that denote a ROLE inbox -- a company function rather than a
# named individual. A statutory request is addressed to the company, so a role
# address is the shape we expect; a personal one means a human being's mailbox,
# which may not even still be theirs.
#
# Kept deliberately tight. Adding 'sales' or a person's first name here would
# defeat the entire point of the module.
ROLE_LOCALPARTS = frozenset({
    "privacy", "privacyrequest", "privacyrequests", "privacysupport",
    "privacyoffice", "privacyteam", "dataprivacy",
    "ccpa", "cpra", "gdpr", "dsar", "dsr", "dpo",
    "optout", "opt-out", "opt_out", "donotsell", "do-not-sell", "dnm",
    "unsubscribe", "removal", "remove", "datarequest", "datarequests",
    "compliance", "compliancedept", "legal", "legalnotices",
})

# Recognised as role-ish when they carry a privacy-ish qualifier, but NOT on
# their own: support@ and info@ reach a general helpdesk, which is a real
# channel but not a designated one, and mail to it is far likelier to be
# mishandled or ignored. These stay out of v1 rather than being argued about
# per-broker.
_AMBIGUOUS_LOCALPARTS = frozenset({
    "support", "info", "contact", "help", "admin", "adminsupport",
    "inquiries", "customerservice", "operations", "hello",
})

_LOCALPART_SPLIT = re.compile(r"[.+_-]")


def _normalise_domain(value: str) -> str:
    """Lowercase *value* and strip scheme, path, port and a leading www."""
    text = (value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0]
    text = text.split(":", 1)[0]
    text = text.strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def _split_address(email: str):
    """Return ``(localpart, domain)`` lowercased, or ``(None, None)``."""
    text = (email or "").strip().lower()
    # One '@', a non-empty both sides, no spaces. Anything stranger is not
    # something to guess about.
    if text.count("@") != 1:
        return None, None
    local, _, domain = text.partition("@")
    if not local or not domain or " " in text:
        return None, None
    return local, _normalise_domain(domain)


def domain_matches(broker_domain: str, email_domain: str) -> bool:
    """Is *email_domain* the broker's own domain, or a subdomain of it?

    Strict on purpose. ``privacy@mail.broker.com`` passes for ``broker.com``;
    ``privacy@brokergroup.com`` does NOT pass for ``broker.com`` even though a
    human would likely recognise it, because "likely" is the failure mode this
    module exists to prevent. A sibling or parent-company domain is a real
    address that a human can confirm -- and once confirmed, the dataset row is
    corrected and it passes here for the ordinary reason.

    Substring matching is specifically avoided: ``broker.com.evil.net`` ends
    with nothing meaningful, but it CONTAINS the broker's domain, and a naive
    check would accept it.
    """
    broker = _normalise_domain(broker_domain)
    other = _normalise_domain(email_domain)
    if not broker or not other:
        return False
    return other == broker or other.endswith("." + broker)


def localpart_is_role(localpart: str) -> bool:
    """Is *localpart* a role inbox rather than a named person?

    Matches the whole local-part, and also its dot/underscore/hyphen-separated
    tokens, so ``privacy.requests`` and ``ccpa-requests`` are recognised while
    ``talbert`` and ``tapster13`` are not.
    """
    text = (localpart or "").strip().lower()
    if not text:
        return False
    if text in ROLE_LOCALPARTS:
        return True
    tokens = [t for t in _LOCALPART_SPLIT.split(text) if t]
    # Every token must be meaningful and at least one must be a role word, so
    # 'privacy.tapster13' does not sneak through on its first token.
    if not tokens or not any(t in ROLE_LOCALPARTS for t in tokens):
        return False
    return all(t in ROLE_LOCALPARTS or t in _AMBIGUOUS_LOCALPARTS
               or t.isdigit() is False and t.isalpha() for t in tokens)


def eligibility(broker_domain: str, email: str) -> tuple:
    """Return ``(ok, reason)`` for sending a request to *email*.

    *reason* is a short machine-stable token, not prose, so the dashboard and
    the research worklist can group by it:

        ``ok``                    -- eligible
        ``no_address``            -- the row records no email at all
        ``malformed``             -- not parseable as a single address
        ``offdomain``             -- role address, but not the broker's domain
        ``personal``              -- on the right domain, but a named person
        ``ambiguous_localpart``   -- support@/info@: a helpdesk, not a
                                     designated privacy channel
    """
    if not (email or "").strip():
        return False, "no_address"
    local, domain = _split_address(email)
    if local is None:
        return False, "malformed"
    if not domain_matches(broker_domain, domain):
        return False, "offdomain"
    if localpart_is_role(local):
        return True, "ok"
    base_tokens = [t for t in _LOCALPART_SPLIT.split(local) if t]
    if local in _AMBIGUOUS_LOCALPARTS or (
            base_tokens and all(t in _AMBIGUOUS_LOCALPARTS for t in base_tokens)):
        return False, "ambiguous_localpart"
    return False, "personal"


def is_eligible(broker_domain: str, email: str) -> bool:
    return eligibility(broker_domain, email)[0]


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

_SUBJECT = "Request to opt out of the sale or sharing of my personal information"

_BODY = """\
To whom it may concern,

I am writing to exercise my right to opt out of the sale and sharing of my
personal information, and to request deletion of the personal information you
hold about me.

Please treat this as a request under every consumer privacy law that applies to
me, including the California Consumer Privacy Act as amended by the CPRA, and
the equivalent statutes of other US states.

So that you can locate my records, the information I am providing is:

{identity_block}

Please confirm in writing when this request has been actioned, and tell me if
you need anything further from me to verify my identity. If you believe an
exemption applies, please say which one and why.

If you have disclosed my personal information to third parties, please forward
this request to them as the applicable law requires.

Thank you.

{full_name}
"""


def _identity_block(identity) -> str:
    lines = ["Name: %s" % identity.full_name]
    for address in getattr(identity, "addresses", []) or []:
        lines.append("Address: %s" % address)
    for email in getattr(identity, "emails", []) or []:
        lines.append("Email: %s" % email)
    for phone in getattr(identity, "phones", []) or []:
        lines.append("Phone: %s" % phone)
    return "\n".join(lines)


def compose_request(broker_id: str, broker_domain: str, broker_email: str,
                    identity, from_address: str) -> EmailMessage:
    """Build the request message, or refuse.

    Refuses on an ineligible address BEFORE building anything. That ordering is
    the whole safety property: there is no code path in this module that
    produces a message body containing the person's PII addressed to an
    unvalidated recipient, so no later mistake -- a mis-wired UI, a caller
    passing the wrong flag, a future refactor -- can send one.
    """
    ok, reason = eligibility(broker_domain, broker_email)
    if not ok:
        raise EmailRefused(
            "refusing to compose for %s: recipient %r is %s"
            % (broker_id, broker_email or "", reason))
    if not (from_address or "").strip():
        raise EmailRefused("refusing to compose for %s: no from_address" % broker_id)

    message = EmailMessage()
    message["To"] = broker_email.strip()
    message["From"] = from_address.strip()
    message["Subject"] = _SUBJECT
    reply_to = next(iter(getattr(identity, "emails", []) or []), "")
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(_BODY.format(identity_block=_identity_block(identity),
                                     full_name=identity.full_name))
    return message


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


# mailto: is a URL, and a long one goes through a shell, a browser and a mail
# client before it arrives. Clients differ on the ceiling; this is comfortably
# under every commonly cited one. Over it, the draft still carries To and
# Subject and the body is written to the record for pasting, rather than being
# silently truncated into a request that says something different from what was
# composed.
MAILTO_SAFE_LENGTH = 1800


def prepare_draft(broker_id: str, broker_domain: str, broker_email: str,
                  identity, from_address: str = "") -> dict:
    """Build a pre-populated draft. Sends nothing.

    Returns ``{"to", "subject", "body", "mailto", "body_truncated"}``. The
    caller opens ``mailto`` (a link in the dashboard, or ``open`` on macOS) and
    the client shows an editable message with everything filled in.

    Refuses on an ineligible recipient exactly as ``compose_request`` does --
    the check is not skipped just because nothing is being sent, because a
    draft addressed to a stranger is still a draft that a person can absently
    press Send on.
    """
    message = compose_request(broker_id, broker_domain, broker_email, identity,
                              from_address or "draft@localhost")
    subject = message["Subject"]
    body = message.get_content()

    query = "subject=%s&body=%s" % (quote(subject, safe=""), quote(body, safe=""))
    mailto = "mailto:%s?%s" % (quote(broker_email.strip(), safe="@"), query)
    truncated = len(mailto) > MAILTO_SAFE_LENGTH
    if truncated:
        mailto = "mailto:%s?subject=%s" % (
            quote(broker_email.strip(), safe="@"), quote(subject, safe=""))
    return {
        "to": broker_email.strip(),
        "subject": subject,
        "body": body,
        "mailto": mailto,
        "body_truncated": truncated,
    }


def prepare_drafts(brokers, identity, from_address: str = "") -> dict:
    """Draft for every eligible broker in *brokers*; explain every skip.

    *brokers* is an iterable of ``(broker_id, domain, email)``. Returns
    ``{"drafts": [...], "skipped": [{"broker_id", "reason"}, ...]}`` so a caller
    can show both -- the skipped list IS the address-verification worklist, and
    burying it would hide the 44 rows that need research behind the 40 that
    do not.
    """
    drafts, skipped = [], []
    for broker_id, domain, email in brokers:
        ok, reason = eligibility(domain, email)
        if not ok:
            skipped.append({"broker_id": broker_id, "reason": reason,
                            "recipient": email or ""})
            continue
        draft = prepare_draft(broker_id, domain, email, identity, from_address)
        draft["broker_id"] = broker_id
        drafts.append(draft)
    return {"drafts": drafts, "skipped": skipped}


def _record(broker_id, broker_email, identity_key, started_at, outcome,
            dry_run, **extra) -> dict:
    record_id = review.attempt_id(broker_id, identity_key, started_at.isoformat())
    base = {
        "id": record_id,
        "basename": review.basename_for(broker_id, started_at, record_id),
        "broker_id": broker_id,
        "channel": "email",
        "recipient": broker_email,
        "identity_key": identity_key,
        "outcome": outcome,
        "dry_run": bool(dry_run),
        "started_at": started_at.isoformat(),
        "finished_at": _utcnow().isoformat(),
    }
    base.update(extra)
    return base


def send_request(broker_id: str, broker_domain: str, broker_email: str,
                 identity, cfg, transport=None, directory=None,
                 dry_run=None, now=None) -> dict:
    """Compose and (unless dry-run) hand one request to *transport*.

    *transport* is any callable taking the ``EmailMessage``. Nothing here opens
    a connection: the caller owns the transport, so a misconfigured deployment
    fails loudly at wiring time rather than silently half-sending.

    Returns the audit record, which is also written to the review folder so
    email requests appear in the same dashboard as form attempts.
    """
    if not getattr(cfg, "optout_email_enabled", False):
        raise EmailRefused(
            "optout_email_enabled is off; refusing before composing anything")

    started_at = now or _utcnow()
    identity_key = identity.identity_key
    effective_dry_run = (
        bool(getattr(cfg, "optout_email_dry_run", True))
        if dry_run is None else bool(dry_run))

    from_address = getattr(cfg, "optout_email_from", "") or ""

    try:
        message = compose_request(broker_id, broker_domain, broker_email,
                                  identity, from_address)
    except EmailRefused as exc:
        # A refusal is a real outcome worth recording, not an error to swallow:
        # it is how the 44 unvalidated brokers show up as needing research.
        record = _record(broker_id, broker_email, identity_key, started_at,
                         review.OUTCOME_NEEDS_MANUAL, effective_dry_run,
                         reason=str(exc),
                         eligibility=eligibility(broker_domain, broker_email)[1])
        _save(record, directory, cfg)
        raise

    if effective_dry_run:
        record = _record(broker_id, broker_email, identity_key, started_at,
                         review.OUTCOME_DRY_RUN, True,
                         subject=message["Subject"],
                         body=message.get_content())
        _save(record, directory, cfg)
        return record

    if transport is None:
        raise EmailRefused(
            "live send requested for %s but no transport was supplied" % broker_id)

    try:
        transport(message)
    except Exception as exc:                       # noqa: BLE001 -- recorded
        record = _record(broker_id, broker_email, identity_key, started_at,
                         review.OUTCOME_FAILED, False,
                         reason="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        _save(record, directory, cfg)
        return record

    record = _record(broker_id, broker_email, identity_key, started_at,
                     review.OUTCOME_SUBMITTED, False,
                     subject=message["Subject"])
    _save(record, directory, cfg)
    return record


def _save(record, directory, cfg):
    try:
        review.save_attempt(directory or review.review_dir(cfg), record)
    except review.ReviewError:
        log.warning("could not write review record for %s", record["broker_id"])
