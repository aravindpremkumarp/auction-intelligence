"""
api/tools/cypher_tools.py
-------------------------
Cypher-backed agent tools that expose the auction knowledge graph to the
PydanticAI agent. Specialized tools for common queries plus a read-only
`run_cypher` escape hatch for novel questions.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from api.neo4j_client import run_query, run_read_query
from pipeline.embeddings import embed_text

VECTOR_INDEX_NAME = "property_desc_idx"

# ── run_cypher guardrails ──────────────────────────────────────────────────

# Word-boundary regex matching any mutating clause. Case-insensitive.
# `CALL db.index.*` is allowed because the vector index tool uses it; writes
# via APOC or procedure calls are rejected by explicit match.
_WRITE_KEYWORD_RE = re.compile(
    r"\b(?:CREATE|MERGE|DELETE|DETACH\s+DELETE|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b",
    re.IGNORECASE,
)
_WRITE_PROCEDURE_RE = re.compile(
    r"\bCALL\s+(?:apoc\.(?:create|merge|refactor|cypher\.runWrite)|db\.create)",
    re.IGNORECASE,
)

_MAX_CYPHER_LENGTH = 4000
_ALLOWED_PARAM_TYPES = (str, int, float, bool, type(None))


def _validate_read_only_cypher(cypher: str) -> None:
    """Raise ValueError if the query text contains write clauses.

    Defense-in-depth: run_read_query also forces READ access at the server,
    but rejecting early gives the agent a clean error to retry against."""
    if not isinstance(cypher, str):
        raise ValueError("cypher must be a string")
    if len(cypher) > _MAX_CYPHER_LENGTH:
        raise ValueError(f"cypher exceeds {_MAX_CYPHER_LENGTH} chars")
    stripped = cypher.strip()
    if not stripped:
        raise ValueError("cypher is empty")
    # Check procedure regex first: `apoc.create.node` contains the bare word
    # "create" which the keyword regex would otherwise flag with a generic
    # message. The procedure-specific error is more actionable for the agent.
    if _WRITE_PROCEDURE_RE.search(stripped):
        raise ValueError("run_cypher rejects write procedures (apoc.create/merge/refactor, db.create).")
    if _WRITE_KEYWORD_RE.search(stripped):
        raise ValueError(
            "run_cypher rejects writes (CREATE/MERGE/DELETE/SET/REMOVE/DROP/LOAD CSV/FOREACH). "
            "Use specialized tools for writes."
        )


def _coerce_params(params: dict | None) -> dict:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    coerced: dict = {}
    for k, v in params.items():
        if not isinstance(k, str):
            raise ValueError(f"param keys must be strings, got {type(k).__name__}")
        if isinstance(v, _ALLOWED_PARAM_TYPES):
            coerced[k] = v
        elif isinstance(v, list):
            if not all(isinstance(x, _ALLOWED_PARAM_TYPES) for x in v):
                raise ValueError(f"param {k!r} list contains non-primitive values")
            coerced[k] = v
        else:
            raise ValueError(
                f"param {k!r} has unsupported type {type(v).__name__}; "
                "allowed: str, int, float, bool, None, or list of those"
            )
    return coerced

_AGG_FIELDS = {"reserve_price_num", "emd_num"}
_AGG_FUNCS = {
    "min":    "min(a.{f})",
    "max":    "max(a.{f})",
    "avg":    "avg(a.{f})",
    "median": "percentileCont(a.{f}, 0.5)",
    "p25":    "percentileCont(a.{f}, 0.25)",
    "p75":    "percentileCont(a.{f}, 0.75)",
}


# Upper bound on how many rows the UI receives for a single search. Matches
# `run_cypher`'s max_rows ceiling so the blast radius is bounded. When the
# model-visible `limit` is smaller than this, the extra rows ride on the
# `_ui_results` side-channel — they never enter the LLM's context.
_UI_ROWS_HARD_CAP = 500

_ORDER_BY_CLAUSES = {
    "deadline_asc": "a.auction_start_dt ASC",
    "price_asc":    "a.reserve_price_num ASC",
    "price_desc":   "a.reserve_price_num DESC",
}


def search_auctions(
    min_price: float | None = None,
    max_price: float | None = None,
    city: str | None = None,
    area: str | None = None,
    property_type: str | None = None,
    asset_category: str | None = None,
    bank: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 20,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    include_past: bool = False,
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

    if order_by not in _ORDER_BY_CLAUSES:
        raise ValueError(
            f"order_by must be one of {sorted(_ORDER_BY_CLAUSES)}, got {order_by!r}"
        )

    where = []
    # ui_limit caps the UI-only row count; fetch enough to cover the full
    # result set up to the hard cap, but never smaller than `limit`.
    ui_limit = max(limit, _UI_ROWS_HARD_CAP)
    params: dict = {"limit": ui_limit}
    if min_price is not None:
        where.append("a.reserve_price_num >= $min_price"); params["min_price"] = min_price
    if max_price is not None:
        where.append("a.reserve_price_num <= $max_price"); params["max_price"] = max_price
    if starts_after is None and not include_past:
        starts_after = datetime.now()
    if starts_after is not None:
        where.append("a.auction_start_dt >= $starts_after"); params["starts_after"] = starts_after.isoformat()
    if starts_before is not None:
        where.append("a.auction_start_dt <= $starts_before"); params["starts_before"] = starts_before.isoformat()

    matches = ["(a:AuctionProperty)"]
    if city:
        matches.append("(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})"); params["city"] = city
    if area:
        matches.append("(a)-[:LOCATED_IN_AREA]->(ar:Area)")
        where.append("toLower(ar.name) CONTAINS toLower($area)")
        params["area"] = area
    if property_type:
        matches.append("(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType {name: $property_type})")
        params["property_type"] = property_type
    if asset_category:
        matches.append("(a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory {name: $asset_category})")
        params["asset_category"] = asset_category
    if bank:
        matches.append("(a)-[:CONDUCTED_BY]->(b:Bank {name: $bank})")
        params["bank"] = bank

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

    ui_results: list[dict] = []
    if limit > 0:
        cypher = f"""
            MATCH {match_clause}
            {where_clause}
            OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
            OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
            OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
            OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
            OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(ptx:PropertyType)
            OPTIONAL MATCH (a)-[:SAME_PROPERTY_AS]->(prev:AuctionProperty)
                WHERE prev.auction_start_dt IS NOT NULL
                  AND a.auction_start_dt IS NOT NULL
                  AND prev.auction_start_dt < a.auction_start_dt
            WITH a, city, area, bank, ac,
                 collect(DISTINCT ptx.name) AS property_types,
                 max(CASE WHEN prev.reserve_price_num IS NOT NULL
                          THEN prev.reserve_price_num END) AS previous_reserve_price,
                 count(DISTINCT prev) AS reauction_count
            RETURN a.auction_id AS auction_id, a.title AS title, a.url AS url,
                   a.reserve_price_num AS reserve_price, a.emd_num AS emd,
                   a.auction_start_dt AS auction_start,
                   city.name AS city, area.name AS area,
                   bank.name AS bank,
                   ac.name AS asset_category,
                   property_types,
                   previous_reserve_price,
                   reauction_count
            ORDER BY {_ORDER_BY_CLAUSES[order_by]}
            LIMIT $limit
        """
        ui_results = run_query(cypher, params)
        for row in ui_results:
            rc = row.get("reauction_count") or 0
            row["reauction_count"] = rc
            row["is_reauction"] = rc > 0

    # LLM-visible slice is capped at the user-requested `limit`; full rows
    # (up to ui_limit) ride on `_ui_results` for the UI side-channel.
    results = ui_results[:limit] if limit > 0 else []

    out: dict = {
        "total_count": total_count,
        "returned": len(results),
        "limit": limit,
        "results": results,
    }
    if len(ui_results) > len(results):
        out["_ui_results"] = ui_results
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
        MATCH (a)-[:HAS_PROPERTY_TYPE]->(:PropertyType {name: $property_type})
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
    area: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    asset_category: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 20,
    include_past: bool = False,
) -> dict:
    """Vector search over AuctionProperty.description with optional structured post-filters.

    Runs a kNN over the `property_desc_idx` vector index, then filters by
    city / area / price / asset_category / date window. Returns
    {returned, limit, results} where each result carries a `score` (cosine
    similarity, higher is better).

    Defaults to future-only (auction_start_dt >= now()) so past auctions
    don't surface as semantic "matches". Pass include_past=True to disable
    that floor for retrospective queries.
    """
    qvec = embed_text(query)
    k = max(limit * 5, 50)

    where = []
    params: dict = {"qvec": qvec, "k": k, "limit": limit}
    if min_price is not None:
        where.append("p.reserve_price_num >= $min_price"); params["min_price"] = min_price
    if max_price is not None:
        where.append("p.reserve_price_num <= $max_price"); params["max_price"] = max_price
    if starts_after is None and not include_past:
        starts_after = datetime.now()
    if starts_after is not None:
        where.append("p.auction_start_dt >= $starts_after"); params["starts_after"] = starts_after.isoformat()
    if starts_before is not None:
        where.append("p.auction_start_dt <= $starts_before"); params["starts_before"] = starts_before.isoformat()

    optional_matches = ""
    if city:
        optional_matches += "\nMATCH (p)-[:LOCATED_IN_CITY]->(:City {name: $city})"
        params["city"] = city
    if asset_category:
        optional_matches += "\nMATCH (p)-[:HAS_ASSET_CATEGORY]->(:AssetCategory {name: $asset_category})"
        params["asset_category"] = asset_category
    if area:
        optional_matches += "\nMATCH (p)-[:LOCATED_IN_AREA]->(ar:Area)"
        where.append("toLower(ar.name) CONTAINS toLower($area)")
        params["area"] = area

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    cypher = f"""
        CALL db.index.vector.queryNodes('{VECTOR_INDEX_NAME}', $k, $qvec)
        YIELD node AS p, score
        {optional_matches}
        {where_clause}
        OPTIONAL MATCH (p)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (p)-[:LOCATED_IN_AREA]->(area:Area)
        OPTIONAL MATCH (p)-[:CONDUCTED_BY]->(bank:Bank)
        OPTIONAL MATCH (p)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
        OPTIONAL MATCH (p)-[:HAS_PROPERTY_TYPE]->(ptx:PropertyType)
        OPTIONAL MATCH (p)-[:SAME_PROPERTY_AS]->(prev:AuctionProperty)
            WHERE prev.auction_start_dt IS NOT NULL
              AND p.auction_start_dt IS NOT NULL
              AND prev.auction_start_dt < p.auction_start_dt
              AND prev.reserve_price_num IS NOT NULL
        WITH p, score, city, area, bank, ac,
             collect(DISTINCT ptx.name) AS property_types,
             max(prev.reserve_price_num) AS previous_reserve_price
        RETURN p.auction_id AS auction_id, p.title AS title, p.url AS url,
               p.reserve_price_num AS reserve_price, p.emd_num AS emd,
               p.auction_start_dt AS auction_start,
               city.name AS city, area.name AS area,
               bank.name AS bank,
               ac.name AS asset_category,
               property_types,
               previous_reserve_price,
               substring(p.description, 0, 300) AS description_excerpt,
               score
        ORDER BY score DESC
        LIMIT $limit
    """
    results = run_query(cypher, params)
    return {"returned": len(results), "limit": limit, "results": results}


# ── match_pasted_listing: find an auction from pasted property text ────────

@dataclass
class _ExtractedListing:
    reserve_price: float | None = None
    auction_date: date | None = None
    emd_date: date | None = None
    pin: str | None = None
    city: str | None = None
    area: str | None = None
    built_up_sqft: int | None = None
    uds_sqft: int | None = None
    raw_text: str = ""


_PRICE_LAKH_CRORE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(lakhs?|crores?|cr|l)\b", re.IGNORECASE
)
_PRICE_RUPEE_RE = re.compile(
    r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE
)
_DATE_DDMMYYYY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_AUCTION_DATE_RE = re.compile(
    r"\bauction\b[^\d/]{0,30}(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE
)
_EMD_DATE_RE = re.compile(
    r"\bemd\b[^\d/]{0,30}(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE
)
_PIN_RE = re.compile(r"\b(\d{6})\b")
_BUILT_UP_RE = re.compile(
    r"built[\s_-]?up[^\d]{0,30}(\d+(?:\.\d+)?)\s*sq", re.IGNORECASE
)
_UDS_RE = re.compile(r"\buds\b[^\d]{0,15}(\d+(?:\.\d+)?)\s*sq", re.IGNORECASE)


@lru_cache(maxsize=1)
def _load_known_locations() -> tuple[frozenset[str], frozenset[str]]:
    """Cached set of all (City, Area) names in the graph. Used to disambiguate
    location tokens in pasted text — we never want to mistake a building
    name ('Sai Nila') for an area."""
    city_rows = run_query("MATCH (c:City) RETURN c.name AS n", {})
    area_rows = run_query("MATCH (a:Area) RETURN a.name AS n", {})
    cities = frozenset(r["n"] for r in city_rows if r.get("n"))
    areas = frozenset(r["n"] for r in area_rows if r.get("n"))
    return cities, areas


def _parse_price_to_inr(text: str) -> float | None:
    m = _PRICE_LAKH_CRORE_RE.search(text)
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("c"):
            return n * 10_000_000
        return n * 100_000  # lakhs / L
    m = _PRICE_RUPEE_RE.search(text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _parse_ddmmyyyy(s: str) -> date | None:
    m = _DATE_DDMMYYYY_RE.search(s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _extract_listing_fields(text: str) -> _ExtractedListing:
    out = _ExtractedListing(raw_text=text)
    out.reserve_price = _parse_price_to_inr(text)

    auction_m = _AUCTION_DATE_RE.search(text)
    if auction_m:
        out.auction_date = _parse_ddmmyyyy(auction_m.group(1))
    emd_m = _EMD_DATE_RE.search(text)
    if emd_m:
        out.emd_date = _parse_ddmmyyyy(emd_m.group(1))
    if out.auction_date is None:
        all_dates = _DATE_DDMMYYYY_RE.findall(text)
        if all_dates:
            d, mo, y = all_dates[-1]
            out.auction_date = _parse_ddmmyyyy(f"{d}/{mo}/{y}")

    pin_m = _PIN_RE.search(text)
    if pin_m:
        out.pin = pin_m.group(1)
    bu_m = _BUILT_UP_RE.search(text)
    if bu_m:
        out.built_up_sqft = int(float(bu_m.group(1)))
    uds_m = _UDS_RE.search(text)
    if uds_m:
        out.uds_sqft = int(float(uds_m.group(1)))

    try:
        cities, areas = _load_known_locations()
    except Exception:
        cities, areas = frozenset(), frozenset()
    text_lower = text.lower()
    # Longest names first so "New Delhi" beats "Delhi" and "Greater Noida" beats "Noida".
    for name in sorted(cities, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name.lower())}\b", text_lower):
            out.city = name
            break
    for name in sorted(areas, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name.lower())}\b", text_lower):
            out.area = name
            break
    return out


_TIE_BREAK_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "with",
    "flat", "no", "plot", "road", "street", "nagar", "chennai", "tamil",
    "nadu", "second", "first", "third", "fourth", "ground", "floor", "sqft",
    "sq", "ft", "uds", "built", "up", "area", "reserve", "price", "auction",
    "emd", "date", "lakhs", "lakh", "crore", "crores", "rs", "inr", "bank",
    "property", "residential", "commercial",
})


def _tie_break_tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _TIE_BREAK_STOPWORDS and len(t) > 1
    }


def _tie_break_score(extracted: _ExtractedListing, candidate: dict) -> float:
    """Jaccard overlap between paste and candidate description — used to
    rank when multiple structured candidates survive the price/date/area
    filter."""
    desc = candidate.get("description") or candidate.get("title") or ""
    p_tok = _tie_break_tokens(extracted.raw_text)
    c_tok = _tie_break_tokens(desc)
    if not p_tok or not c_tok:
        return 0.0
    return len(p_tok & c_tok) / len(p_tok | c_tok)


def match_pasted_listing(pasted_text: str) -> dict:
    """Find the auction in the graph that matches a pasted property listing.

    Extracts reserve price, auction/EMD date, area, city, PIN, built-up area,
    UDS from the pasted text, then runs a structured Cypher narrowed by
    price (±2%), auction date (±2 days), city, and area. Tie-breaks any
    remaining candidates by Jaccard token overlap on the description.

    Returns {match, confidence, alternates, extracted}. `match` is None and
    `confidence` is 0 when no structured candidate is found — the caller
    must NOT present alternates as a 'best match' in that case.
    """
    extracted = _extract_listing_fields(pasted_text)

    where: list[str] = []
    params: dict = {}
    matches = ["(a:AuctionProperty)"]

    if extracted.reserve_price is not None:
        params["min_price"] = extracted.reserve_price * 0.98
        params["max_price"] = extracted.reserve_price * 1.02
        where.append("a.reserve_price_num >= $min_price")
        where.append("a.reserve_price_num <= $max_price")

    if extracted.auction_date is not None:
        starts_after = datetime.combine(
            extracted.auction_date - timedelta(days=2), datetime.min.time()
        )
        starts_before = datetime.combine(
            extracted.auction_date + timedelta(days=2), datetime.max.time()
        )
        params["starts_after"] = starts_after.isoformat()
        params["starts_before"] = starts_before.isoformat()
        where.append("a.auction_start_dt >= $starts_after")
        where.append("a.auction_start_dt <= $starts_before")

    if extracted.city:
        matches.append("(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})")
        params["city"] = extracted.city

    if extracted.area:
        matches.append("(a)-[:LOCATED_IN_AREA]->(ar:Area)")
        where.append("toLower(ar.name) CONTAINS toLower($area)")
        params["area"] = extracted.area

    extracted_dict = {
        "reserve_price": extracted.reserve_price,
        "auction_date": extracted.auction_date.isoformat() if extracted.auction_date else None,
        "emd_date": extracted.emd_date.isoformat() if extracted.emd_date else None,
        "pin": extracted.pin,
        "city": extracted.city,
        "area": extracted.area,
        "built_up_sqft": extracted.built_up_sqft,
        "uds_sqft": extracted.uds_sqft,
    }

    rows: list[dict] = []
    if where:
        rows = _run_match_cypher(where, matches, params)

    if rows:
        scored = sorted(
            rows, key=lambda r: _tie_break_score(extracted, r), reverse=True
        )
        strong_signals = sum(
            1 for v in (
                extracted.reserve_price,
                extracted.auction_date,
                extracted.area,
                extracted.pin,
            ) if v is not None
        )
        confidence = min(1.0, 0.4 + 0.2 * strong_signals)
        if len(rows) > 1:
            confidence = min(confidence, 0.8)
        return {
            "match": scored[0],
            "confidence": confidence,
            "alternates": scored[1:5],
            "candidates": scored[:5],
            "widening_reason": None,
            "extracted": extracted_dict,
        }

    # ── Progressive widening fallback ─────────────────────────────────────
    # Strict price+date+area+city returned nothing. Don't bail — try wider
    # filters so the user always sees CLOSE candidates instead of "no
    # match". Each tier explains itself via `widening_reason` so the LLM
    # surfaces it verbatim ("we couldn't find this exact property — here
    # are the closest matches in <city/area> at ±10% price").
    widened, reason = _widen_until_hits(extracted)
    return {
        "match": None,
        "confidence": 0.0,
        "alternates": widened[:5],
        "candidates": widened[:5],
        "widening_reason": reason,
        "extracted": extracted_dict,
        "note": (
            "No exact structured match. Tell the user we did NOT find their "
            "exact property; present `candidates` as 'closest matches we "
            "found' and quote `widening_reason` so they understand which "
            "constraint was relaxed. Ask for the auction_id or a clearer "
            "location/price/date if these candidates look wrong."
        ),
    }


def _run_match_cypher(where: list[str], matches: list[str], params: dict) -> list[dict]:
    """Execute the candidate-fetch Cypher used by both the strict path and
    the widening tiers. Kept separate so the WHERE/MATCH clauses can be
    rebuilt per tier without duplicating the RETURN body."""
    where_clause = "WHERE " + " AND ".join(where)
    match_clause = ", ".join(matches)
    cypher = f"""
        MATCH {match_clause}
        {where_clause}
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
        RETURN a.auction_id AS auction_id, a.title AS title, a.url AS url,
               a.reserve_price_num AS reserve_price,
               a.emd_num AS emd,
               a.auction_start_dt AS auction_start,
               city.name AS city, area.name AS area,
               bank.name AS bank,
               substring(a.description, 0, 400) AS description
        LIMIT 20
    """
    return run_query(cypher, params)


def _widen_until_hits(extracted: _ExtractedListing) -> tuple[list[dict], str | None]:
    """Try progressively looser filters until something hits. Returns
    (rows, widening_reason). The order — drop date, then widen price, then
    drop area, then drop city — matches what a human would do: the date is
    the most volatile constraint (re-auctions slide by weeks), price is
    next (banks adjust reserves), and city is the strongest geographic
    anchor so we hold it longest. Empty (rows, reason) means even the
    loosest tier found nothing — only happens for paste with zero usable
    fields or a city not in the graph."""
    tiers: list[tuple[str, callable]] = [
        ("dropped auction-date constraint", lambda: _build_filter(
            extracted, drop_date=True,
        )),
        ("widened price band to ±10% and dropped date", lambda: _build_filter(
            extracted, drop_date=True, price_pct=0.10,
        )),
        ("dropped area, kept city + ±10% price", lambda: _build_filter(
            extracted, drop_date=True, drop_area=True, price_pct=0.10,
        )),
        ("kept city only", lambda: _build_filter(
            extracted, drop_date=True, drop_area=True, drop_price=True,
        )),
    ]
    for reason, builder in tiers:
        where, matches, params = builder()
        if not where and len(matches) == 1:
            # Nothing left to filter on — skip this tier.
            continue
        rows = _run_match_cypher(where, matches, params)
        if rows:
            return rows, reason
    return [], None


def _build_filter(
    extracted: _ExtractedListing,
    *,
    drop_date: bool = False,
    drop_area: bool = False,
    drop_price: bool = False,
    price_pct: float = 0.02,
) -> tuple[list[str], list[str], dict]:
    """Rebuild the (where, matches, params) triple with selected
    constraints relaxed. Used by `_widen_until_hits` to construct each
    fallback tier without copy-pasting the assembly logic."""
    where: list[str] = []
    matches = ["(a:AuctionProperty)"]
    params: dict = {}
    if extracted.reserve_price is not None and not drop_price:
        params["min_price"] = extracted.reserve_price * (1 - price_pct)
        params["max_price"] = extracted.reserve_price * (1 + price_pct)
        where.append("a.reserve_price_num >= $min_price")
        where.append("a.reserve_price_num <= $max_price")
    if extracted.auction_date is not None and not drop_date:
        starts_after = datetime.combine(
            extracted.auction_date - timedelta(days=2), datetime.min.time()
        )
        starts_before = datetime.combine(
            extracted.auction_date + timedelta(days=2), datetime.max.time()
        )
        params["starts_after"] = starts_after.isoformat()
        params["starts_before"] = starts_before.isoformat()
        where.append("a.auction_start_dt >= $starts_after")
        where.append("a.auction_start_dt <= $starts_before")
    if extracted.city:
        matches.append("(a)-[:LOCATED_IN_CITY]->(c:City {name: $city})")
        params["city"] = extracted.city
    if extracted.area and not drop_area:
        matches.append("(a)-[:LOCATED_IN_AREA]->(ar:Area)")
        where.append("toLower(ar.name) CONTAINS toLower($area)")
        params["area"] = extracted.area
    return where, matches, params


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
        OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
        OPTIONAL MATCH (a)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
            WHERE doc.public_url IS NOT NULL
        OPTIONAL MATCH (a)-[link:SAME_PROPERTY_AS]->(sibling:AuctionProperty)
        WITH a, city, area, state, bank, borrower, ac,
             collect(DISTINCT pt.name) AS property_types,
             collect(DISTINCT properties(s)) AS survey_numbers,
             collect(DISTINCT {
               filename:     doc.filename,
               public_url:   doc.public_url,
               content_type: doc.content_type,
               doc_type:     doc.doc_type
             }) AS documents,
             collect(DISTINCT CASE WHEN sibling IS NULL THEN NULL ELSE {
               auction_id:        sibling.auction_id,
               title:             sibling.title,
               url:               sibling.url,
               reserve_price_num: sibling.reserve_price_num,
               auction_start_dt:  sibling.auction_start_dt,
               match_reason:      link.match_reason,
               confidence:        link.confidence
             } END) AS siblings
        RETURN properties(a) AS fields,
               {
                 city:           CASE WHEN city     IS NULL THEN NULL ELSE properties(city)     END,
                 area:           CASE WHEN area     IS NULL THEN NULL ELSE properties(area)     END,
                 state:          CASE WHEN state    IS NULL THEN NULL ELSE properties(state)    END,
                 bank:           CASE WHEN bank     IS NULL THEN NULL ELSE properties(bank)     END,
                 borrower:       CASE WHEN borrower IS NULL THEN NULL ELSE properties(borrower) END,
                 asset_category: CASE WHEN ac       IS NULL THEN NULL ELSE properties(ac)       END,
                 property_types: property_types,
                 survey_numbers: survey_numbers
               } AS relationships,
               documents AS documents,
               siblings  AS siblings
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

    # collect() with OPTIONAL MATCH returns a list containing a single empty-
    # valued dict when there are no matches — strip those. Also dedupe by
    # public_url so a property never surfaces the same file twice even if
    # the graph briefly holds duplicate :Document nodes (issue #45).
    documents = []
    seen_doc_keys: set[str] = set()
    for d in (rows[0].get("documents") or []):
        if not d or not d.get("public_url"):
            continue
        key = d.get("public_url") or d.get("filename") or ""
        if key in seen_doc_keys:
            continue
        seen_doc_keys.add(key)
        documents.append(d)

    siblings = [
        s for s in (rows[0].get("siblings") or [])
        if s and s.get("auction_id")
    ]
    price_history: list[dict] = []
    if siblings:
        timeline = [
            {
                "auction_id":        s["auction_id"],
                "title":             s.get("title"),
                "url":               s.get("url"),
                "reserve_price_num": s.get("reserve_price_num"),
                "auction_start_dt":  s.get("auction_start_dt"),
                "match_reason":      s.get("match_reason"),
                "confidence":        s.get("confidence"),
                "is_current":        False,
            }
            for s in siblings
        ]
        timeline.append({
            "auction_id":        auction_id,
            "title":             fields.get("title"),
            "url":               fields.get("url"),
            "reserve_price_num": fields.get("reserve_price_num"),
            "auction_start_dt":  fields.get("auction_start_dt"),
            "match_reason":      None,
            "confidence":        None,
            "is_current":        True,
        })
        timeline.sort(key=lambda r: (
            r["auction_start_dt"] is None,
            r["auction_start_dt"] or "",
        ))
        price_history = timeline

    return {
        "auction_id":    auction_id,
        "fields":        fields,
        "relationships": rows[0]["relationships"],
        "documents":     documents,
        "price_history": price_history,
    }


def survey_search(survey_no: str, subdivision: str | None = None) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
        WHERE s.survey_no = $survey_no
          AND ($subdivision IS NULL OR s.subdivision = $subdivision)
        RETURN a.auction_id AS auction_id, a.title AS title,
               s.survey_no AS survey_no, s.subdivision AS subdivision, s.survey_type AS survey_type
    """
    return run_query(cypher, {"survey_no": survey_no, "subdivision": subdivision})


# ── Phase 1: schema introspection + escape-hatch tools ─────────────────────

# Map a logical field name the agent might use to the (label, relationship)
# pair needed to count AuctionProperty references.
_DISTINCT_FIELDS: dict[str, tuple[str, str]] = {
    "city":           ("City",          "LOCATED_IN_CITY"),
    "area":           ("Area",          "LOCATED_IN_AREA"),
    "state":          ("State",         "LOCATED_IN_STATE"),
    "bank":           ("Bank",          "CONDUCTED_BY"),
    "borrower":       ("Borrower",      "HAS_BORROWER"),
    "asset_category": ("AssetCategory", "HAS_ASSET_CATEGORY"),
    "property_type":  ("PropertyType",  "HAS_PROPERTY_TYPE"),
}

_SCHEMA_CACHE: dict[str, tuple[float, dict]] = {}
_SCHEMA_TTL_SECONDS = 3600.0


def list_distinct(
    field: str,
    limit: int = 100,
    city: str | None = None,
    bank: str | None = None,
    borrower: str | None = None,
    asset_category: str | None = None,
) -> dict:
    """List distinct values of a reference field with counts.

    `field` must be one of the keys in _DISTINCT_FIELDS. Scope filters
    (`city`, `bank`, `borrower`, `asset_category`) narrow the count to
    auctions that match every provided scope. A scope must differ from
    `field` — you can't group by bank while filtering by bank.

    Use this for distribution / breakdown / "spread" questions
    ("property-type mix for SBI", "asset categories in Chennai"). Never
    iterate `get_auction_detail` to compute a count.
    """
    if field not in _DISTINCT_FIELDS:
        raise ValueError(
            f"field must be one of {sorted(_DISTINCT_FIELDS)}, got {field!r}"
        )

    raw_scopes: dict[str, str | None] = {
        "city":           city,
        "bank":           bank,
        "borrower":       borrower,
        "asset_category": asset_category,
    }
    # Filtering by the same dimension you're grouping on is a no-op; drop
    # silently so agents can pass redundant scopes without an error.
    active_scopes = {k: v for k, v in raw_scopes.items() if v and k != field}

    label, rel = _DISTINCT_FIELDS[field]
    params: dict = {"limit": int(limit)}

    scope_matches: list[str] = []
    for scope_field, value in active_scopes.items():
        scope_label, scope_rel = _DISTINCT_FIELDS[scope_field]
        scope_matches.append(
            f"(a)-[:{scope_rel}]->(:{scope_label} {{name: ${scope_field}}})"
        )
        params[scope_field] = value

    match_clauses = ["(a:AuctionProperty)"]
    match_clauses.extend(scope_matches)
    match_clauses.append(f"(a)-[:{rel}]->(n:{label})")
    match_clause = ",\n                  ".join(match_clauses)

    cypher = f"""
            MATCH {match_clause}
            RETURN n.name AS value, count(DISTINCT a) AS auction_count
            ORDER BY auction_count DESC
            LIMIT $limit
        """
    results = run_read_query(cypher, params, max_rows=max(int(limit), 1))
    return {
        "field": field,
        "filter_city": city,
        "filter_bank": bank,
        "filter_borrower": borrower,
        "filter_asset_category": asset_category,
        "results": results,
    }


def describe_schema(refresh: bool = False) -> dict:
    """Return a compact description of the graph schema and cardinalities.

    Results are cached in-process for `_SCHEMA_TTL_SECONDS`. Pass
    `refresh=True` to bypass the cache. Fields returned:

    - node_labels: [{label, count, sample_properties}, ...]
    - relationships: [{type, from, to, count}, ...]
    - enums: {asset_category, property_type, possession_type, ...}
    - numeric_ranges: {reserve_price_num: {...}, emd_num: {...}}
    - date_ranges:    {auction_start_dt: {...}, application_deadline_dt: {...}}
    """
    now = time.time()
    cached = _SCHEMA_CACHE.get("default")
    if cached and not refresh and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
        return cached[1]

    labels = run_read_query(
        "CALL db.labels() YIELD label RETURN label ORDER BY label",
        max_rows=50,
    )
    label_info: list[dict] = []
    for row in labels:
        label = row["label"]
        count_rows = run_read_query(
            f"MATCH (n:`{label}`) RETURN count(n) AS n",
            max_rows=1,
        )
        count = count_rows[0]["n"] if count_rows else 0
        prop_rows = run_read_query(
            f"MATCH (n:`{label}`) WITH n LIMIT 1 RETURN keys(n) AS props",
            max_rows=1,
        )
        props = prop_rows[0]["props"] if prop_rows else []
        label_info.append({"label": label, "count": count, "sample_properties": props})

    rel_rows = run_read_query(
        "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType AS t ORDER BY t",
        max_rows=50,
    )
    rel_info: list[dict] = []
    for row in rel_rows:
        rtype = row["t"]
        c = run_read_query(
            f"MATCH ()-[r:`{rtype}`]->() RETURN count(r) AS n",
            max_rows=1,
        )
        rel_info.append({"type": rtype, "count": c[0]["n"] if c else 0})

    enums: dict[str, list[str]] = {}
    for field in ("asset_category", "property_type"):
        label, rel = _DISTINCT_FIELDS[field]
        rows = run_read_query(
            f"MATCH (n:{label}) RETURN n.name AS v ORDER BY v",
            max_rows=50,
        )
        enums[field] = [r["v"] for r in rows if r.get("v")]

    poss_rows = run_read_query(
        """
        MATCH (a:AuctionProperty)
        WHERE a.possession_type IS NOT NULL
        RETURN DISTINCT a.possession_type AS v
        ORDER BY v
        """,
        max_rows=20,
    )
    enums["possession_type"] = [r["v"] for r in poss_rows if r.get("v")]

    stat_rows = run_read_query(
        """
        MATCH (a:AuctionProperty)
        RETURN
          min(a.reserve_price_num)        AS rp_min,
          max(a.reserve_price_num)        AS rp_max,
          percentileCont(a.reserve_price_num, 0.5)  AS rp_p50,
          percentileCont(a.reserve_price_num, 0.95) AS rp_p95,
          min(a.emd_num)                  AS emd_min,
          max(a.emd_num)                  AS emd_max,
          percentileCont(a.emd_num, 0.5)  AS emd_p50,
          min(a.auction_start_dt)         AS start_min,
          max(a.auction_start_dt)         AS start_max,
          min(a.application_deadline_dt)  AS dl_min,
          max(a.application_deadline_dt)  AS dl_max
        """,
        max_rows=1,
    )
    stats = stat_rows[0] if stat_rows else {}
    numeric_ranges = {
        "reserve_price_num": {
            "min": stats.get("rp_min"),
            "p50": stats.get("rp_p50"),
            "p95": stats.get("rp_p95"),
            "max": stats.get("rp_max"),
        },
        "emd_num": {
            "min": stats.get("emd_min"),
            "p50": stats.get("emd_p50"),
            "max": stats.get("emd_max"),
        },
    }
    date_ranges = {
        "auction_start_dt":        {"min": stats.get("start_min"), "max": stats.get("start_max")},
        "application_deadline_dt": {"min": stats.get("dl_min"),    "max": stats.get("dl_max")},
    }

    out = {
        "node_labels": label_info,
        "relationships": rel_info,
        "enums": enums,
        "numeric_ranges": numeric_ranges,
        "date_ranges": date_ranges,
    }
    _SCHEMA_CACHE["default"] = (now, out)
    return out


def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
    timeout: float = 10.0,
) -> dict:
    """Execute a READ-ONLY Cypher query with multiple guardrails.

    Guardrails:
    1. Regex rejects CREATE / MERGE / DELETE / SET / REMOVE / DROP /
       LOAD CSV / FOREACH, plus write-side apoc and db procedures.
    2. Session forces READ access mode, so anything that slips past (1)
       still fails at the Neo4j server.
    3. Params are validated: keys must be strings, values must be primitive
       types (str / int / float / bool / None) or lists of primitives.
    4. Execution is bounded by `timeout` seconds and the result list is
       trimmed to `max_rows`.

    Returns:
        {
          "description": description passed in,
          "cypher": the query executed,
          "params": coerced params dict,
          "rows": list[dict] (capped at max_rows),
          "returned": len(rows),
          "duration_ms": wall-clock time,
        }

    Raises:
        ValueError on any guardrail violation.
        RuntimeError wrapping a Neo4jError with the server message (so the
        agent can self-correct via ModelRetry).
    """
    _validate_read_only_cypher(cypher)
    coerced = _coerce_params(params)
    max_rows = max(1, min(int(max_rows), 500))

    from neo4j.exceptions import Neo4jError
    start = time.perf_counter()
    try:
        rows = run_read_query(cypher, coerced, timeout=timeout, max_rows=max_rows)
    except Neo4jError as e:
        raise RuntimeError(f"Neo4j error: {e.message}") from e
    duration_ms = int((time.perf_counter() - start) * 1000)

    return {
        "description": description,
        "cypher": cypher,
        "params": coerced,
        "rows": rows,
        "returned": len(rows),
        "duration_ms": duration_ms,
    }
