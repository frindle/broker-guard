"""Real Playwright ``page_action`` for ``playwright_checks.run_playwright_checks``.

What this actually does: it launches a real headless Chromium, navigates to the
broker's search/profile URL, reads the rendered page text, and reports whether
any identity term appears in it. It does NOT fill or submit any opt-out form --
submissions go through the vendored ``eraser`` engine (``eraser_bridge``), which
owns consent, rate limiting and the audit trail. So this module only ever reads.

If Playwright (or its browser binary) is unavailable, ``make_page_action``
returns a checker that reports an errored check rather than crashing the run,
and the service falls back to SERP-only detection.
"""
import logging

from broker_guard.detection import is_people_search_hit
from broker_guard.playwright_checks import is_safe_url

log = logging.getLogger("broker_guard.browser")

# Read-only navigation: never let the page pull in heavy third-party media, and
# never execute a download.
_BLOCKED_RESOURCE_TYPES = ("image", "media", "font")

# --- Bot-wall detection -------------------------------------------------
#
# Why this exists: a CAPTCHA / Cloudflare challenge page navigates fine. The
# request returns 200, the DOM loads, `inner_text("body")` yields real text --
# and the identity terms are (correctly!) absent from it, because it is a
# challenge page, not the broker's listing. Without this check that came back
# as a clean, error-free `{"found": false}`, i.e. "verified not present",
# which autopilot eventually turns into `store.forget()` -- deleting the
# presence row for a broker that is still publishing the person's PII.
#
# Two tiers, because the false-positive cost is real: a broker's genuine "no
# results for that name" page wrongly called a bot wall becomes a permanent
# error for that broker and its listing state can never resolve.
#
# STRONG signatures are full-page interstitials whose wording no listing page
# would contain. They stand on their own.
_BOT_WALL_STRONG = (
    "checking your browser before accessing",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
    "ddos protection by cloudflare",
    "sorry, you have been blocked",           # Cloudflare block page
    "you are unable to access",               # Cloudflare block page subtitle
    "pardon our interruption",                # PerimeterX / HUMAN
    "request unsuccessful. incapsula incident id",  # Imperva
    "our systems have detected unusual traffic",    # Google
    "please complete the security check to access",
    "why do i have to complete a captcha",
)

# WEAK signatures are the generic challenge widget wording -- which a real
# broker page may legitimately contain, because many broker search and opt-out
# forms embed a reCAPTCHA next to their actual content. On its own that is NOT
# a bot wall. It only counts when the page has essentially nothing else on it
# (see _MAX_CHALLENGE_TEXT_LEN): a challenge interstitial is a few hundred
# characters, a real listing page is thousands.
_BOT_WALL_WEAK = (
    "verify you are human",
    "verifying you are human",
    "i'm not a robot",
    "i am not a robot",
    "press and hold to confirm you are",
    "complete the captcha",
    "captcha challenge",
)

# Titles are matched on the whole (normalized) title, not as substrings, so a
# listing page titled "John Smith - Just a moment away from ..." cannot match.
_BOT_WALL_TITLES = (
    "just a moment...",
    "just a moment",
    "attention required! | cloudflare",
    "access denied",
    "security check",
    "one moment, please",
)

# A page shorter than this has no content to speak of; only then does a weak
# (generic CAPTCHA-widget) signature count as a bot wall.
_MAX_CHALLENGE_TEXT_LEN = 1500

# Statuses that mean the broker's edge refused us rather than answered us.
# NOT 404: a broker legitimately 404s a search URL for a name it has no
# records for, and that is a genuine "not present", not an error.
_BOT_WALL_STATUSES = (401, 403, 429)


def bot_wall_reason(text: str | None, title: str | None = None,
                    status: int | None = None) -> str | None:
    """Why this page is a bot wall rather than broker content, or None.

    Pure and side-effect free so the signature list can be tested against
    fixture page text without a browser. Returning None must stay the
    overwhelmingly common case: every false positive here converts a real,
    usable check into a permanent error for that broker.
    """
    if status in _BOT_WALL_STATUSES:
        return "bot wall: HTTP {}".format(status)
    haystack = (text or "").lower()
    normalized_title = " ".join((title or "").split()).lower()
    if normalized_title and normalized_title in _BOT_WALL_TITLES:
        return "bot wall: challenge page title {!r}".format(normalized_title)
    for needle in _BOT_WALL_STRONG:
        if needle in haystack:
            return "bot wall: page says {!r}".format(needle)
    if len(haystack.strip()) <= _MAX_CHALLENGE_TEXT_LEN:
        for needle in _BOT_WALL_WEAK:
            if needle in haystack:
                return "bot wall: near-empty page says {!r}".format(needle)
    return None


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed or no browser binary is present."""


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class PlaywrightChecker:
    """Callable taking a site-check dict and returning ``{'found': bool}``.

    Matches the ``page_action`` contract that ``run_playwright_checks``
    expects: it is handed ``{'broker_id', 'url', 'terms'}`` and returns either
    ``{'found': bool}`` or ``{'error': str}``.
    """

    def __init__(self, timeout_ms: int = 30000, headless: bool = True,
                 user_agent: str | None = None):
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self._playwright = None
        self._browser = None

    def start(self):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        return self

    def close(self):
        for obj, stop in ((self._browser, "close"), (self._playwright, "stop")):
            if obj is not None:
                try:
                    getattr(obj, stop)()
                except Exception as exc:  # pragma: no cover - teardown best effort
                    log.warning("browser teardown failed", extra={"error": str(exc)})
        self._browser = self._playwright = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def new_page(self):
        """A fresh context + page with the read-only routing already applied.

        Factored out of ``__call__`` (which still uses it) so the
        search-form driver in ``search_probe`` can reuse this exact browser
        setup -- same user agent, same ``accept_downloads=False``, same
        blocked resource types -- instead of growing a second, divergent
        copy of it.
        """
        context = self._browser.new_context(
            user_agent=self.user_agent,
            accept_downloads=False,
            java_script_enabled=True,
        )
        context.set_default_timeout(self.timeout_ms)
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                else route.continue_()
            ),
        )
        return context, page

    def __call__(self, check: dict) -> dict:
        url = check.get("url")
        # Re-validated here as well as in build_site_checks: this is the last
        # point before a URL reaches a browser, and a caller could hand-build
        # a check dict.
        if not is_safe_url(url):
            return {"error": f"refusing non-http(s) url for {check.get('broker_id')!r}"}
        if self._browser is None:
            return {"error": "browser not started"}

        context = page = None
        try:
            context, page = self.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            text = page.inner_text("body")
            # `goto` returns the main-document Response, so the status is
            # available without wiring a page.on("response") listener (which
            # would also fire for every subresource and need filtering back
            # down to the document).
            status = getattr(response, "status", None) if response is not None else None
            try:
                title = page.title()
            except Exception:  # pragma: no cover - title is best-effort
                title = ""
        except Exception as exc:
            return {"error": "{}: {}".format(type(exc).__name__, exc)}
        finally:
            for obj in (page, context):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

        # Ask "did we actually see the broker's page?" BEFORE asking "are the
        # identity terms on it?". A challenge page answers the second question
        # with a truthful "no" that means nothing at all.
        wall = bot_wall_reason(text, title, status)
        if wall:
            log.warning("bot wall on broker site", extra={
                "broker_id": check.get("broker_id"), "reason": wall,
            })
            # {"error": ...}, never {"found": False}: interpret_check_result
            # maps this to checked=False, which service.build_presence_checker
            # turns into PresenceUnknown, which run_cycle buckets under
            # `errors` -- and an errored broker is excluded from `resolved`,
            # so autopilot never calls store.forget() on it.
            return {"error": wall}

        # The title is used for bot-wall detection only and deliberately NOT
        # added to the hit haystack: widening what counts as a hit is a
        # separate decision from this one.
        candidate = {"title": "", "snippet": text or "", "url": url}
        return {"found": is_people_search_hit(candidate, check.get("terms") or [])}


def make_page_action(timeout_ms: int = 30000, headless: bool = True,
                     checker_factory=None):
    """Return (page_action, closer). Degrades to an error-reporting stub.

    The returned page_action never raises: an unavailable browser becomes an
    errored check per broker, which ``run_playwright_checks`` records and the
    orchestrator treats as "unknown" rather than "absent".

    ``checker_factory`` defaults to ``PlaywrightChecker``;
    ``search_probe.make_page_action`` passes its ``SearchChecker`` subclass
    so the "is Playwright even here?" degradation logic lives in one place
    rather than being copied per checker.
    """
    factory = checker_factory or PlaywrightChecker
    if not playwright_available():
        log.warning("playwright not installed; skipping browser checks")

        def unavailable(check: dict) -> dict:
            return {"error": "playwright not installed"}

        return unavailable, lambda: None

    checker = factory(timeout_ms=timeout_ms, headless=headless)
    try:
        checker.start()
    except Exception as exc:
        log.warning("playwright browser unavailable", extra={"error": str(exc)})
        message = "playwright browser unavailable: {}".format(exc)

        def failed(check: dict) -> dict:
            return {"error": message}

        return failed, lambda: None
    return checker, checker.close
