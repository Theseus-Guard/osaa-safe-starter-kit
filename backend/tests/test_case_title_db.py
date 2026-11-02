"""Case title change with If-Match (F2, D-16; plans/f0/api-draft.md 3.5), against the database.

A change names the revision it read in If-Match. A missing header is refused with 428, a malformed one with 400, a stale
one with 412. The revision rises only when the title changes. These need PostgreSQL: they run with SAFE_TEST_DATABASE=1
and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test module and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid

from safe_kit.db import connect
from test_case_access import create_case_via_api, db_client, people, requires_database, timezone_set


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def case_url(case_id: str) -> str:
    return f"/api/v1/cases/{case_id}"


@requires_database
def test_a_title_change_with_the_current_revision_is_saved_and_raises_the_revision(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    assert triager.get(case_url(case_id)).headers["ETag"] == '"1"'

    response = triager.patch(case_url(case_id), json={"title": "Renamed case"}, headers={**idem(), "If-Match": '"1"'})

    assert response.status_code == 200
    assert response.headers["ETag"] == '"2"'
    assert response.json()["title"] == "Renamed case"
    assert response.json()["revision"] == 2
    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'case.title_updated' AND target_id = %s", (case_id,)
        ).fetchone()[0]
    assert count == 1


@requires_database
def test_a_stale_revision_is_refused_and_the_second_writer_changes_nothing(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    first = triager.patch(case_url(case_id), json={"title": "First writer"}, headers={**idem(), "If-Match": '"1"'})
    assert first.status_code == 200

    second = triager.patch(case_url(case_id), json={"title": "Second writer"}, headers={**idem(), "If-Match": '"1"'})

    assert second.status_code == 412
    assert second.json()["detail"] == {"status": "precondition_failed"}
    assert second.json()["error"]["code"] == "precondition_failed"
    assert second.json()["error"]["retryable"] is False
    assert second.headers["ETag"] == '"2"'
    assert triager.get(case_url(case_id)).json()["title"] == "First writer"


@requires_database
def test_a_change_without_if_match_is_refused_with_428(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    response = db_client(people["triager"]).patch(case_url(case_id), json={"title": "No header"}, headers=idem())
    assert response.status_code == 428
    assert response.json()["detail"] == {"status": "precondition_required"}


@requires_database
def test_an_if_match_that_is_not_a_quoted_whole_number_is_refused_with_400(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    for value in ("1", 'W/"1"', '"one"', "*", '"-1"'):
        response = triager.patch(case_url(case_id), json={"title": "Bad header"}, headers={**idem(), "If-Match": value})
        assert response.status_code == 400, value
        assert response.json()["detail"] == {"status": "invalid_if_match"}, value


@requires_database
def test_the_same_title_writes_nothing_and_keeps_the_revision(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    current = triager.get(case_url(case_id)).json()["title"]

    response = triager.patch(case_url(case_id), json={"title": f"  {current}  "}, headers={**idem(), "If-Match": '"1"'})

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.headers["ETag"] == '"1"'


@requires_database
def test_a_reporter_may_not_change_the_title(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    response = db_client(people["reporter"]).patch(
        case_url(case_id), json={"title": "Reporter rename"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert response.status_code == 403
    assert db_client(people["triager"]).get(case_url(case_id)).json()["revision"] == 1


@requires_database
def test_a_closed_case_refuses_a_title_change(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'Test closure' "
            "WHERE id = %s",
            (people["triager"], case_id),
        )
    response = db_client(people["triager"]).patch(
        case_url(case_id), json={"title": "Too late"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "case is closed"


@requires_database
def test_a_blank_title_is_refused_with_400(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    response = db_client(people["triager"]).patch(
        case_url(case_id), json={"title": "   "}, headers={**idem(), "If-Match": '"1"'}
    )
    assert response.status_code == 400
