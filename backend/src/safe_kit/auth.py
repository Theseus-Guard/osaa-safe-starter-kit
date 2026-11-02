"""FastAPI dependencies that authenticate the caller and check permissions.

A request is authenticated in one of two ways:
- the Authorization: Bearer header, used by the CLI and by API clients;
- the safe_access cookie, used by the browser.

A request that authenticates with the cookie and changes state (POST, PUT, PATCH, DELETE) must also send the
X-CSRF-Token header, equal to the safe_csrf cookie. A Bearer request needs no CSRF header: the browser does not send
the header on its own, so a cross-site request cannot carry it.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from safe_kit.rbac import permissions_for_role
from safe_kit.security import access_token_ttl, decode_access_token, refresh_token_ttl
from safe_kit.sessions import is_access_token_revoked
from safe_kit.system_timezone import SETUP_ROLE, current_timezone_name
from safe_kit.users import User, find_user_by_id

bearer = HTTPBearer(auto_error=False)

ACCESS_COOKIE = "safe_access"
REFRESH_COOKIE = "safe_refresh"
CSRF_COOKIE = "safe_csrf"
CSRF_HEADER = "X-CSRF-Token"
# The refresh cookie is sent only to the auth routes, so the browser never sends it with other requests.
REFRESH_COOKIE_PATH = "/api/v1/auth"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

INVALID_CREDENTIALS = HTTPException(
    status_code=401,
    detail="invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

CSRF_REFUSED = HTTPException(status_code=403, detail="csrf_token_invalid")
PASSWORD_CHANGE_REQUIRED = HTTPException(status_code=403, detail="password change required")
SYSTEM_SETUP_REQUIRED = HTTPException(status_code=403, detail="system setup required")


@dataclass(frozen=True)
class AccessCredential:
    token: str
    # True when the token came from the safe_access cookie. Such a request must pass the CSRF check when it changes state.
    from_cookie: bool


def access_credential(
    request: Request,
    header: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> AccessCredential | None:
    """The access token of the request: the Bearer header first, then the safe_access cookie. None when neither is sent."""
    if header is not None:
        return AccessCredential(token=header.credentials, from_cookie=False)
    cookie = request.cookies.get(ACCESS_COOKIE)
    if cookie:
        return AccessCredential(token=cookie, from_cookie=True)
    return None


def require_csrf(request: Request) -> None:
    """Refuses a state-changing request whose X-CSRF-Token header is missing or differs from the safe_csrf cookie."""
    if request.method not in UNSAFE_METHODS:
        return
    expected = request.cookies.get(CSRF_COOKIE)
    sent = request.headers.get(CSRF_HEADER)
    if not expected or not sent:
        raise CSRF_REFUSED
    if not hmac.compare_digest(expected.encode("utf-8"), sent.encode("utf-8")):
        raise CSRF_REFUSED


def set_session_cookies(response: Response, access_token: str, refresh_token: str, csrf_token: str) -> None:
    """Sets the three session cookies. The access and refresh cookies are HttpOnly; the CSRF cookie must be readable by the page."""
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=int(access_token_ttl().total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=int(refresh_token_ttl().total_seconds()),
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=int(refresh_token_ttl().total_seconds()),
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="strict")


def _authenticated_user(request: Request, credential: AccessCredential | None) -> User:
    if credential is None:
        raise INVALID_CREDENTIALS
    try:
        claims = decode_access_token(credential.token)
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
    # Checked only after the session is confirmed, so a bad token is always answered with 401.
    if credential.from_cookie:
        require_csrf(request)
    return user


def current_user(
    request: Request,
    credential: AccessCredential | None = Depends(access_credential),
) -> User:
    """Authenticated user who has finished any required password change and the system setup.

    Until an administrator has set the system timezone, only administrators get through: they set it.
    """
    user = _authenticated_user(request, credential)
    if user.must_change_password:
        raise PASSWORD_CHANGE_REQUIRED
    if user.role != SETUP_ROLE and current_timezone_name() is None:
        raise SYSTEM_SETUP_REQUIRED
    return user


def current_user_pending_change(
    request: Request,
    credential: AccessCredential | None = Depends(access_credential),
) -> User:
    """Authenticated user, even if a password change is still required. Use only for /me, logout and change-password."""
    return _authenticated_user(request, credential)


def permissions_of(user: User) -> frozenset[str]:
    return permissions_for_role(user.role)


def require_permission(permission: str) -> Callable[[User], User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if permission not in permissions_for_role(user.role):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dependency


def require_any_permission(*permissions: str) -> Callable[[User], User]:
    """Passes when the caller holds at least one of the permissions. Used where several roles may enter an endpoint."""

    def dependency(user: User = Depends(current_user)) -> User:
        if not set(permissions) & permissions_for_role(user.role):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dependency


def require_any_permission_except_role(excluded_role: str, *permissions: str) -> Callable[[User], User]:
    """Like require_any_permission, but refuses a caller whose role is excluded_role, whatever permissions it holds.

    Used where a permission that a role needs for reading also reaches a write that this role must not make. The
    disclosure approver holds cases:read_all (to read the case it approves), and the deadline and appeal routes accept
    that permission, so they refuse the approver role by name. The refusal is the same 403 as the other permission checks.
    """

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role == excluded_role or not set(permissions) & permissions_for_role(user.role):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dependency
