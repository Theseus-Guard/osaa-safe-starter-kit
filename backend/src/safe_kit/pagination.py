"""Keyset cursors for list endpoints. A cursor is the (created_at, id) position of the last row of a page."""

import base64
import binascii
import uuid
from datetime import datetime


def encode_cursor(created_at: datetime, row_id: str) -> str:
    raw = f"{created_at.isoformat()}|{row_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """The keyset position of a cursor, or None when the cursor is not one this module issued."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_text, row_text = text.split("|", 1)
        created_at = datetime.fromisoformat(created_text)
        uuid.UUID(row_text)
    except (binascii.Error, UnicodeError, ValueError):
        return None
    return created_at, row_text
