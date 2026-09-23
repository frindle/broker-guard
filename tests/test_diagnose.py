"""Tests for the single-broker live diagnosis tool (broker_guard/diagnose.py).

What is deliberately NOT tested here: a real SearXNG query or a real
Chromium navigation. Those are live-integration concerns by definition --
the whole point of the tool is to report what the live backends do today.
What IS tested is everything around them that can be wrong silently:

* broker lookup, including the refuse-rather-than-guess ambiguity rule,
* that every identity a real scan would sweep is actually covered,
* that the production legs' swallowed exceptions are surfaced in full,
* that nothing is written to progress / state.sqlite,
* and that the detection layer is always closed, even when it blows up.
"""
import json

import pytest

from broker_guard import diagnose, profile as profile_mod


@pytest.fixture
def broker_list(brokers_dict):
    return list(brokers_dict["brokers"])


@pytest.fixture
def identity():
    return profile_mod.Identity(first_name="Testy", last_name="Mctestface",
                                emails=["testy@example.invalid"],
                                phones=["+1-555-0100"])


# --- lookup -------------------------------------------------------------

def test_exact_id_wins(broker_list):
    assert diagnose.find_broker(broker_list, "beta")["name"] == "Beta Search"


def test_exact_name_is_case_insensitive(broker_list):
    assert diagnose.find_broker(broker_list, "alpha people")["id"] == "alpha"


def test_substring_match(broker_list):
    assert diagnose.find_broker(broker_list, "Records")["id"] == "gamma"


def test_no_match_raises(broker_list):
    with pytest.raises(diagnose.BrokerLookupError) as excinfo:
        diagnose.find_broker(broker_list, "nobody here")
    assert excinfo.value.candidates == []


def test_ambiguous_substring_refuses_and_reports_candidates(broker_list):
    # "a" is in every one of these names -- the tool must not pick one.
    with pytest.raises(diagnose.BrokerLookupError) as excinfo:
        diagnose.find_broker(broker_list, "a")
    ids = {b["id"] for b in excinfo.value.candidates}
    assert ids == {"alpha", "beta", "gamma"}


def test_exact_id_beats_an_otherwise_ambiguous_substring():
    # 'acme' is a substring of 'acme-plus', so substring matching alone would
    # call this ambiguous. An exact id must still resolve.
    brokers = [
        {"id": "acme", "name": "Acme", "url": "https://acme.invalid"},
        {"id": "acme-plus", "name": "Acme Plus", "url": "https://acme2.invalid"},
    ]
    assert diagnose.find_broker(brokers, "acme")["id"] == "acme"


def test_empty_query_raises(broker_list):
    with pytest.raises(diagnose.BrokerLookupError):
        diagnose.find_broker(broker_list, "   ")


# --- the diagnosis itself ----------------------------------------------

class _Deps:
    def __init__(self, searx_search=None, page_action=None):
        self.searx_search = searx_search
        self.page_action = page_action
        self.closed = 0

    def close(self):
        self.closed += 1


def _run(broker, identities, deps):
    lines = []
    code = diagnose.diagnose_broker(broker, identities, deps, write=lines.append)
    return code, "\n".join(lines)


def test_both_legs_disabled_reports_disabled_not_absent(broker_list, identity):
    code, out = _run(broker_list[0], [identity], _Deps())
    assert "SERP leg:    DISABLED" in out
    assert "browser leg: DISABLED" in out
    assert code == 0


def test_serp_backend_exception_is_shown_in_full_not_collapsed(broker_list, identity):
    def exploding_search(query):
        raise RuntimeError("searxng returned 429 for every engine")

    code, out = _run(broker_list[0], [identity], _Deps(searx_search=exploding_search))
    # run_serpwatch itself swallows this into outcome='error' and logs only the
    # exception CLASS. The diagnostic must still show the message + traceback.
    assert "outcome='error'" in out
    assert "searxng returned 429 for every engine" in out
    assert "Traceback" in out
    assert code == 1


def test_identical_repeated_tracebacks_collapse_but_keep_the_count(broker_list):
    """One dead SearXNG raises the same traceback once per query term. The
    output must not repeat it verbatim N times -- but it must still say N."""
    multi_term = profile_mod.Identity(
        first_name="Testy", last_name="Mctestface", middle_name="Q",
        emails=["a@example.invalid", "b@example.invalid"],
        phones=["+1-555-0100"])

    def exploding_search(query):
        raise RuntimeError("connection refused")

    code, out = _run(broker_list[0], [multi_term], _Deps(searx_search=exploding_search))
    # Five queries (2 name variants + 2 emails + 1 phone), one printed block.
    assert "queries=5" in out
    assert out.count("Traceback (most recent call last)") == 1
    assert "SERP backend exception x5" in out
    assert code == 1


def test_browser_backend_exception_is_shown_in_full(broker_list, identity):
    def exploding_page(check):
        raise RuntimeError("target page closed unexpectedly")

    code, out = _run(broker_list[0], [identity], _Deps(page_action=exploding_page))
    assert "target page closed unexpectedly" in out
    assert "Traceback" in out
    assert code == 1


def test_clean_browser_check_reports_the_full_result_dict(broker_list, identity):
    code, out = _run(broker_list[0], [identity],
                     _Deps(page_action=lambda check: {"found": False}))
    assert "'checked': True" in out
    assert "'present': False" in out
    assert code == 0


def test_non_automatable_broker_reports_no_opinion(broker_list, identity):
    gamma = [b for b in broker_list if b["id"] == "gamma"][0]  # photo_id
    code, out = _run(gamma, [identity], _Deps(page_action=lambda check: {"found": True}))
    assert "NO OPINION" in out


def test_every_identity_is_covered(broker_list):
    identities = [
        profile_mod.Identity(first_name="Aa", last_name="One"),
        profile_mod.Identity(first_name="Bb", last_name="Two"),
        profile_mod.Identity(first_name="Cc", last_name="Three"),
    ]
    seen = []

    def page_action(check):
        seen.append(tuple(check["terms"]))
        return {"found": False}

    code, out = _run(broker_list[0], identities, _Deps(page_action=page_action))
    assert len(seen) == 3
    assert "identity 3/3" in out
    assert code == 0


def test_no_identities_is_an_error_not_a_silent_clean_run(broker_list):
    code, out = _run(broker_list[0], [], _Deps())
    assert "NO IDENTITIES TO SCAN" in out
    assert code == 1


def test_diagnosis_never_records_an_outcome(broker_list, identity, monkeypatch):
    """The dashboard's stored state must be untouched: the tool calls
    _check_serp/_check_browser directly, never _check_pair, so nothing can
    reach progress.record_outcome."""
    from broker_guard import progress as progress_mod

    def boom(*args, **kwargs):
        raise AssertionError("diagnose must not write progress/state")

    monkeypatch.setattr(progress_mod.ScanProgress, "record_outcome", boom,
                        raising=False)
    code, _ = _run(broker_list[0], [identity],
                   _Deps(page_action=lambda check: {"found": False}))
    assert code == 0


# --- run(): wiring, and teardown on failure ----------------------------

@pytest.fixture
def cfg(tmp_path, brokers_dict, monkeypatch):
    from broker_guard import config as config_mod

    brokers_path = tmp_path / "brokers.json"
    brokers_path.write_text(json.dumps(brokers_dict), encoding="utf-8")
    profiles_path = tmp_path / "profiles.json"
    profile_path = tmp_path / "profile.local.json"
    profile_path.write_text(json.dumps({
        "first_name": "Testy", "last_name": "Mctestface",
        "emails": ["testy@example.invalid"],
    }), encoding="utf-8")
    monkeypatch.setenv("BG_BROKERS_PATH", str(brokers_path))
    monkeypatch.setenv("BG_PROFILES_PATH", str(profiles_path))
    monkeypatch.setenv("BG_PROFILE_PATH", str(profile_path))
    monkeypatch.setenv("BG_STATE_PATH", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path / "logs"))
    return config_mod.load_config()


def test_run_loads_identities_the_same_way_a_real_scan_does(cfg, monkeypatch):
    """The legacy-profile fallback inside load_scan_identities must apply --
    a deployment that never opened the Profile page still has an identity."""
    monkeypatch.setattr("broker_guard.service.build_detection",
                        lambda c: (None, None, []))
    lines = []
    code = diagnose.run(cfg, "alpha", write=lines.append)
    out = "\n".join(lines)
    assert "Testy Mctestface" in out
    assert "identities:   1" in out
    assert code == 0


def test_run_reports_ambiguity_and_exits_2(cfg, monkeypatch):
    monkeypatch.setattr("broker_guard.service.build_detection",
                        lambda c: (None, None, []))
    lines = []
    assert diagnose.run(cfg, "a", write=lines.append) == 2
    out = "\n".join(lines)
    assert "ambiguous" in out
    assert "Alpha People" in out
    # ...and the detection layer was never even built for a failed lookup.


def test_run_closes_the_detection_layer_even_when_the_diagnosis_raises(cfg, monkeypatch):
    closed = []
    monkeypatch.setattr("broker_guard.service.build_detection",
                        lambda c: (None, None, [lambda: closed.append(1)]))

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(diagnose, "diagnose_broker", boom)
    with pytest.raises(RuntimeError):
        diagnose.run(cfg, "alpha", write=lambda line: None)
    assert closed == [1], "a leaked closer means a leaked Chromium"


def test_run_closes_the_detection_layer_on_success(cfg, monkeypatch):
    closed = []
    monkeypatch.setattr("broker_guard.service.build_detection",
                        lambda c: (None, None, [lambda: closed.append(1)]))
    diagnose.run(cfg, "alpha", write=lambda line: None)
    assert closed == [1]


def test_cli_flag_dispatches_to_diagnose_and_does_not_start_the_loop(base_env, monkeypatch):
    from broker_guard import service

    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr("broker_guard.diagnose.run",
                        lambda cfg, query, **kw: calls.append(query) or 0)
    assert service.main(["--diagnose-broker", "Alpha People"]) == 0
    assert calls == ["Alpha People"]
