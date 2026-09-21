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
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            text = page.inner_text("body")
        except Exception as exc:
            return {"error": "{}: {}".format(type(exc).__name__, exc)}
        finally:
            for obj in (page, context):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

        candidate = {"title": "", "snippet": text or "", "url": url}
        return {"found": is_people_search_hit(candidate, check.get("terms") or [])}


def make_page_action(timeout_ms: int = 30000, headless: bool = True):
    """Return (page_action, closer). Degrades to an error-reporting stub.

    The returned page_action never raises: an unavailable browser becomes an
    errored check per broker, which ``run_playwright_checks`` records and the
    orchestrator treats as "unknown" rather than "absent".
    """
    if not playwright_available():
        log.warning("playwright not installed; skipping browser checks")

        def unavailable(check: dict) -> dict:
            return {"error": "playwright not installed"}

        return unavailable, lambda: None

    checker = PlaywrightChecker(timeout_ms=timeout_ms, headless=headless)
    try:
        checker.start()
    except Exception as exc:
        log.warning("playwright browser unavailable", extra={"error": str(exc)})
        message = "playwright browser unavailable: {}".format(exc)

        def failed(check: dict) -> dict:
            return {"error": message}

        return failed, lambda: None
    return checker, checker.close
