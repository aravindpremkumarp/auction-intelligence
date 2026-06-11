"""
api/auth/router.py
------------------
`/auth/me` + `/admin/users*` endpoints. Mounted from api/main.py.

Signup, login, email confirmation, password reset and session refresh are
all handled by Supabase on the client side; the backend only exposes the
Neo4j-mirrored profile and admin toggles.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.auth import repository as repo
from api.auth.dependencies import (
    _user_to_out,
    get_current_admin,
    get_current_user,
)
from api.auth.schemas import (
    AdminUserPatchIn,
    UserOut,
    UserPatchIn,
)


router = APIRouter()


@router.get("/auth/me", response_model=UserOut)
async def me(user: UserOut = Depends(get_current_user)) -> UserOut:
    return user


@router.patch("/auth/me", response_model=UserOut)
async def patch_me(
    body: UserPatchIn,
    user: UserOut = Depends(get_current_user),
) -> UserOut:
    row = await repo.patch_user_self(user.id, body.name)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    # `email_verified` is derived from the JWT, not stored in Neo4j — preserve it.
    row["email_verified"] = user.email_verified
    return _user_to_out(row)


@router.get("/admin/users", response_model=list[UserOut])
async def admin_list_users(
    limit: int = 200,
    _admin: UserOut = Depends(get_current_admin),
) -> list[UserOut]:
    rows = await repo.admin_list_users(limit=limit)
    return [_user_to_out({**r, "email_verified": True}) for r in rows]


@router.patch("/admin/users/{user_id}", response_model=UserOut)
async def admin_patch_user(
    user_id: str,
    body: AdminUserPatchIn,
    _admin: UserOut = Depends(get_current_admin),
) -> UserOut:
    row = await repo.admin_patch_user(user_id, body.role, body.enabled)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_to_out({**row, "email_verified": True})
