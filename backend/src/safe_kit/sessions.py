"""Login sessions: refresh-token rotation, access-token revocation, and per-user session cutoff."""

from datetime import datetime

from safe_kit.db import connect
from safe_kit.security import hash_refresh_token, new_refresh_token, refresh_token_ttl, session_max_age


class InvalidRefreshToken(Exception):
    pass


def open_session(user_id: str) -> str:
    """Start a new session family for a user and return its refresh token."""
    token = new_refresh_token()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
            VALUES (%s, gen_random_uuid(), %s, now() + make_interval(secs => %s))
            """,
            (user_id, hash_refresh_token(token), refresh_token_ttl().total_seconds()),
        )
    return token


def rotate_session(raw_token: str) -> tuple[str, str]:
    """Exchange a refresh token for a new one. Returns (user_id, new_refresh_token).

    Presenting an already-rotated token is treated as possible theft: every token in
    that family is revoked and the request fails.
    """
    revoked_family = False
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id::text, user_id::text, family_id::text, revoked_at IS NOT NULL, expires_at > now(),
                   now() - (SELECT min(f.created_at) FROM refresh_tokens f WHERE f.family_id = refresh_tokens.family_id)
                       >= make_interval(secs => %s)
            FROM refresh_tokens WHERE token_hash = %s FOR UPDATE
            """,
            (session_max_age().total_seconds(), hash_refresh_token(raw_token)),
        ).fetchone()
        if row is None:
            raise InvalidRefreshToken
        row_id, user_id, family_id, revoked, unexpired, past_limit = row
        if revoked:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = %s AND revoked_at IS NULL",
                (family_id,),
            )
            revoked_family = True
        elif past_limit:
            # The session has reached its absolute limit. Its tokens are revoked, so the user must log in again.
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = %s AND revoked_at IS NULL",
                (family_id,),
            )
            revoked_family = True
        elif not unexpired:
            raise InvalidRefreshToken
        else:
            conn.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE id = %s", (row_id,))
            new_token = new_refresh_token()
            conn.execute(
                """
                INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
                VALUES (%s, %s, %s, now() + make_interval(secs => %s))
                """,
                (user_id, family_id, hash_refresh_token(new_token), refresh_token_ttl().total_seconds()),
            )
    # Raised after the commit above, so that the family revocation is not rolled back.
    if revoked_family:
        raise InvalidRefreshToken
    return user_id, new_token


def revoke_refresh_token(raw_token: str, user_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE token_hash = %s AND user_id = %s AND revoked_at IS NULL",
            (hash_refresh_token(raw_token), user_id),
        )


def revoke_access_token(jti: str, expires_at: datetime) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO revoked_access_tokens (jti, expires_at) VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING",
            (jti, expires_at),
        )
        # Expired entries are soft-deleted, never removed.
        conn.execute(
            "UPDATE revoked_access_tokens SET is_deleted = true, deleted_at = now() "
            "WHERE expires_at < now() AND NOT is_deleted"
        )


def is_access_token_revoked(jti: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM revoked_access_tokens WHERE jti = %s AND NOT is_deleted",
            (jti,),
        ).fetchone()
    return row is not None
