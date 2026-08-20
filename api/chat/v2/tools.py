"""
api/chat/v2/tools.py
--------------------
The tiered loop's tool surface: thin wrappers over the production
implementations in `api/tools/`, plus the catalogue text the planner sees.

Three deliberate differences from v1's wrappers in `api/agent.py`:

1. **Names match v1 exactly.** `search_auctions`, `semantic_search`,
   `get_auction_detail`, `describe_schema`, `run_cypher`, `internet_search`.
   The golden catalogue asserts tool trajectories by name
   (`evals/cases.py::KNOWN_TOOLS`), so matching means v2 is scored by the same
   assertions with no alias map — the spike needed one and it hid a real
   routing difference.

2. **Enum values live in the docstring, not the prompt.** The first narrowing
   run failed because the planner put "Residential" on `property_type` (a
   valid value, wrong field — it belongs on `asset_category`) and ran a whole
   conversation on zero rows. Moving the enums next to the parameter fixed it.
   They are rendered from the real constants in `api/tools/cypher_tools.py`,
   so a new enum value cannot drift out of the prompt.

3. **Errors come back as data.** See `model_visible_errors`.
"""
from __future__ import annotations

import functools
import inspect
from datetime import datetime
from typing import Callable

from api.tools import cypher_tools as T
from api.tools import web_tools as W

# Private in cypher_tools because nothing outside needed them until now. They
# are imported rather than restated so the catalogue the planner reads and the
# validation the tool performs can never disagree — the spike hardcoded these
# and drifted within a week.
from api.tools.cypher_tools import (  # noqa: PLC2701
    _AGG_FIELDS,
    _AGG_FUNCS,
    _DISTINCT_FIELDS,
    _DISTINCT_NODE_PROPS,
    _ORDER_BY_CLAUSES,
)


def model_visible_errors(fn: Callable) -> Callable:
    """Return validation errors to the model as data instead of raising.

    v1 gets this for free: pydantic-ai feeds a tool exception back to the
    model as a message, and the model self-corrects. Nothing does that for us
    — the tiered loop executes tools in its own executor — so an invalid
    `aggregate_field` would kill the turn outright, which is exactly what
    happened to the spike's variant A.

    Only `ValueError`/`TypeError` are converted: those are the model getting
    an argument wrong, and the message carries the valid values so it can fix
    itself. A real bug still raises.
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}

    return wrapped


def _dt(value: str | datetime | None) -> datetime | None:
    """Models emit ISO strings; `search_auctions` wants datetimes."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# NB: v2 does NOT use `api.tool_returns.split_ui_overflow`. That helper
# returns a pydantic-ai `ToolReturn`, which is meaningless outside v1's agent
# — the executor does the same split into `ExecutedCall.ui_rows` instead, so
# the model-visible result and the UI rows are separated exactly once, in one
# place. The tools here return the raw payload, `_ui_results` included.


# ── the six tools ───────────────────────────────────────────────────────────

@model_visible_errors
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
    starts_after: str | None = None,
    starts_before: str | None = None,
    deadline_within_days: int | None = None,
    order_by: str = "deadline_asc",
    aggregate_field: str | None = None,
    aggregations: list[str] | None = None,
    group_by: str | None = None,
    include_past: bool = False,
) -> dict:
    return T.search_auctions(
        min_price=min_price, max_price=max_price,
        min_emd=min_emd, max_emd=max_emd,
        city=city, area=area,
        property_type=property_type, asset_category=asset_category,
        bank=bank, borrower=borrower,
        auction_type=auction_type, branch_name=branch_name,
        service_provider=service_provider,
        is_reauction=is_reauction,
        starts_after=_dt(starts_after), starts_before=_dt(starts_before),
        deadline_within_days=deadline_within_days,
        limit=T._LLM_ROWS_HARD_CAP,
        order_by=order_by,
        aggregate_field=aggregate_field, aggregations=aggregations,
        group_by=group_by, include_past=include_past,
    )


@model_visible_errors
def semantic_search(
    query: str,
    city: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    limit: int = 10,
) -> dict:
    return T.semantic_search(
        query=query, city=city, min_price=min_price, max_price=max_price,
        limit=limit,
    )


#: Per-call cap in `get_auction_details`. Named here so the truncation can be
#: reported rather than applied silently.
DETAIL_BATCH_CAP = 10


@model_visible_errors
def get_auction_detail(auction_id: str | list[str]) -> dict:
    ids = auction_id if isinstance(auction_id, list) else [auction_id]
    result = T.get_auction_details(ids[:DETAIL_BATCH_CAP])
    if len(ids) > DETAIL_BATCH_CAP and isinstance(result, dict):
        # Observed live: the model asked for 15 ids, got 10, and wrote "this
        # applies to all 15 properties". Silent truncation reads as full
        # coverage, so say what was left out.
        result["not_fetched_ids"] = ids[DETAIL_BATCH_CAP:]
        result["_note"] = (
            f"Only the first {DETAIL_BATCH_CAP} ids were fetched. Do not draw "
            f"conclusions about the ids in not_fetched_ids — request them in a "
            f"follow-up call or say they were not checked."
        )
    return result


@model_visible_errors
def describe_schema(refresh: bool = False) -> dict:
    return T.describe_schema(refresh)


@model_visible_errors
def run_cypher(
    cypher: str,
    params: dict | None = None,
    description: str = "",
    max_rows: int = 200,
) -> dict:
    return T.run_cypher(cypher, params, description, max_rows)


async def internet_search(query: str, max_results: int = 5) -> dict:
    """Already async in production — awaited directly, never sent to a thread."""
    try:
        return await W.internet_search(query, max_results=max_results)
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


# ── the catalogue the planner reads ─────────────────────────────────────────
# Rendered from the real validation constants, so it cannot drift from what
# the tools actually accept.

_ORDER_BY = " | ".join(sorted(_ORDER_BY_CLAUSES))
_GROUP_BY = " | ".join(sorted([*_DISTINCT_FIELDS, *_DISTINCT_NODE_PROPS]))
_AGG_FIELD = " | ".join(sorted(_AGG_FIELDS))
_AGG_FUNC = " | ".join(sorted(_AGG_FUNCS))

ASSET_CATEGORIES = ["Residential", "Commercial", "Industrials"]

search_auctions.__doc__ = f"""Filter or aggregate auction properties.

Filters: min_price / max_price, min_emd / max_emd, city, area,
property_type, asset_category, bank, borrower, auction_type, branch_name,
service_provider (the e-auction platform, e.g. "BAANKNET"), is_reauction,
starts_after / starts_before (ISO datetimes), deadline_within_days.

asset_category is the broad class: {" | ".join(ASSET_CATEGORIES)}.
"Residential property" and "commercial property" questions filter HERE.
property_type is the specific kind (Flat, House, Villa, Plot, Land,
Residential Unit, Land And Building, Commercial Building, Commercial Shop,
Commercial Property, Agricultural Land, Industrial Land, Godown, Shed,
Factory land and Building, Machinary, Vehicle, Car, Others). Putting a
category value on property_type returns zero rows.

Single value or list per filter (OR within a list, AND across filters).
city / property_type / asset_category / bank / branch_name / auction_type
are exact names; area / borrower / service_provider are case-insensitive
substrings — combine area with city so same-named areas elsewhere don't
match.

order_by: {_ORDER_BY} (default deadline_asc). For "cheapest N" / "soonest N"
use ordering and cite the top rows; never invent price or date thresholds.

Aggregates: aggregate_field ∈ {_AGG_FIELD} with aggregations ⊆
[{_AGG_FUNC}]. Distributions: group_by ∈ {_GROUP_BY} returns value→count
buckets; all filters compose with it. Never loop per-value searches for
counts.

Returns {{total_count, returned, results}}. total_count is the true match
count — use it for "how many", never len(results). Future-only unless
include_past=True. A zero-result may carry `relax` / `hint` diagnostics and a
large one may carry `refine` buckets; follow them rather than retrying blind.
Every row carries is_reauction, reauction_count and previous_reserve_price
(vs reserve_price — the price-drop signal)."""

semantic_search.__doc__ = """Semantic + keyword search over property
descriptions and sale-notice text. Use for qualitative questions the
structured filters can't express: neighbourhood character, condition,
boundaries, legal wording, "near a school". Sizes and sub-locality
(village/taluk) exist only in this text, never as fields. Optional city and
price filters."""

get_auction_detail.__doc__ = """Full records for one or more auction_ids —
every stored field plus related city / area / state / bank / borrower /
category / property types, and `price_history` (the re-auction timeline).
Pass a LIST to fetch up to 10 at once; never one call per id. Returns
{results, returned, requested}; `missing_ids` lists ids the graph doesn't
hold — report those as not found rather than retrying."""

describe_schema.__doc__ = """Live graph schema: labels, relationship types,
enum values, numeric and date ranges, and the `cypher_patterns` cheat-sheet.
Cached ~1 h. Call before composing a novel run_cypher."""

run_cypher.__doc__ = """READ-ONLY Cypher for questions the structured tools
can't express. Writes are rejected; caps are 4000 chars, 10 s, and max_rows
(default 200, hard cap 500). `description` is a one-sentence intent summary
shown in the UI chip."""

internet_search.__doc__ = """Public web search for OFF-graph context:
SARFAESI/legal explainers, RBI or bank news, locality background, term
definitions. NEVER for property listings, prices, auction_ids or counts —
those come from the graph. One search per distinct topic. Returns
{sources: [{title, url, snippet, domain, score}], query}; cite inline as
[1], [2] matching the order of `sources`."""


#: Tools the planner may emit in tier 1/2. `describe_schema` and `run_cypher`
#: are deliberately absent — the planner has not seen the schema, so it asks
#: for tier 3 by signalling, and the composer gets them then. Same on-demand
#: shape as v1's deferred `cypher` capability.
PLANNER_TOOLS: dict[str, Callable] = {
    "search_auctions": search_auctions,
    "semantic_search": semantic_search,
    "get_auction_detail": get_auction_detail,
    "internet_search": internet_search,
}

#: Tier 3 only.
CYPHER_TOOLS: dict[str, Callable] = {
    "describe_schema": describe_schema,
    "run_cypher": run_cypher,
}

ALL_TOOLS: dict[str, Callable] = {**PLANNER_TOOLS, **CYPHER_TOOLS}


def render_catalogue(tools: dict[str, Callable] | None = None) -> str:
    """The tool catalogue block for the planner prompt, built from the real
    signatures and docstrings so it cannot describe a parameter that no longer
    exists."""
    tools = tools if tools is not None else PLANNER_TOOLS
    blocks = []
    for name, fn in tools.items():
        sig = inspect.signature(fn)
        params = ", ".join(
            p.name if p.default is inspect.Parameter.empty
            else f"{p.name}={p.default!r}"
            for p in sig.parameters.values()
        )
        doc = inspect.cleandoc(fn.__doc__ or "")
        blocks.append(f"{name}({params})\n{doc}")
    return "\n\n".join(blocks)
