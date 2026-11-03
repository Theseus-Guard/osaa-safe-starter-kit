"""F3 A8 fixture for the T12-A records of an external notice (plans/03 T12-A; plans/02 disclosure flow; plans/01 roles 5).

- Escalation: a reason (unreachable or conflict_of_interest), the authority basis, the missing facts, the next update and an
  independent backup who holds disclosures:approve. The backup may not be the preparer. The escalation changes no deadline
  task. Free text is audited by length only.
- Recipient review and package review: a review bound to the version digest. The reviewer may not have prepared the
  version, and the review needs disclosures:approve. A recipient review names the recipient by its identifier; a package
  review names none.
- Shared: a closed case refuses the records, the rows are insert-only, the database keeps the shape of each row, a repeated
  request with the same key writes nothing, and reads need disclosures:read.

The pure checks of the request bodies run without a database. The rest need PostgreSQL: they run with SAFE_TEST_DATABASE=1
and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid

import psycopg
import pydantic
import pytest

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.external_notice_records_api import (
    EscalationCreate,
    PackageReviewCreate,
    RecipientReviewCreate,
)
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    case_url,
    dworld,
    first_version,
    grant,
)
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import with_independent_triager

TEXT_LIMIT = 500
AUTHORITY = "Backup approver named in the incident response rota for this case"
MISSING = "The member's security contact is not yet confirmed by the owner"
NEXT = "A written update follows by the end of the same working day"


def escalation_values(backup: str, **changes) -> dict:
    values = {
        "reason_code": "unreachable",
        "authority_basis": AUTHORITY,
        "missing_facts": MISSING,
        "next_update": NEXT,
        "backup_user_id": backup,
    }
    values.update(changes)
    return values


def escalations_url(dw, version_id: str, suffix: str = "") -> str:
    return case_url(dw, f"/{version_id}/escalations{suffix}")


def reviews_url(dw, version_id: str, suffix: str = "") -> str:
    return case_url(dw, f"/{version_id}/reviews{suffix}")


def escalate(dw, who: str, version_id: str, values: dict, key: str | None = None):
    return request_as(
        dw["people"][who],
        "post",
        escalations_url(dw, version_id),
        json=values,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def review(dw, who: str, version_id: str, kind: str, values: dict | None = None):
    return request_as(
        dw["people"][who],
        "post",
        reviews_url(dw, version_id, f"/{kind}"),
        json={} if values is None else values,
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


def count_rows(table: str, version_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            f"SELECT count(*) FROM {table} WHERE version_id = %s AND NOT is_deleted",
            (version_id,),
        ).fetchone()[0]


def deadline_snapshot(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT kind, status, due_at FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY kind, id",
            (case_id,),
        ).fetchall()


def audit_rows(target_type: str, target_id: str) -> list[tuple[str, str]]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT action, details::text FROM audit_events
            WHERE target_type = %s AND target_id = %s
            ORDER BY created_at, id
            """,
            (target_type, target_id),
        ).fetchall()


def close_case(dw) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'done' WHERE id = %s",
            (dw["people"]["triager"], dw["case_id"]),
        )


# --- request bodies (no database) -------------------------------------------------------------------------------------


def test_an_escalation_reason_is_one_of_the_two_documented_codes():
    assert EscalationCreate(
        **escalation_values(str(uuid.uuid4()), reason_code="conflict_of_interest")
    )
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(**escalation_values(str(uuid.uuid4()), reason_code="other"))


@pytest.mark.parametrize("field", ["authority_basis", "missing_facts", "next_update"])
def test_each_escalation_text_is_required_and_bounded(field):
    values = escalation_values(str(uuid.uuid4()))
    values.pop(field)
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(**values)
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(**escalation_values(str(uuid.uuid4()), **{field: "   "}))
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(
            **escalation_values(str(uuid.uuid4()), **{field: "x" * (TEXT_LIMIT + 1)})
        )


def test_an_escalation_refuses_a_field_the_record_does_not_hold():
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(
            **escalation_values(str(uuid.uuid4())), deadline_extension_days=30
        )


def test_the_backup_of_an_escalation_is_a_user_identifier():
    with pytest.raises(pydantic.ValidationError):
        EscalationCreate(**escalation_values("not-a-user"))


def test_a_recipient_review_names_the_recipient_by_an_identifier_only():
    assert RecipientReviewCreate(recipient_ref="cert-07").recipient_ref == "cert-07"
    for reference in ("Member CERT", "security@member.example", "", "x" * 65):
        with pytest.raises(pydantic.ValidationError):
            RecipientReviewCreate(recipient_ref=reference)


def test_a_package_review_names_no_recipient():
    assert PackageReviewCreate()
    with pytest.raises(pydantic.ValidationError):
        PackageReviewCreate(recipient_ref="cert-07")


# --- escalation -----------------------------------------------------------------------------------------------------


@requires_database
def test_an_escalation_names_the_reason_the_authority_the_missing_facts_the_next_update_and_the_backup(
    dworld,
):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    backup = db_user("disclosure_approver")
    response = escalate(dw, "independent", version["id"], escalation_values(backup))
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["reason_code"] == "unreachable"
    assert created["authority_basis"] == AUTHORITY
    assert created["missing_facts"] == MISSING
    assert created["next_update"] == NEXT
    assert created["backup_user_id"] == backup
    assert created["escalated_by"] == dw["people"]["independent"]
    assert created["case_id"] == dw["case_id"]
    assert created["version_id"] == version["id"]

    listed = request_as(
        dw["people"]["approver"], "get", escalations_url(dw, version["id"])
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [created["id"]]


@requires_database
def test_an_escalation_changes_no_deadline_task(dworld):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    before = deadline_snapshot(dw["case_id"])
    response = escalate(
        dw,
        "independent",
        version["id"],
        escalation_values(
            db_user("disclosure_approver"), reason_code="conflict_of_interest"
        ),
    )
    assert response.status_code == 201, response.text
    assert deadline_snapshot(dw["case_id"]) == before


@requires_database
def test_the_person_who_prepared_the_version_cannot_name_themselves_as_backup(dworld):
    version, _ = first_version(dworld)
    response = escalate(
        dworld,
        "triager",
        version["id"],
        escalation_values(dworld["people"]["triager"]),
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"status": "preparer_cannot_be_backup"}
    assert count_rows("external_notice_escalations", version["id"]) == 0


@requires_database
def test_the_backup_must_hold_disclosures_approve_and_must_exist(dworld):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    no_approve = escalate(
        dw, "independent", version["id"], escalation_values(db_user("investigator"))
    )
    assert no_approve.status_code == 422, no_approve.text
    assert no_approve.json()["detail"] == {"status": "backup_not_approver"}
    missing = escalate(
        dw, "independent", version["id"], escalation_values(str(uuid.uuid4()))
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"] == {"status": "backup_not_found"}
    assert count_rows("external_notice_escalations", version["id"]) == 0


@requires_database
def test_roles_without_prepare_or_approve_cannot_escalate(dworld):
    version, _ = first_version(dworld)
    for who in ("investigator", "reviewer"):
        response = escalate(
            dworld,
            who,
            version["id"],
            escalation_values(db_user("disclosure_approver")),
        )
        assert response.status_code == 403, who
    assert count_rows("external_notice_escalations", version["id"]) == 0


@requires_database
def test_a_closed_case_refuses_escalations_and_reviews(dworld):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    close_case(dw)
    escalation = escalate(
        dw,
        "independent",
        version["id"],
        escalation_values(db_user("disclosure_approver")),
    )
    assert escalation.status_code == 409
    assert escalation.json()["detail"] == {"status": "case_closed"}
    recipient = review(
        dw, "approver", version["id"], "recipient", {"recipient_ref": "cert-07"}
    )
    assert recipient.status_code == 409
    assert recipient.json()["detail"] == {"status": "case_closed"}
    assert count_rows("external_notice_escalations", version["id"]) == 0
    assert count_rows("external_notice_reviews", version["id"]) == 0


@requires_database
def test_an_unknown_version_is_missing_for_an_escalation(dworld):
    response = escalate(
        dworld,
        "triager",
        str(uuid.uuid4()),
        escalation_values(db_user("disclosure_approver")),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "disclosure version not found"


@requires_database
def test_an_escalation_is_audited_with_identifiers_and_lengths_but_no_free_text(dworld):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    response = escalate(
        dw,
        "independent",
        version["id"],
        escalation_values(db_user("disclosure_approver")),
    )
    assert response.status_code == 201, response.text
    rows = audit_rows("disclosure_escalation", response.json()["id"])
    assert [row[0] for row in rows] == [audit.DISCLOSURE_ESCALATION]
    details = rows[0][1]
    for text in (AUTHORITY, MISSING, NEXT):
        assert text not in details
    assert '"authority_basis": {"length": %d}' % len(AUTHORITY) in details
    assert '"reason_code": "unreachable"' in details


@requires_database
def test_an_escalation_row_is_insert_only(dworld):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    response = escalate(
        dw,
        "independent",
        version["id"],
        escalation_values(db_user("disclosure_approver")),
    )
    assert response.status_code == 201, response.text
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute(
                "UPDATE external_notice_escalations SET reason_code = 'conflict_of_interest' WHERE id = %s",
                (response.json()["id"],),
            )
    assert count_rows("external_notice_escalations", version["id"]) == 1


@requires_database
def test_a_repeated_escalation_with_the_same_key_returns_the_first_answer_and_writes_nothing(
    dworld,
):
    dw = with_independent_triager(dworld)
    version, _ = first_version(dw)
    values = escalation_values(db_user("disclosure_approver"))
    key = uuid.uuid4().hex
    first = escalate(dw, "independent", version["id"], values, key=key)
    again = escalate(dw, "independent", version["id"], values, key=key)
    assert first.status_code == 201, first.text
    assert again.status_code == 201
    assert again.json() == first.json()
    assert count_rows("external_notice_escalations", version["id"]) == 1
    other = escalate(
        dw,
        "independent",
        version["id"],
        escalation_values(values["backup_user_id"], reason_code="conflict_of_interest"),
        key=key,
    )
    assert other.status_code == 409
    assert other.json()["detail"] == {"status": "idempotency_key_reused"}
    assert count_rows("external_notice_escalations", version["id"]) == 1


@requires_database
def test_the_escalation_read_needs_disclosures_read(dworld):
    version, _ = first_version(dworld)
    auditor = db_user("auditor")
    assert (
        request_as(auditor, "get", escalations_url(dworld, version["id"])).status_code
        == 403
    )


# --- recipient and package review -------------------------------------------------------------------------------------


@requires_database
def test_a_recipient_review_binds_the_recipient_reference_and_the_version_digest(
    dworld,
):
    version, _ = first_version(dworld)
    response = review(
        dworld, "approver", version["id"], "recipient", {"recipient_ref": "cert-07"}
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["review_kind"] == "recipient"
    assert created["recipient_ref"] == "cert-07"
    assert created["digest"] == version["digest"]
    assert created["version"] == version["version"]
    assert created["reviewed_by"] == dworld["people"]["approver"]


@requires_database
def test_a_package_review_binds_the_version_digest_and_names_no_recipient(dworld):
    version, _ = first_version(dworld)
    refused = review(
        dworld, "approver", version["id"], "package", {"recipient_ref": "cert-07"}
    )
    # A body that holds a field the record does not hold is a validation error, answered 400 like the other write routes.
    assert refused.status_code == 400
    response = review(dworld, "approver", version["id"], "package")
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["review_kind"] == "package"
    assert created["recipient_ref"] is None
    assert created["digest"] == version["digest"]


@requires_database
def test_the_person_who_prepared_the_version_cannot_review_it(dworld, monkeypatch):
    grant(monkeypatch, "triager", "disclosures:approve")
    version, _ = first_version(dworld)
    recipient = review(
        dworld, "triager", version["id"], "recipient", {"recipient_ref": "cert-07"}
    )
    assert recipient.status_code == 403
    assert recipient.json()["detail"] == {"status": "preparer_cannot_review"}
    package = review(dworld, "triager", version["id"], "package")
    assert package.status_code == 403
    assert package.json()["detail"] == {"status": "preparer_cannot_review"}
    assert count_rows("external_notice_reviews", version["id"]) == 0


@requires_database
def test_roles_without_approve_cannot_review_and_without_read_cannot_list(dworld):
    version, _ = first_version(dworld)
    for who in ("investigator", "reviewer"):
        assert review(dworld, who, version["id"], "package").status_code == 403, who
    auditor = db_user("auditor")
    assert (
        request_as(auditor, "get", reviews_url(dworld, version["id"])).status_code
        == 403
    )
    assert count_rows("external_notice_reviews", version["id"]) == 0


@requires_database
def test_the_reviews_are_listed_newest_first_and_can_be_filtered_by_kind(dworld):
    version, _ = first_version(dworld)
    recipient = review(
        dworld, "approver", version["id"], "recipient", {"recipient_ref": "cert-07"}
    )
    package = review(dworld, "approver", version["id"], "package")
    assert recipient.status_code == 201 and package.status_code == 201
    everything = request_as(
        dworld["people"]["approver"], "get", reviews_url(dworld, version["id"])
    )
    assert everything.status_code == 200, everything.text
    assert [item["id"] for item in everything.json()["items"]] == [
        package.json()["id"],
        recipient.json()["id"],
    ]
    only_package = request_as(
        dworld["people"]["approver"],
        "get",
        reviews_url(dworld, version["id"], "?kind=package"),
    )
    assert [item["review_kind"] for item in only_package.json()["items"]] == ["package"]


@requires_database
def test_a_review_row_is_insert_only_and_the_database_keeps_the_recipient_rule(dworld):
    version, _ = first_version(dworld)
    package = review(dworld, "approver", version["id"], "package")
    assert package.status_code == 201, package.text
    review_id = package.json()["id"]
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute(
                "UPDATE external_notice_reviews SET version = version + 1 WHERE id = %s",
                (review_id,),
            )
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO external_notice_reviews
                    (review_kind, case_id, package_id, version_id, version, digest, recipient_ref, reviewed_by)
                SELECT 'package', case_id, package_id, version_id, version, digest, 'cert-07', reviewed_by
                FROM external_notice_reviews WHERE id = %s
                """,
                (review_id,),
            )
    assert count_rows("external_notice_reviews", version["id"]) == 1


@requires_database
def test_a_review_is_audited_with_identifiers_and_the_digest_only(dworld):
    version, _ = first_version(dworld)
    response = review(
        dworld, "approver", version["id"], "recipient", {"recipient_ref": "cert-07"}
    )
    assert response.status_code == 201, response.text
    rows = audit_rows("disclosure_review", response.json()["id"])
    assert [row[0] for row in rows] == [audit.DISCLOSURE_REVIEW]
    assert version["digest"] in rows[0][1]
    assert "cert-07" in rows[0][1]
