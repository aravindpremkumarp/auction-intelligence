"""
api/auth/router.py
------------------
All `/auth/*` and `/admin/users*` endpoints. Mounted from api/main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import email as email_mod
from api.auth import repository as repo
from api.auth.dependencies import (
    _user_to_out,
    get_current_admin,
    get_current_user,
)
from api.auth.rate_limit import (
    FORGOT_LIMIT,
    LOGIN_LIMIT,
    REGISTER_LIMIT,
    limiter,
)
from api.auth.schemas import (
    AdminUserPatchIn,
    AccessOnlyOut,
    ForgotPasswordIn,
    LoginIn,
    RegisterIn,
    ResetPasswordIn,
    TokenOut,
    UserOut,
    UserPatchIn,
    VerifyEmailIn,
)
from api.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_url_safe_token,
    hash_password,
    verify_password,
)


router = APIRouter()
_bearer = HTTPBearer(auto_error=True)

_VERIFY_TTL = timedelta(hours=24)
_RESET_TTL = timedelta(hours=1)


def _tokenpair_for_user(user_row: dict, ua: str | None, ip: str | None) -> TokenOut:
    access = create_access_token(user_row["id"], user_row.get("role", "user"))
    refresh, jti, exp = create_refresh_token(user_row["id"])
    repo.store_refresh_token(jti, user_row["id"], exp, user_agent=ua, ip=ip)
    return TokenOut(access=access, refresh=refresh, user=_user_to_out(user_row))


# ── register / verify ───────────────────────────────────────────────────────
@router.post("/auth/register", response_model=UserOut, status_code=201)
@limiter.limit(REGISTER_LIMIT)
async def register(
    request: Request,
    body: RegisterIn,
    background: BackgroundTasks,
) -> UserOut:
    existing = repo.get_user_by_email(body.email)
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    user = repo.create_user(body.email, hash_password(body.password), body.name)
    if user is None:
        raise HTTPException(status_code=500, detail="failed to create user")

    token = generate_url_safe_token()
    repo.store_verification_token(
        token, user["id"], "verify_email",
        datetime.now(timezone.utc) + _VERIFY_TTL,
    )
    background.add_task(email_mod.send_verification_email, user["email"], token)
    return _user_to_out(user)


@router.post("/auth/verify-email", response_model=UserOut)
async def verify_email(body: VerifyEmailIn) -> UserOut:
    user_id = repo.consume_verification_token(body.token, "verify_email")
    if not user_id:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    repo.set_email_verified(user_id)
    row = repo.get_user_by_id(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_to_out(row)


# ── login / refresh / logout / me ───────────────────────────────────────────
@router.post("/auth/login", response_model=TokenOut)
@limiter.limit(LOGIN_LIMIT)
async def login(request: Request, body: LoginIn) -> TokenOut:
    row = repo.get_user_by_email(body.email)
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not row.get("enabled", True):
        raise HTTPException(status_code=401, detail="account disabled")
    if not row.get("email_verified"):
        raise HTTPException(status_code=403, detail="email not verified")
    repo.update_last_login(row["id"])
    ua = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return _tokenpair_for_user(row, ua, ip)


@router.post("/auth/refresh", response_model=AccessOnlyOut)
async def refresh_token(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> AccessOnlyOut:
    try:
        payload = decode_token(creds.credentials, expected_type="refresh")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="refresh token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    jti = payload.get("jti")
    if not jti or not repo.is_refresh_token_active(jti):
        raise HTTPException(status_code=401, detail="refresh token revoked")
    row = repo.get_user_by_id(payload["sub"])
    if not row or not row.get("enabled", True):
        raise HTTPException(status_code=401, detail="user unavailable")
    # rotate
    repo.revoke_refresh_token(jti)
    access = create_access_token(row["id"], row.get("role", "user"))
    new_refresh, new_jti, exp = create_refresh_token(row["id"])
    ua = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    repo.store_refresh_token(new_jti, row["id"], exp, user_agent=ua, ip=ip)
    return AccessOnlyOut(access=access, refresh=new_refresh)


@router.post("/auth/logout", status_code=204)
async def logout(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> None:
    try:
        payload = decode_token(creds.credentials, expected_type="refresh")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    jti = payload.get("jti")
    if jti:
        repo.revoke_refresh_token(jti)
    return None


@router.get("/auth/me", response_model=UserOut)
async def me(user: UserOut = Depends(get_current_user)) -> UserOut:
    return user


@router.patch("/auth/me", response_model=UserOut)
async def patch_me(
    body: UserPatchIn,
    user: UserOut = Depends(get_current_user),
) -> UserOut:
    row = repo.patch_user_self(user.id, body.name)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_to_out(row)


# ── forgot / reset password ─────────────────────────────────────────────────
@router.post("/auth/forgot-password", status_code=204)
@limiter.limit(FORGOT_LIMIT)
async def forgot_password(
    request: Request,
    body: ForgotPasswordIn,
    background: BackgroundTasks,
) -> None:
    row = repo.get_user_by_email(body.email)
    if row:
        token = generate_url_safe_token()
        repo.store_verification_token(
            token, row["id"], "reset_password",
            datetime.now(timezone.utc) + _RESET_TTL,
        )
        background.add_task(email_mod.send_password_reset_email, row["email"], token)
    return None


@router.post("/auth/reset-password", status_code=204)
async def reset_password(body: ResetPasswordIn) -> None:
    user_id = repo.consume_verification_token(body.token, "reset_password")
    if not user_id:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    repo.set_password_hash(user_id, hash_password(body.new_password))
    return None


# ── admin ───────────────────────────────────────────────────────────────────
@router.get("/admin/users", response_model=list[UserOut])
async def admin_list_users(
    limit: int = 200,
    _admin: UserOut = Depends(get_current_admin),
) -> list[UserOut]:
    rows = repo.admin_list_users(limit=limit)
    return [_user_to_out(r) for r in rows]


@router.patch("/admin/users/{user_id}", response_model=UserOut)
async def admin_patch_user(
    user_id: str,
    body: AdminUserPatchIn,
    _admin: UserOut = Depends(get_current_admin),
) -> UserOut:
    row = repo.admin_patch_user(user_id, body.role, body.enabled)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_to_out(row)
