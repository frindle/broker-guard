"""Real SearXNG HTTP client used as ``serpwatch``'s ``searx_search`` callable.

Queries carry the person's name/phone/email, so this only ever talks to the
SELF-HOSTED SearXNG instance named in ``BG_SEARXNG_URL``. The host is pinned at
construction time and every request is built against it -- a broker-supplied
string can never redirect the call somewhere else.

Two further properties this client owns, both learned the hard way:

* **Pacing.** Requests are spaced by ``min_interval_s`` plus jitter (see
  ``_wait_for_slot``). A cycle is thousands of queries; unpaced, they got the
  instance's upstream engines rate-limited and CAPTCHA-walled for days.
* **Honest failures.** ``__call__`` RAISES when a search did not happen,
  rather than returning ``[]`` -- a value indistinguishable from a successful
  search of someone who is not listed. ``serpwatch.run_serpwatch`` owns "one
  failure must not abort the cycle", and now counts what it catches.
"""
import logging
import random
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from broker_guard.retry import RetryExhausted, with_retry

log = logging.getLogger("broker_guard.searx")

# Seconds to leave between two successive requests to SearXNG, plus a
# uniform 0..DEFAULT_JITTER_S on top. See SearxClient._wait_for_slot for
# why this exists and why the default is this conservative.
DEFAULT_MIN_INTERVAL_S = 2.0
DEFAULT_JITTER_S = 1.0

# Transport-level failures worth retrying. Import lazily so the module can be
# imported (and unit-tested with an injected session) without `requests`.
try:  # pragma: no cover - trivial import guard
    import requests
    from requests.exceptions import RequestException

    _RETRYABLE = (RequestException,)
except ImportError:  # pragma: no cover
    requests = None
    RequestException = Exception
    _RETRYABLE = (OSError,)


def _safe_error(exc) -> str:
    """Exception type plus a short reason -- never the full text.

    requests embeds the complete request URL in its exception messages, and
    for this client that URL's query string IS the person's name/email. Only
    the class name and first line are kept, and the formatter's redaction is a
    second line of defence.
    """
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return "{}: {}".format(type(exc).__name__, first_line[:200])


class SearxError(RuntimeError):
    """A transient SearXNG failure: unreachable, 5xx, 429 or unparseable."""


class PermanentSearxError(RuntimeError):
    """A SearXNG failure retrying cannot fix (4xx other than 429, bad config).

    Deliberately NOT a subclass of SearxError: the retry predicate matches on
    SearxError, so a permanent failure must not be caught by it.
    """


class SearxClient:
    """Callable returning a list of raw SearXNG result dicts for a query.

    Instances are callable so they can be passed straight to
    ``serpwatch.run_serpwatch(..., searx_search=client)``.
    """

    def __init__(self, base_url: str, timeout_s: int = 20, session=None,
                 auth: str | None = None, engines: str | None = None,
                 attempts: int = 3, base_delay: float = 1.0, sleep=None,
                 max_results: int = 25,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 jitter_s: float = DEFAULT_JITTER_S,
                 monotonic=None, rng=None):
        if not base_url:
            raise PermanentSearxError("BG_SEARXNG_URL is not set; serpwatch cannot run")
        parts = urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PermanentSearxError(f"BG_SEARXNG_URL must be an http(s) URL, got {base_url!r}")
        # Rebuild from parsed components: drops any query/fragment a caller may
        # have appended and fixes the endpoint we will hit.
        path = parts.path.rstrip("/")
        if not path.endswith("/search"):
            path = path + "/search"
        self.search_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        self.timeout_s = timeout_s
        self.auth = auth
        self.engines = engines
        self.attempts = max(1, attempts)
        self.base_delay = base_delay
        self.max_results = max_results
        self._sleep = sleep
        # Pacing state. `_sleep_fn` is the same injected `sleep` the retry
        # path uses (tests pass `lambda _: None`), falling back to the real
        # clock. `_monotonic` is separate from wall time so a system clock
        # step cannot make the pacer think an hour has passed.
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.jitter_s = max(0.0, float(jitter_s))
        self._sleep_fn = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic or time.monotonic
        self._rng = rng or random
        self._pace_lock = threading.Lock()
        self._last_request_at = None
        if session is not None:
            self.session = session
        elif requests is not None:
            self.session = requests.Session()
        else:  # pragma: no cover
            raise PermanentSearxError("requests is not installed and no session was injected")

    def _headers(self) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "broker-guard/1.0"}
        if self.auth:
            headers["Authorization"] = self.auth
        return headers

    def _wait_for_slot(self) -> None:
        """Block until at least ``min_interval_s`` (+ jitter) has passed
        since the previous request left this client.

        Why: ``retry.with_retry`` only ever sleeps AFTER a failure, so it
        never spaced out two *successful* calls. A full cycle is ~827
        brokers x one query per identity value, and it fired every one of
        them back to back as fast as network RTT allowed -- thousands of
        requests in a few minutes. Self-hosted SearXNG forwards those to
        real upstream engines (Brave, Google CSE, DuckDuckGo, Startpage,
        Wikipedia), which respond to that volume by rate-limiting or
        CAPTCHA-walling the instance for days. The instance then returns
        nothing for every query, which -- before the error counting added
        alongside this -- looked exactly like "you are not listed anywhere".

        Pacing lives here, at the single point where an HTTP request is
        actually issued, rather than in ``run_serpwatch``: it then also
        covers retries and any future caller, and it cannot be bypassed by
        a code path that builds its own query loop.

        It deliberately measures from the last request rather than sleeping
        unconditionally, so the backoff ``with_retry`` already performed
        counts toward the gap instead of being paid twice.

        Jitter (uniform 0..``jitter_s``) keeps a long run from settling into
        a perfectly periodic request train, which is itself a bot signal.
        """
        if self.min_interval_s <= 0 and self.jitter_s <= 0:
            return
        with self._pace_lock:
            now = self._monotonic()
            if self._last_request_at is not None:
                target_gap = self.min_interval_s
                if self.jitter_s > 0:
                    target_gap += self._rng.uniform(0, self.jitter_s)
                remaining = target_gap - (now - self._last_request_at)
                if remaining > 0:
                    self._sleep_fn(remaining)
                    now = self._monotonic()
            self._last_request_at = now

    def _once(self, query: str) -> list[dict]:
        self._wait_for_slot()
        params = {"q": query, "format": "json", "safesearch": "0"}
        if self.engines:
            params["engines"] = self.engines
        # params= -> proper URL encoding; the query is never string-concatenated
        # into the URL, so a query containing & or # cannot inject parameters.
        response = self.session.get(
            self.search_url, params=params, timeout=self.timeout_s, headers=self._headers()
        )
        status = getattr(response, "status_code", 200)
        if status >= 500 or status == 429:
            # Transient: let with_retry see it.
            raise SearxError(f"searxng returned HTTP {status}")
        if status >= 400:
            # Permanent (bad request, auth): do not burn retries on it.
            raise PermanentSearxError(f"searxng returned HTTP {status}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearxError(f"searxng returned non-JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SearxError("searxng response was not a JSON object")
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)][: self.max_results]

    def __call__(self, query: str) -> list[dict]:
        """Search, retrying transient failures; RAISES if all attempts fail.

        This used to return ``[]`` on failure "so one dead query cannot abort
        the cycle". The intent was right, the placement was wrong: an empty
        list is the *same value* a successful search of a person who is not
        listed returns, so the caller could not tell the two apart, and
        neither could anything downstream. When SearXNG's upstream engines
        got rate-limited, every one of ~827 brokers came back ``[]`` and the
        cycle logged a confident ``hit_brokers: 0`` -- a total detection
        outage reported as a clean bill of health.

        The "never abort the cycle" property has NOT been given up: it is
        enforced one level up, in ``serpwatch.run_serpwatch``, whose
        per-broker ``except`` still continues past a failure -- and now
        counts it. Raising here is what lets it count.

        A failure is logged before it propagates, without the query text
        (which is the person's name/phone/email).
        """
        kwargs = {"attempts": self.attempts, "base_delay": self.base_delay,
                  "retry_on": _RETRYABLE + (SearxError,), "description": "searxng.search"}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            return with_retry(lambda: self._once(query), **kwargs)
        except PermanentSearxError as exc:
            log.error("searxng rejected the request", extra={"error": _safe_error(exc)})
            raise
        except RetryExhausted as exc:
            cause = exc.__cause__ or exc
            log.error("searxng unreachable", extra={"error": _safe_error(cause)})
            # Re-raised as SearxError rather than RetryExhausted so callers
            # can catch one meaningful type for "this search did not happen".
            raise SearxError(
                "searxng search failed after {} attempt(s): {}".format(
                    self.attempts, _safe_error(cause)
                )
            ) from cause
