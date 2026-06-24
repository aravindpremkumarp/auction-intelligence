"""
api/mcp_server.py
-----------------
Tier-1 MCP (Model Context Protocol) connector: re-exposes AuctionScope's
PUBLIC, READ-ONLY auction tools over MCP's Streamable-HTTP transport so that
Claude.ai / Claude Desktop, ChatGPT, and the Claude + OpenAI APIs can use the
platform as a connector — with no per-user authentication.

The tool implementations are NOT duplicated here. Each wrapper is a thin
adapter over the same `api.tools.cypher_tools` (and `scoring.auction_scorer`)
functions that `api/agent.py` wires into the in-app PydanticAI agent, so the
graph logic lives in exactly one place. The wrapper docstrings double as the
MCP tool descriptions the model reads.

Design notes
============
* Ships dark. `api/main.py` builds + mounts this only when MCP_ENABLED is set,
  so the default build (and prod, until the flag is flipped) behaves exactly as
  before. Importing this module is cheap — it does NOT import the `mcp` package.
* The pure wrapper functions below do not import `mcp`, so their logic is
  unit-testable without the optional dependency installed. `build_mcp()` does
  the FastMCP import lazily and registers them.
* PUBLIC + READ-ONLY only. The per-user tools (watch_property / list_alerts /
  query_user_dossier) and the Tavily-backed internet_search are deliberately
  excluded — they need an authenticated identity or spend a metered quota. They
  belong to the auth'd tiers (see docs/mcp-connector.md).
* `run_cypher` is read-only (writes rejected; 10s / 500-row caps) but is a broad
  escape hatch; withhold it from a fully public connector with
  MCP_EXPOSE_CYPHER=false.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from api.tools import cypher_tools as T
from scoring.auction_scorer import score_auction as _score_auction

logger = logging.getLogger(__name__)


def _site_base() -> str:
    """Public website base for building shareable per-auction URLs in the
    search/fetch results. Defaults to the production site; override per deploy
    with MCP_PUBLIC_SITE_BASE."""
    return os.environ.get("MCP_PUBLIC_SITE_BASE", "https://www.auctionscope.in").rstrip("/")


def _auction_url(auction_id: str) -> str:
    # Mirrors the SPA deep-link route (GET /property/{id}) in api/main.py.
    return f"{_site_base()}/property/{auction_id}"


# ──────────────────────────────────────────────────────────────────────────
# Tool wrappers — thin adapters over cypher_tools / auction_scorer. Kept free
# of any `mcp` import so they stay importable + unit-testable without the
# optional dependency. The function name becomes the MCP tool name; the
# docstring becomes its description.
# ──────────────────────────────────────────────────────────────────────────

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
    """Filter Indian bank-auction properties by price, city, area, property_type,
    asset_category, bank, auction_type, branch, and date window.

    Returns {total_count, returned, limit, results}: `total_count` is the true
    match count (ignores `limit`). Future-only by default; pass
    include_past=True for retrospective questions. `order_by` is one of
    deadline_asc | deadline_desc | price_asc | price_desc. For aggregates set
    `aggregate_field` to "reserve_price_num" or "emd_num" and `aggregations` to
    a subset of [min, max, avg, median, p25, p75]. Each filter takes a single
    value OR a list (OR within a list, AND across filters)."""
    return T.search_auctions(
        min_price=min_price, max_price=max_price,
        city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank, auction_type=auction_type, branch_name=branch_name,
        starts_after=starts_after, starts_before=starts_before,
        limit=limit, order_by=order_by,
        aggregate_field=aggregate_field, aggregations=aggregations,
        include_past=include_past,
    )


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
    """Semantic / qualitative search over property descriptions, notice markdown,
    and notice images (one embedding ranked across three vector indexes in the
    same space). Use for free-text qualities that aren't in structured fields —
    boundaries, neighbourhood, legal caveats, condition, layout. Optional
    city/area/price/category/date filters post-filter the hits. Each row carries
    a `score` and `hit_sources`."""
    try:
        return T.semantic_search(
            query, city=city, area=area,
            min_price=min_price, max_price=max_price,
            asset_category=asset_category,
            starts_after=starts_after, starts_before=starts_before,
            limit=limit, include_past=include_past,
        )
    except RuntimeError as e:
        return {"error": str(e), "results": [], "returned": 0, "limit": limit}


def match_pasted_listing(pasted_text: str) -> dict:
    """Match a pasted property listing (WhatsApp forward, broker note, bank
    circular) to an auction. Anchors on reserve price ±2% AND auction date ±2
    days, widening if nothing strict-matches. Returns {match, confidence (0-1),
    candidates (<=5), widening_reason, extracted}."""
    return T.match_pasted_listing(pasted_text)


def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction_id — every stored field plus related
    city/area/state/bank/borrower/category/property_types and `price_history`
    (the re-auction timeline). Returns None if the auction_id doesn't exist."""
    return T.get_auction_detail(auction_id)


def score_auction(auction_id: str) -> dict | None:
    """Investment score for ONE auction_id: the 10-dimension framework (price,
    location, legal clarity, bank, condition, timeline, due-diligence ease, area
    trend, competition, yield) computed live from the graph. Returns
    {composite_score 0-100, grade A+..F, dimensions:[{name, score, weight,
    rationale}]} or None if the id doesn't exist."""
    result = _score_auction(auction_id)
    return result.to_dict() if result else None


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
    """Distinct values of a reference field with per-value auction counts — for
    distribution / breakdown / "mix" questions. `field` is one of {city, area,
    state, bank, branch, borrower, asset_category, property_type, auction_type}.
    Optional scope filters narrow the counts (scope must differ from `field`)."""
    return T.list_distinct(
        field, limit,
        city=city, bank=bank, borrower=borrower,
        asset_category=asset_category, auction_type=auction_type, branch=branch,
    )


def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    """Auctions whose application deadline falls within the next N days."""
    return T.upcoming_auctions(days, limit)


def borrower_lookup(borrower_name: str) -> list[dict]:
    """Other auction properties tied to a borrower (substring match)."""
    return T.borrower_lookup(borrower_name)


def describe_schema(refresh: bool = False) -> dict:
    """Live graph schema: labels, relationship types, enum values, numeric/date
    ranges, and Cypher pattern hints. Call this BEFORE composing a novel
    run_cypher query. Cached for 1 hour."""
    return T.describe_schema(refresh)


def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
) -> dict:
    """READ-ONLY Cypher escape hatch for novel queries the specialized tools
    can't express. Writes (CREATE/MERGE/DELETE/SET/REMOVE/DROP/LOAD CSV/FOREACH
    and write procedures) are rejected. Caps: 4000-char query, 10s execution,
    `max_rows` (default 200, hard cap 500). Call describe_schema() first if
    unsure about labels or properties; prefer a specialized tool when one fits."""
    return T.run_cypher(cypher, params, description, max_rows)


# ── ChatGPT-connector contract: `search` + `fetch` ─────────────────────────
# ChatGPT's connector / deep-research surface expects a `search` tool returning
# lightweight {id, title, url} hits and a `fetch` tool returning the full
# document for an id. Mapping those onto semantic_search + get_auction_detail
# lets the same MCP server work as a one-click ChatGPT connector, not only in
# Developer Mode. They are additive — the richer domain tools above remain
# available to clients (Claude, Cursor, …) that use arbitrary MCP tools.

def search(query: str, limit: int = 10) -> dict:
    """Search auction properties by natural-language query. Returns
    {results: [{id, title, url}]}; pass an `id` to `fetch` for the full record.
    (ChatGPT-connector contract; wraps semantic_search.)"""
    hits = semantic_search(query, limit=limit)
    results = []
    for row in (hits.get("results") or []):
        aid = row.get("auction_id") or row.get("id")
        if not aid:
            continue
        title = (
            row.get("title")
            or row.get("address")
            or row.get("property_name")
            or row.get("description")
            or f"Auction {aid}"
        )
        results.append({
            "id": str(aid),
            "title": str(title)[:200],
            "url": _auction_url(str(aid)),
        })
    return {"results": results}


def fetch(id: str) -> dict:
    """Fetch the full record for one auction id (as returned by `search`).
    Returns {id, title, text, url, metadata}. (ChatGPT-connector contract;
    wraps get_auction_detail.)"""
    detail = get_auction_detail(id)
    if not detail:
        return {
            "id": id,
            "title": f"Auction {id} (not found)",
            "text": "No auction found for this id.",
            "url": _auction_url(id),
            "metadata": {},
        }
    title = (
        detail.get("title")
        or detail.get("address")
        or detail.get("property_name")
        or f"Auction {id}"
    )
    return {
        "id": id,
        "title": str(title)[:200],
        "text": json.dumps(detail, default=str, ensure_ascii=False, indent=2),
        "url": _auction_url(id),
        "metadata": detail,
    }


# Always-on public, read-only tools. `run_cypher` is registered separately so it
# can be withheld from a fully public connector via MCP_EXPOSE_CYPHER=false.
_CORE_TOOLS = [
    search_auctions,
    semantic_search,
    match_pasted_listing,
    get_auction_detail,
    score_auction,
    list_distinct,
    upcoming_auctions,
    borrower_lookup,
    describe_schema,
    search,
    fetch,
]

_INSTRUCTIONS = (
    "AuctionScope is a read-only knowledge graph of Indian SARFAESI bank-auction "
    "properties (Tamil Nadu). Use `search_auctions` for structured filters "
    "(price/city/type/date) and aggregates; `semantic_search` for qualitative "
    "free-text queries; `match_pasted_listing` to resolve a pasted broker/WhatsApp "
    "blurb to an auction; `get_auction_detail` and `score_auction` for one "
    "auction_id; `list_distinct` for breakdowns; `upcoming_auctions` and "
    "`borrower_lookup` for deadlines and borrower links; `describe_schema` plus "
    "`run_cypher` (read-only) for novel queries. `search` and `fetch` are the "
    "ChatGPT-connector aliases over semantic_search and get_auction_detail. Every "
    "figure is grounded in the graph — never invent auction_ids, prices, or counts."
)


def _expose_cypher() -> bool:
    return os.environ.get("MCP_EXPOSE_CYPHER", "true").lower() in {"1", "true", "yes", "on"}


def tool_names() -> list[str]:
    """Names of the tools this server will register, computed without importing
    the optional `mcp` package — for tests, docs, and introspection."""
    names = [fn.__name__ for fn in _CORE_TOOLS]
    if _expose_cypher():
        names.append(run_cypher.__name__)
    return names


# FastMCP enables DNS-rebinding protection by default and, with an empty
# allow-list, 421s every request whose Host isn't whitelisted. That protection
# guards localhost-bound servers from browser-based attackers; this connector is
# reached server-side by Claude/ChatGPT/OpenAI on a public domain, so we keep
# protection ON but seed the allow-list with the known deployment hosts (mirrors
# CANONICAL_WEB_HOST / the API hosts in api/main.py) plus localhost for dev.
# Front the connector on a different host? Set MCP_ALLOWED_HOSTS (comma-separated;
# "host:*" matches any port). MCP_ALLOWED_ORIGINS does the same for the Origin
# header (only checked when present — browser clients like MCP Inspector).
_DEFAULT_ALLOWED_HOSTS = [
    "auctionscope.in",
    "www.auctionscope.in",
    "api.auctionscope.in",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
]


def _transport_security():
    """Build the FastMCP transport-security settings from env, defaulting to the
    known deployment hosts so the connector works out of the box while staying
    locked down. Lazily imports `mcp`, so only call it from build_mcp()."""
    from mcp.server.transport_security import TransportSecuritySettings

    raw_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    raw_origins = os.environ.get("MCP_ALLOWED_ORIGINS", "").strip()
    hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()] or list(_DEFAULT_ALLOWED_HOSTS)
    origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
    logger.info("MCP transport security: allowed_hosts=%s allowed_origins=%s", hosts, origins or "[]")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_mcp():
    """Build the FastMCP server with the public read-only tools registered.

    The `mcp` import is lazy so the wrapper logic above stays importable without
    the optional dependency. Called by api/main.py only when MCP_ENABLED is set.
    The server is configured stateless (no per-connection session affinity, so
    it survives multiple workers) and serves the Streamable-HTTP endpoint at the
    mount root, so mounting at "/mcp" exposes it at exactly /mcp.
    """
    from mcp.server.fastmcp import FastMCP  # lazy: optional dependency (see requirements.txt)

    mcp = FastMCP(
        "AuctionScope",
        instructions=_INSTRUCTIONS,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(),
    )
    for fn in _CORE_TOOLS:
        mcp.add_tool(fn)
    if _expose_cypher():
        mcp.add_tool(run_cypher)
    return mcp
