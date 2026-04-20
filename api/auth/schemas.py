"""
api/auth/schemas.py
-------------------
Request/response Pydantic models for the auth + admin routers.

Identity itself is handled by Supabase, so the backend only exposes:
- `UserOut`: profile view materialised from the Neo4j `:User` mirror.
- `UserPatchIn`: self-edit of the display name.
- `AdminUserPatchIn`: admin toggles for role/enabled.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class UserPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class AdminUserPatchIn(BaseModel):
    role: Literal["user", "admin"] | None = None
    enabled: bool | None = None


class UserOut(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: Literal["user", "admin"]
    enabled: bool
    email_verified: bool
