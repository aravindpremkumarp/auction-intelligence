# Cross-Portal Matcher: One-to-One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sources.match` pair a portal listing with at most one listing per other portal, so batch sales stop merging distinct properties and the gap report's "new" count becomes exact.

**Architecture:** Keep the bucket (bank + auction day, price within tolerance) but replace "emit every pair in the bucket" with the notice-lot matcher's rules from `pipeline/apply_extractions.py::match_lots_to_listings`: evidence tiers *narrow* the candidates and must reach exactly one; a tie stays unresolved; identifiers shared by more than one listing on a side are not evidence; two listings choosing the same partner are both dropped. Identical postings of one unit on the same portal are grouped first so a duplicate posting does not make its unit number look shared. `find_same_listing_pairs` keeps its signature (callers unchanged); a new `match_listings` also returns the unresolved listings, which `scripts/gap_report.py` counts as `undecided` instead of `new`.

**Tech Stack:** Python 3.14, pytest, rapidfuzz (already a pipeline dependency). No Neo4j in any test.

**Spec:** `docs/superpowers/specs/2026-09-12-source-adapters-design.md` § "Matching — `sources/match.py`" (this plan changes that section in Task 5). Evidence for the change, measured on 2026-09-14 against the live graph (6,327 listings) and the first harvest (BAANKNET 658, bankeauctions 190):

- 648 CONFIRMED/PROBABLE pairs formed 45 clusters holding 2+ listings from the same portal (163 listings) — batch sales linked all-to-all. Example: Futuristic Global Resources, villas 18/19/24/28, one cluster of 11 listings.
- Identifier matches on short generic values (`same flat number g1`) paired different borrowers.
- `bucket_only` pairs (INFERRED) paired different borrowers in different cities in every sample.

## Global Constraints

- Only listings from *different* sources are ever paired; a graph listing is never compared with another graph listing of the same source.
- Grades stay as `pipeline/match_confidence.py::SAME_LISTING_CONFIDENCE` has them: `notice_bytes`/`boundaries` CONFIRMED, `identifier`/`borrower` PROBABLE, `bucket_only` INFERRED. `METHODS` does not change.
- `Pair` fields and `find_same_listing_pairs(incoming, existing) -> list[Pair]` keep their shape — `scripts/link_listings.py`, `scripts/build_spine.py` and `api/canonical.py` read them unchanged.
- Pairs are sorted by `METHODS` order, then `a_id`, then `b_id`.
- All matching code stays pure: no network, no graph, no config imports.
- Run every test with the main checkout's interpreter from this worktree: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest … -q -p no:cacheprovider` (a bare `pytest tests/` also collects `tests/e2e`, which skips the whole session without Razorpay secrets — always name the test files).

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `sources/match.py` | identifier extraction; the matcher (`match_listings`, `find_same_listing_pairs`, `Ambiguity`, `MatchResult`) | 1, 2, 3 |
| `scripts/gap_report.py` | graph candidates read text identifiers too; report counts `undecided` | 1, 4 |
| `scripts/audit_listing_links.py` (new) | offline audit: which strong-pair clusters hold 2+ listings of one portal | 5 |
| `tests/sources/test_match.py` | matcher and extractor tests | 1, 2, 3 |
| `tests/sources/test_gap_report.py` | report tests | 1, 4 |
| `tests/scripts/test_link_listings_and_events.py` | link_listings on a batch sale | 2 |
| `tests/scripts/test_audit_listing_links.py` (new) | audit cluster logic | 5 |
| `docs/superpowers/specs/2026-09-12-source-adapters-design.md` | matching section describes the new rules | 5 |

---

### Task 1: Unit identifiers the batch-sale cases need

Villas and two-letter flat numbers (`Villa No.18`, `Flat FF1`) are what separate the properties of one batch sale; today's extractor reads neither. Graph listings that already have lot identifiers never read their description, so their unit numbers are invisible too.

**Files:**
- Modify: `sources/match.py` (`_FAMILY`, `_IDENT`, `extract_identifiers`)
- Modify: `scripts/gap_report.py` (`graph_candidate`)
- Test: `tests/sources/test_match.py`, `tests/sources/test_gap_report.py`

**Interfaces:**
- Produces: `extract_identifiers(text) -> set[tuple[str, str]]` may now return family `"villa"`; values may be one or two letters followed by digits (`"ff1"`). `graph_candidate(rec)` returns the union of lot identifiers and text identifiers.

- [ ] **Step 1: Write the failing tests**

Add these cases to the `@pytest.mark.parametrize` list of `test_extract_identifiers` in `tests/sources/test_match.py`:

```python
    # Futuristic Global Resources (bn-353994 / 855475): villas separate a batch sale
    ("Residential Villa at Phoenix The Village Residential Villa No.18,Fabiola Block", {("villa", "18")}),
    # Gunasekaran (bn-350805 / 853781): "Flat FF1" has no "No." and a two-letter unit
    ("All the piece and parcel of Residential Flat FF1 measuring 1100 Sq.ft. in First Floor", {("flat", "ff1")}),
    # an area after "flat" is not a flat number
    ("2 BHK flat 1100 sq.ft in the first floor", set()),
```

Add to `tests/sources/test_gap_report.py`:

```python
def test_graph_candidate_reads_unit_numbers_from_text_even_with_lot_identifiers():
    rec = {"auction_id": "855475", "bank": "Indian Bank", "reserve_price_num": 13500000.0,
           "auction_start_dt": "2026-09-28T11:00:00Z", "borrower": "M/s Futuristic Global Resources Private Limited",
           "description": "Property No.1: All that piece and parcel of Villa No.18 having super built up area of 2705 Sq.ft",
           "identifiers": [["survey_old", "123/4"]], "lot_bounds": [], "boundaries": {}}
    assert gr.graph_candidate(rec).identifiers == {("survey", "123/4"), ("villa", "18")}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/sources/test_gap_report.py -q -p no:cacheprovider`
Expected: FAIL — the villa and FF1 cases return `set()`; the graph candidate lacks `("villa", "18")`.

- [ ] **Step 3: Implement**

In `sources/match.py`, add the villa family:

```python
_FAMILY = {"survey_old": "survey", "survey_new": "survey", "survey": "survey",
           "door_old": "door", "door_new": "door", "door": "door",
           "plot": "plot", "flat": "flat", "villa": "villa"}
```

Replace `_IDENT` (flat and villa take an optional "No."; a value may start with two letters; a value followed by an area unit is not an identifier):

```python
_IDENT = re.compile(
    r"\b(?P<kind>"
    r"(?:old\s+|new\s+|t\.?\s*s\.?\s*|r\.?\s*s\.?\s*|re-?survey\s*|survey\s+|s\.?\s*)no\.?s?"
    r"|(?:old\s+|new\s+)?(?:door|d\.?)\s*no\.?s?"
    r"|plot\s+no\.?s?"
    r"|flat(?:\s+no\.?s?)?"
    r"|villa(?:\s+no\.?s?)?"
    r")\s*[:.\-]?\s*"
    r"(?P<value>(?:\d+[A-Za-z]?|[A-Za-z]{1,2}\d+)(?:\s*(?:/|by|-)\s*\d*[A-Za-z]?\d*)*)"
    r"(?![\d.]*\s*(?:sq|sft|cents?\b|acres?\b))",
    re.IGNORECASE,
)
```

In `extract_identifiers`, route the villa kind before the others:

```python
        kind = m.group("kind").lower()
        if "villa" in kind:
            fam = "villa"
        elif "plot" in kind:
            fam = "plot"
        elif "flat" in kind:
            fam = "flat"
        elif kind.startswith("d") or "door" in kind:
            fam = "door"
        else:
            fam = "survey"
```

In `scripts/gap_report.py`, replace the body of `graph_candidate`:

```python
def graph_candidate(rec: dict) -> Candidate:
    """The matcher's view of a graph listing: lot-level boundaries where the
    pipeline has produced them, else whatever the listing's description
    states; identifiers from both the lot and the description, because lot
    extraction reads survey and door numbers but a batch sale is told apart
    by the villa or flat number only the description quotes."""
    text = rec.get("description") or ""
    cand = candidate_from_graph({**rec, "boundaries": graph_boundaries(rec) or extract_boundaries(text)})
    cand.identifiers |= extract_identifiers(text)
    return cand
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/sources/test_gap_report.py tests/scripts/test_link_listings_and_events.py -q -p no:cacheprovider`
Expected: PASS (all existing cases too — `"S.No 3 in the village"` still yields `set()`).

- [ ] **Step 5: Commit**

```bash
git add sources/match.py scripts/gap_report.py tests/sources/test_match.py tests/sources/test_gap_report.py
git commit -m "match: read villa and two-letter flat numbers; graph candidates keep text identifiers"
```

---

### Task 2: One partner per listing — narrowing tiers, unresolved ties, rivalry gate

**Files:**
- Modify: `sources/match.py` (the `# ── the matcher` section, lines from `METHODS = …` to the end of `find_same_listing_pairs`; module docstring)
- Test: `tests/sources/test_match.py`, `tests/scripts/test_link_listings_and_events.py` (its batch case changes meaning)

**Interfaces:**
- Consumes: `extract_identifiers` from Task 1.
- Produces:
  - `@dataclass(frozen=True) class Ambiguity: auction_id: str; source: str; other_source: str; candidates: tuple[str, ...]; reason: str` — `reason` is `"tied"` or `"contested"`.
  - `@dataclass class MatchResult: pairs: list[Pair]; ambiguous: list[Ambiguity]`
  - `match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> MatchResult`
  - `find_same_listing_pairs(incoming, existing) -> list[Pair]` (now `match_listings(...).pairs`)
  - `_twin_groups(side: list[Candidate]) -> list[list[Candidate]]` and `_representative(group: list[Candidate]) -> Candidate` — Task 3 replaces `_twin_groups`' body.

- [ ] **Step 1: Write the failing tests**

In `tests/sources/test_match.py`, change the import to:

```python
from sources.match import (
    Candidate, boundary_matches, candidate_from_graph, candidate_from_row, day_of,
    extract_boundaries, extract_identifiers, find_same_listing_pairs, match_listings, normalize_identifier_value,
)
```

Replace `test_two_new_portals_match_each_other_but_the_graph_never_matches_itself` and `test_pairs_are_sorted_strongest_first_and_unique` with:

```python
def test_two_new_portals_match_each_other_but_the_graph_never_matches_itself():
    inc = [
        candidate_from_row(_row("bn-7", "baanknet", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="R. Suresh")),
        candidate_from_row(_row("be-7", "bankeauctions", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Mr. R Suresh")),
    ]
    ext = [
        candidate_from_graph(_graph("910001", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Suresh R")),
        candidate_from_graph(_graph("910002", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Suresh R")),
    ]
    result = match_listings(inc, ext)
    assert [(p.a_id, p.b_id, p.method) for p in result.pairs] == [("bn-7", "be-7", "borrower")]
    # two graph listings the evidence cannot tell apart: neither portal listing is guessed onto one
    assert {(a.auction_id, a.other_source, a.candidates, a.reason) for a in result.ambiguous} == {
        ("bn-7", "eauctionsindia", ("910001", "910002"), "tied"),
        ("be-7", "eauctionsindia", ("910001", "910002"), "tied"),
    }


def test_evidence_narrows_the_bucket_to_one_partner():
    inc = [candidate_from_row(_row("bn-1", "baanknet", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))]
    ext = [candidate_from_graph(_graph("1", bank="Indian Bank", reserve=100000.0, day="2026-09-25")),
           candidate_from_graph(_graph("2", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))]
    result = match_listings(inc, ext)
    assert [(p.b_id, p.method) for p in result.pairs] == [("2", "borrower")]
    assert result.ambiguous == []


GUNASEKARAN_FF1 = "All the piece and parcel of Residential Flat FF1 measuring 1100 Sq.ft. in First Floor, S.No 31/2"
GUNASEKARAN_FF2 = "All the piece and parcel of Residential Flat FF2 measuring 1100 Sq.ft. in First Floor, S.No 31/2"
SELVARANI_PLOT = "All that piece and parcel of land Plot-A, measuring 1657 sq.ft., S.No 31/2"


def test_a_survey_number_the_whole_batch_shares_is_not_evidence():
    """853780/853781/855959 and bn-350805/bn-350808 (Indian Bank, 2026-09-16):
    one survey number under a borrower's two flats and a neighbour's plot. The
    flat number and the borrower decide; the shared survey number linked
    Selvarani's plot to Gunasekaran's flats before."""
    inc = [
        candidate_from_row(_row("bn-350805", "baanknet", bank="Indian Bank", reserve=2700000.0, day="2026-09-16",
                                borrower="GUNASEKARAN", text=GUNASEKARAN_FF1)),
        candidate_from_row(_row("bn-350808", "baanknet", bank="Indian Bank", reserve=2680000.0, day="2026-09-16",
                                borrower="SELVARANI", text=SELVARANI_PLOT)),
    ]
    ext = [
        candidate_from_graph({**_graph("853781", bank="Indian Bank", reserve=2700000.0, day="2026-09-16", borrower="Mr. R. Gunasekaran"),
                              "identifiers": [["flat", "ff1"], ["survey_old", "31/2"]]}),
        candidate_from_graph({**_graph("855959", bank="Indian Bank", reserve=2700000.0, day="2026-09-16", borrower="Mr. R. Gunasekaran"),
                              "identifiers": [["flat", "ff2"], ["survey_old", "31/2"]]}),
        candidate_from_graph({**_graph("853780", bank="Indian Bank", reserve=2680000.0, day="2026-09-16", borrower="Mrs. S. Selvarani"),
                              "identifiers": [["survey_old", "31/2"]]}),
    ]
    result = match_listings(inc, ext)
    assert [(p.a_id, p.b_id, p.method, p.evidence) for p in result.pairs] == [
        ("bn-350805", "853781", "identifier", "same flat number ff1"),
        ("bn-350808", "853780", "borrower", "borrower 'SELVARANI' ~ 'Mrs. S. Selvarani'"),
    ]


def test_a_short_unit_number_needs_the_borrower_to_agree():
    """bn-356042 / 863627: "Flat No.G1" on both, different borrowers — G1 is on
    half the blocks in Chennai. Without corroboration it is not identity; the
    bucket alone still says 'possibly the same' (INFERRED, never merged)."""
    inc = [candidate_from_row(_row("bn-356042", "baanknet", bank="Canara Bank", reserve=3850000.0, day="2026-09-22",
                                   borrower="ANANDHAN SRINIVASAN", text="Residential Flat Flat No.G1, Ground Floor Annamalai Nagar"))]
    ext = [candidate_from_graph({**_graph("863627", bank="Canara Bank", reserve=3860000.0, day="2026-09-22", borrower="Mrs. V.Monisha"),
                                 "identifiers": [["flat", "g1"]]})]
    [p] = find_same_listing_pairs(inc, ext)
    assert (p.method, p.confidence) == ("bucket_only", INFERRED)

    agreeing = [candidate_from_graph({**_graph("840387", bank="Canara Bank", reserve=3850000.0, day="2026-09-22",
                                                borrower="Mr. Anandhan Srinivasan"), "identifiers": [["flat", "g1"]]})]
    [p] = find_same_listing_pairs(inc, agreeing)
    assert (p.method, p.evidence) == ("identifier", "same flat number g1")


def test_two_listings_choosing_one_partner_are_both_left_unresolved():
    """A batch sale of two properties on BAANKNET against one eauctionsindia
    listing: one of them is it, the other is not, and nothing says which."""
    inc = [
        candidate_from_row(_row("bn-1", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="N Mariappan")),
        candidate_from_row(_row("bn-2", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="N Mariappan")),
    ]
    ext = [candidate_from_graph(_graph("841207", bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="Mr. N. Mariappan"))]
    result = match_listings(inc, ext)
    assert result.pairs == []
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-1", ("841207",), "contested"), ("bn-2", ("841207",), "contested")]


def test_a_batch_the_evidence_cannot_separate_is_tied_not_linked_all_to_all():
    inc = [candidate_from_row(_row("bn-353991", "baanknet", bank="Indian Bank", reserve=13500000.0, day="2026-09-28",
                                   borrower="FUTURISTIC GLOBAL RESOURCES PRIVATE LIMITED"))]
    ext = [candidate_from_graph(_graph(aid, bank="Indian Bank", reserve=13500000.0, day="2026-09-28",
                                       borrower="M/s Futuristic Global Resources Private Limited")) for aid in ("855475", "855476")]
    result = match_listings(inc, ext)
    assert result.pairs == []
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [("bn-353991", ("855475", "855476"), "tied")]


def test_a_listing_both_sides_already_hold_is_one_candidate():
    """After the load, a harvested row is also in the graph under the same id."""
    row = candidate_from_row(_row("bn-9", "baanknet", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))
    graph_copy = candidate_from_graph(_graph("bn-9", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B", source="baanknet"))
    ea = candidate_from_graph(_graph("7", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))
    assert [(p.a_id, p.b_id) for p in find_same_listing_pairs([row], [graph_copy, ea])] == [("bn-9", "7")]
```

In `tests/scripts/test_link_listings_and_events.py`, replace `test_find_pairs_compares_the_whole_graph_across_sources` (its bn-1/bn-2 are one borrower's batch against one graph listing — exactly what must no longer pair) with:

```python
def test_find_pairs_compares_the_whole_graph_across_sources():
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"),
                           _rec("be-1", "bankeauctions", bank="Indian Bank")])
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in pairs] == [("bn-1", "841207", "borrower", "PROBABLE")]
    rows = ll.pair_rows(pairs + [Pair("x", "y", "baanknet", "baanknet", "borrower", "PROBABLE")])
    assert len(rows) == 1 and {"a_id", "b_id", "method", "confidence", "evidence"} == set(rows[0])
    assert "PROBABLE  borrower     1" in ll.summarize(pairs)


def test_find_pairs_writes_nothing_for_a_batch_sale_against_one_listing():
    """bn-1 and bn-2 are one borrower's two properties; 841207 is one of them."""
    assert ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"), _rec("bn-2", "baanknet")]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/scripts/test_link_listings_and_events.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'match_listings'` in test_match.py; `test_find_pairs_writes_nothing_for_a_batch_sale_against_one_listing` fails (today's matcher links bn-1 and bn-2 both to 841207).

- [ ] **Step 3: Implement**

In `sources/match.py`, change the imports at the top:

```python
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable
```

Replace the module docstring's paragraph that starts "Two listings are only ever compared…" through the grade table with:

```python
"""Cross-portal matching: is this incoming listing an auction we already hold?

Pure functions over dicts — no network, no graph. The caller (``scripts/gap_report.py``
and ``scripts/link_listings.py``) fetches what the graph knows and hands it in
beside the harvested rows.

Two listings are only ever compared across *different* sources, inside a
bucket: canonical bank (``pipeline.entity_resolution.org_key``) and auction
calendar day, with reserve prices agreeing within
``pipeline.price_agreement.TOLERANCE_PCT`` (or one side unpriced).

A bucket routinely holds a same-day batch sale — one borrower, several
properties, often one price (BAANKNET ``bn-351743`` / ``bn-351740``). So the
rules are the notice-lot matcher's (``pipeline.apply_extractions.match_lots_to_listings``):

* each listing gets at most ONE partner per other portal; evidence narrows the
  candidates, strongest first, and must reach exactly one;
* a tie that survives every tier stays unresolved (``Ambiguity``), never guessed;
* an identifier held by more than one listing on either side is shared ground
  (the land under a batch), not evidence; a short unit number (``G1``) counts
  only when the borrower agrees or a second identifier does;
* two listings choosing the same partner are both left unresolved;
* identical postings of one unit on one portal are one candidate.

    notice_bytes   the same sale-notice file on both sides       CONFIRMED
    boundaries     three of four neighbours agree                CONFIRMED
    identifier     the same unique survey / door / plot / flat / villa number   PROBABLE
    borrower       the same party (token_set_ratio >= 90)        PROBABLE
    bucket_only    the one price-agreeing listing in the bucket  INFERRED
"""
```

Replace everything from `# ── the matcher` to the end of the file with:

```python
# ── the matcher ──────────────────────────────────────────────────────────────

METHODS = ("notice_bytes", "boundaries", "identifier", "borrower", "bucket_only")

#: Identifier families that name one unit rather than the land a batch shares.
UNIT_FAMILIES = frozenset({"villa", "flat", "plot", "door"})


@dataclass(frozen=True)
class Pair:
    a_id: str
    b_id: str
    a_source: str
    b_source: str
    method: str
    confidence: str
    evidence: str = ""


@dataclass(frozen=True)
class Ambiguity:
    """A listing the evidence could not pin to one partner on another portal.

    ``tied``: several candidates survived every tier. ``contested``: it chose
    a partner another listing chose too. Either way nothing is written, and
    the gap report counts it as undecided rather than new."""
    auction_id: str
    source: str
    other_source: str
    candidates: tuple[str, ...]
    reason: str


@dataclass
class MatchResult:
    pairs: list[Pair]
    ambiguous: list[Ambiguity]


def price_verdict(a: Candidate, b: Candidate) -> str:
    """``agree`` (within tolerance), ``unknown`` (a side has no price — the
    portal writes 0 or nothing for "not published"), or a disagreement."""
    verdict, _ = compare_prices(a.reserve_price_num if a.has_price else None,
                                b.reserve_price_num if b.has_price else None)
    return verdict


def _is_short(value: str) -> bool:
    """``g1``, ``22``, ``s1``: common enough across a city to need corroboration."""
    return len(value.replace("/", "")) <= 2


def _unique_identifiers(side: list[Candidate]) -> set[tuple[str, str]]:
    """Identifiers held by exactly one listing on this side of a bucket."""
    counts = Counter(i for c in side for i in c.identifiers)
    return {i for i, n in counts.items() if n == 1}


def _twin_groups(side: list[Candidate]) -> list[list[Candidate]]:
    """One group per listing."""
    return [[c] for c in side]


def _representative(group: list[Candidate]) -> Candidate:
    """The group as one candidate: the first member, carrying every member's
    identifiers and notice fingerprints."""
    first = group[0]
    if len(group) == 1:
        return first
    return replace(first,
                   identifiers=set().union(*(c.identifiers for c in group)),
                   doc_shas=set().union(*(c.doc_shas for c in group)))


def _notice_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    out = {}
    for pos, b in enumerate(cands):
        shared = a.doc_shas & b.doc_shas
        if shared:
            out[pos] = f"same notice file sha256 {sorted(shared)[0][:12]}…"
    return out


def _boundary_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    return {pos: "three or more boundary neighbours agree"
            for pos, b in enumerate(cands) if boundary_matches(a.boundaries, b.boundaries)}


def _identifier_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    out = {}
    for pos, b in enumerate(cands):
        ids = a.identifiers & b.identifiers & usable
        corroborated = len(ids) >= 2 or borrower_matches(a.borrower, b.borrower)
        ids = {i for i in ids if corroborated or not _is_short(i[1])}
        if ids:
            fam, val = sorted(ids)[0]
            out[pos] = f"same {fam} number {val}"
    return out


def _borrower_tier(a: Candidate, cands: list[Candidate], usable: set) -> dict[int, str]:
    return {pos: f"borrower '{a.borrower.strip()}' ~ '{b.borrower.strip()}'"
            for pos, b in enumerate(cands) if borrower_matches(a.borrower, b.borrower)}


_TIERS = (("notice_bytes", _notice_tier), ("boundaries", _boundary_tier),
          ("identifier", _identifier_tier), ("borrower", _borrower_tier))


def _choose(a: Candidate, side_b: list[Candidate], usable: set) -> tuple | None:
    """Narrow ``side_b`` to ``a``'s partner.

    Returns ``("match", index, method, evidence)``, ``("tied", indexes)`` or
    ``None``. A tier that hits every remaining candidate says nothing about
    which one ``a`` is and is skipped; one that hits some of them narrows."""
    idx = [i for i, b in enumerate(side_b) if price_verdict(a, b) in ("agree", "unknown")]
    if not idx:
        return None
    method, evidence = None, {}
    for name, tier in _TIERS:
        hits = tier(a, [side_b[i] for i in idx], usable)
        if not hits or (len(hits) == len(idx) and len(idx) > 1):
            continue
        evidence = {idx[pos]: why for pos, why in hits.items()}
        idx = sorted(evidence)
        method = name
        if len(idx) == 1:
            break
    if method is None:
        agreeing = [i for i in idx if price_verdict(a, side_b[i]) == "agree"]
        if len(agreeing) == 1:
            return ("match", agreeing[0], "bucket_only", "same bank, reserve price and auction day only")
        return ("tied", agreeing) if len(agreeing) > 1 else None
    if len(idx) == 1:
        return ("match", idx[0], method, evidence[idx[0]])
    return ("tied", idx)


def _assign(side_a: list[Candidate], side_b: list[Candidate], new_ids: set[str]) -> tuple[list[Pair], list[Ambiguity]]:
    """One portal against another inside one bucket."""
    groups_a, groups_b = _twin_groups(side_a), _twin_groups(side_b)
    reps_a = [_representative(g) for g in groups_a]
    reps_b = [_representative(g) for g in groups_b]
    usable = _unique_identifiers(reps_a) & _unique_identifiers(reps_b)

    def unresolved(group_a: list[Candidate], candidate_groups: list[list[Candidate]], reason: str) -> list[Ambiguity]:
        others = tuple(sorted(y.auction_id for g in candidate_groups for y in g))
        return [Ambiguity(x.auction_id, x.source, candidate_groups[0][0].source, others, reason)
                for x in group_a
                if x.auction_id in new_ids or any(y in new_ids for y in others)]

    chosen: dict[int, tuple[int, str, str]] = {}
    ambiguous: list[Ambiguity] = []
    for ia, a in enumerate(reps_a):
        got = _choose(a, reps_b, usable)
        if got is None:
            continue
        if got[0] == "tied":
            ambiguous += unresolved(groups_a[ia], [groups_b[i] for i in got[1]], "tied")
        else:
            chosen[ia] = got[1:]

    by_partner: dict[int, list[int]] = defaultdict(list)
    for ia, (ib, _, _) in chosen.items():
        by_partner[ib].append(ia)

    pairs: list[Pair] = []
    for ib, rivals in sorted(by_partner.items()):
        if len(rivals) > 1:
            for ia in rivals:
                ambiguous += unresolved(groups_a[ia], [groups_b[ib]], "contested")
            continue
        _, method, why = chosen[rivals[0]]
        for x in groups_a[rivals[0]]:
            for y in groups_b[ib]:
                if x.auction_id in new_ids or y.auction_id in new_ids:
                    pairs.append(Pair(x.auction_id, y.auction_id, x.source, y.source,
                                      method, listing_confidence_for(method), why))
    return pairs, ambiguous


def match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> MatchResult:
    """Every cross-source pair the evidence can pin one-to-one, and every
    listing it could not. ``incoming`` is compared against ``existing`` and
    against itself (two new portals can both carry an auction the graph has
    never seen); ``existing`` is never compared with itself — that is
    ``link_reauctions``' job. A listing present on both sides (a loaded
    harvest row) is taken once, from ``incoming``."""
    seen: set[str] = set()
    members_by_bucket: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    new_ids: set[str] = set()
    for is_new, pool in ((True, incoming), (False, existing)):
        for c in pool:
            if not c.auction_id or c.auction_id in seen:
                continue
            seen.add(c.auction_id)
            if is_new:
                new_ids.add(c.auction_id)
            if c.bucket_key is not None:
                members_by_bucket[c.bucket_key].append(c)

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    for key in sorted(members_by_bucket):
        members = members_by_bucket[key]
        sources = sorted({c.source for c in members})
        for i, source_a in enumerate(sources):
            for source_b in sources[i + 1:]:
                side_a = [c for c in members if c.source == source_a]
                side_b = [c for c in members if c.source == source_b]
                if not any(c.auction_id in new_ids for c in side_a + side_b):
                    continue
                got_pairs, got_ambiguous = _assign(side_a, side_b, new_ids)
                pairs += got_pairs
                ambiguous += got_ambiguous
    order = {m: i for i, m in enumerate(METHODS)}
    pairs.sort(key=lambda p: (order[p.method], p.a_id, p.b_id))
    ambiguous.sort(key=lambda x: (x.auction_id, x.other_source))
    return MatchResult(pairs, ambiguous)


def find_same_listing_pairs(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> list[Pair]:
    """The pairs of :func:`match_listings`, strongest first."""
    return match_listings(incoming, existing).pairs
```

Delete the old `evidence_for` function (its tiers now live in `_notice_tier` … `_borrower_tier`); first confirm nothing else imports it:

Run: `rg -n "evidence_for" --glob "*.py"`
Expected: only `sources/match.py` (the definition being deleted).

- [ ] **Step 4: Run tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/scripts/test_link_listings_and_events.py tests/pipeline/test_match_confidence.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sources/match.py tests/sources/test_match.py tests/scripts/test_link_listings_and_events.py
git commit -m "match: one partner per listing — narrowing tiers, unresolved ties, rivalry gate"
```

---

### Task 3: Identical postings of one unit are one candidate

eauctionsindia posts some batch notices twice (Futuristic Global: villas 18/19/24 as `855475-77` and again as `855589-91`). After Task 2 the duplicate makes villa 18 look shared, so the BAANKNET villa-18 listing is left tied. Group postings with the same unit numbers, price and borrower before assigning.

**Files:**
- Modify: `sources/match.py` (`_twin_groups`)
- Test: `tests/sources/test_match.py`

**Interfaces:**
- Consumes: `_twin_groups`, `_representative`, `UNIT_FAMILIES`, `borrower_key`, `_round_reserve` from Task 2 / existing module.
- Produces: `_twin_groups(side) -> list[list[Candidate]]` groups listings with equal `(unit identifiers, rounded reserve, borrower_key)`; listings without unit identifiers stay alone.

- [ ] **Step 1: Write the failing test**

```python
def test_duplicate_postings_of_one_villa_pair_with_its_portal_listing():
    """Futuristic Global Resources, Indian Bank, 2026-09-28: BAANKNET lists
    villas 18 and 19 once each; eauctionsindia posted the notice twice."""
    def villa(aid, source, n, text_prefix="Residential Villa No."):
        return _row(aid, source, bank="Indian Bank", reserve=13500000.0, day="2026-09-28",
                    borrower="FUTURISTIC GLOBAL RESOURCES PRIVATE LIMITED", text=f"{text_prefix}{n}, Fabiola Block")
    inc = [candidate_from_row(villa("bn-353994", "baanknet", 18)), candidate_from_row(villa("bn-353991", "baanknet", 19))]
    ext = [candidate_from_graph({**_graph(aid, bank="Indian Bank", reserve=13500000.0, day="2026-09-28",
                                          borrower="M/s Futuristic Global Resources Private Limited"),
                                 "identifiers": [["villa", str(n)]]})
           for aid, n in (("855475", 18), ("855589", 18), ("855476", 19), ("855590", 19))]
    result = match_listings(inc, ext)
    assert sorted((p.a_id, p.b_id, p.method) for p in result.pairs) == [
        ("bn-353991", "855476", "identifier"), ("bn-353991", "855590", "identifier"),
        ("bn-353994", "855475", "identifier"), ("bn-353994", "855589", "identifier"),
    ]
    assert result.ambiguous == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py::test_duplicate_postings_of_one_villa_pair_with_its_portal_listing -q -p no:cacheprovider`
Expected: FAIL — `result.pairs == []`, both BAANKNET villas tied.

- [ ] **Step 3: Implement**

Replace `_twin_groups` in `sources/match.py`:

```python
def _twin_groups(side: list[Candidate]) -> list[list[Candidate]]:
    """Listings on one portal that are postings of the same unit — equal unit
    numbers (villa / flat / plot / door), reserve price and borrower — as one
    group, in first-seen order. A listing that quotes no unit number is its
    own group: without one there is nothing to say two postings are one unit."""
    out: list[list[Candidate]] = []
    by_key: dict[tuple, list[Candidate]] = {}
    for c in side:
        units = frozenset(i for i in c.identifiers if i[0] in UNIT_FAMILIES)
        if not units:
            out.append([c])
            continue
        key = (units, _round_reserve(c.reserve_price_num), borrower_key(c.borrower))
        if key not in by_key:
            by_key[key] = []
            out.append(by_key[key])
        by_key[key].append(c)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/pipeline/test_match_confidence.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sources/match.py tests/sources/test_match.py
git commit -m "match: duplicate postings of one unit are one candidate"
```

---

### Task 4: Gap report counts undecided listings

**Files:**
- Modify: `scripts/gap_report.py` (imports, `build_report`, `format_report`, `main`)
- Modify: `tests/sources/test_gap_report.py`

**Interfaces:**
- Consumes: `match_listings`, `MatchResult`, `Ambiguity` from Task 2.
- Produces: `build_report(rows_by_source, existing, pairs, ambiguous: Iterable[Ambiguity] = ()) -> dict`; each source gains `"undecided": int`; the report gains `"ambiguous": list[dict]`. For every source, `already_loaded + new + undecided + matched == rows`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/sources/test_gap_report.py` (extend the existing `from sources.match import …` line with `Ambiguity`):

```python
def test_undecided_listings_are_neither_new_nor_matched():
    rows = {"baanknet": [BN_359826, {**BN_359826, "auction_id": "bn-2"}]}
    ambiguous = [Ambiguity("bn-359826", "baanknet", "eauctionsindia", ("1", "2"), "tied")]
    rep = gr.build_report(rows, [], [], ambiguous)
    s = rep["sources"]["baanknet"]
    assert (s["rows"], s["new"], s["undecided"], s["matched"]) == (2, 1, 1, 0)
    assert rep["ambiguous"] == [{"auction_id": "bn-359826", "source": "baanknet", "other_source": "eauctionsindia",
                                 "candidates": ("1", "2"), "reason": "tied"}]
    assert "undecided 1" in gr.format_report(rep)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_gap_report.py -q -p no:cacheprovider`
Expected: FAIL — `test_undecided_listings_are_neither_new_nor_matched` with `TypeError: build_report() takes 3 positional arguments but 4 were given`; every other test in the file passes.

- [ ] **Step 3: Implement**

In `scripts/gap_report.py`, change the matcher import:

```python
from sources.match import (  # noqa: E402
    SIDES, Ambiguity, Candidate, Pair, candidate_from_graph, candidate_from_row, extract_boundaries,
    extract_extent, extract_identifiers, match_listings,
)
```

Change `build_report`'s signature, docstring and bookkeeping:

```python
def build_report(rows_by_source: dict[str, list[dict]], existing: list[dict], pairs: list[Pair],
                 ambiguous: Iterable[Ambiguity] = ()) -> dict:
    """Pure: the per-source numbers from harvested rows, the graph's records
    and the matcher's pairs. ``pairs`` must be sorted strongest first, as
    ``match_listings`` returns them. A listing with no pair but an
    ``Ambiguity`` is ``undecided``: the evidence found candidates it could not
    choose between, so it is neither new nor matched."""
    ambiguous = list(ambiguous)
    undecided_ids = {a.auction_id for a in ambiguous}
```

In the same function, set the report's top level to:

```python
    report: dict = {"sources": {}, "pairs": [p.__dict__ for p in pairs],
                    "ambiguous": [a.__dict__ for a in ambiguous]}
```

Add `undecided = 0` next to `already = new = matched = 0`, and replace the `if p is None:` block with:

```python
            if p is None:
                if aid in undecided_ids:
                    undecided += 1
                    continue
                new += 1
                new_complete[sum(mine.values())] += 1
                photos_new += mine["has_photos"]
                continue
```

Add `"undecided": undecided,` after `"new": new,` in the per-source dict.

Add `from typing import Iterable` to the imports at the top of `scripts/gap_report.py`.

In `format_report`, replace the first `lines.append(f"  rows …")` call with:

```python
        lines.append(f"  rows {s['rows']}  already loaded {s['already_loaded']}  new {s['new']}  undecided {s['undecided']}"
                     f"  matched {s['matched']}"
                     f"  (CONFIRMED {g['CONFIRMED']}, PROBABLE {g['PROBABLE']}, INFERRED {g['INFERRED']}"
                     f"{', ambiguous ' + str(s['ambiguous_inferred']) if s['ambiguous_inferred'] else ''})")
```

In `main`, replace the two matcher lines:

```python
    result = match_listings(incoming, [graph_candidate(r) for r in existing])
    report = build_report(rows_by_source, existing, result.pairs, result.ambiguous)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/scripts/test_link_listings_and_events.py tests/pipeline/test_match_confidence.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/gap_report.py tests/sources/test_gap_report.py
git commit -m "gap_report: count listings the matcher could not decide as undecided, not new"
```

---

### Task 5: Audit on the real harvest, exact new count, spec

**Files:**
- Create: `scripts/audit_listing_links.py`
- Create: `tests/scripts/test_audit_listing_links.py`
- Modify: `docs/superpowers/specs/2026-09-12-source-adapters-design.md` (§ Matching)

**Interfaces:**
- Consumes: the JSON `scripts/gap_report.py --json` writes (`pairs: [{a_id, b_id, a_source, b_source, method, confidence, evidence}]`); `api.canonical.BRIDGE_GRADES`.
- Produces: `same_source_clusters(pairs: list[dict]) -> list[list[str]]` — clusters (by CONFIRMED/PROBABLE pairs) holding 2+ listings of one source, largest first.

- [ ] **Step 1: Write the failing test**

`tests/scripts/test_audit_listing_links.py`:

```python
"""The audit that says whether strong pairs would merge distinct properties in the spine."""
from scripts.audit_listing_links import same_source_clusters


def _p(a, b, conf="PROBABLE"):
    src = lambda x: "baanknet" if x.startswith("bn-") else "eauctionsindia"
    return {"a_id": a, "b_id": b, "a_source": src(a), "b_source": src(b), "method": "borrower", "confidence": conf}


def test_only_strong_clusters_with_two_listings_of_one_portal_are_reported():
    pairs = [_p("bn-1", "10"), _p("bn-2", "10"),            # bn-1 and bn-2 would merge through 10
             _p("bn-3", "30"),                              # a clean one-to-one pair
             _p("bn-4", "40"), _p("bn-5", "40", "INFERRED")]  # INFERRED never merges
    assert same_source_clusters(pairs) == [["10", "bn-1", "bn-2"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/scripts/test_audit_listing_links.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.audit_listing_links'`.

- [ ] **Step 3: Implement**

`scripts/audit_listing_links.py`:

```python
"""
audit_listing_links.py — would the matcher's strong pairs merge distinct properties?

build_spine unions listings over CONFIRMED / PROBABLE :SAME_LISTING_AS edges.
A portal never lists one auction twice, so a cluster holding two listings of
the same portal is either duplicate postings of one unit or distinct
properties merged into one event. This prints every such cluster from a
gap report's JSON with each member's price, borrower and unit numbers, for a
person to tell which.

    python -m scripts.gap_report --existing-json graph.json --json gap.json
    python -m scripts.audit_listing_links gap.json --existing-json graph.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.canonical import BRIDGE_GRADES  # noqa: E402
from sources.match import UNIT_FAMILIES, extract_identifiers  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def same_source_clusters(pairs: list[dict]) -> list[list[str]]:
    """Clusters over strong pairs that hold 2+ listings of one source, each
    sorted, largest cluster first."""
    parent: dict[str, str] = {}
    source: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p.get("confidence") not in BRIDGE_GRADES:
            continue
        source[p["a_id"]], source[p["b_id"]] = p["a_source"], p["b_source"]
        parent[find(p["a_id"])] = find(p["b_id"])
    clusters: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        clusters[find(x)].append(x)
    out = [sorted(m) for m in clusters.values() if len({source[x] for x in m}) < len(m)]
    return sorted(out, key=lambda m: (-len(m), m))


def _members(data_dir: Path, existing_json: Path | None) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for path in sorted((data_dir / "listings").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                by_id[row["auction_id"]] = {"price": row.get("reserve_price_num"), "borrower": row.get("borrower_name"),
                                            "text": " ".join(t for t in (row.get("title"), row.get("description")) if t)}
    if existing_json:
        for rec in json.loads(existing_json.read_text(encoding="utf-8")):
            by_id.setdefault(rec["auction_id"], {"price": rec.get("reserve_price_num"), "borrower": rec.get("borrower"),
                                                 "text": rec.get("description") or ""})
    return by_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="gap_report --json output")
    ap.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    ap.add_argument("--existing-json", default=None, help="the graph listings the report was run against")
    args = ap.parse_args(argv)

    pairs = json.loads(Path(args.report).read_text(encoding="utf-8"))["pairs"]
    clusters = same_source_clusters(pairs)
    info = _members(Path(args.data_dir), Path(args.existing_json) if args.existing_json else None)
    print(f"clusters with 2+ listings of one portal: {len(clusters)}  (listings: {sum(len(c) for c in clusters)})")
    for members in clusters:
        print(f"== {len(members)} listings")
        for aid in members:
            m = info.get(aid, {})
            units = sorted(f"{f} {v}" for f, v in extract_identifiers(m.get("text")) if f in UNIT_FAMILIES)
            print(f"   {aid:<12} Rs {m.get('price')}  {m.get('borrower')!r}  units: {', '.join(units) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/scripts/test_audit_listing_links.py tests/sources tests/scripts/test_link_listings_and_events.py tests/pipeline/test_match_confidence.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Measure on the real harvest (read-only)**

The harvest data lives in the main checkout; run the worktree's code against it. If `graph.json` from 2026-09-14 is not at hand, make a fresh read-only snapshot first (needs the main checkout's `.env`).

```bash
MAIN=E:/01_vibe_coding/08_auction
OUT="${TMPDIR:-/tmp}/matcher-audit"; mkdir -p "$OUT"
cd "$MAIN" && .venv/Scripts/python.exe -m scripts.gap_report --save-existing "$OUT/graph.json" > /dev/null
```

```bash
cd E:/01_vibe_coding/08_auction/.claude/worktrees/local-github-sync-a3da7f
"$MAIN/.venv/Scripts/python.exe" -m scripts.gap_report --data-dir "$MAIN/data" --downloads-dir "$MAIN/downloads" \
  --existing-json "$OUT/graph.json" --json "$OUT/gap_after.json" | grep -E "^(baanknet|bankeauctions)$|  rows "
"$MAIN/.venv/Scripts/python.exe" -m scripts.audit_listing_links "$OUT/gap_after.json" --data-dir "$MAIN/data" --existing-json "$OUT/graph.json"
```

Baseline before this plan (2026-09-14): BAANKNET rows 658 → new 181, matched 477 (CONFIRMED 120 / PROBABLE 318 / INFERRED 39); bankeauctions rows 190 → new 75, matched 115 (27 / 85 / 3); 45 same-portal clusters holding 163 listings.

Expected after: for each portal `already loaded + new + undecided + matched == rows`; the audit lists only clusters whose same-portal members show the **same** unit numbers and price (duplicate postings). Any cluster with different unit numbers or prices is a matcher defect — stop and add a failing test for it before continuing. Record the new BAANKNET/bankeauctions `new` and `undecided` counts in the commit message.

- [ ] **Step 6: Update the spec's matching section**

In `docs/superpowers/specs/2026-09-12-source-adapters-design.md`, replace the lines from `- **Within a bucket**, strongest first:` through `one spine.` (the table and the "Trap" bullet) with:

```markdown
- **Within a bucket, one partner per listing per other portal** — the rules
  of `pipeline/apply_extractions.match_lots_to_listings`, measured on the
  2026-09-14 harvest where emitting every in-bucket pair merged 45 batch
  sales into single spine events. Evidence narrows the candidates, strongest
  first, and must reach exactly one:

  | method | evidence | confidence |
  |---|---|---|
  | `notice_bytes` | both sides' `:Document.content_sha256` equal | CONFIRMED |
  | `boundaries` | ≥3 of 4 boundary neighbours equal after normalisation | CONFIRMED |
  | `identifier` | a survey / door / plot / flat / villa number held by exactly one listing on each side; a short value (`G1`) only with the borrower agreeing | PROBABLE |
  | `borrower` | `token_set_ratio ≥ 90` (as `lot_resolution.py:47`) | PROBABLE |
  | `bucket_only` | the only price-agreeing candidate, nothing more | INFERRED |

- **Unresolved, never guessed:** a tie that survives every tier, or two
  listings choosing the same partner, produces no pair and an `Ambiguity`
  (`tied` / `contested`); the gap report counts it `undecided`.
- **Batch sales:** BAANKNET `bn-351743` / `bn-351740` — same borrower, bank,
  day and reserve, two properties. Same source ⇒ never matched; across
  sources the unit number decides or the listings stay unresolved. Postings
  of one unit repeated on one portal (same unit numbers, price, borrower)
  are one candidate. INFERRED is shown as "possibly the same", never merged.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/audit_listing_links.py tests/scripts/test_audit_listing_links.py docs/superpowers/specs/2026-09-12-source-adapters-design.md
```

Save the commit message as `commit_message.txt` in the worktree root, built from the two `  rows …` lines and the audit's first line that Step 5 printed, in exactly this form (numbers are the printed ones, never estimates):

```
audit_listing_links: flag strong-pair clusters that would merge distinct properties; spec: one-to-one matching

Measured on the 2026-09-14 harvest against 6,327 graph listings:
  baanknet       rows 658  new N  undecided N  matched N
  bankeauctions  rows 190  new N  undecided N  matched N
  same-portal strong clusters: N (all duplicate postings of one unit)
Before: baanknet new 181 matched 477; bankeauctions new 75 matched 115; 45 clusters.
```

```bash
git commit -F commit_message.txt && rm commit_message.txt
```
