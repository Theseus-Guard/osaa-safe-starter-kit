"""F2 fixture T08-A (end to end: recipient, package, approval and notice).

One disclosure package goes through the whole path in one test:
1. a version is prepared from redacted derivatives;
2. a human approver approves it for its recipient, purpose and digest, and the release check passes;
3. a person records the notice of the send against that version;
4. a change to the package (a new version) revokes the approval for the earlier version, and the earlier notice keeps
   its own version and state;
5. no notice can be recorded against the superseded version, and the new version needs its own approval first.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
from safe_kit import disclosure
from safe_kit.disclosure import ReleaseOutcome
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (
    approve,
    body,
    create,
    dworld,
    first_version,
    open_attempt,
    read,
)  # dworld: fixture
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import payload, read_list, record, row_count


@requires_database
def test_an_approved_package_is_noticed_and_a_change_revokes_the_approval_and_refuses_the_old_notice(
    dworld,
):
    first, derived = first_version(dworld)
    assert first["recipient"] and first["purpose"] and first["digest"]

    approved = approve(
        dworld,
        "approver",
        first["id"],
        first["recipient"],
        first["purpose"],
        first["digest"],
    )
    assert approved.status_code == 201, approved.text
    open_attempt(dworld, first["id"])
    assert disclosure.check_release(first["id"]).outcome is ReleaseOutcome.RELEASABLE

    notice = record(dworld, "approver", first["id"], payload(recipient_ref="cert-07"))
    assert notice.status_code == 201, notice.text
    notice_id = notice.json()["id"]
    assert [
        (item["id"], item["state"])
        for item in read_list(dworld, "approver", first["id"]).json()["items"]
    ] == [(notice_id, "sent")]

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
    second = changed.json()

    # The change revokes the approval of the earlier version, and the release check refuses it from now on.
    assert (
        disclosure.check_release(first["id"]).outcome
        is ReleaseOutcome.REVOKED_BY_CHANGE
    )
    assert (
        read(dworld, "triager", f"/{first['id']}").json()["approval_state"]
        == "revoked_by_change"
    )

    # The notice that was already recorded keeps its version and its state: it is history, not a re-send.
    kept = read_list(dworld, "approver", first["id"]).json()["items"]
    assert [(item["id"], item["state"]) for item in kept] == [(notice_id, "sent")]

    # No new notice for the superseded version, and none for the new version until it is approved.
    old = record(dworld, "approver", first["id"], payload(recipient_ref="cert-08"))
    assert old.status_code == 409
    assert old.json()["detail"] == {"status": "version_superseded"}
    new = record(dworld, "approver", second["id"], payload(recipient_ref="cert-08"))
    assert new.status_code == 409
    assert new.json()["detail"] == {"status": "version_not_approved"}
    assert row_count(first["id"]) == 1
    assert row_count(second["id"]) == 0

    # The new version passes only with its own approval, bound to its own digest.
    own = approve(
        dworld,
        "approver",
        second["id"],
        second["recipient"],
        second["purpose"],
        second["digest"],
    )
    assert own.status_code == 201, own.text
    open_attempt(dworld, second["id"])
    assert disclosure.check_release(second["id"]).outcome is ReleaseOutcome.RELEASABLE
    assert (
        record(
            dworld, "approver", second["id"], payload(recipient_ref="cert-08")
        ).status_code
        == 201
    )
