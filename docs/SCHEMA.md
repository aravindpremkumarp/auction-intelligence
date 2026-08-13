# Auction graph schema

The Neo4j model that unifies two sources: **scraped listings** (2,464
`:AuctionProperty`) and **LangExtract notice extractions** (245 of 1,348
`:Document` extracted so far).

One rule decides every modelling call: *anything you search or join by becomes
a node; everything else is a property.*

```
:Document ──HAS_LOT──> :Lot ──IS_PARCEL──> :Parcel <──IS_PARCEL── :AuctionProperty
   notice file          one lot of         the physical land        the listing
                        one notice         across notices/years     (unchanged)
```

- **`:Lot`** — the unit LangExtract actually extracts. A notice can sell many
  lots; the old flat model had nowhere to put them. Key: `filename#lot_index`.
- **`:Parcel`** — the land itself. Replaces `:SAME_PROPERTY_AS`, which as a
  pairwise guess permitted the contradiction A=B, B=C, A≠C. A shared parcel
  cannot. It also makes price history a query: every `:Auction` on one parcel,
  in date order.
- **`:AuctionProperty`** — untouched, still authoritative for the website.
  Notice values live on `:Lot` / `:Auction`, so a notice/website disagreement
  stays visible instead of one silently overwriting the other.

## Running it

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
`ALIAS_OF` edge. Dropping them would blind 2,014 of 2,464 properties.

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

Promotion is gated on `extraction_json IS NOT NULL`, **not** on review status —
all 245 extracted documents are still `extraction_review_status = 'pending'`,
so gating on `'verified'` would promote nothing. Verification is instead
recorded per node (`verified_at` / `verified_by`) so a trusted-subset query
stays possible.

Geography edges carry `source` (`langextract` | `scraped`) and `resolved_at`,
which makes re-resolution a query rather than a re-migration as extraction
coverage grows.

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

## Field coverage (245 extracted documents)

What is safe to build on today.

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
