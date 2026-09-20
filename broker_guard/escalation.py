import hashlib
import json
from datetime import datetime, timedelta


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
    matched = [rule for rule in rules if rule.get("field") in status and status[rule["field"]] == rule["equals"]]
    return sorted(matched, key=lambda rule: rule["priority"])


def is_overdue(submitted_iso: str, now_iso: str, sla_days: int) -> bool:
    deadline = datetime.fromisoformat(submitted_iso) + timedelta(days=sla_days)
    return datetime.fromisoformat(now_iso) > deadline


def days_remaining(submitted_iso: str, now_iso: str, sla_days: int) -> int:
    deadline = datetime.fromisoformat(submitted_iso) + timedelta(days=sla_days)
    return (deadline - datetime.fromisoformat(now_iso)).days


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
    if action_kind == "broker_facing":
        return {"auto_send": True, "requires_confirm": False}
    if action_kind == "regulator_complaint":
        return {"auto_send": bool(config.get("auto_file_regulator_complaints", False)), "requires_confirm_X": True}
    if action_kind == "fcra_freeze":
        return {"auto_send": False, "requires_human_confirm": True}
    return {"auto_send": False, "requires_confirm": True}
