# Auction graph schema

The Neo4j model that unifies two sources: **scraped listings**
(2,964 `:AuctionProperty`) and **LangExtract notice extractions**
(1,625 of 1,625 `:Document` extracted).

> **Status: live.** The promotion runs as stages 4.4 / 4.5 of
> `pipeline/run_pipeline.py`. Counts below were read off the live graph on
> 2026-09-12:
>
> | | nodes | | edges |
> |---|---|---|---|
> | `:Lot` | 3,393 | `:Document`-`HAS_LOT`→`:Lot` | 3,393 |
> | `:Parcel` | 3,265 | `:AuctionProperty`-`IS_LOT`→`:Lot` | 2,975 |
> | `:Identifier` | 11,113 | `IS_PARCEL` (both ends) | 16,144 |
> | `:ResolutionDecision` | 3,077 | `:SAME_PROPERTY_AS` | 80 |
>
> 3 listings hold no `IS_LOT` edge; 94 parcels group more than one lot.
> 1,423 lots resolved to a `:RevenueVillage`.
>
> Where a number further down is dated or sampled, it is labelled as such —
> those are drafting-time measurements kept for the reasoning they support,
> not current totals.

One rule decides every modelling call: *anything you search or join by becomes
a node; everything else is a property.*

```
:Document ──HAS_LOT──> :Lot ──IS_PARCEL──> :Parcel <──IS_PARCEL── :AuctionProperty
   notice file          one lot of         the physical land        the listing
                        one notice         across notices/years     (unchanged)
```

- **`:Lot`** — the unit LangExtract actually extracts. A notice can sell many
  lots; the old flat model had nowhere to put them. Key: `filename#lot_index`.
- **`:Parcel`** — the land itself. Meant to supersede `:SAME_PROPERTY_AS`,
  which as a pairwise guess permitted the contradiction A=B, B=C, A≠C. A
  shared parcel cannot. It also makes price history a query: every `:Auction`
  on one parcel, in date order. The old edge is still written (stage 5,
  `scripts/link_reauctions.py`, 80 edges live) and has not been retired — read
  parcels, not `SAME_PROPERTY_AS`, for new work.
- **`:AuctionProperty`** — untouched, still authoritative for the website.
  Notice values live on `:Lot` / `:Auction`, so a notice/website disagreement
  stays visible instead of one silently overwriting the other.

## Running it

Normally this runs as part of the orchestrator, which also classifies notices,
applies the extractions to `:AuctionProperty` and links re-auctions:

```bash
python -m pipeline.run_pipeline               # stages 1.3, 4.4, 4.5, 5, 6
```

The three steps this document describes, run on their own:

```bash
python -m scripts.init_graph_schema          # constraints + indexes (additive)
python -m pipeline.resolve_places --report   # phase A — geography
python -m pipeline.promote_extractions       # phase B + C — extractions, parcels
```

All three are idempotent and take `--dry-run`. **Nothing is deleted at any
phase** — `LOCATED_IN_CITY` / `LOCATED_IN_AREA` keep working throughout, so no
API change is required.

If Bolt (7687) is blocked — Claude Code on the web, or any HTTP-only egress
proxy — prefix with `NEO4J_HTTP_API=1` to route through Aura's HTTPS Query API.

Phase A needs no extraction at all and resolves ~94% of the un-extracted
backlog on its own, so it can ship before the loader.

---

## Every LangExtract class, and where it lands

The 15 classes in `pipeline/langextract_examples.py`, plus the field catalogue
in `pipeline/prompts/extract_enrichment.txt`.

| class | graph home | notes |
|---|---|---|
| `secured_creditor` | `:Bank` ←`ISSUED_BY`, `:LegalFramework`, `:Trust`, `:Officer`, `:CaseReference` | ARC assignor via `DEBT_ASSIGNED_FROM {assignment_date}`; a court order is **not** a bank |
| `borrower` | `:Borrower` ←`HAS_PARTY {role}` | the 8-value role enum the existing `HAS_BORROWER` edge loses |
| `contact` | `:Contact` | shared — one officer's phone recurs across notices |
| `property` | `:Lot` props + `TITLE_HELD_BY`, `OF_BRANCH` | |
| `full_description` | `Lot.full_description` | the verbatim source of truth, kept whole |
| `location` | `:PlaceAlias` → 3 hierarchies + `:Locality` | see *Geography* below |
| `identifier` | `:Identifier` ←`MENTIONS_IDENTIFIER {as_written}` | 17 kinds; the dedup key that builds `:Parcel` |
| `extent` | `:Measurement` ←`HAS_EXTENT {kind, is_headline}` → `:Unit` | see *Measurement* below |
| `boundary` | `:Boundary` ←`HAS_BOUNDARY {side}` | see *Boundaries* below |
| `schedule` | `:Schedule` ←`HAS_SCHEDULE` | genuinely 1:N (Schedule A/B/C, Item 1/2/3) |
| `auction_terms` | `:Auction` ←`OFFERED_IN` | a node, not props: re-auctions accrue per parcel |
| `outstanding` | `:LoanAccount` ←`SECURES {outstanding_num, as_on, …}` | unique `account_no`, so repeat notices for one loan join |
| `emd_account` | `:EMDAccount` ←`EMD_PAYABLE_TO` | one zonal account serves many notices |
| `full_terms` | `:TermsTemplate` ←`USES_TERMS` | hash-deduped; "non-standard terms" becomes one hop |
| `extras` | `:Fact` ←`HAS_FACT` | `{key, value}`, from `:Lot` or `:Document` |

Only **two** classes flatten to properties, and both are strictly 1:1 with a
lot and never joined on. Everything that repeats, joins, or accrues history is
a node.

---

## Geography

Three **parallel government hierarchies**, deliberately not merged — a property
in Chengalpattu revenue district can register at Kancheepuram:

| hierarchy | chain |
|---|---|
| revenue | `:RevenueVillage → :Taluk → :District → :State` |
| registration (SRO) | `:RegSubDistrict → :RegDistrict` |
| civic | `:LocalBody {kind}` + `ward_no` on the edge |

### `:City` and `:Area` are demoted, never dropped

They were never hierarchy levels — they are untyped text slots holding names
from four levels at once:

- `:City` (49) = 36 districts + 8 taluks + 5 misspellings
- `:Area` (1,035) = 262 taluks + 9 districts + 242 villages + 522 real localities

But they are also the **only** geography 82% of properties have, so they keep
their labels and edges and merely gain a `:PlaceAlias` label plus one
`ALIAS_OF` edge. Dropping them would blind 2,014 of 2,464 properties (measured
when this model was drafted, at 2,464 properties; the corpus has since grown to
2,822 — the ratio, not the absolute count, is the point).

### Resolution is bottom-up and district-scoped

Tamil Nadu split several districts in 2019 (Chengalpattu out of Kancheepuram;
Ranipet and Tirupathur out of Vellore — note district codes 35/36/37). `:City`
kept the **old** names while `:Area` holds the actual taluk, so matching
City→District by name mis-files 240+ properties.

And village — the finest level — is the **least** unique: 1,150 of 15,122
village names are duplicated across 3,192 villages, one appearing 22 times.
133 `:Area` names match *both* a village and a taluk (taluk HQs share their
village's name), and only 26 say "Taluk" in the text.

So `pipeline/resolve_places.py` runs five steps:

1. **anchor** — a coarse district from `:City`, used *only* as a search filter
2. **explicit taluk** — "Taluk"/"Tk" in the text outranks a village match
3. **village, scoped to the anchor** — never matched globally
4. **taluk** — coarser fallback
5. **city** — district-level last resort, only where the area resolved nothing

The stored district is always **derived upward** from whatever resolved.
`:City` is a hint for *searching*, never a source for *answering*. More than one
scoped candidate sets `ambiguous = true` rather than guessing — a visible
backlog instead of a silent wrong answer.

---

## Measurement

`:Measurement` carries one extent with `kind`, `raw`, `value`, `unit`,
`sqft_norm`, `norm_method`, and hangs off `:Unit` for conversion.

**Why a node.** 41% of extracted extents (301 of 734) had no comparable
number, and the misses were almost entirely non-sq-ft units — acre 79%
missing, are 78%, cent 54%, hectare 100%, against square feet at 2%.
Conversion is deterministic arithmetic; `pipeline/measures.py` does it from a
factor table rather than asking a model.

**Unit matching is longest-first, and that is load-bearing:** `squARE feet`
and `hectARE` both contain "are". Matching naively converts square feet as
ares — a 1,076× error. (`acre` does *not* contain `are`.)

**`is_headline` names the price-per-sqft denominator** so no query has to
guess. Flat → `built_up`; land/plot → `total`. `uds_parent` is **never**
eligible: dividing one flat's price by the whole apartment plot understates
price/sqft by an order of magnitude. When a notice omits `property_type`, a
present UDS identifies a flat — only flats hold an undivided share.

> **Open decision.** House and villa legitimately have *two* areas (land extent
> and built-up). The loader currently takes land, because comparables are
> quoted on it. This is a judgement call recorded in `measures._LAND_LIKE`, not
> an inference — revisit it before trusting price/sqft on those types.

---

## Boundaries

`:Boundary` carries `side`, `adjacency_raw`, `access_kind`, `road_width_ft`,
`measurement_raw`, `measurement_ft`, `is_length_valid`.

The extraction already separates *what abuts* a side from *the parcel's own
dimension* along it — zero road-into-measurement pollution across 1,701
boundaries. What the flat model lost was everything inside the adjacency
string:

- **`road_width_ft`** — buried in 178 adjacency strings ("23 Feet wide
  East-West Road"). Road width governs vehicle access and, in many municipal
  rules, permissible setback and FSI. `"properties fronting a 30ft+ road"` was
  unanswerable.
- **`access_kind`** — separates three things that look alike in text:
  `20 feet Road` (frontage), `15 Feet Common Pathway` (much weaker access),
  and `30 FT LAND LEFT BY ROAD` (a widening **setback**, which *reduces* the
  usable parcel — the opposite meaning).
- **`is_length_valid`** — 7 boundary "measurements" are actually areas
  (`19 Sq.Ft`). That is an extraction bug; flagging it stops it corrupting
  plot-shape maths.

Only 43% of boundary sides carry a measurement at all, so plot shape and
frontage remain unreconstructable for most lots.

---

## Provenance

Promotion is gated on `extraction_json IS NOT NULL`, **not** on review status.
The gate was written when every extracted document was still
`extraction_review_status = 'pending'` and gating on `'verified'` would have
promoted nothing; review has since caught up — 1,323 verified against 302
pending on 2026-09-12 — so a verified-only gate is now a real option rather
than an empty one. It remains ungated by choice: a pending document is
promoted, and verification is recorded per node (`verified_at` /
`verified_by`) so a trusted-subset query stays possible.

Geography edges carry `source` (`langextract` | `scraped`) and `resolved_at`,
which makes re-resolution a query rather than a re-migration as extraction
coverage grows.

### `IS_LOT` carries how the listing was matched

The edge **is** the resolution — `AuctionProperty.resolved_lot_key` was
retired (0 live) because a key is only a way to find a node, and `lot_index`
is the extraction model's own numbering, so every stored key was a guess that
a re-extraction silently invalidated.

`IS_LOT.method` records which signal decided the match, and `linked_at` when.
`pipeline/apply_extractions._EXPLAIN_TEXT` holds the reader-facing sentence
for each. Live distribution, 2026-09-12:

| method | edges | |
|---|---|---|
| `exact` | 1,772 | reserve price matches the lot exactly |
| `single` | 991 | the notice sells one lot |
| `borrower` | 64 | borrower name separated lots that tied on money |
| `portal_aid` | 53 | the extraction's own claim, admitted only unopposed |
| `identifier` | 42 | a survey/door number in the listing names one lot |
| `decision` | 21 | a human picked it in the review UI |
| `emd` | 18 | EMD matched (the portal showed no reserve price) |
| `tolerance` | 11 | reserve price within 1% |
| `description` | 3 | listing text reads much more like this lot |

`remainder` — the last unplaced listing paired with the last free lot — is in
the vocabulary but has produced **no live edge**, because `sole_claimants`
withholds a match a second listing also claims.

The tiers are not equally strong, so `IS_LOT.confidence` grades them —
`pipeline/match_confidence.py` is the only table, read by both writers and by
`scripts/backfill_is_lot_confidence.py`:

| confidence | methods | edges |
|---|---|---|
| `CONFIRMED` | `exact`, `single`, `decision` | 2,784 |
| `PROBABLE` | `identifier`, `emd`, `emd_tolerance`, `tolerance` | 71 |
| `INFERRED` | `borrower`, `portal_aid`, `description`, `remainder` | 120 |
| `UNKNOWN` | anything unrecognised | 0 |

The two fields are deliberately separate. `confidence` does not encode the
*reason* for confidence — that is what `method` is for, and collapsing them
would lose the difference between a deterministic price match and a person who
opened the notice and picked. Both read `CONFIRMED`; only `method` says which.

An unrecognised method grades `UNKNOWN`, never `CONFIRMED`: a future tier must
not become high-confidence because someone forgot the table. The write path
degrades quietly (it runs inside a batch and must not abort one); the loud half
is `tests/pipeline/test_match_confidence.py`, which fails CI when a reason
exists in the vocabulary with no grade.

An automated verdict is also mirrored as a `(:ResolutionDecision {kind:
'lot-match'})` (2,940 system, 23 human) so the review UI can re-apply a
human's pick; a system decision whose match stops holding is deleted with the
edge, a human's never is.

**Both geo links are kept on purpose.** After a notice supersedes a scraped
value, the scraped side stays linked: where the two resolve to different
villages, that is a scraper-bug or wrong-property-match detector — the same
class of signal as the existing `description_wrong_property`.

### `Document.expected_lot_count` — the human's lot count

A reviewer confirms how many lots a notice sells at the classification gate,
and it lands on the `:Document` as `expected_lot_count` (confirming a notice
as `single` implies 1). It is deliberately a **human** number, not a derived
one: the count cannot be read reliably off the notice, because lots routinely
share a reserve price and a borrower, and tables survive OCR unevenly.

It earns its place by being used twice:

- **Into extraction** — the count is written into the LangExtract prompt, so
  the model is told how many lots to find and how to number them
  (`lot_index` 1..N).
- **Back out of extraction** — the review queue compares it against the
  distinct `lot_index` values actually extracted; a mismatch is how a missed
  or invented lot surfaces before it reaches `:Lot` / `:Parcel`.

Null means no claim: a document without a confirmed count is never flagged.
Priming does soften the second use — once the model is told "5 lots", a
matching count is weaker evidence than an independent agreement would be —
but it still catches the hard failure, where the model cannot find them.

---

## Parcel resolution is a second pass

You cannot tell which lots share a parcel until every identifier in the corpus
exists, so phase C runs after all lots are promoted:

1. merge lots sharing an identifier **and** a revenue village
2. give every remaining lot its own singleton parcel
3. link listings via their document
4. attach identifiers to the parcel
5. number auction attempts per parcel — and an auction followed by a later one
   on the same parcel **did not sell**, so `outcome = 'unsold'` backfills from
   data already held

Identifier matching is village-scoped because survey numbers repeat across the
state. A bad merge is far harder to undo than a missed one, which is why the
evidence (`IS_PARCEL.confidence`, `.method`, `Parcel.evidence`) lives on the
edge: reversing one is a `DELETE`, not a rebuild.

---

## Field coverage (sampled on 245 extracted documents)

What is safe to build on. Measured on the first 245 extractions, before the
corpus reached 1,530 — the denominators below are entity counts within that
sample, so treat these as ratios rather than current totals. Re-run
`pipeline/validators.py` over the full corpus to refresh them.

| field | coverage | |
|---|---|---|
| `asset_category` | 524/524 — **100%** | both `immovable` and `movable` present |
| `inspection_dt` | 420/593 — 71% | |
| `bid_increment_num` | 399/593 — 67% | |
| `possession_type` | 311/524 — 59% | 3 clean values, zero garbage |
| `extent_sqft` | 433/734 — 59% | → ~95% after unit conversion |
| `auto_extension_minutes` | 251/593 — 42% | |
| `encumbrance` | 218/524 — 42% | a bank's *claim*, not a title search |
| `title_deed_holder` | 125/524 — 24% | |
| `construction_type` | 32/524 — 6% | too thin |
| `latitude`/`longitude` | 29/524 — 5.5% | map features not viable |
| `landmark` | 8/524 — 1.5% | |
| `occupancy_status` | **1/524 — 0.2%** | zero `vacant`, zero `tenanted` |

The thin fields correlate with prompt examples, not with what notices contain:
`encumbrance` has 2 demonstrations → 42%; `occupancy_status` and `landmark`
have none → 0.2% and 1.5%. `langextract_examples.py:24` documents the
mechanism — an attr demonstrated nowhere gets suppressed, which is what
happened to `hobli`. Adding one example each is the cheap test before
concluding the data isn't there.
