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
#: A phrase unique to MULTI_LOT_ID's own notice text (verified live: exactly
#: {744314, 744316}, the two listings sharing that notice's Document).
MULTI_LOT_UNIQUE_PHRASE = "23 Cents 380"
#: Verified live: 802076 is linked by SAME_PROPERTY_AS (high confidence) to
#: 755956, whose reserve was Rs 45.58L against 802076's Rs 41L -- a real -10%
#: drop, the standard SARFAESI reduction after a failed auction.
REAUCTION_ID = "802076"


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


def _identifier_listings(result: dict) -> list[dict]:
    """Every listing row across every matched identifier value, flattened."""
    return [listing for m in (result.get("matches") or []) for listing in m.get("listings", [])]


def _notice_hits(result: dict) -> list[dict]:
    return result.get("results") or []


def _no_invented_valuation(result: dict) -> list[str]:
    """benchmark_price must never lose its not-market-value framing."""
    basis = (result.get("basis") or "").lower()
    if "not market value" not in basis:
        return ["a pricing result dropped the not-market-value basis"]
    return []


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
    # find_by_identifier: same rule, under matches[].listings[].
    for listing in _identifier_listings(result):
        lots = listing.get("notice_lot_count")
        if listing.get("scope") == "lot" and lots != 1:
            problems.append(
                f"{listing.get('auction_id')}: identifier match scoped 'lot' "
                f"on a {lots}-lot notice")
        if lots and lots > 1 and not listing.get("scope_note"):
            problems.append(
                f"{listing.get('auction_id')}: multi-lot identifier match "
                f"with no scope_note")
    # search_notices: a snippet from a multi-lot notice is not any one
    # listing's own description.
    for hit in _notice_hits(result):
        lots = hit.get("notice_lot_count")
        if hit.get("scope") == "lot" and lots != 1:
            problems.append(
                f"{hit.get('auction_id')}: notice-search hit scoped 'lot' "
                f"on a {lots}-lot notice")
        if lots and lots > 1 and not hit.get("scope_note"):
            problems.append(
                f"{hit.get('auction_id')}: multi-lot notice-search hit with "
                f"no scope_note")
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
    Case(
        id="cap_identifier_lookup_finds_known_survey",
        suite="capability",
        question=f"is survey number {MULTI_LOT_SURVEY} in any auction notice",
        tool="find_by_identifier",
        args={"value": MULTI_LOT_SURVEY},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if any(MULTI_LOT_ID == x["auction_id"] for x in _identifier_listings(r))
            else [f"survey {MULTI_LOT_SURVEY} did not surface {MULTI_LOT_ID} via "
                  f"find_by_identifier"]),
        tags=["identifiers"],
    ),
    Case(
        id="cap_search_notices_finds_free_text",
        suite="capability",
        question="notices that mention a borewell",
        tool="search_notices",
        args={"query": "borewell", "limit": 10},
        check=lambda r: (
            [] if r.get("result_count", 0) >= 1
            else ["expected at least one notice mentioning 'borewell'"]),
        tags=["free_text"],
    ),
    Case(
        id="cap_benchmark_price_single_lot",
        suite="capability",
        question=f"is auction {SINGLE_LOT_ID} priced well",
        tool="benchmark_price",
        args={"auction_id": SINGLE_LOT_ID},
        fixture=SINGLE_LOT_ID,
        check=_all(
            _no_invented_valuation,
            lambda r: ([] if r.get("priced") and r.get("comparisons")
                       else [f"expected a priced result with comparisons, got "
                             f"{r.get('reason')!r}"]),
        ),
        tags=["pricing"],
    ),
    Case(
        id="cap_reauction_history_finds_a_drop",
        suite="capability",
        question=f"has auction {REAUCTION_ID} been auctioned before",
        tool="reauction_history",
        args={"auction_id": REAUCTION_ID},
        fixture=REAUCTION_ID,
        check=lambda r: (
            [] if any(o.get("price_change") for o in r.get("earlier_listings") or [])
            else ["expected a linked earlier listing carrying a price change"]),
        tags=["reauction"],
    ),
    Case(
        id="cap_search_notices_and_not_or",
        suite="capability",
        question="notices mentioning a north-facing corner plot",
        tool="search_notices",
        args={"query": "north facing corner plot", "limit": 40},
        check=lambda r: (
            [] if r.get("result_count", 0) <= 20
            else [f"'north facing corner plot' returned {r.get('result_count')} "
                  f"results — bare terms are behaving like OR, not AND (verified "
                  f"live: AND-joined this phrase matches only 2 of 3,335 lots)"]),
        tags=["free_text"],
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
    Case(
        id="fact_identifier_lookup_returns_the_matched_kind",
        suite="lot_facts",
        question=f"what kind of number is {MULTI_LOT_SURVEY} in the notice",
        tool="find_by_identifier",
        args={"value": MULTI_LOT_SURVEY, "identifier_kind": "survey_new"},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if any(m["identifier_kind"] == "survey_new" for m in r.get("matches", []))
            else ["expected a survey_new-kinded match"]),
        tags=["identifiers"],
    ),
    Case(
        id="fact_search_notices_snippet_is_from_the_real_text",
        suite="lot_facts",
        question=f"what does the notice for auction {MULTI_LOT_ID} say about the extent",
        tool="search_notices",
        args={"query": MULTI_LOT_UNIQUE_PHRASE, "limit": 5},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if any(MULTI_LOT_ID == h["auction_id"] for h in _notice_hits(r))
            else [f"searching the notice's own text did not surface {MULTI_LOT_ID}"]),
        tags=["free_text"],
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
    Case(
        id="scope_identifier_match_on_multi_lot_notice_is_notice_scoped",
        suite="scope_honesty",
        question=f"which lot has survey number {MULTI_LOT_SURVEY}",
        tool="find_by_identifier",
        args={"value": MULTI_LOT_SURVEY, "identifier_kind": "survey_new"},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if next((x for x in _identifier_listings(r)
                       if x["auction_id"] == MULTI_LOT_ID), {}).get("scope") == "notice"
            else [f"{MULTI_LOT_ID} (a 2-lot notice) was not scoped 'notice' on "
                  f"an identifier match"]),
        tags=["scope"],
    ),
    Case(
        id="scope_search_notices_hit_on_multi_lot_notice_is_notice_scoped",
        suite="scope_honesty",
        question=f"what does the notice for {MULTI_LOT_ID} say",
        tool="search_notices",
        args={"query": MULTI_LOT_UNIQUE_PHRASE, "limit": 5},
        fixture=MULTI_LOT_ID,
        check=lambda r: (
            [] if next((h for h in _notice_hits(r)
                       if h["auction_id"] == MULTI_LOT_ID), {}).get("scope") == "notice"
            else [f"{MULTI_LOT_ID} (a 2-lot notice) was not scoped 'notice' on "
                  f"a text-search hit"]),
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
    Case(
        id="gap_unknown_identifier_is_a_graph_gap_not_a_denial",
        suite="gaps",
        question="is survey number 999999/ZZZZ-NOTREAL in any auction notice",
        tool="find_by_identifier",
        args={"value": "999999/ZZZZ-NOTREAL"},
        check=lambda r: (
            [] if (r.get("matches") == []
                   and "not that the property doesn't exist" in (r.get("hint") or ""))
            else ["a zero identifier match did not carry the graph-gap "
                  "framing, so the agent may report this as the property "
                  "not existing"]),
        tags=["refusal", "identifiers"],
    ),
    Case(
        id="gap_multi_lot_price_is_refused_with_a_reason",
        suite="gaps",
        question=f"what is auction {MULTI_LOT_ID} worth per square foot",
        tool="benchmark_price",
        args={"auction_id": MULTI_LOT_ID},
        fixture=MULTI_LOT_ID,
        check=_all(
            _no_invented_valuation,
            lambda r: ([] if (r.get("priced") is False
                              and "lots" in (r.get("reason") or ""))
                       else ["a multi-lot notice was priced per sqft, or "
                             "refused without naming the lot ambiguity — the "
                             "reserve is the listing's and the extent is a "
                             "lot's, so the division invents a number"]),
        ),
        tags=["pricing", "refusal"],
    ),
    Case(
        id="gap_first_time_listing_is_not_a_missing_history",
        suite="gaps",
        question=f"has auction {SINGLE_LOT_ID} been auctioned before",
        tool="reauction_history",
        args={"auction_id": SINGLE_LOT_ID},
        fixture=SINGLE_LOT_ID,
        check=lambda r: (
            [] if "No earlier attempt recorded" in (r.get("summary") or "")
            else ["a first-time listing did not say so plainly; it must not "
                  "read as missing history"]),
        tags=["reauction"],
    ),
    Case(
        id="gap_search_notices_zero_result_suggests_a_synonym",
        suite="gaps",
        question="notices mentioning zzznonsensewordzzz",
        tool="search_notices",
        args={"query": "zzznonsensewordzzz"},
        check=lambda r: (
            [] if (r.get("result_count") == 0 and "not semantic" in (r.get("hint") or ""))
            else ["a zero-hit free-text search did not explain that this is "
                  "literal matching, so the agent may over-conclude the "
                  "graph has nothing on the topic"]),
        tags=["refusal", "free_text"],
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
