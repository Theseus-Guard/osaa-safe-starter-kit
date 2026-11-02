"""F2 fixture T12-A (urgent notice while the approver is unavailable): the escalation path of plans/03.

When the named approver cannot record an urgent notice, the notice can go out in one of two ways, and both are recorded:
- a backup approver (another disclosures:approve holder, not the preparer) approves the version and records the notice on
  the normal path;
- an independent person (a preparer who did not prepare this version) records the notice on the manual path, with a reason.

Proved here: the manual record needs an approved version, so it cannot stand in for a missing approval; the person who
prepared the version cannot send it; the escalation is audited with its flag and the length of its reason, never the text;
the escalated notice can be followed through (receipt, follow-up) by the same person; a superseded version is refused on
the escalation path too; the case record may be incomplete, and the notice is still recorded.

Not built, so not asserted here: a separate escalation record, a recipient review record and a package review record. The
manual notice, its reason and its audit row are the only record of an escalation. No veto field exists anywhere in the
notice or approval path.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json

from safe_kit import audit
from safe_kit.db import connect
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import approve, body, create, dworld, first_version, grant  # dworld: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import (
    REASON,
    approved_version,
    notice_action,
    payload,
    receipt,
    record,
    revision_states,
    row_count,
    with_independent_triager,
)


def backup_approver(dw) -> dict:
    """A second disclosure approver, assigned to the case. The named approver is not involved."""
    backup = db_user("disclosure_approver")
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_assignments (case_id, user_id, assigned_by) VALUES (%s, %s, %s)",
            (dw["case_id"], backup, dw["people"]["triager"]),
        )
    return {**dw, "people": {**dw["people"], "backup": backup}}


def preliminary_report(dw, who: str = "triager") -> dict:
    return request_as(dw["people"][who], "get", f"/api/v1/cases/{dw['case_id']}/preliminary-report").json()


@requires_database
def test_an_urgent_notice_is_recorded_by_an_independent_person_while_the_approver_is_unavailable(dworld):
    dw = with_independent_triager(dworld)
    version, _ = approved_version(dw)
    # The case record is incomplete (no classification, no contact). That does not stop the escalation.
    assert preliminary_report(dw)["complete"] is False

    response = record(
        dw,
        "independent",
        version["id"],
        payload(
            channel="secure_manual",
            recipient_ref="cert-07",
            manual_fallback=True,
            fallback_reason="The named approver is on leave and the member must be told today",
        ),
    )
    assert response.status_code == 201, response.text
    notice = response.json()
    assert notice["state"] == "sent"
    assert notice["manual_fallback"] is True
    assert notice["channel"] == "secure_manual"
    assert notice["recorded_by"] == dw["people"]["independent"]
    assert row_count(version["id"]) == 1
    assert preliminary_report(dw)["complete"] is False


@requires_database
def test_the_escalation_is_audited_with_its_flag_and_the_length_of_its_reason_only(dworld):
    dw = with_independent_triager(dworld)
    version, _ = approved_version(dw)
    reason = "Approver unavailable; the member notice is needed before the deadline"
    notice = record(
        dw, "independent", version["id"], payload(manual_fallback=True, fallback_reason=reason)
    ).json()

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action, details::text FROM audit_events
            WHERE target_type = 'disclosure_notice' AND target_id = %s
            ORDER BY created_at, id
            """,
            (notice["id"],),
        ).fetchall()
    assert [row[0] for row in rows] == [audit.DISCLOSURE_NOTICE_RECORD]
    details = json.loads(rows[0][1])
    assert details["manual_fallback"] is True
    assert details["fallback_reason"] == {"length": len(reason)}
    assert details["repeat"] is False
    assert reason not in rows[0][1]


@requires_database
def test_a_manual_record_cannot_stand_in_for_a_missing_approval(dworld):
    dw = with_independent_triager(dworld)
    view, _ = first_version(dw)  # prepared by the triager, and nobody has approved it
    response = record(
        dw,
        "independent",
        view["id"],
        payload(manual_fallback=True, fallback_reason="Urgent, and the approver is unavailable"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "version_not_approved"}
    assert row_count(view["id"]) == 0


@requires_database
def test_the_person_who_prepared_the_version_cannot_send_it_on_the_escalation_path(dworld):
    version, _ = approved_version(dworld)
    response = record(
        dworld,
        "triager",
        version["id"],
        payload(manual_fallback=True, fallback_reason="Approver unavailable and I am the only one here"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {"status": "preparer_cannot_record"}
    assert row_count(version["id"]) == 0


@requires_database
def test_a_backup_approver_approves_and_the_notice_goes_out_on_the_normal_path(dworld):
    dw = backup_approver(with_independent_triager(dworld))
    view, _ = first_version(dw)
    approval = approve(dw, "backup", view["id"], view["recipient"], view["purpose"], view["digest"])
    assert approval.status_code == 201, approval.text

    response = record(dw, "backup", view["id"], payload(recipient_ref="cert-07"))
    assert response.status_code == 201, response.text
    assert response.json()["manual_fallback"] is False
    assert response.json()["recorded_by"] == dw["people"]["backup"]


@requires_database
def test_the_escalated_notice_is_followed_through_by_the_same_independent_person(dworld):
    dw = with_independent_triager(dworld)
    version, _ = approved_version(dw)
    notice = record(
        dw, "independent", version["id"], payload(manual_fallback=True, fallback_reason="Approver unavailable")
    ).json()

    unknown = receipt(
        dw,
        version["id"],
        notice["id"],
        {"outcome": "unknown", "reason_code": "provider_accepted_no_receipt"},
        who="independent",
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["state"] == "receipt_unknown"

    required = notice_action(
        dw, "independent", version["id"], notice["id"], "follow-up", {"action": "require", "reason": REASON}
    )
    assert required.status_code == 200, required.text
    done = notice_action(
        dw,
        "independent",
        version["id"],
        notice["id"],
        "follow-up",
        {"action": "complete", "reason": "Reached the member's security contact"},
    )
    assert done.status_code == 200, done.text
    assert revision_states(notice["id"]) == ["sent", "receipt_unknown", "follow_up_required", "follow_up_done"]


@requires_database
def test_a_superseded_version_cannot_be_sent_on_the_escalation_path_either(dworld):
    dw = with_independent_triager(dworld)
    first, derived = approved_version(dw)
    changed = create(
        dw,
        "triager",
        body([derived], package_id=first["package_id"], expected_version=1, purpose="Changed after the approval"),
    )
    assert changed.status_code == 201, changed.text

    response = record(
        dw, "independent", first["id"], payload(manual_fallback=True, fallback_reason="Approver unavailable")
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "version_superseded"}
    assert row_count(first["id"]) == 0
