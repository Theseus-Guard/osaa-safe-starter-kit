"""Apply pending SQL migrations in one transaction, under an advisory lock."""

import hashlib
import os
import sys
from pathlib import Path

from safe_kit.db import connect

LOCK_KEY = 0x5AFE0001
MIGRATIONS_DIR = Path(os.environ.get("SAFE_MIGRATIONS_DIR", "migrations"))


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, "
            "checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = dict(
            conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        for path in files:
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    print(
                        f"checksum mismatch: applied migration {version} was modified",
                        file=sys.stderr,
                    )
                    return 1
                continue
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            print(f"applied: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
