"""
scripts/create_admin.py
-----------------------
Create or promote an admin user. Safe to re-run: MERGEs on email.

Examples:
    python -m scripts.create_admin --email you@example.com --name "You"
    ADMIN_BOOTSTRAP_EMAIL=you@example.com python -m scripts.create_admin --auto
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.auth.security import hash_password
from api.neo4j_client import run_query


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def upsert_admin(email: str, name: str, password: str) -> str:
    rows = run_query(
        """
        MERGE (u:User {email: toLower($email)})
          ON CREATE SET
            u.id = $id,
            u.created_at = datetime($now)
        SET u.password_hash = $hash,
            u.name = $name,
            u.role = 'admin',
            u.email_verified = true,
            u.enabled = true
        RETURN u.id AS id
        """,
        {
            "id": str(uuid.uuid4()),
            "email": email,
            "name": name,
            "hash": hash_password(password),
            "now": _utcnow_iso(),
        },
    )
    return rows[0]["id"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--email", help="Admin email")
    p.add_argument("--name", default="Admin", help="Display name")
    p.add_argument("--password", help="Optional; if omitted, prompted or generated")
    p.add_argument("--auto", action="store_true",
                   help="Use ADMIN_BOOTSTRAP_EMAIL + generate password")
    args = p.parse_args()

    email = args.email or os.environ.get("ADMIN_BOOTSTRAP_EMAIL") or ""
    if not email:
        p.error("--email required (or set ADMIN_BOOTSTRAP_EMAIL)")

    if args.password:
        password = args.password
    elif args.auto:
        password = secrets.token_urlsafe(18)
        print(f"Generated password for {email}: {password}")
        print("(store this now — it will not be shown again)")
    else:
        password = getpass.getpass("Password: ")
        if len(password) < 8:
            p.error("password must be at least 8 characters")

    uid = upsert_admin(email, args.name, password)
    print(f"✓ admin ready: id={uid} email={email}")


if __name__ == "__main__":
    main()
