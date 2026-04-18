"""Quick inspect: count edges by type around PropertyType."""
from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

QUERIES = {
    "AuctionProperty total":
        "MATCH (a:AuctionProperty) RETURN count(a) AS n",
    "AuctionProperty -[:HAS_PROPERTY_TYPE]-> PropertyType (current schema)":
        "MATCH (:AuctionProperty)-[r:HAS_PROPERTY_TYPE]->(:PropertyType) RETURN count(r) AS n",
    "AuctionProperty -[:OF_PROPERTY_TYPE]-> PropertyType (stray from earlier backfill)":
        "MATCH (:AuctionProperty)-[r:OF_PROPERTY_TYPE]->(:PropertyType) RETURN count(r) AS n",
    "AssetCategory -[:HAS_TYPE]-> PropertyType (legacy broken)":
        "MATCH (:AssetCategory)-[r:HAS_TYPE]->(:PropertyType) RETURN count(r) AS n",
    "Auctions WITHOUT any HAS_PROPERTY_TYPE edge":
        "MATCH (a:AuctionProperty) WHERE NOT (a)-[:HAS_PROPERTY_TYPE]->() RETURN count(a) AS n",
}


def main() -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DATABASE) as session:
        for label, q in QUERIES.items():
            n = session.run(q).single()["n"]
            print(f"  {n:>6}  {label}")
    driver.close()


if __name__ == "__main__":
    main()
