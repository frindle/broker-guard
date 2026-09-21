# TASK: bg-webui-s3-escalation-audit

## Confirmed defect (observed, not suspected)

`broker_guard/webui_data.py` exposes `query_presence_history` and
`load_health_summary`, but has no way to turn a list of escalation records into
countdowns for the webUI. Verified by reading the module: there is no function
accepting `deadline_iso` records, so an audit view cannot show how much time is
left before each broker escalates (or that it already has).

## Entry point

broker_guard/webui_data.py -- append a new public function at module level, after `load_health_summary`.

## Required change

Add escalation_countdowns(records: list[dict], now_iso: str) -> list[dict] to broker_guard/webui_data.py. Each input record has {'broker_id': str, 'stage': str, 'deadline_iso': str}. Return one dict per record: {'broker_id', 'stage', 'deadline_iso', 'seconds_remaining': int, 'overdue': bool}. seconds_remaining = deadline - now in seconds (can be negative); overdue = seconds_remaining < 0. Sort the output by seconds_remaining ascending (most urgent/most overdue first).

Contract details:
- `now_iso` and each `deadline_iso` are ISO-8601 strings; parse with `datetime.fromisoformat`.
- `seconds_remaining` is an int of whole seconds, deadline minus now; negative when the deadline has passed.
- `overdue` is a bool, true exactly when `seconds_remaining < 0` (a deadline equal to now is NOT overdue).
- Output rows carry exactly the five keys above, with `broker_id`, `stage`, `deadline_iso` copied through unchanged from the input record.
- Empty `records` returns `[]`.

Behaviour that must NOT change:
- `query_presence_history(conn, broker_id=None, limit=200)` keeps its exact signature, parameterised SELECTs, ordering by `last_seen DESC`, and dict shape.
- `load_health_summary(lines)` still returns the last parseable JSON object line or the zeroed summary `{'total': 0, 'ok': 0, 'failed': 0, 'by_broker': {}}`.
- The module stays read-only: no writes, commits, or mutation of any connection.

## Must contain

- `def escalation_countdowns(records: list[dict], now_iso: str) -> list[dict]:`
- `"seconds_remaining"`
- `"overdue"`

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
