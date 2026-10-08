"""Phone notifications for opt-out state changes (ntfy + the alert log).

broker-guard is send-only: it never reads Penn's inbox. Anything that needs a
human -- a confirmation link a broker emailed him, a CAPTCHA, a form that
needs hands, a listing that came back -- arrives as one of these
notifications, each carrying a tap-through deep link to the relevant
dashboard item (``/review#attempt-<id>`` when an attempt record exists,
``/optouts#opt-<broker>`` otherwise) when ``Config.public_base_url`` is set.

Notification bodies name the BROKER and a short reason only. No profile
field value ever goes into one: they are pushed through a self-hosted server,
but they also land in ``alerts.jsonl``.
"""
import logging

from broker_guard import optouts

log = logging.getLogger("broker_guard.notify")

# ntfy priorities: 1 min .. 5 urgent.
_KINDS = {
    optouts.NEEDS_USER: ("Needs you", 4, ["warning"]),
    optouts.AWAITING_USER_CONFIRM: ("Click the confirmation email", 3, ["email"]),
    optouts.RELISTED: ("Listed again", 3, ["repeat"]),
    optouts.REMOVED: ("Removed", 2, ["white_check_mark"]),
    optouts.FAILED: ("Opt-out failed", 3, ["x"]),
    "digest": ("Waiting on your click", 3, ["email"]),
    "captcha": ("Solve a CAPTCHA", 5, ["rotating_light"]),
}


def link_for(row: dict, base_url: str | None, path: str | None = None) -> str | None:
    """Deep link for *row*, or ``None`` without a configured public URL."""
    if not base_url:
        return None
    base = base_url.rstrip("/")
    if path:
        return base + path
    if row.get("last_attempt_id"):
        return "{}/review#attempt-{}".format(base, row["last_attempt_id"])
    return "{}/optouts#opt-{}".format(base, row.get("broker_id", ""))


def build_notification(kind: str, row: dict, broker_name: str = "",
                       base_url: str | None = None, detail: str = "",
                       path: str | None = None) -> dict:
    head, priority, tags = _KINDS.get(kind, (kind, 3, []))
    name = broker_name or row.get("broker_id") or "a broker"
    lines = []
    if kind == optouts.AWAITING_USER_CONFIRM:
        sender = row.get("expected_sender") or ""
        lines.append("{} should email you a confirmation link{}. Click it, then "
                     "tap done in the dashboard.".format(
                         name, " (sender: {})".format(sender) if sender else ""))
    elif kind == optouts.NEEDS_USER:
        lines.append("{} needs a human.{}".format(
            name, " " + (detail or row.get("last_reason") or "").strip()))
    elif kind == optouts.RELISTED:
        lines.append("{} is listing you again; a new opt-out is queued.".format(name))
    elif kind == optouts.REMOVED:
        lines.append("{} no longer lists you.".format(name))
    else:
        lines.append(("{}. {}".format(name, detail or row.get("last_reason") or "")).strip())
    click = link_for(row, base_url, path)
    ntfy = {"priority": priority, "tags": list(tags)}
    if click:
        ntfy["click"] = click
    return {
        "kind": "optout_" + kind,
        "title": "{}: {}".format(head, name),
        "message": " ".join(lines).strip(),
        "broker_id": row.get("broker_id"),
        "ntfy": ntfy,
    }


def build_digest(rows: list, names: dict | None = None,
                 base_url: str | None = None) -> dict | None:
    """The daily 'awaiting your confirmation click' list, or ``None`` if empty."""
    if not rows:
        return None
    names = names or {}
    lines = []
    for row in rows:
        name = names.get(row["broker_id"]) or row["broker_id"]
        sender = row.get("expected_sender")
        lines.append("- {}{}".format(
            name, " (email from {})".format(sender) if sender else ""))
    click = link_for({}, base_url, "/optouts")
    ntfy = {"priority": 3, "tags": list(_KINDS["digest"][2])}
    if click:
        ntfy["click"] = click
    return {
        "kind": "optout_digest",
        "title": "{}: {} item(s)".format(_KINDS["digest"][0], len(rows)),
        "message": "\n".join(lines),
        "ntfy": ntfy,
    }


class OptoutNotifier:
    """Fan one notification out to every sink; never raises."""

    def __init__(self, sinks, base_url: str | None = None, names: dict | None = None):
        self.sinks = [s for s in sinks if s is not None]
        self.base_url = base_url
        self.names = names or {}

    def send(self, notification: dict) -> int:
        delivered = 0
        for sink in self.sinks:
            try:
                if sink(notification):
                    delivered += 1
            except Exception as exc:
                log.error("notification sink raised", extra={
                    "sink": type(sink).__name__, "error": type(exc).__name__})
        return delivered

    def state_changed(self, kind: str, row: dict | None, detail: str = "") -> int:
        if not row:
            return 0
        name = self.names.get(row.get("broker_id"), "")
        return self.send(build_notification(kind, row, name, self.base_url, detail))

    def digest(self, rows: list) -> int:
        note = build_digest(rows, self.names, self.base_url)
        return self.send(note) if note else 0


def build_notifier(cfg, brokers: list | None = None) -> OptoutNotifier:
    from broker_guard import sinks

    names = {b.get("id"): b.get("name") for b in (brokers or []) if b.get("id")}
    return OptoutNotifier(
        [sinks.FileAlertSink(cfg.alert_log_path), sinks.build_ntfy_sink(cfg)],
        base_url=getattr(cfg, "public_base_url", None), names=names)
