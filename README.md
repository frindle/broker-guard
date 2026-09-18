# broker-guard

Self-hosted data-broker **monitoring + auto-removal** loop (self-hostable equivalent of
the Incogni/Cloaked monitoring layer). Watches for a person's own identity re-appearing
across people-search / data-broker sites and drives removals.

## Pipeline
profile -> brokers -> serpwatch -> playwright_checks -> state -> alert -> eraser_bridge -> orchestrator -> scheduler

- **profile**: identity model + config load + query/name variant generation
- **brokers**: load + normalize `data/brokers.json`
- **serpwatch**: build + run SearXNG queries per broker+identity, detect hits
- **playwright_checks**: per-site presence checks on top people-search sites
- **state**: SQLite presence history + new-appearance diff
- **alert**: notify on newly-detected appearance
- **eraser_bridge**: invoke vendored `eraser` (github.com/drumandbytes/eraser, MIT) removal engine + re-verify
- **orchestrator**: the full loop
- **scheduler**: launchd/cron wiring

## Reuse (do not reimplement)
- Removal engine: **eraser** (vendored under `vendor/eraser/`), called via CLI.
- Broker seed data: `data/brokers.json` (compiled separately; runtime data).
- SERP engine: self-hosted **SearXNG**.

Module code is authored via the ollama-dispatch verify-gated pipeline (qwen on studio).
