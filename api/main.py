"""
api/main.py
-----------
FastAPI entry point + static UI serving.
Run with: uvicorn api.main:app --reload
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _parse_to_utc(s: str) -> datetime:
    """Parse an ISO-8601 query-string date and force tz-aware UTC.
    Stored AuctionProperty dates are ZONED DATETIME — comparing against a
    naive Python datetime yields zero matches in Cypher."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)
from slowapi.errors import RateLimitExceeded

from api.agent import ChatDeps, agent
from api.auth import get_current_admin, get_optional_user, router as auth_router
from api.auth.rate_limit import limiter
from api.auth.schemas import UserOut
from api.conversations import router as conversations_router
from api.neo4j_client import run_query
from api.review import router as review_router
from api.tools.cypher_tools import get_auction_detail
from api.watchlist import router as watchlist_router

_SEARCH_TOOLS = {"search_auctions", "semantic_property_search"}

# Args that describe scope we want to carry across turns. Excludes output
# controls (limit, order_by) and aggregate knobs — those don't narrow the
# user's target set, they just shape the current call.
_CARRY_FORWARD_FILTER_KEYS = {
    "min_price", "max_price",
    "city", "area",
    "property_type", "asset_category",
    "bank",
    "starts_after", "starts_before",
}

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="Bank Auction Intelligence API", version="0.1.0")


def _cors_allow_list() -> list[str]:
    base = os.environ.get("APP_BASE_URL", "").strip()
    env = os.environ.get("APP_ENV", "prod").lower()
    if env in {"dev", "test"}:
        return ["*"]
    origins = {"http://localhost:5173", "http://localhost:3000"}
    if base:
        origins.add(base.rstrip("/"))
    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_list(),
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# Rate limiter (slowapi) — shared across auth + anonymous chat throttles.
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Routed through the FastAPI exception handler chain (inside CORSMiddleware)
    # so the response carries Access-Control-Allow-Origin. Without this, browsers
    # see Starlette's bare 500 from ServerErrorMiddleware, strip it for missing
    # CORS, and report "Failed to fetch" instead of the real status.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


if os.environ.get("AUTH_ENABLED", "true").lower() != "false":
    app.include_router(auth_router)
    app.include_router(watchlist_router)
    app.include_router(conversations_router)
    app.include_router(review_router)


_GATED_MODES = {"deep-research", "report"}

# Simple in-memory hourly counter for anonymous /chat. slowapi's decorator can't
# see Depends-provided state, so we enforce this manually only when user=None.
_ANON_CHAT_MAX_PER_HOUR = 10
_anon_chat_hits: dict[str, list[float]] = {}


def _enforce_anon_chat_limit(request: Request) -> None:
    if os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    import time
    now = time.time()
    window = 3600.0
    ip = request.client.host if request.client else "unknown"
    hits = [t for t in _anon_chat_hits.get(ip, []) if now - t < window]
    if len(hits) >= _ANON_CHAT_MAX_PER_HOUR:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    hits.append(now)
    _anon_chat_hits[ip] = hits


class ChatRequest(BaseModel):
    message: str
    message_history: list[dict[str, Any]] | None = None
    mode: str | None = None
    # Filters the client wants the agent to scope to on the next turn — e.g.
    # the browse panel's current selection. History-extracted filters layer
    # on top so the rolling-scope behavior across turns still works.
    active_filters: dict[str, Any] | None = None


# Whitelist of modes the agent will overlay. Each maps to a modes/<id>.md file.
# Keeping this explicit prevents arbitrary file reads from the modes/ dir.
_AVAILABLE_MODES: list[dict[str, Any]] = [
    {
        "id": "ask",
        "label": "Ask",
        "description": "Free-form Q&A over the Neo4j graph (default).",
        "examples": [
            "Residential auctions in Chennai under 30 lakhs",
            "What is the price range in Kanchipuram?",
            "Show auctions with deadline in the next 7 days",
            "How many banks have auctions in Chennai?",
            "Which borrowers have more than 3 properties?",
            "List all cities in the database",
        ],
    },
    {
        "id": "deep-research",
        "label": "Deep research",
        "description": "7-step due-diligence workflow on one auction_id.",
        "examples": [
            "Deep research on auction AUC-12345",
        ],
    },
    {
        "id": "compare",
        "label": "Compare",
        "description": "Side-by-side comparison of 2–5 auctions.",
        "examples": [
            "Compare AUC-12345 and AUC-67890",
        ],
    },
    {
        "id": "report",
        "label": "Personalized report",
        "description": "Investment report tuned to an investor profile.",
        "examples": [
            "Report on AUC-12345 for a conservative investor under 50 lakhs",
        ],
    },
]


class ToolArtifact(BaseModel):
    tool: str
    args: dict[str, Any] | str | None = None
    result: Any = None
    # UI-only row overflow from search tools. Populated when a search's
    # `total_count` exceeds the model-visible `limit` — lets the right-side
    # artifacts panel render every match without inflating the LLM's
    # context. Never included in `result` (which is what the model sees on
    # follow-up turns).
    ui_rows: list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    answer: str
    artifacts: list[ToolArtifact] = []
    message_history: list[dict[str, Any]] = []


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


@app.get("/properties")
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep() -> dict:
    """Extended health check: verifies Neo4j connectivity, counts the main
    node label, and confirms the vector index exists. Used by monitoring
    and during PR reviews to catch environment drift."""
    checks: dict[str, Any] = {"status": "ok", "errors": []}
    try:
        rows = run_query("MATCH (a:AuctionProperty) RETURN count(a) AS n")
        checks["auction_count"] = rows[0]["n"] if rows else 0
    except Exception as e:
        checks["errors"].append(f"neo4j: {e!r}")
    try:
        idx = run_query(
            "SHOW INDEXES YIELD name, type WHERE name = 'property_desc_idx' "
            "RETURN name, type"
        )
        checks["vector_index"] = idx[0] if idx else None
    except Exception as e:
        checks["errors"].append(f"vector_index: {e!r}")
    if checks["errors"]:
        checks["status"] = "degraded"
    return checks


@app.get("/modes")
def list_modes() -> dict:
    """Mode registry consumed by the web UI to render the mode selector and
    suggestion chips. Mirrors the career-ops pattern of surfacing each
    markdown mode file as a user-facing entry point."""
    return {"modes": _AVAILABLE_MODES}


def _extract_active_filters(messages) -> tuple[dict, int | None]:
    """Walk the message history and merge every prior search tool's scope
    filters into a single rolling dict — the "active scope" of the
    conversation so far.

    Rules:
      * Later calls overwrite earlier ones for the same key, so the latest
        user narrowing wins.
      * Keys not in `_CARRY_FORWARD_FILTER_KEYS` (e.g. `limit`, `order_by`,
        `aggregations`) are ignored — they're per-call, not scope.
      * A call that passes an explicit `None` for a key clears that key —
        the model is effectively telling us it dropped that scope.
      * Non-search tool calls don't affect the scope.

    Returns (filters, last_total_count). `last_total_count` is the
    total_count of the most recent search for context.
    """
    calls: dict[str, ToolCallPart] = {}
    filters: dict = {}
    last_total: int | None = None
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart) and part.tool_name in _SEARCH_TOOLS:
                call = calls.get(part.tool_call_id)
                if call is None:
                    continue
                try:
                    args = call.args_as_dict()
                except Exception:
                    args = {}
                for key in _CARRY_FORWARD_FILTER_KEYS:
                    if key not in args:
                        continue
                    val = args[key]
                    if val is None:
                        filters.pop(key, None)
                    else:
                        filters[key] = val
                result = part.content
                if isinstance(result, dict) and "total_count" in result:
                    last_total = result["total_count"]
                elif isinstance(result, list):
                    last_total = len(result)
    return filters, last_total


def _split_ui_rows(result: Any) -> tuple[Any, list[dict[str, Any]] | None]:
    """Pop the UI-only overflow from a search-tool result.

    `search_auctions` returns `_ui_results` when total_count exceeds the
    model-visible `limit`. That list belongs in the HTTP response so the
    right-side panel can render every match — but it must NOT flow back
    into the LLM's message history on the next turn, or we defeat the
    whole point of the split. We copy the dict (so we don't mutate the
    in-memory ToolReturnPart content), pop `_ui_results`, and return the
    trimmed copy for the LLM + the raw list for the UI.
    """
    if not isinstance(result, dict) or "_ui_results" not in result:
        return result, None
    trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
    ui_rows = result.get("_ui_results")
    if not isinstance(ui_rows, list):
        ui_rows = None
    return trimmed, ui_rows


def _extract_artifacts(messages) -> list[ToolArtifact]:
    """Pair ToolCallPart with its matching ToolReturnPart by tool_call_id.

    When a search tool returns `_ui_results`, move those rows onto the
    artifact's `ui_rows` field and strip them from `result`. This keeps
    the UI payload rich while the LLM-facing `result` stays lean.
    """
    calls: dict[str, ToolCallPart] = {}
    artifacts: list[ToolArtifact] = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart):
                call = calls.get(part.tool_call_id)
                if call is not None:
                    try:
                        args = call.args_as_dict()
                    except Exception:
                        args = call.args if isinstance(call.args, (dict, str)) else str(call.args)
                    result, ui_rows = _split_ui_rows(part.content)
                    artifacts.append(ToolArtifact(
                        tool=part.tool_name,
                        args=args,
                        result=result,
                        ui_rows=ui_rows,
                    ))
    return artifacts


def _strip_ui_rows_from_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove `_ui_results` from any ToolReturnPart content in a dumped
    message history. Prevents the UI overflow from ballooning the LLM's
    context on the next turn when the client echoes history back.
    """
    for msg in history:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and "_ui_results" in content:
                content.pop("_ui_results", None)
    return history


# ── old tool-result trimming ───────────────────────────────────────────────
# Once the model has read a heavy tool result and written its prose answer,
# the raw rows rarely need to re-enter context on later turns — the agent can
# always re-query the graph. We keep the most recent turns fully detailed and
# squeeze older, heavy tool returns down to a breadcrumb stub, shrinking the
# history the client echoes back into the LLM on the next /chat turn. This is
# the same philosophy as `_strip_ui_rows_from_history`, applied to the rows the
# model *did* see (but only on stale turns).
_HISTORY_KEEP_FULL_TURNS = max(1, int(os.getenv("CHAT_HISTORY_KEEP_FULL_TURNS", "2")))
# Only trim tool returns whose JSON is at least this many chars — leaves small
# aggregate/stat results (list_distinct, location_analysis) untouched so the
# model keeps cheap-but-useful context, and avoids stubs larger than the
# original payload.
_HISTORY_TRIM_MIN_CHARS = int(os.getenv("CHAT_HISTORY_TRIM_MIN_CHARS", "600"))
# How many auction_ids to keep as a breadcrumb so the model can still reference
# (or re-query) specific properties from a trimmed turn.
_HISTORY_TRIM_ID_SAMPLE = int(os.getenv("CHAT_HISTORY_TRIM_ID_SAMPLE", "20"))
_HISTORY_TRIM_NOTE = "older result trimmed to save context — re-call the tool for full rows"


def _summarize_tool_return_content(content: Any) -> Any:
    """Collapse a heavy tool-return `content` to a compact breadcrumb stub.

    Keeps the cheap facts the model reasons over (row count, total_count, a
    sample of auction_ids) and drops the bulky per-row payload. Content that
    is already a stub or isn't row-shaped is returned unchanged.
    """
    if isinstance(content, dict):
        if content.get("_trimmed"):
            return content  # already trimmed — idempotent
        stub: dict[str, Any] = {"_trimmed": True}
        if "total_count" in content:
            stub["total_count"] = content["total_count"]
        rows = content.get("results")
        if isinstance(rows, list):
            stub["returned"] = len(rows)
            ids = [r["auction_id"] for r in rows
                   if isinstance(r, dict) and r.get("auction_id")]
            if ids:
                stub["auction_ids"] = ids[:_HISTORY_TRIM_ID_SAMPLE]
        elif content.get("auction_id"):
            # Single-record result (e.g. get_auction_detail).
            stub["auction_id"] = content["auction_id"]
        stub["_note"] = _HISTORY_TRIM_NOTE
        return stub
    if isinstance(content, list):
        ids = [r["auction_id"] for r in content
               if isinstance(r, dict) and r.get("auction_id")]
        stub = {"_trimmed": True, "returned": len(content), "_note": _HISTORY_TRIM_NOTE}
        if ids:
            stub["auction_ids"] = ids[:_HISTORY_TRIM_ID_SAMPLE]
        return stub
    return content


def _trim_old_tool_results(
    history: list[dict[str, Any]],
    keep_full_turns: int = _HISTORY_KEEP_FULL_TURNS,
) -> list[dict[str, Any]]:
    """Squeeze heavy tool-return payloads on all but the most recent turns.

    A "turn" starts at each message carrying a `user-prompt` part. The last
    `keep_full_turns` turns are left fully intact (the agent may still be
    working with their results); older turns get their large tool returns
    replaced with breadcrumb stubs. Mutates and returns `history`.
    """
    if not history or keep_full_turns < 1:
        return history
    turn_starts = [
        i for i, msg in enumerate(history)
        if any(p.get("part_kind") == "user-prompt" for p in msg.get("parts", []))
    ]
    if len(turn_starts) <= keep_full_turns:
        return history  # nothing older than the keep window
    cutoff = turn_starts[-keep_full_turns]
    trimmed_count = 0
    saved_chars = 0
    for msg in history[:cutoff]:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and content.get("_trimmed"):
                continue
            content_json = json.dumps(content, default=str)
            if len(content_json) < _HISTORY_TRIM_MIN_CHARS:
                continue
            part["content"] = _summarize_tool_return_content(content)
            saved_chars += max(0, len(content_json) - len(json.dumps(part["content"], default=str)))
            trimmed_count += 1
    if trimmed_count:
        logger.info(
            "chat history: trimmed %d old tool result(s) (~%d chars, ≈%d tokens) "
            "beyond the last %d turn(s)",
            trimmed_count, saved_chars, saved_chars // 4, keep_full_turns,
        )
    return history


def _strip_dynamic_system_prompts_from_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop any SystemPromptPart with a `dynamic_ref` from incoming history.

    The agent used to register `inject_prior_search` and `inject_mode_overlay`
    as `@agent.system_prompt(dynamic=True)`, which persists their output as
    SystemPromptParts carrying a `dynamic_ref` qualname. After we migrated
    those functions to `@agent.instructions` (cleaner Gemini cache prefix —
    instructions are skipped when empty, and not persisted in history),
    the refs in older stored histories no longer resolve to runners, so
    pydantic-ai would leave them frozen with stale "Active scope" text.
    Stripping them keeps history clean while letting the agent re-add the
    fresh content as an instruction message on the new turn.
    """
    for msg in history:
        parts = msg.get("parts", [])
        msg["parts"] = [
            p for p in parts
            if not (
                p.get("part_kind") == "system-prompt"
                and p.get("dynamic_ref")
            )
        ]
    return history


class FeedbackRequest(BaseModel):
    kind: Literal["message", "general"] = "message"
    rating: Literal["up", "down"] | None = None
    text: str | None = None
    session_id: str
    message_index: int = -1
    question: str = ""
    answer: str = ""
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None
    page_url: str | None = None
    property_id: str | None = None


class FeedbackRecord(BaseModel):
    id: str
    kind: Literal["message", "general"] = "message"
    rating: Literal["up", "down"] | None = None
    text: str | None = None
    session_id: str
    message_index: int
    question: str
    answer: str
    artifacts: list[dict[str, Any]] | None = None
    context_turns: list[dict[str, Any]] | None = None
    user_agent: str | None = None
    page_url: str | None = None
    property_id: str | None = None
    created_at: str
    resolved: bool = False
    resolved_at: str | None = None


def _strip_artifacts(arts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not arts:
        return []
    return [{"tool": a.get("tool"), "args": a.get("args")} for a in arts]


def _strip_context_turns(turns: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep role, content, and tool_calls (tool+args only). Drop everything else."""
    if not turns:
        return []
    out: list[dict[str, Any]] = []
    for t in turns:
        role = t.get("role")
        if role not in ("user", "assistant"):
            continue
        entry: dict[str, Any] = {"role": role, "content": (t.get("content") or "")[:2000]}
        if role == "assistant":
            entry["tool_calls"] = _strip_artifacts(t.get("tool_calls"))
        out.append(entry)
    return out


def _feedback_row_to_record(row: dict) -> FeedbackRecord:
    f = row["f"] if "f" in row else row
    try:
        artifacts = json.loads(f.get("artifacts_json") or "[]")
    except json.JSONDecodeError:
        artifacts = []
    try:
        context_turns = json.loads(f.get("context_turns_json") or "[]")
    except json.JSONDecodeError:
        context_turns = []
    created_at = f.get("created_at")
    # neo4j DateTime → ISO string
    created_at_str = created_at.iso_format() if hasattr(created_at, "iso_format") else str(created_at)
    resolved_at = f.get("resolved_at")
    resolved_at_str: str | None
    if resolved_at is None:
        resolved_at_str = None
    else:
        resolved_at_str = resolved_at.iso_format() if hasattr(resolved_at, "iso_format") else str(resolved_at)
    return FeedbackRecord(
        id=f["id"],
        kind=f.get("kind") or "message",
        rating=f.get("rating"),
        text=f.get("text"),
        session_id=f["session_id"],
        message_index=f["message_index"],
        question=f.get("question") or "",
        answer=f.get("answer") or "",
        artifacts=artifacts,
        context_turns=context_turns,
        user_agent=f.get("user_agent"),
        page_url=f.get("page_url"),
        property_id=f.get("property_id"),
        created_at=created_at_str,
        resolved=bool(f.get("resolved", False)),
        resolved_at=resolved_at_str,
    )


@app.post("/feedback")
async def submit_feedback(
    req: FeedbackRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> dict:
    if req.kind == "general" and not (req.text and req.text.strip()) and req.rating is None:
        raise HTTPException(status_code=400, detail="General feedback requires a rating or text.")
    fid = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts_json = json.dumps(_strip_artifacts(req.artifacts))
    context_turns_json = json.dumps(_strip_context_turns(req.context_turns))
    answer_trimmed = (req.answer or "")[:4000]
    run_query(
        """
        CREATE (f:Feedback {
          id: $id, kind: $kind, rating: $rating, text: $text, session_id: $session_id,
          message_index: $message_index, question: $question, answer: $answer,
          artifacts_json: $artifacts_json, context_turns_json: $context_turns_json,
          user_agent: $user_agent, page_url: $page_url, user_id: $user_id,
          property_id: $property_id,
          created_at: datetime($created_at), resolved: false
        })
        RETURN f.id AS id
        """,
        {
            "id": fid,
            "kind": req.kind,
            "rating": req.rating,
            "text": req.text,
            "session_id": req.session_id,
            "message_index": req.message_index,
            "question": req.question,
            "answer": answer_trimmed,
            "artifacts_json": artifacts_json,
            "context_turns_json": context_turns_json,
            "user_agent": req.user_agent,
            "page_url": req.page_url,
            "user_id": user.id if user else None,
            "property_id": req.property_id,
            "created_at": created_at,
        },
    )
    return {"id": fid, "status": "saved"}


@app.get("/feedback/recent", response_model=list[FeedbackRecord])
async def list_feedback(
    limit: int = 50,
    unresolved_only: bool = True,
    rating: Literal["up", "down"] | None = None,
    kind: Literal["message", "general"] | None = None,
) -> list[FeedbackRecord]:
    rows = run_query(
        """
        MATCH (f:Feedback)
        WHERE ($unresolved = false OR f.resolved = false)
          AND ($rating IS NULL OR f.rating = $rating)
          AND ($kind IS NULL OR coalesce(f.kind, 'message') = $kind)
        RETURN f { .* } AS f
        ORDER BY f.created_at DESC
        LIMIT $limit
        """,
        {"unresolved": unresolved_only, "rating": rating, "kind": kind, "limit": limit},
    )
    return [_feedback_row_to_record(r) for r in rows]


@app.patch("/feedback/{feedback_id}/resolve")
async def resolve_feedback(
    feedback_id: str,
    x_resolve_token: str | None = Header(default=None),
    user: UserOut | None = Depends(get_optional_user),
) -> dict:
    """Mark a feedback item as resolved.

    Accepts either a shared `X-Resolve-Token` (used by the GitHub
    `resolve-feedback` workflow) or a Supabase JWT from an admin user. When
    an admin closes the item we also persist `resolved_by` / `resolved_by_email`
    for audit.
    """
    expected = os.environ.get("FEEDBACK_RESOLVE_TOKEN")
    token_ok = bool(expected) and x_resolve_token == expected
    admin_ok = user is not None and user.role == "admin"
    if not (token_ok or admin_ok):
        raise HTTPException(status_code=401, detail="Invalid resolve credentials")

    params: dict[str, Any] = {"id": feedback_id}
    set_clause = "SET f.resolved = true, f.resolved_at = datetime()"
    if admin_ok and user is not None:
        set_clause += ", f.resolved_by = $resolved_by, f.resolved_by_email = $resolved_by_email"
        params["resolved_by"] = user.id
        params["resolved_by_email"] = user.email

    rows = run_query(
        f"""
        MATCH (f:Feedback {{id: $id}})
        {set_clause}
        RETURN f.id AS id
        """,
        params,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"id": feedback_id, "resolved": True}


@app.get("/admin/feedback", response_model=list[FeedbackRecord])
async def list_admin_feedback(
    limit: int = 100,
    unresolved_only: bool = True,
    _admin: UserOut = Depends(get_current_admin),
) -> list[FeedbackRecord]:
    rows = run_query(
        """
        MATCH (f:Feedback)
        WHERE ($unresolved = false OR f.resolved = false)
        RETURN f { .* } AS f
        ORDER BY f.created_at DESC
        LIMIT $limit
        """,
        {"unresolved": unresolved_only, "limit": limit},
    )
    return [_feedback_row_to_record(r) for r in rows]


@app.get("/auction/{auction_id}")
def auction_detail(auction_id: str) -> dict:
    detail = get_auction_detail(auction_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Auction not found")
    return detail


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    req: ChatRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> ChatResponse:
    mode = req.mode
    if mode in _GATED_MODES and (user is None or not user.email_verified):
        raise HTTPException(status_code=401, detail="login required for this mode")
    if user is None:
        _enforce_anon_chat_limit(request)
    history = (
        ModelMessagesTypeAdapter.validate_python(
            _strip_dynamic_system_prompts_from_history(req.message_history)
        )
        if req.message_history
        else None
    )
    if mode:
        valid_ids = {m["id"] for m in _AVAILABLE_MODES}
        if mode not in valid_ids or mode == "ask":
            # Unknown mode or the default "ask" sentinel — don't overlay anything.
            mode = None
    if history:
        history_filters, last_total = _extract_active_filters(history)
    else:
        history_filters, last_total = {}, None
    # Client-supplied filters (e.g. from the browse panel "chat about these"
    # button) seed the scope; whatever the agent has narrowed across prior
    # turns layers on top so explicit refinements still win.
    client_filters = req.active_filters or {}
    active_filters = {**client_filters, **history_filters}
    deps = ChatDeps(
        active_filters=active_filters or None,
        last_total_count=last_total,
        mode=mode,
    )
    try:
        result = await agent.run(req.message, message_history=history, deps=deps)
    except Exception:
        # Most often pydantic-ai's UnexpectedModelBehavior or a transient
        # OpenRouter error. Log with the user message so the failing input is
        # recoverable from Render logs, then surface a friendly 500.
        logger.exception("agent.run failed for message=%r mode=%r", req.message, mode)
        raise HTTPException(status_code=500, detail="chat agent failed — please retry")
    dumped_history = ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
    # Strip `_ui_results` from the history echoed back to the client —
    # otherwise the client ships it back on the next /chat turn and it
    # re-enters the LLM's context, defeating the UI/LLM split.
    dumped_history = _strip_ui_rows_from_history(dumped_history)
    # Squeeze heavy tool results on stale turns down to breadcrumb stubs so a
    # long conversation's history stops re-billing the model for rows it has
    # already summarized. Recent turns stay fully detailed; the agent can
    # re-query for anything it trimmed. Artifacts (above) are extracted from
    # the live `new_messages()` objects, so the UI payload is unaffected.
    dumped_history = _trim_old_tool_results(dumped_history)
    return ChatResponse(
        answer=result.output,
        artifacts=_extract_artifacts(result.new_messages()),
        message_history=dumped_history,
    )


# Serve the single-page UI
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def root() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

    @app.get("/auth.js")
    def auth_js() -> FileResponse:
        return FileResponse(str(WEB_DIR / "auth.js"), media_type="application/javascript")

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "admin.html"))

    @app.get("/review")
    def review_page() -> FileResponse:
        return FileResponse(str(WEB_DIR / "review.html"))
