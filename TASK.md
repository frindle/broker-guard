# TASK: bg-retire-searxng-for-playwright

## Confirmed defect (observed, not suspected)

`build_detection(cfg)` in `broker_guard/service.py` unconditionally builds a
SearXNG client whenever `cfg.searxng_url` is set -- even when
`cfg.playwright_enabled` is True. Observed by reading the code: the guard on
the SearxClient construction is only `if cfg.searxng_url:` (around line 469),
so with both a URL and Playwright enabled, every cycle runs the SERP leg
against an already-rate-limited third-party service even though every broker
now has a hand-mapped Playwright recipe (search_forms.py/optout_forms.py).

## Entry point

broker_guard/service.py:469 -- the `if cfg.searxng_url:` guard inside
`build_detection(cfg)` that decides whether a SearxClient is instantiated.

## Required change

In broker_guard/service.py's build_detection(cfg): when cfg.playwright_enabled is True, do NOT build a searx_search client at all -- leave it None, regardless of whether cfg.searxng_url is set. SearXNG is being retired in favor of Playwright: every broker has now been hand-mapped to a Playwright recipe (search_forms.py/optout_forms.py), so once playwright_enabled is on, running the SearXNG/SERP leg alongside it is pure redundant load against an already-rate-limited third-party service. When cfg.playwright_enabled is False, behavior must be UNCHANGED: build searx_search exactly as today whenever cfg.searxng_url is set, with the existing PermanentSearxError handling and warning log preserved verbatim. This is a one-line-of-logic gate, not a rewrite -- do not touch page_action construction, sweep.py, or serpwatch.py. NOTE: this fix touches ONLY build_detection's client-construction gating, a pure offline-testable branch with zero live-page/browser interaction of its own -- it never calls page.evaluate, opens a page, or touches a frame; it only decides whether a SearxClient object gets instantiated.

Behaviour that must NOT change:
- `cfg.playwright_enabled` False + `cfg.searxng_url` set -> a SearxClient is still constructed with the exact same arguments (url, timeout_s, auth, engines, attempts, base_delay, min_interval_s, jitter_s) and the "searxng enabled" info log still fires.
- `cfg.playwright_enabled` False + no URL -> searx_search stays None AND the warning log "no SearXNG URL configured; SERP detection disabled" is preserved verbatim.
- A PermanentSearxError from construction (e.g. malformed URL) with Playwright off is still caught and logged as "searxng disabled", leaving searx_search None -- not raised.
- page_action construction via `make_page_action` when playwright_enabled is True, including the closer appended to `closers`, is untouched.

## Must contain

- in broker_guard/service.py: `if cfg.searxng_url and not cfg.playwright_enabled:`
- in broker_guard/service.py: `def build_detection(cfg: Config) -> tuple:`
- in broker_guard/service.py: `no SearXNG URL configured; SERP detection disabled`
- in broker_guard/service.py: `except PermanentSearxError as exc:`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `broker_guard/service.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
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
