"""Risk impact values against PostgreSQL: versions, the audit row, the CHECK constraints, idempotency and access.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, users
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from error_body import without_request_id

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

ACTION = "case.impact_scores_changed"


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


@pytest.fixture
def people():
    return {
        "reporter": ActingClient(new_user("reporter")),
        "other_reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
        "reviewer": ActingClient(new_user("reviewer")),
        "investigator": ActingClient(new_user("investigator")),
        "auditor": ActingClient(new_user("auditor")),
        "reporting_owner": ActingClient(new_user("reporting_owner")),
    }


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def new_case(reporter: ActingClient) -> str:
    response = reporter.post("/api/v1/cases", json={"title": "Case", "awareness_time": past()}, headers=idem())
    assert response.status_code == 201, response.text
    return response.json()["id"]


def scores_path(case_id: str) -> str:
    return f"/api/v1/cases/{case_id}/impact-scores"


def values(version: int, c, b, a, **extra) -> dict:
    body = {"expected_version": version, "c": c, "b": b, "a": a}
    body.update(extra)
    return body


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def stored_versions(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT version, confidentiality, integrity, availability FROM case_impact_scores
            WHERE case_id = %s AND NOT is_deleted ORDER BY version
            """,
            (case_id,),
        ).fetchall()


def audit_rows(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT actor_id::text, details FROM audit_events
            WHERE action = %s AND target_id = %s AND NOT is_deleted ORDER BY created_at, id
            """,
            (ACTION, case_id),
        ).fetchall()


def test_a_new_case_has_no_values_and_no_score(people):
    case_id = new_case(people["reporter"])
    for who in ("reporter", "triager"):
        response = people[who].get(scores_path(case_id))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == 0
        assert body["c"] is None and body["b"] is None and body["a"] is None
        assert body["risk_score"] is None
    detail = people["reporter"].get(f"/api/v1/cases/{case_id}").json()
    assert detail["risk_score"] is None and detail["impact_c"] is None
    listed = [item for item in people["reporter"].get("/api/v1/cases?limit=200").json()["items"] if item["id"] == case_id]
    assert listed[0]["risk_score"] is None


def test_setting_the_values_stores_a_version_and_shows_the_score_everywhere(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    response = triager.post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem())
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1
    assert response.json()["risk_score"] == 3.0
    assert stored_versions(case_id) == [(1, 2, 3, 4)]
    assert triager.get(scores_path(case_id)).json()["risk_score"] == 3.0
    detail = people["reporter"].get(f"/api/v1/cases/{case_id}").json()
    assert (detail["risk_score"], detail["impact_c"], detail["impact_b"], detail["impact_a"]) == (3.0, 2, 3, 4)
    listed = [item for item in people["reporter"].get("/api/v1/cases?limit=200").json()["items"] if item["id"] == case_id]
    assert listed[0]["risk_score"] == 3.0


def test_a_new_version_leaves_the_older_versions_unchanged(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    assert triager.post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem()).status_code == 200
    response = triager.post(scores_path(case_id), json=values(1, 5, 3, 4), headers=idem())
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["risk_score"] == 4.0
    assert stored_versions(case_id) == [(1, 2, 3, 4), (2, 5, 3, 4)]


def test_a_stale_expected_version_is_a_409_and_stores_nothing(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    triager.post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem())
    triager.post(scores_path(case_id), json=values(1, 5, 3, 4), headers=idem())
    before = len(audit_rows(case_id))
    response = triager.post(scores_path(case_id), json=values(1, 1, 1, 1), headers=idem())
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "impact_version_conflict", "current_version": 2}
    assert len(stored_versions(case_id)) == 2
    assert len(audit_rows(case_id)) == before


def test_each_change_writes_one_audit_row_with_the_actor_and_the_old_and_new_values(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    triager.post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem())
    triager.post(scores_path(case_id), json=values(1, 5, 3, None), headers=idem())
    rows = audit_rows(case_id)
    assert len(rows) == 2
    actor, details = rows[1]
    assert actor == triager.user_id
    assert details == {
        "version": {"old": 1, "new": 2},
        "confidentiality": {"old": 2, "new": 5},
        "availability": {"old": 4, "new": None},
    }


def test_a_save_that_changes_nothing_stores_nothing_and_writes_no_audit_row(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    triager.post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem())
    before = len(audit_rows(case_id))
    response = triager.post(scores_path(case_id), json=values(1, 2, 3, 4), headers=idem())
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert stored_versions(case_id) == [(1, 2, 3, 4)]
    assert len(audit_rows(case_id)) == before


def test_a_value_left_unset_gives_no_score_until_all_three_are_set(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    response = triager.post(scores_path(case_id), json=values(0, 5, None, None), headers=idem())
    assert response.json()["risk_score"] is None
    assert triager.get(scores_path(case_id)).json()["risk_score"] is None
    response = triager.post(scores_path(case_id), json=values(1, 5, 1, 1), headers=idem())
    assert response.json()["risk_score"] == 2.3


def test_a_repeated_request_with_the_same_key_returns_the_first_answer_and_writes_nothing(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = triager.post(scores_path(case_id), json=values(0, 1, 1, 1), headers=key)
    again = triager.post(scores_path(case_id), json=values(0, 1, 1, 1), headers=key)
    assert first.status_code == again.status_code == 200
    assert without_request_id(again) == without_request_id(first)
    assert stored_versions(case_id) == [(1, 1, 1, 1)]
    assert len(audit_rows(case_id)) == 1


def test_the_same_key_with_another_request_is_refused(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    key = {"Idempotency-Key": str(uuid.uuid4())}
    triager.post(scores_path(case_id), json=values(0, 1, 1, 1), headers=key)
    response = triager.post(scores_path(case_id), json=values(0, 5, 5, 5), headers=key)
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "idempotency_key_reused"}
    assert stored_versions(case_id) == [(1, 1, 1, 1)]


def test_a_closed_case_refuses_the_write_but_still_reads(people):
    case_id = new_case(people["reporter"])
    triager = people["triager"]
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (triager.user_id, case_id),
        )
    assert triager.post(scores_path(case_id), json=values(0, 1, 1, 1), headers=idem()).status_code == 409
    assert triager.get(scores_path(case_id)).status_code == 200


def test_only_the_triager_role_writes_and_every_case_reader_reads(people):
    case_id = new_case(people["reporter"])
    for who in ("reviewer", "investigator", "auditor", "reporting_owner", "other_reporter"):
        response = people[who].post(scores_path(case_id), json=values(0, 1, 1, 1), headers=idem())
        assert response.status_code in (403, 404), (who, response.text)
    assert stored_versions(case_id) == []
    # The owner (read_own) reads the values. A reporter who did not open the case gets 404.
    assert people["reporter"].get(scores_path(case_id)).status_code == 200
    assert people["other_reporter"].get(scores_path(case_id)).status_code == 404
    assert people["auditor"].get(scores_path(case_id)).status_code == 403
    assert people["triager"].get(scores_path(case_id)).status_code == 200


def test_the_database_refuses_a_value_outside_one_to_five(people):
    case_id = new_case(people["reporter"])
    with connect() as conn:
        user_id = conn.execute("SELECT id::text FROM users WHERE NOT is_deleted LIMIT 1").fetchone()[0]
    for column in ("confidentiality", "integrity", "availability"):
        for value in (0, 6):
            with pytest.raises(psycopg.errors.CheckViolation):
                with connect() as conn:
                    conn.execute(
                        f"INSERT INTO case_impact_scores (case_id, version, {column}, created_by) VALUES (%s, 1, %s, %s)",
                        (case_id, value, user_id),
                    )


def test_the_database_refuses_a_version_below_one(people):
    case_id = new_case(people["reporter"])
    with connect() as conn:
        user_id = conn.execute("SELECT id::text FROM users WHERE NOT is_deleted LIMIT 1").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                "INSERT INTO case_impact_scores (case_id, version, created_by) VALUES (%s, 0, %s)",
                (case_id, user_id),
            )


def test_the_database_accepts_an_unset_value_and_the_bounds(people):
    case_id = new_case(people["reporter"])
    with connect() as conn:
        user_id = conn.execute("SELECT id::text FROM users WHERE NOT is_deleted LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO case_impact_scores (case_id, version, confidentiality, integrity, availability, created_by) "
            "VALUES (%s, 1, NULL, 1, 5, %s)",
            (case_id, user_id),
        )
    assert stored_versions(case_id) == [(1, None, 1, 5)]


def test_the_table_has_the_standard_columns_and_the_updated_at_trigger():
    with connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
                ("case_impact_scores",),
            ).fetchall()
        }
        assert {"created_at", "updated_at", "deleted_at", "is_deleted"} <= columns
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
            ("case_impact_scores", "case_impact_scores_set_updated_at"),
        ).fetchone()[0]
        assert triggers == 1


def test_a_soft_deleted_version_is_not_read(people):
    case_id = new_case(people["reporter"])
    people["triager"].post(scores_path(case_id), json=values(0, 2, 3, 4), headers=idem())
    with connect() as conn:
        conn.execute(
            "UPDATE case_impact_scores SET is_deleted = true, deleted_at = now() WHERE case_id = %s",
            (case_id,),
        )
    response = people["triager"].get(scores_path(case_id))
    assert response.json()["version"] == 0
    assert response.json()["risk_score"] is None
