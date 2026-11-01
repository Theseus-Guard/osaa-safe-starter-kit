import csv
import io
import uuid
from datetime import UTC, datetime

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from safe_kit.auth import (
    INVALID_CREDENTIALS,
    bearer,
    current_user,
    current_user_pending_change,
    require_permission,
)
from safe_kit.audit import record_login_failure
from safe_kit.cases import Case, count_own_cases, create_case, find_own_case, list_own_cases
from safe_kit.db import connect
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
from safe_kit.rbac import RoleOutcome, list_catalog, list_role_permissions, set_role_permissions
from safe_kit.security import (
    MIN_PASSWORD_LENGTH,
    access_token_ttl,
    decode_access_token,
    issue_access_token,
    verify_password,
)
from safe_kit.sessions import (
    InvalidRefreshToken,
    open_session,
    revoke_access_token,
    revoke_refresh_token,
    rotate_session,
)
from safe_kit.users import (
    ROLES,
    InvalidUsername,
    UpdateOutcome,
    User,
    admin_exists,
    change_own_password,
    count_users,
    create_user,
    find_user,
    find_user_by_id,
    list_users,
    reset_password,
    soft_delete_user,
    update_user,
)

app = FastAPI(title="SAFE-Starter-Kit API", version="0.1.0")


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


class UserPage(BaseModel):
    items: list[UserSummary]
    total: int
    limit: int
    offset: int


class RoleSummary(BaseModel):
    role: str
    permissions: list[str]


class PermissionSummary(BaseModel):
    name: str
    description: str


class RolePermissionsRequest(BaseModel):
    permissions: list[str] = Field(max_length=100)


def _client_ip(request: Request) -> str:
    # The gateway overwrites X-Real-IP with the address it received the connection from.
    # This is safe only because the API is reachable solely through the gateway.
    if real_ip := request.headers.get("x-real-ip"):
        return real_ip
    return request.client.host if request.client else "unknown"


def _issue_tokens(user: User, refresh_token: str) -> TokenResponse:
    access_token, _ = issue_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(access_token_ttl().total_seconds()),
        password_change_required=user.must_change_password,
    )


PROTECTED_DETAIL = "the protected admin account cannot be disabled, deleted, or demoted"


def _summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        password_change_required=user.must_change_password,
        created_at=user.created_at,
        is_protected=user.is_protected,
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
def login(body: LoginRequest, request: Request) -> TokenResponse:
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
        record_login_failure(user.id if user else None, reason, body.username, _client_ip(request))
        raise INVALID_CREDENTIALS
    # Until the system timezone is set, only administrators may sign in: they are the ones who set it.
    if user.role != SETUP_ROLE and current_timezone_name() is None:
        raise SETUP_REQUIRED
    clear_rate_limit(f"login:user:{body.username}")
    return _issue_tokens(user, open_session(user.id))


@app.post("/api/v1/auth/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest) -> TokenResponse:
    try:
        user_id, new_refresh_token = rotate_session(body.refresh_token)
    except InvalidRefreshToken:
        raise INVALID_CREDENTIALS
    user = find_user_by_id(user_id)
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS
    return _issue_tokens(user, new_refresh_token)


@app.post("/api/v1/auth/logout", status_code=204)
def logout(
    body: LogoutRequest | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    user: User = Depends(current_user_pending_change),
) -> Response:
    # The access token stays in the revocation list until it would have expired anyway.
    claims = decode_access_token(credentials.credentials)
    revoke_access_token(claims["jti"], datetime.fromtimestamp(claims["exp"], UTC))
    if body is not None and body.refresh_token:
        revoke_refresh_token(body.refresh_token, user.id)
    return Response(status_code=204)


@app.post("/api/v1/auth/password", status_code=204)
def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(current_user_pending_change),
) -> Response:
    # Used both for the forced change at first login and for voluntary changes.
    # The current password is required, so a stolen access token alone cannot change it.
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    change_own_password(user.id, body.new_password)
    # All sessions end, including this one. The client must log in again with the new password.
    return Response(status_code=204)


@app.get("/api/v1/auth/me", response_model=MeResponse)
def me(user: User = Depends(current_user_pending_change)) -> MeResponse:
    return MeResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        password_change_required=user.must_change_password,
        timezone_setup_required=user.role == SETUP_ROLE and current_timezone_name() is None,
    )


def _timezone_response(setting: TimezoneSetting | None) -> SystemTimezoneResponse:
    if setting is None:
        return SystemTimezoneResponse(timezone=None, effective_from=None, utc_offset=None)
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
        raise HTTPException(status_code=409, detail="timezone changed by someone else; reload")
    if outcome is ChangeOutcome.CONFIRMATION_REQUIRED:
        raise HTTPException(status_code=422, detail="confirmation required")
    return _timezone_response(setting)


@app.get("/api/v1/users", response_model=UserPage)
def admin_list_users(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_permission("users:manage")),
) -> UserPage:
    return UserPage(
        items=[_summary(user) for user in list_users(limit, offset)],
        total=count_users(),
        limit=limit,
        offset=offset,
    )


@app.post("/api/v1/users", status_code=201, response_model=UserCreatedResponse)
def admin_create_user(
    body: UserCreateRequest,
    actor: User = Depends(require_permission("users:manage")),
) -> UserCreatedResponse:
    try:
        user_id = create_user(body.username, body.temporary_password, body.role, actor_id=actor.id)
    except InvalidUsername as error:
        raise HTTPException(status_code=422, detail=str(error))
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
    actor: User = Depends(require_permission("users:manage")),
) -> UserSummary:
    if body.role is None and body.is_active is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    # Admins cannot change their own role or status. This keeps a single admin mistake from locking
    # everyone out, and another active admin can always make the change.
    if str(user_id) == actor.id:
        raise HTTPException(status_code=409, detail="cannot change your own role or status")
    outcome = update_user(str(user_id), role=body.role, is_active=body.is_active, actor_id=actor.id)
    if outcome is UpdateOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="user not found")
    if outcome is UpdateOutcome.PROTECTED:
        raise HTTPException(status_code=409, detail=PROTECTED_DETAIL)
    if outcome is UpdateOutcome.LAST_ADMIN:
        raise HTTPException(status_code=409, detail="the last active admin cannot be removed")
    updated = find_user_by_id(str(user_id))
    assert updated is not None
    return _summary(updated)


@app.delete("/api/v1/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: uuid.UUID,
    actor: User = Depends(require_permission("users:manage")),
) -> Response:
    # Soft delete only: the row is kept, the user is disabled and loses its sessions.
    # Physical removal is done by a database administrator, never through the API.
    if str(user_id) == actor.id:
        raise HTTPException(status_code=409, detail="cannot change your own role or status")
    outcome = soft_delete_user(str(user_id), actor_id=actor.id)
    if outcome is UpdateOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="user not found")
    if outcome is UpdateOutcome.PROTECTED:
        raise HTTPException(status_code=409, detail=PROTECTED_DETAIL)
    if outcome is UpdateOutcome.LAST_ADMIN:
        raise HTTPException(status_code=409, detail="the last active admin cannot be removed")
    return Response(status_code=204)


@app.post("/api/v1/users/{user_id}/password", status_code=204)
def admin_reset_password(
    user_id: uuid.UUID,
    body: TemporaryPasswordRequest,
    actor: User = Depends(require_permission("users:reset_password")),
) -> Response:
    # Self-service reset is intentionally not offered. The admin sets a temporary password
    # and the user must replace it at next login.
    if not reset_password(str(user_id), body.temporary_password, actor_id=actor.id):
        raise HTTPException(status_code=404, detail="user not found")
    return Response(status_code=204)


@app.get("/api/v1/rbac/roles", response_model=list[RoleSummary])
def admin_list_roles(actor: User = Depends(require_permission("users:manage"))) -> list[RoleSummary]:
    return [
        RoleSummary(role=role, permissions=permissions)
        for role, permissions in list_role_permissions().items()
    ]


@app.get("/api/v1/rbac/permissions", response_model=list[PermissionSummary])
def admin_list_permissions(actor: User = Depends(require_permission("users:manage"))) -> list[PermissionSummary]:
    return [PermissionSummary(name=name, description=description) for name, description in list_catalog()]


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
    return RoleSummary(role=role, permissions=list_role_permissions()[role])


class DeadlineTaskResponse(BaseModel):
    kind: str
    due_at: datetime
    status: str


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
    deadlines: list[DeadlineTaskResponse]


class CaseSummary(BaseModel):
    id: str
    reference: str
    title: str
    awareness_time: datetime
    status: str
    created_at: datetime


class CasePage(BaseModel):
    items: list[CaseSummary]
    total: int
    limit: int
    offset: int


def _case_response(case: Case) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        reference=case.reference,
        title=case.title,
        awareness_time=case.awareness_time,
        status=case.status,
        calendar_version=case.calendar_version,
        created_at=case.created_at,
        deadlines=[
            DeadlineTaskResponse(kind=task.kind, due_at=task.due_at, status=task.status)
            for task in case.deadlines
        ],
    )


def _case_summary(case: Case) -> CaseSummary:
    return CaseSummary(
        id=case.id,
        reference=case.reference,
        title=case.title,
        awareness_time=case.awareness_time,
        status=case.status,
        created_at=case.created_at,
    )


@app.post("/api/v1/cases", status_code=201, response_model=CaseResponse)
def submit_case(
    body: CaseCreateRequest,
    actor: User = Depends(require_permission("cases:submit")),
) -> CaseResponse:
    try:
        case = create_case(body.title, body.awareness_time, created_by=actor.id)
    except TimezoneNotConfigured:
        raise HTTPException(status_code=409, detail="system timezone is not configured")
    return _case_response(case)


@app.get("/api/v1/cases", response_model=CasePage)
def list_my_cases(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_permission("cases:read_own")),
) -> CasePage:
    return CasePage(
        items=[_case_summary(case) for case in list_own_cases(actor.id, limit, offset)],
        total=count_own_cases(actor.id),
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/cases/{case_id}", response_model=CaseResponse)
def get_my_case(
    case_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:read_own")),
) -> CaseResponse:
    # Another user's case gets the same 404 as a missing one, so existence is not revealed.
    case = find_own_case(str(case_id), actor.id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return _case_response(case)


@app.get("/api/v1/privacy/inventory")
def privacy_inventory(actor: User = Depends(require_permission("privacy:read"))) -> dict:
    # Metadata and row counts only. Row contents and secrets are never read or returned here.
    return {"generated_at": datetime.now(UTC).isoformat(), "items": inventory()}


@app.get("/api/v1/privacy/inventory/export")
def privacy_inventory_export(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    actor: User = Depends(require_permission("privacy:read")),
) -> Response:
    generated_at = datetime.now(UTC)
    items = inventory()
    if format == "json":
        return JSONResponse(
            content={"generated_at": generated_at.isoformat(), "items": items},
            headers={"Content-Disposition": 'attachment; filename="data-inventory.json"'},
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
