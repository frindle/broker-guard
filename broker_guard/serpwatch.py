"""SERP watch: run identity searches per broker and collect presence hits."""
import logging
from urllib.parse import urlsplit

from broker_guard import detection

log = logging.getLogger("broker_guard.serpwatch")


def searxng_result_to_candidate(raw: dict) -> dict:
    """Map a raw SearXNG result to the candidate shape used by hit matching."""
    if not isinstance(raw, dict):
        return {'title': '', 'snippet': '', 'url': ''}
    return {
        'title': raw.get('title', '') or '',
        'snippet': raw.get('content', '') or '',
        'url': raw.get('url', '') or '',
    }


def broker_domain(broker: dict) -> str:
    """The registrable-ish host for *broker*, lowercased and www-stripped.

    Returns '' when the broker has no usable url, which callers treat as
    "cannot scope to this broker".
    """
    url = (broker.get('url') or '').strip()
    if not url:
        return ''
    if '://' not in url:
        url = 'https://' + url
    host = urlsplit(url).hostname or ''
    host = host.lower()
    return host[4:] if host.startswith('www.') else host


def url_belongs_to(url: str, domain: str) -> bool:
    """True when *url*'s host is *domain* or a subdomain of it.

    Substring matching is deliberately avoided: ``evil-spokeo.com.attacker.net``
    must not count as a hit on ``spokeo.com``.
    """
    if not domain:
        return False
    host = (urlsplit(url or '').hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host == domain or host.endswith('.' + domain)


def build_query_text(query: dict, domain: str = '') -> str:
    """Render one query dict into the text handed to the search engine.

    When *domain* is given the query is scoped with a ``site:`` filter so each
    broker is searched on its own site rather than the whole web.
    """
    if query['kind'] == 'name_address':
        text = '"{}" "{}"'.format(query['name'], query['address'])
    else:
        text = '"{}"'.format(query['value'])
    return 'site:{} {}'.format(domain, text) if domain else text


def query_record_key(query: dict) -> str:
    if query['kind'] == 'name_address':
        return query['name'] + '|' + query['address']
    return query['value']


def _notify(observer, broker_id: str, outcome: str, hits: int = 0, errors: int = 0) -> None:
    """Report one broker's outcome, never letting the reporting itself fail.

    The observer is a progress counter / dashboard feed -- strictly
    telemetry. A bug in it must not be able to abort a scan that is
    otherwise working, which is the same reasoning the per-broker
    ``except`` below already applies to the search backend.
    """
    if observer is None:
        return
    try:
        observer(broker_id, outcome, hits, errors)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("serpwatch observer failed",
                    extra={"broker_id": broker_id, "outcome": outcome,
                           "error": "{}: {}".format(type(exc).__name__, exc)})


def run_serpwatch(
    brokers: list[dict],
    phones: list[str],
    emails: list[str],
    name_variants: list[str],
    addresses: list[str],
    searx_search,
    observer=None,
) -> list:
    """Search each broker's site for each identity query and return hits.

    Two correctness properties that the per-slice version did not have:

    * queries are scoped per broker with ``site:<broker domain>``, so the same
      unscoped query is not repeated once per broker; and
    * a result only counts as a hit for a broker when the result URL actually
      lives on that broker's domain. Previously ANY matching result anywhere on
      the web was recorded against EVERY broker in the list, which reported the
      whole broker set as "present" the moment one page matched.

    A broker with no usable url is skipped rather than searched unscoped.
    ``searx_search`` raising for one query does not abort the run; that query
    is skipped and the rest continue.

    Errors are COUNTED, not swallowed
    ----------------------------------
    The per-query ``except`` below still continues past a failure -- one
    dead broker must never cost the cycle -- but it no longer pretends the
    failure did not happen. Previously a bare ``except Exception: continue``
    made "searched this broker, found nothing" and "every query against this
    broker blew up" completely indistinguishable, both downstream and in the
    logs. When SearXNG's upstream engines get themselves rate-limited or
    CAPTCHA-walled, that turned a total detection outage into a confident,
    silent ``hit_brokers: 0``.

    Each failure is now logged (PII-free: the broker id and the exception
    class only -- NEVER the query text, which is the person's name, phone or
    email) and reported to *observer*, so the caller can tell 827 clean
    checks from 827 failed ones.

    ``observer`` is an optional ``(broker_id, outcome, hits, errors)``
    callable invoked exactly once per broker, where *outcome* is one of
    ``'hit'``, ``'checked'``, ``'error'`` or ``'skipped'`` (see
    ``broker_guard.progress``). A broker whose queries partly failed and
    partly matched is reported as ``'hit'`` -- a positive is a positive; a
    broker with ANY failed query and NO match is reported as ``'error'``,
    because "some of the evidence never arrived" is genuinely unknown, not
    absent.
    """
    queries = detection.build_search_queries(phones, emails, name_variants, addresses)
    terms = list(name_variants) + list(phones) + list(emails) + list(addresses)
    hits = []
    for broker in brokers:
        broker_id = broker.get('id')
        domain = broker_domain(broker)
        if not domain:
            _notify(observer, broker_id, 'skipped')
            continue
        broker_hits = 0
        broker_errors = 0
        for query in queries:
            try:
                raw_results = searx_search(build_query_text(query, domain)) or []
            except Exception as exc:
                # A single failing query must not lose the whole cycle -- but
                # it MUST be visible. Only the exception class and a
                # truncated first line are logged; the query text is PII.
                broker_errors += 1
                # ONLY the exception class -- never str(exc). The search
                # backend is handed the query, and the query IS the
                # person's name/phone/email, so an arbitrary backend's
                # exception message is assumed to contain it (requests, for
                # one, embeds the full request URL in its errors). The
                # client itself logs a PII-scrubbed reason for the same
                # failure, so no diagnostic detail is actually lost here.
                log.warning("serpwatch query failed", extra={
                    "broker_id": broker_id,
                    "query_kind": query.get('kind'),
                    "error": type(exc).__name__,
                })
                continue
            for raw in raw_results:
                candidate = searxng_result_to_candidate(raw)
                if not url_belongs_to(candidate['url'], domain):
                    continue
                if detection.is_people_search_hit(candidate, terms):
                    broker_hits += 1
                    hits.append(
                        detection.PresenceResult(
                            broker['id'], query_record_key(query), [query['kind']]
                        )
                    )
        if broker_hits:
            outcome = 'hit'
        elif broker_errors:
            outcome = 'error'
        else:
            outcome = 'checked'
        _notify(observer, broker_id, outcome, broker_hits, broker_errors)
    return detection.dedupe_hits(hits)
