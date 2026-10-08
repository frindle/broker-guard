"""The one place a Chromium is launched, so every browser leg looks the same.

Why this exists
---------------
``browser.PlaywrightChecker`` and ``optout_submit.OptOutSubmitter`` each
carried their own copy of "start Playwright, launch headless Chromium, force a
hard-coded Linux user agent". That is the most fingerprintable
browser possible: headless, a stale UA string that does not match the real
build (so ``navigator.userAgentData`` and the UA disagree), a throwaway empty
profile every time. 83 Cloudflare and 10 PerimeterX walls in the mapping were
recorded under exactly that fingerprint, and some of them (versium, visitiq)
were fingerprint false positives rather than real walls.

What a launch now is
--------------------
* **Native user agent.** ``user_agent`` is passed to Playwright ONLY when an
  operator explicitly sets one. By default the browser reports itself.
* **Headful under Xvfb.** ``headless=False`` is the default in the image
  (docker-entrypoint.sh starts Xvfb on ``:99``). Where no display exists
  (a Linux box without ``DISPLAY``) the launch falls back to headless with a
  warning instead of crashing.
* **Persistent profile.** With ``profile_dir`` set (``/data/browser-profile``)
  each leg gets its own sub-profile (cookies, consent state, localStorage
  persist between runs, as a real person's browser would). A profile that is
  locked by another process falls back to an ephemeral launch.
* **Optional stealth build.** ``stealth`` = ``patchright`` or ``rebrowser``
  swaps the Playwright import for the open-source patched fork; if it is not
  installed the launch falls back to stock Playwright and says so. It is OFF
  by default.
* **Home IP.** Nothing here proxies: egress is the host's residential address.

``new_context()`` always returns something whose ``close()`` is safe to call
per attempt: for a persistent profile it is the shared context with ``close``
neutralised (closing it would kill the browser and the profile lock).
"""
import logging
import os
import sys

log = logging.getLogger("broker_guard.browser_launch")

STEALTH_FLAVORS = ("", "patchright", "rebrowser")

_STEALTH_MODULES = {
    "patchright": "patchright.sync_api",
    "rebrowser": "rebrowser_playwright.sync_api",
}


def _import_sync_playwright(stealth: str = ""):
    """``sync_playwright`` from the requested flavour, else stock Playwright."""
    import importlib

    module = _STEALTH_MODULES.get(stealth or "")
    if module:
        try:
            return importlib.import_module(module).sync_playwright
        except ImportError:
            log.warning("stealth browser %r requested but not installed; "
                        "using stock Playwright", stealth)
    from playwright.sync_api import sync_playwright

    return sync_playwright


def effective_headless(headless: bool, env=None, platform: str | None = None) -> bool:
    """A headful request on a Linux box with no ``DISPLAY`` degrades to headless."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if not headless and platform.startswith("linux") and not env.get("DISPLAY"):
        log.warning("headful browser requested but DISPLAY is unset; running headless")
        return True
    return headless


class _SharedContext:
    """Wraps a persistent context so per-attempt ``close()`` is a no-op."""

    def __init__(self, context):
        self._context = context

    def close(self):  # the shared profile outlives any single attempt
        pass

    def __getattr__(self, name):
        return getattr(self._context, name)


class BrowserSession:
    """A started Playwright + Chromium (ephemeral or persistent profile)."""

    def __init__(self, headless: bool = False, stealth: str = "",
                 profile_dir: str | None = None, leg: str = "default",
                 user_agent: str | None = None, sync_playwright=None):
        self.headless = headless
        self.stealth = stealth or ""
        self.profile_dir = profile_dir
        self.leg = leg
        self.user_agent = user_agent or None
        self._sync_playwright = sync_playwright
        self._playwright = None
        self.browser = None          # set for an ephemeral launch
        self.persistent = None       # set for a persistent-profile launch

    @property
    def launched(self) -> bool:
        return self.browser is not None or self.persistent is not None

    def _args(self) -> list:
        return ["--disable-blink-features=AutomationControlled"] if self.stealth else []

    def start(self):
        sync_playwright = self._sync_playwright or _import_sync_playwright(self.stealth)
        self._playwright = sync_playwright().start()
        headless = effective_headless(self.headless)
        chromium = self._playwright.chromium
        if self.profile_dir:
            path = os.path.join(self.profile_dir, self.leg)
            try:
                os.makedirs(path, exist_ok=True)
                try:
                    os.chmod(self.profile_dir, 0o700)
                except OSError:
                    pass
                kwargs = dict(headless=headless, args=self._args(),
                              accept_downloads=False, java_script_enabled=True)
                if self.user_agent:
                    kwargs["user_agent"] = self.user_agent
                self.persistent = chromium.launch_persistent_context(path, **kwargs)
                return self
            except Exception as exc:
                log.warning("persistent browser profile unavailable (%s); "
                            "using an ephemeral one", type(exc).__name__)
        self.browser = chromium.launch(headless=headless, args=self._args())
        return self

    def new_context(self, timeout_ms: int | None = None, **kwargs):
        """A context for one attempt. ``user_agent`` is only forwarded when set."""
        if self.user_agent and "user_agent" not in kwargs:
            kwargs["user_agent"] = self.user_agent
        if self.persistent is not None:
            context = _SharedContext(self.persistent)
        else:
            kwargs.setdefault("accept_downloads", False)
            kwargs.setdefault("java_script_enabled", True)
            context = self.browser.new_context(**kwargs)
        if timeout_ms:
            context.set_default_timeout(timeout_ms)
        return context

    def close(self):
        for obj, stop in ((self.persistent, "close"), (self.browser, "close"),
                          (self._playwright, "stop")):
            if obj is not None:
                try:
                    getattr(obj, stop)()
                except Exception as exc:  # pragma: no cover - teardown best effort
                    log.warning("browser teardown failed",
                                extra={"error": type(exc).__name__})
        self.persistent = self.browser = self._playwright = None


def session_from_config(cfg, leg: str, headless: bool | None = None) -> BrowserSession:
    """Build (not start) a session from ``Config`` for the given leg."""
    return BrowserSession(
        headless=cfg.playwright_headless if headless is None else headless,
        stealth=getattr(cfg, "browser_stealth", "") or "",
        profile_dir=getattr(cfg, "browser_profile_dir", None) or None,
        leg=leg,
        user_agent=getattr(cfg, "browser_user_agent", None) or None,
    )
