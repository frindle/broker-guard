"""Ask every recipe whether its page still looks the way it was written down.

Waiting is the slow way to find recipe rot
---------------------------------------------
``recipe_health`` catches drift when a scan or an opt-out attempt trips over
it. That works, but it is passive: a broker's search recipe only gets
exercised when that broker comes round in a multi-hour sweep, and an opt-out
recipe is only exercised when a human presses the button -- which for most
brokers is never. This module is the active half: point it at the
allow-lists and it opens each recipe's page and checks, element by element,
that every selector the recipe names still matches something.

It is deliberately NOT a submission and NOT a search
-------------------------------------------------------
Nothing is typed, nothing is clicked, nothing is submitted. Each page is
opened and queried, which is strictly less than ``search_probe`` already
does on the read-only leg and far less than ``optout_submit``'s dry run.
That is what makes it safe to run over the WHOLE allow-list on a schedule:
it cannot spend a DSAR request, cannot trip a rate limiter with a search
query, and cannot leave anything behind on a broker's side.

What counts as drift here
----------------------------
A selector that matches ZERO elements is drift: the recipe names something
that is not there. A selector matching MORE than one is also reported,
because ``page.fill`` runs in Playwright's strict mode and a second match
turns into a hard failure the next time the recipe actually runs -- the
classic outcome of a site adding a mobile copy of its own form.

A page that cannot be read at all (timeout, bot wall) is reported as
``blocked``/``transient`` and does NOT count as drift, for the same reason
``recipe_health`` does not alert on those: ThatsThem's recipe is correct and
walled, and calling that "broken" would train Penn to ignore the report.
"""
import logging

from broker_guard import optout_forms, recipe_health, search_forms
from broker_guard.browser import bot_wall_reason

log = logging.getLogger("broker_guard.recipe_check")

_SETTLE_MS = 2500


def selectors_for_search(recipe) -> list:
    """Every selector a search recipe would touch, with its human label."""
    out = [(f.selector, f.label) for f in recipe.fields]
    out.append((recipe.submit_selector, "Search button"))
    return [(s, l) for s, l in out if s]


def selectors_for_optout(recipe) -> list:
    """Every selector an opt-out recipe would touch, with its human label.

    Reuses ``optout_forms.ordered_steps`` rather than re-walking the recipe
    shape, so a recipe written with ``steps`` and one written with
    ``choices``/``fields`` are covered identically -- and so a future step
    type is covered the moment ``ordered_steps`` knows about it.
    """
    out = []
    for step in optout_forms.ordered_steps(recipe):
        if isinstance(step, (optout_forms.Field, optout_forms.Check)):
            # A Field whose value is a deliberately-empty literal (the
            # ACHCOOP State / SearchPublicRecords Age refusals) is still a
            # real element on the page, so it is still worth checking.
            out.append((step.selector, step.label))
        elif isinstance(step, optout_forms.Choice):   # covers Select
            out.append((step.container, step.label))
    if recipe.submit_selector:
        out.append((recipe.submit_selector, "Submit button"))
    return [(s, l) for s, l in out if s]


def check_page(page, url: str, selectors: list, settle_ms: int = _SETTLE_MS) -> dict:
    """Open *url* and report which of *selectors* still match.

    Returns ``{"status", "reason", "missing", "ambiguous", "checked"}``.
    ``status`` is one of ``"ok"``, ``"drift"``, or one of
    ``recipe_health``'s non-alerting classes. Never raises: a probe that
    blows up must report, not abort a sweep over sixty recipes.
    """
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        reason = "{}: {}".format(type(exc).__name__, str(exc).splitlines()[0][:200])
        return {"status": recipe_health.classify_failure(reason), "reason": reason,
                "missing": [], "ambiguous": [], "checked": 0}
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass

    try:
        text, title = page.inner_text("body"), page.title()
    except Exception:
        text, title = "", ""
    wall = bot_wall_reason(text, title, None)
    if wall:
        return {"status": recipe_health.BLOCKED, "reason": wall,
                "missing": [], "ambiguous": [], "checked": 0}

    missing, ambiguous = [], []
    for selector, label in selectors:
        try:
            found = len(page.query_selector_all(selector))
        except Exception as exc:
            # An invalid selector is a recipe bug, not a page change, but it
            # is the same fix (go and edit the recipe), so it reports here.
            missing.append("{} [{}]: bad selector: {}".format(
                label, selector, type(exc).__name__))
            continue
        if found == 0:
            missing.append("{} [{}]".format(label, selector))
        elif found > 1:
            ambiguous.append("{} [{}] matches {}".format(label, selector, found))

    status = "drift" if (missing or ambiguous) else "ok"
    return {"status": status, "reason": None, "missing": missing,
            "ambiguous": ambiguous, "checked": len(selectors)}


def check_all(new_page, search_recipes=None, optout_recipes=None) -> list:
    """Probe every recipe in both allow-lists.

    *new_page* is a zero-argument callable returning a fresh page (a real
    one in production, a fake in the tests). One page per recipe, closed
    afterwards, so a broker that wedges its tab cannot poison the next.
    """
    reports = []
    plan = [
        (recipe_health.LEG_SEARCH,
         search_recipes if search_recipes is not None else search_forms.RECIPES,
         lambda r: r.search_url, selectors_for_search),
        (recipe_health.LEG_OPTOUT,
         optout_recipes if optout_recipes is not None else optout_forms.RECIPES,
         lambda r: r.url, selectors_for_optout),
    ]
    for leg, recipes, url_of, selectors_of in plan:
        for broker_id in sorted(recipes):
            recipe = recipes[broker_id]
            page = None
            try:
                page = new_page()
                result = check_page(page, url_of(recipe), selectors_of(recipe))
            except Exception as exc:
                result = {"status": recipe_health.UNKNOWN,
                          "reason": "{}: {}".format(type(exc).__name__, exc),
                          "missing": [], "ambiguous": [], "checked": 0}
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
            result.update(broker_id=broker_id, leg=leg, url=url_of(recipe))
            reports.append(result)
    return reports


def drift_events(reports, at: str | None = None) -> list:
    """``recipe_drift`` events for the reports that found real drift."""
    events = []
    for report in reports or []:
        if report.get("status") != "drift":
            continue
        detail = "; ".join(
            list(report.get("missing") or []) + list(report.get("ambiguous") or []))
        events.append(recipe_health.drift_event(
            report.get("broker_id"), report.get("leg"),
            "could not fill: recipe selectors no longer match: " + detail, at=at))
    return events


def format_report(reports) -> str:
    """The human-readable ``--check-recipes`` output."""
    lines, drifted = [], 0
    for report in reports or []:
        status = report.get("status")
        line = "{:<10} {:<34} {}".format(status, report.get("broker_id") or "?",
                                         report.get("leg") or "?")
        if report.get("reason"):
            line += "  ({})".format(report["reason"])
        lines.append(line)
        for item in list(report.get("missing") or []):
            lines.append("    MISSING   " + item)
        for item in list(report.get("ambiguous") or []):
            lines.append("    AMBIGUOUS " + item)
        if status == "drift":
            drifted += 1
    lines.append("")
    lines.append("{} recipe(s) checked, {} showing drift".format(
        len(reports or []), drifted))
    return "\n".join(lines)
