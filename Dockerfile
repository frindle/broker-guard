# broker-guard -- scheduled/looping monitor, NOT a webserver. No port is exposed.
#
# Playwright: browser_checks.py DOES launch a real headless Chromium (see
# broker_guard/browser.py) to read broker pages, so the Chromium binary and its
# system libraries are installed below. It only ever READS pages -- opt-out form
# submission goes through the vendored `eraser` engine, not through Playwright.
# If you run SERP-only (BG_PLAYWRIGHT_ENABLED=false, the default), you can build
# the much smaller image with `--build-arg INSTALL_BROWSERS=false`.

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

# Run unprivileged. /data and /logs are created here so the volume mounts land
# on directories this user owns.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin guard \
 && mkdir -p /data /logs \
 && chown -R guard:guard /app /data /logs
USER guard

ENV BG_PROFILE_PATH=/data/profile.local.json \
    BG_BROKERS_PATH=/data/brokers.json \
    BG_STATE_PATH=/data/state.sqlite \
    BG_LOG_DIR=/logs

# Fails if the last cycle never completed or the heartbeat has gone stale.
HEALTHCHECK --interval=5m --timeout=15s --start-period=2m --retries=3 \
  CMD python -c "import json,os,sys;from broker_guard.health import heartbeat_stale;from datetime import datetime,timezone;p=os.path.join(os.environ.get('BG_LOG_DIR','/logs'),'heartbeat.json');h=json.load(open(p));sys.exit(0 if h.get('ok') and not heartbeat_stale(h['last_run'],datetime.now(timezone.utc).isoformat(),int(os.environ.get('BG_INTERVAL_SECONDS','86400'))*2) else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "broker_guard"]
