"""Query construction and hit matching for people-search detection."""
import re
from dataclasses import dataclass


def build_search_queries(phones: list[str], emails: list[str], name_variants: list[str], addresses: list[str]) -> list[dict]:
    """Build the per-identity query set.

    Phone and email queries are one per value. Name queries are crossed with
    every address; when the profile carries NO address a bare ``name`` query is
    emitted instead, so a profile without an address still gets searched at all
    (previously such a profile produced zero name queries).
    """
    queries = []
    for phone in phones:
        query = {'kind':'phone','value': phone}
        if query not in queries:
            queries.append(query)
    for email in emails:
        query = {'kind':'email','value': email}
        if query not in queries:
            queries.append(query)
    for name_variant in name_variants:
        if addresses:
            for address in addresses:
                query = {'kind': 'name_address', 'name': name_variant, 'address': address}
                if query not in queries:
                    queries.append(query)
        else:
            query = {'kind': 'name', 'value': name_variant}
            if query not in queries:
                queries.append(query)
    return queries


_DIGITS_RE = re.compile(r"\D+")
# A term shorter than this is too generic to be evidence of a listing.
_MIN_TERM_LEN = 4


def _phone_digits(value: str) -> str:
    digits = _DIGITS_RE.sub("", value)
    # Drop a US country-code prefix so +1-555-0100 matches (555) 010-0.
    # The remainder must be a plausible subscriber/national number (7 or 10
    # digits) -- otherwise a leading 1 is part of the number, not a prefix.
    if digits.startswith("1") and len(digits) - 1 in (7, 10):
        digits = digits[1:]
    return digits


def is_people_search_hit(result: dict, terms: list[str]) -> bool:
    """True when any of *terms* appears in the result's title/snippet/url.

    Matching is case-insensitive. Terms that look like phone numbers are
    additionally matched on their digits alone, so a profile phone written as
    ``+1-555-0100`` still matches a page rendering it as ``(555) 010-0``.
    Terms shorter than four characters are ignored -- they match essentially
    any page and only produce false presence reports.
    """
    if not isinstance(result, dict):
        return False
    haystack = "".join(
        str(result.get(field, "") or "") for field in ("title", "snippet", "url")
    ).lower()
    haystack_digits = _DIGITS_RE.sub("", haystack)
    for term in terms or []:
        if not isinstance(term, str):
            continue
        term = term.strip()
        if len(term) < _MIN_TERM_LEN:
            continue
        if term.lower() in haystack:
            return True
        digits = _phone_digits(term)
        if len(digits) >= 7 and digits in haystack_digits:
            return True
    return False


@dataclass
class PresenceResult:
    broker_id: str
    record_key: str
    matched_on: list[str]


def dedupe_hits(hits: list[PresenceResult]) -> list[PresenceResult]:
    merged = {}
    order = []
    for hit in hits:
        key = (hit.broker_id, hit.record_key)
        if key not in merged:
            merged[key] = PresenceResult(hit.broker_id, hit.record_key, [])
            order.append(key)
        tags = merged[key].matched_on
        for tag in hit.matched_on:
            if tag not in tags:
                tags.append(tag)
    return [merged[key] for key in order]
