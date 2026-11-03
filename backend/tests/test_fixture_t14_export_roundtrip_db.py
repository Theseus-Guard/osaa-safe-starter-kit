"""F2 export roundtrip (T14, export side) over the real database, API and CLI.

Each export is read back with the reader in test_fixture_t14_export_roundtrip.py, under the F2 policy (unknown field
refused, unknown version refused, namespace duplicate refused). The checks are: the file keeps the history, the
verification summary, the approval and custody fields, and the TLP labels; and no hidden link, digest or original
content comes back.

They need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import getpass
import io
import json

import pytest

from safe_kit import cli
from safe_kit.catalog import CatalogOutcome, change_entry, digest_of, get_entry
from safe_kit.users import find_user_by_id
from test_catalog_db import (
    HIDDEN_LINK,
    OTHER_HIDDEN_LINK,
    as_user,
    entry,
    new_user,
    recommendation,
    run,
)
from test_disclosure_export_db import export_as, original_markers
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    approve,
    assign,
    body,
    case_url,
    create,
    dworld,
    first_version,
    grant,
    unassign,
)
from test_fixture_t06a_redaction import make_derived, world  # world: fixture
from test_fixture_t14_export_roundtrip import canonical, read_export

BASE_URL = "http://api.test:8000"
PASSWORD = "pw-roundtrip-" + "export-test"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def cli_as_api(monkeypatch):
    """Run the real CLI against the FastAPI test client. The API user comes from as_user(), which the caller sets
    first. The CLI's login is answered here, because token issuing is covered by the authentication tests."""
    import urllib.error
    import urllib.parse

    from fastapi.testclient import TestClient

    from safe_kit import api

    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt="": pytest.fail("getpass must not be used")
    )
    token_body = {
        "access_token": "access-roundtrip",
        "refresh_token": "refresh-roundtrip",
        "token_type": "bearer",
        "expires_in": 900,
        "password_change_required": False,
    }
    client = TestClient(api.app)

    def urlopen(request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        if (method, parts.path) == ("POST", "/api/v1/auth/token"):
            return _Response(200, json.dumps(token_body).encode("utf-8"))
        if (method, parts.path) == ("POST", "/api/v1/auth/logout"):
            return _Response(204, b"")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        response = client.request(
            method, path, headers={"Authorization": request.get_header("Authorization")}
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                "error",
                {},
                io.BytesIO(response.content),
            )
        return _Response(response.status_code, response.content)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    def run_cli(capsys, username: str, *argv: str):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))
        code = cli.main(["catalog", "export", "--username", username, *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run_cli


@requires_database
def test_a_catalog_export_reads_back_with_its_history_verification_and_status(
    monkeypatch,
):
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    run_id = run(recommendation(reviewer), reviewer)
    created = entry(reviewer, run_id=run_id, reference=HIDDEN_LINK)
    corrected = change_entry(
        created.id,
        1,
        "correction",
        {
            "title": "Corrected title",
            "body": "Corrected body",
            "reason": "wording",
            "reference": None,
        },
        reviewer,
    )
    assert corrected.outcome is CatalogOutcome.OK
    retired = entry(reviewer, reference=OTHER_HIDDEN_LINK)
    assert (
        change_entry(
            retired.id, 1, "withdrawal", {"reason": "retired"}, reviewer
        ).outcome
        is CatalogOutcome.OK
    )

    response = as_user(auditor).get("/api/v1/catalog/export")
    assert response.status_code == 200
    document = read_export(response.content, "catalog")

    assert canonical(document) == canonical(
        response.json()
    )  # the reader keeps every field it was given
    for secret in (
        HIDDEN_LINK,
        OTHER_HIDDEN_LINK,
        digest_of(HIDDEN_LINK),
        digest_of(OTHER_HIDDEN_LINK),
    ):
        assert (
            secret not in response.text
        )  # the hidden link and its digest do not come back (T11-A)

    by_id = {item["id"]: item for item in document["catalog_export"]["entries"]}
    exported = by_id[created.id]
    stored = get_entry(created.id, with_restricted=False)
    assert exported["version"] == stored.current.version == 2
    assert exported["status"] == stored.current.status == "active"
    first, second = exported["versions"]
    assert [first["operation"], second["operation"]] == ["create", "correction"]
    assert second["reason"] == "wording" and first["reason"] is None
    assert second["replacement_entry_id"] is None
    assert first["verification"]["result"] == "pass"  # the verification summary is kept
    assert first["verification"]["method"] == "replayed traffic"
    assert first["verification"]["subject"] == "gateway deny-by-default rule"
    assert (
        second["verification"] is None
    )  # the correction cites no run, so it shows none
    assert by_id[retired.id]["status"] == "withdrawn"


@requires_database
def test_the_catalog_export_file_from_the_cli_is_the_api_body(
    tmp_path, capsys, cli_as_api
):
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    entry(reviewer)
    entry(reviewer, reference=HIDDEN_LINK)
    api_body = as_user(auditor).get("/api/v1/catalog/export").content
    user = find_user_by_id(auditor)
    assert user is not None
    target = tmp_path / "catalog-export.json"

    code, out, err = cli_as_api(capsys, user.username, "--out", str(target))

    assert (code, out, err) == (0, "", "")
    assert (
        target.read_bytes() == api_body + b"\n"
    )  # the CLI file is the API body, with one trailing newline
    assert read_export(
        target.read_bytes(), "catalog"
    )  # and it reads back under the F2 policy

    code, out, err = cli_as_api(capsys, user.username)
    assert code == 0 and err == ""
    assert out == api_body.decode("utf-8") + "\n"  # printed to stdout, the same bytes
    assert HIDDEN_LINK not in out


@requires_database
def test_a_disclosure_export_reads_back_with_its_approval_custody_and_tlp(dworld):
    view, derived = first_version(dworld)
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
    response = export_as(dworld, "triager", view["id"])
    assert response.status_code == 200, response.text
    document = read_export(response.content, "disclosure")

    assert canonical(document) == canonical(response.json())
    export = document["disclosure_export"]
    assert export["digest"] == view["digest"] == export["approval"]["digest"]
    # Custody: the approver is not the preparer, and the separation rule is kept in the document.
    assert export["approval"]["approved_by"] == dworld["people"]["approver"]
    assert export["approval"]["approved_by"] != dworld["people"]["triager"]
    assert export["approval"]["separation_rule"] == "preparer_differs_from_approver"
    # ACL and TLP: the item keeps the derivative's label, and the export names only the derivative.
    assert export["tlp_audience"] == "AMBER"
    [visible] = [item for item in export["items"] if not item["withheld"]]
    assert visible["evidence_id"] == derived
    assert visible["tlp_label"] == "AMBER"
    assert export["withheld_count"] == 0
    for marker in original_markers(dworld):
        assert marker not in response.text, (
            marker
        )  # no original text, name, checksum or ID
