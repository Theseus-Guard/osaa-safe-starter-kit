"""F3 A6 fixture: the backup approver of an external notice (owner decisions 2026-11-03; plans/02 disclosure flow).

- Pool: the owner (holder of disclosures:assign_backup) sets the pool of a case. At least two distinct people, each holding
  disclosures:approve. The primary approver cannot assign (403), nobody names themselves, the platform administrator holds
  no such permission. Each change is audited. A removal is a soft delete, and the pool is replaced in one transaction.
- Waiting time: the backup may decide only from backup_may_act_at (four hours by default, from the setting). Before it, the
  decision is refused with 409 backup_wait_not_over.
- Decision: approve, reject (a reason is required) or emergency_manual_notice. Recorded once, after the waiting time, by the
  named backup only, and only while the backup is still in the pool. The decision sends nothing, changes no deadline, and
  creates no approval.
- Narrow read: the named backup reads only the packet to send and the missing facts. Not the authority basis, not the next
  update, not the case title, and only while the escalation is undecided.

The body checks run without a database. The rest need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_*
settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import uuid
from datetime import datetime, timedelta

import pydantic
import pytest

from safe_kit import audit
from safe_kit import external_notice_backup as backup
from safe_kit.db import connect
from safe_kit.external_notice_backup_api import BackupDecisionCreate, PoolReplace
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (
    approval_count,
    dworld,
    first_version,
    grant,
)  # dworld: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12a_records import (
    AUTHORITY,
    MISSING,
    NEXT,
    POOL_ASSIGN,
    close_case,
    count_rows,
    deadline_snapshot,
    escalate,
    escalation_values,
    pool_url,
    with_pool,
)
from test_fixture_t12_notices import with_independent_triager

REASON = (
    "Member security contact changed; the named approver cannot confirm the new contact"
)


def decision_url(dw, version_id: str, escalation_id: str) -> str:
    return f"/api/v1/cases/{dw['case_id']}/disclosures/{version_id}/escalations/{escalation_id}/backup-decision"


def packet_url(dw, version_id: str, escalation_id: str) -> str:
    return f"/api/v1/cases/{dw['case_id']}/disclosures/{version_id}/escalations/{escalation_id}/backup-packet"


def decide(
    dw,
    user_id: str,
    version_id: str,
    escalation_id: str,
    body: dict,
    key: str | None = None,
):
    return request_as(
        user_id,
        "post",
        decision_url(dw, version_id, escalation_id),
        json=body,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def packet(dw, user_id: str, version_id: str, escalation_id: str):
    return request_as(user_id, "get", packet_url(dw, version_id, escalation_id))


def pool_put(dw, who: str, members: list[str]):
    return request_as(
        dw["people"][who], "put", pool_url(dw), json={"backup_user_ids": members}
    )


def open_escalation(dw, monkeypatch) -> tuple[dict, dict, dict]:
    """A case with a pool of two backups, one version prepared by the triager, and an escalation to the first backup."""
    dw = with_pool(dw, monkeypatch)
    version, _ = first_version(dw)
    response = escalate(
        dw, "triager", version["id"], escalation_values(dw["backups"][0])
    )
    assert response.status_code == 201, response.text
    return dw, version, response.json()


def after_the_waiting_time(monkeypatch, escalation: dict) -> None:
    """Move the backup's clock past backup_may_act_at (the decision reads utc_now)."""
    later = datetime.fromisoformat(escalation["backup_may_act_at"]) + timedelta(
        minutes=1
    )
    monkeypatch.setattr(backup, "utc_now", lambda: later)


def decisions_of(escalation_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT decision, reason, decided_by::text FROM external_notice_backup_decisions "
            "WHERE escalation_id = %s AND NOT is_deleted",
            (escalation_id,),
        ).fetchall()


def pool_rows(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT backup_user_id::text, is_deleted, deleted_at IS NOT NULL FROM external_notice_backups "
            "WHERE case_id = %s ORDER BY assigned_at, id",
            (case_id,),
        ).fetchall()


def audit_for(action: str, case_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT details::text FROM audit_events WHERE action = %s AND details->>'case_id' = %s ORDER BY created_at, id",
            (action, case_id),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


# --- request bodies (no database) -------------------------------------------------------------------------------------


def test_a_decision_is_one_of_the_three_documented_ones():
    for decision in ("approve", "emergency_manual_notice"):
        assert BackupDecisionCreate(decision=decision).reason is None
    with pytest.raises(pydantic.ValidationError):
        BackupDecisionCreate(decision="wait")


def test_a_reject_needs_a_reason():
    with pytest.raises(pydantic.ValidationError):
        BackupDecisionCreate(decision="reject")
    with pytest.raises(pydantic.ValidationError):
        BackupDecisionCreate(decision="reject", reason="   ")
    assert BackupDecisionCreate(decision="reject", reason=REASON).reason == REASON


def test_a_reason_is_bounded_and_a_decision_refuses_unknown_fields():
    with pytest.raises(pydantic.ValidationError):
        BackupDecisionCreate(decision="approve", reason="x" * (backup.REASON_LIMIT + 1))
    with pytest.raises(pydantic.ValidationError):
        BackupDecisionCreate(decision="approve", wait_longer=True)


def test_a_pool_is_a_list_of_user_identifiers_of_bounded_size():
    assert PoolReplace(backup_user_ids=[uuid.uuid4(), uuid.uuid4()])
    with pytest.raises(pydantic.ValidationError):
        PoolReplace(backup_user_ids=["not-a-user"])
    with pytest.raises(pydantic.ValidationError):
        PoolReplace(backup_user_ids=[uuid.uuid4()] * (backup.MAX_POOL + 1))


def test_the_clock_of_a_decision_is_timezone_aware():
    assert backup.utc_now().tzinfo is not None


# --- the pool (owner) -----------------------------------------------------------------------------------------------


@requires_database
def test_the_owner_sets_a_pool_of_two_backups_and_each_assignment_is_audited(
    dworld, monkeypatch
):
    grant(monkeypatch, "triager", POOL_ASSIGN)
    first, second = db_user("disclosure_approver"), db_user("disclosure_approver")
    response = pool_put(dworld, "triager", [first, second])
    assert response.status_code == 200, response.text
    assert {item["backup_user_id"] for item in response.json()["items"]} == {
        first,
        second,
    }
    assert all(
        item["assigned_by"] == dworld["people"]["triager"]
        for item in response.json()["items"]
    )

    listed = request_as(dworld["people"]["triager"], "get", pool_url(dworld))
    assert listed.status_code == 200, listed.text
    assert {item["backup_user_id"] for item in listed.json()["items"]} == {
        first,
        second,
    }

    rows = audit_for(audit.DISCLOSURE_BACKUP_ASSIGNED, dworld["case_id"])
    assert sorted(row["backup_user_id"] for row in rows) == sorted([first, second])


@requires_database
def test_a_pool_of_fewer_than_two_people_is_refused(dworld, monkeypatch):
    grant(monkeypatch, "triager", POOL_ASSIGN)
    one = db_user("disclosure_approver")
    for members in ([one], [one, one], []):
        response = pool_put(dworld, "triager", members)
        assert response.status_code == 422, (members, response.text)
        assert response.json()["detail"] == {"status": "backup_pool_too_small"}
    assert pool_rows(dworld["case_id"]) == []


@requires_database
def test_a_backup_must_hold_disclosures_approve_and_must_exist(dworld, monkeypatch):
    grant(monkeypatch, "triager", POOL_ASSIGN)
    approver = db_user("disclosure_approver")
    not_approver = pool_put(dworld, "triager", [approver, db_user("investigator")])
    assert not_approver.status_code == 422, not_approver.text
    assert not_approver.json()["detail"] == {"status": "backup_not_approver"}
    missing = pool_put(dworld, "triager", [approver, str(uuid.uuid4())])
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"] == {"status": "backup_not_found"}
    assert pool_rows(dworld["case_id"]) == []


@requires_database
def test_only_the_holder_of_assign_backup_sets_the_pool(dworld):
    members = [db_user("disclosure_approver"), db_user("disclosure_approver")]
    for who in ("triager", "investigator", "auditor"):
        user = dworld["people"].get(who) or db_user(who)
        response = request_as(
            user, "put", pool_url(dworld), json={"backup_user_ids": members}
        )
        assert response.status_code == 403, who
    admin = db_user("admin")
    response = request_as(
        admin, "put", pool_url(dworld), json={"backup_user_ids": members}
    )
    assert response.status_code == 403, "the platform administrator"
    assert pool_rows(dworld["case_id"]) == []


@requires_database
def test_the_primary_approver_cannot_choose_backups_and_nobody_names_themselves(
    dworld, monkeypatch
):
    grant(monkeypatch, "disclosure_approver", POOL_ASSIGN)
    members = [db_user("disclosure_approver"), db_user("disclosure_approver")]
    as_approver = pool_put(dworld, "approver", members)
    assert as_approver.status_code == 403, as_approver.text
    assert as_approver.json()["detail"] == {"status": "approver_cannot_assign_backup"}

    grant(monkeypatch, "triager", POOL_ASSIGN)
    self_named = pool_put(dworld, "triager", [dworld["people"]["triager"], members[0]])
    assert self_named.status_code == 422, self_named.text
    assert self_named.json()["detail"] == {"status": "assigner_cannot_be_backup"}
    assert pool_rows(dworld["case_id"]) == []


@requires_database
def test_replacing_the_pool_soft_deletes_the_removed_and_keeps_two(dworld, monkeypatch):
    grant(monkeypatch, "triager", POOL_ASSIGN)
    first, second, third = (db_user("disclosure_approver") for _ in range(3))
    assert pool_put(dworld, "triager", [first, second]).status_code == 200
    replaced = pool_put(dworld, "triager", [second, third])
    assert replaced.status_code == 200, replaced.text
    assert {item["backup_user_id"] for item in replaced.json()["items"]} == {
        second,
        third,
    }

    rows = {row[0]: row for row in pool_rows(dworld["case_id"])}
    assert rows[first][1] is True and rows[first][2] is True, (
        "the removed backup is soft deleted"
    )
    assert rows[second][1] is False and rows[third][1] is False

    below_two = pool_put(dworld, "triager", [second])
    assert below_two.status_code == 422, below_two.text
    listed = request_as(dworld["people"]["triager"], "get", pool_url(dworld))
    assert {item["backup_user_id"] for item in listed.json()["items"]} == {
        second,
        third,
    }

    readded = pool_put(dworld, "triager", [first, second])
    assert readded.status_code == 200, readded.text
    assert {item["backup_user_id"] for item in readded.json()["items"]} == {
        first,
        second,
    }

    removed = audit_for(audit.DISCLOSURE_BACKUP_REVOKED, dworld["case_id"])
    assert sorted(row["backup_user_id"] for row in removed) == sorted([first, third])


@requires_database
def test_a_closed_case_takes_no_pool_change(dworld, monkeypatch):
    grant(monkeypatch, "triager", POOL_ASSIGN)
    close_case(dworld)
    response = pool_put(
        dworld,
        "triager",
        [db_user("disclosure_approver"), db_user("disclosure_approver")],
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "case_closed"}
    assert pool_rows(dworld["case_id"]) == []


@requires_database
def test_the_pool_read_needs_disclosures_read(dworld):
    assert request_as(db_user("auditor"), "get", pool_url(dworld)).status_code == 403


# --- the waiting time, the decision ----------------------------------------------------------------------------------


@requires_database
def test_the_backup_cannot_act_before_the_waiting_time_is_over(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    response = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "backup_wait_not_over"
    assert response.json()["detail"]["may_act_at"] == escalation["backup_may_act_at"]
    assert decisions_of(escalation["id"]) == []


@requires_database
def test_the_backup_decides_after_the_waiting_time_and_the_decision_is_audited(
    dworld, monkeypatch
):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    response = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["decision"] == "approve"
    assert created["reason"] is None
    assert created["decided_by"] == dw["backups"][0]
    assert created["escalation_id"] == escalation["id"]

    rows = audit_for(audit.DISCLOSURE_BACKUP_DECISION, dw["case_id"])
    assert len(rows) == 1
    assert rows[0] == {
        "case_id": dw["case_id"],
        "version_id": version["id"],
        "escalation_id": escalation["id"],
        "decision": "approve",
        "reason": {"length": 0},
    }


@requires_database
def test_a_decision_sends_nothing_changes_no_deadline_and_creates_no_approval(
    dworld, monkeypatch
):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    before = deadline_snapshot(dw["case_id"])
    after_the_waiting_time(monkeypatch, escalation)
    response = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "emergency_manual_notice"},
    )
    assert response.status_code == 201, response.text
    assert deadline_snapshot(dw["case_id"]) == before
    assert approval_count(version["id"]) == 0
    assert count_rows("external_notices", version["id"]) == 0


@requires_database
def test_a_reject_needs_a_reason_and_the_reason_is_kept_in_its_row_only(
    dworld, monkeypatch
):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    no_reason = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "reject"}
    )
    assert no_reason.status_code == 400, no_reason.text
    blank = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "reject", "reason": "   "},
    )
    assert blank.status_code == 400, blank.text
    assert decisions_of(escalation["id"]) == []

    response = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "reject", "reason": REASON},
    )
    assert response.status_code == 201, response.text
    assert response.json()["reason"] == REASON
    assert decisions_of(escalation["id"]) == [("reject", REASON, dw["backups"][0])]
    rows = audit_for(audit.DISCLOSURE_BACKUP_DECISION, dw["case_id"])
    assert rows[0]["reason"] == {"length": len(REASON)}
    with connect() as conn:
        details = conn.execute(
            "SELECT details::text FROM audit_events WHERE action = %s AND details->>'case_id' = %s",
            (audit.DISCLOSURE_BACKUP_DECISION, dw["case_id"]),
        ).fetchall()
    assert all(REASON not in row[0] for row in details)


@requires_database
def test_a_decision_is_recorded_once(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    first = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert first.status_code == 201, first.text
    again = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "reject", "reason": REASON},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == {"status": "already_decided"}
    assert len(decisions_of(escalation["id"])) == 1


@requires_database
def test_only_the_named_backup_decides(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    other = decide(
        dw, dw["backups"][1], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert other.status_code == 403, other.text
    assert other.json()["detail"] == {"status": "not_the_backup"}
    triager = decide(
        dw,
        dw["people"]["triager"],
        version["id"],
        escalation["id"],
        {"decision": "approve"},
    )
    assert triager.status_code == 403, triager.text
    assert decisions_of(escalation["id"]) == []


@requires_database
def test_a_backup_removed_from_the_pool_cannot_decide(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    replacement = db_user("disclosure_approver")
    assert pool_put(dw, "triager", [dw["backups"][1], replacement]).status_code == 200
    response = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "backup_not_in_pool"}
    assert decisions_of(escalation["id"]) == []


@requires_database
def test_a_closed_case_refuses_the_decision(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    close_case(dw)
    response = decide(
        dw, dw["backups"][0], version["id"], escalation["id"], {"decision": "approve"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "case_closed"}
    assert decisions_of(escalation["id"]) == []


@requires_database
def test_a_decision_needs_a_key_and_a_repeat_with_the_same_key_returns_the_first_answer(
    dworld, monkeypatch
):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    no_key = request_as(
        dw["backups"][0],
        "post",
        decision_url(dw, version["id"], escalation["id"]),
        json={"decision": "approve"},
    )
    assert no_key.status_code == 400, no_key.text
    key = uuid.uuid4().hex
    first = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "approve"},
        key=key,
    )
    again = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "approve"},
        key=key,
    )
    assert first.status_code == 201, first.text
    assert again.status_code == 201
    assert again.json() == first.json()
    assert len(decisions_of(escalation["id"])) == 1


@requires_database
def test_an_unknown_escalation_is_missing_for_the_decision(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    response = decide(
        dw, dw["backups"][0], version["id"], str(uuid.uuid4()), {"decision": "approve"}
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "escalation not found"


# --- the narrow read of the backup -----------------------------------------------------------------------------------


@requires_database
def test_the_named_backup_reads_only_the_packet_and_the_missing_facts(
    dworld, monkeypatch
):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    response = packet(dw, dw["backups"][0], version["id"], escalation["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"escalation_id", "missing_facts", "may_act_at", "packet"}
    assert body["escalation_id"] == escalation["id"]
    assert body["missing_facts"] == MISSING
    assert body["may_act_at"] == escalation["backup_may_act_at"]
    assert {
        "digest",
        "recipient",
        "purpose",
        "tlp_audience",
        "evidence",
        "template",
    } <= set(body["packet"])
    assert body["packet"]["digest"] == version["digest"]

    with connect() as conn:
        title = conn.execute(
            "SELECT title FROM cases WHERE id = %s", (dw["case_id"],)
        ).fetchone()[0]
    text = response.text
    assert title not in text
    assert AUTHORITY not in text
    assert NEXT not in text


@requires_database
def test_only_the_named_backup_reads_the_packet(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    other = packet(dw, dw["backups"][1], version["id"], escalation["id"])
    assert other.status_code == 403, other.text
    assert other.json()["detail"] == {"status": "not_the_backup"}
    triager = packet(dw, dw["people"]["triager"], version["id"], escalation["id"])
    assert triager.status_code == 403, triager.text


@requires_database
def test_the_packet_is_closed_once_the_backup_has_decided(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    assert (
        decide(
            dw,
            dw["backups"][0],
            version["id"],
            escalation["id"],
            {"decision": "approve"},
        ).status_code
        == 201
    )
    response = packet(dw, dw["backups"][0], version["id"], escalation["id"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "already_decided"}


@requires_database
def test_a_packet_read_of_an_unknown_escalation_is_missing(dworld, monkeypatch):
    dw, version, escalation = open_escalation(dworld, monkeypatch)
    response = packet(dw, dw["backups"][0], version["id"], str(uuid.uuid4()))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "escalation not found"
