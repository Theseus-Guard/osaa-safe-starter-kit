import pytest

from safe_kit.security import is_csrf_token, new_csrf_token


def test_a_generated_token_has_the_expected_shape():
    for _ in range(20):
        assert is_csrf_token(new_csrf_token())


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "short",
        "a" * 42,
        "a" * 44,
        "abc\r\nSet-Cookie: injected=1" + "a" * 20,
        "a" * 42 + "=",
        "a" * 42 + " ",
        "a" * 42 + ";",
    ],
)
def test_anything_else_is_not_a_csrf_token(value):
    assert is_csrf_token(value) is False
