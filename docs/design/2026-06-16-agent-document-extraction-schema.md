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
        "property_description_full": "verbatim block" | null,  // unchanged contract w/ existing pipeline
        "undivided_share":  "string with unit" | null,
        "total_area":       "string with unit" | null,         // preserve ½ ¼ ¾
        "built_up_area":    "string with unit" | null,
        "survey_numbers":   { "old": ["string"], "new": ["string"] },
        "door_numbers":     { "old": ["string"], "new": ["string"] },
        "boundaries":       { "north": "…"|null, "south": "…"|null,
                              "east": "…"|null,  "west": "…"|null },
        "village":  "string" | null,
        "taluk":    "string" | null,
        "district": "string" | null,
        "city":     "string" | null,
        "state":    "string" | null,
        "area":     "string" | null,
        "registration_district":     "string" | null,
        "registration_sub_district": "string" | null
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
