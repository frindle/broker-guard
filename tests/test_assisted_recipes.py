"""Assisted recipe learning: platform detection, the label mapper, the approval
ladder, the dry-run-only guarantee, and the weekly health pass.

No network and no browser: pages, models and notifiers are fakes.
"""
import json

import pytest
from fastapi.testclient import TestClient

from broker_guard import assisted, autopilot, optout_forms, optout_submit, platforms, recipe_store, review
from broker_guard.config import Config
from broker_guard.profile import Identity
from conftest import FAKE_EMAIL, FAKE_FIRST, FAKE_LAST
from test_optout_submit import FakePage, FakeSubmitter


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    for bid in list(optout_forms.LEARNED):
        optout_forms.unregister_recipe(bid)


@pytest.fixture
def identity():
    return Identity(first_name=FAKE_FIRST, last_name=FAKE_LAST, middle_name="Q",
                    emails=[FAKE_EMAIL], addresses=["12 Elm St, Springfield, IL 62704"])


def ctl(label, selector, type="text", tag="input", required=True, visible=True, options=(), name=""):
    return {"label": label, "selector": selector, "type": type, "tag": tag, "required": required,
            "visible": visible, "options": list(options), "name": name}


BROKER = {"id": "acme-data", "name": "Acme Data", "optout_url": "https://acme.invalid/optout"}
BTN = [{"text": "Submit request", "type": "submit", "selector": "#go"}]


# --- platforms ------------------------------------------------------------------

@pytest.mark.parametrize("html,expected", [
    ("<script src='https://cdn.cookielaw.org/x.js'></script>", "onetrust"),
    ("<script src='https://consent.trustarc.com/a.js'>", "trustarc"),
    ("<div class='osano-cm-window'>", "osano"),
    ("<script src='https://app.datagrail.io/x'>", "datagrail"),
    ("<script src='https://cdn.transcend.io/airgap.js'>", "transcend"),
    ("<p>hello</p>", None),
])
def test_detect_platform(html, expected):
    assert platforms.detect_platform(html) == expected


def test_dedicated_request_platform_beats_generic_form_builder():
    html = "cdn.cookielaw.org privacyportal hsforms.net"
    assert platforms.detect_platform(html) == "onetrust"


# --- keyword mapping ----------------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("Email address", "email"), ("First Name *", "first_name"), ("Surname", "last_name"),
    ("Full name", "full_name"), ("Phone number", "phone"), ("ZIP / Postal code", "zip"),
    ("State/Province", "state"), ("Country", "country"), ("Street address", "street"),
    ("Social Security Number", assisted.FORBIDDEN), ("Date of birth", assisted.FORBIDDEN),
    ("Upload your ID", assisted.FORBIDDEN), ("Favourite colour", None),
])
def test_keyword_source(label, expected):
    assert assisted.keyword_source(label) == expected


# --- building a recipe ---------------------------------------------------------------

def test_build_recipe_fills_mapped_fields_and_never_targets_forbidden_ones(identity):
    controls = [
        ctl("First name", "#fn"), ctl("Last name", "#ln"), ctl("Email", "#em", type="email"),
        ctl("Social Security Number", "#ssn", required=False),
        ctl("Trap", "#website", visible=False, required=False),
        ctl("Details", "#msg", tag="textarea", required=False),
    ]
    built = assisted.build_recipe(BROKER, controls, BTN, "onetrust")
    recipe = built["recipe"]
    assert built["ok"] and recipe.submit_selector == "#go"
    assert set(recipe.forbidden_selectors) == {"#ssn", "#website"}
    optout_forms.assert_no_forbidden(recipe)   # does not raise
    resolved = optout_forms.resolve_fields(recipe, identity)
    assert resolved["values"]["#fn"] == FAKE_FIRST
    assert resolved["values"]["#em"] == FAKE_EMAIL
    assert "#ssn" not in resolved["values"] and "#website" not in resolved["values"]
    assert "delete" in resolved["values"]["#msg"].lower()
    assert recipe.confirmation_email is True and recipe.no_captcha_verified is False


def test_a_required_forbidden_field_blocks_approval():
    built = assisted.build_recipe(
        BROKER, [ctl("Email", "#em"), ctl("SSN", "#ssn")], BTN, None)
    assert built["ok"] is False
    assert any("will not send" in u for u in built["unmapped"])


def test_a_required_unknown_field_is_listed_not_guessed():
    built = assisted.build_recipe(BROKER, [ctl("Email", "#em"), ctl("Favourite colour", "#c")], BTN, None)
    assert built["ok"] is False and "Favourite colour" in built["unmapped"]


def test_missing_email_or_submit_is_a_problem():
    built = assisted.build_recipe(BROKER, [ctl("First name", "#fn")], [], None)
    assert set(built["problems"]) == {"no email field found", "no submit button found"}


def test_request_type_select_picks_the_deletion_option_and_checkboxes_attest():
    controls = [
        ctl("Email", "#em"),
        ctl("Request type", "#rt", tag="select", type="select-one",
            options=["Select...", "Access my data", "Delete my data", "Correct my data"]),
        ctl("I confirm this information is accurate", "#ok", type="checkbox"),
        ctl("Subscribe to newsletter", "#nl", type="checkbox", required=False),
        ctl("State", "#st", tag="select", type="select-one", options=["Illinois"]),
    ]
    recipe = assisted.build_recipe(BROKER, controls, BTN, None)["recipe"]
    kinds = {type(s).__name__ + ":" + (getattr(s, "selector", None) or s.container) for s in recipe.steps}
    assert "Select:#rt" in kinds and "Check:#ok" in kinds and "Field:#st" in kinds
    assert not any("#nl" in k for k in kinds)
    sel = [s for s in recipe.steps if isinstance(s, optout_forms.Select)][0]
    assert sel.option_label == "Delete my data"


def test_wrong_request_options_are_never_chosen():
    controls = [ctl("Email", "#em"), ctl("Request", "#rt", tag="select", type="select-one",
                                         options=["Access my data", "Correct my data"])]
    built = assisted.build_recipe(BROKER, controls, BTN, None)
    assert "Request" in built["unmapped"]


def test_the_label_model_sees_only_labels_and_bad_answers_are_dropped():
    sent = []

    class Sess:
        def post(self, url, json=None, timeout=None):
            sent.append(json)

            class R:
                status_code = 200

                def json(self_inner):
                    return {"message": {"content": '{"Company": "skip", "Mood": "rm -rf", "Cell": "phone"}'}}
            return R()

    mapper = assisted.LlmLabelMapper("http://o:11434", session=Sess())
    out = mapper(["Company", "Mood", "Cell", "not asked"])
    assert out == {"Company": "skip", "Cell": "phone"}
    body = json.dumps(sent[0])
    assert FAKE_FIRST not in body and FAKE_EMAIL not in body
    assert "Company" in sent[0]["messages"][0]["content"]


def test_unknown_labels_go_to_the_model_and_its_guess_is_used():
    controls = [ctl("Email", "#em"), ctl("Your contact number", "#cn", required=False)]
    mapper = lambda labels: {"Your contact number": "phone"}
    recipe = assisted.build_recipe(BROKER, controls, BTN, None, label_mapper=mapper)["recipe"]
    assert any(isinstance(s, optout_forms.Field) and s.source == "phone" for s in recipe.steps)


def test_a_crashing_model_leaves_the_field_unmapped_instead_of_failing():
    def boom(labels):
        raise RuntimeError("down")

    built = assisted.build_recipe(BROKER, [ctl("Email", "#em"), ctl("Mystery", "#m")], BTN, None,
                                  label_mapper=boom)
    assert "Mystery" in built["unmapped"]


# --- reading a live page -----------------------------------------------------------------

class ReadPage(FakePage):
    def __init__(self, controls=None, buttons=None, html="", **kw):
        super().__init__(**kw)
        self._data = {"controls": controls or [], "buttons": buttons or [], "captcha": False}
        self._html = html
        self.frames = []
        self.has_form = bool(controls)

    def content(self):
        return self._html

    def wait_for_selector(self, selector, timeout=None):
        if not self.has_form:
            raise RuntimeError("timeout")

    def evaluate(self, js):
        return self._data


def test_assist_broker_builds_a_candidate_and_detects_the_platform():
    page = ReadPage([ctl("Email", "#em")], BTN, html="https://cdn.cookielaw.org/x.js")
    out = assisted.assist_broker(page, BROKER, settle_ms=0)
    assert out["status"] == "candidate" and out["platform"] == "onetrust"
    assert page.url == BROKER["optout_url"]


def test_assist_broker_reports_a_wall_and_a_missing_form():
    walled = ReadPage(body="Checking your browser before accessing", title="Just a moment...")
    assert assisted.assist_broker(walled, BROKER, settle_ms=0)["status"] == "blocked"
    empty = ReadPage()
    assert assisted.assist_broker(empty, BROKER, settle_ms=0)["status"] == "no_form"


# --- the store and the approval ladder -----------------------------------------------------

def test_every_shipped_recipe_roundtrips_through_the_store_format():
    for recipe in optout_forms.RECIPES.values():
        again = recipe_store.recipe_from_dict(json.loads(json.dumps(recipe_store.recipe_to_dict(recipe))))
        assert again == recipe, recipe.broker_id


def make_candidate():
    return assisted.build_recipe(BROKER, [ctl("Email", "#em")], BTN, "onetrust")["recipe"]


def test_only_approved_and_live_recipes_join_the_allow_list(tmp_path):
    store = recipe_store.RecipeStore(str(tmp_path / "r.json"))
    store.add_candidate(make_candidate(), now="t")
    assert recipe_store.register_all(store) == []
    assert not optout_forms.is_supported("acme-data")

    assert store.transition("acme-data", recipe_store.APPROVED)
    assert recipe_store.register_all(store) == ["acme-data"]
    assert optout_forms.is_supported("acme-data")

    assert store.transition("acme-data", recipe_store.REJECTED)
    recipe_store.register_all(store)
    assert not optout_forms.is_supported("acme-data")


def test_illegal_transitions_are_refused(tmp_path):
    store = recipe_store.RecipeStore(str(tmp_path / "r.json"))
    store.add_candidate(make_candidate())
    assert not store.transition("acme-data", recipe_store.LIVE)   # cannot skip approval
    assert not store.transition("nope", recipe_store.APPROVED)


def test_a_learned_recipe_never_replaces_a_shipped_one(tmp_path):
    shipped = optout_forms.RECIPES["pipl-com"]
    fake = recipe_store.recipe_from_dict(recipe_store.recipe_to_dict(shipped))
    from dataclasses import replace
    assert optout_forms.register_recipe(replace(fake, notes="learned")) is False
    assert optout_forms.RECIPES["pipl-com"] is shipped


def test_a_candidate_is_not_overwritten_or_demoted(tmp_path):
    store = recipe_store.RecipeStore(str(tmp_path / "r.json"))
    store.add_candidate(make_candidate())
    store.transition("acme-data", recipe_store.APPROVED)
    assert store.add_candidate(make_candidate()) is False
    assert store.get("acme-data")["state"] == "approved"


def test_the_store_file_is_owner_only(tmp_path):
    import os
    path = tmp_path / "r.json"
    recipe_store.RecipeStore(str(path)).add_candidate(make_candidate())
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


# --- the dry-run-only guarantee and promotion ------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    return Config(review_dir=str(tmp_path / "review"), state_path=str(tmp_path / "s.sqlite"),
                  optout_submit_enabled=True, optout_submit_dry_run=True, playwright_enabled=True,
                  assist_enabled=True, learned_recipes_path=str(tmp_path / "learned.json"))


def test_an_unapproved_candidate_cannot_be_submitted_for_real(identity, cfg):
    recipe = make_candidate()
    page = FakePage(present={"#em", "#go"})
    live = Config(**{**cfg.__dict__, "optout_submit_dry_run": False})
    with pytest.raises(optout_submit.SubmissionRefused):
        optout_submit.submit_optout(recipe, identity, live, submitter=FakeSubmitter(page),
                                    allow_candidate=True, dry_run=False)
    with pytest.raises(optout_submit.SubmissionRefused):
        optout_submit.submit_optout(recipe, identity, live, submitter=FakeSubmitter(page))
    assert page.submitted is False


def test_a_candidate_can_be_dry_run_filled_and_photographed(identity, cfg):
    recipe = make_candidate()
    page = FakePage(present={"#em", "#go"})
    saved = optout_submit.submit_optout(recipe, identity, cfg, submitter=FakeSubmitter(page),
                                        allow_candidate=True, dry_run=True)
    assert saved["outcome"] == review.OUTCOME_DRY_RUN
    assert page.filled["#em"] == FAKE_EMAIL and page.submitted is False


def test_first_confirmed_real_submit_promotes_an_approved_recipe_to_live(identity, cfg):
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    recipe = make_candidate()
    store.add_candidate(recipe)
    store.transition("acme-data", recipe_store.APPROVED)
    recipe_store.register_all(store)
    live = Config(**{**cfg.__dict__, "optout_submit_dry_run": False})
    page = FakePage(present={"#em", "#go"}, result_body="Thank you, your request has been received")
    page.click = lambda sel: setattr(page, "submitted", True)
    saved = optout_submit.submit_optout(recipe, identity, live, submitter=FakeSubmitter(page))
    assert saved["outcome"] == review.OUTCOME_SUBMITTED
    assert store.get("acme-data")["state"] == "live"


def test_a_dry_run_or_failed_attempt_does_not_promote(identity, cfg):
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    store.add_candidate(make_candidate())
    store.transition("acme-data", recipe_store.APPROVED)
    recipe_store.register_all(store)
    page = FakePage(present={"#em", "#go"})
    optout_submit.submit_optout(optout_forms.RECIPES["acme-data"], identity, cfg,
                                submitter=FakeSubmitter(page))
    assert store.get("acme-data")["state"] == "approved"


# --- the pass --------------------------------------------------------------------------------

class Collect:
    def __init__(self):
        self.sent = []

    def send(self, n):
        self.sent.append(n)
        return 1


class OneShotSubmitter(FakeSubmitter):
    def __init__(self, page):
        super().__init__(page)


def undecided_broker():
    bid = sorted(optout_forms.OPTOUT_UNDECIDED)[0]
    return {"id": bid, "name": "Undecided Co", "optout_url": "https://undecided.invalid/optout"}


def test_assisted_pass_is_off_unless_switched_on_and_needs_the_submit_gate(identity, cfg):
    off = Config(**{**cfg.__dict__, "assist_enabled": False})
    assert "skipped" in autopilot.run_assisted_pass([identity], [undecided_broker()], off)
    nosubmit = Config(**{**cfg.__dict__, "optout_submit_enabled": False})
    assert "skipped" in autopilot.run_assisted_pass([identity], [undecided_broker()], nosubmit)


def test_assisted_pass_learns_dry_runs_and_asks_but_never_registers(identity, cfg):
    broker = undecided_broker()
    page = ReadPage([ctl("First name", "#fn"), ctl("Email", "#em")], BTN,
                    html="cdn.cookielaw.org", present={"#fn", "#em", "#go"})
    notifier = Collect()
    out = autopilot.run_assisted_pass([identity], [broker], cfg, notifier=notifier,
                                      submitter=OneShotSubmitter(page))
    assert out["candidates"] == 1 and out["dry_runs"] == 1 and out["errors"] == 0
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    row = store.get(broker["id"])
    assert row["state"] == "candidate" and row["platform"] == "onetrust" and row["dry_run_record"]
    assert page.submitted is False and page.filled["#em"] == FAKE_EMAIL
    assert not optout_forms.is_supported(broker["id"])
    assert notifier.sent and notifier.sent[0]["title"].startswith("Review a learned")
    # a second pass does not redo it
    again = autopilot.run_assisted_pass([identity], [broker], cfg, notifier=notifier,
                                        submitter=OneShotSubmitter(page))
    assert again["tried"] == 0


def test_an_unreadable_page_is_remembered_as_unbuildable(identity, cfg):
    broker = undecided_broker()
    page = ReadPage(body="Checking your browser before accessing", title="Just a moment...")
    out = autopilot.run_assisted_pass([identity], [broker], cfg, submitter=OneShotSubmitter(page))
    assert out["unbuildable"] == 1
    assert recipe_store.RecipeStore(recipe_store.store_path(cfg)).get(broker["id"])["state"] == "unbuildable"


def test_a_candidate_with_unresolved_fields_is_stored_but_not_dry_run(identity, cfg):
    broker = undecided_broker()
    page = ReadPage([ctl("Email", "#em"), ctl("Favourite colour", "#c")], BTN, present={"#em", "#c", "#go"})
    out = autopilot.run_assisted_pass([identity], [broker], cfg, submitter=OneShotSubmitter(page))
    assert out["candidates"] == 1 and out["dry_runs"] == 0
    assert page.filled == {}


def test_only_undecided_brokers_are_considered(identity, cfg):
    walled = {"id": sorted(optout_forms.OPTOUT_BLOCKED)[0], "optout_url": "https://x.invalid"}
    page = ReadPage([ctl("Email", "#em")], BTN)
    out = autopilot.run_assisted_pass([identity], [walled], cfg, submitter=OneShotSubmitter(page))
    assert out == {"tried": 0, "candidates": 0, "unbuildable": 0, "dry_runs": 0, "errors": 0}


# --- weekly health ------------------------------------------------------------------------------

def test_weekly_health_alerts_on_drift_and_not_on_blocked(cfg):
    reports = [
        {"status": "ok", "broker_id": "a", "leg": "optout", "url": "u"},
        {"status": "blocked", "broker_id": "b", "leg": "optout", "url": "u"},
        {"status": "drift", "broker_id": "c", "leg": "optout", "url": "u", "missing": ["#x"], "ambiguous": []},
    ]
    alerts, notifier = [], Collect()
    out = autopilot.run_recipe_health_pass(cfg, notifier=notifier, alert_sink=alerts.append,
                                           check=lambda: reports)
    assert out == {"checked": 3, "drift": 1, "blocked": 1}
    assert alerts[0]["recipe_drift"][0]["broker_id"] == "c"
    assert "c" in notifier.sent[0]["message"]


def test_weekly_health_is_quiet_when_nothing_drifted(cfg):
    alerts, notifier = [], Collect()
    autopilot.run_recipe_health_pass(cfg, notifier=notifier, alert_sink=alerts.append,
                                     check=lambda: [{"status": "ok", "broker_id": "a", "leg": "optout"}])
    assert alerts == [] and notifier.sent == []


def test_weekly_health_respects_its_switches(cfg):
    off = Config(**{**cfg.__dict__, "recipe_health_enabled": False})
    assert "skipped" in autopilot.run_recipe_health_pass(off, check=lambda: 1 / 0)
    nobrowser = Config(**{**cfg.__dict__, "playwright_enabled": False})
    assert "skipped" in autopilot.run_recipe_health_pass(nobrowser, check=lambda: 1 / 0)


def test_the_loop_schedules_assist_and_weekly_health():
    iv = autopilot.Intervals()
    assert iv.recipe_health_seconds == 604800 and iv.assist_seconds > 0


# --- the /recipes page -------------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path, cfg):
    from broker_guard import webui
    cfg2 = Config(**{**cfg.__dict__, "brokers_path": str(tmp_path / "b.json"),
                     "profile_path": str(tmp_path / "p.json"), "settings_path": str(tmp_path / "set.json")})
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg2
    yield TestClient(webui.app), cfg2
    webui.app.dependency_overrides.clear()


def test_recipes_page_lists_candidates_and_approval_needs_a_dry_run(web):
    client, cfg = web
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    store.add_candidate(make_candidate())
    assert "Acme Data" in client.get("/recipes").text or "acme-data" in client.get("/recipes").text
    assert client.post("/recipes/acme-data/approve", follow_redirects=False).status_code == 409
    store.set_dry_run("acme-data", "rec-1", "x.png")
    assert client.post("/recipes/acme-data/approve", follow_redirects=False).status_code == 303
    assert store.get("acme-data")["state"] == "approved"
    assert optout_forms.is_supported("acme-data")
    client.post("/recipes/acme-data/reject", follow_redirects=False)
    assert not optout_forms.is_supported("acme-data")


def test_a_candidate_with_unresolved_fields_cannot_be_approved(web):
    client, cfg = web
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    store.add_candidate(make_candidate(), unmapped=["Favourite colour"])
    store.set_dry_run("acme-data", "rec-1", None)
    assert client.post("/recipes/acme-data/approve", follow_redirects=False).status_code == 409
    assert "Cannot be approved yet" in client.get("/recipes").text


def test_unbuildable_can_be_retried(web):
    client, cfg = web
    store = recipe_store.RecipeStore(recipe_store.store_path(cfg))
    store.mark_unbuildable("zzz", "bot wall")
    assert client.post("/recipes/zzz/reopen", follow_redirects=False).status_code == 303
    assert store.get("zzz") is None
