"""Build the vendored eraser removal command for a broker."""
import re

_BROKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def build_eraser_cmd(broker_id: str, profile: dict, eraser_bin: str = 'eraser') -> list[str]:
    if not isinstance(broker_id, str):
        raise ValueError("broker_id must be a non-empty string")
    normalized = broker_id.strip().lower()
    if not _BROKER_ID_RE.match(normalized):
        raise ValueError(f"invalid broker_id: {broker_id!r}")
    full_name = profile['full_name']
    if not isinstance(full_name, str) or not full_name.strip():
        raise ValueError("profile['full_name'] must be a non-empty string")
    return [eraser_bin, "remove", "--name", full_name, "--broker", normalized]


def status_after_removal(current: str, result: dict) -> str:
    if current == 'pending' and result.get('success'):
        return 'submitted'
    return current


def needs_reverify(status: str, present_now: bool) -> bool:
    return status == 'confirmed' and bool(present_now)
