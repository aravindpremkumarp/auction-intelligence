"""
api/auth/dependencies.py
------------------------
FastAPI `Depends` helpers for Supabase-authenticated requests.

Every authenticated call materialises/refreshes a Neo4j `:User` row via
`upsert_user_from_supabase` so downstream handlers that attach graph edges
(starred auctions, feedback, uploaded documents) can rely on the node
existing.
"""
from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import repository as repo
from api.auth.schemas import UserOut
from api.auth.supabase_jwt import verify_access_token


_bearer = HTTPBearer(auto_error=False)


def _user_to_out(row: dict) -> UserOut:
    return UserOut(
        id=row["id"],
        email=row["email"],
        name=row.get("name") or "",
        role=row.get("role") or "user",
        enabled=bool(row.get("enabled", True)),
        email_verified=bool(row.get("email_verified", True)),
    )


async def get_optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserOut | None:
    """Return the current user if a valid Supabase access token is present.

    Raises 401 only when a token is sent but invalid/expired/disabled — bare
    requests (no Authorization header) pass through as anonymous.
    """
    if creds is None or not creds.credentials:
        return None
    try:
        claims = verify_access_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    sub = claims.get("sub")
    email = claims.get("email") or ""
    if not sub or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    name = (claims.get("user_metadata") or {}).get("name") or ""
    verified = bool(claims.get("email_confirmed_at") or claims.get("email_verified", True))

    row = await repo.upsert_user_from_supabase(sub, email, name)
    if not row.get("enabled", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account disabled")
    row["email_verified"] = verified
    user = _user_to_out(row)
    request.state.user = user
    return user


async def get_current_user(user: UserOut | None = Depends(get_optional_user)) -> UserOut:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user


async def require_verified_user(user: UserOut = Depends(get_current_user)) -> UserOut:
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="email not verified")
    return user


async def get_current_admin(user: UserOut = Depends(get_current_user)) -> UserOut:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user
