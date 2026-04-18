"""
cleanup_has_type.py
-------------------
One-off cleanup: remove the misleading (AssetCategory)-[:HAS_TYPE]->(PropertyType)
edges. These were created by the original load_tn_to_neo4j.py at the wrong
level — aggregating per-auction property_type under a shared AssetCategory node
— which is the shared-taxonomy bug we just fixed with per-auction
(AuctionProperty)-[:OF_PROPERTY_TYPE]->(PropertyType) edges.

Run:  python scripts/cleanup_has_type.py
"""

from neo4j import GraphDatabase

# ── Connection (same creds as load_tn_to_neo4j.py) ───────────────────────────
NEO4J_URI      = "neo4j+s://cc513ea9.databases.neo4j.io"
NEO4J_USERNAME = "cc513ea9"
NEO4J_PASSWORD = "ZCgIWawTvdawFfPrwSPnl-kEQF-HEjFR4_iYI-mMT08"
NEO4J_DATABASE = "cc513ea9"

COUNT_QUERY  = "MATCH (:AssetCategory)-[r:HAS_TYPE]->(:PropertyType) RETURN count(r) AS n"
DELETE_QUERY = "MATCH (:AssetCategory)-[r:HAS_TYPE]->(:PropertyType) DELETE r"


def main() -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DATABASE) as session:
        before = session.run(COUNT_QUERY).single()["n"]
        print(f"HAS_TYPE edges before: {before}")

        if before == 0:
            print("Nothing to delete.")
        else:
            session.run(DELETE_QUERY)
            after = session.run(COUNT_QUERY).single()["n"]
            print(f"HAS_TYPE edges after:  {after}")
            print(f"Deleted: {before - after}")

    driver.close()


if __name__ == "__main__":
    main()
