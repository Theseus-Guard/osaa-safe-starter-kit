"""Conflicts, recusal and independent appeals on review rounds, against PostgreSQL (plans/03 T09, and the reviewer part of T12-A).

Each test creates its own case and users and never removes rows. They need SAFE_TEST_DATABASE=1 and the SAFE_DB_*
settings with the migrations applied; scripts/test-backend.sh --db starts a throwaway database for them.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth, system_timezone, users
from safe_kit.db import connect

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(autouse=True)
def system_timezone_configured():
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


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


@pytest.fixture
def db_client():
    yield ActingClient
    api.app.dependency_overrides.clear()


@pytest.fixture
def people():
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    return {
        "reporter": new_user("reporter"),
        "triager": new_user("triager"),
        # Assigned reviewers who hold reviews:write.
        "reviewer": new_user("reviewer"),
        "reviewer_b": new_user("reviewer"),
        "reviewer_c": new_user("reviewer"),
        # Holds reviews:write, but is never assigned to the case.
        "unassigned_reviewer": new_user("reviewer"),
        # Assigned, but without reviews:write.
        "investigator": new_user("investigator"),
        "other_investigator": new_user("investigator"),
        "admin": new_user("admin"),
        "auditor": new_user("auditor"),
    }


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def create_case_via_api(db_client, reporter: str) -> str:
    response = db_client(reporter).post(
        "/api/v1/cases",
        json={
            "title": "Review independence database case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def assign(db_client, people: dict, case_id: str, user: str) -> None:
    response = db_client(people["triager"]).post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": user})
    assert response.status_code == 201, response.text


def setup_round(db_client, people: dict) -> tuple[str, str]:
    """A case with reviewers A (opens the round), B, C and an investigator assigned; A opens round 1."""
    case_id = create_case_via_api(db_client, people["reporter"])
    for name in ("reviewer", "reviewer_b", "reviewer_c", "investigator"):
        assign(db_client, people, case_id, people[name])
    response = db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json={"case_id": case_id})
    assert response.status_code == 201, response.text
    return case_id, response.json()["id"]


def declare(db_client, people: dict, who: str, round_id: str, **body):
    return db_client(people[who]).post(f"/api/v1/reviews/{round_id}/conflicts", headers=idem(), json=body)


def recuse(db_client, people: dict, who: str, round_id: str, replacement: str, **extra):
    return db_client(people[who]).post(
        f"/api/v1/reviews/{round_id}/recusals", headers=idem(), json={"replacement_id": people[replacement], **extra}
    )


def appeal(db_client, people: dict, who: str, round_id: str, grounds: str = "The model layer was not tested"):
    # The filer names no reviewer: the system names the independent reviewer.
    return db_client(people[who]).post(
        f"/api/v1/reviews/{round_id}/appeals", headers=idem(), json={"grounds": grounds}
    )


def set_layer(db_client, people: dict, who: str, round_id: str, revision: int, outcome: str = "pass", reason=None):
    body = {"outcome": outcome, "expected_revision": revision}
    if reason is not None:
        body["reason"] = reason
    return db_client(people[who]).patch(f"/api/v1/reviews/{round_id}/layers/model", json=body)


def count(sql: str, params: tuple) -> int:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


# --- conflict blocks setting a layer and assignment (T09) --------------------------------------------------------------


@requires_database
def test_t09_a_declared_conflict_blocks_the_reviewer_from_setting_a_layer_and_nothing_else(db_client, people):
    _, round_id = setup_round(db_client, people)
    declared = declare(db_client, people, "reviewer", round_id, kind="declared", description="I built the model")
    assert declared.status_code == 201, declared.text
    assert declared.json()["current"] is True

    blocked = set_layer(db_client, people, "reviewer", round_id, revision=1, outcome="fail", reason="Missing logs")
    assert blocked.status_code == 403
    assert blocked.json() == {
        "detail": "a conflict is declared on this review round, so this reviewer cannot set a layer"
    }

    # Another assigned reviewer is not affected, and the refused write changed nothing.
    other = set_layer(db_client, people, "reviewer_b", round_id, revision=1)
    assert other.status_code == 200, other.text
    assert other.json()["revision"] == 2


@requires_database
def test_t09_a_correction_appends_and_a_withdrawal_lifts_the_block(db_client, people):
    _, round_id = setup_round(db_client, people)
    first = declare(db_client, people, "reviewer", round_id, kind="declared", description="First wording").json()

    again = declare(db_client, people, "reviewer", round_id, kind="declared", description="Second wording")
    assert again.status_code == 409
    assert again.json() == {"detail": {"status": "conflict_already_declared"}}

    corrected = declare(
        db_client, people, "reviewer", round_id, kind="declared", description="Second wording",
        supersedes_id=first["id"],
    )
    assert corrected.status_code == 201, corrected.text
    second = corrected.json()
    assert second["supersedes_id"] == first["id"]

    # The history keeps both entries, newest first. The first one is unchanged.
    history = db_client(people["triager"]).get(f"/api/v1/reviews/{round_id}/conflicts").json()
    assert [item["id"] for item in history] == [second["id"], first["id"]]
    assert [item["current"] for item in history] == [True, False]
    assert history[1]["description"] == "First wording"

    stale = declare(db_client, people, "reviewer", round_id, kind="withdrawn", supersedes_id=first["id"])
    assert stale.status_code == 409
    assert stale.json() == {"detail": {"status": "supersedes_mismatch"}}

    withdrawn = declare(db_client, people, "reviewer", round_id, kind="withdrawn", supersedes_id=second["id"])
    assert withdrawn.status_code == 201, withdrawn.text
    assert set_layer(db_client, people, "reviewer", round_id, revision=1).status_code == 200

    nothing_left = declare(db_client, people, "reviewer", round_id, kind="withdrawn", supersedes_id=withdrawn.json()["id"])
    assert nothing_left.status_code == 409
    assert nothing_left.json() == {"detail": {"status": "no_declared_conflict"}}


@requires_database
def test_t09_the_database_refuses_an_edit_of_a_declaration_and_a_soft_delete_is_one_way(db_client, people):
    _, round_id = setup_round(db_client, people)
    entry = declare(db_client, people, "reviewer", round_id, kind="declared", description="Original").json()
    assert recuse(db_client, people, "reviewer", round_id, "reviewer_b", reason="Original").status_code == 201
    assert appeal(db_client, people, "reporter", round_id, grounds="Original").status_code == 201

    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute("UPDATE review_conflicts SET description = 'Changed' WHERE id = %s", (entry["id"],))
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute("UPDATE review_recusals SET reason = 'Changed' WHERE round_id = %s", (round_id,))
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute("UPDATE review_appeals SET grounds = 'Changed' WHERE round_id = %s", (round_id,))

    with connect() as conn:
        conn.execute(
            "UPDATE review_conflicts SET is_deleted = true, deleted_at = now() WHERE id = %s", (entry["id"],)
        )
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute("UPDATE review_conflicts SET is_deleted = false, deleted_at = NULL WHERE id = %s", (entry["id"],))
    assert db_client(people["triager"]).get(f"/api/v1/reviews/{round_id}/conflicts").json() == []


@requires_database
def test_t09_a_conflicted_reviewer_cannot_be_named_as_the_replacement(db_client, people):
    _, round_id = setup_round(db_client, people)
    assert declare(db_client, people, "reviewer_b", round_id, kind="declared", description="Same team").status_code == 201

    refused = recuse(db_client, people, "reviewer", round_id, "reviewer_b", reason="Leaving")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "the replacement has a declared conflict on this review round"
    assert count("SELECT count(*) FROM review_recusals WHERE round_id = %s", (round_id,)) == 0


# --- recusal and replacement (T09, reviewer part of T12-A) -------------------------------------------------------------


@requires_database
def test_t09_recusal_needs_an_eligible_replacement_and_the_replacement_takes_over(db_client, people):
    _, round_id = setup_round(db_client, people)

    not_a_reviewer = recuse(db_client, people, "reviewer", round_id, "investigator")
    assert not_a_reviewer.status_code == 422
    not_assigned = recuse(db_client, people, "reviewer", round_id, "unassigned_reviewer")
    assert not_assigned.status_code == 422
    assert "active, assigned user" in not_assigned.json()["detail"]
    itself = recuse(db_client, people, "reviewer", round_id, "reviewer")
    assert itself.status_code == 422
    assert itself.json()["detail"] == "a reviewer cannot name themselves as the replacement"

    recorded = recuse(db_client, people, "reviewer", round_id, "reviewer_b", reason="Worked on the system")
    assert recorded.status_code == 201, recorded.text
    body = recorded.json()
    assert body["replacement_id"] == people["reviewer_b"]
    assert body["current"] is True

    # The withdrawn reviewer cannot set a layer; the named replacement can (T12-A reviewer part).
    withdrawn = set_layer(db_client, people, "reviewer", round_id, revision=1, outcome="fail", reason="Late")
    assert withdrawn.status_code == 403
    assert withdrawn.json()["detail"] == "this reviewer has withdrawn from this review round, so cannot set a layer"
    replacement = set_layer(db_client, people, "reviewer_b", round_id, revision=1)
    assert replacement.status_code == 200, replacement.text


@requires_database
def test_t09_a_second_recusal_needs_a_correction_and_a_correction_keeps_the_old_row(db_client, people):
    _, round_id = setup_round(db_client, people)
    first = recuse(db_client, people, "reviewer", round_id, "reviewer_b").json()

    again = recuse(db_client, people, "reviewer", round_id, "reviewer_c")
    assert again.status_code == 409
    assert again.json() == {"detail": {"status": "already_recused"}}
    wrong = recuse(db_client, people, "reviewer", round_id, "reviewer_c", supersedes_id=str(uuid.uuid4()))
    assert wrong.status_code == 409
    assert wrong.json() == {"detail": {"status": "supersedes_mismatch"}}

    corrected = recuse(db_client, people, "reviewer", round_id, "reviewer_c", supersedes_id=first["id"])
    assert corrected.status_code == 201, corrected.text

    history = db_client(people["triager"]).get(f"/api/v1/reviews/{round_id}/recusals").json()
    assert [item["replacement_id"] for item in history] == [people["reviewer_c"], people["reviewer_b"]]
    assert [item["current"] for item in history] == [True, False]


@requires_database
def test_t09_a_withdrawn_reviewer_cannot_become_a_replacement(db_client, people):
    _, round_id = setup_round(db_client, people)
    assert recuse(db_client, people, "reviewer", round_id, "reviewer_b").status_code == 201

    # B now withdraws, and names the reviewer who already withdrew: that is refused.
    refused = recuse(db_client, people, "reviewer_b", round_id, "reviewer")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "the replacement has withdrawn from this review round"


@requires_database
def test_t09_a_recusal_by_a_user_who_is_not_assigned_is_not_found(db_client, people):
    _, round_id = setup_round(db_client, people)
    response = recuse(db_client, people, "unassigned_reviewer", round_id, "reviewer_b")
    assert response.status_code == 404
    assert response.json() == {"detail": "review not found"}


# --- appeal to an independent reviewer (T09) ---------------------------------------------------------------------------


@requires_database
def test_t09_the_system_names_an_independent_reviewer_who_took_no_part_and_has_no_conflict(db_client, people):
    case_id, round_id = setup_round(db_client, people)
    assert set_layer(db_client, people, "reviewer", round_id, revision=1, outcome="fail", reason="No tests").status_code == 200
    assert declare(db_client, people, "reviewer_c", round_id, kind="declared", description="Shared team").status_code == 201

    # The original reviewer opened the round and set a layer, reviewer_c declared a conflict, the investigator has no
    # reviews:write, and the unassigned reviewer is not on the case. The only eligible reviewer is B.
    filed = appeal(db_client, people, "reporter", round_id)
    assert filed.status_code == 201, filed.text
    body = filed.json()
    assert body["independent_reviewer_id"] == people["reviewer_b"]
    assert body["filed_by"] == people["reporter"]

    history = db_client(people["reviewer"]).get(f"/api/v1/reviews/{round_id}/appeals")
    assert history.status_code == 200
    (item,) = history.json()
    assert item["grounds"] is None  # the reviewer is neither the appellant nor a full-view reader
    assert item["independent_reviewer_id"] == people["reviewer_b"]
    assert count("SELECT count(*) FROM audit_events WHERE target_id = %s AND action = %s",
                 (case_id, audit.CASE_REVIEW_APPEAL_RECORDED)) == 1


@requires_database
def test_t09_the_system_picks_the_eligible_reviewer_with_the_fewest_open_appeals_then_the_lowest_id(db_client, people):
    _, round_id = setup_round(db_client, people)
    # The opener is not eligible, so B and C are the candidates. Both have no appeal yet: the lowest ID is chosen.
    first = appeal(db_client, people, "reporter", round_id)
    assert first.status_code == 201, first.text
    lowest, other = sorted([people["reviewer_b"], people["reviewer_c"]])
    assert first.json()["independent_reviewer_id"] == lowest

    # The reviewer with one open appeal now has more than the other one, so the next appeal goes to the other one.
    second = appeal(db_client, people, "reporter", round_id)
    assert second.status_code == 201, second.text
    assert second.json()["independent_reviewer_id"] == other

    # Both have one open appeal again: the lowest ID is chosen.
    third = appeal(db_client, people, "reporter", round_id)
    assert third.status_code == 201, third.text
    assert third.json()["independent_reviewer_id"] == lowest


@requires_database
def test_t09_no_eligible_reviewer_refuses_the_appeal_and_stores_nothing(db_client, people):
    _, round_id = setup_round(db_client, people)
    # B and C both declare a conflict on the round. A opened it, so nobody is eligible.
    assert declare(db_client, people, "reviewer_b", round_id, kind="declared", description="Own team").status_code == 201
    assert declare(db_client, people, "reviewer_c", round_id, kind="declared", description="Own team").status_code == 201

    refused = appeal(db_client, people, "reporter", round_id)
    assert refused.status_code == 409
    assert refused.json() == {"detail": {"status": "no_eligible_reviewer"}}
    assert count("SELECT count(*) FROM review_appeals WHERE round_id = %s", (round_id,)) == 0


@requires_database
def test_t09_a_user_who_recused_or_was_named_as_replacement_is_not_chosen(db_client, people):
    _, round_id = setup_round(db_client, people)
    assert recuse(db_client, people, "reviewer", round_id, "reviewer_b").status_code == 201

    # B was named as the replacement and A withdrew, so both took part. C is the only one left.
    filed = appeal(db_client, people, "reporter", round_id)
    assert filed.status_code == 201, filed.text
    assert filed.json()["independent_reviewer_id"] == people["reviewer_c"]


@requires_database
def test_t09_nothing_blocks_an_appeal_and_the_case_stays_as_it_was(db_client, people):
    case_id, round_id = setup_round(db_client, people)
    # The original reviewer has withdrawn and a conflict is declared by the replacement. Neither blocks an appeal.
    assert recuse(db_client, people, "reviewer", round_id, "reviewer_b").status_code == 201
    assert declare(db_client, people, "reviewer_b", round_id, kind="declared", description="Own team").status_code == 201

    # The investigator, who is assigned but not a reviewer, files the appeal. The system names C.
    filed = appeal(db_client, people, "investigator", round_id, grounds="Decision needs a second look")
    assert filed.status_code == 201, filed.text
    assert filed.json()["grounds"] == "Decision needs a second look"
    assert filed.json()["independent_reviewer_id"] == people["reviewer_c"]

    # The case itself is unchanged: an appeal writes no layer, no revision and no status.
    rounds = db_client(people["triager"]).get("/api/v1/reviews", params={"case_id": case_id}).json()
    assert rounds[0]["revision"] == 1
    assert rounds[0]["status"] == "open"


# --- visibility of the free text -----------------------------------------------------------------------------------------


@requires_database
def test_t09_free_text_is_shown_to_the_writer_and_the_full_view_only(db_client, people):
    _, round_id = setup_round(db_client, people)
    # A declares a conflict, then withdraws and names B. The appeal is filed by the reporter and names C.
    assert declare(db_client, people, "reviewer", round_id, kind="declared", description="Shared team with the author").status_code == 201
    assert recuse(db_client, people, "reviewer", round_id, "reviewer_b", reason="Too close to the author").status_code == 201
    assert appeal(db_client, people, "reporter", round_id, grounds="The data was not checked").status_code == 201

    conflict_views = {
        who: db_client(people[who]).get(f"/api/v1/reviews/{round_id}/conflicts").json()[0]["description"]
        for who in ("triager", "reviewer", "reviewer_c", "investigator")
    }
    assert conflict_views == {
        "triager": "Shared team with the author",  # cases:read_all
        "reviewer": "Shared team with the author",  # the reviewer concerned
        "reviewer_c": None,
        "investigator": None,
    }

    assert db_client(people["triager"]).get(f"/api/v1/reviews/{round_id}/recusals").json()[0]["reason"] == (
        "Too close to the author"
    )
    assert db_client(people["reviewer"]).get(f"/api/v1/reviews/{round_id}/recusals").json()[0]["reason"] == (
        "Too close to the author"
    )
    assert db_client(people["reviewer_b"]).get(f"/api/v1/reviews/{round_id}/recusals").json()[0]["reason"] is None

    assert db_client(people["reporter"]).get(f"/api/v1/reviews/{round_id}/appeals").json()[0]["grounds"] == (
        "The data was not checked"
    )
    assert db_client(people["reviewer_b"]).get(f"/api/v1/reviews/{round_id}/appeals").json()[0]["grounds"] is None


# --- permissions and visibility of the routes ----------------------------------------------------------------------------


@requires_database
def test_t09_permissions_and_visibility_of_the_routes(db_client, people):
    _, round_id = setup_round(db_client, people)

    assert declare(db_client, people, "admin", round_id, kind="declared", description="x").status_code == 403
    assert declare(db_client, people, "investigator", round_id, kind="declared", description="x").status_code == 403
    assert recuse(db_client, people, "investigator", round_id, "reviewer_b").status_code == 403
    assert db_client(people["admin"]).get(f"/api/v1/reviews/{round_id}/conflicts").status_code == 403
    assert db_client(people["admin"]).get(f"/api/v1/reviews/{round_id}/appeals").status_code == 403

    # An assigned investigator may read; a user outside the case gets the same 404 as for a missing round.
    assert db_client(people["investigator"]).get(f"/api/v1/reviews/{round_id}/conflicts").status_code == 200
    hidden = db_client(people["other_investigator"]).get(f"/api/v1/reviews/{round_id}/conflicts")
    absent = db_client(people["triager"]).get(f"/api/v1/reviews/{uuid.uuid4()}/conflicts")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json() == {"detail": "review not found"}
    assert declare(db_client, people, "unassigned_reviewer", round_id, kind="declared", description="x").status_code == 404


# --- Idempotency-Key on the three writes ---------------------------------------------------------------------------------


@requires_database
def test_the_same_key_and_body_replays_the_first_conflict_and_writes_nothing_again(db_client, people):
    case_id, round_id = setup_round(db_client, people)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"kind": "declared", "description": "Replay me"}
    url = f"/api/v1/reviews/{round_id}/conflicts"
    first = db_client(people["reviewer"]).post(url, headers=key, json=body)
    again = db_client(people["reviewer"]).post(url, headers=key, json=body)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert count("SELECT count(*) FROM review_conflicts WHERE round_id = %s", (round_id,)) == 1
    assert count(
        "SELECT count(*) FROM audit_events WHERE target_id = %s AND action = %s AND NOT is_deleted",
        (case_id, audit.CASE_REVIEW_CONFLICT_RECORDED),
    ) == 1


@requires_database
def test_the_same_key_with_another_request_is_refused_and_creates_nothing(db_client, people):
    _, round_id = setup_round(db_client, people)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    url = f"/api/v1/reviews/{round_id}/conflicts"
    assert db_client(people["reviewer"]).post(url, headers=key, json={"kind": "declared", "description": "one"}).status_code == 201
    other = db_client(people["reviewer"]).post(url, headers=key, json={"kind": "declared", "description": "two"})
    assert other.status_code == 409
    assert other.json() == {"detail": {"status": "idempotency_key_reused"}}
    assert count("SELECT count(*) FROM review_conflicts WHERE round_id = %s", (round_id,)) == 1


@requires_database
def test_a_refused_request_stores_no_key_so_a_retry_with_the_same_key_succeeds(db_client, people):
    _, round_id = setup_round(db_client, people)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    url = f"/api/v1/reviews/{round_id}/conflicts"
    refused = db_client(people["reviewer"]).post(url, headers=key, json={"kind": "withdrawn"})
    assert refused.status_code == 409
    retried = db_client(people["reviewer"]).post(url, headers=key, json={"kind": "declared", "description": "Now"})
    assert retried.status_code == 201, retried.text


@requires_database
def test_recusal_and_appeal_replays_write_one_row_each(db_client, people):
    _, round_id = setup_round(db_client, people)
    recusal_key = {"Idempotency-Key": str(uuid.uuid4())}
    recusal_body = {"replacement_id": people["reviewer_b"], "reason": "Once"}
    url = f"/api/v1/reviews/{round_id}/recusals"
    first = db_client(people["reviewer"]).post(url, headers=recusal_key, json=recusal_body)
    again = db_client(people["reviewer"]).post(url, headers=recusal_key, json=recusal_body)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert count("SELECT count(*) FROM review_recusals WHERE round_id = %s", (round_id,)) == 1

    appeal_key = {"Idempotency-Key": str(uuid.uuid4())}
    appeal_body = {"grounds": "Once only"}
    appeal_url = f"/api/v1/reviews/{round_id}/appeals"
    first_appeal = db_client(people["reporter"]).post(appeal_url, headers=appeal_key, json=appeal_body)
    again_appeal = db_client(people["reporter"]).post(appeal_url, headers=appeal_key, json=appeal_body)
    assert first_appeal.status_code == again_appeal.status_code == 201
    assert again_appeal.json() == first_appeal.json()
    assert count("SELECT count(*) FROM review_appeals WHERE round_id = %s", (round_id,)) == 1


@requires_database
def test_keys_are_scoped_to_the_principal(db_client, people):
    _, round_id = setup_round(db_client, people)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    url = f"/api/v1/reviews/{round_id}/conflicts"
    assert db_client(people["reviewer"]).post(url, headers=key, json={"kind": "declared", "description": "A"}).status_code == 201
    # The same key from another principal is a different scope, so it is a new request, not a replay of A's answer.
    other = db_client(people["reviewer_b"]).post(url, headers=key, json={"kind": "declared", "description": "B"})
    assert other.status_code == 201
    assert other.json()["reviewer_id"] == people["reviewer_b"]
