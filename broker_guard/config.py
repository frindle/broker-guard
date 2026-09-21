"""Runtime configuration: environment variables + the local profile file.

Every secret and endpoint is read from the environment. Nothing is hardcoded
and nothing is written back out. See README "Running with Docker" for the full
variable list.
"""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_PROFILE_PATH = "profile.local.json"
DEFAULT_BROKERS_PATH = "data/brokers.json"
DEFAULT_STATE_PATH = "data/state.sqlite"
DEFAULT_LOG_DIR = "logs"

# Keys whose values must never be logged, echoed or written to an alert body.
SECRET_ENV_KEYS = (
    "BG_CAPTCHA_API_KEY",
    "BG_ALERT_WEBHOOK_URL",
    "BG_SEARXNG_AUTH",
)


class ConfigError(ValueError):
    """Raised when the environment/profile combination cannot produce a run."""


def _env_str(env, name, default=None):
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(env, name, default):
    raw = _env_str(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")


def _env_bool(env, name, default=False):
    raw = _env_str(env, name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _require_http_url(name, value, allow_empty=True):
    if not value:
        if allow_empty:
            return None
        raise ConfigError(f"{name} is required")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}")
    return value


@dataclass
class Config:
    profile_path: str = DEFAULT_PROFILE_PATH
    brokers_path: str = DEFAULT_BROKERS_PATH
    state_path: str = DEFAULT_STATE_PATH
    log_dir: str = DEFAULT_LOG_DIR

    searxng_url: str | None = None
    searxng_auth: str | None = None
    searxng_timeout_s: int = 20
    searxng_engines: str | None = None

    alert_webhook_url: str | None = None
    alert_log_path: str = "logs/alerts.jsonl"

    eraser_bin: str = "eraser"
    eraser_enabled: bool = False
    eraser_dry_run: bool = True
    eraser_timeout_s: int = 300

    playwright_enabled: bool = False
    playwright_timeout_ms: int = 30000
    playwright_headless: bool = True

    captcha_api_key: str | None = field(default=None, repr=False)

    interval_seconds: int = 86400
    run_once: bool = False
    max_retries: int = 3
    retry_base_delay_s: float = 1.0
    log_level: str = "INFO"
    log_pii: bool = False

    def redacted(self) -> dict:
        """A dict of this config safe to log: secrets replaced with a marker."""
        out = {}
        for key, value in self.__dict__.items():
            if key in ("captcha_api_key", "searxng_auth", "alert_webhook_url"):
                out[key] = "<set>" if value else None
            else:
                out[key] = value
        return out


def load_config(env=None) -> Config:
    """Build a Config from *env* (defaults to os.environ) and validate it.

    *env* is passed explicitly rather than read globally so tests can inject a
    plain dict without mutating the process environment.
    """
    env = os.environ if env is None else env
    cfg = Config(
        profile_path=_env_str(env, "BG_PROFILE_PATH", DEFAULT_PROFILE_PATH),
        brokers_path=_env_str(env, "BG_BROKERS_PATH", DEFAULT_BROKERS_PATH),
        state_path=_env_str(env, "BG_STATE_PATH", DEFAULT_STATE_PATH),
        log_dir=_env_str(env, "BG_LOG_DIR", DEFAULT_LOG_DIR),
        searxng_url=_env_str(env, "BG_SEARXNG_URL"),
        searxng_auth=_env_str(env, "BG_SEARXNG_AUTH"),
        searxng_timeout_s=_env_int(env, "BG_SEARXNG_TIMEOUT_S", 20),
        searxng_engines=_env_str(env, "BG_SEARXNG_ENGINES"),
        alert_webhook_url=_env_str(env, "BG_ALERT_WEBHOOK_URL"),
        eraser_bin=_env_str(env, "BG_ERASER_BIN", "eraser"),
        eraser_enabled=_env_bool(env, "BG_ERASER_ENABLED", False),
        eraser_dry_run=_env_bool(env, "BG_ERASER_DRY_RUN", True),
        eraser_timeout_s=_env_int(env, "BG_ERASER_TIMEOUT_S", 300),
        playwright_enabled=_env_bool(env, "BG_PLAYWRIGHT_ENABLED", False),
        playwright_timeout_ms=_env_int(env, "BG_PLAYWRIGHT_TIMEOUT_MS", 30000),
        playwright_headless=_env_bool(env, "BG_PLAYWRIGHT_HEADLESS", True),
        captcha_api_key=_env_str(env, "BG_CAPTCHA_API_KEY"),
        interval_seconds=_env_int(env, "BG_INTERVAL_SECONDS", 86400),
        run_once=_env_bool(env, "BG_RUN_ONCE", False),
        max_retries=_env_int(env, "BG_MAX_RETRIES", 3),
        log_level=_env_str(env, "BG_LOG_LEVEL", "INFO").upper(),
        log_pii=_env_bool(env, "BG_LOG_PII", False),
    )
    cfg.alert_log_path = _env_str(env, "BG_ALERT_LOG_PATH", os.path.join(cfg.log_dir, "alerts.jsonl"))
    _require_http_url("BG_SEARXNG_URL", cfg.searxng_url)
    _require_http_url("BG_ALERT_WEBHOOK_URL", cfg.alert_webhook_url)

    if cfg.interval_seconds < 60:
        raise ConfigError("BG_INTERVAL_SECONDS must be at least 60 (be polite to brokers)")
    if cfg.max_retries < 0:
        raise ConfigError("BG_MAX_RETRIES must be >= 0")
    if cfg.searxng_timeout_s <= 0 or cfg.eraser_timeout_s <= 0:
        raise ConfigError("timeouts must be positive")
    if cfg.eraser_enabled and not cfg.eraser_bin:
        raise ConfigError("BG_ERASER_ENABLED is set but BG_ERASER_BIN is empty")
    return cfg


def validate_runtime_paths(cfg: Config) -> list[str]:
    """Return human-readable problems with the on-disk inputs (empty == fine)."""
    problems = []
    if not os.path.exists(cfg.profile_path):
        problems.append(
            f"profile not found at {cfg.profile_path} "
            "(copy profile.example.json -> profile.local.json and fill it in)"
        )
    if not os.path.exists(cfg.brokers_path):
        problems.append(
            f"broker dataset not found at {cfg.brokers_path} "
            "(pull it from the public dataset repo, or point BG_BROKERS_PATH at it)"
        )
    return problems
