"""Keyset cursors for list endpoints. A cursor is the sort position of the last row of a page: a timestamp or a
name, then the row id as the tie-breaker. Pages are read with a condition on that position, never with an offset."""

import base64
import binascii
import uuid
from datetime import datetime


def _pack(key: str, row_id: str) -> str:
    raw = f"{key}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unpack(cursor: str) -> tuple[str, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        key, row_text = text.rsplit("|", 1)
        uuid.UUID(row_text)
    except (binascii.Error, UnicodeError, ValueError):
        return None
    return key, row_text


def encode_cursor(created_at: datetime, row_id: str) -> str:
    return _pack(created_at.isoformat(), row_id)


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """The keyset position of a cursor, or None when the cursor is not one this module issued."""
    unpacked = _unpack(cursor)
    if unpacked is None:
        return None
    try:
        created_at = datetime.fromisoformat(unpacked[0])
    except ValueError:
        return None
    return created_at, unpacked[1]


def encode_name_cursor(name: str, row_id: str) -> str:
    return _pack(name, row_id)


def decode_name_cursor(cursor: str) -> tuple[str, str] | None:
    """The (name, id) position of a cursor, or None when the cursor is not one this module issued."""
    return _unpack(cursor)
