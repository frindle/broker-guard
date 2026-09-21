"""Structured JSON logging with PII redaction.

Logs go to stdout (for `docker logs`) and, when a log dir is configured, to a
rotating file on the mounted volume. Nothing leaves the host: there is no
remote handler here by design, and adding one would defeat the point of a
self-hosted privacy tool.
"""
import json
import logging
import logging.handlers
import os
import re

# Redaction patterns applied to every rendered message and every string field.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 7+ digits with optional separators: covers both 7-digit subscriber numbers
# and full E.164. Timestamps would also match, which is why the formatter
# excludes the 'ts' field from redaction rather than loosening this further.
_PHONE_RE = re.compile(r"(?<!\w)\+?(?:\d[\s().-]?){6,14}\d(?!\w)")
# A URL query string carries the SEARCH TERMS -- i.e. the person's name,
# email and address, percent-encoded so the email/phone patterns above miss
# them. requests/urllib3 put the full URL in their exception text, so this is
# the pattern that actually keeps identity out of the error logs.
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>]*?)\?[^\s\"'<>]*")
# urllib3/requests report a RELATIVE url ("with url: /search?q=...") inside
# their exception text, which the absolute pattern above does not match.
_PATH_QUERY_RE = re.compile(r"(?<![\w:])(/[^\s\"'<>?]*)\?[^\s\"'<>]*")
# Belt and braces: any search-term parameter, wherever it turns up.
_QUERY_PARAM_RE = re.compile(r"\b(q|query|search|search_query)=[^\s&\"'<>]+", re.I)

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


def redact(value):
    """Replace anything that looks like an email or phone number with a marker."""
    if isinstance(value, str):
        value = _URL_QUERY_RE.sub(r"\1?<redacted-query>", value)
        value = _PATH_QUERY_RE.sub(r"\1?<redacted-query>", value)
        value = _QUERY_PARAM_RE.sub(r"\1=<redacted>", value)
        value = _EMAIL_RE.sub("<email>", value)
        return _PHONE_RE.sub("<phone>", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object, optionally redacting PII."""

    def __init__(self, redact_pii: bool = True):
        super().__init__()
        self.redact_pii = redact_pii

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if self.redact_pii:
            # 'ts' is machine-generated and contains no PII, but an ISO date
            # is digit-shaped enough to trip the phone pattern -- redacting it
            # would make the logs useless.
            ts = payload.pop("ts")
            payload = redact(payload)
            payload["ts"] = ts
        try:
            return json.dumps(payload, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "logger": payload["logger"], "msg": str(payload["msg"])})


def setup_logging(level: str = "INFO", log_dir: str | None = None,
                  log_pii: bool = False, filename: str = "broker-guard.jsonl") -> logging.Logger:
    """Install the JSON handlers on the root logger and return broker-guard's.

    Idempotent: repeated calls replace the handlers rather than stacking them.
    *log_pii* must stay False unless someone is actively debugging a match.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = JsonFormatter(redact_pii=not log_pii)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, filename)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            # A read-only or missing log volume must not stop the run; stdout
            # logging still works.
            root.warning("file logging disabled", extra={"log_dir": log_dir, "error": str(exc)})

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # urllib3 logs full request URLs at DEBUG, which would carry query PII.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("broker_guard")
