"""If-Match on the changes to a deadline task, a legal duty and a user (F2, D-16; plans/f0/api-draft.md 3.5), against the
database.

Each of these records has its own revision that starts at 1. A change names the revision it read in If-Match. A missing
header is refused with 428, a malformed one with 400, a stale one with 412 (the current revision in the body and as the
ETag). A success returns the new ETag and raises the revision by one. A change that changes nothing keeps it.

Evidence availability has no endpoint in this code base, so it has no test here. A change to the case itself (its
title) keeps its own revision and is covered by test_case_title_db.py. These tests also check that a change to a child
row does not raise the case revision.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test module and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime, timedelta

from safe_kit import audit
from safe_kit.db import connect
from test_case_access import (
    create_case_via_api,
    db_client,
    people,
    requires_database,
    timezone_set,
)

from if_match import duty_if_match, task_if_match, user_if_match


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def case_url(case_id: str) -> str:
    return f"/api/v1/cases/{case_id}"


def task_url(case_id: str, task_id: str) -> str:
    return f"/api/v1/cases/{case_id}/deadlines/{task_id}"


def duty_url(case_id: str, duty_id: str) -> str:
    return f"/api/v1/cases/{case_id}/legal-duties/{duty_id}"


def user_url(user_id: str) -> str:
    return f"/api/v1/users/{user_id}"


def audit_count(action: str, target_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (action, target_id),
        ).fetchone()[0]


def task_of(client, case_id: str, kind: str) -> dict:
    body = client.get(case_url(case_id)).json()
    return next(task for task in body["deadlines"] if task["kind"] == kind)


def new_duty(triager, case_id: str) -> dict:
    body = {
        "kind": "statutory",
        "description": "Notify the regulator",
        "source_reference": "Art. 33",
        "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
    }
    response = triager.post(
        f"/api/v1/cases/{case_id}/legal-duties", json=body, headers=idem()
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- deadline tasks ------------------------------------------------------------------------------------------------


@requires_database
def test_deadline_change_without_if_match_is_refused_with_428(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    task = task_of(triager, case_id, "affected_customers")

    response = triager.patch(
        task_url(case_id, task["id"]), json={"status": "done"}, headers=idem()
    )

    assert response.status_code == 428
    assert response.json()["detail"] == {"status": "precondition_required"}
    after = task_of(triager, case_id, "affected_customers")
    assert (after["status"], after["revision"]) == ("open", 1)


@requires_database
def test_deadline_change_with_a_malformed_if_match_is_refused_with_400(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    task = task_of(triager, case_id, "affected_customers")
    for value in ("1", '"one"', '"1.0"', '"-1"', "*"):
        response = triager.patch(
            task_url(case_id, task["id"]),
            json={"status": "done"},
            headers={**idem(), "If-Match": value},
        )
        assert response.status_code == 400, value
        assert response.json()["detail"] == {"status": "invalid_if_match"}, value
    assert task_of(triager, case_id, "affected_customers")["revision"] == 1


@requires_database
def test_deadline_change_with_a_stale_revision_is_refused_and_names_the_current_one(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    task = task_of(triager, case_id, "affected_customers")
    url = task_url(case_id, task["id"])
    first = triager.patch(
        url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert first.status_code == 200, first.text

    second = triager.patch(
        url, json={"status": "open"}, headers={**idem(), "If-Match": '"1"'}
    )

    assert second.status_code == 412
    assert second.json()["detail"] == {
        "status": "precondition_failed",
        "current_revision": 2,
    }
    assert second.headers["ETag"] == '"2"'
    after = task_of(triager, case_id, "affected_customers")
    assert (after["status"], after["revision"]) == ("done", 2)


@requires_database
def test_deadline_change_with_the_current_revision_raises_it_once_and_returns_the_etag(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    task = task_of(triager, case_id, "affected_customers")
    url = task_url(case_id, task["id"])

    response = triager.patch(
        url,
        json={"status": "done"},
        headers={**idem(), **task_if_match(triager, url)},
    )

    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"2"'
    changed = next(t for t in response.json()["deadlines"] if t["id"] == task["id"])
    assert changed["revision"] == 2
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1
    # The case revision is not raised by a change to its deadline task (see the note in the module docstring).
    assert triager.get(case_url(case_id)).headers["ETag"] == '"1"'


@requires_database
def test_deadline_change_that_changes_nothing_keeps_the_revision(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    task = task_of(triager, case_id, "affected_customers")
    url = task_url(case_id, task["id"])
    assert (
        triager.patch(
            url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
        ).status_code
        == 200
    )
    audited = audit_count(audit.CASE_DEADLINE_UPDATE, case_id)

    again = triager.patch(
        url, json={"status": "done"}, headers={**idem(), "If-Match": '"2"'}
    )

    assert again.status_code == 200, again.text
    assert again.headers["ETag"] == '"2"'
    assert task_of(triager, case_id, "affected_customers")["revision"] == 2
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == audited


# --- legal duties --------------------------------------------------------------------------------------------------


@requires_database
def test_legal_duty_change_without_if_match_is_refused_with_428(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    duty = new_duty(triager, case_id)
    assert duty["revision"] == 1

    response = triager.patch(
        duty_url(case_id, duty["id"]), json={"status": "done"}, headers=idem()
    )

    assert response.status_code == 428
    assert response.json()["detail"] == {"status": "precondition_required"}
    listed = triager.get(f"/api/v1/cases/{case_id}/legal-duties").json()
    assert [(d["status"], d["revision"]) for d in listed] == [("open", 1)]


@requires_database
def test_legal_duty_change_with_a_malformed_if_match_is_refused_with_400(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    duty = new_duty(triager, case_id)
    for value in ("1", '"x"', '"2 "'):
        response = triager.patch(
            duty_url(case_id, duty["id"]),
            json={"status": "done"},
            headers={**idem(), "If-Match": value},
        )
        assert response.status_code == 400, value
        assert response.json()["detail"] == {"status": "invalid_if_match"}, value


@requires_database
def test_legal_duty_change_with_a_stale_revision_is_refused_and_names_the_current_one(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    duty = new_duty(triager, case_id)
    url = duty_url(case_id, duty["id"])
    first = triager.patch(
        url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert first.status_code == 200, first.text

    second = triager.patch(
        url,
        json={"description": "Changed by the second writer"},
        headers={**idem(), "If-Match": '"1"'},
    )

    assert second.status_code == 412
    assert second.json()["detail"] == {
        "status": "precondition_failed",
        "current_revision": 2,
    }
    assert second.headers["ETag"] == '"2"'
    listed = triager.get(f"/api/v1/cases/{case_id}/legal-duties").json()
    assert listed[0]["description"] == "Notify the regulator"


@requires_database
def test_legal_duty_change_with_the_current_revision_raises_it_and_leaves_the_case_alone(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    duty = new_duty(triager, case_id)
    url = duty_url(case_id, duty["id"])

    response = triager.patch(
        url,
        json={"status": "done"},
        headers={**idem(), **duty_if_match(triager, url)},
    )

    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"2"'
    assert response.json()["revision"] == 2
    assert audit_count(audit.CASE_LEGAL_DUTY_CHANGE, case_id) == 1
    assert triager.get(case_url(case_id)).headers["ETag"] == '"1"'


@requires_database
def test_legal_duty_change_that_changes_nothing_keeps_the_revision(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    duty = new_duty(triager, case_id)
    url = duty_url(case_id, duty["id"])
    assert (
        triager.patch(
            url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
        ).status_code
        == 200
    )
    audited = audit_count(audit.CASE_LEGAL_DUTY_CHANGE, case_id)

    again = triager.patch(
        url, json={"status": "done"}, headers={**idem(), "If-Match": '"2"'}
    )

    assert again.status_code == 200, again.text
    assert again.headers["ETag"] == '"2"'
    assert again.json()["revision"] == 2
    assert audit_count(audit.CASE_LEGAL_DUTY_CHANGE, case_id) == audited


# --- users ---------------------------------------------------------------------------------------------------------


@requires_database
def test_user_change_without_if_match_is_refused_with_428(db_client, people):
    admin = db_client(people["admin"])
    target = people["investigator"]

    response = admin.patch(user_url(target), json={"role": "reviewer"}, headers=idem())

    assert response.status_code == 428
    assert response.json()["detail"] == {"status": "precondition_required"}
    assert user_if_match(admin, target) == {"If-Match": '"1"'}


@requires_database
def test_user_change_with_a_malformed_if_match_is_refused_with_400(db_client, people):
    admin = db_client(people["admin"])
    target = people["investigator"]
    for value in ("1", '"one"', '""'):
        response = admin.patch(
            user_url(target),
            json={"role": "reviewer"},
            headers={**idem(), "If-Match": value},
        )
        assert response.status_code == 400, value
        assert response.json()["detail"] == {"status": "invalid_if_match"}, value


@requires_database
def test_user_change_with_a_stale_revision_is_refused_and_names_the_current_one(
    db_client, people
):
    admin = db_client(people["admin"])
    target = people["investigator"]
    url = user_url(target)
    first = admin.patch(
        url, json={"role": "reviewer"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert first.status_code == 200, first.text

    second = admin.patch(
        url, json={"role": "auditor"}, headers={**idem(), "If-Match": '"1"'}
    )

    assert second.status_code == 412
    assert second.json()["detail"] == {
        "status": "precondition_failed",
        "current_revision": 2,
    }
    assert second.headers["ETag"] == '"2"'
    # Only the current revision is returned, not the account.
    assert "role" not in second.json()
    assert user_if_match(admin, target) == {"If-Match": '"2"'}


@requires_database
def test_user_change_with_the_current_revision_raises_it_and_returns_the_etag(
    db_client, people
):
    admin = db_client(people["admin"])
    target = people["investigator"]
    url = user_url(target)

    response = admin.patch(
        url,
        json={"role": "reviewer"},
        headers={**idem(), **user_if_match(admin, target)},
    )

    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"2"'
    assert response.json()["role"] == "reviewer"
    assert response.json()["revision"] == 2
    assert audit_count(audit.USER_UPDATE, target) == 1


@requires_database
def test_user_change_that_changes_nothing_keeps_the_revision(db_client, people):
    admin = db_client(people["admin"])
    target = people["investigator"]
    url = user_url(target)
    assert (
        admin.patch(
            url, json={"role": "reviewer"}, headers={**idem(), "If-Match": '"1"'}
        ).status_code
        == 200
    )
    audited = audit_count(audit.USER_UPDATE, target)

    again = admin.patch(
        url, json={"role": "reviewer"}, headers={**idem(), "If-Match": '"2"'}
    )

    assert again.status_code == 200, again.text
    assert again.headers["ETag"] == '"2"'
    assert audit_count(audit.USER_UPDATE, target) == audited
    assert user_if_match(admin, target) == {"If-Match": '"2"'}
