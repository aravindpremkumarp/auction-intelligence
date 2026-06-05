"""
api/properties/router.py
------------------------
Browse-all-properties listing (`/properties`), single-auction detail
(`/auction/{id}`), and a public data-freshness snapshot (`/stats`).

The filter/facet Cypher builders are kept as pure functions so the listing's
multi-select + cascading-facet semantics can be unit-tested without a live
Neo4j (see tests/api/test_properties_filters.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from api.neo4j_client import run_query
from api.tools.cypher_tools import get_auction_detail

router = APIRouter()


def _parse_to_utc(s: str) -> datetime:
    """Parse an ISO-8601 query-string date and force tz-aware UTC.
    Stored AuctionProperty dates are ZONED DATETIME — comparing against a
    naive Python datetime yields zero matches in Cypher."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_PROPERTIES_SORT_CLAUSES = {
    "date_asc":   "a.auction_start_dt ASC",
    "date_desc":  "a.auction_start_dt DESC",
    "price_asc":  "a.reserve_price_num ASC",
    "price_desc": "a.reserve_price_num DESC",
    # Legacy alias: clients running cached HTML still send `sort=date`.
    "date":       "a.auction_start_dt ASC",
}
_PROPERTIES_MAX_LIMIT = 200


def _properties_filter_cypher(filters: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Build the MATCH + WHERE + params for the browse-properties filter set.

    Each filter narrows via either an additional MATCH (when it pins a node by
    name) or a WHERE clause (numeric / date / free-text). Returned MATCH and
    WHERE strings are composable into both the count, results, and per-facet
    queries so the filter semantics stay consistent across them.
    """
    matches = ["(a:AuctionProperty)"]
    where: list[str] = []
    params: dict[str, Any] = {}

    # Categorical filters that support multi-select. With a single value the
    # inline pattern stays (cheap, indexed lookup); with multiple values an
    # aliased node + IN-list WHERE makes the dimension act as OR-within while
    # still AND-ing across dimensions.
    _categorical = (
        ("state",         "LOCATED_IN_STATE",   "State",         "f_state",         "s_state"),
        ("district",      "LOCATED_IN_CITY",    "City",          "f_district",      "s_district"),
        ("village",       "LOCATED_IN_AREA",    "Area",          "f_village",       "s_village"),
        ("bank",          "CONDUCTED_BY",       "Bank",          "f_bank",          "s_bank"),
        ("type",          "HAS_ASSET_CATEGORY", "AssetCategory", "f_type",          "s_type"),
        ("property_type", "HAS_PROPERTY_TYPE",  "PropertyType",  "f_property_type", "s_property_type"),
    )
    for key, rel, label, param_key, alias in _categorical:
        raw = filters.get(key)
        if raw in (None, "", []):
            continue
        vals = raw if isinstance(raw, list) else [raw]
        vals = [v for v in vals if v]
        if not vals:
            continue
        if len(vals) == 1:
            matches.append(f"(a)-[:{rel}]->(:{label} {{name: ${param_key}}})")
            params[param_key] = vals[0]
        else:
            matches.append(f"(a)-[:{rel}]->({alias}:{label})")
            where.append(f"{alias}.name IN ${param_key}_list")
            params[f"{param_key}_list"] = vals
    if filters.get("min_price") is not None:
        where.append("a.reserve_price_num >= $f_min_price")
        params["f_min_price"] = float(filters["min_price"])
    if filters.get("max_price") is not None:
        where.append("a.reserve_price_num <= $f_max_price")
        params["f_max_price"] = float(filters["max_price"])
    if filters.get("date_from"):
        where.append("a.auction_start_dt >= $f_date_from")
        params["f_date_from"] = _parse_to_utc(filters["date_from"])
    if filters.get("date_to"):
        where.append("a.auction_start_dt <= $f_date_to")
        params["f_date_to"] = _parse_to_utc(filters["date_to"])
    if filters.get("q"):
        # Match free-text against title and the names of the most useful linked
        # nodes — that's what the design's "search by location, bank, type"
        # placeholder promises.
        where.append(
            "(toLower(coalesce(a.title, '')) CONTAINS $f_q "
            " OR EXISTS { MATCH (a)-[:LOCATED_IN_CITY]->(c:City) WHERE toLower(c.name) CONTAINS $f_q } "
            " OR EXISTS { MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area) WHERE toLower(ar.name) CONTAINS $f_q } "
            " OR EXISTS { MATCH (a)-[:CONDUCTED_BY]->(b:Bank) WHERE toLower(b.name) CONTAINS $f_q } "
            " OR EXISTS { MATCH (a)-[:HAS_ASSET_CATEGORY]->(acq:AssetCategory) WHERE toLower(acq.name) CONTAINS $f_q } "
            " OR EXISTS { MATCH (a)-[:HAS_PROPERTY_TYPE]->(ptq:PropertyType) WHERE toLower(ptq.name) CONTAINS $f_q })"
        )
        params["f_q"] = filters["q"].strip().lower()

    match_clause = ", ".join(matches)
    where_clause = "WHERE " + " AND ".join(where) if where else ""
    return match_clause, where_clause, params


def _properties_facet(match_clause: str, where_clause: str, params: dict[str, Any],
                      label: str, rel: str, alias: str) -> list[dict]:
    """Count distinct values of a single facet dimension under the given filters.

    Facets reflect every applied filter — selecting a state will narrow the
    bank facet to banks active in that state, and so on. Cleaner UX than
    showing impossible options that yield zero results when picked.
    """
    cypher = f"""
        MATCH {match_clause}
        {where_clause}
        OPTIONAL MATCH (a)-[:{rel}]->({alias}:{label})
        WITH {alias}.name AS value, count(DISTINCT a) AS count
        WHERE value IS NOT NULL
        RETURN value, count
        ORDER BY count DESC, value ASC
        LIMIT 200
    """
    return run_query(cypher, params)


# When computing a facet for one dimension, drop that dimension's own filter
# from the WHERE clause — and for cascading geographic filters, also drop
# downstream dimensions. Without this, selecting state="Tamil Nadu" would
# narrow the state facet to only Tamil Nadu, leaving the user no way to add
# a second state from the same dropdown panel.
_FACET_FILTER_EXCLUDE: dict[str, tuple[str, ...]] = {
    "type":          ("type",),
    "property_type": ("property_type",),
    "bank":          ("bank",),
    "state":         ("state", "district", "village"),
    "district":      ("district", "village"),
    "village":       ("village",),
}


def _facet_filters_for(filters: dict[str, Any], dim_key: str) -> dict[str, Any]:
    """Filters with `dim_key`'s own filter (and any downstream cascade dim's
    filters) removed — used so a dimension's facet keeps showing options the
    user could still add, instead of narrowing to what's already selected."""
    drop = _FACET_FILTER_EXCLUDE.get(dim_key, (dim_key,))
    return {k: v for k, v in filters.items() if k not in drop}


def _facet_for(
    filters: dict[str, Any],
    dim_key: str,
    label: str,
    rel: str,
    alias: str,
) -> list[dict]:
    """Run the facet query for `dim_key` against the cascade-aware filter set."""
    facet_filters = _facet_filters_for(filters, dim_key)
    f_match, f_where, f_params = _properties_filter_cypher(facet_filters)
    return _properties_facet(f_match, f_where, f_params, label, rel, alias)


@router.get("/properties")
def list_properties(
    q: str | None = None,
    type: list[str] | None = Query(default=None),
    property_type: list[str] | None = Query(default=None),
    bank: list[str] | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    district: list[str] | None = Query(default=None),
    village: list[str] | None = Query(default=None),
    min_price: float | None = None,
    max_price: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "date",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    """Browse-all-properties listing for the landing-page section.

    `min_price`/`max_price` are in raw rupees (the unit stored on
    `reserve_price_num`); the UI converts ₹L → ₹ before calling.
    `date_from`/`date_to` are ISO-8601 strings compared against
    `auction_start_dt` directly.
    """
    if sort not in _PROPERTIES_SORT_CLAUSES:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_PROPERTIES_SORT_CLAUSES)}")
    limit = max(1, min(int(limit), _PROPERTIES_MAX_LIMIT))
    offset = max(0, int(offset))

    filters = {
        "q": q, "type": type, "property_type": property_type, "bank": bank,
        "state": state, "district": district, "village": village,
        "min_price": min_price, "max_price": max_price,
        "date_from": date_from, "date_to": date_to,
    }
    match_clause, where_clause, params = _properties_filter_cypher(filters)

    total_rows = run_query(
        f"MATCH {match_clause} {where_clause} RETURN count(DISTINCT a) AS total",
        params,
    )
    total = int(total_rows[0]["total"]) if total_rows else 0

    page_params = {**params, "limit": limit, "offset": offset}
    results_cypher = f"""
        MATCH {match_clause}
        {where_clause}
        OPTIONAL MATCH (a)-[:LOCATED_IN_STATE]->(stt:State)
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(cty:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ara:Area)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bnk:Bank)
        OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(asc:AssetCategory)
        OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(pty:PropertyType)
        OPTIONAL MATCH (a)-[:SAME_PROPERTY_AS]->(prv:AuctionProperty)
            WHERE prv.auction_start_dt IS NOT NULL
              AND a.auction_start_dt IS NOT NULL
              AND prv.auction_start_dt < a.auction_start_dt
        WITH a, stt, cty, ara, bnk, asc,
             collect(DISTINCT pty.name) AS property_types,
             max(CASE WHEN prv.reserve_price_num IS NOT NULL
                      THEN prv.reserve_price_num END) AS previous_reserve_price,
             count(DISTINCT prv) AS reauction_count
        RETURN a.auction_id AS auction_id, a.title AS title, a.url AS url,
               a.reserve_price_num AS reserve_price, a.emd_num AS emd,
               toString(a.auction_start_dt) AS auction_start,
               stt.name AS state, cty.name AS city, ara.name AS area,
               bnk.name AS bank,
               asc.name AS asset_category,
               property_types,
               previous_reserve_price,
               reauction_count
        ORDER BY {_PROPERTIES_SORT_CLAUSES[sort]}, a.auction_id ASC
        SKIP $offset
        LIMIT $limit
    """
    results = run_query(results_cypher, page_params)
    for row in results:
        rc = row.get("reauction_count") or 0
        row["reauction_count"] = rc
        row["is_reauction"] = rc > 0

    facets = {
        "type":          _facet_for(filters, "type",          "AssetCategory", "HAS_ASSET_CATEGORY", "ac"),
        "property_type": _facet_for(filters, "property_type", "PropertyType",  "HAS_PROPERTY_TYPE",  "pt"),
        "bank":          _facet_for(filters, "bank",          "Bank",          "CONDUCTED_BY",       "bk"),
        "state":         _facet_for(filters, "state",         "State",         "LOCATED_IN_STATE",   "st"),
        "district":      _facet_for(filters, "district",      "City",          "LOCATED_IN_CITY",    "ct"),
        "village":       _facet_for(filters, "village",       "Area",          "LOCATED_IN_AREA",    "ar"),
    }

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": results,
        "facets": facets,
    }


@router.get("/stats")
def stats() -> dict:
    """Public data-freshness + coverage snapshot.

    Powers the UI's "Data as of …" indicator and doubles as a cheap probe for
    ingestion-freshness monitoring. Every value degrades gracefully to 0/None
    when the graph is empty or a field is missing, so this never 500s.
    """
    rows = run_query(
        """
        MATCH (a:AuctionProperty)
        RETURN count(a) AS total,
               sum(CASE WHEN a.auction_start_dt >= datetime() THEN 1 ELSE 0 END) AS upcoming,
               toString(max(a.verified_at)) AS last_enriched
        """
    )
    row = rows[0] if rows else {}
    return {
        "total_auctions": row.get("total") or 0,
        "upcoming_auctions": row.get("upcoming") or 0,
        "last_enriched": row.get("last_enriched"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@router.get("/auction/{auction_id}")
def auction_detail(auction_id: str) -> dict:
    detail = get_auction_detail(auction_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Auction not found")
    return detail
