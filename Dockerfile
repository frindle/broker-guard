# broker-guard -- scheduled/looping monitor by default; can also serve a web
# dashboard (BG_SERVE_WEB=true) in the same container/process. No port is
# exposed/bound unless that's turned on -- see docker-compose.yml.
#
# Playwright: browser_checks.py DOES launch a real headless Chromium (see
# broker_guard/browser.py) to read broker pages, so the Chromium binary and its
# system libraries are installed below. It only ever READS pages -- opt-out form
# submission goes through the vendored `eraser` engine, not through Playwright.
# If you run SERP-only (BG_PLAYWRIGHT_ENABLED=false, the default), you can build
# the much smaller image with `--build-arg INSTALL_BROWSERS=false`.
#
# eraser is built HERE, cloned fresh from its upstream fork -- no manual
# `go build` step on the host, no Go toolchain needed there, and (as of this
# change) no host-side `vendor/eraser/` checkout needed either: it's
# gitignored ("cloned, not committed"), so a fresh `git clone`+build of THIS
# repo used to fail with "COPY vendor/eraser/ ./: not found" until someone
# manually cloned it in first. Pin ERASER_REF to a real release tag so a
# rebuild doesn't silently pick up unreviewed upstream changes; bump it
# deliberately when you want to take a new eraser release.
# go.mod declares `go 1.26`; the builder image below pins 1.27 (Go's
# forward-compatibility guarantee: a toolchain newer than a module's `go`
# directive always builds it) to match the toolchain this repo's own
# CI/local dev already uses.
FROM golang:1.27-bookworm AS eraser-builder

ARG ERASER_REF=v0.7.4

WORKDIR /build/eraser
RUN git clone --depth 1 --branch ${ERASER_REF} \
        https://github.com/drumandbytes/eraser.git . \
 && rm -rf .git
RUN go build -o /build/eraser-bin ./cmd/eraser

FROM python:3.12-slim AS base

ARG INSTALL_BROWSERS=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# ca-certificates is needed for HTTPS to SearXNG and broker sites; tini gives
# us correct PID 1 signal handling so `docker stop` reaches the service loop.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# `--with-deps` pulls the system libraries Chromium needs. Done as root, into a
# shared path, so the unprivileged runtime user can still read the browser.
RUN if [ "$INSTALL_BROWSERS" = "true" ]; then \
        playwright install --with-deps chromium && \
        chmod -R a+rX /opt/playwright ; \
    else \
        echo "skipping browser install (INSTALL_BROWSERS=$INSTALL_BROWSERS)" ; \
    fi

# Application code only. Nothing PII-bearing is copied in: profile.local.json,
# the state db and the logs all arrive via mounted volumes at runtime, and
# .dockerignore keeps them out of the build context entirely.
COPY broker_guard/ ./broker_guard/
COPY profile.example.json ./profile.example.json

# The bundled broker source dataset (public, no PII -- see
# broker_normalize.ensure_brokers_file) and eraser's own broker list (for
# eraser_id cross-referencing), copied straight from the builder stage so
# both stay in sync with whatever vendor/eraser/ was actually built above.
COPY data/source-brokers.json ./data/source-brokers.json
COPY --from=eraser-builder /build/eraser/data/brokers.yaml ./vendor/eraser/data/brokers.yaml

# The compiled eraser binary -- baked in, no manual host-side `go build`
# step and no Go toolchain needed on the deploy host at all.
COPY --from=eraser-builder /build/eraser-bin /opt/eraser/bin/eraser
RUN chmod 755 /opt/eraser/bin/eraser

# Run unprivileged. /data and /logs are created here so the volume mounts land
# on directories this user owns.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin guard \
 && mkdir -p /data /logs \
 && chown -R guard:guard /app /data /logs
USER guard

ENV BG_PROFILE_PATH=/data/profile.local.json \
    BG_BROKERS_PATH=/data/brokers.json \
    BG_STATE_PATH=/data/state.sqlite \
    BG_LOG_DIR=/logs \
    BG_ID_DOCUMENTS_DIR=/data/id_documents \
    BG_FREEZE_STATE_PATH=/data/freeze_state.json \
    BG_REVIEW_DIR=/data/review \
    BG_ERASER_BIN=/opt/eraser/bin/eraser

# Documentation only -- does not publish/bind anything by itself. Actually
# reachable only when BG_SERVE_WEB=true AND docker-compose.yml's `ports:`
# maps it (see that file).
EXPOSE 8000

# Fails if the last cycle never completed or the heartbeat has gone stale.
HEALTHCHECK --interval=5m --timeout=15s --start-period=2m --retries=3 \
  CMD python -c "import json,os,sys;from broker_guard.health import heartbeat_stale;from datetime import datetime,timezone;p=os.path.join(os.environ.get('BG_LOG_DIR','/logs'),'heartbeat.json');h=json.load(open(p));sys.exit(0 if h.get('ok') and not heartbeat_stale(h['last_run'],datetime.now(timezone.utc).isoformat(),int(os.environ.get('BG_INTERVAL_SECONDS','86400'))*2) else 1)" || exit 1

# Unchanged from before: mode selection (headless loop vs. web dashboard) is
# entirely via BG_SERVE_WEB / --serve-web, read inside broker_guard.service,
# not via this entrypoint/CMD -- the default headless deployment is exactly
# as it was.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "broker_guard"]
