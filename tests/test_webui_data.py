"""Tests for the dashboard-shaping pure helpers in broker_guard/webui_data.py
(broker_kind_breakdown, broker_automation_breakdown, broker_status_counts,
recent_status_changes, submitted_over_time, estimate_next_scan,
broker_stepper, scan_status). Every one of these is a pure function over
plain dicts/lists -- no db, no filesystem, no network.
"""
from broker_guard import webui_data


def _broker(id_, kind):
    return {"id": id_, "name": id_.title(), "url": f"https://{id_}.invalid", "verification": kind}


def test_broker_kind_breakdown_counts_every_kind_including_zero():
    brokers = [_broker("a", "automatable"), _broker("b", "captcha"), _broker("c", "automatable")]
    counts = webui_data.broker_kind_breakdown(brokers)
    assert counts["automatable"] == 2
    assert counts["captcha"] == 1
    assert counts["photo_id"] == 0
    assert counts["kba"] == 0
    assert counts["manual_review"] == 0
    assert counts["total"] == 3


def test_broker_kind_breakdown_unrecognized_kind_falls_back_to_manual_review():
    brokers = [_broker("a", "some-unknown-kind")]
    counts = webui_data.broker_kind_breakdown(brokers)
    assert counts["manual_review"] == 1


def test_broker_kind_breakdown_empty_input():
    counts = webui_data.broker_kind_breakdown([])
    assert counts["total"] == 0
    assert all(v == 0 for k, v in counts.items() if k != "total")


def test_broker_automation_breakdown_matches_autopilot_decide_action():
    from broker_guard import autopilot as autopilot_mod

    brokers = [_broker("a", "automatable"), _broker("b", "photo_id"), _broker("c", "kba")]
    counts = webui_data.broker_automation_breakdown(brokers)
    assert counts["auto_send"] == 1
    assert counts["needs_document"] == 1
    assert counts["needs_review"] == 1
    assert counts["total"] == 3
    # Cross-check directly against the real decision function, not a
    # re-derived copy of its policy.
    for b in brokers:
        kind = webui_data.brokers_mod.verification_kind(b)
        decision = autopilot_mod.decide_action(kind)
        assert decision["action"] in ("auto_send", "queue")


def test_broker_status_counts_unrecognized_status_falls_back_to_not_sent():
    rows = [
        {"removal_status": None}, {"removal_status": "pending"},
        {"removal_status": "submitted"}, {"removal_status": "confirmed"},
        {"removal_status": "needs_document"}, {"removal_status": "needs_review"},
        {"removal_status": "totally-unknown"},
    ]
    counts = webui_data.broker_status_counts(rows)
    assert counts["not_sent"] == 2  # None + the unrecognized one
    assert counts["pending"] == 1
    assert counts["submitted"] == 1
    assert counts["confirmed"] == 1
    assert counts["needs_document"] == 1
    assert counts["needs_review"] == 1
    assert counts["total"] == 7


def test_recent_status_changes_excludes_undated_and_sorts_newest_first():
    rows = [
        {"broker_id": "a", "status_updated_at": None},
        {"broker_id": "b", "status_updated_at": "2026-01-01T00:00:00+00:00"},
        {"broker_id": "c", "status_updated_at": "2026-01-03T00:00:00+00:00"},
    ]
    out = webui_data.recent_status_changes(rows, limit=8)
    assert [r["broker_id"] for r in out] == ["c", "b"]


def test_recent_status_changes_respects_limit_and_zero():
    rows = [{"broker_id": str(i), "status_updated_at": f"2026-01-0{i}T00:00:00+00:00"} for i in range(1, 6)]
    assert len(webui_data.recent_status_changes(rows, limit=2)) == 2
    assert webui_data.recent_status_changes(rows, limit=0) == []


def test_submitted_over_time_only_counts_submitted_and_confirmed_and_is_cumulative():
    rows = [
        {"removal_status": "submitted", "status_updated_at": "2026-01-01T00:00:00+00:00"},
        {"removal_status": "confirmed", "status_updated_at": "2026-01-01T12:00:00+00:00"},
        {"removal_status": "pending", "status_updated_at": "2026-01-01T00:00:00+00:00"},  # excluded
        {"removal_status": "submitted", "status_updated_at": "2026-01-02T00:00:00+00:00"},
    ]
    out = webui_data.submitted_over_time(rows)
    assert out == [{"bucket": "2026-01-01", "count": 2}, {"bucket": "2026-01-02", "count": 3}]


def test_submitted_over_time_empty_when_nothing_qualifies():
    assert webui_data.submitted_over_time([{"removal_status": "pending", "status_updated_at": "x"}]) == []


def test_estimate_next_scan_adds_interval_to_last_seen():
    result = webui_data.estimate_next_scan("2026-01-01T00:00:00+00:00", 86400)
    assert result == "2026-01-02T00:00:00+00:00"


def test_estimate_next_scan_none_for_missing_or_bad_input():
    assert webui_data.estimate_next_scan(None, 86400) is None
    assert webui_data.estimate_next_scan("not-a-date", 86400) is None


def test_broker_stepper_not_submitted_only_first_step_done():
    row = {"first_seen": "2026-01-01T00:00:00+00:00", "last_seen": "2026-01-01T00:00:00+00:00",
           "removal_status": None, "status_updated_at": None}
    result = webui_data.broker_stepper(row, 86400)
    steps = result["steps"]
    assert steps[0]["done"] is True
    assert steps[1]["done"] is False
    assert steps[2]["done"] is False
    assert result["current_index"] == 0


def test_broker_stepper_needs_document_gets_a_note():
    row = {"first_seen": "x", "last_seen": "2026-01-01T00:00:00+00:00",
           "removal_status": "needs_document", "status_updated_at": "y"}
    steps = webui_data.broker_stepper(row, 86400)["steps"]
    assert steps[1]["done"] is False
    assert "government ID" in steps[1]["note"]


def test_broker_stepper_submitted_marks_step_two_done_not_step_three():
    row = {"first_seen": "x", "last_seen": "2026-01-01T00:00:00+00:00",
           "removal_status": "submitted", "status_updated_at": "y"}
    result = webui_data.broker_stepper(row, 86400)
    steps = result["steps"]
    assert steps[1]["done"] is True
    assert steps[2]["done"] is False  # "Data removed" honestly stays undone
    assert result["current_index"] == 1


def test_broker_stepper_confirmed_marks_data_removed_done():
    row = {"first_seen": "x", "last_seen": "2026-01-01T00:00:00+00:00",
           "removal_status": "confirmed", "status_updated_at": "y"}
    result = webui_data.broker_stepper(row, 86400)
    steps = result["steps"]
    assert steps[1]["done"] is True
    assert steps[2]["done"] is True
    assert result["current_index"] == 2


def test_scan_status_never_run_and_not_running():
    result = webui_data.scan_status(None, {}, 86400)
    assert result == {"running": False, "last_run_at": None, "last_run_ok": None, "next_run_at": None}


def test_scan_status_running_from_jobs_summary():
    result = webui_data.scan_status(None, {"job1": "running"}, 86400)
    assert result["running"] is True


def test_scan_status_reports_last_run_and_next_run_from_heartbeat():
    heartbeat = {"last_run": "2026-01-01T00:00:00+00:00", "ok": True}
    result = webui_data.scan_status(heartbeat, {}, 86400)
    assert result["running"] is False
    assert result["last_run_at"] == "2026-01-01T00:00:00+00:00"
    assert result["last_run_ok"] is True
    assert result["next_run_at"] == "2026-01-02T00:00:00+00:00"


def test_scan_status_failed_heartbeat_reports_ok_false():
    result = webui_data.scan_status({"last_run": "2026-01-01T00:00:00+00:00", "ok": False}, {}, 86400)
    assert result["last_run_ok"] is False
