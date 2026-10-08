"""browser_launch: one launcher, native UA, headful/headless fallback, profiles."""
import importlib.util
import os
import sys

import pytest

from broker_guard import browser_launch as bl
from broker_guard.browser import PlaywrightChecker
from broker_guard.config import Config
from broker_guard.optout_submit import OptOutSubmitter


class FakeContext:
    def __init__(self, kw):
        self.kw, self.closed, self.timeout = kw, False, None

    def set_default_timeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False

    def new_context(self, **kw):
        c = FakeContext(kw)
        self.contexts.append(c)
        return c

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, fail_persistent=False):
        self.launch_kw = self.persist_kw = self.persist_path = None
        self.browser = FakeBrowser()
        self.persist_ctx = None
        self.fail_persistent = fail_persistent

    def launch(self, **kw):
        self.launch_kw = kw
        return self.browser

    def launch_persistent_context(self, path, **kw):
        if self.fail_persistent:
            raise RuntimeError("profile locked")
        self.persist_path, self.persist_kw = path, kw
        self.persist_ctx = FakeContext(kw)
        return self.persist_ctx


class FakePW:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


def factory(chromium):
    pw = FakePW(chromium)

    class _Mgr:
        def start(self_inner):
            return pw
    return lambda: _Mgr()


def test_native_ua_is_never_forced():
    ch = FakeChromium()
    s = bl.BrowserSession(headless=True, sync_playwright=factory(ch)).start()
    ctx = s.new_context(timeout_ms=5)
    assert "user_agent" not in ch.browser.contexts[0].kw
    assert ctx.timeout == 5


def test_explicit_ua_is_forwarded():
    ch = FakeChromium()
    s = bl.BrowserSession(headless=True, user_agent="X/1", sync_playwright=factory(ch)).start()
    s.new_context()
    assert ch.browser.contexts[0].kw["user_agent"] == "X/1"


def test_headful_without_display_degrades_to_headless():
    assert bl.effective_headless(False, env={}, platform="linux") is True
    assert bl.effective_headless(False, env={"DISPLAY": ":99"}, platform="linux") is False
    assert bl.effective_headless(False, env={}, platform="darwin") is False
    assert bl.effective_headless(True, env={}, platform="linux") is True


def test_persistent_profile_per_leg_and_shared_close_is_noop(tmp_path):
    ch = FakeChromium()
    s = bl.BrowserSession(headless=True, profile_dir=str(tmp_path), leg="submit",
                          sync_playwright=factory(ch)).start()
    assert ch.persist_path == os.path.join(str(tmp_path), "submit")
    ctx = s.new_context()
    ctx.close()
    assert ch.persist_ctx.closed is False  # per-attempt close must not kill the profile
    s.close()
    assert ch.persist_ctx.closed is True


def test_locked_profile_falls_back_to_ephemeral(tmp_path):
    ch = FakeChromium(fail_persistent=True)
    s = bl.BrowserSession(headless=True, profile_dir=str(tmp_path),
                          sync_playwright=factory(ch)).start()
    assert s.persistent is None and s.browser is ch.browser
    s.new_context().close()
    assert ch.browser.contexts[0].closed is True


def test_stealth_flavour_falls_back_to_stock_when_missing():
    import playwright.sync_api as stock
    assert bl._import_sync_playwright("patchright") is stock.sync_playwright
    assert bl._import_sync_playwright("") is stock.sync_playwright


def test_stealth_flavour_imported_when_present(monkeypatch):
    import types
    mod = types.ModuleType("patchright.sync_api")
    mod.sync_playwright = lambda: "patched"
    monkeypatch.setitem(sys.modules, "patchright", types.ModuleType("patchright"))
    monkeypatch.setitem(sys.modules, "patchright.sync_api", mod)
    assert bl._import_sync_playwright("patchright")() == "patched"


def test_checker_and_submitter_use_launcher_without_hardcoded_ua(monkeypatch):
    seen = []

    class Rec(bl.BrowserSession):
        def start(self):
            seen.append((self.leg, self.user_agent, self.profile_dir, self.stealth))
            return self

    monkeypatch.setattr(bl, "BrowserSession", Rec)
    cfg = Config(browser_stealth="patchright", browser_profile_dir="/p", playwright_headless=False)
    PlaywrightChecker.from_config(cfg).start()
    OptOutSubmitter.from_config(cfg).start()
    assert seen == [("checker", None, "/p", "patchright"), ("submit", None, "/p", "patchright")]


def test_no_hardcoded_user_agent_in_source():
    root = os.path.dirname(bl.__file__)
    for name in ("browser.py", "optout_submit.py", "browser_launch.py"):
        text = open(os.path.join(root, name)).read()
        assert "Mozilla/5.0" not in text and "Chrome/124" not in text, name


def test_config_env_parsing():
    from broker_guard.config import load_config
    cfg = load_config({"BG_BROWSER_STEALTH": " Patchright ", "BG_BROWSER_PROFILE_DIR": "/d"})
    assert cfg.browser_stealth == "patchright" and cfg.browser_profile_dir == "/d"
    assert load_config({}).browser_user_agent is None


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "reprobe_blocked", os.path.join(os.path.dirname(bl.__file__), "..", "tools", "reprobe_blocked.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reprobe_classifies_open_walled_and_errors():
    tool = _load_tool()

    class Resp:
        def __init__(self, s): self.status = s

    class Page:
        def __init__(self, spec): self.spec = spec
        def goto(self, url, **kw):
            if self.spec == "boom":
                raise TimeoutError()
            return Resp(self.spec[2])
        def wait_for_timeout(self, ms): pass
        def inner_text(self, sel): return self.spec[0]
        def title(self): return self.spec[1]
        def close(self): pass

    class Ctx:
        def __init__(self, spec): self.spec = spec
        def new_page(self): return Page(self.spec)
        def close(self): pass

    specs = {"http://a": ("Opt out of our service. Enter your name and email below " * 5, "Opt out", 200),
             "http://b": ("x", "t", 403), "http://c": "boom"}

    class Sess:
        def new_context(self, timeout_ms=None):
            return Ctx(specs[self.url])

    sess = Sess()
    urls = {"a": "http://a", "b": "http://b", "c": "http://c"}

    class Tracking(Sess):
        pass

    # route each id's url into the fake session
    class S2:
        def __init__(self): self.i = 0
        def new_context(self, timeout_ms=None):
            url = list(urls.values())[self.i]; self.i += 1
            return Ctx(specs[url])

    out = tool.reprobe(["a", "b", "c", "d"], urls, S2())
    assert out["open"] == ["a"]
    assert out["still_walled"] == {"b": "bot wall: HTTP 403"}
    assert out["errors"] == {"c": "TimeoutError"}
    assert out["no_url"] == ["d"]
