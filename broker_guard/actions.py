import hashlib

MANUAL_ACTION_SOURCES = ("captcha_fallback", "photo_id", "kba", "email_confirm")


def build_manual_action(source: str, broker_id: str, context: dict) -> dict:
    if source not in MANUAL_ACTION_SOURCES:
        raise ValueError("source must be one of captcha_fallback, photo_id, kba, email_confirm")
    if not isinstance(context, dict):
        raise ValueError("context must be a dict")
    for required in ("key", "now"):
        # 'now' was read unchecked while 'key' was validated, so a caller
        # omitting it got a bare KeyError instead of this message.
        if required not in context:
            raise ValueError(f"context must include {required}")
    return {
        "id": hashlib.sha256(f'{broker_id}|{source}|{context["key"]}'.encode('utf-8')).hexdigest(),
        "broker_id": broker_id,
        "source": source,
        "status": 'open',
        "created_at": context['now'],
        "payload": context.get('payload', {}),
    }


def build_removal_action(broker_id: str, method: str = "email") -> dict:
    if not isinstance(broker_id, str) or not broker_id.strip():
        raise ValueError("broker_id must be a non-empty string")
    return {"action": "removal", "broker_id": broker_id.strip(), "method": method, "status": "pending"}


def build_escalation_action(broker_id: str, statute: str) -> dict:
    if not isinstance(broker_id, str) or not broker_id.strip():
        raise ValueError("broker_id must be a non-empty string")
    if not isinstance(statute, str) or not statute.strip():
        raise ValueError("statute must be a non-empty string")
    return {"action": "escalation", "broker_id": broker_id.strip(), "statute": statute.strip().upper(), "status": "draft"}


def merge_actions(existing: list[dict], incoming: list[dict]) -> list[dict]:
    known = {item["id"] for item in existing if "id" in item}
    merged = list(existing)
    for item in incoming:
        if "id" not in item or item["id"] not in known:
            merged.append(item)
    return merged
