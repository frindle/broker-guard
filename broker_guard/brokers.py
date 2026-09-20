"""Load and normalize broker records from a brokers.json file."""
import json

VALID_KINDS = ("automatable", "captcha", "photo_id", "kba")


def load_brokers(path):
    """Read the JSON document at *path* and return its normalized brokers.

    The top level must be an object with a 'brokers' list; every entry must
    carry id, name and url.     String values are stripped of surrounding whitespace and each id is lowercased. Raises ValueError otherwise.

    Records whose normalized ids collide are deduplicated, keeping the first
    occurrence of each id in input order.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "brokers" not in data:
        raise ValueError("expected an object with a 'brokers' list")
    brokers = []
    seen = set()
    for record in data["brokers"]:
        missing = [k for k in ("id", "name", "url") if k not in record]
        if missing:
            raise ValueError(
                "broker is missing required field(s): " + ", ".join(missing))
        out = {}
        for key, value in record.items():
            if isinstance(value, str):
                value = value.strip()
            out[key] = value
        out["id"] = out["id"].lower()
        if out["id"] in seen:
            continue
        seen.add(out["id"])
        brokers.append(out)
    return brokers


def verification_kind(broker):
    kind = broker.get('verification')
    if not kind:
        return 'automatable'
    if kind in VALID_KINDS:
        return kind
    return 'manual_review'


def is_automatable(kind):
    return kind in ('automatable', 'captcha')
