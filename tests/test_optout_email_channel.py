"""The email channel wired into the autopilot, and the state-law templates."""
import pytest
from fastapi.testclient import TestClient

from broker_guard import autopilot, optout_email, optouts, review, state, webui
from broker_guard.config import Config
from broker_guard.profile import Identity

T0 = "2026-10-01T00:00:00+00:00"


def ident(addr):
    return Identity(first_name="Testy", last_name="Mctestface",
                    emails=["testy.mctestface@example.invalid"],
                    addresses=[addr], phones=["+1-555-0100"])


NV = ident("1 Fake St, Reno, NV 89501")
CA = ident("1 Fake St, Fresno, CA 93650")
IL = ident("1 Fake St, Springfield, IL 62704")


def broker(bid, email, url=None):
    return {"id": bid, "name": bid, "url": url or "https://%s.example" % bid,
            "optout_email": email}


BROKERS = [
    broker("good-one", "privacy@good-one.example"),
    broker("good-two", "ccpa@good-two.example"),
    broker("gmail-one", "someone@gmail.com", "https://gmail-one.example"),
    broker("personal-one", "talbert@personal-one.example"),
    broker("noemail", ""),
]


@pytest.fixture
def store(tmp_path):
    return state.StateStore.open(str(tmp_path / "s.sqlite"))


def cfg_(tmp_path, **kw):
    base = dict(optout_email_enabled=True, optout_email_dry_run=True,
                optout_email_from="alias@example.invalid", optout_email_batch=20,
                review_dir=str(tmp_path / "rv"))
    base.update(kw)
    return Config(**base)


class Sent(list):
    def __call__(self, message):
        self.append(message)


# --- templates ------------------------------------------------------------------

def test_nevada_template_cites_nrs_603a_345_and_60_days():
    m = optout_email.compose_request("b", "b.example", "privacy@b.example", NV, "a@x.invalid")
    body = m.get_content()
    assert "NRS 603A.345" in m["Subject"] and "NRS 603A.345" in body
    assert "60 days" in body and "California" not in body.split("voluntarily")[0]
    assert optout_email.sla_days_for(NV) == 60


def test_california_template_cites_1798_105_and_120_and_45_days():
    m = optout_email.compose_request("b", "b.example", "privacy@b.example", CA, "a@x.invalid")
    body = m.get_content()
    assert "1798.105" in body and "1798.120" in body and "45 days" in body
    assert optout_email.sla_days_for(CA) == 45


def test_other_states_get_the_generic_template():
    assert optout_email.template_for(IL) == "generic"
    m = optout_email.compose_request("b", "b.example", "privacy@b.example", IL, "a@x.invalid")
    assert "Nevada" not in m.get_content() and "NRS" not in m["Subject"]


# --- the pass -----------------------------------------------------------------------

def run(tmp_path, store, identities, cfg, transport=None, brokers=BROKERS, **kw):
    return autopilot.run_optout_email_pass(
        identities, brokers, cfg, store=store, transport=transport, now=T0,
        sleep=lambda s: None, **kw)


def test_disabled_sends_and_records_nothing(tmp_path, store):
    sent = Sent()
    c = run(tmp_path, store, [NV], cfg_(tmp_path, optout_email_enabled=False), sent)
    assert c["disabled"] == 1 and not sent and store.optouts.list() == []


def test_dry_run_composes_records_and_never_sends(tmp_path, store):
    sent = Sent()
    c = run(tmp_path, store, [NV], cfg_(tmp_path), sent)
    assert c["dry_run"] == 2 and c["sent"] == 0 and not sent
    row = store.optouts.get(NV.identity_key, "good-one")
    assert row["state"] == optouts.QUEUED and row["last_dry_run"] == 1
    # rehearsal is not repeated next pass
    assert run(tmp_path, store, [NV], cfg_(tmp_path), sent)["dry_run"] == 0


def test_live_sends_only_to_eligible_role_addresses_and_tracks_sla(tmp_path, store):
    sent = Sent()
    cfg = cfg_(tmp_path, optout_email_dry_run=False)
    c = run(tmp_path, store, [NV], cfg, sent)
    assert c["sent"] == 2 and c["ineligible"] == 2
    assert sorted(m["To"] for m in sent) == ["ccpa@good-two.example", "privacy@good-one.example"]
    assert not any("gmail" in m["To"] or "talbert" in m["To"] for m in sent)
    row = store.optouts.get(NV.identity_key, "good-one")
    assert row["state"] == optouts.SUBMITTED and row["channel"] == "email"
    assert row["sla_due_at"].startswith("2026-11-30")        # +60 days (Nevada)
    # not sent twice
    assert run(tmp_path, store, [NV], cfg, sent)["sent"] == 0 and len(sent) == 2


def test_batch_limit_trickles_the_backlog(tmp_path, store):
    sent = Sent()
    cfg = cfg_(tmp_path, optout_email_dry_run=False, optout_email_batch=1)
    assert run(tmp_path, store, [NV], cfg, sent)["sent"] == 1
    assert run(tmp_path, store, [NV], cfg, sent)["sent"] == 1
    assert len(sent) == 2


def test_live_without_a_transport_sends_nothing_and_counts_errors(tmp_path, store):
    cfg = cfg_(tmp_path, optout_email_dry_run=False)        # no SMTP host configured
    c = run(tmp_path, store, [NV], cfg, None)
    assert c["sent"] == 0 and c["errors"] == 2


def test_smtp_failure_is_a_retryable_failure_not_a_crash(tmp_path, store):
    def boom(message):
        raise OSError("down")
    c = run(tmp_path, store, [NV], cfg_(tmp_path, optout_email_dry_run=False), boom)
    assert c["sent"] == 0
    assert store.optouts.get(NV.identity_key, "good-one")["state"] == optouts.FAILED


def test_form_recipe_brokers_are_left_to_the_form_channel(tmp_path, store):
    sent = Sent()
    brokers = [broker("thatsthem-com", "privacy@thatsthem-com.example")]
    cfg = cfg_(tmp_path, optout_email_dry_run=False)
    assert run(tmp_path, store, [NV], cfg, sent, brokers=brokers)["sent"] == 0


def test_form_recipe_broker_falls_back_to_email_once_the_form_gave_up(tmp_path, store):
    sent = Sent()
    brokers = [broker("thatsthem-com", "privacy@thatsthem-com.example")]
    store.optouts.record_attempt(NV.identity_key, {
        "id": "r", "broker_id": "thatsthem-com", "outcome": "needs_manual_action"}, T0)
    cfg = cfg_(tmp_path, optout_email_dry_run=False)
    assert run(tmp_path, store, [NV], cfg, sent, brokers=brokers)["sent"] == 1
    assert store.optouts.get(NV.identity_key, "thatsthem-com")["state"] == optouts.SUBMITTED


def test_overdue_requests_escalate_to_the_user_with_a_notification(tmp_path, store):
    cfg = cfg_(tmp_path, optout_email_dry_run=False)
    run(tmp_path, store, [CA], cfg, Sent())
    calls = []

    class N:
        def state_changed(self, kind, row, detail=""):
            calls.append((kind, row["broker_id"]))
            return 1

    later = optouts._plus(T0, 46 * 86400)
    c = autopilot.run_optout_email_pass([CA], BROKERS, cfg, store=store, notifier=N(),
                                        transport=Sent(), now=later, sleep=lambda s: None)
    assert c["escalated"] == 2
    assert {x[1] for x in calls} == {"good-one", "good-two"}
    assert store.optouts.get(CA.identity_key, "good-one")["state"] == optouts.NEEDS_USER


def test_not_overdue_before_the_window(tmp_path, store):
    cfg = cfg_(tmp_path, optout_email_dry_run=False)
    run(tmp_path, store, [CA], cfg, Sent())
    assert store.optouts.overdue(optouts._plus(T0, 44 * 86400)) == []


def test_no_imap_anywhere_in_the_channel():
    import pathlib
    src = pathlib.Path(autopilot.__file__).read_text()
    assert "imaplib" not in src and "IMAP4" not in src


# --- settings switch can't flip to LIVE by omission ------------------------------------

def test_settings_post_without_the_email_fields_leaves_dry_run_alone(tmp_path):
    from broker_guard import settings as settings_mod
    cfg = Config(settings_path=str(tmp_path / "set.json"))
    settings_mod.update_settings(cfg.settings_path, {"optout_email_dry_run": True})
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    try:
        r = TestClient(webui.app).post("/settings", data={"interval_seconds": "86400", "searxng_min_interval_s": "2.0",
                                             "searxng_jitter_s": "1.0"},
                                       follow_redirects=False)
        assert r.status_code == 303
        assert settings_mod.load_settings(cfg.settings_path)["optout_email_dry_run"] is True
    finally:
        webui.app.dependency_overrides.clear()
