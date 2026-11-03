import base64
import uuid
from datetime import UTC, datetime

import pytest

from safe_kit.pagination import decode_cursor, encode_cursor


def test_a_cursor_round_trips_to_the_same_position():
    created_at = datetime(2026, 11, 2, 9, 30, 15, 123456, tzinfo=UTC)
    row_id = str(uuid.uuid4())
    assert decode_cursor(encode_cursor(created_at, row_id)) == (created_at, row_id)


@pytest.mark.parametrize("cursor", ["", "invalid", "!!!!", "bm90IGEgY3Vyc29y"])
def test_a_string_that_is_not_a_cursor_is_refused(cursor):
    assert decode_cursor(cursor) is None


def test_a_cursor_with_a_row_id_that_is_not_a_uuid_is_refused():
    raw = "2026-11-02T09:30:15+00:00|not-a-uuid".encode("utf-8")
    assert (
        decode_cursor(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")) is None
    )
