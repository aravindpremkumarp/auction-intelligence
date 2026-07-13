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
from datetime import datetime, timedelta, timezone
from api.neo4j_client import run_query, run_read_query, run_read_query_async
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

# Bucket cap for `group_by` distributions (matches the old list_distinct
# default). Distributions are value→count pairs, so 100 stays tiny in context.
_GROUP_BY_MAX_BUCKETS = 100

# ── narrowing diagnostics (attached to search_auctions results) ──────────────
# Dimensions offered as counted "narrow it down" hints on a broad result, in
# priority order; a dimension the search already constrains is skipped (it
# can't narrow further). See the `refine` block in search_auctions.
_REFINE_DIMS = ("property_type", "area", "asset_category", "bank")
_MAX_REFINE_DIMS = 2       # at most this many dimensions per result
_REFINE_BUCKETS = 4        # top buckets shown per dimension
# Only attach refine hints once the match set is bigger than what the model
# ever sees (the LLM row cap) — below that it already has the whole set.
_REFINE_MIN_TOTAL = _LLM_ROWS_HARD_CAP

# Substantive filters whose leave-one-out count diagnoses an over-constrained
# zero ("drop max_price → 6 matches"). Date/window filters are deliberately
# excluded: the future-only floor has its own past_matches diagnostic, and a
# caller-set window is an intentional zero (see test_zero_result_hints).
_RELAXABLE_FILTERS = (
    "min_price", "max_price", "min_emd", "max_emd",
    "city", "area", "property_type", "asset_category",
    "bank", "borrower", "auction_type", "branch_name",
    "service_provider", "is_reauction",
)

# `deadline_*` orders by the application deadline (the actionable bidding
# cutoff); `start_*` by the auction start. Historically both `deadline_*`
# keys pointed at `auction_start_dt` — a mislabel, since the row's deadline
# field is `application_deadline_dt`. They now sort by the field they name.
_ORDER_BY_CLAUSES = {
    "deadline_asc":  "a.application_deadline_dt ASC",
    "deadline_desc": "a.application_deadline_dt DESC",
    "start_asc":     "a.auction_start_dt ASC",
    "start_desc":    "a.auction_start_dt DESC",
    "price_asc":     "a.reserve_price_num ASC",
    "price_desc":    "a.reserve_price_num DESC",
    "emd_asc":       "a.emd_num ASC",
    "emd_desc":      "a.emd_num DESC",
}


def _distribution_query(
    dim: str, matches: list[str], where: list[str], params: dict, limit: int,
) -> list[dict]:
    """value → auction_count buckets for one dimension under the given
    match/where scope, ordered by count desc. Shared by `group_by`
    distributions and the `refine` narrowing hints so both bucket the same
    way. Walks an edge to the dimension node (or reads a node prop for
    `service_provider`); `params` is passed through minus the row `limit`."""
    dist_matches = list(matches)
    dist_where = list(where)
    if dim in _DISTINCT_NODE_PROPS:
        value_expr = _DISTINCT_NODE_PROPS[dim]
        dist_where.append(f"{value_expr} IS NOT NULL")
    else:
        g_label, g_rel = _DISTINCT_FIELDS[dim]
        dist_matches.append(f"(a)-[:{g_rel}]->(g:{g_label})")
        value_expr = "g.name"
    dist_where_clause = ("WHERE " + " AND ".join(dist_where)) if dist_where else ""
    dist_cypher = f"""
        MATCH {', '.join(dist_matches)}
        {dist_where_clause}
        RETURN {value_expr} AS value, count(DISTINCT a) AS auction_count
        ORDER BY auction_count DESC
        LIMIT {limit}
    """
    return run_read_query(
        dist_cypher, {k: v for k, v in params.items() if k != "limit"},
        timeout=15.0, max_rows=limit,
    )


def search_auctions(
    min_price: float | None = None,
    max_price: float | None = None,
    min_emd: float | None = None,
    max_emd: float | None = None,
    city: str | list[str] | None = None,
    area: str | list[str] | None = None,
    property_type: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    borrower: str | list[str] | None = None,
    auction_type: str | list[str] | None = None,
    branch_name: str | list[str] | None = None,
    service_provider: str | list[str] | None = None,
    is_reauction: bool | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    deadline_within_days: int | None = None,
    limit: int = 10,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    group_by: str | None = None,
    include_past: bool = False,
    _diagnose: bool = True,
) -> dict:
    # Raw caller-supplied filters, captured before the future-only floor
    # rewrites `starts_after` below. Drives the `refine` narrowing hints and
    # the over-constrained-zero leave-one-out. `_diagnose=False` on the
    # recursive probes those diagnostics spawn stops them recursing.
    _user_filters: dict = {
        "min_price": min_price, "max_price": max_price,
        "min_emd": min_emd, "max_emd": max_emd,
        "city": city, "area": area,
        "property_type": property_type, "asset_category": asset_category,
        "bank": bank, "borrower": borrower,
        "auction_type": auction_type, "branch_name": branch_name,
        "service_provider": service_provider, "is_reauction": is_reauction,
        "starts_after": starts_after, "starts_before": starts_before,
        "deadline_within_days": deadline_within_days,
    }
    if group_by is not None and (
        group_by not in _DISTINCT_FIELDS and group_by not in _DISTINCT_NODE_PROPS
    ):
        valid = sorted([*_DISTINCT_FIELDS, *_DISTINCT_NODE_PROPS])
        raise ValueError(f"group_by must be one of {valid}, got {group_by!r}")
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
    if min_emd is not None:
        where.append("a.emd_num >= $min_emd")
        params["min_emd"] = min_emd
    if max_emd is not None:
        where.append("a.emd_num <= $max_emd")
        params["max_emd"] = max_emd
    # A re-listing is a property with a SAME_PROPERTY_AS neighbour whose
    # auction started EARLIER — same shape the row query uses to derive
    # `is_reauction`, so the filter and the row flag can't disagree.
    if is_reauction is not None:
        exists_clause = (
            "EXISTS { MATCH (a)-[:SAME_PROPERTY_AS]->(p:AuctionProperty) "
            "WHERE p.auction_start_dt < a.auction_start_dt }"
        )
        where.append(exists_clause if is_reauction else f"NOT {exists_clause}")
    # Substring, not exact: live values are messy near-duplicates
    # ("Public Auction" vs "PublicAuction", "bankeauctions.com / C1 India"),
    # so exact enum matching would be a zero-result trap.
    if service_provider:
        sp_list = (
            [service_provider] if isinstance(service_provider, str)
            else list(service_provider)
        )
        where.append(
            "any(x IN $service_provider WHERE toLower(a.service_provider) CONTAINS toLower(x))"
        )
        params["service_provider"] = sp_list
    # `deadline_within_days` bounds the application-deadline window (now ..
    # now+N) — the "upcoming deadlines in N days" query. It's already an
    # explicit future window, so it stands in for the default future-only
    # start floor (same as a caller-set starts_after): no extra floor, no
    # zero-result diagnostic.
    if deadline_within_days is not None:
        now = datetime.now(timezone.utc)
        where.append("a.application_deadline_dt >= $deadline_from")
        where.append("a.application_deadline_dt <= $deadline_to")
        params["deadline_from"] = now
        params["deadline_to"] = now + timedelta(days=deadline_within_days)
    # Remember whether the future-only floor was defaulted (vs. caller-set):
    # on a zero-result outcome we diagnose whether that default is what hid
    # the matches, so the model gets one clear signal instead of flailing
    # through filter variations (observed: a 26-tool-call retry loop).
    default_future_only = (
        starts_after is None and not include_past and deadline_within_days is None
    )
    if default_future_only:
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
    if borrower:
        borrower_list = [borrower] if isinstance(borrower, str) else list(borrower)
        matches.append("(a)-[:HAS_BORROWER]->(bor:Borrower)")
        where.append("any(x IN $borrower WHERE toLower(bor.name) CONTAINS toLower(x))")
        params["borrower"] = borrower_list
    if auction_type:
        at_list = [auction_type] if isinstance(auction_type, str) else list(auction_type)
        matches.append("(a)-[:IS_AUCTION_TYPE]->(at:AuctionType)")
        where.append("at.name IN $auction_type")
        params["auction_type"] = at_list
    if branch_name:
        br_list = [branch_name] if isinstance(branch_name, str) else list(branch_name)
        matches.append("(a)-[:LISTED_BY_BRANCH]->(br:Branch)")
        where.append("br.name IN $branch_name")
        params["branch_name"] = br_list

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

    # `group_by` turns the call into a distribution query: value → auction
    # count over the SAME filter set (this is what the old list_distinct did,
    # but with the full search scope — price/EMD/date/platform included). The
    # row fetch is skipped; the buckets are the answer.
    distribution: list[dict] | None = None
    if group_by is not None and total_count > 0:
        distribution = _distribution_query(
            group_by, matches, where, params, _GROUP_BY_MAX_BUCKETS,
        )
    elif group_by is not None:
        distribution = []

    ui_results: list[dict] = []
    if limit > 0 and total_count > 0 and group_by is None:
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
                   toString(a.application_deadline_dt) AS application_deadline,
                   a.service_provider AS service_provider,
                   city.name AS city, area.name AS area,
                   bank.name AS bank, bank.short_name AS bank_short,
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
    if group_by is not None:
        out["group_by"] = group_by
        out["distribution"] = distribution or []

    # Counted "narrow it down" hints (idea: refine). On a plain search whose
    # match set is bigger than the model ever sees, attach top buckets for up
    # to _MAX_REFINE_DIMS dimensions the search doesn't already constrain, so
    # the broad-result nudge names concrete filters WITH live counts instead
    # of guessing from its row sample. Reuses the group_by distribution query;
    # skipped on the diagnostic-free probe path and when the caller asked for
    # stats/distribution rather than rows.
    if (
        _diagnose and group_by is None and not aggregations
        and total_count > _REFINE_MIN_TOTAL
    ):
        active = {k for k, v in _user_filters.items() if v is not None and v != []}
        refine: dict[str, list[dict]] = {}
        for dim in _REFINE_DIMS:
            if len(refine) >= _MAX_REFINE_DIMS:
                break
            if dim in active:
                continue
            try:
                raw = _distribution_query(dim, matches, where, params, _REFINE_BUCKETS)
            except Exception:  # noqa: BLE001 - narrowing hints are best-effort
                continue
            buckets = [
                {"value": b["value"], "count": b["auction_count"]}
                for b in raw if b.get("value") and b.get("auction_count")
            ]
            if len(buckets) >= 2:  # a single bucket can't narrow anything
                refine[dim] = buckets
        if refine:
            out["refine"] = refine

    if _diagnose and total_count == 0:
        # (a) Over-constrained? Leave-one-out over substantive filters: which
        #     single filter, dropped, would let matches through ("drop
        #     max_price → 6"). Date/window filters are excluded — the floor has
        #     the past_matches diagnostic in (b) — so this needs >=2 of them.
        relax: list[dict] = []
        active_relaxable = [
            k for k in _RELAXABLE_FILTERS
            if _user_filters.get(k) is not None and _user_filters.get(k) != []
        ]
        if len(active_relaxable) >= 2:
            for drop in active_relaxable:
                probe = dict(_user_filters)
                probe[drop] = None
                try:
                    sub = search_auctions(
                        **probe, include_past=include_past, limit=0, _diagnose=False,
                    )
                except Exception:  # noqa: BLE001 - best-effort diagnostic
                    continue
                n = sub.get("total_count", 0) if isinstance(sub, dict) else 0
                if n:
                    relax.append({"filter": drop, "matches": n})
            relax.sort(key=lambda r: r["matches"], reverse=True)
            if relax:
                out["relax"] = relax

        # (b) Future-only floor the culprit? One cheap count with the floor
        #     dropped — only when the floor was defaulted, never when the caller
        #     set the window (that zero is intentional). `floor_probed` stays
        #     False if the count fails, so a timed-out diagnostic yields a plain
        #     zero (no confident "nothing anywhere" claim) rather than a wrong one.
        past_total = 0
        floor_probed = False
        if default_future_only:
            where_no_floor = [w for w in where if w != "a.auction_start_dt >= $starts_after"]
            no_floor_clause = 'WHERE ' + ' AND '.join(where_no_floor) if where_no_floor else ''
            try:
                past_rows = run_read_query(
                    f"MATCH {match_clause} {no_floor_clause} RETURN count(a) AS total_count",
                    {k: v for k, v in params.items() if k not in ("limit", "starts_after")},
                    timeout=15.0, max_rows=1,
                )
            except Exception:  # noqa: BLE001 - the hint is best-effort, never fatal
                # The unfloored count is heavier than the indexed primary (no
                # date anchor); a timeout must not fail a valid 0-match answer.
                past_rows = None
            if past_rows is not None:
                floor_probed = True
                past_total = past_rows[0].get("total_count", 0) if past_rows else 0
                if past_total:
                    out["past_matches"] = past_total

        # (c) One coherent hint, most actionable diagnosis first.
        if relax:
            lead = relax[0]
            tail = (
                f" ({past_total} past auction(s) also match — include_past=true "
                "for a retrospective view.)" if past_total else ""
            )
            out["hint"] = (
                "0 matches with every filter combined, but relaxing one would "
                f"help — see `relax` (e.g. drop {lead['filter']} → {lead['matches']} "
                "match(es)). Tell the user which single constraint to loosen and "
                f"confirm before widening; don't silently drop filters.{tail}"
            )
        elif past_total:
            out["hint"] = (
                f"0 upcoming auctions match, but {past_total} past auction(s) "
                "do — the future-only default excluded them. For a "
                "retrospective question retry once with include_past=true; "
                "otherwise report no upcoming matches. Do not retry other "
                "filter variations."
            )
        elif floor_probed:
            out["hint"] = (
                "No auctions match these filters in any time window — that "
                "is the answer. Report no matches and offer the closest "
                "alternative; widening price or dropping filters cannot help "
                "when nothing exists in any window. Only re-search to fix an "
                "obvious enum/spelling error — otherwise do not retry the "
                "same shape."
            )
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
               toString(a.application_deadline_dt) AS application_deadline,
               a.service_provider AS service_provider,
               city.name AS city, area.name AS area,
               bank.name AS bank, bank.short_name AS bank_short,
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
    # Same zero-result diagnosis as search_auctions: remember whether the
    # future-only floor was defaulted so an empty result can say WHY it's
    # empty instead of inviting paraphrase-retry loops.
    default_future_only = starts_after is None and not include_past
    if default_future_only:
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
            include_keyword = False
            cypher = _semantic_search_cypher(optional_matches, where_clause, False)
            results = run_read_query(cypher, params, timeout=15.0, max_rows=max_rows)
    else:
        results = run_read_query(cypher, params, timeout=15.0, max_rows=max_rows)

    # LLM/UI split — same shape as search_auctions. The model only needs the
    # top slice to reason and cite; the full ranked set (up to the fetched
    # `limit`) rides on `_ui_results` for the matches panel, which the agent
    # wrapper moves onto ToolReturn metadata so it never enters context. This
    # is also what makes "raise the limit once for more recall" safe: a big
    # limit feeds the UI without dumping dozens of rows into the model's
    # replayed history.
    llm_results = results[:_LLM_ROWS_HARD_CAP]
    out: dict = {"returned": len(llm_results), "limit": limit, "results": llm_results}
    if len(results) > len(llm_results):
        out["_ui_results"] = results
    if not results and default_future_only:
        # Re-run WITHOUT the future-only floor, reusing the embedding already
        # computed above — one extra Neo4j query, no extra Gemini call. Tells
        # the model whether past auctions would have matched, so a zero turns
        # into one informed decision instead of rephrased retries.
        where_no_floor = [w for w in where if w != "p.auction_start_dt >= $starts_after"]
        no_floor_clause = ("WHERE " + " AND ".join(where_no_floor)) if where_no_floor else ""
        no_floor_cypher = _semantic_search_cypher(
            optional_matches, no_floor_clause, include_keyword
        )
        no_floor_params = {k: v for k, v in params.items() if k != "starts_after"}
        try:
            past_hits = run_read_query(
                no_floor_cypher, no_floor_params, timeout=15.0, max_rows=max_rows
            )
        except Exception:  # noqa: BLE001 - the hint is best-effort, never fatal
            # Distinguish "diagnostic failed" from "verified zero": no hint at
            # all here, so the model never gets a confident no-matches claim
            # the code didn't actually verify.
            past_hits = None
        if past_hits:
            out["past_matches"] = len(past_hits)
            out["hint"] = (
                f"The top semantic matches are all past auctions ({len(past_hits)} "
                "found) — the future-only default excluded them. For a "
                "retrospective question retry once with include_past=true; "
                "otherwise report no upcoming matches. Do not rephrase and retry."
            )
        elif past_hits is not None:
            out["hint"] = (
                "No semantic matches in any time window — do not retry with "
                "rephrased wording. Switch to search_auctions filters or report "
                "no matches."
            )
    return out


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
               bank.name AS bank, bank.short_name AS bank_short,
               ac.name AS asset_category,
               property_types,
               previous_reserve_price,
               substring(p.description, 0, 300) AS description_excerpt,
               score, hit_sources
        ORDER BY score DESC
        LIMIT $limit
    """


def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction: every stored node property plus related
    entities. Uses properties(a) so new schema fields auto-surface with no
    tool change; raw `*_embedding` vectors are stripped before return (they're
    huge and unreadable to the model)."""
    cypher = """
        MATCH (a:AuctionProperty {auction_id: $auction_id})
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(city:City)
        OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(area:Area)
        OPTIONAL MATCH (a)-[:LOCATED_IN_STATE]->(state:State)
        OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bank:Bank)
        OPTIONAL MATCH (a)-[:HAS_BORROWER]->(borrower:Borrower)
        OPTIONAL MATCH (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
        OPTIONAL MATCH (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
        OPTIONAL MATCH (a)-[:LISTED_BY_BRANCH]->(branch:Branch)
        OPTIONAL MATCH (a)-[:IS_AUCTION_TYPE]->(atype:AuctionType)
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
            WHERE doc.public_url IS NOT NULL
        OPTIONAL MATCH (a)-[link:SAME_PROPERTY_AS]->(sibling:AuctionProperty)
        WITH a, city, area, state, bank, borrower, ac, branch, atype,
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
                 branch:         CASE WHEN branch   IS NULL THEN NULL ELSE properties(branch)   END,
                 auction_type:   CASE WHEN atype    IS NULL THEN NULL ELSE properties(atype)    END,
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

    # `properties(a)` grabs EVERY node property, which includes the raw
    # `description_embedding` vector the embed pipeline writes back onto the
    # node (3072 gemini floats ≈ 40k chars / ~10k tokens). That array is
    # meaningless to the model and dwarfs the useful fields, so drop any
    # `*_embedding` key before the record ever reaches the agent. Matching by
    # suffix (not a hardcoded name) also fences off markdown/image or any
    # future vector field that might land on the node later.
    for key in [k for k in fields if k.endswith("_embedding")]:
        del fields[key]

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

# Distinct-able AuctionProperty *properties* (no edge to walk — grouped
# straight off the node). Kept separate from _DISTINCT_FIELDS, whose
# (label, rel) tuples drive the edge-walk Cypher.
_DISTINCT_NODE_PROPS: dict[str, str] = {
    "service_provider": "a.service_provider",
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
    "For scoped breakdowns, prefer search_auctions(group_by=...) — filters "
    "compose with the grouping — before writing a run_cypher; the tool "
    "already composes the correct Cypher shape.",
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


def _compute_schema_dynamic() -> dict:
    """Run the live graph-introspection queries (~25 of them) that yield the
    data-derived half of the schema: node labels + counts, relationship types
    + counts, enum values, and numeric/date ranges.

    This is the expensive part describe_schema caches durably on the
    :SchemaCache node. The static `cypher_patterns` are deliberately NOT
    included here — they live in code and are re-attached on every read, so
    tuning them is never shadowed by a stale cache.
    """
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

    return {
        "node_labels": label_info,
        "relationships": rel_info,
        "enums": enums,
        "numeric_ranges": numeric_ranges,
        "date_ranges": date_ranges,
        "date_capabilities": date_capabilities,
    }


# ── Durable schema cache ─────────────────────────────────────────────────────
# The dynamic (data-derived) half of the schema only changes when the pipeline
# ingests new auctions, so we persist it on a singleton :SchemaCache node and
# read it back in ONE query instead of re-running the ~25 introspection queries
# on every cold start (Render free spins down often, so cold starts are common).
# The pipeline refreshes it after each ingestion (run_pipeline Stage 6); the API
# seeds it lazily if it is ever missing.
_SCHEMA_CACHE_NODE_ID = "default"


def _cypher_patterns() -> dict:
    """Static run_cypher guidance, always sourced from code (never the durable
    cache) so edits to the rules/examples take effect immediately."""
    return {"rules": _CYPHER_PATTERN_RULES, "examples": _CYPHER_PATTERN_EXAMPLES}


def _read_schema_cache_node() -> dict | None:
    """Read the precomputed dynamic-schema blob off the singleton :SchemaCache
    node. Returns None when the node is absent or unreadable, so callers fall
    back to a live compute."""
    try:
        rows = run_read_query(
            "MATCH (s:SchemaCache {id: $id}) RETURN s.json AS json",
            {"id": _SCHEMA_CACHE_NODE_ID},
            max_rows=1,
        )
    except Exception:
        return None
    if not rows:
        return None
    raw = rows[0].get("json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_schema_cache_node(dynamic: dict) -> None:
    """Persist the dynamic-schema blob onto the singleton :SchemaCache node.
    Best-effort: describe_schema is a read tool, so a write failure must never
    break a chat turn — the in-process cache still serves it, so we swallow
    errors here and let the next refresh retry."""
    try:
        run_query(
            "MERGE (s:SchemaCache {id: $id}) "
            "SET s.json = $json, s.computed_at = datetime()",
            {"id": _SCHEMA_CACHE_NODE_ID, "json": json.dumps(dynamic)},
        )
    except Exception:
        pass


def describe_schema(refresh: bool = False) -> dict:
    """Return a compact description of the graph schema and cardinalities.

    The data-derived half is cached durably on the :SchemaCache node (written
    by the pipeline after each ingestion) and in-process for
    `_SCHEMA_TTL_SECONDS`. A normal call reads the node in one query instead of
    re-running ~25 introspection queries; `refresh=True` recomputes live and
    rewrites the node. The static `cypher_patterns` are always re-attached from
    code. Fields returned:

    - node_labels: [{label, count, sample_properties}, ...]
    - relationships: [{type, from, to, count}, ...]
    - enums: {asset_category, property_type, ...}
    - numeric_ranges: {reserve_price_num: {...}, emd_num: {...}}
    - date_ranges:    {auction_start_dt: {...}, application_deadline_dt: {...}}
    - cypher_patterns: {rules: [...], examples: [...]}
    """
    now = time.time()
    cached = _SCHEMA_CACHE.get("default")
    if cached and not refresh and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
        return cached[1]

    if refresh:
        dynamic = _compute_schema_dynamic()
        _write_schema_cache_node(dynamic)
    else:
        dynamic = _read_schema_cache_node()
        if dynamic is None:
            # Nothing precomputed yet (first deploy before the next ingestion,
            # or the node was cleared) — compute live and seed the node so the
            # next cold start reads it instead of recomputing.
            dynamic = _compute_schema_dynamic()
            _write_schema_cache_node(dynamic)

    out = {**dynamic, "cypher_patterns": _cypher_patterns()}
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
