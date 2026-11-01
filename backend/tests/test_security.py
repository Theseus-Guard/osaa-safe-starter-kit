from datetime import UTC, datetime, timedelta

import jwt
import pytest

from safe_kit import security


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    key_file = tmp_path / "jwt_signing_key.txt"
    key_file.write_text("test-signing-key-with-enough-length-1234567890", encoding="utf-8")
    monkeypatch.setenv("SAFE_JWT_KEY_FILE", str(key_file))
    return key_file


def test_password_round_trip():
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password(hashed, "correct horse battery staple")
    assert not security.verify_password(hashed, "wrong password")


def test_missing_hash_never_verifies():
    assert not security.verify_password(None, "anything")


def test_password_expires_only_after_180_days():
    changed = datetime(2026, 1, 1, tzinfo=UTC)
    assert security.password_expired(changed, now=changed + timedelta(days=180)) is False
    assert security.password_expired(changed, now=changed + timedelta(days=180, seconds=1)) is True


def test_recent_password_is_matched_through_salted_hashes():
    recent = [security.hash_password("first passphrase here"), security.hash_password("second passphrase here")]
    assert security.is_recent_password("second passphrase here", recent)
    assert not security.is_recent_password("third passphrase here", recent)
    assert not security.is_recent_password("anything at all", [])


def test_access_token_round_trip_has_unique_jti(signing_key):
    first, _ = security.issue_access_token("user-1", "alice", "triager")
    second, _ = security.issue_access_token("user-1", "alice", "triager")
    claims = security.decode_access_token(first)
    assert claims["sub"] == "user-1"
    assert claims["role"] == "triager"
    assert claims["jti"] != security.decode_access_token(second)["jti"]


def test_default_access_token_lifetime_is_60_minutes(signing_key, monkeypatch):
    monkeypatch.delenv("SAFE_ACCESS_TTL_MINUTES", raising=False)
    _, expires_at = security.issue_access_token("user-1", "alice", "triager")
    assert expires_at - datetime.now(UTC) == pytest.approx(timedelta(minutes=60), abs=timedelta(seconds=5))


def test_refresh_token_lifetime_matches_the_access_token_by_default(monkeypatch):
    # Both start at issue time, so the frontend renews the pair before the access token expires.
    monkeypatch.delenv("SAFE_REFRESH_TTL_MINUTES", raising=False)
    assert security.refresh_token_ttl() == timedelta(minutes=60)
    assert security.refresh_token_ttl() == security.access_token_ttl()


def test_expired_token_is_rejected(signing_key, monkeypatch):
    monkeypatch.setenv("SAFE_ACCESS_TTL_MINUTES", "-1")
    token, _ = security.issue_access_token("user-1", "alice", "triager")
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(token)


def test_tampered_token_is_rejected(signing_key):
    token, _ = security.issue_access_token("user-1", "alice", "triager")
    header, _, signature = token.split(".")
    forged_payload = jwt.utils.base64url_encode(
        b'{"sub":"user-1","jti":"x","iss":"safe-starter-kit","aud":"safe-starter-kit-api",'
        b'"iat":1,"exp":9999999999,"role":"admin"}'
    ).decode()
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_access_token(f"{header}.{forged_payload}.{signature}")


def test_unsigned_token_is_rejected(signing_key):
    now = datetime.now(UTC)
    claims = {
        "sub": "user-1",
        "jti": "x",
        "iss": security.ISSUER,
        "aud": security.AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_access_token(unsigned)


def test_token_for_other_audience_is_rejected(signing_key):
    now = datetime.now(UTC)
    claims = {
        "sub": "user-1",
        "jti": "x",
        "iss": security.ISSUER,
        "aud": "some-other-service",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(claims, signing_key.read_text(encoding="utf-8"), algorithm="HS256")
    with pytest.raises(jwt.InvalidAudienceError):
        security.decode_access_token(token)


def test_refresh_token_is_random_and_stored_only_as_hash():
    first = security.new_refresh_token()
    second = security.new_refresh_token()
    assert first != second
    assert security.hash_refresh_token(first) != first
    assert security.hash_refresh_token(first) == security.hash_refresh_token(first)
