# TASK: bg-webui-s2-health-summary

## Confirmed defect (observed, not suspected)

`broker_guard/webui_data.py` exposes `query_presence_history` for the webUI but has no way to surface broker health: there is no function that can take the rolling log of `build_report` JSON lines (as written by `broker_guard/health.py`) and hand back the most recent report. Verified by reading the module -- it contains only `query_presence_history`; a grep for `load_health_summary` in the tree finds no definition, so any webUI health panel has nothing to call.

## Entry point

`broker_guard/webui_data.py` (module level; new function appended after `query_presence_history`)

## Required change

Add `load_health_summary(lines: list[str]) -> dict` to `broker_guard/webui_data.py`. Each line in `lines` is one JSON object as written by `broker_guard/health.py`'s `build_report` output (has 'total', 'ok', 'failed', 'by_broker'). Return only the LAST valid line parsed as a dict (the most recent report); if `lines` is empty or every line fails to parse, return {'total': 0, 'ok': 0, 'failed': 0, 'by_broker': {}}.

Behaviour that must NOT change:
- `query_presence_history(conn, broker_id=None, limit=200)` keeps its exact contract: parameterised SELECT over `presence`, ordered by `last_seen DESC`, optional single-broker filter, capped at `limit`, returns dicts keyed `identity_key`/`broker_id`/`first_seen`/`last_seen`, never writes through `conn`.
- The module stays read-only and importable with only the standard library.

## Must contain

- `def load_health_summary(lines: list[str]) -> dict:`
- `json.loads`
- `"by_broker"`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

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
