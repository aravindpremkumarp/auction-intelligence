"""
api/auth
--------
User-management subpackage: JWT auth, bcrypt passwords, email verification
and password reset via Resend, plus a minimal admin surface.

Public re-exports keep `from api.auth import router, get_current_user` terse.
"""
from __future__ import annotations

from api.auth.dependencies import (
    get_current_admin,
    get_current_user,
    get_optional_user,
    require_verified_user,
)
from api.auth.router import router

__all__ = [
    "router",
    "get_current_user",
    "get_current_admin",
    "get_optional_user",
    "require_verified_user",
]
