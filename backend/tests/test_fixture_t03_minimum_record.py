"""F1 fixture T03 (minimum record): the F1 part of the T03 acceptance scenario.

The minimum record has a contact, a classification version, the classification itself, containment, the evidence gap
plan, and the next update. An incomplete record is never refused. Its gaps are listed in the preliminary report, and the
report is still produced.

Covered without a database:
- an empty record lists every minimum field as missing, and the report is still built;
- each minimum field that is blank is the only gap listed when everything else is recorded;
- a missing contact, a missing awareness event, a not-assessed impact and an impact marked unknown are all listed.

Covered with a database:
- a partial classification is stored as a version, and the report lists the fields that are still missing;
- a second version records a new next update, and the report shows the version and the new update.

Not testable yet, so not asserted here:
- Whether a recorded impact area is complete, for example whether its scope text is enough to decide on it. F1 only
  distinguishes not assessed, unknown and recorded. The completeness rule belongs to F2.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import api, system_timezone
from safe_kit.db import connect
from safe_kit.case_records import (
    IMPACT_AREAS,
    REPORT_CLASSIFICATION_FIELDS,
    AwarenessEvent,
    Classification,
    ImpactAssessment,
    build_preliminary_report,
)
from safe_kit.cases import Case, calendar_for, deadline_tasks_for
from safe_kit.migrate import main as migrate
from test_case_records_db import ActingClient, classification_body, new_case, new_user
from test_evidence_lifecycle import requires_database

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
CASE_ID = "99999999-9999-9999-9999-999999999999"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def complete_record(version: int = 1) -> Classification:
    return Classification(
        version=version,
        triggers=("boundary_escape",),
        event_kind="near_miss",
        classification_rationale="rationale",
        responsible_operator="operator",
        affected_versions="model 1.2",
        approved_scope="scope",
        scope_permission_source="owner approval",
        containment="contained",
        evidence_gap_plan="collect logs",
        next_update_due_at=NOW + timedelta(days=7),
        created_by=TRIAGER_ID,
        created_at=NOW,
    )


def recorded_impacts(severity: str = "low") -> list[ImpactAssessment]:
    return [
        ImpactAssessment(
            area=area, assessed=True, severity=severity, scope="scope", updated_at=NOW
        )
        for area in IMPACT_AREAS
    ]


def one_event() -> list[AwarenessEvent]:
    return [
        AwarenessEvent(
            id="event-1",
            case_id=CASE_ID,
            awareness_kind="operator",
            event_kind="near_miss",
            occurred_at=NOW,
            basis="monitoring alert",
            recorded_by=TRIAGER_ID,
            created_at=NOW,
        )
    ]


def case_now() -> Case:
    return Case(
        id=CASE_ID,
        reference="SAFE-2026-000001",
        title="Sample",
        awareness_time=NOW,
        status="open",
        calendar_version="provisional-1",
        calendar_timezone="Europe/Istanbul",
        created_at=NOW,
        created_by="11111111-1111-1111-1111-111111111111",
        deadlines=deadline_tasks_for(NOW, calendar_for("Europe/Istanbul")),
    )


def blank(name: str):
    return () if name == "triggers" else None


def test_an_empty_record_lists_every_minimum_field_and_still_produces_the_report():
    empty = Classification(version=0)
    not_assessed = [
        ImpactAssessment(area=area, assessed=False) for area in IMPACT_AREAS
    ]
    report = build_preliminary_report(
        case_now(), empty, [], not_assessed, [], False, NOW
    )

    assert report["complete"] is False
    assert report["classification"]["version"] == 0
    missing = {(item["field"], item["state"]) for item in report["missing_fields"]}
    assert ("contact", "missing") in missing
    assert ("awareness_events", "missing") in missing
    for name in REPORT_CLASSIFICATION_FIELDS:
        assert (f"classification.{name}", "missing") in missing, name
    assert {(f"impacts.{area}", "missing") for area in IMPACT_AREAS} <= missing
    assert len(report["safe_deadlines"]) == 5


@pytest.mark.parametrize("name", REPORT_CLASSIFICATION_FIELDS)
def test_a_single_blank_classification_field_is_the_only_gap(name):
    record = replace(complete_record(), **{name: blank(name)})
    report = build_preliminary_report(
        case_now(), record, one_event(), recorded_impacts(), [], True, NOW
    )

    assert report["complete"] is False
    assert report["missing_fields"] == [
        {"field": f"classification.{name}", "state": "missing"}
    ]


def test_a_complete_record_has_no_gap_and_shows_the_version_containment_gap_and_next_update():
    report = build_preliminary_report(
        case_now(),
        complete_record(version=3),
        one_event(),
        recorded_impacts(),
        [],
        True,
        NOW,
    )

    assert report["complete"] is True
    assert report["missing_fields"] == []
    classification = report["classification"]
    assert classification["version"] == 3
    assert classification["containment"] == "contained"
    assert classification["evidence_gap_plan"] == "collect logs"
    assert classification["next_update_due_at"] == (NOW + timedelta(days=7)).isoformat()


@pytest.mark.parametrize("area", IMPACT_AREAS)
def test_a_single_impact_area_not_assessed_is_the_only_gap_for_that_area(area):
    impacts = [
        ImpactAssessment(
            area=other,
            assessed=other != area,
            severity="low",
            scope="scope",
            updated_at=NOW,
        )
        for other in IMPACT_AREAS
    ]
    report = build_preliminary_report(
        case_now(), complete_record(), one_event(), impacts, [], True, NOW
    )

    assert report["complete"] is False
    assert report["missing_fields"] == [
        {"field": f"impacts.{area}", "state": "missing"}
    ]
    assert report["impacts"][IMPACT_AREAS.index(area)]["assessed"] is False


@pytest.mark.parametrize("area", IMPACT_AREAS)
def test_an_impact_area_marked_unknown_is_listed_as_unknown_and_the_report_is_still_built(
    area,
):
    impacts = [
        ImpactAssessment(
            area=other,
            assessed=True,
            severity="unknown" if other == area else "low",
            scope="to research" if other == area else "scope",
            updated_at=NOW,
        )
        for other in IMPACT_AREAS
    ]
    report = build_preliminary_report(
        case_now(), complete_record(), one_event(), impacts, [], True, NOW
    )

    assert report["complete"] is False
    assert report["missing_fields"] == [
        {"field": f"impacts.{area}", "state": "unknown"}
    ]
    assert len(report["impacts"]) == len(IMPACT_AREAS)
    assert report["classification"]["containment"] == "contained"


def test_a_missing_contact_a_missing_event_and_an_unknown_impact_are_listed_with_their_own_state():
    impacts = recorded_impacts()
    impacts[1] = ImpactAssessment(
        area="severity",
        assessed=True,
        severity="unknown",
        scope="to research",
        updated_at=NOW,
    )
    impacts[2] = ImpactAssessment(area="scale", assessed=False)
    report = build_preliminary_report(
        case_now(), complete_record(), [], impacts, [], False, NOW
    )

    missing = {(item["field"], item["state"]) for item in report["missing_fields"]}
    assert ("contact", "missing") in missing
    assert ("awareness_events", "missing") in missing
    assert ("impacts.severity", "unknown") in missing
    assert ("impacts.scale", "missing") in missing
    assert report["contact"] == {"status": "missing"}


# --- with a database ------------------------------------------------------------------------------------------------


@pytest.fixture
def record_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {
        "reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
    }
    yield people
    api.app.dependency_overrides.clear()


@requires_database
def test_a_partial_classification_is_stored_and_the_report_still_lists_its_gaps(
    record_people,
):
    case_id = new_case(record_people["reporter"])
    triager = record_people["triager"]
    body = classification_body(0)
    body["containment"] = None
    body["evidence_gap_plan"] = None

    saved = triager.put(
        f"/api/v1/cases/{case_id}/classification", json=body, headers=idem()
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1

    response = triager.get(f"/api/v1/cases/{case_id}/preliminary-report")
    assert response.status_code == 200
    report = response.json()
    assert report["complete"] is False
    assert report["classification"]["version"] == 1
    missing = {(item["field"], item["state"]) for item in report["missing_fields"]}
    assert ("classification.containment", "missing") in missing
    assert ("classification.evidence_gap_plan", "missing") in missing
    assert len(report["impacts"]) == len(IMPACT_AREAS)


@requires_database
def test_each_new_version_records_its_next_update_and_the_report_shows_the_latest(
    record_people,
):
    case_id = new_case(record_people["reporter"])
    triager = record_people["triager"]
    assert (
        triager.put(
            f"/api/v1/cases/{case_id}/classification",
            json=classification_body(0),
            headers=idem(),
        ).status_code
        == 200
    )

    later = (datetime.now(UTC) + timedelta(days=14)).replace(microsecond=0).isoformat()
    changed = classification_body(1, event_kind="incident", next_update_due_at=later)
    assert (
        triager.put(
            f"/api/v1/cases/{case_id}/classification", json=changed, headers=idem()
        ).status_code
        == 200
    )

    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    assert report["classification"]["version"] == 2
    assert datetime.fromisoformat(
        report["classification"]["next_update_due_at"]
    ) == datetime.fromisoformat(later)


@requires_database
def test_open_gaps_are_stored_as_gaps_and_every_read_lists_them(record_people):
    """Nothing fills a gap in: a missing containment stays NULL in the stored version, an impact area with no row stays
    not assessed, and an area recorded as unknown stays unknown. The report and each read show the same open items."""
    case_id = new_case(record_people["reporter"])
    triager = record_people["triager"]
    body = classification_body(0)
    body["containment"] = None
    body["evidence_gap_plan"] = None
    assert (
        triager.put(
            f"/api/v1/cases/{case_id}/classification", json=body, headers=idem()
        ).status_code
        == 200
    )
    scale = triager.put(
        f"/api/v1/cases/{case_id}/impacts/scale",
        json={"severity": "unknown", "scope": "to research"},
        headers=idem(),
    )
    assert scale.status_code == 200, scale.text

    with connect() as conn:
        stored_classification = conn.execute(
            """
            SELECT containment, evidence_gap_plan FROM case_classifications
            WHERE case_id = %s AND NOT is_deleted ORDER BY version DESC LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        stored_impacts = conn.execute(
            "SELECT area, severity FROM impacts WHERE case_id = %s AND NOT is_deleted ORDER BY area",
            (case_id,),
        ).fetchall()
    assert stored_classification == (None, None)
    assert stored_impacts == [("scale", "unknown")]

    impacts = {
        item["area"]: item
        for item in triager.get(f"/api/v1/cases/{case_id}/impacts").json()
    }
    assert set(impacts) == set(IMPACT_AREAS)
    assert (
        impacts["scale"]["assessed"] is True
        and impacts["scale"]["severity"] == "unknown"
    )
    assert all(
        not impacts[area]["assessed"] for area in IMPACT_AREAS if area != "scale"
    )

    classification = triager.get(f"/api/v1/cases/{case_id}/classification").json()
    assert (
        classification["containment"] is None
        and classification["evidence_gap_plan"] is None
    )

    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    assert report["complete"] is False
    missing = {(item["field"], item["state"]) for item in report["missing_fields"]}
    assert {
        ("classification.containment", "missing"),
        ("classification.evidence_gap_plan", "missing"),
        ("impacts.scale", "unknown"),
    } <= missing
    assert {
        (f"impacts.{area}", "missing") for area in IMPACT_AREAS if area != "scale"
    } <= missing
    # Reading again gives the same open items, so nothing was filled in by the read.
    again = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    assert again["missing_fields"] == report["missing_fields"]
