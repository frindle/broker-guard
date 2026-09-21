"""Single monitoring pass over a set of brokers for one identity."""


def run_cycle(identity_key: str, brokers: list[dict], presence_checker, state_conn, alert_sink, now_iso: str) -> dict:
    """Check every broker, diff against stored state and alert on what is new.

    ``state_conn`` is anything implementing ``is_seen`` / ``record_appearance``
    / ``seen_brokers`` -- in production ``state.StateStore``.

    A ``presence_checker`` that raises for one broker is treated as "unknown",
    not as "absent": the broker is left in whatever state it was already in and
    is NOT reported as resolved, because a transient network error must never
    look like a successful removal. Errored brokers are reported separately
    under ``errors``.
    """
    current = []
    new_appearances = []
    resolved = []
    errors = []

    for broker in brokers:
        broker_id = broker["id"]
        try:
            present = presence_checker(broker, identity_key)
        except Exception as exc:
            errors.append({"broker_id": broker_id, "error": "{}: {}".format(type(exc).__name__, exc)})
            continue
        if not present:
            continue
        current.append(broker_id)
        if not state_conn.is_seen(identity_key, broker_id):
            state_conn.record_appearance(identity_key, broker_id, now_iso)
            new_appearances.append(broker_id)
        else:
            # Advance last_seen so staleness of a listing is knowable.
            touch = getattr(state_conn, "touch", None)
            if callable(touch):
                touch(identity_key, broker_id, now_iso)

    errored_ids = {e["broker_id"] for e in errors}
    for seen_id in state_conn.seen_brokers(identity_key):
        if seen_id not in current and seen_id not in errored_ids:
            resolved.append(seen_id)

    alerts_sent = False
    if new_appearances or resolved:
        alert_sink({
            "identity_key": identity_key,
            "new_appearances": list(new_appearances),
            "resolved": list(resolved),
            "now_iso": now_iso,
        })
        alerts_sent = True

    return {
        "identity_key": identity_key,
        "current": current,
        "new_appearances": new_appearances,
        "resolved": resolved,
        "errors": errors,
        "alerts_sent": alerts_sent,
        "ran_at": now_iso,
    }
