"""
api/auth/supabase_jwt.py
------------------------
Verify Supabase-issued access tokens.

Supabase rotated the default signing path to asymmetric keys (ES256) in 2024;
JWKS survives key rotation and avoids shipping a shared secret to every
service, so we use PyJWT's PyJWKClient against
`${SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

The JWKS client caches keys in-process; on a `kid` miss it refetches.
"""
from __future__ import annotations

import os
from typing import Any

import jwt
from jwt import PyJWKClient


_JWKS_CLIENT: PyJWKClient | None = None


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not configured")
    return url


def _jwks_client() -> PyJWKClient:
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        _JWKS_CLIENT = PyJWKClient(
            f"{_supabase_url()}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=3600,
            # Keys are cached for an hour, so a hung refresh would otherwise
            # block every authenticated request until Supabase recovers.
            timeout=10,
        )
    return _JWKS_CLIENT


def verify_access_token(token: str) -> dict[str, Any]:
    """Decode + validate a Supabase access token. Raises jwt.InvalidTokenError on failure."""
    signing_key = _jwks_client().get_signing_key_from_jwt(token).key
    issuer = f"{_supabase_url()}/auth/v1"
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["ES256", "RS256"],
        audience="authenticated",
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )
    if claims.get("role") == "anon":
        raise jwt.InvalidTokenError("anon role rejected")
    return claims
