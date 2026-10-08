"""Self-hosted CAPTCHA chain: order, fallbacks, pacing, stats, and privacy.

No network, no browser: the driver, the HTTP session, sleep and clocks are
all fakes.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from broker_guard import captcha, optout_forms, optout_submit, review
from broker_guard.config import Config
from broker_guard.profile import Identity
from conftest import FAKE_EMAIL, FAKE_FIRST, FAKE_LAST
from test_optout_submit import FakeSubmitter, combo_ready


class FakeDriver:
    def __init__(self, passes_after=None, audio=b"AUDIO", tiles=None, text_img=b"IMG"):
        self.pass_state = False
        self.passes_after = passes_after or {}   # event -> becomes passed
        self.audio = audio
        self.tiles = tiles
        self.text_img = text_img
        self.log = []
        self.polls = 0

    def _event(self, name):
        self.log.append(name)
        if self.passes_after.get(name):
            self.pass_state = True

    def passed(self):
        self.polls += 1
        if self.polls == self.passes_after.get("poll_n"):
            self.pass_state = True
        return self.pass_state

    def audio_challenge(self):
        self.log.append("audio_challenge")
        return self.audio

    def submit_audio(self, text):
        self.log.append("submit_audio:" + text)
        self._event("after_audio")

    def tile_challenge(self):
        self.log.append("tile_challenge")
        return self.tiles

    def click_tiles(self, idx):
        self.log.append("click:%s" % idx)
        self._event("after_tiles")

    def text_image(self):
        self.log.append("text_image")
        return self.text_img

    def submit_text(self, text):
        self.log.append("text:" + text)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    return c


def pipeline(conn=None, whisper=None, vision=None, human=None, **kw):
    return captcha.CaptchaPipeline(
        whisper=whisper, vision=vision, human=human,
        stats=captcha.CaptchaStats(conn) if conn is not None else None,
        sleep=lambda s: None, jitter=lambda: 0, native_wait_s=2, **kw)


class FakeVision:
    def __init__(self, text="AB12", yes=(0,)):
        self.text, self.yes, self.calls = text, set(yes), 0

    def read_text(self, img):
        return self.text

    def tile_matches(self, img, target):
        self.calls += 1
        return 1.0 if img in self.yes else 0.0


def test_native_pass_short_circuits_everything_else():
    d = FakeDriver(passes_after={"poll_n": 2})
    whisper_calls = []
    res = pipeline(whisper=lambda a: whisper_calls.append(a) or "x").run(d, "b", captcha.KIND_TURNSTILE)
    assert res["ok"] and res["solver"] == "native"
    assert whisper_calls == [] and "audio_challenge" not in d.log


def test_chain_order_native_audio_vision_human_and_each_fallback():
    order = []
    d = FakeDriver(tiles={"target": "traffic lights", "tiles": [0, 1]})

    def whisper(audio):
        order.append("audio")
        return "Seven Four"

    class V(FakeVision):
        def tile_matches(self, img, target):
            order.append("vision")
            return 0.0

    def human(driver, name):
        order.append("human")
        return True

    res = pipeline(whisper=whisper, vision=V(), human=human).run(d, "b", captcha.KIND_RECAPTCHA)
    assert res["ok"] and res["solver"] == "human"
    assert [a["solver"] for a in res["attempts"]] == ["native", "audio", "vision", "human"]
    assert order[0] == "audio" and order[-1] == "human" and "vision" in order
    assert "submit_audio:seven four" in d.log


def test_audio_solver_wins_when_the_transcript_is_accepted():
    d = FakeDriver(passes_after={"after_audio": True})
    res = pipeline(whisper=lambda a: " 123 ").run(d, "b", captcha.KIND_RECAPTCHA)
    assert res["solver"] == "audio" and res["ok"]


def test_vision_grid_clicks_only_matching_tiles():
    d = FakeDriver(tiles={"target": "buses", "tiles": [0, 1, 2]}, passes_after={"after_tiles": True})
    res = pipeline(vision=FakeVision(yes=(0, 2))).run(d, "b", captcha.KIND_HCAPTCHA)
    assert res["solver"] == "vision"
    assert "click:[0, 2]" in d.log


def test_text_captcha_is_typed_and_not_run_through_native_wait():
    d = FakeDriver()
    res = pipeline(vision=FakeVision(text="Q7ZK")).run(d, "b", captcha.KIND_IMAGE_TEXT)
    assert res["ok"] and res["solver"] == "vision"
    assert "text:Q7ZK" in d.log
    assert d.polls == 0   # no pointless native wait on a plain text CAPTCHA


def test_a_crashing_solver_is_recorded_and_the_chain_continues():
    d = FakeDriver()

    def boom():
        raise RuntimeError("secret detail that must not be recorded")

    d.audio_challenge = boom
    res = pipeline(whisper=lambda a: "1", human=lambda dr, n: True).run(d, "b", captcha.KIND_RECAPTCHA)
    audio = [a for a in res["attempts"] if a["solver"] == "audio"][0]
    assert audio["ok"] is False and audio["error"] == "RuntimeError"
    assert "secret" not in str(res)
    assert res["solver"] == "human"


def test_no_human_and_no_solver_reports_failure_with_a_reason():
    res = pipeline().run(FakeDriver(), "b", captcha.KIND_TURNSTILE)
    assert res["ok"] is False and "turnstile" in res["reason"]


def test_pacing_caps_runs_per_broker_per_day(conn):
    p = pipeline(conn, daily_cap=2)
    for _ in range(2):
        p.run(FakeDriver(), "b", captcha.KIND_TURNSTILE)
    third = p.run(FakeDriver(), "b", captcha.KIND_TURNSTILE)
    assert third["ok"] is False and "pacing" in third["reason"]
    assert third["attempts"] == []
    # another broker is unaffected
    assert "pacing" not in (p.run(FakeDriver(), "other", captcha.KIND_TURNSTILE).get("reason") or "")


def test_pacing_window_expires(conn):
    now = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    stats = captcha.CaptchaStats(conn, clock=lambda: now[0])
    p = captcha.CaptchaPipeline(stats=stats, daily_cap=1, sleep=lambda s: None,
                                clock=lambda: now[0], native_wait_s=0)
    p.run(FakeDriver(), "b", captcha.KIND_TURNSTILE)
    assert "pacing" in p.run(FakeDriver(), "b", captcha.KIND_TURNSTILE)["reason"]
    now[0] += timedelta(hours=25)
    assert "pacing" not in (p.run(FakeDriver(), "b", captcha.KIND_TURNSTILE).get("reason") or "")


def test_stats_are_per_type_and_solver_and_exclude_pacing_rows(conn):
    p = pipeline(conn, whisper=lambda a: "1")
    p.run(FakeDriver(passes_after={"after_audio": True}), "b", captcha.KIND_RECAPTCHA)
    p.run(FakeDriver(), "b2", captcha.KIND_RECAPTCHA)
    rows = {(r["kind"], r["solver"]): r for r in captcha.CaptchaStats(conn).summary()}
    assert rows[("recaptcha", "audio")]["ok"] == 1 and rows[("recaptcha", "audio")]["total"] == 2
    assert rows[("recaptcha", "native")]["ok"] == 0
    assert all(k[1] != "run" for k in rows)


# --- human fallback --------------------------------------------------------------

def test_human_fallback_pushes_once_and_holds_until_solved():
    pushed, t = [], [0.0]
    d = FakeDriver()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
        if t[0] >= 30:
            d.pass_state = True

    h = captcha.HumanFallback(pushed.append, "https://novnc.example/x", hold_seconds=900,
                              poll_seconds=10, sleep=sleep, clock=lambda: t[0])
    assert h(d, "Acme") is True
    assert len(pushed) == 1
    assert pushed[0]["title"] == "Solve 1 CAPTCHA for Acme"
    assert pushed[0]["ntfy"]["click"] == "https://novnc.example/x"


def test_human_fallback_gives_up_after_the_hold_window():
    t = [0.0]

    def sleep(s):
        t[0] += s

    h = captcha.HumanFallback(lambda e: None, None, hold_seconds=900, poll_seconds=60,
                              sleep=sleep, clock=lambda: t[0])
    assert h(FakeDriver(), "Acme") is False
    assert 900 <= t[0] < 1000


def test_a_failed_push_does_not_break_the_hold():
    def bad(e):
        raise OSError("ntfy down")

    d = FakeDriver()
    d.pass_state = True
    assert captcha.HumanFallback(bad, None, sleep=lambda s: None)(d, "x") is True


# --- privacy: solvers only ever receive the CAPTCHA --------------------------------

class Rec:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))

        class R:
            status_code = 200

            def json(self_inner):
                return {"text": "42", "message": {"content": "yes"}}
        return R()


def test_whisper_client_sends_only_the_audio_bytes():
    rec = Rec()
    out = captcha.WhisperClient("http://w:8000", session=rec)(b"\x00AUDIO")
    url, kw = rec.calls[0]
    assert out == "42" and url == "http://w:8000/v1/audio/transcriptions"
    assert kw["files"]["file"][1] == b"\x00AUDIO"
    assert set(kw["data"]) == {"model", "language", "response_format"}


def test_vision_client_sends_one_image_and_a_sanitised_target_only():
    rec = Rec()
    v = captcha.VisionClient("http://o:11434", session=rec)
    assert v.tile_matches(b"TILE", "traffic <script>lights\n{{name}}") == 1.0
    url, kw = rec.calls[0]
    msg = kw["json"]["messages"][0]
    assert url == "http://o:11434/api/chat" and len(msg["images"]) == 1
    assert "<" not in msg["content"] and "{" not in msg["content"]
    assert kw["json"]["model"] == "qwen3-vl:8b"


def test_clean_target_strips_everything_but_plain_words():
    assert captcha.clean_target("Select all <b>cars</b>!") == "Select all bcarsb"
    assert len(captcha.clean_target("x" * 500)) == 60


def test_the_driver_interface_never_exposes_a_page_screenshot():
    names = {n for n in dir(captcha.PlaywrightCaptchaDriver) if not n.startswith("_")}
    assert "screenshot" not in names and "page_text" not in names


def test_kind_from_selector():
    k = captcha.kind_from_selector
    assert k("iframe[src*='recaptcha']") == "recaptcha"
    assert k(".h-captcha") == "hcaptcha"
    assert k("input[name='cf-turnstile-response']") == "turnstile"
    assert k("#captchaCode") == "image_text"


def test_build_pipeline_is_none_when_disabled_and_wired_when_enabled(conn):
    assert captcha.build_pipeline(Config()) is None
    cfg = Config(captcha_enabled=True, captcha_whisper_url="http://w", captcha_vision_url="http://o",
                 captcha_novnc_url="http://n", captcha_hold_seconds=60, captcha_max_tries=5)
    p = captcha.build_pipeline(cfg, conn=conn, notify=lambda e: None)
    assert p.whisper and p.vision and p.human.hold_seconds == 60 and p.daily_cap == 5
    off = captcha.build_pipeline(Config(captcha_enabled=True, captcha_human_enabled=False))
    assert off.human is None and off.whisper is None


def test_the_third_party_solver_path_is_gone():
    import broker_guard.config as c
    assert not hasattr(Config(), "captcha_api_key")
    assert "BG_CAPTCHA_API_KEY" not in c.SECRET_ENV_KEYS


# --- wired into the opt-out attempt --------------------------------------------------

RECIPE = optout_forms.CONSUMER_CANVAS


@pytest.fixture
def identity():
    return Identity(first_name=FAKE_FIRST, last_name=FAKE_LAST, middle_name="Q",
                    emails=[FAKE_EMAIL], addresses=["Springfield, IL"])


@pytest.fixture
def cfg(tmp_path):
    return Config(review_dir=str(tmp_path / "review"), optout_submit_enabled=True,
                  optout_submit_dry_run=True)


class StubPipeline:
    native_wait_s = 0

    def __init__(self, ok, reason=None):
        self.ok, self.reason, self.calls = ok, reason, []

    def run(self, driver, broker_id, kind, broker_name=None):
        self.calls.append((broker_id, kind, type(driver).__name__))
        return {"ok": self.ok, "kind": kind, "solver": "vision" if self.ok else None,
                "attempts": [{"solver": "vision", "ok": self.ok}], "reason": self.reason}


def test_a_solved_captcha_lets_the_attempt_continue_to_dry_run(identity, cfg):
    page = combo_ready(present={"#captchaCode"})
    stub = StubPipeline(True)
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page),
                                        captcha=stub)
    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert saved["captcha"]["solver"] == "vision" and saved["captcha"]["kind"] == "image_text"
    assert stub.calls == [(RECIPE.broker_id, "image_text", "PlaywrightCaptchaDriver")]
    assert page.submitted is False


def test_a_solved_captcha_in_a_live_run_presses_submit(identity, tmp_path):
    live = Config(review_dir=str(tmp_path / "r"), optout_submit_enabled=True,
                  optout_submit_dry_run=False)
    page = combo_ready(present={"#captchaCode"}, result_body="Thank you, request received")
    saved = optout_submit.submit_optout(RECIPE, identity, live, submitter=FakeSubmitter(page),
                                        captcha=StubPipeline(True))
    assert page.submitted is True
    assert saved["captcha"]["ok"] is True


def test_an_unsolved_captcha_still_stops_and_says_why(identity, cfg):
    page = combo_ready(present={"#captchaCode"})
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page),
                                        captcha=StubPipeline(False, "no solver passed"))
    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert saved["manual_action_source"] == "captcha_fallback"
    assert "self-hosted solvers did not clear it" in saved["reason"]
    assert saved["captcha"]["ok"] is False
    assert page.submitted is False


def test_without_a_pipeline_the_old_stop_behaviour_is_unchanged(identity, cfg):
    page = combo_ready(present={"#captchaCode"})
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page))
    assert saved["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert "captcha" not in saved


def test_the_pipeline_never_sees_a_filled_page_object_as_data(identity, cfg):
    """The solver gets a driver (widget bytes only); PII lives in the page."""
    page = combo_ready(present={"#captchaCode"})
    seen = []

    class Spy(StubPipeline):
        def run(self, driver, *a, **kw):
            seen.append(driver)
            return super().run(driver, *a, **kw)

    optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page), captcha=Spy(False))
    drv = seen[0]
    # whatever the driver can hand a solver is bytes/str of the widget, not form data
    assert not hasattr(drv, "form_values") and not hasattr(drv, "screenshot")


def test_a_whole_page_interstitial_gets_a_native_wait_before_giving_up(identity, cfg):
    page = combo_ready(body="Checking your browser before accessing", title="Just a moment...")
    cleared = []

    def wait(ms):
        cleared.append(ms)
        if len(cleared) >= 2:
            page.body, page._title = "Privacy Rights Portal", "Consumer Canvas"

    page.wait_for_timeout = wait
    stub = StubPipeline(True)
    stub.native_wait_s = 10
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page), captcha=stub)
    assert saved.get("detected") != "bot_wall"
    assert saved["outcome"] == review.OUTCOME_DRY_RUN


def test_an_interstitial_that_never_clears_still_stops(identity, cfg):
    page = combo_ready(body="Checking your browser before accessing", title="Just a moment...")
    stub = StubPipeline(True)
    stub.native_wait_s = 5
    saved = optout_submit.submit_optout(RECIPE, identity, cfg, submitter=FakeSubmitter(page), captcha=stub)
    assert saved["detected"] == "bot_wall"
    assert page.filled == {}


# --- Playwright driver against fake frames ----------------------------------------------

class El:
    def __init__(self, value="", href=None, tag="IMG", shot=b"S"):
        self.value, self.href, self.tag, self.shot = value, href, tag, shot
        self.clicked, self.filled = 0, None

    def input_value(self): return self.value
    def get_attribute(self, n): return self.href if n in ("href", "src") else None
    def click(self): self.clicked += 1
    def fill(self, v): self.filled = v
    def screenshot(self): return self.shot
    def evaluate(self, js): return self.tag
    def inner_text(self): return "traffic lights"


class Frame:
    def __init__(self, els, many=None):
        self.els, self.many = els, many or {}

    def query_selector(self, sel):
        for k, v in self.els.items():
            if k in sel:
                return v

    def query_selector_all(self, sel):
        for k, v in self.many.items():
            if k in sel:
                return v
        return []


class Handle:
    def __init__(self, frame): self.frame = frame
    def content_frame(self): return self.frame


class FakeWebPage:
    def __init__(self, handles=None, els=None):
        self.handles, self.els = handles or {}, els or {}
        self.context = type("C", (), {"request": type("R", (), {"get": staticmethod(
            lambda url: type("Resp", (), {"body": staticmethod(lambda: b"AUDIOBYTES")})())})()})()

    def query_selector(self, sel):
        for k, v in self.handles.items():
            if k in sel:
                return Handle(v)
        for k, v in self.els.items():
            if k in sel:
                return v

    def wait_for_timeout(self, ms): pass


def test_driver_reports_passed_from_the_token_field_or_checkbox():
    page = FakeWebPage(els={"g-recaptcha-response": El(value="TOKEN")})
    assert captcha.PlaywrightCaptchaDriver(page).passed() is True
    empty = FakeWebPage(els={"g-recaptcha-response": El(value="")})
    assert captcha.PlaywrightCaptchaDriver(empty).passed() is False
    checked = FakeWebPage(handles={"anchor": Frame({"recaptcha-checkbox-checked": El()})})
    assert captcha.PlaywrightCaptchaDriver(checked).passed() is True


def test_driver_fetches_audio_and_submits_the_answer():
    b = Frame({"audio-button": El(), "tdownload": El(href="http://g/a.mp3"),
               "audio-response": El(), "verify-button": El()})
    anchor = Frame({"recaptcha-anchor": El()})
    page = FakeWebPage(handles={"anchor": anchor, "bframe": b})
    d = captcha.PlaywrightCaptchaDriver(page)
    assert d.audio_challenge() == b"AUDIOBYTES"
    d.submit_audio("seven")
    assert b.els["audio-response"].filled == "seven"
    assert b.els["verify-button"].clicked == 1


def test_driver_returns_tile_images_and_clicks_chosen_tiles():
    tiles = [El(shot=b"t0"), El(shot=b"t1"), El(shot=b"t2")]
    b = Frame({"imageselect-desc": El(), "verify-button": El()}, many={"imageselect-tile": tiles})
    page = FakeWebPage(handles={"anchor": Frame({"recaptcha-anchor": El()}), "bframe": b})
    d = captcha.PlaywrightCaptchaDriver(page)
    ch = d.tile_challenge()
    assert ch == {"target": "traffic lights", "tiles": [b"t0", b"t1", b"t2"]}
    d.click_tiles([1, 7])
    assert [t.clicked for t in tiles] == [0, 1, 0]


def test_driver_text_captcha_screenshot_is_the_image_element_only():
    page = FakeWebPage(els={"img[src*": El(shot=b"CAPIMG"), "input[name": El(tag="INPUT")})
    d = captcha.PlaywrightCaptchaDriver(page)
    assert d.text_image() == b"CAPIMG"
    d.submit_text("AB12")
    assert page.els["input[name"].filled == "AB12"


# --- settings + dashboard ------------------------------------------------------------

def test_captcha_settings_are_ui_editable_and_the_old_key_is_gone():
    from broker_guard import settings as settings_mod
    for key in ("captcha_enabled", "captcha_human_enabled", "captcha_whisper_url",
                "captcha_vision_url", "captcha_novnc_url"):
        assert key in settings_mod.SPEC_BY_KEY
    assert "captcha_api_key" not in settings_mod.SPEC_BY_KEY
    assert settings_mod.SPEC_BY_KEY["ntfy_token"].secret is True


def test_optouts_page_shows_solver_stats(tmp_path):
    from fastapi.testclient import TestClient

    from broker_guard import state, webui
    cfg = Config(state_path=str(tmp_path / "s.sqlite"), brokers_path=str(tmp_path / "b.json"),
                 profile_path=str(tmp_path / "p.json"), review_dir=str(tmp_path / "r"),
                 settings_path=str(tmp_path / "set.json"))
    conn = state.init_db(cfg.state_path)
    stats = captcha.CaptchaStats(conn)
    stats.record("b", "recaptcha", "audio", True)
    stats.record("b", "recaptcha", "audio", False)
    conn.close()
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    try:
        text = TestClient(webui.app).get("/optouts").text
    finally:
        webui.app.dependency_overrides.clear()
    assert "CAPTCHA solvers" in text and "1 / 2" in text and "50%" in text
