import pytest

from safe_kit.users import InvalidUsername, validate_username


@pytest.mark.parametrize("name", ["osaa-safe-admin", "alice", "j.doe", "team_1", "abc"])
def test_accepts_plain_usernames(name):
    validate_username(name)


@pytest.mark.parametrize(
    "name", ["admin", "root", "Admin", "ROOT", "administrator", "superuser", "system"]
)
def test_rejects_reserved_names_in_any_case(name):
    with pytest.raises(InvalidUsername, match="reserved"):
        validate_username(name)


@pytest.mark.parametrize(
    "name", ["ab", "Alice", "has space", "bad@name", "x" * 65, "-leading", ""]
)
def test_rejects_malformed_usernames(name):
    with pytest.raises(InvalidUsername):
        validate_username(name)
