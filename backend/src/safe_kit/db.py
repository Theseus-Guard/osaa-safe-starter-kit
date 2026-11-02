"""PostgreSQL connection. Settings come from environment variables; the password is read from a secret file.

No connection setting has a default. A missing value raises ConfigError instead of connecting to a guessed host.
"""

import os
from pathlib import Path

import psycopg

from safe_kit.config_guard import ConfigError


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required and must not be empty")
    return value


def connect() -> psycopg.Connection:
    host = _required("SAFE_DB_HOST")
    port = int(_required("SAFE_DB_PORT"))
    dbname = _required("SAFE_DB_NAME")
    user = _required("SAFE_DB_USER")
    password = Path(os.environ["SAFE_DB_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    return psycopg.connect(host=host, port=port, dbname=dbname, user=user, password=password)
