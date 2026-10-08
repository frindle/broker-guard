"""tools/promote_staged.py and tools/probe_broker_forms.py (prober fixes)."""
import importlib.util
import os

from broker_guard import review

ROOT = os.path.join(os.path.dirname(__file__), "..")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_promote_verdicts():
    tool = load("promote_staged")
    assert tool.verdict({"outcome": review.OUTCOME_DRY_RUN}, {"status": "ok"}) == "pass"
    assert tool.verdict({"outcome": review.OUTCOME_NEEDS_MANUAL}, {"status": "ok"}) == "blocked"
    assert tool.verdict({"outcome": review.OUTCOME_DRY_RUN}, {"status": "drift"}) == "drift"
    assert tool.verdict({"outcome": review.OUTCOME_FAILED}, None) == "error"


def test_promote_never_submits_and_uses_dummy_identity():
    tool = load("promote_staged")
    assert tool.DUMMY.emails == ["jane.doe@example.com"]
    src = open(os.path.join(ROOT, "tools", "promote_staged.py")).read()
    assert "dry_run=True" in src and "dry_run=False" not in src


def test_prober_waits_for_the_form_and_reports_react_comboboxes():
    tool = load("probe_broker_forms")
    assert "role=combobox" in tool.JS and "nth-of-type" in tool.JS
    src = open(os.path.join(ROOT, "tools", "probe_broker_forms.py")).read()
    assert 'wait_for_selector("form, input, select, textarea"' in src
    assert "user_agent=" not in src


def test_prober_consent_accept_clicks_first_match_only():
    tool = load("probe_broker_forms")

    class El:
        def __init__(self): self.n = 0
        def click(self): self.n += 1

    els = {"#onetrust-accept-btn-handler": El(), "#truste-consent-button": El()}

    class Page:
        def query_selector(self, s): return els.get(s)
        def wait_for_timeout(self, ms): pass

    assert tool.accept_consent(Page()) is True
    assert [e.n for e in els.values()] == [1, 0]
    assert tool.accept_consent(type("P", (), {"query_selector": lambda s, x: None,
                                              "wait_for_timeout": lambda s, m: None})()) is False


def test_prober_consent_is_opt_in():
    src = open(os.path.join(ROOT, "tools", "probe_broker_forms.py")).read()
    assert '"--accept-consent" in argv' in src
