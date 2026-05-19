"""
api/agent.py
------------
PydanticAI agent wired to OpenRouter (Gemini 2.0 Flash) with Cypher tools.
Keeps the existing OpenRouter config from pipeline/config.py.

The system prompt is assembled from two parts:
1. A short role statement defined here.
2. `modes/_shared.md` — schema, enum lists, tool-choice rules, Cypher
   cheat-sheet. Keeping the schema in markdown lets us edit it without
   touching Python and makes the prompt reviewable in PRs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from pipeline.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from api.tools import cypher_tools as T
from api.tools import web_tools as W


@dataclass
class ChatDeps:
    # `active_filters` is the rolling scope narrowed across prior turns —
    # every non-aggregate, non-limit arg the user stuck with so far. Injected
    # into the system prompt so the model carries them forward on the next
    # search_auctions call unless the user explicitly changes or drops one.
    active_filters: dict | None = None
    last_total_count: int | None = None
    mode: str | None = None


_ROLE_PROMPT = """\
You are an AI assistant for the Bank Auction Intelligence Platform. You help
users find, analyze, score, and track Indian bank auction properties
(primarily SARFAESI) over a Neo4j knowledge graph of 3,391 Tamil Nadu
properties.

Rules:
1. Ground every answer in tools. Never invent auction_ids, prices, counts,
   enums, or numeric thresholds (`min_price`, `max_price`, `starts_after`,
   `starts_before`). For superlatives ("cheapest N", "soonest N", "top N
   priced") use `order_by` + `limit` on the carried scope — not a made-up
   threshold. Cite by `auction_id`.
2. Tool choice: prefer the specialized tool that matches; fall back to
   `run_cypher` only for novel queries. Call `describe_schema()` before
   composing a novel Cypher. For distribution / breakdown / "spread"
   questions use `list_distinct` — never iterate `get_auction_detail` to
   compute counts or aggregates.
3. Cypher shape: every domain relationship starts on `AuctionProperty`
   (`HAS_ASSET_CATEGORY`, `HAS_PROPERTY_TYPE`, `CONDUCTED_BY`,
   `HAS_BORROWER`, `LOCATED_IN_*`). MATCH each from the AuctionProperty
   node and join with commas; never chain `(Bank)-[:HAS_PROPERTY_TYPE]`
   or similar — those edges don't exist.
4. Zero results → loosen (drop property_type, widen price, recheck
   city/area spelling) before declaring "no matches".
5. `internet_search` is for OFF-GRAPH context only: SARFAESI/legal
   explainers, RBI/bank news, locality background, term definitions.
   Never for properties, prices, deadlines, auction_ids, or counts. For
   hybrid questions, query the graph first. Retry at most once per turn;
   on `{error}` say web search is unavailable. When you use it, cite
   inline as `[1]`, `[2]` matching the order of `sources`; the UI
   renders the chip list — do NOT print a "Sources:" footer.
6. Don't offer follow-ups outside the tool surface. The graph holds
   AuctionProperty, Borrower, Bank, City, Area, Document, AssetCategory
   — and nothing else. No litigations, court cases, FIRs, credit
   history, ownership chains, encumbrance certificates, market
   valuations, or external records. Frame borrower follow-ups as
   `borrower_lookup` output ("other auctions tied to this borrower"),
   never "check legal records". Confirm before any state-changing
   action (scoring, tracker transitions).
7. Markdown only for genuine multi-section answers: open each section
   with `### <emoji> **Title**` (one emoji matching intent — 📍 location,
   🔍 search, 🏆 top, 📊 data, 📰 news, ⚡ insight, ⚠️ caveat, ✅, 💰, 📅).
   Separate sections with a blank line + `---` + blank line. Use **bold**
   for load-bearing facts; short bullets for parallel points; real
   Markdown tables (with `|---|`) for tabular data. Don't wrap a short
   single-section reply in headers.
"""

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODES_DIR = _REPO_ROOT / "modes"


def _load_mode_file(name: str) -> str:
    """Read a modes/<name>.md file if it exists; otherwise return ''."""
    path = _MODES_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


_SHARED_CONTEXT = _load_mode_file("_shared")

SYSTEM_PROMPT = f"{_ROLE_PROMPT}\n\n---\n\n{_SHARED_CONTEXT}" if _SHARED_CONTEXT else _ROLE_PROMPT


_provider = OpenAIProvider(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
_model = OpenAIModel(OPENROUTER_MODEL, provider=_provider)

agent = Agent(_model, deps_type=ChatDeps, system_prompt=SYSTEM_PROMPT)


# Both inject_* are registered as `@agent.instructions` rather than
# `@agent.system_prompt(dynamic=True)`. Two reasons:
#   1. Cache stability — instructions are skipped when they return "" or
#      None (pydantic-ai's _get_instructions filters falsy strings), so
#      no empty system message rides on the wire when there's no active
#      mode. Dynamic system prompts always emit a SystemPromptPart even
#      for empty output, which polluted Gemini's implicit cache prefix.
#   2. Instructions are not persisted in message history — they're added
#      fresh at request-build time on every turn, so old stored turns
#      can't carry a stale "Active search scope" block when the user
#      narrows scope differently later.
# Old stored histories may still contain dynamic SystemPromptParts that
# referenced these functions; the /chat handler strips them before
# forwarding so they don't linger as orphan refs.
@agent.instructions
def inject_prior_search(ctx: RunContext[ChatDeps]) -> str:
    filters = ctx.deps.active_filters if ctx.deps else None
    total = ctx.deps.last_total_count if ctx.deps else None
    if not filters and total is None:
        return ""
    lines = ["Active search scope (carry on every search_auctions call until the user changes it):"]
    if filters:
        for k, v in filters.items():
            lines.append(f"- {k}: {v!r}")
    else:
        lines.append("- (no scope filters yet)")
    if total is not None:
        lines.append(f"- last total_count: {total}")
    return "\n".join(lines)


@agent.instructions
def inject_mode_overlay(ctx: RunContext[ChatDeps]) -> str:
    """If the caller requested a mode (deep-research / compare / report),
    append the mode's markdown spec to the turn instructions."""
    mode = ctx.deps.mode if ctx.deps else None
    if not mode:
        return ""
    text = _load_mode_file(mode)
    if not text:
        return ""
    return f"---\n\n# Active mode: {mode}\n\n{text}"


@agent.tool_plain
def search_auctions(
    min_price: float | None = None, max_price: float | None = None,
    city: str | list[str] | None = None,
    area: str | list[str] | None = None,
    property_type: str | list[str] | None = None,
    asset_category: str | list[str] | None = None,
    bank: str | list[str] | None = None,
    auction_type: str | None = None,
    branch_name: str | None = None,
    starts_after: datetime | None = None, starts_before: datetime | None = None,
    limit: int = 20,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    include_past: bool = False,
) -> dict:
    """Filter auctions by price, city, area, type, asset category, bank,
    auction type, branch, and date window.

    Returns {total_count, returned, limit, results}. `total_count` is the
    true match count (ignoring `limit`); `results` is capped at `limit`.
    Use `total_count` for quantity questions, never `len(results)`.

    Defaults to future-only (`auction_start_dt >= now()`). Pass
    `include_past=True` only for retrospective questions.

    Filters — each accepts a single string OR a list (OR semantics within
    the list, AND across filters):
      - `city`: exact City name ("Chennai", "Kanchipuram").
      - `area`: case-insensitive substring on Area name. Combine with
        `city` so identically-named areas in other cities don't match.
      - `property_type`: exact PropertyType name. Expand domain synonyms
        BEFORE calling — "independent house" → ["House","Villa",
        "Bungalow","Land And Building"]; "plot" → ["Plot","Land",
        "Non-Agricultural Land"]; "shop" → ["Commercial Shop",
        "Commercial Property"].
      - `asset_category`: exact AssetCategory name ("Residential",
        "Commercial", etc.). Use this — NOT `property_type` — when the
        user says "residential" / "commercial" / "industrial".
      - `bank`: exact Bank name. Carry across follow-up turns until the
        user changes scope.
      - `auction_type`: one of "SARFAESI Auction", "DRT Auction",
        "Liquidation Auction", "Private Property".
      - `branch_name`: exact Branch name.

    Ordering — `order_by` is one of "deadline_asc" (default),
    "deadline_desc", "price_asc", "price_desc". For "cheapest N" /
    "soonest N" / "most expensive N", use ordering + `limit=N`; do NOT
    invent `min_price` / `max_price` / date thresholds the user didn't
    state.

    Aggregations — for "price range" / "median" / "average" / "spread":
    set `aggregate_field` to "reserve_price_num" or "emd_num" and
    `aggregations` to any subset of ["min","max","avg","median","p25",
    "p75"]. Pass `limit=0` to skip row fetch when only stats are needed.
    Results are added as an `aggregations` key.
    """
    return T.search_auctions(
        min_price=min_price, max_price=max_price,
        city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank,
        auction_type=auction_type, branch_name=branch_name,
        starts_after=starts_after, starts_before=starts_before,
        limit=limit, order_by=order_by,
        aggregate_field=aggregate_field, aggregations=aggregations,
        include_past=include_past,
    )


@agent.tool_plain
def find_similar_properties(auction_id: str, price_tolerance_pct: float = 25.0, limit: int = 10) -> list[dict]:
    """Comparable properties in the same area with similar price."""
    return T.find_similar_properties(auction_id, price_tolerance_pct, limit)


@agent.tool_plain
def bank_portfolio(bank_name: str) -> list[dict]:
    """Aggregate stats for all auctions by a bank."""
    return T.bank_portfolio(bank_name)


@agent.tool_plain
def location_analysis(location: str, location_type: str = "city") -> list[dict]:
    """Price distribution + density for a city, area, or state."""
    return T.location_analysis(location, location_type)


@agent.tool_plain
def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    """Auctions with application deadline within N days."""
    return T.upcoming_auctions(days, limit)


@agent.tool_plain
def price_comparison(city: str, property_type: str) -> list[dict]:
    """Reserve prices for one property type in one city, sorted asc."""
    return T.price_comparison(city, property_type)


@agent.tool_plain
def borrower_lookup(borrower_name: str) -> list[dict]:
    """Find properties tied to a borrower (substring match)."""
    return T.borrower_lookup(borrower_name)


@agent.tool_plain
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
    """Semantic search over descriptions, notice markdown, and notice
    images (single gemini-embedding-2 call ranked across three indexes
    in the same vector space). Use for qualitative text — boundaries,
    neighborhood, legal caveats, condition, layout — anything in free
    text or the notice but absent from structured fields.

    For a PASTED listing (WhatsApp forward / broker note with a price,
    date, or area) use `match_pasted_listing` instead.

    Optional `city` / `area` / `min_price` / `max_price` /
    `asset_category` / date window post-filter the hits. Future-only by
    default; `include_past=True` for retrospective queries.

    Each row carries `score` and `hit_sources` (subset of
    'desc'/'markdown'/'image'). On embedding-backend failure returns
    `{"error": ..., "results": []}` — fall back to `search_auctions`.
    """
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


@agent.tool_plain
def match_pasted_listing(pasted_text: str) -> dict:
    """Match a pasted property listing (WhatsApp forward, broker note,
    bank circular) to an auction. Use whenever the user pastes a blurb
    with a price / EMD / auction date / building / plot / area / PIN —
    even if messy. Always preferred over `semantic_search` for this.

    Anchors on reserve price ±2% AND auction date ±2 days, widens if
    nothing strict-matches. Returns {match, confidence (0-1),
    candidates (≤5), widening_reason, extracted}.

    Present results by confidence:
      - confidence ≥ 0.85 → "Found it: <match>".
      - 0.6 ≤ conf < 0.85 → "Very likely this property" + ask to confirm.
      - conf < 0.6 (widened) → "Couldn't find this exact property; closest
        matches (<widening_reason>):" — list candidates, quote
        `widening_reason` verbatim.
      - match None & candidates empty → say so; ask for auction_id or
        clearer price/date."""
    return T.match_pasted_listing(pasted_text)


@agent.tool_plain
def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction_id — every stored property plus
    related city/area/state/bank/borrower/category/property_types and
    `price_history` (re-auction timeline). Call this before concluding
    a field is unavailable for a specific auction. Returns None if the
    auction_id doesn't exist."""
    return T.get_auction_detail(auction_id)


@agent.tool_plain
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
    """Distinct values of a reference field with per-value auction counts —
    use for distribution / breakdown / "spread" / "mix" questions.

    `field` ∈ {"city","area","state","bank","branch","borrower",
    "asset_category","property_type","auction_type"}.

    Optional scope filters narrow the count: `city`, `bank`, `borrower`,
    `asset_category`, `auction_type`, `branch` (each single str or list,
    any-match). Scope must differ from `field`. Example: property-type
    mix for SBI → field="property_type", bank="State Bank of India"."""
    return T.list_distinct(
        field,
        limit,
        city=city,
        bank=bank,
        borrower=borrower,
        asset_category=asset_category,
        auction_type=auction_type,
        branch=branch,
    )


@agent.tool_plain
def describe_schema(refresh: bool = False) -> dict:
    """Graph schema: labels, relationship types, enum values, numeric/
    date ranges, and `cypher_patterns` (must-know MATCH-shape rules,
    DATETIME handling, ready-to-adapt examples). Cached for 1 hour.
    Call this BEFORE composing a novel `run_cypher` query."""
    return T.describe_schema(refresh)


@agent.tool_plain
def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
) -> dict:
    """READ-ONLY Cypher escape hatch for novel queries the specialized
    tools can't express. Writes (CREATE/MERGE/DELETE/SET/REMOVE/DROP/
    LOAD CSV/FOREACH and write procedures) are rejected. Caps: 4000
    chars query, 10s execution, `max_rows` (default 200, hard cap 500).

    Always prefer specialized tools when one fits. Call
    `describe_schema()` first if unsure about labels or properties.

    `description` is a one-sentence intent summary shown in the UI chip."""
    return T.run_cypher(cypher, params, description, max_rows)


@agent.tool_plain
async def internet_search(query: str, max_results: int = 5) -> dict:
    """Public web search (Tavily) for OFF-graph context: SARFAESI/legal
    explainers, RBI/bank news, locality background, term definitions.
    NEVER for property listings, prices, auction_ids, or counts. Cite
    inline as [1], [2] matching the order of `sources`; the UI renders
    chips automatically.

    Returns {sources: [{title, url, snippet, domain, score}], query} or
    {error: str} on failure."""
    return await W.internet_search(query, max_results=max_results)
