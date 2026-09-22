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
| `./state:/data` | `profile.local.json` (your identity), `brokers.json` (dataset, auto-generated if absent), `state.sqlite` (presence history), `id_documents/` + `freeze_state.json` (web UI only) |
| `./logs:/logs` | JSON logs, `alerts.jsonl` digests, `heartbeat.json` |
| `./state/eraser-config:/home/guard/.eraser:ro` | eraser's own config/profile |

The `eraser` binary itself is built into the image (see the Dockerfile's
`eraser-builder` stage) — no separate volume, no host-side Go toolchain.

### Environment variables
All config is read from the environment; nothing is hardcoded and no secret is
ever written to a log.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BG_PROFILE_PATH` | `/data/profile.local.json` | your identity file |
| `BG_BROKERS_PATH` | `/data/brokers.json` | broker dataset (no PII) |
| `BG_STATE_PATH` | `/data/state.sqlite` | presence history db |
| `BG_LOG_DIR` | `/logs` | log + heartbeat directory |
| `BG_ID_DOCUMENTS_DIR` | `data/id_documents` | encrypted ID-document uploads (web UI only) |
| `BG_FREEZE_STATE_PATH` | `data/freeze_state.json` | credit-freeze tracker state (web UI only) |
| `BG_SERVE_WEB` | `false` | serve the web dashboard + autopilot loop instead of the headless loop |
| `BG_WEB_PORT` | `8000` | port for the web dashboard (only meaningful with `BG_SERVE_WEB=true`) |
| `BG_CRYPTO_KEY` | *(unset)* | Fernet key encrypting ID documents + freeze PINs at rest; required only for those two features |
| `BG_INTERVAL_SECONDS` | `86400` | sweep interval; minimum 60 |
| `BG_RUN_ONCE` | `false` | run a single cycle and exit |
| `BG_SEARXNG_URL` | *(unset)* | your self-hosted SearXNG; unset disables SERP detection |
| `BG_SEARXNG_AUTH` | *(unset)* | optional `Authorization` header value |
| `BG_SEARXNG_TIMEOUT_S` | `20` | per-request timeout |
| `BG_SEARXNG_ENGINES` | *(unset)* | restrict to specific SearXNG engines |
| `BG_PLAYWRIGHT_ENABLED` | `false` | enable headless browser checks |
| `BG_PLAYWRIGHT_HEADLESS` | `true` | run Chromium headless |
| `BG_PLAYWRIGHT_TIMEOUT_MS` | `30000` | per-page timeout |
| `BG_ERASER_ENABLED` | `false` | enable the removal engine |
| `BG_ERASER_DRY_RUN` | `true` | **keep true until you mean it** — no request is sent while set |
| `BG_ERASER_BIN` | `eraser` | path to the compiled binary |
| `BG_ERASER_TIMEOUT_S` | `300` | subprocess timeout |
| `BG_ALERT_WEBHOOK_URL` | *(unset)* | your own webhook (HA, ntfy…); unset = file sink only |
| `BG_ALERT_LOG_PATH` | `$BG_LOG_DIR/alerts.jsonl` | append-only digest file |
| `BG_CAPTCHA_API_KEY` | *(unset)* | third-party captcha solver key |
| `BG_MAX_RETRIES` | `3` | attempts per network call |
| `BG_LOG_LEVEL` | `INFO` | log level |
| `BG_LOG_PII` | `false` | **leave false** — true disables log redaction |

Two safety interlocks are on by default: `BG_ERASER_ENABLED=false` and
`BG_ERASER_DRY_RUN=true`, so no opt-out request is ever transmitted until both
are deliberately changed.

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
