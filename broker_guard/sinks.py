"""Alert sinks: where a cycle's digest actually goes.

Two sinks ship: an append-only JSON-lines file on the state volume, and an
optional webhook to a URL the operator supplies (a Home Assistant webhook, an
ntfy topic, etc.). Both are LOCAL/self-chosen by design -- there is no default
endpoint, nothing is sent anywhere unless BG_ALERT_WEBHOOK_URL is set, and no
third-party analytics exist in this file.
"""
import json
import logging
import os

from broker_guard import alert
from broker_guard.retry import RetryExhausted, with_retry

log = logging.getLogger("broker_guard.alert")

try:  # pragma: no cover
    import requests
    from requests.exceptions import RequestException

    _RETRYABLE = (RequestException,)
except ImportError:  # pragma: no cover
    requests = None
    _RETRYABLE = (OSError,)


class FileAlertSink:
    """Append each notification to a JSON-lines file, one object per line."""

    def __init__(self, path: str):
        self.path = path

    def __call__(self, notification: dict) -> bool:
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(notification, default=str) + "\n")
            try:
                os.chmod(self.path, 0o600)  # the digest names brokers: PII.
            except OSError:
                pass
            return True
        except OSError as exc:
            log.error("alert file write failed", extra={"path": self.path, "error": str(exc)})
            return False


class WebhookAlertSink:
    """POST the notification JSON to an operator-supplied webhook URL."""

    def __init__(self, url: str, timeout_s: int = 10, session=None,
                 attempts: int = 3, sleep=None):
        self.url = url
        self.timeout_s = timeout_s
        self.attempts = attempts
        self._sleep = sleep
        self.session = session or (requests.Session() if requests else None)

    def __call__(self, notification: dict) -> bool:
        if not self.url or self.session is None:
            return False

        def post():
            response = self.session.post(
                self.url, json=notification, timeout=self.timeout_s,
                headers={"User-Agent": "broker-guard/1.0"},
            )
            status = getattr(response, "status_code", 200)
            if status >= 400:
                raise RuntimeError(f"webhook returned HTTP {status}")
            return True

        kwargs = {"attempts": self.attempts, "retry_on": _RETRYABLE + (RuntimeError,),
                  "description": "alert.webhook"}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            return with_retry(post, **kwargs)
        except RetryExhausted as exc:
            # Never log the URL itself -- it is a bearer credential.
            log.error("alert webhook failed", extra={"error": str(exc.__cause__ or exc)})
            return False


class CompositeAlertSink:
    """The ``alert_sink`` handed to ``orchestrator.run_cycle``.

    ``run_cycle`` calls this with a raw cycle payload; this turns it into a
    digest + notification via ``alert.events_from_cycle`` /
    ``alert.batch_digest`` / ``alert.format_notification`` and fans it out to
    every configured sink. A sink failing is logged, never raised -- losing a
    notification must not lose the state write that came before it.
    """

    def __init__(self, sinks, drift_ledger=None):
        self.sinks = [s for s in sinks if s is not None]
        self.last_notification = None
        # Optional ``recipe_health.DriftLedger``. Its only job is to keep a
        # recipe that has been broken for a week from re-notifying every
        # cycle for a week -- see that class's docstring.
        self.drift_ledger = drift_ledger

    def _apply_drift_ledger(self, payload: dict, events: list) -> list:
        """Drop repeat ``recipe_drift`` events; forget brokers that recovered.

        Order matters: brokers that were healthy THIS cycle are cleared
        first, so a broker that broke, was fixed, and broke again alerts the
        second time too. A broker that merely errored transiently is neither
        cleared nor reported -- it is not in this cycle's drift events and
        not in ``current`` either, so its ledger entry (if any) stands.
        """
        ledger = self.drift_ledger
        if ledger is None:
            return events
        from broker_guard import recipe_health

        drifted = {e.get("broker_id") for e in events
                   if isinstance(e, dict) and e.get("kind") == recipe_health.KIND}
        for broker_id in (payload.get("current") or []) if isinstance(payload, dict) else []:
            if broker_id not in drifted:
                ledger.clear(broker_id, recipe_health.LEG_SEARCH)

        keep, drift = [], []
        for event in events:
            if isinstance(event, dict) and event.get("kind") == recipe_health.KIND:
                drift.append(event)
            else:
                keep.append(event)
        return keep + ledger.filter_new(drift)

    def __call__(self, cycle_payload: dict) -> dict:
        events = self._apply_drift_ledger(
            cycle_payload if isinstance(cycle_payload, dict) else {},
            alert.events_from_cycle(cycle_payload))
        if not events:
            # Nothing happened that anyone asked to hear about. Returning
            # before delivery keeps "no new activity" out of the alert log:
            # ``run_cycle`` now calls this whenever a broker ERRORED, and
            # most errors are transient by design (see recipe_health), so
            # without this a quiet night would write one empty notification
            # per cycle forever.
            return {"delivered": 0, "notification": None}
        digest = alert.batch_digest(events)
        notification = alert.format_notification(digest)
        notification["counts"] = digest["counts"]
        notification["items"] = digest["items"]
        self.last_notification = notification
        delivered = 0
        for sink in self.sinks:
            try:
                if sink(notification):
                    delivered += 1
            except Exception as exc:
                log.error("alert sink raised", extra={
                    "sink": type(sink).__name__,
                    "error": "{}: {}".format(type(exc).__name__, exc),
                })
        log.info("alert dispatched", extra={"delivered": delivered,
                                            "sinks": len(self.sinks),
                                            "counts": digest["counts"]})
        return {"delivered": delivered, "notification": notification}


def build_alert_sink(cfg) -> CompositeAlertSink:
    from broker_guard.recipe_health import DriftLedger

    sinks = [FileAlertSink(cfg.alert_log_path)]
    if cfg.alert_webhook_url:
        sinks.append(WebhookAlertSink(cfg.alert_webhook_url, attempts=cfg.max_retries or 1))
    # No new destination: recipe-drift alerts ride the SAME two sinks (the
    # JSON-lines alert log, and the operator's webhook if they set
    # BG_ALERT_WEBHOOK_URL). The ledger only decides how OFTEN they are sent.
    path = getattr(cfg, "recipe_drift_path", "") or ""
    return CompositeAlertSink(sinks, drift_ledger=DriftLedger(path) if path else None)
