"""
api/chat/gating.py
------------------
Per-turn gating shared by the v1 chat router and the v2 tiered loop: the
durable day + month chat quota, and the tier-aware model / reasoning-effort
resolution.

Extracted from `api/chat/router.py` when /chat/v2 arrived. It has to be
shared rather than copied: the quota is a counter, and two implementations
bumping it independently would let a caller spend the same allowance twice
by alternating endpoints.

Names keep their leading underscore — they are internal to the chat package,
and the router still refers to several of them by their original names.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from api.auth import repository as auth_repo
from api.auth.schemas import UserOut
from api.model_selection import resolve_chat_model, resolve_reasoning_effort

logger = logging.getLogger(__name__)

_CHAT_ANON_DAILY_LIMIT_DEFAULT = 10
_CHAT_ANON_MONTHLY_LIMIT_DEFAULT = 30
_CHAT_FREE_DAILY_LIMIT_DEFAULT = 20
_CHAT_FREE_MONTHLY_LIMIT_DEFAULT = 100
_CHAT_PAID_DAILY_LIMIT_DEFAULT = 1000


def _ratelimit_disabled() -> bool:
    return os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _anon_caps() -> tuple[int, int]:
    """(daily, monthly) caps for anonymous callers."""
    return (
        _int_env("CHAT_ANON_DAILY_LIMIT", _CHAT_ANON_DAILY_LIMIT_DEFAULT),
        _int_env("CHAT_ANON_MONTHLY_LIMIT", _CHAT_ANON_MONTHLY_LIMIT_DEFAULT),
    )


def _user_caps(user: UserOut) -> tuple[int, int | None]:
    """(daily, monthly) caps for a logged-in user. Paid has no monthly cap."""
    if user.tier == "paid":
        return _int_env("CHAT_PAID_DAILY_LIMIT", _CHAT_PAID_DAILY_LIMIT_DEFAULT), None
    return (
        _int_env("CHAT_FREE_DAILY_LIMIT", _CHAT_FREE_DAILY_LIMIT_DEFAULT),
        _int_env("CHAT_FREE_MONTHLY_LIMIT", _CHAT_FREE_MONTHLY_LIMIT_DEFAULT),
    )


def _today_bucket() -> str:
    """UTC day key for the quota window (e.g. ``20260614``)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _month_bucket() -> str:
    """UTC month key for the quota window (e.g. ``202606``)."""
    return datetime.now(timezone.utc).strftime("%Y%m")


def _hash_ip(ip: str) -> str:
    """Stable, non-reversible key for an anon caller. Salted so the stored
    counters aren't a plain list of visitor IPs."""
    salt = os.environ.get("QUOTA_IP_SALT", "auctionscope-quota")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


async def _enforce_anon_chat_quota(request: Request) -> None:
    """Durable per-IP day + month cap for anonymous callers. One atomic Cypher
    bump per turn (correct under concurrency, survives restarts); the UTC
    buckets reset their windows implicitly. Fails open on a Neo4j hiccup so a
    DB blip can't take chat down — the prepaid OpenRouter key is the hard
    backstop either way."""
    if _ratelimit_disabled():
        return
    ip = request.client.host if request.client else "unknown"
    try:
        counts = await auth_repo.bump_anon_quota(
            _hash_ip(ip), _today_bucket(), _month_bucket()
        )
    except Exception:  # noqa: BLE001 - availability over strict enforcement
        logger.exception("anon chat quota check failed — failing open")
        return
    daily, monthly = _anon_caps()
    if counts["day"] > daily:
        raise HTTPException(
            status_code=429,
            detail="daily chat limit reached — log in for more, or try again tomorrow",
        )
    if counts["month"] > monthly:
        raise HTTPException(
            status_code=429,
            detail="monthly chat limit reached — log in for a higher limit",
        )


async def _enforce_user_chat_quota(user: UserOut) -> None:
    """Durable, tier-aware day + month cap, keyed by account (IP-independent).
    One atomic Cypher bump per turn, correct under concurrent tabs and durable
    across restarts. Counts attempts, and fails open on a Neo4j hiccup so a DB
    blip can't take chat down."""
    if _ratelimit_disabled():
        return
    try:
        counts = await auth_repo.bump_chat_quota(
            user.id, _today_bucket(), _month_bucket()
        )
    except Exception:  # noqa: BLE001 - availability over strict enforcement
        logger.exception("chat quota check failed for user=%s — failing open", user.id)
        return
    if counts is None:
        logger.warning("chat quota: no :User row for %s — failing open", user.id)
        return
    daily, monthly = _user_caps(user)
    if counts["day"] > daily:
        detail = (
            "daily chat limit reached — try again tomorrow"
            if user.tier == "paid"
            else "daily chat limit reached — upgrade for more, or try again tomorrow"
        )
        raise HTTPException(status_code=429, detail=detail)
    if monthly is not None and counts["month"] > monthly:
        raise HTTPException(
            status_code=429,
            detail="monthly chat limit reached — upgrade for more, or try again next month",
        )


def resolve_turn_model(user: UserOut | None, model: str | None,
                       reasoning_effort: str | None) -> tuple[str, str | None]:
    """(model_name, effort) for this turn, gated by tier.

    Anonymous callers and the free tier are locked to Flash; only paid users
    can opt into Pro. Reasoning effort is likewise clamped for free/anon
    (reasoning tokens bill as output), so the client toggle only takes effect
    for paid users. Both inputs are advisory — a tampered client cannot
    unlock the pricey model or an unknown effort.
    """
    tier = user.tier if user else "free"
    return resolve_chat_model(tier, model), resolve_reasoning_effort(reasoning_effort, tier)


async def enforce_chat_quota(request: Request, user: UserOut | None) -> None:
    """Bump and check this turn's quota — the anon path for logged-out
    callers, the account path otherwise. Raises HTTPException(429) when a cap
    is exceeded."""
    if user is None:
        await _enforce_anon_chat_quota(request)
    else:
        await _enforce_user_chat_quota(user)
