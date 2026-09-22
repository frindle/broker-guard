"""Credit-freeze bureau registry + a per-bureau status tracker.

There is no consumer API to place a credit freeze -- every bureau below
requires a human to actually submit the request. This module is therefore a
registry of deep links (not an automation), a small status model to track
where each bureau stands, and a stubbed interface
(:func:`semi_automate_freeze`) a future browser-automation flow (an eraser
``fill``/chromedp step) can implement later.

Freeze PINs are sensitive and are encrypted at rest with the same mechanism
used elsewhere in broker-guard for field-level secrets
(:mod:`broker_guard.crypto`, Fernet). The key is always injected by the
caller -- this module never sources or stores a key itself, mirroring how
ID-image encryption is expected to work.

Bureau URLs
-----------
Several of these bureaus don't have a single, stable, well-documented
freeze URL the way Equifax/Experian/TransUnion do. Where the exact deep
path isn't confidently known, the registry points at the bureau's own
security-freeze landing page instead of guessing a specific path, and the
entry is commented ``# needs-verification`` -- check the live page before
surfacing that link to a user.
"""
from dataclasses import dataclass

from broker_guard.crypto import decrypt_field, encrypt_field

# --- bureau registry --------------------------------------------------------

REQUIRED_BUREAU_FIELDS = ("display_name", "freeze_url", "thaw_url", "online_selfserve", "notes")

BUREAUS = {
    "equifax": {
        "display_name": "Equifax",
        "freeze_url": "https://www.equifax.com/personal/credit-report-services/credit-freeze/",
        "thaw_url": "https://www.equifax.com/personal/credit-report-services/credit-freeze/",
        "online_selfserve": True,
        "notes": "Self-serve via a myEquifax account; the same portal handles "
                 "both placing and lifting a freeze after login.",
    },
    "experian": {
        "display_name": "Experian",
        "freeze_url": "https://www.experian.com/freeze/center.html",
        "thaw_url": "https://www.experian.com/freeze/center.html",
        "online_selfserve": True,
        "notes": "The Security Freeze Center handles both placing and lifting a freeze.",
    },
    "transunion": {
        "display_name": "TransUnion",
        "freeze_url": "https://www.transunion.com/credit-freeze",
        "thaw_url": "https://www.transunion.com/credit-freeze",
        "online_selfserve": True,
        "notes": "",
    },
    "innovis": {
        "display_name": "Innovis",
        # needs-verification: Innovis has restructured its site before; this
        # is the bureau's own security-freeze landing page, not a confirmed
        # deep-linked form path.
        "freeze_url": "https://www.innovis.com/securityFreeze/index",
        "thaw_url": "https://www.innovis.com/securityFreeze/index",
        "online_selfserve": True,
        "notes": "needs-verification: confirm the exact freeze/thaw form path "
                 "against the live site before surfacing this link to a user.",
    },
    "chexsystems": {
        "display_name": "ChexSystems",
        # needs-verification: exact path under chexsystems.com has moved
        # before (consumer-facing security-freeze section).
        "freeze_url": "https://www.chexsystems.com/security-freeze/place-freeze/",
        "thaw_url": "https://www.chexsystems.com/security-freeze/place-freeze/",
        "online_selfserve": True,
        "notes": "needs-verification: confirm the exact freeze/thaw form path "
                 "against the live site before surfacing this link to a user. "
                 "ChexSystems covers checking-account history, not credit per se.",
    },
    "nctue": {
        "display_name": "NCTUE",
        # needs-verification: NCTUE's consumer freeze portal URL/path.
        "freeze_url": "https://www.nctue.com/consumers",
        "thaw_url": "https://www.nctue.com/consumers",
        "online_selfserve": True,
        "notes": "needs-verification: confirm the exact freeze request path "
                 "against the live site. NCTUE aggregates telecom/utility "
                 "payment history, not a traditional credit file.",
    },
    "lexisnexis": {
        "display_name": "LexisNexis Risk Solutions",
        # needs-verification: LexisNexis's consumer freeze/opt-out portal
        # path has changed hosts before (lexisnexis.com vs. risk.lexisnexis.com).
        "freeze_url": "https://consumer.risk.lexisnexis.com/freeze",
        "thaw_url": "https://consumer.risk.lexisnexis.com/freeze",
        "online_selfserve": True,
        "notes": "needs-verification: confirm the exact current host/path -- "
                 "this bureau's consumer portal URL has moved before.",
    },
}


def validate_registry(registry: dict = BUREAUS) -> list[str]:
    """Return a list of problems with *registry* (empty == fine). Every
    bureau must carry all of REQUIRED_BUREAU_FIELDS with the right types."""
    problems = []
    for key, entry in registry.items():
        if not isinstance(entry, dict):
            problems.append(f"{key}: entry is not a dict")
            continue
        for field_name in REQUIRED_BUREAU_FIELDS:
            if field_name not in entry:
                problems.append(f"{key}: missing field {field_name!r}")
        if "online_selfserve" in entry and not isinstance(entry["online_selfserve"], bool):
            problems.append(f"{key}: online_selfserve must be a bool")
        for url_field in ("freeze_url", "thaw_url"):
            value = entry.get(url_field)
            if value is not None and not (isinstance(value, str) and value.startswith("https://")):
                problems.append(f"{key}: {url_field} must be an https:// URL")
    return problems


# --- status model ------------------------------------------------------------

STATUS_NOT_STARTED = "not_started"
STATUS_PENDING = "pending"
STATUS_FROZEN = "frozen"
STATUS_THAWED = "thawed"
VALID_STATUSES = (STATUS_NOT_STARTED, STATUS_PENDING, STATUS_FROZEN, STATUS_THAWED)

# Allowed next-status sets. A freeze normally goes not_started -> pending
# (request submitted, awaiting confirmation) -> frozen; a frozen bureau can
# be thawed; a thaw can be re-frozen directly or re-enter pending if the
# re-freeze request itself needs to be tracked as in-flight.
_ALLOWED_TRANSITIONS = {
    STATUS_NOT_STARTED: {STATUS_PENDING, STATUS_FROZEN},
    STATUS_PENDING: {STATUS_FROZEN, STATUS_NOT_STARTED},
    STATUS_FROZEN: {STATUS_THAWED},
    STATUS_THAWED: {STATUS_FROZEN, STATUS_PENDING},
}


def is_valid_transition(current: str, new_status: str) -> bool:
    if current not in VALID_STATUSES or new_status not in VALID_STATUSES:
        return False
    return new_status in _ALLOWED_TRANSITIONS.get(current, set())


@dataclass
class BureauFreezeState:
    """Per-bureau tracked state for one identity.

    ``pin_token`` is a Fernet ciphertext (or None) -- the plaintext PIN is
    never stored anywhere on this object; it only ever exists transiently
    inside :meth:`set_pin`/:meth:`get_pin`, both of which require the caller
    to supply the encryption key.
    """

    bureau_key: str
    status: str = STATUS_NOT_STARTED
    pin_token: str | None = None
    last_updated: str | None = None
    reminder_date: str | None = None

    def set_pin(self, plaintext_pin: str, key: bytes) -> None:
        """Encrypt *plaintext_pin* with *key* and store only the ciphertext."""
        if not plaintext_pin:
            raise ValueError("pin must be a non-empty string")
        self.pin_token = encrypt_field(plaintext_pin, key)

    def has_pin(self) -> bool:
        return self.pin_token is not None

    def get_pin(self, key: bytes) -> str:
        """Decrypt and return the stored PIN. Raises ``cryptography.fernet.
        InvalidToken`` on a wrong key -- never returns garbage."""
        if not self.pin_token:
            raise ValueError("no pin stored for this bureau")
        return decrypt_field(self.pin_token, key)


def transition(state: BureauFreezeState, new_status: str, now_iso: str) -> BureauFreezeState:
    """Move *state* to *new_status*, stamping ``last_updated``. Raises
    ValueError on an invalid transition; *state* is left unchanged in that
    case."""
    if not is_valid_transition(state.status, new_status):
        raise ValueError(f"cannot transition from {state.status!r} to {new_status!r}")
    state.status = new_status
    state.last_updated = now_iso
    return state


def semi_automate_freeze(bureau_key: str) -> dict:
    """Interface for a future eraser ``fill``/chromedp flow to semi-automate
    placing a freeze at *bureau_key*.

    Not implemented yet -- every bureau in :data:`BUREAUS` must currently be
    frozen by hand via its ``freeze_url``. This function exists so callers
    can be written against the eventual interface now; it always raises
    NotImplementedError today.
    """
    if bureau_key not in BUREAUS:
        raise ValueError(f"unknown bureau {bureau_key!r}")
    raise NotImplementedError(
        f"semi-automated freeze filling for {bureau_key!r} is not implemented yet; "
        f"complete it manually via {BUREAUS[bureau_key]['freeze_url']}"
    )
