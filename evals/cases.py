"""
evals/cases.py
--------------
The golden-question catalogue: every realistic question type we want the chat
agent to handle, grouped by intent, with the tool(s) it should call.

This module is intentionally **dependency-free** (stdlib only) so it can be
imported by both the offline pytest shape test (`tests/api/test_golden_questions.py`)
and the live `pydantic-evals` runner (`evals/run_golden.py`) without dragging in
pydantic-ai / Neo4j. It is the single source of truth for the catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Every tool the agent exposes (api/agent.py). `acceptable_tools` entries are
# validated against this set so a renamed/removed tool fails the shape test
# instead of silently never matching in the live eval.
KNOWN_TOOLS: set[str] = {
    "search_auctions",
    "semantic_property_search",
    "get_auction_detail",
    "select_properties",
    "describe_schema",
    "run_cypher",
}


@dataclass
class GoldenCase:
    intent: str
    question: str
    acceptable_tools: list[str] = field(default_factory=list)
    # When True, a passing answer must not leak an internal write-rejection /
    # error string to the user (the agent is read-only over the graph).
    must_not_mention_write_error: bool = True


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
    GoldenCase("aggregation", "What is the 95th percentile reserve price?",
               ["search_auctions", "run_cypher", "describe_schema"]),

    # ─── Multi-hop / novel ──────────────────────────────────────────────
    GoldenCase("multi_hop", "Banks with more than 50 auctions in Tamil Nadu",
               ["run_cypher", "search_auctions"]),
    GoldenCase("multi_hop", "Borrowers with multiple properties",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Areas where EMD is more than 15% of reserve on average",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Which bank has the lowest average reserve price in Chennai?",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Property types available in industrial asset category",
               ["search_auctions", "run_cypher", "describe_schema"]),
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
    GoldenCase("schema", "What cities do you have data for?",
               ["search_auctions"]),
    GoldenCase("schema", "List all banks in the database",
               ["search_auctions"]),
    GoldenCase("schema", "What property types are available?",
               ["search_auctions", "describe_schema"]),
    GoldenCase("schema", "What asset categories exist?",
               ["search_auctions", "describe_schema"]),
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
    GoldenCase("semantic", "Properties facing a main road",
               ["semantic_property_search"]),
    GoldenCase("semantic", "Plots with clear boundaries mentioned in the description",
               ["semantic_property_search"]),
    GoldenCase("semantic", "CMDA approved plots in Sriperumbudur",
               ["semantic_property_search", "search_auctions"]),
    GoldenCase("semantic", "Properties with a channel or paddy field nearby",
               ["semantic_property_search"]),

    # ─── Temporal ───────────────────────────────────────────────────────
    GoldenCase("temporal", "Auctions with deadline in the next 7 days",
               ["search_auctions"]),
    GoldenCase("temporal", "Auctions starting in Q1 2026 in Chennai",
               ["search_auctions"]),
    GoldenCase("temporal", "Auctions closing this week",
               ["search_auctions"]),

    # ─── Borrower lookup ────────────────────────────────────────────────
    GoldenCase("borrower", "Auctions tied to borrower XYZ Industries",
               ["search_auctions"]),

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
]


EXPECTED_INTENTS: set[str] = {
    "basic_filter", "aggregation", "multi_hop", "schema",
    "specific_auction", "semantic", "temporal", "borrower", "edge",
}
