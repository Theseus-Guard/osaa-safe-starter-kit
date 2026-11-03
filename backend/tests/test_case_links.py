"""Case links without a database: the pair rules and the HTTP mapping. The service functions are replaced.

The database tests (test_case_links_db.py) cover the uniqueness constraint, the soft delete, the audit rows and the
whole flow against PostgreSQL.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit import case_links_api
from safe_kit.case_links import CaseLink, LinkOutcome, check_pair
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
CASE_ID = "99999999-9999-9999-9999-999999999999"
OTHER_ID = "88888888-8888-4888-8888-888888888888"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def make_user(user_id, role):
    return User(
        id=user_id,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


# --- pair rules -----------------------------------------------------------------------------------------------------


def test_an_open_visible_pair_may_be_changed():
    assert check_pair(CASE_ID, OTHER_ID, {CASE_ID: "open", OTHER_ID: "open"}) is None


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        # The source case is not visible: not found, whatever the other side is.
        ({OTHER_ID: "open"}, LinkOutcome.NOT_FOUND),
        # The linked case is not visible (or does not exist): not found, so its existence is not revealed.
        ({CASE_ID: "open"}, LinkOutcome.NOT_FOUND),
        ({}, LinkOutcome.NOT_FOUND),
        # The source case is closed.
        ({CASE_ID: "closed", OTHER_ID: "open"}, LinkOutcome.CLOSED),
        # The linked case is closed.
        ({CASE_ID: "open", OTHER_ID: "closed"}, LinkOutcome.CLOSED),
    ],
)
def test_pair_refusals_for_visibility_and_closed_cases(statuses, expected):
    assert check_pair(CASE_ID, OTHER_ID, statuses) is expected


def test_a_case_cannot_be_linked_to_itself():
    assert check_pair(CASE_ID, CASE_ID, {CASE_ID: "open"}) is LinkOutcome.SAME_CASE


def test_same_case_is_checked_after_the_source_visibility():
    # An invisible case is not found even when the caller names it twice.
    assert check_pair(CASE_ID, CASE_ID, {}) is LinkOutcome.NOT_FOUND


def test_source_not_found_takes_precedence_over_a_closed_linked_case():
    assert check_pair(CASE_ID, OTHER_ID, {OTHER_ID: "closed"}) is LinkOutcome.NOT_FOUND


# --- HTTP mapping ---------------------------------------------------------------------------------------------------


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(TRIAGER_ID, "triager")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_role(caller, user_id, role):
    caller["user"] = make_user(user_id, role)


def link(**overrides) -> CaseLink:
    values = {
        "case_id": CASE_ID,
        "linked_case_id": OTHER_ID,
        "linked_case_title": "Related case",
        "created_at": NOW,
    }
    values.update(overrides)
    return CaseLink(**values)


@pytest.mark.parametrize("role", ["reporter", "admin", "auditor"])
def test_writes_need_cases_triage(client, caller, monkeypatch, role):
    as_role(caller, REPORTER_ID, role)
    monkeypatch.setattr(
        case_links_api,
        "add_link",
        lambda *args, **kwargs: pytest.fail("the service must not be reached"),
    )
    monkeypatch.setattr(
        case_links_api,
        "remove_link",
        lambda *args, **kwargs: pytest.fail("the service must not be reached"),
    )
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links",
            json={"linked_case_id": OTHER_ID},
            headers=idem(),
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/cases/{CASE_ID}/links/{OTHER_ID}").status_code == 403


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (LinkOutcome.NOT_FOUND, 404, "case not found"),
        (LinkOutcome.SAME_CASE, 422, "a case cannot be linked to itself"),
        (LinkOutcome.CLOSED, 409, "case is closed"),
        (LinkOutcome.DUPLICATE, 409, "cases are already linked"),
    ],
)
def test_add_refusals(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        case_links_api, "add_link", lambda *args, **kwargs: (outcome, None)
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json={"linked_case_id": OTHER_ID},
        headers=idem(),
    )
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_add_success_is_201_with_the_linked_case_id_and_title(
    client, caller, monkeypatch
):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        case_links_api, "add_link", lambda *args, **kwargs: (LinkOutcome.OK, link())
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json={"linked_case_id": OTHER_ID},
        headers=idem(),
    )
    assert response.status_code == 201
    assert response.json() == {
        "linked_case_id": OTHER_ID,
        "linked_case_title": "Related case",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
    }


def test_add_passes_the_actor_and_the_scope_to_the_service(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    seen = {}

    def fake_add(case_id, linked_case_id, actor, scope, **kwargs):
        seen.update(
            case_id=case_id, linked_case_id=linked_case_id, actor=actor, scope=scope
        )
        return LinkOutcome.OK, link()

    monkeypatch.setattr(case_links_api, "add_link", fake_add)
    client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json={"linked_case_id": OTHER_ID},
        headers=idem(),
    )
    assert seen["case_id"] == CASE_ID
    assert seen["linked_case_id"] == OTHER_ID
    assert seen["actor"] == TRIAGER_ID
    assert seen["scope"].user_id == TRIAGER_ID


def test_linked_case_id_must_be_a_uuid(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links",
            json={"linked_case_id": "not-a-uuid"},
            headers=idem(),
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (LinkOutcome.NOT_FOUND, 404, "case not found"),
        (LinkOutcome.CLOSED, 409, "case is closed"),
        (LinkOutcome.LINK_NOT_FOUND, 404, "link not found"),
    ],
)
def test_remove_refusals(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(case_links_api, "remove_link", lambda *args, **kwargs: outcome)
    response = client.delete(f"/api/v1/cases/{CASE_ID}/links/{OTHER_ID}")
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_remove_success_is_204(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        case_links_api, "remove_link", lambda *args, **kwargs: LinkOutcome.OK
    )
    assert client.delete(f"/api/v1/cases/{CASE_ID}/links/{OTHER_ID}").status_code == 204


@pytest.mark.parametrize("role", ["reporter", "investigator", "reviewer"])
def test_reads_need_a_case_read_permission(client, caller, monkeypatch, role):
    as_role(caller, REPORTER_ID, role)
    monkeypatch.setattr(case_links_api, "list_links", lambda case_id, scope: [])
    assert client.get(f"/api/v1/cases/{CASE_ID}/links").status_code == 200


@pytest.mark.parametrize("role", ["admin", "auditor"])
def test_roles_without_case_permissions_cannot_read_links(
    client, caller, monkeypatch, role
):
    as_role(caller, REPORTER_ID, role)
    monkeypatch.setattr(
        case_links_api,
        "list_links",
        lambda *args, **kwargs: pytest.fail("the service must not be reached"),
    )
    assert client.get(f"/api/v1/cases/{CASE_ID}/links").status_code == 403


def test_an_invisible_case_is_404_on_read(client, caller, monkeypatch):
    as_role(caller, REPORTER_ID, "reporter")
    monkeypatch.setattr(case_links_api, "list_links", lambda case_id, scope: None)
    response = client.get(f"/api/v1/cases/{CASE_ID}/links")
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


def test_a_link_to_a_case_the_caller_cannot_see_carries_no_id_and_no_title(
    client, caller, monkeypatch
):
    as_role(caller, REPORTER_ID, "reporter")
    hidden = link(linked_case_id=None, linked_case_title=None)
    monkeypatch.setattr(case_links_api, "list_links", lambda case_id, scope: [hidden])
    response = client.get(f"/api/v1/cases/{CASE_ID}/links")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "linked_case_id": None,
            "linked_case_title": None,
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    ]
    # Nothing else about the hidden case or about the people who made the link is returned.
    assert set(body[0]) == {"linked_case_id", "linked_case_title", "created_at"}
