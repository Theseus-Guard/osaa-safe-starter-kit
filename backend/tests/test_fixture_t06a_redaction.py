"""F2 fixture for T04 (redaction custody), T06-A (redacted scope and claims), and the T07 access negatives of audience/TLP.

- T04: a redaction makes a derived item with its own ID, leaves the original row and its object unchanged, and writes a
  custody event on the original that names the derived item and the actor. The original stays readable by its own rules.
- T06-A: the manifest lists the removed scope and the reason, not the removed text. A claim (a review layer citing the
  original) is assessed insufficient_evidence on the derived item; the review layer itself is unchanged.
- T07: RBAC, the case ACL, and the TLP audience each deny on their own. A CLEAR item needs a release approval record.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from safe_kit import (
    api,
    audit,
    cases,
    evidence,
    evidence_api,
    migrate,
    redaction,
    reviews,
    system_timezone,
    tlp,
)
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.reviews import RoundOutcome
from test_evidence import MemoryStorage, db_user, evidence_count, request_as
from test_evidence_lifecycle import (
    PDF,
    provider,
    requires_database,
)  # provider: fixture
from error_body import without_request_id

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TEXT = b"Case notes\nFirst line about the incident\nSecret detail: account 4411\nLast line\n"
REDACTED_TEXT = b"Case notes\nFirst line about the incident\n[REDACTED]\nLast line\n"
REMOVED = [{"from_line": 3, "to_line": 3, "reason": "contains an account number"}]


@pytest.fixture
def world(monkeypatch, provider):  # noqa: F811
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    people = {
        "reporter": db_user("reporter"),
        "triager": db_user("triager"),
        "investigator": db_user("investigator"),
        "other_investigator": db_user("investigator"),
        "reviewer": db_user("reviewer"),
    }
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, people["triager"])
    storage = MemoryStorage()
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: storage)
    scope = CaseScope(people["triager"], read_all=True)
    case = cases.create_case(
        "Redaction case", datetime(2026, 10, 30, 9, tzinfo=UTC), people["reporter"]
    )
    for name in ("investigator", "reviewer"):
        outcome, _ = cases.assign_member(case.id, people[name], scope)
        assert outcome is cases.Outcome.OK
    original = evidence.create_evidence(
        case.id,
        people["investigator"],
        "notes.txt",
        "text/plain",
        TEXT,
        storage,
        provider,
        scope,
    )
    # A second item of the same case that no claim cites: a pass citing only it makes no claim about the original.
    other = evidence.create_evidence(
        case.id,
        people["investigator"],
        "other.txt",
        "text/plain",
        b"Other notes\n",
        storage,
        provider,
        scope,
    )
    yield {
        "people": people,
        "case_id": case.id,
        "original_id": original.id,
        "other_id": other.id,
        "storage": storage,
        "provider": provider,
    }
    api.app.dependency_overrides.clear()


def redact(world, who, evidence_id, label="AMBER", key=None, removed=REMOVED):
    return request_as(
        world["people"][who],
        "post",
        f"/api/v1/evidence/{evidence_id}/derivatives",
        json={"removed": removed, "tlp_label": label},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def download(world, who, evidence_id):
    return request_as(
        world["people"][who], "get", f"/api/v1/evidence/{evidence_id}/download"
    )


def original_row(evidence_id):
    with connect() as conn:
        return conn.execute(
            """
            SELECT status, original_filename, media_type, size_bytes, sha256_plaintext, sha256_ciphertext,
                   storage_key, key_id, wrapped_data_key, nonce, tlp_label, created_at, updated_at
            FROM evidence_items WHERE id = %s
            """,
            (evidence_id,),
        ).fetchone()


def audit_count(action, target_id):
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s",
            (action, target_id),
        ).fetchone()[0]


def make_derived(world, label, who="investigator"):
    response = redact(world, who, world["original_id"], label)
    assert response.status_code == 201, response.text
    return response.json()


def fresh_round(world):
    scope = CaseScope(world["people"]["reviewer"], read_assigned=True)
    outcome, review, _ = reviews.open_round(world["case_id"], scope)
    assert outcome is RoundOutcome.OK
    return review, scope


# --- T04: the derived item, the unchanged original, and the custody event -------------------------------------------


@requires_database
def test_redaction_makes_a_derived_item_with_a_new_id_and_the_original_is_unchanged(
    world,
):
    before = original_row(world["original_id"])
    created = make_derived(world, "AMBER")

    derived_id = created["derived"]["id"]
    assert derived_id != world["original_id"]
    assert created["original_evidence_id"] == world["original_id"]
    assert original_row(world["original_id"]) == before
    assert download(world, "investigator", world["original_id"]).content == TEXT
    assert download(world, "investigator", derived_id).content == REDACTED_TEXT


@requires_database
def test_custody_names_the_original_the_derived_item_and_the_actor(world):
    created = make_derived(world, "AMBER")
    original_id, derived_id = world["original_id"], created["derived"]["id"]

    redacted = [
        event
        for event in evidence.custody_events(original_id)
        if event.action == evidence.REDACTED
    ]
    assert len(redacted) == 1
    assert redacted[0].actor_id == world["people"]["investigator"]
    assert redacted[0].details == {
        "derived_evidence_id": derived_id,
        "redaction_id": created["redaction_id"],
    }

    derived_events = [
        (event.action, event.actor_id) for event in evidence.custody_events(derived_id)
    ]
    assert derived_events == [("uploaded", world["people"]["investigator"])]
    assert audit_count(audit.EVIDENCE_REDACTED, derived_id) == 1


@requires_database
def test_the_derived_row_carries_the_label_and_the_original_row_has_none(world):
    created = make_derived(world, "AMBER+STRICT")
    with connect() as conn:
        derived_label = conn.execute(
            "SELECT tlp_label FROM evidence_items WHERE id = %s",
            (created["derived"]["id"],),
        ).fetchone()[0]
        original_label = conn.execute(
            "SELECT tlp_label FROM evidence_items WHERE id = %s",
            (world["original_id"],),
        ).fetchone()[0]
    assert derived_label == "AMBER+STRICT"
    assert original_label is None


@requires_database
def test_content_of_an_evidence_item_cannot_be_updated_in_the_database(world):
    for statement in (
        "UPDATE evidence_items SET original_filename = 'changed.txt' WHERE id = %s",
        "UPDATE evidence_items SET tlp_label = 'RED' WHERE id = %s",
        "UPDATE evidence_items SET sha256_plaintext = %s WHERE id = %s",
    ):
        params = (
            ("0" * 64, world["original_id"])
            if "sha256" in statement
            else (world["original_id"],)
        )
        with pytest.raises(psycopg.errors.RaiseException):
            with connect() as conn:
                conn.execute(statement, params)


# --- T06-A: manifest, redacted scope, and claims --------------------------------------------------------------------


@requires_database
def test_manifest_lists_the_removed_scope_and_reason_but_not_the_removed_text(world):
    created = make_derived(world, "AMBER")
    assert created["manifest"]["removed"] == [
        {
            "from_line": 3,
            "to_line": 3,
            "lines": 1,
            "reason": "contains an account number",
        }
    ]
    assert created["manifest"]["tlp_label"] == "AMBER"
    assert "account 4411" not in str(created)

    manifest = request_as(
        world["people"]["investigator"],
        "get",
        f"/api/v1/evidence/{created['derived']['id']}/redaction",
    )
    assert manifest.status_code == 200
    assert manifest.json()["redaction_id"] == created["redaction_id"]
    assert "account 4411" not in manifest.text


@requires_database
def test_a_claim_citing_the_original_is_insufficient_evidence_on_the_derived_item(
    world,
):
    review, scope = fresh_round(world)
    outcome, _ = reviews.set_layer(
        review.id, "model", "pass", None, [world["original_id"]], 1, scope
    )
    assert outcome is RoundOutcome.OK
    outcome, _ = reviews.set_layer(
        review.id, "instructions", "pass", None, [world["other_id"]], 2, scope
    )
    assert outcome is RoundOutcome.OK

    created = make_derived(world, "AMBER")

    assessed = {claim["layer"]: claim for claim in created["claim_assessments"]}
    assert set(assessed) == {"model"}
    assert assessed["model"]["outcome"] == "insufficient_evidence"
    assert assessed["model"]["reason"] == redaction.CLAIM_REASON
    layers = {
        entry.layer: entry.outcome
        for entry in reviews.get_round(review.id, scope).layers
    }
    assert layers["model"] == "pass"
    assert layers["instructions"] == "pass"


@requires_database
def test_a_claim_that_does_not_cite_the_original_is_not_assessed(world):
    review, scope = fresh_round(world)
    outcome, _ = reviews.set_layer(
        review.id, "tools", "pass", None, [world["other_id"]], 1, scope
    )
    assert outcome is RoundOutcome.OK
    assert make_derived(world, "AMBER")["claim_assessments"] == []


# --- refusals write nothing ----------------------------------------------------------------------------------------


@requires_database
def test_an_invalid_range_is_refused_and_writes_nothing(world):
    before_items = evidence_count(world["case_id"])
    response = redact(
        world,
        "investigator",
        world["original_id"],
        "AMBER",
        removed=[{"from_line": 40, "to_line": 40, "reason": "beyond the end"}],
    )
    assert response.status_code == 422
    assert evidence_count(world["case_id"]) == before_items
    assert [e.action for e in evidence.custody_events(world["original_id"])] == [
        "uploaded"
    ]


@requires_database
def test_only_text_evidence_is_redacted(world):
    pdf = evidence.create_evidence(
        world["case_id"],
        world["people"]["investigator"],
        "scan.pdf",
        "application/pdf",
        PDF,
        world["storage"],
        world["provider"],
        CaseScope(world["people"]["triager"], read_all=True),
    )
    response = redact(world, "investigator", pdf.id)
    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "redaction is supported for text/plain evidence only"
    )


@requires_database
def test_a_derivative_may_not_be_labelled_less_restrictively_than_its_original(world):
    derived = make_derived(world, "AMBER")
    response = redact(world, "investigator", derived["derived"]["id"], "GREEN")
    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "the TLP label must be at least as restrictive as the original's label"
    )


# --- T07: each access check denies on its own ----------------------------------------------------------------------


@requires_database
def test_rbac_alone_denies_a_derived_item_the_case_and_label_would_allow(world):
    derived = make_derived(world, "GREEN")["derived"]["id"]
    # The reporter created the case (the ACL allows it) and GREEN admits every caller (TLP allows it).
    # The reporter role has no evidence:read, so RBAC is the only check that denies.
    assert download(world, "reporter", derived).status_code == 403


@requires_database
def test_case_acl_alone_denies_a_derived_item_the_label_and_role_would_allow(world):
    derived = make_derived(world, "GREEN")["derived"]["id"]
    # The other investigator has evidence:read and the label admits everyone, but the case is not assigned to it.
    response = download(world, "other_investigator", derived)
    assert response.status_code == 404
    assert response.json()["detail"] == "evidence not found"


@requires_database
def test_tlp_alone_denies_a_derived_item_the_case_and_role_would_allow(world):
    derived = make_derived(world, "RED")["derived"]["id"]
    # The triager reads every case (ACL) and holds evidence:read (RBAC), but it is not named on the case, which RED needs.
    assert download(world, "triager", derived).status_code == 404
    # The assigned investigator is named and is allowed.
    assert download(world, "investigator", derived).status_code == 200


@requires_database
def test_amber_strict_admits_the_organisation_but_not_outside_parties(world):
    derived = make_derived(world, "AMBER+STRICT")["derived"]["id"]
    assert download(world, "triager", derived).status_code == 200


@requires_database
def test_a_clear_item_is_refused_until_a_release_approval_exists(world):
    derived = make_derived(world, "CLEAR")["derived"]["id"]
    assert download(world, "investigator", derived).status_code == 404
    listed_before = request_as(
        world["people"]["investigator"],
        "get",
        f"/api/v1/cases/{world['case_id']}/evidence",
    )
    assert derived not in [item["id"] for item in listed_before.json()["items"]]

    with pytest.raises(tlp.ReleaseNotApproved):
        tlp.record_release_approval(world["original_id"], world["people"]["triager"])

    tlp.record_release_approval(derived, world["people"]["triager"])
    assert download(world, "investigator", derived).status_code == 200


@requires_database
def test_the_sql_audience_matches_the_pure_rule_for_every_label_and_caller(world):
    derived = {
        label: make_derived(world, label)["derived"]["id"] for label in tlp.LABELS
    }
    callers = {
        # name: (user, is named on the case, is internal staff, is reporter channel)
        "investigator": ("investigator", True, True, False),
        "other_investigator": ("other_investigator", False, True, False),
        "triager": ("triager", False, True, False),
        "reporter": ("reporter", False, False, True),
    }
    scope = CaseScope(world["people"]["triager"], read_all=True)
    for label, derived_id in derived.items():
        stored = evidence.find_stored(derived_id, scope)
        for name, (who, named, organisation, external) in callers.items():
            recipient = tlp.Recipient(
                world["people"][who], organisation=organisation, external=external
            )
            expected = tlp.permits(
                label,
                named=named,
                organisation=organisation,
                external=external,
                approved=tlp.is_release_approved(derived_id),
            )
            assert tlp.item_permitted(stored, recipient) is expected, (label, name)

    recipient = tlp.Recipient(
        world["people"]["triager"], organisation=True, external=False
    )
    listed, next_cursor = evidence.list_evidence(world["case_id"], 50, None, recipient)
    visible = {item.id for item in listed}
    assert derived["RED"] not in visible
    assert derived["CLEAR"] not in visible
    assert derived["AMBER"] in visible
    assert next_cursor is None


# --- idempotency -------------------------------------------------------------------------------------------------------


@requires_database
def test_a_replayed_key_returns_the_first_answer_and_writes_nothing_again(world):
    key = uuid.uuid4().hex
    items_before = evidence_count(world["case_id"])
    first = redact(world, "investigator", world["original_id"], "AMBER", key=key)
    second = redact(world, "investigator", world["original_id"], "AMBER", key=key)

    assert first.status_code == second.status_code == 201
    assert without_request_id(second) == without_request_id(first)
    assert evidence_count(world["case_id"]) == items_before + 1
    redacted = [
        e
        for e in evidence.custody_events(world["original_id"])
        if e.action == evidence.REDACTED
    ]
    assert len(redacted) == 1


@requires_database
def test_a_key_used_with_another_request_is_refused_without_writing(world):
    key = uuid.uuid4().hex
    first = redact(world, "investigator", world["original_id"], "AMBER", key=key)
    assert first.status_code == 201
    items_before = evidence_count(world["case_id"])

    reused = redact(world, "investigator", world["original_id"], "GREEN", key=key)
    assert reused.status_code == 409
    assert reused.json()["detail"] == {"status": "idempotency_key_reused"}
    assert evidence_count(world["case_id"]) == items_before


@requires_database
def test_a_request_without_a_key_is_refused(world):
    response = request_as(
        world["people"]["investigator"],
        "post",
        f"/api/v1/evidence/{world['original_id']}/derivatives",
        json={"removed": REMOVED, "tlp_label": "AMBER"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_required"}
