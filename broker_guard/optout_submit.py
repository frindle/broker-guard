"""Automated opt-out form SUBMISSION -- the one code path with side effects.

What makes this different from everything else here
------------------------------------------------------
``browser.py`` is explicit that it "only ever reads". This module is the
opposite: it types Penn's real name, email and state into a third party's
web form and, when fully switched on, presses Submit on his behalf,
unattended. Nothing else in this codebase does that. Every design choice
below follows from that one fact.

Four interlocks, and all four must be open
---------------------------------------------
1. ``Config.optout_submit_enabled`` (``BG_OPTOUT_SUBMIT_ENABLED``) --
   defaults False. Off means this module refuses before it ever opens a
   browser. It is deliberately NOT wired into the autopilot scan loop's
   default behaviour: a scan cycle does not submit anything.
2. ``Config.optout_submit_dry_run`` (``BG_OPTOUT_SUBMIT_DRY_RUN``) --
   defaults True. On means fill + screenshot + audit record, and stop
   short of the Submit click.
3. ``optout_forms.RECIPES`` -- an explicit per-broker allow-list. A broker
   with no hand-verified recipe cannot be submitted to at all.
4. ``Config.playwright_enabled`` -- no browser, nothing happens.

Turning 1 on and 2 off are two separate acts, on purpose. Neither alone
can cause a real submission.

The CAPTCHA rule is "stop", never "solve"
--------------------------------------------
If a bot check is detected, this module screenshots the filled form,
records the attempt as ``needs_manual_action`` and closes the page. It does
not solve, bypass, out-source or retry it. ``captcha.py`` exists in this
repo and is deliberately NOT imported here.

That is not a rare edge case for the first broker we support: Consumer
Canvas's OneTrust form carries a mandatory BotDetect image CAPTCHA, so a
REAL submission to it will always stop here. The useful artifact in that
case is the screenshot of the fully-filled form plus the audit record --
Penn opens the form, types the six characters, and presses Submit himself,
with everything else already done. That is an honest outcome, not a
failure, and the review page says so.

Because bailing out is the safe direction, CAPTCHA detection here is
deliberately BROAD -- the opposite tuning from ``browser.bot_wall_reason``,
where a false positive costs a broker its presence check. Here a false
positive costs one attempt that a human then completes. Over-detecting is
the correct bias.

Testability
--------------
``OptOutSubmitter`` follows ``browser.PlaywrightChecker``'s shape exactly:
the real Playwright import happens only inside ``start()``, and
``_browser`` can be assigned directly by a test (the pattern
``tests/test_detection_blindness.py`` already uses). The suite therefore
drives the whole fill/detect/bail/dry-run flow against a fake page object
with Playwright uninstalled, and **never** touches a real broker's form.
"""
import logging
from datetime import datetime, timezone

from broker_guard import optout_forms, review
from broker_guard.browser import bot_wall_reason
from broker_guard.playwright_checks import is_safe_url

log = logging.getLogger("broker_guard.optout_submit")

# Generic bot-check markers, swept on every form regardless of recipe. A
# recipe's own ``captcha_selectors`` are additive to these.
_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='challenge']",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[data-sitekey]",
    "img[src*='captcha']",
    "input[name*='captcha']",
    "input[id*='captcha']",
)

# How long to wait after pressing Submit before reading the result page.
_POST_SUBMIT_WAIT_MS = 5000
# The broker's own confirmation text kept in the record. The broker's words,
# not the person's -- but still truncated rather than stored unbounded.
_CONFIRMATION_CHARS = 600


class SubmissionRefused(RuntimeError):
    """The attempt was refused by an interlock before any browser opened."""


def _utcnow():
    return datetime.now(timezone.utc)


def _safe_error(exc) -> str:
    """Exception class + first line only -- never the full text.

    Playwright embeds the page URL (and selector text) in its error
    messages, and a form URL can carry a query string. Same rule
    ``searx_client._safe_error`` and ``exposure._safe_error`` already apply.
    """
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return "{}: {}".format(type(exc).__name__, first_line[:200])


# --- detection ---------------------------------------------------------------

def captcha_selectors_for(recipe) -> tuple:
    """Every selector whose presence means "bot check" for *recipe*."""
    return tuple(_CAPTCHA_SELECTORS) + tuple(getattr(recipe, "captcha_selectors", ()) or ())


def detect_captcha(page, recipe) -> str | None:
    """The first bot-check selector present on *page*, or ``None``.

    Only presence is required, not visibility: a CAPTCHA widget that is in
    the DOM but not yet painted is still a CAPTCHA, and asking a fake page
    for layout geometry would make this untestable. A selector that raises
    is treated as absent -- a broken selector must not be able to claim a
    bot check that is not there, nor crash the attempt.
    """
    for selector in captcha_selectors_for(recipe):
        try:
            if page.query_selector(selector) is not None:
                return selector
        except Exception:
            continue
    return None


def classify_submission(text: str | None, recipe) -> bool:
    """Did the post-submit page confirm the request?

    True only when one of the recipe's ``success_markers`` appears. An
    unrecognized result page is NOT treated as success: the attempt is
    recorded as ``failed`` so it gets looked at, rather than silently
    counted as a request that was never actually accepted.
    """
    haystack = (text or "").lower()
    return any(marker.lower() in haystack for marker in (recipe.success_markers or ()))


# --- page actions ------------------------------------------------------------

def _fill_text(page, selector: str, value: str) -> None:
    page.fill(selector, value)


def _fill_combo(page, selector: str, value: str) -> None:
    """Type into a ``role=combobox`` autocomplete and pick the match.

    A plain ``fill`` leaves an Angular autocomplete's MODEL empty even
    though the box shows the text, so the form stays invalid and the submit
    button stays disabled. The value has to be typed (so the popup filters)
    and then the matching option clicked.

    Falls back to pressing Enter when no option element can be found, which
    is what the widget does for an exact match, and never raises on the
    fallback path alone -- ``verify_ready`` is what ultimately decides
    whether the form is fillable.
    """
    page.fill(selector, "")
    page.type(selector, value, delay=30)
    option = "[role='option'][aria-label='{}']".format(value)
    try:
        page.wait_for_selector(option, timeout=3000)
        page.click(option)
        return
    except Exception:
        pass
    try:
        page.press(selector, "Enter")
    except Exception:
        pass


def _click_listbox_option(page, choice) -> None:
    """Click one option in a OneTrust ``role=listbox`` group."""
    page.click("{} [role='option'][aria-label=\"{}\"]".format(
        choice.container, choice.option_label))


def _select_option(page, select) -> None:
    """Pick an option in a real ``<select>`` (L.S Mobile's form).

    By LABEL, not by value: the label is what a human read off the page and
    wrote into the recipe, and it is what a diff of the recipe can be
    reviewed against. L.S Mobile happens to use the label as the value too,
    but relying on that would be relying on a coincidence.
    """
    page.select_option(select.container, label=select.option_label)


def _pick_listbox_button(page, selector: str, value: str) -> None:
    """Open a collapsed ``role=combobox`` BUTTON and click its matching option.

    Different from ``_click_listbox_option`` (OneTrust's listbox, already
    expanded/present in the DOM, no open click needed): this is a Wix
    dropdown (ACHCOOP's State field) that starts collapsed
    (``aria-expanded="false"``) and only renders its options -- into the
    element named by the button's own ``aria-controls`` -- after being
    clicked. Matched by exact accessible name (the option's visible text;
    these carry no ``aria-label``), scoped to that popup so a same-named
    option on some other widget on the page could not be clicked instead.
    """
    page.click(selector)
    controls = page.get_attribute(selector, "aria-controls")
    scope = page.locator("#{}".format(controls)) if controls else page
    scope.get_by_role("option", name=value, exact=True).click()


def _select_field(page, selector: str, value: str) -> None:
    """Pick an option in a real ``<select>`` by a PROFILE-derived value.

    The ``Field(kind="select")`` counterpart to ``_select_option``: that
    function is for a fixed literal written into the recipe (L.S Mobile's
    Territory); this is for a plain HTML state dropdown whose correct
    choice varies with the identity being run, the same way a ``combo``
    field's typed text does. Matched by label, same reasoning as above.
    """
    page.select_option(selector, label=value)


def apply_recipe(page, recipe, resolved: dict) -> dict:
    """Run every step of *recipe* on *page*, in order.

    Returns ``{"filled": {label: value}, "chosen": [label, ...]}`` -- the
    record of what was actually put on the form, which becomes the audit
    record's ``fields``.

    The order comes from ``optout_forms.ordered_steps``: choices-then-fields
    for a form like Consumer Canvas's, or the recipe's own explicit ``steps``
    for one like Nielsen's, where the request-type listbox does not exist in
    the DOM until Country has been filled.

    Re-checks the forbidden-selector rule here, in the one function that can
    actually type into a page, rather than trusting the caller. A honeypot
    that gets filled is not recoverable after the fact.
    """
    optout_forms.assert_no_forbidden(recipe)

    chosen, filled = [], {}
    for step in optout_forms.ordered_steps(recipe):
        if isinstance(step, optout_forms.Select):
            _select_option(page, step)
            chosen.append({"label": step.label, "value": step.option_label})
        elif isinstance(step, optout_forms.Choice):
            _click_listbox_option(page, step)
            chosen.append({"label": step.label, "value": step.option_label})
        elif isinstance(step, optout_forms.Check):
            # force=True: several real checkboxes (ACHCOOP's Wix "Select
            # your request" group, confirmed live 2026-09-22) are a
            # visually-tiny native <input> with the clickable look drawn by
            # a sibling span/label -- functionally a real checkbox, but too
            # small/overlapped for Playwright's own actionability wait
            # (visible + receives-pointer-events) to ever pass. force=True
            # skips that wait and clicks it directly; for an ordinary,
            # unobstructed checkbox (L.S Mobile's) this changes nothing.
            page.check(step.selector, force=True)
            chosen.append({"label": step.label, "value": "checked"})
        elif isinstance(step, optout_forms.Field):
            value = resolved["values"].get(step.selector)
            if not value:
                continue
            if step.kind == "combo":
                _fill_combo(page, step.selector, value)
            elif step.kind == "select":
                _select_field(page, step.selector, value)
            elif step.kind == "listbox_button":
                _pick_listbox_button(page, step.selector, value)
            else:
                _fill_text(page, step.selector, value)
            filled[step.label] = value
        else:
            raise TypeError("unknown recipe step: {!r}".format(type(step).__name__))
    return {"filled": filled, "chosen": chosen}


def _screenshot(page) -> bytes | None:
    try:
        return page.screenshot(full_page=True)
    except Exception as exc:
        log.warning("could not capture attempt screenshot",
                    extra={"error": _safe_error(exc)})
        return None


# --- the attempt -------------------------------------------------------------

class OptOutSubmitter:
    """Drives one broker's opt-out form in a real headless Chromium.

    Mirrors ``browser.PlaywrightChecker``: the Playwright import lives in
    ``start()`` alone, so the module imports (and the test suite runs) with
    Playwright absent, and a test can assign ``_browser`` directly.
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
                    log.warning("submitter teardown failed",
                                extra={"error": _safe_error(exc)})
        self._browser = self._playwright = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def new_page(self):
        context = self._browser.new_context(
            user_agent=self.user_agent,
            accept_downloads=False,
            java_script_enabled=True,
        )
        context.set_default_timeout(self.timeout_ms)
        return context, context.new_page()


def _notify_drift(alert_sink, record: dict, recipe) -> None:
    """Tell the alert sink when THIS broker's form stopped matching its recipe.

    The opt-out leg never goes through ``orchestrator.run_cycle``, so it
    cannot reach the sinks the way the search leg does -- but the rot it can
    suffer is identical (an input id renamed, a submit button that moved),
    and it is just as invisible: today a drifted recipe produces one
    ``failed`` row on the review page and nothing else. This sends the same
    ``recipe_drift`` event the search leg sends, to the same sinks, using
    the same classifier, so there is one notification path and not two.

    Total and best effort: an alerting failure must never turn a recorded
    attempt into a raised exception.
    """
    if alert_sink is None:
        return
    from broker_guard import recipe_health

    reason = record.get("reason") or ""
    if not recipe_health.is_recipe_drift(reason):
        return
    event = recipe_health.drift_event(
        recipe.broker_id, recipe_health.LEG_OPTOUT, reason,
        at=record.get("finished_at"), identity_key=record.get("identity_key"))
    try:
        alert_sink({"identity_key": record.get("identity_key"),
                    "now_iso": record.get("finished_at"),
                    "recipe_drift": [event]})
    except Exception as exc:  # pragma: no cover - alerting is best effort
        log.warning("recipe drift alert failed", extra={"error": _safe_error(exc)})


def _record(recipe, identity_key, started_at, outcome, dry_run, **extra) -> dict:
    record_id = review.attempt_id(recipe.broker_id, identity_key, started_at.isoformat())
    base = {
        "id": record_id,
        "basename": review.basename_for(recipe.broker_id, started_at, record_id),
        "broker_id": recipe.broker_id,
        "broker_name": recipe.broker_name,
        "form_url": recipe.url,
        "form_flavor": recipe.flavor,
        "identity_key": identity_key,
        "outcome": outcome,
        "dry_run": bool(dry_run),
        "started_at": started_at.isoformat(),
        "finished_at": _utcnow().isoformat(),
    }
    base.update(extra)
    # What KIND of failure this was, in recipe_health's vocabulary, stored
    # on the record itself so the review page (and anyone reading the JSON
    # later) can tell "their form changed" from "the network hiccuped"
    # without re-parsing free text. Successful outcomes carry None.
    from broker_guard import recipe_health

    reason = base.get("reason")
    unhappy = outcome in (review.OUTCOME_FAILED, review.OUTCOME_NEEDS_MANUAL)
    base["failure_class"] = (
        recipe_health.classify_failure(reason) if (reason and unhappy) else None)
    return base


def submit_optout(recipe, identity, cfg, submitter=None, directory=None,
                  dry_run=None, now=None, alert_sink=None) -> dict:
    """Run one opt-out submission attempt and persist its audit record.

    Returns the saved record dict. NEVER raises for an ordinary failure --
    a broken form, an unreachable site or a bot check all come back as a
    recorded attempt, because an attempt that happened must leave a trace.
    ``SubmissionRefused`` is raised only for an interlock refusal, where
    nothing happened at all and there is nothing to audit.

    *submitter* is an already-started ``OptOutSubmitter`` (or any object
    with a compatible ``new_page()``). *dry_run* overrides
    ``cfg.optout_submit_dry_run`` for a one-off "fill it but do not send it"
    run from the UI.
    """
    if not getattr(cfg, "optout_submit_enabled", False):
        raise SubmissionRefused(
            "automated opt-out submission is off (set BG_OPTOUT_SUBMIT_ENABLED, "
            "or turn it on in Settings)")
    if not optout_forms.is_supported(recipe.broker_id):
        raise SubmissionRefused(
            "no verified form recipe for {!r}".format(recipe.broker_id))
    if not is_safe_url(recipe.url):
        raise SubmissionRefused("refusing a non-http(s) form url")
    try:
        # A honeypot (or an SSN box) that a recipe edit accidentally started
        # targeting is caught here, before a browser is ever opened.
        optout_forms.assert_no_forbidden(recipe)
    except optout_forms.ForbiddenFieldError as exc:
        raise SubmissionRefused(str(exc))

    effective_dry_run = (
        bool(getattr(cfg, "optout_submit_dry_run", True)) if dry_run is None else bool(dry_run)
    )
    directory = directory or review.review_dir(cfg)
    started_at = now or _utcnow()
    identity_key = getattr(identity, "identity_key", "") or ""

    def _finish(record, screenshot=None):
        """Persist the attempt, then notify if it looks like recipe rot.

        One funnel for all six exit points below, so a new outcome branch
        cannot forget the alert -- and so the SAVE always happens first:
        the audit record is the legally interesting half, and an alerting
        problem must never cost it.
        """
        saved = review.save_attempt(directory, record, screenshot)
        _notify_drift(alert_sink, saved, recipe)
        return saved

    resolved = optout_forms.resolve_fields(recipe, identity)
    if resolved["missing"]:
        # Refuse to send a half-filled DSAR under Penn's name: the broker
        # answers it and the request is spent. Recorded so the reason is
        # visible on the review page rather than lost in a log line.
        record = _record(
            recipe, identity_key, started_at, review.OUTCOME_FAILED, effective_dry_run,
            reason="missing required profile fields: " + ", ".join(resolved["missing"]),
            missing=resolved["missing"], fields={}, choices=[],
        )
        return _finish(record)

    if submitter is None or getattr(submitter, "_browser", None) is None:
        record = _record(
            recipe, identity_key, started_at, review.OUTCOME_FAILED, effective_dry_run,
            reason="no browser available (is BG_PLAYWRIGHT_ENABLED on, and was the "
                   "image built with INSTALL_BROWSERS=true?)",
            missing=[], fields={}, choices=[],
        )
        return _finish(record)

    context = page = None
    screenshot = None
    try:
        context, page = submitter.new_page()
        page.goto(recipe.url, wait_until="domcontentloaded")
        # Same reasoning as search_probe's _FORM_SETTLE_MS (measured there
        # against usphonebook.com, which rewrites a raw form post before its
        # JS binds): "domcontentloaded" is not "this page's JS has finished
        # wiring itself up". Confirmed live against ACHCOOP's opt-out page
        # (a Wix SPA) on 2026-09-22 -- opening its custom State dropdown
        # immediately after domcontentloaded intermittently clicked a
        # not-yet-hydrated version of the widget and the whole attempt came
        # back "failed" with an empty, unfilled form in the screenshot; a
        # short settle here fixed it every time after.
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Same question browser.py asks first: did we actually reach the
        # form, or an interstitial? An interstitial is a bot check too.
        try:
            text, title = page.inner_text("body"), page.title()
        except Exception:
            text, title = "", ""
        wall = bot_wall_reason(text, title, None)
        if wall:
            screenshot = _screenshot(page)
            record = _record(
                recipe, identity_key, started_at, review.OUTCOME_NEEDS_MANUAL,
                effective_dry_run, reason=wall, detected="bot_wall",
                fields={}, choices=[], missing=[],
                manual_action_source="captcha_fallback",
            )
            return _finish(record, screenshot)

        applied = apply_recipe(page, recipe, resolved)

        # Screenshot the FILLED form. This is the artifact that makes a
        # bail-out useful: it is exactly what a human would finish by hand.
        screenshot = _screenshot(page)

        found = detect_captcha(page, recipe)
        if found:
            # Policy: stop. Never solve, never bypass, never retry.
            record = _record(
                recipe, identity_key, started_at, review.OUTCOME_NEEDS_MANUAL,
                effective_dry_run,
                reason="bot check present on the form ({}); form was filled but NOT "
                       "submitted -- finish it by hand".format(found),
                detected=found, fields=applied["filled"], choices=applied["chosen"],
                missing=[], manual_action_source="captcha_fallback",
            )
            return _finish(record, screenshot)

        if effective_dry_run:
            record = _record(
                recipe, identity_key, started_at, review.OUTCOME_DRY_RUN, True,
                reason="dry run: form filled, Submit deliberately not pressed",
                fields=applied["filled"], choices=applied["chosen"], missing=[],
            )
            return _finish(record, screenshot)

        # --- the real thing ------------------------------------------------
        page.click(recipe.submit_selector)
        try:
            page.wait_for_timeout(_POST_SUBMIT_WAIT_MS)
            result_text = page.inner_text("body")
        except Exception:
            result_text = ""

        confirmed = classify_submission(result_text, recipe)
        record = _record(
            recipe, identity_key, started_at,
            review.OUTCOME_SUBMITTED if confirmed else review.OUTCOME_FAILED,
            False,
            reason=None if confirmed else
                   "Submit was pressed but the page did not confirm the request; "
                   "check the screenshot before re-trying",
            fields=applied["filled"], choices=applied["chosen"], missing=[],
            confirmation_text=(result_text or "")[:_CONFIRMATION_CHARS],
        )
        return _finish(record, screenshot)

    except Exception as exc:
        # Everything is an audited failure, never a crash: an attempt that
        # touched a real form must leave a record even when it broke.
        if screenshot is None and page is not None:
            screenshot = _screenshot(page)
        log.warning("opt-out attempt failed", extra={
            "broker_id": recipe.broker_id, "error": _safe_error(exc),
        })
        record = _record(
            recipe, identity_key, started_at, review.OUTCOME_FAILED,
            effective_dry_run, reason=_safe_error(exc),
            fields={}, choices=[], missing=[],
        )
        return _finish(record, screenshot)
    finally:
        for obj in (page, context):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def run_attempt(broker_id: str, identity, cfg, dry_run=None, alert_sink=None) -> dict:
    """Open a browser, run one attempt for *broker_id*, close it again.

    The convenience entry point the web UI button uses. Deliberately
    one-shot: it builds and tears down its own browser rather than holding
    one open, because submission is an occasional, human-initiated action,
    not a sweep.

    *alert_sink* defaults to the standard one built from *cfg*, so the
    caller (the web UI's run button) does not have to know that recipe-rot
    alerting exists. A caller that passes one explicitly -- or a test with a
    ``cfg`` that has no alerting settings on it at all -- is respected.
    """
    recipe = optout_forms.recipe_for(broker_id)
    if alert_sink is None:
        try:
            from broker_guard.sinks import build_alert_sink

            alert_sink = build_alert_sink(cfg)
        except Exception as exc:
            # Alerting must never be the reason an opt-out attempt does not
            # happen; the attempt is the point, the notification is not.
            log.warning("could not build the alert sink for this attempt",
                        extra={"error": _safe_error(exc)})
    if not getattr(cfg, "optout_submit_enabled", False):
        raise SubmissionRefused(
            "automated opt-out submission is off (set BG_OPTOUT_SUBMIT_ENABLED, "
            "or turn it on in Settings)")

    submitter = OptOutSubmitter(
        timeout_ms=getattr(cfg, "playwright_timeout_ms", 30000),
        headless=getattr(cfg, "playwright_headless", True),
    )
    try:
        submitter.start()
    except Exception as exc:
        log.warning("browser unavailable for opt-out attempt",
                    extra={"error": _safe_error(exc)})
        submitter = None
    try:
        return submit_optout(recipe, identity, cfg, submitter=submitter,
                             dry_run=dry_run, alert_sink=alert_sink)
    finally:
        if submitter is not None:
            submitter.close()
