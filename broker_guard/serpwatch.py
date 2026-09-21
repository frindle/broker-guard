"""SERP watch: run identity searches per broker and collect presence hits."""
from broker_guard import detection


def searxng_result_to_candidate(raw: dict) -> dict:
    """Map a raw SearXNG result to the candidate shape used by hit matching."""
    return {
        'title': raw.get('title', ''),
        'snippet': raw.get('content', ''),
        'url': raw.get('url', ''),
    }


def run_serpwatch(brokers: list[dict], phones: list[str], emails: list[str], name_variants: list[str], addresses: list[str], searx_search) -> list:
    queries = detection.build_search_queries(phones, emails, name_variants, addresses)
    terms = name_variants + phones + emails
    hits = []
    for broker in brokers:
        for query in queries:
            if query['kind'] == 'name_address':
                query_text = query['name'] + ' ' + query['address']
            else:
                query_text = query['value']
            record_key = query.get('value') or (query['name'] + '|' + query['address'])
            for raw in searx_search(query_text) or []:
                candidate = searxng_result_to_candidate(raw)
                if detection.is_people_search_hit(candidate, terms):
                    hits.append(detection.PresenceResult(broker['id'], record_key, [query['kind']]))
    return detection.dedupe_hits(hits)
