"""Breach-exposure lookups and password hygiene -- two free, key-less data
sources feeding the exposure panel.

1. XposedOrNot (breach identity per email; no API key)::

     check_email(email)       -> GET /v1/check-email/{email}      (breach name list)
     breach_analytics(email)  -> GET /v1/breach-analytics?email=  (full detail; primary
                                                                    call for the panel)

   Rate limits are strict (documented as roughly 2/s, 25-50/hr, 100/day per
   IP), so every lookup goes through an on-disk, per-email TTL cache before
   any HTTP call is made, and transient failures back off via
   ``retry.with_retry`` rather than hammering the endpoint.

2. HIBP Pwned Passwords (password hygiene; no API key, k-anonymity)::

     password_pwned_count(password) -> SHA1(password), first 5 hex chars as
     the range prefix, GET /range/{prefix}; the remaining 35 hex chars are
     matched locally against the response body. Only the 5-char prefix ever
     leaves this process -- never the password, and never the full hash.

3. ``profile_exposure(emails)`` maps each profile email to its breach list --
   the shape the UI panel renders.

XposedOrNot's terms require a visible credit line wherever their data is
shown; ``XPOSEDORNOT_ATTRIBUTION`` is that string for the UI to surface.

Modeled on ``broker_guard/searx_client.py``'s conventions: an injectable
``session``/``sleep`` for tests, a permanent-vs-transient exception split so
retries never chase a 4xx, and PII (the email/password being checked) kept
out of every log line and exception message -- only exception class + a
truncated, PII-free reason are logged.

Note on ``breach_analytics``'s response shape: XposedOrNot does not publish
a formal schema. The summarizer here targets the commonly observed shape
(``ExposedBreaches.breaches_details`` + ``BreachMetrics.risk``) -- verify
against a live response before relying on it for anything user-facing;
``breach_analytics()`` itself always returns the raw parsed JSON untouched
so nothing is lost if the shape differs.
"""
import hashlib
import json
import logging
import os
import time

from broker_guard.retry import RetryExhausted, with_retry

log = logging.getLogger("broker_guard.exposure")

XPOSEDORNOT_ATTRIBUTION = "Data via XposedOrNot"

XPOSEDORNOT_BASE_URL = "https://api.xposedornot.com/v1"
HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"

DEFAULT_CACHE_PATH = "data/exposure_cache.json"
DEFAULT_CACHE_TTL_S = 24 * 60 * 60  # a day -- keeps a repeatedly-reloaded UI
                                     # panel from re-spending the 100/day quota

# Import lazily so the module can be imported (and unit-tested with an
# injected session) without `requests` installed -- same guard as searx_client.
try:  # pragma: no cover - trivial import guard
    import requests
    from requests.exceptions import RequestException

    _RETRYABLE = (RequestException,)
except ImportError:  # pragma: no cover
    requests = None
    RequestException = Exception
    _RETRYABLE = (OSError,)


class ExposureError(RuntimeError):
    """A transient exposure-lookup failure: unreachable, 5xx, or 429."""


class PermanentExposureError(RuntimeError):
    """A failure retrying cannot fix (4xx other than 429, unparseable body).

    Deliberately NOT a subclass of ExposureError -- the retry predicate
    matches on ExposureError, so a permanent failure must not be retried.
    """


def _safe_error(exc) -> str:
    """Exception type plus a short, PII-free reason.

    requests embeds the full request URL (which for check-email/breach-
    analytics IS the person's email address) in its exception messages --
    only the class name and a truncated first line are kept.
    """
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return "{}: {}".format(type(exc).__name__, first_line[:200])


class ExposureCache:
    """A tiny on-disk JSON cache, keyed by (kind, email), TTL-gated.

    One flat JSON file (default ``data/exposure_cache.json``, alongside
    ``data/state.sqlite``) mapping a cache key to ``{fetched_at, value}``.
    The cache file itself necessarily holds PII (an email plus what breach
    data was fetched for it) -- same as ``profile.local.json``/the state db
    already do locally; it is written owner-only (0600) and is never logged.
    """

    def __init__(self, path: str = DEFAULT_CACHE_PATH, ttl_seconds: int = DEFAULT_CACHE_TTL_S):
        self.path = path
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(kind: str, email: str) -> str:
        return f"{kind}:{(email or '').strip().lower()}"

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        """Best-effort write -- never raises.

        ``_load`` above has always degraded gracefully (``except (OSError,
        ValueError): return {}``); this didn't, so a filesystem/permissions
        problem on ``self.path`` (e.g. an unwritable working directory in a
        container) surfaced as an unhandled OSError all the way up through
        ``set()`` -> ``check_email``/``breach_analytics`` -> the ``/exposure``
        route as a 500. A cache write that fails just means the next lookup
        re-fetches instead of hitting the cache -- degraded, not broken.
        """
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            log.warning("exposure cache write failed", extra={"error": str(exc)})

    def get(self, kind: str, email: str):
        entry = self._load().get(self._key(kind, email))
        if not entry:
            return None
        if time.time() - entry.get("fetched_at", 0) > self.ttl_seconds:
            return None
        return entry.get("value")

    def set(self, kind: str, email: str, value) -> None:
        data = self._load()
        data[self._key(kind, email)] = {"fetched_at": time.time(), "value": value}
        self._save(data)


def _flatten_breach_names(raw) -> list[str]:
    """check-email's ``breaches`` key has been observed as either a flat
    list of names or a single-element list wrapping that list -- flatten
    defensively rather than assume one shape."""
    names: list[str] = []
    if raw is None:
        return names
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, list):
            names.extend(x for x in item if isinstance(x, str))
    return names


def summarize_breach_analytics(raw: dict) -> list[dict]:
    """Normalize a ``breach_analytics`` response into the list the exposure
    panel renders: ``{name, domain, year, industry, exposed_data, risk_score}``
    per breach. Best-effort against XposedOrNot's commonly observed shape
    (see module docstring) -- missing/renamed keys degrade gracefully to an
    empty list rather than raising, since this is a rendering convenience,
    not something correctness depends on.
    """
    if not isinstance(raw, dict):
        return []
    exposed = raw.get("ExposedBreaches") or {}
    details = exposed.get("breaches_details") if isinstance(exposed, dict) else None
    if not isinstance(details, list):
        return []

    risk_score = None
    metrics = raw.get("BreachMetrics") or {}
    risk_list = metrics.get("risk") if isinstance(metrics, dict) else None
    if isinstance(risk_list, list) and risk_list and isinstance(risk_list[0], dict):
        risk_score = risk_list[0].get("risk_score")

    out = []
    for item in details:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": item.get("breach"),
            "domain": item.get("domain"),
            "year": item.get("xposed_date"),
            "industry": item.get("industry"),
            "exposed_data": item.get("xposed_data"),
            "risk_score": risk_score,
        })
    return out


class XposedOrNotClient:
    """Callable-free client for the two XposedOrNot endpoints this module
    needs, with cache + retry/backoff built in."""

    def __init__(self, base_url: str = XPOSEDORNOT_BASE_URL, timeout_s: int = 20,
                 session=None, attempts: int = 3, base_delay: float = 2.0,
                 sleep=None, cache: "ExposureCache | None" = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.attempts = max(1, attempts)
        self.base_delay = base_delay
        self._sleep = sleep
        self.cache = cache if cache is not None else ExposureCache()
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:  # pragma: no cover
            raise PermanentExposureError("requests is not installed and no session was injected")

    def _headers(self) -> dict:
        return {"Accept": "application/json", "User-Agent": "broker-guard/1.0"}

    def _get(self, path: str, params: dict | None = None):
        """GET *path*, retrying transient failures. Returns the parsed JSON
        body, or None for a 404 (XposedOrNot's "nothing found" response --
        not an error). Never raises for a request-level failure; logs and
        returns None so one dead lookup doesn't abort a panel render."""
        url = f"{self.base_url}{path}"

        def _once():
            response = self.session.get(
                url, params=params, timeout=self.timeout_s, headers=self._headers()
            )
            status = getattr(response, "status_code", 200)
            if status == 404:
                return None
            if status >= 500 or status == 429:
                raise ExposureError(f"xposedornot returned HTTP {status}")
            if status >= 400:
                raise PermanentExposureError(f"xposedornot returned HTTP {status}")
            try:
                return response.json()
            except ValueError as exc:
                raise ExposureError(f"xposedornot returned non-JSON: {exc}") from exc

        kwargs = {"attempts": self.attempts, "base_delay": self.base_delay,
                  "retry_on": _RETRYABLE + (ExposureError,), "description": f"xposedornot{path}"}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            return with_retry(_once, **kwargs)
        except PermanentExposureError as exc:
            log.error("xposedornot rejected the request", extra={"error": _safe_error(exc)})
            return None
        except RetryExhausted as exc:
            log.error("xposedornot unreachable", extra={"error": _safe_error(exc.__cause__ or exc)})
            return None

    def check_email(self, email: str) -> list[str]:
        """Breach names *email* appears in (``[]`` if none/unreachable)."""
        cached = self.cache.get("check_email", email)
        if cached is not None:
            return cached
        raw = self._get(f"/check-email/{email}")
        names = _flatten_breach_names(raw.get("breaches")) if isinstance(raw, dict) else []
        self.cache.set("check_email", email, names)
        return names

    def breach_analytics(self, email: str) -> dict:
        """Full breach-analytics payload for *email* (``{}`` if none/unreachable).

        This is the primary call the exposure panel should use; pass its
        result through ``summarize_breach_analytics`` to get the panel's
        per-breach rows.
        """
        cached = self.cache.get("breach_analytics", email)
        if cached is not None:
            return cached
        raw = self._get("/breach-analytics", params={"email": email})
        result = raw if isinstance(raw, dict) else {}
        self.cache.set("breach_analytics", email, result)
        return result


def password_pwned_count(password: str, session=None, timeout_s: int = 20) -> int:
    """How many times *password* appears in HIBP's breach corpus.

    k-anonymity: only the first 5 hex characters of the SHA1 hash are ever
    sent to the API. The full password and the full hash never leave this
    process; the remaining 35 hex characters are matched locally against
    the returned suffix list.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]

    sess = session
    if sess is None:
        if requests is None:  # pragma: no cover
            raise PermanentExposureError("requests is not installed and no session was injected")
        sess = requests.Session()

    url = HIBP_RANGE_URL.format(prefix=prefix)
    try:
        response = sess.get(url, timeout=timeout_s, headers={"User-Agent": "broker-guard/1.0"})
    except _RETRYABLE as exc:
        log.error("hibp range lookup failed", extra={"error": _safe_error(exc)})
        return 0

    status = getattr(response, "status_code", 200)
    if status != 200:
        log.error("hibp range lookup failed", extra={"status": status})
        return 0

    text = getattr(response, "text", "") or ""
    for line in text.splitlines():
        line_suffix, _, count = line.strip().partition(":")
        if line_suffix.strip().upper() == suffix:
            try:
                return int(count.strip())
            except ValueError:
                return 0
    return 0


def profile_exposure(emails: list[str], client: "XposedOrNotClient | None" = None) -> dict:
    """Map each of *emails* to its breach name list -- what the UI panel
    iterates to render one row per email."""
    client = client or XposedOrNotClient()
    return {email: client.check_email(email) for email in emails if email}
