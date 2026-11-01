"""FastAPI dependencies that authenticate the caller and check permissions."""

from collections.abc import Callable
from datetime import UTC, datetime

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from safe_kit.rbac import permissions_for_role
from safe_kit.security import decode_access_token
from safe_kit.sessions import is_access_token_revoked
from safe_kit.system_timezone import SETUP_ROLE, current_timezone_name
from safe_kit.users import User, find_user_by_id

bearer = HTTPBearer(auto_error=False)

INVALID_CREDENTIALS = HTTPException(
    status_code=401,
    detail="invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

PASSWORD_CHANGE_REQUIRED = HTTPException(status_code=403, detail="password change required")
SYSTEM_SETUP_REQUIRED = HTTPException(status_code=403, detail="system setup required")


def _authenticated_user(credentials: HTTPAuthorizationCredentials | None) -> User:
    if credentials is None:
        raise INVALID_CREDENTIALS
    try:
        claims = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise INVALID_CREDENTIALS
    if is_access_token_revoked(claims["jti"]):
        raise INVALID_CREDENTIALS
    user = find_user_by_id(claims["sub"])
    # The user is re-read on every request, so disabling an account or resetting a password
    # takes effect immediately, not when the token expires.
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS
    # iat has whole-second precision, so a token issued in the same second as a password reset
    # is also rejected. The caller must log in again, which is the safe direction to fail in.
    issued_at = datetime.fromtimestamp(claims["iat"], UTC)
    if issued_at < user.sessions_valid_after:
        raise INVALID_CREDENTIALS
    return user


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    """Authenticated user who has finished any required password change and the system setup.

    Until an administrator has set the system timezone, only administrators get through: they set it.
    """
    user = _authenticated_user(credentials)
    if user.must_change_password:
        raise PASSWORD_CHANGE_REQUIRED
    if user.role != SETUP_ROLE and current_timezone_name() is None:
        raise SYSTEM_SETUP_REQUIRED
    return user


def current_user_pending_change(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    """Authenticated user, even if a password change is still required. Use only for /me, logout and change-password."""
    return _authenticated_user(credentials)


def require_permission(permission: str) -> Callable[[User], User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if permission not in permissions_for_role(user.role):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dependency
