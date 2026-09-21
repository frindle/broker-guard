"""Adversarial fixture for: bg-dashboard-s2-identity-view-and-update

INTEGRATION fixture: drives the real FastAPI app through TestClient, asserting
status codes AND response bodies. Cases separate "did the job" from "made the
test go green":

  * GET /identity renders a pre-filled form (values present, lists joined)
  * POST /identity round-trips: 303 redirect + JSON on disk in load_profile shape
    with list fields split back into real lists
  * blank first_name/last_name -> 422 (not 500), file untouched
  * missing profile.json -> GET is a clean 4xx, not a 500
  * regression: the existing index route still serves

Each case: (description, callable_returning_actual, expected)
"""
import json
import os
import sys
import tempfile
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/webui.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)

from fastapi.testclient import TestClient  # noqa: E402


def _client_with_profile(profile_dict):
    """Point PROFILE_PATH at a temp file holding profile_dict (or None for a
    missing file) and return a fresh TestClient."""
    tmp = tempfile.mkdtemp(prefix="bg-identity-fixture-")
    path = os.path.join(tmp, "profile.json")
    if profile_dict is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile_dict, f)
    target.PROFILE_PATH = path
    return TestClient(target.app), path


BASE_PROFILE = {
    "first_name": "Jane",
    "middle_name": "Q",
    "last_name": "Doe",
    "emails": ["jane.doe@example.com"],
    "phones": ["+1-555-0100"],
    "addresses": ["Springfield, IL"],
}


def case_get_prefilled():
    client, _ = _client_with_profile(BASE_PROFILE)
    r = client.get("/identity")
    body = r.text
    return all([
        r.status_code == 200,
        'name="first_name"' in body and "Jane" in body,
        'name="last_name"' in body and "Doe" in body,
        'name="middle_name"' in body and "Q" in body,
        'name="emails"' in body and "jane.doe@example.com" in body,
        'name="phones"' in body and "+1-555-0100" in body,
        'name="addresses"' in body and "Springfield, IL" in body,
    ])


def case_post_roundtrip():
    client, path = _client_with_profile(BASE_PROFILE)
    r = client.post("/identity", data={
        "first_name": "John",
        "middle_name": "",
        "last_name": "Smith",
        "emails": "a@x.com\nb@y.com",
        "phones": "+1-555-0200\n+1-555-0300",
        "addresses": "Reno, NV\nLas Vegas, NV",
    })
    with open(path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    return all([
        r.status_code == 303,
        r.headers.get("location") == "/identity",
        saved["first_name"] == "John",
        saved["last_name"] == "Smith",
        saved["emails"] == ["a@x.com", "b@y.com"],
        saved["phones"] == ["+1-555-0200", "+1-555-0300"],
        saved["addresses"] == ["Reno, NV", "Las Vegas, NV"],
    ])


def case_post_blank_names_rejected():
    client, path = _client_with_profile(BASE_PROFILE)
    r = client.post("/identity", data={
        "first_name": "   ",
        "middle_name": "",
        "last_name": "",
        "emails": "x@y.com",
        "phones": "",
        "addresses": "",
    })
    with open(path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    return all([
        r.status_code == 422,
        saved["first_name"] == "Jane",   # file untouched on rejection
        saved["last_name"] == "Doe",
    ])


def case_get_missing_profile_is_4xx():
    client, _ = _client_with_profile(None)
    r = client.get("/identity")
    return 400 <= r.status_code < 500


def case_index_still_serves():
    client, _ = _client_with_profile(BASE_PROFILE)
    r = client.get("/")
    return all([r.status_code == 200, "Broker Guard" in r.text])


CASES = [
    ("GET /identity renders a form pre-filled with the profile", case_get_prefilled, True),
    ("POST /identity -> 303 to /identity and JSON on disk with lists split back", case_post_roundtrip, True),
    ("POST with blank first/last name -> 422 and file untouched", case_post_blank_names_rejected, True),
    ("GET /identity with no profile.json is a clean 4xx, not a 500", case_get_missing_profile_is_4xx, True),
    ("regression: index route still serves the dashboard", case_index_still_serves, True),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
