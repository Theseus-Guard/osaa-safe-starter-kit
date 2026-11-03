"""Recommendation list over PostgreSQL: the keyset walk returns every recommendation once, in creation order.

Needs SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db). Rows are only added, never removed.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest

from safe_kit import users
from safe_kit.catalog import add_recommendation, list_recommendations
from safe_kit.migrate import main as migrate

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(autouse=True)
def database_ready():
    if os.environ.get("SAFE_TEST_DATABASE") == "1":
        migrate()
    yield


def new_user(role: str) -> str:
    return users.create_user("p-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def add(actor: str, label: str) -> str:
    result = add_recommendation(
        {
            "failure_or_system": f"{label} {uuid.uuid4().hex[:8]}",
            "required_defence_outcome": "the gateway refuses the call",
            "minimum_control": "deny by default",
            "control_alternative": None,
            "responsible": "platform owner",
            "due_at": datetime(2026, 12, 1, 9, tzinfo=UTC),
            "evidence_to_keep": "policy diff",
        },
        actor,
    )
    return result.value.id


@requires_database
def test_the_keyset_walk_returns_each_recommendation_once_in_creation_order():
    actor = new_user("reviewer")
    created = [add(actor, "walk") for _ in range(5)]

    seen: list[str] = []
    cursor = None
    while True:
        page = list_recommendations(2, cursor)
        assert page is not None
        items, cursor = page
        assert len(items) <= 2
        seen.extend(item.id for item in items)
        if cursor is None:
            break

    # Other tests add rows too, so check that ours appear once each and in the order they were created.
    assert len(seen) == len(set(seen))
    positions = [seen.index(item) for item in created]
    assert positions == sorted(positions)


@requires_database
def test_a_recommendation_in_the_list_has_no_result_field():
    actor = new_user("reviewer")
    recommendation_id = add(actor, "shape")
    items, _ = list_recommendations(200, None)
    match = next(item for item in items if item.id == recommendation_id)
    assert not hasattr(match, "result")
    assert not hasattr(match, "verified")
