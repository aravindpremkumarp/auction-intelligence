"""
api/tools/cypher_tools.py
-------------------------
Eight Cypher-backed agent tools that expose the auction knowledge graph to
the PydanticAI agent. Each tool is a parameterized query returning list[dict].
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from api.neo4j_client import run_query
from pipeline.embeddings import embed_text

VECTOR_INDEX_NAME = "property_desc_idx"

_AGG_FIELDS = {"reserve_price_num", "emd_num"}
_AGG_FUNCS = {
    "min":    "min(a.{f})",
    "max":    "max(a.{f})",
    "avg":    "avg(a.{f})",
    "median": "percentileCont(a.{f}, 0.5)",
    "p25":    "percentileCont(a.{f}, 0.25)",
    "p75":    "percentileCont(a.{f}, 0.75)",
}


def search_auctions(
    min_price: float | None = None,
    max_price: float | None = None,
    city: str | None = None,
    property_type: str | None = None,
    asset_category: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 20,
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
) -> dict:
    if aggregations:
        if aggregate_field not in _AGG_FIELDS:
            raise ValueError(
                f"aggregate_field must be one of {sorted(_AGG_FIELDS)}, got {aggregate_field!r}"
            )
        unknown = [a for a in aggregations if a not in _AGG_FUNCS]
        if unknown:
            raise ValueError(
                f"aggregations must be a subset of {sorted(_AGG_FUNCS)}, unknown: {unknown}"
            )

    where = []
    params: dict = {"limit": limit}
    if min_price is not None:
        where.append("a.reserve_price_num >= $min_price"); params["min_price"] = min_price
    if max_price is not None:
        where.append("a.reserve_price_num <= $max_price"); params["max_price"] = max_price
    if starts_after is not None:
        where.append("a.auction_start_dt >= $starts_after"); params["starts_after"] = starts_after.isoformat()
    if starts_before is not None:
        where.append("a.auction_start_dt <= $starts_before"); params["starts_before"] = starts_before.isoformat()

    matches = ["(a:AuctionProperty)"]
    if city:
        matches.append("(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})"); params["city"] = city
    if property_type:
        matches.append("(a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)-[:HAS_TYPE]->(pt:PropertyType {name: $property_type})")
        params["property_type"] = property_type
    if asset_category:
        matches.append("(a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory {name: $asset_category})")
        params["asset_category"] = asset_category

    where_clause = 'WHERE ' + ' AND '.join(where) if where else ''
    match_clause = ', '.join(matches)

    agg_returns = ["count(a) AS total_count"]
    if aggregations:
        for name in aggregations:
            agg_returns.append(f"{_AGG_FUNCS[name].format(f=aggregate_field)} AS {name}")
    agg_cypher = f"MATCH {match_clause} {where_clause} RETURN {', '.join(agg_returns)}"
    agg_rows = run_query(agg_cypher, {k: v for k, v in params.items() if k != "limit"})
    agg_row = agg_rows[0] if agg_rows else {}
    total_count = agg_row.get("total_count", 0)

    results: list[dict] = []
    if limit > 0:
        cypher = f"""
            MATCH {match_clause}
            {where_clause}
            OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
            OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
            OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
            OPTIONAL MATCH (ac)-[:HAS_TYPE]->(pt:PropertyType)
            RETURN a.auction_id AS auction_id, a.title AS title, a.url AS url,
                   a.reserve_price_num AS reserve_price, a.emd_num AS emd,
                   a.auction_start_dt AS auction_start,
                   city.name AS city, area.name AS area,
                   ac.name AS asset_category, pt.name AS property_type
            ORDER BY a.auction_start_dt ASC
            LIMIT $limit
        """
        results = run_query(cypher, params)

    out: dict = {
        "total_count": total_count,
        "returned": len(results),
        "limit": limit,
        "results": results,
    }
    if aggregations:
        out["aggregations"] = {name: agg_row.get(name) for name in aggregations}
    return out


def find_similar_properties(auction_id: str, price_tolerance_pct: float = 25.0, limit: int = 10) -> list[dict]:
    cypher = """
        MATCH (seed:AuctionProperty {auction_id: $auction_id})-[:LOCATED_IN_AREA]->(area:Area)
        MATCH (other:AuctionProperty)-[:LOCATED_IN_AREA]->(area)
        WHERE other.auction_id <> seed.auction_id
          AND other.reserve_price_num >= seed.reserve_price_num * (1 - $tol/100.0)
          AND other.reserve_price_num <= seed.reserve_price_num * (1 + $tol/100.0)
        RETURN other.auction_id AS auction_id, other.title AS title,
               other.reserve_price_num AS reserve_price, area.name AS area
        LIMIT $limit
    """
    return run_query(cypher, {"auction_id": auction_id, "tol": price_tolerance_pct, "limit": limit})


def bank_portfolio(bank_name: str) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:CONDUCTED_BY]->(b:Bank {name: $bank_name})
        RETURN count(a) AS total_auctions,
               avg(a.reserve_price_num) AS avg_reserve,
               min(a.reserve_price_num) AS min_reserve,
               max(a.reserve_price_num) AS max_reserve,
               collect(DISTINCT a.auction_id)[0..20] AS sample_auction_ids
    """
    return run_query(cypher, {"bank_name": bank_name})


def location_analysis(location: str, location_type: str = "city") -> list[dict]:
    label = {"city": "City", "area": "Area", "state": "State"}.get(location_type, "City")
    rel = {"city": "LOCATED_IN_CITY", "area": "LOCATED_IN_AREA", "state": "LOCATED_IN_STATE"}[location_type]
    cypher = f"""
        MATCH (a:AuctionProperty)-[:{rel}]->(loc:{label} {{name: $location}})
        RETURN count(a) AS total,
               avg(a.reserve_price_num) AS avg_reserve,
               percentileCont(a.reserve_price_num, 0.5) AS median_reserve,
               min(a.reserve_price_num) AS min_reserve,
               max(a.reserve_price_num) AS max_reserve
    """
    return run_query(cypher, {"location": location})


def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    cutoff = (datetime.now() + timedelta(days=days)).isoformat()
    cypher = """
        MATCH (a:AuctionProperty)
        WHERE a.application_deadline_dt <= $cutoff
          AND a.application_deadline_dt >= $now
        RETURN a.auction_id AS auction_id, a.title AS title,
               a.application_deadline_dt AS deadline,
               a.reserve_price_num AS reserve_price
        ORDER BY a.application_deadline_dt ASC
        LIMIT $limit
    """
    return run_query(cypher, {"cutoff": cutoff, "now": datetime.now().isoformat(), "limit": limit})


def price_comparison(city: str, property_type: str) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name: $city})
        MATCH (a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)-[:HAS_TYPE]->(:PropertyType {name: $property_type})
        RETURN a.auction_id AS auction_id, a.title AS title,
               a.reserve_price_num AS reserve_price
        ORDER BY a.reserve_price_num ASC
    """
    return run_query(cypher, {"city": city, "property_type": property_type})


def borrower_lookup(borrower_name: str) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:HAS_BORROWER]->(b:Borrower)
        WHERE toLower(b.name) CONTAINS toLower($name)
        RETURN a.auction_id AS auction_id, a.title AS title, b.name AS borrower
        LIMIT 50
    """
    return run_query(cypher, {"name": borrower_name})


def semantic_property_search(
    query: str,
    city: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    asset_category: str | None = None,
    limit: int = 20,
) -> dict:
    """Vector search over AuctionProperty.description with optional structured post-filters.

    Runs a kNN over the `property_desc_idx` vector index, then filters by
    city / price / asset_category. Returns {returned, limit, results} where
    each result carries a `score` (cosine similarity, higher is better).
    """
    qvec = embed_text(query)
    k = max(limit * 5, 50)

    where = []
    params: dict = {"qvec": qvec, "k": k, "limit": limit}
    if min_price is not None:
        where.append("p.reserve_price_num >= $min_price"); params["min_price"] = min_price
    if max_price is not None:
        where.append("p.reserve_price_num <= $max_price"); params["max_price"] = max_price

    optional_matches = ""
    if city:
        optional_matches += "\nMATCH (p)-[:LOCATED_IN_CITY]->(:City {name: $city})"
        params["city"] = city
    if asset_category:
        optional_matches += "\nMATCH (p)-[:HAS_ASSET_CATEGORY]->(:AssetCategory {name: $asset_category})"
        params["asset_category"] = asset_category

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    cypher = f"""
        CALL db.index.vector.queryNodes('{VECTOR_INDEX_NAME}', $k, $qvec)
        YIELD node AS p, score
        {optional_matches}
        {where_clause}
        RETURN p.auction_id AS auction_id, p.title AS title, p.url AS url,
               p.reserve_price_num AS reserve_price,
               p.auction_start_dt AS auction_start,
               substring(p.description, 0, 300) AS description_excerpt,
               score
        ORDER BY score DESC
        LIMIT $limit
    """
    results = run_query(cypher, params)
    return {"returned": len(results), "limit": limit, "results": results}


def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction: every stored node property plus related
    entities. Uses properties(a) so new schema fields auto-surface with no
    tool change."""
    cypher = """
        MATCH (a:AuctionProperty {auction_id: $auction_id})
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        OPTIONAL MATCH (a)-[:LOCATED_IN_STATE]->(state:State)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(borrower:Borrower)
        OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
        OPTIONAL MATCH (ac)-[:HAS_TYPE]->(pt:PropertyType)
        OPTIONAL MATCH (a)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
        WITH a, city, area, state, bank, borrower, ac, pt,
             collect(DISTINCT properties(s)) AS survey_numbers
        RETURN properties(a) AS fields,
               {
                 city:           CASE WHEN city     IS NULL THEN NULL ELSE properties(city)     END,
                 area:           CASE WHEN area     IS NULL THEN NULL ELSE properties(area)     END,
                 state:          CASE WHEN state    IS NULL THEN NULL ELSE properties(state)    END,
                 bank:           CASE WHEN bank     IS NULL THEN NULL ELSE properties(bank)     END,
                 borrower:       CASE WHEN borrower IS NULL THEN NULL ELSE properties(borrower) END,
                 asset_category: CASE WHEN ac       IS NULL THEN NULL ELSE properties(ac)       END,
                 property_type:  CASE WHEN pt       IS NULL THEN NULL ELSE properties(pt)       END,
                 survey_numbers: survey_numbers
               } AS relationships
    """
    rows = run_query(cypher, {"auction_id": auction_id})
    if not rows:
        return None
    fields = dict(rows[0]["fields"])

    extras_raw = fields.get("extras")
    if isinstance(extras_raw, str) and extras_raw.strip().startswith(("{", "[")):
        try:
            fields["extras"] = json.loads(extras_raw)
        except json.JSONDecodeError:
            pass

    return {"auction_id": auction_id, "fields": fields, "relationships": rows[0]["relationships"]}


def survey_search(survey_no: str, subdivision: str | None = None) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
        WHERE s.survey_no = $survey_no
          AND ($subdivision IS NULL OR s.subdivision = $subdivision)
        RETURN a.auction_id AS auction_id, a.title AS title,
               s.survey_no AS survey_no, s.subdivision AS subdivision, s.survey_type AS survey_type
    """
    return run_query(cypher, {"survey_no": survey_no, "subdivision": subdivision})
