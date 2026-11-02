import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit.cases import (
    BROADER_ADVISORY_CONDITION_REASON,
    CALENDAR_VERSION,
    Case,
    calendar_for,
    create_case,
    deadline_tasks_for,
)
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.system_timezone import TimezoneNotConfigured
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
ADMIN_ID = "33333333-3333-3333-3333-333333333333"
CASE_ID = "99999999-9999-9999-9999-999999999999"


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


def test_calendar_takes_the_timezone_and_keeps_the_provisional_version():
    calendar = calendar_for("Pacific/Auckland")
    assert calendar.timezone == ZoneInfo("Pacific/Auckland")
    assert calendar.holidays == frozenset()
    assert calendar.version == CALENDAR_VERSION == "provisional-1"


def test_deadline_tasks_come_from_deadlines_for():
    t0 = datetime(2026, 11, 6, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))  # Friday
    tasks = {task.kind: task for task in deadline_tasks_for(t0, calendar_for("Europe/Istanbul"))}
    assert set(tasks) == {
        "affected_customers",
        "preliminary_safe_report",
        "broader_advisory",
        "preliminary_public_report",
        "remediation_status",
    }
    assert tasks["affected_customers"].due_at == t0 + timedelta(hours=72)
    # Four business days from Friday 09:00 local is Thursday 09:00 local.
    assert tasks["preliminary_safe_report"].due_at == datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    # The conditional 14-day advisory starts not applicable, with a reason. The other tasks start open.
    assert tasks["broader_advisory"].status == "not_applicable"
    assert tasks["broader_advisory"].not_applicable_reason == BROADER_ADVISORY_CONDITION_REASON
    assert {kind for kind, task in tasks.items() if task.status == "open"} == {
        "affected_customers",
        "preliminary_safe_report",
        "preliminary_public_report",
        "remediation_status",
    }
    assert all(task.not_applicable_reason is None for kind, task in tasks.items() if kind != "broader_advisory")


def test_weekly_update_is_not_created_with_the_case():
    t0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert "weekly_update" not in {task.kind for task in deadline_tasks_for(t0, calendar_for("Europe/Istanbul"))}


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


@pytest.fixture
def store(monkeypatch):
    state = {"cases": {}, "created": []}

    def fake_create(title, awareness_time, created_by):
        # The first case gets CASE_ID, the others their own id, so several users can each have a case.
        case_id = CASE_ID if not state["cases"] else str(uuid.uuid4())
        case = Case(
            id=case_id,
            reference="SAFE-2026-000001",
            title=title,
            awareness_time=awareness_time,
            status="open",
            calendar_version=CALENDAR_VERSION,
            calendar_timezone="Europe/Istanbul",
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            created_by=created_by,
            deadlines=deadline_tasks_for(awareness_time, calendar_for("Europe/Istanbul")),
        )
        state["created"].append((title, awareness_time, created_by))
        state["cases"][case_id] = case
        return case

    def visible(case, scope):
        return scope.read_all or (scope.read_own and case.created_by == scope.user_id)

    def fake_find(case_id, scope):
        case = state["cases"].get(case_id)
        return case if case is not None and visible(case, scope) else None

    def fake_list(scope, limit, offset, q=None):
        mine = [case for case in state["cases"].values() if visible(case, scope)]
        return mine[offset : offset + limit]

    def fake_count(scope, q=None):
        return sum(1 for case in state["cases"].values() if visible(case, scope))

    monkeypatch.setattr(api, "create_case", fake_create)
    monkeypatch.setattr(api, "find_visible_case", fake_find)
    monkeypatch.setattr(api, "list_visible_cases", fake_list)
    monkeypatch.setattr(api, "count_visible_cases", fake_count)
    return state


def valid_body(**overrides):
    body = {"title": "Suspected token leak", "awareness_time": "2026-10-30T09:00:00+03:00"}
    body.update(overrides)
    return body


def test_reporter_submits_a_case_and_gets_its_deadlines(client, store):
    response = client.post("/api/v1/cases", json=valid_body())
    assert response.status_code == 201
    body = response.json()
    assert body["reference"] == "SAFE-2026-000001"
    assert body["calendar_version"] == "provisional-1"
    assert body["status"] == "open"
    assert {task["kind"] for task in body["deadlines"]} >= {"affected_customers", "remediation_status"}
    assert store["created"][0][2] == REPORTER_ID  # created_by is the caller, never a body field


def test_title_is_stripped_and_blank_titles_are_rejected(client, store):
    assert client.post("/api/v1/cases", json=valid_body(title="  Leak  ")).json()["title"] == "Leak"
    assert client.post("/api/v1/cases", json=valid_body(title="   ")).status_code == 422
    assert client.post("/api/v1/cases", json=valid_body(title="")).status_code == 422
    assert client.post("/api/v1/cases", json=valid_body(title="x" * 201)).status_code == 422


def test_naive_awareness_time_is_rejected(client, store):
    response = client.post("/api/v1/cases", json=valid_body(awareness_time="2026-10-30T09:00:00"))
    assert response.status_code == 422
    assert store["created"] == []


def test_future_awareness_time_is_rejected(client, store):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert client.post("/api/v1/cases", json=valid_body(awareness_time=future)).status_code == 422
    assert store["created"] == []


def test_admin_has_no_case_permissions(client, caller, store):
    caller["user"] = make_user(ADMIN_ID, "admin")
    assert client.post("/api/v1/cases", json=valid_body()).status_code == 403
    assert client.get("/api/v1/cases").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}").status_code == 403


def test_owner_reads_own_case(client, store):
    client.post("/api/v1/cases", json=valid_body())
    response = client.get(f"/api/v1/cases/{CASE_ID}")
    assert response.status_code == 200
    assert response.json()["id"] == CASE_ID


def test_other_users_case_looks_exactly_like_a_missing_one(client, caller, store):
    client.post("/api/v1/cases", json=valid_body())
    caller["user"] = make_user(OTHER_ID, "reporter")
    other = client.get(f"/api/v1/cases/{CASE_ID}")
    missing = client.get("/api/v1/cases/88888888-8888-8888-8888-888888888888")
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()


def test_case_id_must_be_a_uuid(client, store):
    assert client.get("/api/v1/cases/not-a-uuid").status_code == 422


def test_list_returns_only_the_callers_cases_with_paging(client, caller, store):
    client.post("/api/v1/cases", json=valid_body())
    caller["user"] = make_user(OTHER_ID, "reporter")
    client.post("/api/v1/cases", json=valid_body(title="Other's case"))
    caller["user"] = make_user(REPORTER_ID, "reporter")
    page = client.get("/api/v1/cases?limit=1&offset=0").json()
    assert page["total"] == 1
    assert [item["title"] for item in page["items"]] == ["Suspected token leak"]
    assert "deadlines" not in page["items"][0]


def test_list_paging_bounds_are_enforced(client, store):
    assert client.get("/api/v1/cases?limit=0").status_code == 422
    assert client.get("/api/v1/cases?limit=201").status_code == 422
    assert client.get("/api/v1/cases?offset=-1").status_code == 422


def test_case_records_the_system_timezone_in_effect_and_computes_deadlines_in_it(timezone_db):
    timezone_db.seed_timezone("Pacific/Auckland", datetime(2026, 10, 1, tzinfo=UTC))
    t0 = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)  # Friday 09:00 in Auckland (NZDT, UTC+13)

    case = create_case("Auckland case", t0, created_by=REPORTER_ID)

    assert case.calendar_timezone == "Pacific/Auckland"
    assert timezone_db.case_rows[0][8] == "Pacific/Auckland"
    tasks = {task.kind: task for task in case.deadlines}
    # Four business days from Friday 09:00 local is Thursday 09:00 local, which is 20:00 UTC on the Wednesday.
    assert tasks["preliminary_safe_report"].due_at == datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    audit = timezone_db.audit_rows[-1]
    assert audit["action"] == "case.create"
    assert audit["details"] == {
        "calendar_version": "provisional-1",
        "calendar_timezone": "Pacific/Auckland",
        "not_applicable": ["broader_advisory"],
    }


def test_later_timezone_change_does_not_move_an_existing_case(timezone_db):
    timezone_db.seed_timezone("Europe/Istanbul", datetime(2026, 10, 1, tzinfo=UTC))
    case = create_case("Before the change", datetime(2026, 11, 1, 6, 0, tzinfo=UTC), created_by=REPORTER_ID)
    timezone_db.seed_timezone("Pacific/Auckland", datetime(2026, 11, 2, tzinfo=UTC))

    assert timezone_db.case_rows[-1][8] == "Europe/Istanbul"
    assert case.calendar_timezone == "Europe/Istanbul"


def test_case_cannot_be_created_before_the_system_timezone_is_set(timezone_db):
    with pytest.raises(TimezoneNotConfigured):
        create_case("No timezone", datetime(2026, 11, 1, 6, 0, tzinfo=UTC), created_by=REPORTER_ID)
    assert timezone_db.case_rows == []


def test_api_refuses_case_creation_without_a_system_timezone(client, timezone_db):
    response = client.post("/api/v1/cases", json=valid_body())
    assert response.status_code == 409
    assert response.json() == {"detail": "system timezone is not configured"}
    assert timezone_db.case_rows == []
