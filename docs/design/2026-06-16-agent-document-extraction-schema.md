# Design: Agent Document Extraction Schema

Branch: claude/neo4j-access-6dd6qy
Repo: aravindpremkumarp/auction-intelligence
Status: DRAFT
Date: 2026-06-16

## Problem

Every listing is backed by an OCR'd bank **sale notice** (`Document.markdown`,
present on 885/889 properties). Those notices are dense with structured data a
bidder needs — borrower, outstanding dues, reserve price, EMD, bid increment,
inspection window, auction date-time, survey/door numbers, extent, boundaries,
revenue + registration geography, contact officer. Today we extract only a
sliver of it:

- `pipeline/extract_descriptions.py` — pulls **only** `property_description_full`
  (single notices) or `schedules[]` of `{reserve_price_num, property_description_full}`
  (multi notices). Everything else in the notice is discarded.
- `pipeline/prompts/extract_auction.txt` + `verify_and_enrich.py` — a richer
  `verifiable / enrichment / extras` schema, but it runs on **per-file vision OCR**
  (cache: `ocr_results/`), is **not notice-type aware**, and predates the clean
  MinerU markdown now stored on every Document.

This document defines a single, unified extraction **scheme** that runs off the
canonical `Document.markdown`, is notice-type aware (`single` vs `multi`), and
emits the full structured record — reusing the established
`verifiable / enrichment / extras` convention so it merges cleanly into the graph.

## Source material (grounding)

Notices are SARFAESI Act / DRT e-auction sale notices, OCR'd by MinerU into
Markdown that preserves table structure and Unicode fractions (½ ¼ ¾).

| Layout | `notice_type` | Count | Shape |
|---|---|---|---|
| Single lot | `single` | 321 | One reserve price; property may span Item 1/2, Schedule A/B |
| Multi lot | `multi` | 176 | A table of N lots, each with its own reserve price + EMD |

Field families present in essentially every notice: secured creditor & branch,
borrower/guarantor block, outstanding amount + "as on" date, per-lot property
description (survey no., door no., extent, boundaries N/S/E/W, village/taluk/
district, registration district/sub-district), reserve price / EMD / bid
increment, inspection date-time, auction date-time (with auto-extension), and the
authorised officer's contact details.

## Extraction schema

> **Runnable scheme:** the finalized, ready-to-run extraction prompt encoding
> this schema lives at [`pipeline/prompts/extract_enrichment.txt`](../../pipeline/prompts/extract_enrichment.txt).
> That file is the single artifact to run per property across the 889-property
> corpus; this section is the design rationale behind it.

One JSON object per Document. The top level is **notice-type aware**: a `single`
notice yields one `lots[]` entry; a `multi` notice yields one entry per auction
lot. Notice-level fields (creditor, borrower, dates, contact) live once at the
top; per-lot fields (description, price, geography) live inside each lot.

```jsonc
{
  "notice_type": "single" | "multi",
  "lot_count": 1,                         // == lots.length; cross-check vs Document.property_count

  // ── notice-level: one per notice ──────────────────────────────────────────
  "notice": {
    "bank_name":            "string" | null,   // secured creditor
    "branch_name":          "string" | null,
    "authorised_officer":   "string" | null,
    "contact_phones":       ["string"] | [],
    "contact_email":        "string" | null,
    "outstanding_amount_num": number | null,   // rupees; demand total across borrowers
    "outstanding_as_on":    "ISO date" | null,
    "demand_notice_date":   "ISO date" | null,
    "possession_type":      "symbolic" | "physical" | "constructive" | null,
    "sale_terms":           "string" | null    // "As is where is / As is what is"
  },

  "borrowers": [                               // names + roles, as listed
    { "name": "string", "role": "borrower"|"guarantor"|"director"|"partner"|"mortgagor"|null,
      "address": "string" | null }
  ],

  // ── auction logistics (shared unless a lot overrides) ─────────────────────
  "auction": {
    "auction_start_dt":        "ISO date-time" | null,
    "auction_end_dt":          "ISO date-time" | null,
    "auto_extension_minutes":  number | null,
    "application_deadline_dt": "ISO date-time" | null,   // EMD / KYC submission cutoff
    "inspection_dt":           "ISO date-time or range string" | null,
    "platform":                "string" | null            // e.g. e-auction service provider
  },

  // ── per-lot: 1 for single, N for multi ────────────────────────────────────
  "lots": [
    {
      "lot_no": "string" | null,                 // "Property 1", "Sl.No.2", label as printed

      // verifiable — authoritative, cross-checked against scraped fields
      "verifiable": {
        "reserve_price_num":   number | null,     // rupees, e.g. 8100000
        "emd_num":             number | null,     // rupees
        "bid_increment_num":   number | null,     // rupees
        "asset_category":      "string" | null,    // immovable / movable
        "property_type":       "string" | null,    // flat / land / house / plot / ...
        "auction_type":        "string" | null
      },

      // enrichment — schema-stable structured detail
      "enrichment": {
        "property_description_full": "verbatim block" | null,  // [0] unchanged contract w/ existing pipeline
        "address": "full property address as written" | null,  // [5]

        // [2] schedule breakdown — a single lot often spans Schedule A/B/C/D
        // (e.g. A = land, B = undivided share, C = building). One entry each.
        "schedules": [
          { "label": "A"|"B"|"C"|"D"|"string", "type": "land"|"building"|"uds"|"flat"|null,
            "description": "verbatim text for this schedule" | null }
        ],

        // [3] survey numbers incl. subdivision
        "survey_numbers": { "old": ["string"], "new": ["string"], "subdivision": ["string"] },

        // [16] unit identification (flats / apartments)
        "flat_no": "string" | null,
        "block":   "string" | null,
        "floor":   "string" | null,
        "door_numbers": { "old": ["string"], "new": ["string"] },

        // [9] undivided share + the parent land it is carved from
        "undivided_share":   "string with unit, e.g. '453.50 sq.ft UDS' or '1/4 share'" | null,
        "uds_parent_extent": "total land from which the UDS is taken, e.g. '10065 sq.ft'" | null,

        // [10][17] areas — keep verbatim string (preserve ½ ¼ ¾) + a normalized sqft
        "total_area":          "string with unit" | null,
        "super_built_up_area": "string with unit" | null,
        "built_up_area":       "string with unit" | null,
        "carpet_area":         "string with unit" | null,
        "extent_sqft":         number | null,                  // [17] normalized buying size in sq.ft

        // [12] boundary adjacency — what lies on each side
        "boundaries": { "north": "…"|null, "south": "…"|null,
                        "east": "…"|null,  "west": "…"|null },
        // [13] boundary measurements — dimension along each side (distinct from adjacency)
        "boundary_measurements": { "north": "…"|null, "south": "…"|null,
                                   "east": "…"|null,  "west": "…"|null },

        // [11] encumbrances — known charges/dues disclosed in the notice
        "encumbrance": "string (e.g. 'Nil known' or specific charge)" | null,

        // [15] title / [19] approval references
        "sale_deed_no":      "string, e.g. 'Doc No 5682/2020'" | null,
        "approved_layout_no": "string, e.g. '16/2019 DTCP' or 'xxxx/yyyy'" | null,

        // [6][7][8] geography (resolves to graph nodes)
        "village":  "string" | null,
        "taluk":    "string" | null,
        "district": "string" | null,
        "city":     "string" | null,
        "state":    "string" | null,
        "area":     "string" | null,
        "registration_district":     "string" | null,   // [18]
        "registration_sub_district": "string" | null    // [18]
      },

      // extras — open-ended bag (encumbrances, RERA/GST, tax dues, EC refs,
      // valuation report refs, known encumbrances, easements, road access …)
      "extras": { "key_in_snake_case": "value" },

      "enriched_description": "2-4 sentence buyer-facing summary of THIS lot"
    }
  ]
}
```

### Field rules (carried from the existing prompts)

1. **Verbatim, no invention.** Copy exact text/values from *this* document. Never
   carry values over from the website description (supplied for context only).
   Set absent fields to `null` / `[]` / `{}`.
2. **Money → integer rupees.** "Rs. 81,00,000/-" → `8100000`; "Rs. 45 lakh" →
   `4500000`. If only an illegible raw string survives, `null`.
3. **Preserve Unicode fractions** ½ ¼ ¾ exactly — never substitute `1/2`, `/`, `%`.
4. **`property_description_full`** keeps its current contract: the complete
   property-description block (preamble + all Items/Schedules + trailing
   structural details), section labels preserved, boilerplate/terms excluded.
   This keeps `apply_descriptions.py` and description-completeness scoring working
   unchanged.
5. **Single vs multi routing** is decided upstream by `notice_type`; the agent
   emits `lots[]` of length 1 for `single`. `lot_count` must equal `lots.length`
   and is cross-checked against `Document.property_count` (reuse the existing
   `count_mismatch` status).
6. **Dates** as ISO where parseable, else the raw string as shown; downstream
   `_norm_date` already tolerates dd/mm/yyyy.
7. **Adjacency vs measurement are separate.** `boundaries.*` captures *what* lies
   on each side ("Road", "Plot of Mr.X"); `boundary_measurements.*` captures the
   *dimension* along that side ("109 ft", "20 ft"). Notices frequently give both —
   never collapse them into one field.
8. **Schedules stay itemised.** When a single lot is described across Schedule
   A/B/C/D (or Item 1/2…), emit one `schedules[]` entry per schedule **and** keep
   the concatenated `property_description_full` (the legacy contract). Possession
   type stays notice-level (`notice.possession_type`).

## Observed-field coverage

The 20 enrichment points observed in real notices, mapped to the schema:

| # | Observed | Schema path |
|---|---|---|
| 0 | Full description | `enrichment.property_description_full` |
| 1 | Possession type | `notice.possession_type` |
| 2 | Schedule A/B/C/D | `enrichment.schedules[]` |
| 3 | Old / New survey + subdivision no. | `enrichment.survey_numbers.{old,new,subdivision}` |
| 5 | Address | `enrichment.address` |
| 6 | State | `enrichment.state` |
| 7 | District | `enrichment.district` |
| 8 | Village | `enrichment.village` |
| 9 | UDS + parent land | `enrichment.undivided_share` + `enrichment.uds_parent_extent` |
| 10 | Super built-up / built-up / carpet | `enrichment.super_built_up_area`, `built_up_area`, `carpet_area` |
| 11 | Encumbrance | `enrichment.encumbrance` |
| 12 | Boundaries N/S/E/W (adjacency) | `enrichment.boundaries.*` |
| 13 | Boundary measurements N/S/E/W | `enrichment.boundary_measurements.*` |
| 14 | Property type | `lots[].verifiable.property_type` |
| 15 | Sale deed no. | `enrichment.sale_deed_no` |
| 16 | Flat / block / floor | `enrichment.flat_no`, `block`, `floor` |
| 17 | Buying size (sq.ft) | `enrichment.extent_sqft` (+ `total_area` verbatim) |
| 18 | Registration district / sub-district | `enrichment.registration_district`, `registration_sub_district` |
| 19 | Approved layout no. | `enrichment.approved_layout_no` |

## Additional fields from full-corpus review

Sampling one property per `PropertyType` (flat, villa, house, plot, land,
commercial, industrial, godown, cold storage, factory, non-agricultural)
surfaced field variants beyond the original 20. These are in the runnable prompt:

- **Geo coordinates** — `latitude` / `longitude` (some notices print them, e.g. 745829).
- **Patta number** — `patta_no` (revenue title; frequent on TN land).
- **Assessment / tax numbers** — `assessment_no.{old,new}` (door/property-tax assessment).
- **Admin hierarchy** — `panchayat`, `municipality_corporation`, `ward_no`, `hobli`
  (Karnataka/Kerala notices); state is **not** always Tamil Nadu (e.g. Kerala 766551).
- **Landmark** — `landmark` ("Nearby Hotel Deepam…").
- **Loan account no.** — `notice.loan_account_no[]`.
- **Survey-number kinds** — captured as `{kind, value, status}` to cover S.F.No /
  R.S.No / T.S.No / Re Sy No / UDR SF No / Block No without losing subdivisions.
- **Super plinth area** folded into `super_built_up_area`; units span
  sq.ft / sq.m / Ares / Cents / Acres / Hectare (keep verbatim + normalize `extent_sqft`).
- **Multi-lot notices** — primary lot in `property`, extras in `additional_lots[]`.

A second pass over 20 more notices (8 lenders incl. an ARC) added:

- **ARC assignments** — `notice.assignor_bank` + `notice.trust_name` (the seller
  is an asset-reconstruction company, e.g. "ACRE 166 Trust", with the debt
  assigned from the original bank). `bank_name` is the current secured creditor.
- **Revenue records beyond Patta** — `chitta_no` (TN e-Chitta), `khata_no`
  (Karnataka), `property_id_no` (municipal PID).
- **Title deed holder** — `title_deed_holder`, when named apart from the borrower.
- **Multiple loan accounts** — `loan_account_no[]` + total in
  `outstanding_amount_num`, per-account split in `extras.outstanding_by_account`.

A third pass over 30 more notices (incl. ARCIL, Omkara, Piramal, U GRO, Jana SFB,
Karnataka Bank, an IBC liquidation and DRT sales) added:

- **`notice.legal_basis`** — not every notice is SARFAESI. **DRT** sales label the
  price "Upset Price" (→ `reserve_price_num`) and carry OA/TRC/RC/RP case refs;
  **IBC** liquidations sell via a **liquidator** + NCLT order (`liquidator`,
  `court_reference`) and the asset may be movable/intangible (e.g. trademarks,
  742868) → `asset_category: "intangible"`.
- **`notice.assignment_date`** — date of the ARC assignment agreement.
- **`role: "legal-heir"`** added to borrowers (legal heirs of a deceased borrower).
- **`property.branch_of_lot`** — for mega multi-branch auctions (e.g. Karnataka
  Bank 745873) where each lot sits under a different branch.

**Data-quality note for the run:** the corpus contains **duplicate markdown**
across distinct `auction_id`s (e.g. 737966/737973/737974/737977 are identical;
likewise 737162/737163, 734248/734249, 738029/738033) — re-auctions or repeated
docs. De-dupe on `Document.file_path` / markdown hash before/while running so the
same notice isn't extracted (and billed) many times.

## Mapping to the graph

The schema lines up with existing nodes/relationships — no new graph shape needed.

| Schema path | Graph target |
|---|---|
| `notice.bank_name` / `branch_name` | `(:AuctionProperty)-[:CONDUCTED_BY]->(:Bank)-[:HAS_BRANCH]->(:Branch)`, `[:LISTED_BY_BRANCH]` |
| `borrowers[].name` | `(:AuctionProperty)-[:HAS_BORROWER]->(:Borrower)` |
| `lots[].verifiable.reserve_price_num` / `emd_num` | `AuctionProperty.reserve_price_num`, `.emd_num` |
| `lots[].verifiable.property_type` / `asset_category` / `auction_type` | `[:HAS_PROPERTY_TYPE]`, `[:HAS_ASSET_CATEGORY]`, `[:IS_AUCTION_TYPE]` |
| `auction.*_dt` | `AuctionProperty.auction_start_dt` / `auction_end_dt` / `application_deadline_dt` |
| `enrichment.village/taluk/district` | resolve to `RevenueVillage`→`Taluk`→`District` (taluk/village graph already loaded) |
| `enrichment.city/area/state` | `[:LOCATED_IN_CITY]`, `[:LOCATED_IN_AREA]`, `[:LOCATED_IN_STATE]` |
| `enrichment.boundaries/door_numbers/survey_numbers/...` | flatten onto `AuctionProperty` (cf. `flatten_enrichment`) |
| `extras` | `AuctionProperty.extras_json` (string) |
| `bid_increment_num`, `outstanding_amount_num`, contact | new scalar props on `AuctionProperty` (additive) |

New verifiable signals worth promoting to first-class properties: `bid_increment_num`,
`outstanding_amount_num` + `outstanding_as_on`, `possession_type`, `inspection_dt`.

## Agent extraction workflow

Mirror the existing `extract_descriptions.py` orchestrator so caching, status,
and concurrency behaviour stay identical:

1. **Worklist** — `Document` where `markdown` is non-empty and
   `notice_type IN ['single','multi']` (reuse `fetch_extraction_work`, incl.
   `--missing-only` safe backfill).
2. **Prompt routing** — `single` → single-lot prompt; `multi` → multi-lot prompt.
   Same model knobs (`OPENROUTER_MODEL_DESCRIPTION_SINGLE/MULTI`).
3. **Call** the LLM with the prompt + the MinerU markdown (the existing
   "layout-aware OCR" framing line stays).
4. **Validate** — JSON parse, `lot_count == lots.length`, every lot has a
   non-empty `property_description_full`; on multi, cross-check against
   `Document.property_count` → `count_mismatch`.
5. **Cache** to a new versioned dir (e.g. `cache/notice_extractions_v1/<safe>.json`)
   so existing `notice_descriptions_v3` caches are untouched.
6. **Status** — reuse `Document.description_extraction_status`
   {pending, cached, applied, failed, count_mismatch, needs_reextract}.
7. **Apply** — an `apply_extractions.py` step writes verifiable/enrichment/extras
   onto `AuctionProperty` and resolves the geography/bank/borrower relationships,
   honouring the existing PDF-is-source-of-truth conflict policy
   (`<field>_scraped` + `field_conflicts`) from `verify_and_enrich.py`.

### Migration / rollout

- **Additive.** New cache dir + new prompts; the legacy `property_description_full`
  contract is preserved, so description scoring and `apply_descriptions.py` keep
  working during transition.
- **Backfill** the 885 documents that already have markdown; re-run is idempotent
  via the cache + `applied` status.
- The 4 properties without markdown (3 with no `Document`, 1 with a doc but no
  extracted markdown — auction_id 763405) are out of scope until their markdown
  exists; surface them in the worklist report, don't fabricate.

## Open questions

1. Promote `bid_increment_num`, `outstanding_amount_num`, `possession_type`,
   `inspection_dt` to first-class `AuctionProperty` properties, or keep in `extras`?
2. For multi notices, do we split into N `AuctionProperty` nodes, or keep one node
   with a `lots`/schedule sub-structure? (Current graph has one node per listing;
   today's `schedules[]` is concatenated into a single description.)
3. Resolve `village`/`taluk` against the loaded `RevenueVillage`/`Taluk` graph at
   extraction time, or in a separate geo-resolution pass?
