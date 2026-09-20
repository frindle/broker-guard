"""Single monitoring pass over a set of brokers for one identity."""


def run_cycle(identity_key: str, brokers: list[dict], presence_checker, state_conn, alert_sink, now_iso: str) -> dict:
    current = []
    new_appearances = []
    resolved = []

    for broker in brokers:
        if not presence_checker(broker, identity_key):
            continue
        broker_id = broker["id"]
        current.append(broker_id)
        if not state_conn.is_seen(identity_key, broker_id):
            state_conn.record_appearance(identity_key, broker_id, now_iso)
            new_appearances.append(broker_id)

    for seen_id in state_conn.seen_brokers(identity_key):
        if seen_id not in current:
            resolved.append(seen_id)

    alerts_sent = False
    if new_appearances:
        alert_sink({
            "identity_key": identity_key,
            "new_appearances": list(new_appearances),
            "now_iso": now_iso,
        })
        alerts_sent = True

    return {
        "current": current,
        "new_appearances": new_appearances,
        "resolved": resolved,
        "ran_at": now_iso,
    }
