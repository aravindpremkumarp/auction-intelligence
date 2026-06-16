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
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from api.neo4j_client import run_read_query, run_read_query_async
from pipeline.embeddings import embed_query_gemini

# Three Gemini vector indexes, all 3072-dim, all over gemini-embedding-2.
# Scores are directly comparable across indexes, so `semantic_search` ranks
# by max cosine.
PROPERTY_DESC_INDEX = "property_desc_idx"        # AuctionProperty.description_embedding (text)
NOTICE_MARKDOWN_INDEX = "notice_markdown_idx"    # Document.markdown_embedding (structured text)
NOTICE_IMAGE_INDEX = "notice_image_idx"          # Document.image_embedding (image / PDF bytes)

# Lucene fulltext index over AuctionProperty title + description. Adds a
# lexical "keyword" lens to semantic_search so exact tokens the embedding
# may smear out — locality names ("Balaraman Nagar"), survey/plot numbers,
# bank names — rank properties directly. Created by
# scripts/load_tn_to_neo4j.py; semantic_search degrades to vector-only when
# the index is absent.
PROPERTY_FULLTEXT_INDEX = "property_text_idx"

# Lucene scores are unbounded, so the keyword branch max-normalizes them to
# [0, 1] per query and scales by this weight to sit in the same range as the
# vector cosines. < 1.0 so a weak best-keyword hit can't outrank a strong
# vector consensus (same alpha idea as neo4j-graphrag's HybridRetriever).
_KEYWORD_WEIGHT = 0.8

# Strip Lucene operators so raw user text can't break query parsing or smuggle
# in boolean syntax; terms are left OR-joined (Lucene's default).
_LUCENE_SPECIALS_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def _lucene_query(text: str) -> str | None:
    """Sanitize free text into a safe Lucene query, or None if nothing
    searchable survives."""
    terms = _LUCENE_SPECIALS_RE.sub(" ", text or "").split()
    return " ".join(terms) if terms else None

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


# Neo4j DATETIME values must be serialized to ISO strings before leaving the
# API boundary — FastAPI's response serializer can't encode neo4j.time.*.
def _iso(v):
    """Coerce a neo4j.time.DateTime to its ISO string. Pass through anything
    else (str, None, numbers) untouched."""
    return v.iso_format() if hasattr(v, "iso_format") else v


def _json_safe(v):
    """Recursively coerce neo4j temporal types (Date/Time/DateTime/Duration),
    wherever they sit in a nested value, to ISO strings.

    `properties(node)` hands back raw neo4j temporal objects, which FastAPI's
    pydantic-v2 response serializer cannot encode — it raises and the route
    500s. The detail payload previously coerced only a hardcoded set of
    top-level AuctionProperty keys, so any datetime on a related node
    (Bank/City/Borrower/…) — or a newly-added AuctionProperty datetime field —
    slipped through raw and 500'd every property detail."""
    if hasattr(v, "iso_format"):
        return v.iso_format()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _aware(dt: datetime | None) -> datetime | None:
    """Stored AuctionProperty dates are ZONED DATETIME. Cypher comparison
    between ZONED and LOCAL DATETIME silently yields zero matches, so any
    naive datetime arriving from the agent or API layer must be promoted
    to tz-aware. We assume UTC for naive inputs — the underlying data was
    written with timezone-naive ISO strings, so UTC is the correct anchor."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


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

# Upper bound on how many rows ever enter the LLM's context from one search,
# independent of the model-requested `limit`. The model can ask for a large
# `limit` (e.g. to "find insights"), which would otherwise serialize hundreds
# of rows into the prompt — one such call dominated a recent session's token
# bill. The UI still receives the full set (up to `_UI_ROWS_HARD_CAP`) via the
# `_ui_results` side-channel; this only bounds what the model sees. Quantities
# come from `total_count`, never the row count, so capping rows loses no facts.
_LLM_ROWS_HARD_CAP = 25

_ORDER_BY_CLAUSES = {
    "deadline_asc":  "a.auction_start_dt ASC",
    "deadline_desc": "a.auction_start_dt DESC",
    "price_asc":     "a.reserve_price_num ASC",
    "price_desc":    "a.reserve_price_num DESC",
}


def search_auctions(
    min_price: float | None = None,
    max_price: float | None = None,
    city: str | list[str] | None = None,
    area: str | list[str] | None = None,
    property_type: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    auction_type: str | None = None,
    branch_name: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 10,
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
    # ui_limit caps the UI-only row count. Always fetch up to the hard cap so
    # the `_ui_results` side-channel is fully populated, and never beyond it —
    # a model-requested limit above the cap must not widen the blast radius.
    ui_limit = _UI_ROWS_HARD_CAP
    params: dict = {"limit": ui_limit}
    if min_price is not None:
        where.append("a.reserve_price_num >= $min_price")
        params["min_price"] = min_price
    if max_price is not None:
        where.append("a.reserve_price_num <= $max_price")
        params["max_price"] = max_price
    if starts_after is None and not include_past:
        starts_after = datetime.now(timezone.utc)
    if starts_after is not None:
        where.append("a.auction_start_dt >= $starts_after")
        params["starts_after"] = _aware(starts_after)
    if starts_before is not None:
        where.append("a.auction_start_dt <= $starts_before")
        params["starts_before"] = _aware(starts_before)

    matches = ["(a:AuctionProperty)"]
    if city:
        city_list = [city] if isinstance(city, str) else list(city)
        matches.append("(a)-[:LOCATED_IN_CITY]->(c:City)")
        where.append("c.name IN $city")
        params["city"] = city_list
    if area:
        area_list = [area] if isinstance(area, str) else list(area)
        matches.append("(a)-[:LOCATED_IN_AREA]->(ar:Area)")
        where.append("any(x IN $area WHERE toLower(ar.name) CONTAINS toLower(x))")
        params["area"] = area_list
    if property_type:
        pt_list = [property_type] if isinstance(property_type, str) else list(property_type)
        matches.append("(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)")
        where.append("pt.name IN $property_type")
        params["property_type"] = pt_list
    if asset_category:
        ac_list = [asset_category] if isinstance(asset_category, str) else list(asset_category)
        matches.append("(a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)")
        where.append("ac.name IN $asset_category")
        params["asset_category"] = ac_list
    if bank:
        bank_list = [bank] if isinstance(bank, str) else list(bank)
        matches.append("(a)-[:CONDUCTED_BY]->(b:Bank)")
        where.append("b.name IN $bank")
        params["bank"] = bank_list
    if auction_type:
        matches.append("(a)-[:IS_AUCTION_TYPE]->(:AuctionType {name: $auction_type})")
        params["auction_type"] = auction_type
    if branch_name:
        matches.append("(a)-[:LISTED_BY_BRANCH]->(:Branch {name: $branch_name})")
        params["branch_name"] = branch_name

    where_clause = 'WHERE ' + ' AND '.join(where) if where else ''
    match_clause = ', '.join(matches)

    agg_returns = ["count(a) AS total_count"]
    if aggregations:
        for name in aggregations:
            agg_returns.append(f"{_AGG_FUNCS[name].format(f=aggregate_field)} AS {name}")
    agg_cypher = f"MATCH {match_clause} {where_clause} RETURN {', '.join(agg_returns)}"
    agg_rows = run_read_query(
        agg_cypher, {k: v for k, v in params.items() if k != "limit"},
        timeout=15.0, max_rows=1,
    )
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
                   toString(a.auction_start_dt) AS auction_start,
                   city.name AS city, area.name AS area,
                   bank.name AS bank,
                   ac.name AS asset_category,
                   property_types,
                   previous_reserve_price,
                   reauction_count
            ORDER BY {_ORDER_BY_CLAUSES[order_by]}
            LIMIT $limit
        """
        ui_results = run_read_query(cypher, params, timeout=15.0, max_rows=ui_limit)
        for row in ui_results:
            rc = row.get("reauction_count") or 0
            row["reauction_count"] = rc
            row["is_reauction"] = rc > 0

    # LLM-visible slice is capped at the user-requested `limit` AND at the
    # hard `_LLM_ROWS_HARD_CAP` ceiling, so a large model-requested `limit`
    # can't dump hundreds of rows into context. Full rows (up to ui_limit)
    # still ride on `_ui_results` for the UI side-channel.
    llm_limit = min(limit, _LLM_ROWS_HARD_CAP)
    results = ui_results[:llm_limit] if limit > 0 else []

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


# Cap on how many ids one `get_auctions_by_ids` call resolves. The agent uses
# it to mirror an answer's subset ("top three of those") into the UI matches
# panel, so real calls are tiny; the cap just bounds a runaway one.
_BY_IDS_MAX = 25


def get_auctions_by_ids(auction_ids: list[str]) -> dict:
    """Full search-shaped rows for specific auction_ids, in the caller's
    order (the agent's ranking). Ids that don't resolve are reported under
    `missing_ids` so the agent can correct itself instead of presenting a
    property the panel won't show."""
    ids: list[str] = []
    for i in auction_ids or []:
        s = str(i).strip()
        if s and s not in ids:
            ids.append(s)
    ids = ids[:_BY_IDS_MAX]
    if not ids:
        return {"total_count": 0, "returned": 0, "results": []}
    cypher = """
        MATCH (a:AuctionProperty)
        WHERE a.auction_id IN $ids
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
               toString(a.auction_start_dt) AS auction_start,
               city.name AS city, area.name AS area,
               bank.name AS bank,
               ac.name AS asset_category,
               property_types,
               previous_reserve_price,
               reauction_count
    """
    rows = run_read_query(cypher, {"ids": ids}, timeout=15.0, max_rows=_BY_IDS_MAX)
    by_id: dict[str, dict] = {}
    for row in rows:
        rc = row.get("reauction_count") or 0
        row["reauction_count"] = rc
        row["is_reauction"] = rc > 0
        by_id[str(row.get("auction_id"))] = row
    ordered = [by_id[i] for i in ids if i in by_id]
    out: dict = {
        "total_count": len(ordered),
        "returned": len(ordered),
        "results": ordered,
    }
    missing = [i for i in ids if i not in by_id]
    if missing:
        out["missing_ids"] = missing
    return out


def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    cypher = """
        MATCH (a:AuctionProperty)
        WHERE a.application_deadline_dt <= $cutoff
          AND a.application_deadline_dt >= $now
        RETURN a.auction_id AS auction_id, a.title AS title,
               toString(a.application_deadline_dt) AS deadline,
               a.reserve_price_num AS reserve_price
        ORDER BY a.application_deadline_dt ASC
        LIMIT $limit
    """
    return run_read_query(
        cypher, {"cutoff": cutoff, "now": now, "limit": limit},
        max_rows=min(max(int(limit), 1), _UI_ROWS_HARD_CAP),
    )


def borrower_lookup(borrower_name: str) -> list[dict]:
    cypher = """
        MATCH (a:AuctionProperty)-[:HAS_BORROWER]->(b:Borrower)
        WHERE toLower(b.name) CONTAINS toLower($name)
        RETURN a.auction_id AS auction_id, a.title AS title, b.name AS borrower
        LIMIT 50
    """
    return run_read_query(cypher, {"name": borrower_name}, max_rows=50)


def semantic_search(
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
    """Unified semantic search across descriptions, notice markdown, and notice files.

    Embeds the query once via gemini-embedding-2 (3072-dim) and ranks
    AuctionProperty results across three indexes that share the same vector
    space, so cosine scores are directly comparable:

      - property_desc_idx     (AuctionProperty.description_embedding) —
        tight property text, post-extraction. Best for narrow queries
        like "3-BR flat in Adyar with elevator".
      - notice_markdown_idx   (Document.markdown_embedding) — structured
        notice text from MinerU. Best for queries that touch the formal
        notice content (bank framing, parties, schedule, terms).
      - notice_image_idx      (Document.image_embedding) — multimodal
        notice file (image / PDF bytes). Best for layout / visual signal
        ("tabular SFC notices in Villupuram") and as a fallback when the
        text-side description is sparse.
      - property_text_idx     (Lucene fulltext over title + description) —
        lexical "keyword" lens. Catches exact tokens embeddings smear out:
        locality names, survey/plot numbers, bank names. Scores are
        max-normalized per query and weighted into the cosine range.

    For each property the best score from any lens wins, and `hit_sources`
    indicates which lenses matched. Defaults to future-only auctions; pass
    include_past=True for retrospective queries. If the fulltext index is
    missing the search silently degrades to vector-only.

    Returns {returned, limit, results} where each result carries `score`
    (higher is better) and `hit_sources` (list of 'desc' / 'markdown' /
    'image' / 'keyword').
    """
    qvec = embed_query_gemini(query)
    k = max(limit * 5, 50)

    where = []
    params: dict = {"qvec": qvec, "k": k, "limit": limit}
    if min_price is not None:
        where.append("p.reserve_price_num >= $min_price")
        params["min_price"] = min_price
    if max_price is not None:
        where.append("p.reserve_price_num <= $max_price")
        params["max_price"] = max_price
    if starts_after is None and not include_past:
        starts_after = datetime.now(timezone.utc)
    if starts_after is not None:
        where.append("p.auction_start_dt >= $starts_after")
        params["starts_after"] = _aware(starts_after)
    if starts_before is not None:
        where.append("p.auction_start_dt <= $starts_before")
        params["starts_before"] = _aware(starts_before)

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

    ft_query = _lucene_query(query)
    include_keyword = ft_query is not None
    if include_keyword:
        params["ft_query"] = ft_query
        params["keyword_weight"] = _KEYWORD_WEIGHT

    cypher = _semantic_search_cypher(optional_matches, where_clause, include_keyword)
    max_rows = min(max(int(limit), 1), _UI_ROWS_HARD_CAP)
    if include_keyword:
        from neo4j.exceptions import Neo4jError
        try:
            results = run_read_query(cypher, params, timeout=15.0, max_rows=max_rows)
        except Neo4jError:
            # Most likely the fulltext index hasn't been created on this
            # database yet — retry with the vector-only shape so search
            # keeps working. If something else is wrong, the retry's error
            # propagates with the real cause.
            cypher = _semantic_search_cypher(optional_matches, where_clause, False)
            results = run_read_query(cypher, params, timeout=15.0, max_rows=max_rows)
    else:
        results = run_read_query(cypher, params, timeout=15.0, max_rows=max_rows)
    return {"returned": len(results), "limit": limit, "results": results}


def _semantic_search_cypher(
    optional_matches: str, where_clause: str, include_keyword: bool
) -> str:
    """Compose the hybrid retrieval Cypher.

    Fans out across the three Gemini vector indexes — plus, when
    `include_keyword` is set, the Lucene fulltext index — inside a
    CALL () { … UNION … } block (Neo4j 5.7+; the empty variable-scope import
    makes $k / $qvec visible without explicit import). The block returns
    (p, score, source) rows; the outer query applies structured post-filters,
    normalizes keyword scores into the cosine range, and dedupes by p
    (max score across lenses).
    """
    keyword_branch = f"""
            UNION
            CALL db.index.fulltext.queryNodes('{PROPERTY_FULLTEXT_INDEX}', $ft_query, {{limit: $k}})
            YIELD node AS p, score
            RETURN p, score, 'keyword' AS source""" if include_keyword else ""

    # Lucene scores are unbounded while cosines live in [0, 1]; max-normalize
    # the keyword rows per query, then scale by $keyword_weight. The
    # collect/UNWIND round-trip is bounded by 4 × $k rows.
    keyword_normalize = """
        WITH collect({p: p, score: score, source: source}) AS rows
        WITH rows, reduce(m = 0.0, r IN [x IN rows WHERE x.source = 'keyword'] |
                          CASE WHEN r.score > m THEN r.score ELSE m END) AS ft_max
        UNWIND rows AS row
        WITH row.p AS p,
             CASE WHEN row.source = 'keyword'
                  THEN (row.score / CASE WHEN ft_max > 0.0 THEN ft_max ELSE 1.0 END)
                       * $keyword_weight
                  ELSE row.score END AS score,
             row.source AS source""" if include_keyword else ""

    return f"""
        CALL () {{
            CALL db.index.vector.queryNodes('{PROPERTY_DESC_INDEX}', $k, $qvec)
            YIELD node AS p, score
            RETURN p, score, 'desc' AS source
            UNION
            CALL db.index.vector.queryNodes('{NOTICE_MARKDOWN_INDEX}', $k, $qvec)
            YIELD node AS d, score
            MATCH (d)<-[:HAS_DOCUMENT]-(p:AuctionProperty)
            RETURN p, score, 'markdown' AS source
            UNION
            CALL db.index.vector.queryNodes('{NOTICE_IMAGE_INDEX}', $k, $qvec)
            YIELD node AS d, score
            MATCH (d)<-[:HAS_DOCUMENT]-(p:AuctionProperty)
            RETURN p, score, 'image' AS source{keyword_branch}
        }}
        WITH p, score, source
        {optional_matches}
        {where_clause}
        {keyword_normalize}
        WITH p, max(score) AS score, collect(DISTINCT source) AS hit_sources
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
        WITH p, score, hit_sources, city, area, bank, ac,
             collect(DISTINCT ptx.name) AS property_types,
             max(prev.reserve_price_num) AS previous_reserve_price
        RETURN p.auction_id AS auction_id, p.title AS title, p.url AS url,
               p.reserve_price_num AS reserve_price, p.emd_num AS emd,
               toString(p.auction_start_dt) AS auction_start,
               city.name AS city, area.name AS area,
               bank.name AS bank,
               ac.name AS asset_category,
               property_types,
               previous_reserve_price,
               substring(p.description, 0, 300) AS description_excerpt,
               score, hit_sources
        ORDER BY score DESC
        LIMIT $limit
    """


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
    plot_no: str | None = None
    locality_tokens: list[str] = None  # type: ignore[assignment]
    raw_text: str = ""

    def __post_init__(self) -> None:
        if self.locality_tokens is None:
            self.locality_tokens = []


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
# `Plot No.46`, `Plot No 46`, `Plot No: 46`, `Plot Number 46`.
_PLOT_NO_RE = re.compile(
    r"\bplot\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z]?\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)
# Distinctive locality / building names. Two patterns:
#   1. `<Capitalized Word(s)> Nagar/Street/Colony/Layout/Garden/Avenue/...`
#      — neighborhood / sub-locality names, almost unique within a city.
#   2. `<Capitalized Word(s)> Flats/Apartments/Towers/Heights/Residency`
#      — building names. Less unique but high-signal when paired with price+date.
# Both run on the original-case text, then we lower the captures.
_LOCALITY_SUFFIXES = (
    r"Nagar|Nagara|Nagaram|Street|Colony|Layout|Garden|Gardens|"
    r"Avenue|Road|Lane|Block|Phase|Sector"
)
_BUILDING_SUFFIXES = (
    r"Flats?|Apartments?|Towers?|Heights?|Residency|Residences?|"
    r"Enclave|Mansions?|Court|Plaza"
)
_LOCALITY_RE = re.compile(
    rf"\b((?:[A-Z][A-Za-z]+\s+){{1,3}}(?:{_LOCALITY_SUFFIXES}))\b"
)
_BUILDING_RE = re.compile(
    rf"\b((?:[A-Z][A-Za-z]+\s+){{1,3}}(?:{_BUILDING_SUFFIXES}))\b"
)


@lru_cache(maxsize=1)
def _load_known_locations() -> tuple[frozenset[str], frozenset[str]]:
    """Cached set of all (City, Area) names in the graph. Used to disambiguate
    location tokens in pasted text — we never want to mistake a building
    name ('Sai Nila') for an area."""
    city_rows = run_read_query("MATCH (c:City) RETURN c.name AS n", {}, max_rows=20000)
    area_rows = run_read_query("MATCH (a:Area) RETURN a.name AS n", {}, max_rows=20000)
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
    plot_m = _PLOT_NO_RE.search(text)
    if plot_m:
        out.plot_no = plot_m.group(1)

    # Distinctive locality / building tokens. Paste-side capture is on the
    # original case (the regexes require a leading capital), then we lower
    # the captures so the eventual `description CONTAINS toLower(token)`
    # check is case-insensitive.
    seen: set[str] = set()
    tokens: list[str] = []
    for regex in (_LOCALITY_RE, _BUILDING_RE):
        for m in regex.finditer(text):
            tok = " ".join(m.group(1).split()).lower()
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    out.locality_tokens = tokens

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
    """Jaccard overlap — kept only as a fallback ranking signal when the
    weighted multi-signal score below produces a tie."""
    desc = candidate.get("description") or candidate.get("title") or ""
    p_tok = _tie_break_tokens(extracted.raw_text)
    c_tok = _tie_break_tokens(desc)
    if not p_tok or not c_tok:
        return 0.0
    return len(p_tok & c_tok) / len(p_tok | c_tok)


# Per-signal weights for confidence scoring. Sum is 1.0 only when every
# signal is present in the paste AND matches the candidate's description.
# Price + date together = 0.60 (the primary filter already enforces both),
# so any candidate returned by the strict Cypher starts at 0.60. The
# remaining 0.40 comes from description-text matches that confirm the
# candidate is the SAME property as the paste describes.
_SCORE_WEIGHT_PRICE = 0.30
_SCORE_WEIGHT_DATE = 0.30
_SCORE_WEIGHT_BUILT_UP = 0.15
_SCORE_WEIGHT_UDS = 0.10
_SCORE_WEIGHT_LOCALITY = 0.10
_SCORE_WEIGHT_PLOT = 0.05


def _score_candidate(
    extracted: _ExtractedListing,
    candidate: dict,
    *,
    price_matches: bool,
    date_matches: bool,
) -> float:
    """Confidence in [0.0, 1.0]. `price_matches` / `date_matches` are
    passed in because the caller knows which tier of widening produced
    this candidate — at a wider tier the price/date "match" is partial,
    so the corresponding weight is dropped."""
    score = 0.0
    if price_matches:
        score += _SCORE_WEIGHT_PRICE
    if date_matches:
        score += _SCORE_WEIGHT_DATE

    # Description-text confirmations. Lowercased substring search so
    # variations like "741 Sqft" / "741 sq.ft" / "741 sq ft" all hit.
    desc = (candidate.get("description") or "").lower()
    if extracted.built_up_sqft and f"{extracted.built_up_sqft} sq" in desc:
        score += _SCORE_WEIGHT_BUILT_UP
    if extracted.uds_sqft and f"{extracted.uds_sqft} sq" in desc:
        score += _SCORE_WEIGHT_UDS
    if extracted.locality_tokens and any(
        tok in desc for tok in extracted.locality_tokens
    ):
        score += _SCORE_WEIGHT_LOCALITY
    if extracted.plot_no:
        # Match `plot no 46`, `plot no. 46`, `plot 46`, `plot-46`.
        plot_re = re.compile(
            rf"\bplot\s*(?:no\.?|number|#)?\s*[:\-]?\s*{re.escape(extracted.plot_no)}\b",
            re.IGNORECASE,
        )
        if plot_re.search(desc):
            score += _SCORE_WEIGHT_PLOT

    return min(1.0, score)


def match_pasted_listing(pasted_text: str) -> dict:
    """Find the auction in the graph that matches a pasted property listing.

    Strategy:
      1. Extract reserve_price, auction_date, built_up_sqft, uds_sqft,
         plot_no, locality tokens (e.g. "Balaraman Nagar"), PIN.
      2. Primary Cypher: filter on `reserve_price ±2% AND auction_date
         ±2 days` ONLY. **No city, no area in the filter** — Tamil Nadu
         auctions in greater Chennai are filed under Tiruvallur or
         Kanchipuram administrative districts but locals always say
         "Chennai", so a strict city match misses the right property.
      3. Score each candidate by counting independent signals from the
         paste that ALSO appear in the candidate's description text:
         built-up area number, UDS number, distinctive locality tokens,
         plot number. Each adds weight (see `_score_candidate`).
      4. If the strict price+date filter returns nothing, widen
         (drop date → widen price ±10% → drop both) and surface the
         closest hits with a `widening_reason` so the LLM never says
         "no match" — always at least "closest matches we found".

    Returns {match, confidence, alternates, candidates, widening_reason,
    extracted}. `match` is None when no candidate hits the strict tier
    or a useful widening tier; the caller must NOT present alternates
    as a "best match" in that case.
    """
    extracted = _extract_listing_fields(pasted_text)
    extracted_dict = _extracted_to_dict(extracted)

    where, matches, params = _build_filter(extracted)
    rows: list[dict] = []
    if where:
        rows = _run_match_cypher(where, matches, params)

    if rows:
        # Strict tier: price+date both matched by definition.
        scored = sorted(
            rows,
            key=lambda r: (
                _score_candidate(extracted, r, price_matches=True, date_matches=True),
                _tie_break_score(extracted, r),
            ),
            reverse=True,
        )
        top = scored[0]
        confidence = _score_candidate(
            extracted, top, price_matches=True, date_matches=True
        )
        return {
            "match": top,
            "confidence": confidence,
            "alternates": scored[1:5],
            "candidates": scored[:5],
            "widening_reason": None,
            "extracted": extracted_dict,
        }

    # ── Progressive widening fallback ─────────────────────────────────────
    widened, reason, price_ok, date_ok = _widen_until_hits(extracted)
    if widened:
        scored = sorted(
            widened,
            key=lambda r: (
                _score_candidate(extracted, r, price_matches=price_ok, date_matches=date_ok),
                _tie_break_score(extracted, r),
            ),
            reverse=True,
        )
    else:
        scored = []
    return {
        "match": None,
        "confidence": 0.0,
        "alternates": scored[:5],
        "candidates": scored[:5],
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


def _extracted_to_dict(extracted: _ExtractedListing) -> dict:
    return {
        "reserve_price": extracted.reserve_price,
        "auction_date": extracted.auction_date.isoformat() if extracted.auction_date else None,
        "emd_date": extracted.emd_date.isoformat() if extracted.emd_date else None,
        "pin": extracted.pin,
        "city": extracted.city,
        "area": extracted.area,
        "built_up_sqft": extracted.built_up_sqft,
        "uds_sqft": extracted.uds_sqft,
        "plot_no": extracted.plot_no,
        "locality_tokens": list(extracted.locality_tokens),
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
        LIMIT 50
    """
    return run_read_query(cypher, params, max_rows=50)


def _widen_until_hits(
    extracted: _ExtractedListing,
) -> tuple[list[dict], str | None, bool, bool]:
    """Try progressively looser filters until something hits. Returns
    (rows, widening_reason, price_still_strict, date_still_strict).
    The trailing booleans tell the caller whether to count price/date
    as a confidence-boosting match — at a wider tier the constraint is
    only approximate, so the corresponding scoring weight is suppressed.

    Tier order, most-volatile-first:
      1. drop locality (typos / OCR errors / abbreviated names)
      2. drop date (re-auctions slide by weeks)
      3. drop date + widen price ±10% (banks adjust reserves)
      4. drop everything (description-only fallback)

    City and area are NEVER part of the filter — see `_build_filter`."""
    tiers: list[tuple[str, dict, bool, bool]] = [
        # (reason, build_kwargs, price_still_strict, date_still_strict)
        ("dropped locality / building token constraint",
            {"drop_locality": True}, True, True),
        ("dropped locality and auction-date constraints",
            {"drop_locality": True, "drop_date": True}, True, False),
        ("dropped locality + date and widened price band to ±10%",
            {"drop_locality": True, "drop_date": True, "price_pct": 0.10}, False, False),
        ("dropped all structured constraints",
            {"drop_locality": True, "drop_date": True, "drop_price": True}, False, False),
    ]
    # Seed with the strict tier's filter so we don't re-run it as a "widening"
    # tier when one of the relaxations is a no-op (e.g. drop_locality when
    # the paste yielded no locality tokens). Key by both WHERE-text and
    # param values — a price-widen tier reuses the same WHERE clauses but
    # bumps min_price/max_price, so clauses-alone dedup would skip it.
    def _filter_key(where: list[str], params: dict) -> tuple:
        return (tuple(sorted(where)),
                tuple(sorted((k, str(v)) for k, v in params.items())))

    strict_where, _, strict_params = _build_filter(extracted)
    tried: set[tuple] = {_filter_key(strict_where, strict_params)}
    for reason, kwargs, price_ok, date_ok in tiers:
        where, matches, params = _build_filter(extracted, **kwargs)
        if not where:
            continue
        key = _filter_key(where, params)
        if key in tried:
            continue
        tried.add(key)
        rows = _run_match_cypher(where, matches, params)
        if rows:
            return rows, reason, price_ok, date_ok
    return [], None, False, False


def _build_filter(
    extracted: _ExtractedListing,
    *,
    drop_date: bool = False,
    drop_price: bool = False,
    drop_locality: bool = False,
    price_pct: float = 0.02,
) -> tuple[list[str], list[str], dict]:
    """Rebuild the (where, matches, params) triple with selected
    constraints relaxed.

    Note on `locality_tokens`: when the paste yields distinctive
    locality tokens (e.g. "balaraman nagar"), they're added to the
    Cypher WHERE as OR-joined `toLower(a.description) CONTAINS`
    clauses. This is the killer narrowing signal — locality names
    are nearly unique within a city, so a single token usually
    pinpoints the exact property. v3 promoted this from a scoring
    weight (v2) to a primary filter."""
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
        params["starts_after"] = _aware(starts_after)
        params["starts_before"] = _aware(starts_before)
        where.append("a.auction_start_dt >= $starts_after")
        where.append("a.auction_start_dt <= $starts_before")
    if extracted.locality_tokens and not drop_locality:
        # OR-joined CONTAINS over each locality / building token.
        # Bind each token as $loc_0, $loc_1, ... so we keep parameterised
        # queries and don't string-format user-derived text into Cypher.
        clauses: list[str] = []
        for i, tok in enumerate(extracted.locality_tokens):
            key = f"loc_{i}"
            clauses.append(f"toLower(a.description) CONTAINS toLower(${key})")
            params[key] = tok
        where.append("(" + " OR ".join(clauses) + ")")
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
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
            WHERE doc.public_url IS NOT NULL
        OPTIONAL MATCH (a)-[link:SAME_PROPERTY_AS]->(sibling:AuctionProperty)
        WITH a, city, area, state, bank, borrower, ac,
             collect(DISTINCT pt.name) AS property_types,
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
                 property_types: property_types
               } AS relationships,
               documents AS documents,
               siblings  AS siblings
    """
    rows = run_read_query(cypher, {"auction_id": auction_id}, max_rows=1)
    if not rows:
        return None
    # Coerce every neo4j temporal value (top-level fields AND nested
    # related-node maps) to ISO strings up front so the response serializer
    # never sees a raw neo4j.time.* object.
    fields = _json_safe(dict(rows[0]["fields"]))

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
                "auction_start_dt":  _iso(s.get("auction_start_dt")),
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
        "relationships": _json_safe(rows[0]["relationships"]),
        "documents":     documents,
        "price_history": price_history,
    }


# ── Phase 1: schema introspection + escape-hatch tools ─────────────────────

# Map a logical field name the agent might use to the (label, relationship)
# pair needed to count AuctionProperty references.
_DISTINCT_FIELDS: dict[str, tuple[str, str]] = {
    "city":           ("City",          "LOCATED_IN_CITY"),
    "area":           ("Area",          "LOCATED_IN_AREA"),
    "state":          ("State",         "LOCATED_IN_STATE"),
    "bank":           ("Bank",          "CONDUCTED_BY"),
    "branch":         ("Branch",        "LISTED_BY_BRANCH"),
    "borrower":       ("Borrower",      "HAS_BORROWER"),
    "asset_category": ("AssetCategory", "HAS_ASSET_CATEGORY"),
    "property_type":  ("PropertyType",  "HAS_PROPERTY_TYPE"),
    "auction_type":   ("AuctionType",   "IS_AUCTION_TYPE"),
}

_SCHEMA_CACHE: dict[str, tuple[float, dict]] = {}
_SCHEMA_TTL_SECONDS = 3600.0

# Live AuctionProperty node count, cached in-process. The agent's system
# instructions surface this so "how many properties are there" answers track
# the real graph size instead of a hardcoded number that goes stale as the
# loader ingests new auctions. The count only moves when the loader runs, so
# the schema TTL is plenty; on any read failure we serve the last good value
# (or None) rather than a wrong one.
_PROPERTY_COUNT_CACHE: dict[str, tuple[float, int]] = {}


async def graph_property_count_async(refresh: bool = False) -> int | None:
    """Total AuctionProperty nodes in the graph (live, cached for
    `_SCHEMA_TTL_SECONDS`). Returns None when the count can't be read — a
    cold cache plus DB outage — so callers can fall back to a number-free
    phrasing instead of asserting a stale figure. A transient failure after a
    successful read keeps serving the last good value."""
    now = time.time()
    cached = _PROPERTY_COUNT_CACHE.get("default")
    if cached and not refresh and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
        return cached[1]
    try:
        rows = await run_read_query_async(
            "MATCH (a:AuctionProperty) RETURN count(a) AS n", max_rows=1
        )
    except Exception:
        # Never let a count read break a chat turn; degrade to last good value.
        return cached[1] if cached else None
    count = rows[0].get("n") if rows else None
    if not isinstance(count, int):
        return cached[1] if cached else None
    _PROPERTY_COUNT_CACHE["default"] = (now, count)
    return count


# Cypher patterns surfaced via describe_schema() so they don't bloat the
# per-turn system prompt. The agent only needs these when composing
# run_cypher queries; pulling them on demand keeps the baseline chat
# prompt ~1.5K tokens lighter.
_CYPHER_PATTERN_RULES = [
    "HAS_ASSET_CATEGORY, HAS_PROPERTY_TYPE, CONDUCTED_BY, HAS_BORROWER, "
    "and LOCATED_IN_* all start on AuctionProperty. MATCH each relationship "
    "independently from `a` and join with commas. Do NOT chain "
    "(Bank)-[:HAS_PROPERTY_TYPE] or (Bank)-[:HAS_ASSET_CATEGORY] — those "
    "relationships do not exist.",
    "auction_start_dt / auction_end_dt / application_deadline_dt are native "
    "ZONED DATETIME (UTC). Group by component accessors "
    "(.year .month .day .hour .dayOfWeek .quarter), never substring().",
    "Now / arithmetic: datetime() for now; datetime() + duration({days: 7}) "
    "for 'now + 7 days'.",
    "Gaps: duration.between(a, b) returns a Duration with .days, .hours; "
    "for numeric hours use duration.inSeconds(a, b).seconds / 3600.0.",
    "Calendar-day equality: date(a.auction_start_dt) = date($other) strips "
    "time-of-day so two timestamps on the same day match.",
    "NEVER compare a DATETIME column against a raw ISO string parameter — "
    "Cypher silently returns zero matches across ZONED-vs-LOCAL DATETIME. "
    "If you must pass an ISO string, wrap it on the WHERE side: "
    "WHERE a.auction_start_dt >= datetime($iso).",
    "For scoped breakdowns, prefer the list_distinct tool (with city, bank, "
    "borrower, asset_category, auction_type, or branch scope) before writing "
    "a run_cypher — the tool already composes the correct Cypher shape.",
]

_CYPHER_PATTERN_EXAMPLES = [
    {
        "purpose": "Count auctions per city",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(c:City)\n"
            "RETURN c.name AS city, count(a) AS n\n"
            "ORDER BY n DESC LIMIT 20"
        ),
    },
    {
        "purpose": "Auctions per bank in a city",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:CONDUCTED_BY]->(b:Bank),\n"
            "      (a)-[:LOCATED_IN_CITY]->(c:City {name: $city})\n"
            "RETURN b.name AS bank, count(a) AS n\n"
            "ORDER BY n DESC"
        ),
    },
    {
        "purpose": "Monthly auction volume",
        "cypher": (
            "MATCH (a:AuctionProperty)\n"
            "WHERE a.auction_start_dt IS NOT NULL\n"
            "RETURN a.auction_start_dt.year  AS year,\n"
            "       a.auction_start_dt.month AS month,\n"
            "       count(a) AS n\n"
            "ORDER BY year, month"
        ),
    },
    {
        "purpose": "Borrowers with multiple properties",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:HAS_BORROWER]->(b:Borrower)\n"
            "WITH b, count(a) AS n WHERE n > 1\n"
            "RETURN b.name AS borrower, n ORDER BY n DESC"
        ),
    },
    {
        "purpose": "Areas where EMD-to-reserve ratio is unusual",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:LOCATED_IN_AREA]->(ar:Area)\n"
            "WHERE a.reserve_price_num > 0 AND a.emd_num > 0\n"
            "WITH ar, avg(a.emd_num / a.reserve_price_num) AS emd_ratio, count(a) AS n\n"
            "WHERE n >= 5\n"
            "RETURN ar.name AS area, emd_ratio, n ORDER BY emd_ratio DESC LIMIT 20"
        ),
    },
    {
        "purpose": "Property-type breakdown filtered by bank",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:CONDUCTED_BY]->(:Bank {name: $bank}),\n"
            "      (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)\n"
            "RETURN pt.name AS property_type, count(DISTINCT a) AS n\n"
            "ORDER BY n DESC"
        ),
    },
    {
        "purpose": "Asset-category breakdown in a city",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name: $city}),\n"
            "      (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)\n"
            "RETURN ac.name AS asset_category, count(DISTINCT a) AS n\n"
            "ORDER BY n DESC"
        ),
    },
    {
        "purpose": "Auctions ending in the next 7 days",
        "cypher": (
            "MATCH (a:AuctionProperty)\n"
            "WHERE a.auction_end_dt >= datetime()\n"
            "  AND a.auction_end_dt <  datetime() + duration({days: 7})\n"
            "RETURN count(a) AS n"
        ),
    },
    {
        "purpose": "Distribution by weekday (1 = Monday … 7 = Sunday)",
        "cypher": (
            "MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL\n"
            "RETURN a.auction_start_dt.dayOfWeek AS dow, count(a) AS n\n"
            "ORDER BY dow"
        ),
    },
    {
        "purpose": "Auctions starting during business hours (9–17, server-local clock)",
        "cypher": (
            "MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL\n"
            "WITH a.auction_start_dt.hour AS h\n"
            "WHERE h >= 9 AND h <= 17\n"
            "RETURN count(*) AS n"
        ),
    },
    {
        "purpose": "Quarter bucket",
        "cypher": (
            "MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL\n"
            "RETURN a.auction_start_dt.year    AS year,\n"
            "       a.auction_start_dt.quarter AS q,\n"
            "       count(a) AS n\n"
            "ORDER BY year, q"
        ),
    },
    {
        "purpose": "Deadline-to-start gap in hours",
        "cypher": (
            "MATCH (a:AuctionProperty)\n"
            "WHERE a.application_deadline_dt IS NOT NULL\n"
            "  AND a.auction_start_dt        IS NOT NULL\n"
            "RETURN a.auction_id AS id,\n"
            "       duration.inSeconds(a.application_deadline_dt,\n"
            "                          a.auction_start_dt).seconds / 3600.0 AS gap_hours\n"
            "ORDER BY gap_hours"
        ),
    },
    {
        "purpose": "Deadline within 24 hours of the auction start",
        "cypher": (
            "MATCH (a:AuctionProperty)\n"
            "WHERE a.application_deadline_dt IS NOT NULL\n"
            "  AND a.auction_start_dt        IS NOT NULL\n"
            "WITH a, duration.inSeconds(a.application_deadline_dt,\n"
            "                           a.auction_start_dt).seconds / 3600.0 AS gap_hours\n"
            "WHERE abs(gap_hours) <= 24\n"
            "RETURN count(a) AS n"
        ),
    },
    {
        "purpose": "Same-calendar-day siblings (batch-sale detection)",
        "cypher": (
            "MATCH (a:AuctionProperty {auction_id: $id})-[:SAME_PROPERTY_AS]->(s)\n"
            "WHERE date(s.auction_start_dt) = date(a.auction_start_dt)\n"
            "RETURN s.auction_id AS sibling_id,\n"
            "       toString(s.auction_start_dt) AS starts"
        ),
    },
    {
        "purpose": "Re-auction velocity: avg days between a property's listings",
        "cypher": (
            "MATCH (a:AuctionProperty)-[:SAME_PROPERTY_AS]->(prev:AuctionProperty)\n"
            "WHERE a.auction_start_dt IS NOT NULL AND prev.auction_start_dt IS NOT NULL\n"
            "WITH duration.inSeconds(prev.auction_start_dt, a.auction_start_dt).seconds / 86400.0 AS gap_days\n"
            "RETURN avg(gap_days) AS avg_gap_days, count(*) AS pairs"
        ),
    },
]


def list_distinct(
    field: str,
    limit: int = 100,
    city: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    borrower: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    auction_type: str | list[str] | None = None,
    branch: str | list[str] | None = None,
) -> dict:
    """List distinct values of a reference field with counts.

    `field` must be one of the keys in _DISTINCT_FIELDS. Scope filters
    (`city`, `bank`, `borrower`, `asset_category`, `auction_type`,
    `branch`) narrow the count to auctions that match every provided
    scope. Each scope accepts either a single string or a list of
    strings (any-match within the list). A scope must differ from
    `field` — you can't group by bank while filtering by bank.

    Use this for distribution / breakdown / "spread" questions
    ("property-type mix for SBI", "asset categories in Chennai",
    "auction-type breakdown for Canara Bank"). Never iterate
    `get_auction_detail` to compute a count.
    """
    if field not in _DISTINCT_FIELDS:
        raise ValueError(
            f"field must be one of {sorted(_DISTINCT_FIELDS)}, got {field!r}"
        )

    raw_scopes: dict[str, str | list[str] | None] = {
        "city":           city,
        "bank":           bank,
        "borrower":       borrower,
        "asset_category": asset_category,
        "auction_type":   auction_type,
        "branch":         branch,
    }
    # Filtering by the same dimension you're grouping on is a no-op; drop
    # silently so agents can pass redundant scopes without an error.
    active_scopes = {k: v for k, v in raw_scopes.items() if v and k != field}

    label, rel = _DISTINCT_FIELDS[field]
    params: dict = {"limit": int(limit)}

    scope_matches: list[str] = []
    where_clauses: list[str] = []
    for scope_field, value in active_scopes.items():
        scope_label, scope_rel = _DISTINCT_FIELDS[scope_field]
        value_list = [value] if isinstance(value, str) else list(value)
        var = f"n_{scope_field}"
        scope_matches.append(f"(a)-[:{scope_rel}]->({var}:{scope_label})")
        where_clauses.append(f"{var}.name IN ${scope_field}")
        params[scope_field] = value_list

    match_clauses = ["(a:AuctionProperty)"]
    match_clauses.extend(scope_matches)
    match_clauses.append(f"(a)-[:{rel}]->(n:{label})")
    match_clause = ",\n                  ".join(match_clauses)
    where_clause = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    cypher = f"""
            MATCH {match_clause}
            {where_clause}
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
        "filter_auction_type": auction_type,
        "filter_branch": branch,
        "results": results,
    }


def describe_schema(refresh: bool = False) -> dict:
    """Return a compact description of the graph schema and cardinalities.

    Results are cached in-process for `_SCHEMA_TTL_SECONDS`. Pass
    `refresh=True` to bypass the cache. Fields returned:

    - node_labels: [{label, count, sample_properties}, ...]
    - relationships: [{type, from, to, count}, ...]
    - enums: {asset_category, property_type, ...}
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
          toString(min(a.auction_start_dt))         AS start_min,
          toString(max(a.auction_start_dt))         AS start_max,
          toString(min(a.application_deadline_dt))  AS dl_min,
          toString(max(a.application_deadline_dt))  AS dl_max
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
    date_capabilities = {
        "type": "ZONED DATETIME (UTC)",
        "fields": ["auction_start_dt", "auction_end_dt", "application_deadline_dt"],
        "supports": [
            "component accessors: .year .month .day .hour .dayOfWeek .quarter",
            "now: datetime()",
            "arithmetic: datetime() + duration({days: 7})",
            "gaps: duration.between(a, b), duration.inSeconds(a, b).seconds",
            "calendar equality: date(dt_a) = date(dt_b)",
            "range indexes exist on all three fields",
        ],
        "warning": (
            "Comparing a DATETIME column against a raw ISO string parameter "
            "silently returns zero matches. In run_cypher, either pass a "
            "real datetime via the structured tools, or wrap the parameter "
            "on the WHERE side: WHERE a.auction_start_dt >= datetime($iso)."
        ),
    }

    out = {
        "node_labels": label_info,
        "relationships": rel_info,
        "enums": enums,
        "numeric_ranges": numeric_ranges,
        "date_ranges": date_ranges,
        "date_capabilities": date_capabilities,
        "cypher_patterns": {
            "rules": _CYPHER_PATTERN_RULES,
            "examples": _CYPHER_PATTERN_EXAMPLES,
        },
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

    # Defensive serialization: when the agent's Cypher returns DATETIME
    # columns directly (without toString()), neo4j.time.DateTime objects
    # land in the rows. Pydantic-AI's serializer can't handle them and the
    # whole /chat response 500s. Walk every row and coerce DateTime → ISO.
    rows = [_serialize_row(r) for r in rows]

    return {
        "description": description,
        "cypher": cypher,
        "params": coerced,
        "rows": rows,
        "returned": len(rows),
        "duration_ms": duration_ms,
    }


def _serialize_row(row: dict) -> dict:
    """Recursively replace neo4j.time.DateTime / Date / Time / Duration
    values in a row dict with their string forms, so JSON / Pydantic
    serialization downstream can't choke on them. No-op for primitives."""
    return {k: _serialize_value(v) for k, v in row.items()}


def _serialize_value(v):
    if hasattr(v, "iso_format"):  # neo4j.time.DateTime / Date / Time
        return v.iso_format()
    if hasattr(v, "iso_format") or v.__class__.__name__ == "Duration":
        return str(v)
    if isinstance(v, dict):
        return {k: _serialize_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_serialize_value(x) for x in v]
    return v
