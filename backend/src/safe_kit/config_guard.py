"""Startup guard: decides from the environment whether a process may start.

load_settings is a pure function over a mapping (os.environ in the entrypoints). It opens no connection and reads no
secret file. The API and the worker call it before any database connection is made, and a ConfigError stops the
process. Error messages name the setting that is wrong and never echo its value.
"""

from collections.abc import Mapping
from dataclasses import dataclass

MODE_DEMO = "demo"
MODE_PRODUCTION = "production"
SYNTHETIC_DEMO_DATASET = "synthetic-demo"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

MAX_PORT = 65535
# Token and session lifetimes. The values are required: there is no default. The upper bounds only catch typos.
MAX_TTL_MINUTES = 525_600  # one year
MAX_SESSION_HOURS = 8_760  # one year


class ConfigError(Exception):
    """The environment does not allow this process to start."""


@dataclass(frozen=True)
class Settings:
    mode: str
    bind_host: str | None
    dataset: str | None
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password_file: str
    access_ttl_minutes: int
    refresh_ttl_minutes: int
    session_max_hours: int


def _value(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _required(env: Mapping[str, str], name: str) -> str:
    value = _value(env, name)
    if not value:
        raise ConfigError(f"{name} is required and must not be empty")
    return value


def required_int(env: Mapping[str, str], name: str, *, minimum: int | None = 1, maximum: int | None = None) -> int:
    """A required whole number. Missing, empty, or not a whole number is refused; so is a value outside the bounds.

    minimum=None skips the lower bound. security.py uses that: the startup guard has already checked the range, and
    the token helpers only need the value.
    """
    raw = _required(env, name)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        bounds = f"from {minimum} to {maximum}" if maximum is not None else f"of at least {minimum}"
        raise ConfigError(f"{name} must be a whole number {bounds}")
    return value


def _db_port(env: Mapping[str, str]) -> int:
    raw = _required(env, "SAFE_DB_PORT")
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_PORT:
        raise ConfigError(f"SAFE_DB_PORT must be a port number from 1 to {MAX_PORT}")
    return int(raw)


def load_settings(env: Mapping[str, str]) -> Settings:
    """Validate the environment and return the settings. Raises ConfigError when the process must not start."""
    # SAFE_MODE is compared exactly: no trimming and no case folding, so a typo never selects a mode by accident.
    mode = env.get("SAFE_MODE")
    if mode not in (MODE_DEMO, MODE_PRODUCTION):
        raise ConfigError("SAFE_MODE is required and must be exactly 'demo' or 'production'")

    bind_host = _value(env, "SAFE_BIND_HOST") or None
    dataset = _value(env, "SAFE_DATASET") or None

    if mode == MODE_DEMO:
        if bind_host is None:
            raise ConfigError("SAFE_BIND_HOST is required in demo mode")
        # SAFE_BIND_HOST is the published host address of the demo, not the address the process listens on.
        if bind_host not in LOOPBACK_HOSTS:
            raise ConfigError("SAFE_BIND_HOST is the published host address; in demo mode it must be 127.0.0.1 or ::1")
        if dataset != SYNTHETIC_DEMO_DATASET:
            raise ConfigError(f"SAFE_DATASET must be set to '{SYNTHETIC_DEMO_DATASET}' in demo mode")
    elif dataset is not None:
        raise ConfigError("SAFE_DATASET must be unset or empty in production mode")

    return Settings(
        mode=mode,
        bind_host=bind_host,
        dataset=dataset,
        db_host=_required(env, "SAFE_DB_HOST"),
        db_port=_db_port(env),
        db_name=_required(env, "SAFE_DB_NAME"),
        db_user=_required(env, "SAFE_DB_USER"),
        db_password_file=_required(env, "SAFE_DB_PASSWORD_FILE"),
        access_ttl_minutes=required_int(env, "SAFE_ACCESS_TTL_MINUTES", maximum=MAX_TTL_MINUTES),
        refresh_ttl_minutes=required_int(env, "SAFE_REFRESH_TTL_MINUTES", maximum=MAX_TTL_MINUTES),
        session_max_hours=required_int(env, "SAFE_SESSION_MAX_HOURS", maximum=MAX_SESSION_HOURS),
    )
