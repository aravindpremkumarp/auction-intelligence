"""
migrate_property_types.py
-------------------------
One-shot migration to fix two property_type bugs in the live Neo4j DB:

1. Comma-joined PropertyType node names (one node per source record,
   instead of one node per canonical value).
2. PropertyType attached to shared AssetCategory nodes via
   (:AssetCategory)-[:HAS_TYPE]->(:PropertyType), which leaked types
   across unrelated auctions.

Run AFTER deploying the fixed ingestion code:
    python scripts/load_tn_to_neo4j.py   # repopulate with the new schema
    python scripts/migrate_property_types.py   # delete stale edges + nodes

Idempotent: safe to re-run. Queries connection details from the same
env vars/defaults as load_tn_to_neo4j.py.
"""

from __future__ import annotations

from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI,
    NEO4J_USERNAME,
    NEO4J_PASSWORD,
    NEO4J_DATABASE,
)


DROP_HAS_TYPE = """
MATCH (:AssetCategory)-[r:HAS_TYPE]->(:PropertyType)
DELETE r
RETURN count(r) AS deleted
"""

DROP_COMMA_NODES = """
MATCH (pt:PropertyType)
WHERE pt.name CONTAINS ','
DETACH DELETE pt
RETURN count(pt) AS deleted
"""

COUNT_LEGACY_EDGES = """
MATCH (:AssetCategory)-[r:HAS_TYPE]->(:PropertyType) RETURN count(r) AS n
"""

COUNT_COMMA_NODES = """
MATCH (pt:PropertyType) WHERE pt.name CONTAINS ',' RETURN count(pt) AS n
"""

COUNT_AUCTION_EDGES = """
MATCH (:AuctionProperty)-[r:HAS_PROPERTY_TYPE]->(:PropertyType)
RETURN count(r) AS n
"""


def _run(session, cypher: str, label: str) -> int:
    result = session.run(cypher).single()
    n = result["deleted"] if result and "deleted" in result.keys() else 0
    print(f"  {label}: deleted {n}")
    return n


def main() -> None:
    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        print("\nBEFORE:")
        print(f"  legacy AssetCategory-[:HAS_TYPE]->PropertyType edges : "
              f"{session.run(COUNT_LEGACY_EDGES).single()['n']}")
        print(f"  PropertyType nodes with comma in name                : "
              f"{session.run(COUNT_COMMA_NODES).single()['n']}")
        print(f"  Auction-[:HAS_PROPERTY_TYPE]->PropertyType edges     : "
              f"{session.run(COUNT_AUCTION_EDGES).single()['n']}")

        print("\nRunning migration ...")
        _run(session, DROP_HAS_TYPE, "legacy HAS_TYPE edges")
        _run(session, DROP_COMMA_NODES, "comma-joined PropertyType nodes")

        print("\nAFTER:")
        print(f"  legacy AssetCategory-[:HAS_TYPE]->PropertyType edges : "
              f"{session.run(COUNT_LEGACY_EDGES).single()['n']}")
        print(f"  PropertyType nodes with comma in name                : "
              f"{session.run(COUNT_COMMA_NODES).single()['n']}")
        print(f"  Auction-[:HAS_PROPERTY_TYPE]->PropertyType edges     : "
              f"{session.run(COUNT_AUCTION_EDGES).single()['n']}")

    driver.close()
    print("\nDone. If Auction-[:HAS_PROPERTY_TYPE] count is 0, re-run "
          "scripts/load_tn_to_neo4j.py to repopulate.")


if __name__ == "__main__":
    main()
