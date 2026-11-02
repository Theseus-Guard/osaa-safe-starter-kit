"""Redaction and its API without a database: line handling, refusals, the three access checks, and response mapping.

The database tests (new ID, custody, claims, idempotent replay, the TLP parity) are in test_fixture_t06a_redaction.py.
Database calls are replaced here, and each test says which call it replaces.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F811
from safe_kit import auth, evidence, evidence_api, redaction, tlp
from safe_kit.cases import CaseScope
from safe_kit.idempotency import StoredResponse
from test_evidence import INVESTIGATOR, MemoryStorage, stored_record
# The provider fixture is requested by parameter name; pytest needs the name in this module. Ruff sees it as unused.
from test_evidence_lifecycle import provider  # noqa: F401  # fixture: local KEK provider

TEXT = b"Case notes\nFirst line about the incident\nSecret detail: account 4411\nLast line\n"
KEY = "redaction-test-key-1"
POST_URL = "/api/v1/evidence/{}/derivatives"
REMOVE_LINE_THREE = [redaction.RemovedRange(3, 3, "contains an account number")]


def refuse(*args, **kwargs):
    raise AssertionError("the service must not run for this request")


def labelled_stored(label=None, status="available", media_type="text/plain") -> evidence.StoredEvidence:
    return evidence.StoredEvidence(
        id=str(uuid.uuid4()),
        case_id=str(uuid.uuid4()),
        original_filename="notes.txt",
        media_type=media_type,
        sha256_plaintext="0" * 64,
        sha256_ciphertext="0" * 64,
        storage_key=str(uuid.uuid4()),
        key_id="test-key",
        wrapped_data_key=b"",
        nonce=b"",
        status=status,
        tlp_label=label,
    )


# --- the line rule -----------------------------------------------------------------------------------------------------


def test_removed_lines_are_replaced_and_the_line_count_does_not_change():
    text, entries, count = redaction.redact_text(TEXT.decode(), REMOVE_LINE_THREE)
    assert text == "Case notes\nFirst line about the incident\n[REDACTED]\nLast line\n"
    assert count == 4
    assert entries == [{"from_line": 3, "to_line": 3, "lines": 1, "reason": "contains an account number"}]


def test_text_without_a_final_newline_stays_without_one():
    text, _, count = redaction.redact_text("one\ntwo\nthree", [redaction.RemovedRange(2, 3, "gone")])
    assert text == "one\n[REDACTED]\n[REDACTED]"
    assert count == 3


def test_several_ranges_are_applied_in_line_order_and_listed_in_that_order():
    ranges = [redaction.RemovedRange(4, 4, "b"), redaction.RemovedRange(1, 2, "a")]
    text, entries, _ = redaction.redact_text("1\n2\n3\n4\n5\n", ranges)
    assert text == "[REDACTED]\n[REDACTED]\n3\n[REDACTED]\n5\n"
    assert [(e["from_line"], e["to_line"], e["lines"]) for e in entries] == [(1, 2, 2), (4, 4, 1)]


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        [redaction.RemovedRange(0, 1, "a")],
        [redaction.RemovedRange(3, 2, "reversed")],
        [redaction.RemovedRange(1, 5, "past the end")],
        [redaction.RemovedRange(1, 2, "a"), redaction.RemovedRange(2, 2, "overlap")],
        [redaction.RemovedRange(1, 1, "x")] * (redaction.MAX_REMOVED_RANGES + 1),
    ],
)
def test_invalid_ranges_are_refused(ranges):
    with pytest.raises(redaction.RedactionRefused) as refusal:
        redaction.redact_text("a\nb\nc\n", ranges)
    assert refusal.value.code == "invalid_removal"


def test_manifest_never_contains_the_removed_text():
    _, entries, _ = redaction.redact_text(TEXT.decode(), REMOVE_LINE_THREE)
    assert "4411" not in repr(entries)


# --- refusals decided before any database access -----------------------------------------------------------------------


@pytest.fixture
def stored_text(provider):  # noqa: F811
    storage = MemoryStorage()
    record = stored_record(TEXT, str(uuid.uuid4()), provider, storage)
    return replace(record, media_type="text/plain", original_filename="notes.txt"), storage


def scope() -> CaseScope:
    return CaseScope(str(uuid.uuid4()), read_all=True)


def test_a_pending_original_is_not_available(stored_text, provider):  # noqa: F811
    record, storage = stored_text
    with pytest.raises(redaction.RedactionRefused) as refusal:
        redaction.create_derivative(
            replace(record, status="pending"), str(uuid.uuid4()), scope(),
            REMOVE_LINE_THREE, "AMBER", storage, provider,
        )
    assert refusal.value.code == "not_available"


def test_only_text_is_redacted(stored_text, provider):  # noqa: F811
    record, storage = stored_text
    with pytest.raises(redaction.RedactionRefused) as refusal:
        redaction.create_derivative(
            replace(record, media_type="application/pdf"), str(uuid.uuid4()), scope(),
            REMOVE_LINE_THREE, "AMBER", storage, provider,
        )
    assert refusal.value.code == "unsupported_media_type"


def test_the_derivative_may_not_be_labelled_less_restrictively_than_the_original(stored_text, provider):  # noqa: F811
    record, storage = stored_text
    with pytest.raises(redaction.RedactionRefused) as refusal:
        redaction.create_derivative(
            replace(record, tlp_label="AMBER"), str(uuid.uuid4()), scope(),
            REMOVE_LINE_THREE, "GREEN", storage, provider,
        )
    assert refusal.value.code == "label_less_restrictive"


def test_an_invalid_range_is_refused_after_the_original_is_verified(stored_text, provider):  # noqa: F811
    record, storage = stored_text
    with pytest.raises(redaction.RedactionRefused) as refusal:
        redaction.create_derivative(
            record, str(uuid.uuid4()), scope(), [redaction.RemovedRange(9, 9, "beyond")], "AMBER", storage, provider,
        )
    assert refusal.value.code == "invalid_removal"


# --- API: the three access checks, each denying alone -------------------------------------------------------------------


@pytest.fixture
def api_client(monkeypatch, provider):  # noqa: F811
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: INVESTIGATOR
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read", "evidence:write"}))
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: MemoryStorage())
    return TestClient(app)


def body(label="AMBER"):
    return {"removed": [{"from_line": 3, "to_line": 3, "reason": "contains an account number"}], "tlp_label": label}


def post(client, evidence_id=None, key=KEY, json_body=None):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        POST_URL.format(evidence_id or uuid.uuid4()), json=json_body or body(), headers=headers
    )


def allow_original(monkeypatch, label=None, status="available"):
    stored = labelled_stored(label=label, status=status)
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: True)
    return stored


def test_rbac_alone_denies_a_redaction(api_client, monkeypatch):
    allow_original(monkeypatch)
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read"}))
    monkeypatch.setattr(redaction, "create_derivative", refuse)
    assert post(api_client).status_code == 403


def test_a_read_only_caller_cannot_create_a_derivative(api_client, monkeypatch):
    allow_original(monkeypatch)
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:write"}))
    monkeypatch.setattr(redaction, "create_derivative", refuse)
    assert post(api_client).status_code == 403


def test_case_acl_alone_denies_a_redaction(api_client, monkeypatch):
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: None)
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: True)
    monkeypatch.setattr(redaction, "create_derivative", refuse)
    response = post(api_client)
    assert response.status_code == 404
    assert response.json() == {"detail": "evidence not found"}


def test_tlp_alone_denies_a_redaction(api_client, monkeypatch):
    stored = labelled_stored(label="RED")
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: False)
    monkeypatch.setattr(redaction, "create_derivative", refuse)
    response = post(api_client)
    assert response.status_code == 404
    assert response.json() == {"detail": "evidence not found"}


@pytest.mark.parametrize(
    "url_template, method",
    [("/api/v1/evidence/{}/download", "get"), ("/api/v1/evidence/{}/custody", "get")],
)
def test_each_read_check_denies_alone_on_download_and_custody(api_client, monkeypatch, url_template, method):
    # RBAC alone: the role has no evidence:read.
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:write"}))
    monkeypatch.setattr(evidence, "find_stored", refuse)
    assert getattr(api_client, method)(url_template.format(uuid.uuid4())).status_code == 403

    # Case ACL alone: the item is not visible to the case scope.
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read"}))
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: None)
    assert getattr(api_client, method)(url_template.format(uuid.uuid4())).status_code == 404

    # TLP alone: visible to the case scope, but the audience of the label excludes the caller.
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: labelled_stored(label="RED"))
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: False)
    assert getattr(api_client, method)(url_template.format(uuid.uuid4())).status_code == 404


class _RecordingCursor:
    def __init__(self, sql):
        self.sql = sql

    def fetchone(self):
        # The TLP clause for CLEAR asks for a release approval. No row means the item is not releasable.
        assert "evidence_release_approvals" in self.sql
        return None


class _RecordingConnection:
    def __init__(self, seen):
        self.seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.seen.append(sql)
        return _RecordingCursor(sql)


def test_a_clear_item_without_approval_is_not_downloadable(api_client, monkeypatch):
    seen_sql = []
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: labelled_stored(label="CLEAR"))
    monkeypatch.setattr(tlp, "connect", lambda: _RecordingConnection(seen_sql))
    response = api_client.get(f"/api/v1/evidence/{uuid.uuid4()}/download")
    assert response.status_code == 404
    assert seen_sql, "the TLP check must run for a labelled item"


def test_missing_idempotency_key_is_refused(api_client, monkeypatch):
    allow_original(monkeypatch)
    response = post(api_client, key=None)
    assert response.status_code == 400
    assert response.json() == {"detail": {"status": "idempotency_key_required"}}


def test_malformed_idempotency_key_is_refused(api_client, monkeypatch):
    allow_original(monkeypatch)
    response = post(api_client, key="has a space")
    assert response.status_code == 400
    assert response.json() == {"detail": {"status": "idempotency_key_invalid"}}


@pytest.mark.parametrize(
    "json_body",
    [
        {"removed": [], "tlp_label": "AMBER"},
        {"removed": [{"from_line": 0, "to_line": 1, "reason": "x"}], "tlp_label": "AMBER"},
        {"removed": [{"from_line": 1, "to_line": 1, "reason": "   "}], "tlp_label": "AMBER"},
        {"removed": [{"from_line": 1, "to_line": 1, "reason": "x"}], "tlp_label": "PUBLIC"},
        {"removed": [{"from_line": 1, "to_line": 1, "reason": "x"}]},
    ],
)
def test_invalid_request_bodies_are_422(api_client, monkeypatch, json_body):
    allow_original(monkeypatch)
    monkeypatch.setattr(redaction, "create_derivative", refuse)
    assert post(api_client, json_body=json_body).status_code == 422


def make_derivation(label="AMBER") -> redaction.Derivation:
    item = evidence.EvidenceItem(
        id=str(uuid.uuid4()),
        case_id=str(uuid.uuid4()),
        uploaded_by=INVESTIGATOR.id,
        original_filename="redacted-notes.txt",
        media_type="text/plain",
        size_bytes=10,
        sha256_plaintext="a" * 64,
        status="available",
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
        tlp_label=label,
    )
    claim = redaction.ClaimAssessment(
        review_layer_id=str(uuid.uuid4()),
        round_id=str(uuid.uuid4()),
        layer="model",
        outcome="insufficient_evidence",
        reason=redaction.CLAIM_REASON,
    )
    return redaction.Derivation(
        redaction_id=str(uuid.uuid4()),
        original_evidence_id=str(uuid.uuid4()),
        derived=item,
        manifest={"method": "replace_lines", "removed": [{"from_line": 3, "to_line": 3}]},
        claims=[claim],
    )


def test_a_created_derivative_is_answered_with_201_and_its_manifest(api_client, monkeypatch):
    allow_original(monkeypatch)
    derivation = make_derivation()
    seen = {}

    def fake_create(original, actor_id, scope, removed, label, storage, provider, idempotency=None, render=None):
        seen.update(removed=removed, label=label, key=idempotency.key)
        return redaction.Result(redaction.Outcome.CREATED, derivation=derivation)

    monkeypatch.setattr(redaction, "create_derivative", fake_create)
    response = post(api_client)
    assert response.status_code == 201
    result = response.json()
    assert result["derived"]["id"] == derivation.derived.id
    assert result["derived"]["tlp_label"] == "AMBER"
    assert result["manifest"] == derivation.manifest
    assert result["claim_assessments"][0]["outcome"] == "insufficient_evidence"
    assert seen["label"] == "AMBER"
    assert seen["key"] == KEY
    assert seen["removed"][0].from_line == 3


def test_a_replayed_key_returns_the_stored_answer(api_client, monkeypatch):
    allow_original(monkeypatch)
    stored_body = {"redaction_id": "r", "original_evidence_id": "o", "derived": {}, "manifest": {}, "claim_assessments": []}
    monkeypatch.setattr(
        redaction, "create_derivative",
        lambda *args, **kwargs: redaction.Result(redaction.Outcome.REPLAY, stored=StoredResponse(201, stored_body)),
    )
    response = post(api_client)
    assert response.status_code == 201
    assert response.json() == stored_body


def test_a_key_used_with_another_request_is_refused(api_client, monkeypatch):
    allow_original(monkeypatch)
    monkeypatch.setattr(
        redaction, "create_derivative", lambda *args, **kwargs: redaction.Result(redaction.Outcome.KEY_REUSED)
    )
    response = post(api_client)
    assert response.status_code == 409
    assert response.json() == {"detail": {"status": "idempotency_key_reused"}}


@pytest.mark.parametrize(
    "code, status_code",
    [("not_available", 409), ("unsupported_media_type", 422), ("label_less_restrictive", 422), ("integrity_failed", 500)],
)
def test_refusals_map_to_their_status(api_client, monkeypatch, code, status_code):
    allow_original(monkeypatch)

    def refused(*args, **kwargs):
        raise redaction.RedactionRefused(code)

    monkeypatch.setattr(redaction, "create_derivative", refused)
    assert post(api_client).status_code == status_code


def test_the_manifest_of_an_original_is_not_found(api_client, monkeypatch):
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: labelled_stored())
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: True)
    monkeypatch.setattr(redaction, "load_derivation", lambda derived_id: None)
    assert api_client.get(f"/api/v1/evidence/{uuid.uuid4()}/redaction").status_code == 404


def test_the_manifest_of_a_derived_item_is_returned(api_client, monkeypatch):
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: labelled_stored(label="AMBER"))
    monkeypatch.setattr(tlp, "item_permitted", lambda stored, recipient: True)
    monkeypatch.setattr(redaction, "load_derivation", lambda derived_id: make_derivation())
    response = api_client.get(f"/api/v1/evidence/{uuid.uuid4()}/redaction")
    assert response.status_code == 200
    assert response.json()["claim_assessments"][0]["layer"] == "model"
