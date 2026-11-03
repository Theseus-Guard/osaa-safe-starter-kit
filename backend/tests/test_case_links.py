"""Case links without a database: the pair rules, the request shape and the HTTP mapping. The service functions are replaced.

The database tests (test_case_links_db.py) cover the uniqueness constraint, the soft delete, the audit rows, the
duplicate closure, the visibility rules and the whole flow against PostgreSQL.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit import case_links_api
from safe_kit.case_links import (
    CaseLink,
    LinkOutcome,
    check_pair,
    request_error,
)
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


def body(**overrides) -> dict:
    """A well-formed link request. Override fields to make a refusal."""
    values = {
        "linked_case_id": OTHER_ID,
        "link_type": "related",
        "reason": "Same incident",
        "share_visibility": False,
    }
    values.update(overrides)
    return values


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


# --- request shape (type, reason, main case) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link_type", "primary", "reason"),
    [
        ("related", None, "Same incident"),
        ("duplicate", CASE_ID, "Same incident"),
        ("duplicate", OTHER_ID, "Same incident"),
        ("root_sub", OTHER_ID, "Analysis of the root"),
    ],
)
def test_a_well_formed_request_has_no_error(link_type, primary, reason):
    assert request_error(CASE_ID, OTHER_ID, link_type, reason, primary) is None


@pytest.mark.parametrize(
    ("link_type", "primary", "reason", "message"),
    [
        ("partner", None, "Same incident", "link_type must be"),
        ("", None, "Same incident", "link_type must be"),
        ("related", None, "", "a reason is required"),
        ("related", None, "   ", "a reason is required"),
        ("related", None, "x" * 501, "a reason is required"),
        ("related", CASE_ID, "Same incident", "a related link has no main case"),
        ("duplicate", None, "Same incident", "needs primary_case_id"),
        ("root_sub", None, "Same incident", "needs primary_case_id"),
        (
            "root_sub",
            "77777777-7777-4777-8777-777777777777",
            "Same incident",
            "primary_case_id must be one of the two cases",
        ),
    ],
)
def test_a_malformed_request_is_refused_before_the_database(
    link_type, primary, reason, message
):
    error = request_error(CASE_ID, OTHER_ID, link_type, reason, primary)
    assert error is not None
    assert message in error


def test_a_reason_of_500_characters_is_accepted():
    assert request_error(CASE_ID, OTHER_ID, "related", "x" * 500, None) is None


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
        "link_type": "related",
        "reason": "Same incident",
        "share_visibility": False,
        "primary_side": None,
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
            json=body(),
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
        case_links_api, "add_link", lambda *args, **kwargs: (outcome, None, [])
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json=body(),
        headers=idem(),
    )
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_a_blocked_duplicate_is_409_and_names_the_blockers(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    blockers = ["deadline task affected_customers is open", "no review is recorded"]
    monkeypatch.setattr(
        case_links_api,
        "add_link",
        lambda *args, **kwargs: (LinkOutcome.BLOCKED, None, blockers),
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json=body(link_type="duplicate", primary_case_id=CASE_ID),
        headers=idem(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "duplicate case cannot be closed: "
        "deadline task affected_customers is open; no review is recorded"
    )


def test_add_success_is_201_with_the_full_link(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        case_links_api,
        "add_link",
        lambda *args, **kwargs: (LinkOutcome.OK, link(), []),
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json=body(),
        headers=idem(),
    )
    assert response.status_code == 201
    assert response.json() == {
        "linked_case_id": OTHER_ID,
        "linked_case_title": "Related case",
        "link_type": "related",
        "reason": "Same incident",
        "share_visibility": False,
        "primary_side": None,
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
    }


def test_add_passes_the_type_the_reason_and_the_checkbox_to_the_service(
    client, caller, monkeypatch
):
    as_role(caller, TRIAGER_ID, "triager")
    seen = {}

    def fake_add(case_id, linked_case_id, actor, scope, **kwargs):
        seen.update(
            case_id=case_id,
            linked_case_id=linked_case_id,
            actor=actor,
            scope=scope,
            **kwargs,
        )
        return LinkOutcome.OK, link(), []

    monkeypatch.setattr(case_links_api, "add_link", fake_add)
    client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json=body(
            link_type="root_sub",
            primary_case_id=OTHER_ID,
            share_visibility=True,
            reason="  Root of the analysis  ",
        ),
        headers=idem(),
    )
    assert seen["case_id"] == CASE_ID
    assert seen["linked_case_id"] == OTHER_ID
    assert seen["actor"] == TRIAGER_ID
    assert seen["scope"].user_id == TRIAGER_ID
    assert seen["link_type"] == "root_sub"
    assert seen["primary_case_id"] == OTHER_ID
    assert seen["share_visibility"] is True
    assert seen["reason"] == "  Root of the analysis  "


def test_the_checkbox_defaults_to_off(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    seen = {}

    def fake_add(*args, **kwargs):
        seen.update(kwargs)
        return LinkOutcome.OK, link(), []

    monkeypatch.setattr(case_links_api, "add_link", fake_add)
    payload = body()
    del payload["share_visibility"]
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links", json=payload, headers=idem()
        ).status_code
        == 201
    )
    assert seen["share_visibility"] is False


def test_linked_case_id_must_be_a_uuid(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links",
            json=body(linked_case_id="not-a-uuid"),
            headers=idem(),
        ).status_code
        == 400
    )


def test_link_type_must_be_one_of_the_three_types(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links",
            json=body(link_type="partner"),
            headers=idem(),
        ).status_code
        == 400
    )


def test_a_reason_is_required(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    payload = body()
    del payload["reason"]
    assert (
        client.post(
            f"/api/v1/cases/{CASE_ID}/links", json=payload, headers=idem()
        ).status_code
        == 400
    )


def test_a_malformed_request_answers_422_without_reaching_the_service(
    client, caller, monkeypatch
):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        case_links_api,
        "add_link",
        lambda *args, **kwargs: pytest.fail("the service must not be reached"),
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/links",
        json=body(link_type="duplicate"),
        headers=idem(),
    )
    assert response.status_code == 422
    assert "needs primary_case_id" in response.json()["detail"]


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
    hidden = link(
        linked_case_id=None,
        linked_case_title=None,
        primary_side="linked",
        link_type="root_sub",
    )
    monkeypatch.setattr(case_links_api, "list_links", lambda case_id, scope: [hidden])
    response = client.get(f"/api/v1/cases/{CASE_ID}/links")
    assert response.status_code == 200
    body_ = response.json()
    assert body_ == [
        {
            "linked_case_id": None,
            "linked_case_title": None,
            "link_type": "root_sub",
            "reason": "Same incident",
            "share_visibility": False,
            "primary_side": "linked",
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    ]
    # Nothing else about the hidden case or about the people who made the link is returned.
    assert set(body_[0]) == {
        "linked_case_id",
        "linked_case_title",
        "link_type",
        "reason",
        "share_visibility",
        "primary_side",
        "created_at",
    }
