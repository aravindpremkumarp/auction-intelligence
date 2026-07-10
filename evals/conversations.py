"""
evals/conversations.py
----------------------
The multi-turn conversation catalogue: scripted dialogues that exercise the
behaviors a single-turn eval is structurally blind to —

  * **Progressive refinement** — a conversation that narrows from a broad match
    set down to a handful by successive filters ("residential in Chennai" →
    "under 40 lakhs" → "only re-auctions" → "closing this week"). Tests the
    rolling-scope carry-over (`_extract_active_filters` +
    `inject_prior_search`), and that the match count monotonically shrinks.
  * **Topic switch** — an unrelated question dropped into the middle of a
    thread. Two flavours: an *off-graph aside* (a term definition) that must
    route to `internet_search` without destroying the carried property scope,
    and a *scope replacement* ("commercial in Coimbatore instead") where the
    agent must REPLACE the conflicting filter rather than AND the stale one on
    and silently over-constrain the search.

Like `evals/cases.py`, this module is intentionally **dependency-free** (stdlib
only) so the offline shape test can import it without pydantic-ai / Neo4j. It is
the single source of truth for the conversation catalogue; the live runner is
`evals/run_conversations.py`.

`ANY` is the sentinel for an `expect_filters` value that must be *present with
any value* (e.g. the model picks its own `deadline_within_days`), vs. an exact
value that must match.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.cases import KNOWN_TOOLS  # noqa: F401  (re-exported for the shape test)

# Sentinel: "this filter key must be carried, but any value is acceptable".
ANY = "*ANY*"

# Scope-carrying filter keys, mirrored from api/chat/router.py's
# `_CARRY_FORWARD_FILTER_KEYS`. Kept here so this module stays dependency-free;
# the offline shape test cross-checks the two sets so they can't drift.
CARRY_FORWARD_FILTER_KEYS: set[str] = {
    "min_price", "max_price",
    "min_emd", "max_emd",
    "city", "area",
    "property_type", "asset_category",
    "bank", "borrower",
    "auction_type", "branch_name",
    "service_provider",
    "is_reauction",
    "starts_after", "starts_before",
    "deadline_within_days",
}


@dataclass
class Turn:
    """One user message in a scripted conversation, plus what should be true
    after the agent answers it."""

    message: str
    # At least one of these tools must be called on this turn. Empty = no tool
    # requirement (e.g. a pure re-presentation of already-found rows).
    expected_tools: list[str] = field(default_factory=list)
    # Part of the monotonic-narrowing chain: this turn's search `total_count`
    # must be <= the previous narrowing turn's. Set on refinement turns.
    narrows: bool = False
    # Filters that must be present in the rolling scope AFTER this turn (as
    # re-derived by the router's `_extract_active_filters`). Value `ANY` means
    # "key present, any value". Proves carry-over accumulated / replaced right.
    expect_filters: dict = field(default_factory=dict)
    # This turn changes topic. Combined with `expect_filters` (the new scope)
    # and `forbid_tool_arg_values` (stale values that must NOT appear in this
    # turn's search args), it catches the stale-scope bug.
    topic_switch: bool = False
    # Per-search-arg values that must NOT appear in this turn's search tool
    # call (e.g. the old city on a scope-replacement pivot). Maps arg key ->
    # forbidden value.
    forbid_tool_arg_values: dict = field(default_factory=dict)


@dataclass
class GoldenConversation:
    conv_id: str
    description: str
    turns: list[Turn]


GOLDEN_CONVERSATIONS: list[GoldenConversation] = [
    # ─── Refinement funnel: broad → narrow, scope accumulates ───────────
    GoldenConversation(
        "refine_residential_chennai",
        "Narrow a residential-Chennai search down by price, re-auction, deadline.",
        [
            Turn("Residential auctions in Chennai",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Chennai", "asset_category": "Residential"}),
            Turn("Only the ones under 40 lakhs",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Chennai", "asset_category": "Residential",
                                 "max_price": 4000000}),
            Turn("Among those, just the re-auctions",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Chennai", "asset_category": "Residential",
                                 "max_price": 4000000, "is_reauction": True}),
            Turn("Which of those close within the next week?",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Chennai", "asset_category": "Residential",
                                 "max_price": 4000000, "is_reauction": True,
                                 "deadline_within_days": ANY}),
        ],
    ),

    # ─── Refinement funnel: commercial, price then platform ─────────────
    GoldenConversation(
        "refine_commercial_coimbatore",
        "Commercial-Coimbatore search narrowed by price band and platform.",
        [
            Turn("Commercial auctions in Coimbatore",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Coimbatore", "asset_category": "Commercial"}),
            Turn("Only between 50 lakhs and 1 crore",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Coimbatore", "asset_category": "Commercial",
                                 "min_price": 5000000, "max_price": 10000000}),
            Turn("And only the ones on BAANKNET",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Coimbatore", "asset_category": "Commercial",
                                 "min_price": 5000000, "max_price": 10000000,
                                 "service_provider": ANY}),
        ],
    ),

    # ─── Off-graph aside: pivot to a definition, scope must survive ─────
    GoldenConversation(
        "aside_offgraph_then_resume",
        "An EMD-definition aside mid-thread must use internet_search and NOT "
        "wipe the carried property scope when the user resumes.",
        [
            Turn("Industrial auctions in Kanchipuram",
                 expected_tools=["search_auctions"], narrows=True,
                 expect_filters={"city": "Kanchipuram", "asset_category": "Industrials"}),
            # Topic switch to an off-graph definition. Routes to internet_search;
            # the property scope legitimately persists across the aside (not
            # asserted dropped — resuming should still see Kanchipuram).
            Turn("Quick question — what does EMD mean in a bank auction?",
                 expected_tools=["internet_search"], topic_switch=True),
            # Resume: the earlier scope must still be in effect.
            Turn("Ok, back to those — show me the cheapest few",
                 expected_tools=["search_auctions"],
                 expect_filters={"city": "Kanchipuram", "asset_category": "Industrials"}),
        ],
    ),

    # ─── Scope replacement: the stale-filter bug catcher ────────────────
    GoldenConversation(
        "switch_replace_scope",
        "A mid-thread pivot to a different city+category must REPLACE the "
        "conflicting filters, not AND the stale ones on.",
        [
            Turn("Flats in Chennai under 50 lakhs",
                 expected_tools=["search_auctions"],
                 expect_filters={"city": "Chennai", "max_price": 5000000}),
            # Pivot: new city + category. The post-turn scope must show
            # Coimbatore/Commercial, and this turn's search args must NOT carry
            # the stale city "Chennai" (the over-constrain bug).
            Turn("Actually, show me commercial properties in Coimbatore instead",
                 expected_tools=["search_auctions"], topic_switch=True,
                 expect_filters={"city": "Coimbatore", "asset_category": "Commercial"},
                 forbid_tool_arg_values={"city": "Chennai"}),
        ],
    ),

    # ─── Aggregate then drill-down: scope carries from a stats turn ─────
    GoldenConversation(
        "aggregate_then_list",
        "Scope set on an aggregation turn must carry into the follow-up listing.",
        [
            Turn("What's the median reserve price for flats in Chennai?",
                 expected_tools=["search_auctions"],
                 expect_filters={"city": "Chennai"}),
            Turn("Now list a few of those flats, cheapest first",
                 expected_tools=["search_auctions"],
                 expect_filters={"city": "Chennai"}),
        ],
    ),
]
