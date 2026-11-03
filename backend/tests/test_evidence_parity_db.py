"""Evidence reads for CLI and UI parity: one item (GET /api/v1/evidence/{id}) and the derivatives of an original
(GET /api/v1/evidence/{id}/derivatives). Both apply the case ACL and the TLP rule, as the download does.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# The world fixture and its helpers come from the sibling test module, the same way the redaction tests share them.
# ruff: noqa: F401, F811
import uuid

from test_evidence import request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_t06a_redaction import make_derived, redact, world  # world: fixture


def read(world, who, path):
    return request_as(world["people"][who], "get", path)


@requires_database
def test_the_item_read_carries_its_tlp_label_and_an_original_has_none(world):
    derived = make_derived(world, "AMBER")["derived"]["id"]
    original = read(world, "investigator", f"/api/v1/evidence/{world['original_id']}")
    assert original.status_code == 200
    assert original.json()["tlp_label"] is None
    assert original.json()["case_id"] == world["case_id"]
    item = read(world, "investigator", f"/api/v1/evidence/{derived}")
    assert item.status_code == 200
    assert item.json()["tlp_label"] == "AMBER"
    assert item.json()["status"] == "available"


@requires_database
def test_a_tlp_hidden_item_answers_the_same_404_as_a_missing_one(world):
    derived = make_derived(world, "RED")["derived"]["id"]
    # The triager reads the case, but RED names only the assigned people.
    hidden = read(world, "triager", f"/api/v1/evidence/{derived}")
    missing = read(world, "triager", f"/api/v1/evidence/{uuid.uuid4()}")
    assert hidden.status_code == 404
    assert missing.status_code == 404
    assert hidden.json()["detail"] == missing.json()["detail"] == "evidence not found"


@requires_database
def test_the_derivative_list_leaves_out_what_the_tlp_rule_hides(world):
    amber = make_derived(world, "AMBER")["derived"]["id"]
    red = make_derived(world, "RED")["derived"]["id"]
    path = f"/api/v1/evidence/{world['original_id']}/derivatives"
    # The assigned investigator is named on the case, so both derivatives show, oldest first.
    named = read(world, "investigator", path)
    assert named.status_code == 200
    assert [item["id"] for item in named.json()] == [amber, red]
    # The triager is organisation but not named: RED is left out, AMBER shows.
    organisation = read(world, "triager", path)
    assert organisation.status_code == 200
    assert [item["id"] for item in organisation.json()] == [amber]


@requires_database
def test_the_derivative_list_and_the_item_need_the_permission_and_the_case(world):
    make_derived(world, "GREEN")
    original_path = f"/api/v1/evidence/{world['original_id']}"
    # Not assigned: the case ACL hides the original, so the answer is the same 404 as for a missing item.
    hidden = read(world, "other_investigator", f"{original_path}/derivatives")
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "evidence not found"
    # The reporter holds no evidence:read, so RBAC refuses before any lookup.
    assert read(world, "reporter", f"{original_path}/derivatives").status_code == 403
    assert read(world, "reporter", original_path).status_code == 403
