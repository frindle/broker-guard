import hashlib
import json
from datetime import datetime, timedelta, timezone


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'.

    Naive timestamps are assumed UTC so that a naive and an aware timestamp
    can always be compared without raising TypeError.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a non-empty ISO-8601 timestamp string")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def audit_entry(action: dict, actor: str, ts_iso: str, prev_hash: str) -> dict:
    payload = {"ts": ts_iso, "actor": actor, "action": action, "prev_hash": prev_hash}
    return {
        "ts": ts_iso,
        "actor": actor,
        "action": action,
        "prev_hash": prev_hash,
        "hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def next_escalation_actions(status: dict, rules: list[dict]) -> list[dict]:
    matched = [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("field") in status
        and status[rule["field"]] == rule.get("equals")
    ]
    return sorted(matched, key=lambda rule: rule.get("priority", 0))


def is_overdue(submitted_iso: str, now_iso: str, sla_days: int) -> bool:
    deadline = _parse_ts(submitted_iso) + timedelta(days=sla_days)
    return _parse_ts(now_iso) > deadline


def days_remaining(submitted_iso: str, now_iso: str, sla_days: int) -> int:
    deadline = _parse_ts(submitted_iso) + timedelta(days=sla_days)
    return (deadline - _parse_ts(now_iso)).days


def render_letter(template: str, context: dict) -> str:
    out = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            if i + 1 < n and template[i + 1] == "{":
                out.append("{")
                i += 2
                continue
            close = template.find("}", i)
            if close == -1:
                raise ValueError("unterminated placeholder in letter template: {!r}".format(template))
            key = template[i + 1 : close]
            if key not in context:
                raise ValueError("letter template references missing context key: {!r}".format(key))
            out.append(str(context[key]))
            i = close + 1
        elif ch == "}":
            if i + 1 < n and template[i + 1] == "}":
                out.append("}")
                i += 2
                continue
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def route_escalation(action_kind: str, config: dict) -> dict:
    """Decide whether an escalation of *action_kind* may be sent automatically.

    Every branch returns the SAME three keys, so callers can rely on the shape:
    ``auto_send``, ``requires_confirm`` and ``requires_human_confirm``.
    Anything that is not auto-sent requires a confirmation; ``fcra_freeze``
    additionally requires a *human* (never an agent) to confirm.
    """
    if not isinstance(config, dict):
        config = {}
    if action_kind == "broker_facing":
        # Broker-facing follow-ups are low-risk and tier-1 auto.
        return {"auto_send": True, "requires_confirm": False, "requires_human_confirm": False}
    if action_kind == "regulator_complaint":
        auto = bool(config.get("auto_file_regulator_complaints", False))
        return {"auto_send": auto, "requires_confirm": not auto, "requires_human_confirm": not auto}
    if action_kind == "fcra_freeze":
        return {"auto_send": False, "requires_confirm": True, "requires_human_confirm": True}
    return {"auto_send": False, "requires_confirm": True, "requires_human_confirm": True}
