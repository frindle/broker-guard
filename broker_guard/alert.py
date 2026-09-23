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
    for event in events or []:
        if not isinstance(event, dict):
            # A non-dict event used to raise AttributeError and lose the whole
            # digest; group it under (None, None) like any other unlabelled one.
            kind = broker_id = None
        else:
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
        total = sum(counts.values()) if isinstance(counts, dict) else 0
        title = "{} new broker alert(s)".format(total)
        lines = []
        for item in items:
            if isinstance(item, dict):
                kind = item.get("kind")
                broker_id = item.get("broker_id")
            else:
                kind = "unknown"
                broker_id = "unknown"
            line = "- {} ({})".format(kind, broker_id)
            # A recipe_drift item is the one kind whose whole value is in the
            # detail: "recipe_drift (spokeo-com)" tells a reader nothing they
            # can act on, while "[search] could not fill 'Last Name'" names
            # the selector to go and re-read. Kept to the same one-line shape
            # so an existing consumer of this text is not reformatted.
            if isinstance(item, dict) and kind == "recipe_drift":
                extra = " ".join(part for part in (
                    "[{}]".format(item.get("leg")) if item.get("leg") else "",
                    str(item.get("detail") or "").strip(),
                ) if part)
                if extra:
                    line = "{}: {}".format(line, extra)
            lines.append(line)
        message = title + "\n\n" + "\n".join(lines)

    obsidian_md = "# {}\n\n{}".format(title, "".join(line + "\n" for line in lines))
    return {
        "title": title,
        "message": message,
        "ha": {"service": "notify.hass", "data": {"title": title, "message": message}},
        "obsidian_md": obsidian_md,
    }


def events_from_cycle(cycle_result: dict) -> list[dict]:
    """Convert an ``orchestrator.run_cycle`` result into ``batch_digest`` events.

    ``run_cycle`` hands its ``alert_sink`` a single
    ``{identity_key, new_appearances, now_iso}`` payload, but ``batch_digest``
    consumes a LIST of ``{kind, broker_id}`` events. The two slices were
    authored against different shapes; this is the adapter between them.

    Both ``new_appearances`` and ``resolved`` are emitted, so a digest can
    report removals landing as well as new listings appearing.

    A third kind, ``recipe_drift``, is derived from the payload's ``errors``
    -- but only from the ones ``recipe_health`` calls STRUCTURAL, i.e. "the
    broker's page is not shaped the way our recipe says". The other error
    classes (a timeout, a bot wall, a profile that cannot fill a required
    field) are deliberately dropped here rather than filtered downstream, so
    there is exactly one place that decides what an errored broker is worth
    telling somebody about. A payload may also carry ready-made
    ``recipe_drift`` events directly -- that is how the opt-out leg, which
    never goes through ``run_cycle``, reaches the same sinks.
    """
    if not isinstance(cycle_result, dict):
        return []
    from broker_guard import recipe_health

    now_iso = cycle_result.get("now_iso") or cycle_result.get("ran_at")
    identity_key = cycle_result.get("identity_key")
    events = []
    for kind, field in (("new_appearance", "new_appearances"), ("resolved", "resolved")):
        for broker_id in cycle_result.get(field) or []:
            events.append(
                {
                    "kind": kind,
                    "broker_id": broker_id,
                    "identity_key": identity_key,
                    "at": now_iso,
                }
            )
    events.extend(recipe_health.drift_events_from_errors(
        cycle_result.get("errors"), leg=recipe_health.LEG_SEARCH,
        at=now_iso, identity_key=identity_key))
    for event in cycle_result.get("recipe_drift") or []:
        if isinstance(event, dict):
            events.append(event)
    return events
