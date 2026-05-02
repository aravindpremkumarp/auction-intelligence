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
users find, analyze, score, and track Indian bank auction properties (primarily
SARFAESI Act auctions) over a Neo4j knowledge graph of 3,391 Tamil Nadu
properties.

Operating principles:
1. Always ground answers with tools — never fabricate auction_ids, prices,
   counts, or enum values.
2. Cite auction_id values when recommending specific properties.
3. Explain trade-offs (price vs. location, urgency vs. diligence).
4. For state-changing actions (scoring commits, tracker transitions), ask
   the user to confirm before proceeding.
5. If a specialized tool matches the question, prefer it. Fall back to
   `run_cypher` only for genuinely novel queries the specialized tools
   cannot express. When in doubt about labels or property names, call
   `describe_schema()` first.
6. When a filter returns zero, try loosening (drop property_type, broaden
   price, verify city/area spelling) before telling the user there are no
   matches.
7. Never compute a count, sum, or distribution by iterating
   `get_auction_detail` across many auctions. For distribution / breakdown
   / "spread" questions, use `list_distinct` with the appropriate scope
   (`city`, `bank`, `borrower`, `asset_category`). If a `run_cypher`
   aggregate returns zero or a wrong-shape result, rewrite the query or
   call `describe_schema()` — do not fall back to per-row fetches.
8. `HAS_ASSET_CATEGORY`, `HAS_PROPERTY_TYPE`, `CONDUCTED_BY`,
   `HAS_BORROWER`, and `LOCATED_IN_*` all start on `AuctionProperty`.
   When composing multi-hop Cyphers, MATCH each relationship
   independently from the AuctionProperty node and join with commas;
   never chain `(Bank)-[:HAS_PROPERTY_TYPE]` or
   `(Bank)-[:HAS_ASSET_CATEGORY]` — those relationships do not exist.
9. Never fabricate numeric thresholds. Do not invent `min_price`,
   `max_price`, `starts_after`, or `starts_before` values the user did
   not state. For superlatives — "cheap", "cheapest N", "top N
   cheap", "most expensive", "soonest deadlines" — use `order_by` +
   `limit` on the already-narrowed scope, not a made-up threshold.
10. Carry forward scope filters across turns. If the user narrowed to a
    bank, city, area, property_type, or asset_category in any prior
    turn, keep passing that filter on every follow-up search_auctions
    call until the user explicitly changes or drops it. The "Active
    search scope" block in the system prompt lists the scope you must
    keep.
11. Use `internet_search` ONLY for questions that cannot be answered from
    the Neo4j auction graph: SARFAESI / legal procedure explanations,
    current bank or RBI news, locality background not stored in the
    graph, definitions of auction terms, recent regulatory changes.
    NEVER use it to look up specific properties, prices, deadlines,
    auction_ids, or counts — those live in the graph and the specialized
    tools are the source of truth. For hybrid questions (part graph,
    part web), call the graph tools FIRST, then `internet_search` for
    the explanatory part. Do not retry `internet_search` more than once
    per turn; if it returns `{error}`, tell the user web search is
    unavailable and answer from your training knowledge with a caveat.
12. When you use `internet_search`, weave the information into your prose
    answer and cite sources inline as bracketed numbers like [1], [2].
    The numbers must correspond 1-to-1 with the order of sources in the
    tool result. Do NOT print a separate "Sources:" list at the end —
    the UI renders source chips below your message automatically.
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


@agent.system_prompt(dynamic=True)
def inject_prior_search(ctx: RunContext[ChatDeps]) -> str:
    filters = ctx.deps.active_filters if ctx.deps else None
    total = ctx.deps.last_total_count if ctx.deps else None
    if not filters and total is None:
        return ""
    lines = ["Active search scope narrowed across prior turns:"]
    if filters:
        for k, v in filters.items():
            lines.append(f"- {k}: {v!r}")
    else:
        lines.append("- (no scope filters active yet)")
    if total is not None:
        lines.append(f"- last total_count: {total}")
    lines.append(
        "Rule: unless the user explicitly changes or removes one of these "
        "scopes in the current turn, include ALL of them in your next "
        "search_auctions call. Never introduce a filter the user never "
        "mentioned — especially min_price / max_price / starts_after / "
        "starts_before. For 'cheapest N' / 'top N' / 'soonest N' use "
        "order_by + limit on top of the carried scope; do not fabricate a "
        "price or date threshold."
    )
    return "\n".join(lines)


@agent.system_prompt(dynamic=True)
def inject_mode_overlay(ctx: RunContext[ChatDeps]) -> str:
    """If the caller requested a mode (deep-research / compare / report),
    append the mode's markdown spec to the system prompt."""
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
    city: str | None = None, area: str | None = None,
    property_type: str | None = None,
    asset_category: str | None = None,
    bank: str | None = None,
    starts_after: datetime | None = None, starts_before: datetime | None = None,
    limit: int = 20,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    include_past: bool = False,
) -> dict:
    """Filter auctions by price, city, area, type, asset category, bank, and
    date window. Optional `order_by` and `limit` control row ordering.

    Returns {total_count, returned, limit, results}. `total_count` is the true
    number of matches in the graph (ignoring limit); `results` is capped at
    `limit`. Use `total_count` whenever the user asks about quantity, totals,
    availability, or any aggregate question — do not infer counts from
    `len(results)`, which only reflects the page size.

    By default this tool excludes past auctions (auction_start_dt < now()).
    Pass `include_past=True` ONLY for genuine retrospective questions
    ("how many auctions happened last year", "average price across the
    full 2025 catalog"). For all forward-looking buyer questions, leave
    it false.

    Location filters:
      - `city` matches a City node by exact name (e.g. "Chennai", "Kanchipuram").
      - `area` matches an Area node inside a city (suburb / taluk / locality,
        e.g. "Ambattur", "Sriperumbudur"). Case-insensitive substring match,
        so "ambattur" and "Ambattur" both work. Use this for
        "show me properties in <area>" style queries — combine with `city`
        when the user also names the city.

    Scope filters:
      - `bank` matches a Bank node by exact name (e.g. "Canara Bank",
        "State Bank of India"). Use this when the user narrowed the
        conversation to a specific bank — once they say "in Canara Bank",
        keep passing bank="Canara Bank" on every follow-up search until
        they clearly change scope.

    Ordering / superlatives:
      - `order_by` selects row order: "deadline_asc" (default — soonest
        auction first), "price_asc" (cheapest first), "price_desc"
        (most expensive first).
      - For "cheapest N", "5 cheap ones", "top cheap", "lowest priced":
        pass order_by="price_asc" and set `limit` to N. DO NOT invent a
        `max_price` threshold the user did not state.
      - For "most expensive N" / "top priced": order_by="price_desc".
      - For "soonest N" / "next N deadlines": order_by="deadline_asc".

    For aggregate/quantitative questions — "price range", "median price",
    "average EMD", "distribution", etc. — ALSO set:
      - aggregate_field: one of "reserve_price_num" or "emd_num"
      - aggregations: any subset of ["min","max","avg","median","p25","p75"]
    These compute over the FULL filtered set (ignoring `limit`) and are added
    as an `aggregations` key in the return dict. When you only need stats and
    not sample rows, pass `limit=0` to skip the row-fetch entirely.

    Example for "what is the price range of the 422 Chennai flats":
      search_auctions(city="Chennai", property_type="Flat",
                      aggregate_field="reserve_price_num",
                      aggregations=["min","max"], limit=0)

    Example for "5 cheapest Canara Bank lands in Chennai":
      search_auctions(bank="Canara Bank", property_type="Land",
                      city="Chennai", order_by="price_asc", limit=5)
    """
    return T.search_auctions(
        min_price=min_price, max_price=max_price,
        city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank,
        starts_after=starts_after, starts_before=starts_before,
        limit=limit, order_by=order_by,
        aggregate_field=aggregate_field, aggregations=aggregations,
        include_past=include_past,
    )


@agent.tool_plain
def find_similar_properties(auction_id: str, price_tolerance_pct: float = 25.0, limit: int = 10) -> list[dict]:
    """Given an auction_id, find comparable properties in the same area with similar price."""
    return T.find_similar_properties(auction_id, price_tolerance_pct, limit)


@agent.tool_plain
def bank_portfolio(bank_name: str) -> list[dict]:
    """All auctions by a given bank with aggregated statistics."""
    return T.bank_portfolio(bank_name)


@agent.tool_plain
def location_analysis(location: str, location_type: str = "city") -> list[dict]:
    """Price distribution and auction density for a city, area, or state."""
    return T.location_analysis(location, location_type)


@agent.tool_plain
def upcoming_auctions(days: int = 14, limit: int = 20) -> list[dict]:
    """Auctions with application deadline within N days."""
    return T.upcoming_auctions(days, limit)


@agent.tool_plain
def price_comparison(city: str, property_type: str) -> list[dict]:
    """Compare reserve prices for similar properties in the same area."""
    return T.price_comparison(city, property_type)


@agent.tool_plain
def borrower_lookup(borrower_name: str) -> list[dict]:
    """Find all properties tied to a borrower name (fuzzy match on substring)."""
    return T.borrower_lookup(borrower_name)


@agent.tool_plain
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
    """Vector search over property descriptions for qualitative traits.

    Use this when the user asks about features that live in free-text
    descriptions (boundaries, neighborhood character, legal language,
    property condition) rather than structured fields. Optional city /
    area / price / asset_category / date window act as post-filters on
    the semantic hits. Results include a `score` (higher = more similar).

    Defaults to future-only auctions; pass include_past=True for
    retrospective queries. For "find me this exact pasted property"
    use `match_pasted_listing` instead — semantic search alone has no
    way to disambiguate between similar listings."""
    return T.semantic_property_search(
        query, city=city, area=area,
        min_price=min_price, max_price=max_price,
        asset_category=asset_category,
        starts_after=starts_after, starts_before=starts_before,
        limit=limit, include_past=include_past,
    )


@agent.tool_plain
def survey_search(survey_no: str, subdivision: str | None = None) -> list[dict]:
    """Find properties by survey number (with optional subdivision)."""
    return T.survey_search(survey_no, subdivision)


@agent.tool_plain
def match_pasted_listing(pasted_text: str) -> dict:
    """Find the auction that matches a pasted property listing (WhatsApp
    forward, broker note, bank circular). Use this WHENEVER the user
    pastes a blurb that includes any of: a price, an EMD/auction date,
    a building name, a plot number, an area, or a PIN — even if the
    paste is messy with emojis or line noise. Always preferred over
    `semantic_property_search` for this task.

    Returns {match, confidence, candidates, alternates, widening_reason,
    extracted, note?}:
      - `match`: top auction row when the strict price+date+area+city
        filter hits, else None.
      - `confidence`: 0.0–1.0. When `match` is None, confidence is 0.
      - `candidates`: ALWAYS populated when the graph has anything close
        — even if `match` is None. Up to 5 rows.
      - `widening_reason`: None on a strict hit; on a widened result, a
        short string ("dropped auction-date constraint", "widened price
        band to ±10% and dropped date", etc.) explaining which constraint
        was relaxed to find these candidates.
      - `extracted`: the structured fields parsed from the paste, so
        you can show the user what we understood.

    How to present the result to the user:
      - `match` set & confidence ≥ 0.6 → "Found it: <match>".
      - `match` is None & `candidates` non-empty → "I couldn't find
        this exact property in the graph. Here are the closest matches
        (<widening_reason>): ...". List the candidates. Quote the
        widening_reason verbatim so the user sees what was relaxed.
      - `match` is None & `candidates` empty → tell the user we have
        nothing close; ask for the auction_id or clearer location/price."""
    return T.match_pasted_listing(pasted_text)


@agent.tool_plain
def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction: every stored node property plus related
    city/area/state/bank/borrower/asset_category/property_type/survey_numbers.
    Use whenever the user's question about a specific auction_id needs more
    than the thin fields returned by `search_auctions` rows — before
    concluding a field is unavailable, call this. Returns None if the
    auction_id does not exist."""
    return T.get_auction_detail(auction_id)


@agent.tool_plain
def list_distinct(
    field: str,
    limit: int = 100,
    city: str | None = None,
    bank: str | None = None,
    borrower: str | None = None,
    asset_category: str | None = None,
) -> dict:
    """List distinct values of a reference field with per-value auction counts.

    `field` must be one of: "city", "area", "state", "bank", "borrower",
    "asset_category", "property_type".

    Scope filters narrow the count. Supply any combination of `city`,
    `bank`, `borrower`, `asset_category`; a scope must differ from
    `field`. Examples:
      - property-type mix for SBI: field="property_type", bank="State Bank of India"
      - asset categories in Chennai: field="asset_category", city="Chennai"
      - residential property types in Kanchipuram: field="property_type",
        city="Kanchipuram", asset_category="Residential"

    Use this for distribution / breakdown / "spread" questions. Do NOT
    compute distributions by iterating `get_auction_detail`."""
    return T.list_distinct(
        field,
        limit,
        city=city,
        bank=bank,
        borrower=borrower,
        asset_category=asset_category,
    )


@agent.tool_plain
def describe_schema(refresh: bool = False) -> dict:
    """Describe the graph's labels, relationship types, enum values, and the
    numeric/date ranges of key AuctionProperty fields. Cached for 1 hour.

    Call this BEFORE using `run_cypher` on a novel question when you are
    unsure about label names, relationship names, property names, or what
    enum values exist."""
    return T.describe_schema(refresh)


@agent.tool_plain
def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
) -> dict:
    """Execute a READ-ONLY Cypher query for questions the specialized tools
    cannot express. Server-side guardrails reject CREATE/MERGE/DELETE/SET/
    REMOVE/DROP/LOAD CSV/FOREACH and write-side procedures; the session
    also forces READ access mode. Query text is capped at 4000 chars,
    execution at 10 seconds, and results at `max_rows` (default 200,
    hard-capped at 500).

    ALWAYS prefer the specialized tools when one fits. Before composing a
    `run_cypher` query for a novel question, call `describe_schema()` if
    you're unsure about labels, relationships, or property names.

    `description` is a one-sentence human-readable summary of intent — it
    surfaces in the artifact chip and helps users understand what ran.

    Returns {description, cypher, params, rows, returned, duration_ms}."""
    return T.run_cypher(cypher, params, description, max_rows)


@agent.tool_plain
async def internet_search(query: str, max_results: int = 5) -> dict:
    """Search the public web (Tavily) for general information OUTSIDE the
    Tamil Nadu auction graph: SARFAESI / legal explanations, market or RBI
    news, locality background, term definitions. NOT for property listings,
    prices, auction_ids, or counts — those live in the graph and the
    specialized tools are the source of truth.

    Cite each source you use in your prose as [1], [2], ... matching the
    order of the returned `sources`. The UI renders source chips below the
    message automatically — do not add a trailing "Sources:" list.

    Returns {sources: [{title, url, snippet, domain, score}], query} on
    success or empty results, or {error: str} on failure / when disabled."""
    return await W.internet_search(query, max_results=max_results)
