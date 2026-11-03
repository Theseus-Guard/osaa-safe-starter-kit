"""Keyset paging of the case history and the evidence list against PostgreSQL (F3, A4).

Every test here is marked "database": it needs SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, and the migrations are
applied first. The tests create rows and never remove them, so use a throwaway database.

The walks call the domain functions directly and follow the cursor the function returns, so the cursor round trip is
tested without the route. Rows the caller may not see must never appear on any page.
"""

# The fixtures and helpers below come from the evidence lifecycle tests, as in test_fixture_t04_evidence.py.
import uuid

from safe_kit import audit, cases, evidence, tlp, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence_storage import LocalVolumeStorage
from safe_kit.pagination import decode_cursor

from test_evidence_lifecycle import PDF
from test_evidence_lifecycle import database_case
from test_evidence_lifecycle import provider  # noqa: F401 - fixture: local KEK provider
from test_evidence_lifecycle import requires_database


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def walk_history(case_id: str, scope: CaseScope, limit: int, include_evidence: bool):
    """Follows next_cursor from the first page to the last. Returns the entries in order and the size of each page."""
    entries_seen, sizes, cursor = [], [], None
    for _ in range(100):
        after = decode_cursor(cursor) if cursor is not None else None
        page, cursor = cases.case_history(
            case_id, scope, limit, after, include_evidence=include_evidence
        )
        entries_seen.extend(page)
        sizes.append(len(page))
        if cursor is None:
            return entries_seen, sizes
    raise AssertionError("the walk did not end")


def walk_evidence(case_id: str, limit: int, recipient):
    """Follows next_cursor from the first page to the last. Returns the ids in order and the size of each page."""
    ids, sizes, cursor = [], [], None
    for _ in range(100):
        after = decode_cursor(cursor) if cursor is not None else None
        items, cursor = evidence.list_evidence(case_id, limit, after, recipient)
        ids.extend(item.id for item in items)
        sizes.append(len(items))
        if cursor is None:
            return ids, sizes
    raise AssertionError("the walk did not end")


@requires_database
def test_case_history_walks_every_row_once_and_the_last_page_has_no_cursor():
    creator, case_id = database_case()
    scope = CaseScope(creator, read_all=True)
    for _ in range(3):
        outcome, _ = cases.assign_member(case_id, new_user("investigator"), scope)
        assert outcome is cases.Outcome.OK

    everything, _ = cases.case_history(case_id, scope, 200)
    expected = [entry.id for entry in everything]
    # database_case writes no creation row, so the three assignments are the whole history.
    assert len(expected) == 3

    walked, sizes = walk_history(case_id, scope, 2, include_evidence=True)
    assert [entry.id for entry in walked] == expected
    assert len(set(expected)) == len(expected)
    assert all(size <= 2 for size in sizes)


@requires_database
def test_case_history_without_evidence_read_never_pages_evidence_rows(tmp_path, provider):
    creator, case_id = database_case()
    scope = CaseScope(creator, read_all=True)
    evidence.create_evidence(
        case_id,
        creator,
        "scan.pdf",
        "application/pdf",
        PDF + b"history",
        LocalVolumeStorage(tmp_path),
        provider,
        scope,
    )

    with_evidence, _ = walk_history(case_id, scope, 1, include_evidence=True)
    without, _ = walk_history(case_id, scope, 1, include_evidence=False)

    # The upload wrote evidence rows, so the caller with evidence:read sees more rows than the other one.
    assert any(entry.action in audit.EVIDENCE_AUDIT_ACTIONS for entry in with_evidence)
    assert len(without) < len(with_evidence)
    assert not any(entry.action in audit.EVIDENCE_AUDIT_ACTIONS for entry in without)


@requires_database
def test_evidence_pages_walk_every_item_the_recipient_may_see_and_no_hidden_item(
    tmp_path, provider
):
    creator, case_id = database_case()
    scope = CaseScope(creator, read_all=True)
    storage = LocalVolumeStorage(tmp_path)
    created = [
        evidence.create_evidence(
            case_id,
            creator,
            f"scan-{number}.pdf",
            "application/pdf",
            PDF + str(number).encode(),
            storage,
            provider,
            scope,
        ).id
        for number in range(5)
    ]
    # Items 1 and 3 are RED: only the named users of the case may see them. The recipient below is not named.
    # The content trigger refuses a label change, so it is switched off for this one transaction. SET LOCAL needs
    # a superuser, as in test_database.py.
    hidden = {created[1], created[3]}
    with connect() as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            "UPDATE evidence_items SET tlp_label = 'RED' WHERE id = ANY(%s::uuid[])",
            (list(hidden),),
        )

    stranger = tlp.Recipient(new_user("investigator"), organisation=False, external=False)
    visible_expected = [item for item in created if item not in hidden]

    walked, sizes = walk_evidence(case_id, 2, stranger)
    assert walked == visible_expected
    assert not (set(walked) & hidden)
    assert all(size <= 2 for size in sizes)

    # Without a recipient every item is listed, and the walk still ends without a cursor.
    everything, _ = walk_evidence(case_id, 2, None)
    assert everything == created
