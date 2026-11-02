"""Idempotency-Key on the case, record, evidence and review writes, against PostgreSQL (plans/f0/api-draft.md section 3.4).

For each write route the same four checks run:
  1. a missing key is refused with 400, and so is a key that is not visible ASCII or is too long;
  2. the first request with a key stores its answer;
  3. the same key with the same request replays the stored answer, with the same status and body, and writes nothing
     (the audit row count does not move);
  4. the same key with another request is refused with 409 and writes nothing.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied. They create rows and never remove them, so use a throwaway database.
"""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, evidence_api, users
from safe_kit.db import connect
from safe_kit.migrate import main as migrate

from test_case_import import example_record, envelope
from test_evidence import MemoryStorage, write_kek
from safe_kit.evidence_keys import LocalFileKeyProvider
from error_body import without_request_id

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def future(days: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def new_key() -> str:
    return str(uuid.uuid4())


def audit_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]


class Actor:
    """One real user. The caller is set before every request, so actors never share a caller."""

    def __init__(self, role: str):
        self.user_id = users.create_user(
            "t-" + uuid.uuid4().hex[:12], "temporary passphrase", role
        )
        self.client = TestClient(api.app)

    def send(self, method: str, url: str, **kwargs):
        user = users.find_user_by_id(self.user_id)
        assert user is not None
        api.app.dependency_overrides[auth.current_user] = lambda: user
        return self.client.request(method, url, **kwargs)

    def ok(self, method: str, url: str, expect: int, **kwargs) -> dict:
        extra = kwargs.pop("headers", {})
        response = self.send(
            method, url, headers={"Idempotency-Key": new_key(), **extra}, **kwargs
        )
        assert response.status_code == expect, response.text
        return response.json() if response.content else {}


@dataclass
class World:
    reporter: Actor
    triager: Actor
    investigator: Actor
    reviewer: Actor


@pytest.fixture
def world(monkeypatch, tmp_path):
    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)"
        ).fetchone()[0]
        if not exists:
            conn.execute(
                "INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')"
            )
    # Evidence goes to memory and to a throwaway key file: no storage volume or key service is needed.
    monkeypatch.setattr(
        evidence_api, "_PROVIDER", LocalFileKeyProvider(write_kek(tmp_path / "kek.bin"))
    )
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: MemoryStorage())
    yield World(
        reporter=Actor("reporter"),
        triager=Actor("triager"),
        investigator=Actor("investigator"),
        reviewer=Actor("reviewer"),
    )
    api.app.dependency_overrides.clear()


# --- setup helpers ---------------------------------------------------------------------------------------------------


def open_case(world: World, title: str = "Case") -> dict:
    return world.reporter.ok(
        "POST", "/api/v1/cases", 201, json={"title": title, "awareness_time": past()}
    )


def assign(world: World, case_id: str, user_id: str) -> None:
    world.triager.ok(
        "POST", f"/api/v1/cases/{case_id}/assignments", 201, json={"user_id": user_id}
    )


def settle_deadlines(world: World, case: dict) -> None:
    for task in case["deadlines"]:
        world.triager.ok(
            "PATCH",
            f"/api/v1/cases/{case['id']}/deadlines/{task['id']}",
            200,
            json={"status": "done"},
        )


def closable_case(world: World) -> dict:
    """An open case with a recorded review layer and every deadline task done, so it may be closed."""
    case = open_case(world)
    assign(world, case["id"], world.reviewer.user_id)
    round_ = world.reviewer.ok(
        "POST", "/api/v1/reviews", 201, json={"case_id": case["id"]}
    )
    world.reviewer.ok(
        "PATCH",
        f"/api/v1/reviews/{round_['id']}/layers/model",
        200,
        json={"outcome": "fail", "reason": "Gap found"},
        headers={"If-Match": f'"{round_["revision"]}"'},
    )
    settle_deadlines(world, case)
    return case


# --- one scenario per route ------------------------------------------------------------------------------------------


@dataclass
class Scenario:
    actor: Actor
    method: str
    url: str
    expect: int
    body: dict | None = None
    other_body: dict | None = None
    other_url: str | None = None
    files: dict | None = None
    other_files: dict | None = None
    # Sent as If-Match (the revision a review layer change was made against).
    if_match: str | None = None

    def _request(self, key: str | None, url: str, body, files):
        headers = {} if key is None else {"Idempotency-Key": key}
        if self.if_match is not None:
            headers["If-Match"] = self.if_match
        if files is not None:
            return self.actor.send(self.method, url, headers=headers, files=files)
        return self.actor.send(self.method, url, headers=headers, json=body)

    def send(self, key: str | None):
        return self._request(key, self.url, self.body, self.files)

    def send_other(self, key: str):
        other_files = self.other_files if self.other_files is not None else self.files
        other_body = self.other_body if self.other_body is not None else self.body
        return self._request(key, self.other_url or self.url, other_body, other_files)


def scenario_create_case(world: World) -> Scenario:
    return Scenario(
        world.reporter,
        "POST",
        "/api/v1/cases",
        201,
        body={"title": "Case one", "awareness_time": past()},
        other_body={"title": "Case two", "awareness_time": past()},
    )


def scenario_report(world: World) -> Scenario:
    return Scenario(
        world.reporter,
        "POST",
        "/api/v1/reports",
        201,
        body={"title": "Report one", "awareness_time": past(), "contact": None},
        other_body={"title": "Report two", "awareness_time": past(), "contact": None},
    )


def scenario_import(world: World) -> Scenario:
    first = json.loads(envelope([example_record()]))
    other = json.loads(envelope([example_record()]))
    return Scenario(
        world.reporter,
        "POST",
        "/api/v1/cases/import",
        200,
        body=first,
        other_body=other,
    )


def scenario_evidence(world: World) -> Scenario:
    case = open_case(world)
    assign(world, case["id"], world.investigator.user_id)
    return Scenario(
        world.investigator,
        "POST",
        f"/api/v1/cases/{case['id']}/evidence",
        201,
        files={"file": ("note.txt", b"first note", "text/plain")},
        other_files={"file": ("note.txt", b"second note", "text/plain")},
    )


def scenario_link(world: World) -> Scenario:
    first, second, third = (
        open_case(world, "A"),
        open_case(world, "B"),
        open_case(world, "C"),
    )
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{first['id']}/links",
        201,
        body={"linked_case_id": second["id"]},
        other_body={"linked_case_id": third["id"]},
    )


def scenario_assignment(world: World) -> Scenario:
    case = open_case(world)
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/assignments",
        201,
        body={"user_id": world.investigator.user_id},
        other_body={"user_id": world.reviewer.user_id},
    )


def scenario_close(world: World) -> Scenario:
    case = closable_case(world)
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/close",
        200,
        body={"reason": "Closed after review"},
        other_body={"reason": "Another reason"},
    )


def scenario_reopen(world: World) -> Scenario:
    case = closable_case(world)
    world.triager.ok(
        "POST", f"/api/v1/cases/{case['id']}/close", 200, json={"reason": "Closed"}
    )
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/reopen",
        200,
        body={"reason": "Reopened for review"},
        other_body={"reason": "Another reason"},
    )


def scenario_awareness(world: World) -> Scenario:
    case = open_case(world)
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/awareness-events",
        201,
        body={
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": past(),
            "basis": "Alert",
        },
        other_body={
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": past(),
            "basis": "Other",
        },
    )


def scenario_legal_duty(world: World) -> Scenario:
    case = open_case(world)
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/legal-duties",
        201,
        body={
            "kind": "statutory",
            "description": "Notify",
            "source_reference": "Act 1",
            "due_at": future(),
        },
        other_body={
            "kind": "statutory",
            "description": "Other duty",
            "source_reference": "Act 1",
            "due_at": future(),
        },
    )


def scenario_impact(world: World) -> Scenario:
    case = open_case(world)
    return Scenario(
        world.triager,
        "PUT",
        f"/api/v1/cases/{case['id']}/impacts/severity",
        200,
        body={"severity": "high", "scope": "Wide"},
        other_body={"severity": "low", "scope": "Wide"},
    )


def _classification(**changes) -> dict:
    body = {
        "expected_version": 0,
        "triggers": ["none_applies"],
        "event_kind": None,
        "classification_rationale": None,
        "responsible_operator": None,
        "affected_versions": None,
        "approved_scope": None,
        "scope_permission_source": None,
        "containment": None,
        "evidence_gap_plan": None,
        "next_update_due_at": None,
    }
    body.update(changes)
    return body


def scenario_classification(world: World) -> Scenario:
    case = open_case(world)
    return Scenario(
        world.triager,
        "PUT",
        f"/api/v1/cases/{case['id']}/classification",
        200,
        body=_classification(),
        other_body=_classification(classification_rationale="Another rationale"),
    )


def scenario_deadline(world: World) -> Scenario:
    case = open_case(world)
    task = case["deadlines"][0]
    return Scenario(
        world.triager,
        "PATCH",
        f"/api/v1/cases/{case['id']}/deadlines/{task['id']}",
        200,
        body={"status": "done"},
        other_body={"status": "open"},
    )


def scenario_restore(world: World) -> Scenario:
    case = open_case(world)
    first, second = case["deadlines"][0], case["deadlines"][1]
    for task in (first, second):
        world.triager.ok(
            "PATCH",
            f"/api/v1/cases/{case['id']}/deadlines/{task['id']}",
            200,
            json={"status": "not_applicable", "reason": "Not needed here"},
        )
    return Scenario(
        world.triager,
        "POST",
        f"/api/v1/cases/{case['id']}/deadlines/{first['id']}/restore",
        200,
        other_url=f"/api/v1/cases/{case['id']}/deadlines/{second['id']}/restore",
    )


def scenario_review_layer(world: World) -> Scenario:
    case = open_case(world)
    assign(world, case["id"], world.reviewer.user_id)
    round_ = world.reviewer.ok(
        "POST", "/api/v1/reviews", 201, json={"case_id": case["id"]}
    )
    return Scenario(
        world.reviewer,
        "PATCH",
        f"/api/v1/reviews/{round_['id']}/layers/model",
        200,
        body={"outcome": "fail", "reason": "Gap"},
        other_body={"outcome": "fail", "reason": "Other gap"},
        if_match=f'"{round_["revision"]}"',
    )


SCENARIOS = {
    "create_case": scenario_create_case,
    "report": scenario_report,
    "import": scenario_import,
    "evidence": scenario_evidence,
    "link": scenario_link,
    "assignment": scenario_assignment,
    "close": scenario_close,
    "reopen": scenario_reopen,
    "awareness": scenario_awareness,
    "legal_duty": scenario_legal_duty,
    "impact": scenario_impact,
    "classification": scenario_classification,
    "deadline_patch": scenario_deadline,
    "deadline_restore": scenario_restore,
    "review_layer": scenario_review_layer,
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_missing_or_invalid_key_is_refused_with_400(world, name):
    scenario = SCENARIOS[name](world)
    response = scenario.send(None)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == {"status": "idempotency_key_required"}
    for invalid in ("two words", "k" * 129, "tab\tkey"):
        response = scenario.send(invalid)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == {"status": "idempotency_key_invalid"}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_first_request_stores_and_the_same_request_replays_without_writing(world, name):
    scenario = SCENARIOS[name](world)
    key = new_key()
    first = scenario.send(key)
    assert first.status_code == scenario.expect, first.text
    before = audit_count()
    replay = scenario.send(key)
    assert replay.status_code == first.status_code
    assert without_request_id(replay) == without_request_id(first)
    assert audit_count() == before


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_same_key_with_another_request_is_409_and_writes_nothing(world, name):
    scenario = SCENARIOS[name](world)
    key = new_key()
    assert scenario.send(key).status_code == scenario.expect
    before = audit_count()
    response = scenario.send_other(key)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "idempotency_key_reused"}
    assert audit_count() == before


def test_a_refused_request_stores_no_key_so_the_retry_with_the_same_key_succeeds(world):
    case = open_case(world)
    key = new_key()
    # The task does not exist: refused with 404, nothing stored.
    refused = world.triager.send(
        "PATCH",
        f"/api/v1/cases/{case['id']}/deadlines/{uuid.uuid4()}",
        headers={"Idempotency-Key": key},
        json={"status": "done"},
    )
    assert refused.status_code == 404
    task = case["deadlines"][0]
    retry = world.triager.send(
        "PATCH",
        f"/api/v1/cases/{case['id']}/deadlines/{task['id']}",
        headers={"Idempotency-Key": key},
        json={"status": "done"},
    )
    assert retry.status_code == 200, retry.text


def test_keys_are_scoped_to_the_principal(world):
    case = open_case(world)
    key = new_key()
    body = {
        "awareness_kind": "operator",
        "event_kind": "incident",
        "occurred_at": past(),
        "basis": "Alert",
    }
    url = f"/api/v1/cases/{case['id']}/awareness-events"
    assert (
        world.triager.send(
            "POST", url, headers={"Idempotency-Key": key}, json=body
        ).status_code
        == 201
    )
    # Another triager with the same key and the same request is a new request, so it is stored and answered as one.
    another = Actor("triager")
    response = another.send("POST", url, headers={"Idempotency-Key": key}, json=body)
    assert response.status_code == 201, response.text
