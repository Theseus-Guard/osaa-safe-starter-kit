"""Case links against PostgreSQL: the typed links, the constraints, the duplicate closure, the visibility rules and the
audit rows, through the whole flow.

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
from safe_kit.rbac import list_role_permissions, set_role_permissions
from safe_kit.reviews import TEMPLATE_LAYERS
from test_evidence import db_user
# Fixtures are requested by name, which ruff reports as a redefinition. That is the intended use here.
# ruff: noqa: F401, F811
from test_fixture_p6_disclosure import dworld  # dworld: fixture
from test_evidence_lifecycle import provider  # provider: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import (  # the notice helpers of the disclosure world
    approved_version,
    notice_action,
    payload,
    record,
    with_independent_triager,
)
from test_fixture_t12a_records import escalate, escalation_values, review

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)"
        ).fetchone()[0]
        if not exists:
            conn.execute(
                "INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')"
            )
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

    def patch(self, url, **kwargs):
        return self._request("patch", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._request("delete", url, **kwargs)


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def new_case(reporter: ActingClient, title: str = "Case") -> str:
    response = reporter.post(
        "/api/v1/cases", json={"title": title, "awareness_time": past()}, headers=idem()
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def new_reporter_channel_case(reporter: ActingClient) -> str:
    response = reporter.post(
        "/api/v1/reports",
        json={"title": "Report", "awareness_time": past(), "contact": None},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def link_body(
    other: str,
    link_type: str = "related",
    reason: str = "Same incident",
    primary: str | None = None,
    share: bool = False,
) -> dict:
    body = {
        "linked_case_id": other,
        "link_type": link_type,
        "reason": reason,
        "share_visibility": share,
    }
    if primary is not None:
        body["primary_case_id"] = primary
    return body


def add_link(client: ActingClient, case_id: str, other: str, **kwargs):
    return client.post(
        f"/api/v1/cases/{case_id}/links", json=link_body(other, **kwargs), headers=idem()
    )


def close_case_in_database(case_id: str, actor_id: str) -> None:
    # Closing through the API needs a review and settled deadlines. The schema is what these tests check, so the
    # closed state is set directly here. The application itself never changes a case this way.
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (actor_id, case_id),
        )


def finish_review_layers(people: dict, round_id: str, revision: int) -> None:
    """Set every layer of the round that is not set yet to not_applicable, with a reason.

    A pass needs an evidence item of the case, which these tests do not create, so not_applicable is used.
    """
    for layer in TEMPLATE_LAYERS:
        if layer == "model":  # set by settle_for_closure
            continue
        response = people["reviewer"].patch(
            f"/api/v1/reviews/{round_id}/layers/{layer}",
            json={"outcome": "not_applicable", "reason": "Not applicable to this test case"},
            headers={**idem(), "If-Match": f'"{revision}"'},
        )
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]


def settle_for_closure(people: dict, case_id: str, *, review_layers: bool = True) -> None:
    """Make a case closable: a reviewer is assigned and reviews it, and every open deadline task is done.

    With review_layers every layer of the round is set, so no review record is open. Without it only one layer is set.
    The task rows are set directly; the tests then check that nothing else about the tasks changed.
    """
    assigned = people["triager"].post(
        f"/api/v1/cases/{case_id}/assignments",
        json={"user_id": people["reviewer"].user_id},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    opened = people["reviewer"].post(
        "/api/v1/reviews", headers=idem(), json={"case_id": case_id}
    )
    assert opened.status_code == 201, opened.text
    reviewed = people["reviewer"].patch(
        f"/api/v1/reviews/{opened.json()['id']}/layers/model",
        json={"outcome": "fail", "reason": "Reviewed for this test"},
        headers={**idem(), "If-Match": f'"{opened.json()["revision"]}"'},
    )
    assert reviewed.status_code == 200, reviewed.text
    if review_layers:
        finish_review_layers(people, opened.json()["id"], reviewed.json()["revision"])
    with connect() as conn:
        conn.execute(
            "UPDATE deadline_tasks SET status = 'done' WHERE case_id = %s AND NOT is_deleted AND status = 'open'",
            (case_id,),
        )


def task_snapshot(case_id: str) -> list[tuple[str, str]]:
    with connect() as conn:
        return conn.execute(
            "SELECT kind, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY kind",
            (case_id,),
        ).fetchall()


def case_status(case_id: str) -> str:
    with connect() as conn:
        return conn.execute(
            "SELECT status FROM cases WHERE id = %s", (case_id,)
        ).fetchone()[0]


def link_rows(first: str, second: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT link_type, is_deleted FROM case_links WHERE NOT is_deleted "
            "AND ((case_id = %s AND linked_case_id = %s) OR (case_id = %s AND linked_case_id = %s))",
            (first, second, second, first),
        ).fetchall()


def audit_rows(action: str, case_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT actor_id::text, target_type, target_id, details FROM audit_events "
            "WHERE action = %s AND target_id = %s AND NOT is_deleted ORDER BY created_at, id",
            (action, case_id),
        ).fetchall()
    return [
        {
            "actor_id": row[0],
            "target_type": row[1],
            "target_id": row[2],
            "details": row[3],
        }
        for row in rows
    ]


def insert_link(case_id: str, linked_case_id: str, created_by: str) -> None:
    # A direct insert of a related link without a reason, the shape of a link made before link types existed.
    # Used for setup that the API cannot reach. The application always sends a type and a reason.
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_links (case_id, linked_case_id, created_by, link_type) VALUES (%s, %s, %s, 'related')",
            (case_id, linked_case_id, created_by),
        )


def insert_typed_link(
    case_id: str,
    linked_case_id: str,
    created_by: str,
    link_type: str,
    reason: str | None,
    primary: str | None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_links (case_id, linked_case_id, created_by, link_type, reason, primary_case_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (case_id, linked_case_id, created_by, link_type, reason, primary),
        )


def set_triage_for(role: str, granted: bool):
    """Temporarily grants (or restores) cases:triage for one role. Returns a restore callable."""
    before = frozenset(list_role_permissions()[role])
    set_role_permissions(role, before | {"cases:triage"} if granted else before)
    return lambda: set_role_permissions(role, before)


@pytest.fixture
def people():
    return {
        "reporter": ActingClient(new_user("reporter")),
        "other_reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
        "reviewer": ActingClient(new_user("reviewer")),
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
        assert {
            "id",
            "case_id",
            "linked_case_id",
            "created_by",
            "created_at",
            "updated_at",
            "deleted_at",
            "is_deleted",
            "link_type",
            "reason",
            "primary_case_id",
            "share_visibility",
        } <= columns
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'case_links'::regclass AND tgname = 'case_links_set_updated_at'"
        ).fetchone()[0]
    assert triggers == 1


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
        conn.execute(
            "UPDATE case_links SET is_deleted = true, deleted_at = now() WHERE case_id = %s",
            (first,),
        )
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
def test_the_soft_delete_check_refuses_inconsistent_rows(
    people, is_deleted, deleted_at_sql
):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = new_user("triager")
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                f"INSERT INTO case_links (case_id, linked_case_id, created_by, link_type, is_deleted, deleted_at) "
                f"VALUES (%s, %s, %s, 'related', %s, {deleted_at_sql})",
                (first, second, triager, is_deleted),
            )


@pytest.mark.parametrize(
    ("link_type", "reason", "use_primary", "primary_is_third"),
    [
        pytest.param("partner", "Same incident", False, False, id="unknown-type"),
        pytest.param("related", "   ", False, False, id="blank-reason"),
        pytest.param("related", "x" * 501, False, False, id="reason-too-long"),
        pytest.param("related", "Same incident", True, False, id="related-with-main"),
        pytest.param("duplicate", "Same incident", False, False, id="duplicate-without-main"),
        pytest.param("root_sub", "Same incident", True, True, id="main-outside-the-pair"),
    ],
)
def test_the_typed_link_checks_refuse_invalid_rows(
    people, link_type, reason, use_primary, primary_is_third
):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    third = new_case(people["reporter"], "Third")
    primary = third if primary_is_third else (first if use_primary else None)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_typed_link(
            first, second, new_user("triager"), link_type, reason, primary
        )


def test_a_related_row_without_a_reason_is_accepted_and_can_be_soft_deleted(people):
    # Links made before migration 0052 have no reason, and the reason check accepts NULL so they can still be soft deleted.
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    insert_link(first, second, new_user("triager"))
    with connect() as conn:
        row = conn.execute(
            "SELECT link_type, reason, primary_case_id, share_visibility FROM case_links "
            "WHERE case_id = %s AND linked_case_id = %s",
            (first, second),
        ).fetchone()
        assert row == ("related", None, None, False)
        conn.execute(
            "UPDATE case_links SET is_deleted = true, deleted_at = now() WHERE case_id = %s AND linked_case_id = %s",
            (first, second),
        )


# --- flow -----------------------------------------------------------------------------------------------------------


def test_triager_links_two_open_cases_and_both_sides_list_the_link(people):
    first = new_case(people["reporter"], "Incident one")
    second = new_case(people["reporter"], "Incident two")
    triager = people["triager"]

    created = add_link(triager, first, second, reason="  Same incident  ")
    assert created.status_code == 201, created.text
    assert created.json()["linked_case_id"] == second
    assert created.json()["linked_case_title"] == "Incident two"
    assert created.json()["link_type"] == "related"
    assert created.json()["reason"] == "Same incident"
    assert created.json()["share_visibility"] is False
    assert created.json()["primary_side"] is None

    from_first = triager.get(f"/api/v1/cases/{first}/links").json()
    from_second = triager.get(f"/api/v1/cases/{second}/links").json()
    assert [entry["linked_case_id"] for entry in from_first] == [second]
    assert [entry["linked_case_id"] for entry in from_second] == [first]
    assert from_second[0]["linked_case_title"] == "Incident one"


def test_a_reason_is_required_and_the_type_is_checked_by_the_api(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    no_reason = triager.post(
        f"/api/v1/cases/{first}/links",
        json={"linked_case_id": second, "link_type": "related"},
        headers=idem(),
    )
    assert no_reason.status_code == 400
    blank = add_link(triager, first, second, reason="   ")
    assert blank.status_code == 422
    assert "a reason is required" in blank.json()["detail"]
    no_main = add_link(triager, first, second, link_type="root_sub")
    assert no_main.status_code == 422
    assert "primary_case_id" in no_main.json()["detail"]
    assert link_rows(first, second) == []


def test_the_pair_cannot_be_linked_twice_and_the_same_case_is_refused(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert add_link(triager, first, second).status_code == 201

    again = add_link(triager, first, second)
    assert again.status_code == 409
    assert again.json()["detail"] == "cases are already linked"
    reverse = add_link(triager, second, first)
    assert reverse.status_code == 409

    same = add_link(triager, first, first)
    assert same.status_code == 422
    assert same.json()["detail"] == "a case cannot be linked to itself"


def test_a_closed_case_on_either_side_refuses_the_link(people):
    open_case = new_case(people["reporter"], "Open")
    closed_case = new_case(people["reporter"], "Closed")
    close_case_in_database(closed_case, new_user("triager"))
    triager = people["triager"]

    for source, linked in ((open_case, closed_case), (closed_case, open_case)):
        response = add_link(triager, source, linked)
        assert response.status_code == 409
        assert response.json()["detail"] == "case is closed"


def test_the_case_must_be_visible_to_add_a_link_and_is_not_found_otherwise(people):
    own = new_case(people["reporter"], "Own")
    channel = new_reporter_channel_case(people["reporter"])
    # An investigator assigned to the own case cannot see the unassigned reporter-channel case, so the link is refused
    # as not found.
    investigator = new_user("investigator")
    assigned = people["triager"].post(
        f"/api/v1/cases/{own}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    # Linking needs cases:triage, granted to the investigator for this block only, so the 404 comes from visibility.
    restore = set_triage_for("investigator", True)
    try:
        response = add_link(ActingClient(investigator), own, channel)
    finally:
        restore()
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"
    # The triager (cases:assign) sees the reporter-channel case before assignment, so the same link is accepted.
    linked = add_link(people["triager"], own, channel)
    assert linked.status_code == 201, linked.text


def test_a_link_to_a_case_the_reader_cannot_see_has_no_id_and_no_title(people):
    own = new_case(people["reporter"], "Own")
    channel = new_reporter_channel_case(people["reporter"])
    investigator = new_user("investigator")
    assigned = people["triager"].post(
        f"/api/v1/cases/{own}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    insert_link(own, channel, new_user("triager"))

    # The investigator sees the own case but not the reporter-channel case it links to.
    listed = ActingClient(investigator).get(f"/api/v1/cases/{own}/links")
    assert listed.status_code == 200
    assert listed.json()[0]["linked_case_id"] is None
    assert listed.json()[0]["linked_case_title"] is None
    assert set(listed.json()[0]) == {
        "linked_case_id",
        "linked_case_title",
        "link_type",
        "reason",
        "share_visibility",
        "primary_side",
        "primary_case_id",
        "created_at",
    }


def test_the_reporter_reads_links_of_an_own_case_but_cannot_change_them(people):
    own = new_case(people["reporter"], "Own")
    other = new_case(people["other_reporter"], "Other")
    assert add_link(people["triager"], own, other).status_code == 201
    assert people["reporter"].get(f"/api/v1/cases/{own}/links").status_code == 200
    # Writes need cases:triage, which the reporter role does not hold.
    assert add_link(people["reporter"], own, other).status_code == 403
    assert (
        people["reporter"].delete(f"/api/v1/cases/{own}/links/{other}").status_code
        == 403
    )


def test_removing_a_link_soft_deletes_it_and_the_pair_can_be_linked_again(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert add_link(triager, first, second).status_code == 201

    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 204
    with connect() as conn:
        row = conn.execute(
            "SELECT is_deleted, deleted_at IS NOT NULL FROM case_links WHERE case_id = %s AND linked_case_id = %s",
            (first, second),
        ).fetchone()
    assert row == (True, True)
    assert triager.get(f"/api/v1/cases/{first}/links").json() == []
    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 404
    assert add_link(triager, second, first).status_code == 201


def test_a_closed_case_refuses_removal_too(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    assert add_link(triager, first, second).status_code == 201
    close_case_in_database(second, new_user("triager"))
    response = triager.delete(f"/api/v1/cases/{first}/links/{second}")
    assert response.status_code == 409
    assert response.json()["detail"] == "case is closed"


# --- related: visibility and reference only -----------------------------------------------------------------------


def test_a_related_link_changes_no_status_and_no_task_on_either_side(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    before = (
        case_status(first),
        case_status(second),
        task_snapshot(first),
        task_snapshot(second),
    )
    assert add_link(people["triager"], first, second, link_type="related").status_code == 201
    assert people["triager"].delete(f"/api/v1/cases/{first}/links/{second}").status_code == 204
    after = (
        case_status(first),
        case_status(second),
        task_snapshot(first),
        task_snapshot(second),
    )
    assert after == before


# --- duplicate: closed through the link, tasks kept ---------------------------------------------------------------


@pytest.mark.parametrize(
    "from_duplicate_page",
    [pytest.param(False, id="from-the-main-case"), pytest.param(True, id="from-the-duplicate-case")],
)
def test_a_duplicate_link_closes_the_duplicate_and_points_to_the_main_case(
    people, from_duplicate_page
):
    main = new_case(people["reporter"], "Main")
    duplicate = new_case(people["reporter"], "Duplicate")
    settle_for_closure(people, duplicate)
    main_tasks = task_snapshot(main)
    duplicate_tasks = task_snapshot(duplicate)
    assert any(status == "open" for _kind, status in main_tasks)

    triager = people["triager"]
    # The link is made from the page of one case; the closure is the same whichever page it is made from.
    page, other = (duplicate, main) if from_duplicate_page else (main, duplicate)
    created = add_link(triager, page, other, link_type="duplicate", primary=main)
    assert created.status_code == 201, created.text
    assert created.json()["link_type"] == "duplicate"
    assert created.json()["primary_case_id"] == main
    # From the main page the main case is "this"; from the duplicate page it is "linked".
    assert created.json()["primary_side"] == ("linked" if from_duplicate_page else "this")

    # The duplicate is closed; the main case stays open.
    assert case_status(duplicate) == "closed"
    assert case_status(main) == "open"
    # Tasks are not moved to the main case, and the duplicate keeps its own tasks.
    assert task_snapshot(main) == main_tasks
    assert task_snapshot(duplicate) == duplicate_tasks
    # The closed duplicate shows the main case as its linked case.
    from_dup = triager.get(f"/api/v1/cases/{duplicate}/links").json()
    assert from_dup[0]["linked_case_id"] == main
    assert from_dup[0]["primary_side"] == "linked"
    assert from_dup[0]["primary_case_id"] == main
    # One closure audit row for the duplicate, by the actor; one link audit row on the page case.
    closed = audit_rows(audit.CASE_CLOSE, duplicate)
    assert len(closed) == 1
    assert closed[0]["actor_id"] == triager.user_id
    assert len(audit_rows(audit.CASE_LINK_ADD, page)) == 1
    assert link_rows(main, duplicate) == [("duplicate", False)]


def test_a_duplicate_with_an_open_deadline_task_is_refused_and_nothing_changes(people):
    main = new_case(people["reporter"], "Main")
    duplicate = new_case(people["reporter"], "Duplicate")
    before_tasks = task_snapshot(duplicate)
    assert any(status == "open" for _kind, status in before_tasks)

    refused = add_link(
        people["triager"], main, duplicate, link_type="duplicate", primary=main
    )
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("duplicate case cannot be closed: ")
    assert "deadline task" in refused.json()["detail"]
    assert case_status(duplicate) == "open"
    assert task_snapshot(duplicate) == before_tasks
    assert link_rows(main, duplicate) == []
    assert audit_rows(audit.CASE_LINK_ADD, main) == []
    assert audit_rows(audit.CASE_CLOSE, duplicate) == []


def test_open_tasks_of_the_main_case_do_not_block_the_duplicate_closure(people):
    main = new_case(people["reporter"], "Main")
    duplicate = new_case(people["reporter"], "Duplicate")
    settle_for_closure(people, duplicate)
    assert any(status == "open" for _kind, status in task_snapshot(main))
    created = add_link(
        people["triager"], main, duplicate, link_type="duplicate", primary=main
    )
    assert created.status_code == 201, created.text
    assert case_status(duplicate) == "closed"


def test_an_unset_review_layer_refuses_the_duplicate_closure_and_nothing_changes(people):
    main = new_case(people["reporter"], "Main")
    duplicate = new_case(people["reporter"], "Duplicate")
    settle_for_closure(people, duplicate, review_layers=False)
    before_tasks = task_snapshot(duplicate)

    refused = add_link(
        people["triager"], main, duplicate, link_type="duplicate", primary=main
    )
    assert refused.status_code == 409
    assert "review layer(s) are not set" in refused.json()["detail"]
    assert case_status(duplicate) == "open"
    assert task_snapshot(duplicate) == before_tasks
    assert link_rows(main, duplicate) == []

    # Once every layer is set (a pass, or a reason), the same link is accepted.
    with connect() as conn:
        round_id, revision = conn.execute(
            "SELECT rr.id::text, rr.revision FROM review_rounds rr "
            "WHERE rr.case_id = %s AND NOT rr.is_deleted ORDER BY rr.opened_at DESC LIMIT 1",
            (duplicate,),
        ).fetchone()
    finish_review_layers(people, round_id, revision)
    created = add_link(
        people["triager"], main, duplicate, link_type="duplicate", primary=main
    )
    assert created.status_code == 201, created.text
    assert case_status(duplicate) == "closed"


def test_an_open_notice_refuses_the_duplicate_closure_until_it_is_cancelled(
    people, dworld
):
    settle_for_closure(people, dworld["case_id"])
    version, _ = approved_version(dworld)
    recorded = record(dworld, "approver", version["id"], payload())
    assert recorded.status_code == 201, recorded.text
    notice = recorded.json()
    main = new_case(people["reporter"], "Main")

    refused = add_link(
        people["triager"], main, dworld["case_id"], link_type="duplicate", primary=main
    )
    assert refused.status_code == 409
    assert "external notice(s) are open" in refused.json()["detail"]
    assert case_status(dworld["case_id"]) == "open"

    cancelled = notice_action(
        dworld,
        "approver",
        version["id"],
        notice["id"],
        "cancel",
        {"reason": "Withdrawn for the test"},
    )
    assert cancelled.status_code == 200, cancelled.text
    created = add_link(
        people["triager"], main, dworld["case_id"], link_type="duplicate", primary=main
    )
    assert created.status_code == 201, created.text
    assert case_status(dworld["case_id"]) == "closed"


def test_an_escalation_and_a_notice_review_are_open_until_a_notice_is_recorded(
    people, dworld
):
    dw = with_independent_triager(dworld)
    settle_for_closure(people, dw["case_id"])
    version, _ = approved_version(dw)
    escalated = escalate(
        dw, "independent", version["id"], escalation_values(db_user("disclosure_approver"))
    )
    assert escalated.status_code == 201, escalated.text
    package = review(dw, "approver", version["id"], "package")
    assert package.status_code == 201, package.text
    main = new_case(people["reporter"], "Main")

    refused = add_link(
        people["triager"], main, dw["case_id"], link_type="duplicate", primary=main
    )
    assert refused.status_code == 409
    assert "escalation(s) have no notice recorded yet" in refused.json()["detail"]
    assert "notice review(s) have no notice recorded yet" in refused.json()["detail"]
    assert case_status(dw["case_id"]) == "open"

    # A notice for the version clears both records; the notice itself is open until it is cancelled.
    recorded = record(dw, "approver", version["id"], payload())
    assert recorded.status_code == 201, recorded.text
    notice = recorded.json()
    refused = add_link(
        people["triager"], main, dw["case_id"], link_type="duplicate", primary=main
    )
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "external notice(s) are open" in detail
    assert "have no notice recorded yet" not in detail
    cancelled = notice_action(
        dw,
        "approver",
        version["id"],
        notice["id"],
        "cancel",
        {"reason": "Withdrawn for the test"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert add_link(
        people["triager"], main, dw["case_id"], link_type="duplicate", primary=main
    ).status_code == 201
    assert case_status(dw["case_id"]) == "closed"


# --- root and sub-case: read only through the link ----------------------------------------------------------------


def test_a_reader_of_the_sub_case_reads_the_root_through_the_link_but_cannot_change_it(
    people,
):
    root = new_case(people["other_reporter"], "Root")
    sub = new_case(people["reporter"], "Sub")
    investigator = new_user("investigator")
    assigned = people["triager"].post(
        f"/api/v1/cases/{sub}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    reader = ActingClient(investigator)
    assert reader.get(f"/api/v1/cases/{root}").status_code == 404

    created = add_link(
        people["triager"], sub, root, link_type="root_sub", primary=root
    )
    assert created.status_code == 201, created.text
    assert created.json()["primary_side"] == "linked"

    # The sub-case reader reads the root for analysis.
    seen = reader.get(f"/api/v1/cases/{root}")
    assert seen.status_code == 200
    assert seen.json()["title"] == "Root"
    # Read only: a write to the root is refused as not found, even with cases:triage.
    restore = set_triage_for("investigator", True)
    try:
        write = add_link(reader, root, new_case(people["other_reporter"], "Other"))
    finally:
        restore()
    assert write.status_code == 404
    assert case_status(root) == "open"


def test_the_root_read_through_is_one_way(people):
    root = new_case(people["other_reporter"], "Root")
    sub = new_case(people["reporter"], "Sub")
    assert add_link(
        people["triager"], sub, root, link_type="root_sub", primary=root
    ).status_code == 201
    # A reader of the root only does not see the sub-case through the link.
    investigator = new_user("investigator")
    assigned = people["triager"].post(
        f"/api/v1/cases/{root}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    assert ActingClient(investigator).get(f"/api/v1/cases/{root}").status_code == 200
    assert ActingClient(investigator).get(f"/api/v1/cases/{sub}").status_code == 404


def test_removing_a_root_sub_link_closes_the_root_read_through(people):
    root = new_case(people["other_reporter"], "Root")
    sub = new_case(people["reporter"], "Sub")
    investigator = new_user("investigator")
    assert people["triager"].post(
        f"/api/v1/cases/{sub}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    ).status_code == 201
    assert add_link(
        people["triager"], sub, root, link_type="root_sub", primary=root
    ).status_code == 201
    reader = ActingClient(investigator)
    assert reader.get(f"/api/v1/cases/{root}").status_code == 200
    assert people["triager"].delete(f"/api/v1/cases/{sub}/links/{root}").status_code == 204
    assert reader.get(f"/api/v1/cases/{root}").status_code == 404


# --- visibility checkbox: users of one case see the other ---------------------------------------------------------


def test_the_visibility_checkbox_lets_users_of_one_case_see_the_other_and_nothing_more(
    people,
):
    a = new_case(people["reporter"], "Case A")
    b = new_case(people["other_reporter"], "Case B")
    c = new_case(people["other_reporter"], "Case C")
    investigator = new_user("investigator")
    assigned = people["triager"].post(
        f"/api/v1/cases/{a}/assignments",
        json={"user_id": investigator},
        headers=idem(),
    )
    assert assigned.status_code == 201, assigned.text
    reader = ActingClient(investigator)
    assert reader.get(f"/api/v1/cases/{b}").status_code == 404

    # Without the checkbox the other case stays hidden.
    assert add_link(people["triager"], a, b, share=False).status_code == 201
    assert reader.get(f"/api/v1/cases/{b}").status_code == 404
    assert people["triager"].delete(f"/api/v1/cases/{a}/links/{b}").status_code == 204

    # With the checkbox the users of A see B, and the link shows B's title to them.
    assert add_link(people["triager"], a, b, share=True).status_code == 201
    assert reader.get(f"/api/v1/cases/{b}").status_code == 200
    listed = reader.get(f"/api/v1/cases/{a}/links").json()
    assert listed[0]["linked_case_title"] == "Case B"
    assert listed[0]["share_visibility"] is True
    assert b in [item["id"] for item in reader.get("/api/v1/cases?limit=200").json()["items"]]

    # One step only: C is linked to B with the checkbox, but B is not the investigator's own case.
    assert add_link(people["triager"], b, c, share=True).status_code == 201
    assert reader.get(f"/api/v1/cases/{c}").status_code == 404

    # The shared case is read only: a write to B through the link is refused as not found.
    restore = set_triage_for("investigator", True)
    try:
        write = add_link(reader, b, c)
    finally:
        restore()
    assert write.status_code == 404
    assert case_status(b) == "open"

    # Removing the link hides B again.
    assert people["triager"].delete(f"/api/v1/cases/{a}/links/{b}").status_code == 204
    assert reader.get(f"/api/v1/cases/{b}").status_code == 404


def test_a_case_read_through_a_root_link_shows_no_further_link_of_the_root(people):
    # The investigator owns the sub-case. The root is one step away; a related link on the root is a second step.
    root = new_case(people["other_reporter"], "Root")
    sub = new_case(people["reporter"], "Sub")
    other = new_case(people["other_reporter"], "Other")
    investigator = new_user("investigator")
    assert people["triager"].post(
        f"/api/v1/cases/{sub}/assignments", json={"user_id": investigator}, headers=idem()
    ).status_code == 201
    assert add_link(people["triager"], sub, root, link_type="root_sub", primary=root).status_code == 201
    assert add_link(people["triager"], root, other, share=True).status_code == 201

    reader = ActingClient(investigator)
    assert reader.get(f"/api/v1/cases/{root}").status_code == 200
    assert reader.get(f"/api/v1/cases/{other}").status_code == 404


def test_a_case_read_through_a_share_link_shows_neither_its_root_nor_a_second_share(
    people,
):
    own = new_case(people["reporter"], "Own")
    shared = new_case(people["other_reporter"], "Shared")
    root = new_case(people["other_reporter"], "Root")
    second = new_case(people["other_reporter"], "Second")
    investigator = new_user("investigator")
    assert people["triager"].post(
        f"/api/v1/cases/{own}/assignments", json={"user_id": investigator}, headers=idem()
    ).status_code == 201
    assert add_link(people["triager"], own, shared, share=True).status_code == 201
    # The shared case is the sub-case of a root_sub link, and it shares a second case. Neither is a step the reader takes.
    assert add_link(people["triager"], shared, root, link_type="root_sub", primary=root).status_code == 201
    assert add_link(people["triager"], shared, second, share=True).status_code == 201

    reader = ActingClient(investigator)
    assert reader.get(f"/api/v1/cases/{shared}").status_code == 200
    assert reader.get(f"/api/v1/cases/{root}").status_code == 404
    assert reader.get(f"/api/v1/cases/{second}").status_code == 404


def test_the_reason_is_shown_only_when_the_reader_can_see_the_other_case(people):
    mine = new_case(people["reporter"], "Mine")
    theirs = new_case(people["other_reporter"], "Theirs")
    investigator = new_user("investigator")
    assert people["triager"].post(
        f"/api/v1/cases/{mine}/assignments", json={"user_id": investigator}, headers=idem()
    ).status_code == 201
    reader = ActingClient(investigator)

    # Checkbox off: the reader sees one case, so the link shows no reason and no other case.
    assert add_link(
        people["triager"], mine, theirs, reason="Confidential detail"
    ).status_code == 201
    hidden = reader.get(f"/api/v1/cases/{mine}/links").json()[0]
    assert hidden["linked_case_id"] is None
    assert hidden["reason"] is None
    assert "Confidential detail" not in str(hidden)

    # Checkbox on: the reader sees the other case, so the reason is shown too.
    assert people["triager"].delete(f"/api/v1/cases/{mine}/links/{theirs}").status_code == 204
    assert add_link(
        people["triager"], mine, theirs, reason="Confidential detail", share=True
    ).status_code == 201
    shown = reader.get(f"/api/v1/cases/{mine}/links").json()[0]
    assert shown["linked_case_id"] == theirs
    assert shown["reason"] == "Confidential detail"

    # The triager sees both cases, so the reason is shown from both sides.
    both = people["triager"].get(f"/api/v1/cases/{theirs}/links").json()[0]
    assert both["reason"] == "Confidential detail"


def test_the_main_case_id_is_shown_to_a_reader_of_the_root_and_not_of_a_hidden_sub(
    people,
):
    root = new_case(people["reporter"], "Root")
    sub = new_case(people["other_reporter"], "Sub")
    investigator = new_user("investigator")
    assert people["triager"].post(
        f"/api/v1/cases/{root}/assignments", json={"user_id": investigator}, headers=idem()
    ).status_code == 201
    assert add_link(
        people["triager"], sub, root, link_type="root_sub", primary=root
    ).status_code == 201

    # The root is the reader's own case, so its ID is shown. The sub-case is not visible, so its title and reason are not.
    listed = ActingClient(investigator).get(f"/api/v1/cases/{root}/links").json()[0]
    assert listed["primary_side"] == "this"
    assert listed["primary_case_id"] == root
    assert listed["linked_case_id"] is None
    assert listed["reason"] is None


# --- audit----------------------------------------------------------------------------------------------------------


def test_each_add_and_remove_writes_one_audit_row_with_the_actor_and_no_free_text(
    people,
):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    triager = people["triager"]
    triager_id = triager.user_id

    assert (
        add_link(triager, first, second, reason="Confidential detail").status_code == 201
    )
    assert add_link(triager, first, second).status_code == 409
    assert triager.delete(f"/api/v1/cases/{first}/links/{second}").status_code == 204

    added = audit_rows(audit.CASE_LINK_ADD, first)
    removed = audit_rows(audit.CASE_LINK_REMOVE, first)
    assert len(added) == 1, "a refused add writes no audit row"
    assert len(removed) == 1
    assert added[0]["actor_id"] == triager_id
    assert added[0]["target_type"] == "case"
    assert added[0]["details"]["linked_case_id"] == second
    assert added[0]["details"]["link_type"] == "related"
    assert added[0]["details"]["reason"] == {"length": len("Confidential detail")}
    assert removed[0]["actor_id"] == triager_id
    assert removed[0]["details"]["link_id"] == added[0]["details"]["link_id"]
    assert set(added[0]["details"]) == {
        "link_id",
        "linked_case_id",
        "link_type",
        "share_visibility",
        "reason",
    }
    assert "Confidential detail" not in str(added[0]["details"])


def test_a_refused_remove_writes_no_audit_row(people):
    first = new_case(people["reporter"], "First")
    second = new_case(people["reporter"], "Second")
    assert (
        people["triager"].delete(f"/api/v1/cases/{first}/links/{second}").status_code
        == 404
    )
    assert audit_rows(audit.CASE_LINK_REMOVE, first) == []
