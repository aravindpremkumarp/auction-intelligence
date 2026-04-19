"""
api/auth/rate_limit.py
----------------------
Thin wrapper over slowapi for per-IP rate limits. Disable in tests with
RATELIMIT_DISABLED=1.
"""
from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address


def _disabled() -> bool:
    return os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    enabled=not _disabled(),
)

# Policies referenced from router decorators. Keep values here so they're easy
# to tune without hunting through the router.
LOGIN_LIMIT = "5/minute"
FORGOT_LIMIT = "5/minute"
REGISTER_LIMIT = "10/hour"
ANON_CHAT_LIMIT = "10/hour"
