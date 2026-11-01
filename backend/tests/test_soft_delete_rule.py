"""Guard for the rule in CLAUDE.md: the application never deletes rows. Deletion is a soft delete."""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "safe_kit"
HARD_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)
TRUNCATE = re.compile(r"\bTRUNCATE\b", re.IGNORECASE)


def test_application_code_contains_no_hard_deletes():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if HARD_DELETE.search(text) or TRUNCATE.search(text):
            offenders.append(path.name)
    assert offenders == []
