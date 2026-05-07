"""Golden-question evaluation harness (career-ops `test-all.mjs` pattern).

This file is both:

1. A **catalogue** — every realistic question type we want the agent to
   handle, grouped by intent, with the tool(s) it should call. Offline
   `pytest` validates the catalogue shape so it stays well-formed as we
   extend it.

2. A **runnable live eval** — when the environment variable
   `RUN_LIVE_EVAL=1` is set, each question is sent through the real
   `/chat` stack (OpenRouter + Neo4j). We assert that at least one of the
   `acceptable_tools` was invoked. Pass rate is logged.

Nightly CI (`.github/workflows/golden.yml`) sets `RUN_LIVE_EVAL=1` and
posts regressions as PR comments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest


@dataclass
class GoldenCase:
    intent: str
    question: str
    acceptable_tools: list[str] = field(default_factory=list)
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
               ["run_cypher", "list_distinct"]),
    GoldenCase("aggregation", "Monthly auction volume in 2026",
               ["run_cypher"]),
    GoldenCase("aggregation", "Which cities have the most auctions?",
               ["list_distinct", "run_cypher"]),
    GoldenCase("aggregation", "What is the 95th percentile reserve price?",
               ["search_auctions", "run_cypher", "describe_schema"]),

    # ─── Multi-hop / novel ──────────────────────────────────────────────
    GoldenCase("multi_hop", "Banks with more than 50 auctions in Tamil Nadu",
               ["run_cypher", "bank_portfolio"]),
    GoldenCase("multi_hop", "Borrowers with multiple properties",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Areas where EMD is more than 15% of reserve on average",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Which bank has the lowest average reserve price in Chennai?",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Property types available in industrial asset category",
               ["list_distinct", "run_cypher", "describe_schema"]),
    GoldenCase("multi_hop", "Top 5 areas by auction count",
               ["list_distinct", "run_cypher"]),
    GoldenCase("multi_hop", "Cities that appear in both residential and commercial auctions",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Auctions where the borrower has more than one property",
               ["run_cypher"]),
    GoldenCase("multi_hop", "Banks active in Chennai sorted by portfolio size",
               ["run_cypher", "list_distinct"]),
    GoldenCase("multi_hop", "Which banks have the highest median reserve price?",
               ["run_cypher"]),

    # ─── Schema / enum discovery ────────────────────────────────────────
    GoldenCase("schema", "What cities do you have data for?",
               ["list_distinct"]),
    GoldenCase("schema", "List all banks in the database",
               ["list_distinct"]),
    GoldenCase("schema", "What property types are available?",
               ["list_distinct", "describe_schema"]),
    GoldenCase("schema", "What asset categories exist?",
               ["list_distinct", "describe_schema"]),
    GoldenCase("schema", "What fields does an auction property have?",
               ["describe_schema"]),

    # ─── Specific auction ───────────────────────────────────────────────
    GoldenCase("specific_auction", "Give me details for auction AUC-12345",
               ["get_auction_detail"]),
    GoldenCase("specific_auction", "What is the possession type for auction AUC-12345?",
               ["get_auction_detail"]),
    GoldenCase("specific_auction", "Find properties similar to AUC-12345",
               ["find_similar_properties"]),
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
               ["upcoming_auctions"]),
    GoldenCase("temporal", "Auctions starting in Q1 2026 in Chennai",
               ["search_auctions"]),
    GoldenCase("temporal", "Auctions closing this week",
               ["upcoming_auctions"]),

    # ─── Borrower lookup ────────────────────────────────────────────────
    GoldenCase("borrower", "Auctions tied to borrower XYZ Industries",
               ["borrower_lookup"]),
]


def test_catalogue_well_formed() -> None:
    """Validates the catalogue structure so additions stay consistent."""
    assert len(GOLDEN) >= 40
    intents = {c.intent for c in GOLDEN}
    expected_intents = {
        "basic_filter", "aggregation", "multi_hop", "schema",
        "specific_auction", "semantic", "temporal", "borrower",
    }
    assert expected_intents.issubset(intents)

    for c in GOLDEN:
        assert c.question.strip(), "question must be non-empty"
        assert c.acceptable_tools, f"{c.question!r} has no acceptable_tools"
        for t in c.acceptable_tools:
            assert t in {
                "search_auctions", "find_similar_properties", "bank_portfolio",
                "location_analysis", "upcoming_auctions", "price_comparison",
                "borrower_lookup", "semantic_property_search",
                "get_auction_detail", "list_distinct", "describe_schema",
                "run_cypher",
            }, f"unknown tool {t!r} on {c.question!r}"


# ─── Live-eval block — only runs when RUN_LIVE_EVAL=1 ──────────────────
# The live run is intentionally kept out of `pytest` default runs because
# it hits OpenRouter + Neo4j Aura. Run manually with:
#
#     RUN_LIVE_EVAL=1 pytest tests/api/test_golden_questions.py -v
#
# CI runs it on a schedule via .github/workflows/golden.yml.

LIVE = os.environ.get("RUN_LIVE_EVAL") == "1"


@pytest.mark.skipif(not LIVE, reason="RUN_LIVE_EVAL=1 to enable live OpenRouter+Neo4j eval")
@pytest.mark.parametrize("case", GOLDEN, ids=[c.question[:60] for c in GOLDEN])
def test_golden_live(case: GoldenCase) -> None:
    """Live end-to-end: runs the question through the agent and asserts the
    trajectory includes at least one acceptable tool."""
    import asyncio

    from api.agent import ChatDeps, agent  # imports real agent

    async def _run() -> list[str]:
        result = await agent.run(case.question, deps=ChatDeps())
        tools_called: list[str] = []
        for msg in result.all_messages():
            for part in getattr(msg, "parts", []):
                tool = getattr(part, "tool_name", None)
                if tool:
                    tools_called.append(tool)
        return tools_called

    tools_called = asyncio.run(_run())
    assert any(t in case.acceptable_tools for t in tools_called), (
        f"{case.question!r} called {tools_called}, "
        f"none matched acceptable {case.acceptable_tools}"
    )
