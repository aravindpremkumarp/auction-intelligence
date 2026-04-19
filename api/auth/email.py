"""
api/auth/email.py
-----------------
Resend REST client for transactional emails (verification + password reset).

If RESEND_API_KEY is unset, send_* become no-ops that log — useful for tests
and first-boot before the sending domain is verified.
"""
from __future__ import annotations

import logging
import os

import httpx


_LOG = logging.getLogger("auth.email")
_RESEND_URL = "https://api.resend.com/emails"


def _api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def _from_addr() -> str:
    return os.environ.get("RESEND_FROM", "onboarding@resend.dev").strip()


def _app_base() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:5173").rstrip("/")


async def _send(to: str, subject: str, html: str) -> None:
    key = _api_key()
    if not key:
        _LOG.info("[resend-noop] to=%s subject=%s", to, subject)
        return
    payload = {"from": _from_addr(), "to": [to], "subject": subject, "html": html}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(_RESEND_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            _LOG.warning("resend non-2xx: %s %s", r.status_code, r.text[:300])
    except Exception as e:
        _LOG.warning("resend send failed: %r", e)


async def send_verification_email(to: str, token: str) -> None:
    link = f"{_app_base()}/verify?token={token}"
    html = f"""
    <p>Welcome to Bank Auction Intelligence.</p>
    <p>Please verify your email by clicking the link below:</p>
    <p><a href="{link}">{link}</a></p>
    <p>This link expires in 24 hours.</p>
    """
    await _send(to, "Verify your email", html)


async def send_password_reset_email(to: str, token: str) -> None:
    link = f"{_app_base()}/reset?token={token}"
    html = f"""
    <p>We received a request to reset your password.</p>
    <p>Click the link below to choose a new password:</p>
    <p><a href="{link}">{link}</a></p>
    <p>If you did not request this, ignore this email. The link expires in 1 hour.</p>
    """
    await _send(to, "Reset your password", html)
