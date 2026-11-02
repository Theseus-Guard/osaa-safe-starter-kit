"""F2 fixture for P6 (disclosure packages and human approval): the approval parts of T07, T08-A and T12-A.

- T07 (approval negatives): an approval for another recipient, purpose or digest is refused; a change after approval
  revokes it for the send check; a CLEAR package has no send without its approval; RBAC, the case ACL and the TLP
  audience each deny on their own; the preview holds no original text.
- T08-A (approval part): the preparer and the approver are different people, the rule is recorded on the approval, and
  a case the approver cannot see answers as a missing case does.
- T12-A (approval part): a human with the disclosure approval permission approves one version, bound to its recipient,
  purpose and digest, and the send check re-checks that binding before anything is sent.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db). They
reuse the redaction world of test_fixture_t06a_redaction, so the derived items come from the real redaction endpoint.
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import uuid

import psycopg
import pytest

from safe_kit import auth, disclosure, tlp
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.disclosure import ReleaseOutcome
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_t06a_redaction import make_derived, world  # world: fixture

# Text that only the original holds, or the redaction reason, or the file names. None may appear in a preview.
ORIGINAL_MARKERS = (
    "Secret detail",
    "account 4411",
    "Case notes",
    "First line about the incident",
    "notes.txt",
    "redacted-notes",
    "contains an account number",
)


def grant(monkeypatch, role: str, *permissions: str) -> None:
    """Give one role extra permissions for this test only. The seeded role matrix is not changed."""
    real = auth.permissions_for_role
    extra = frozenset(permissions)

    def patched(name: str) -> frozenset[str]:
        return real(name) | (extra if name == role else frozenset())

    monkeypatch.setattr(auth, "permissions_for_role", patched)


@pytest.fixture
def dworld(world, monkeypatch):
    """The redaction world, plus an approver who is assigned to the case. The approver role holds no case read
    permission in the seed, so the test gives it cases:read_assigned; an assignment alone does not grant a read."""
    approver = db_user("disclosure_approver")
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_assignments (case_id, user_id, assigned_by) VALUES (%s, %s, %s)",
            (world["case_id"], approver, world["people"]["triager"]),
        )
    grant(monkeypatch, "disclosure_approver", "cases:read_assigned")
    return {**world, "people": {**world["people"], "approver": approver}}


def case_url(world, suffix: str = "") -> str:
    return f"/api/v1/cases/{world['case_id']}/disclosures{suffix}"


def body(derived_ids, **changes) -> dict:
    values = {
        "template": "confidential_rapid_alert",
        "tlp_audience": "AMBER",
        "recipient": "Member CERT",
        "purpose": "Rapid alert on the affected gateway",
        "evidence_ids": list(derived_ids),
    }
    values.update(changes)
    return values


def create(world, who: str, payload: dict, key: str | None = None):
    return request_as(
        world["people"][who],
        "post",
        case_url(world),
        json=payload,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def approve(
    world,
    who: str,
    version_id: str,
    recipient: str,
    purpose: str,
    digest: str,
    key: str | None = None,
):
    return request_as(
        world["people"][who],
        "post",
        case_url(world, f"/{version_id}/approvals"),
        json={"recipient": recipient, "purpose": purpose, "digest": digest},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def read(world, who: str, suffix: str = ""):
    return request_as(world["people"][who], "get", case_url(world, suffix))


def first_version(world, label: str = "AMBER", **changes) -> tuple[dict, str]:
    derived = make_derived(world, label)["derived"]["id"]
    response = create(world, "triager", body([derived], **changes))
    assert response.status_code == 201, response.text
    return response.json(), derived


def open_attempt(world, version_id: str, purpose: str = "external_notice") -> None:
    """Commit an open release attempt for a version, so that a direct check_release can pass. In the product the release
    path writes the attempt and closes it in one transaction, inside the notice record or the export; a direct call of
    check_release has no such transaction, so these tests write the open attempt themselves."""
    attempt_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO release_attempts (id, attempt_id, state, purpose, case_id, package_id, version_id, actor_id)
            SELECT %s, %s, 'attempted', %s, case_id, package_id, id, %s FROM disclosure_versions WHERE id = %s
            """,
            (attempt_id, attempt_id, purpose, world["people"]["approver"], version_id),
        )


def version_count(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM disclosure_versions WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]


def approval_count(version_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM disclosure_approvals WHERE version_id = %s AND NOT is_deleted",
            (version_id,),
        ).fetchone()[0]


def assign(case_id: str, user_id: str, assigned_by: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_assignments (case_id, user_id, assigned_by) VALUES (%s, %s, %s)",
            (case_id, user_id, assigned_by),
        )


def unassign(case_id: str, user_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE case_assignments SET is_deleted = true, deleted_at = now() WHERE case_id = %s AND user_id = %s",
            (case_id, user_id),
        )


# --- versions: content, digest, immutability ----------------------------------------------------------------------


@requires_database
def test_a_version_holds_derivatives_only_and_its_digest_matches_its_content(dworld):
    view, derived = first_version(dworld)
    assert view["version"] == 1 and view["is_latest"] is True
    assert view["approval_state"] == "not_approved"
    assert len(view["digest"]) == 64
    assert [entry["evidence_id"] for entry in view["evidence"]] == [derived]
    assert dworld["original_id"] not in json.dumps(view)
    assert all("reason" not in entry for entry in view["manifest"]["items"])
    assert "contains an account number" not in json.dumps(view)

    stored = disclosure.get_version(
        dworld["case_id"],
        view["id"],
        CaseScope(dworld["people"]["triager"], read_all=True),
    )
    assert stored is not None
    assert disclosure.digest_of_version(stored) == view["digest"]


@requires_database
def test_a_changed_package_is_a_new_version_and_the_old_one_is_kept(dworld):
    first, derived = first_version(dworld)
    response = create(
        dworld,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=1,
            purpose="Updated purpose for the members",
        ),
    )
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["version"] == 2 and second["package_id"] == first["package_id"]
    assert second["digest"] != first["digest"]
    old = read(dworld, "triager", f"/{first['id']}").json()
    assert old["is_latest"] is False
    assert old["purpose"] == first["purpose"] and old["digest"] == first["digest"]


@requires_database
def test_an_unchanged_package_is_not_saved_as_a_new_version(dworld):
    first, derived = first_version(dworld)
    response = create(
        dworld,
        "triager",
        body([derived], package_id=first["package_id"], expected_version=1),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "no_change"
    assert version_count(dworld["case_id"]) == 1


@requires_database
def test_a_stale_expected_version_is_a_conflict_and_nothing_is_stored(dworld):
    first, derived = first_version(dworld)
    assert (
        create(
            dworld,
            "triager",
            body(
                [derived],
                package_id=first["package_id"],
                expected_version=1,
                purpose="Second text",
            ),
        ).status_code
        == 201
    )
    response = create(
        dworld,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=1,
            purpose="Third text",
        ),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "status": "version_conflict",
        "current_version": 2,
    }
    assert version_count(dworld["case_id"]) == 2


@requires_database
def test_the_stored_version_rows_are_insert_only(dworld):
    view, _ = first_version(dworld)
    for statement in (
        "UPDATE disclosure_versions SET purpose = 'changed' WHERE id = %s",
        "UPDATE disclosure_version_items SET tlp_label = 'GREEN' WHERE version_id = %s",
    ):
        with pytest.raises(psycopg.Error):
            with connect() as conn:
                conn.execute(statement, (view["id"],))


# --- evidence: derivatives only, the audience, the TLP rule ---------------------------------------------------------


@requires_database
def test_an_original_cannot_be_listed_in_a_package(dworld):
    response = create(dworld, "triager", body([dworld["original_id"]]))
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "evidence_not_eligible"
    assert version_count(dworld["case_id"]) == 0


@requires_database
def test_an_unknown_evidence_id_is_refused_the_same_way_as_an_original(dworld):
    response = create(dworld, "triager", body([str(uuid.uuid4())]))
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "evidence_not_eligible"


@requires_database
def test_a_package_may_not_be_less_restrictive_than_one_of_its_items(dworld):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    response = create(dworld, "triager", body([derived], tlp_audience="GREEN"))
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "audience_wider_than_evidence"


@requires_database
def test_an_item_outside_the_preparer_audience_is_refused_and_inside_it_accepted(
    dworld,
):
    red = make_derived(dworld, "RED")["derived"]["id"]
    # The triager reads every case but is not assigned to this one, so a RED item is outside the audience.
    refused = create(dworld, "triager", body([red], tlp_audience="RED"))
    assert refused.status_code == 422
    assert refused.json()["detail"]["status"] == "evidence_not_eligible"

    assign(dworld["case_id"], dworld["people"]["triager"], dworld["people"]["triager"])
    accepted = create(dworld, "triager", body([red], tlp_audience="RED"))
    assert accepted.status_code == 201, accepted.text


# --- approval: binding, separation, revocation ----------------------------------------------------------------------


@requires_database
def test_an_approval_is_refused_for_another_recipient_purpose_or_digest(dworld):
    view, _ = first_version(dworld)
    wrong = [
        {"recipient": "Other CERT"},
        {"purpose": "Another purpose"},
        {"digest": "f" * 64},
    ]
    for change in wrong:
        values = {
            "recipient": view["recipient"],
            "purpose": view["purpose"],
            "digest": view["digest"],
            **change,
        }
        response = request_as(
            dworld["people"]["approver"],
            "post",
            case_url(dworld, f"/{view['id']}/approvals"),
            json=values,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 409, change
        assert response.json()["detail"]["status"] == "approval_binding_mismatch"
    assert approval_count(view["id"]) == 0
    assert (
        read(dworld, "triager", f"/{view['id']}").json()["approval_state"]
        == "not_approved"
    )


@requires_database
def test_an_approval_records_the_approver_the_preparer_and_the_separation_rule(dworld):
    view, _ = first_version(dworld)
    response = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["approval_state"] == "approved"
    approval = result["approval"]
    assert approval["approved_by"] == dworld["people"]["approver"]
    assert approval["preparer_id"] == dworld["people"]["triager"]
    assert approval["separation_rule"] == "preparer_differs_from_approver"
    assert (
        approval["digest"] == view["digest"]
        and approval["recipient"] == view["recipient"]
    )


@requires_database
def test_the_preparer_cannot_approve_their_own_version(dworld, monkeypatch):
    grant(
        monkeypatch, "triager", "disclosures:approve"
    )  # the triager would otherwise lack the permission
    view, _ = first_version(dworld)
    response = approve(
        dworld,
        "triager",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert response.status_code == 403
    assert response.json()["detail"]["status"] == "preparer_is_approver"
    assert approval_count(view["id"]) == 0


@requires_database
def test_the_database_refuses_an_approval_that_does_not_match_its_version(dworld):
    view, _ = first_version(dworld)
    with pytest.raises(psycopg.Error):  # the digest differs from the version's
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO disclosure_approvals
                    (version_id, package_id, case_id, recipient, purpose, digest, preparer_id, approved_by, separation_rule)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'preparer_differs_from_approver')
                """,
                (
                    view["id"],
                    view["package_id"],
                    view["case_id"],
                    view["recipient"],
                    view["purpose"],
                    "f" * 64,
                    view["created_by"],
                    dworld["people"]["approver"],
                ),
            )
    with pytest.raises(psycopg.Error):  # the row matches, but the preparer approves it
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO disclosure_approvals
                    (version_id, package_id, case_id, recipient, purpose, digest, preparer_id, approved_by, separation_rule)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'preparer_differs_from_approver')
                """,
                (
                    view["id"],
                    view["package_id"],
                    view["case_id"],
                    view["recipient"],
                    view["purpose"],
                    view["digest"],
                    view["created_by"],
                    view["created_by"],
                ),
            )


@requires_database
def test_only_the_latest_version_can_be_approved(dworld):
    first, derived = first_version(dworld)
    assert (
        create(
            dworld,
            "triager",
            body(
                [derived],
                package_id=first["package_id"],
                expected_version=1,
                purpose="Second text",
            ),
        ).status_code
        == 201
    )
    response = approve(
        dworld,
        "approver",
        first["id"],
        first["recipient"],
        first["purpose"],
        first["digest"],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "not_latest_version"


@requires_database
def test_a_version_is_approved_once(dworld):
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
    response = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "already_approved"
    assert approval_count(view["id"]) == 1


@requires_database
def test_a_repeated_approval_with_the_same_key_is_answered_from_the_store(dworld):
    view, _ = first_version(dworld)
    key = uuid.uuid4().hex
    first = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
        key=key,
    )
    second = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
        key=key,
    )
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["approval"]["id"] == first.json()["approval"]["id"]
    assert approval_count(view["id"]) == 1


@requires_database
def test_a_change_after_approval_revokes_it_for_the_send_check(dworld):
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
    open_attempt(dworld, first["id"])
    assert disclosure.check_release(first["id"]).outcome is ReleaseOutcome.RELEASABLE

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

    assert (
        disclosure.check_release(first["id"]).outcome
        is ReleaseOutcome.REVOKED_BY_CHANGE
    )
    assert (
        read(dworld, "triager", f"/{first['id']}").json()["approval_state"]
        == "revoked_by_change"
    )
    assert disclosure.check_release(second["id"]).outcome is ReleaseOutcome.NOT_APPROVED

    # The new version needs its own approval, bound to its own digest.
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


@requires_database
def test_a_version_without_approval_is_refused_by_the_send_check(dworld):
    view, _ = first_version(dworld)
    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.NOT_APPROVED


@requires_database
def test_a_clear_package_is_not_releasable_before_its_approval(dworld):
    clear = make_derived(dworld, "CLEAR")["derived"]["id"]
    created = create(dworld, "triager", body([clear], tlp_audience="CLEAR"))
    assert created.status_code == 201, created.text
    view = created.json()
    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.NOT_APPROVED
    assert tlp.is_release_approved(clear) is False

    response = approve(
        dworld,
        "approver",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert response.status_code == 201, response.text
    # Approving the CLEAR package records the release of its CLEAR item in the same transaction.
    assert tlp.is_release_approved(clear) is True
    open_attempt(dworld, view["id"])
    assert disclosure.check_release(view["id"]).outcome is ReleaseOutcome.RELEASABLE


# --- permissions and the case ACL -----------------------------------------------------------------------------------


@requires_database
def test_a_role_without_disclosures_prepare_cannot_create_a_version(dworld):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    for who in ("investigator", "reviewer", "approver"):
        response = create(dworld, who, body([derived]))
        assert response.status_code == 403, who
    assert version_count(dworld["case_id"]) == 0


@requires_database
def test_a_role_without_disclosures_read_cannot_read_the_package(dworld):
    view, _ = first_version(dworld)
    auditor = db_user("auditor")  # an auditor reads the audit trail, not the packages
    for suffix in ("", f"/{view['id']}", f"/{view['id']}/preview"):
        assert (
            request_as(auditor, "get", case_url(dworld, suffix)).status_code == 403
        ), suffix


@requires_database
def test_a_role_without_disclosures_approve_cannot_approve(dworld):
    view, _ = first_version(dworld)
    response = approve(
        dworld,
        "reviewer",
        view["id"],
        view["recipient"],
        view["purpose"],
        view["digest"],
    )
    assert response.status_code == 403
    assert approval_count(view["id"]) == 0


@requires_database
def test_a_case_the_caller_cannot_see_answers_as_a_missing_case(dworld, monkeypatch):
    view, _ = first_version(dworld)
    # The approver reads every case (cases:read_all, migration 0044), so the stranger is a reviewer: it holds only
    # cases:read_assigned, is not assigned to this case, and is given the disclosure permissions for this test.
    stranger = db_user("reviewer")
    grant(monkeypatch, "reviewer", "disclosures:read", "disclosures:approve")
    for suffix in ("", f"/{view['id']}", f"/{view['id']}/preview"):
        hidden = request_as(stranger, "get", case_url(dworld, suffix))
        assert hidden.status_code == 404, suffix
    missing = request_as(stranger, "get", f"/api/v1/cases/{uuid.uuid4()}/disclosures")
    hidden = request_as(stranger, "get", case_url(dworld))
    assert missing.status_code == hidden.status_code == 404
    assert missing.json() == hidden.json()


# --- the preview: structure only -------------------------------------------------------------------------------------


@requires_database
def test_the_preview_shows_the_structure_and_no_original_text(dworld):
    view, derived = first_version(dworld)
    response = read(dworld, "triager", f"/{view['id']}/preview")
    assert response.status_code == 200, response.text
    for marker in ORIGINAL_MARKERS:
        assert marker not in response.text, marker
    preview = response.json()
    assert preview["template"]["key"] == "confidential_rapid_alert"
    assert preview["template"]["sections"][0]["title"] == "Header"
    assert preview["evidence"][0]["evidence_id"] == derived
    assert preview["evidence"][0]["removed_lines"] == 1
    assert preview["withheld_count"] == 0


@requires_database
def test_the_preview_withholds_an_item_the_caller_may_not_see(dworld):
    assign(dworld["case_id"], dworld["people"]["triager"], dworld["people"]["triager"])
    red = make_derived(dworld, "RED")["derived"]["id"]
    created = create(dworld, "triager", body([red], tlp_audience="RED"))
    assert created.status_code == 201, created.text
    view = created.json()

    unassign(
        dworld["case_id"], dworld["people"]["triager"]
    )  # the triager may still read the case, but not the item
    preview = read(dworld, "triager", f"/{view['id']}/preview").json()
    assert preview["withheld_count"] == 1
    assert preview["evidence"] == [{"withheld": True}]
    assert red not in json.dumps(preview)


@requires_database
@pytest.mark.parametrize("template", list(disclosure.TEMPLATES))
def test_each_template_is_stored_and_previewed_with_its_own_structure(dworld, template):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    created = create(dworld, "triager", body([derived], template=template))
    assert created.status_code == 201, created.text
    preview = read(dworld, "triager", f"/{created.json()['id']}/preview").json()
    assert preview["template"]["key"] == template
    assert preview["template"]["title"] == disclosure.TEMPLATES[template].title


# --- idempotency and audit ------------------------------------------------------------------------------------------


@requires_database
def test_a_repeated_create_with_the_same_key_returns_the_first_answer_and_writes_nothing(
    dworld,
):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    key = uuid.uuid4().hex
    first = create(dworld, "triager", body([derived]), key=key)
    second = create(dworld, "triager", body([derived]), key=key)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert version_count(dworld["case_id"]) == 1


@requires_database
def test_a_key_used_with_another_body_is_refused_without_writing(dworld):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    key = uuid.uuid4().hex
    assert create(dworld, "triager", body([derived]), key=key).status_code == 201
    response = create(
        dworld, "triager", body([derived], purpose="Another purpose"), key=key
    )
    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "idempotency_key_reused"
    assert version_count(dworld["case_id"]) == 1


@requires_database
def test_a_create_without_a_key_or_with_a_malformed_key_is_refused(dworld):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    missing = request_as(
        dworld["people"]["triager"], "post", case_url(dworld), json=body([derived])
    )
    assert missing.status_code == 400
    assert missing.json()["detail"]["status"] == "idempotency_key_required"
    malformed = create(dworld, "triager", body([derived]), key="not a valid key")
    assert malformed.status_code == 400
    assert malformed.json()["detail"]["status"] == "idempotency_key_invalid"
    assert version_count(dworld["case_id"]) == 0


@requires_database
def test_the_audit_rows_hold_identifiers_and_counts_but_no_free_text(dworld):
    derived = make_derived(dworld, "AMBER")["derived"]["id"]
    created = create(
        dworld,
        "triager",
        body(
            [derived], recipient="Recipient marker 7731", purpose="Purpose marker 7732"
        ),
    )
    assert created.status_code == 201, created.text
    view = created.json()
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
    with connect() as conn:
        rows = conn.execute(
            "SELECT action, details::text FROM audit_events WHERE target_id = %s AND action LIKE 'disclosure.%%'",
            (view["id"],),
        ).fetchall()
    assert {row[0] for row in rows} == {
        "disclosure.version_created",
        "disclosure.approved",
    }
    for _, details in rows:
        assert "7731" not in details and "7732" not in details
