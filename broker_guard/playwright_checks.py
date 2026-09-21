"""Normalize raw page_action results into a fixed three-key shape."""
from broker_guard.brokers import is_automatable, verification_kind


def build_site_checks(brokers: list[dict], base_terms: list[str]) -> list[dict]:
    checks = []
    for broker in brokers:
        if is_automatable(verification_kind(broker)):
            checks.append({"broker_id": broker["id"], "url": broker["url"], "terms": base_terms})
    return checks


def interpret_check_result(raw: dict) -> dict:
    if isinstance(raw, dict) and "found" in raw:
        return {"checked": True, "present": bool(raw["found"]), "error": None}
    message = raw.get("error") if isinstance(raw, dict) else "unrecognized page_action result"
    return {"checked": False, "present": False, "error": str(message)}


def run_playwright_checks(site_checks: list[dict], page_action) -> dict:
    results = {}
    for check in site_checks:
        results[check["broker_id"]] = interpret_check_result(page_action(check))
    return results
