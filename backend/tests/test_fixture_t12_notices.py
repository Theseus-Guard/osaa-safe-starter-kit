"""F2 fixture for P8 (external notices of an approved disclosure version): T12 and T12-B.

- T12: a notice records the channel, the time sent, the recipient reference and who recorded it; it needs an approved,
  current version; a receipt, an unknown receipt, a follow-up and a cancel each move the state, with a reason where one
  is required; the history keeps every revision.
- T12-B: provider acceptance without a receipt stays unknown; a timeout or no answer is unknown, never delivered; a repeat
  notice needs a reason and is then a new record; nothing is sent or resent by the application.
- Also: cancel and the manual fallback (with reason), refusal for an unapproved or superseded version, permissions,
  idempotency, the closed case, and the database guards (insert-only rows, start only against an approved version).

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db). They
reuse the disclosure world of test_fixture_p6_disclosure, so the versions and approvals are the real ones.
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from safe_kit import audit
from safe_kit.db import connect
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    approve,
    body,
    case_url,
    create,
    dworld,
    first_version,
    grant,
)
from test_fixture_t06a_redaction import make_derived, world  # world: fixture

REASON = "Follow-up call to the member's security contact"


def sent_now() -> str:
    return datetime.now(UTC).isoformat()


def approved_version(dw, label: str = "AMBER") -> tuple[dict, str]:
    """A version of the package that is approved for its own recipient, purpose and digest, and its derivative ID."""
    view, derived = first_version(dw, label)
    response = approve(dw, "approver", view["id"], view["recipient"], view["purpose"], view["digest"])
    assert response.status_code == 201, response.text
    return response.json(), derived


def notices_url(dw, version_id: str, suffix: str = "") -> str:
    return case_url(dw, f"/{version_id}/notices{suffix}")


def payload(**changes) -> dict:
    values = {"channel": "email", "recipient_ref": "cert-07", "sent_at": sent_now()}
    values.update(changes)
    return values


def record(dw, who: str, version_id: str, values: dict, key: str | None = None):
    return request_as(
        dw["people"][who],
        "post",
        notices_url(dw, version_id),
        json=values,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def notice_action(dw, who: str, version_id: str, notice_id: str, action: str, values: dict, key: str | None = None):
    return request_as(
        dw["people"][who],
        "post",
        notices_url(dw, version_id, f"/{notice_id}/{action}"),
        json=values,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def receipt(dw, version_id: str, notice_id: str, values: dict, who: str = "approver", key: str | None = None):
    return notice_action(dw, who, version_id, notice_id, "receipt", values, key)


def read_one(dw, who: str, version_id: str, notice_id: str):
    return request_as(dw["people"][who], "get", notices_url(dw, version_id, f"/{notice_id}"))


def read_list(dw, who: str, version_id: str):
    return request_as(dw["people"][who], "get", notices_url(dw, version_id))


def recorded_notice(dw, label: str = "AMBER", **changes) -> tuple[dict, dict]:
    """An approved version with one notice recorded against it: (version view, notice view)."""
    version, _ = approved_version(dw, label)
    response = record(dw, "approver", version["id"], payload(**changes))
    assert response.status_code == 201, response.text
    return version, response.json()


def row_count(version_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM external_notices WHERE version_id = %s AND NOT is_deleted", (version_id,)
        ).fetchone()[0]


def revision_states(notice_id: str) -> list[str]:
    with connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT state FROM external_notices WHERE notice_id = %s AND NOT is_deleted ORDER BY revision",
                (notice_id,),
            ).fetchall()
        ]


def open_deadline_tasks(case_id: str) -> list[tuple[str, str]]:
    with connect() as conn:
        return conn.execute(
            "SELECT kind, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY kind, id",
            (case_id,),
        ).fetchall()


# --- T12: channel, time, state, follow-up, approval --------------------------------------------------------------------


@requires_database
def test_a_notice_is_refused_for_a_version_that_is_not_approved(dworld):
    view, _ = first_version(dworld)
    response = record(dworld, "approver", view["id"], payload())
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "version_not_approved"}
    assert row_count(view["id"]) == 0


@requires_database
def test_a_notice_is_refused_for_a_superseded_version_and_the_new_one_needs_its_own_approval(dworld):
    first, derived = approved_version(dworld)
    changed = create(
        dworld,
        "triager",
        body([derived], package_id=first["package_id"], expected_version=1, purpose="Changed after the approval"),
    )
    assert changed.status_code == 201, changed.text
    second = changed.json()

    old = record(dworld, "approver", first["id"], payload())
    assert old.status_code == 409
    assert old.json()["detail"] == {"status": "version_superseded"}
    new = record(dworld, "approver", second["id"], payload())
    assert new.status_code == 409
    assert new.json()["detail"] == {"status": "version_not_approved"}
    assert row_count(first["id"]) == 0 and row_count(second["id"]) == 0

    assert (
        approve(dworld, "approver", second["id"], second["recipient"], second["purpose"], second["digest"]).status_code
        == 201
    )
    assert record(dworld, "approver", second["id"], payload()).status_code == 201


@requires_database
def test_a_notice_records_the_channel_time_recipient_reference_and_recorder(dworld):
    version, _ = approved_version(dworld)
    sent_at = sent_now()
    response = record(
        dworld, "approver", version["id"], payload(channel="postal", recipient_ref="org.cert-07", sent_at=sent_at)
    )
    assert response.status_code == 201, response.text
    notice = response.json()
    assert notice["state"] == "sent"
    assert notice["channel"] == "postal"
    assert notice["recipient_ref"] == "org.cert-07"
    assert notice["version_id"] == version["id"]
    assert notice["version"] == 1
    assert notice["revision"] == 1
    assert notice["recorded_by"] == dworld["people"]["approver"]
    assert notice["received_at"] is None
    assert datetime.fromisoformat(notice["sent_at"]) == datetime.fromisoformat(sent_at)

    detail = read_one(dworld, "approver", version["id"], notice["id"])
    assert detail.status_code == 200
    assert [entry["state"] for entry in detail.json()["history"]] == ["sent"]


@requires_database
def test_the_channel_is_a_closed_list_and_other_needs_a_description(dworld):
    version, _ = approved_version(dworld)
    assert record(dworld, "approver", version["id"], payload(channel="fax")).status_code == 422
    assert record(dworld, "approver", version["id"], payload(channel="other")).status_code == 422
    assert record(dworld, "approver", version["id"], payload(channel="email", channel_other="x")).status_code == 422
    assert row_count(version["id"]) == 0

    response = record(dworld, "approver", version["id"], payload(channel="other", channel_other="courier by hand"))
    assert response.status_code == 201, response.text
    assert response.json()["channel_other"] == "courier by hand"


@requires_database
@pytest.mark.parametrize("reference", ["Jane Doe", "jane@example.org", "-cert", "a" * 65, ""])
def test_the_recipient_reference_is_an_identifier_not_free_text(dworld, reference):
    version, _ = approved_version(dworld)
    response = record(dworld, "approver", version["id"], payload(recipient_ref=reference))
    assert response.status_code == 422
    assert row_count(version["id"]) == 0


@requires_database
def test_the_time_sent_must_not_be_before_the_approval_or_in_the_future(dworld):
    version, _ = approved_version(dworld)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for sent_at in (yesterday, later):
        response = record(dworld, "approver", version["id"], payload(sent_at=sent_at))
        assert response.status_code == 422, sent_at
        assert response.json()["detail"] == {"status": "time_out_of_range"}
    assert row_count(version["id"]) == 0


@requires_database
def test_a_receipt_moves_the_notice_to_received_and_the_history_keeps_both_states(dworld):
    version, notice = recorded_notice(dworld)
    received_at = sent_now()
    response = receipt(dworld, version["id"], notice["id"], {"outcome": "received", "received_at": received_at})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "receipt_received"
    assert response.json()["revision"] == 2
    assert response.json()["received_at"] is not None

    detail = read_one(dworld, "approver", version["id"], notice["id"]).json()
    assert [entry["state"] for entry in detail["history"]] == ["sent", "receipt_received"]
    assert revision_states(notice["id"]) == ["sent", "receipt_received"]

    again = receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "timeout"})
    assert again.status_code == 409
    assert again.json()["detail"] == {"status": "state_conflict", "current_state": "receipt_received"}
    cancel = notice_action(dworld, "approver", version["id"], notice["id"], "cancel", {"reason": REASON})
    assert cancel.status_code == 409
    assert revision_states(notice["id"]) == ["sent", "receipt_received"]


@requires_database
def test_a_receipt_cannot_be_earlier_than_the_send(dworld):
    version, notice = recorded_notice(dworld)
    too_early = (datetime.fromisoformat(notice["sent_at"]) - timedelta(minutes=5)).isoformat()
    response = receipt(dworld, version["id"], notice["id"], {"outcome": "received", "received_at": too_early})
    assert response.status_code == 422
    assert response.json()["detail"] == {"status": "time_out_of_range"}
    assert revision_states(notice["id"]) == ["sent"]


@requires_database
def test_a_follow_up_is_required_and_then_done_and_each_step_needs_a_reason(dworld):
    version, notice = recorded_notice(dworld)
    unknown = receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "no_answer"})
    assert unknown.status_code == 200, unknown.text

    no_reason = notice_action(dworld, "approver", version["id"], notice["id"], "follow-up", {"action": "require"})
    assert no_reason.status_code == 422

    required = notice_action(
        dworld, "approver", version["id"], notice["id"], "follow-up", {"action": "require", "reason": REASON}
    )
    assert required.status_code == 200, required.text
    assert required.json()["state"] == "follow_up_required"

    done = notice_action(
        dworld, "approver", version["id"], notice["id"], "follow-up", {"action": "complete", "reason": "Reached the contact"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "follow_up_done"
    assert revision_states(notice["id"]) == ["sent", "receipt_unknown", "follow_up_required", "follow_up_done"]

    completed_twice = notice_action(
        dworld, "approver", version["id"], notice["id"], "follow-up", {"action": "complete", "reason": REASON}
    )
    assert completed_twice.status_code == 409
    assert completed_twice.json()["detail"]["current_state"] == "follow_up_done"


@requires_database
def test_a_follow_up_cannot_be_completed_before_it_is_required(dworld):
    version, notice = recorded_notice(dworld)
    response = notice_action(
        dworld, "approver", version["id"], notice["id"], "follow-up", {"action": "complete", "reason": REASON}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "state_conflict", "current_state": "sent"}


@requires_database
def test_a_cancel_needs_a_reason_and_is_refused_after_a_receipt_or_a_second_cancel(dworld):
    version, notice = recorded_notice(dworld)
    no_reason = notice_action(dworld, "approver", version["id"], notice["id"], "cancel", {})
    assert no_reason.status_code == 422

    cancelled = notice_action(dworld, "approver", version["id"], notice["id"], "cancel", {"reason": "Sent to the wrong contact"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["state_reason"] == "Sent to the wrong contact"

    second = notice_action(dworld, "approver", version["id"], notice["id"], "cancel", {"reason": REASON})
    assert second.status_code == 409
    assert second.json()["detail"]["current_state"] == "cancelled"
    assert receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "timeout"}).status_code == 409


# --- T12-B: acceptance is not delivery, timeout is unknown, no blind retry ----------------------------------------------


@requires_database
def test_provider_acceptance_without_a_receipt_stays_unknown(dworld):
    version, notice = recorded_notice(dworld, channel="webhook", recipient_ref="hook-3")
    response = receipt(
        dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "provider_accepted_no_receipt"}
    )
    assert response.status_code == 200, response.text
    body_out = response.json()
    assert body_out["state"] == "receipt_unknown"
    assert body_out["state_reason_code"] == "provider_accepted_no_receipt"
    assert body_out["received_at"] is None

    listed = read_list(dworld, "approver", version["id"]).json()["items"]
    assert [item["state"] for item in listed] == ["receipt_unknown"]
    assert "receipt_received" not in revision_states(notice["id"])


@requires_database
@pytest.mark.parametrize("reason_code", ["timeout", "no_answer"])
def test_a_timeout_or_no_answer_is_unknown_and_a_receipt_needs_its_time(dworld, reason_code):
    version, notice = recorded_notice(dworld)
    unknown = receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": reason_code})
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["state"] == "receipt_unknown"
    assert unknown.json()["state_reason_code"] == reason_code

    # A received receipt without its time, and an unknown one without its reason, are refused by the request itself.
    assert receipt(dworld, version["id"], notice["id"], {"outcome": "received"}).status_code == 422
    assert receipt(dworld, version["id"], notice["id"], {"outcome": "unknown"}).status_code == 422
    assert receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "accepted"}).status_code == 422
    assert revision_states(notice["id"]) == ["sent", "receipt_unknown"]


@requires_database
def test_a_repeat_notice_needs_a_reason_and_is_a_new_record_and_nothing_is_resent(dworld):
    version, first = recorded_notice(dworld, recipient_ref="cert-07")
    count_before = row_count(version["id"])

    repeat_without_reason = record(dworld, "approver", version["id"], payload(recipient_ref="cert-07"))
    assert repeat_without_reason.status_code == 422
    assert repeat_without_reason.json()["detail"] == {"status": "repeat_reason_required"}
    assert row_count(version["id"]) == count_before

    repeat = record(
        dworld,
        "approver",
        version["id"],
        payload(recipient_ref="cert-07", repeat_reason="The member asked for the notice to be sent again"),
    )
    assert repeat.status_code == 201, repeat.text
    assert repeat.json()["id"] != first["id"]
    assert repeat.json()["repeat_reason"] is not None

    other_recipient = record(dworld, "approver", version["id"], payload(recipient_ref="cert-08"))
    assert other_recipient.status_code == 201

    # Reading never sends or changes anything: the states stay as they were recorded.
    listed = read_list(dworld, "approver", version["id"]).json()["items"]
    assert len(listed) == 3
    assert all(item["state"] == "sent" for item in listed)
    assert revision_states(first["id"]) == ["sent"]


@requires_database
def test_a_timeout_on_a_repeat_is_a_new_unknown_record_not_a_second_delivery(dworld):
    version, first = recorded_notice(dworld)
    repeat = record(dworld, "approver", version["id"], payload(repeat_reason="Second attempt after the first timed out"))
    assert repeat.status_code == 201
    unknown = receipt(dworld, version["id"], repeat.json()["id"], {"outcome": "unknown", "reason_code": "timeout"})
    assert unknown.status_code == 200
    assert revision_states(first["id"]) == ["sent"]
    assert revision_states(repeat.json()["id"]) == ["sent", "receipt_unknown"]


# --- manual fallback, cancel and the permissions -----------------------------------------------------------------------


@requires_database
def test_the_manual_path_needs_a_reason_and_is_open_to_a_preparer_who_is_not_the_approver(dworld):
    version, _ = approved_version(dworld)
    normal = record(dworld, "triager", version["id"], payload())
    assert normal.status_code == 403
    assert normal.json()["detail"] == {"status": "manual_fallback_required"}

    no_reason = record(dworld, "triager", version["id"], payload(manual_fallback=True))
    assert no_reason.status_code == 422

    manual = record(
        dworld,
        "triager",
        version["id"],
        payload(manual_fallback=True, fallback_reason="The approver is on leave and the member needs the notice today"),
    )
    assert manual.status_code == 201, manual.text
    notice = manual.json()
    assert notice["manual_fallback"] is True
    assert notice["fallback_reason"].startswith("The approver is on leave")
    assert notice["recorded_by"] == dworld["people"]["triager"]
    assert notice["state"] == "sent"


@requires_database
def test_the_manual_path_does_not_pause_or_change_the_deadlines(dworld):
    version, _ = approved_version(dworld)
    before = open_deadline_tasks(dworld["case_id"])
    response = record(
        dworld,
        "triager",
        version["id"],
        payload(manual_fallback=True, fallback_reason="Approver unavailable during the deadline window"),
    )
    assert response.status_code == 201
    assert open_deadline_tasks(dworld["case_id"]) == before


@requires_database
def test_a_notice_is_not_an_internal_deadline_task_and_has_no_link_to_one(dworld):
    version, notice = recorded_notice(dworld)
    with connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'external_notices'"
            ).fetchall()
        }
    assert not any("deadline" in column or "task" in column for column in columns)


@requires_database
def test_roles_without_prepare_or_approve_cannot_record_or_change_a_notice(dworld):
    version, notice = recorded_notice(dworld)
    for who in ("investigator", "reviewer"):
        assert record(dworld, who, version["id"], payload()).status_code == 403, who
        assert receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "timeout"}, who=who).status_code == 403
        assert notice_action(dworld, who, version["id"], notice["id"], "cancel", {"reason": REASON}).status_code == 403
    assert revision_states(notice["id"]) == ["sent"]
    assert row_count(version["id"]) == 1


@requires_database
def test_a_role_without_disclosures_read_cannot_read_the_notices(dworld):
    version, notice = recorded_notice(dworld)
    auditor = db_user("auditor")
    assert request_as(auditor, "get", notices_url(dworld, version["id"])).status_code == 403
    assert request_as(auditor, "get", notices_url(dworld, version["id"], f"/{notice['id']}")).status_code == 403


@requires_database
def test_a_case_the_caller_cannot_see_answers_as_a_missing_case(dworld, monkeypatch):
    version, notice = recorded_notice(dworld)
    stranger = db_user("disclosure_approver")  # holds the approve permission, but is not assigned to this case
    grant(monkeypatch, "disclosure_approver", "cases:read_assigned")
    hidden_list = request_as(stranger, "get", notices_url(dworld, version["id"]))
    hidden_one = request_as(stranger, "get", notices_url(dworld, version["id"], f"/{notice['id']}"))
    hidden_write = request_as(
        stranger,
        "post",
        notices_url(dworld, version["id"]),
        json=payload(),
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    missing = request_as(stranger, "get", f"/api/v1/cases/{uuid.uuid4()}/disclosures/{version['id']}/notices")
    assert hidden_list.status_code == hidden_one.status_code == hidden_write.status_code == 404
    assert missing.status_code == 404
    assert missing.json() == hidden_list.json()
    assert row_count(version["id"]) == 1


@requires_database
def test_a_closed_case_refuses_new_notices_and_changes(dworld):
    version, notice = recorded_notice(dworld)
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'done' WHERE id = %s",
            (dworld["people"]["triager"], dworld["case_id"]),
        )
    assert record(dworld, "approver", version["id"], payload(recipient_ref="cert-09")).status_code == 409
    response = receipt(dworld, version["id"], notice["id"], {"outcome": "unknown", "reason_code": "timeout"})
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "case_closed"}
    assert revision_states(notice["id"]) == ["sent"]


# --- idempotency ------------------------------------------------------------------------------------------------------


@requires_database
def test_a_repeated_record_with_the_same_key_returns_the_first_answer_and_writes_nothing(dworld):
    version, _ = approved_version(dworld)
    key = uuid.uuid4().hex
    values = payload(recipient_ref="cert-20")
    first = record(dworld, "approver", version["id"], values, key=key)
    again = record(dworld, "approver", version["id"], values, key=key)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert row_count(version["id"]) == 1


@requires_database
def test_a_key_used_for_another_body_is_refused_without_writing(dworld):
    version, _ = approved_version(dworld)
    key = uuid.uuid4().hex
    record(dworld, "approver", version["id"], payload(recipient_ref="cert-21"), key=key)
    other = record(dworld, "approver", version["id"], payload(recipient_ref="cert-22"), key=key)
    assert other.status_code == 409
    assert other.json()["detail"] == {"status": "idempotency_key_reused"}
    assert row_count(version["id"]) == 1


@requires_database
def test_a_repeated_receipt_with_the_same_key_is_answered_from_the_store(dworld):
    version, notice = recorded_notice(dworld)
    key = uuid.uuid4().hex
    values = {"outcome": "received", "received_at": sent_now()}
    first = receipt(dworld, version["id"], notice["id"], values, key=key)
    again = receipt(dworld, version["id"], notice["id"], values, key=key)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert revision_states(notice["id"]) == ["sent", "receipt_received"]


@requires_database
def test_a_write_without_a_key_or_with_a_malformed_key_is_refused(dworld):
    version, _ = approved_version(dworld)
    no_key = request_as(dworld["people"]["approver"], "post", notices_url(dworld, version["id"]), json=payload())
    assert no_key.status_code == 400
    assert no_key.json()["detail"] == {"status": "idempotency_key_required"}
    bad_key = request_as(
        dworld["people"]["approver"],
        "post",
        notices_url(dworld, version["id"]),
        json=payload(),
        headers={"Idempotency-Key": "two words"},
    )
    assert bad_key.status_code == 400
    assert row_count(version["id"]) == 0


# --- the database guards and the audit trail --------------------------------------------------------------------------


@requires_database
def test_the_notice_rows_are_insert_only(dworld):
    version, notice = recorded_notice(dworld)
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute(
                "UPDATE external_notices SET recipient_ref = 'cert-99' WHERE notice_id = %s", (notice["id"],)
            )
    assert row_count(version["id"]) == 1


@requires_database
def test_the_database_refuses_a_notice_for_a_version_without_approval(dworld):
    view, _ = first_version(dworld)
    with pytest.raises(psycopg.Error, match="approved disclosure version"):
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO external_notices (
                    notice_id, revision, case_id, package_id, version_id, version, channel, recipient_ref,
                    sent_at, recorded_at, recorded_by, state, changed_by
                )
                VALUES (%s, 1, %s, %s, %s, 1, 'email', 'cert-01', now(), now(), %s, 'sent', %s)
                """,
                (
                    str(uuid.uuid4()),
                    dworld["case_id"],
                    view["package_id"],
                    view["id"],
                    dworld["people"]["approver"],
                    dworld["people"]["approver"],
                ),
            )
    assert row_count(view["id"]) == 0


@requires_database
def test_the_audit_rows_hold_identifiers_states_and_lengths_but_no_free_text(dworld):
    version, notice = recorded_notice(dworld)
    response = notice_action(dworld, "approver", version["id"], notice["id"], "cancel", {"reason": REASON})
    assert response.status_code == 200
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action, target_type, target_id::text, details::text
            FROM audit_events
            WHERE target_type = 'disclosure_notice' AND target_id = %s
            ORDER BY created_at, id
            """,
            (notice["id"],),
        ).fetchall()
    actions = [row[0] for row in rows]
    assert actions == [audit.DISCLOSURE_NOTICE_RECORD, audit.DISCLOSURE_NOTICE_STATE]
    assert all(REASON not in row[3] for row in rows)
    assert '"length"' in rows[1][3]
