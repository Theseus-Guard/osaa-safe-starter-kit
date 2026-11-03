"""F1 fixture T07 (access): the negative access cases of the F1 subset.

RBAC alone does not decide access to a case. A role permission lets a caller in, and the case ACL decides which cases
the caller sees. This file is the index of the F1 negative cases, grouped in four parts. Each part is asserted here.
The helpers and the fixtures come from the sibling test modules, so the rules are not written twice.

- Case ACL: a reporter sees only the cases it created, and the list and the search run inside that scope. An assignee-only
  investigator gets the same 404 for a case it cannot see as for a missing case.
- Search: the q parameter runs inside the caller's scope and does not reveal another reporter's case.
- Download, list and custody: a role without evidence:read gets 403. An unassigned investigator gets the same 404 for a
  hidden item as for a missing one.
- Intake: a role without cases:submit cannot open a report, open a case, or import records. A role without evidence:write
  cannot upload.

Not asserted here, because F2 owns them: the audience and TLP rules (RED, AMBER, AMBER+STRICT, GREEN), redaction,
approval before a CLEAR release, and export. A change of recipient or package that cancels an approval is also F2.

The non-database tests replace the service functions, as the sibling tests do. The database tests are skipped unless
SAFE_TEST_DATABASE=1.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import api, evidence, evidence_api
from safe_kit.cases import CaseScope
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from test_case_access import (  # fixtures and helpers from the case access tests
    REPORTER_ID,
    TRIAGER_ID,
    as_role,
    caller,
    client,
    create_case_via_api,
    db_client,
    listed_ids,
    people,
    requires_database,
    timezone_set,
)
from test_evidence import evidence_world, multipart
from test_evidence import request_as as evidence_request_as
from test_evidence_lifecycle import (
    PDF,
    provider,
)  # the sample PDF and the local key provider
from error_body import without_request_id

# Roles that the default seed gives no evidence:read, and no evidence:write, and no cases:submit.
ROLES_WITHOUT_EVIDENCE_READ = ("reporter", "admin", "auditor", "disclosure_approver")
ROLES_WITHOUT_EVIDENCE_WRITE = (
    "reporter",
    "triager",
    "reviewer",
    "admin",
    "auditor",
    "disclosure_approver",
)
ROLES_WITHOUT_CASE_SUBMIT = (
    "triager",
    "investigator",
    "reviewer",
    "admin",
    "auditor",
    "disclosure_approver",
)
EVIDENCE_READ_URLS = (
    "/api/v1/cases/{}/evidence",
    "/api/v1/evidence/{}/download",
    "/api/v1/evidence/{}/custody",
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def refuse(*args, **kwargs):
    raise AssertionError("the service must not run for this caller")


# --- case ACL and search, without a database -------------------------------------------------------------------------


def test_a_reporter_sees_only_the_cases_it_created():
    scope = CaseScope.from_permissions(
        REPORTER_ID, DEFAULT_ROLE_PERMISSIONS["reporter"]
    )
    assert scope.read_all is False
    assert scope.visibility_sql(writing=True) == ("(cases.created_by = %s)", (REPORTER_ID,))


def test_a_reporter_list_and_search_run_inside_the_reporters_scope(
    client, caller, monkeypatch
):
    seen = {}

    def fake_list(scope, limit, after=None, q=None, **kwargs):
        seen["scope"], seen["q"] = scope, q
        return []

    monkeypatch.setattr(api, "list_visible_cases", fake_list)
    as_role(caller, REPORTER_ID, "reporter")

    response = client.get("/api/v1/cases", params={"q": "SAFE-2026"})

    assert response.status_code == 200
    assert seen["q"] == "SAFE-2026"
    assert seen["scope"] == CaseScope.from_permissions(
        REPORTER_ID, DEFAULT_ROLE_PERMISSIONS["reporter"]
    )


@pytest.mark.parametrize("role", ROLES_WITHOUT_EVIDENCE_READ)
@pytest.mark.parametrize("url_template", EVIDENCE_READ_URLS)
def test_a_role_without_evidence_read_gets_403_on_list_download_and_custody(
    client, caller, monkeypatch, role, url_template
):
    monkeypatch.setattr(evidence_api, "is_visible", refuse)
    monkeypatch.setattr(evidence, "find_stored", refuse)
    monkeypatch.setattr(evidence, "list_evidence", refuse)
    as_role(caller, REPORTER_ID, role)

    response = client.get(url_template.format(uuid.uuid4()))

    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


# --- intake, without a database --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES_WITHOUT_CASE_SUBMIT)
@pytest.mark.parametrize(
    "url", ["/api/v1/reports", "/api/v1/cases", "/api/v1/cases/import"]
)
def test_a_role_without_cases_submit_cannot_open_or_import_a_case(
    client, caller, monkeypatch, role, url
):
    monkeypatch.setattr(api, "submit_report", refuse)
    monkeypatch.setattr(api, "create_case", refuse)
    as_role(caller, TRIAGER_ID, role)
    body = {
        "title": "Intake attempt",
        "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }

    response = client.post(url, json=body)

    assert response.status_code == 403


@pytest.mark.parametrize("role", ROLES_WITHOUT_EVIDENCE_WRITE)
def test_a_role_without_evidence_write_cannot_upload(client, caller, monkeypatch, role):
    monkeypatch.setattr(evidence, "create_evidence", refuse)
    as_role(caller, REPORTER_ID, role)
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])

    response = client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence",
        content=body,
        headers={**idem(), "Content-Type": content_type},
    )

    assert response.status_code == 403


# --- with a database ------------------------------------------------------------------------------------------------


@requires_database
def test_an_unassigned_investigator_gets_the_same_404_as_a_missing_case(
    db_client, people
):
    case_id = create_case_via_api(db_client, people["reporter"])
    investigator = db_client(people["investigator"])
    missing = str(uuid.uuid4())
    for suffix in ("", "/history", "/assignments"):
        hidden = investigator.get(f"/api/v1/cases/{case_id}{suffix}")
        absent = investigator.get(f"/api/v1/cases/{missing}{suffix}")
        assert hidden.status_code == absent.status_code == 404, suffix
        assert without_request_id(hidden) == without_request_id(absent), suffix


@requires_database
def test_a_reporter_search_does_not_reveal_another_reporters_case(db_client, people):
    own = create_case_via_api(db_client, people["reporter"])
    reference = (
        db_client(people["reporter"]).get(f"/api/v1/cases/{own}").json()["reference"]
    )

    assert own in listed_ids(db_client, people["reporter"], reference)
    assert own not in listed_ids(db_client, people["other_reporter"], reference)
    assert own not in listed_ids(db_client, people["other_reporter"], own[:8])
    assert (
        db_client(people["other_reporter"]).get(f"/api/v1/cases/{own}").status_code
        == 404
    )


@requires_database
def test_an_unassigned_investigator_gets_the_same_404_for_evidence_as_for_a_missing_item(
    evidence_world,
):
    other = evidence_world["people"]["other_investigator"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    missing_case, missing_item = str(uuid.uuid4()), str(uuid.uuid4())
    for hidden_url, absent_url in [
        (f"/api/v1/cases/{case_id}/evidence", f"/api/v1/cases/{missing_case}/evidence"),
        (
            f"/api/v1/evidence/{evidence_id}/download",
            f"/api/v1/evidence/{missing_item}/download",
        ),
        (
            f"/api/v1/evidence/{evidence_id}/custody",
            f"/api/v1/evidence/{missing_item}/custody",
        ),
    ]:
        hidden = evidence_request_as(other, "get", hidden_url)
        absent = evidence_request_as(other, "get", absent_url)
        assert hidden.status_code == absent.status_code == 404, hidden_url
        assert without_request_id(hidden) == without_request_id(absent), hidden_url
