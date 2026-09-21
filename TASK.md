# TASK: bg-dashboard-s2-identity-view-and-update

## Confirmed defect (observed, not suspected)

The dashboard app (`broker_guard/webui.py`) only serves the index route. A
request to `GET /identity` returns 404 -- there is no page to view or edit the
operator's Identity profile, even though `broker_guard.profile.load_profile`
already reads a JSON profile and the rest of the system depends on that file
being correct. Reproduced: `TestClient(app).get("/identity")` -> 404 against
the current worktree.

## Entry point

broker_guard/webui.py:13 (end of module, after the existing `index()` route)

## Required change

In broker_guard/webui.py: `GET /identity` calls `broker_guard.profile.load_profile(path)` (a module-level PROFILE_PATH constant, default e.g. 'profile.json') and renders an HTML form pre-filled with the Identity's first_name, last_name, emails, phones, addresses (join list fields with newlines in a <textarea> or comma-join in an <input>). `POST /identity` accepts those same form fields (FastAPI Form(...) params), writes them back to PROFILE_PATH as the same JSON shape load_profile reads (first_name, middle_name, last_name, emails, phones, addresses -- list fields split back from the submitted text), then redirects (303) to /identity so the page reflects the update. Do not add a new function to profile.py -- read/write the JSON directly in webui.py using the same shape load_profile expects.

Behaviour that must NOT change:
- The existing `GET /` index route keeps serving the escalation/health dashboard (200, "Broker Guard" page).
- A POST with blank first_name or last_name is rejected with a 4xx (not a 500) and does not overwrite the profile file.
- GET /identity when the profile file is missing/unreadable returns a clean 4xx, never a 500.

## Must contain

- `PROFILE_PATH = 'profile.json'`
- `@app.get("/identity", response_class=HTMLResponse)`
- `def identity_view():`
- `profile.load_profile(PROFILE_PATH)`
- `@app.post("/identity")`
- `def identity_update(`
- `first_name: str = Form("")`
- `RedirectResponse(url="/identity", status_code=303)`

## Scope

Only edit `broker_guard/webui.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to the implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
