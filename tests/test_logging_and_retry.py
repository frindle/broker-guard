"""Tests for the support layer: PII redaction in logs, and retry/backoff."""
import logging

import pytest

from broker_guard.logging_setup import JsonFormatter, redact, setup_logging
from broker_guard.retry import RetryExhausted, backoff_delays, with_retry


# --- redaction --------------------------------------------------------------

def test_redact_strips_emails_and_phones():
    out = redact("contact testy@example.invalid or +1-555-0100 now")
    assert "example.invalid" not in out and "555" not in out
    assert "<email>" in out and "<phone>" in out


def test_redact_recurses_into_containers():
    out = redact({"a": ["x@y.invalid"], "b": {"c": "555-010-0100"}})
    assert out["a"] == ["<email>"] and "<phone>" in out["b"]["c"]


def test_json_formatter_redacts_by_default_and_includes_extras():
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "found testy@example.invalid", (), None)
    record.broker_id = "alpha"
    line = JsonFormatter().format(record)
    assert "<email>" in line and '"broker_id": "alpha"' in line
    assert "example.invalid" not in line


def test_json_formatter_can_be_opted_out_of_redaction():
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "testy@example.invalid", (), None)
    assert "example.invalid" in JsonFormatter(redact_pii=False).format(record)


def test_json_formatter_never_raises_on_unserializable_extras():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
    record.weird = object()
    assert JsonFormatter().format(record)


def test_setup_logging_is_idempotent(tmp_path):
    for _ in range(3):
        setup_logging("INFO", str(tmp_path / "logs"))
    assert len(logging.getLogger().handlers) == 2  # stream + file


def test_setup_logging_survives_an_unwritable_log_dir(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    logger = setup_logging("INFO", str(blocker))  # must not raise
    assert logger is not None
    logging.getLogger().handlers = []


# --- retry ------------------------------------------------------------------

def test_backoff_delays_are_exponential_and_capped():
    assert backoff_delays(5, base_delay=1, max_delay=4, jitter=False) == [1, 2, 4, 4]


def test_backoff_delays_has_one_fewer_entry_than_attempts():
    assert len(backoff_delays(3, jitter=False)) == 2
    assert backoff_delays(1, jitter=False) == []


def test_backoff_jitter_stays_within_the_cap():
    for delay in backoff_delays(6, base_delay=1, max_delay=8):
        assert 0 <= delay <= 8


def test_with_retry_returns_on_first_success():
    calls = []
    assert with_retry(lambda: calls.append(1) or "ok", sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_with_retry_recovers_after_transient_failures():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("transient")
        return "ok"

    assert with_retry(flaky, attempts=3, sleep=lambda _: None) == "ok"
    assert state["n"] == 3


def test_with_retry_raises_retry_exhausted_with_the_last_cause():
    def always():
        raise OSError("still down")

    with pytest.raises(RetryExhausted) as excinfo:
        with_retry(always, attempts=2, sleep=lambda _: None)
    assert isinstance(excinfo.value.__cause__, OSError)


def test_with_retry_does_not_retry_unlisted_exceptions():
    calls = []

    def bad_config():
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        with_retry(bad_config, attempts=5, retry_on=(OSError,), sleep=lambda _: None)
    assert len(calls) == 1


def test_with_retry_sleeps_between_attempts_only():
    slept = []

    def always():
        raise OSError("x")

    with pytest.raises(RetryExhausted):
        with_retry(always, attempts=3, sleep=slept.append, jitter=False, base_delay=1)
    assert slept == [1, 2]


# --- the leak found during end-to-end smoke testing -------------------------

REAL_URLLIB3_ERROR = (
    "ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=9): Max retries "
    "exceeded with url: /search?q=site%3Aalpha.invalid+%22Testy+Q+Mctestface%22"
    "+%22Springfield%2C+IL%22&format=json&safesearch=0 (Caused by NewConnectionError)"
)


def test_search_query_never_survives_redaction_in_a_transport_error():
    """requests puts the full URL -- i.e. the search terms -- in its error text.

    The query string IS the person's name/address, percent-encoded so the
    email and phone patterns miss it. Regression for a leak found by running
    the service against an unreachable SearXNG.
    """
    out = redact(REAL_URLLIB3_ERROR)
    for secret in ("Mctestface", "Testy", "Springfield", "%22"):
        assert secret not in out
    assert "127.0.0.1" in out  # the useful diagnostic part survives


def test_absolute_url_query_is_redacted_but_the_endpoint_is_kept():
    out = redact("GET https://searx.invalid/search?q=%22Testy+Mctestface%22&format=json failed")
    assert out.startswith("GET https://searx.invalid/search?<redacted-query>")
    assert "Mctestface" not in out


def test_percent_encoded_email_in_a_query_is_redacted():
    out = redact("url: /search?q=testy%40example.invalid&format=json")
    assert "example.invalid" not in out


def test_formatter_redacts_a_logged_transport_error_end_to_end():
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "retryable failure", (), None)
    record.error = REAL_URLLIB3_ERROR
    line = JsonFormatter().format(record)
    assert "Mctestface" not in line and "Springfield" not in line
