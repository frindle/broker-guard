# TASK: bg-webui-s4-auth-token

## Confirmed defect (observed, not suspected)

`broker_guard/webui_data.py` has no token-verification helper at all -- the webUI
layer that needs to check a caller-supplied auth token against the configured one
has nothing to call. Verified by reading the module: it contains only
`query_presence_history`, `load_health_summary` and `escalation_countdowns`; there
is no function taking `(provided, expected)` tokens anywhere in the file.

## Entry point

broker_guard/webui_data.py (module level -- add a new top-level function; the
file currently ends after `escalation_countdowns`)

## Required change

Add `verify_token(provided: str | None, expected: str) -> bool` to
`broker_guard/webui_data.py`, using `hmac.compare_digest` for a constant-time
comparison. Contract:

- `provided=None` or an empty string must return `False` without raising, even if
  `expected` is non-empty.
- A wrong (non-matching) token returns `False`.
- An exact match of two non-empty strings returns `True`.
- `expected` must never be empty/falsy at call time (assume the caller validates
  that); but if it IS empty, always return `False` (fail closed).
- The comparison itself must go through `hmac.compare_digest` so it does not
  short-circuit on the first differing byte.

Behaviour that must NOT change:
- `query_presence_history`, `load_health_summary` and `escalation_countdowns`
  keep their exact signatures, docstrings and behaviour -- this slice only ADDS a
  function (and the `hmac` import); it does not rewrite or delete anything.

## Must contain

- `def verify_token(provided: str | None, expected: str) -> bool:`
- `hmac.compare_digest`

## Scope

Only edit `broker_guard/webui_data.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as a
  header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
