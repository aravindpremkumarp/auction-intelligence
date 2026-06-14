"""
scripts/init_dossier_schema.py
------------------------------
Idempotent creation of Neo4j constraints + indexes for the private document
dossier (api/dossier). Run once per environment:

    python -m scripts.init_dossier_schema

Labels (kept separate from the public auction graph):
  :Dossier          — a per-user, per-property locker
  :DossierDocument  — one uploaded user document (NOT :Document, which the
                      public notice pipeline owns)
  :UserProperty     — an off-graph property a user vets that isn't scraped
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.neo4j_client import run_query


STATEMENTS = [
    "CREATE CONSTRAINT dossier_id_unique IF NOT EXISTS FOR (d:Dossier) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT dossier_document_id_unique IF NOT EXISTS FOR (x:DossierDocument) REQUIRE x.id IS UNIQUE",
    "CREATE CONSTRAINT user_property_id_unique IF NOT EXISTS FOR (p:UserProperty) REQUIRE p.id IS UNIQUE",
    "CREATE INDEX dossier_owner_idx IF NOT EXISTS FOR (d:Dossier) ON (d.owner_supabase_id)",
    "CREATE INDEX user_property_owner_idx IF NOT EXISTS FOR (p:UserProperty) ON (p.owner_supabase_id)",
]


def main() -> None:
    for stmt in STATEMENTS:
        print(f"→ {stmt}")
        run_query(stmt)
    print("✓ dossier schema initialised")


if __name__ == "__main__":
    main()
