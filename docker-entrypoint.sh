#!/bin/sh
# Starts Xvfb (a virtual display) so Chromium can run HEADFUL, which is far
# less detectable than headless, then execs the real command.
# Skipped when BG_PLAYWRIGHT_HEADLESS=true or a DISPLAY is already provided.
set -e
case "$(echo "${BG_PLAYWRIGHT_HEADLESS:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on) ;;
  *)
    if [ -z "${DISPLAY:-}" ] && command -v Xvfb >/dev/null 2>&1; then
      export DISPLAY=:99
      Xvfb :99 -screen 0 1366x900x24 -nolisten tcp >/dev/null 2>&1 &
      # Wait up to ~5s for the X socket; on failure the launcher degrades to headless.
      i=0
      while [ ! -S /tmp/.X11-unix/X99 ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done
      [ -S /tmp/.X11-unix/X99 ] || unset DISPLAY
    fi
    ;;
esac
# Optional noVNC view of the live browser (CAPTCHA human fallback): x11vnc on
# the Xvfb display, bridged to a browser by websockify on :6080. FAILS CLOSED:
# without BG_NOVNC_PASSWORD nothing is started, because an unauthenticated
# view of a browser holding Penn's filled forms must never exist.
case "$(echo "${BG_NOVNC:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on)
    if [ -z "${DISPLAY:-}" ]; then
      echo "noVNC requested but there is no display (headless?); not started" >&2
    elif [ -z "${BG_NOVNC_PASSWORD:-}" ]; then
      echo "noVNC requested but BG_NOVNC_PASSWORD is unset; not started" >&2
    elif command -v x11vnc >/dev/null 2>&1 && command -v websockify >/dev/null 2>&1; then
      x11vnc -storepasswd "$BG_NOVNC_PASSWORD" /tmp/.vncpass >/dev/null 2>&1
      x11vnc -display "$DISPLAY" -localhost -forever -shared -quiet \
        -rfbauth /tmp/.vncpass -rfbport 5900 >/dev/null 2>&1 &
      websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    fi
    ;;
esac
exec "$@"
