"""
evals/agent3_cases.py
---------------------
The eval catalogue for agent3's tools, run against the LIVE graph.

`evals/cases.py` scores an agent's tool trajectory. This scores something
different and, for these two tools, more important: whether the values coming
out of the graph are right, and whether the *shape* of a result can be
misread. There is no model in the loop — a failure here is a data or tool bug,
not a prompting one, which is what makes it worth running on every change.

Four suites:

  capability      Questions the old tool surface cannot express at all. These
                  must return rows, or the new layer is not wired up.
  lot_facts       Specific values from the notice layer, checked against the
                  graph. Fixture-based: if a fixture listing disappears from
                  the graph the case SKIPS rather than fails, because that is
                  a data change, not a regression.
  scope_honesty   The gate. A lot fact from a multi-lot notice must never be
                  reachable as a per-property value. This is the failure mode
                  the notice layer introduces and nothing else catches it.
  gaps            What the notice omits must be named.

The scope_honesty invariant additionally runs over EVERY row every other case
produces — see `INVARIANTS`. A case that passes its own check while emitting
a scope violation still fails the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Fixture listings, verified against the live graph 21 Aug 2026.
#:   748779  1 lot, 714 sqft headline, physical possession, survey_old +
#:           survey_new + door_old + assessment_old + property_id, NO patta,
#:           NO encumbrance clause, NO EMD account on the notice.
#:   744314  2 lots, 3,359 and 7,040 sqft, survey 331/1 — the multi-lot case.
SINGLE_LOT_ID = "748779"
MULTI_LOT_ID = "744314"
MULTI_LOT_SURVEY = "331/1"


@dataclass
class Case:
    id: str
    suite: str
    #: The user question this case stands in for. Not executed — it is what
    #: makes a failure legible when someone reads the report a month later.
    question: str
    tool: str
    args: dict
    check: Callable[[dict], list[str]]
    #: When set, the case skips (rather than fails) if the fixture is gone.
    fixture: str | None = None
    tags: list[str] = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────

def _rows(result: dict) -> list[dict]:
    return result.get("rows") or []


def _prop(result: dict) -> dict | None:
    props = result.get("properties") or []
    return props[0] if props else None


def _needs_rows(minimum: int = 1) -> Callable[[dict], list[str]]:
    def check(result: dict) -> list[str]:
        if result.get("error"):
            return [f"tool returned an error: {result['error']}"]
        n = result.get("total_count", len(_rows(result)))
        if n < minimum:
            return [f"expected at least {minimum} match, got {n}"]
        return []
    return check


def _all(*checks: Callable[[dict], list[str]]) -> Callable[[dict], list[str]]:
    def check(result: dict) -> list[str]:
        out: list[str] = []
        for c in checks:
            out.extend(c(result))
        return out
    return check


# ── invariants: applied to every case's result ───────────────────────────

def scope_invariant(result: dict) -> list[str]:
    """A per-property measurement may only appear on a single-lot notice.

    This is the whole scope-honesty gate, expressed once. A notice covers 4.4
    lots on average and does not say which one the listing is, so a flat
    `area_sqft` on a multi-lot row is a claim the graph cannot support.
    """
    problems: list[str] = []
    for row in _rows(result):
        lots = row.get("notice_lot_count")
        if "area_sqft" in row and lots != 1:
            problems.append(
                f"{row.get('auction_id')}: per-property area_sqft on a "
                f"{lots}-lot notice")
        if row.get("area_sqft_scope") == "lot" and lots != 1:
            problems.append(
                f"{row.get('auction_id')}: scope 'lot' on a {lots}-lot notice")
    for prop in result.get("properties") or []:
        lots = prop.get("notice_lot_count")
        if "property" in prop and lots != 1:
            problems.append(
                f"{prop.get('auction_id')}: flat `property` block on a "
                f"{lots}-lot notice")
        if lots and lots > 1 and not prop.get("scope_note"):
            problems.append(
                f"{prop.get('auction_id')}: multi-lot notice with no scope_note")
    return problems


def no_invented_capability(result: dict) -> list[str]:
    """Nothing may surface a sold price or a market valuation.

    `Auction.outcome` is only ever "unsold" and there are no sale results in
    this graph. A key called `sold_price` appearing anywhere would mean an
    invented field, which is the likeliest hallucination this domain invites.
    """
    banned = {"sold_price", "sale_price", "market_value", "final_bid",
              "winning_bid", "sold_for"}
    found = _walk_keys(result) & banned
    return [f"result exposes a field this graph cannot support: {k}" for k in sorted(found)]


def _walk_keys(node: Any, depth: int = 0) -> set[str]:
    if depth > 8:
        return set()
    keys: set[str] = set()
    if isinstance(node, dict):
        keys |= set(node.keys())
        for v in node.values():
            keys |= _walk_keys(v, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            keys |= _walk_keys(v, depth + 1)
    return keys


INVARIANTS: tuple[Callable[[dict], list[str]], ...] = (
    scope_invariant, no_invented_capability,
)


# ── capability: questions the old surface cannot express ─────────────────

def _sqft_band_respected(lo: float, hi: float) -> Callable[[dict], list[str]]:
    def check(result: dict) -> list[str]:
        problems = _needs_rows(1)(result)
        for row in _rows(result):
            if row.get("notice_lot_count") != 1:
                continue  # a multi-lot range legitimately straddles the band
            size = row.get("area_sqft")
            if size is not None and not (lo <= size <= hi):
                problems.append(
                    f"{row['auction_id']}: single-lot notice measured {size} "
                    f"sqft, outside the requested {lo}-{hi}")
        return problems
    return check


CAPABILITY: list[Case] = [
    Case(
        id="cap_sqft_filter",
        suite="capability",
        question="plots over 2,000 sqft",
        tool="find_properties",
        args={"area_sqft_min": 2000, "upcoming_only": False, "limit": 10},
        check=_sqft_band_respected(2000, 500_000),
        tags=["extent"],
    ),
    Case(
        id="cap_sqft_band_upper",
        suite="capability",
        question="properties between 500 and 1,200 sqft",
        tool="find_properties",
        args={"area_sqft_min": 500, "area_sqft_max": 1200,
              "upcoming_only": False, "limit": 10},
        check=_sqft_band_respected(500, 1200),
        tags=["extent"],
    ),
    Case(
        id="cap_possession_physical",
        suite="capability",
        question="properties where the bank already has physical possession",
        tool="find_properties",
        args={"possession": "physical", "upcoming_only": False, "limit": 5},
        check=_all(_needs_rows(1),
                   lambda r: ([] if r.get("scope_notes")
                              else ["a notice-level filter produced no scope_notes"])),
        tags=["possession"],
    ),
    Case(
        id="cap_reauction",
        suite="capability",
        question="properties that failed to sell at an earlier auction",
        tool="find_properties",
        args={"reauction_only": True, "upcoming_only": False, "limit": 10},
        check=_all(
            _needs_rows(1),
            lambda r: [f"{x['auction_id']}: attempt {x.get('auction_attempt')} "
                       f"is not a re-auction"
                       for x in _rows(r)
                       if (x.get("auction_attempt") or 2) < 2],
        ),
        tags=["reauction"],
    ),
    Case(
        id="cap_road_width",
        suite="capability",
        question="plots on a road at least 30 feet wide",
        tool="find_properties",
        args={"road_width_ft_min": 30, "upcoming_only": False, "limit": 5},
        check=_needs_rows(1),
        tags=["access"],
    ),
    Case(
        id="cap_encumbrance_note",
        suite="capability",
        question="notices that spell out an encumbrance",
        tool="find_properties",
        args={"has_encumbrance_note": True, "upcoming_only": False, "limit": 5},
        check=_needs_rows(1),
        tags=["encumbrance"],
    ),
    Case(
        id="cap_outstanding",
        suite="capability",
        question="properties where the secured loan outstanding is under 50 lakh",
        tool="find_properties",
        args={"outstanding_max": 5_000_000, "upcoming_only": False, "limit": 5},
        check=_needs_rows(1),
        tags=["loan"],
    ),
    Case(
        id="cap_combined",
        suite="capability",
        question=("residential land in Coimbatore over 2,000 sqft with "
                  "physical possession under 60 lakh"),
        tool="find_properties",
        args={"city": "Coimbatore", "asset_category": "Residential",
              "property_type": "plot", "area_sqft_min": 2000,
              "possession": "physical", "reserve_price_max": 6_000_000,
              "upcoming_only": False, "limit": 10},
        check=_needs_rows(1),
        tags=["combined"],
    ),
    Case(
        id="cap_group_by_possession",
        suite="capability",
        question="how do auctions split by possession type",
        tool="find_properties",
        args={"group_by": "possession", "upcoming_only": False},
        check=lambda r: ([] if len(r.get("distribution") or []) >= 2
                         else ["expected at least two possession buckets"]),
        tags=["breakdown"],
    ),
    Case(
        id="cap_refine_present_on_broad_search",
        suite="capability",
        question="show me residential auctions (deliberately broad)",
        tool="find_properties",
        args={"asset_category": "Residential", "upcoming_only": False, "limit": 5},
        check=_all(
            _needs_rows(50),
            lambda r: ([] if r.get("refine")
                       else ["a broad search returned no refine options, so the "
                             "agent has nothing to narrow with and will re-search"]),
        ),
        tags=["refine"],
    ),
]


# ── lot_facts: specific values ───────────────────────────────────────────

LOT_FACTS: list[Case] = [
    Case(
        id="fact_single_lot_extent",
        suite="lot_facts",
        question=f"how big is auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if (_prop(r) or {}).get("property", {}).get("headline_sqft") == 714.0
            else [f"expected headline_sqft 714.0, got "
                  f"{(_prop(r) or {}).get('property', {}).get('headline_sqft')!r}"]),
        tags=["extent"],
    ),
    Case(
        id="fact_survey_numbers_present",
        suite="lot_facts",
        question=f"what is the survey number for auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if any(i.get("kind", "").startswith("survey")
                      for i in (_prop(r) or {}).get("property", {}).get("identifiers", []))
            else ["no survey identifier returned for a lot that has two"]),
        tags=["identifiers"],
    ),
    Case(
        id="fact_possession_reported",
        suite="lot_facts",
        question=f"does the bank have possession of auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if ((_prop(r) or {}).get("property", {}).get("possession") or {}
                   ).get("type") == "physical"
            else ["expected physical possession"]),
        tags=["possession"],
    ),
    Case(
        id="fact_boundaries_have_sides",
        suite="lot_facts",
        question=f"what are the boundaries of auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if {b.get("side") for b in
                   (_prop(r) or {}).get("property", {}).get("boundaries", [])}
            >= {"north", "south", "east", "west"}
            else ["expected all four boundary sides"]),
        tags=["boundaries"],
    ),
    Case(
        id="fact_identifier_lookup_finds_the_listing",
        suite="lot_facts",
        question=f"is survey number {MULTI_LOT_SURVEY} in any auction notice",
        tool="find_properties",
        args={"identifier": MULTI_LOT_SURVEY, "identifier_kind": "survey_new",
              "upcoming_only": False, "limit": 20},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if MULTI_LOT_ID in {x["auction_id"] for x in _rows(r)}
            else [f"survey {MULTI_LOT_SURVEY} did not surface {MULTI_LOT_ID}; "
                  f"got {[x['auction_id'] for x in _rows(r)][:5]}"]),
        tags=["identifiers"],
    ),
    Case(
        id="fact_auction_terms_present",
        suite="lot_facts",
        question=f"what is the bid increment for auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if (_prop(r) or {}).get("property", {}).get("auctions")
            else ["no auction terms returned"]),
        tags=["bidding"],
    ),
]


# ── scope_honesty: the gate ──────────────────────────────────────────────

SCOPE_HONESTY: list[Case] = [
    Case(
        id="scope_multi_lot_has_no_property_block",
        suite="scope_honesty",
        question=f"how big is auction {MULTI_LOT_ID}",
        tool="get_property",
        args={"auction_ids": MULTI_LOT_ID, "depth": "full"},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if ("property" not in (_prop(r) or {})
                   and (_prop(r) or {}).get("scope") == "notice")
            else ["a 2-lot notice exposed a flat `property` block — the agent "
                  "can now state one lot's size as the property's own"]),
        tags=["scope"],
    ),
    Case(
        id="scope_multi_lot_reports_a_range",
        suite="scope_honesty",
        question=f"how big is auction {MULTI_LOT_ID}",
        tool="get_property",
        args={"auction_ids": MULTI_LOT_ID, "depth": "full"},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if ((_prop(r) or {}).get("notice_summary", {}).get("sqft_range")
                   == [3359.0, 7040.0])
            else [f"expected sqft_range [3359.0, 7040.0], got "
                  f"{(_prop(r) or {}).get('notice_summary', {}).get('sqft_range')!r}"]),
        tags=["scope"],
    ),
    Case(
        id="scope_multi_lot_carries_the_sentence",
        suite="scope_honesty",
        question=f"how big is auction {MULTI_LOT_ID}",
        tool="get_property",
        args={"auction_ids": MULTI_LOT_ID, "depth": "full"},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if "2 lots" in ((_prop(r) or {}).get("scope_note") or "")
            else ["multi-lot notice did not carry a usable scope_note"]),
        tags=["scope"],
    ),
    Case(
        id="scope_single_lot_is_allowed_to_be_definite",
        suite="scope_honesty",
        question=f"how big is auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if ((_prop(r) or {}).get("scope") == "lot"
                   and "scope_note" not in (_prop(r) or {}))
            else ["a single-lot notice was hedged as notice-scoped, which "
                  "makes the agent vague where it could be exact"]),
        tags=["scope"],
    ),
    Case(
        id="scope_search_rows_never_overclaim",
        suite="scope_honesty",
        question="properties over 2,000 sqft (a mixed single/multi-lot set)",
        tool="find_properties",
        args={"area_sqft_min": 2000, "upcoming_only": False, "limit": 40},
        # the invariant does the work; this case exists to run it over a
        # deliberately wide, mixed result set
        check=lambda r: _needs_rows(5)(r),
        tags=["scope"],
    ),
    Case(
        id="scope_notice_filters_are_declared",
        suite="scope_honesty",
        question="properties with physical possession over 1,000 sqft",
        tool="find_properties",
        args={"possession": "physical", "area_sqft_min": 1000,
              "upcoming_only": False, "limit": 10},
        check=lambda r: (
            [] if len(r.get("scope_notes") or []) >= 2
            else ["two notice-level filters were applied but not both declared "
                  "in scope_notes"]),
        tags=["scope"],
    ),
]


# ── gaps: what the notice does not say ───────────────────────────────────

def _gap_mentions(*needles: str) -> Callable[[dict], list[str]]:
    def check(result: dict) -> list[str]:
        gaps = " ".join((_prop(result) or {}).get("gaps") or []).lower()
        return [f"gaps did not mention {n!r}: {gaps[:200]!r}"
                for n in needles if n.lower() not in gaps]
    return check


GAPS: list[Case] = [
    Case(
        id="gap_missing_patta",
        suite="gaps",
        question=f"what is the patta number for auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=_gap_mentions("patta"),
        tags=["identifiers"],
    ),
    Case(
        id="gap_encumbrance_unstated_not_absent",
        suite="gaps",
        question=f"is auction {SINGLE_LOT_ID} free of encumbrances",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=_gap_mentions("unstated"),
        tags=["encumbrance"],
    ),
    Case(
        id="gap_missing_emd_account",
        suite="gaps",
        question=f"where do I pay the EMD for auction {SINGLE_LOT_ID}",
        tool="get_property",
        args={"auction_ids": SINGLE_LOT_ID, "depth": "full"},
        fixture=SINGLE_LOT_ID,
        check=_gap_mentions("emd account"),
        tags=["bidding"],
    ),
    Case(
        id="gap_unknown_id_is_not_answered",
        suite="gaps",
        question="tell me about auction ZZZ-NOT-REAL",
        tool="get_property",
        args={"auction_ids": "ZZZ-NOT-REAL"},
        check=lambda r: (
            [] if r.get("not_found") == ["ZZZ-NOT-REAL"] and not r.get("properties")
            else ["an unknown auction_id did not come back as not_found"]),
        tags=["refusal"],
    ),
    Case(
        id="gap_zero_result_names_a_filter_to_drop",
        suite="gaps",
        question="flats in Chennai under 1 lakh with physical possession",
        tool="find_properties",
        args={"city": "Chennai", "property_type": "flat",
              "reserve_price_max": 100_000, "possession": "physical",
              "upcoming_only": False, "limit": 10},
        check=lambda r: (
            [] if (r.get("total_count") != 0 or r.get("relax") or r.get("hint"))
            else ["a zero result carried neither relax nor hint, so the agent "
                  "has nothing to offer but a dead end"]),
        tags=["zero"],
    ),
]


ALL_CASES: list[Case] = [*CAPABILITY, *LOT_FACTS, *SCOPE_HONESTY, *GAPS]

#: Gates, from docs/auction-deep-agent-2026-08.md §9. scope_honesty is 100%
#: because a scope violation is a confidently wrong answer, not a miss.
GATES: dict[str, float] = {
    "capability": 0.90,
    "lot_facts": 0.90,
    "scope_honesty": 1.00,
    "gaps": 0.90,
}
