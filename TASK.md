# TASK: bg-eraser-cmd

## Confirmed defect (observed, not suspected)

CONFIRMED live: every eraser invocation fails with
`failed to load config: open /home/guard/.eraser/config.yaml: no such file`
because no command builder in `broker_guard/eraser.py` passes eraser's global
`--config` flag (so eraser falls back to its default `$HOME/.eraser/config.yaml`,
which does not exist for the guard user), and `build_eraser_cmd` only passes
`--profile` when the profile dict happens to carry an `eraser_profile` key --
there is no way to pass a Profile ID directly.

## Entry point

broker_guard/eraser.py:30 (`build_eraser_cmd`) and its sibling builders
(`build_eraser_status_cmd`, `build_eraser_monitor_cmd`, `build_eraser_fill_cmd`).

## Required change

CONFIRMED live: every eraser invocation fails 'failed to load config: open /home/guard/.eraser/config.yaml: no such file' because no command builder in broker_guard/eraser.py passes eraser's global --config flag, and build_eraser_cmd only passes --profile when the profile dict has an eraser_profile key. Must hold: build_eraser_cmd, build_eraser_status_cmd and the monitor/fill builders accept a keyword config_path (default None) and, when it is a non-empty string, emit exactly the two argv items '--config', config_path; build_eraser_cmd also accepts keyword profile_id (default None) and when non-empty emits '--profile', profile_id (taking precedence over the dict's eraser_profile); with both None the argv is byte-identical to today's; no PII from the profile dict ever appears in argv (existing no-PII-on-command-line rule).

Behaviour that must NOT change:
- `build_eraser_cmd("acme", {}, dry_run=True)` still returns exactly
  `["eraser", "send", "--broker", "acme", "--dry-run"]` when the new keywords
  are not passed.
- The dict's `eraser_profile` key is still honoured (validated, lowercased)
  when no `profile_id` keyword is given.
- `build_eraser_status_cmd(limit=10)` still returns exactly
  `["eraser", "status", "--limit", "10"]`.
- The monitor/fill builders' existing `profile_id` behaviour and their base
  argv (`[bin, "monitor"]`, `[bin, "fill"]`) are unchanged when no new keyword
  is passed.
- Invalid broker ids / non-dict profiles still raise `ValueError`; the command
  is always a list (never a shell string).

## Must contain

- in broker_guard/eraser.py: `config_path`
- in broker_guard/eraser.py: `"--config"`
- in broker_guard/eraser.py: `profile_id`
- in broker_guard/eraser.py: `"--profile"`

## Scope

Only edit `broker_guard/eraser.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
This is not about adding bogus assertions for constants; it's about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
