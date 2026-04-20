"""
scripts/init_auth_schema.py
---------------------------
Idempotent creation of Neo4j constraints + indexes for the `:User` profile
mirror (identity itself lives in Supabase).

Run once per environment:
    python -m scripts.init_auth_schema
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.neo4j_client import run_query


STATEMENTS = [
    "CREATE CONSTRAINT user_supabase_id_unique IF NOT EXISTS FOR (u:User) REQUIRE u.supabase_id IS UNIQUE",
    "CREATE CONSTRAINT user_email_unique IF NOT EXISTS FOR (u:User) REQUIRE u.email IS UNIQUE",
    "CREATE INDEX user_role_idx IF NOT EXISTS FOR (u:User) ON (u.role)",
]


def main() -> None:
    for stmt in STATEMENTS:
        print(f"→ {stmt}")
        run_query(stmt)
    print("✓ auth schema initialised")


if __name__ == "__main__":
    main()
