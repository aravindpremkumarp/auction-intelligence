"""Smoke test: verify the two feedback-failing queries now return data."""
from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)

QUERIES = {
    "Chennai Flats (5fcd2638 case)":
        """
        MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name:'Chennai'})
        MATCH (a)-[:HAS_PROPERTY_TYPE]->(:PropertyType {name:'Flat'})
        RETURN count(a) AS n
        """,
    "Chennai total auctions":
        "MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name:'Chennai'}) RETURN count(a) AS n",
    "Kanchipuram Flats (was inflated to 473 by the old bug)":
        """
        MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name:'Kanchipuram'})
        MATCH (a)-[:HAS_PROPERTY_TYPE]->(:PropertyType {name:'Flat'})
        RETURN count(a) AS n
        """,
    "Ambattur area match (19224426 case)":
        """
        MATCH (a:AuctionProperty)-[:LOCATED_IN_AREA]->(ar:Area)
        WHERE toLower(ar.name) CONTAINS 'ambattur'
        RETURN count(a) AS n
        """,
    "Auction 717410 property_types (72a75404 case, already merged)":
        """
        MATCH (a:AuctionProperty {auction_id:'717410'})-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
        RETURN collect(pt.name) AS types
        """,
}


def main() -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    with driver.session(database=NEO4J_DATABASE) as session:
        for label, q in QUERIES.items():
            row = session.run(q).single()
            if "n" in row.keys():
                print(f"  {row['n']:>5}  {label}")
            else:
                print(f"  {row['types']}  {label}")
    driver.close()


if __name__ == "__main__":
    main()
