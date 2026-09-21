# TASK: bg-dashboard-s1-app-and-status

## Confirmed defect (observed, not suspected)

Confirmed gap, not a regression: `broker_guard/webui.py` does not exist in
this repo (verified: no file of that name, and `grep`-ing the repo for
`escalation_countdowns()` / `load_health_summary()` calls outside
`broker_guard/webui_data.py` itself returns nothing). Those two functions
are fully implemented and already tested in `webui_data.py`, but nothing
anywhere calls them -- there is no web app, so no one can see broker-guard's
status. This slice creates the app and its first page.

## Entry point

broker_guard/webui.py (new file, created by this slice)

## Required change

Real signatures (read from `broker_guard/webui_data.py`, do not guess):
`escalation_countdowns(records: list[dict], now_iso: str) -> list[dict]` and
`load_health_summary(lines: list[str]) -> dict`. Neither is zero-arg.

In broker_guard/webui.py: `import broker_guard.webui_data as webui_data` (module
import, NOT `from ... import escalation_countdowns` -- the test monkeypatches
`broker_guard.webui_data.escalation_countdowns` as a module attribute, which a
`from`-import would silently bypass). Create `app = FastAPI(title="Broker Guard")`.
Add a module-level `HEALTH_LOG_PATH = "logs/health.jsonl"` constant. Add `GET /`
returning HTMLResponse: read `HEALTH_LOG_PATH`'s lines with `.readlines()` if the
file exists else `[]`, call `webui_data.load_health_summary(lines)`; there is no
persisted escalation-records store yet, so call
`webui_data.escalation_countdowns([], datetime.now(timezone.utc).isoformat())`
(an empty list is the correct, honest input until a real store exists -- this is
not a stub, it is what "no escalations recorded yet" means). Render a simple HTML
page (a `<h1>Broker Guard</h1>` heading, a `<table>` row per escalation countdown
dict -- broker_id/stage/deadline_iso/seconds_remaining/overdue -- and a
health-summary block showing total/ok/failed) via an f-string or string .format --
no external template files, no Jinja2. If either data function raises, GET / must
still return 200 with an empty/placeholder table, never 500 (wrap each call in its
own try/except).

Behaviour that must NOT change:
- `broker_guard/webui_data.py` and `broker_guard/profile.py` are read-only from this
  slice's point of view -- call their functions, do not edit them.
- GET / must return 200 in every case (empty data, exception from either data
  function) -- a dashboard that 500s on empty data is worse than one with no data.

## Must contain

- `FastAPI(title="Broker Guard")`
- `webui_data.escalation_countdowns(`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Edit `broker_guard/webui.py`. You may also add `fastapi` and `httpx` to
`requirements.txt` (FastAPI is a new dependency this slice introduces; `httpx`
is required by `fastapi.testclient.TestClient`) -- do not edit `verify.sh`,
`test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
