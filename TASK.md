# TASK: bg-webui-s1-presence-query

## Confirmed defect (observed, not suspected)

`broker_guard/webui_data.py` exists only as a one-line placeholder stub
(`"""Stub for broker_guard/webui_data.py -- implement per TASK.md."""`) with no
callable at all. The webUI needs a read-only query over the presence state db
and there is nothing to import: `python3 -c "import ast; ..."` shows the module
defines zero functions, so any caller of `query_presence_history` fails with
ImportError/AttributeError.

## Entry point

broker_guard/webui_data.py:1 (the whole file is the stub)

## Required change

Create broker_guard/webui_data.py with query_presence_history(conn: sqlite3.Connection, broker_id: str | None = None, limit: int = 200) -> list[dict]. Reads the existing presence table (columns: broker_id, identity_key, present, checked_at -- see broker_guard/state.py's schema for the exact table/column names) ordered by checked_at DESC, optionally filtered to one broker_id, capped at limit rows, each row returned as a dict with those column names as keys.

Concretely, against the real schema in `broker_guard/state.py` (table
`presence`, columns `identity_key`, `broker_id`, `first_seen`, `last_seen`;
the "checked_at" check-time column is `last_seen`):

- Run one parameterised SELECT over `presence`.
- When `broker_id` is not None, add `WHERE broker_id = ?`; when it is None,
  return rows for all brokers.
- Order by `last_seen DESC` (newest check first).
- Cap the result at `limit` rows (`LIMIT ?`, default 200).
- Return a plain Python list of dicts, one per row, keyed exactly by the four
  column names: `identity_key`, `broker_id`, `first_seen`, `last_seen`.
- Empty table or unknown broker_id returns `[]`; never raise for those.

Behaviour that must NOT change:
- Nothing else in the package is touched; `broker_guard/state.py` and its
  helpers (`init_db`, `record_presence`, `get_present`, `StateStore`) keep
  working exactly as before -- this module only reads, it never writes or
  commits.

## Must contain

- `def query_presence_history(conn: sqlite3.Connection, broker_id: str | None = None, limit: int = 200) -> list[dict]:`
- `ORDER BY last_seen DESC`
- `"identity_key"`

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans helper files and the route/wiring that calls them --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `broker_guard/webui_data.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
