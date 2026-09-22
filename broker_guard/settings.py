"""UI-editable runtime settings, persisted on the /data volume.

Why this module exists (the bug it fixes)
--------------------------------------------
A handful of runtime knobs -- SERP endpoint, browser-detection on/off, the
SearXNG pacing pair, the alert webhook, the removal engine's two safety
switches, the captcha key and the scan interval -- existed ONLY as ``BG_*``
environment variables set in the tracked ``docker-compose.yml``. The Unraid
host redeploys with::

    git fetch && git reset --hard origin/main && docker compose build && docker compose up -d

so any manual edit to that tracked file is silently reverted to its committed
default on the next deploy. That is the root cause of the recurring "the web
UI turned itself off again" / "Playwright detection turned itself off again"
reports, and ``docker-compose.yml``'s own inline comments document two
one-off workarounds for it (``${VAR:-default}`` substitution plus a
gitignored ``.env``).

A ``.env`` file is not an answer to "why do I need a file at all when there
is a front-facing UI?". This module is: the settings live in a small JSON
document on the SAME writable ``/data`` volume that already holds
``state.sqlite``/``profiles.json``/``freeze_state.json``, so they survive
``git reset --hard``, a rebuild and a container recreate, and they are
editable from ``/settings`` in the dashboard.

Precedence (the ONE rule every consumer follows)
---------------------------------------------------
For each setting, effective value is the first of:

1. **stored**   -- the key is present in ``settings.json`` (written by the UI)
2. **env**      -- the corresponding ``BG_*`` variable is set in the process
                   environment (i.e. what ``docker-compose.yml`` supplies)
3. **default**  -- the hardcoded fallback, which is literally the
                   ``config.Config`` dataclass default for that field (read
                   from ``Config()`` here rather than re-declared, so there
                   is exactly one copy of every default in the codebase)

First boot is therefore byte-for-byte unchanged: with no ``settings.json``
every value still resolves through the env vars exactly as before. Once the
UI writes a key, that key wins from then on -- including across redeploys.

What is deliberately NOT here
--------------------------------
``BG_SERVE_WEB`` is a bootstrap paradox: if it is off there is no running web
UI in which to expose a toggle for it, so it stays env-var-only and is gated
at container start. Host-path plumbing (every ``BG_*_PATH``/``_DIR`` var,
including ``BG_SETTINGS_PATH`` itself -- a store cannot relocate itself),
``BG_WEB_PORT``, ``TZ``, ``BG_LOG_LEVEL``, ``BG_MAX_RETRIES``,
``BG_LOG_PII``, ``BG_ERASER_BIN``, ``BG_ERASER_TIMEOUT_S`` and the two
``BG_PLAYWRIGHT_HEADLESS``/``_TIMEOUT_MS`` tuning knobs are also left alone:
this is about the settings that actually caused reset-wipes-my-config
incidents, not a blanket env-to-UI migration.

Storage shape
----------------
A flat JSON object keyed by ``Config`` FIELD name (not by ``BG_*`` env name
-- the env name is metadata on the spec, see ``SettingSpec.env``), holding
only the keys someone has actually set::

    {
      "playwright_enabled": true,
      "searxng_url": "http://10.0.8.20:8080",
      "searxng_min_interval_s": 2.5,
      "interval_seconds": 86400
    }

Absent key == "not stored" == fall through to env. A key stored as ``""``
(empty string) is NOT absent: it is an explicit "disabled", which is how the
UI turns off the SearXNG URL or the alert webhook without deleting the file.

Concurrency / durability: writes go through ``save_settings``, which is the
same tmp-file + ``os.replace`` + ``chmod 0600`` pattern
``profiles.save_profiles``/``profiles.write_legacy_profile`` already use, so
a reader never observes a half-written file. Read-modify-write
(``update_settings``) additionally holds a module-level lock, because unlike
``profiles.json`` this file is written from the web request thread while the
autopilot background thread reads it every cycle (see ``webapp.py`` on why
both live in one process).

Imports of ``config`` are function-local in the couple of places that need
the live default, matching ``profiles.py``'s deliberate no-import-time-
coupling convention -- ``config.load_config`` has no dependency on this
module either, so the settings layer is strictly an overlay applied by
callers, never something ``Config`` construction silently reaches for.
"""
import dataclasses
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("broker_guard.settings")

DEFAULT_SETTINGS_PATH = "data/settings.json"

SOURCE_STORED = "stored"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"

# BG_INTERVAL_SECONDS' floor, matching config.load_config's own check and the
# "minimum accepted is 60" comment in docker-compose.yml. Enforced on the
# stored tier too: a UI-set 5 would otherwise sail straight past the
# validation load_config only applies to the env tier.
MIN_INTERVAL_SECONDS = 60


class SettingsError(ValueError):
    """A stored/submitted settings value is not usable."""


@dataclass(frozen=True)
class SettingSpec:
    """One UI-editable setting.

    ``key`` is both the ``config.Config`` field name and the ``settings.json``
    key -- one name, so ``effective_config`` can overlay by ``setattr`` and
    the store never needs a translation table. ``env`` is the ``BG_*``
    variable the same value comes from when nothing is stored.
    """

    key: str
    env: str
    kind: str          # "bool" | "str" | "int" | "float"
    label: str
    help: str
    secret: bool = False
    minimum: float | None = None
    is_url: bool = False


SETTING_SPECS = (
    SettingSpec(
        key="playwright_enabled", env="BG_PLAYWRIGHT_ENABLED", kind="bool",
        label="Browser (Playwright) detection",
        help="Check broker sites with a real headless browser in addition to SERP "
             "detection. Requires the image to have been built with "
             "INSTALL_BROWSERS=true (the default).",
    ),
    SettingSpec(
        key="searxng_url", env="BG_SEARXNG_URL", kind="str", is_url=True,
        label="SearXNG URL",
        help="Your self-hosted SearXNG instance, e.g. http://10.0.8.20:8080. "
             "Leave blank to disable SERP detection entirely.",
    ),
    SettingSpec(
        key="searxng_min_interval_s", env="BG_SEARXNG_MIN_INTERVAL_S", kind="float",
        minimum=0.0,
        label="SearXNG minimum interval (s)",
        help="Minimum seconds between two SearXNG requests. Do NOT lower this to "
             "speed up a scan -- unpaced requests got the instance's upstream "
             "engines CAPTCHA-walled for weeks.",
    ),
    SettingSpec(
        key="searxng_jitter_s", env="BG_SEARXNG_JITTER_S", kind="float", minimum=0.0,
        label="SearXNG jitter (s)",
        help="A uniform 0..N seconds added on top of the minimum interval.",
    ),
    SettingSpec(
        key="alert_webhook_url", env="BG_ALERT_WEBHOOK_URL", kind="str", is_url=True,
        label="Alert webhook URL",
        help="Optional. Home Assistant / ntfy / anything that takes a POST. "
             "Blank means alerts are written to the log file only.",
    ),
    SettingSpec(
        key="eraser_enabled", env="BG_ERASER_ENABLED", kind="bool",
        label="Removal engine enabled",
        help="Let the autopilot drive the vendored eraser engine. Off means no "
             "opt-out request is ever attempted.",
    ),
    SettingSpec(
        key="eraser_dry_run", env="BG_ERASER_DRY_RUN", kind="bool",
        label="Removal engine DRY RUN",
        help="Keep this ON until you have watched a dry run do the right thing. "
             "Turning it off sends REAL opt-out requests.",
    ),
    SettingSpec(
        key="captcha_api_key", env="BG_CAPTCHA_API_KEY", kind="str", secret=True,
        label="CAPTCHA solver API key",
        help="Third-party CAPTCHA solving service credential. Optional.",
    ),
    SettingSpec(
        key="interval_seconds", env="BG_INTERVAL_SECONDS", kind="int",
        minimum=MIN_INTERVAL_SECONDS,
        label="Scan interval (seconds)",
        help="How often the autopilot runs a full sweep. 86400 = once a day. "
             "Minimum {}.".format(MIN_INTERVAL_SECONDS),
    ),
)

SPEC_BY_KEY = {spec.key: spec for spec in SETTING_SPECS}

# Settings whose change invalidates the presence-detection layer the autopilot
# built for the previous cycle (SearXNG client + Playwright page action). See
# autopilot.build_dependencies: when this tuple's values change, that layer is
# torn down and rebuilt on the next cycle, which is what makes these four
# genuinely live rather than "live after a container restart".
DETECTION_KEYS = ("playwright_enabled", "searxng_url",
                  "searxng_min_interval_s", "searxng_jitter_s")

_WRITE_LOCK = threading.Lock()


# --- coercion / validation ---------------------------------------------------

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off", "")


def coerce_value(spec: SettingSpec, raw):
    """Turn *raw* (a JSON scalar or a form string) into the stored/typed value
    for *spec*, or raise ``SettingsError``.

    Accepts both the JSON-native type (``true``, ``2.5``) and its string
    spelling (``"true"``, ``"2.5"``) so the same function validates a
    hand-edited settings.json and an HTML form submission -- one validator,
    no second copy that could drift from this one.
    """
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw if raw is not None else "").strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        raise SettingsError(f"{spec.env} must be a boolean, got {raw!r}")

    if spec.kind == "str":
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise SettingsError(f"{spec.env} must be a string, got {raw!r}")
        value = raw.strip()
        if value and spec.is_url:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise SettingsError(f"{spec.env} must be an http(s) URL, got {value!r}")
        return value

    if spec.kind in ("int", "float"):
        if isinstance(raw, bool):  # bool is an int subclass; never a number here
            raise SettingsError(f"{spec.env} must be a number, got {raw!r}")
        try:
            value = int(raw) if spec.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise SettingsError(f"{spec.env} must be a number, got {raw!r}")
        if spec.minimum is not None and value < spec.minimum:
            raise SettingsError(
                "{} must be at least {}, got {}".format(spec.env, spec.minimum, value)
            )
        return value

    raise SettingsError(f"unknown setting kind {spec.kind!r}")  # pragma: no cover


def to_config_value(spec: SettingSpec, value):
    """The value as ``Config`` wants it.

    The only translation is for strings: ``Config`` spells "unset" as ``None``
    (``searxng_url: str | None``), while the store spells it as ``""`` so the
    key can stay present and keep winning over the env tier. Everything else
    passes through untouched.
    """
    if spec.kind == "str":
        return value or None
    return value


# --- store -------------------------------------------------------------------

def load_settings(path: str) -> dict:
    """Every setting currently stored at *path*, typed and validated.

    A missing file is an empty dict -- "nothing has been set through the UI
    yet", which is the normal first-boot state, not an error (same convention
    as ``profiles.load_profiles`` treats a missing profiles.json).

    An individual key that is unknown or unusable is DROPPED with a warning
    rather than raising: this file is read on the request path and on every
    autopilot cycle, and one bad hand-edited value must degrade to "that key
    falls through to its env var", never to a dead dashboard. A file that is
    not a JSON object at all does raise -- that is a corrupt store, not one
    bad key, and silently treating it as empty would mean silently reverting
    every setting.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} must contain a JSON object")

    out = {}
    for key, value in raw.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            log.warning("ignoring unknown stored setting", extra={"setting": key})
            continue
        try:
            out[key] = coerce_value(spec, value)
        except SettingsError as exc:
            log.warning("ignoring unusable stored setting",
                        extra={"setting": key, "error": str(exc)})
    return out


def save_settings(path: str, values: dict) -> None:
    """Replace the whole store with *values* (already-typed, already-validated).

    Atomic tmp-file + ``os.replace``, owner-only permissions -- the same
    pattern as ``profiles.save_profiles``, and for the same two reasons: a
    concurrent reader never sees a partial document, and the file can hold a
    credential (the CAPTCHA key), so it is no more world-readable than
    profiles.json is.
    """
    unknown = [k for k in values if k not in SPEC_BY_KEY]
    if unknown:
        raise SettingsError("unknown setting(s): " + ", ".join(sorted(unknown)))

    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".settings-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_settings(path: str, changes: dict) -> dict:
    """Merge *changes* into the store and persist it; returns the new store.

    A value of ``None`` in *changes* DELETES that key, which is how the UI
    says "stop overriding this, go back to the env var" -- distinct from
    storing ``""``, which is an explicit "disabled" that still wins over the
    env. Every value is validated before anything is written, so a bad
    submission never half-applies.

    The read-modify-write runs under ``_WRITE_LOCK``: the dashboard request
    thread and the autopilot thread share one process (see ``webapp.py``), so
    two concurrent saves would otherwise be able to interleave their reads and
    lose one of the two edits. The write itself is still atomic on top of
    that, for readers and for a second process.
    """
    prepared = {}
    for key, raw in changes.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise SettingsError(f"unknown setting: {key}")
        prepared[key] = None if raw is None else coerce_value(spec, raw)

    with _WRITE_LOCK:
        current = load_settings(path)
        for key, value in prepared.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        save_settings(path, current)
        return dict(current)


# --- precedence --------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedSetting:
    """One setting's effective value plus WHERE it came from.

    ``source`` is rendered next to the value on ``/settings`` so it is never
    ambiguous why a value is what it is -- the whole class of confusion that
    made "it reset itself again" so hard to pin down in the first place.
    """

    spec: SettingSpec
    value: object          # Config-shaped (strings already ""->None)
    source: str            # SOURCE_STORED | SOURCE_ENV | SOURCE_DEFAULT
    stored: bool
    env_raw: str | None
    default_value: object

    @property
    def key(self) -> str:
        return self.spec.key

    def display(self) -> str:
        """The value as the UI shows it -- never the real secret."""
        if self.spec.secret:
            return "set" if self.value else "not set"
        if self.spec.kind == "bool":
            return "on" if self.value else "off"
        if self.value is None or self.value == "":
            return ""
        return str(self.value)


def _config_defaults():
    """The hardcoded fallback tier: ``config.Config``'s own field defaults.

    Read off a freshly-constructed ``Config`` rather than re-declared in this
    module, so "the hardcoded default" has exactly one definition and these
    two files cannot drift. Imported inside the function on purpose -- see
    the module docstring on keeping ``config`` free of a cycle through here.
    """
    from broker_guard.config import Config

    blank = Config()
    return {spec.key: getattr(blank, spec.key) for spec in SETTING_SPECS}


def resolve(path: str, env=None) -> list[ResolvedSetting]:
    """Every setting's effective value and source, in ``SETTING_SPECS`` order.

    Each tier is read independently here (stored file, *env*, ``Config``
    defaults) rather than inferred from an already-resolved ``Config``, so the
    page can show all three and say which one won.
    """
    env = os.environ if env is None else env
    try:
        stored = load_settings(path)
    except (OSError, ValueError) as exc:
        log.warning("settings store unreadable; falling back to env",
                    extra={"path": path, "error": str(exc)})
        stored = {}
    defaults = _config_defaults()

    out = []
    for spec in SETTING_SPECS:
        env_raw = env.get(spec.env)
        if env_raw is not None:
            env_raw = env_raw.strip() or None

        if spec.key in stored:
            value = to_config_value(spec, stored[spec.key])
            source = SOURCE_STORED
        elif env_raw is not None:
            try:
                value = to_config_value(spec, coerce_value(spec, env_raw))
                source = SOURCE_ENV
            except SettingsError as exc:
                log.warning("ignoring unusable environment value",
                            extra={"setting": spec.env, "error": str(exc)})
                value = defaults[spec.key]
                source = SOURCE_DEFAULT
        else:
            value = defaults[spec.key]
            source = SOURCE_DEFAULT

        out.append(ResolvedSetting(
            spec=spec, value=value, source=source, stored=spec.key in stored,
            env_raw=env_raw, default_value=defaults[spec.key],
        ))
    return out


def store_path(cfg) -> str:
    """Where *cfg*'s settings store lives.

    ``Config.settings_path`` is ``None`` on a hand-constructed Config (see its
    comment): that means "the module default", read HERE at use time rather
    than frozen into the dataclass, which is what lets the test suite point
    the whole store somewhere hermetic in one place.
    """
    return getattr(cfg, "settings_path", None) or DEFAULT_SETTINGS_PATH


def effective_config(cfg, path: str | None = None):
    """*cfg* with every STORED setting overlaid on top of it.

    The env and default tiers are already baked into *cfg* by
    ``config.load_config`` (which reads the ``BG_*`` vars and falls back to the
    dataclass defaults), so the only thing this has to add is the stored tier
    -- which is exactly why the precedence is stored > env > default and why a
    deployment with no settings.json behaves byte-for-byte as it did before
    this module existed.

    Returns a NEW Config (``dataclasses.replace``); *cfg* is never mutated, so
    a caller holding the env-only baseline keeps it. A missing/corrupt store
    logs and returns an unmodified copy rather than raising: losing the
    overlay degrades to the previous behavior, losing the process does not.
    """
    resolved_path = path if path is not None else store_path(cfg)
    try:
        stored = load_settings(resolved_path)
    except (OSError, ValueError) as exc:
        log.warning("settings store unreadable; using environment config only",
                    extra={"path": resolved_path, "error": str(exc)})
        return dataclasses.replace(cfg)

    overlay = {}
    for key, value in stored.items():
        spec = SPEC_BY_KEY[key]
        overlay[key] = to_config_value(spec, value)
    return dataclasses.replace(cfg, **overlay)


def detection_fingerprint(cfg) -> tuple:
    """The detection-layer-relevant slice of *cfg*, as a comparable tuple.

    ``autopilot`` keeps the previous cycle's fingerprint and rebuilds its
    SearXNG client / Playwright page action when this changes -- the mechanism
    that makes those four settings take effect on the next scan instead of on
    the next container restart. Also covers the two non-UI Playwright tuning
    knobs, since they feed the same constructed object.
    """
    return tuple(getattr(cfg, key, None) for key in DETECTION_KEYS) + (
        cfg.searxng_auth, cfg.searxng_timeout_s, cfg.searxng_engines,
        cfg.playwright_timeout_ms, cfg.playwright_headless,
        cfg.max_retries, cfg.retry_base_delay_s,
    )
