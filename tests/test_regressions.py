"""Regression tests for the cross-module bugs found in the holistic review.

Each test names the bug it pins. These are the "would it come back?" tests:
every one of them fails against the pre-review code.
"""
import pytest

from broker_guard import (
    alert,
    brokers as brokers_mod,
    captcha,
    detection,
    escalation,
    eraser,
    formfill,
    health,
    playwright_checks,
    profile as profile_mod,
    serpwatch,
    state,
)


# --- BUG: every broker was credited with every SERP hit ---------------------

def test_serpwatch_does_not_attribute_a_hit_to_unrelated_brokers():
    brokers = [
        {"id": "alpha", "name": "A", "url": "https://alpha.invalid"},
        {"id": "beta", "name": "B", "url": "https://beta.invalid"},
    ]

    def search(query):
        # One result, living on alpha's domain only.
        return [{"title": "Testy Mctestface", "url": "https://alpha.invalid/p/1",
                 "content": "Testy Mctestface profile"}]

    hits = serpwatch.run_serpwatch(brokers, [], [], ["Testy Mctestface"], ["Springfield, IL"], search)
    assert {h.broker_id for h in hits} == {"alpha"}


def test_serpwatch_scopes_each_query_to_the_broker_domain():
    seen = []

    def search(query):
        seen.append(query)
        return []

    serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://www.alpha.invalid/search"}],
        [], [], ["Testy Mctestface"], [], search,
    )
    assert seen and all(q.startswith("site:alpha.invalid ") for q in seen)


def test_serpwatch_skips_brokers_without_a_usable_url():
    calls = []
    hits = serpwatch.run_serpwatch(
        [{"id": "nourl", "name": "N", "url": ""}],
        [], [], ["Testy Mctestface"], [], lambda q: calls.append(q) or [],
    )
    assert hits == [] and calls == []


def test_serpwatch_survives_a_failing_search_backend():
    def boom(query):
        raise RuntimeError("searxng down")

    assert serpwatch.run_serpwatch(
        [{"id": "alpha", "name": "A", "url": "https://alpha.invalid"}],
        [], [], ["Testy Mctestface"], [], boom,
    ) == []


@pytest.mark.parametrize("url,domain,expected", [
    ("https://alpha.invalid/x", "alpha.invalid", True),
    ("https://www.alpha.invalid/x", "alpha.invalid", True),
    ("https://sub.alpha.invalid/x", "alpha.invalid", True),
    # The lookalike that a substring check would wrongly accept:
    ("https://alpha.invalid.attacker.test/x", "alpha.invalid", False),
    ("https://notalpha.invalid/x", "alpha.invalid", False),
    ("", "alpha.invalid", False),
])
def test_url_belongs_to_is_not_a_substring_check(url, domain, expected):
    assert serpwatch.url_belongs_to(url, domain) is expected


# --- BUG: a profile with no address produced ZERO name queries --------------

def test_name_only_query_when_profile_has_no_address():
    queries = detection.build_search_queries([], [], ["Testy Mctestface"], [])
    assert queries == [{"kind": "name", "value": "Testy Mctestface"}]


def test_name_address_queries_still_produced_when_addresses_exist():
    queries = detection.build_search_queries([], [], ["Testy Mctestface"], ["Springfield, IL"])
    assert queries == [{"kind": "name_address", "name": "Testy Mctestface",
                        "address": "Springfield, IL"}]


def test_phone_matches_across_formatting_differences():
    result = {"title": "", "snippet": "reachable at (555) 010-0 anytime", "url": ""}
    assert detection.is_people_search_hit(result, ["+1-555-0100"]) is True


def test_short_terms_do_not_match_everything():
    assert detection.is_people_search_hit({"title": "unrelated page"}, ["Q", "a"]) is False


# --- BUG: orchestrator's state interface did not exist on state.py ----------

def test_state_store_satisfies_the_orchestrator_interface():
    store = state.StateStore.open(":memory:")
    for method in ("is_seen", "record_appearance", "seen_brokers"):
        assert callable(getattr(store, method))
    assert store.is_seen("k", "alpha") is False
    store.record_appearance("k", "alpha", "2026-01-01T00:00:00+00:00")
    assert store.is_seen("k", "alpha") is True
    assert store.seen_brokers("k") == ["alpha"]


def test_state_store_preserves_first_seen_on_reappearance():
    store = state.StateStore.open(":memory:")
    store.record_appearance("k", "alpha", "2026-01-01T00:00:00+00:00")
    store.touch("k", "alpha", "2026-02-01T00:00:00+00:00")
    row = store.conn.execute(
        "SELECT first_seen, last_seen FROM presence WHERE identity_key='k'").fetchone()
    assert row == ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00")


# --- BUG: run_cycle's alert payload did not match batch_digest's input ------

def test_events_from_cycle_feeds_batch_digest():
    payload = {"identity_key": "k", "new_appearances": ["alpha"],
               "resolved": ["beta"], "now_iso": "2026-01-01T00:00:00+00:00"}
    digest = alert.batch_digest(alert.events_from_cycle(payload))
    assert digest["counts"] == {"new_appearance": 1, "resolved": 1}
    assert alert.format_notification(digest)["title"] == "2 new broker alert(s)"


def test_batch_digest_tolerates_non_dict_events():
    digest = alert.batch_digest(["junk", {"kind": "new_appearance", "broker_id": "alpha"}])
    assert digest["counts"][None] == 1


# --- BUG: escalation returned a typo'd key and crashed on 'Z' timestamps ----

@pytest.mark.parametrize("kind", ["broker_facing", "regulator_complaint", "fcra_freeze", "other"])
def test_route_escalation_shape_is_uniform(kind):
    out = escalation.route_escalation(kind, {})
    assert set(out) == {"auto_send", "requires_confirm", "requires_human_confirm"}


def test_regulator_complaint_is_not_auto_by_default():
    assert escalation.route_escalation("regulator_complaint", {})["auto_send"] is False
    assert escalation.route_escalation(
        "regulator_complaint", {"auto_file_regulator_complaints": True})["auto_send"] is True


def test_fcra_freeze_always_needs_a_human():
    out = escalation.route_escalation("fcra_freeze", {"auto_file_regulator_complaints": True})
    assert out["auto_send"] is False and out["requires_human_confirm"] is True


def test_sla_helpers_accept_mixed_naive_and_z_timestamps():
    assert escalation.is_overdue("2026-01-01T00:00:00Z", "2026-03-01T00:00:00", 45) is True
    assert escalation.days_remaining("2026-01-01T00:00:00", "2026-01-10T00:00:00Z", 45) == 36


def test_next_escalation_actions_tolerates_rules_without_priority():
    rules = [{"field": "status", "equals": "overdue"}]
    assert escalation.next_escalation_actions({"status": "overdue"}, rules) == rules


# --- BUG: eraser command did not match the vendored CLI ---------------------

def test_eraser_cmd_uses_a_real_subcommand_and_leaks_no_pii():
    cmd = eraser.build_eraser_cmd("alpha", {"full_name": "Testy Mctestface"})
    assert cmd[:4] == ["eraser", "send", "--broker", "alpha"]
    assert "remove" not in cmd and "--name" not in cmd
    # The person's name must never reach argv / the process table.
    assert not any("Testy" in part for part in cmd)


def test_eraser_cmd_dry_run_and_profile():
    cmd = eraser.build_eraser_cmd("alpha", {"eraser_profile": "default"}, dry_run=True)
    assert "--dry-run" in cmd and cmd[-2:] == ["--profile", "default"]


@pytest.mark.parametrize("bad", [
    "; rm -rf /", "../../etc/passwd", "--help", "alpha beta", "$(whoami)", "", "a|b", None, 5,
])
def test_eraser_cmd_rejects_injection_shaped_broker_ids(bad):
    with pytest.raises(ValueError):
        eraser.build_eraser_cmd(bad, {"full_name": "Testy Mctestface"})


def test_eraser_cmd_rejects_injection_shaped_profile_id():
    with pytest.raises(ValueError):
        eraser.build_eraser_cmd("alpha", {"eraser_profile": "../../x"})


@pytest.mark.parametrize("stdout,rc,expected", [
    ("sent 1 request", 0, True),
    ("1 failed", 0, False),
    ("error: smtp refused", 0, False),
    ("", 3, False),
])
def test_parse_eraser_result(stdout, rc, expected):
    assert eraser.parse_eraser_result(stdout, rc)["success"] is expected


# --- BUG: browser could be pointed at file:// / javascript: URLs ------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,<b>x", "", None, "ftp://x.invalid",
])
def test_unsafe_urls_never_become_site_checks(url):
    checks = playwright_checks.build_site_checks(
        [{"id": "alpha", "url": url, "verification": "automatable"}], ["Testy Mctestface"])
    assert checks == []


def test_safe_urls_do_become_site_checks():
    checks = playwright_checks.build_site_checks(
        [{"id": "alpha", "url": "https://alpha.invalid", "verification": "automatable"}], ["t"])
    assert checks == [{"broker_id": "alpha", "url": "https://alpha.invalid", "terms": ["t"]}]


def test_a_raising_page_action_becomes_an_errored_check_not_a_crash():
    def boom(check):
        raise TimeoutError("navigation timeout")

    results = playwright_checks.run_playwright_checks(
        [{"broker_id": "alpha", "url": "https://alpha.invalid", "terms": []}], boom)
    assert results["alpha"] == {"checked": False, "present": False,
                                "error": "TimeoutError: navigation timeout"}


# --- Misc hardening ---------------------------------------------------------

def test_load_brokers_rejects_non_object_records(tmp_path):
    path = tmp_path / "b.json"
    path.write_text('{"brokers": ["id-name-url"]}', encoding="utf-8")
    with pytest.raises(ValueError):
        brokers_mod.load_brokers(str(path))


def test_load_brokers_rejects_non_list_brokers(tmp_path):
    path = tmp_path / "b.json"
    path.write_text('{"brokers": {"id": "x"}}', encoding="utf-8")
    with pytest.raises(ValueError):
        brokers_mod.load_brokers(str(path))


def test_captcha_provider_returning_non_dict_does_not_break_the_chain():
    def bad(challenge):
        return "token"

    def good(challenge):
        return {"ok": True, "token": "t"}

    out = captcha.solve({}, [bad, good])
    assert out["ok"] is True and len(out["attempts"]) == 1


def test_formfill_rejects_a_field_named_missing():
    with pytest.raises(ValueError):
        formfill.map_profile_to_form({}, {"missing": "first_name"})


def test_health_report_survives_malformed_runs():
    report = health.build_report([{"ok": True, "broker_id": "alpha"}, {}, "junk"])
    assert report["total"] == 3 and report["failed"] == 2


def test_profile_derives_a_locality_from_city_state(profile_file):
    identity = profile_mod.load_profile(profile_file)
    assert identity.addresses == ["Springfield, IL"]
    assert identity.full_name == "Testy Q Mctestface"


def test_identity_key_is_stable_and_not_the_plaintext_name(profile_file):
    identity = profile_mod.load_profile(profile_file)
    key = identity.identity_key
    assert key == profile_mod.load_profile(profile_file).identity_key
    assert "Mctestface" not in key and len(key) == 32
