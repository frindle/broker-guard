# TASK: bg-eraser-bridge-config

## Confirmed defect (observed, not suspected)

CONFIRMED live: eraser fails with `failed to load config: open /home/guard/.eraser/config.yaml: no such file` because EraserBridge never passes the config path to the command builders. The builders in `broker_guard/eraser.py` gained a `config_path` keyword in 55a39df, but nothing supplies it -- so every invocation falls back to eraser's default `$HOME/.eraser/config.yaml`, which does not exist on this host. Reproduced by inspecting the argv the runner receives: none of the four builder call sites in `broker_guard/eraser_bridge.py` pass `config_path`.

## Entry point

broker_guard/eraser_bridge.py:41 (`EraserBridge.__init__`) and the four builder call sites in `submit_removal`, `status`, `monitor`, `fill`.

## Required change

CONFIRMED live: eraser fails 'failed to load config: open /home/guard/.eraser/config.yaml: no such file' because EraserBridge never passes the config path to the command builders (eraser.py builders gained a config_path keyword in 55a39df but nothing supplies it). Must hold: EraserBridge.__init__ accepts keyword config_path (default None) stored as self.config_path; submit_removal, status, monitor and fill each pass config_path=self.config_path to build_eraser_cmd / build_eraser_status_cmd / build_eraser_monitor_cmd / build_eraser_fill_cmd so the argv the runner receives ends with '--config', <path> when set; with config_path None every argv is byte-identical to today's; existing positional args (eraser_bin, timeout_s, dry_run) and runner/cwd/env keywords keep working.

Behaviour that must NOT change:
- `shell=False`, list argv, timeout always passed to the runner -- no new flags or reordering of existing argv elements.
- With `config_path` unset/None, every argv is byte-identical to today's (no `--config` token anywhere).
- Existing positional args (`eraser_bin`, `timeout_s`, `dry_run`) and the `runner`/`cwd`/`env` keywords keep working exactly as before.
- Result dict shape from each method (`success`, `detail`, `timed_out`, plus per-method keys) is unchanged; failure paths (non-zero exit, timeout, missing binary -> EraserUnavailable) behave as today.

## Must contain

- `config_path: str | None = None`
- `self.config_path = config_path`
- `config_path=self.config_path`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `broker_guard/eraser_bridge.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
