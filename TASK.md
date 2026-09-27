# TASK: bg-automate-optout-form-submission

## Confirmed defect (observed, not suspected)

Opt-out FORM submission only ever happens when a human clicks the button on
the /review page. `optout_submit.run_attempt()` is called ONLY from webui.py's
/review/run route; `broker_guard/autopilot.py` never calls it -- verified by
grep: no reference to `optout_submit` anywhere in autopilot.py, and
`run_forever`'s while loop ticks exactly two counters (`elapsed_since_scan`,
`elapsed_since_confirmation`). So with the feature enabled, nothing submits an
opt-out form on its own cadence.

## Entry point

broker_guard/autopilot.py: `Intervals` dataclass (scan_seconds /
confirmation_seconds) and `run_forever`'s while loop -- the new pass slots in
alongside `run_confirmation_pass`, which it mirrors exactly.

## Required change

Automate opt-out FORM submission so it runs on the sweep's own cadence -- no
human has to click a button on the /review page. Add a new pure function
`run_optout_submission_pass(identities: list, cfg) -> dict` in
broker_guard/autopilot.py: for each identity in identities, for each broker_id
in `broker_guard.optout_forms.supported_broker_ids()`, first check
`broker_guard.review.load_attempts(broker_guard.review.review_dir(cfg))` for any
EXISTING record whose `broker_id` matches and whose `identity_key` matches
`identity.identity_key` -- if one already exists (any outcome), SKIP that pair
(one-shot-per-broker-per-identity, never resubmitted; the review folder is
already the audit trail). Otherwise call
`broker_guard.optout_submit.run_attempt(broker_id, identity, cfg)` inside a
try/except that catches `optout_submit.SubmissionRefused` (the normal, expected
state when `cfg.optout_submit_enabled` is False -- log nothing louder than a
debug line and continue; do NOT treat it as an error) and a bare Exception
(log a warning with only the broker_id and exception TYPE name -- never the
exception message or any field value, matching review.py's PII-logging
discipline -- and continue to the next pair). Return a dict:
`{'attempted': n, 'skipped_existing': n, 'submission_disabled': n, 'errors': n}`.

Then wire it into `run_forever()`: add `optout_seconds: int = 21600` to the
Intervals dataclass (same default as confirmation_seconds), add an
`elapsed_since_optout` counter initialized and ticked exactly like
`elapsed_since_confirmation` is, and when `elapsed_since_optout >=
intervals.optout_seconds`, call `run_optout_submission_pass(_scan_identities(),
cfg)` inside the same try/except-log-and-continue pattern run_confirmation_pass
already uses, then reset `elapsed_since_optout = 0`. Do NOT gate the tick on
`cfg.optout_submit_enabled` -- with it off every real attempt is a fast,
harmless SubmissionRefused catch.

NOTE: `run_optout_submission_pass` itself never opens a page or touches browser
internals -- it only calls the ALREADY-EXISTING `optout_submit.run_attempt()`
(which owns all real browser interaction and is untouched by this fix) and
reads/dedupes against review records. Pure orchestration/control-flow,
offline-testable with fakes for run_attempt and review.load_attempts.

Behaviour that must NOT change:
- `run_scan_cycle` / `run_scan_cycles` / `decide_action` / `run_confirmation_pass`
  behave exactly as before (the fixture's run_forever case exercises the scan +
  confirmation passes alongside the new one).
- A pair with an existing review record is NEVER re-submitted, whatever its
  outcome; a pair for a DIFFERENT identity or broker is still attempted.
- `SubmissionRefused` never increments `errors`; any other exception does, and
  the pass continues to the next pair instead of aborting.
- The new pass fires exactly once per cadence (counter resets after firing) --
  it must not re-fire on the very next tick.

## Must contain

- `def run_optout_submission_pass(identities: list, cfg) -> dict:`
- `"skipped_existing"`
- `"submission_disabled"`
- `optout_seconds: int = 21600`
- `elapsed_since_optout`
- `run_optout_submission_pass(_scan_identities(), cfg)`

## Scope

Only edit `broker_guard/autopilot.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
