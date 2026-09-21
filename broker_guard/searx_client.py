"""Real SearXNG HTTP client used as ``serpwatch``'s ``searx_search`` callable.

Queries carry the person's name/phone/email, so this only ever talks to the
SELF-HOSTED SearXNG instance named in ``BG_SEARXNG_URL``. The host is pinned at
construction time and every request is built against it -- a broker-supplied
string can never redirect the call somewhere else.
"""
import logging
from urllib.parse import urlsplit, urlunsplit

from broker_guard.retry import RetryExhausted, with_retry

log = logging.getLogger("broker_guard.searx")

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
                 max_results: int = 25):
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

    def _once(self, query: str) -> list[dict]:
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
        """Search, retrying transient failures; returns [] if all attempts fail.

        Returning [] rather than raising keeps one dead query from aborting the
        whole cycle -- the failure is logged (without the query text, which is
        PII) and the run continues.
        """
        kwargs = {"attempts": self.attempts, "base_delay": self.base_delay,
                  "retry_on": _RETRYABLE + (SearxError,), "description": "searxng.search"}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            return with_retry(lambda: self._once(query), **kwargs)
        except PermanentSearxError as exc:
            log.error("searxng rejected the request", extra={"error": _safe_error(exc)})
            return []
        except RetryExhausted as exc:
            log.error("searxng unreachable", extra={"error": _safe_error(exc.__cause__ or exc)})
            return []
