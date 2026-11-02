"""F2 fixture T15-A (evidence states the clients must tell apart).

The evidence API has four states a caller can meet, and each has its own answer:
- empty: the case is visible and holds no evidence. The list answers 200 with no items. Nothing in the answer is a
  result, so an empty case is never read as a pass.
- failed or pending (unavailable): the item is listed with its status, and a download answers 409 "evidence is not
  available". No bytes are returned and no download is recorded.
- hidden: the item or the case is not visible to the caller. It answers 404 with the same body as a missing one, and
  that body is never the "not available" answer, so the caller cannot tell a hidden item from a missing one, and
  cannot learn that a case exists.
- a load failure is a client-side state (see the frontend tests of CaseEvidence); the backend answers its own error.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime

import pytest

from safe_kit import cases
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from test_evidence import downloaded_events, evidence_world, request_as  # evidence_world: fixture
from test_evidence_lifecycle import PDF, provider, requires_database  # provider: fixture

NOT_AVAILABLE = {"detail": "evidence is not available"}


def set_status(evidence_id: str, status: str) -> None:
    """Move a stored item to another status, as a failed write or a crash would leave it. Test setup only."""
    with connect() as conn:
        conn.execute("UPDATE evidence_items SET status = %s WHERE id = %s", (status, evidence_id))


def empty_case(people: dict) -> str:
    """A case the investigator is assigned to, with no evidence in it."""
    case = cases.create_case("Empty evidence case", datetime(2026, 10, 30, 9, tzinfo=UTC), people["reporter"])
    outcome, _ = cases.assign_member(case.id, people["investigator"], CaseScope(people["triager"], read_all=True))
    assert outcome is cases.Outcome.OK
    return case.id


@requires_database
def test_an_empty_case_answers_200_with_no_items_and_no_result_field(evidence_world):
    people = evidence_world["people"]
    response = request_as(people["investigator"], "get", f"/api/v1/cases/{empty_case(people)}/evidence")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert set(body) == {"items", "total", "limit", "offset"}


@requires_database
@pytest.mark.parametrize("status", ["failed", "pending"])
def test_an_unavailable_item_is_listed_with_its_status_and_cannot_be_downloaded(evidence_world, status):
    people = evidence_world["people"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    set_status(evidence_id, status)

    listing = request_as(people["investigator"], "get", f"/api/v1/cases/{case_id}/evidence")
    assert listing.status_code == 200, listing.text
    item = next(entry for entry in listing.json()["items"] if entry["id"] == evidence_id)
    assert item["status"] == status

    download = request_as(people["investigator"], "get", f"/api/v1/evidence/{evidence_id}/download")
    assert download.status_code == 409
    assert download.json()["detail"] == NOT_AVAILABLE["detail"]
    assert download.content != PDF
    assert downloaded_events(evidence_id) == 0


@requires_database
def test_a_hidden_case_and_a_missing_case_give_the_same_404_and_never_the_not_available_answer(evidence_world):
    people = evidence_world["people"]
    other = people["other_investigator"]
    case_id = evidence_world["case_id"]
    missing_case = str(uuid.uuid4())

    hidden = request_as(other, "get", f"/api/v1/cases/{case_id}/evidence")
    absent = request_as(other, "get", f"/api/v1/cases/{missing_case}/evidence")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json()["detail"] == absent.json()["detail"] == "case not found"

    # The same caller sees a visible but empty case as an empty list, which is a different state from a 404.
    assert request_as(people["investigator"], "get", f"/api/v1/cases/{empty_case(people)}/evidence").status_code == 200


@requires_database
def test_a_hidden_item_and_a_missing_item_give_the_same_404_and_not_the_unavailable_answer(evidence_world):
    other = evidence_world["people"]["other_investigator"]
    evidence_id = evidence_world["evidence_id"]
    set_status(evidence_id, "failed")

    hidden = request_as(other, "get", f"/api/v1/evidence/{evidence_id}/download")
    absent = request_as(other, "get", f"/api/v1/evidence/{uuid.uuid4()}/download")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json()["detail"] == absent.json()["detail"] == "evidence not found"
    assert hidden.json()["detail"] != NOT_AVAILABLE["detail"]
