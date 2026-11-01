"""Case links against PostgreSQL: the uniqueness constraint, the soft-delete check, the audit rows and the whole flow.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth, users
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


class ActingClient:
    """Makes requests as one real user. The caller is set before every request, so clients never share a caller."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.client = TestClient(api.app)

    def _request(self, method: str, url: str, **kwargs):
        user = users.find_user_by_id(self.user_id)
        assert user is not None
        api.app.dependency_overrides[auth.current_user] = lambda: user
        return getattr(self.client, method)(url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._request("delete", url, **kwargs)


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def new_case(reporter: ActingClient, title: str = "Case") -> str:
    response = reporter.post("/api/v1/cases", json={"title": title, "awareness_time": past()})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def new_reporter_channel_case(reporter: ActingClient) -> str:
    response = reporter.post("/api/v1/reports", json={"title": "Report", "awareness_time": past(), "contact": None})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def close_case_in_database(case_id: str, actor_id: str) -> None:
    # Closing through the API needs a review and settled deadlines. The schema is what these tests check, so the
    # closed state is set directly here. The application itself never changes a case this way.
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (actor_id, case_id),
        )


def audit_rows(action: str, case_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT actor_id::text, target_type, target_id, details FROM audit_events "
            "WHERE action = %s AND target_id = %s AND NOT is_deleted ORDER BY created_at, id",
            (action, case_id),
        ).fetchall()
    return [{"actor_id": row[0], "target_type": row[1], "target_id": row[2], "details": row[3]} for row in rows]


@pytest.fixture
def people():
    return {
        "reporter": ActingClient(new_user("reporter")),
        "other_reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
    }


# --- schema -------------------------------------------------------------------------------------------------------


def test_case_links_has_the_standard_columns_and_the_updated_at_trigger():
    with connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'case_links'"
            ).fetchall()
        }
        assert {"id", "case_id", "linked_case_id", "created_by", "created_at", "updated_at", "deleted_at", "is_deleted"} <= columns
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'case_links'::regclass AND tgname = 'case_links_set_updated_at'"
        ).fetchone()[0]
    assert triggers == 1


def insert_link(case_id: str, linked_case_id: str, created_by: str) -> None:
    # A direct insert, so the constraints themselves are tested. Used for setup that the API cannot reach.
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_links (case_id, linked_case_id, created_by) VALUES (%s, %s, %s)",
            (case_id, linked_case_id, created_by),
        )


def test_the_unique_index_refuses_the_same_pair_in_either_order(people):
    triager = new_user("triager")
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    insert_link(first, second, triager)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_link(second, first, triager)


def test_the_unique_index_refuses_a_second_active_row_for_the_same_pair(people):
    triager = new_user("triager")
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    insert_link(first, second, triager)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_link(first, second, triager)


def test_a_soft_deleted_link_does_not_block_linking_the_pair_again(people):
    triager = new_user("triager")
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    insert_link(first, second, triager)
    with connect() as conn:
        conn.execute("UPDATE case_links SET is_deleted = true, deleted_at = now() WHERE case_id = %s", (first,))
    insert_link(second, first, triager)


def test_a_case_cannot_be_linked_to_itself(people):
    case_id = new_case(people["reporter"])
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_link(case_id, case_id, new_user("triager"))


@pytest.mark.parametrize(
    ("is_deleted", "deleted_at_sql"),
    [
        pytest.param(True, "NULL", id="deleted-without-deleted_at"),
        pytest.param(False, "now()", id="active-with-deleted_at"),
    ],
)
def test_the_soft_delete_check_refuses_inconsistent_rows(people, is_deleted, deleted_at_sql):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = new_user("triager")
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                f"INSERT INTO case_links (case_id, linked_case_id, created_by, is_deleted, deleted_at) "
                f"VALUES (%s, %s, %s, %s, {deleted_at_sql})",
                (first, second, triager, is_deleted),
            )


# --- flow -----------------------------------------------------------------------------------------------------------


def test_triager_links_two_open_cases_and_both_sides_list_the_link(people):
    first = new_case(people["reporter"], "Incident one")
    second = new_case(people["reporter"], "Incident two")
    triager = people["triager"]

    created = triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second})
    assert created.status_code == 201, created.text
    assert created.json()["linked_case_id"] == second
    assert created.json()["linked_case_title"] == "Incident two"

    from_first = triager.get(f"/api/v1/cases/{first}/links").json()
    from_second = triager.get(f"/api/v1/cases/{second}/links").json()
    assert [entry["linked_case_id"] for entry in from_first] == [second]
    assert [entry["linked_case_id"] for entry in from_second] == [first]
    assert from_second[0]["linked_case_title"] == "Incident one"


def test_the_pair_cannot_be_linked_twice_and_the_same_case_is_refused(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second}).status_code == 201

    again = triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second})
    assert again.status_code == 409
    assert again.json() == {"detail": "cases are already linked"}
    reverse = triager.post(f"/api/v1/cases/{second}/links", json={"linked_case_id": first})
    assert reverse.status_code == 409

    same = triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": first})
    assert same.status_code == 422
    assert same.json() == {"detail": "a case cannot be linked to itself"}


def test_a_closed_case_on_either_side_refuses_the_link(people):
    open_case = new_case(people["reporter"], "Open")
    closed_case = new_case(people["reporter"], "Closed")
    close_case_in_database(closed_case, new_user("triager"))
    triager = people["triager"]

    for source, linked in ((open_case, closed_case), (closed_case, open_case)):
        response = triager.post(f"/api/v1/cases/{source}/links", json={"linked_case_id": linked})
        assert response.status_code == 409
        assert response.json() == {"detail": "case is closed"}


def test_the_case_must_be_visible_to_add_a_link_and_is_not_found_otherwise(people):
    own = new_case(people["reporter"], "Own")
    channel = new_reporter_channel_case(people["reporter"])
    # The triager cannot see a reporter-channel case until it is assigned, so the link is refused as not found.
    response = people["triager"].post(f"/api/v1/cases/{own}/links", json={"linked_case_id": channel})
    assert response.status_code == 404
    assert response.json() == {"detail": "case not found"}


def test_a_link_to_a_case_the_reader_cannot_see_has_no_id_and_no_title(people):
    own = new_case(people["reporter"], "Own")
    channel = new_reporter_channel_case(people["reporter"])
    insert_link(own, channel, new_user("triager"))

    listed = people["triager"].get(f"/api/v1/cases/{own}/links")
    assert listed.status_code == 200
    assert listed.json()[0]["linked_case_id"] is None
    assert listed.json()[0]["linked_case_title"] is None
    assert set(listed.json()[0]) == {"linked_case_id", "linked_case_title", "created_at"}


def test_the_reporter_reads_links_of_an_own_case_but_cannot_change_them(people):
    own = new_case(people["reporter"], "Own")
    other = new_case(people["other_reporter"], "Other")
    assert people["triager"].post(f"/api/v1/cases/{own}/links", json={"linked_case_id": other}).status_code == 201
    assert people["reporter"].get(f"/api/v1/cases/{own}/links").status_code == 200
    # Writes need cases:triage, which the reporter role does not hold.
    assert people["reporter"].post(f"/api/v1/cases/{own}/links", json={"linked_case_id": other}).status_code == 403
    assert people["reporter"].delete(f"/api/v1/cases/{own}/links/{other}").status_code == 403


def test_removing_a_link_soft_deletes_it_and_the_pair_can_be_linked_again(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second}).status_code == 201

    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 204
    with connect() as conn:
        row = conn.execute(
            "SELECT is_deleted, deleted_at IS NOT NULL FROM case_links WHERE case_id = %s AND linked_case_id = %s",
            (first, second),
        ).fetchone()
    assert row == (True, True)
    assert triager.get(f"/api/v1/cases/{first}/links").json() == []
    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 404
    assert triager.post(f"/api/v1/cases/{second}/links", json={"linked_case_id": first}).status_code == 201


def test_a_closed_case_refuses_removal_too(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second}).status_code == 201
    close_case_in_database(second, new_user("triager"))
    response = triager.delete(f"/api/v1/cases/{first}/links/{second}")
    assert response.status_code == 409
    assert response.json() == {"detail": "case is closed"}


# --- audit ----------------------------------------------------------------------------------------------------------


def test_each_add_and_remove_writes_one_audit_row_with_the_actor_and_no_free_text(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    triager_id = triager.user_id

    assert triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second}).status_code == 201
    assert triager.post(f"/api/v1/cases/{first}/links", json={"linked_case_id": second}).status_code == 409
    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 204

    added = audit_rows(audit.CASE_LINK_ADD, first)
    removed = audit_rows(audit.CASE_LINK_REMOVE, first)
    assert len(added) == 1, "a refused add writes no audit row"
    assert len(removed) == 1
    assert added[0]["actor_id"] == triager_id
    assert added[0]["target_type"] == "case"
    assert added[0]["details"]["linked_case_id"] == second
    assert removed[0]["actor_id"] == triager_id
    assert removed[0]["details"]["link_id"] == added[0]["details"]["link_id"]
    assert set(added[0]["details"]) == {"link_id", "linked_case_id"}


def test_a_refused_remove_writes_no_audit_row(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    assert people["triager"].delete(f"/api/v1/cases/{first}/links/{second}").status_code == 404
    assert audit_rows(audit.CASE_LINK_REMOVE, first) == []
