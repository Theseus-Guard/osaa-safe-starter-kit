"""F2 fixture T06-B (release gate and execution trace) on the disclosure package.

- No approval means no release: the release check answers NOT_APPROVED, and the notice record and the export are both
  refused. Each refusal is recorded as a refused release attempt; nothing else is stored.
- An approval without a release attempt is not a release: the check answers RELEASE_TRACE_MISSING, and the notice record
  and the export are refused with release_trace_missing. Nothing is created.
- A release goes through the release path: the notice record and the export each write an attempt before the check, and
  close it as done in the same transaction. A done attempt is the trace of that release; it does not stay open.
- A refused attempt (a time or repeat rule, a superseded version) is recorded as refused and never counts as a release.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import audit, disclosure
from safe_kit.db import connect
from safe_kit.disclosure import ReleaseOutcome
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (
    approve,
    body,
    create,
    dworld,
    first_version,
    read,
)  # dworld: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import payload, read_list, record, row_count


@pytest.fixture
def no_attempt_written(monkeypatch):
    """Fault injection: the release path writes no attempt and no outcome, so the check finds no trace. The notice record
    and the export both reach the attempt through these two functions of the disclosure module."""
    monkeypatch.setattr(
        disclosure,
        "open_release_attempt",
        lambda conn, version, purpose, actor_id: disclosure.ReleaseAttempt(
            "00000000-0000-0000-0000-000000000000", version, purpose, actor_id
        ),
    )
    monkeypatch.setattr(
        disclosure,
        "close_release_attempt",
        lambda conn, attempt, reason_code=None: None,
    )


def attempts(version_id: str) -> list[tuple[str, str, str | None]]:
    """(state, purpose, reason_code) of every attempt row of the version, as a sorted list."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT state, purpose, reason_code FROM release_attempts WHERE version_id = %s AND NOT is_deleted",
            (version_id,),
        ).fetchall()
    return sorted(rows, key=lambda row: (row[0], row[1], row[2] or ""))


def refusal_audit_count(version_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s",
            (audit.DISCLOSURE_RELEASE_REFUSED, version_id),
        ).fetchone()[0]


@requires_database
def test_no_approval_means_no_release_and_each_refusal_is_recorded_as_refused(dworld):
    view, _ = first_version(dworld)

    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.NOT_APPROVED

    notice = record(dworld, "approver", view["id"], payload())
    assert notice.status_code == 409
    assert notice.json()["detail"] == {"status": "version_not_approved"}
    assert row_count(view["id"]) == 0

    export = read(dworld, "approver", f"/{view['id']}/export")
    assert export.status_code == 409
    assert export.json()["detail"] == {"status": "version_not_approved"}
    assert "disclosure_export" not in export.text

    # Both refused releases are kept as refused attempts; neither counts as a release.
    assert attempts(view["id"]) == sorted(
        [
            ("attempted", "external_notice", None),
            ("refused", "external_notice", "not_approved"),
            ("attempted", "export", None),
            ("refused", "export", "not_approved"),
        ],
        key=lambda row: (row[0], row[1], row[2] or ""),
    )


@requires_database
def test_an_approval_without_a_release_attempt_is_refused_by_the_check(dworld):
    view, _ = first_version(dworld)
    approved = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert approved.status_code == 201, approved.text

    # The approval matches the version, but no release attempt is open: the check refuses, and it writes nothing.
    assert (
        disclosure.check_release(view["id"]).outcome
        is ReleaseOutcome.RELEASE_TRACE_MISSING
    )
    assert attempts(view["id"]) == []
    assert read_list(dworld, "approver", view["id"]).json()["items"] == []


@requires_database
def test_the_notice_record_and_the_export_refuse_without_a_trace(
    dworld, no_attempt_written
):
    view, _ = first_version(dworld)
    assert (
        approve(
            dworld,
            "approver",
            view["id"],
            view["recipient"],
            view["purpose"],
            view["digest"],
        ).status_code
        == 201
    )

    notice = record(dworld, "approver", view["id"], payload())
    assert notice.status_code == 409, notice.text
    assert notice.json()["detail"] == {"status": "release_trace_missing"}
    assert row_count(view["id"]) == 0
    assert read_list(dworld, "approver", view["id"]).json()["items"] == []

    export = read(dworld, "approver", f"/{view['id']}/export")
    assert export.status_code == 409, export.text
    assert export.json()["detail"] == {"status": "release_trace_missing"}
    assert "disclosure_export" not in export.text


@requires_database
def test_a_release_attempt_recorded_by_the_release_path_lets_the_release_through(
    dworld,
):
    view, _ = first_version(dworld)
    assert (
        approve(
            dworld,
            "approver",
            view["id"],
            view["recipient"],
            view["purpose"],
            view["digest"],
        ).status_code
        == 201
    )

    notice = record(dworld, "approver", view["id"], payload())
    assert notice.status_code == 201, notice.text
    assert [
        item["state"]
        for item in read_list(dworld, "approver", view["id"]).json()["items"]
    ] == ["sent"]
    assert attempts(view["id"]) == sorted(
        [("attempted", "external_notice", None), ("done", "external_notice", None)],
        key=lambda row: (row[0], row[1], row[2] or ""),
    )

    # The export is a release too: it has its own attempt, and it goes through the same check.
    export = read(dworld, "approver", f"/{view['id']}/export")
    assert export.status_code == 200, export.text
    assert export.json()["disclosure_export"]["status"] == "approved"
    assert len(attempts(view["id"])) == 4

    # A done attempt does not stay open: a direct check now needs a new attempt.
    assert (
        disclosure.check_release(view["id"]).outcome
        is ReleaseOutcome.RELEASE_TRACE_MISSING
    )


@requires_database
def test_a_refused_attempt_is_recorded_as_refused_and_never_counts_as_a_release(dworld):
    view, _ = first_version(dworld)
    assert (
        approve(
            dworld,
            "approver",
            view["id"],
            view["recipient"],
            view["purpose"],
            view["digest"],
        ).status_code
        == 201
    )

    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    refused = record(dworld, "approver", view["id"], payload(sent_at=yesterday))
    assert refused.status_code == 422
    assert refused.json()["detail"] == {"status": "time_out_of_range"}

    assert attempts(view["id"]) == sorted(
        [
            ("attempted", "external_notice", None),
            ("refused", "external_notice", "time_out_of_range"),
        ],
        key=lambda row: (row[0], row[1], row[2] or ""),
    )
    assert refusal_audit_count(view["id"]) == 1
    assert row_count(view["id"]) == 0
    assert (
        disclosure.check_release(view["id"]).outcome
        is ReleaseOutcome.RELEASE_TRACE_MISSING
    )

    # A later, valid release opens its own attempt and goes through.
    assert record(dworld, "approver", view["id"], payload()).status_code == 201
    assert row_count(view["id"]) == 1


@requires_database
def test_a_change_after_approval_still_revokes_the_release_and_the_export_stays_history(
    dworld,
):
    first, derived = first_version(dworld)
    assert (
        approve(
            dworld,
            "approver",
            first["id"],
            first["recipient"],
            first["purpose"],
            first["digest"],
        ).status_code
        == 201
    )

    changed = create(
        dworld,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=1,
            purpose="Changed after the approval",
        ),
    )
    assert changed.status_code == 201, changed.text

    notice = record(dworld, "approver", first["id"], payload())
    assert notice.status_code == 409
    assert notice.json()["detail"] == {"status": "version_superseded"}
    assert row_count(first["id"]) == 0

    # The superseded version is still exported as history, with its status marked; that export is a release attempt too.
    export = read(dworld, "approver", f"/{first['id']}/export")
    assert export.status_code == 200, export.text
    assert export.json()["disclosure_export"]["status"] == "superseded"

    assert attempts(first["id"]) == sorted(
        [
            ("attempted", "external_notice", None),
            ("refused", "external_notice", "revoked_by_change"),
            ("attempted", "export", None),
            ("done", "export", None),
        ],
        key=lambda row: (row[0], row[1], row[2] or ""),
    )
