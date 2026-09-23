"""Tests for broker_guard.broker_normalize.

All records used here are fake/invalid-TLD, matching this repo's existing
convention of never putting real broker/identity data in tests.
"""
import json
import os

from broker_guard import brokers as brokers_mod
from broker_guard.broker_normalize import (
    VALID_KINDS,
    classify_kind,
    ensure_brokers_file,
    match_eraser_id,
    normalize_dataset,
    normalize_record,
    slugify,
)

REAL_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data-broker-optout-list", "brokers.json",
)


# --- classify_kind: one case per documented branch -------------------------

def test_confirmed_government_id_text_is_photo_id():
    record = {"opt_out_method": "web-form",
               "verification_step": "requires proof of identity (SSN/DOB; some require government-ID upload or notarization)"}
    assert classify_kind(record) == "photo_id"


def test_confirmed_ssn_text_is_photo_id():
    record = {"opt_out_method": "web-form", "verification_step": "SSN required"}
    assert classify_kind(record) == "photo_id"


def test_confirmed_captcha_text_is_captcha():
    record = {"opt_out_method": "web-form", "verification_step": "CAPTCHA required on the opt-out form"}
    assert classify_kind(record) == "captcha"


def test_id_marker_wins_over_captcha_when_both_present():
    record = {"opt_out_method": "web-form",
               "verification_step": "CAPTCHA required on the opt-out form; requires proof of identity (SSN/DOB)"}
    assert classify_kind(record) == "photo_id"


def test_confirmed_phone_verification_text_is_kba():
    record = {"opt_out_method": "web-form", "verification_step": "phone verification (call / code) required"}
    assert classify_kind(record) == "kba"


def test_phone_opt_out_method_is_kba_even_without_matching_text():
    record = {"opt_out_method": "phone", "verification_step": ""}
    assert classify_kind(record) == "kba"


def test_unconfirmed_placeholder_text_does_not_trigger_captcha_keyword():
    # The real dataset's placeholder literally contains the word "CAPTCHA"
    # as generic advice ("check the form for CAPTCHA/ID/email-confirmation
    # requirements") -- this must NOT be read as a confirmed CAPTCHA finding.
    record = {
        "opt_out_method": "web-form",
        "verification_step": "not individually confirmed — check the form for "
                              "CAPTCHA/ID/email-confirmation requirements",
    }
    assert classify_kind(record) == "automatable"


def test_unconfirmed_with_email_method_is_automatable():
    record = {
        "opt_out_method": "email",
        "verification_step": "not individually confirmed — some brokers reply "
                              "requesting further proof before acting",
    }
    assert classify_kind(record) == "automatable"


def test_web_form_or_email_with_no_heavy_gate_is_automatable():
    assert classify_kind({"opt_out_method": "web-form", "verification_step": ""}) == "automatable"
    assert classify_kind({"opt_out_method": "email", "verification_step": "email confirmation link required to finalize"}) == "automatable"


def test_unknown_method_falls_back_to_kba():
    assert classify_kind({"opt_out_method": "unknown", "verification_step": "unknown"}) == "kba"


def test_classify_kind_always_returns_a_valid_kind():
    for method in ("web-form", "email", "phone", "unknown", ""):
        for text in ("", "unknown", "CAPTCHA required", "SSN required",
                     "not individually confirmed — check the form for CAPTCHA/ID/email-confirmation requirements"):
            assert classify_kind({"opt_out_method": method, "verification_step": text}) in VALID_KINDS


# --- drop rule ---------------------------------------------------------

def test_unknown_method_with_no_channel_is_dropped():
    record = {"name": "Dead End Co", "domain": "deadend.invalid",
               "opt_out_method": "unknown", "opt_out_url": None, "opt_out_email": None,
               "category": "marketing", "verification_step": "unknown"}
    assert normalize_record(record, {"by_domain": {}, "by_email": {}, "by_name": {}}) is None


def test_unknown_method_with_a_url_is_kept():
    record = {"name": "Still Reachable Co", "domain": "reachable.invalid",
               "opt_out_method": "unknown", "opt_out_url": "https://reachable.invalid/optout",
               "opt_out_email": None, "category": "marketing", "verification_step": "unknown"}
    out = normalize_record(record, {"by_domain": {}, "by_email": {}, "by_name": {}})
    assert out is not None
    assert out["verification"] == "kba"


# --- id / url derivation -------------------------------------------------

def test_id_and_url_derived_from_domain():
    record = {"name": "Example People Search", "domain": "www.example-broker.invalid",
               "opt_out_method": "web-form", "opt_out_url": "https://example-broker.invalid/optout",
               "opt_out_email": None, "category": "people-search", "verification_step": ""}
    out = normalize_record(record, {"by_domain": {}, "by_email": {}, "by_name": {}})
    assert out["id"] == "example-broker-invalid"
    assert out["url"] == "https://example-broker.invalid"


def test_id_falls_back_to_name_slug_when_no_domain():
    record = {"name": "No Domain Co.", "domain": None,
               "opt_out_method": "email", "opt_out_url": None,
               "opt_out_email": "privacy@somewhere-else.invalid",
               "category": "marketing", "verification_step": ""}
    out = normalize_record(record, {"by_domain": {}, "by_email": {}, "by_name": {}})
    assert out["id"] == slugify("No Domain Co.")
    assert out["url"] == ""


# --- eraser_id matching --------------------------------------------------

def test_match_eraser_id_prefers_domain_over_email_and_name():
    index = {
        "by_domain": {"broker.invalid": "eraser-broker"},
        "by_email": {"privacy@broker.invalid": "wrong-email-match"},
        "by_name": {"broker": "wrong-name-match"},
    }
    record = {"domain": "broker.invalid", "opt_out_email": "privacy@broker.invalid", "name": "Broker"}
    assert match_eraser_id(record, index) == "eraser-broker"


def test_match_eraser_id_falls_back_to_email_then_name():
    index = {"by_domain": {}, "by_email": {"jean@else.invalid": "eraser-by-email"}, "by_name": {}}
    record = {"domain": None, "opt_out_email": "jean@else.invalid", "name": "Some Co"}
    assert match_eraser_id(record, index) == "eraser-by-email"

    # "Some Co" normalizes to "some" -- the trailing " co" legal suffix is
    # stripped (see _NAME_SUFFIXES) before the exact-match lookup.
    index2 = {"by_domain": {}, "by_email": {}, "by_name": {"some": "eraser-by-name"}}
    record2 = {"domain": None, "opt_out_email": None, "name": "Some Co"}
    assert match_eraser_id(record2, index2) == "eraser-by-name"


def test_match_eraser_id_returns_none_when_nothing_matches():
    index = {"by_domain": {}, "by_email": {}, "by_name": {}}
    record = {"domain": "nowhere.invalid", "opt_out_email": None, "name": "Nobody"}
    assert match_eraser_id(record, index) is None


# --- normalize_dataset: dedup / stats shape -------------------------------

def test_normalize_dataset_stats_and_dedup(tmp_path):
    source = [
        {"name": "Alpha", "domain": "alpha.invalid", "opt_out_method": "web-form",
         "opt_out_url": "https://alpha.invalid/optout", "opt_out_email": None,
         "category": "people-search", "verification_step": ""},
        {"name": "Alpha Dupe", "domain": "alpha.invalid", "opt_out_method": "web-form",
         "opt_out_url": "https://alpha.invalid/optout2", "opt_out_email": None,
         "category": "people-search", "verification_step": ""},
        {"name": "Dead End", "domain": None, "opt_out_method": "unknown",
         "opt_out_url": None, "opt_out_email": None,
         "category": "marketing", "verification_step": "unknown"},
    ]
    out, stats = normalize_dataset(source, [])
    assert stats["total_source_records"] == 3
    assert stats["dropped"] == 1
    assert stats["id_collisions_dropped"] == 1
    assert stats["total_output_records"] == 1
    assert out[0]["name"] == "Alpha"


# --- end-to-end against the real 853-record dataset -----------------------

def test_real_dataset_normalizes_and_loads_cleanly_via_load_brokers(tmp_path):
    """The task's core acceptance test: every normalized record must be
    accepted by brokers.load_brokers() with zero ValueErrors."""
    if not os.path.exists(REAL_SOURCE_PATH):
        import pytest
        pytest.skip(f"real dataset not present at {REAL_SOURCE_PATH}")

    with open(REAL_SOURCE_PATH, "r", encoding="utf-8") as fh:
        source_records = json.load(fh)
    assert len(source_records) == 853

    out, stats = normalize_dataset(source_records, [])
    assert stats["total_source_records"] == 853
    assert stats["dropped"] == 26
    assert stats["total_output_records"] == 827

    out_path = tmp_path / "normalized.json"
    out_path.write_text(json.dumps({"brokers": out}), encoding="utf-8")

    loaded = brokers_mod.load_brokers(str(out_path))
    assert len(loaded) == 827
    for broker in loaded:
        assert brokers_mod.verification_kind(broker) in VALID_KINDS


# --- ensure_brokers_file -----------------------------------------------------

BUNDLED_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "source-brokers.json",
)


def test_ensure_brokers_file_generates_from_bundled_source_when_missing(tmp_path):
    if not os.path.exists(BUNDLED_SOURCE_PATH):
        import pytest
        pytest.skip(f"bundled source dataset not present at {BUNDLED_SOURCE_PATH}")

    target = tmp_path / "brokers.json"
    generated = ensure_brokers_file(str(target), source_path=BUNDLED_SOURCE_PATH,
                                     eraser_brokers_path=str(tmp_path / "no-such-eraser-list.yaml"))
    assert generated is True
    assert target.exists()

    loaded = brokers_mod.load_brokers(str(target))
    # 969 source records (853 original + 15 manually-researched additions + 102
    # from the Incogni-gap pass: 50 CourtRecords.us state sites, 37 people-search
    # brands, 15 B2B marketing-data companies -- see data/source-brokers.json;
    # then the incogni-b2b-research pass added ASL Marketing and merged away two
    # duplicate pairs, Clay Labs and Ekata: 970 + 1 - 2 = 969),
    # 26 dropped for no actionable channel -> 943.
    assert len(loaded) == 943


def test_ensure_brokers_file_never_touches_an_existing_file(tmp_path):
    target = tmp_path / "brokers.json"
    target.write_text(json.dumps({"brokers": [{"id": "custom", "name": "Custom", "url": "https://custom.invalid"}]}))
    before = target.read_text(encoding="utf-8")

    generated = ensure_brokers_file(str(target), source_path=BUNDLED_SOURCE_PATH)

    assert generated is False
    assert target.read_text(encoding="utf-8") == before  # byte-for-byte untouched


def test_ensure_brokers_file_missing_source_does_not_raise(tmp_path):
    target = tmp_path / "brokers.json"
    generated = ensure_brokers_file(str(target), source_path=str(tmp_path / "no-such-source.json"))
    assert generated is False
    assert not target.exists()
