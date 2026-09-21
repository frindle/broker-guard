"""Build per-site presence checks and normalize raw page_action results.

``build_site_checks`` is the only place a broker-supplied URL becomes something
a browser will be pointed at, so it is also where the scheme is validated.
"""
from urllib.parse import urlsplit

from broker_guard.brokers import is_automatable, verification_kind

# Only these schemes may ever reach a browser. The broker dataset is pulled
# from a separate repo at runtime, so a `file://`, `javascript:` or `data:`
# entry in it must not become a local-file read or script execution.
ALLOWED_SCHEMES = ("http", "https")


def is_safe_url(url) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parts = urlsplit(url.strip())
    return parts.scheme.lower() in ALLOWED_SCHEMES and bool(parts.hostname)


def build_site_checks(brokers: list[dict], base_terms: list[str]) -> list[dict]:
    """One check per automatable broker with a usable http(s) URL.

    Brokers whose URL is missing or uses a non-http(s) scheme are skipped
    rather than handed to the browser.
    """
    checks = []
    for broker in brokers:
        if not is_automatable(verification_kind(broker)):
            continue
        url = broker.get("url")
        if not is_safe_url(url):
            continue
        checks.append(
            {"broker_id": broker["id"], "url": url.strip(), "terms": list(base_terms)}
        )
    return checks


def interpret_check_result(raw: dict) -> dict:
    if isinstance(raw, dict) and "found" in raw:
        return {"checked": True, "present": bool(raw["found"]), "error": None}
    message = raw.get("error") if isinstance(raw, dict) else "unrecognized page_action result"
    return {"checked": False, "present": False, "error": str(message)}


def run_playwright_checks(site_checks: list[dict], page_action) -> dict:
    """Run every check, mapping broker_id -> normalized result.

    A ``page_action`` that raises (browser crash, navigation timeout, target
    closed) is recorded as an errored check for that broker instead of
    aborting the whole sweep -- one dead broker site must not cost the run.
    """
    results = {}
    for check in site_checks:
        try:
            raw = page_action(check)
        except Exception as exc:
            raw = {"error": "{}: {}".format(type(exc).__name__, exc)}
        results[check["broker_id"]] = interpret_check_result(raw)
    return results
