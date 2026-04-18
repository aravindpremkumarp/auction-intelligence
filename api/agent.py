"""
api/agent.py
------------
PydanticAI agent wired to OpenRouter (Gemini 2.0 Flash) with Cypher tools.
Keeps the existing OpenRouter config from pipeline/config.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from pipeline.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from api.tools import cypher_tools as T


@dataclass
class ChatDeps:
    last_search: dict | None = None

SYSTEM_PROMPT = """\
You are an AI assistant for the Bank Auction Intelligence Platform. You help
users find, analyze, score, and track Indian bank auction properties (primarily
SARFAESI Act auctions).

You have tools to query a Neo4j knowledge graph of 3,391 Tamil Nadu auction
properties. Always:
1. Use tools to ground answers — never fabricate auction_ids or prices.
2. Cite auction_id values when recommending.
3. Explain trade-offs (price vs. location, urgency vs. diligence).
4. For scoring/tracking actions that change state, ask for user confirmation.

## Schema notes — pick the right filter

- `asset_category` is the BROAD class. The 7 exact values are:
  "Residential", "Commercial", "Industrials", "Scrap, Plant & Machinery"
  (single value — the comma is part of the name, do not split it),
  "Vehicle Auctions", "Gold Auctions", "Others".
  **When a user says "residential", "commercial", "industrial" — use asset_category.**

- `property_type` is GRANULAR and is constrained by asset_category. An
  auction can have multiple property types, so `search_auctions` returns
  a `property_types` list per row — do not split a single value on
  commas when presenting it to the user. Pass only ONE value at a time
  to the `property_type` filter. Allowed values per category:
    - Residential: Plot, Land And Building, Land, Agricultural Land, Flat,
      House, Non-Agricultural Land, Residential Unit, Bungalow, Villa
    - Commercial: Commercial Office, Commercial Property, Commercial Shop,
      Commercial Building, Cold Storage Land And Building
    - Industrials: Factory land and Building, Shed, Industrial Land,
      Industrial Land & Building, Godown, Land
    - Scrap, Plant & Machinery: Plant & Machinery, Machinary, Scrap
    - Vehicle Auctions: Car, Vehicle, Bus, Bike
    - Gold Auctions: (none)
    - Others: Others
  Values are stored verbatim, so use the exact casing and spelling above
  (including the source typo "Machinary" and mixed-case
  "Factory land and Building"). **Use property_type only when the user
  specifies a concrete type like "flat" or "plot".**

- Prices are in INR. "30 lakhs" = 3,000,000. "1 crore" = 10,000,000.

- Cities are already in title case (e.g. "Chennai", "Kanchipuram").

- `area` narrows within a city (suburb / taluk / locality, e.g. "Ambattur"
  inside Chennai, "Sriperumbudur" inside Kanchipuram). Pass `area=...` to
  `search_auctions` whenever the user names a place that is not a full city.
  Case-insensitive, so "ambattur" and "Ambattur" both match.

If a search returns zero, try loosening (drop property_type, broaden price,
check city/area spelling) before telling the user there are no matches.

## Choosing between search_auctions and semantic_property_search

- Use `search_auctions` (Cypher) for structured filters: price ranges,
  cities, asset_category / property_type, date windows, or any combination
  of these. This is the default for most user queries.
- Use `semantic_property_search` only when the user asks about qualitative
  traits buried in free-text descriptions — boundaries ("next to a channel",
  "faces main road"), neighborhood features, legal caveats, or property
  condition language that the structured schema does not capture. You may
  still pass city / price / asset_category as post-filters on the semantic
  result set.

## Single-property detail vs. list rows

`search_auctions` rows are thin projections meant for browsing. Any
question about ONE specific auction that goes beyond those row fields —
whatever the user's phrasing — should be answered by calling
`get_auction_detail(auction_id)`, which returns the full stored record
(all node properties + related city/area/state/bank/borrower/category/
property_types list/survey numbers). Before saying a field is
unavailable, call this tool. Summarize what's relevant to the user's
question rather than dumping the raw dict. Note that `property_types`
is a list — present each value as-is without re-splitting on commas.
"""

_provider = OpenAIProvider(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
_model = OpenAIModel(OPENROUTER_MODEL, provider=_provider)

agent = Agent(_model, deps_type=ChatDeps, system_prompt=SYSTEM_PROMPT)


@agent.system_prompt(dynamic=True)
def inject_prior_search(ctx: RunContext[ChatDeps]) -> str:
    ls = ctx.deps.last_search if ctx.deps else None
    if not ls:
        return ""
    return (
        "Prior tool result in this conversation:\n"
        f"- tool: {ls['tool']}\n"
        f"- filters: {ls['filters']}\n"
        f"- total_count: {ls['total_count']}"
    )


@agent.tool_plain
def search_auctions(
    min_price: float | None = None, max_price: float | None = None,
    city: str | None = None, area: str | None = None,
    property_type: str | None = None,
    asset_category: str | None = None,
    starts_after: datetime | None = None, starts_before: datetime | None = None,
    limit: int = 20,
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
) -> dict:
    """Filter auctions by price, city, area, type, asset category, and date window.

    Returns {total_count, returned, limit, results}. `total_count` is the true
    number of matches in the graph (ignoring limit); `results` is capped at
    `limit`. Use `total_count` whenever the user asks about quantity, totals,
    availability, or any aggregate question — do not infer counts from
    `len(results)`, which only reflects the page size.

    Location filters:
      - `city` matches a City node by exact name (e.g. "Chennai", "Kanchipuram").
      - `area` matches an Area node inside a city (suburb / taluk / locality,
        e.g. "Ambattur", "Sriperumbudur"). Case-insensitive substring match,
        so "ambattur" and "Ambattur" both work. Use this for
        "show me properties in <area>" style queries — combine with `city`
        when the user also names the city.

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
    """
    return T.search_auctions(min_price, max_price, city, area, property_type,
                             asset_category, starts_after, starts_before, limit,
                             aggregate_field, aggregations)


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
    min_price: float | None = None,
    max_price: float | None = None,
    asset_category: str | None = None,
    limit: int = 20,
) -> dict:
    """Vector search over property descriptions for qualitative traits.

    Use this when the user asks about features that live in free-text
    descriptions (boundaries, neighborhood character, legal language,
    property condition) rather than structured fields. Optional city /
    price / asset_category act as post-filters on the semantic hits.
    Results include a `score` (higher = more similar).
    """
    return T.semantic_property_search(query, city, min_price, max_price, asset_category, limit)


@agent.tool_plain
def survey_search(survey_no: str, subdivision: str | None = None) -> list[dict]:
    """Find properties by survey number (with optional subdivision)."""
    return T.survey_search(survey_no, subdivision)


@agent.tool_plain
def get_auction_detail(auction_id: str) -> dict | None:
    """Full record for ONE auction: every stored node property plus related
    city/area/state/bank/borrower/asset_category/property_type/survey_numbers.
    Use whenever the user's question about a specific auction_id needs more
    than the thin fields returned by `search_auctions` rows — before
    concluding a field is unavailable, call this. Returns None if the
    auction_id does not exist."""
    return T.get_auction_detail(auction_id)
