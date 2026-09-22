# TASK: bg-dashboard-s3-brokers-matched-removed

## Confirmed defect (observed, not suspected)

`broker_guard/webui.py` has no `/brokers` route at all -- the dashboard only
serves `/`, `/identity`. The presence history that `webui_data.query_presence_history`
already knows how to read is unreachable from the web UI: there is no page that
opens the state db and renders the `presence` rows.

## Entry point

broker_guard/webui.py (module level -- add the route next to the existing `/`,
`/identity` routes)

## Required change

In broker_guard/webui.py: `query_presence_history(conn, broker_id=None, limit=200)` is not zero-arg -- it needs a sqlite3 connection. Add a module-level `PRESENCE_DB_PATH` constant; `GET /brokers` opens it with `broker_guard.state.init_db(PRESENCE_DB_PATH)` and calls `webui_data.query_presence_history(conn)`, and renders one row per presence record showing its broker_id/identity_key and first_seen/last_seen (the actual columns the function returns -- do not invent matched/removed fields it doesn't have). Fewer/zero rows must not crash the page.

Behaviour that must NOT change:
- `/` still renders escalation countdowns + health summary exactly as before.
- `GET /identity` and `POST /identity` keep working unchanged.
- The route goes through `broker_guard.state.init_db` for the connection (the
  fixture patches it) and through `webui_data.query_presence_history` for the
  rows -- do not open sqlite directly or invent a new query function.

## Must contain

- `PRESENCE_DB_PATH = "data/presence.sqlite3"`
- `import broker_guard.state`
- `@app.get("/brokers", response_class=HTMLResponse)`
- `def brokers():`
- `broker_guard.state.init_db(PRESENCE_DB_PATH)`
- `webui_data.query_presence_history(conn)`

## Scope

Only edit `broker_guard/webui.py` (the fix) and `test_fixture.py` (for this
refine pass: add adversarial cases that catch the surviving mutations); do not
edit `verify.sh`. `refimpl.py` and `TASK.md` may change only to keep themselves
accurate. Never weaken or delete existing fixture cases, and never edit the
reference impl to dodge a mutation.

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
