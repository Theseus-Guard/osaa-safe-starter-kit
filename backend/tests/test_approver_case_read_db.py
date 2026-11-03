"""F2 approver case read (database): the disclosure approver reads the case and its disclosure versions, and is refused
what cases:read_all does not cover for this role: the deadline changes, the appeal, the reporter contact, the audit
summary and the users. The refusals use the same 403 "forbidden" the other permission checks use.

The approver here holds the seeded role (no monkeypatched permissions), so these tests check migration 0044 end to end.
Other roles keep their behaviour: the triager still changes deadlines.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid

from safe_kit.db import connect
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (
    case_url,
    first_version,
)  # world: fixture via first_version
from test_fixture_t06a_redaction import make_derived, world  # world: fixture


def idem() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


@requires_database
def test_the_approver_reads_the_case_detail(world):
    approver = db_user("disclosure_approver")
    response = request_as(approver, "get", f"/api/v1/cases/{world['case_id']}")
    assert response.status_code == 200
    assert response.json()["id"] == world["case_id"]


@requires_database
def test_the_approver_reads_the_disclosure_versions(world):
    view, _ = first_version(world)
    approver = db_user("disclosure_approver")
    assert request_as(approver, "get", case_url(world)).status_code == 200
    assert (
        request_as(approver, "get", case_url(world, f"/{view['id']}")).status_code
        == 200
    )
    assert (
        request_as(
            approver, "get", case_url(world, f"/{view['id']}/preview")
        ).status_code
        == 200
    )


@requires_database
def test_the_approver_may_not_change_a_deadline_and_a_triager_still_may(world):
    triager = world["people"]["triager"]
    approver = db_user("disclosure_approver")
    detail = request_as(triager, "get", f"/api/v1/cases/{world['case_id']}").json()
    task = next(item for item in detail["deadlines"] if item["id"] is not None)
    url = f"/api/v1/cases/{world['case_id']}/deadlines/{task['id']}"
    for response in (
        request_as(approver, "patch", url, json={"status": "done"}, headers=idem()),
        request_as(approver, "post", f"{url}/restore", headers=idem()),
    ):
        assert response.status_code == 403
        assert response.json()["detail"] == "forbidden"
    # The revision the case response above carried for this task.
    assert (
        request_as(
            triager,
            "patch",
            url,
            json={"status": "done"},
            headers={**idem(), "If-Match": f'"{task["revision"]}"'},
        ).status_code
        == 200
    )


@requires_database
def test_the_approver_may_not_file_an_appeal(world):
    approver = db_user("disclosure_approver")
    response = request_as(
        approver,
        "post",
        f"/api/v1/reviews/{uuid.uuid4()}/appeals",
        json={"grounds": "Wrong outcome"},
        headers=idem(),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


@requires_database
def test_the_approver_gets_the_same_refusal_as_a_triager_for_the_reporter_contact(
    world,
):
    approver = db_user("disclosure_approver")
    triager = world["people"]["triager"]
    url = f"/api/v1/reports/{world['case_id']}/contact"
    approver_response = request_as(approver, "get", url)
    triager_response = request_as(triager, "get", url)
    assert approver_response.status_code == triager_response.status_code == 403
    assert (
        approver_response.json()["detail"]
        == triager_response.json()["detail"]
        == "forbidden"
    )


@requires_database
def test_the_approver_is_refused_the_audit_summary_and_the_user_list(world):
    approver = db_user("disclosure_approver")
    for path in ("/api/v1/audit/summary", "/api/v1/users"):
        response = request_as(approver, "get", path)
        assert response.status_code == 403, path
        assert response.json()["detail"] == "forbidden", path


@requires_database
def test_the_migration_grants_the_approver_only_the_case_read_row(world):
    # The world fixture migrates the throwaway database. The approver's stored permissions are the seed of 0007, 0040
    # and 0044; no other role's grant is read here.
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rp.permission
            FROM role_permissions rp
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE rp.role = 'disclosure_approver' AND NOT rp.is_deleted
            ORDER BY rp.permission
            """
        ).fetchall()
    assert [row[0] for row in rows] == [
        "cases:read_all",
        "disclosures:approve",
        "disclosures:read",
    ]
