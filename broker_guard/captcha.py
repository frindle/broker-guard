"""Self-hosted CAPTCHA pipeline.

Policy (changed 2026-10-08, was "never solve"): a bot check on an opt-out form
is attempted by a chain of SELF-HOSTED solvers, in order, and only then handed
to Penn:

1. **native**   -- wait a few seconds. With a real headful browser on the home
                   IP, Turnstile and reCAPTCHA v3 very often pass by themselves.
2. **audio**    -- reCAPTCHA's audio challenge, transcribed by a faster-whisper
                   container on Unraid (``BG_CAPTCHA_WHISPER_URL``).
3. **vision**   -- image-grid tiles and distorted-text images, read by
                   ``qwen3-vl:8b`` on the Unraid Ollama (``BG_CAPTCHA_VISION_URL``).
4. **human**    -- an ntfy push ("solve 1 CAPTCHA for <broker>") with a link to
                   the live browser over noVNC. The page is held up to
                   ``BG_CAPTCHA_HOLD_SECONDS`` (15 min) and the submit continues
                   the moment it is solved.

What the solvers may see
------------------------
ONLY the CAPTCHA's own image or audio (and the challenge's one-line target
text, e.g. "traffic lights"). Never the filled form, never a screenshot of the
page, never profile data, and nothing leaves the LAN: no third-party solving
service exists in this module (``BG_CAPTCHA_API_KEY`` was removed). The
``CaptchaDriver`` interface is what enforces that: it hands this module bytes
of the widget, not the page.

Everything is injected (HTTP session, sleep, clock, driver) so the suite runs
the whole chain against fakes with no network or browser.
"""
import base64
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

try:  # requests is a hard dependency; the guard keeps import cheap in tests
    import requests
except ImportError:  # pragma: no cover
    requests = None

log = logging.getLogger("broker_guard.captcha")

KIND_RECAPTCHA = "recaptcha"
KIND_HCAPTCHA = "hcaptcha"
KIND_TURNSTILE = "turnstile"
KIND_IMAGE_TEXT = "image_text"
KIND_UNKNOWN = "unknown"

SOLVER_NATIVE = "native"
SOLVER_AUDIO = "audio"
SOLVER_VISION = "vision"
SOLVER_HUMAN = "human"
SOLVER_ORDER = (SOLVER_NATIVE, SOLVER_AUDIO, SOLVER_VISION, SOLVER_HUMAN)

_RUN_MARK = "run"  # stats rows with this solver count pipeline runs (pacing)


def kind_from_selector(selector: str | None) -> str:
    """Which CAPTCHA family a ``detect_captcha`` selector belongs to."""
    s = (selector or "").lower()
    if "turnstile" in s or "cf-" in s:
        return KIND_TURNSTILE
    if "hcaptcha" in s or "h-captcha" in s:
        return KIND_HCAPTCHA
    if "recaptcha" in s:
        return KIND_RECAPTCHA
    if "img" in s or "input" in s or "captcha" in s:
        return KIND_IMAGE_TEXT
    return KIND_UNKNOWN


# --- legacy generic helpers (kept: the chain/grid contracts are still used) ---

def solve(challenge: dict, providers: list) -> dict:
    """Try each provider callable in order; first ``ok`` result wins."""
    attempts = []
    for provider in providers or []:
        name = getattr(provider, "__name__", "unknown")
        try:
            result = provider(challenge)
        except Exception as exc:
            attempts.append({"ok": False, "provider": name, "error": type(exc).__name__})
            continue
        if not isinstance(result, dict):
            # A provider returning a bare token/None used to raise
            # AttributeError and abort the whole fallback chain.
            attempts.append({"ok": False, "provider": name, "error": "provider returned a non-dict result"})
            continue
        if not result.get("ok"):
            attempts.append(dict(result))
            continue
        out = dict(result)
        out["attempts"] = attempts
        return out
    return {"ok": False, "token": None, "fallback": "manual_queue", "attempts": attempts}


def solve_audio(audio, transcribe):
    try:
        transcript = transcribe(audio)
    except Exception:
        return {'ok': False, 'provider': 'whisper', 'token': None}
    if not isinstance(transcript, str):
        return {'ok': False, 'provider': 'whisper', 'token': None}
    token = transcript.strip().lower()
    if not token:
        return {'ok': False, 'provider': 'whisper', 'token': None}
    return {'ok': True, 'provider': 'whisper', 'token': token}


def solve_grid(challenge: dict, classify) -> dict:
    tiles = challenge.get('tiles') or []
    target = challenge.get('target', '')
    threshold = challenge.get('threshold', 0.5)
    selection = []
    max_confidence = 0.0
    for index, tile in enumerate(tiles):
        try:
            confidence = float(classify(tile, target))
        except Exception:
            confidence = 0.0
        if confidence > max_confidence:
            max_confidence = confidence
        if confidence >= threshold:
            selection.append(index)
    return {'ok': bool(selection), 'provider': 'vision', 'selection': selection,
            'low_confidence': max_confidence < 0.8}


# --- self-hosted solver clients ------------------------------------------------

_THINK = re.compile(r"<think>.*?</think>", re.S)
_SAFE_TARGET = re.compile(r"[^A-Za-z0-9 ,'-]")


def clean_target(text) -> str:
    """The challenge's one-line target, stripped to harmless characters."""
    return _SAFE_TARGET.sub("", str(text or ""))[:60].strip()


class WhisperClient:
    """faster-whisper behind an OpenAI-compatible ``/v1/audio/transcriptions``.

    Receives audio bytes and nothing else.
    """

    def __init__(self, url: str, model: str = "Systran/faster-whisper-small",
                 timeout_s: int = 60, session=None):
        self.url = (url or "").rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.session = session or (requests.Session() if requests else None)

    def __call__(self, audio: bytes) -> str:
        if not self.url or self.session is None:
            raise RuntimeError("whisper not configured")
        response = self.session.post(
            self.url + "/v1/audio/transcriptions",
            files={"file": ("challenge.mp3", audio, "audio/mpeg")},
            data={"model": self.model, "language": "en", "response_format": "json"},
            timeout=self.timeout_s)
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("whisper HTTP %s" % response.status_code)
        return str(response.json().get("text") or "")


class VisionClient:
    """qwen3-vl on Ollama. Receives one CAPTCHA image (or tile) per call."""

    def __init__(self, url: str, model: str = "qwen3-vl:8b", timeout_s: int = 120,
                 session=None):
        self.url = (url or "").rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.session = session or (requests.Session() if requests else None)

    def _ask(self, prompt: str, image: bytes) -> str:
        if not self.url or self.session is None:
            raise RuntimeError("vision model not configured")
        response = self.session.post(
            self.url + "/api/chat",
            json={"model": self.model, "stream": False, "think": False,
                  "options": {"temperature": 0},
                  "messages": [{"role": "user", "content": prompt,
                                "images": [base64.b64encode(image).decode("ascii")]}]},
            timeout=self.timeout_s)
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("vision HTTP %s" % response.status_code)
        content = (response.json().get("message") or {}).get("content") or ""
        return _THINK.sub("", content).strip()

    def read_text(self, image: bytes) -> str:
        """The characters in a text-CAPTCHA image, alphanumerics only."""
        raw = self._ask(
            "This image is a distorted-text CAPTCHA. Reply with ONLY the "
            "characters shown, no spaces, no explanation.", image)
        return re.sub(r"[^A-Za-z0-9]", "", raw)

    def tile_matches(self, image: bytes, target: str) -> float:
        """1.0 when the tile shows *target*, else 0.0."""
        target = clean_target(target) or "the requested object"
        raw = self._ask(
            "Does this image contain %s? Answer with exactly one word: yes or no."
            % target, image).lower()
        return 1.0 if raw.startswith("yes") else 0.0


# --- stats ------------------------------------------------------------------------

def init_tables(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS captcha_stats ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, broker_id TEXT, "
        "kind TEXT NOT NULL, solver TEXT NOT NULL, ok INTEGER NOT NULL)")
    conn.commit()


class CaptchaStats:
    """Per-type, per-solver success counts, plus per-broker run pacing."""

    def __init__(self, conn, clock=None):
        self.conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        init_tables(conn)

    def record(self, broker_id, kind, solver, ok) -> None:
        self.conn.execute(
            "INSERT INTO captcha_stats (ts, broker_id, kind, solver, ok) VALUES (?,?,?,?,?)",
            (self._clock().isoformat(), broker_id, kind, solver, 1 if ok else 0))
        self.conn.commit()

    def summary(self) -> list:
        """[{kind, solver, ok, total, rate}] excluding the pacing marker rows."""
        rows = self.conn.execute(
            "SELECT kind, solver, SUM(ok), COUNT(*) FROM captcha_stats "
            "WHERE solver != ? GROUP BY kind, solver ORDER BY kind, solver",
            (_RUN_MARK,)).fetchall()
        return [{"kind": k, "solver": s, "ok": int(o or 0), "total": int(t),
                 "rate": (float(o or 0) / t) if t else 0.0} for k, s, o, t in rows]

    def runs_since(self, broker_id, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM captcha_stats WHERE broker_id=? AND solver=? AND ts>=?",
            (broker_id, _RUN_MARK, since.isoformat())).fetchone()
        return int(row[0])


# --- the chain ------------------------------------------------------------------

class HumanFallback:
    """Push "solve 1 CAPTCHA" and hold the live page until it is solved."""

    def __init__(self, notify, novnc_url: str | None, hold_seconds: int = 900,
                 poll_seconds: float = 3.0, sleep=time.sleep, clock=time.monotonic):
        self.notify = notify
        self.novnc_url = novnc_url
        self.hold_seconds = hold_seconds
        self.poll_seconds = poll_seconds
        self._sleep = sleep
        self._clock = clock

    def __call__(self, driver, broker_name: str) -> bool:
        if self.notify is not None:
            event = {
                "title": "Solve 1 CAPTCHA for %s" % broker_name,
                "message": "A broker form is waiting on a CAPTCHA. Open the live "
                           "browser and solve it; broker-guard continues by itself. "
                           "Held for %d min." % max(1, self.hold_seconds // 60),
                "ntfy": {"priority": 5, "tags": ["robot"],
                         **({"click": self.novnc_url} if self.novnc_url else {})},
            }
            try:
                self.notify(event)
            except Exception as exc:
                log.warning("captcha push failed", extra={"error": type(exc).__name__})
        deadline = self._clock() + self.hold_seconds
        while self._clock() < deadline:
            try:
                if driver.passed():
                    return True
            except Exception:
                pass
            self._sleep(self.poll_seconds)
        return False


class CaptchaPipeline:
    """native -> audio -> vision -> human, with pacing and stats."""

    def __init__(self, whisper=None, vision=None, human=None, stats=None,
                 tries_per_solver: int = 2, daily_cap: int = 3,
                 native_wait_s: float = 8.0, sleep=time.sleep, jitter=None,
                 clock=None):
        self.whisper = whisper
        self.vision = vision
        self.human = human
        self.stats = stats
        self.tries = max(1, tries_per_solver)
        self.daily_cap = daily_cap
        self.native_wait_s = native_wait_s
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(2.0, 5.0))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _record(self, broker_id, kind, solver, ok):
        if self.stats is not None:
            try:
                self.stats.record(broker_id, kind, solver, ok)
            except Exception as exc:  # stats must never break a solve
                log.warning("captcha stats write failed", extra={"error": type(exc).__name__})

    def _step(self, attempts, broker_id, kind, solver, func):
        try:
            ok = bool(func())
            error = None
        except Exception as exc:
            ok, error = False, type(exc).__name__
        attempts.append({"solver": solver, "ok": ok, **({"error": error} if error else {})})
        self._record(broker_id, kind, solver, ok)
        return ok

    def _native(self, driver, kind):
        waited = 0.0
        while True:
            if driver.passed():
                return True
            if waited >= self.native_wait_s:
                return False
            self._sleep(1.0)
            waited += 1.0

    def _audio(self, driver):
        if self.whisper is None:
            return False
        for attempt in range(self.tries):
            if attempt:
                self._sleep(self._jitter())
            audio = driver.audio_challenge()
            if not audio:
                return False  # Google's "try again later" -- more tries only dig deeper
            driver.submit_audio(solve_audio(audio, self.whisper).get("token") or "")
            if driver.passed():
                return True
        return False

    def _vision(self, driver, kind):
        if self.vision is None:
            return False
        for attempt in range(self.tries):
            if attempt:
                self._sleep(self._jitter())
            if kind == KIND_IMAGE_TEXT:
                image = driver.text_image()
                if not image:
                    return False
                text = self.vision.read_text(image)
                if not text:
                    continue
                driver.submit_text(text)
                return True  # cannot be verified here; the real submit confirms
            challenge = driver.tile_challenge()
            if not challenge:
                return False
            graded = solve_grid(
                {"tiles": challenge.get("tiles") or [],
                 "target": clean_target(challenge.get("target"))},
                self.vision.tile_matches)
            if graded["ok"]:
                driver.click_tiles(graded["selection"])
            if driver.passed():
                return True
        return False

    def run(self, driver, broker_id: str, kind: str, broker_name: str | None = None) -> dict:
        """Solve (or hand off) one CAPTCHA. Always returns, never raises."""
        attempts = []
        result = {"ok": False, "kind": kind, "solver": None, "attempts": attempts}
        if self.stats is not None and self.daily_cap:
            since = self._clock() - timedelta(hours=24)
            if self.stats.runs_since(broker_id, since) >= self.daily_cap:
                result["reason"] = ("CAPTCHA pacing: %d attempts in the last 24h for this broker; "
                                    "not trying again until tomorrow" % self.daily_cap)
                return result
        self._record(broker_id, kind, _RUN_MARK, True)

        chain = []
        if kind != KIND_IMAGE_TEXT:
            chain.append((SOLVER_NATIVE, lambda: self._native(driver, kind)))
        if kind == KIND_RECAPTCHA:
            chain.append((SOLVER_AUDIO, lambda: self._audio(driver)))
        if kind in (KIND_RECAPTCHA, KIND_HCAPTCHA, KIND_IMAGE_TEXT):
            chain.append((SOLVER_VISION, lambda: self._vision(driver, kind)))
        if self.human is not None:
            chain.append((SOLVER_HUMAN, lambda: self.human(driver, broker_name or broker_id)))

        for solver, func in chain:
            if self._step(attempts, broker_id, kind, solver, func):
                result.update(ok=True, solver=solver)
                return result
        result["reason"] = "no solver passed the %s check (%s)" % (
            kind, ", ".join(a["solver"] for a in attempts) or "none enabled")
        return result


def build_pipeline(cfg, conn=None, notify=None, session=None):
    """A ``CaptchaPipeline`` from config, or ``None`` when solving is off."""
    if not getattr(cfg, "captcha_enabled", False):
        return None
    whisper = vision = None
    if getattr(cfg, "captcha_whisper_url", None):
        whisper = WhisperClient(cfg.captcha_whisper_url,
                                model=getattr(cfg, "captcha_whisper_model", None)
                                or "Systran/faster-whisper-small", session=session)
    if getattr(cfg, "captcha_vision_url", None):
        vision = VisionClient(cfg.captcha_vision_url,
                              model=getattr(cfg, "captcha_vision_model", None) or "qwen3-vl:8b",
                              session=session)
    human = None
    if getattr(cfg, "captcha_human_enabled", True):
        human = HumanFallback(notify, getattr(cfg, "captcha_novnc_url", None),
                              hold_seconds=int(getattr(cfg, "captcha_hold_seconds", 900)))
    stats = CaptchaStats(conn) if conn is not None else None
    return CaptchaPipeline(whisper=whisper, vision=vision, human=human, stats=stats,
                           daily_cap=int(getattr(cfg, "captcha_max_tries", 3)))


# --- Playwright driver ------------------------------------------------------------

class PlaywrightCaptchaDriver:
    """Adapts a live Playwright page to the interface the pipeline drives.

    Every method returns only the widget's own data (bytes of the CAPTCHA
    image/audio, tile images, a target label); none returns page content. Each
    is best-effort: a selector that is absent yields ``None``/``False`` and the
    pipeline moves to the next solver. The selectors target Google reCAPTCHA v2
    and generic image CAPTCHAs; they are exercised against fakes in the suite
    and must be re-verified against a real widget in dry-run before the
    channel is trusted.
    """

    ANCHOR = "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise/anchor']"
    BFRAME = "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe']"
    TOKEN_FIELDS = ("textarea[name='g-recaptcha-response']", "textarea[name='h-captcha-response']",
                    "input[name='cf-turnstile-response']")

    def __init__(self, page, recipe=None):
        self.page = page
        self.recipe = recipe

    def _frame(self, selector):
        handle = self.page.query_selector(selector)
        return handle.content_frame() if handle else None

    def passed(self) -> bool:
        for sel in self.TOKEN_FIELDS:
            el = self.page.query_selector(sel)
            if el is not None:
                try:
                    if (el.input_value() or "").strip():
                        return True
                except Exception:
                    pass
        anchor = self._frame(self.ANCHOR)
        if anchor is not None and anchor.query_selector(".recaptcha-checkbox-checked") is not None:
            return True
        return False

    def _open_challenge(self):
        anchor = self._frame(self.ANCHOR)
        if anchor is None:
            return None
        box = anchor.query_selector("#recaptcha-anchor")
        if box is not None:
            box.click()
            self.page.wait_for_timeout(2000)
        if self.passed():
            return None
        return self._frame(self.BFRAME)

    def audio_challenge(self):
        frame = self._open_challenge()
        if frame is None:
            return None
        button = frame.query_selector("#recaptcha-audio-button")
        if button is not None:
            button.click()
            self.page.wait_for_timeout(2000)
        link = frame.query_selector(".rc-audiochallenge-tdownload-link, audio#audio-source")
        if link is None:
            return None
        url = link.get_attribute("href") or link.get_attribute("src")
        if not url:
            return None
        return self.page.context.request.get(url).body()

    def submit_audio(self, text: str) -> None:
        frame = self._frame(self.BFRAME)
        if frame is None:
            return
        field = frame.query_selector("#audio-response")
        if field is not None:
            field.fill(text)
        verify = frame.query_selector("#recaptcha-verify-button")
        if verify is not None:
            verify.click()
            self.page.wait_for_timeout(2000)

    def tile_challenge(self):
        frame = self._open_challenge() or self._frame(self.BFRAME)
        if frame is None:
            return None
        label = frame.query_selector(".rc-imageselect-desc-no-canonical strong, "
                                     ".rc-imageselect-desc strong")
        tiles = frame.query_selector_all("td.rc-imageselect-tile")
        if label is None or not tiles:
            return None
        return {"target": label.inner_text(),
                "tiles": [t.screenshot() for t in tiles]}  # each tile image only

    def click_tiles(self, indexes) -> None:
        frame = self._frame(self.BFRAME)
        if frame is None:
            return
        tiles = frame.query_selector_all("td.rc-imageselect-tile")
        for index in indexes:
            if 0 <= index < len(tiles):
                tiles[index].click()
        verify = frame.query_selector("#recaptcha-verify-button")
        if verify is not None:
            verify.click()
            self.page.wait_for_timeout(2500)

    def text_image(self):
        selectors = ["img[src*='captcha' i]"] + list(
            getattr(self.recipe, "captcha_selectors", ()) or ())
        for sel in selectors:
            el = self.page.query_selector(sel)
            if el is not None and (el.evaluate("e => e.tagName") or "").upper() == "IMG":
                return el.screenshot()
        return None

    def submit_text(self, text: str) -> None:
        field = self.page.query_selector(
            "input[name*='captcha' i]:not([type=hidden]), input[id*='captcha' i]:not([type=hidden])")
        if field is not None:
            field.fill(text)
