"""
evals/cases.py
--------------
The golden-question catalogue: every realistic question type we want the chat
agent to handle, grouped by intent, with the tool(s) it should call.

This module is intentionally **dependency-free** (stdlib only) so it can be
imported by both the offline pytest shape test (`tests/api/test_golden_questions.py`)
and the live `pydantic-evals` runner (`evals/run_golden.py`) without dragging in
pydantic-ai / Neo4j. It is the single source of truth for the catalogue.

Two kinds of case:

- **Tool-trajectory cases** carry `acceptable_tools` (the tool[s] a passing
  answer must route through). Gated live by the `ToolTrajectory` evaluator.
- **Refusal cases** (`expect_refusal=True`) test the Rule-4 boundary — the
  agent must decline an out-of-scope request gracefully instead of fabricating
  or promising an action no tool performs. They carry no `acceptable_tools`
  (the correct behavior is usually *no* data tool) and instead a
  `refusal_required_any` lexicon; gated live by the `GracefulRefusal`
  evaluator. See `evals/evaluators.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The always-on, model-visible tools the agent exposes (api/agent.py). Every
# `acceptable_tools` entry is validated against this set by the shape test, and
# the set itself is cross-checked against api/agent.py's actually-decorated
# tools (see tests/api/test_golden_questions.py::test_known_tools_match_agent)
# so a renamed/removed tool fails the shape test instead of silently never
# matching in the live eval. Excludes `query_user_dossier`, which ships dark
# (registered conditionally, not via a decorator) and is not exercised here.
KNOWN_TOOLS: set[str] = {
    "search_auctions",
    "semantic_search",
    "get_auction_detail",
    "describe_schema",
    "run_cypher",
    "internet_search",
}

# Decline lexicon for "we don't hold that data" refusals (litigation, market
# valuation, ownership chains, …). A graceful refusal contains at least one of
# these; kept broad so it tolerates phrasing variation while still failing an
# answer that fabricates the data instead of declining.
_DECLINE_MARKERS: list[str] = [
    "don't have", "do not have", "not have", "no data", "not available",
    "isn't available", "is not available", "not something", "no information",
    "cannot", "can't", "unable", "not in the", "not part of", "outside",
    "don't track", "do not track", "no litigation", "not cover", "beyond",
]


@dataclass
class GoldenCase:
    intent: str
    question: str
    acceptable_tools: list[str] = field(default_factory=list)
    # When True, a passing answer must not leak an internal write-rejection /
    # error string to the user (the agent is read-only over the graph).
    must_not_mention_write_error: bool = True
    # Refusal case: the request is out of scope (Rule 4) and the agent must
    # decline gracefully. `refusal_required_any` = substrings, at least one of
    # which a graceful refusal contains (case-insensitive). Checked by the
    # GracefulRefusal evaluator; ignored when expect_refusal is False.
    expect_refusal: bool = False
    refusal_required_any: list[str] = field(default_factory=list)


GOLDEN: list[GoldenCase] = [
    # ─── Basic filters ──────────────────────────────────────────────────
    GoldenCase("basic_filter", "Residential auctions in Chennai under 30 lakhs",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Show all properties in Ambattur",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Commercial auctions in Coimbatore between 50 lakhs and 1 crore",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Industrial properties in Kanchipuram",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Agricultural land in Tamil Nadu",
               ["search_auctions"]),
    # Under-tested structured filters that have first-class tool args.
    GoldenCase("basic_filter", "DRT auctions in Chennai",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Liquidation auctions in Tamil Nadu",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Auctions on the BAANKNET platform",
               ["search_auctions"]),
    GoldenCase("basic_filter", "Auctions listed by the Anna Nagar branch",
               ["search_auctions"]),

    # ─── Aggregations ───────────────────────────────────────────────────
    GoldenCase("aggregation", "What is the median reserve price for flats in Chennai?",
               ["search_auctions"]),
    GoldenCase("aggregation", "Average EMD across all residential auctions",
               ["search_auctions"]),
    GoldenCase("aggregation", "Price range of plots in Sriperumbudur",
               ["search_auctions"]),
    GoldenCase("aggregation", "How many auctions are there in Tamil Nadu?",
               ["search_auctions", "run_cypher"]),
    GoldenCase("aggregation", "Count of auctions per city, top 10",
               ["run_cypher", "search_auctions"]),
    GoldenCase("aggregation", "Monthly auction volume in 2026",
               ["run_cypher"]),
    GoldenCase("aggregation", "Which cities have the most auctions?",
               ["search_auctions", "run_cypher"]),
    # p95 isn't a search_auctions aggregation (min/max/avg/median/p25/p75
    # only) — the real path is run_cypher; describe_schema is preparatory, not
    # an answer, so it's no longer accepted here.
    GoldenCase("aggregation", "What is the 95th percentile reserve price?",
               ["run_cypher"]),

    # ─── Multi-hop / novel ──────────────────────────────────────────────
    GoldenCase("multi_hop", "Banks with more than 50 auctions in Tamil Nadu",
               ["run_cypher", "search_auctions"]),
    GoldenCase("multi_hop", "Borrowers with multiple properties",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Areas where EMD is more than 15% of reserve on average",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Which bank has the lowest average reserve price in Chennai?",
               ["run_cypher"]),
    # "Which property types appear in the industrial category" is a filtered
    # graph query (types linked to Industrial auctions), not a static enum
    # dump — describe_schema (global enums) doesn't answer it.
    GoldenCase("multi_hop", "Property types available in industrial asset category",
               ["search_auctions", "run_cypher"]),
    GoldenCase("multi_hop", "Top 5 areas by auction count",
               ["search_auctions", "run_cypher"]),
    GoldenCase("multi_hop", "Cities that appear in both residential and commercial auctions",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Auctions where the borrower has more than one property",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Banks active in Chennai sorted by portfolio size",
               ["run_cypher", "search_auctions"]),
    GoldenCase("multi_hop", "Which banks have the highest median reserve price?",
               ["run_cypher"]),

    # ─── Schema / enum discovery ────────────────────────────────────────
    # Enumerating all values of a node label is equally natural via a
    # group_by search or a `MATCH (n:Label) RETURN n.name` run_cypher, so both
    # are accepted (accepting only search_auctions scored a sensible run_cypher
    # answer as a false negative).
    GoldenCase("schema", "What cities do you have data for?",
               ["search_auctions", "run_cypher"]),
    GoldenCase("schema", "List all banks in the database",
               ["search_auctions", "run_cypher"]),
    # For a global enum list, describe_schema genuinely returns the answer.
    GoldenCase("schema", "What property types are available?",
               ["search_auctions", "describe_schema", "run_cypher"]),
    GoldenCase("schema", "What asset categories exist?",
               ["search_auctions", "describe_schema", "run_cypher"]),
    GoldenCase("schema", "What fields does an auction property have?",
               ["describe_schema"]),

    # ─── Specific auction ───────────────────────────────────────────────
    GoldenCase("specific_auction", "Give me details for auction AUC-12345",
               ["get_auction_detail"]),
    GoldenCase("specific_auction", "What is the possession type for auction AUC-12345?",
               ["get_auction_detail"]),
    GoldenCase("specific_auction", "Find properties similar to AUC-12345",
               ["get_auction_detail", "search_auctions"]),
    GoldenCase("specific_auction", "Show the borrower for auction AUC-12345",
               ["get_auction_detail"]),
    GoldenCase("specific_auction", "Which bank conducted auction AUC-12345?",
               ["get_auction_detail"]),

    # ─── Semantic / description ─────────────────────────────────────────
    # NB: the tool is `semantic_search` (api/agent.py). It was catalogued under
    # its pre-rename name `semantic_property_search` — a string the live agent
    # never emits — so every one of these silently failed the trajectory gate
    # nightly until this fix.
    GoldenCase("semantic", "Properties facing a main road",
               ["semantic_search"]),
    GoldenCase("semantic", "Plots with clear boundaries mentioned in the description",
               ["semantic_search"]),
    GoldenCase("semantic", "CMDA approved plots in Sriperumbudur",
               ["semantic_search", "search_auctions"]),
    GoldenCase("semantic", "Properties with a channel or paddy field nearby",
               ["semantic_search"]),

    # ─── Temporal ───────────────────────────────────────────────────────
    GoldenCase("temporal", "Auctions with deadline in the next 7 days",
               ["search_auctions"]),
    GoldenCase("temporal", "Auctions starting in Q1 2026 in Chennai",
               ["search_auctions"]),
    GoldenCase("temporal", "Auctions closing this week",
               ["search_auctions"]),

    # ─── Superlatives (ordering + limit, never invented thresholds) ─────
    GoldenCase("superlative", "Cheapest 5 flats in Chennai",
               ["search_auctions"]),
    GoldenCase("superlative", "Most expensive commercial auction in Coimbatore",
               ["search_auctions"]),
    GoldenCase("superlative", "Auctions with the soonest application deadline",
               ["search_auctions"]),
    GoldenCase("superlative", "Highest EMD auctions in Tamil Nadu",
               ["search_auctions"]),

    # ─── Re-auctions (is_reauction + price-drop fields) ─────────────────
    GoldenCase("reauction", "Show re-auctioned properties in Chennai",
               ["search_auctions"]),
    GoldenCase("reauction", "Properties where the reserve price dropped from a previous auction",
               ["search_auctions"]),
    GoldenCase("reauction", "Fresh listings only in Coimbatore, no re-auctions",
               ["search_auctions"]),
    GoldenCase("reauction", "How many auctions are re-auctions?",
               ["search_auctions", "run_cypher"]),

    # ─── Borrower lookup ────────────────────────────────────────────────
    GoldenCase("borrower", "Auctions tied to borrower XYZ Industries",
               ["search_auctions"]),
    GoldenCase("borrower", "Show auctions for borrower Sri Lakshmi Enterprises",
               ["search_auctions"]),
    GoldenCase("borrower", "Which borrowers have properties in Chennai?",
               ["search_auctions", "run_cypher"]),

    # ─── Off-graph context (internet_search, per Rule 3) ────────────────
    GoldenCase("off_graph", "What does SARFAESI mean?",
               ["internet_search"]),
    GoldenCase("off_graph", "Explain what EMD is in a bank auction",
               ["internet_search"]),
    GoldenCase("off_graph", "What are the RBI guidelines for e-auction of secured assets?",
               ["internet_search"]),

    # ─── Edge / negative cases ──────────────────────────────────────────
    # Zero-result and out-of-coverage questions: the agent must still ground
    # the answer in a tool call and say "none found" rather than hallucinate
    # listings or invent coverage it doesn't have.
    GoldenCase("edge", "Residential auctions in Mumbai",
               ["search_auctions"]),
    GoldenCase("edge", "Flats in Chennai under 1000 rupees",
               ["search_auctions"]),
    GoldenCase("edge", "Auctions conducted by the Bank of Narnia",
               ["search_auctions"]),
    GoldenCase("edge", "Show me the details of auction id 999999999",
               ["get_auction_detail"]),
    GoldenCase("edge", "Which auctions does borrower Walter White have?",
               ["search_auctions"]),

    # ─── Refusal / out-of-scope (Rule 4) ────────────────────────────────
    # No tool performs these; a passing answer declines gracefully instead of
    # fabricating data or promising an action the platform can't take. The
    # track/save/alert requests must point the user at the Save button.
    GoldenCase("refusal", "Track this auction and alert me before the deadline",
               expect_refusal=True, refusal_required_any=["save"]),
    GoldenCase("refusal", "Set up an alert for new auctions in Chennai",
               expect_refusal=True, refusal_required_any=["save"]),
    GoldenCase("refusal",
               "Are there any court cases or pending litigation against borrower XYZ Industries?",
               expect_refusal=True, refusal_required_any=_DECLINE_MARKERS),
    GoldenCase("refusal", "What is the current market value of properties in Anna Nagar?",
               expect_refusal=True, refusal_required_any=_DECLINE_MARKERS),
    GoldenCase("refusal", "Give me the credit score and repayment history of borrower XYZ Industries",
               expect_refusal=True, refusal_required_any=_DECLINE_MARKERS),
]


EXPECTED_INTENTS: set[str] = {
    "basic_filter", "aggregation", "multi_hop", "schema",
    "specific_auction", "semantic", "temporal", "superlative", "reauction",
    "borrower", "off_graph", "edge", "refusal",
}
