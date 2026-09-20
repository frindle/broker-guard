"""broker_guard.profile -- Identity dataclass for broker profiles."""
import json
from dataclasses import dataclass, field


@dataclass
class Identity:
    first_name: str
    last_name: str
    middle_name: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)


def name_variants(identity: Identity) -> list[str]:
    first, last, middle = (identity.first_name.strip(), identity.last_name.strip(), identity.middle_name.strip())
    candidates = [f"{first} {last}".strip()] + ([f"{first} {middle} {last}".strip()] if middle else [])
    return [variant for variant in dict.fromkeys(candidates) if variant]


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    raise ValueError("expected a string or a list")


def load_profile(path: str) -> Identity:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("profile must be a JSON object")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    for name in (first_name, last_name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("first_name and last_name are required non-blank strings")
    middle_name = data.get("middle_name", "")
    return Identity(
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        emails=_as_list(data.get("emails")),
        phones=_as_list(data.get("phones")),
        addresses=_as_list(data.get("addresses")),
    )
