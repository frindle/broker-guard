"""Adversarial fixture for: bg-scan-error-reason

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests
the property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

Each case: (description, callable_returning_actual, expected)
"""
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/progress.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


def _entry(broker_id="b1"):
    """The per-broker entry recorded under identity None, or {} if absent."""
    p = target.ScanProgress()
    return p.brokers.get(target.entry_key(None, broker_id), {})


def _record_error_with_reason(reason):
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1, reason=reason)
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _retry_after_error():
    """An error with a reason, then a clean retry (replace=True)."""
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1,
                     reason="GET https://x.example/search?q=jane+doe failed")
    p.record_outcome("b1", "checked", replace=True)
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _retry_error_with_reason():
    """A retry (replace=True) that IS still an error and carries a reason."""
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1,
                     reason="GET https://x.example/search?q=jane+doe failed")
    p.record_outcome("b1", "error", errors=1, replace=True,
                     reason="ERR_NAME_NOT_RESOLVED for x.example")
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _retry_non_error_with_reason():
    """A retry (replace=True) that is NOT an error but a reason was passed."""
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1,
                     reason="ERR_NAME_NOT_RESOLVED for x.example")
    p.record_outcome("b1", "checked", replace=True,
                     reason="https://x.example/search?q=jane+doe")
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _clean_after_error_merge():
    """An errored leg first (with reason), then a clean leg (rank merge)."""
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1,
                     reason="ERR_TIMED_OUT after 30s")
    p.record_outcome("b1", "checked")
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _merge_after_error():
    """A clean SERP leg first, then an errored browser leg (rank merge)."""
    p = target.ScanProgress()
    p.record_outcome("b1", "checked")
    p.record_outcome("b1", "error", errors=1, reason="ERR_TIMED_OUT after 30s")
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _no_reason_unchanged():
    """Existing callers that pass no reason must see exactly the old shape."""
    p = target.ScanProgress()
    p.record_outcome("b1", "checked")
    e = p.brokers[target.entry_key(None, "b1")]
    return sorted(e.keys())


def _non_error_with_reason():
    """A hit/checked/skipped outcome must never carry reason/error_kind."""
    p = target.ScanProgress()
    p.record_outcome("b1", "hit", hits=1, reason="https://x.example/q?name=jane")
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _hit_after_error_merge():
    """An errored leg first (with reason), then a HIT leg: hit outranks error,
    so the entry is no longer an error and must not keep the stale reason."""
    p = target.ScanProgress()
    p.record_outcome("b1", "error", errors=1, reason="ERR_TIMED_OUT after 30s")
    p.record_outcome("b1", "hit", hits=1)
    e = p.brokers[target.entry_key(None, "b1")]
    return {"outcome": e["outcome"], "reason": e.get("reason"),
            "error_kind": e.get("error_kind")}


def _classify(message):
    return target.classify_error(message)


def _redact(message):
    return target.redact_reason(message)



CASES = [
    # -- the defect itself: error + reason -> redacted reason + kind --------
    ("error with a DNS message stores redacted URL and kind 'dns'",
     lambda: _record_error_with_reason(
         "GET https://searx.example/search?q=jane+doe+555-1234 failed: "
         "ERR_NAME_NOT_RESOLVED"),
     {"outcome": "error",
      "reason": "GET https://searx.example failed: ERR_NAME_NOT_RESOLVED",
      "error_kind": "dns"}),

    ("retry (replace=True) that is no longer an error drops the stale reason",
     _retry_after_error,
     {"outcome": "checked", "reason": None, "error_kind": None}),

    # -- replace path: a retry that IS still an error carries its own reason -
    ("retry (replace=True) that is still an error stores redacted reason + kind",
     _retry_error_with_reason,
     {"outcome": "error",
      "reason": "ERR_NAME_NOT_RESOLVED for x.example",
      "error_kind": "dns"}),

    # -- replace path: non-error retry must not store keys even with a reason -
    ("retry (replace=True) that is checked never stores reason/error_kind",
     _retry_non_error_with_reason,
     {"outcome": "checked", "reason": None, "error_kind": None}),

    # -- merge path: clean leg after errored leg drops the stale reason ------
    ("rank-merge: clean leg after errored leg keeps the error AND its reason",
     _clean_after_error_merge,
     {"outcome": "error", "reason": "ERR_TIMED_OUT after 30s",
      "error_kind": "timeout"}),

    ("rank-merge path also carries reason (clean leg then errored leg)",
     _merge_after_error,
     {"outcome": "error", "reason": "ERR_TIMED_OUT after 30s",
      "error_kind": "timeout"}),

    ("rank-merge: a hit leg after an errored leg drops reason AND error_kind",
     _hit_after_error_merge,
     {"outcome": "hit", "reason": None, "error_kind": None}),

    # -- regression: no-reason callers see exactly the old entry shape ------
    ("no reason -> entry has none of the new keys (old shape preserved)",
     _no_reason_unchanged,
     ["broker_id", "checked_at", "errors", "hits", "identity_key",
      "outcome", "phase"]),

    # -- non-error outcomes never get the keys even if a reason is passed ---
    ("hit outcome with a reason still gets no reason/error_kind keys",
     _non_error_with_reason,
     {"outcome": "hit", "reason": None, "error_kind": None}),

    # -- classify_error: one case per bucket + degenerate inputs ------------
    ("classify_error: NXDOMAIN -> dns (case-insensitive)",
     lambda: _classify("lookup x.example: no such host (NXDOMAIN)"), "dns"),
    ("classify_error: 'Name or service not known' -> dns",
     lambda: _classify("getaddrinfo failed: Name or service not known"), "dns"),
    ("classify_error: ERR_CERT_ -> ssl",
     lambda: _classify("net::ERR_CERT_AUTHORITY_INVALID at https://x.example/"), "ssl"),
    ("classify_error: 'certificate' (lowercase) -> ssl",
     lambda: _classify("handshake failed: self-signed certificate"), "ssl"),
    ("classify_error: TimeoutError -> timeout",
     lambda: _classify("TimeoutError: read timed out after 30s"), "timeout"),
    ("classify_error: ERR_CONNECTION_REFUSED -> refused",
     lambda: _classify("net::ERR_CONNECTION_REFUSED"), "refused"),
    ("classify_error: 'Connection refused' (no net:: prefix) -> refused",
     lambda: _classify("socket connect failed: Connection refused by peer"), "refused"),
    ("classify_error: HTTP 429 -> blocked",
     lambda: _classify("HTTP 429 Too Many Requests from https://x.example/"), "blocked"),
    ("classify_error: Cloudflare captcha -> blocked",
     lambda: _classify("Cloudflare challenge page (captcha) served instead of results"), "blocked"),
    ("classify_error: Access Denied -> blocked (case-insensitive)",
     lambda: _classify("403 access denied by upstream proxy"), "blocked"),
    ("classify_error: unrelated message -> other",
     lambda: _classify("502 Bad Gateway from origin"), "other"),
    ("classify_error: None -> other (must not raise)",
     lambda: _classify(None), "other"),
    ("classify_error: empty string -> other (must not raise)",
     lambda: _classify(""), "other"),

    # -- redact_reason: URL reduction + truncation boundary -----------------
    ("redact_reason strips path, query AND fragment down to scheme://host",
     lambda: _redact("failed https://x.example/a/b?q=jane#frag and http://y.example"),
     "failed https://x.example and http://y.example"),
    ("redact_reason truncates a long message to exactly 300 chars",
     lambda: len(_redact("x" * 400)), 300),
    ("redact_reason keeps the reduced URL intact when under 300 chars",
     lambda: _redact("GET https://h.example/search?q=secret+phone failed"),
     "GET https://h.example failed"),
    ("redact_reason(None) is None (must not raise)",
     lambda: _redact(None), None),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
