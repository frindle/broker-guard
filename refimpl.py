#!/usr/bin/env python3
"""Reference impl for: bg-scan-error-reason

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES
the spec (a refimpl that goes green while a "Must contain" literal is absent
means the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'broker_guard/progress.py'
t = p.read_text()

# --- change 1: insert classify_error + redact_reason before _check_outcome --
ANCHOR = "def _check_outcome(outcome: str) -> None:"
HELPERS = r'''def classify_error(message: str | None) -> str:
    """Bucket an error message into one of the sweep's failure kinds.

    Returns exactly one of ``'dns'``, ``'ssl'``, ``'timeout'``,
    ``'refused'``, ``'blocked'`` or ``'other'``; matching is
    case-insensitive, and a missing/empty message is ``'other'``.
    """
    if not message:  # relevance: unobservable
        return "other"
    m = str(message).lower()
    if any(k in m for k in ("err_name_not_resolved", "nxdomain",
                            "name or service not known", "getaddrinfo")):
        return "dns"
    if any(k in m for k in ("err_connection_refused", "err_connection_reset",
                            "err_connection_closed", "connection refused")):
        return "refused"
    if any(k in m for k in ("http 403", "429", "captcha", "cloudflare",
                            "access denied")):
        return "blocked"
    if any(k in m for k in ("timeout", "err_timed_out", "timed out")):
        return "timeout"
    if any(k in m for k in ("err_cert_", "ssl", "certificate")):
        return "ssl"
    return "other"


def redact_reason(message: str | None) -> str | None:
    """Reduce every URL in *message* to ``scheme://host`` and truncate.

    Search URLs carry the user's name/phone/email in their path, query or
    fragment, so only the origin may survive into a stored reason; the
    result is capped at 300 chars. ``None`` stays ``None``.
    """
    if message is None:
        return None
    import re as _re
    # scheme://host -- the origin only; path, query and fragment are dropped.
    redacted = _re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^/\s?#]+)[^\s]*",
        lambda m: m.group(1) + "://" + m.group(2),
        str(message))
    return redacted[:300]


'''

assert ANCHOR in t, "refimpl anchor not found -- did the target change?"
t = t.replace(ANCHOR, HELPERS + ANCHOR, 1)

# --- change 2: record_outcome gains reason= and carries it on both paths ----
OLD_SIG = """    def record_outcome(self, broker_id, outcome: str, hits: int = 0, errors: int = 0,
                       replace: bool = False) -> None:"""
NEW_SIG = """    def record_outcome(self, broker_id, outcome: str, hits: int = 0, errors: int = 0,
                       replace: bool = False, reason: str | None = None) -> None:"""

OLD_REPLACE = '''            if replace:
                key = entry_key(self.identity_key, broker_id)
                self.brokers[key] = {
                    "broker_id": str(broker_id), "outcome": outcome,
                    "hits": max(0, int(hits or 0)), "errors": max(0, int(errors or 0)),
                    "checked_at": self.updated_at, "phase": self.phase,
                    "identity_key": self.identity_key, "retried": True,
                }
                return'''
NEW_REPLACE = '''            if replace:
                key = entry_key(self.identity_key, broker_id)
                extra = ({"reason": redact_reason(reason), "error_kind": classify_error(reason)}
                         if outcome == "error" and reason else {})
                self.brokers[key] = {
                    "broker_id": str(broker_id), "outcome": outcome,
                    "hits": max(0, int(hits or 0)), "errors": max(0, int(errors or 0)),
                    "checked_at": self.updated_at, "phase": self.phase,
                    "identity_key": self.identity_key, "retried": True,
                    **extra,
                }
                return'''

OLD_MERGE = '''            elif OUTCOME_RANK[outcome] >= OUTCOME_RANK[entry["outcome"]]:
                entry["outcome"] = outcome
            entry["hits"] += max(0, int(hits or 0))'''
NEW_MERGE = '''            elif OUTCOME_RANK[outcome] >= OUTCOME_RANK[entry["outcome"]]:
                entry["outcome"] = outcome
            if outcome == "error" and reason:
                entry.update({"reason": redact_reason(reason), "error_kind": classify_error(reason)})
            else:
                entry.pop("reason", None)
                entry.pop("error_kind", None)
            entry["hits"] += max(0, int(hits or 0))'''

for old in (OLD_SIG, OLD_REPLACE, OLD_MERGE):
    assert old in t, "refimpl anchor not found -- did the target change?"
t = t.replace(OLD_SIG, NEW_SIG, 1)
t = t.replace(OLD_REPLACE, NEW_REPLACE, 1)
t = t.replace(OLD_MERGE, NEW_MERGE, 1)

p.write_text(t)
print("refimpl applied")
