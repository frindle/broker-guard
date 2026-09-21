"""SERP watch: run identity searches per broker and collect presence hits."""
from urllib.parse import urlsplit

from broker_guard import detection


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


def run_serpwatch(
    brokers: list[dict],
    phones: list[str],
    emails: list[str],
    name_variants: list[str],
    addresses: list[str],
    searx_search,
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
    """
    queries = detection.build_search_queries(phones, emails, name_variants, addresses)
    terms = list(name_variants) + list(phones) + list(emails) + list(addresses)
    hits = []
    for broker in brokers:
        domain = broker_domain(broker)
        if not domain:
            continue
        for query in queries:
            try:
                raw_results = searx_search(build_query_text(query, domain)) or []
            except Exception:
                # A single failing query must not lose the whole cycle; the
                # caller's search client is responsible for logging/retrying.
                continue
            for raw in raw_results:
                candidate = searxng_result_to_candidate(raw)
                if not url_belongs_to(candidate['url'], domain):
                    continue
                if detection.is_people_search_hit(candidate, terms):
                    hits.append(
                        detection.PresenceResult(
                            broker['id'], query_record_key(query), [query['kind']]
                        )
                    )
    return detection.dedupe_hits(hits)
