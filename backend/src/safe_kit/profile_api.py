"""Profile endpoints: a user's own name, title and email, and an administrator's edit of another user's profile.

The router is separate from api.py. It is mounted with app.include_router(profile_router) in api.py.
The rules live in users.py, so the API and the CLI share them.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from safe_kit.auth import current_user, current_user_pending_change, require_permission
from safe_kit.users import (
    PROFILE_FIELDS,
    EmailAlreadyInUse,
    InvalidEmail,
    InvalidProfile,
    User,
    normalize_profile,
    update_profile,
)

router = APIRouter(prefix="/api/v1")

# Input bound only. The field rules (length, email syntax) are applied by normalize_profile, so their messages
# stay the plain ones the owner specified.
INPUT_MAX_LENGTH = 1024


class ProfileResponse(BaseModel):
    first_name: str | None
    last_name: str | None
    title: str | None
    email: str | None


class ProfileUpdateRequest(BaseModel):
    # Every field is optional. A field that is sent with null clears it, except email, which is required.
    # Unknown keys are refused, so a misspelled field name cannot be silently ignored.
    model_config = ConfigDict(extra="forbid")

    first_name: str | None = Field(default=None, max_length=INPUT_MAX_LENGTH)
    last_name: str | None = Field(default=None, max_length=INPUT_MAX_LENGTH)
    title: str | None = Field(default=None, max_length=INPUT_MAX_LENGTH)
    email: str | None = Field(default=None, max_length=INPUT_MAX_LENGTH)


def profile_fields(user: User) -> dict[str, str | None]:
    """The profile values of a user, for building API responses that include them (for example UserSummary)."""
    return {field: getattr(user, field) for field in PROFILE_FIELDS}


def _profile_response(user: User) -> ProfileResponse:
    return ProfileResponse(**profile_fields(user))


def _changes_from(body: ProfileUpdateRequest) -> dict[str, str | None]:
    # Only the fields the caller sent are changed. An absent field keeps its value.
    sent = {field: getattr(body, field) for field in body.model_fields_set}
    if not sent:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        return normalize_profile(sent)
    except (InvalidEmail, InvalidProfile) as error:
        raise HTTPException(status_code=422, detail=str(error))


def _save(
    user_id: str, changes: dict[str, str | None], actor_id: str
) -> ProfileResponse:
    try:
        updated = update_profile(user_id, changes, actor_id=actor_id)
    except EmailAlreadyInUse:
        raise HTTPException(status_code=409, detail="email already in use")
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _profile_response(updated)


@router.get("/auth/profile", response_model=ProfileResponse)
def get_own_profile(
    user: User = Depends(current_user_pending_change),
) -> ProfileResponse:
    # Allowed before the password change, so the change screen can show who is signed in.
    return _profile_response(user)


@router.patch("/auth/profile", response_model=ProfileResponse)
def update_own_profile(
    body: ProfileUpdateRequest, user: User = Depends(current_user)
) -> ProfileResponse:
    return _save(user.id, _changes_from(body), actor_id=user.id)


@router.patch("/users/{user_id}/profile", response_model=ProfileResponse)
def admin_update_user_profile(
    user_id: uuid.UUID,
    body: ProfileUpdateRequest,
    actor: User = Depends(require_permission("users:manage")),
) -> ProfileResponse:
    # Own profile changes go through /auth/profile, so each path has one meaning.
    if str(user_id) == actor.id:
        raise HTTPException(status_code=409, detail="use your own profile endpoint")
    return _save(str(user_id), _changes_from(body), actor_id=actor.id)
