"""Disclosure packages, without a database: the templates, the canonical digest, the manifest content, the request rules,
and the approval state of a version. The database tests are in test_fixture_p6_disclosure.py.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safe_kit import disclosure, disclosure_api, tlp
from safe_kit.disclosure import Approval, Item, Version

CASE_ID = "11111111-2222-4333-8444-555555555555"
PACKAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000001"
VERSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000002"
PREPARER = "aaaaaaaa-bbbb-4ccc-8ddd-0000000000aa"
APPROVER = "aaaaaaaa-bbbb-4ccc-8ddd-0000000000bb"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def item(evidence_id: str, label: str = "AMBER", sha: str = "c" * 64) -> Item:
    return Item(
        evidence_id=evidence_id,
        redaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "redaction-" + evidence_id)),
        media_type="text/plain",
        size_bytes=42,
        sha256_plaintext=sha,
        tlp_label=label,
    )


ITEM_ONE = "00000000-0000-4000-8000-000000000001"
ITEM_TWO = "00000000-0000-4000-8000-000000000002"


def manifest_for(items: list[Item]) -> dict:
    return {
        "items": [
            {
                "evidence_id": entry.evidence_id,
                "redaction_id": entry.redaction_id,
                "source_sha256": "d" * 64,
                "lines_total": 4,
                "removed_lines": 1,
                "removed": [{"from_line": 3, "to_line": 3, "lines": 1}],
            }
            for entry in sorted(items, key=lambda value: value.evidence_id)
        ]
    }


def digest(**changes) -> str:
    items = changes.pop("items", [item(ITEM_ONE), item(ITEM_TWO)])
    values = {
        "case_id": CASE_ID,
        "template": "confidential_rapid_alert",
        "tlp_audience": "AMBER",
        "recipient": "Member CERT",
        "purpose": "Rapid alert on the affected gateway",
        "items": items,
        "manifest": manifest_for(items),
    }
    values.update(changes)
    content = disclosure.canonical_content(
        values["case_id"],
        values["template"],
        values["tlp_audience"],
        values["recipient"],
        values["purpose"],
        values["items"],
        values["manifest"],
    )
    return disclosure.digest_of(content)


def version(**changes) -> Version:
    items = tuple(
        sorted(
            changes.pop("items", [item(ITEM_ONE)]), key=lambda entry: entry.evidence_id
        )
    )
    base = {
        "id": VERSION_ID,
        "package_id": PACKAGE_ID,
        "case_id": CASE_ID,
        "version": 1,
        "template": "confidential_rapid_alert",
        "tlp_audience": "AMBER",
        "recipient": "Member CERT",
        "purpose": "Rapid alert on the affected gateway",
        "digest": DIGEST_A,
        "manifest": manifest_for(list(items)),
        "items": items,
        "created_by": PREPARER,
        "created_at": datetime(2026, 11, 2, 9, tzinfo=UTC),
        "is_latest": True,
        "approval": None,
    }
    base.update(changes)
    return Version(**base)


def test_there_are_exactly_the_three_templates_of_the_plan():
    assert set(disclosure.TEMPLATES) == {
        "confidential_rapid_alert",
        "de_identified_member_advisory",
        "redacted_public_safety_report",
    }
    titles = {template.title for template in disclosure.TEMPLATES.values()}
    assert titles == {
        "Confidential rapid alert",
        "De-identified member advisory",
        "Redacted public safety report",
    }
    for template in disclosure.TEMPLATES.values():
        assert template.sections, "every template names the parts of its package"


def test_the_digest_is_a_lower_case_sha256_hex_string():
    value = digest()
    assert len(value) == 64
    assert value == value.lower()
    assert all(char in "0123456789abcdef" for char in value)


def test_the_digest_does_not_depend_on_the_order_of_the_items():
    assert digest(items=[item(ITEM_ONE), item(ITEM_TWO)]) == digest(
        items=[item(ITEM_TWO), item(ITEM_ONE)]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "Another purpose"},
        {"recipient": "Other CERT"},
        {"tlp_audience": "AMBER+STRICT"},
        {"template": "redacted_public_safety_report"},
        {"case_id": "99999999-2222-4333-8444-555555555555"},
        {"items": [item(ITEM_ONE), item(ITEM_TWO, sha="e" * 64)]},
        {"items": [item(ITEM_ONE), item(ITEM_TWO, label="RED")]},
        {"items": [item(ITEM_ONE)]},
    ],
)
def test_the_digest_changes_when_any_part_of_the_content_changes(change):
    assert digest(**change) != digest()


def test_the_digest_changes_when_the_manifest_changes():
    items = [item(ITEM_ONE), item(ITEM_TWO)]
    changed = manifest_for(items)
    changed["items"][0]["removed"] = [{"from_line": 2, "to_line": 2, "lines": 1}]
    base = disclosure.canonical_content(
        CASE_ID,
        "confidential_rapid_alert",
        "AMBER",
        "Member CERT",
        "Rapid alert on the affected gateway",
        items,
        manifest_for(items),
    )
    other = disclosure.canonical_content(
        CASE_ID,
        "confidential_rapid_alert",
        "AMBER",
        "Member CERT",
        "Rapid alert on the affected gateway",
        items,
        changed,
    )
    assert disclosure.digest_of(base) != disclosure.digest_of(other)


def test_the_manifest_of_an_item_carries_line_ranges_and_never_a_reason():
    redaction_manifest = {
        "method": "replace_lines",
        "source_sha256": "d" * 64,
        "lines_total": 4,
        "removed_lines": 1,
        "removed": [
            {
                "from_line": 3,
                "to_line": 3,
                "lines": 1,
                "reason": "contains an account number",
            }
        ],
    }
    entry = disclosure._item_manifest(ITEM_ONE, "redaction-id", redaction_manifest)
    assert entry["removed"] == [{"from_line": 3, "to_line": 3, "lines": 1}]
    assert "contains an account number" not in repr(entry)
    assert "reason" not in repr(entry)


def test_a_version_without_approval_is_not_approved():
    assert version().approval_state == "not_approved"


def test_an_approved_latest_version_is_approved():
    approval = Approval(
        id=str(uuid.uuid4()),
        version_id=VERSION_ID,
        recipient="Member CERT",
        purpose="Rapid alert on the affected gateway",
        digest=DIGEST_A,
        preparer_id=PREPARER,
        approved_by=APPROVER,
        approved_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
    )
    assert version(approval=approval).approval_state == "approved"


def test_an_approval_of_an_earlier_version_is_revoked_by_change():
    approval = Approval(
        id=str(uuid.uuid4()),
        version_id=VERSION_ID,
        recipient="Member CERT",
        purpose="Rapid alert on the affected gateway",
        digest=DIGEST_A,
        preparer_id=PREPARER,
        approved_by=APPROVER,
        approved_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
    )
    assert (
        version(approval=approval, is_latest=False).approval_state
        == "revoked_by_change"
    )


def test_a_new_package_needs_no_package_id_and_no_expected_version():
    body = disclosure_api.VersionRequest.model_validate(
        {
            "template": "confidential_rapid_alert",
            "tlp_audience": "AMBER",
            "recipient": "  Member CERT  ",
            "purpose": "Rapid alert",
            "evidence_ids": [ITEM_ONE],
        }
    )
    assert body.recipient == "Member CERT"
    assert body.package_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"template": "unknown_template"},
        {"tlp_audience": "PUBLIC"},
        {"evidence_ids": []},
        {"evidence_ids": [ITEM_ONE, ITEM_ONE]},
        {"recipient": "   "},
        {"purpose": "x" * (disclosure.PURPOSE_LIMIT + 1)},
        {"unknown_field": 1},
        {"package_id": PACKAGE_ID},  # an existing package needs expected_version
        {"expected_version": 1},  # expected_version is given only with package_id
    ],
)
def test_an_invalid_version_request_is_refused(changes):
    body = {
        "template": "confidential_rapid_alert",
        "tlp_audience": "AMBER",
        "recipient": "Member CERT",
        "purpose": "Rapid alert",
        "evidence_ids": [ITEM_ONE],
    }
    body.update(changes)
    with pytest.raises(ValidationError):
        disclosure_api.VersionRequest.model_validate(body)


def test_an_approval_needs_a_sha256_digest_and_no_extra_fields():
    ok = {"recipient": "Member CERT", "purpose": "Rapid alert", "digest": DIGEST_B}
    assert disclosure_api.ApprovalRequest.model_validate(ok).digest == DIGEST_B
    with pytest.raises(ValidationError):
        disclosure_api.ApprovalRequest.model_validate({**ok, "digest": "not-a-digest"})
    with pytest.raises(ValidationError):
        disclosure_api.ApprovalRequest.model_validate(
            {**ok, "digest": DIGEST_B.upper()}
        )
    with pytest.raises(ValidationError):
        disclosure_api.ApprovalRequest.model_validate({**ok, "extra": True})


def test_the_routes_are_mounted_under_the_case_and_follow_the_api_draft():
    routes = {
        (method, route.path)
        for route in disclosure_api.disclosure_router.routes
        for method in route.methods
    }
    assert ("POST", "/api/v1/cases/{case_id}/disclosures") in routes
    assert ("GET", "/api/v1/cases/{case_id}/disclosures") in routes
    assert ("GET", "/api/v1/cases/{case_id}/disclosures/{version_id}") in routes
    assert ("GET", "/api/v1/cases/{case_id}/disclosures/{version_id}/preview") in routes
    assert (
        "POST",
        "/api/v1/cases/{case_id}/disclosures/{version_id}/approvals",
    ) in routes


def test_the_tlp_labels_of_a_package_are_the_labels_of_the_tlp_module():
    assert set(
        disclosure_api.VersionRequest.model_fields["tlp_audience"].annotation.__args__
    ) == set(tlp.LABELS)
