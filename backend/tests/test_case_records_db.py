"""Case record flow against PostgreSQL: awareness events, impacts, legal duties, classification, and the preliminary report.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, users
from safe_kit.case_records import IMPACT_AREAS
from safe_kit.db import connect
from safe_kit.rbac import list_role_permissions, set_role_permissions

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

NEW_TABLES = ("awareness_events", "impacts", "legal_duties", "case_classifications")


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
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

    def put(self, url, **kwargs):
        return self._request("put", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._request("patch", url, **kwargs)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


@pytest.fixture
def people():
    return {
        "reporter": ActingClient(new_user("reporter")),
        "other_reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
        "investigator_id": new_user("investigator"),
    }


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def future_free(days: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def new_case(reporter: ActingClient) -> str:
    response = reporter.post("/api/v1/cases", json={"title": "Case", "awareness_time": past()})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def new_reporter_channel_case(reporter: ActingClient, contact: str | None) -> str:
    response = reporter.post(
        "/api/v1/reports", json={"title": "Report", "awareness_time": past(), "contact": contact}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def audit_count(action: str, case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (action, case_id),
        ).fetchone()[0]


def soft_delete(table: str, case_id: str) -> None:
    # Only the test removes a row's visibility here, as a manual administrator action would. The application never does.
    with connect() as conn:
        conn.execute(
            f"UPDATE {table} SET is_deleted = true, deleted_at = now() WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        )


def awareness_body(event_kind: str = "incident", basis: str = "monitoring alert") -> dict:
    return {
        "awareness_kind": "operator",
        "event_kind": event_kind,
        "occurred_at": past(hours=1),
        "basis": basis,
    }


# Fixed once, so that a repeated request carries the same values and is not a change.
NEXT_UPDATE = (datetime.now(UTC) + timedelta(days=7)).replace(microsecond=0).isoformat()


def classification_body(expected_version: int, event_kind: str = "near_miss", **extra) -> dict:
    body = {
        "expected_version": expected_version,
        "triggers": ["boundary_escape", "harm_inaction_near_miss"],
        "event_kind": event_kind,
        "internal_incident_option": False,
        "classification_rationale": "the rationale",
        "responsible_operator": "operator team",
        "affected_versions": "model 1.2, runtime 3",
        "approved_scope": "sandbox only",
        "scope_permission_source": "system owner approval",
        "containment": "sandbox stopped",
        "evidence_gap_plan": "collect the original trace",
        "next_update_due_at": NEXT_UPDATE,
    }
    body.update(extra)
    return body


@contextmanager
def reporter_contact_holder(role: str = "triager"):
    """Grant reporter_contact:read to a role for the length of the block, then restore the role's permissions.

    Only a holder of reporter_contact:read can assign a reporter-channel case before it has an assignee
    (cases.CaseScope.visibility_sql), so the assignment in the tests below runs inside this block.
    """
    before = frozenset(list_role_permissions()[role])
    set_role_permissions(role, before | {"reporter_contact:read"})
    try:
        yield
    finally:
        set_role_permissions(role, before)


def assign_investigator(triager: ActingClient, case_id: str, investigator_id: str) -> None:
    with reporter_contact_holder():
        response = triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": investigator_id})
    assert response.status_code == 201, response.text


# --- schema -------------------------------------------------------------------------------------------------------


def test_new_tables_have_the_standard_columns_and_the_updated_at_trigger():
    with connect() as conn:
        for table in NEW_TABLES:
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
                    (table,),
                ).fetchall()
            }
            assert {"created_at", "updated_at", "deleted_at", "is_deleted"} <= columns, table
            triggers = conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
                (table, f"{table}_set_updated_at"),
            ).fetchone()[0]
            assert triggers == 1, table


def test_the_updated_at_trigger_moves_updated_at_on_every_change(people):
    case_id = new_case(people["reporter"])
    people["triager"].put(
        f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "low", "scope": "three users"}
    )
    with connect() as conn:
        conn.execute(
            "UPDATE impacts SET updated_at = '2000-01-01T00:00:00+00', scope = 'changed' WHERE case_id = %s",
            (case_id,),
        )
        moved = conn.execute(
            "SELECT updated_at > '2000-01-01T00:00:00+00' FROM impacts WHERE case_id = %s AND NOT is_deleted", (case_id,)
        ).fetchone()[0]
    assert moved


def test_soft_deleted_rows_disappear_from_every_new_read(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    assert triager.post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body()).status_code == 201
    assert triager.put(f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "low", "scope": "s"}).status_code == 200
    duty = {"kind": "statutory", "description": "notify", "source_reference": "s. 4", "due_at": future_free()}
    assert triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty).status_code == 201
    assert triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0)).status_code == 200

    for table in NEW_TABLES:
        soft_delete(table, case_id)

    assert triager.get(f"/api/v1/cases/{case_id}/awareness-events").json() == []
    scale = next(row for row in triager.get(f"/api/v1/cases/{case_id}/impacts").json() if row["area"] == "scale")
    assert scale["assessed"] is False
    assert triager.get(f"/api/v1/cases/{case_id}/legal-duties").json() == []
    assert triager.get(f"/api/v1/cases/{case_id}/classification").json()["version"] == 0


# --- visibility -----------------------------------------------------------------------------------------------------


def test_reporter_reads_own_awareness_without_the_basis_and_others_get_404(people):
    case_id = new_case(people["reporter"])
    people["triager"].post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body(basis="secret basis"))

    own = people["reporter"].get(f"/api/v1/cases/{case_id}/awareness-events")
    assert own.status_code == 200
    assert own.json()[0]["basis"] is None
    assert own.json()[0]["event_kind"] == "incident"

    assert people["other_reporter"].get(f"/api/v1/cases/{case_id}/awareness-events").status_code == 404


def test_reporter_channel_case_is_hidden_from_triagers_until_assigned(people):
    case_id = new_reporter_channel_case(people["reporter"], contact="contact-1")
    triager = people["triager"]
    assert triager.get(f"/api/v1/cases/{case_id}/impacts").status_code == 404
    assert triager.get(f"/api/v1/cases/{case_id}/classification").status_code == 404
    assert triager.get(f"/api/v1/cases/{case_id}/preliminary-report").status_code == 404

    assign_investigator(triager, case_id, people["investigator_id"])
    assert triager.get(f"/api/v1/cases/{case_id}/impacts").status_code == 200


def test_investigator_cannot_write_triage_records(people):
    case_id = new_case(people["reporter"])
    investigator = ActingClient(people["investigator_id"])
    assert investigator.post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body()).status_code == 403
    assert investigator.put(f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "low", "scope": "s"}).status_code == 403
    assert investigator.post(
        f"/api/v1/cases/{case_id}/legal-duties",
        json={"kind": "statutory", "description": "d", "source_reference": "s", "due_at": future_free()},
    ).status_code == 403
    assert investigator.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0)).status_code == 403


# --- T01: awareness events ----------------------------------------------------------------------------------------


def test_awareness_events_keep_incident_near_miss_and_blocked_attempt_apart(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    for kind in ("incident", "near_miss", "blocked_attempt"):
        assert triager.post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body(kind)).status_code == 201
    kinds = [row["event_kind"] for row in triager.get(f"/api/v1/cases/{case_id}/awareness-events").json()]
    assert sorted(kinds) == ["blocked_attempt", "incident", "near_miss"]


def test_closed_case_refuses_every_record_write_but_still_reads(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    assert triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0)).status_code == 200
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (people["triager"].user_id, case_id),
        )
    assert triager.post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body()).status_code == 409
    assert triager.put(f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "low", "scope": "s"}).status_code == 409
    assert triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(1)).status_code == 409
    assert triager.get(f"/api/v1/cases/{case_id}/classification").status_code == 200


# --- impacts: an empty set is not "no impact" ---------------------------------------------------------------------


def test_impacts_list_every_area_and_an_assessed_none_differs_from_not_assessed(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    rows = triager.get(f"/api/v1/cases/{case_id}/impacts").json()
    assert [row["area"] for row in rows] == list(IMPACT_AREAS)
    assert all(row["assessed"] is False for row in rows)

    assert triager.put(f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "none", "scope": "no users"}).status_code == 200
    rows = {row["area"]: row for row in triager.get(f"/api/v1/cases/{case_id}/impacts").json()}
    assert rows["scale"]["assessed"] is True and rows["scale"]["severity"] == "none"
    assert rows["duration"]["assessed"] is False


def test_impact_changes_are_audited_and_a_repeat_writes_no_row(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    triager.put(f"/api/v1/cases/{case_id}/impacts/reversibility", json={"severity": "unknown", "scope": "to research"})
    assert audit_count("case.impact_changed", case_id) == 1
    triager.put(f"/api/v1/cases/{case_id}/impacts/reversibility", json={"severity": "unknown", "scope": "to research"})
    assert audit_count("case.impact_changed", case_id) == 1
    triager.put(f"/api/v1/cases/{case_id}/impacts/reversibility", json={"severity": "high", "scope": "to research"})
    assert audit_count("case.impact_changed", case_id) == 2


# --- legal duties are independent of the SAFE deadlines -------------------------------------------------------------


def test_legal_duty_leaves_the_safe_deadlines_alone_and_its_changes_are_audited(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    deadlines_before = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]

    duty = {"kind": "contractual", "description": "tell the customer", "source_reference": "clause 9", "due_at": future_free(1)}
    created = triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty)
    assert created.status_code == 201
    duty_id = created.json()["id"]
    assert audit_count("case.legal_duty_added", case_id) == 1

    assert triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"] == deadlines_before

    done = triager.patch(f"/api/v1/cases/{case_id}/legal-duties/{duty_id}", json={"status": "done"})
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert audit_count("case.legal_duty_changed", case_id) == 1
    triager.patch(f"/api/v1/cases/{case_id}/legal-duties/{duty_id}", json={"status": "done"})
    assert audit_count("case.legal_duty_changed", case_id) == 1


def test_legal_duty_patch_refuses_nulls_and_unknown_duties(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    duty = {"kind": "statutory", "description": "d", "source_reference": "s", "due_at": future_free()}
    duty_id = triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty).json()["id"]
    assert triager.patch(f"/api/v1/cases/{case_id}/legal-duties/{duty_id}", json={"description": None}).status_code == 422
    assert triager.patch(f"/api/v1/cases/{case_id}/legal-duties/{duty_id}", json={}).status_code == 422
    unknown = str(uuid.uuid4())
    assert triager.patch(f"/api/v1/cases/{case_id}/legal-duties/{unknown}", json={"status": "done"}).status_code == 404


# --- T03: classification versions and the preliminary report ------------------------------------------------------


def test_classification_is_stored_as_versions_with_a_conflict_check(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    assert triager.get(f"/api/v1/cases/{case_id}/classification").json()["version"] == 0

    first = triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0))
    assert first.status_code == 200 and first.json()["version"] == 1

    stale = triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0, event_kind="incident"))
    assert stale.status_code == 409
    assert stale.json() == {"detail": {"status": "classification_version_conflict", "current_version": 1}}

    same = triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(1))
    assert same.status_code == 200 and same.json()["version"] == 1
    assert audit_count("case.classification_changed", case_id) == 1

    changed = triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(1, event_kind="incident"))
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert audit_count("case.classification_changed", case_id) == 2
    assert triager.get(f"/api/v1/cases/{case_id}/classification").json()["event_kind"] == "incident"


def test_internal_incident_option_keeps_every_safe_due_date(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    deadlines_before = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    body = classification_body(0, internal_incident_option=True, classification_rationale="handled internally")
    assert triager.put(f"/api/v1/cases/{case_id}/classification", json=body).status_code == 200
    deadlines_after = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    # The option changes statuses of the external notice tasks only. No due date moves and no task is removed.
    assert [(task["kind"], task["due_at"]) for task in deadlines_after] == [
        (task["kind"], task["due_at"]) for task in deadlines_before
    ]


def test_internal_incident_option_without_rationale_is_refused(people):
    case_id = new_case(people["reporter"])
    body = classification_body(0, internal_incident_option=True, classification_rationale=None)
    assert people["triager"].put(f"/api/v1/cases/{case_id}/classification", json=body).status_code == 422


def test_reporter_cannot_store_a_classification(people):
    case_id = new_case(people["reporter"])
    assert people["reporter"].put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0)).status_code == 403


def test_preliminary_report_is_generated_with_missing_fields_marked(people):
    case_id = new_case(people["reporter"])
    response = people["triager"].get(f"/api/v1/cases/{case_id}/preliminary-report")
    assert response.status_code == 200
    report = response.json()
    assert report["complete"] is False
    fields = {(item["field"], item["state"]) for item in report["missing_fields"]}
    assert ("contact", "missing") in fields
    assert ("classification.triggers", "missing") in fields
    assert ("awareness_events", "missing") in fields
    assert ("impacts.scale", "missing") in fields
    assert len(report["impacts"]) == len(IMPACT_AREAS)


def test_preliminary_report_is_complete_when_every_field_is_recorded(people):
    case_id = new_reporter_channel_case(people["reporter"], contact="contact-2")
    triager = people["triager"]
    assign_investigator(triager, case_id, people["investigator_id"])
    assert triager.post(f"/api/v1/cases/{case_id}/awareness-events", json=awareness_body()).status_code == 201
    for area in IMPACT_AREAS:
        assert triager.put(f"/api/v1/cases/{case_id}/impacts/{area}", json={"severity": "low", "scope": "scope"}).status_code == 200
    assert triager.put(f"/api/v1/cases/{case_id}/classification", json=classification_body(0)).status_code == 200
    duty = {"kind": "statutory", "description": "notify", "source_reference": "s. 4", "due_at": future_free()}
    assert triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty).status_code == 201

    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    assert report["complete"] is True
    assert report["missing_fields"] == []
    assert report["contact"] == {"status": "recorded"}
    assert len(report["legal_duties"]) == 1
    assert len(report["safe_deadlines"]) == 5


def test_preliminary_report_marks_an_unknown_impact_as_unknown(people):
    case_id = new_reporter_channel_case(people["reporter"], contact="contact-3")
    triager = people["triager"]
    assign_investigator(triager, case_id, people["investigator_id"])
    triager.put(f"/api/v1/cases/{case_id}/impacts/scale", json={"severity": "unknown", "scope": "to research"})
    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    assert {"field": "impacts.scale", "state": "unknown"} in report["missing_fields"]


def test_preliminary_report_is_for_triagers_only(people):
    case_id = new_case(people["reporter"])
    assert people["reporter"].get(f"/api/v1/cases/{case_id}/preliminary-report").status_code == 403
    investigator = ActingClient(people["investigator_id"])
    assert investigator.get(f"/api/v1/cases/{case_id}/preliminary-report").status_code == 403
