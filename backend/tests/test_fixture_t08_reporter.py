"""F1 fixture T08 (reporter channel): the F1 subset of the reporter protection.

The reporter is the person who may be at risk for reporting. F1 protects that person's identity and contact from
everyone who does not hold the contact permission, administrators included.

Asserted without a database:
- the administrator role cannot hold a case or contact permission, through the service or through the API;
- an administrator gets 403 on a reporter contact and on case reads;
- no case response carries the reporter's account or the contact.

Asserted with a database:
- only a holder of reporter_contact:read reads a contact, and each read writes one audit row; a non-holder and an
  administrator are refused and write nothing;
- a reporter-channel case is hidden from triagers until it has an assignee, and the assignment needs the holder.

- No role other than reporting_owner can be granted reporter_contact:read through the role matrix, so an administrator
  cannot hand the contact to a triager account. The refusal is 409 with detail status reporter_contact_not_grantable.
  reporting_owner holds the grant by a fixed seed, and the matrix refuses every change to that role (see
  test_reporting_owner_db.py).

Not in this file: the manual emergency escalation runbook and the abuse rate limit are not application code in F1. The
reporter account's username is visible to an administrator, who creates reporter accounts. The case link and the
contact are not. Whether the username should be hidden from administrators is an open decision.

The database tests use the helpers of tests/test_case_records_db.py and tests/test_reporter_channel.py and are skipped
unless SAFE_TEST_DATABASE=1.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, system_timezone
from safe_kit.api import CaseResponse, CaseSummary
from safe_kit.case_records_api import AwarenessEventResponse
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from safe_kit.rbac import ADMIN_REQUIRED_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, RoleOutcome, list_role_permissions, set_role_permissions
from safe_kit.reporter_channel import REPORTER_CONTACT_READ
from test_case_access import ADMIN_ID, CASE_ID, REPORTER_ID, TRIAGER_ID, as_role, caller, client
from test_case_records_db import ActingClient, new_user, reporter_contact_holder, assign_investigator
from test_evidence_lifecycle import requires_database

REPORTER_IDENTITY_FIELDS = {"reporter", "reporter_id", "reporter_user_id", "created_by", "contact", "reporter_contact"}


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def refuse(*args, **kwargs):
    raise AssertionError("the service must not run for this caller")


# --- without a database ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    ["reporter_contact:read", "cases:read_all", "evidence:read", "reviews:write", "disclosures:approve"],
)
def test_the_admin_role_cannot_hold_a_case_or_contact_permission(extra):
    outcome = set_role_permissions("admin", frozenset(ADMIN_REQUIRED_PERMISSIONS | {extra}))
    assert outcome is RoleOutcome.ADMIN_CASE_CONTENT


@pytest.mark.parametrize("role", ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor"])
def test_no_non_admin_role_can_be_granted_the_contact_permission(role):
    outcome = set_role_permissions(role, frozenset(DEFAULT_ROLE_PERMISSIONS[role] | {"reporter_contact:read"}))
    assert outcome is RoleOutcome.REPORTER_CONTACT_NOT_GRANTABLE


def test_the_triager_role_api_refuses_the_contact_permission_with_a_status_detail(client, caller, monkeypatch):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    as_role(caller, ADMIN_ID, "admin")
    body = {"permissions": sorted(DEFAULT_ROLE_PERMISSIONS["triager"] | {"reporter_contact:read"})}

    response = client.put("/api/v1/rbac/roles/triager/permissions", json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "reporter_contact_not_grantable"}


def test_the_admin_role_api_refuses_the_contact_permission_with_409(client, caller, monkeypatch):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    as_role(caller, ADMIN_ID, "admin")
    body = {"permissions": sorted(ADMIN_REQUIRED_PERMISSIONS | {"reporter_contact:read"})}

    response = client.put("/api/v1/rbac/roles/admin/permissions", json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == "the admin role cannot hold case or reporter contact permissions"


def test_an_administrator_gets_403_on_the_contact_and_on_case_reads(client, caller, monkeypatch):
    monkeypatch.setattr(api, "read_reporter_contact", refuse)
    monkeypatch.setattr(api, "find_visible_case", refuse)
    monkeypatch.setattr(api, "list_visible_cases", refuse)
    as_role(caller, ADMIN_ID, "admin")

    assert client.get(f"/api/v1/reports/{CASE_ID}/contact").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}").status_code == 403
    assert client.get("/api/v1/cases").status_code == 403


def test_no_case_response_carries_the_reporter_account_or_the_contact():
    for model in (CaseResponse, CaseSummary, AwarenessEventResponse):
        assert not REPORTER_IDENTITY_FIELDS & set(model.model_fields), model.__name__


# --- with a database -------------------------------------------------------------------------------------------------


@pytest.fixture
def channel_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {
        "reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
        "other_triager": ActingClient(new_user("triager")),
        "admin": ActingClient(new_user("admin")),
        "investigator_id": new_user("investigator"),
    }
    yield people
    api.app.dependency_overrides.clear()


def open_report(reporter: ActingClient, contact: str) -> str:
    response = reporter.post(
        "/api/v1/reports",
        json={
            "title": "Reporter channel fixture",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "contact": contact,
        }, headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def contact_audit_rows(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (REPORTER_CONTACT_READ, case_id),
        ).fetchone()[0]


@requires_database
def test_only_a_holder_reads_the_contact_and_every_read_is_audited(channel_people):
    contact = "fixture-contact-" + uuid.uuid4().hex[:8]
    case_id = open_report(channel_people["reporter"], contact)
    triager, other, admin = channel_people["triager"], channel_people["other_triager"], channel_people["admin"]
    assert contact_audit_rows(case_id) == 0

    with reporter_contact_holder():
        read = triager.get(f"/api/v1/reports/{case_id}/contact")
    assert read.status_code == 200, read.text
    assert read.json()["contact"] == contact
    assert contact_audit_rows(case_id) == 1

    assert other.get(f"/api/v1/reports/{case_id}/contact").status_code == 403
    assert admin.get(f"/api/v1/reports/{case_id}/contact").status_code == 403
    assert contact_audit_rows(case_id) == 1


@requires_database
def test_a_reporter_channel_case_is_hidden_from_triagers_until_assigned(channel_people):
    case_id = open_report(channel_people["reporter"], "fixture-contact-hidden")
    triager, admin = channel_people["triager"], channel_people["admin"]

    assert case_id not in {item["id"] for item in triager.get("/api/v1/cases", params={"limit": 200}).json()["items"]}
    assert triager.get(f"/api/v1/cases/{case_id}").status_code == 404
    assert admin.get(f"/api/v1/cases/{case_id}").status_code == 403

    assign_investigator(triager, case_id, channel_people["investigator_id"])

    assert triager.get(f"/api/v1/cases/{case_id}").status_code == 200


@requires_database
def test_only_reporting_owner_holds_the_contact_permission_after_the_migrations():
    migrate()
    matrix = list_role_permissions()
    holders = {role for role, permissions in matrix.items() if "reporter_contact:read" in permissions}
    assert holders == {"reporting_owner"}


@requires_database
def test_the_admin_cannot_grant_the_contact_permission_to_another_role(channel_people):
    admin = channel_people["admin"]
    before = frozenset(list_role_permissions()["triager"])
    try:
        response = admin.put(
            "/api/v1/rbac/roles/triager/permissions",
            json={"permissions": sorted(before | {"reporter_contact:read"})},
        )
        assert response.status_code == 409
    finally:
        set_role_permissions("triager", before)


@requires_database
def test_the_handler_assigned_to_the_case_cannot_read_the_reporter_contact(channel_people):
    """T08-A: the primary handler sees the case, but not the reporter's contact or identity, and the refused read is not
    audited as a read. The handler is the one who is party to the event: nothing in the case response names the reporter."""
    contact = "fixture-contact-" + uuid.uuid4().hex[:8]
    case_id = open_report(channel_people["reporter"], contact)
    handler = ActingClient(channel_people["investigator_id"])
    assign_investigator(channel_people["triager"], case_id, channel_people["investigator_id"])
    assert handler.get(f"/api/v1/cases/{case_id}").status_code == 200

    audited_before = contact_audit_rows(case_id)
    refused = handler.get(f"/api/v1/reports/{case_id}/contact")
    assert refused.status_code == 403
    assert contact not in refused.text
    assert contact_audit_rows(case_id) == audited_before
    assert not REPORTER_IDENTITY_FIELDS & set(handler.get(f"/api/v1/cases/{case_id}").json())
