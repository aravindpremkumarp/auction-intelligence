# Portal-match governance — design

Status: approved in brainstorming 2026-09-15. Replaces the "same property" rule of
`sources/match.py` (PR #472) before BAANKNET and bankeauctions listings are linked
into the graph (`scripts/link_listings.py`, pipeline stage 5a).

## Why

The first Tamil Nadu harvest (BAANKNET 658, bankeauctions 190 listings) is about to
be linked to the 6,327 eauctionsindia listings in Neo4j. `scripts/build_spine.py`
merges CONFIRMED/PROBABLE `:SAME_LISTING_AS` pairs into one `:AuctionEvent` and
`api/canonical.py` hides lower-ranked copies, so a wrong pair makes a real property
disappear or merge with another. The evidence-tier rule shipped in #472 was hard to
reason about and graded some pairs on clues a reviewer cannot check at a glance.

The governance layer protects against **wrong matches and merges**. It does not gate
listing data quality, and it does not require sign-off on new listings.

## The rule

Two listings are compared only when they come from **different sources**, name the
**same bank** (`pipeline.entity_resolution.org_key`) and the **same auction day**
(`sources.match.day_of`). The *subject* is the listing from the better-ranked source
(`sources.base.SOURCE_RANK`: baanknet 1, bankeauctions 2, eauctionsindia 3); the
*candidates* are that bucket's listings from one other source.

Agreement on the two remaining fields:

- **Reserve price** — both present and equal to the rupee (`abs(a - b) < 1`).
- **Borrower** — `sources.match.borrower_matches` (honorifics stripped,
  `token_set_ratio >= 90`); a missing borrower never agrees.

Identical postings of one unit on one source (equal villa/flat/plot/door numbers,
rounded reserve and borrower key — `sources.match._twin_groups`) count as one
candidate; a decision on the group applies to every member.

| Situation (per subject, against one other source) | Outcome | Method | Grade |
|---|---|---|---|
| Exactly one candidate agrees on price **and** borrower, and no villa/flat/plot number disagrees | link | `four_fields` | CONFIRMED |
| Several agree on price and borrower; a villa/flat/plot/door number held by exactly one of them matches the subject | link that one | `unit_number` | CONFIRMED |
| Several agree on price and borrower; no unit number separates them | review `batch` | `review` | PENDING |
| One candidate agrees on both, but villa/flat/plot numbers disagree (after `_unit_key` normalisation) | review `units_disagree` | `review` | PENDING |
| Price agrees with a candidate, borrower agrees with none | review `price_only` | `review` | PENDING |
| Borrower agrees with a candidate, price agrees with none | review `borrower_only` | `review` | PENDING |
| Price agrees with one candidate and borrower with a different one | review `split` | `review` | PENDING |
| Two subjects would link to the same candidate group | review `contested` (both) | `review` | PENDING |
| No candidate agrees on price or borrower | none — the subject is **new** | — | — |
| A stored human decision covers the subject (see Decisions) | per decision | `decision` | CONFIRMED |

PENDING edges are written one per candidate so the waiting state is visible in the
graph; `build_spine` (`MERGE_GRADES`) and `api/canonical` (`BRIDGE_GRADES`) merge only
CONFIRMED/PROBABLE, so both listings stay separate on the site while a case waits.

Measured on the 2026-09-14 harvest against the graph snapshot (exact price; measured
without twin grouping, so duplicate eauctionsindia postings counted as a batch — the
built rule may confirm slightly more and send slightly fewer to review):

| | BAANKNET | bankeauctions | total |
|---|---|---|---|
| CONFIRMED (`four_fields`) | 334 | 108 | 442 |
| CONFIRMED (`unit_number`) | 4 | 0 | 4 |
| review `price_only` | 73 | 4 | 77 |
| review `batch` | 44 | 1 | 45 |
| review `borrower_only` | 6 | 2 | 8 |
| review `units_disagree` | 5 | 0 | 5 |
| new | 192 | 75 | 267 |

"New" still includes re-auctions of properties the graph already holds on a different
day; linking those is `scripts/link_reauctions.py`'s job, unchanged.

Built rule, 2026-09-14 snapshot:

```
baanknet       rows 658  already loaded 0  new 192  review 111 (batch 23, borrower_only 6, contested 4, price_only 73, units_disagree 5)  confirmed 355 (four_fields 348, unit_number 7, decision 0)
bankeauctions  rows 190  already loaded 0  new 75  review 6 (borrower_only 2, price_only 4)  confirmed 109 (four_fields 109, unit_number 0, decision 0)
```

The notice-file, boundary, survey-number and description clues from #472 no longer
decide anything; they are shown to the reviewer as supporting detail.

## Components

### `sources/match.py` — the rule (pure)

- Replace the evidence tiers with the rule above. Keep: `Candidate` (display fields in
  one `info` dict the rule never reads), `day_of`,
  `extract_identifiers`, `_unit_key`, `_twin_groups`, `borrower_matches`,
  `Ambiguity` (field `reason` takes the review reasons above), `MatchResult`.
- `match_listings(incoming, existing, *, decisions=()) -> MatchResult` returns
  `pairs` (`Pair` with method `four_fields` / `unit_number` / `decision` / `review`)
  and `ambiguous` (one `Ambiguity` per subject in review, candidates listed).
- `find_same_listing_pairs` keeps its signature.

### `pipeline/match_confidence.py`

- `SAME_LISTING_CONFIDENCE = {"four_fields": CONFIRMED, "unit_number": CONFIRMED,
  "decision": CONFIRMED, "review": PENDING}`; add `PENDING = "PENDING"`.
- `tests/pipeline/test_match_confidence.py` updated for the new vocabulary and grade set.

### Decisions — `pipeline/resolution_review.py`, `api/review/queries.py`

- New kind `portal-match` in `KINDS`; `portal_match_key(subject_id, other_source) ->
  "portal-match:{subject_id}:{other_source}"` (one decision per subject per other
  source — one review row).
- Payload: `{"subject_id", "other_source", "linked_ids": [...], "rejected_ids": [...],
  "snapshot": {"bank", "reserve_price", "borrower", "auction_day"}, "note",
  "spot_check": bool}`.
- Verdicts:
  - **approved** ("Same property", with ticks) → every `linked_ids` candidate (and its
    twin group) links with method `decision`, CONFIRMED.
  - **rejected** ("Not the same" / "None of these") → `rejected_ids` are removed from
    the subject's candidates before the rule runs, forever; the subject is re-evaluated
    without them (usually becoming new).
  - **Skip** stores nothing.
  - **Undo** — existing `undo_resolution_decision` — reopens the case.
- **Staleness**: a decision whose `snapshot` no longer equals the subject's current
  bank / reserve price / borrower / auction day is ignored and the case reopens.
- `record_resolution_decision` validates a `portal-match` payload: at least one linked or
  rejected listing; an approval links at least one; the subject and every linked/rejected
  listing must exist. The `snapshot` is read from the graph server-side and replaces any
  sent by the caller. Candidacy is not checked at decide time: the matcher applies a
  decision's ids only inside the subject's own bank + day bucket, so an id from elsewhere
  is never used.

### Review queue — `api/review/queries.py`, `api/review/router.py`, `web/review.html`

- `scripts/link_listings.py` — the code that writes the links — stores the review rows
  (every subject in review plus the day's spot-check rows) on
  `(:PipelineState {key:'link_listings'}).review_json`; `--queue-only` stores them without
  writing edges. `_portal_matches(decisions)` serves those rows in the existing
  `GET /review/resolution` response, hiding any whose subject has a decision on the same
  snapshot. Decisions go through the existing `POST /review/resolution/decide` and `/undo`.
- Layout A (chosen in the mockup):
  - tabs by reason with counts (All · price_only · batch · borrower_only ·
    units_disagree · contested · spot-check);
  - each row: subject vs candidate in a check table — Bank / Auction date / Reserve
    price / Borrower with ✓ ✗, then place, description, EMD, notice and photo links,
    portal link;
  - single-candidate rows: **Same property** · **Not the same** · **Skip** + note;
  - batch rows: one line per candidate with a checkbox and the survey/unit hint pills;
    **Same property** (links every ticked one) · **None of these** · **Skip** + note;
  - footer: decisions are saved with who and when, can be undone, and take effect on
    the next linking run.

### Safety stop — `scripts/link_listings.py`

Before `DROP_EXISTING`, `link_listings.run` checks and, on any failure, writes nothing
and exits non-zero with the reason:

1. same-source members of one CONFIRMED cluster (`scripts/audit_listing_links.same_source_clusters`)
   must agree on reserve price and villa/flat/plot numbers — identical twin postings pass;
2. no PENDING review pair joins two listings that CONFIRMED links already put in one cluster
   (a chain such as bn→ea and be→ea confirmed while bn↔be waits for review);
3. no listing is both CONFIRMED-linked and in review against the same other source.

### Spot-check

Each linking run selects 10 CONFIRMED `four_fields`/`unit_number` (subject, other
source) pairs with no `portal-match` decision for that pair yet (deterministic:
sorted by pair, sampled with the run date as seed); each becomes its own spot-check
row and lists them in the queue under the spot-check tab. Links are
rewritten every run, so "not yet spot-checked" is read from the decisions, not from
edge history. They stay linked; a **Not the same**
verdict stores a rejected decision, so the next run removes the link and never
proposes it again.

### `scripts/gap_report.py`

- Uses the new `match_listings`; per source reports `confirmed`, `review` (with the
  per-reason split), `new`, and `already_loaded`;
  `already_loaded + new + review + confirmed == rows`.

## Error handling

- A listing with no bank or no auction day is never compared (no bucket) → new.
- A stored decision whose snapshot no longer matches the subject, or whose linked
  listings are no longer among its candidates, is ignored; its case reappears in the
  review queue. There is no separate stale-decision count.
- The review queue endpoint returns the panel empty (not an error) when the graph
  holds no portal listings yet.

## Testing

Pure tests, no database (`tests/sources/test_match.py`, `tests/pipeline/test_resolution_review.py`):

- ARR Tex (`bn-359756` vs `853518`: price ✓, "A R R TEX" vs "M/s ARR Tex") → review `price_only`.
- Ekadanta Enterprises (`bn-359636` vs `842118`/`842546`/`844968`) → review `batch`.
- A batch where one candidate's flat number matches the subject → `unit_number` CONFIRMED.
- Four fields agree but plot 45 vs plots 44/47 → review `units_disagree`.
- "N MARIAPPAN" vs "Mr. N. Mariappan", same bank, day and price → `four_fields` CONFIRMED.
- Two subjects agreeing with one candidate → both review `contested`.
- Decisions: approve with two ticks links both; reject removes the pair and the subject
  becomes new; undo reopens; a changed reserve price in the snapshot reopens.
- Safety stop: each of the three checks, when violated, writes nothing.
- Spot-check selection is deterministic for a given run date.

Real data: `python -m scripts.gap_report --existing-json <2026-09-14 snapshot>` reports
≈ 446 confirmed · 135 review · 267 new.

UI: open `web/review.html`, approve one case, reject one, undo one; no console errors.

## Rollout

1. Build on branch `claude/portal-match-governance`; PR; merge.
2. Pipeline step 4 (`load_tn_to_neo4j --backfill-source`) is unchanged — it loads, it
   does not link — and still waits for explicit approval.
3. Work the review queue at any time; an unreviewed case only means two separate
   listings on the site.
4. Pipeline step 5 (`run_pipeline`): stage 5a runs the safety stop, then writes
   CONFIRMED + approved links and PENDING review edges; 5b builds the spine.

## Out of scope

- Data-quality gating of listings, sign-off on new listings, batch rollback.
- Re-auction linking across days.
- Expired BAANKNET listings and their photos.
