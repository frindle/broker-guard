"""Normalize the public data-broker-optout-list dataset into broker-guard's
own broker-record shape (see ``data/brokers.example.json``) and, where
possible, cross-reference each record against the vendored ``eraser`` CLI's
own broker list so ``eraser_id`` can be used to translate broker-guard ids
into the ids eraser's ``send --broker <id>`` expects.

Source dataset shape (flat list, no ``id``/``url``)::

    {name, domain, category, opt_out_url, opt_out_method, opt_out_email,
     required_fields, verification_step, opt_out, source, last_verified, notes}

Output shape, one broker-guard record per kept source entry::

    {id, name, url, category, optout_url, optout_method, optout_email,
     verification, eraser_id}

``brokers.load_brokers()`` only requires ``id``/``name``/``url`` to be
present, so every extra key here (``optout_email``, ``verification``,
``eraser_id``) passes straight through untouched.

Kind mapping (``verification``, see ``brokers.VALID_KINDS``)
--------------------------------------------------------------
Applied in priority order against the source's ``verification_step`` text
and ``opt_out_method``:

0. If ``verification_step`` is one of the dataset's two "not individually
   confirmed" placeholder texts (or blank/"unknown") -- both of which go on
   to say "check the form for CAPTCHA/ID/email-confirmation requirements"
   as generic advice, not a finding -- none of the keyword checks below
   are trusted; classification falls straight through to the
   channel-based rule (step 4).
1. Any *confirmed* mention of a government ID, notarization, SSN or
   "proof of identity" -> ``photo_id`` (the heaviest gate the source data
   calls out).
2. Any *confirmed* mention of CAPTCHA -> ``captcha`` (checked after the ID
   signal so the one record needing both lands on the heavier photo_id
   gate).
3. Confirmed phone verification text, or an opt-out channel that IS a
   phone call (``opt_out_method == "phone"``) -> ``kba``. There's no
   dedicated "phone" kind in ``VALID_KINDS``; a live call is a
   human-gated identity check, closer to ``kba`` than to a document
   upload.
4. Otherwise: **maximize automation** (Penn's call) -- if there is an
   actionable channel at all (``opt_out_method`` is ``web-form`` or
   ``email``) and none of the heavy-gate keywords above matched, mark it
   ``automatable``, even when the source's own ``verification_step`` says
   "not individually confirmed" (~90% of this dataset). A broker that
   actually turns out to need a CAPTCHA/ID simply bounces the automated
   attempt at send time -- low cost, and it beats defaulting the bulk of
   the list to a human-review kind that never gets touched.
5. Anything left over (``opt_out_method`` is ``unknown``/absent and no
   channel signal survived) -> ``kba``, the fallback for "we don't even
   know how you'd reach this broker automatically."

Records dropped
----------------
A source record is dropped (not emitted) when ``opt_out_method`` is
"unknown" *and* neither ``opt_out_url`` nor ``opt_out_email`` is set --
i.e. there is no channel at all to act on. In the current dataset this is
exactly the 26 records whose own ``opt_out`` field is "unknown" (confirmed
dead ends per their ``notes``: FCRA/GLBA exemption replies, hard-bounced
mailboxes, etc). Every other record keeps at least one actionable channel.
"""
import argparse
import json
import re
import sys
from urllib.parse import urlsplit

VALID_KINDS = ("automatable", "captcha", "photo_id", "kba")

# Text fragments in `verification_step`, checked lowercase, that signal a
# government-ID / notarization / SSN requirement.
_PHOTO_ID_MARKERS = (
    "government-id", "government id", "notariz", "ssn", "proof of identity",
)
_CAPTCHA_MARKER = "captcha"
_PHONE_MARKER = "phone verification"
_AUTOMATABLE_METHODS = ("web-form", "email")

# Legal-entity suffixes stripped when normalizing a name for fuzz-free
# exact-match comparison against eraser's broker list.
_NAME_SUFFIXES = (
    " inc", " incorporated", " llc", " l.l.c.", " corp", " corporation",
    " co", " company", " ltd", " limited", " group", " holdings",
)


def slugify(text: str) -> str:
    """Lowercase, non-alnum runs collapsed to a single hyphen, trimmed."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "broker"


def _domain_id(domain: str) -> str:
    """A stable id for a domain: dots become hyphens (``example.com`` ->
    ``example-com``), so the id reads close to the site name rather than
    eraser's own bare-name ids (kept separate in ``eraser_id``)."""
    domain = (domain or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return slugify(domain)


def _normalize_domain(url_or_domain: str) -> str | None:
    """Extract a bare, www-stripped, lowercase hostname from a URL or a
    bare domain string. Returns None when nothing usable is present."""
    if not url_or_domain:
        return None
    value = url_or_domain.strip()
    if "//" not in value:
        value = "//" + value
    host = urlsplit(value).hostname
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _normalize_name(name: str) -> str:
    """Fold a company name down for exact (not fuzzy) comparison: lowercase,
    strip a trailing legal-entity suffix, drop punctuation/whitespace runs."""
    n = (name or "").strip().lower()
    n = re.sub(r"[.,]", "", n)
    for suffix in _NAME_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


_UNCONFIRMED_MARKER = "not individually confirmed"


def classify_kind(record: dict) -> str:
    """Map one source record onto ``brokers.VALID_KINDS``. See the module
    docstring's "Kind mapping" section for the full, documented policy.

    The two "not individually confirmed" texts in this dataset both go on
    to say "check the form for CAPTCHA/ID/email-confirmation requirements"
    -- generic advice, not a finding -- so a bare substring check for
    "captcha"/"government id"/etc would misfire on ~90% of records that
    never actually confirmed any of that. Those heavy-gate keywords are
    only trusted when the text is NOT one of the unconfirmed placeholders.
    """
    text = (record.get("verification_step") or "").lower()
    method = (record.get("opt_out_method") or "").lower()
    confirmed = bool(text) and text != "unknown" and _UNCONFIRMED_MARKER not in text

    if confirmed:
        if any(marker in text for marker in _PHOTO_ID_MARKERS):
            return "photo_id"
        if _CAPTCHA_MARKER in text:
            return "captcha"
        if _PHONE_MARKER in text:
            return "kba"
    if method == "phone":
        return "kba"
    if method in _AUTOMATABLE_METHODS:
        return "automatable"
    return "kba"


def _should_drop(record: dict) -> bool:
    method = (record.get("opt_out_method") or "").lower()
    return (
        method == "unknown"
        and not (record.get("opt_out_url") or "").strip()
        and not (record.get("opt_out_email") or "").strip()
    )


_OPTOUT_METHOD_MAP = {
    "web-form": "form",
    "email": "email",
    "phone": "phone",
    "unknown": "unknown",
}


def build_eraser_index(eraser_brokers: list) -> dict:
    """Build domain / email / normalized-name -> eraser id lookup tables from
    eraser's own ``data/brokers.yaml`` broker list. A key is left out of an
    index entirely if more than one eraser broker shares it -- an ambiguous
    key must not produce a confident match."""
    by_domain: dict[str, list[str]] = {}
    by_email: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}

    for eb in eraser_brokers:
        eid = eb.get("id")
        if not eid:
            continue
        domain = _normalize_domain(eb.get("website") or "")
        if domain:
            by_domain.setdefault(domain, []).append(eid)
        email = (eb.get("email") or "").strip().lower()
        if email:
            by_email.setdefault(email, []).append(eid)
        name_key = _normalize_name(eb.get("name") or "")
        if name_key:
            by_name.setdefault(name_key, []).append(eid)

    def _unambiguous(index: dict) -> dict:
        return {k: v[0] for k, v in index.items() if len(v) == 1}

    return {
        "by_domain": _unambiguous(by_domain),
        "by_email": _unambiguous(by_email),
        "by_name": _unambiguous(by_name),
    }


def match_eraser_id(record: dict, index: dict) -> str | None:
    """Best-confidence eraser id for one source record, or None. Domain
    match first (highest confidence), then exact opt_out_email match (for
    the handful of source records with no domain), then an exact
    normalized-name match (lowest confidence of the three, still exact --
    no fuzzy/substring matching, to avoid a false positive)."""
    domain = _normalize_domain(record.get("domain") or "")
    if domain and domain in index["by_domain"]:
        return index["by_domain"][domain]
    email = (record.get("opt_out_email") or "").strip().lower()
    if email and email in index["by_email"]:
        return index["by_email"][email]
    name_key = _normalize_name(record.get("name") or "")
    if name_key and name_key in index["by_name"]:
        return index["by_name"][name_key]
    return None


def normalize_record(record: dict, index: dict) -> dict | None:
    """Convert one source record into broker-guard's broker shape, or None
    if it should be dropped (see module docstring)."""
    if _should_drop(record):
        return None

    name = (record.get("name") or "").strip()
    domain = (record.get("domain") or "").strip()

    if domain:
        broker_id = _domain_id(domain)
        lowered = domain.lower()
        host = lowered[4:] if lowered.startswith("www.") else lowered
        url = f"https://{host}"
    else:
        # No domain at all (4 records in the current dataset) -- fall back
        # to a name-derived id, and leave url "" rather than guess a site
        # from an opt-out-only mailbox address that may not share the
        # company's own domain (observed in this dataset: an opt-out email
        # hosted on a third-party domain unrelated to the company name).
        broker_id = slugify(name)
        url = ""

    method = (record.get("opt_out_method") or "").strip().lower()
    optout_method = _OPTOUT_METHOD_MAP.get(method, method or "unknown")

    return {
        "id": broker_id,
        "name": name,
        "url": url,
        "category": (record.get("category") or "").strip().lower() or "other",
        "optout_url": (record.get("opt_out_url") or "") or "",
        "optout_method": optout_method,
        "optout_email": (record.get("opt_out_email") or "") or "",
        "verification": classify_kind(record),
        "eraser_id": match_eraser_id(record, index),
    }


def normalize_dataset(source_records: list, eraser_brokers: list) -> tuple[list, dict]:
    """Normalize every record; return (output_records, stats)."""
    index = build_eraser_index(eraser_brokers)
    out = []
    dropped = 0
    kind_counts = {k: 0 for k in VALID_KINDS}
    matched = 0
    seen_ids = set()
    id_collisions = 0

    for record in source_records:
        normalized = normalize_record(record, index)
        if normalized is None:
            dropped += 1
            continue
        if normalized["id"] in seen_ids:
            # Extremely unlikely given the domain-derived ids are unique in
            # the current dataset, but stay honest if it ever happens: keep
            # the first occurrence, same dedup rule as brokers.load_brokers.
            id_collisions += 1
            continue
        seen_ids.add(normalized["id"])
        kind_counts[normalized["verification"]] += 1
        if normalized["eraser_id"]:
            matched += 1
        out.append(normalized)

    total_in = len(source_records)
    total_out = len(out)
    stats = {
        "total_source_records": total_in,
        "dropped": dropped,
        "id_collisions_dropped": id_collisions,
        "total_output_records": total_out,
        "kind_counts": kind_counts,
        "eraser_id_matched": matched,
        "eraser_id_match_rate": round(matched / total_out, 4) if total_out else 0.0,
    }
    return out, stats


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_eraser_brokers(path: str) -> list:
    import yaml  # local import: only the CLI/matching path needs PyYAML

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("brokers") or []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m broker_guard.broker_normalize",
        description="Normalize the public data-broker-optout-list dataset into "
                     "broker-guard's broker-record shape.",
    )
    parser.add_argument("input", help="Path to the source brokers.json (flat list).")
    parser.add_argument("output", help="Path to write the normalized {'brokers': [...]} JSON.")
    parser.add_argument(
        "--eraser-brokers",
        default="vendor/eraser/data/brokers.yaml",
        help="Path to eraser's own broker list, for eraser_id matching "
             "(default: vendor/eraser/data/brokers.yaml).",
    )
    args = parser.parse_args(argv)

    source_records = _load_json(args.input)
    if not isinstance(source_records, list):
        print("error: input must be a JSON array of broker records", file=sys.stderr)
        return 2
    eraser_brokers = _load_eraser_brokers(args.eraser_brokers)

    out, stats = normalize_dataset(source_records, eraser_brokers)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump({"brokers": out}, fh, indent=2)
        fh.write("\n")

    print(f"wrote {stats['total_output_records']} brokers -> {args.output}")
    print(f"source records: {stats['total_source_records']}")
    print(f"dropped (no actionable channel): {stats['dropped']}")
    if stats["id_collisions_dropped"]:
        print(f"dropped (id collision): {stats['id_collisions_dropped']}")
    print("verification kind counts:")
    for kind in VALID_KINDS:
        print(f"  {kind}: {stats['kind_counts'][kind]}")
    print(
        f"eraser_id matched: {stats['eraser_id_matched']}/{stats['total_output_records']} "
        f"({stats['eraser_id_match_rate']:.1%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
