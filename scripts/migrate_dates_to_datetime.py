"""
migrate_dates_to_datetime.py
----------------------------
One-shot migration: convert AuctionProperty date properties from ISO-8601
STRING to native DATETIME, and add range indexes.

Six fields:
  auction_start_dt, auction_end_dt, application_deadline_dt
  auction_start_dt_scraped, auction_end_dt_scraped, application_deadline_dt_scraped

Pre-flight verified live: every populated value matches
'^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}$' (zero malformed).

APOC is not installed; idempotency is guarded with the native
``IS :: STRING`` predicate (Neo4j 5.13+).

Run AFTER deploying the matching write-time casts in
scripts/load_tn_to_neo4j.py and pipeline/load_enriched.py:

    python scripts/migrate_dates_to_datetime.py

Idempotent: safe to re-run.
"""

from __future__ import annotations

from neo4j import GraphDatabase

from scripts.load_tn_to_neo4j import (
    NEO4J_URI,
    NEO4J_USERNAME,
    NEO4J_PASSWORD,
    NEO4J_DATABASE,
)


PRIMARY_FIELDS = (
    "auction_start_dt",
    "auction_end_dt",
    "application_deadline_dt",
)
SCRAPED_FIELDS = (
    "auction_start_dt_scraped",
    "auction_end_dt_scraped",
    "application_deadline_dt_scraped",
)
ALL_DATE_FIELDS = PRIMARY_FIELDS + SCRAPED_FIELDS


def _malformed(session, field: str) -> int:
    cypher = f"""
        MATCH (a:AuctionProperty)
        WHERE a.{field} IS NOT NULL
          AND a.{field} IS :: STRING
          AND NOT a.{field} =~ '^\\\\d{{4}}-\\\\d{{2}}-\\\\d{{2}}T\\\\d{{2}}:\\\\d{{2}}:\\\\d{{2}}$'
        RETURN count(*) AS n
    """
    return session.run(cypher).single()["n"]


def _string_count(session, field: str) -> int:
    # IS :: STRING matches NULL too (nullable type predicate); add IS NOT NULL
    # to count only populated string values.
    return session.run(
        f"MATCH (a:AuctionProperty) "
        f"WHERE a.{field} IS NOT NULL AND a.{field} IS :: STRING "
        "RETURN count(*) AS n"
    ).single()["n"]


def _datetime_count(session, field: str) -> int:
    return session.run(
        "MATCH (a:AuctionProperty) "
        f"WHERE a.{field} IS NOT NULL "
        f"AND (a.{field} IS :: ZONED DATETIME OR a.{field} IS :: LOCAL DATETIME) "
        "RETURN count(*) AS n"
    ).single()["n"]


def _convert(session, field: str) -> int:
    cypher = f"""
        MATCH (a:AuctionProperty)
        WHERE a.{field} IS NOT NULL AND a.{field} IS :: STRING
        SET a.{field} = datetime(a.{field})
        RETURN count(*) AS n
    """
    return session.run(cypher).single()["n"]


INDEX_STATEMENTS = [
    "CREATE INDEX auction_start_dt_idx        IF NOT EXISTS FOR (a:AuctionProperty) ON (a.auction_start_dt)",
    "CREATE INDEX auction_end_dt_idx          IF NOT EXISTS FOR (a:AuctionProperty) ON (a.auction_end_dt)",
    "CREATE INDEX application_deadline_dt_idx IF NOT EXISTS FOR (a:AuctionProperty) ON (a.application_deadline_dt)",
    "CREATE INDEX reserve_price_num_idx       IF NOT EXISTS FOR (a:AuctionProperty) ON (a.reserve_price_num)",
    "CREATE INDEX emd_num_idx                 IF NOT EXISTS FOR (a:AuctionProperty) ON (a.emd_num)",
]


def main() -> None:
    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        print("\nPre-flight (malformed values per field, must be 0):")
        bad_total = 0
        for f in ALL_DATE_FIELDS:
            n = _malformed(session, f)
            bad_total += n
            print(f"  {f:<40} {n}")
        if bad_total:
            print("\nABORT: malformed values found; fix upstream before converting.")
            driver.close()
            return

        print("\nBEFORE (STRING / DATETIME counts per field):")
        for f in ALL_DATE_FIELDS:
            print(f"  {f:<40} STRING={_string_count(session, f):<6} DATETIME={_datetime_count(session, f)}")

        print("\nConverting STRING -> DATETIME ...")
        for f in ALL_DATE_FIELDS:
            n = _convert(session, f)
            print(f"  {f:<40} converted={n}")

        print("\nCreating range indexes (IF NOT EXISTS) ...")
        for stmt in INDEX_STATEMENTS:
            session.run(stmt)
            print(f"  OK: {stmt[:80]}...")

        print("\nAFTER (STRING / DATETIME counts per field):")
        for f in ALL_DATE_FIELDS:
            print(f"  {f:<40} STRING={_string_count(session, f):<6} DATETIME={_datetime_count(session, f)}")

    driver.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
