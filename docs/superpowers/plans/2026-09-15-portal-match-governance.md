# Portal-Match Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Link a BAANKNET / bankeauctions listing to a listing we hold only when bank, auction day, reserve price and borrower all agree on exactly one listing; send every partial agreement to a human review queue whose verdicts are stored, undoable and applied on every linking run.

**Architecture:** `sources/match.py` becomes a four-field rule that returns CONFIRMED links and PENDING review pairs plus one `Ambiguity` per subject waiting for a person. Human verdicts are `(:ResolutionDecision {kind:'portal-match'})` nodes (existing review machinery); the matcher reads them as a pure input. `scripts/link_listings.py` runs a safety stop before writing, picks a daily spot-check sample, and stores the review rows on `(:PipelineState {key:'link_listings'})`; the review API serves those rows (minus decided ones) and `web/review.html` renders them as the side-by-side check table the user chose.

**Tech Stack:** Python 3.14, pytest, rapidfuzz, FastAPI + pydantic, Neo4j (Cypher via `api.neo4j_client`), vanilla JS in `web/review.html`.

**Spec:** `docs/superpowers/specs/2026-09-15-portal-match-governance-design.md`

Rulings this plan makes against the spec (each is also applied to the spec in Task 7):
1. **Queue source.** The spec says the queue runs `match_listings` over the graph on request. That fetch reads every listing (~7k, ~1 minute), too slow for a page load. Instead `link_listings` — the same code that writes the links — stores the review rows, and the API filters out decided rows at read time (the pattern the bank-merge queue already uses with `proposals_json`). A new `--queue-only` flag fills the queue without writing links, so review can start before pipeline step 5.
2. **Safety check 3.** "confirmed + review + new equals subjects per source" is ill-defined when one listing is compared against two other sources. It becomes: *no listing is both CONFIRMED-linked and in review against the same other source.*
3. **Test example.** "Shylaja K" vs "Mrs Sailaja.K" does **not** pass `borrower_matches` (it was among the measured bucket-only pairs), so the CONFIRMED example is "N MARIAPPAN" vs "Mr. N. Mariappan"; Shylaja is not used.
4. **Display fields.** Instead of adding `emd_num`, `title`, `description`, `city` as `Candidate` attributes, a single `info: dict` carries display-only fields the rule never reads.
5. **Decision validation.** The spec asks that every linked/rejected id be "a current candidate of the subject". Knowing the current candidates needs a full graph fetch and match at decide time; the endpoint instead checks that the subject and every id exist. Safe because the matcher only ever applies a decision's ids to listings in the subject's own bank + day bucket — an id from elsewhere is simply never used.
6. **Stale-decision footer.** The spec's "N stale decisions" footer count is omitted: a stale decision already shows as its case reappearing in the queue, which is the only thing a reviewer acts on.

## Global Constraints

- Two listings are compared only when they come from different sources, share `org_key(bank)` and the same auction calendar day (`day_of`).
- Reserve price agrees only when both are present and `abs(a - b) < 1` (rupee exact). Borrower agrees only via `sources.match.borrower_matches`.
- The subject of a comparison is the listing from the better-ranked source per `sources.base.SOURCE_RANK` (baanknet 1, bankeauctions 2, eauctionsindia 3; an unlisted source ranks 99, ties broken by name).
- Grades: `four_fields`, `unit_number`, `decision` → CONFIRMED; `review` → PENDING. `build_spine` (`MERGE_GRADES`) and `api/canonical` (`BRIDGE_GRADES`) stay untouched and merge only CONFIRMED/PROBABLE.
- Review reasons (exact strings): `batch`, `units_disagree`, `price_only`, `borrower_only`, `split`, `contested`.
- A `portal-match` decision key is `portal-match:{subject_id}:{other_source}` (one decision per subject per other source — one queue row); its payload is `{subject_id, other_source, linked_ids, rejected_ids, snapshot, note?}` where the server, not the client, sets `snapshot`. `portal_decisions` and `match_listings(decisions=)` key verdicts by `(subject_id, other_source)`. (Ruling during Task 2 review.)
- A decision is ignored when its `snapshot` differs from the subject's current `snapshot_of` (bank key, rounded reserve, borrower key, auction day).
- Spot-check: 10 CONFIRMED `four_fields`/`unit_number` subjects without a decision, sampled with `random.Random(run_date.isoformat())`.
- `find_same_listing_pairs(incoming, existing) -> list[Pair]` and the `Pair` fields keep their shape.
- Matching code in `sources/match.py` and `pipeline/resolution_review.py` stays pure: no network, no graph, no config.
- Run tests only with `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest <named files> -q -p no:cacheprovider -o addopts=""` from the worktree; never a bare `pytest tests/` (tests/e2e skips the session).
- Commit messages end with a blank line and `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Never amend, never push.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `sources/match.py` | the four-field rule, decisions applied as pure input, `snapshot_of` | 1, 2 |
| `pipeline/match_confidence.py` | `SAME_LISTING_CONFIDENCE` vocabulary + `PENDING` | 1 |
| `pipeline/resolution_review.py` | `portal-match` kind, key, `portal_decisions` | 2 |
| `api/review/queries.py` | validate + store `portal-match` verdicts; serve stored review rows | 2, 5 |
| `api/review/router.py` | `portal-match` in the decide Literal; `PortalMatchRow` models | 2, 5 |
| `scripts/gap_report.py` | confirmed / review / new counts; fetch display fields | 3, 4 |
| `scripts/link_listings.py` | safety stop, spot-check, review rows, `--queue-only` | 4 |
| `web/review.html` | "Portal matches — same property?" panel (layout A) | 6 |
| `docs/superpowers/specs/2026-09-15-portal-match-governance-design.md` | rulings 1–4 | 7 |
| tests: `tests/sources/test_match.py`, `tests/pipeline/test_match_confidence.py`, `tests/scripts/test_link_listings_and_events.py`, `tests/sources/test_gap_report.py`, `tests/pipeline/test_resolution_review.py`, `tests/api/test_review_resolution.py` | | 1–5 |

---

### Task 1: The four-field rule and its grades

**Files:**
- Modify: `sources/match.py` (module docstring; `Candidate`; `candidate_from_row`; `candidate_from_graph`; everything from `# ── the matcher` to the end)
- Modify: `pipeline/match_confidence.py` (`SAME_LISTING_CONFIDENCE`, new `PENDING`)
- Modify: `tests/sources/test_match.py`, `tests/pipeline/test_match_confidence.py`, `tests/scripts/test_link_listings_and_events.py`, `tests/sources/test_gap_report.py`

**Interfaces:**
- Produces:
  - `pipeline.match_confidence.PENDING = "PENDING"`
  - `sources.match.METHODS = ("decision", "four_fields", "unit_number", "review")`
  - `sources.match.REVIEW_REASONS = ("batch", "units_disagree", "price_only", "borrower_only", "split", "contested")`
  - `Candidate.info: dict` (display only: `title`, `description`, `city`, `district`, `url`, `emd`, `public_url`)
  - `price_equal(a: Candidate, b: Candidate) -> bool`
  - `Ambiguity(auction_id, source, other_source, candidates: tuple[str, ...], reason)`; `MatchResult(pairs, ambiguous)`
  - `match_listings(incoming, existing) -> MatchResult`; `find_same_listing_pairs(incoming, existing) -> list[Pair]`
  - Pair orientation: `a_id` is always the subject (better-ranked source).

- [ ] **Step 1: Write the failing tests**

In `tests/sources/test_match.py`, change the imports at the top to:

```python
import pytest

from pipeline.match_confidence import CONFIRMED, PENDING
from sources.match import (
    Candidate, boundary_matches, candidate_from_graph, candidate_from_row, day_of,
    extract_boundaries, extract_identifiers, find_same_listing_pairs, match_listings, normalize_identifier_value,
)
```

Replace everything from the line `# ── the matcher ─` to the end of the file, **except** keep `test_unit_key_normalises_notation` (move it into the new block unchanged), with:

```python
# ── the matcher ─────────────────────────────────────────────────────────────

def _bn(aid, *, bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="N MARIAPPAN", text="", source="baanknet"):
    return candidate_from_row(_row(aid, source, bank=bank, reserve=reserve, day=day, borrower=borrower, text=text))


def _ea(aid, *, bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="Mr. N. Mariappan", identifiers=()):
    return candidate_from_graph(_graph(aid, bank=bank, reserve=reserve, day=day, borrower=borrower, identifiers=identifiers))


def test_bank_day_price_and_borrower_on_one_listing_is_confirmed():
    [p] = find_same_listing_pairs([_bn("bn-359826")], [_ea("841207")])
    assert (p.a_id, p.b_id, p.method, p.confidence) == ("bn-359826", "841207", "four_fields", CONFIRMED)


def test_price_agrees_but_borrower_differs_waits_for_a_person():
    """ARR Tex (bn-359756 / 853518): same price, 'A R R TEX' vs 'M/s ARR Tex'."""
    result = match_listings(
        [_bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")],
        [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")])
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in result.pairs] == [("bn-359756", "853518", "review", PENDING)]
    assert [(a.auction_id, a.other_source, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-359756", "eauctionsindia", ("853518",), "price_only")]


def test_borrower_agrees_but_price_differs_waits_for_a_person():
    result = match_listings([_bn("bn-1")], [_ea("841207", reserve=4700000.0)])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-1", "borrower_only")]


def test_a_batch_sale_no_unit_number_separates_waits_for_a_person():
    """Ekadanta Enterprises (bn-359636): three of ours agree on all four."""
    ours = [_ea(aid, bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="M/s. Ekadanta Enterprises")
            for aid in ("842118", "842546", "844968")]
    result = match_listings([_bn("bn-359636", bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15",
                                 borrower="Ekadanta Enterprises", text="Sy.No.788/2, Dry. Ext. Hec. 0.40.5")], ours)
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-359636", ("842118", "842546", "844968"), "batch")]
    assert {p.confidence for p in result.pairs} == {PENDING} and len(result.pairs) == 3


def test_a_unit_number_that_picks_one_listing_settles_a_batch():
    ours = [_ea("856500", identifiers=[("flat", "f3")]), _ea("856501", identifiers=[("flat", "f4")])]
    [p] = find_same_listing_pairs([_bn("bn-1", text="Residential Flat No. F3, Second Floor")], ours)
    assert (p.b_id, p.method, p.confidence) == ("856500", "unit_number", CONFIRMED)
    assert "same flat number 3" in p.evidence


def test_all_four_agree_but_plot_numbers_differ_waits_for_a_person():
    result = match_listings([_bn("bn-352470", text="Plot No. 45, S.No. 73/7")],
                            [_ea("866338", identifiers=[("plot", "44"), ("plot", "47")])])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-352470", "units_disagree")]


def test_two_portal_listings_claiming_one_of_ours_are_contested():
    result = match_listings([_bn("bn-1"), _bn("bn-2")], [_ea("841207")])
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-1", ("841207",), "contested"), ("bn-2", ("841207",), "contested")]
    assert all(p.confidence == PENDING for p in result.pairs)


def test_price_on_one_listing_and_borrower_on_another_is_split():
    result = match_listings([_bn("bn-1")], [_ea("1", borrower="Mr. Haridas P"), _ea("2", reserve=5000000.0)])
    assert [(a.candidates, a.reason) for a in result.ambiguous] == [(("1", "2"), "split")]


def test_nothing_agreeing_is_new():
    result = match_listings([_bn("bn-1")], [_ea("1", reserve=100000.0, borrower="Mr. Haridas P")])
    assert result.pairs == [] and result.ambiguous == []


def test_another_bank_or_day_is_never_compared():
    result = match_listings([_bn("bn-1")], [_ea("1", bank="Canara Bank"), _ea("2", day="2026-09-25")])
    assert result.pairs == [] and result.ambiguous == []


def test_a_missing_price_never_agrees():
    result = match_listings([_bn("bn-1", reserve=None)], [_ea("1", reserve=None)])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-1", "borrower_only")]


def test_same_source_listings_are_never_compared():
    assert match_listings([_bn("bn-1"), _bn("bn-2")], []).pairs == []


def test_the_graph_is_never_matched_with_itself():
    assert match_listings([], [_ea("1"), _ea("2")]).pairs == []


def test_duplicate_postings_of_one_unit_link_together():
    ours = [_ea(aid, identifiers=[("villa", "18")]) for aid in ("855475", "855589")]
    pairs = find_same_listing_pairs([_bn("bn-353994", text="Residential Villa No.18, Fabiola Block")], ours)
    assert sorted((p.b_id, p.method) for p in pairs) == [("855475", "four_fields"), ("855589", "four_fields")]


def test_the_better_ranked_portal_is_the_subject():
    [p] = find_same_listing_pairs([_bn("be-7", source="bankeauctions", borrower="Mr. N Mariappan"), _bn("bn-7")], [])
    assert (p.a_id, p.b_id, p.a_source, p.b_source) == ("bn-7", "be-7", "baanknet", "bankeauctions")


def test_display_fields_ride_along_but_never_decide():
    row = candidate_from_row(_row("bn-1", "baanknet", bank="Indian Bank", reserve=1.0, day="2026-09-25",
                                  text="Land", source_url="https://baanknet.com/x", emd_num=100.0, city="Salem"))
    assert row.info["url"] == "https://baanknet.com/x" and row.info["emd"] == 100.0 and row.info["city"] == "Salem"
    graph = candidate_from_graph({**_graph("1", bank="Indian Bank", reserve=1.0, day="2026-09-25"),
                                  "title": "House", "url": "https://eauctionsindia.com/1", "emd_num": 5.0,
                                  "public_url": "https://r2/n.pdf", "description": "desc", "city": "Salem"})
    assert graph.info == {"title": "House", "description": "desc", "city": "Salem", "district": None,
                          "url": "https://eauctionsindia.com/1", "emd": 5.0, "public_url": "https://r2/n.pdf"}
```

In `tests/pipeline/test_match_confidence.py`, add `PENDING` to the `from pipeline.match_confidence import (...)` list, then replace `test_same_listing_methods_match_the_matcher_and_are_graded` and `test_same_listing_table_does_not_leak_into_is_lot_grades` with:

```python
def test_same_listing_methods_match_the_matcher_and_are_graded():
    """`sources.match.METHODS` is the vocabulary that can reach a
    SAME_LISTING_AS edge; each must be graded, and nothing else may be."""
    from pipeline.match_confidence import SAME_LISTING_CONFIDENCE, listing_confidence_for
    from sources.match import METHODS

    assert set(SAME_LISTING_CONFIDENCE) == set(METHODS)
    assert set(SAME_LISTING_CONFIDENCE.values()) == {CONFIRMED, PENDING}
    assert listing_confidence_for("four_fields") == CONFIRMED
    assert listing_confidence_for("review") == PENDING
    for bad in ("exact", "borrower", "", None, 7, "FOUR_FIELDS"):
        assert listing_confidence_for(bad) == UNKNOWN


def test_same_listing_table_does_not_leak_into_is_lot_grades():
    from pipeline.match_confidence import SAME_LISTING_CONFIDENCE, listing_confidence_for

    assert listing_confidence_for("decision") == confidence_for("decision") == CONFIRMED
    assert confidence_for("four_fields") == UNKNOWN
    assert set(SAME_LISTING_CONFIDENCE) - set(MATCH_CONFIDENCE) == {"four_fields", "unit_number", "review"}
```

In `tests/scripts/test_link_listings_and_events.py`, replace the two `test_find_pairs_*` tests with:

```python
def test_find_pairs_compares_the_whole_graph_across_sources():
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"),
                           _rec("be-1", "bankeauctions", bank="Indian Bank")])
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in pairs] == [("bn-1", "841207", "four_fields", "CONFIRMED")]
    rows = ll.pair_rows(pairs + [Pair("x", "y", "baanknet", "baanknet", "four_fields", "CONFIRMED")])
    assert len(rows) == 1 and {"a_id", "b_id", "method", "confidence", "evidence"} == set(rows[0])
    assert "CONFIRMED four_fields  1" in ll.summarize(pairs)


def test_find_pairs_never_confirms_a_batch_sale_against_one_listing():
    """bn-1 and bn-2 are one borrower's two properties; 841207 is one of them."""
    pairs = ll.find_pairs([_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet"), _rec("bn-2", "baanknet")])
    assert pairs and all(p.confidence == "PENDING" for p in pairs)
```

In `tests/sources/test_gap_report.py::test_build_report_counts_new_matched_fills_and_photos`, change only these expectations (the report's shape changes in Task 3):
- `[(p.a_id, p.b_id, p.method) for p in pairs] == [("bn-359826", "841207", "four_fields")]`
- `bn["matched_by_confidence"] == {"CONFIRMED": 1, "PROBABLE": 0, "INFERRED": 0}`
- `(m["matches"], m["confidence"], m["fills"]) == ("841207", "CONFIRMED", ["extent", "possession", "has_photos"])`
- `"bn-359826 ~ 841207  CONFIRMED four_fields" in text`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/pipeline/test_match_confidence.py tests/scripts/test_link_listings_and_events.py tests/sources/test_gap_report.py -q -p no:cacheprovider -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'PENDING'`.

- [ ] **Step 3: Implement the grades**

In `pipeline/match_confidence.py`, add after `UNKNOWN = "UNKNOWN"`:

```python
#: A cross-portal pair waiting for a person. Never merged: build_spine and
#: api/canonical merge only CONFIRMED / PROBABLE.
PENDING = "PENDING"
```

and replace the `SAME_LISTING_CONFIDENCE` block (comment and dict) with:

```python
#: Grade per `SAME_LISTING_AS.method` — the cross-portal bridge written by
#: `sources.match` (spec: docs/superpowers/specs/2026-09-15-portal-match-governance-design.md).
#: Bank, auction day, reserve price and borrower agreeing on exactly one
#: listing is CONFIRMED; so is a unit number that picks one listing out of a
#: batch sale, and a person's verdict. Every partial agreement waits for a
#: person as PENDING.
SAME_LISTING_CONFIDENCE: dict[str, str] = {
    "four_fields": CONFIRMED,
    "unit_number": CONFIRMED,
    "decision": CONFIRMED,
    "review": PENDING,
}
```

- [ ] **Step 4: Implement the rule**

In `sources/match.py`:

Replace the module docstring with:

```python
"""Cross-portal matching: is this incoming listing an auction we already hold?

Pure functions over dicts — no network, no graph. The callers
(``scripts/gap_report.py``, ``scripts/link_listings.py``) fetch what the graph
knows and hand it in beside the harvested rows.

Two listings are compared only when they come from different sources, name the
same bank (``pipeline.entity_resolution.org_key``) and the same auction day. The
*subject* is the listing from the better-ranked source (``sources.base.SOURCE_RANK``).
Against each other source (spec: docs/superpowers/specs/2026-09-15-portal-match-governance-design.md):

    exactly one listing agrees on reserve price (to the rupee) and borrower   four_fields  CONFIRMED
    several agree, and one villa/flat/plot/door number picks one of them      unit_number  CONFIRMED
    a person confirmed it                                                     decision     CONFIRMED
    anything partial — a batch, price only, borrower only, a split,
    disagreeing unit numbers, two subjects on one listing                     review       PENDING

Identical postings of one unit on one source count as one candidate.
"""
```

Add to the imports:

```python
from sources.base import SOURCE_RANK
```

and remove `from pipeline.price_agreement import compare_prices` (no longer used). Keep every other import.

Add a field to `Candidate`, after `doc_shas`:

```python
    #: Display only — what a reviewer reads. The rule never looks at it.
    info: dict = field(default_factory=dict)
```

In `candidate_from_row`, add to the `Candidate(...)` call:

```python
        info={"title": row.get("title"), "description": row.get("description"), "city": row.get("city"),
              "district": row.get("district"), "url": row.get("source_url") or row.get("url"),
              "emd": row.get("emd_num"), "public_url": None},
```

In `candidate_from_graph`, add to the `Candidate(...)` call:

```python
        info={"title": rec.get("title"), "description": rec.get("description"), "city": rec.get("city"),
              "district": rec.get("district"), "url": rec.get("url"), "emd": rec.get("emd_num"),
              "public_url": rec.get("public_url")},
```

Replace everything from `# ── the matcher` to the end of the file with:

```python
# ── the matcher ──────────────────────────────────────────────────────────────

METHODS = ("decision", "four_fields", "unit_number", "review")
_ORDER = {m: i for i, m in enumerate(METHODS)}

#: Why a subject waits for a person (``Ambiguity.reason``).
REVIEW_REASONS = ("batch", "units_disagree", "price_only", "borrower_only", "split", "contested")

#: Identifier families that name one unit rather than the land a batch shares.
UNIT_FAMILIES = frozenset({"villa", "flat", "plot", "door"})

#: Unit families whose disagreement sends a pair to review. Not door: a door
#: number is usually the building's address, shared by every flat in it.
VETO_FAMILIES = frozenset({"villa", "flat", "plot"})

#: Rank for a source ``SOURCE_RANK`` does not list: after every known one.
_UNKNOWN_RANK = 99


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
    """A subject listing waiting for a person. ``reason`` is one of
    :data:`REVIEW_REASONS`; ``candidates`` are the other source's listings the
    person decides between."""
    auction_id: str
    source: str
    other_source: str
    candidates: tuple[str, ...]
    reason: str


@dataclass
class MatchResult:
    pairs: list[Pair]
    ambiguous: list[Ambiguity]


def price_equal(a: Candidate, b: Candidate) -> bool:
    """Both publish a reserve price and they agree to the rupee."""
    if not (a.has_price and b.has_price):
        return False
    return abs(float(a.reserve_price_num) - float(b.reserve_price_num)) < 1


def _unit_key(value: str) -> str:
    """A unit number for comparison across notations: ``f/1`` and ``f1`` → ``1``,
    ``b/510`` → ``510``, ``86/b`` → ``86b``; ``ff12`` stays."""
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    if re.match(r"^[a-z]\d", key):
        key = key[1:]
    return key


def _unit_keys(c: Candidate, families: frozenset[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for fam, value in c.identifiers:
        if fam in families:
            out[fam].add(_unit_key(value))
    return out


def _units_disagree(a: Candidate, b: Candidate) -> bool:
    """Both quote a villa / flat / plot number and none of them agree."""
    ka, kb = _unit_keys(a, VETO_FAMILIES), _unit_keys(b, VETO_FAMILIES)
    return any(ka[f] and kb[f] and not ka[f] & kb[f] for f in VETO_FAMILIES)


def _shared_unit(a: Candidate, b: Candidate) -> tuple[str, str] | None:
    """The first unit number (family, key) both quote, or None."""
    ka, kb = _unit_keys(a, UNIT_FAMILIES), _unit_keys(b, UNIT_FAMILIES)
    for fam in sorted(UNIT_FAMILIES):
        common = ka[fam] & kb[fam]
        if common:
            return fam, sorted(common)[0]
    return None


def _twin_groups(side: list[Candidate]) -> list[list[Candidate]]:
    """Listings on one source that are postings of the same unit — equal unit
    numbers (villa / flat / plot / door), reserve price and borrower — as one
    group, in first-seen order. A listing that quotes no unit number is its
    own group."""
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


def _representative(group: list[Candidate]) -> Candidate:
    """The group as one candidate: the first member, carrying every member's
    identifiers and notice fingerprints."""
    first = group[0]
    if len(group) == 1:
        return first
    return replace(first,
                   identifiers=set().union(*(c.identifiers for c in group)),
                   doc_shas=set().union(*(c.doc_shas for c in group)))


def _judge(subject: Candidate, reps: list[Candidate]) -> tuple | None:
    """The rule for one subject against one other source's candidates.

    Returns ``("link", index, method, evidence)``, ``("review", reason,
    indexes)`` or ``None`` (nothing agrees: the subject is new)."""
    price = [i for i, c in enumerate(reps) if price_equal(subject, c)]
    borrower = [i for i, c in enumerate(reps) if borrower_matches(subject.borrower, c.borrower)]
    if not price and not borrower:
        return None
    full = [i for i in price if i in borrower]
    if len(full) == 1:
        if _units_disagree(subject, reps[full[0]]):
            return ("review", "units_disagree", full)
        return ("link", full[0], "four_fields", "same bank, auction day, reserve price and borrower")
    if len(full) > 1:
        hits = []
        for i in full:
            unit = _shared_unit(subject, reps[i])
            if unit and not _units_disagree(subject, reps[i]):
                hits.append((i, unit))
        if len(hits) == 1:
            i, (fam, value) = hits[0]
            return ("link", i, "unit_number",
                    f"same bank, auction day, reserve price and borrower; same {fam} number {value}")
        return ("review", "batch", full)
    if price and not borrower:
        return ("review", "price_only", price)
    if borrower and not price:
        return ("review", "borrower_only", borrower)
    return ("review", "split", sorted(set(price) | set(borrower)))


def _pairs(group_s: list[Candidate], targets: list[list[Candidate]], method: str, evidence: str,
           new_ids: set[str]) -> list[Pair]:
    return [Pair(x.auction_id, y.auction_id, x.source, y.source, method, listing_confidence_for(method), evidence)
            for x in group_s for g in targets for y in g
            if x.auction_id in new_ids or y.auction_id in new_ids]


def _assign(side_s: list[Candidate], side_o: list[Candidate], new_ids: set[str]) -> tuple[list[Pair], list[Ambiguity]]:
    """Every subject group of one source against one other source, in one bucket.
    Two subjects that would link the same candidate group are both contested."""
    groups_s, groups_o = _twin_groups(side_s), _twin_groups(side_o)
    reps_o = [_representative(g) for g in groups_o]
    verdicts: dict[int, tuple | None] = {si: _judge(_representative(g), reps_o) for si, g in enumerate(groups_s)}

    linkers: dict[int, list[int]] = defaultdict(list)
    for si, v in verdicts.items():
        if v is not None and v[0] == "link":
            linkers[v[1]].append(si)
    for oi, sis in linkers.items():
        if len(sis) > 1:
            for si in sis:
                verdicts[si] = ("review", "contested", [oi])

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    for si, v in sorted(verdicts.items()):
        if v is None:
            continue
        if v[0] == "link":
            _, oi, method, evidence = v
            pairs += _pairs(groups_s[si], [groups_o[oi]], method, evidence, new_ids)
            continue
        _, reason, ois = v
        targets = [groups_o[oi] for oi in ois]
        pairs += _pairs(groups_s[si], targets, "review", reason, new_ids)
        others = tuple(sorted(y.auction_id for g in targets for y in g))
        ambiguous += [Ambiguity(x.auction_id, x.source, targets[0][0].source, others, reason)
                      for x in groups_s[si] if x.auction_id in new_ids]
    return pairs, ambiguous


def _rank(source: str) -> tuple[int, str]:
    return (SOURCE_RANK.get(source, _UNKNOWN_RANK), source)


def match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> MatchResult:
    """Every link the rule confirms, every pair waiting for a person, and one
    ``Ambiguity`` per waiting subject. ``incoming`` is compared against
    ``existing`` and against itself; ``existing`` is never compared with itself
    — that is ``link_reauctions``' job. A listing present on both sides (a
    loaded harvest row) is taken once, from ``incoming``."""
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
        sources = sorted({c.source for c in members}, key=_rank)
        for i, source_s in enumerate(sources):
            for source_o in sources[i + 1:]:
                side_s = [c for c in members if c.source == source_s]
                side_o = [c for c in members if c.source == source_o]
                if not any(c.auction_id in new_ids for c in side_s + side_o):
                    continue
                got_pairs, got_ambiguous = _assign(side_s, side_o, new_ids)
                pairs += got_pairs
                ambiguous += got_ambiguous
    pairs.sort(key=lambda p: (_ORDER[p.method], p.a_id, p.b_id))
    ambiguous.sort(key=lambda x: (x.auction_id, x.other_source))
    return MatchResult(pairs, ambiguous)


def find_same_listing_pairs(incoming: Iterable[Candidate], existing: Iterable[Candidate]) -> list[Pair]:
    """The pairs of :func:`match_listings`."""
    return match_listings(incoming, existing).pairs
```

Also update the `bucket_key` docstring in `Candidate` to: `"""Bank + day: the only listings the rule ever compares."""`.

- [ ] **Step 5: Check nothing else used the removed names**

Run: `git grep -n "price_verdict\|_is_short\|_unique_identifiers\|_identifier_tier\|_borrower_tier\|bucket_only\|notice_bytes" -- "*.py"`
Expected: no hits outside `tests/pipeline/test_match_confidence.py` comments, if any. If a hit is in production code, stop and report it.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/pipeline/test_match_confidence.py tests/scripts/test_link_listings_and_events.py tests/scripts/test_audit_listing_links.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add sources/match.py pipeline/match_confidence.py tests/sources/test_match.py tests/pipeline/test_match_confidence.py tests/scripts/test_link_listings_and_events.py tests/sources/test_gap_report.py
git commit -m "match: bank, day, price and borrower confirm; everything partial waits for review

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Human verdicts as stored decisions

**Files:**
- Modify: `sources/match.py` (`snapshot_of`; `_assign` and `match_listings` take decisions)
- Modify: `pipeline/resolution_review.py` (`KINDS`, `portal_match_key`, `decision_key`, `portal_decisions`)
- Modify: `api/review/queries.py` (`record_resolution_decision`)
- Modify: `api/review/router.py` (`ResolutionDecisionIn.kind`)
- Test: `tests/sources/test_match.py`, `tests/pipeline/test_resolution_review.py`, `tests/api/test_review_resolution.py`

**Interfaces:**
- Consumes: Task 1's `match_listings`, `_judge`, `_pairs`, `_twin_groups`, `_representative`, `Candidate`.
- Produces:
  - `sources.match.snapshot_of(c: Candidate) -> dict` → `{"bank": str, "reserve_price": int | None, "borrower": str, "auction_day": str | None}`
  - `match_listings(incoming, existing, *, decisions: dict[str, dict] | None = None) -> MatchResult`; decision dict shape `{"verdict": "approved"|"rejected", "linked_ids": set[str], "rejected_ids": set[str], "snapshot": dict}` keyed by subject id.
  - `pipeline.resolution_review.portal_match_key(subject_id: str) -> str`
  - `pipeline.resolution_review.portal_decisions(decisions: list[dict]) -> dict[str, dict]` (the shape above)
  - `record_resolution_decision("portal-match", payload, verdict, by_email)` stores `payload["snapshot"]` computed server-side.

- [ ] **Step 1: Write the failing tests**

Append to `tests/sources/test_match.py`:

```python
from sources.match import snapshot_of  # noqa: E402


def _decided(subject, verdict, linked=(), rejected=(), **snapshot_changes):
    return {subject.auction_id: {"verdict": verdict, "linked_ids": set(linked), "rejected_ids": set(rejected),
                                 "snapshot": {**snapshot_of(subject), **snapshot_changes}}}


def test_a_person_confirming_links_with_method_decision():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    result = match_listings([subject], ours, decisions=_decided(subject, "approved", linked=["853518"]))
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in result.pairs] == [("bn-359756", "853518", "decision", CONFIRMED)]
    assert result.ambiguous == []


def test_ticking_two_duplicate_postings_links_both():
    subject = _bn("bn-359636", bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="Ekadanta Enterprises")
    ours = [_ea(aid, bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="M/s. Ekadanta Enterprises")
            for aid in ("842118", "842546", "844968")]
    decisions = _decided(subject, "approved", linked=["842546", "844968"], rejected=["842118"])
    result = match_listings([subject], ours, decisions=decisions)
    assert sorted((p.b_id, p.method) for p in result.pairs) == [("842546", "decision"), ("844968", "decision")]
    assert result.ambiguous == []


def test_not_the_same_removes_the_pair_for_good():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    result = match_listings([subject], ours, decisions=_decided(subject, "rejected", rejected=["853518"]))
    assert result.pairs == [] and result.ambiguous == []


def test_a_decision_on_facts_that_changed_is_ignored():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    stale = _decided(subject, "approved", linked=["853518"], reserve_price=2900000)
    result = match_listings([subject], ours, decisions=stale)
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-359756", "price_only")]


def test_a_rule_link_to_a_listing_a_person_already_linked_is_contested():
    decided = _bn("bn-1", borrower="Mr. Haridas P")
    other = _bn("bn-2")
    result = match_listings([decided, other], [_ea("841207")], decisions=_decided(decided, "approved", linked=["841207"]))
    assert [(p.a_id, p.method) for p in result.pairs if p.method == "decision"] == [("bn-1", "decision")]
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-2", "contested")]


def test_snapshot_of_reads_the_four_facts():
    assert snapshot_of(_bn("bn-1")) == {"bank": "bank indian overseas", "reserve_price": 4626500,
                                        "borrower": "n mariappan", "auction_day": "2026-09-24"}
```

Append to `tests/pipeline/test_resolution_review.py`:

```python
def test_portal_match_key_is_one_per_subject():
    from pipeline.resolution_review import portal_match_key
    assert portal_match_key("bn-359756") == "portal-match:bn-359756"
    assert decision_key("portal-match", {"subject_id": "bn-359756", "linked_ids": ["853518"]}) == "portal-match:bn-359756"


def test_portal_decisions_reads_verdicts_into_sets():
    from pipeline.resolution_review import portal_decisions
    snap = {"bank": "bank indian", "reserve_price": 2944000, "borrower": "a r r tex", "auction_day": "2026-09-25"}
    decisions = [
        _decision("portal-match", {"subject_id": "bn-1", "linked_ids": ["853518"], "rejected_ids": [], "snapshot": snap}, "approved"),
        _decision("portal-match", {"subject_id": "bn-2", "linked_ids": [], "rejected_ids": ["9"], "snapshot": snap}, "rejected"),
        _decision("lot-match", {"auction_id": "bn-3", "lot_key": "x"}, "approved"),
    ]
    assert portal_decisions(decisions) == {
        "bn-1": {"verdict": "approved", "linked_ids": {"853518"}, "rejected_ids": set(), "snapshot": snap},
        "bn-2": {"verdict": "rejected", "linked_ids": set(), "rejected_ids": {"9"}, "snapshot": snap},
    }
```

Append to `tests/api/test_review_resolution.py`:

```python
def test_portal_match_decision_stores_a_server_side_snapshot(monkeypatch):
    written = {}

    def fake_count(cypher, params=None):
        if "IN $ids" in cypher:
            return {"n": len(set(params["ids"]))}
        return {"auction_id": "bn-359756", "bank": "Indian Bank", "reserve_price_num": 2944000.0,
                "auction_start_dt": "2026-09-25T10:00:00", "borrower": "A R R TEX"}

    monkeypatch.setattr(q, "_count_query", fake_count)
    monkeypatch.setattr(q, "run_query", lambda cypher, params=None: written.update(params or {}) or [])
    out = q.record_resolution_decision(
        "portal-match",
        {"subject_id": "bn-359756", "linked_ids": ["853518"], "rejected_ids": [],
         "snapshot": {"bank": "forged"}, "note": "same owner"},
        "approved", by_email="admin@example.com")
    assert out["key"] == "portal-match:bn-359756"
    stored = json.loads(written["payload"])
    assert stored["snapshot"] == {"bank": "bank indian", "reserve_price": 2944000,
                                  "borrower": "a r r tex", "auction_day": "2026-09-25"}
    assert stored["note"] == "same owner"


def test_portal_match_decision_refuses_bad_payloads(monkeypatch):
    monkeypatch.setattr(q, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(q, "_count_query", lambda cypher, params=None: {"n": 0} if "IN $ids" in cypher else {})
    with pytest.raises(ValueError, match="at least one"):
        q.record_resolution_decision("portal-match", {"subject_id": "bn-1", "linked_ids": [], "rejected_ids": []},
                                     "rejected", by_email="x")
    with pytest.raises(ValueError, match="must link"):
        q.record_resolution_decision("portal-match", {"subject_id": "bn-1", "linked_ids": [], "rejected_ids": ["2"]},
                                     "approved", by_email="x")
    with pytest.raises(ValueError, match="no listing"):
        q.record_resolution_decision("portal-match", {"subject_id": "bn-1", "linked_ids": ["2"], "rejected_ids": []},
                                     "approved", by_email="x")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_match.py tests/pipeline/test_resolution_review.py tests/api/test_review_resolution.py -q -p no:cacheprovider -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'snapshot_of'`.

- [ ] **Step 3: Implement decisions in the rule**

In `sources/match.py`, add after `price_equal`:

```python
def snapshot_of(c: Candidate) -> dict:
    """The four facts a portal-match decision was made on. A decision whose
    snapshot no longer equals the subject's is ignored: the facts changed."""
    return {"bank": c.bank_key, "reserve_price": _round_reserve(c.reserve_price_num),
            "borrower": borrower_key(c.borrower), "auction_day": c.day}


def _active_decision(group: list[Candidate], decisions: dict[str, dict]) -> dict | None:
    for x in group:
        d = decisions.get(x.auction_id)
        if d and d.get("snapshot") == snapshot_of(x):
            return d
    return None
```

Replace `_assign` with:

```python
def _assign(side_s: list[Candidate], side_o: list[Candidate], new_ids: set[str],
            decisions: dict[str, dict]) -> tuple[list[Pair], list[Ambiguity]]:
    """Every subject group of one source against one other source, in one bucket.

    A current decision is applied first: its rejected listings leave the
    subject's candidates for good, and an approval links the ticked listings
    (method ``decision``). The rule then judges the rest. Two subjects that
    would link the same candidate group — or a rule link to a group a person
    already linked — are contested."""
    groups_s, groups_o = _twin_groups(side_s), _twin_groups(side_o)
    reps_o = [_representative(g) for g in groups_o]
    verdicts: dict[int, tuple | None] = {}
    decided_targets: set[int] = set()

    for si, g in enumerate(groups_s):
        decision = _active_decision(g, decisions)
        rejected = decision["rejected_ids"] if decision else set()
        pool = [oi for oi, og in enumerate(groups_o) if not any(y.auction_id in rejected for y in og)]
        if decision and decision["verdict"] == "approved":
            linked = [oi for oi in pool if any(y.auction_id in decision["linked_ids"] for y in groups_o[oi])]
            if linked:
                verdicts[si] = ("decision", linked)
                decided_targets.update(linked)
                continue
        judged = _judge(_representative(g), [reps_o[oi] for oi in pool])
        if judged is None:
            verdicts[si] = None
        elif judged[0] == "link":
            verdicts[si] = ("link", pool[judged[1]], judged[2], judged[3])
        else:
            verdicts[si] = ("review", judged[1], [pool[i] for i in judged[2]])

    linkers: dict[int, list[int]] = defaultdict(list)
    for si, v in verdicts.items():
        if v is not None and v[0] == "link":
            linkers[v[1]].append(si)
    for oi, sis in linkers.items():
        if len(sis) > 1 or oi in decided_targets:
            for si in sis:
                verdicts[si] = ("review", "contested", [oi])

    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    for si, v in sorted(verdicts.items()):
        if v is None:
            continue
        if v[0] == "decision":
            pairs += _pairs(groups_s[si], [groups_o[oi] for oi in v[1]], "decision",
                            "a person confirmed these are the same property", new_ids)
            continue
        if v[0] == "link":
            _, oi, method, evidence = v
            pairs += _pairs(groups_s[si], [groups_o[oi]], method, evidence, new_ids)
            continue
        _, reason, ois = v
        targets = [groups_o[oi] for oi in ois]
        pairs += _pairs(groups_s[si], targets, "review", reason, new_ids)
        others = tuple(sorted(y.auction_id for g in targets for y in g))
        ambiguous += [Ambiguity(x.auction_id, x.source, targets[0][0].source, others, reason)
                      for x in groups_s[si] if x.auction_id in new_ids]
    return pairs, ambiguous
```

In `match_listings`, change the signature and docstring's first line to:

```python
def match_listings(incoming: Iterable[Candidate], existing: Iterable[Candidate], *,
                   decisions: dict[str, dict] | None = None) -> MatchResult:
    """Every link the rule or a person confirms, every pair waiting for a
    person, and one ``Ambiguity`` per waiting subject. ``decisions`` maps a
    subject id to its current verdict (``pipeline.resolution_review.portal_decisions``).
```

keep the rest of its docstring, and change the `_assign` call to `_assign(side_s, side_o, new_ids, decisions or {})`.

- [ ] **Step 4: Implement the decision kind**

In `pipeline/resolution_review.py`:

Add `"portal-match"` to `KINDS`:

```python
KINDS = ("bank-merge", "branch-merge", "district-conflict",
         "village-alias", "village-skip", "lot-match", "portal-match")
```

Add after `area_check_key`:

```python
def portal_match_key(subject_id: str) -> str:
    """Key for "a person decided which of our listings this portal listing is".

    One decision per subject: re-deciding replaces it, undo reopens it."""
    return f"portal-match:{subject_id}"
```

In `decision_key`, add before the final `raise`:

```python
    if kind == "portal-match":
        return portal_match_key(payload["subject_id"])
```

Append at the end of the file:

```python
def portal_decisions(decisions: list[dict]) -> dict[str, dict]:
    """Every portal-match verdict, keyed by subject id, in the shape
    ``sources.match.match_listings(decisions=...)`` reads:
    ``{"verdict", "linked_ids": set, "rejected_ids": set, "snapshot": dict}``."""
    out: dict[str, dict] = {}
    for d in _decided(decisions, "portal-match").values():
        payload = d.get("payload") or {}
        sid = payload.get("subject_id")
        if not sid or d.get("verdict") not in (APPROVED, REJECTED):
            continue
        out[sid] = {"verdict": d["verdict"],
                    "linked_ids": set(payload.get("linked_ids") or ()),
                    "rejected_ids": set(payload.get("rejected_ids") or ()),
                    "snapshot": payload.get("snapshot") or {}}
    return out
```

- [ ] **Step 5: Validate and store the verdict server-side**

In `api/review/queries.py`, add above `record_resolution_decision`:

```python
_PORTAL_SUBJECT = """
MATCH (a:AuctionProperty {auction_id: $auction_id})
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bk:Bank)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(br:Borrower)
RETURN a.auction_id AS auction_id, collect(DISTINCT bk.name)[0] AS bank,
       a.reserve_price_num AS reserve_price_num,
       toString(a.auction_start_dt) AS auction_start_dt,
       collect(DISTINCT br.name)[0] AS borrower
"""
```

In `record_resolution_decision`, insert right after the `try: key = decision_key(kind, payload) … except KeyError` block:

```python
    if kind == "portal-match":
        linked = list(payload.get("linked_ids") or [])
        rejected = list(payload.get("rejected_ids") or [])
        if not linked and not rejected:
            raise ValueError("portal-match needs at least one linked or rejected listing")
        if verdict == APPROVED and not linked:
            raise ValueError("an approved portal-match must link at least one listing")
        subject = _count_query(_PORTAL_SUBJECT, {"auction_id": payload.get("subject_id")})
        if not subject.get("auction_id"):
            raise ValueError(f"no listing with auction_id {payload.get('subject_id')!r}")
        ids = sorted(set(linked) | set(rejected))
        found = _count_query("MATCH (p:AuctionProperty) WHERE p.auction_id IN $ids RETURN count(p) AS n",
                             {"ids": ids})
        if int(found.get("n") or 0) != len(ids):
            raise ValueError("every linked or rejected listing must exist")
        # The snapshot is what the decision is valid for; it is read from the
        # graph, never taken from the caller.
        from sources.match import Candidate, snapshot_of
        payload = {**payload, "snapshot": snapshot_of(Candidate(
            auction_id=subject["auction_id"], source="", bank=subject.get("bank") or "",
            reserve_price_num=subject.get("reserve_price_num"),
            auction_start_dt=subject.get("auction_start_dt"), borrower=subject.get("borrower") or ""))}
```

In `api/review/router.py`, add `"portal-match"` to `ResolutionDecisionIn.kind`:

```python
    kind: Literal["bank-merge", "branch-merge", "district-conflict",
                  "village-alias", "village-skip", "lot-match", "price-check",
                  "area-check", "portal-match"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/pipeline/test_resolution_review.py tests/pipeline/test_match_confidence.py tests/api/test_review_resolution.py tests/scripts/test_link_listings_and_events.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sources/match.py pipeline/resolution_review.py api/review/queries.py api/review/router.py tests/sources/test_match.py tests/pipeline/test_resolution_review.py tests/api/test_review_resolution.py
git commit -m "portal-match decisions: approve links ticked listings, reject is permanent, stale facts reopen

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Gap report counts confirmed / review / new

**Files:**
- Modify: `scripts/gap_report.py` (module docstring legend, remove `GRADES`, `build_report`, `format_report`)
- Test: `tests/sources/test_gap_report.py`

**Interfaces:**
- Consumes: Task 1 `Pair`, `Ambiguity`, `match_listings`.
- Produces: per source `{"rows", "already_loaded", "new", "review", "review_by_reason": {reason: n}, "confirmed", "confirmed_by_method": {"four_fields", "unit_number", "decision"}, "fills", "photos_gained": {"new", "confirmed"}, "new_core_complete", "new_core_avg", "confirmed_listings"}`; invariant `already_loaded + new + review + confirmed == rows`.

- [ ] **Step 1: Write the failing tests**

In `tests/sources/test_gap_report.py`, change the import to `from sources.match import Ambiguity, candidate_from_row, match_listings`, delete `test_ambiguous_inferred_is_counted_once_per_listing` and `test_undecided_listings_are_neither_new_nor_matched`, and replace `test_build_report_counts_new_matched_fills_and_photos` with:

```python
def _graph_841207():
    return {"auction_id": "841207", "source": "eauctionsindia", "bank": "Indian Overseas Bank", "reserve_price_num": 4626500.0,
            "auction_start_dt": "2026-09-24T11:00:00Z", "borrower": "Mr. N. Mariappan", "doc_shas": [], "identifiers": [],
            "lot_bounds": [], "n_extents": 0, "possession": None, "property_type": "house", "district": "Tirunelveli",
            "total_area": None, "boundaries": {}, "boundary_measurements": [], "photo_urls": None}


def test_build_report_counts_confirmed_review_new_fills_and_photos():
    graph = [GRAPH_779491, _graph_841207()]
    rows = {"baanknet": [BN_359826, {**BN_359826, "auction_id": "bn-1", "bank_name": "Nobody Bank", "has_photos": True}],
            "bankeauctions": [BE_236961, {**BE_236961, "auction_id": "779491"}]}   # a re-run of a loaded id
    result = match_listings([candidate_from_row(r) for rs in rows.values() for r in rs],
                            [gr.graph_candidate(g) for g in graph])
    assert [(p.a_id, p.b_id, p.method) for p in result.pairs] == [("bn-359826", "841207", "four_fields")]

    rep = gr.build_report(rows, graph, result.pairs, result.ambiguous)
    bn = rep["sources"]["baanknet"]
    assert (bn["rows"], bn["already_loaded"], bn["new"], bn["review"], bn["confirmed"]) == (2, 0, 1, 0, 1)
    assert bn["confirmed_by_method"] == {"four_fields": 1, "unit_number": 0, "decision": 0}
    assert {f for f, n in bn["fills"].items() if n} == {"extent", "possession", "has_photos"}
    assert bn["photos_gained"] == {"new": 1, "confirmed": 1}
    [m] = bn["confirmed_listings"]
    assert (m["matches"], m["method"], m["fills"]) == ("841207", "four_fields", ["extent", "possession", "has_photos"])

    be = rep["sources"]["bankeauctions"]
    assert (be["rows"], be["already_loaded"], be["new"], be["review"], be["confirmed"]) == (2, 1, 1, 0, 0)

    text = gr.format_report(rep)
    assert "bn-359826 ~ 841207  four_fields" in text
    assert "confirmed 1 (four_fields 1, unit_number 0, decision 0)" in text


def test_review_listings_are_neither_new_nor_confirmed():
    rows = {"baanknet": [BN_359826, {**BN_359826, "auction_id": "bn-2"}]}
    ambiguous = [Ambiguity("bn-359826", "baanknet", "eauctionsindia", ("1", "2"), "batch")]
    rep = gr.build_report(rows, [], [], ambiguous)
    s = rep["sources"]["baanknet"]
    assert (s["rows"], s["new"], s["review"], s["confirmed"]) == (2, 1, 1, 0)
    assert s["review_by_reason"] == {"batch": 1}
    assert rep["ambiguous"] == [{"auction_id": "bn-359826", "source": "baanknet", "other_source": "eauctionsindia",
                                 "candidates": ("1", "2"), "reason": "batch"}]
    assert "review 1 (batch 1)" in gr.format_report(rep)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources/test_gap_report.py -q -p no:cacheprovider -o addopts=""`
Expected: FAIL — `KeyError: 'review'`.

- [ ] **Step 3: Implement**

In `scripts/gap_report.py`, replace the legend lines from `  new             the matcher found no partner…` through `many of those INFERRED matches are ambiguous (several … a batch sale, most likely)` with:

```
  new             no listing of another source agrees on reserve price or borrower
  review          a partial agreement waiting for a person, by reason — batch,
                  units_disagree, price_only, borrower_only, split, contested
  confirmed       bank, auction day, reserve price and borrower agree on one listing
                  (four_fields), a unit number settled a batch (unit_number), or a
                  person confirmed it (decision)
```

Delete the line `GRADES = ("CONFIRMED", "PROBABLE", "INFERRED")`.

Replace `build_report` and `format_report` with:

```python
CONFIRMED_METHODS = ("four_fields", "unit_number", "decision")


def build_report(rows_by_source: dict[str, list[dict]], existing: list[dict], pairs: list[Pair],
                 ambiguous: Iterable[Ambiguity] = ()) -> dict:
    """Pure: the per-source numbers from harvested rows, the graph's records
    and the matcher's result. A row is ``confirmed`` when a CONFIRMED pair
    names it, ``review`` when it waits for a person, ``new`` otherwise."""
    ambiguous = list(ambiguous)
    review_reason = {a.auction_id: a.reason for a in ambiguous}
    graph_by_id = {r["auction_id"]: r for r in existing}
    graph_core = {aid: core_from_graph(r) for aid, r in graph_by_id.items()}
    incoming_ids = {row["auction_id"] for rows in rows_by_source.values() for row in rows}

    confirmed_pair: dict[str, Pair] = {}
    for p in pairs:
        if p.confidence != "CONFIRMED":
            continue
        for me in (p.a_id, p.b_id):
            if me in incoming_ids:
                confirmed_pair.setdefault(me, p)

    report: dict = {"sources": {}, "pairs": [p.__dict__ for p in pairs],
                    "ambiguous": [a.__dict__ for a in ambiguous]}
    for source, rows in rows_by_source.items():
        by_method = Counter()
        by_reason = Counter()
        fills = Counter()
        photos_new = photos_confirmed = 0
        new_complete = Counter()
        already = new = review = confirmed = 0
        confirmed_ids: list[dict] = []
        for row in rows:
            aid = row["auction_id"]
            if aid in graph_by_id:
                already += 1
                continue
            mine = core_from_row(row)
            p = confirmed_pair.get(aid)
            if p is None:
                if aid in review_reason:
                    review += 1
                    by_reason[review_reason[aid]] += 1
                    continue
                new += 1
                new_complete[sum(mine.values())] += 1
                photos_new += mine["has_photos"]
                continue
            confirmed += 1
            by_method[p.method] += 1
            other = p.b_id if p.a_id == aid else p.a_id
            theirs = graph_core.get(other)
            if theirs is None:                      # confirmed against another new portal's row
                theirs = core_from_row(next(r for rs in rows_by_source.values() for r in rs if r["auction_id"] == other))
            gained = [f for f in CORE_FIELDS if mine[f] and not theirs[f]]
            for f in gained:
                fills[f] += 1
            photos_confirmed += "has_photos" in gained
            confirmed_ids.append({"auction_id": aid, "matches": other, "method": p.method,
                                  "fills": gained, "evidence": p.evidence})
        n_new = sum(new_complete.values())
        report["sources"][source] = {
            "rows": len(rows),
            "already_loaded": already,
            "new": new,
            "review": review,
            "review_by_reason": dict(sorted(by_reason.items())),
            "confirmed": confirmed,
            "confirmed_by_method": {m: by_method.get(m, 0) for m in CONFIRMED_METHODS},
            "fills": {f: fills.get(f, 0) for f in CORE_FIELDS},
            "photos_gained": {"new": photos_new, "confirmed": photos_confirmed},
            "new_core_complete": {str(k): v for k, v in sorted(new_complete.items())},
            "new_core_avg": round(sum(k * v for k, v in new_complete.items()) / n_new, 1) if n_new else None,
            "confirmed_listings": confirmed_ids,
        }
    return report


def format_report(report: dict) -> str:
    lines = []
    for source, s in report["sources"].items():
        m = s["confirmed_by_method"]
        reasons = ", ".join(f"{r} {n}" for r, n in s["review_by_reason"].items())
        lines.append(f"{source}")
        lines.append(f"  rows {s['rows']}  already loaded {s['already_loaded']}  new {s['new']}"
                     f"  review {s['review']}{' (' + reasons + ')' if reasons else ''}"
                     f"  confirmed {s['confirmed']} (four_fields {m['four_fields']}, unit_number {m['unit_number']},"
                     f" decision {m['decision']})")
        if s["new"]:
            hist = ", ".join(f"{k}/9: {v}" for k, v in s["new_core_complete"].items())
            lines.append(f"  new listings core fields  avg {s['new_core_avg']}  [{hist}]")
        filled = {f: n for f, n in s["fills"].items() if n}
        if filled:
            lines.append("  fills on confirmed  " + ", ".join(f"{f} +{n}" for f, n in filled.items()))
        lines.append(f"  photos gained  new {s['photos_gained']['new']}  confirmed {s['photos_gained']['confirmed']}")
        for c in s["confirmed_listings"]:
            fills = f"  fills {', '.join(c['fills'])}" if c["fills"] else ""
            lines.append(f"    {c['auction_id']} ~ {c['matches']}  {c['method']:<12} {c['evidence']}{fills}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/scripts/test_audit_listing_links.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gap_report.py tests/sources/test_gap_report.py
git commit -m "gap_report: confirmed / review (by reason) / new

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Linking — safety stop, spot-check, stored review queue

**Files:**
- Modify: `scripts/gap_report.py` (`FETCH_EXISTING` returns display fields)
- Modify: `scripts/link_listings.py`
- Test: `tests/scripts/test_link_listings_and_events.py`

**Interfaces:**
- Consumes: Task 1/2 `match_listings(…, decisions=)`, `snapshot_of`, `MatchResult`, `Ambiguity`, `Pair`; `pipeline.resolution_review.portal_decisions`; `scripts.audit_listing_links.same_source_clusters`; `scripts.gap_report.FETCH_EXISTING`, `graph_candidate`.
- Produces (all in `scripts/link_listings.py`):
  - `class LinkSafetyError(RuntimeError)`
  - `match(records: list[dict], decisions: dict[str, dict] | None = None) -> MatchResult`
  - `find_pairs(records, decisions=None) -> list[Pair]` (unchanged name)
  - `safety_problems(result: MatchResult, records: list[dict]) -> list[str]`
  - `spot_check_sample(result: MatchResult, decided_ids: set[str], run_date: date, size: int = 10) -> list[str]`
  - `listing_row(rec: dict) -> dict` → `{auction_id, source, bank, borrower, reserve, emd, auction_day, city, district, title, description, url, public_url}`
  - `review_rows(result: MatchResult, records: list[dict], spot_ids: list[str]) -> list[dict]` → `{subject: listing_row, other_source, reason, candidates: [listing_row], spot_check: bool, snapshot: dict}`
  - `run(dry_run: bool = False, queue_only: bool = False) -> int` — stores `review_json` and `spot_check_json` on `(:PipelineState {key:'link_listings'})`; raises `LinkSafetyError` before any write when `safety_problems` is non-empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_link_listings_and_events.py`:

```python
from datetime import date  # noqa: E402

import pytest  # noqa: E402

from sources.match import Ambiguity, MatchResult  # noqa: E402


def test_safety_stop_flags_a_cluster_that_merges_two_different_prices():
    records = [_rec("841207", "eauctionsindia"), _rec("841208", "eauctionsindia", reserve=5000000.0),
               _rec("bn-1", "baanknet")]
    result = MatchResult(pairs=[Pair("bn-1", "841207", "baanknet", "eauctionsindia", "four_fields", "CONFIRMED"),
                                Pair("bn-1", "841208", "baanknet", "eauctionsindia", "decision", "CONFIRMED")],
                         ambiguous=[])
    [problem] = ll.safety_problems(result, records)
    assert "841207" in problem and "841208" in problem and "price" in problem


def test_safety_stop_flags_a_listing_both_confirmed_and_in_review_against_one_source():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    result = MatchResult(pairs=[Pair("bn-1", "841207", "baanknet", "eauctionsindia", "four_fields", "CONFIRMED")],
                         ambiguous=[Ambiguity("bn-1", "baanknet", "eauctionsindia", ("841207",), "batch")])
    [problem] = ll.safety_problems(result, records)
    assert "bn-1" in problem and "review" in problem


def test_a_clean_result_has_no_safety_problems():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    assert ll.safety_problems(ll.match(records), records) == []


def test_spot_check_is_deterministic_and_skips_decided_subjects():
    pairs = [Pair(f"bn-{i}", str(900000 + i), "baanknet", "eauctionsindia", "four_fields", "CONFIRMED") for i in range(30)]
    pairs.append(Pair("bn-99", "999", "baanknet", "eauctionsindia", "decision", "CONFIRMED"))
    result = MatchResult(pairs=pairs, ambiguous=[])
    first = ll.spot_check_sample(result, {"bn-3"}, date(2026, 9, 15))
    assert first == ll.spot_check_sample(result, {"bn-3"}, date(2026, 9, 15))
    assert len(first) == 10 and "bn-3" not in first and "bn-99" not in first
    assert first != ll.spot_check_sample(result, {"bn-3"}, date(2026, 9, 16))


def test_review_rows_carry_both_sides_and_the_snapshot():
    records = [_rec("853518", "eauctionsindia", borrower="M/s ARR Tex"), _rec("bn-359756", "baanknet", borrower="A R R TEX")]
    result = ll.match(records)
    [row] = ll.review_rows(result, records, [])
    assert (row["subject"]["auction_id"], row["other_source"], row["reason"], row["spot_check"]) == \
        ("bn-359756", "eauctionsindia", "price_only", False)
    assert [c["auction_id"] for c in row["candidates"]] == ["853518"]
    assert row["snapshot"]["borrower"] == "a r r tex"


def test_review_rows_include_spot_checked_confirmations():
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]
    result = ll.match(records)
    [row] = ll.review_rows(result, records, ["bn-1"])
    assert (row["subject"]["auction_id"], row["reason"], row["spot_check"]) == ("bn-1", "spot_check", True)
    assert [c["auction_id"] for c in row["candidates"]] == ["841207"]


def test_run_writes_nothing_when_the_safety_stop_fires(monkeypatch):
    calls = []
    records = [_rec("841207", "eauctionsindia"), _rec("bn-1", "baanknet")]

    def fake_run_query(cypher, params=None):
        calls.append(cypher)
        if "ResolutionDecision" in cypher:
            return []
        return records

    import api.neo4j_client
    monkeypatch.setattr(api.neo4j_client, "run_query", fake_run_query)
    monkeypatch.setattr(ll, "safety_problems", lambda result, recs: ["two different prices merged"])
    with pytest.raises(ll.LinkSafetyError, match="two different prices"):
        ll.run()
    assert not any("DELETE" in c or "MERGE" in c for c in calls)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/scripts/test_link_listings_and_events.py -q -p no:cacheprovider -o addopts=""`
Expected: FAIL — `AttributeError: module 'scripts.link_listings' has no attribute 'safety_problems'`.

- [ ] **Step 3: Fetch the display fields**

In `scripts/gap_report.py`, in `FETCH_EXISTING`, replace the documents subquery line pair:

```
CALL { WITH a OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
       RETURN [s IN collect(DISTINCT doc.content_sha256) WHERE s IS NOT NULL] AS doc_shas, count(doc) AS n_docs }
```

with:

```
CALL { WITH a OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(doc:Document)
       RETURN [s IN collect(DISTINCT doc.content_sha256) WHERE s IS NOT NULL] AS doc_shas, count(doc) AS n_docs,
              [u IN collect(DISTINCT doc.public_url) WHERE u IS NOT NULL][0] AS public_url }
```

and add this line to the `RETURN`, directly below the existing last line `       a.photo_urls AS photo_urls` (the new line starts with the comma, so do not add one to the line above):

```
       , a.title AS title, coalesce(a.source_url, a.url) AS url, a.emd_num AS emd_num, public_url
```

- [ ] **Step 4: Implement the linking changes**

Replace `scripts/link_listings.py` with:

```python
"""
link_listings.py — the cross-portal bridge: :SAME_LISTING_AS between copies
of one auction on different portals.

Reads every listing the way scripts/gap_report.py does, loads the human
portal-match verdicts, runs sources.match.match_listings (bank + auction day +
reserve price + borrower; spec docs/superpowers/specs/2026-09-15-portal-match-governance-design.md)
and then, in order:

  1. the safety stop — refuses to write anything if CONFIRMED links would
     merge two listings of one portal with different prices or unit numbers,
     or if a listing is both confirmed and in review against the same portal;
  2. stores the review queue (every listing waiting for a person, plus a
     daily spot-check of 10 automatic confirmations) on
     (:PipelineState {key:'link_listings'}) for the review page;
  3. drops every :SAME_LISTING_AS edge and MERGEs the new pairs both ways —
     CONFIRMED links and PENDING review pairs — with {method, confidence,
     evidence, linked_at}. Same-source pairs are never written.

    python -m scripts.link_listings --dry-run      # match, check and summarise; write nothing
    python -m scripts.link_listings --queue-only   # also store the review queue; no edges
    python -m scripts.link_listings
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.resolution_review import portal_decisions  # noqa: E402
from scripts.audit_listing_links import same_source_clusters  # noqa: E402
from scripts.gap_report import FETCH_EXISTING, graph_candidate  # noqa: E402
from sources.match import (  # noqa: E402
    MatchResult, Pair, _units_disagree, day_of, match_listings, snapshot_of,
)

BATCH = 200
SPOT_CHECK_SIZE = 10
_SPOT_CHECK_METHODS = ("four_fields", "unit_number")

DROP_EXISTING = "MATCH ()-[r:SAME_LISTING_AS]->() DELETE r"

MERGE_PAIR = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.a_id})
MATCH (b:AuctionProperty {auction_id: r.b_id})
MERGE (a)-[ab:SAME_LISTING_AS]->(b)
  SET ab.method = r.method, ab.confidence = r.confidence, ab.evidence = r.evidence, ab.linked_at = datetime()
MERGE (b)-[ba:SAME_LISTING_AS]->(a)
  SET ba.method = r.method, ba.confidence = r.confidence, ba.evidence = r.evidence, ba.linked_at = datetime()
RETURN count(*) AS n
"""

LOAD_DECISIONS = """
MATCH (r:ResolutionDecision {kind: 'portal-match'})
RETURN r.key AS key, r.kind AS kind, r.verdict AS verdict, r.payload_json AS payload_json
"""

SAVE_QUEUE = """
MERGE (s:PipelineState {key: 'link_listings'})
SET s.review_json = $rows, s.spot_check_json = $spot, s.ran_at = datetime()
"""


class LinkSafetyError(RuntimeError):
    """The safety stop fired: nothing was written."""


def pair_rows(pairs: list[Pair]) -> list[dict]:
    return [{"a_id": p.a_id, "b_id": p.b_id, "method": p.method, "confidence": p.confidence, "evidence": p.evidence}
            for p in pairs if p.a_source != p.b_source]


def match(records: list[dict], decisions: dict[str, dict] | None = None) -> MatchResult:
    """Every cross-source verdict among the graph's listings. The whole graph
    is the ``incoming`` side so listings are compared with each other."""
    return match_listings([graph_candidate(r) for r in records], [], decisions=decisions)


def find_pairs(records: list[dict], decisions: dict[str, dict] | None = None) -> list[Pair]:
    return match(records, decisions).pairs


def safety_problems(result: MatchResult, records: list[dict]) -> list[str]:
    """Why writing this result would be unsafe; empty when it is safe."""
    by_id = {r["auction_id"]: r for r in records}
    cands = {aid: graph_candidate(r) for aid, r in by_id.items()}
    problems: list[str] = []

    for cluster in same_source_clusters([p.__dict__ for p in result.pairs]):
        by_source: dict[str, list[str]] = defaultdict(list)
        for aid in cluster:
            by_source[(by_id.get(aid) or {}).get("source") or "eauctionsindia"].append(aid)
        for source, ids in sorted(by_source.items()):
            if len(ids) < 2:
                continue
            prices = {round(float(by_id[i].get("reserve_price_num") or 0)) for i in ids if i in by_id}
            if len(prices) > 1:
                problems.append(f"confirmed links would merge {source} listings {', '.join(sorted(ids))} with different reserve prices")
                continue
            if any(_units_disagree(cands[x], cands[y]) for x in ids for y in ids if x < y and x in cands and y in cands):
                problems.append(f"confirmed links would merge {source} listings {', '.join(sorted(ids))} with different unit numbers")

    confirmed_against = {(p.a_id, p.b_source) for p in result.pairs if p.confidence == "CONFIRMED"}
    for a in result.ambiguous:
        if (a.auction_id, a.other_source) in confirmed_against:
            problems.append(f"{a.auction_id} is both confirmed and in review against {a.other_source}")
    return problems


def spot_check_sample(result: MatchResult, decided_ids: set[str], run_date: date, size: int = SPOT_CHECK_SIZE) -> list[str]:
    """Subjects of automatic confirmations nobody has looked at, sampled the
    same way for the same day."""
    subjects = sorted({p.a_id for p in result.pairs
                       if p.confidence == "CONFIRMED" and p.method in _SPOT_CHECK_METHODS and p.a_id not in decided_ids})
    return sorted(random.Random(run_date.isoformat()).sample(subjects, min(size, len(subjects))))


def listing_row(rec: dict) -> dict:
    """What a reviewer reads about one listing."""
    return {"auction_id": rec.get("auction_id"), "source": rec.get("source") or "eauctionsindia",
            "bank": rec.get("bank"), "borrower": rec.get("borrower"), "reserve": rec.get("reserve_price_num"),
            "emd": rec.get("emd_num"), "auction_day": day_of(rec.get("auction_start_dt")),
            "city": rec.get("city"), "district": rec.get("district"), "title": rec.get("title"),
            "description": (rec.get("description") or "")[:600] or None,
            "url": rec.get("url"), "public_url": rec.get("public_url")}


def review_rows(result: MatchResult, records: list[dict], spot_ids: list[str]) -> list[dict]:
    """The review queue: one row per subject waiting for a person, then one per
    spot-checked confirmation."""
    by_id = {r["auction_id"]: r for r in records}
    rows: list[dict] = []
    for a in result.ambiguous:
        subject = by_id.get(a.auction_id)
        if subject is None:
            continue
        rows.append({"subject": listing_row(subject), "other_source": a.other_source, "reason": a.reason,
                     "candidates": [listing_row(by_id[c]) for c in a.candidates if c in by_id],
                     "spot_check": False, "snapshot": snapshot_of(graph_candidate(subject))})
    linked: dict[str, list[Pair]] = defaultdict(list)
    for p in result.pairs:
        if p.a_id in spot_ids and p.confidence == "CONFIRMED":
            linked[p.a_id].append(p)
    for aid in spot_ids:
        subject = by_id.get(aid)
        if subject is None or not linked[aid]:
            continue
        rows.append({"subject": listing_row(subject), "other_source": linked[aid][0].b_source, "reason": "spot_check",
                     "candidates": [listing_row(by_id[p.b_id]) for p in linked[aid] if p.b_id in by_id],
                     "spot_check": True, "snapshot": snapshot_of(graph_candidate(subject))})
    return rows


def summarize(pairs: list[Pair]) -> str:
    by = Counter((p.confidence, p.method) for p in pairs)
    lines = [f"pairs: {len(pairs)}"]
    for (conf, method), n in sorted(by.items()):
        lines.append(f"  {conf:<9} {method:<12} {n}")
    return "\n".join(lines)


def _load_decisions(run_query) -> dict[str, dict]:
    rows = run_query(LOAD_DECISIONS)
    decisions = []
    for r in rows:
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        decisions.append({"key": r.get("key"), "kind": r.get("kind"), "verdict": r.get("verdict"), "payload": payload})
    return portal_decisions(decisions)


def run(dry_run: bool = False, queue_only: bool = False) -> int:
    from api.neo4j_client import run_query

    t0 = time.monotonic()
    records = run_query(FETCH_EXISTING)
    decisions = _load_decisions(run_query)
    print(f"  {len(records)} listings, {len(decisions)} portal-match decisions fetched in {time.monotonic() - t0:.0f}s")
    result = match(records, decisions)
    print(summarize(result.pairs))
    print(f"  waiting for review: {len(result.ambiguous)}  " +
          ", ".join(f"{r} {n}" for r, n in sorted(Counter(a.reason for a in result.ambiguous).items())))

    problems = safety_problems(result, records)
    if problems:
        for problem in problems:
            print(f"  SAFETY STOP: {problem}")
        raise LinkSafetyError("; ".join(problems))

    spot = spot_check_sample(result, {subject_id for subject_id, _other in decisions}, date.today())
    rows = review_rows(result, records, spot)
    if dry_run:
        print(f"[dry-run] {len(rows)} review rows, spot-check {spot}; no writes")
        return 0
    run_query(SAVE_QUEUE, {"rows": json.dumps(rows, ensure_ascii=False), "spot": json.dumps(spot)})
    print(f"  review queue stored: {len(rows)} rows")
    if queue_only:
        return 0
    run_query(DROP_EXISTING)
    out = pair_rows(result.pairs)
    for i in range(0, len(out), BATCH):
        run_query(MERGE_PAIR, {"rows": out[i:i + BATCH]})
    print(f"  {len(out)} pairs written ({len(out) * 2} directed edges) in {time.monotonic() - t0:.0f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="match, check and summarise; write nothing")
    ap.add_argument("--queue-only", action="store_true", help="store the review queue but write no edges")
    args = ap.parse_args(argv)
    try:
        return run(dry_run=args.dry_run, queue_only=args.queue_only)
    except LinkSafetyError:
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

Note: `run_query` returns list rows for `FETCH_EXISTING` and `LOAD_DECISIONS`; the fake in the test returns `[]` for the decisions query and the records otherwise.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/scripts/test_link_listings_and_events.py tests/scripts/test_audit_listing_links.py tests/sources tests/pipeline/test_run_pipeline.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/link_listings.py scripts/gap_report.py tests/scripts/test_link_listings_and_events.py
git commit -m "link_listings: safety stop before writing, daily spot-check, stored review queue, --queue-only

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Review API serves the portal-match queue

**Files:**
- Modify: `api/review/queries.py` (`_portal_matches`, `resolution_review`, `_resolution_review_panels`)
- Modify: `api/review/router.py` (`PortalListing`, `PortalMatchRow`, `ResolutionReviewOut.portal_matches`)
- Test: `tests/api/test_review_resolution.py`

**Interfaces:**
- Consumes: Task 4's stored `review_json` row shape; Task 2's `portal_decisions`.
- Produces: `GET /review/resolution` → `portal_matches: list[PortalMatchRow]` where `PortalMatchRow = {subject: PortalListing, other_source: str, reason: str, candidates: list[PortalListing], spot_check: bool, snapshot: dict}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_review_resolution.py`:

```python
def _stored_row(subject_id, snapshot, reason="price_only"):
    listing = {"auction_id": subject_id, "source": "baanknet", "bank": "Indian Bank", "borrower": "A R R TEX",
               "reserve": 2944000.0, "emd": 294400.0, "auction_day": "2026-09-25", "city": "Salem", "district": None,
               "title": "Land and Residential Building", "description": "SF no.97/6A1", "url": "https://baanknet.com/x",
               "public_url": None}
    return {"subject": listing, "other_source": "eauctionsindia", "reason": reason,
            "candidates": [{**listing, "auction_id": "853518", "source": "eauctionsindia", "borrower": "M/s ARR Tex"}],
            "spot_check": False, "snapshot": snapshot}


def test_portal_matches_hide_rows_with_a_current_decision(monkeypatch):
    from pipeline.resolution_review import decision_key

    snap = {"bank": "bank indian", "reserve_price": 2944000, "borrower": "a r r tex", "auction_day": "2026-09-25"}
    stored = [_stored_row("bn-1", snap), _stored_row("bn-2", snap), _stored_row("bn-3", snap)]
    monkeypatch.setattr(q, "_count_query", lambda cypher, params=None: {"rj": json.dumps(stored)})

    def decision(subject, snapshot):
        payload = {"subject_id": subject, "other_source": "eauctionsindia", "linked_ids": ["853518"],
                   "rejected_ids": [], "snapshot": snapshot}
        return {"key": decision_key("portal-match", payload), "kind": "portal-match", "verdict": "approved", "payload": payload}

    decisions = [decision("bn-1", snap), decision("bn-2", {**snap, "reserve_price": 1})]   # bn-2's facts changed
    rows = q._portal_matches(decisions)
    assert [r["subject"]["auction_id"] for r in rows] == ["bn-2", "bn-3"]


def test_portal_match_rows_fit_the_response_model():
    from api.review.router import PortalMatchRow

    snap = {"bank": "bank indian", "reserve_price": 2944000, "borrower": "a r r tex", "auction_day": "2026-09-25"}
    row = PortalMatchRow(**_stored_row("bn-1", snap))
    assert row.subject.auction_id == "bn-1" and row.candidates[0].borrower == "M/s ARR Tex"
    assert row.snapshot == snap and row.reason == "price_only"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/api/test_review_resolution.py -q -p no:cacheprovider -o addopts=""`
Expected: FAIL — `AttributeError: module 'api.review.queries' has no attribute '_portal_matches'`.

- [ ] **Step 3: Implement the query**

In `api/review/queries.py`, add after `resolution_review`'s `_LOT_MATCH_LIMIT = 200` line:

```python
#: Cap on portal-match rows in one queue load (135 measured on the first harvest).
_PORTAL_MATCH_LIMIT = 400


def _portal_matches(decisions: list[dict]) -> list[dict]:
    """Portal listings waiting for a person, as `scripts/link_listings.py` —
    the code that writes the links — stored them on its last run. A row whose
    subject already has a decision on the same facts is settled and hidden;
    a decision on facts that since changed does not hide it."""
    import json as _json

    from pipeline.resolution_review import portal_decisions

    state = _count_query("MATCH (s:PipelineState {key:'link_listings'}) RETURN s.review_json AS rj")
    try:
        rows = _json.loads(state.get("rj") or "[]")
    except (TypeError, ValueError):
        rows = []
    decided = portal_decisions(decisions)
    out = []
    for r in rows:
        d = decided.get(((r.get("subject") or {}).get("auction_id"), r.get("other_source")))
        if d and d["snapshot"] == r.get("snapshot"):
            continue
        out.append(r)
    return out[:_PORTAL_MATCH_LIMIT]
```

In `resolution_review()`, add `portal_matches = _portal_matches(decisions)` after `area_checks = _area_checks(decisions)`, add `"portal_matches": portal_matches,` to the returned dict after `"area_checks": area_checks,`, and add `+ len(portal_matches)` inside the `"open"` sum (after `+ len(area_checks)`).

In `_resolution_review_panels`, add to the "Open questions" rows after `("sizes that contradict", len(queues["area_checks"])),`:

```python
            ("portal matches to review", len(queues["portal_matches"])),
```

- [ ] **Step 4: Implement the response models**

In `api/review/router.py`, add before `class ResolutionReviewOut`:

```python
class PortalListing(BaseModel):
    """One side of a portal match, as a reviewer reads it."""
    auction_id: str
    source: str
    bank: str | None = None
    borrower: str | None = None
    reserve: float | None = None
    emd: float | None = None
    auction_day: str | None = None
    city: str | None = None
    district: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    public_url: str | None = None


class PortalMatchRow(BaseModel):
    """A portal listing waiting for a person: which of ours (if any) is it?
    `reason` is batch | units_disagree | price_only | borrower_only | split |
    contested | spot_check. `snapshot` rides back to the decide call so the
    reviewer's verdict is tied to the facts they saw."""
    subject: PortalListing
    other_source: str
    reason: str
    candidates: list[PortalListing] = []
    spot_check: bool = False
    snapshot: dict = {}
```

and add to `ResolutionReviewOut`, after `area_checks: list[AreaCheck] = []`:

```python
    portal_matches: list[PortalMatchRow] = []
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/api/test_review_resolution.py tests/pipeline/test_resolution_review.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/review/queries.py api/review/router.py tests/api/test_review_resolution.py
git commit -m "review API: serve the stored portal-match queue, hiding settled rows

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Review page — "Portal matches — same property?" (layout A)

**Files:**
- Modify: `web/review.html` (new `portalMatchRow` + `portalMatchTabs`; panel in `loadResolutionQueues`; click handling; decided-row undo in `decideResolution`)

**Interfaces:**
- Consumes: `GET /review/resolution` → `portal_matches` (Task 5); `POST /review/resolution/decide` and `/undo` with `kind: 'portal-match'` and payload `{subject_id, linked_ids, rejected_ids, snapshot, note?}` (Task 2).
- Produces: UI only.

- [ ] **Step 1: Add the row renderer**

In `web/review.html`, insert immediately before the line `  // Verdicts land instantly in the queue but are applied to the graph by the` (the comment above `let rrPollTimer = null;`):

```js
  // ─── Portal matches (layout A): a portal listing beside the listing(s) of
  // ours it partly agrees with. Bank and auction day always agree — the rule
  // never compares anything else — so the ✓/✗ column is price and borrower.
  const PM_REASON_LABEL = {
    price_only: 'Price agrees, borrower differs',
    borrower_only: 'Borrower agrees, price differs',
    batch: 'Batch sale — several of ours agree on all four',
    units_disagree: 'All four agree, but unit numbers differ',
    split: 'Price agrees with one listing, borrower with another',
    contested: 'Another portal listing claims the same one',
    spot_check: 'Spot-check of an automatic confirmation',
  };

  function pmPriceOk(a, b) {
    return a.reserve != null && b.reserve != null && Math.abs(a.reserve - b.reserve) < 1;
  }

  function pmMark(ok) {
    return ok
      ? '<span style="color:#137333;font-weight:700">✓</span>'
      : '<span style="color:#b3261e;font-weight:700">✗</span>';
  }

  function pmCell(label, left, right, mark) {
    return `<tr><th style="text-align:left;color:var(--muted);font-weight:600;padding:4px 6px;width:16%">${label}</th>
      <td style="padding:4px 6px">${left}</td><td style="padding:4px 6px">${right}</td>
      <td style="padding:4px 6px;width:6%">${mark}</td></tr>`;
  }

  function pmLinks(l) {
    return [
      l.public_url ? `<a href="${escapeHtml(l.public_url)}" target="_blank" rel="noopener">📄 notice</a>` : '',
      l.url ? `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener">portal ↗</a>` : '',
    ].filter(Boolean).join(' · ') || '<span style="color:var(--muted)">no files</span>';
  }

  function portalMatchRow(m) {
    const s = m.subject;
    const base = { subject_id: s.auction_id, other_source: m.other_source, snapshot: m.snapshot };
    const head = `<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">
        <b>${escapeHtml(s.title || s.auction_id)}${s.city ? ' · ' + escapeHtml(s.city) : ''}</b>
        <span style="font-size:12px;color:#8a5a00;background:#fff4dc;border-radius:4px;padding:2px 6px">${
          escapeHtml(PM_REASON_LABEL[m.reason] || m.reason)}</span>
      </div>`;
    const note = `<input class="pm-note" placeholder="optional note…" style="flex:1;min-width:140px">`;
    const skip = `<button class="ghost pm-skip">Skip</button>`;

    if (m.candidates.length === 1) {
      const c = m.candidates[0];
      const borrowerOk = !['price_only', 'split'].includes(m.reason);
      const table = `<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
        <tr><th></th><th style="text-align:left">${escapeHtml(s.source)} · ${escapeHtml(s.auction_id)}</th>
          <th style="text-align:left">ours · ${escapeHtml(c.auction_id)} (${escapeHtml(c.source)})</th><th></th></tr>
        ${pmCell('Bank', escapeHtml(s.bank || '—'), escapeHtml(c.bank || '—'), pmMark(true))}
        ${pmCell('Auction date', escapeHtml(s.auction_day || '—'), escapeHtml(c.auction_day || '—'), pmMark(true))}
        ${pmCell('Reserve price', currency(s.reserve) || '—', currency(c.reserve) || '—', pmMark(pmPriceOk(s, c)))}
        ${pmCell('Borrower', escapeHtml(s.borrower || '—'), escapeHtml(c.borrower || '—'), pmMark(borrowerOk))}
        ${pmCell('EMD', currency(s.emd) || '—', currency(c.emd) || '—', '')}
        ${pmCell('Place', escapeHtml([s.city, s.district].filter(Boolean).join(', ') || '—'),
                 escapeHtml([c.city, c.district].filter(Boolean).join(', ') || '—'), '')}
        ${pmCell('Description', escapeHtml(s.description || '—'), escapeHtml(c.description || '—'), '')}
        ${pmCell('Files', pmLinks(s), pmLinks(c), '')}
      </table>`;
      const buttons = rrBtn('✓ Same property', { kind: 'portal-match',
          payload: { ...base, linked_ids: [c.auction_id], rejected_ids: [] }, verdict: 'approved' }) +
        rrBtn('✗ Not the same', { kind: 'portal-match',
          payload: { ...base, linked_ids: [], rejected_ids: [c.auction_id] }, verdict: 'rejected' });
      return `<div class="pm-row" data-reason="${escapeHtml(m.reason)}" style="border:1px solid var(--border,#ddd);border-radius:8px;padding:10px;margin-bottom:10px">
        ${head}${table}
        <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">${buttons}${skip}${note}</div>
      </div>`;
    }

    const ids = m.candidates.map(c => c.auction_id);
    const subjectLine = `<p class="meta" style="margin:6px 0">${escapeHtml(s.source)} · ${escapeHtml(s.auction_id)} ·
      ${escapeHtml(s.bank || '')} · ${escapeHtml(s.borrower || '—')} · ${currency(s.reserve) || 'no reserve'} ·
      ${escapeHtml(s.auction_day || '')} · ${pmLinks(s)}<br>
      <span style="color:var(--muted)">${escapeHtml(s.description || '')}</span></p>
      <div class="lot-lbl">tick every listing of ours that is this property (duplicate postings included)</div>`;
    const cands = m.candidates.map(c => `<label style="display:flex;gap:8px;align-items:flex-start;border:1px solid var(--border,#eee);border-radius:6px;padding:6px;margin-top:6px">
        <input type="checkbox" class="pm-tick" value="${escapeHtml(c.auction_id)}">
        <span style="flex:1;font-size:12px"><b>${escapeHtml(c.auction_id)}</b> · ${escapeHtml(c.borrower || '—')} ·
          ${currency(c.reserve) || 'no reserve'} ${pmMark(pmPriceOk(s, c))} · ${pmLinks(c)}<br>
          <span style="color:var(--muted)">${escapeHtml(c.description || '')}</span></span>
      </label>`).join('');
    const buttons = rrBtn('✓ Same property', { kind: 'portal-match',
        payload: { ...base, linked_ids: [], rejected_ids: [], ticks: ids }, verdict: 'approved' }) +
      rrBtn('None of these', { kind: 'portal-match',
        payload: { ...base, linked_ids: [], rejected_ids: ids }, verdict: 'rejected' });
    return `<div class="pm-row" data-reason="${escapeHtml(m.reason)}" style="border:1px solid var(--border,#ddd);border-radius:8px;padding:10px;margin-bottom:10px">
      ${head}${subjectLine}${cands}
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">${buttons}${skip}${note}</div>
    </div>`;
  }

  function portalMatchTabs(rows) {
    const counts = {};
    rows.forEach(r => { counts[r.reason] = (counts[r.reason] || 0) + 1; });
    const tab = (reason, label, n) => `<button class="ghost pm-tab" data-reason="${reason}">${label} · ${n}</button>`;
    return `<div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 12px">${
      tab('all', 'All', rows.length)}${
      Object.keys(counts).sort().map(r => tab(r, escapeHtml(PM_REASON_LABEL[r] || r), counts[r])).join('')}</div>
      <p class="meta" style="color:var(--muted)">decisions are saved with who and when, can be undone, and
      take effect on the next linking run</p>`;
  }

```

- [ ] **Step 2: Add the panel**

In `loadResolutionQueues`, find these two consecutive lines (the end of the `lot_matches` block):

```js
        ${d.lot_matches.map(lotMatchRow).join('')}`));
    }
```

and insert directly after them:

```js
    if (d.portal_matches && d.portal_matches.length) {
      blocks.push(rrPanel(`Portal matches — same property?`,
        portalMatchTabs(d.portal_matches) + d.portal_matches.map(portalMatchRow).join('')));
    }
```

- [ ] **Step 3: Handle ticks, notes, tabs and skip**

In the `host.querySelectorAll('.rr-btn').forEach(...)` handler, replace:

```js
      if (args.kind === 'lot-match' || args.kind === 'price-check') {
```

with:

```js
      if (args.kind === 'portal-match') {
        const row = b.closest('.pm-row');
        const note = row?.querySelector('.pm-note')?.value.trim();
        if (note) args.payload.note = note;
        if (args.payload.ticks) {
          const ticked = [...row.querySelectorAll('.pm-tick:checked')].map(t => t.value);
          if (!ticked.length) { alert('tick at least one listing, or use "None of these"'); return; }
          args.payload.linked_ids = ticked;
          args.payload.rejected_ids = args.payload.ticks.filter(id => !ticked.includes(id));
          delete args.payload.ticks;
        }
      } else if (args.kind === 'lot-match' || args.kind === 'price-check') {
```

Immediately before the line `    wireApplyButton();` at the end of `loadResolutionQueues`, add:

```js
    host.querySelectorAll('.pm-tab').forEach(t => t.addEventListener('click', () => {
      const reason = t.dataset.reason;
      host.querySelectorAll('.pm-row').forEach(r => {
        r.hidden = reason !== 'all' && r.dataset.reason !== reason;
      });
    }));
    host.querySelectorAll('.pm-skip').forEach(s => s.addEventListener('click', () => {
      s.closest('.pm-row').hidden = true;
    }));
```

- [ ] **Step 4: Show "decided — Undo" instead of removing the row**

In `decideResolution`, replace:

```js
    const row = btn && btn.closest('.pc-row, .lot-row, .ov-row');
```

with:

```js
    const row = btn && btn.closest('.pm-row, .pc-row, .lot-row, .ov-row');
    if (kind === 'portal-match' && row) {
      row.innerHTML = `<span class="meta">decided (${escapeHtml(verdict)}) — saved with your name; takes effect on
        the next linking run</span> <button class="ghost pm-undo">Undo</button>`;
      row.querySelector('.pm-undo').addEventListener('click', async () => {
        const u = await authFetch(API + '/review/resolution/undo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, payload, verdict }),
        });
        if (!u.ok) { alert('undo failed: ' + u.status); return; }
        loadResolutionQueues();
      });
      return;
    }
```

- [ ] **Step 5: Verify in the browser**

1. Start the API from the worktree: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m uvicorn api.main:app --port 8000` (uses the main checkout's `.env` only if copied; if the API cannot start locally, record that and do the check against a deployed preview instead).
2. Open `web/review.html`, sign in as an admin, open the resolution review stage (`resolve_ok`).
3. With a stored queue present (after `python -m scripts.link_listings --queue-only` on a graph holding portal listings), confirm: the "Portal matches — same property?" panel renders; tabs filter rows; a single-candidate row shows the ✓/✗ table; a batch row shows checkboxes; "Same property" with no tick alerts; a decision replaces the row with "decided — Undo"; Undo reloads the queue with the row back; the browser console shows no errors.
4. If no stored queue exists yet, verify rendering by running in the browser console on the review page: `document.body.insertAdjacentHTML('beforeend', portalMatchRow({subject:{auction_id:'bn-1',source:'baanknet',bank:'Indian Bank',borrower:'A R R TEX',reserve:2944000,auction_day:'2026-09-25'},other_source:'eauctionsindia',reason:'price_only',candidates:[{auction_id:'853518',source:'eauctionsindia',bank:'Indian Bank',borrower:'M/s ARR Tex',reserve:2944000,auction_day:'2026-09-25'}],spot_check:false,snapshot:{}}))` — the row must render with ✓ ✓ ✓ ✗ and no console error.

Record what was verified (and anything that could not be) in the task report.

- [ ] **Step 6: Commit**

```bash
git add web/review.html
git commit -m "review page: portal matches panel — check table, ticks for duplicates, undo

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Measure on the real harvest; bring the spec in line

**Files:**
- Modify: `docs/superpowers/specs/2026-09-15-portal-match-governance-design.md`

**Interfaces:**
- Consumes: everything above; read-only data in `E:/01_vibe_coding/08_auction/data` and a graph snapshot.

- [ ] **Step 1: Run the gap report on the 2026-09-14 snapshot (read-only)**

```bash
MAIN=E:/01_vibe_coding/08_auction
GRAPH=C:/Users/91805/AppData/Local/Temp/claude/E--01-vibe-coding-08-auction--claude-worktrees-local-github-sync-a3da7f/34361033-fffb-4070-ab10-15f65ba173f9/scratchpad/graph_existing.json
OUT=C:/Users/91805/AppData/Local/Temp/claude/E--01-vibe-coding-08-auction--claude-worktrees-local-github-sync-a3da7f/34361033-fffb-4070-ab10-15f65ba173f9/scratchpad/governance
mkdir -p "$OUT"
"$MAIN/.venv/Scripts/python.exe" -m scripts.gap_report --data-dir "$MAIN/data" --downloads-dir "$MAIN/downloads" \
  --existing-json "$GRAPH" --json "$OUT/gap.json" > "$OUT/gap.txt"
grep -E "^(baanknet|bankeauctions)$|  rows " "$OUT/gap.txt"
```

If the snapshot file is missing, stop and report NEEDS_CONTEXT (a fresh snapshot needs Neo4j credentials: `cd $MAIN && .venv/Scripts/python.exe -m scripts.gap_report --save-existing "$GRAPH"`).

Expected, from the brainstorming measurement (which had no twin grouping): combined confirmed ≈ 446, review ≈ 135, new ≈ 267. Twin grouping may move a few rows from review to confirmed. For each source `already loaded + new + review + confirmed == rows`. If any figure is off by more than 15 from the expectation, report DONE_WITH_CONCERNS with both lines and do not change code.

- [ ] **Step 2: Apply the plan's rulings to the spec**

In `docs/superpowers/specs/2026-09-15-portal-match-governance-design.md`:

1. In **Review queue**, replace the first bullet (`_portal_match_candidates(decisions)` builds rows by running `match_listings` over the graph …) with:
   ```markdown
   - `scripts/link_listings.py` — the code that writes the links — stores the review rows
     (every subject in review plus the day's spot-check rows) on
     `(:PipelineState {key:'link_listings'}).review_json`; `--queue-only` stores them without
     writing edges. `_portal_matches(decisions)` serves those rows in the existing
     `GET /review/resolution` response, hiding any whose subject has a decision on the same
     snapshot. Decisions go through the existing `POST /review/resolution/decide` and `/undo`.
   ```
2. In **Safety stop**, replace check 3 with: `3. no listing is both CONFIRMED-linked and in review against the same other source.`
3. In **Testing**, replace the line `- "Shylaja K" vs "Mrs Sailaja.K", same price → \`four_fields\` CONFIRMED.` with `- "N MARIAPPAN" vs "Mr. N. Mariappan", same bank, day and price → \`four_fields\` CONFIRMED.`
4. In **Components → `sources/match.py`**, replace `Keep: \`Candidate\` (add \`emd_num\`, \`title\`, \`description\`, \`city\` for display)` with `Keep: \`Candidate\` (display fields in one \`info\` dict the rule never reads)`.
5. Append under **Measured** the two `rows` lines from Step 1 as "Built rule, 2026-09-14 snapshot".
6. In **Decisions**, replace `` `portal_match_key(subject_id) -> "portal-match:{subject_id}"` (one decision per subject) `` with `` `portal_match_key(subject_id, other_source) -> "portal-match:{subject_id}:{other_source}"` (one decision per subject per other source — one review row) ``, and add `"other_source"` to the payload list.

- [ ] **Step 3: Run the full governance test set once more**

Run: `E:/01_vibe_coding/08_auction/.venv/Scripts/python.exe -m pytest tests/sources tests/pipeline/test_match_confidence.py tests/pipeline/test_resolution_review.py tests/api/test_review_resolution.py tests/scripts/test_link_listings_and_events.py tests/scripts/test_audit_listing_links.py tests/pipeline/test_run_pipeline.py -q -p no:cacheprovider -o addopts=""`
Expected: PASS.

- [ ] **Step 4: Commit**

Save the message below as `commit_message.txt` in the worktree root with the printed numbers substituted, then:

```
spec: portal-match governance matches the build; measured on the 2026-09-14 harvest

Built rule, 2026-09-14 snapshot (6,327 graph listings):
  baanknet       rows 658  new N  review N  confirmed N
  bankeauctions  rows 190  new N  review N  confirmed N

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

```bash
git add docs/superpowers/specs/2026-09-15-portal-match-governance-design.md
git commit -F commit_message.txt && rm commit_message.txt
```
