"""Opt-out attempts must run off the caller's thread, and a failed attempt
that filled nothing must not block the autopilot from retrying it.

Regression for the 2026-09-27 live scan: the autopilot's submission pass
called run_attempt from the scan thread while the scan's own Playwright was
open there, Playwright refused to start ("using Playwright Sync API inside
the asyncio loop"), and every scheduled attempt was recorded as "no browser
available" -- which the dedupe then treated as a spent request forever.
"""
import asyncio
import threading
from types import SimpleNamespace

from broker_guard import autopilot, optout_forms, optout_submit, review


def test_run_attempt_starts_browser_off_a_thread_with_a_running_loop(monkeypatch):
    seen = {}

    class FakeSubmitter:
        def __init__(self, **kw):
            self._browser = None

        def start(self):
            # Mimic Playwright's sync API: it refuses to start on a thread
            # that already has a running asyncio loop.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._browser = object()
                seen["started_on"] = threading.get_ident()
                return self
            raise RuntimeError("It looks like you are using Playwright Sync API "
                               "inside the asyncio loop.")

        def close(self):
            pass

    def fake_submit(recipe, identity, cfg, submitter=None, **kw):
        return {"browser": submitter is not None and submitter._browser is not None}

    monkeypatch.setattr(optout_submit, "OptOutSubmitter", FakeSubmitter)
    monkeypatch.setattr(optout_submit, "submit_optout", fake_submit)
    monkeypatch.setattr(optout_submit.optout_forms, "recipe_for", lambda b: object())
    cfg = SimpleNamespace(optout_submit_enabled=True)

    async def caller():
        # Same situation as the scan thread: a loop is running right here.
        return optout_submit.run_attempt("x", SimpleNamespace(), cfg,
                                         alert_sink=lambda *_: None), threading.get_ident()

    result, caller_thread = asyncio.run(caller())
    assert result == {"browser": True}
    assert seen["started_on"] != caller_thread


def _pass_with(monkeypatch, existing):
    attempted = []
    monkeypatch.setattr(review, "load_attempts", lambda d: existing)
    monkeypatch.setattr(review, "review_dir", lambda cfg: "/nowhere")
    monkeypatch.setattr(optout_forms, "supported_broker_ids", lambda: ["thatsthem-com"])
    monkeypatch.setattr(optout_submit, "run_attempt",
                        lambda b, ident, cfg: attempted.append(b) or {})
    counts = autopilot.run_optout_submission_pass(
        [SimpleNamespace(identity_key="k1")], SimpleNamespace())
    return attempted, counts


def test_failed_attempt_that_filled_nothing_is_retried(monkeypatch):
    attempted, counts = _pass_with(monkeypatch, [
        {"broker_id": "thatsthem-com", "identity_key": "k1",
         "outcome": review.OUTCOME_FAILED, "fields": {}},
    ])
    assert attempted == ["thatsthem-com"]
    assert counts["skipped_existing"] == 0


def test_attempt_that_filled_fields_is_never_resubmitted(monkeypatch):
    attempted, counts = _pass_with(monkeypatch, [
        {"broker_id": "thatsthem-com", "identity_key": "k1",
         "outcome": review.OUTCOME_FAILED, "fields": {"First name": "filled"}},
    ])
    assert attempted == []
    assert counts["skipped_existing"] == 1


def test_non_failed_attempt_is_never_resubmitted(monkeypatch):
    for outcome in {v for k, v in vars(review).items()
                    if k.startswith("OUTCOME_") and v != review.OUTCOME_FAILED}:
        attempted, _ = _pass_with(monkeypatch, [
            {"broker_id": "thatsthem-com", "identity_key": "k1",
             "outcome": outcome, "fields": {}},
        ])
        assert attempted == [], outcome
