"""broker_guard.profile -- Identity dataclass for broker profiles."""
import hashlib
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
    # Optional eraser profile *id* (not PII) -- see eraser.build_eraser_cmd.
    eraser_profile: str | None = None

    @property
    def full_name(self) -> str:
        parts = [self.first_name.strip(), self.middle_name.strip(), self.last_name.strip()]
        return " ".join(p for p in parts if p)

    @property
    def identity_key(self) -> str:
        """Stable, non-reversible key for this identity.

        Used as the ``identity_key`` column in the state db so the database is
        keyed by a digest rather than by the person's plaintext name.
        """
        seed = "{}|{}|{}".format(
            self.first_name.strip().lower(),
            self.middle_name.strip().lower(),
            self.last_name.strip().lower(),
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def to_eraser_profile(self) -> dict:
        """The dict shape ``eraser.build_eraser_cmd`` consumes."""
        return {"full_name": self.full_name, "eraser_profile": self.eraser_profile}


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
    middle_name = data.get("middle_name") or ""
    if not isinstance(middle_name, str):
        raise ValueError("middle_name must be a string")

    addresses = [a for a in _as_list(data.get("addresses")) if isinstance(a, str) and a.strip()]
    # profile.example.json carries city/state rather than a full address line;
    # without this, such a profile produced no address at all and therefore no
    # name-scoped searches under the original query builder.
    city = (data.get("city") or "").strip()
    region = (data.get("state") or "").strip()
    locality = ", ".join(p for p in (city, region) if p)
    if locality and locality not in addresses:
        addresses.append(locality)

    eraser_profile = data.get("eraser_profile")
    if eraser_profile is not None and not isinstance(eraser_profile, str):
        raise ValueError("eraser_profile must be a string id")

    return Identity(
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        emails=[e for e in _as_list(data.get("emails")) if isinstance(e, str) and e.strip()],
        phones=[p for p in _as_list(data.get("phones")) if isinstance(p, str) and p.strip()],
        addresses=addresses,
        eraser_profile=eraser_profile,
    )
