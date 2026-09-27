# TASK: bg-scan-error-reason

## Confirmed defect (observed, not suspected)

When a broker check fails, `ScanProgress.record_outcome` stores only the bare
outcome (`error`) in its per-broker map -- no message, no kind. Reproduced by
driving `record_outcome(broker_id, "error", errors=1)` on a fresh
`ScanProgress()` and inspecting `brokers[entry_key(None, broker_id)]`: the entry
has exactly the keys `broker_id/outcome/hits/errors/checked_at/phase/identity_key`,
so `/brokers` cannot say WHY a broker errored (DNS? cert? rate-limited?) or which
of the ~827 failures are retryable. The error message itself is dropped at the
call site, and even where it survives in logs it carries full search URLs whose
query strings include the user's name/phone/email -- so a raw message must not be
stored verbatim either.

## Entry point

broker_guard/progress.py:349 (`ScanProgress.record_outcome`) plus the module-level
helpers section just above `_check_outcome` (line ~520).

## Required change

`progress.record_outcome` gains an optional keyword `reason: str|None=None`. When
`outcome=='error'` and `reason` is given, the per-broker entry stored in
`self.brokers` gets two extra keys: `'reason'` (the message with every URL reduced
to `scheme://host` -- path, query and fragment removed, because search URLs carry
the user's name/phone/email -- and truncated to 300 chars) and `'error_kind'` from
a new module-level function `classify_error(message)->str` returning exactly one of:

- `'dns'`: ERR_NAME_NOT_RESOLVED, NXDOMAIN, 'Name or service not known', getaddrinfo
- `'ssl'`: ERR_CERT_, SSL, certificate
- `'timeout'`: Timeout, ERR_TIMED_OUT, timed out
- `'refused'`: ERR_CONNECTION_REFUSED, ERR_CONNECTION_RESET, ERR_CONNECTION_CLOSED, Connection refused
- `'blocked'`: HTTP 403, 429, captcha, Cloudflare, Access Denied
- else `'other'`

Matching is case-insensitive; `None`/empty message -> `'other'`. Non-error outcomes
never get `reason`/`error_kind` keys (even if a reason was passed), and a retry
(`replace=True`) that is no longer an error must not keep a stale reason. Both the
replace path AND the rank-merge path must carry reason when they record an error. On the rank-merge path, a later non-error leg (e.g. a clean SERP leg) merged into an entry whose outcome is STILL error keeps that entry's reason and error_kind; they are removed only when the entry's outcome is no longer error. A hit (which outranks error) merged after an errored leg makes the outcome hit, so reason and error_kind must be removed.
Existing callers that pass no reason behave exactly as before.

Add two module-level functions:

```python
def classify_error(message: str | None) -> str: ...
def redact_reason(message: str | None) -> str | None: ...
```

`redact_reason(None)` returns `None`; otherwise it returns the string with every
URL reduced to its origin (`scheme://host`) and truncated to 300 chars.

Behaviour that must NOT change:
- `record()` (aggregate-only counting), `_check_outcome`, `OUTCOMES`, `OUTCOME_RANK`
- entries recorded without a reason keep EXACTLY the old key set
  (`broker_id, outcome, hits, errors, checked_at, phase, identity_key`)
- the replace path still sets `"retried": True` and leaves aggregate counters alone
- rank merge precedence (hit > error > checked > skipped) is unchanged; a clean leg
  recorded before an errored leg still yields `outcome == 'error'` after the merge

## Must contain

- `def classify_error(message: str | None) -> str:`
- `def redact_reason(message: str | None) -> str | None:`
- `replace: bool = False, reason: str | None = None`
- `"reason": redact_reason(reason)`
- `"error_kind": classify_error(reason)`

## Scope

Only edit `broker_guard/progress.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
