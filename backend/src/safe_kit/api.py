import csv
import hashlib
import io
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from safe_kit.auth import (
    CSRF_COOKIE,
    INVALID_CREDENTIALS,
    REFRESH_COOKIE,
    AccessCredential,
    access_credential,
    clear_session_cookies,
    current_user,
    current_user_pending_change,
    permissions_of,
    require_any_permission,
    require_any_permission_except_role,
    require_csrf,
    require_permission,
    set_session_cookies,
)
from safe_kit.audit import record_login_failure
from safe_kit.case_import import (
    IMPORT_FORMAT_VERSION,
    MAX_IMPORT_BYTES,
    RECORD_SCHEMA_VERSION,
    DryRunResult,
    ImportFileError,
    RecordResult,
    import_records,
    parse_import_file,
    preview_records,
    received_format_version,
)
from safe_kit.cases import (
    Assignment,
    Case,
    CaseScope,
    CaseStatus,
    Outcome,
    assign_member,
    case_history,
    close_case,
    create_case,
    find_visible_case,
    list_assignable_users,
    list_assignments,
    list_visible_cases,
    redact_history,
    reopen_case,
    restore_deadline,
    unassign_member,
    update_case_title,
    update_deadline,
)
from safe_kit.client_address import resolve_client_address
from safe_kit.config_guard import (
    is_demo,
    load_settings,
    retention_years,
    trusted_proxies,
)
from safe_kit.db import connect
from safe_kit.pagination import (
    decode_cursor,
    decode_name_cursor,
    encode_cursor,
    encode_name_cursor,
)
from safe_kit.demo_auth import (
    DEMO_ROLES,
    DEMO_USERNAMES,
    demo_credential_hash,
    demo_sign_in_allowed,
)
from safe_kit.system_timezone import (
    SETUP_ROLE,
    ChangeOutcome,
    TimezoneNotConfigured,
    TimezoneSetting,
    current_setting,
    current_timezone_name,
    set_timezone,
    utc_offset,
    validate_timezone,
)
from safe_kit.rate_limit import (
    LOGIN_LIMIT_PER_CLIENT,
    LOGIN_LIMIT_PER_USERNAME,
    LOGIN_WINDOW,
)
from safe_kit.rate_limit import clear as clear_rate_limit
from safe_kit.rate_limit import hit as hit_rate_limit
from safe_kit.privacy import CSV_COLUMNS, inventory, inventory_csv_rows
from safe_kit.rbac import (
    DISCLOSURE_APPROVER_ROLE,
    RoleOutcome,
    list_catalog,
    list_role_permissions,
    set_role_permissions,
)
from safe_kit.idempotency import KeyReused, Replayed, checked_key, key_scope
from safe_kit.reporter_channel import (
    ContactOutcome,
    hide_reporter,
    read_reporter_contact,
    submit_report,
)
from safe_kit.revisions import etag_for, if_match_revision
from safe_kit.security import (
    MIN_PASSWORD_LENGTH,
    access_token_ttl,
    decode_access_token,
    issue_access_token,
    is_csrf_token,
    new_csrf_token,
    verify_password,
)
from safe_kit.sessions import (
    InvalidRefreshToken,
    open_session,
    revoke_access_token,
    revoke_refresh_token,
    rotate_session,
)
from safe_kit.profile_api import profile_fields, router as profile_router
from safe_kit.impact import ImpactScores, current_scores_for_cases
from safe_kit.impact_api import impact_router, risk_fields
from safe_kit.users import (
    ROLES,
    EmailAlreadyInUse,
    InvalidEmail,
    InvalidProfile,
    InvalidUsername,
    PasswordReused,
    ReporterAccountResetRefused,
    UpdateOutcome,
    User,
    admin_exists,
    change_own_password,
    create_user_with_profile,
    find_user,
    find_user_by_id,
    list_users,
    reset_password,
    soft_delete_user,
    update_user,
)


@asynccontextmanager
async def _startup_guard(_app: FastAPI) -> AsyncIterator[None]:
    # uvicorn imports this module and runs the lifespan before it serves any request. ConfigError makes the startup
    # fail, so the server does not start. No database connection is opened before this check.
    load_settings(os.environ)
    yield


app = FastAPI(title="SAFE-Starter-Kit API", version="0.2.0", lifespan=_startup_guard)

# Error contract (plans/f0/api-draft.md sections 3.3 and 3.6). Every error response has the same body: the existing
# "detail" field, which the web UI and the CLI read, and an "error" object with a stable code, a safe message, the
# request ID, and retryable. Validation errors also list the failing paths and problem types. Rejected values are
# never returned.
REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# Draft 3.3: 256 KiB for a normal JSON body (the lower of the draft's two JSON values). The import and the evidence
# upload read their own, larger limits in their routes, so they are exempt here.
JSON_BODY_LIMIT_BYTES = 256 * 1024
_BODY_LIMIT_EXEMPT = (
    ("POST", re.compile(r"/api/v1/cases/import(/dry-run)?")),
    ("POST", re.compile(r"/api/v1/cases/[^/]+/evidence")),
)
_CODE_BY_STATUS = {
    400: "validation_failed",
    401: "unauthenticated",
    403: "forbidden_role",
    404: "not_found",
    409: "state_conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "domain_rule_violated",
    429: "rate_limited",
    500: "internal_error",
    503: "temporarily_unavailable",
}
_MESSAGE_BY_STATUS = {
    400: "The request is not valid. Check the fields it names.",
    401: "Sign in to continue.",
    403: "Your role may not use this action.",
    404: "The requested item was not found.",
    409: "The request conflicts with the current state of the item.",
    413: "The request is larger than the server allows.",
    415: "The media type of the request is not supported.",
    422: "The request breaks a rule of this system.",
    429: "Too many requests. Wait and retry.",
    500: "An internal error occurred. Quote the request ID when you report it.",
    503: "The service is temporarily unavailable. Retry later.",
}
ERROR_MESSAGES = {
    "precondition_required": "This change must say which version it changes. Send If-Match with the ETag you read.",
    "precondition_failed": "The record was changed by someone else. Reload it, then retry with its new ETag.",
    "invalid_if_match": 'If-Match must be the ETag of the record, a quoted whole number such as "2".',
    "validation_failed": "A field in the request is missing, has the wrong type, or is out of range.",
    "unknown_field": "The request contains a field that is not allowed.",
    "revision_conflict": "The record was changed by another request. Reload it and retry.",
    "idempotency_key_reused": "This idempotency key was already used with another request.",
    "setup_required": "The first administrator has not been created yet.",
    "payload_too_large": "The request is larger than the server allows.",
}
# A status string in "detail" can name a code under another name. "unavailable" is the readiness status.
_CODE_ALIASES = {"unavailable": "temporarily_unavailable"}
_RETRYABLE_CODES = frozenset(
    {"temporarily_unavailable", "rate_limited", "idempotency_request_in_progress"}
)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _error_code(status: int, detail: Any) -> str:
    if isinstance(detail, dict) and isinstance(detail.get("status"), str):
        return _CODE_ALIASES.get(detail["status"], detail["status"])
    return _CODE_BY_STATUS.get(status, "request_failed")


def _error_response(
    status: int,
    detail: Any,
    request_id: str,
    *,
    code: str | None = None,
    fields: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The one place where an error becomes a response. Every error path in the API ends here."""
    code = code or _error_code(status, detail)
    error: dict[str, Any] = {
        "code": code,
        "message": ERROR_MESSAGES.get(
            code, _MESSAGE_BY_STATUS.get(status, "The request could not be completed.")
        ),
        "request_id": request_id,
        "retryable": code in _RETRYABLE_CODES,
    }
    if fields is not None:
        error["fields"] = fields
    content = jsonable_encoder({"detail": detail, "error": error})
    return JSONResponse(
        status_code=status,
        content=content,
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


@app.exception_handler(StarletteHTTPException)
def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    return _error_response(
        error.status_code, error.detail, _request_id(request), headers=error.headers
    )


@app.exception_handler(Replayed)
def _replayed_request(request: Request, error: Replayed) -> JSONResponse:
    """A repeated request with the same Idempotency-Key: the stored answer, with its status and body. Nothing is written."""
    return JSONResponse(status_code=error.stored.status_code, content=error.stored.body)


@app.exception_handler(KeyReused)
def _reused_key(request: Request, error: KeyReused) -> JSONResponse:
    """The key came with another request. Nothing of the stored request is returned."""
    return _error_response(
        409, {"status": "idempotency_key_reused"}, _request_id(request)
    )


@app.exception_handler(Exception)
def _unhandled_error(request: Request, error: Exception) -> JSONResponse:
    # Uvicorn logs the traceback. The body names no part of the failure; the request ID links it to the log.
    return _error_response(500, "Internal Server Error", _request_id(request))


class _RequestIdMiddleware:
    """Outermost. Keeps a well-formed X-Request-ID that the client sent, or makes one, and returns it on every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            if _REQUEST_ID_PATTERN.fullmatch(candidate):
                return candidate
            break
    return str(uuid.uuid4())


class _BodyLimitMiddleware:
    """Refuses a request body over JSON_BODY_LIMIT_BYTES with 413. A declared Content-Length is checked before anything
    is read; a body without one is counted as it arrives, and the read stops at the limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _body_limit_exempt(scope):
            await self.app(scope, receive, send)
            return
        request_id = scope["state"]["request_id"]
        declared = next(
            (
                value.decode("latin-1")
                for name, value in scope["headers"]
                if name == b"content-length"
            ),
            None,
        )
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > JSON_BODY_LIMIT_BYTES
        ):
            response = _error_response(413, {"status": "payload_too_large"}, request_id)
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > JSON_BODY_LIMIT_BYTES:
                    raise HTTPException(
                        status_code=413, detail={"status": "payload_too_large"}
                    )
            return message

        await self.app(scope, limited_receive, send)


def _body_limit_exempt(scope: Scope) -> bool:
    return any(
        scope["method"] == method and pattern.fullmatch(scope["path"])
        for method, pattern in _BODY_LIMIT_EXEMPT
    )


# Added inner first: the request ID middleware is outside the body limit, so a 413 carries the request ID too.
app.add_middleware(_BodyLimitMiddleware)
app.add_middleware(_RequestIdMiddleware)


CASES_ROUTE = "/api/v1/cases"
REPORTS_ROUTE = "/api/v1/reports"
IMPORT_ROUTE = "/api/v1/cases/import"
ASSIGNMENTS_ROUTE = "/api/v1/cases/{case_id}/assignments"
CLOSE_ROUTE = "/api/v1/cases/{case_id}/close"
REOPEN_ROUTE = "/api/v1/cases/{case_id}/reopen"
CASE_ROUTE = "/api/v1/cases/{case_id}"
DEADLINE_ROUTE = "/api/v1/cases/{case_id}/deadlines/{task_id}"
RESTORE_ROUTE = "/api/v1/cases/{case_id}/deadlines/{task_id}/restore"


def _error_path(loc: tuple[Any, ...]) -> str:
    parts = list(loc[1:]) if loc and loc[0] == "body" else list(loc)
    return ".".join(str(part) for part in parts) or "body"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # FastAPI echoes the rejected value in "input". For a password field that would send the password back, so it is dropped.
    raw = exc.errors()
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in raw
    ]
    fields = [
        {
            "path": _error_path(error["loc"]),
            "problem": "unknown_field"
            if error["type"] == "extra_forbidden"
            else error["type"],
        }
        for error in raw
    ]
    code = (
        "unknown_field"
        if any(error["type"] == "extra_forbidden" for error in raw)
        else "validation_failed"
    )
    return _error_response(400, errors, _request_id(request), code=code, fields=fields)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class TemporaryPasswordRequest(BaseModel):
    temporary_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    role: str
    temporary_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    email: str | None = Field(default=None, max_length=1024)
    first_name: str | None = Field(default=None, max_length=1024)
    last_name: str | None = Field(default=None, max_length=1024)
    title: str | None = Field(default=None, max_length=1024)

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError("unknown role")
        return value


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str | None) -> str | None:
        if value is not None and value not in ROLES:
            raise ValueError("unknown role")
        return value


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    password_change_required: bool


class MeResponse(BaseModel):
    id: str
    username: str
    role: str
    password_change_required: bool
    timezone_setup_required: bool
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    # Sorted permission names of the caller's role. The frontend uses them to show or hide actions; the API still checks each request.
    permissions: list[str]


class SystemTimezoneResponse(BaseModel):
    timezone: str | None
    effective_from: datetime | None
    utc_offset: str | None


class SystemTimezoneUpdate(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)
    confirm: bool = False
    # What the client last read. Required, so a client always states the value it is replacing (null when unset).
    current: str | None = Field(max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        return validate_timezone(value)


class UserCreatedResponse(BaseModel):
    id: str
    username: str
    role: str
    password_change_required: bool


class UserSummary(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    password_change_required: bool
    created_at: datetime
    is_protected: bool
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    # Sent back as If-Match on PATCH users/{user_id}. A list has one ETag per response, so each user carries its own.
    revision: int


class UserPage(BaseModel):
    items: list[UserSummary]
    next_cursor: str | None


class RoleSummary(BaseModel):
    role: str
    permissions: list[str]


class PermissionSummary(BaseModel):
    name: str
    description: str


class RolePermissionsRequest(BaseModel):
    permissions: list[str] = Field(max_length=100)


def _client_ip(request: Request) -> str:
    # The one place that decides the client address for demo sign-in and the login rate limits.
    # X-Forwarded-For counts only when the socket peer is a trusted proxy (client_address.py).
    peer = request.client.host if request.client else None
    return resolve_client_address(
        peer, request.headers.get("x-forwarded-for"), trusted_proxies(os.environ)
    )


def _issue_tokens(user: User, refresh_token: str) -> TokenResponse:
    access_token, _ = issue_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(access_token_ttl().total_seconds()),
        password_change_required=user.must_change_password,
    )


PROTECTED_DETAIL = "the protected admin account cannot be disabled, deleted, or demoted"
# Detail code of the 422 when a new password matches one of the account's last PASSWORD_HISTORY_DEPTH passwords
# or a temporary password issued to the account.
PASSWORD_REUSED_DETAIL = "password_reused"
# Detail code of the 409 when an administrator tries to reset the password of a reporter account.
REPORTER_RESET_REFUSED_DETAIL = "reporter_account_reset_refused"


def _summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        password_change_required=user.must_change_password,
        created_at=user.created_at,
        is_protected=user.is_protected,
        revision=user.revision,
        **profile_fields(user),
    )


@app.get("/api/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


SETUP_REQUIRED = HTTPException(status_code=503, detail={"status": "setup_required"})


@app.get("/api/v1/health/ready")
def ready() -> dict[str, str]:
    try:
        with connect() as conn:
            conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        if not admin_exists():
            raise SETUP_REQUIRED
    except psycopg.Error:
        raise HTTPException(status_code=503, detail={"status": "unavailable"})
    return {"status": "ok"}


@app.post("/api/v1/auth/token", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, response: Response) -> TokenResponse:
    # Until the first admin exists, nobody can log in. The operator creates that admin with the CLI.
    if not admin_exists():
        raise SETUP_REQUIRED
    # Count the attempt before checking the password, so that parallel guesses cannot exceed the limit.
    user_hits = hit_rate_limit(f"login:user:{body.username}", LOGIN_WINDOW)
    client_hits = hit_rate_limit(f"login:client:{_client_ip(request)}", LOGIN_WINDOW)
    if user_hits > LOGIN_LIMIT_PER_USERNAME or client_hits > LOGIN_LIMIT_PER_CLIENT:
        raise HTTPException(
            status_code=429,
            detail="too many login attempts",
            headers={"Retry-After": str(int(LOGIN_WINDOW.total_seconds()))},
        )
    user = find_user(body.username)
    password_ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not user.is_active or not password_ok:
        if user is None:
            reason = "unknown_user"
        elif not user.is_active:
            reason = "account_inactive"
        else:
            reason = "wrong_password"
        record_login_failure(
            user.id if user else None, reason, body.username, _client_ip(request)
        )
        raise INVALID_CREDENTIALS
    # Until the system timezone is set, only administrators may sign in: they are the ones who set it.
    if user.role != SETUP_ROLE and current_timezone_name() is None:
        raise SETUP_REQUIRED
    clear_rate_limit(f"login:user:{body.username}")
    tokens = _issue_tokens(user, open_session(user.id))
    # A new session gets a new CSRF value. The browser keeps the cookies; a CLI client uses the body.
    set_session_cookies(
        response, tokens.access_token, tokens.refresh_token, new_csrf_token()
    )
    return tokens


class DemoUser(BaseModel):
    username: str
    name: str
    role: str


class DemoStatus(BaseModel):
    demo: bool
    # The demo roles that can sign in. Names and roles only, never a credential. Empty outside demo mode.
    users: list[DemoUser] = []


class DemoSignInRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    credential: str = Field(min_length=1, max_length=256)


DEMO_SIGN_IN_REFUSED = HTTPException(
    status_code=403, detail="demo sign-in is not available"
)


@app.get("/api/v1/demo/status", response_model=DemoStatus)
def demo_status() -> DemoStatus:
    """True only in demo mode with the synthetic dataset. The UI shows the demo banner and the role picker when it is true."""
    if not is_demo(os.environ):
        return DemoStatus(demo=False)
    return DemoStatus(
        demo=True,
        users=[
            DemoUser(username=r.username, name=r.name, role=r.role) for r in DEMO_ROLES
        ],
    )


@app.post("/api/v1/demo/sign-in", response_model=TokenResponse)
def demo_sign_in(
    body: DemoSignInRequest, request: Request, response: Response
) -> TokenResponse:
    """Signs in one demo role with its own per-install demo credential (safe_kit.demo_auth).

    Refused (403) outside demo mode and for a caller that is not on loopback. A wrong username, a username outside the
    demo roles, an inactive account, and a wrong credential all get the same 401 as a failed password sign-in. The
    credential is never logged.
    """
    client = _client_ip(request)
    if not demo_sign_in_allowed(os.environ, client):
        raise DEMO_SIGN_IN_REFUSED
    client_hits = hit_rate_limit(f"demo:client:{client}", LOGIN_WINDOW)
    if client_hits > LOGIN_LIMIT_PER_CLIENT:
        raise HTTPException(
            status_code=429,
            detail="too many login attempts",
            headers={"Retry-After": str(int(LOGIN_WINDOW.total_seconds()))},
        )
    user = find_user(body.username) if body.username in DEMO_USERNAMES else None
    # The password verifier runs even when there is no user, so the timing does not show which check failed.
    password_ok = verify_password(
        demo_credential_hash(user.id) if user else None, body.credential
    )
    if user is None or not user.is_active or not password_ok:
        if user is None:
            reason = "unknown_user"
        elif not user.is_active:
            reason = "account_inactive"
        else:
            reason = "wrong_password"
        record_login_failure(user.id if user else None, reason, body.username, client)
        raise INVALID_CREDENTIALS
    clear_rate_limit(f"demo:client:{client}")
    tokens = _issue_tokens(user, open_session(user.id))
    set_session_cookies(
        response, tokens.access_token, tokens.refresh_token, new_csrf_token()
    )
    return tokens


@app.post("/api/v1/auth/refresh", response_model=TokenResponse)
def refresh(
    request: Request, response: Response, body: RefreshRequest | None = None
) -> TokenResponse:
    # The browser sends no body: the refresh token comes from the safe_refresh cookie, and that request must carry the CSRF header.
    if body is not None:
        presented = body.refresh_token
    else:
        presented = request.cookies.get(REFRESH_COOKIE)
        if not presented:
            raise INVALID_CREDENTIALS
        require_csrf(request)
    try:
        user_id, new_refresh_token = rotate_session(presented)
    except InvalidRefreshToken:
        raise INVALID_CREDENTIALS
    user = find_user_by_id(user_id)
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS
    tokens = _issue_tokens(user, new_refresh_token)
    # The CSRF value stays the same across a refresh, so requests already in flight with it still pass. Only a value
    # of the expected shape is kept; anything else is replaced by a new token.
    existing_csrf = request.cookies.get(CSRF_COOKIE)
    csrf_value = existing_csrf if is_csrf_token(existing_csrf) else new_csrf_token()
    set_session_cookies(response, tokens.access_token, tokens.refresh_token, csrf_value)
    return tokens


@app.post("/api/v1/auth/logout", status_code=204)
def logout(
    request: Request,
    body: LogoutRequest | None = None,
    credential: AccessCredential | None = Depends(access_credential),
    user: User = Depends(current_user_pending_change),
) -> Response:
    # The access token stays in the revocation list until it would have expired anyway.
    # current_user_pending_change has already refused a request without a valid token, so credential is set here.
    claims = decode_access_token(credential.token)
    revoke_access_token(claims["jti"], datetime.fromtimestamp(claims["exp"], UTC))
    if body is not None and body.refresh_token:
        revoke_refresh_token(body.refresh_token, user.id)
    elif refresh_cookie := request.cookies.get(REFRESH_COOKIE):
        revoke_refresh_token(refresh_cookie, user.id)
    response = Response(status_code=204)
    clear_session_cookies(response)
    return response


@app.post("/api/v1/auth/password", status_code=204)
def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(current_user_pending_change),
) -> Response:
    # Used both for the forced change at first login and for voluntary changes.
    # The current password is required, so a stolen access token alone cannot change it.
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    try:
        change_own_password(user.id, body.new_password)
    except PasswordReused:
        raise HTTPException(status_code=422, detail=PASSWORD_REUSED_DETAIL)
    # All sessions end, including this one. The client must log in again with the new password.
    # The session cookies go too, so the browser does not keep tokens that no longer work.
    response = Response(status_code=204)
    clear_session_cookies(response)
    return response


@app.get("/api/v1/auth/me", response_model=MeResponse)
def me(user: User = Depends(current_user_pending_change)) -> MeResponse:
    return MeResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        password_change_required=user.must_change_password,
        timezone_setup_required=user.role == SETUP_ROLE
        and current_timezone_name() is None,
        permissions=sorted(permissions_of(user)),
        **profile_fields(user),
    )


def _timezone_response(setting: TimezoneSetting | None) -> SystemTimezoneResponse:
    if setting is None:
        return SystemTimezoneResponse(
            timezone=None, effective_from=None, utc_offset=None
        )
    return SystemTimezoneResponse(
        timezone=setting.timezone,
        effective_from=setting.effective_from,
        utc_offset=utc_offset(setting.timezone, datetime.now(UTC)),
    )


@app.get("/api/v1/system/timezone", response_model=SystemTimezoneResponse)
def get_system_timezone(user: User = Depends(current_user)) -> SystemTimezoneResponse:
    return _timezone_response(current_setting())


@app.put("/api/v1/system/timezone", response_model=SystemTimezoneResponse)
def admin_set_system_timezone(
    body: SystemTimezoneUpdate,
    actor: User = Depends(require_permission("users:manage")),
) -> SystemTimezoneResponse:
    # The effective time is the database transaction time, set inside set_timezone. The client cannot supply it.
    outcome, setting = set_timezone(body.timezone, body.current, body.confirm, actor.id)
    if outcome is ChangeOutcome.STALE:
        raise HTTPException(
            status_code=409, detail="timezone changed by someone else; reload"
        )
    if outcome is ChangeOutcome.CONFIRMATION_REQUIRED:
        raise HTTPException(status_code=422, detail="confirmation required")
    return _timezone_response(setting)


@app.get("/api/v1/users", response_model=UserPage)
def admin_list_users(
    limit: int = Query(default=50, ge=1, le=200),
    # Opaque. Omit it for the first page; use next_cursor from the previous page for the next one.
    cursor: str | None = Query(default=None, max_length=200),
    actor: User = Depends(require_permission("users:manage")),
) -> UserPage:
    after = None
    if cursor is not None:
        after = decode_name_cursor(cursor)
        if after is None:
            raise HTTPException(status_code=400, detail={"status": "invalid_cursor"})
    # One extra row tells whether another page exists; it is not returned.
    page_users = list_users(limit + 1, after)
    has_more = len(page_users) > limit
    page_users = page_users[:limit]
    return UserPage(
        items=[_summary(user) for user in page_users],
        next_cursor=encode_name_cursor(page_users[-1].username, page_users[-1].id)
        if has_more
        else None,
    )


@app.post("/api/v1/users", status_code=201, response_model=UserCreatedResponse)
def admin_create_user(
    body: UserCreateRequest,
    actor: User = Depends(require_permission("users:manage")),
) -> UserCreatedResponse:
    try:
        user_id = create_user_with_profile(
            body.username,
            body.temporary_password,
            body.role,
            body.email,
            body.first_name,
            body.last_name,
            body.title,
            actor_id=actor.id,
        )
    except InvalidUsername as error:
        raise HTTPException(status_code=422, detail=str(error))
    except (InvalidEmail, InvalidProfile) as error:
        raise HTTPException(status_code=422, detail=str(error))
    except EmailAlreadyInUse:
        raise HTTPException(status_code=409, detail="email already in use")
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="username already exists")
    return UserCreatedResponse(
        id=user_id,
        username=body.username,
        role=body.role,
        password_change_required=True,
    )


@app.patch("/api/v1/users/{user_id}", response_model=UserSummary)
def admin_update_user(
    user_id: uuid.UUID,
    body: UserUpdateRequest,
    response: Response,
    actor: User = Depends(require_permission("users:manage")),
    # The revision of the account the caller read (its UserSummary in the list). Required: missing is a 428.
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> UserSummary:
    expected_revision = if_match_revision(if_match)
    if body.role is None and body.is_active is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    # Admins cannot change their own role or status. This keeps a single admin mistake from locking
    # everyone out, and another active admin can always make the change.
    if str(user_id) == actor.id:
        raise HTTPException(
            status_code=409, detail="cannot change your own role or status"
        )
    outcome = update_user(
        str(user_id),
        role=body.role,
        is_active=body.is_active,
        actor_id=actor.id,
        expected_revision=expected_revision,
    )
    if outcome is UpdateOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="user not found")
    if outcome is UpdateOutcome.REVISION_MISMATCH:
        current = find_user_by_id(str(user_id))
        assert current is not None
        # Only the current revision, in the body and as the ETag. Nothing of the account is returned.
        raise HTTPException(
            status_code=412,
            detail={
                "status": "precondition_failed",
                "current_revision": current.revision,
            },
            headers={"ETag": etag_for(current.revision)},
        )
    if outcome is UpdateOutcome.PROTECTED:
        raise HTTPException(status_code=409, detail=PROTECTED_DETAIL)
    if outcome is UpdateOutcome.LAST_ADMIN:
        raise HTTPException(
            status_code=409, detail="the last active admin cannot be removed"
        )
    updated = find_user_by_id(str(user_id))
    assert updated is not None
    response.headers["ETag"] = etag_for(updated.revision)
    return _summary(updated)


@app.delete("/api/v1/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: uuid.UUID,
    actor: User = Depends(require_permission("users:manage")),
) -> Response:
    # Soft delete only: the row is kept, the user is disabled and loses its sessions.
    # Physical removal is done by a database administrator, never through the API.
    if str(user_id) == actor.id:
        raise HTTPException(
            status_code=409, detail="cannot change your own role or status"
        )
    outcome = soft_delete_user(str(user_id), actor_id=actor.id)
    if outcome is UpdateOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="user not found")
    if outcome is UpdateOutcome.PROTECTED:
        raise HTTPException(status_code=409, detail=PROTECTED_DETAIL)
    if outcome is UpdateOutcome.LAST_ADMIN:
        raise HTTPException(
            status_code=409, detail="the last active admin cannot be removed"
        )
    return Response(status_code=204)


@app.post("/api/v1/users/{user_id}/password", status_code=204)
def admin_reset_password(
    user_id: uuid.UUID,
    body: TemporaryPasswordRequest,
    actor: User = Depends(require_permission("users:reset_password")),
) -> Response:
    # Self-service reset is intentionally not offered. The admin sets a temporary password
    # and the user must replace it at next login.
    try:
        found = reset_password(str(user_id), body.temporary_password, actor_id=actor.id)
    except PasswordReused:
        raise HTTPException(status_code=422, detail=PASSWORD_REUSED_DETAIL)
    except ReporterAccountResetRefused:
        raise HTTPException(status_code=409, detail=REPORTER_RESET_REFUSED_DETAIL)
    if not found:
        raise HTTPException(status_code=404, detail="user not found")
    return Response(status_code=204)


@app.get("/api/v1/rbac/roles", response_model=list[RoleSummary])
def admin_list_roles(
    actor: User = Depends(require_permission("users:manage")),
) -> list[RoleSummary]:
    return [
        RoleSummary(role=role, permissions=permissions)
        for role, permissions in list_role_permissions().items()
    ]


@app.get("/api/v1/rbac/permissions", response_model=list[PermissionSummary])
def admin_list_permissions(
    actor: User = Depends(require_permission("users:manage")),
) -> list[PermissionSummary]:
    return [
        PermissionSummary(name=name, description=description)
        for name, description in list_catalog()
    ]


@app.put("/api/v1/rbac/roles/{role}/permissions", response_model=RoleSummary)
def admin_set_role_permissions(
    role: str,
    body: RolePermissionsRequest,
    actor: User = Depends(require_permission("roles:manage")),
) -> RoleSummary:
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="role not found")
    outcome = set_role_permissions(role, frozenset(body.permissions), actor_id=actor.id)
    if outcome is RoleOutcome.UNKNOWN_PERMISSION:
        raise HTTPException(status_code=422, detail="unknown permission")
    if outcome is RoleOutcome.ADMIN_GUARD:
        raise HTTPException(
            status_code=409,
            detail="the admin role must keep users:manage, users:reset_password and roles:manage",
        )
    if outcome is RoleOutcome.ADMIN_CASE_CONTENT:
        raise HTTPException(
            status_code=409,
            detail="the admin role cannot hold case or reporter contact permissions",
        )
    if outcome is RoleOutcome.REPORTING_OWNER_FIXED:
        raise HTTPException(status_code=409, detail={"status": "reporting_owner_fixed"})
    if outcome is RoleOutcome.REPORTER_CONTACT_NOT_GRANTABLE:
        raise HTTPException(
            status_code=409, detail={"status": "reporter_contact_not_grantable"}
        )
    return RoleSummary(role=role, permissions=list_role_permissions()[role])


class DeadlineTaskResponse(BaseModel):
    id: str | None
    kind: str
    due_at: datetime
    status: str
    not_applicable_reason: str | None
    # Sent back as If-Match on PATCH cases/{case_id}/deadlines/{task_id}. Null for a task that is not stored yet.
    # The case ETag is the case revision, not this one.
    revision: int | None = None


class CaseCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    # Must be timezone-aware: a naive time cannot be compared with the deadlines reliably.
    awareness_time: AwareDatetime

    @field_validator("title")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("awareness_time")
    @classmethod
    def not_in_future(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC):
            raise ValueError("awareness_time cannot be in the future")
        return value


class CaseResponse(BaseModel):
    id: str
    reference: str
    title: str
    awareness_time: datetime
    status: str
    calendar_version: str
    created_at: datetime
    closed_at: datetime | None
    closed_by: str | None
    close_reason: str | None
    # Reason of the latest reopening, null when the case was never reopened.
    reopen_reason: str | None
    # Sent back as If-Match on a change to this case. Also sent as the ETag header.
    revision: int
    deadlines: list[DeadlineTaskResponse]
    # Risk (safe_kit.impact): the three impact values and the risk score. Null until the values are set.
    risk_score: float | None
    impact_c: int | None
    impact_b: int | None
    impact_a: int | None


class CaseSummary(BaseModel):
    id: str
    reference: str
    title: str
    awareness_time: datetime
    status: str
    created_at: datetime
    # The earliest due date of the case's open deadline tasks. Null when no task is open.
    next_deadline: datetime | None
    # The time of the case's latest change, from the audit log. Equals created_at when nothing has changed since.
    updated_at: datetime
    # Risk (safe_kit.impact), as in CaseResponse.
    risk_score: float | None
    impact_c: int | None
    impact_b: int | None
    impact_a: int | None


class CasePage(BaseModel):
    items: list[CaseSummary]
    next_cursor: str | None


class AssignmentRequest(BaseModel):
    user_id: uuid.UUID


class AssignableUserResponse(BaseModel):
    id: str
    username: str
    first_name: str | None
    last_name: str | None
    role: str


class AssignmentResponse(BaseModel):
    id: str
    case_id: str
    user_id: str
    username: str
    role: str
    assigned_by: str
    assigned_at: datetime


class ReasonRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_length(cls, value: str) -> str:
        cleaned = _clean_text(value)
        if cleaned is None:
            raise ValueError("reason must not be blank")
        return cleaned


class DeadlineUpdateRequest(BaseModel):
    status: Literal["open", "done", "not_applicable"]
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def reason_length(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @model_validator(mode="after")
    def reason_matches_status(self) -> "DeadlineUpdateRequest":
        if self.status == "not_applicable" and self.reason is None:
            raise ValueError("reason is required when status is not_applicable")
        if self.status != "not_applicable" and self.reason is not None:
            raise ValueError("reason is only allowed when status is not_applicable")
        return self


class HistoryEntryResponse(BaseModel):
    id: str
    action: str
    created_at: datetime
    # Null for callers without full case access: they see only the action and the timestamp.
    actor_id: str | None
    details: dict[str, Any] | None


class HistoryPage(BaseModel):
    items: list[HistoryEntryResponse]
    next_cursor: str | None


class CaseImportRecordResult(BaseModel):
    # index is the position of the record in the file's records array.
    index: int
    status: Literal["imported", "duplicate", "conflict", "rejected", "failed"]
    source_case_id: str | None
    case_id: str | None
    reference: str | None
    errors: list[str]


class CaseImportResponse(BaseModel):
    imported: int
    duplicate: int
    conflict: int
    rejected: int
    failed: int
    results: list[CaseImportRecordResult]


class CaseImportDryRunRecordResult(BaseModel):
    # index is the position of the record in the file's records array. Nothing is stored for any record.
    index: int
    status: Literal["would_import", "duplicate", "conflict", "rejected"]
    source_case_id: str | None
    case_id: str | None
    reference: str | None
    title: str | None
    awareness_time: str | None
    errors: list[str]


class ImportFormatCheck(BaseModel):
    supported: int
    # The value the file declares, or null when it declares none that this report can show.
    received: int | str | None
    accepted: bool


class CaseImportDryRunResponse(BaseModel):
    written: Literal[False] = False
    import_format_version: ImportFormatCheck
    record_schema_version: str
    # File-level problems (JSON, envelope, format version). When this is not empty, no record was checked.
    file_errors: list[str]
    would_import: int
    duplicate: int
    conflict: int
    rejected: int
    results: list[CaseImportDryRunRecordResult]


# Any role that may open a case: the reporter (own), the triager (all), or an assignee.
CASE_READ_PERMISSIONS = ("cases:read_own", "cases:read_all", "cases:read_assigned")
CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
CASE_CLOSED = HTTPException(status_code=409, detail="case is closed")


TEXT_LIMIT = 1000


def _clean_text(value: str | None) -> str | None:
    """Strip the text. Blank becomes None, and text longer than TEXT_LIMIT is refused."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > TEXT_LIMIT:
        raise ValueError(f"must be at most {TEXT_LIMIT} characters")
    return value


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _case_response(case: Case, scope: CaseScope) -> CaseResponse:
    # Reasons are shown only to callers with the full view, the same rule as the history details.
    scores = current_scores_for_cases([case.id]).get(case.id)
    return CaseResponse(
        **risk_fields(scores),
        id=case.id,
        reference=case.reference,
        title=case.title,
        awareness_time=case.awareness_time,
        status=case.status,
        calendar_version=case.calendar_version,
        created_at=case.created_at,
        closed_at=case.closed_at,
        closed_by=case.closed_by,
        close_reason=case.close_reason if scope.read_all else None,
        reopen_reason=case.reopen_reason if scope.read_all else None,
        revision=case.revision,
        deadlines=[
            DeadlineTaskResponse(
                id=task.id,
                kind=task.kind,
                due_at=task.due_at,
                status=task.status,
                not_applicable_reason=task.not_applicable_reason,
                revision=task.revision,
            )
            for task in case.deadlines
        ],
    )


def _case_summary(case: Case, scores: ImpactScores | None) -> CaseSummary:
    return CaseSummary(
        id=case.id,
        reference=case.reference,
        title=case.title,
        awareness_time=case.awareness_time,
        status=case.status,
        created_at=case.created_at,
        next_deadline=case.next_deadline,
        # list_visible_cases always sets updated_at; created_at is the fallback for a case built without it.
        updated_at=case.updated_at or case.created_at,
        **risk_fields(scores),
    )


def _assignment_response(assignment: Assignment) -> AssignmentResponse:
    return AssignmentResponse(
        id=assignment.id,
        case_id=assignment.case_id,
        user_id=assignment.user_id,
        username=assignment.username,
        role=assignment.role,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
    )


def _visible_case_or_404(case_id: uuid.UUID, scope: CaseScope) -> Case:
    case = find_visible_case(str(case_id), scope)
    if case is None:
        raise CASE_NOT_FOUND
    return case


@app.post("/api/v1/cases", status_code=201, response_model=CaseResponse)
def submit_case(
    body: CaseCreateRequest,
    actor: User = Depends(require_permission("cases:submit")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseResponse:
    scope_key = key_scope(
        actor.id,
        "POST",
        CASES_ROUTE,
        checked_key(idempotency_key),
        CASES_ROUTE,
        body.model_dump(mode="json"),
    )
    try:
        case = create_case(
            body.title,
            body.awareness_time,
            created_by=actor.id,
            idempotency=scope_key,
            render=lambda created: _case_response(created, _scope(actor)).model_dump(
                mode="json"
            ),
        )
    except TimezoneNotConfigured:
        raise HTTPException(status_code=409, detail="system timezone is not configured")
    return _case_response(case, _scope(actor))


class ReportCreateRequest(CaseCreateRequest):
    # Optional way to reach the reporter. Kept apart from the case content and never returned by the API.
    contact: str | None = Field(default=None, max_length=200)

    @field_validator("contact")
    @classmethod
    def contact_stripped(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


@app.post("/api/v1/reports", status_code=201, response_model=CaseResponse)
def open_report(
    body: ReportCreateRequest,
    actor: User = Depends(require_permission("cases:submit")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseResponse:
    """Open a case through the reporter channel. The case, its deadlines, and the reporter identity are stored together."""
    # The contact is part of the request, so it is in the fingerprint; it is never part of a stored response.
    scope_key = key_scope(
        actor.id,
        "POST",
        REPORTS_ROUTE,
        checked_key(idempotency_key),
        REPORTS_ROUTE,
        body.model_dump(mode="json"),
    )
    try:
        case = submit_report(
            body.title,
            body.awareness_time,
            body.contact,
            actor.id,
            idempotency=scope_key,
            render=lambda created: _case_response(created, _scope(actor)).model_dump(
                mode="json"
            ),
        )
    except TimezoneNotConfigured:
        raise HTTPException(status_code=409, detail="system timezone is not configured")
    return _case_response(case, _scope(actor))


class ReporterContactResponse(BaseModel):
    case_id: str
    contact: str | None


@app.get("/api/v1/reports/{case_id}/contact", response_model=ReporterContactResponse)
def get_reporter_contact(
    case_id: uuid.UUID,
    actor: User = Depends(require_permission("reporter_contact:read")),
) -> ReporterContactResponse:
    """The contact the reporter gave. Audited in the same transaction as the read.

    Readable while the case is open, or within REPORTER_CONTACT_WINDOW_HOURS (72) after its latest closure. A case
    closed longer ago is refused with 409 and detail status reporter_contact_window_closed; the refusal writes no
    audit row. A reopened case is open again and readable. A case with no reporter contact is 404.
    """
    outcome, contact = read_reporter_contact(str(case_id), _scope(actor))
    if outcome is ContactOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is ContactOutcome.NO_REPORTER:
        raise HTTPException(status_code=404, detail="reporter contact not found")
    if outcome is ContactOutcome.WINDOW_CLOSED:
        raise HTTPException(
            status_code=409, detail={"status": "reporter_contact_window_closed"}
        )
    return ReporterContactResponse(case_id=str(case_id), contact=contact)


async def _read_import_body(request: Request) -> bytes:
    """Read the import file with a size limit. A request with no Content-Length cannot exceed it either."""
    message = f"the import file exceeds the limit of {MAX_IMPORT_BYTES} bytes"
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=message)
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=message)
    return bytes(received)


def _import_response(results: list[RecordResult]) -> CaseImportResponse:
    counts = {
        status: 0
        for status in ("imported", "duplicate", "conflict", "rejected", "failed")
    }
    for result in results:
        counts[result.status] += 1
    return CaseImportResponse(
        **counts,
        results=[
            CaseImportRecordResult(
                index=result.index,
                status=result.status,
                source_case_id=result.source_case_id,
                case_id=result.case_id,
                reference=result.reference,
                errors=list(result.errors),
            )
            for result in results
        ],
    )


@app.post("/api/v1/cases/import", response_model=CaseImportResponse)
async def import_cases(
    request: Request,
    actor: User = Depends(require_permission("cases:submit")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseImportResponse:
    """Import case records in the project format. Each record is stored or refused on its own; see safe_kit.case_import.

    The fingerprint is the SHA-256 of the file. A repeated import with the same key gets the stored answer of the first.
    """
    key = checked_key(idempotency_key)
    body = await _read_import_body(request)
    try:
        records = parse_import_file(body)
    except ImportFileError as error:
        raise HTTPException(status_code=422, detail=str(error))
    scope_key = key_scope(
        actor.id,
        "POST",
        IMPORT_ROUTE,
        key,
        IMPORT_ROUTE,
        {"file_sha256": hashlib.sha256(body).hexdigest()},
    )
    try:
        results = await run_in_threadpool(
            import_records,
            records,
            _scope(actor),
            idempotency=scope_key,
            render=lambda done: _import_response(done).model_dump(mode="json"),
        )
    except TimezoneNotConfigured:
        raise HTTPException(status_code=409, detail="system timezone is not configured")
    return _import_response(results)


def _dry_run_response(
    results: list[DryRunResult],
    received: int | str | None,
    accepted: bool,
    file_errors: list[str],
) -> CaseImportDryRunResponse:
    counts = {
        status: 0 for status in ("would_import", "duplicate", "conflict", "rejected")
    }
    for result in results:
        counts[result.status] += 1
    return CaseImportDryRunResponse(
        import_format_version=ImportFormatCheck(
            supported=IMPORT_FORMAT_VERSION, received=received, accepted=accepted
        ),
        record_schema_version=RECORD_SCHEMA_VERSION,
        file_errors=file_errors,
        **counts,
        results=[
            CaseImportDryRunRecordResult(
                index=result.index,
                status=result.status,
                source_case_id=result.source_case_id,
                case_id=result.case_id,
                reference=result.reference,
                title=result.title,
                awareness_time=result.awareness_time.isoformat()
                if result.awareness_time
                else None,
                errors=list(result.errors),
            )
            for result in results
        ],
    )


@app.post("/api/v1/cases/import/dry-run", response_model=CaseImportDryRunResponse)
async def dry_run_case_import(
    request: Request,
    actor: User = Depends(require_permission("cases:submit")),
) -> CaseImportDryRunResponse:
    """Check an import file and report what the commit route (POST /api/v1/cases/import) would do. Nothing is written.

    The validation and the duplicate and conflict rules are those of the commit route. The one difference is the answer
    to a file-level problem: the commit refuses it with 422, while the dry run answers 200 with the problem in
    file_errors and the declared format version in import_format_version, so the caller sees the whole report.
    """
    body = await _read_import_body(request)
    received = received_format_version(body)
    accepted = type(received) is int and received == IMPORT_FORMAT_VERSION
    try:
        records = parse_import_file(body)
    except ImportFileError as error:
        return _dry_run_response([], received, accepted, [str(error)])
    try:
        results = await run_in_threadpool(preview_records, records, _scope(actor))
    except TimezoneNotConfigured:
        raise HTTPException(status_code=409, detail="system timezone is not configured")
    return _dry_run_response(results, received, accepted, [])


@app.get("/api/v1/cases", response_model=CasePage)
def list_cases(
    limit: int = Query(default=50, ge=1, le=200),
    # Opaque. Omit it for the first page; use next_cursor from the previous page for the next one.
    cursor: str | None = Query(default=None, max_length=200),
    q: str | None = Query(default=None, max_length=100),
    # Omitted means every status. Any other value is refused with 422 (the Literal type).
    status: CaseStatus | None = Query(default=None),
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS)),
) -> CasePage:
    # q matches the reference (partial) or the id prefix. status and q both narrow the cases the caller may already see.
    permissions = permissions_of(actor)
    scope = _scope(actor)
    search = q.strip() if q is not None else None
    include_evidence = "evidence:read" in permissions
    after = None
    if cursor is not None:
        after = decode_cursor(cursor)
        if after is None:
            raise HTTPException(status_code=400, detail={"status": "invalid_cursor"})
    # One extra row tells whether another page exists; it is not returned.
    page_cases = list_visible_cases(
        scope,
        limit + 1,
        after,
        q=search,
        status=status,
        include_evidence=include_evidence,
    )
    has_more = len(page_cases) > limit
    page_cases = page_cases[:limit]
    scores = current_scores_for_cases(case.id for case in page_cases)
    return CasePage(
        items=[_case_summary(case, scores.get(case.id)) for case in page_cases],
        next_cursor=encode_cursor(page_cases[-1].created_at, page_cases[-1].id)
        if has_more
        else None,
    )


@app.get("/api/v1/cases/{case_id}", response_model=CaseResponse)
def get_case(
    case_id: uuid.UUID,
    response: Response,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS)),
) -> CaseResponse:
    # A case the caller may not see gets the same 404 as a missing one, so existence is not revealed.
    scope = _scope(actor)
    case = _visible_case_or_404(case_id, scope)
    response.headers["ETag"] = etag_for(case.revision)
    return _case_response(case, scope)


class CaseTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


@app.patch("/api/v1/cases/{case_id}", response_model=CaseResponse)
def update_case(
    case_id: uuid.UUID,
    body: CaseTitleRequest,
    response: Response,
    actor: User = Depends(require_any_permission("cases:triage")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    # The ETag the caller read (see GET). Required: a change without it is refused with 428.
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CaseResponse:
    expected_revision = if_match_revision(if_match)
    scope = _scope(actor)
    scope_key = key_scope(
        actor.id,
        "PATCH",
        CASE_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}",
        body.model_dump(mode="json"),
    )
    outcome, revision = update_case_title(
        str(case_id),
        body.title,
        expected_revision,
        scope,
        idempotency=scope_key,
        render=lambda changed: _case_response(changed, scope).model_dump(mode="json"),
    )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.REVISION_MISMATCH:
        # Only the current revision, in the body and as the ETag, like the review layer (reviews_api.py).
        raise HTTPException(
            status_code=412,
            detail={"status": "precondition_failed", "current_revision": revision},
            headers={"ETag": etag_for(revision)},
        )
    case = _visible_case_or_404(case_id, scope)
    response.headers["ETag"] = etag_for(case.revision)
    return _case_response(case, scope)


@app.get("/api/v1/cases/{case_id}/assignments", response_model=list[AssignmentResponse])
def get_case_assignments(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS)),
) -> list[AssignmentResponse]:
    assignments = list_assignments(str(case_id), _scope(actor))
    if assignments is None:
        raise CASE_NOT_FOUND
    return [_assignment_response(assignment) for assignment in assignments]


@app.get(
    "/api/v1/cases/{case_id}/assignable-users",
    response_model=list[AssignableUserResponse],
)
def get_assignable_users(
    case_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:assign")),
) -> list[AssignableUserResponse]:
    """List the users the assignment picker may offer on this case.

    It exists so that a cases:assign holder can pick an assignee without users:manage, which GET /api/v1/users
    needs. It is read-only, so it writes no audit row. Only name and role are returned, no email or timestamps.
    """
    users_for_case = list_assignable_users(str(case_id), _scope(actor))
    if users_for_case is None:
        raise CASE_NOT_FOUND
    return [
        AssignableUserResponse(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            role=user.role,
        )
        for user in users_for_case
    ]


@app.post(
    "/api/v1/cases/{case_id}/assignments",
    status_code=201,
    response_model=AssignmentResponse,
)
def add_case_assignment(
    case_id: uuid.UUID,
    body: AssignmentRequest,
    actor: User = Depends(require_permission("cases:assign")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AssignmentResponse:
    scope_key = key_scope(
        actor.id,
        "POST",
        ASSIGNMENTS_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}/assignments",
        body.model_dump(mode="json"),
    )
    outcome, assignment = assign_member(
        str(case_id),
        str(body.user_id),
        _scope(actor),
        idempotency=scope_key,
        render=lambda created: _assignment_response(created).model_dump(mode="json"),
    )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.ASSIGNEE_INACTIVE:
        raise HTTPException(status_code=422, detail="assignee is not an active user")
    if outcome is Outcome.ROLE_NOT_ASSIGNABLE:
        raise HTTPException(
            status_code=422, detail="assignee must be an investigator or reviewer"
        )
    if outcome is Outcome.DUPLICATE:
        raise HTTPException(
            status_code=409, detail="user is already assigned to this case"
        )
    assert assignment is not None
    return _assignment_response(assignment)


@app.delete("/api/v1/cases/{case_id}/assignments/{user_id}", status_code=204)
def remove_case_assignment(
    case_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:assign")),
) -> Response:
    outcome = unassign_member(str(case_id), str(user_id), _scope(actor))
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.ASSIGNMENT_NOT_FOUND:
        raise HTTPException(status_code=404, detail="assignment not found")
    return Response(status_code=204)


@app.post("/api/v1/cases/{case_id}/close", response_model=CaseResponse)
def close_case_endpoint(
    case_id: uuid.UUID,
    body: ReasonRequest,
    actor: User = Depends(require_permission("cases:close")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseResponse:
    scope = _scope(actor)
    scope_key = key_scope(
        actor.id,
        "POST",
        CLOSE_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}/close",
        body.model_dump(mode="json"),
    )
    outcome, blockers = close_case(
        str(case_id),
        body.reason,
        scope,
        idempotency=scope_key,
        render=lambda closed: _case_response(closed, scope).model_dump(mode="json"),
    )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.BLOCKED:
        raise HTTPException(
            status_code=409, detail="case cannot be closed: " + "; ".join(blockers)
        )
    return _case_response(_visible_case_or_404(case_id, scope), scope)


@app.post("/api/v1/cases/{case_id}/reopen", response_model=CaseResponse)
def reopen_case_endpoint(
    case_id: uuid.UUID,
    body: ReasonRequest,
    actor: User = Depends(require_permission("cases:close")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseResponse:
    scope = _scope(actor)
    scope_key = key_scope(
        actor.id,
        "POST",
        REOPEN_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}/reopen",
        body.model_dump(mode="json"),
    )
    outcome = reopen_case(
        str(case_id),
        body.reason,
        scope,
        idempotency=scope_key,
        render=lambda reopened: _case_response(reopened, scope).model_dump(mode="json"),
    )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.NOT_CLOSED:
        raise HTTPException(status_code=409, detail="case is not closed")
    return _case_response(_visible_case_or_404(case_id, scope), scope)


@app.patch("/api/v1/cases/{case_id}/deadlines/{task_id}", response_model=CaseResponse)
def update_case_deadline(
    case_id: uuid.UUID,
    task_id: uuid.UUID,
    body: DeadlineUpdateRequest,
    response: Response,
    # The disclosure approver holds cases:read_all to read the case it approves, and may not change deadlines.
    actor: User = Depends(
        require_any_permission_except_role(
            DISCLOSURE_APPROVER_ROLE, "cases:read_all", "cases:read_assigned"
        )
    ),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    # The revision of the task the caller read (DeadlineTaskResponse.revision). Required: a change without it is a 428.
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CaseResponse:
    expected_revision = if_match_revision(if_match)
    scope = _scope(actor)
    scope_key = key_scope(
        actor.id,
        "PATCH",
        DEADLINE_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}/deadlines/{task_id}",
        body.model_dump(mode="json"),
    )
    try:
        outcome, revision = update_deadline(
            str(case_id),
            str(task_id),
            body.status,
            body.reason,
            scope,
            may_mark_not_applicable="cases:triage" in permissions_of(actor),
            expected_revision=expected_revision,
            idempotency=scope_key,
            render=lambda changed: _case_response(changed, scope).model_dump(
                mode="json"
            ),
        )
    except Replayed as replayed:
        # The same request again: the first answer. The ETag is the task's current revision.
        case = _visible_case_or_404(case_id, scope)
        task = next((t for t in case.deadlines if t.id == str(task_id)), None)
        headers = {"ETag": etag_for(task.revision)} if task and task.revision else {}
        return JSONResponse(
            status_code=replayed.stored.status_code,
            content=replayed.stored.body,
            headers=headers,
        )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.TASK_NOT_FOUND:
        raise HTTPException(status_code=404, detail="deadline task not found")
    if outcome is Outcome.NOT_APPLICABLE_FORBIDDEN:
        raise HTTPException(
            status_code=403, detail="only a triager may mark a deadline not applicable"
        )
    assert revision is not None
    if outcome is Outcome.REVISION_MISMATCH:
        # Only the task's current revision, in the body and as the ETag. Nothing of the task is returned.
        raise HTTPException(
            status_code=412,
            detail={"status": "precondition_failed", "current_revision": revision},
            headers={"ETag": etag_for(revision)},
        )
    # The ETag names the task that changed (its revision), not the case. The body is the case.
    response.headers["ETag"] = etag_for(revision)
    return _case_response(_visible_case_or_404(case_id, scope), scope)


@app.post(
    "/api/v1/cases/{case_id}/deadlines/{task_id}/restore", response_model=CaseResponse
)
def restore_case_deadline(
    case_id: uuid.UUID,
    task_id: uuid.UUID,
    actor: User = Depends(
        require_any_permission_except_role(
            DISCLOSURE_APPROVER_ROLE, "cases:read_all", "cases:read_assigned"
        )
    ),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseResponse:
    """Turn off the not applicable exception of a deadline task: it goes back to the status it had before."""
    scope = _scope(actor)
    scope_key = key_scope(
        actor.id,
        "POST",
        RESTORE_ROUTE,
        checked_key(idempotency_key),
        f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore",
        {},
    )
    outcome = restore_deadline(
        str(case_id),
        str(task_id),
        scope,
        may_mark_not_applicable="cases:triage" in permissions_of(actor),
        idempotency=scope_key,
        render=lambda restored: _case_response(restored, scope).model_dump(mode="json"),
    )
    if outcome is Outcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is Outcome.CLOSED:
        raise CASE_CLOSED
    if outcome is Outcome.TASK_NOT_FOUND:
        raise HTTPException(status_code=404, detail="deadline task not found")
    if outcome is Outcome.NOT_MARKED_NOT_APPLICABLE:
        raise HTTPException(
            status_code=409, detail={"status": "deadline_not_marked_not_applicable"}
        )
    if outcome is Outcome.NOT_APPLICABLE_FORBIDDEN:
        raise HTTPException(
            status_code=403,
            detail="only a triager may restore a deadline that is not applicable",
        )
    return _case_response(_visible_case_or_404(case_id, scope), scope)


@app.get("/api/v1/cases/{case_id}/history", response_model=HistoryPage)
def get_case_history(
    case_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    # Opaque. Omit it for the first page; use next_cursor from the previous page for the next one.
    cursor: str | None = Query(default=None, max_length=200),
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS)),
) -> HistoryPage:
    permissions = permissions_of(actor)
    after = None
    if cursor is not None:
        after = decode_cursor(cursor)
        if after is None:
            raise HTTPException(status_code=400, detail={"status": "invalid_cursor"})
    result = case_history(
        str(case_id),
        _scope(actor),
        limit,
        after,
        include_evidence="evidence:read" in permissions,
    )
    if result is None:
        raise CASE_NOT_FOUND
    entries, next_cursor = result
    # The reporter's account is never shown, not even to triagers: the reporter must not be exposed to retaliation.
    entries = hide_reporter(entries)
    # Full details go to callers who can read every case (the triager). Everyone else sees action and time only.
    if "cases:read_all" not in permissions:
        entries = redact_history(entries)
    return HistoryPage(
        items=[
            HistoryEntryResponse(
                id=entry.id,
                action=entry.action,
                created_at=entry.created_at,
                actor_id=entry.actor_id,
                details=entry.details,
            )
            for entry in entries
        ],
        next_cursor=next_cursor,
    )


@app.get("/api/v1/privacy/inventory")
def privacy_inventory(
    actor: User = Depends(require_permission("privacy:read")),
) -> dict:
    # Metadata and row counts only. Row contents and secrets are never read or returned here.
    years = retention_years(os.environ)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "retention_years": years,
        "items": inventory(years),
    }


@app.get("/api/v1/privacy/inventory/export")
def privacy_inventory_export(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    actor: User = Depends(require_permission("privacy:read")),
) -> Response:
    generated_at = datetime.now(UTC)
    years = retention_years(os.environ)
    items = inventory(years)
    if format == "json":
        return JSONResponse(
            content={
                "generated_at": generated_at.isoformat(),
                "retention_years": years,
                "items": items,
            },
            headers={
                "Content-Disposition": 'attachment; filename="data-inventory.json"'
            },
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(inventory_csv_rows(items))
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="data-inventory.csv"'},
    )


# Evidence endpoints (see safe_kit.evidence_api).
from safe_kit.evidence_api import evidence_router  # noqa: E402

app.include_router(evidence_router)

# Case record endpoints: awareness events, impacts, legal duties, classification, and the preliminary report.
from safe_kit.case_records_api import case_records_router  # noqa: E402

app.include_router(case_records_router)

# Case link endpoints: a record that two cases concern the same incident.
from safe_kit.case_links_api import case_links_router  # noqa: E402

app.include_router(case_links_router)

# Model record endpoints: the model, runtime and reasoning configuration of an agent run (plans/03 T05).
from safe_kit.model_records_api import model_records_router  # noqa: E402

app.include_router(model_records_router)

# Risk endpoints: the C, B and A impact values of a case and its risk score (see safe_kit.impact_api).
app.include_router(impact_router)

# Weekly update endpoints: the machine-readable updates of a case, read-only (see safe_kit.weekly_updates).
from safe_kit.weekly_updates_api import weekly_updates_router  # noqa: E402

app.include_router(weekly_updates_router)

# Review round endpoints: open a round, read rounds, and set one layer of a round.
from safe_kit.reviews_api import reviews_router  # noqa: E402

app.include_router(reviews_router)

# Conflicts, recusals and appeals on review rounds (safe_kit.review_independence_api).
from safe_kit.review_independence_api import review_independence_router  # noqa: E402

app.include_router(review_independence_router)

# Overview dashboard endpoints: counts and dates, gated by role (see safe_kit.overview_api).
from safe_kit.overview_api import overview_router  # noqa: E402

app.include_router(overview_router)

# Catalog endpoints: recommendations, verification runs, and the versioned catalog (see safe_kit.catalog_api).
from safe_kit.catalog_api import catalog_router  # noqa: E402

app.include_router(catalog_router)

# Disclosure packages: versions, the preview, and the human approval (see safe_kit.disclosure_api).
from safe_kit.disclosure_api import disclosure_router  # noqa: E402

app.include_router(disclosure_router)

# External notices of a disclosure version: recorded sends, receipts, follow-ups and cancels (see safe_kit.external_notices_api).
from safe_kit.external_notices_api import notice_router  # noqa: E402

app.include_router(notice_router)

app.include_router(profile_router)
