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


def build_site_checks(brokers: list[dict], base_terms: list[str],
                      identity=None) -> list[dict]:
    """One check per automatable broker with a usable http(s) URL.

    Brokers whose URL is missing or uses a non-http(s) scheme are skipped
    rather than handed to the browser.

    When *identity* is given AND the broker has a hand-verified recipe in
    ``search_forms.RECIPES``, the check additionally carries a ``search``
    block -- the recipe's broker id plus the already-resolved
    ``{selector: value}`` map -- and ``search_probe.SearchChecker`` uses it
    to drive that broker's own people-search form instead of scanning its
    homepage. The homepage ``url`` stays on the check either way, so a
    checker that does not understand the block (today's
    ``browser.PlaywrightChecker``) behaves exactly as before.

    The block is added only when the profile can fill every REQUIRED field
    of the recipe; a profile that cannot keeps the homepage check rather
    than submitting a half-filled search. Which set of brokers gets checked
    at all is deliberately unchanged -- this adds detail to existing checks,
    it never adds or removes a broker.
    """
    checks = []
    for broker in brokers:
        if not is_automatable(verification_kind(broker)):
            continue
        url = broker.get("url")
        if not is_safe_url(url):
            continue
        check = {"broker_id": broker["id"], "url": url.strip(),
                 "terms": list(base_terms)}
        search = _search_block(broker["id"], identity)
        if search is not None:
            check["search"] = search
        checks.append(check)
    return checks


def _search_block(broker_id: str, identity) -> "dict | None":
    """The ``search`` block for *broker_id*, or None to keep homepage behaviour.

    Deliberately total: any failure to build the block (no recipe, an
    unfillable profile, an unsafe-looking recipe) returns None, i.e. falls
    back to the status quo. Losing the better check is acceptable; losing
    the broker is not.
    """
    if identity is None:
        return None
    from broker_guard import search_forms

    if not search_forms.is_supported(broker_id):
        return None
    recipe = search_forms.recipe_for(broker_id)
    try:
        search_forms.assert_read_only(recipe)
    except search_forms.UnsafeRecipeError:
        return None
    resolved = search_forms.resolve_search_fields(recipe, identity)
    if resolved["missing"]:
        return None
    return {"broker_id": recipe.broker_id, "values": resolved["values"]}


def interpret_check_result(raw: dict) -> dict:
    if isinstance(raw, dict) and "found" in raw:
        return {"checked": True, "present": bool(raw["found"]), "error": None}
    message = raw.get("error") if isinstance(raw, dict) else "unrecognized page_action result"
    return {"checked": False, "present": False, "error": str(message)}


def run_playwright_checks(site_checks: list[dict], page_action, observer=None) -> dict:
    """Run every check, mapping broker_id -> normalized result.

    A ``page_action`` that raises (browser crash, navigation timeout, target
    closed) is recorded as an errored check for that broker instead of
    aborting the whole sweep -- one dead broker site must not cost the run.

    ``observer`` is an optional ``(broker_id, outcome, hits, errors)``
    callable invoked once per check, with the same ``'hit' | 'checked' |
    'error' | 'skipped'`` vocabulary ``serpwatch.run_serpwatch`` uses (see
    ``broker_guard.progress``), so the dashboard's live counter advances
    through the browser leg the same way it does through the SERP leg. A
    check that errored is reported as ``'error'``, never as ``'checked'``
    -- an unreachable broker site is unknown, not absent, which is the
    same distinction ``service.build_presence_checker`` already enforces by
    raising ``PresenceUnknown``.
    """
    results = {}
    for check in site_checks:
        try:
            raw = page_action(check)
        except Exception as exc:
            raw = {"error": "{}: {}".format(type(exc).__name__, exc)}
        result = interpret_check_result(raw)
        results[check["broker_id"]] = result
        if observer is not None:
            if not result["checked"]:
                outcome = "error"
            elif result["present"]:
                outcome = "hit"
            else:
                outcome = "checked"
            try:
                observer(check["broker_id"], outcome,
                         1 if outcome == "hit" else 0,
                         1 if outcome == "error" else 0)
            except Exception:  # pragma: no cover - telemetry must not abort a sweep
                pass
    return results
