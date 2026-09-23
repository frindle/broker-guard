"""Tell "this broker's site CHANGED" apart from "this broker was unreachable".

Why this exists
------------------
``search_forms.RECIPES`` and ``optout_forms.RECIPES`` are hand-written
transcriptions of somebody else's HTML. That HTML is not ours and nobody
tells us when it changes. When it does, the failure is quiet and
well-behaved by design: ``search_probe.run_search`` turns a selector that
no longer matches into ``{"error": ...}``, ``interpret_check_result`` turns
that into checked=False, and the broker lands in the cycle's ``errors``
bucket -- correct (an unknown is never a false "absent"), and completely
invisible. A recipe can rot for months inside a green-looking sweep.

Meanwhile the SAME bucket collects Wi-Fi blips, a broker's five-minute
outage, and every bot wall in the pilot. So "alert on errors" is not an
option: it would fire constantly, Penn would mute it, and the one error
that meant something would be muted with it.

This module is the classifier that separates them. It is pure text ->
label, with no I/O and no browser, so every rule below is unit-tested
against the exact strings the two drivers actually produce (the tests
quote them from ``search_probe``/``optout_submit``/``browser``, not from
imagination).

The five classes, and who alerts
-----------------------------------
* ``STRUCTURAL`` -- the page is not shaped the way the recipe says.
  A field selector that matched nothing, a submit that went nowhere, a
  results page carrying none of the recipe's markers, a strict-mode
  violation because a selector now matches two elements. **This is the
  only class that alerts**, because it is the only one a human can fix,
  and the fix is always "re-read the page and update the recipe".
* ``TRANSIENT`` -- a network blip, a navigation timeout, a connection
  reset. Retry; the next cycle probably succeeds. Never alerts.
* ``BLOCKED`` -- a bot wall or a captcha. Expected, already visible in the
  review page and the logs, and NOT something a recipe edit repairs (see
  ThatsThem's and SearchPublicRecords' notes: their recipes are correct
  and blocked anyway). Never alerts.
* ``PROFILE`` -- the recipe is fine and the profile cannot fill it
  ("missing required profile fields: Age"). A data problem on our side.
  Never alerts here; the review page already says it.
* ``ENVIRONMENT`` -- our own browser is missing or was never started.
  Never alerts here; that is a deployment problem the health check owns.

Anything unrecognized is ``UNKNOWN`` and does NOT alert. That direction is
deliberate: a new, unclassified error string should make this module quiet
and wrong-by-omission rather than loud and wrong-by-guess. The
``--check-recipes`` command exists precisely so drift can also be found by
asking, instead of only by waiting.

Once per breakage, not once per cycle
----------------------------------------
A broken recipe stays broken until somebody edits it, so an alert every
cycle is the same notification repeated hourly until it is ignored.
``DriftLedger`` remembers which (broker, leg) pairs have already been
reported and suppresses the repeat, then forgets a pair as soon as that
broker checks cleanly again -- so a recurrence after a fix alerts afresh.
"""
import json
import logging
import os
import re

log = logging.getLogger("broker_guard.recipe_health")

STRUCTURAL = "structural"
TRANSIENT = "transient"
BLOCKED = "blocked"
PROFILE = "profile"
ENVIRONMENT = "environment"
UNKNOWN = "unknown"

# The one class worth waking somebody for. Kept as a tuple (not a bare
# constant) so a future "also alert on X" is a one-line, reviewable change
# rather than a new branch in three call sites.
ALERTING_CLASSES = (STRUCTURAL,)

# The event kind this module contributes to ``alert.batch_digest``, alongside
# the existing "new_appearance" / "resolved".
KIND = "recipe_drift"

# Legs, for the event payload and the ledger key.
LEG_SEARCH = "search"
LEG_OPTOUT = "optout"


# --- the rules ---------------------------------------------------------------
#
# Ordered: the first matching rule wins, so the specific patterns sit above
# the general ones. Every needle below was copied from the string that
# produces it -- the module that emits it is named in the comment -- rather
# than guessed at, because a classifier tuned against imagined error text is
# a classifier that silently classifies nothing.

# Checked FIRST: a bot wall's text can contain words ("timeout", "denied")
# that the rules below would otherwise read as something else.
_BLOCKED = (
    "bot wall:",                       # browser.bot_wall_reason, every shape
    "bot check present on the form",   # optout_submit's captcha stop
    "verify you are human",
    "captcha",
)

# Playwright's own API names. A timeout waiting for NAVIGATION is a network
# fact; a timeout waiting for an ELEMENT is a statement about the page's
# shape, which is exactly what a stale selector looks like. Same exception
# class, opposite meanings -- this distinction is the heart of the module.
_STRUCTURAL_PW_CALLS = (
    "page.fill", "page.click", "page.check", "page.select_option",
    "page.type", "page.press", "page.wait_for_selector", "page.get_attribute",
    "page.inner_text", "page.eval_on_selector",
    "locator.click", "locator.fill", "locator.check", "locator.select_option",
    "elementhandle.click", "elementhandle.fill",
    "strict mode violation",
)

_TRANSIENT_PW_CALLS = (
    "page.goto", "frame.goto", "page.wait_for_load_state",
)

_STRUCTURAL = (
    # search_probe.run_search
    "could not fill",
    "could not submit the search",
    # search_probe.await_results, via run_search's wrapper
    "no page served by",
    "results page never settled",
    # search_forms.classify_search_page
    "matched no known marker",
    "recipe may be stale",
    "none of the identity terms appear",
    "says both 'no results' and 'results'",
    "with no identity term on it",
    "search results page was empty",
    # search_probe.SearchChecker / optout_submit interlocks: a recipe that
    # points somewhere it should not is a recipe bug, and a loud one.
    "targets state-changing surface",
    "targets forbidden input",
    "refusing a non-http(s) form url",
    "refusing non-http(s) search url",
    # optout_submit's post-submit classification
    "did not confirm the request",
)

_TRANSIENT = (
    "err_connection_refused",
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_timed_out",
    "err_timed_out",
    "err_network_changed",
    "err_internet_disconnected",
    "err_empty_response",
    "err_socket_not_connected",
    "could not open search form",
    "net::err_abort",
    "timeout",          # after the Playwright-call rules above have run
    "temporarily unavailable",
    "connection",
)

# A host that no longer resolves is not a blip: either the broker is gone or
# the recipe's URL is. Both need a human, so it is structural.
_STRUCTURAL_NETWORK = (
    "err_name_not_resolved",
    "err_cert_",
    "err_ssl_",
)

_PROFILE = (
    "missing required profile fields",
    "no value for required search field",
)

_ENVIRONMENT = (
    "no browser available",
    "browser not started",
    "playwright",
)


def _has(needles, haystack: str) -> bool:
    return any(n in haystack for n in needles)


def classify_failure(text) -> str:
    """Which of the five classes *text* describes.

    *text* is any failure string this codebase produces for a broker: a
    ``run_search`` error, an ``optout_submit`` record's ``reason``, or a
    ``run_cycle`` error entry (``"<broker>: check failed: <error>"``).
    Returns ``UNKNOWN`` for anything unrecognized, which never alerts.
    """
    haystack = (text or "")
    if not isinstance(haystack, str) or not haystack.strip():
        return UNKNOWN
    haystack = haystack.lower()

    if _has(_BLOCKED, haystack):
        return BLOCKED
    if _has(_PROFILE, haystack):
        return PROFILE
    if _has(_ENVIRONMENT, haystack):
        return ENVIRONMENT
    if _has(_STRUCTURAL_NETWORK, haystack):
        return STRUCTURAL
    # Playwright call names before the generic "timeout" rule below -- see
    # the comment on _STRUCTURAL_PW_CALLS.
    if _has(_STRUCTURAL_PW_CALLS, haystack):
        return STRUCTURAL
    if _has(_TRANSIENT_PW_CALLS, haystack):
        return TRANSIENT
    if _has(_STRUCTURAL, haystack):
        return STRUCTURAL
    if _has(_TRANSIENT, haystack):
        return TRANSIENT
    return UNKNOWN


def is_recipe_drift(text) -> bool:
    """Does *text* look like the broker's site changed under a recipe?"""
    return classify_failure(text) in ALERTING_CLASSES


# ``run_cycle``'s error entries are "<broker_id>: check failed: <error>"
# (service.build_presence_checker's PresenceUnknown message). The prefix is
# stripped for the alert body so the notification reads as the broker's own
# error rather than as our wrapper.
_CHECK_FAILED = re.compile(r"^\s*[\w.\-]+:\s*check failed:\s*", re.I)

# An alert body is read by a human, but it is also written to a file and
# possibly POSTed to a webhook, so it is truncated like every other
# broker-supplied string in this codebase.
_DETAIL_CHARS = 300


def drift_event(broker_id: str, leg: str, text: str, at: str | None = None,
                identity_key: str | None = None) -> dict:
    """One ``recipe_drift`` event, in ``alert.batch_digest``'s shape."""
    detail = _CHECK_FAILED.sub("", str(text or "")).strip()
    return {
        "kind": KIND,
        "broker_id": broker_id,
        "leg": leg,
        "failure_class": classify_failure(text),
        "detail": detail[:_DETAIL_CHARS],
        "identity_key": identity_key,
        "at": at,
    }


def drift_events_from_errors(errors, leg: str = LEG_SEARCH,
                             at: str | None = None,
                             identity_key: str | None = None) -> list:
    """The ``recipe_drift`` events implied by a cycle's ``errors`` list.

    Takes ``run_cycle``'s ``[{"broker_id", "error"}, ...]`` and keeps only
    the entries that look like the site changed. Total and defensive: a
    malformed entry is skipped rather than raising inside an alert path.
    """
    out = []
    for entry in errors or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get("error") or entry.get("reason") or ""
        if not is_recipe_drift(text):
            continue
        out.append(drift_event(entry.get("broker_id"), leg, text, at=at,
                               identity_key=identity_key))
    return out


# --- once per breakage, not once per cycle -----------------------------------

class DriftLedger:
    """Remembers which (broker, leg) pairs have already been reported.

    Deliberately a plain JSON file rather than a table in ``state.sqlite``:
    it is alert bookkeeping, not presence state, and losing it costs one
    duplicate notification rather than a person's scan history. Every method
    is total -- a corrupt or unreadable ledger degrades to "alert again",
    which is the safe direction for a rot detector.
    """

    def __init__(self, path: str):
        self.path = path
        self._seen = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._seen, fh, indent=1, sort_keys=True)
            try:
                os.chmod(self.path, 0o600)  # it names brokers: PII.
            except OSError:
                pass
        except OSError as exc:
            log.warning("recipe drift ledger write failed",
                        extra={"path": self.path, "error": str(exc)})

    @staticmethod
    def key(broker_id, leg) -> str:
        return "{}|{}".format(broker_id or "", leg or "")

    def should_report(self, event: dict) -> bool:
        """True the FIRST time this (broker, leg) breaks, false while it stays broken."""
        if not isinstance(event, dict):
            return False
        return self.key(event.get("broker_id"), event.get("leg")) not in self._seen

    def remember(self, events) -> None:
        changed = False
        for event in events or []:
            if not isinstance(event, dict):
                continue
            key = self.key(event.get("broker_id"), event.get("leg"))
            if key not in self._seen:
                self._seen[key] = {"at": event.get("at"),
                                   "failure_class": event.get("failure_class"),
                                   "detail": event.get("detail")}
                changed = True
        if changed:
            self._save()

    def clear(self, broker_id, leg) -> None:
        """Forget a pair, so a recurrence after a fix alerts afresh."""
        if self._seen.pop(self.key(broker_id, leg), None) is not None:
            self._save()

    def filter_new(self, events) -> list:
        """The events worth sending, remembering them as it goes."""
        fresh = [e for e in (events or []) if self.should_report(e)]
        self.remember(fresh)
        return fresh

    def known(self) -> dict:
        return dict(self._seen)
