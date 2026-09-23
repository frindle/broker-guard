# broker-guard

Self-hosted data-broker **monitoring + auto-removal + auto-escalation** loop
(self-hostable equivalent of the Incogni/Cloaked monitoring layer). Watches for
a person's own identity re-appearing across people-search / data-broker sites,
drives removals, and escalates when brokers miss statutory deadlines.

## Pipeline
profile -> brokers -> serpwatch -> playwright_checks -> state -> alert -> eraser_bridge -> health -> escalation -> orchestrator -> scheduler

- **profile**: identity model + config load + query/name variant generation
- **brokers**: load + normalize `data/brokers.json`
- **serpwatch**: build + run SearXNG queries per broker+identity, detect hits
- **playwright_checks**: per-site presence checks on top people-search sites
- **state**: SQLite presence history + new-appearance diff
- **alert**: notify on newly-detected appearance (batched per-run digest)
- **eraser_bridge**: invoke vendored `eraser` (github.com/drumandbytes/eraser, MIT) removal engine + re-verify
- **health**: SELF-MONITORING -- per-broker status state machine, failure
  classification (broker-side vs tool-side automation drift), a self-heal hook
  that emits dispatch-ready task files for drift into `needs-fix/`, heartbeat /
  dead-man's switch, and JSON-lines + human-readable status reports
- **escalation**: AUTOMATED LEGAL ESCALATION -- SLA timers stamped from
  `escalation-rules.json` (CCPA/CPRA, GDPR, VCDPA/CPA/CTDPA/UCPA/TDPSA, NV NRS
  603A, FCRA, DPPA, FTC backstop) per residency; auto-fill escalation letters;
  tiered auto-send (broker-facing follow-up = auto; regulator complaint =
  prepare-but-confirm, `auto_file_regulator_complaints: false` by default; FCRA
  freeze/dispute = human-confirm); full PII audit trail
- **orchestrator**: the full loop
- **scheduler**: launchd/cron wiring

### Runtime layer (wires the pure modules to real I/O)
- **config**: env-var config + profile/dataset validation (`BG_*`, see below)
- **logging_setup**: structured JSON logs with PII redaction (emails, phones,
  and search-query strings are stripped before anything is written)
- **retry**: exponential backoff with full jitter for flaky network calls
- **searx_client**: real SearXNG HTTP client (the `searx_search` callable)
- **browser**: real headless-Chromium `page_action` (READ-only: it reads page
  text and matches identity terms; it never fills or submits a form)
- **eraser_bridge**: real `eraser` subprocess invocation (`shell=False`, argv
  list, strict broker-id allowlist, always timeout-bounded, dry-run by default)
- **sinks**: alert sinks -- append-only JSONL file and an optional webhook
- **service**: the actual entrypoint and interval loop (`python -m broker_guard`)
- **webapp**: optional (`BG_SERVE_WEB=true`) -- serves `webui.py`'s FastAPI
  dashboard and runs `autopilot`'s scan/confirmation loop as a background
  thread in the same process, instead of the headless loop
- **webui**: the dashboard routes -- identity, brokers/status, on-demand
  scan/removal, breach-exposure panel, credit-freeze tracker
- **autopilot**: the kind-aware decision loop (`automatable`/`captcha` ->
  auto-send, `photo_id`/`kba` -> queued for a human) plus periodic
  confirmation and reappearance re-scanning

## Running

```bash
cp profile.example.json profile.local.json   # then fill in YOUR identity
python -m broker_guard --check-config        # validate env + inputs, exit
python -m broker_guard --once                # one cycle, then exit
python -m broker_guard                       # loop on BG_INTERVAL_SECONDS
python -m broker_guard --serve-web           # web dashboard + autopilot loop, one process
pytest tests/ -q                             # run the test suite
```

If `BG_BROKERS_PATH` doesn't exist yet, every mode above generates it
automatically from the bundled public dataset (`data/source-brokers.json`)
on startup — no manual `broker_normalize` run needed for a fresh identity.
An existing file (yours, or a previous run's) is never touched.

## Running with Docker

By default this is still a **looping/scheduled job, not a webserver** — no
port is exposed. Set `BG_SERVE_WEB=true` to also serve the web dashboard
(`webui.py`) from the SAME container, on `BG_WEB_PORT` (default `8000`); see
`broker_guard/webapp.py` for why this is one process rather than two.

```bash
docker compose build          # add --build-arg INSTALL_BROWSERS=false to skip Chromium
mkdir -p state logs
cp profile.example.json state/profile.local.json   # then fill it in
docker compose run --rm broker-guard --check-config
docker compose up -d
docker compose logs -f
```

The broker dataset (`state/brokers.json`) is generated automatically on
first boot from the bundled public dataset baked into the image — supply
your own file there first if you don't want that. The `eraser` binary is
also built into the image (multi-stage Dockerfile, from `vendor/eraser/`) —
no Go toolchain and no manual build step on the host anymore; only
`./state/eraser-config` (eraser's own identity/config) is still a volume.

**Playwright:** `broker_guard/browser.py` launches a real headless Chromium, so
the Dockerfile runs `playwright install --with-deps chromium`. It only ever
*reads* broker pages; opt-out submission goes through the vendored `eraser`
engine, never through the browser. Browser checks are **off by default**
(`BG_PLAYWRIGHT_ENABLED=false`) — build with `INSTALL_BROWSERS=false` for a much
smaller SERP-only image.

### Volumes (PII — never baked into the image)
Both are gitignored and dockerignored. Back them up like a password database.

| Mount | Holds |
| --- | --- |
| `./state:/data` | `profile.local.json` (your identity), `brokers.json` (dataset, auto-generated if absent), `state.sqlite` (presence history), `settings.json` (UI-editable runtime settings), `id_documents/` + `freeze_state.json` (web UI only) |
| `./logs:/logs` | JSON logs, `alerts.jsonl` digests, `heartbeat.json` |
| `./state/eraser-config:/home/guard/.eraser:ro` | eraser's own config/profile |

The `eraser` binary itself is built into the image (see the Dockerfile's
`eraser-builder` stage) — no separate volume, no host-side Go toolchain.

### Settings (`/settings`) — the UI beats the env var

Nine runtime settings are editable in the web dashboard and persisted to
`/data/settings.json` (`BG_SETTINGS_PATH`), next to `state.sqlite` and
`profiles.json`. Precedence, per setting:

```
saved in settings.json   >   BG_* environment variable   >   built-in default
```

So a fresh deployment behaves exactly as before — the env vars still decide
everything until someone saves something on the page — and once a value *is*
saved, it wins from then on, **including across a redeploy**. That last part is
the point: the Unraid host redeploys with `git fetch && git reset --hard
origin/main && docker compose build && docker compose up -d`, so any hand-edit
to the tracked `docker-compose.yml` is silently reverted. That was the root
cause of the recurring "Playwright detection turned itself off again" reports.

| Setting | When a change takes effect |
| --- | --- |
| `BG_PLAYWRIGHT_ENABLED` | next autopilot scan cycle (detection layer is rebuilt) |
| `BG_SEARXNG_URL` | next autopilot scan cycle |
| `BG_SEARXNG_MIN_INTERVAL_S` | next autopilot scan cycle |
| `BG_SEARXNG_JITTER_S` | next autopilot scan cycle |
| `BG_ERASER_ENABLED` | next removal (re-resolved per call) |
| `BG_ERASER_DRY_RUN` | next removal |
| `BG_OPTOUT_SUBMIT_ENABLED` | next run started from /review |
| `BG_OPTOUT_SUBMIT_DRY_RUN` | next run started from /review |
| `BG_ALERT_WEBHOOK_URL` | next alert |
| `BG_CAPTCHA_API_KEY` | immediately (write-only — never rendered back into the page) |
| `BG_INTERVAL_SECONDS` | next loop tick — it does not interrupt a sleep already running, so worst case is one confirmation interval (6h by default) |

Anything triggered from the dashboard (the **Run scan now** button) picks up
every setting immediately, because each request rebuilds its config.

`BG_SERVE_WEB` is deliberately **not** on that page: it decides whether the
dashboard runs at all, so it cannot be toggled from inside the dashboard. It
stays an environment variable — put `BG_SERVE_WEB=true` in a gitignored `.env`.

Each row on the page shows the effective value *and* which tier it came from
(`stored` / `env` / `default`), plus a checkbox to drop a stored override and
fall back to the environment variable again.

### Environment variables
All config is read from the environment; nothing is hardcoded and no secret is
ever written to a log. The variables marked **UI** below are the fallback tier
for a setting that `/settings` can override (see above).

| Variable | Default | Meaning |
| --- | --- | --- |
| `BG_PROFILE_PATH` | `/data/profile.local.json` | your identity file |
| `BG_BROKERS_PATH` | `/data/brokers.json` | broker dataset (no PII) |
| `BG_STATE_PATH` | `/data/state.sqlite` | presence history db |
| `BG_LOG_DIR` | `/logs` | log + heartbeat directory |
| `BG_ID_DOCUMENTS_DIR` | `data/id_documents` | encrypted ID-document uploads (web UI only) |
| `BG_FREEZE_STATE_PATH` | `data/freeze_state.json` | credit-freeze tracker state (web UI only) |
| `BG_SETTINGS_PATH` | `data/settings.json` | UI-editable settings store (see above); env-only, a store cannot relocate itself |
| `BG_REVIEW_DIR` | `data/review` | opt-out submission audit trail (JSON record + screenshot per attempt); env-only |
| `BG_SERVE_WEB` | `false` | serve the web dashboard + autopilot loop instead of the headless loop |
| `BG_WEB_PORT` | `8000` | port for the web dashboard (only meaningful with `BG_SERVE_WEB=true`) |
| `BG_CRYPTO_KEY` | *(unset)* | Fernet key encrypting ID documents + freeze PINs at rest; required only for those two features |
| `BG_INTERVAL_SECONDS` | `86400` | **UI** sweep interval; minimum 60 |
| `BG_RUN_ONCE` | `false` | run a single cycle and exit |
| `BG_SEARXNG_URL` | *(unset)* | **UI** your self-hosted SearXNG; unset disables SERP detection |
| `BG_SEARXNG_AUTH` | *(unset)* | optional `Authorization` header value |
| `BG_SEARXNG_TIMEOUT_S` | `20` | per-request timeout |
| `BG_SEARXNG_ENGINES` | *(unset)* | restrict to specific SearXNG engines |
| `BG_SEARXNG_MIN_INTERVAL_S` | `2.0` | **UI** minimum seconds between two SearXNG requests (see below) |
| `BG_SEARXNG_JITTER_S` | `1.0` | **UI** extra uniform `0..N` seconds added to that gap |
| `BG_PLAYWRIGHT_ENABLED` | `false` | **UI** enable headless browser checks |
| `BG_PLAYWRIGHT_HEADLESS` | `true` | run Chromium headless |
| `BG_PLAYWRIGHT_TIMEOUT_MS` | `30000` | per-page timeout |
| `BG_ERASER_ENABLED` | `false` | **UI** enable the removal engine |
| `BG_ERASER_DRY_RUN` | `true` | **UI** — **keep true until you mean it**; no request is sent while set |
| `BG_ERASER_BIN` | `eraser` | path to the compiled binary |
| `BG_ERASER_TIMEOUT_S` | `300` | subprocess timeout |
| `BG_ALERT_WEBHOOK_URL` | *(unset)* | **UI** your own webhook (HA, ntfy…); unset = file sink only |
| `BG_ALERT_LOG_PATH` | `$BG_LOG_DIR/alerts.jsonl` | append-only digest file |
| `BG_OPTOUT_SUBMIT_ENABLED` | `false` | **UI** enable automated opt-out form submission (see below) |
| `BG_OPTOUT_SUBMIT_DRY_RUN` | `true` | **UI** — **keep true until you have checked a screenshot**; Submit is never pressed while set |
| `BG_CAPTCHA_API_KEY` | *(unset)* | **UI** third-party captcha solver key |
| `BG_MAX_RETRIES` | `3` | attempts per network call |
| `BG_LOG_LEVEL` | `INFO` | log level |
| `BG_LOG_PII` | `false` | **leave false** — true disables log redaction |

Two safety interlocks are on by default: `BG_ERASER_ENABLED=false` and
`BG_ERASER_DRY_RUN=true`, so no opt-out request is ever transmitted until both
are deliberately changed. The same pattern guards automated form submission:
`BG_OPTOUT_SUBMIT_ENABLED=false` and `BG_OPTOUT_SUBMIT_DRY_RUN=true`.

### Automated opt-out submission

Everything else in this project only ever *reads* a broker's site. This is the
one feature that acts: it opens the broker's opt-out webform in a real browser,
fills in your actual name, email and state from your profile, and — fully
switched on — presses Submit on your behalf.

Because of that, four things must all be true before anything is sent:

1. `BG_OPTOUT_SUBMIT_ENABLED=true` (default `false`). This is **not** wired
   into the autopilot scan loop — a scan never submits anything. Runs are
   started by hand from the **Opt-out review** page.
2. `BG_OPTOUT_SUBMIT_DRY_RUN=false` (default `true`). While true, the form is
   filled and photographed but Submit is never pressed.
3. The broker has a hand-verified recipe in `broker_guard/optout_forms.py`.
   That allow-list is the safety boundary: there is no "guess the form from
   the URL" fallback. Currently five brokers, each one opened and read by
   hand: **CONSUMER CANVAS LLC**, **Nielsen**, **bolttech**, **Credit.com**
   and **L.S Mobile Apps Holdings Ltd**.
4. `BG_PLAYWRIGHT_ENABLED=true` and the image was built with
   `INSTALL_BROWSERS=true` (the default).

**CAPTCHAs are never solved.** If a bot check is detected, the run stops,
screenshots the filled-in form, and records the attempt as *needs you* — which
shows up in the existing **Action needed** badge. It is not bypassed, not
out-sourced to a solver, not retried.

That is the normal outcome for **all five** supported brokers, not an edge
case: Consumer Canvas, Nielsen and Credit.com carry a BotDetect image CAPTCHA,
bolttech uses reCAPTCHA v2, and L.S Mobile Apps draws its own security code on
a canvas. What you get is a screenshot of the completely filled-in form plus
the audit record — open the form, type the code, press Submit yourself. Turning
DRY RUN off therefore does not currently change the outcome for any supported
broker; it only removes the last interlock for when a CAPTCHA-free form is
added.

Two things to check against your own profile before running these:

* **Nielsen** offers no "consumer" subject type at all (its options are
  panel-member and employee flavoured), so the recipe picks *Other (see
  description)*, and the form has no free-text box to explain in.
* **L.S Mobile Apps** requires a phone number in international format and asks
  whether you use one of their apps; the recipe answers **No**. If you do use
  one, change that line in `optout_forms.py` — it is a factual claim made in
  your name.

**Every attempt is audited.** Success, failure and bail-out alike write a JSON
record (timestamp, broker, form URL, the exact fields submitted) plus a
screenshot into `BG_REVIEW_DIR`, viewable on **Opt-out review**. Those records
deliberately contain your unredacted PII — that is the point of an audit trail
you own — so they are written `0600` inside a `0700` directory, and nothing is
ever logged to stdout.

Suggested first run: turn the feature on, leave DRY RUN on, press **Dry run
(fill only)** on `/review`, and look at the screenshot. Only turn DRY RUN off
once the mapping looks right.

### SearXNG pacing — do not turn this down

A full cycle is ~827 brokers × one query per identity value: thousands of
requests. Unpaced, they went out as fast as network RTT allowed, and the
self-hosted SearXNG instance's **upstream** engines (Brave, Google CSE,
DuckDuckGo, Startpage, Wikipedia) responded by rate-limiting and CAPTCHA-walling
it — a block that can take up to ~2 weeks to clear, during which every query
returns nothing.

`BG_SEARXNG_MIN_INTERVAL_S` (default `2.0`) plus `BG_SEARXNG_JITTER_S` (default
`0`–`1.0` extra) enforce a gap between successive requests, inside
`SearxClient` itself so retries and any future caller are covered too. At that
rate a full sweep takes a few hours, which is fine: the default scan interval is
a day. Lowering these to "speed up" a scan is how the instance got blocked.

### Scan progress and error counting

The dashboard shows a live per-broker counter while a sweep runs
("Checking brokers: 412/827 — 3 found, 0 errors.") and, once it finishes,
the outcome tally for the completed run. Each broker lands in exactly one
bucket — `hit`, `checked`, `error` or `skipped` — and `error` is kept strictly
distinct from `checked`.

That distinction is the point: a search failure used to be swallowed and
counted as "checked, found nothing", so a total SearXNG outage produced a
confident `hit_brokers: 0` and looked like good news. A cycle now reports
"checked 827, 0 errors" and "checked 487, 340 errors" as visibly different
states. One broker's failure still never aborts the cycle — it is just no
longer invisible.

### Silent detection failures (a failure must never read as a removal)

A broker is reported **resolved** — and its presence row deleted, which is what
makes a *re*appearance detectable — only when it was actually checked and found
absent. Anything that merely *looks* like an absence has to raise instead, so
`orchestrator.run_cycle` buckets it under `errors` and leaves the broker's state
alone. Two failure modes used to slip through, because neither raised:

- **SearXNG answering 200 with nothing in it.** When the instance's upstream
  engines are all rate-limited or CAPTCHA-walled it returns a valid, empty
  `results: []` for every query — the same value a genuine "not listed" search
  returns. `searx_client` now also reads the `unresponsive_engines` field
  SearXNG ships alongside the results and raises `SearxError` when an *empty*
  result set came back with either every `BG_SEARXNG_ENGINES` engine
  unresponsive, or (when no engine set is pinned) three or more of them. A
  single flaky engine is **not** an outage — SearXNG falls back to the others,
  and flagging that would turn ordinary flakiness into a cycle-wide error storm.
  Non-empty results never raise, however many engines are down: if something
  answered and found something, that is real signal.
- **A broker's bot wall.** A Cloudflare/CAPTCHA challenge page navigates fine
  and returns 200; the identity terms are correctly absent from it, because it
  is not the broker's listing page. `browser.bot_wall_reason` matches a short,
  conservative signature list (plus HTTP 401/403/429) and reports `{"error": …}`
  rather than `{"found": false}`. Generic CAPTCHA-widget wording only counts on
  a near-empty page, since a real opt-out page may legitimately embed one. A
  404, and a broker's own "no results found" copy, are genuine absences and are
  left alone.

### Per-broker scan results on /brokers

`/brokers` has two cards, answering two different questions:

- **Tracked listings** — the `presence`/`broker_status` history: where this
  profile was actually found, and what has been sent. It can only ever
  contain brokers the person was FOUND on.
- **Scan results** — every broker in `brokers.json` with the outcome the
  most recent scan in this process recorded for it: *Listing found*,
  *Checked — clean*, *Check failed*, *Not checkable*, or **Not yet
  checked** for one the current scan has not reached. "Not yet checked" is
  deliberately its own state; a broker nobody has looked at yet must never
  render like one that was checked and came back clean.

The per-broker map lives in `broker_guard/progress.py` alongside the
aggregate counters and is **in-memory for the life of the process** (same
scope as the aggregate counters — see that module's "Scope: ONE process").
It survives the end of a cycle, so results stay readable after a scan
finishes; it does not survive a restart, and nothing persistent is written
for it. Both cards can be scoped to a saved profile with the picker at the
top right (`?identity=<profile id>`, or `all`); the tracked-listings card
is per `identity_key` in the db, so a profile that is not the active one
still shows its own history.

While a scan is running the card live-updates from `GET /status?brokers=1`
(the same poll shape the dashboard uses; the per-broker map is opt-in so
the dashboard's 1.5s poll stays small) and reloads once the scan ends.

### Scan order

One cycle walks the broker list in `service.order_brokers_for_scan`'s
order: brokers this identity has no presence record for first, then
everything else, both groups freshly shuffled with a new `random.Random()`
per cycle. Unknown-first spends a slow or rate-limited cycle's budget where
a new listing can actually turn up; the shuffle means a cycle that dies two
thirds of the way through does not starve the same tail of the list every
single time. The order is computed ONCE and both legs (SERP and browser)
follow it.

Networking is plain bridge; see the commented `macvlan` block at the bottom of
`docker-compose.yml` for where a static homelab IP would go.

## Reuse (do not reimplement)
- Removal engine: **eraser** (vendored under `vendor/eraser/`), called via CLI.
- Broker seed data: `data/brokers.json` (maintained in the SEPARATE PUBLIC repo; pulled at runtime; contains NO personal data).
- SERP engine: self-hosted **SearXNG**.

## Privacy split
- PUBLIC (separate repo): the anonymized broker dataset (`brokers.json`, opt-out methods) and `escalation-rules.json`.
- PRIVATE (this repo): the tool + everything tied to Penn's identity. `profile.local.json`, state DB, logs, serpwatch hits, snapshots, removal/escalation records, and the `needs-fix/` dispatch queue are all gitignored. Copy `profile.example.json` -> `profile.local.json`.

Module code is authored via the ollama-dispatch verify-gated pipeline (qwen on studio).
