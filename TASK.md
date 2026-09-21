# TASK: bg-webui-s5-alerts-feed

## Confirmed defect (observed, not suspected)

The webUI data module (`broker_guard/webui_data.py`) already serves the
presence history, health summary and escalation countdowns, but there is no
function that turns `logs/alerts.jsonl` into a recent-alerts feed: the file's
lines are raw JSON strings with no parsing, filtering or ordering helper in
the module (verified by reading the current file -- it ends at
`verify_token`, no alert-related function exists).

## Entry point

broker_guard/webui_data.py: end of file (after `verify_token`) -- append the new function.

## Required change

Add load_recent_alerts(lines: list[str], limit: int = 50) -> list[dict] to broker_guard/webui_data.py. Each line is one JSON object from logs/alerts.jsonl (an alert digest). Parse each line as JSON, skip blank lines and lines that fail to parse, and return the LAST `limit` valid entries in newest-first order (i.e. reverse the file's natural oldest-to-newest append order).

Behaviour that must NOT change:
- `query_presence_history`, `load_health_summary`, `escalation_countdowns` and `verify_token` keep their exact current signatures and behaviour -- this is an additive change only, nothing existing may be deleted or rewritten.
- Blank lines, unparseable lines, non-object JSON (lists, strings, numbers, null) and `None` entries are skipped without raising; an empty or all-invalid input returns `[]`.

## Must contain

- `def load_recent_alerts(lines: list[str], limit: int = 50) -> list[dict]:`
- `valid[-limit:]`

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
