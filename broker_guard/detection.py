def build_search_queries(phones: list[str], emails: list[str], name_variants: list[str], addresses: list[str]) -> list[dict]:
    queries = []
    for phone in phones:
        query = {'kind':'phone','value': phone}
        if query not in queries:
            queries.append(query)
    for email in emails:
        query = {'kind':'email','value': email}
        if query not in queries:
            queries.append(query)
    for name_variant in name_variants:
        for address in addresses:
            query = {'kind':'name_address','name': name_variant,'address': address}
            if query not in queries:
                queries.append(query)
    return queries


def is_people_search_hit(result: dict, terms: list[str]) -> bool:
    haystack = (result.get("title", "") + result.get("snippet", "") + result.get("url", "")).lower()
    return any(term.lower() in haystack for term in terms)


from dataclasses import dataclass


@dataclass
class PresenceResult:
    broker_id: str
    record_key: str
    matched_on: list[str]


def dedupe_hits(hits: list[PresenceResult]) -> list[PresenceResult]:
    merged = {}
    order = []
    for hit in hits:
        key = (hit.broker_id, hit.record_key)
        if key not in merged:
            merged[key] = PresenceResult(hit.broker_id, hit.record_key, [])
            order.append(key)
        tags = merged[key].matched_on
        for tag in hit.matched_on:
            if tag not in tags:
                tags.append(tag)
    return [merged[key] for key in order]
