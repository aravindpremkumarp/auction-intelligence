"""
remove_non_property_categories.py
---------------------------------
Remove auction listings that aren't real estate properties (gold loans,
vehicle auctions, scrap/plant & machinery, miscellaneous "Others") from the
Neo4j graph.

These categories slipped in from the upstream scraper but aren't useful for
the property-investment use case the app is built around.

For each AssetCategory in NON_PROPERTY_CATEGORIES this script:
  1. Deletes Document nodes attached only to auctions in those categories.
  2. Deletes Borrower nodes attached only to those auctions.
  3. DETACH DELETEs the AuctionProperty nodes themselves (also drops any
     SAVED watchlist edges, InvestmentTracker/DecisionTrace links, etc.).
  4. Sweeps any Document node left with no incoming HAS_DOCUMENT edge. This
     is a backstop for step 1, which only catches documents whose ownership
     edges were present and exclusively bad when it ran; a document linked
     after that step (or missed by it) is instead orphaned by step 3's
     DETACH DELETE, which drops the edge but keeps the node. The sweep also
     self-heals orphans left behind by earlier runs.
  5. DETACH DELETEs the AssetCategory nodes once they have no auctions left.
  6. Cleans up orphan PropertyType nodes left with no incoming HAS_PROPERTY_TYPE.

Shared nodes (Bank, Branch, City, State, Area, AuctionType) are kept — they
have edges from the remaining property auctions.

Idempotent: re-running after a clean DB is a no-op.

Run:  python scripts/remove_non_property_categories.py
"""

from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

NON_PROPERTY_CATEGORIES = [
    "Gold Auctions",
    "Others",
    "Scrap, Plant & Machinery",
    "Vehicle Auctions",
]

DELETE_EXCLUSIVE_DOCUMENTS = """
MATCH (a:AuctionProperty)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
WHERE ac.name IN $bad_cats
WITH collect(DISTINCT a) AS bad_auctions
MATCH (d:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
WITH d, collect(DISTINCT a) AS owners, bad_auctions
WHERE all(x IN owners WHERE x IN bad_auctions)
DETACH DELETE d
RETURN count(d) AS deleted
"""

DELETE_EXCLUSIVE_BORROWERS = """
MATCH (a:AuctionProperty)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
WHERE ac.name IN $bad_cats
WITH collect(DISTINCT a) AS bad_auctions
MATCH (b:Borrower)<-[:HAS_BORROWER]-(a:AuctionProperty)
WITH b, collect(DISTINCT a) AS owners, bad_auctions
WHERE all(x IN owners WHERE x IN bad_auctions)
DETACH DELETE b
RETURN count(b) AS deleted
"""

DELETE_AUCTIONS = """
MATCH (a:AuctionProperty)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
WHERE ac.name IN $bad_cats
DETACH DELETE a
RETURN count(a) AS deleted
"""

# Backstop sweep: a Document is only meaningful while an AuctionProperty
# points at it via HAS_DOCUMENT (the sole relationship Documents carry).
# Once its last owner is gone, the node is dead weight, so remove any that
# no property references — including orphans left by earlier runs.
DELETE_ORPHAN_DOCUMENTS = """
MATCH (d:Document)
WHERE NOT (d)<-[:HAS_DOCUMENT]-(:AuctionProperty)
DETACH DELETE d
RETURN count(d) AS deleted
"""

DELETE_ASSET_CATEGORIES = """
MATCH (ac:AssetCategory) WHERE ac.name IN $bad_cats
DETACH DELETE ac
RETURN count(ac) AS deleted
"""

DELETE_ORPHAN_PROPERTY_TYPES = """
MATCH (pt:PropertyType)
WHERE NOT (pt)<-[:HAS_PROPERTY_TYPE]-(:AuctionProperty)
WITH collect(pt) AS orphans, collect(pt.name) AS names
FOREACH (n IN orphans | DETACH DELETE n)
RETURN names
"""

FINAL_STATE = """
MATCH (ac:AssetCategory)
OPTIONAL MATCH (a:AuctionProperty)-[:HAS_ASSET_CATEGORY]->(ac)
RETURN ac.name AS category, count(DISTINCT a) AS auctions
ORDER BY auctions DESC
"""


def main() -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    params = {"bad_cats": NON_PROPERTY_CATEGORIES}

    with driver.session(database=NEO4J_DATABASE) as session:
        print(f"Removing categories: {NON_PROPERTY_CATEGORIES}")

        docs = session.run(DELETE_EXCLUSIVE_DOCUMENTS, params).single()["deleted"]
        print(f"  Documents deleted   : {docs}")

        borrowers = session.run(DELETE_EXCLUSIVE_BORROWERS, params).single()["deleted"]
        print(f"  Borrowers deleted   : {borrowers}")

        auctions = session.run(DELETE_AUCTIONS, params).single()["deleted"]
        print(f"  Auctions deleted    : {auctions}")

        orphan_docs = session.run(DELETE_ORPHAN_DOCUMENTS).single()["deleted"]
        print(f"  Orphan Documents    : {orphan_docs}")

        cats = session.run(DELETE_ASSET_CATEGORIES, params).single()["deleted"]
        print(f"  Categories deleted  : {cats}")

        orphan_types = session.run(DELETE_ORPHAN_PROPERTY_TYPES).single()["names"]
        print(f"  Orphan PropertyTypes: {orphan_types}")

        print("\nRemaining AssetCategory distribution:")
        for row in session.run(FINAL_STATE):
            print(f"  {row['category']:<20} {row['auctions']}")

    driver.close()


if __name__ == "__main__":
    main()
