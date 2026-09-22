"""Tests for broker_guard.exposure.

All HTTP is mocked -- these tests never touch the live XposedOrNot/HIBP
APIs (strict daily quotas, and flaky). The password used for the k-anonymity
tests is a fake dummy string, never a real password.
"""
import hashlib
import json

from broker_guard.exposure import (
    XPOSEDORNOT_ATTRIBUTION,
    ExposureCache,
    XposedOrNotClient,
    password_pwned_count,
    profile_exposure,
    summarize_breach_analytics,
)

FAKE_EMAIL = "testy.mctestface@example.invalid"
FAKE_PASSWORD = "definitely-not-a-real-password-123"  # dummy, never a real credential


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class FakeSession:
    """Records calls, returns canned responses keyed by call index."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params})
        if not self.responses:
            raise AssertionError("FakeSession ran out of canned responses")
        return self.responses.pop(0)


def _no_sleep(_seconds):
    pass


# --- check_email -----------------------------------------------------------

def test_check_email_flattens_nested_breach_list(tmp_path):
    session = FakeSession([FakeResponse(200, {"breaches": [["Adobe", "LinkedIn"]]})])
    client = XposedOrNotClient(session=session, sleep=_no_sleep,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert client.check_email(FAKE_EMAIL) == ["Adobe", "LinkedIn"]


def test_check_email_404_means_no_breaches(tmp_path):
    session = FakeSession([FakeResponse(404)])
    client = XposedOrNotClient(session=session, sleep=_no_sleep,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert client.check_email(FAKE_EMAIL) == []


def test_check_email_uses_cache_on_second_call(tmp_path):
    session = FakeSession([FakeResponse(200, {"breaches": [["Adobe"]]})])
    cache = ExposureCache(str(tmp_path / "cache.json"))
    client = XposedOrNotClient(session=session, sleep=_no_sleep, cache=cache)
    assert client.check_email(FAKE_EMAIL) == ["Adobe"]
    # Second call must NOT hit the (now-empty) session again.
    assert client.check_email(FAKE_EMAIL) == ["Adobe"]
    assert len(session.calls) == 1


def test_check_email_cache_is_shared_across_client_instances(tmp_path):
    cache_path = str(tmp_path / "cache.json")
    session1 = FakeSession([FakeResponse(200, {"breaches": [["Adobe"]]})])
    XposedOrNotClient(session=session1, sleep=_no_sleep, cache=ExposureCache(cache_path)).check_email(FAKE_EMAIL)

    session2 = FakeSession([])  # must not be called at all
    result = XposedOrNotClient(session=session2, sleep=_no_sleep,
                                cache=ExposureCache(cache_path)).check_email(FAKE_EMAIL)
    assert result == ["Adobe"]
    assert session2.calls == []


def test_check_email_backs_off_on_429_then_succeeds(tmp_path):
    session = FakeSession([FakeResponse(429), FakeResponse(200, {"breaches": [["Adobe"]]})])
    client = XposedOrNotClient(session=session, sleep=_no_sleep, attempts=3, base_delay=0.01,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert client.check_email(FAKE_EMAIL) == ["Adobe"]
    assert len(session.calls) == 2


def test_check_email_gives_up_after_repeated_5xx(tmp_path):
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    client = XposedOrNotClient(session=session, sleep=_no_sleep, attempts=3, base_delay=0.01,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert client.check_email(FAKE_EMAIL) == []


# --- breach_analytics / summarize_breach_analytics --------------------------

CANNED_ANALYTICS = {
    "ExposedBreaches": {
        "breaches_details": [
            {"breach": "Adobe", "domain": "adobe.com", "xposed_date": "2013",
             "industry": "Technology", "xposed_data": "Email, Password, Username"},
            {"breach": "LinkedIn", "domain": "linkedin.com", "xposed_date": "2012",
             "industry": "Social Media", "xposed_data": "Email, Password"},
        ]
    },
    "BreachMetrics": {"risk": [{"risk_score": 7, "risk_label": "High"}]},
}


def test_breach_analytics_returns_raw_payload(tmp_path):
    session = FakeSession([FakeResponse(200, CANNED_ANALYTICS)])
    client = XposedOrNotClient(session=session, sleep=_no_sleep,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert client.breach_analytics(FAKE_EMAIL) == CANNED_ANALYTICS


def test_summarize_breach_analytics_extracts_expected_rows():
    rows = summarize_breach_analytics(CANNED_ANALYTICS)
    assert len(rows) == 2
    assert rows[0] == {"name": "Adobe", "domain": "adobe.com", "year": "2013",
                        "industry": "Technology", "exposed_data": "Email, Password, Username",
                        "risk_score": 7}
    assert rows[1]["name"] == "LinkedIn"


def test_summarize_breach_analytics_handles_missing_keys_gracefully():
    assert summarize_breach_analytics({}) == []
    assert summarize_breach_analytics({"ExposedBreaches": {}}) == []
    assert summarize_breach_analytics(None) == []


# --- password_pwned_count: k-anonymity -------------------------------------

def test_password_pwned_count_matches_known_suffix_in_range_response():
    digest = hashlib.sha1(FAKE_PASSWORD.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    body = f"{suffix}:42\r\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:1\r\n"
    session = FakeSession([FakeResponse(200, text=body)])
    count = password_pwned_count(FAKE_PASSWORD, session=session)
    assert count == 42
    # Only the 5-char prefix must appear in the request -- never the full
    # password or the full hash.
    called_url = session.calls[0]["url"]
    assert prefix in called_url
    assert FAKE_PASSWORD not in called_url
    assert digest not in called_url
    assert suffix not in called_url


def test_password_pwned_count_returns_zero_when_suffix_not_in_range_response():
    body = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:1\r\nBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB:2\r\n"
    session = FakeSession([FakeResponse(200, text=body)])
    assert password_pwned_count(FAKE_PASSWORD, session=session) == 0


def test_password_pwned_count_returns_zero_on_bad_status():
    session = FakeSession([FakeResponse(503, text="")])
    assert password_pwned_count(FAKE_PASSWORD, session=session) == 0


def test_password_pwned_count_rejects_empty_password():
    import pytest
    with pytest.raises(ValueError):
        password_pwned_count("")


# --- profile_exposure --------------------------------------------------

def test_profile_exposure_maps_each_email(tmp_path):
    session = FakeSession([
        FakeResponse(200, {"breaches": [["Adobe"]]}),
        FakeResponse(404),
    ])
    client = XposedOrNotClient(session=session, sleep=_no_sleep,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    result = profile_exposure([FAKE_EMAIL, "other@example.invalid"], client=client)
    assert result == {FAKE_EMAIL: ["Adobe"], "other@example.invalid": []}


def test_profile_exposure_skips_blank_emails(tmp_path):
    client = XposedOrNotClient(session=FakeSession([]), sleep=_no_sleep,
                                cache=ExposureCache(str(tmp_path / "cache.json")))
    assert profile_exposure(["", None], client=client) == {}


# --- attribution constant --------------------------------------------------

def test_attribution_constant_mentions_xposedornot():
    assert "XposedOrNot" in XPOSEDORNOT_ATTRIBUTION


# --- ExposureCache -----------------------------------------------------

def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    cache = ExposureCache(str(tmp_path / "cache.json"), ttl_seconds=1)
    cache.set("check_email", FAKE_EMAIL, ["Adobe"])
    assert cache.get("check_email", FAKE_EMAIL) == ["Adobe"]

    import broker_guard.exposure as exposure_mod
    real_time = exposure_mod.time.time
    monkeypatch.setattr(exposure_mod.time, "time", lambda: real_time() + 10)
    assert cache.get("check_email", FAKE_EMAIL) is None


def test_cache_file_is_owner_only(tmp_path):
    import stat
    path = tmp_path / "cache.json"
    cache = ExposureCache(str(path))
    cache.set("check_email", FAKE_EMAIL, ["Adobe"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_cache_survives_corrupt_file(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("not json", encoding="utf-8")
    cache = ExposureCache(str(path))
    assert cache.get("check_email", FAKE_EMAIL) is None
    cache.set("check_email", FAKE_EMAIL, ["Adobe"])
    assert cache.get("check_email", FAKE_EMAIL) == ["Adobe"]
