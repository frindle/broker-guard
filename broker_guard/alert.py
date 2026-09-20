"""Digest broker alert events into counts plus de-duplicated items."""


def batch_digest(events):
    """Group events by kind and de-duplicate (kind, broker_id) pairs.

    Returns {'counts': {kind: n}, 'items': [first-seen event per pair]}.
    Events missing 'kind' or 'broker_id' are treated as None for grouping
    and dedup; input dicts are never mutated.
    """
    counts = {}
    items = []
    seen = set()
    for event in events:
        kind = event.get("kind")
        broker_id = event.get("broker_id")
        counts[kind] = counts.get(kind, 0) + 1
        pair = (kind, broker_id)
        if pair not in seen:
            seen.add(pair)
            items.append(event)
    return {"counts": counts, "items": items}


def format_notification(digest: dict) -> dict:
    """Turn a batch_digest result into a Home Assistant / Obsidian notification.

    Returns {'title', 'message', 'ha', 'obsidian_md'}. An empty digest ({} or
    missing/None counts/items), or non-dict input, yields a benign
    "No new activity" notification with the same four-key shape. The digest is
    never mutated.
    """
    if not isinstance(digest, dict):
        digest = {}
    counts = digest.get("counts")
    items = digest.get("items")

    if counts is None or items is None:
        title = "No new activity"
        message = "No new broker alert activity."
        lines = []
    else:
        total = sum(counts.values())
        title = "{} new broker alert(s)".format(total)
        lines = []
        for item in items:
            if isinstance(item, dict):
                kind = item.get("kind")
                broker_id = item.get("broker_id")
            else:
                kind = "unknown"
                broker_id = "unknown"
            lines.append("- {} ({})".format(kind, broker_id))
        message = title + "\n\n" + "\n".join(lines)

    obsidian_md = "# {}\n\n{}".format(title, "".join(line + "\n" for line in lines))
    return {
        "title": title,
        "message": message,
        "ha": {"service": "notify.hass", "data": {"title": title, "message": message}},
        "obsidian_md": obsidian_md,
    }
