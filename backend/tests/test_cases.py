import uuid
from dataclasses import replace
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
    state = {"cases": {}, "created": [], "list_calls": []}

    def fake_create(title, awareness_time, created_by, **kwargs):
        # The first case gets CASE_ID, the others their own id, so several users can each have a case.
        case_id = CASE_ID if not state["cases"] else str(uuid.uuid4())
        number = len(state["cases"]) + 1
        case = Case(
            id=case_id,
            reference=f"SAFE-2026-{number:06d}",
            title=title,
            awareness_time=awareness_time,
            status="open",
            calendar_version=CALENDAR_VERSION,
            calendar_timezone="Europe/Istanbul",
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            created_by=created_by,
            deadlines=deadline_tasks_for(awareness_time, calendar_for("Europe/Istanbul")),
            updated_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        state["created"].append((title, awareness_time, created_by))
        state["cases"][case_id] = case
        return case

    def visible(case, scope):
        return scope.read_all or (scope.read_own and case.created_by == scope.user_id)

    def fake_find(case_id, scope):
        case = state["cases"].get(case_id)
        return case if case is not None and visible(case, scope) else None

    def matches(case, q, status):
        # The same rules as the SQL: q is a case-insensitive part of the reference, or the start of the ID.
        if status is not None and case.status != status:
            return False
        if q and q.lower() not in case.reference.lower() and not case.id.startswith(q.lower()):
            return False
        return True

    def fake_list(scope, limit, offset, q=None, status=None, include_evidence=False):
        state["list_calls"].append({"q": q, "status": status, "include_evidence": include_evidence})
        mine = [case for case in state["cases"].values() if visible(case, scope) and matches(case, q, status)]
        return mine[offset : offset + limit]

    def fake_count(scope, q=None, status=None):
        return sum(1 for case in state["cases"].values() if visible(case, scope) and matches(case, q, status))

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
    response = client.post("/api/v1/cases", json=valid_body(), headers=idem())
    assert response.status_code == 201
    body = response.json()
    assert body["reference"] == "SAFE-2026-000001"
    assert body["calendar_version"] == "provisional-1"
    assert body["status"] == "open"
    assert {task["kind"] for task in body["deadlines"]} >= {"affected_customers", "remediation_status"}
    assert store["created"][0][2] == REPORTER_ID  # created_by is the caller, never a body field


def test_title_is_stripped_and_blank_titles_are_rejected(client, store):
    assert client.post("/api/v1/cases", json=valid_body(title="  Leak  "), headers=idem()).json()["title"] == "Leak"
    assert client.post("/api/v1/cases", json=valid_body(title="   "), headers=idem()).status_code == 422
    assert client.post("/api/v1/cases", json=valid_body(title=""), headers=idem()).status_code == 422
    assert client.post("/api/v1/cases", json=valid_body(title="x" * 201), headers=idem()).status_code == 422


def test_naive_awareness_time_is_rejected(client, store):
    response = client.post("/api/v1/cases", json=valid_body(awareness_time="2026-10-30T09:00:00"), headers=idem())
    assert response.status_code == 422
    assert store["created"] == []


def test_future_awareness_time_is_rejected(client, store):
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert client.post("/api/v1/cases", json=valid_body(awareness_time=future), headers=idem()).status_code == 422
    assert store["created"] == []


def test_admin_has_no_case_permissions(client, caller, store):
    caller["user"] = make_user(ADMIN_ID, "admin")
    assert client.post("/api/v1/cases", json=valid_body(), headers=idem()).status_code == 403
    assert client.get("/api/v1/cases").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}").status_code == 403


def test_owner_reads_own_case(client, store):
    client.post("/api/v1/cases", json=valid_body(), headers=idem())
    response = client.get(f"/api/v1/cases/{CASE_ID}")
    assert response.status_code == 200
    assert response.json()["id"] == CASE_ID


def test_other_users_case_looks_exactly_like_a_missing_one(client, caller, store):
    client.post("/api/v1/cases", json=valid_body(), headers=idem())
    caller["user"] = make_user(OTHER_ID, "reporter")
    other = client.get(f"/api/v1/cases/{CASE_ID}")
    missing = client.get("/api/v1/cases/88888888-8888-8888-8888-888888888888")
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()


def test_case_id_must_be_a_uuid(client, store):
    assert client.get("/api/v1/cases/not-a-uuid").status_code == 422


def test_list_returns_only_the_callers_cases_with_paging(client, caller, store):
    client.post("/api/v1/cases", json=valid_body(), headers=idem())
    caller["user"] = make_user(OTHER_ID, "reporter")
    client.post("/api/v1/cases", json=valid_body(title="Other's case"), headers=idem())
    caller["user"] = make_user(REPORTER_ID, "reporter")
    page = client.get("/api/v1/cases?limit=1&offset=0").json()
    assert page["total"] == 1
    assert [item["title"] for item in page["items"]] == ["Suspected token leak"]
    assert "deadlines" not in page["items"][0]


def test_list_paging_bounds_are_enforced(client, store):
    assert client.get("/api/v1/cases?limit=0").status_code == 422
    assert client.get("/api/v1/cases?limit=201").status_code == 422
    assert client.get("/api/v1/cases?offset=-1").status_code == 422


def set_fields(store, case_id, **fields):
    store["cases"][case_id] = replace(store["cases"][case_id], **fields)


def test_list_filters_by_status(client, store):
    first = client.post("/api/v1/cases", json=valid_body(title="First"), headers=idem()).json()["id"]
    client.post("/api/v1/cases", json=valid_body(title="Second"), headers=idem())
    set_fields(store, first, status="closed")

    closed = client.get("/api/v1/cases?status=closed").json()
    assert closed["total"] == 1
    assert [item["id"] for item in closed["items"]] == [first]
    opened = client.get("/api/v1/cases?status=open").json()
    assert opened["total"] == 1
    assert [item["title"] for item in opened["items"]] == ["Second"]
    assert client.get("/api/v1/cases").json()["total"] == 2


@pytest.mark.parametrize("value", ["archived", "OPEN", "all", ""])
def test_list_refuses_an_unknown_status_with_422(client, store, value):
    response = client.get("/api/v1/cases", params={"status": value})
    assert response.status_code == 422
    assert store["list_calls"] == []


def test_status_combines_with_search_and_paging(client, store):
    ids = [client.post("/api/v1/cases", json=valid_body(title=title), headers=idem()).json()["id"] for title in ("Alpha", "Beta", "Gamma", "Delta")]
    set_fields(store, ids[0], status="closed")
    set_fields(store, ids[2], status="closed")

    only_beta = client.get("/api/v1/cases?status=open&q=SAFE-2026-000002").json()
    assert only_beta["total"] == 1
    assert [item["title"] for item in only_beta["items"]] == ["Beta"]
    # Beta is open but Alpha is the one with reference 000001: the status and the search must both hold.
    assert client.get("/api/v1/cases?status=closed&q=SAFE-2026-000002").json()["total"] == 0
    page = client.get("/api/v1/cases?status=open&q=SAFE-2026-00000&limit=1&offset=1").json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["offset"] == 1


def test_list_items_carry_the_next_deadline_and_the_last_update(client, store):
    case_id = client.post("/api/v1/cases", json=valid_body(), headers=idem()).json()["id"]
    set_fields(
        store,
        case_id,
        next_deadline=datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 2, 8, 30, tzinfo=UTC),
    )
    item = client.get("/api/v1/cases").json()["items"][0]
    assert item["next_deadline"] == "2026-11-09T09:00:00Z"
    assert item["updated_at"] == "2026-11-02T08:30:00Z"


def test_list_item_has_a_null_next_deadline_when_no_task_is_open(client, store):
    case_id = client.post("/api/v1/cases", json=valid_body(), headers=idem()).json()["id"]
    set_fields(store, case_id, next_deadline=None, updated_at=None)
    item = client.get("/api/v1/cases").json()["items"][0]
    assert item["next_deadline"] is None
    # With no recorded change, the last update is the creation time.
    assert item["updated_at"] == "2026-11-01T00:00:00Z"


def test_list_items_keep_their_fields_and_add_only_the_two_new_ones(client, store):
    client.post("/api/v1/cases", json=valid_body(), headers=idem())
    item = client.get("/api/v1/cases").json()["items"][0]
    assert set(item) == {
        "id",
        "reference",
        "title",
        "awareness_time",
        "status",
        "created_at",
        "next_deadline",
        "updated_at",
        "risk_score",
        "impact_c",
        "impact_b",
        "impact_a",
    }


def test_list_does_not_leak_a_case_the_caller_cannot_see_or_its_deadline(client, caller, store):
    caller["user"] = make_user(OTHER_ID, "reporter")
    other_case = client.post("/api/v1/cases", json=valid_body(title="Not yours"), headers=idem()).json()["id"]
    set_fields(store, other_case, next_deadline=datetime(2026, 12, 24, 9, 0, tzinfo=UTC))
    caller["user"] = make_user(REPORTER_ID, "reporter")

    response = client.get("/api/v1/cases?status=open")
    assert response.json()["total"] == 0
    assert response.json()["items"] == []
    assert other_case not in response.text
    assert "2026-12-24" not in response.text


def test_list_passes_the_status_and_the_evidence_right_to_the_query(client, caller, store):
    client.get("/api/v1/cases?status=open")
    assert store["list_calls"][-1] == {"q": None, "status": "open", "include_evidence": False}

    caller["user"] = make_user(OTHER_ID, "investigator")  # holds evidence:read
    client.get("/api/v1/cases")
    assert store["list_calls"][-1] == {"q": None, "status": None, "include_evidence": True}


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
    response = client.post("/api/v1/cases", json=valid_body(), headers=idem())
    assert response.status_code == 409
    assert response.json() == {"detail": "system timezone is not configured"}
    assert timezone_db.case_rows == []
