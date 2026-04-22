"""
api/auth
--------
User-management subpackage: Supabase-issued JWT verification + a minimal
Neo4j `:User` profile mirror so auction graph edges (stars, feedback,
uploaded documents) can attach to a stable node.

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
