"""Conflict, recusal and appeal endpoints on review rounds (see review_independence.py and review_independence_api.py).

Without a database the service functions are replaced and the HTTP mapping, the validation, the Idempotency-Key rules,
the permissions, and the text visibility are checked. The database rules (blocking, replacement and independence
checks, corrections, replay against stored keys) are in test_review_independence_db.py.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, review_independence_api, reviews_api
from safe_kit.cases import CaseScope
from safe_kit.idempotency import StoredResponse, fingerprint
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.review_independence import AppealEntry, ConflictEntry, IndependenceOutcome, RecusalEntry
from safe_kit.reviews import RoundOutcome
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
OTHER_REVIEWER_ID = "77777777-7777-4777-8777-777777777777"
ADMIN_ID = "55555555-5555-5555-5555-555555555555"
CASE_ID = "99999999-9999-9999-9999-999999999999"
ROUND_ID = "12121212-1212-1212-1212-121212121212"
ENTRY_ID = "abababab-abab-4bab-8bab-abababababab"
PRIOR_ID = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
DESCRIPTION = "I worked on the system under review"


def idem() -> dict:
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
        created_at=NOW,
    )


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(REPORTER_ID, "reporter")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_role(caller, user_id, role):
    caller["user"] = make_user(user_id, role)


def conflict_entry(*, reviewer_id=REVIEWER_ID, description=DESCRIPTION, kind="declared", current=True) -> ConflictEntry:
    return ConflictEntry(
        id=ENTRY_ID,
        round_id=ROUND_ID,
        reviewer_id=reviewer_id,
        kind=kind,
        description=description,
        supersedes_id=None,
        created_at=NOW,
        current=current,
    )


def recusal_entry(*, reviewer_id=REVIEWER_ID, reason="Close to the author", current=True) -> RecusalEntry:
    return RecusalEntry(
        id=ENTRY_ID,
        round_id=ROUND_ID,
        reviewer_id=reviewer_id,
        replacement_id=OTHER_REVIEWER_ID,
        reason=reason,
        supersedes_id=None,
        created_at=NOW,
        current=current,
    )


def appeal_entry(*, filed_by=REPORTER_ID, grounds="The model layer was not tested") -> AppealEntry:
    return AppealEntry(
        id=ENTRY_ID,
        round_id=ROUND_ID,
        filed_by=filed_by,
        independent_reviewer_id=OTHER_REVIEWER_ID,
        grounds=grounds,
        created_at=NOW,
    )


CONFLICT_URL = f"/api/v1/reviews/{ROUND_ID}/conflicts"
RECUSAL_URL = f"/api/v1/reviews/{ROUND_ID}/recusals"
APPEAL_URL = f"/api/v1/reviews/{ROUND_ID}/appeals"


def forbid(name: str):
    def fail(*args, **kwargs):
        pytest.fail(f"must not reach the service: {name}")

    return fail


# --- permissions ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver", "reporter", "investigator"])
def test_declaring_a_conflict_needs_reviews_write(client, caller, monkeypatch, role):
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    monkeypatch.setattr(review_independence_api, "list_conflicts", forbid("list_conflicts"))
    as_role(caller, ADMIN_ID, role)
    body = {"kind": "declared", "description": DESCRIPTION}
    assert client.post(CONFLICT_URL, headers=idem(), json=body).status_code == 403


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver", "reporter", "investigator"])
def test_recording_a_recusal_needs_reviews_write(client, caller, monkeypatch, role):
    monkeypatch.setattr(review_independence_api, "record_recusal", forbid("record_recusal"))
    as_role(caller, ADMIN_ID, role)
    body = {"replacement_id": OTHER_REVIEWER_ID}
    assert client.post(RECUSAL_URL, headers=idem(), json=body).status_code == 403


@pytest.mark.parametrize("role", ["admin", "auditor"])
def test_filing_an_appeal_needs_a_case_read_permission(client, caller, monkeypatch, role):
    monkeypatch.setattr(review_independence_api, "file_appeal", forbid("file_appeal"))
    monkeypatch.setattr(review_independence_api, "list_appeals", forbid("list_appeals"))
    as_role(caller, ADMIN_ID, role)
    body = {"grounds": "Wrong outcome"}
    assert client.post(APPEAL_URL, headers=idem(), json=body).status_code == 403
    assert client.get(APPEAL_URL).status_code == 403


def test_the_disclosure_approver_reads_appeals_but_may_not_file_one(client, caller, monkeypatch):
    # cases:read_all lets the approver read the case and so its appeals; filing an appeal is refused to this role.
    monkeypatch.setattr(review_independence_api, "file_appeal", forbid("file_appeal"))
    monkeypatch.setattr(review_independence_api, "list_appeals", lambda *args, **kwargs: [])
    as_role(caller, ADMIN_ID, "disclosure_approver")
    body = {"grounds": "Wrong outcome"}
    response = client.post(APPEAL_URL, headers=idem(), json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"
    assert client.get(APPEAL_URL).status_code == 200


def test_a_reporter_may_file_an_appeal_but_not_declare_a_conflict(client, caller, monkeypatch):
    seen = {}

    def fake_file(round_id, grounds, scope, *, idempotency, render):
        seen["scope"] = scope
        return IndependenceOutcome.OK, appeal_entry(), None

    monkeypatch.setattr(review_independence_api, "file_appeal", fake_file)
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    as_role(caller, REPORTER_ID, "reporter")
    body = {"grounds": "Wrong outcome"}
    assert client.post(APPEAL_URL, headers=idem(), json=body).status_code == 201
    assert seen["scope"] == CaseScope(user_id=REPORTER_ID, read_own=True)
    assert client.post(CONFLICT_URL, headers=idem(), json={"kind": "declared", "description": "x"}).status_code == 403


def test_the_permission_refusal_comes_before_the_key_check(client, caller):
    as_role(caller, ADMIN_ID, "admin")
    assert client.post(CONFLICT_URL, json={"kind": "declared", "description": "x"}).status_code == 403
    assert client.post(RECUSAL_URL, json={"replacement_id": OTHER_REVIEWER_ID}).status_code == 403
    assert client.post(APPEAL_URL, json={"grounds": "x"}).status_code == 403


# --- Idempotency-Key ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [CONFLICT_URL, RECUSAL_URL, APPEAL_URL])
def test_each_side_effect_post_requires_an_idempotency_key(client, caller, monkeypatch, url):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    monkeypatch.setattr(review_independence_api, "record_recusal", forbid("record_recusal"))
    monkeypatch.setattr(review_independence_api, "file_appeal", forbid("file_appeal"))
    body = {"kind": "declared", "description": "x"} if url == CONFLICT_URL else (
        {"replacement_id": OTHER_REVIEWER_ID} if url == RECUSAL_URL else {"grounds": "x"}
    )
    missing = client.post(url, json=body)
    assert missing.status_code == 400
    assert missing.json()["detail"] == {"status": "idempotency_key_required"}


@pytest.mark.parametrize("key", ["has space", "x" * 129, ""])
def test_the_idempotency_key_must_be_1_to_128_visible_ascii(client, caller, monkeypatch, key):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    response = client.post(CONFLICT_URL, headers={"Idempotency-Key": key}, json={"kind": "withdrawn"})
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_invalid"}


def test_the_key_scope_uses_the_principal_route_template_and_key(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    seen = {}

    def fake_declare(round_id, kind, description, supersedes_id, scope, *, idempotency, render):
        seen["idempotency"] = idempotency
        return IndependenceOutcome.NOT_FOUND, None, None

    monkeypatch.setattr(review_independence_api, "declare_conflict", fake_declare)
    body = {"kind": "declared", "description": DESCRIPTION}
    client.post(CONFLICT_URL, headers={"Idempotency-Key": "k-1"}, json=body)
    scope = seen["idempotency"]
    assert (scope.principal_id, scope.method, scope.route, scope.key) == (
        REVIEWER_ID, "POST", "/api/v1/reviews/{review_id}/conflicts", "k-1"
    )
    # The handler fingerprints the parsed body, which always carries supersedes_id (null when not sent).
    assert scope.fingerprint == fingerprint(
        "POST", CONFLICT_URL, "", {"kind": "declared", "description": DESCRIPTION, "supersedes_id": None}
    )


def test_the_fingerprint_changes_with_the_body_and_the_round(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    prints = []

    def fake_record(round_id, replacement_id, reason, supersedes_id, scope, *, idempotency, render):
        prints.append(idempotency.fingerprint)
        return IndependenceOutcome.NOT_FOUND, None, None

    monkeypatch.setattr(review_independence_api, "record_recusal", fake_record)
    client.post(RECUSAL_URL, headers={"Idempotency-Key": "k"}, json={"replacement_id": OTHER_REVIEWER_ID})
    client.post(RECUSAL_URL, headers={"Idempotency-Key": "k"}, json={"replacement_id": str(uuid.uuid4())})
    client.post(
        f"/api/v1/reviews/{uuid.uuid4()}/recusals", headers={"Idempotency-Key": "k"},
        json={"replacement_id": OTHER_REVIEWER_ID},
    )
    assert len(set(prints)) == 3


def test_a_stored_answer_is_replayed_with_its_status_and_body(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    stored = {"id": ENTRY_ID, "kind": "declared"}
    monkeypatch.setattr(
        review_independence_api,
        "declare_conflict",
        lambda *args, **kwargs: (IndependenceOutcome.REPLAY, None, StoredResponse(status_code=201, body=stored)),
    )
    response = client.post(CONFLICT_URL, headers={"Idempotency-Key": "k-1"}, json={"kind": "declared", "description": "x"})
    assert response.status_code == 201
    assert response.json() == stored


@pytest.mark.parametrize(
    ("url", "service", "body"),
    [
        (CONFLICT_URL, "declare_conflict", {"kind": "declared", "description": "x"}),
        (RECUSAL_URL, "record_recusal", {"replacement_id": OTHER_REVIEWER_ID}),
        (APPEAL_URL, "file_appeal", {"grounds": "x"}),
    ],
)
def test_a_key_used_with_another_request_is_refused_without_its_content(client, caller, monkeypatch, url, service, body):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(
        review_independence_api, service, lambda *args, **kwargs: (IndependenceOutcome.KEY_REUSED, None, None)
    )
    response = client.post(url, headers={"Idempotency-Key": "k-1"}, json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "idempotency_key_reused"}


# --- validation --------------------------------------------------------------------------------------------------------


def test_a_declared_conflict_needs_a_description_and_a_withdrawal_does_not(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(
        review_independence_api,
        "declare_conflict",
        lambda *args, **kwargs: (IndependenceOutcome.NO_DECLARED_CONFLICT, None, None),
    )
    assert client.post(CONFLICT_URL, headers=idem(), json={"kind": "declared"}).status_code == 400
    assert client.post(CONFLICT_URL, headers=idem(), json={"kind": "declared", "description": "   "}).status_code == 400
    assert client.post(CONFLICT_URL, headers=idem(), json={"kind": "withdrawn"}).status_code == 409


def test_conflict_kind_must_be_declared_or_withdrawn(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    assert client.post(CONFLICT_URL, headers=idem(), json={"kind": "resolved"}).status_code == 400


def test_description_is_limited_to_1000_characters(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    body = {"kind": "declared", "description": "x" * 1001}
    assert client.post(CONFLICT_URL, headers=idem(), json=body).status_code == 400


def test_supersedes_must_be_an_identifier(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", forbid("declare_conflict"))
    body = {"kind": "declared", "description": "x", "supersedes_id": "not-a-uuid"}
    assert client.post(CONFLICT_URL, headers=idem(), json=body).status_code == 400


def test_the_replacement_is_required_and_must_be_an_identifier(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "record_recusal", forbid("record_recusal"))
    assert client.post(RECUSAL_URL, headers=idem(), json={"reason": "x"}).status_code == 400
    assert client.post(RECUSAL_URL, headers=idem(), json={"replacement_id": "someone"}).status_code == 400


def test_appeal_grounds_are_required_and_limited(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "file_appeal", forbid("file_appeal"))
    assert client.post(APPEAL_URL, headers=idem(), json={"grounds": "  "}).status_code == 400
    assert client.post(APPEAL_URL, headers=idem(), json={"grounds": "x" * 2001}).status_code == 400


@pytest.mark.parametrize("reviewer", [OTHER_REVIEWER_ID, "nobody", None])
def test_an_appeal_that_names_a_reviewer_is_refused(client, caller, monkeypatch, reviewer):
    # The filer cannot choose the independent reviewer: the field is refused, whatever its value.
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "file_appeal", forbid("file_appeal"))
    response = client.post(APPEAL_URL, headers=idem(), json={"grounds": "x", "independent_reviewer_id": reviewer})
    assert response.status_code == 400
    assert any(error["loc"][-1] == "independent_reviewer_id" for error in response.json()["detail"])


# --- refusal mapping ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (IndependenceOutcome.NOT_FOUND, 404, {"detail": "review not found"}),
        (IndependenceOutcome.CLOSED, 409, {"detail": "case is closed"}),
        (IndependenceOutcome.NOT_ASSIGNED, 403, {"detail": "only an assigned reviewer can declare a conflict"}),
        (IndependenceOutcome.ALREADY_DECLARED, 409, {"detail": {"status": "conflict_already_declared"}}),
        (IndependenceOutcome.NO_DECLARED_CONFLICT, 409, {"detail": {"status": "no_declared_conflict"}}),
        (IndependenceOutcome.SUPERSEDES_MISMATCH, 409, {"detail": {"status": "supersedes_mismatch"}}),
    ],
)
def test_conflict_refusals_map_to_their_status_codes(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "declare_conflict", lambda *args, **kwargs: (outcome, None, None))
    response = client.post(CONFLICT_URL, headers=idem(), json={"kind": "declared", "description": "x"})
    assert response.status_code == status
    assert response.json()["detail"] == detail["detail"]


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (IndependenceOutcome.NOT_ASSIGNED, 403, "only an assigned reviewer can withdraw from a review round"),
        (IndependenceOutcome.ALREADY_RECUSED, 409, {"status": "already_recused"}),
        (IndependenceOutcome.REPLACEMENT_IS_SELF, 422, "a reviewer cannot name themselves as the replacement"),
        (
            IndependenceOutcome.REPLACEMENT_NOT_ELIGIBLE,
            422,
            "the replacement must be an active, assigned user who holds reviews:write",
        ),
        (
            IndependenceOutcome.REPLACEMENT_CONFLICTED,
            422,
            "the replacement has a declared conflict on this review round",
        ),
        (IndependenceOutcome.REPLACEMENT_WITHDRAWN, 422, "the replacement has withdrawn from this review round"),
        (IndependenceOutcome.SUPERSEDES_MISMATCH, 409, {"status": "supersedes_mismatch"}),
    ],
)
def test_recusal_refusals_map_to_their_status_codes(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(review_independence_api, "record_recusal", lambda *args, **kwargs: (outcome, None, None))
    response = client.post(RECUSAL_URL, headers=idem(), json={"replacement_id": OTHER_REVIEWER_ID})
    assert response.status_code == status
    assert response.json()["detail"] == detail


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (IndependenceOutcome.NO_ELIGIBLE_REVIEWER, 409, {"status": "no_eligible_reviewer"}),
        (IndependenceOutcome.NOT_FOUND, 404, "review not found"),
    ],
)
def test_appeal_refusals_map_to_their_status_codes(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, REPORTER_ID, "reporter")
    monkeypatch.setattr(review_independence_api, "file_appeal", lambda *args, **kwargs: (outcome, None, None))
    response = client.post(APPEAL_URL, headers=idem(), json={"grounds": "x"})
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_success_returns_the_created_entry_with_its_own_text(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(
        review_independence_api,
        "declare_conflict",
        lambda *args, **kwargs: (IndependenceOutcome.OK, conflict_entry(), None),
    )
    response = client.post(CONFLICT_URL, headers=idem(), json={"kind": "declared", "description": DESCRIPTION})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "round_id", "reviewer_id", "kind", "description", "supersedes_id", "created_at", "current"}
    assert body["description"] == DESCRIPTION
    assert body["current"] is True


# --- text visibility ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "user_id", "expected"),
    [
        ("triager", ADMIN_ID, DESCRIPTION),  # cases:read_all, the full view
        ("reviewer", REVIEWER_ID, DESCRIPTION),  # the reviewer concerned
        ("reviewer", OTHER_REVIEWER_ID, None),
        ("investigator", OTHER_REVIEWER_ID, None),
    ],
)
def test_a_conflict_description_is_shown_to_the_full_view_and_the_reviewer_only(
    client, caller, monkeypatch, role, user_id, expected
):
    monkeypatch.setattr(
        review_independence_api,
        "list_conflicts",
        lambda round_id, scope: [conflict_entry(reviewer_id=REVIEWER_ID)],
    )
    as_role(caller, user_id, role)
    (item,) = client.get(CONFLICT_URL).json()
    assert item["description"] == expected


@pytest.mark.parametrize(
    ("role", "user_id", "expected"),
    [
        ("triager", ADMIN_ID, "Close to the author"),
        ("reviewer", REVIEWER_ID, "Close to the author"),
        ("reviewer", OTHER_REVIEWER_ID, None),
    ],
)
def test_a_recusal_reason_is_shown_to_the_full_view_and_the_reviewer_only(
    client, caller, monkeypatch, role, user_id, expected
):
    monkeypatch.setattr(review_independence_api, "list_recusals", lambda round_id, scope: [recusal_entry()])
    as_role(caller, user_id, role)
    (item,) = client.get(RECUSAL_URL).json()
    assert item["reason"] == expected
    assert item["replacement_id"] == OTHER_REVIEWER_ID


@pytest.mark.parametrize(
    ("role", "user_id", "expected"),
    [
        ("triager", ADMIN_ID, "The model layer was not tested"),
        ("reporter", REPORTER_ID, "The model layer was not tested"),  # the appellant
        ("reviewer", OTHER_REVIEWER_ID, None),
    ],
)
def test_appeal_grounds_are_shown_to_the_full_view_and_the_appellant_only(
    client, caller, monkeypatch, role, user_id, expected
):
    monkeypatch.setattr(review_independence_api, "list_appeals", lambda round_id, scope: [appeal_entry()])
    as_role(caller, user_id, role)
    (item,) = client.get(APPEAL_URL).json()
    assert item["grounds"] == expected
    assert item["independent_reviewer_id"] == OTHER_REVIEWER_ID


def test_the_lists_answer_404_for_a_round_the_caller_may_not_see(client, caller, monkeypatch):
    monkeypatch.setattr(review_independence_api, "list_conflicts", lambda round_id, scope: None)
    monkeypatch.setattr(review_independence_api, "list_recusals", lambda round_id, scope: None)
    monkeypatch.setattr(review_independence_api, "list_appeals", lambda round_id, scope: None)
    for url in (CONFLICT_URL, RECUSAL_URL, APPEAL_URL):
        response = client.get(url)
        assert response.status_code == 404
        assert response.json()["detail"] == "review not found"


def test_the_list_routes_pass_the_caller_scope_to_the_service(client, caller, monkeypatch):
    seen = {}

    def fake_list(round_id, scope):
        seen["args"] = (round_id, scope)
        return []

    monkeypatch.setattr(review_independence_api, "list_conflicts", fake_list)
    as_role(caller, REVIEWER_ID, "reviewer")
    assert client.get(CONFLICT_URL).json() == []
    assert seen["args"] == (ROUND_ID, CaseScope(user_id=REVIEWER_ID, read_assigned=True))


def test_the_list_routes_need_a_case_read_permission(client, caller, monkeypatch):
    monkeypatch.setattr(review_independence_api, "list_conflicts", forbid("list_conflicts"))
    as_role(caller, ADMIN_ID, "admin")
    assert client.get(CONFLICT_URL).status_code == 403


def test_the_round_path_must_be_a_uuid(client, caller):
    as_role(caller, REVIEWER_ID, "reviewer")
    assert client.get("/api/v1/reviews/not-a-uuid/conflicts").status_code == 400


# --- the layer refusals of reviews_api ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (RoundOutcome.CONFLICT_DECLARED, "a conflict is declared on this review round, so this reviewer cannot set a layer"),
        (RoundOutcome.WITHDRAWN, "this reviewer has withdrawn from this review round, so cannot set a layer"),
    ],
)
def test_a_declared_conflict_or_a_withdrawal_blocks_setting_a_layer(client, caller, monkeypatch, outcome, detail):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (outcome, None))
    response = client.patch(
        f"/api/v1/reviews/{ROUND_ID}/layers/model", json={"outcome": "pass"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == detail
