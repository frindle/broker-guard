"""The parity score: how much of the broker list broker-guard handles on its own.

Computed from the code and the dataset, never typed in. Every row of
``data/source-brokers.json`` lands in exactly ONE bucket, by the first rule
that matches:

``automated``  a final automated state exists: a live form recipe (hand-written,
               promoted, or an approved learned one) or an eligible opt-out
               email address (a role inbox on the broker's own domain).
``needs_you``  a human must act and broker-guard prepares the packet
               (``manual_packets``): phone or SMS verification, a mailed or
               notarized letter, KBA, an SSN/DOB/unredacted-ID demand, an
               account-gated site, or a record the person must pick.
``pipeline``   automatable in principle, not yet automatic: a staged recipe
               awaiting one dry run, a blocked form the real-browser/CAPTCHA
               pipeline may clear, an undecided form the assisted filler may
               learn, or a redacted-ID form still waiting for its recipe.
``no_surface`` the broker offers no opt-out at all (nothing to automate or do).

Deliberately honest: ``automated`` counts what is wired, not what has been
switched live -- every channel still defaults to dry-run until Penn flips it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from broker_guard import broker_normalize, optout_email, optout_forms

AUTOMATED, NEEDS_YOU, PIPELINE, NO_SURFACE = "automated", "needs_you", "pipeline", "no_surface"
BUCKETS = (AUTOMATED, NEEDS_YOU, PIPELINE, NO_SURFACE)

# detail labels
RECIPE, EMAIL = "recipe", "email"
PHONE, MAIL, NOTARY, KBA = "phone", "mail_letter", "notarized_letter", "kba"
ID_UNREDACTED, SSN_DOB, ACCOUNT, SELECTION, OTHER_SCOPE = (
    "id_unredacted", "ssn_dob", "account", "selection", "out_of_scope")
STAGED, BLOCKED, UNDECIDED, ID_RECIPE_PENDING, UNMAPPED = (
    "staged", "blocked", "undecided", "redacted_id_recipe_pending", "unmapped")

_PHONE = re.compile(r"phone verification|sms|text message|verification (call|code)", re.I)
_NOTARY = re.compile(r"notari[sz]", re.I)
_MAIL = re.compile(r"postal|by (e?mail|fax)[, ]+(fax|mail|or)|fax, or mail|mailed in|"
                   r"print(ed)? (and )?(mail|send)|certified mail|pdf opt-out form", re.I)
_KBA = re.compile(r"knowledge[- ](based|questions)|\bkba\b", re.I)
_ACCOUNT = re.compile(
    r"requires? (an? )?(account|login|log-in|sign-?in)|account (is )?(required|needed)|"
    r"create an account|must (have|create) an account|log ?in (is )?required|"
    r"behind (a )?(login|sign-?in)|account-gated", re.I)


def row_id(row: dict) -> str:
    return broker_normalize._domain_id(row.get("domain") or "")


def load_source(path: str | None = None) -> list:
    with open(path or broker_normalize.DEFAULT_SOURCE_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else data.get("brokers", [])


def _prose(bid: str) -> str:
    for name in ("OPTOUT_UNDECIDED", "OPTOUT_BLOCKED", "OPTOUT_OUT_OF_SCOPE",
                 "NO_OPTOUT_SURFACE"):
        text = getattr(optout_forms, name, {}).get(bid)
        if text:
            return text
    return ""


def email_eligible(row: dict) -> bool:
    email = (row.get("opt_out_email") or "").strip()
    if not email:
        return False
    try:
        return bool(optout_email.is_eligible(row.get("domain") or "", email))
    except Exception:
        return False


def manual_kind(row: dict, bid: str) -> str | None:
    """Why a human has to act on this row, or ``None``. Source columns first
    (they are the dataset's own words), then the structured out-of-scope verdict."""
    demand = optout_forms.id_demand(bid)
    if demand == "unredacted":
        return ID_UNREDACTED
    if demand in ("ssn", "dob"):
        return SSN_DOB
    if demand == "kba":
        return KBA
    if demand == "selection":
        return SELECTION
    step = " ".join(str(row.get(k) or "") for k in ("verification_step", "opt_out_method", "notes"))
    if _NOTARY.search(step):
        return NOTARY
    if _PHONE.search(step) or (row.get("opt_out_method") or "") == "phone":
        return PHONE
    if _MAIL.search(step):
        return MAIL
    if _KBA.search(step):
        return KBA
    if _ACCOUNT.search(step) or _ACCOUNT.search(_prose(bid)):
        return ACCOUNT
    return None


def classify_row(row: dict, live_recipes=None, email_channel: bool = True) -> tuple:
    """``(bucket, detail)`` for one source row."""
    bid = row_id(row)
    recipes = optout_forms.RECIPES if live_recipes is None else live_recipes
    if bid in recipes:
        return AUTOMATED, RECIPE
    if email_channel and email_eligible(row):
        return AUTOMATED, EMAIL
    kind = manual_kind(row, bid)
    if kind:
        return NEEDS_YOU, kind
    if bid in optout_forms.OPTOUT_OUT_OF_SCOPE:
        if optout_forms.id_demand(bid) in optout_forms.REDACTABLE_ID_DEMANDS:
            return PIPELINE, ID_RECIPE_PENDING
        return NEEDS_YOU, OTHER_SCOPE
    if bid in optout_forms.STAGED_RECIPES:
        return PIPELINE, STAGED
    if bid in optout_forms.OPTOUT_BLOCKED:
        return PIPELINE, BLOCKED
    if bid in optout_forms.OPTOUT_UNDECIDED:
        return PIPELINE, UNDECIDED
    if bid in optout_forms.NO_OPTOUT_SURFACE:
        return NO_SURFACE, NO_SURFACE
    return PIPELINE, UNMAPPED


@dataclass(frozen=True)
class Parity:
    total: int
    counts: dict            # bucket -> n
    detail: dict            # "bucket/detail" -> n

    def share(self, bucket: str, of_reachable: bool = False) -> float:
        denom = self.total - (self.counts.get(NO_SURFACE, 0) if of_reachable else 0)
        return (self.counts.get(bucket, 0) / denom) if denom > 0 else 0.0

    def as_dict(self) -> dict:
        return {"total": self.total, "counts": dict(self.counts), "detail": dict(self.detail),
                "automated_share": self.share(AUTOMATED),
                "needs_you_share": self.share(NEEDS_YOU),
                "automated_share_of_reachable": self.share(AUTOMATED, True)}


def compute(rows, live_recipes=None, email_channel: bool = True) -> Parity:
    counts = {b: 0 for b in BUCKETS}
    detail: dict = {}
    seen = set()
    for row in rows:
        bid = row_id(row)
        if bid in seen:
            continue
        seen.add(bid)
        bucket, why = classify_row(row, live_recipes, email_channel)
        counts[bucket] += 1
        detail["{}/{}".format(bucket, why)] = detail.get("{}/{}".format(bucket, why), 0) + 1
    return Parity(total=len(seen), counts=counts, detail=detail)
