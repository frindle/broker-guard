"""Assisted generic form filler: learn a recipe from the live page.

For a broker with no hand-written recipe, read its real opt-out form, map every
control to a profile field (keywords first, a LOCAL model for the leftovers),
and store the result as a *candidate* recipe (``recipe_store``). The first run
is always a dry run whose screenshot Penn approves; only after one confirmed
real submission does the recipe become ``live``.

What the model sees
-------------------
Only control LABEL strings from the broker's page ("Company name", "Preferred
contact method"), never a value, never the profile, never a screenshot. It
answers with one word from a fixed vocabulary; anything else is discarded.

What it never fills
-------------------
SSN, date of birth, driver's licence, passport, card/account numbers, ID
uploads, and any hidden/offscreen control (honeypots) go to
``forbidden_selectors``. A REQUIRED control that cannot be mapped is listed in
``unmapped`` and the candidate cannot be approved until a human resolves it.
"""
import json
import logging
import re

from broker_guard import optout_forms, platforms
from broker_guard.browser import bot_wall_reason

log = logging.getLogger("broker_guard.assisted")

SOURCES = ("first_name", "last_name", "full_name", "email", "phone", "address", "street",
           "city", "state", "state_code", "zip", "country", "message", "skip")
FORBIDDEN = "forbidden"

_FORBIDDEN = re.compile(
    r"\bssn\b|social\s*security|date\s*of\s*birth|\bdob\b|birth\s*date|birthday|driver|licen[cs]e|"
    r"passport|credit\s*card|card\s*number|account\s*number|tax\s*id|national\s*id|"
    r"id\s*(document|upload|number)|upload|attach|mother|maiden|salary|income", re.I)
_RULES = (
    ("email", re.compile(r"e-?mail", re.I)),
    ("first_name", re.compile(r"first\s*name|given\s*name|\bfname\b|forename", re.I)),
    ("last_name", re.compile(r"last\s*name|sur\s*name|family\s*name|\blname\b", re.I)),
    ("phone", re.compile(r"phone|mobile|\btel\b|telephone|cell", re.I)),
    ("zip", re.compile(r"\bzip\b|postal|post\s*code", re.I)),
    ("city", re.compile(r"\bcity\b|\btown\b", re.I)),
    ("country", re.compile(r"country", re.I)),
    ("state", re.compile(r"\bstate\b|province|region|territory", re.I)),
    ("street", re.compile(r"street|address\s*(line)?\s*1|^address$|home\s*address|mailing\s*address", re.I)),
    ("full_name", re.compile(r"^\s*(your\s*|full\s*|legal\s*)?name\s*\*?\s*$|full\s*name|your\s*name", re.I)),
)
_MESSAGE = re.compile(r"message|comments?|details|describe|additional|request\s*details|reason|how can we", re.I)
_ATTEST = re.compile(r"agree|confirm|certif|accurate|authori[sz]ed|true and correct|acknowledge|"
                     r"i am the|under penalty", re.I)
_WANTED_REQUEST = re.compile(r"delet|eras|remov|do not sell|don'?t sell|opt.?out|do not share", re.I)
_UNWANTED_REQUEST = re.compile(r"access|know|correct|rectif|portab|copy|update|categories", re.I)
_SUBMIT_TEXT = re.compile(r"submit|send|request|continue|opt.?out|delete|confirm|next|apply", re.I)
_NOT_SUBMIT = re.compile(r"cancel|back|reset|search|clear|close|login|sign\s*in|subscribe", re.I)

CONTROLS_JS = r"""() => {
  const out = {controls: [], buttons: [], captcha: false};
  const cssId = s => /^[A-Za-z][\w-]*$/.test(s || '');
  const sel = el => {
    if (cssId(el.id)) return '#' + el.id;
    const n = el.getAttribute('name');
    if (n && !/["\\]/.test(n)) {
      const s = el.tagName.toLowerCase() + '[name="' + n + '"]';
      if (el.type === 'radio' || el.type === 'checkbox') {
        const v = el.getAttribute('value');
        if (v && !/["\\]/.test(v)) return s + '[value="' + v + '"]';
      }
      if (document.querySelectorAll(s).length === 1) return s;
    }
    return null;
  };
  const label = el => {
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) return l.innerText; }
    const p = el.closest('label'); if (p) return p.innerText;
    return el.getAttribute('aria-label') || el.placeholder || el.getAttribute('name') || '';
  };
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none' && r.left > -100; };
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    const c = {tag: el.tagName.toLowerCase(), type, name: el.getAttribute('name') || '',
      selector: sel(el), label: (label(el) || '').replace(/\s+/g, ' ').trim().slice(0, 120),
      required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label(el) || ''),
      visible: vis(el), options: []};
    if (el.tagName === 'SELECT') c.options = Array.from(el.options).slice(0, 80).map(o => o.text.trim());
    out.controls.push(c);
  });
  document.querySelectorAll('button, input[type=submit], [role=button]').forEach(el => {
    if (!vis(el)) return;
    out.buttons.push({text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      type: (el.type || '').toLowerCase(), selector: sel(el)});
  });
  out.captcha = !!document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha, .h-captcha, [class*='turnstile']");
  return out;
}"""


def keyword_source(label: str, name: str = "") -> str | None:
    """The profile source for a control, ``FORBIDDEN``, or ``None`` if unknown."""
    text = "{} {}".format(label or "", name or "").strip()
    if _FORBIDDEN.search(text):
        return FORBIDDEN
    for source, pattern in _RULES:
        if pattern.search(label or "") or pattern.search(name or ""):
            return source
    return None


class LlmLabelMapper:
    """Map unknown control labels to a source with a local Ollama model.

    Sends ONLY label strings. The reply is validated against ``SOURCES``;
    anything else becomes ``None`` (left unmapped for a human).
    """

    def __init__(self, url: str, model: str = "qwen3-vl:8b", timeout_s: int = 90, session=None):
        self.url, self.model, self.timeout_s = (url or "").rstrip("/"), model, timeout_s
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def __call__(self, labels: list) -> dict:
        labels = [str(l)[:120] for l in labels if l]
        if not labels or not self.url:
            return {}
        prompt = ("Each string below is the label of a field on a privacy opt-out web form. "
                  "For each, answer which ONE of these it asks for: {}. Use 'skip' for anything "
                  "else (company, role, how you heard of us, preferences). Reply with JSON only: "
                  '{{"<label>": "<one word>"}}.\nLabels: {}').format(
                      ", ".join(SOURCES), json.dumps(labels))
        response = self.session.post(
            self.url + "/api/chat",
            json={"model": self.model, "stream": False, "format": "json", "think": False,
                  "options": {"temperature": 0},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=self.timeout_s)
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("label model HTTP %s" % response.status_code)
        raw = (response.json().get("message") or {}).get("content") or "{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return {k: v for k, v in (parsed.items() if isinstance(parsed, dict) else ())
                if k in labels and v in SOURCES}


def _best_option(options: list, wanted: str | None = None) -> str | None:
    if wanted:
        for opt in options:
            if wanted.lower() == opt.lower():
                return opt
    for opt in options:
        if _WANTED_REQUEST.search(opt) and not _UNWANTED_REQUEST.search(opt):
            return opt
    return None


def build_recipe(broker: dict, controls: list, buttons: list, platform: str | None,
                 label_mapper=None, url: str | None = None) -> dict:
    """Turn extracted controls into ``{recipe, unmapped, skipped, ok}``. Pure."""
    steps, forbidden, unmapped, skipped = [], [], [], []
    pending = []   # (control) needing the model

    def selector_of(c):
        return c.get("selector")

    resolved = {}
    for c in controls:
        if not c.get("visible"):
            if c.get("selector"):
                forbidden.append(c["selector"])
            continue
        label = c.get("label") or c.get("name") or ""
        src = keyword_source(label, c.get("name", ""))
        resolved[id(c)] = src
        if src is None and c.get("type") not in ("checkbox", "radio") and c.get("tag") != "select":
            pending.append(c)
        elif src is None and c.get("tag") == "select":
            pending.append(c)

    guesses = {}
    if label_mapper and pending:
        try:
            guesses = label_mapper(sorted({p.get("label") or p.get("name") for p in pending}))
        except Exception as exc:
            log.warning("label model failed", extra={"error": type(exc).__name__})

    for c in controls:
        if not c.get("visible"):
            continue
        sel = selector_of(c)
        label = c.get("label") or c.get("name") or "(unlabelled)"
        required = bool(c.get("required"))
        src = resolved.get(id(c))
        if src is None:
            src = guesses.get(c.get("label") or c.get("name"))
        kind_type, tag = c.get("type"), c.get("tag")

        if src == FORBIDDEN:
            if sel:
                forbidden.append(sel)
            if required:
                unmapped.append(label + " (needs information broker-guard will not send)")
            continue
        if not sel:
            (unmapped if required else skipped).append(label + " (no stable selector)")
            continue

        if kind_type in ("checkbox", "radio"):
            text = label
            if _WANTED_REQUEST.search(text) and not _UNWANTED_REQUEST.search(text):
                steps.append(optout_forms.Check(selector=sel, label=label))
            elif required and _ATTEST.search(text):
                steps.append(optout_forms.Check(selector=sel, label=label))
            elif required and kind_type == "checkbox":
                unmapped.append(label)
            else:
                skipped.append(label)
            continue

        if tag == "select":
            opts = c.get("options") or []
            if src in ("country", "state", "state_code"):
                steps.append(optout_forms.Field(selector=sel, source=src, label=label,
                                                kind="select", required=required))
                continue
            pick = _best_option(opts)
            if pick:
                steps.append(optout_forms.Select(container=sel, option_label=pick, label=label))
            elif required:
                unmapped.append(label)
            else:
                skipped.append(label)
            continue

        if src in ("skip", None):
            if tag == "textarea" or _MESSAGE.search(label):
                steps.append(optout_forms.Field(selector=sel, source="literal", label=label,
                                                value=optout_forms._DELETION_DETAILS,
                                                required=required))
            elif required:
                unmapped.append(label)
            else:
                skipped.append(label)
            continue
        if src == "message":
            steps.append(optout_forms.Field(selector=sel, source="literal", label=label,
                                            value=optout_forms._DELETION_DETAILS, required=required))
            continue
        steps.append(optout_forms.Field(selector=sel, source=src, label=label, required=required))

    submit = None
    for b in buttons:
        if b.get("selector") and _SUBMIT_TEXT.search(b.get("text", "")) and not _NOT_SUBMIT.search(b.get("text", "")):
            submit = b["selector"]
            if b.get("type") == "submit":
                break
    if submit is None:
        for b in buttons:
            if b.get("type") == "submit" and b.get("selector"):
                submit = b["selector"]
    has_email = any(isinstance(s, optout_forms.Field) and s.source == "email" for s in steps)
    problems = []
    if not has_email:
        problems.append("no email field found")
    if not submit:
        problems.append("no submit button found")
    recipe = optout_forms.FormRecipe(
        broker_id=broker["id"], broker_name=broker.get("name") or broker["id"],
        url=url or broker.get("optout_url") or broker.get("url") or "",
        flavor="assisted_generic", steps=tuple(steps),
        forbidden_selectors=tuple(dict.fromkeys(forbidden)), submit_selector=submit or "",
        no_captcha_verified=False,
        success_markers=("thank you", "request has been", "received", "submitted", "check your email"),
        notes="Learned by the assisted filler{}; unverified until approved and confirmed.".format(
            " (platform: %s)" % platform if platform else ""),
        # Unknown forms are assumed to email a verification link: the safe
        # direction (Penn is asked to click, rather than the request being
        # assumed live).
        confirmation_email=True,
        confirmation_sender="")
    return {"recipe": recipe, "unmapped": unmapped, "skipped": skipped, "problems": problems,
            "ok": not unmapped and not problems}


def _accept_consent(page, platform) -> None:
    for sel in platforms.consent_selectors(platform):
        try:
            el = page.query_selector(sel)
            if el is not None:
                el.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            continue
    for text in platforms.GENERIC_ACCEPT_TEXTS:
        try:
            el = page.query_selector("button:text-is('%s' i)" % text)
            if el is not None:
                el.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def assist_broker(page, broker: dict, label_mapper=None, settle_ms: int = 2500) -> dict:
    """Open the broker's opt-out page and build a candidate. Never submits.

    Returns ``{"status": "candidate"|"blocked"|"no_form", ...}``.
    """
    url = broker.get("optout_url") or broker.get("url")
    if not url:
        return {"status": "no_form", "reason": "no opt-out URL"}
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(settle_ms)
    try:
        text, title = page.inner_text("body"), page.title()
    except Exception:
        text, title = "", ""
    wall = bot_wall_reason(text, title, None)
    if wall:
        return {"status": "blocked", "reason": wall}
    try:
        html = page.content()
    except Exception:
        html = ""
    platform = platforms.detect_platform(html, [url] + [f.url for f in getattr(page, "frames", [])
                                                          if getattr(f, "url", None)])
    _accept_consent(page, platform)
    try:
        page.wait_for_selector("input, textarea, select", timeout=10000)
    except Exception:
        return {"status": "no_form", "reason": "no form controls rendered", "platform": platform}
    data = page.evaluate(CONTROLS_JS)
    built = build_recipe(broker, data.get("controls") or [], data.get("buttons") or [], platform,
                         label_mapper=label_mapper, url=url)
    built.update(status="candidate", platform=platform, captcha=bool(data.get("captcha")))
    if not any(isinstance(s, optout_forms.Field) for s in built["recipe"].steps):
        built.update(status="no_form", reason="no usable fields")
    return built
