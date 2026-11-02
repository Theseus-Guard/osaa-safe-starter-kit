"""F2 fixture T06-B (release gate and execution trace) on the disclosure package.

- No approval means no release: the release check answers NOT_APPROVED, no notice can be recorded for the version, and
  its export is refused. Nothing is stored.
- An approval without an execution trace is not a release: the release check passes (it is a gate, not a proof), but no
  notice is recorded, the notice list is empty, and the version shows no sent state. The trace exists only once a person
  records the send, and recording it does not consume the approval.

The application never sends, so the send itself is outside the check. The check runs before the trace is written
(external_notices.record_notice calls it), so it cannot refuse a version for a missing trace; the trace is what shows
that the release happened.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
from safe_kit import disclosure
from safe_kit.disclosure import ReleaseOutcome
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import approve, dworld, first_version, read  # dworld: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import payload, read_list, record, row_count


@requires_database
def test_no_approval_means_no_release_and_no_trace_can_be_recorded(dworld):
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


@requires_database
def test_an_approval_without_an_execution_trace_is_not_a_release(dworld):
    view, _ = first_version(dworld)
    approved = approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"])
    assert approved.status_code == 201, approved.text

    # The gate passes: the approval matches the version, so a send may be prepared.
    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.RELEASABLE

    # No send has been recorded, so no release has happened: no trace, no sent state, no notice row.
    assert read_list(dworld, "approver", view["id"]).json()["items"] == []
    assert row_count(view["id"]) == 0


@requires_database
def test_a_recorded_trace_follows_the_approval_and_does_not_consume_it(dworld):
    view, _ = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code == 201
    )

    traced = record(dworld, "approver", view["id"], payload())
    assert traced.status_code == 201, traced.text

    listed = read_list(dworld, "approver", view["id"]).json()["items"]
    assert [item["state"] for item in listed] == ["sent"]
    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.RELEASABLE
