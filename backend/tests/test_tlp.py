"""TLP audience rule as a pure function, the restrictiveness order, and the recipient categories. No database.

The database test that checks the SQL against this function is in test_fixture_t06a_redaction.py.
"""

import itertools
from datetime import UTC, datetime

import pytest

from safe_kit import tlp
from safe_kit.users import User

# (named, organisation, external, approved) -> is the caller in the audience of the label
AUDIENCE = {
    None: lambda named, organisation, external, approved: True,
    "GREEN": lambda named, organisation, external, approved: True,
    "RED": lambda named, organisation, external, approved: named,
    "AMBER+STRICT": lambda named, organisation, external, approved: named or organisation,
    "AMBER": lambda named, organisation, external, approved: named or organisation or external,
    "CLEAR": lambda named, organisation, external, approved: approved,
}


def user(role: str) -> User:
    return User(
        id="33333333-3333-3333-3333-333333333333",
        username="tlp-test",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize("label", [None, *tlp.LABELS])
@pytest.mark.parametrize("flags", list(itertools.product([False, True], repeat=4)))
def test_permits_follows_the_audience_of_each_label(label, flags):
    named, organisation, external, approved = flags
    expected = AUDIENCE[label](named, organisation, external, approved)
    assert tlp.permits(label, named=named, organisation=organisation, external=external, approved=approved) is expected


def test_each_label_is_narrower_than_the_next_wider_audience():
    # RED: only named people. AMBER+STRICT adds the organisation. AMBER adds external parties.
    only_organisation = dict(named=False, organisation=True, external=False, approved=False)
    only_external = dict(named=False, organisation=False, external=True, approved=False)
    assert not tlp.permits("RED", **only_organisation)
    assert tlp.permits("AMBER+STRICT", **only_organisation)
    assert not tlp.permits("AMBER+STRICT", **only_external)
    assert tlp.permits("AMBER", **only_external)


def test_clear_is_refused_without_an_approval_for_everyone():
    everyone = dict(named=True, organisation=True, external=True)
    assert not tlp.permits("CLEAR", approved=False, **everyone)
    assert tlp.permits("CLEAR", approved=True, **everyone)


def test_restrictiveness_order_runs_from_clear_to_red():
    order = ["CLEAR", "GREEN", "AMBER", "AMBER+STRICT", "RED"]
    for narrower, wider in zip(order[1:], order[:-1]):
        assert tlp.at_least_as_restrictive(narrower, wider)
        assert not tlp.at_least_as_restrictive(wider, narrower)
    for label in order:
        assert tlp.at_least_as_restrictive(label, label)


def test_a_derivative_with_no_original_label_may_take_any_label():
    for label in tlp.LABELS:
        assert tlp.at_least_as_restrictive(label, None)


def test_unknown_label_is_an_error():
    with pytest.raises(ValueError):
        tlp.permits("PUBLIC", named=True, organisation=True, external=True, approved=True)


@pytest.mark.parametrize("role", ["reporter", "reporting_owner"])
def test_reporter_channel_roles_are_external_and_not_organisation(role):
    recipient = tlp.recipient_for(user(role))
    assert recipient.external is True
    assert recipient.organisation is False


@pytest.mark.parametrize("role", ["triager", "investigator", "reviewer", "admin", "auditor", "disclosure_approver"])
def test_staff_roles_are_organisation_and_not_external(role):
    recipient = tlp.recipient_for(user(role))
    assert recipient.organisation is True
    assert recipient.external is False


def test_visibility_sql_has_one_parameter_per_placeholder():
    sql, params = tlp.visibility_sql(tlp.recipient_for(user("investigator")))
    assert sql.count("%s") == len(params)
