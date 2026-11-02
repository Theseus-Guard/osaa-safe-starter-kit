"""Model records against PostgreSQL: versions, replay, revocation, the insert-only trigger, idempotency and audit.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import json
import os
import uuid
from datetime import datetime

import psycopg
import pytest

from safe_kit import api, rbac, users
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from test_case_links_db import ActingClient, idem, new_case, new_user

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

CONFIG = {
    "model_name": "model-a",
    "model_version": "2026-10",
    "runtime": "runtime/1.4",
    "provider_alias": "provider-main",
    "reasoning": {"mode": "enabled", "effort": "high", "budget_tokens": 8000},
}


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
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


def record_body(**overrides) -> dict:
    body = {**CONFIG, "evidence_item_id": None, "review_round_id": None}
    body.update(overrides)
    return body


def close_case_in_database(case_id: str, actor_id: str) -> None:
    # The closed state is set directly here, as in test_case_links_db. The application itself closes through the API.
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, "
            "close_reason = 'test' WHERE id = %s",
            (actor_id, case_id),
        )


def audit_rows(action: str, case_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT actor_id::text, target_type, target_id, details FROM audit_events "
            "WHERE action = %s AND target_id = %s AND NOT is_deleted ORDER BY created_at",
            (action, case_id),
        ).fetchall()
    return [{"actor_id": r[0], "target_type": r[1], "target_id": r[2], "details": r[3]} for r in rows]


def record_count(case_id: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM model_records WHERE case_id = %s", (case_id,)).fetchone()[0]


def add_review_round(case_id: str, opened_by: str) -> str:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO review_rounds (case_id, opened_by) VALUES (%s, %s) RETURNING id::text", (case_id, opened_by)
        ).fetchone()[0]


@pytest.fixture
def people():
    triager_id = new_user("triager")
    investigator_id = new_user("investigator")
    outsider_id = new_user("investigator")
    reporter = ActingClient(new_user("reporter"))
    case_id = new_case(reporter)
    triager = ActingClient(triager_id)
    assigned = triager.post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": investigator_id}, headers=idem()
    )
    assert assigned.status_code == 201, assigned.text
    return {
        "case_id": case_id,
        "triager": triager,
        "investigator": ActingClient(investigator_id),
        "outsider": ActingClient(outsider_id),
        "reporter": reporter,
        "investigator_id": investigator_id,
        "triager_id": triager_id,
    }


def record(client: ActingClient, case_id: str, **overrides):
    return client.post(f"/api/v1/cases/{case_id}/model-records", json=record_body(**overrides), headers=idem())


def test_model_records_has_the_standard_columns_and_the_insert_only_trigger():
    with connect() as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'model_records'"
            ).fetchall()
        }
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'model_records'::regclass AND NOT tgisinternal"
            ).fetchall()
        }
    assert {"created_at", "updated_at", "deleted_at", "is_deleted"} <= columns
    assert "model_records_append_only" in triggers


def test_a_record_is_written_once_and_two_reads_give_identical_content(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id)
    assert created.status_code == 201, created.text
    record_id = created.json()["record_id"]

    first = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{record_id}")
    second = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{record_id}")

    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["configuration"] == CONFIG
    assert first.json()["version"] == 1
    assert first.json()["usable_as_current"] is True


def test_the_read_configuration_can_be_replayed_as_a_new_record(people):
    case_id = people["case_id"]
    original = record(people["investigator"], case_id).json()
    read = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{original['record_id']}").json()

    replayed = people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records",
        json=record_body(**read["configuration"]),
        headers=idem(),
    )

    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["record_id"] != original["record_id"]
    assert replayed.json()["configuration"] == original["configuration"]
    # The original is unchanged by the replay.
    again = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{original['record_id']}").json()
    assert again == read


def test_the_insert_only_trigger_refuses_a_change_to_stored_content(people):
    created = record(people["investigator"], people["case_id"]).json()
    with pytest.raises(psycopg.Error, match="append-only"):
        with connect() as conn:
            conn.execute(
                "UPDATE model_records SET model_name = 'changed' WHERE record_id = %s", (created["record_id"],)
            )


def test_a_revocation_is_a_new_version_with_the_reason_and_the_record_stays_readable(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id).json()
    record_id = created["record_id"]

    revoked = people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke",
        json={"reason": "  wrong provider alias  "},
        headers=idem(),
    )

    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["version"] == 2
    assert body["revoked"] is True
    assert body["usable_as_current"] is False
    assert body["configuration"] == CONFIG
    # The reason is free text: the triager (cases:read_all) sees it, the investigator does not.
    assert body["revocation_reason"] is None
    seen_by_triager = people["triager"].get(f"/api/v1/cases/{case_id}/model-records/{record_id}").json()
    assert seen_by_triager["revocation_reason"] == "wrong provider alias"
    assert seen_by_triager["usable_as_current"] is False

    with connect() as conn:
        rows = conn.execute(
            "SELECT version, revoked FROM model_records WHERE record_id = %s ORDER BY version", (record_id,)
        ).fetchall()
    assert rows == [(1, False), (2, True)]


def test_the_current_listing_leaves_out_revoked_records_and_the_full_listing_keeps_them(people):
    case_id = people["case_id"]
    kept = record(people["investigator"], case_id).json()["record_id"]
    revoked = record(people["investigator"], case_id, model_name="model-b").json()["record_id"]
    people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{revoked}/revoke", json={"reason": "superseded"}, headers=idem()
    )

    everything = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records").json()
    current = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records?current_only=true").json()

    assert {r["record_id"] for r in everything} == {kept, revoked}
    assert [r["record_id"] for r in current] == [kept]


def test_a_second_revocation_is_refused_and_writes_nothing(people):
    case_id = people["case_id"]
    record_id = record(people["investigator"], case_id).json()["record_id"]
    url = f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke"
    assert people["investigator"].post(url, json={"reason": "first"}, headers=idem()).status_code == 200

    second = people["investigator"].post(url, json={"reason": "second"}, headers=idem())

    assert second.status_code == 409
    assert second.json() == {"detail": "model record is already revoked"}
    with connect() as conn:
        count = conn.execute("SELECT count(*) FROM model_records WHERE record_id = %s", (record_id,)).fetchone()[0]
    assert count == 2


def test_a_repeated_key_replays_the_stored_response_and_a_different_request_with_it_is_refused(people):
    case_id = people["case_id"]
    key = {"Idempotency-Key": str(uuid.uuid4())}
    url = f"/api/v1/cases/{case_id}/model-records"

    first = people["investigator"].post(url, json=record_body(), headers=key)
    replay = people["investigator"].post(url, json=record_body(), headers=key)
    reused = people["investigator"].post(url, json=record_body(model_name="model-b"), headers=key)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert reused.status_code == 409
    assert record_count(case_id) == 1


def test_a_write_without_a_key_is_refused_before_anything_is_stored(people):
    response = people["investigator"].post(f"/api/v1/cases/{people['case_id']}/model-records", json=record_body())
    assert response.status_code == 400
    assert record_count(people["case_id"]) == 0


def test_permissions_and_visibility_decide_who_reads_and_writes(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id).json()

    # The triager holds evidence:read only: it reads, and cannot record.
    assert people["triager"].get(f"/api/v1/cases/{case_id}/model-records").status_code == 200
    assert record(people["triager"], case_id).status_code == 403
    # The reporter holds no evidence permission.
    assert people["reporter"].get(f"/api/v1/cases/{case_id}/model-records").status_code == 403
    # An investigator who is not assigned cannot see the case: the answer is the same as for a missing case.
    outsider_read = people["outsider"].get(f"/api/v1/cases/{case_id}/model-records/{created['record_id']}")
    assert outsider_read.status_code == 404
    assert record(people["outsider"], case_id).status_code == 404


def test_an_unknown_case_and_a_closed_case_are_refused(people):
    unknown = str(uuid.uuid4())
    assert record(people["investigator"], unknown).status_code == 404

    created = record(people["investigator"], people["case_id"]).json()
    close_case_in_database(people["case_id"], people["triager_id"])

    assert record(people["investigator"], people["case_id"], model_name="model-b").status_code == 409
    revoke = people["investigator"].post(
        f"/api/v1/cases/{people['case_id']}/model-records/{created['record_id']}/revoke",
        json={"reason": "late"},
        headers=idem(),
    )
    assert revoke.status_code == 409
    assert record_count(people["case_id"]) == 1


def test_a_review_round_must_belong_to_the_same_case(people):
    case_id = people["case_id"]
    own_round = add_review_round(case_id, people["triager_id"])
    other_case = new_case(people["reporter"], title="Other case")
    other_round = add_review_round(other_case, people["triager_id"])

    linked = record(people["investigator"], case_id, review_round_id=own_round)
    assert linked.status_code == 201, linked.text
    assert linked.json()["review_round_id"] == own_round
    assert linked.json()["evidence_item_id"] is None

    foreign = record(people["investigator"], case_id, review_round_id=other_round)
    assert foreign.status_code == 404
    missing = record(people["investigator"], case_id, evidence_item_id=str(uuid.uuid4()))
    assert missing.status_code == 404
    assert missing.json() == {"detail": "evidence item or review round not found"}


def test_each_write_writes_one_audit_row_and_the_reason_is_not_logged(people):
    case_id = people["case_id"]
    record_id = record(people["investigator"], case_id).json()["record_id"]
    reason = "config mismatch with the runtime log"
    people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke", json={"reason": reason}, headers=idem()
    )

    added = audit_rows("case.model_record_added", case_id)
    revoked = audit_rows("case.model_record_revoked", case_id)

    assert len(added) == 1
    assert added[0]["actor_id"] == people["investigator_id"]
    assert added[0]["details"]["model_record_id"] == record_id
    assert len(revoked) == 1
    assert revoked[0]["details"]["reason"] == {"length": len(reason)}
    assert reason not in json.dumps(revoked[0]["details"])
    assert revoked[0]["details"]["version"] == {"old": 1, "new": 2}


def test_a_revocation_keeps_the_recorded_time_and_names_the_user_who_revoked(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id).json()
    revoked = people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{created['record_id']}/revoke",
        json={"reason": "x"},
        headers=idem(),
    )
    assert revoked.status_code == 200, revoked.text
    current = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{created['record_id']}").json()

    assert current["recorded_at"] == created["recorded_at"]
    assert current["written_by"] == people["investigator_id"]
    assert current["revoked_at"] is not None
    assert datetime.fromisoformat(current["revoked_at"]) >= datetime.fromisoformat(created["recorded_at"])


def record_rows(record_id: str) -> list[tuple]:
    """The stored versions of one record, read straight from the table, not through the API."""
    with connect() as conn:
        return conn.execute(
            "SELECT case_id::text, version, model_name, model_version, runtime, provider_alias, reasoning_mode, "
            "reasoning_effort, reasoning_budget_tokens, revoked, created_by::text, recorded_at IS NOT NULL "
            "FROM model_records WHERE record_id = %s ORDER BY version",
            (record_id,),
        ).fetchall()


def test_the_stored_row_holds_every_field_the_configuration_needs(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id).json()

    assert record_rows(created["record_id"]) == [
        (case_id, 1, "model-a", "2026-10", "runtime/1.4", "provider-main", "enabled", "high", 8000, False,
         people["investigator_id"], True),
    ]


def test_a_configuration_without_reasoning_is_replayed_without_effort_or_budget(people):
    case_id = people["case_id"]
    disabled = {"mode": "disabled", "effort": None, "budget_tokens": None}
    original = record(people["investigator"], case_id, reasoning=disabled).json()
    assert original["configuration"]["reasoning"] == disabled
    read = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{original['record_id']}").json()

    replayed = people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records", json=record_body(**read["configuration"]), headers=idem()
    )

    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["configuration"] == read["configuration"]
    assert replayed.json()["configuration"]["reasoning"] == disabled


def test_the_list_and_the_single_read_return_the_same_content(people):
    case_id = people["case_id"]
    created = record(people["investigator"], case_id).json()

    listed = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records").json()
    single = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{created['record_id']}").json()

    assert listed == [single]


def test_a_revoked_record_stays_readable_by_id_for_the_investigator_and_leaves_the_current_listing(people):
    case_id = people["case_id"]
    record_id = record(people["investigator"], case_id).json()["record_id"]
    people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke", json={"reason": "superseded"}, headers=idem()
    )

    after = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records/{record_id}")
    current = people["investigator"].get(f"/api/v1/cases/{case_id}/model-records?current_only=true")

    assert after.status_code == 200
    assert after.json()["configuration"] == CONFIG
    assert after.json()["revoked"] is True
    assert after.json()["usable_as_current"] is False
    # Without cases:read_all the reason is not returned, but the revocation itself is visible.
    assert after.json()["revocation_reason"] is None
    assert current.json() == []


def test_a_record_cannot_be_revoked_through_another_case(people):
    case_id = people["case_id"]
    record_id = record(people["investigator"], case_id).json()["record_id"]
    other_case = new_case(people["reporter"], title="Other case")
    assigned = people["triager"].post(
        f"/api/v1/cases/{other_case}/assignments", json={"user_id": people["investigator_id"]}, headers=idem()
    )
    assert assigned.status_code == 201, assigned.text

    response = people["investigator"].post(
        f"/api/v1/cases/{other_case}/model-records/{record_id}/revoke", json={"reason": "wrong case"}, headers=idem()
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "model record not found"}
    assert [row[9] for row in record_rows(record_id)] == [False]


def test_revoking_an_unknown_record_is_not_found_and_writes_nothing(people):
    case_id = people["case_id"]
    unknown = str(uuid.uuid4())

    response = people["investigator"].post(
        f"/api/v1/cases/{case_id}/model-records/{unknown}/revoke", json={"reason": "x"}, headers=idem()
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "model record not found"}
    assert record_count(case_id) == 0


def test_only_the_investigator_role_may_write_model_records_by_default():
    # A write needs evidence:write and evidence:read. The live role matrix decides, so this pins the current grants.
    writers = {role for role in users.ROLES if {"evidence:read", "evidence:write"} <= rbac.permissions_for_role(role)}
    assert writers == {"investigator"}


@pytest.mark.parametrize("role", sorted(users.ROLES))
def test_each_role_gets_exactly_the_access_the_matrix_gives(people, role):
    case_id = people["case_id"]
    permissions = rbac.permissions_for_role(role)
    writer = {"evidence:read", "evidence:write"} <= permissions
    reader = "evidence:read" in permissions

    created = record(people["investigator"], case_id).json()
    actor_id = new_user(role)
    if "cases:assignable" in permissions:
        # A role that can be assigned must see the case through an assignment, so the 403 or 201 is about permission.
        assigned = people["triager"].post(
            f"/api/v1/cases/{case_id}/assignments", json={"user_id": actor_id}, headers=idem()
        )
        assert assigned.status_code == 201, assigned.text
    actor = ActingClient(actor_id)

    listed = actor.get(f"/api/v1/cases/{case_id}/model-records")
    assert listed.status_code == (200 if reader else 403), listed.text
    written = record(actor, case_id)
    assert written.status_code == (201 if writer else 403), written.text
    revoked = actor.post(
        f"/api/v1/cases/{case_id}/model-records/{created['record_id']}/revoke",
        json={"reason": "matrix check"},
        headers=idem(),
    )
    assert revoked.status_code == (200 if writer else 403), revoked.text
