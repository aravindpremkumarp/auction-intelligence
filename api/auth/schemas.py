"""
api/auth/schemas.py
-------------------
Request/response Pydantic models for the auth + admin routers.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


_PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,128}$")


def _validate_password(v: str) -> str:
    if not _PASSWORD_RE.match(v):
        raise ValueError(
            "password must be 8-128 chars and contain at least one letter and one digit"
        )
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    name: str = Field(min_length=1, max_length=120)

    @field_validator("password")
    @classmethod
    def _p(cls, v: str) -> str:
        return _validate_password(v)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailIn(BaseModel):
    token: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _p(cls, v: str) -> str:
        return _validate_password(v)


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
    email_verified: bool
    enabled: bool


class TokenOut(BaseModel):
    access: str
    refresh: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class AccessOnlyOut(BaseModel):
    access: str
    refresh: str | None = None
    token_type: Literal["bearer"] = "bearer"
