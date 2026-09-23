"""Drive a broker's own people-search form and read the answer back.

The driver half of ``search_forms.py``, and a thin one on purpose: it owns
no browser plumbing of its own. ``SearchChecker`` subclasses
``browser.PlaywrightChecker`` so the Chromium launch, the read-only resource
blocking (``browser._BLOCKED_RESOURCE_TYPES``), the context/page lifecycle
and ``browser.bot_wall_reason`` are all the existing, already-tested ones.
What is new here is only the four steps a homepage fetch does not have:
fill, submit, find the page the answer landed on, and wait for it to settle.

Contract
-----------
``SearchChecker`` IS a ``page_action``: same ``{'broker_id','url','terms'}``
check dict in, same ``{'found': bool} | {'error': str}`` out, so
``playwright_checks.run_playwright_checks`` and everything downstream of it
are unchanged. A check that carries a ``search`` block (added by
``build_site_checks`` when the broker has a verified recipe AND the profile
can fill it) takes the search path; every other check falls through to
``PlaywrightChecker.__call__``, i.e. exactly today's homepage behaviour.
Nothing stops being checked, and nothing is checked differently unless a
human wrote a recipe for it.

Every failure is an error, never a "no"
------------------------------------------
Form missing, submit did nothing, still on the interstitial, landed on
another company's site, bot wall, timeout: all ``{"error": ...}``.
``interpret_check_result`` maps that to checked=False ->
``service.PresenceUnknown`` -> the broker is bucketed under ``errors`` and
excluded from ``resolved``, so autopilot never calls ``store.forget()`` on
it. The only two ways this module returns ``{"found": False}`` are a result
count the broker printed as zero and no-results wording read off that
broker's own miss page.
"""
import logging
import time
from urllib.parse import urlsplit

from broker_guard import search_forms
from broker_guard.browser import PlaywrightChecker, bot_wall_reason
from broker_guard.playwright_checks import is_safe_url

log = logging.getLogger("broker_guard.search_probe")

# How long to keep asking "has the results page settled yet?". ThatsThem sits
# on a "Searching | ThatsThem" interstitial for several seconds before the
# real results render, so this cannot be a single read after load.
_READY_TIMEOUT_MS = 20000
_READY_POLL_MS = 500

# How long to let the form page finish wiring itself up before typing into it.
#
# Not a superstition -- this was measured. USPhonebook's hero form is a plain
# ``<form method=post action=/person-search>`` that its JavaScript intercepts
# and rewrites into a slug navigation. Clicking Submit at ``domcontentloaded``,
# before that handler is bound, fires the RAW post, which the site answers
# with a 404 page titled "Page not found 404 error". Read naively, that page
# is a broker saying "no such person" -- a false negative manufactured
# entirely by clicking too early. With this settle (verified live on
# 2026-09-22) the same run lands on "247 Public Records Found for John Smith".
_FORM_SETTLE_MS = 2000


def _settle(page, settle_ms: int) -> None:
    """Best effort: wait for load, then a beat. Never raises.

    ``wait_for_load_state``/``wait_for_timeout`` are absent on the fake pages
    the test suite drives, and a page that never reaches ``load`` is not a
    reason to abandon a search -- the ready-marker poll is the real gate.
    """
    for call, arg in ((getattr(page, "wait_for_load_state", None), "load"),
                      (getattr(page, "wait_for_timeout", None), settle_ms)):
        if call is None:
            continue
        try:
            call(arg)
        except Exception:
            pass


def _safe_error(exc) -> str:
    """Exception class + first line, truncated.

    Playwright puts the page URL and the full selector into its error
    messages; a broker search URL can carry the query string, i.e. the
    person's name. Same rule ``optout_submit._safe_error`` applies.
    """
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return "{}: {}".format(type(exc).__name__, first_line[:200])


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:  # pragma: no cover - defensive
        return ""


def host_matches(url: str, expected: str) -> bool:
    """Is *url* served by *expected* (or a subdomain of it)?

    ``www.usphonebook.com`` matches ``usphonebook.com``;
    ``spokeo.com`` does not, and that is the point -- see
    ``search_forms``'s finding (2): ThatsThem's submit button opens the
    results in a new tab and redirects the ORIGINAL tab to Spokeo. Reading
    whichever page happens to be in hand would have Spokeo's homepage
    answering a question about ThatsThem.
    """
    host, want = _host(url), (expected or "").lower().strip(".")
    if not host or not want:
        return False
    return host == want or host.endswith("." + want)


def _pages_of(context, fallback):
    pages = getattr(context, "pages", None)
    if not pages:
        return [fallback]
    return list(pages)


def _read(page):
    """(text, title) for *page*, best effort and never raising."""
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    return text or "", title or ""


def find_results_page(context, page, recipe):
    """The page still served by the broker, or None.

    Checked on every poll rather than once, because the results tab may not
    exist (or may still be ``about:blank``) at the moment Submit is clicked.
    """
    for candidate in _pages_of(context, page):
        try:
            url = candidate.url
        except Exception:  # pragma: no cover - defensive
            continue
        if host_matches(url, recipe.results_host):
            return candidate
    return None


def await_results(context, page, recipe, timeout_ms=_READY_TIMEOUT_MS,
                  sleep=time.sleep, now=time.monotonic):
    """Poll until the broker's results page settles.

    Returns ``(page, text, title)`` or raises ``TimeoutError``. A bot wall
    spotted while polling ends the wait immediately -- there is no point
    waiting out a challenge page, and pretending we might still get results
    is how a wall becomes a "no".
    """
    deadline = now() + (timeout_ms / 1000.0)
    last = "no page served by {} after submitting".format(recipe.results_host)
    while True:
        candidate = find_results_page(context, page, recipe)
        if candidate is not None:
            text, title = _read(candidate)
            wall = bot_wall_reason(text, title, None)
            if wall:
                return candidate, text, title
            if search_forms.page_is_ready(recipe, text):
                return candidate, text, title
            last = "results page never settled (title {!r})".format(title[:80])
        if now() >= deadline:
            raise TimeoutError(last)
        sleep(_READY_POLL_MS / 1000.0)


def run_search(context, page, recipe, values: dict, terms: list,
               timeout_ms: int = 30000,
               ready_timeout_ms: int = _READY_TIMEOUT_MS,
               settle_ms: int = _FORM_SETTLE_MS) -> dict:
    """Fill *recipe*'s form on *page*, submit it, and classify the answer.

    Takes an already-open (context, page) so it is drivable by a fake in the
    test suite exactly the way ``optout_submit`` is. ``ready_timeout_ms`` is
    separate from ``timeout_ms`` because "how long a navigation may take" and
    "how long this broker's search may churn before it prints an answer" are
    different questions; the test suite also shortens it so the
    never-settles cases do not cost twenty real seconds each.
    """
    # Re-asserted here, in the one function that can actually type into a
    # broker's page, and not only in the caller. A recipe is data and data
    # gets edited; "it only ever searches" has to be true at runtime.
    search_forms.assert_read_only(recipe)

    try:
        page.goto(recipe.search_url, wait_until="domcontentloaded",
                  timeout=timeout_ms)
    except Exception as exc:
        return {"error": "could not open search form: " + _safe_error(exc)}

    _settle(page, settle_ms)

    text, title = _read(page)
    wall = bot_wall_reason(text, title, None)
    if wall:
        log.warning("bot wall on broker search form",
                    extra={"broker_id": recipe.broker_id, "reason": wall})
        return {"error": wall}

    for field in recipe.fields:
        value = values.get(field.selector)
        if not value:
            if field.required:
                return {"error": "no value for required search field {!r}".format(
                    field.label)}
            continue
        try:
            page.fill(field.selector, value)
        except Exception as exc:
            # Selector drift after a redesign. Unknown, emphatically not
            # "this broker has no record of them".
            return {"error": "could not fill {!r}: {}".format(
                field.label, _safe_error(exc))}

    try:
        page.click(recipe.submit_selector)
    except Exception as exc:
        return {"error": "could not submit the search: " + _safe_error(exc)}

    try:
        results_page, text, title = await_results(
            context, page, recipe, timeout_ms=ready_timeout_ms)
    except TimeoutError as exc:
        return {"error": "search did not produce a results page: {}".format(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": "waiting for results failed: " + _safe_error(exc)}

    # Asked again on the RESULTS page: a site can serve the form happily and
    # then challenge the search itself.
    wall = bot_wall_reason(text, title, None)
    if wall:
        log.warning("bot wall on broker results page",
                    extra={"broker_id": recipe.broker_id, "reason": wall})
        return {"error": wall}

    return search_forms.classify_search_page(recipe, text, terms)


class SearchChecker(PlaywrightChecker):
    """``PlaywrightChecker`` that uses a broker's own search form when we have one.

    Drop-in for ``browser.PlaywrightChecker``: a check with no ``search``
    block behaves identically to today, because it literally is today's code
    path.
    """

    def __call__(self, check: dict) -> dict:
        search = check.get("search") if isinstance(check, dict) else None
        if not search:
            return super().__call__(check)

        broker_id = search.get("broker_id") or check.get("broker_id")
        try:
            recipe = search_forms.recipe_for(broker_id)
        except search_forms.SearchRecipeNotFound:
            # The check claims a recipe that no longer exists (a recipe was
            # deleted while a built check list was in flight). Fall back to
            # the homepage check rather than dropping the broker.
            return super().__call__(check)
        try:
            search_forms.assert_read_only(recipe)
        except search_forms.UnsafeRecipeError as exc:
            return {"error": str(exc)}
        if not is_safe_url(recipe.search_url):
            return {"error": "refusing non-http(s) search url for {!r}".format(
                broker_id)}
        if self._browser is None:
            return {"error": "browser not started"}

        context = page = None
        try:
            context, page = self.new_page()
            return run_search(context, page, recipe,
                              search.get("values") or {},
                              check.get("terms") or [],
                              timeout_ms=self.timeout_ms)
        except Exception as exc:
            return {"error": _safe_error(exc)}
        finally:
            # Every page in the context, not just ours: a target=_blank form
            # leaves a second tab behind, and a sweep over hundreds of
            # brokers must not accumulate them.
            for obj in (_pages_of(context, page) if context is not None else [page]):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


def make_page_action(timeout_ms: int = 30000, headless: bool = True):
    """``browser.make_page_action``, but search-form aware.

    Same contract and the same graceful degradation when Playwright is
    missing -- it reuses that function, only swapping in this checker class.
    """
    from broker_guard.browser import make_page_action as _make

    return _make(timeout_ms=timeout_ms, headless=headless,
                 checker_factory=SearchChecker)
