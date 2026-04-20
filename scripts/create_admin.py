"""
scripts/create_admin.py
-----------------------
Promote a Supabase-authenticated user to admin in the Neo4j `:User` mirror.

The user must already exist in Supabase (signed up via the web UI or via
Supabase dashboard). This script looks them up by email with the service-role
key, then MERGEs the Neo4j profile row keyed by `supabase_id` and sets
role='admin'.

Examples:
    python -m scripts.create_admin --email you@example.com
    ADMIN_BOOTSTRAP_EMAIL=you@example.com python -m scripts.create_admin
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from api.neo4j_client import run_query


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lookup_supabase_user(email: str) -> dict | None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    # The Supabase admin list endpoint doesn't filter server-side by email in
    # all versions; paginate and match locally (admin lists are small).
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    needle = email.lower()
    page = 1
    while True:
        r = httpx.get(
            f"{url}/auth/v1/admin/users",
            params={"page": page, "per_page": 200},
            headers=headers,
            timeout=10.0,
        )
        r.raise_for_status()
        body = r.json()
        users = body.get("users", body) if isinstance(body, dict) else body
        for u in users or []:
            if (u.get("email") or "").lower() == needle:
                return u
        if not users or len(users) < 200:
            return None
        page += 1


def promote_admin(sub: str, email: str, name: str) -> None:
    run_query(
        """
        MERGE (u:User {supabase_id: $sub})
          ON CREATE SET u.created_at = datetime($now)
        SET u.email = toLower($email),
            u.name = coalesce(u.name, $name),
            u.role = 'admin',
            u.enabled = true,
            u.last_login_at = datetime($now)
        """,
        {"sub": sub, "email": email, "name": name, "now": _utcnow_iso()},
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--email", help="Admin email (must already exist in Supabase)")
    args = p.parse_args()

    email = args.email or os.environ.get("ADMIN_BOOTSTRAP_EMAIL") or ""
    if not email:
        p.error("--email required (or set ADMIN_BOOTSTRAP_EMAIL)")

    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            p.error(f"{var} must be set")

    user = _lookup_supabase_user(email)
    if not user:
        p.error(
            f"no Supabase user for {email} — sign up through the web UI first, "
            "then rerun this script"
        )

    sub = user["id"]
    name = (user.get("user_metadata") or {}).get("name") or ""
    promote_admin(sub, email, name)
    print(f"✓ admin ready: supabase_id={sub} email={email}")


if __name__ == "__main__":
    main()
