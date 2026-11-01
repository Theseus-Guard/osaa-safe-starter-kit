"""PostgreSQL connection. Settings come from environment variables; the password is read from a secret file."""

import os
from pathlib import Path

import psycopg


def connect() -> psycopg.Connection:
    password = Path(os.environ["SAFE_DB_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    return psycopg.connect(
        host=os.environ.get("SAFE_DB_HOST", "db"),
        port=int(os.environ.get("SAFE_DB_PORT", "5432")),
        dbname=os.environ.get("SAFE_DB_NAME", "safe"),
        user=os.environ.get("SAFE_DB_USER", "safe_app"),
        password=password,
    )
