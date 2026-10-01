# Knowledge-graph schema for the property extraction pipeline

Date: 2026-07-20
Status: proposal
Inputs: live Neo4j schema (introspected via APOC), `pipeline/langextract_examples.py`
(15 grounded entity classes), `pipeline/prompts/extract_enrichment.txt` (canonical
field catalogue), `pipeline/apply_extractions.py`, `docs/extraction-pipeline-review-2026-07.md`.

This document designs the target Neo4j schema that makes the grounded
LangExtract output (Path B) a first-class graph citizen, unifies the two
location hierarchies, separates the physical property from the auction event,
and attaches provenance + confidence to every extracted fact.

---

## 1. Current knowledge-graph analysis

### 1.1 Node labels (live counts)

| Label | Count | Key | Role |
|---|---|---|---|
| `AuctionProperty` | 2,464 | `auction_id` (unique) | Hub: scraped listing + auction terms + flattened extraction fields |
| `Document` | 1,348 | `storage_key`, `file_path` (unique) | Notice file + OCR/markdown state + `extraction_json` blob |
| `Borrower` | 4,730 | none | Borrower per listing, keyed by raw name string |
| `Bank` | 124 | `name` (unique) | Secured creditor |
| `Branch` | 501 | none | Bank branch |
| `City` | 49 / `Area` 1,035 / `State` 1 | `name` (unique each) | Marketplace geo hierarchy (from scrape) |
| `District` 38 / `Taluk` 316 / `RevenueVillage` 17,164 | LGD codes (unique) | Admin gazetteer (from TN LGD data) |
| `PropertyType` 23 / `AssetCategory` 3 / `AuctionType` 4 | `name` (unique) | Category dimensions |
| `User` 7, `Conversation` 130, `Dossier` 2, `Feedback` 15, `AnonQuota` 20, `VerificationToken` 1, `SchemaCache` 1 | various | Application/ops nodes (out of scope here) |

### 1.2 Relationship types

| Type | Count | Pattern |
|---|---|---|
| `CONDUCTED_BY` | 2,464 | `(AuctionProperty)->(Bank)` |
| `LISTED_BY_BRANCH` | 2,464 | `(AuctionProperty)->(Branch)` |
| `HAS_BRANCH` | 676 | `(Bank)->(Branch)` |
| `LOCATED_IN_CITY` / `LOCATED_IN_STATE` / `LOCATED_IN_AREA` | 2,464 each | `(AuctionProperty)->(City/State/Area)` |
| `PART_OF_CITY` | 1,053 | `(Area)->(City)` |
| `IN_STATE` | 87 | `(City)->(State)`, `(District)->(State)` |
| `IN_TALUK` | 17,119 | `(RevenueVillage)->(Taluk)` |
| `IN_DISTRICT` | 316 | `(Taluk)->(District)` |
| `HAS_PROPERTY_TYPE` / `HAS_ASSET_CATEGORY` | 2,464 each | `(AuctionProperty)->(PropertyType/AssetCategory)` |
| `IS_AUCTION_TYPE` | 2,061 | `(AuctionProperty)->(AuctionType)` — **403 listings missing** |
| `HAS_BORROWER` | 2,061 | `(AuctionProperty)->(Borrower)` — **403 listings missing** |
| `HAS_DOCUMENT` | 2,475 | `(AuctionProperty)->(Document)` (N:M — shared mega-notice docs) |
| `SAME_PROPERTY_AS` | 80 | `(AuctionProperty)->(AuctionProperty)` `{confidence, match_reason, linked_at}` — re-auction links |
| `SAVED` 5, `OWNS` 132, `FOR_PROPERTY` 2 | — | User features |

Only `SAME_PROPERTY_AS` and `SAVED` carry relationship properties. Everything
else is bare.

### 1.3 Findings — inconsistencies, duplicates, missing constraints

1. **Two disconnected location hierarchies.** The marketplace hierarchy
   (`Area→City→State`, populated from the scrape) and the LGD gazetteer
   (`RevenueVillage→Taluk→District→State`) never touch: `AuctionProperty`
   stores `village`/`taluk`/`district` as free-text properties while a
   17k-node canonical gazetteer sits unused one hop away. Every
   transliteration variant (Tiruvallur/Thiruvallur) is a distinct string.
2. **The registration hierarchy is strings only.**
   `registration_district`/`registration_sub_district` are properties on
   `AuctionProperty` despite being high-value, enumerable entities (SRO
   offices) that the extraction guide works hard to capture.
3. **`Borrower` has no uniqueness constraint and no normalization.** 4,730
   nodes keyed by raw display string; "Mr. K. Yoganand" and "K Yoganand"
   are different nodes, so "borrowers with >3 properties" queries silently
   undercount. `Branch` likewise has no constraint — cross-bank name
   collisions ("Main Branch") merge or duplicate arbitrarily.
4. **The extraction result is a JSON blob, not graph data.**
   `Document.extraction_json` holds the 15-class grounded entities
   (currently 29/1,348 documents populated); `apply_extractions.py` flattens
   a small subset onto `AuctionProperty` (boundaries ×8 strings, door
   numbers as comma-joined strings, village/taluk/district, UDS,
   total_area). Identifiers (survey/patta/flat/CERSAI numbers), schedules,
   parties beyond one borrower name, loan accounts, outstanding dues,
   possession, EMD accounts and per-lot structure are all dropped or
   trapped in the blob — unqueryable.
5. **`AuctionProperty` conflates three concepts:** the scraped listing (ids,
   urls, scrape state), the auction event (prices, dates), and the physical
   property (survey numbers, boundaries, extent). Re-auctions of the same
   asset are patched over with pairwise `SAME_PROPERTY_AS` edges instead of
   a shared property identity, so price history requires transitive-closure
   queries.
6. **`Document` is a 60-property mega-node**: file identity, OCR state
   machine, markdown + raw markdown + blocks, review workflow, embeddings
   and the extraction blob on one node. Workable, but pipeline-run metadata
   (batch, model, prompt version) is mixed into document identity.
7. **Orphaned constraints.** A `SurveyNumber` node-key constraint
   (`survey_no, subdivision, survey_type`) survives from a node type removed
   2026-05; `UserProperty`/`DossierDocument` constraints exist with zero
   nodes (dormant dossier feature — fine, but should be documented).
8. **Partial coverage without explanation:** `HAS_BORROWER` and
   `IS_AUCTION_TYPE` cover 2,061 of 2,464 listings; nothing distinguishes
   "extraction found none" from "not yet processed".
9. **No per-fact provenance.** `enrichment_source` / `grounded_source_file`
   are listing-level; when scrape and notice disagree, or two notices
   disagree, the loser is silently overwritten (first-non-null-wins).

---

## 2. LangExtract entity analysis

Path B emits 15 grounded classes (`pipeline/langextract_examples.py`), each a
verbatim char-span + attributes, per-lot entities tagged `lot_index`. Mapping
each to its graph-modeling role:

| Class | Cardinality per notice | Graph role | Rationale |
|---|---|---|---|
| `secured_creditor` | 1 | **Node refs + rel props** → `Bank` (+ assignor `Bank`, trust/assignment on the rel), `legal_basis`/`court_reference` → `Auction` props | Seller identity is shared across notices; ARC assignment is a relationship between two orgs |
| `contact` | 0..N | **Properties** on `Auction` (phones, email) | No cross-notice join value; PII-ish; never queried as an entity |
| `borrower` | 1..N per lot | **Node** → `Party`, role on the relationship | "Same borrower, other auctions" is a real query; role (borrower/guarantor/partner) varies per auction → relationship property |
| `property` | 1 per lot | **Node** → `Lot` (+ classification rels to `PropertyType`/`AssetCategory`) | The per-lot anchor; possession/occupancy/title-holder are lot facts |
| `full_description` | 1 per lot | **Property** on `Lot` (`description`, verbatim) | Long text, single source of truth; also the provenance span |
| `location` | 1..2 per lot | **Node refs** → `RevenueVillage`/`Taluk`/`District` (gazetteer-resolved) + `SubRegistrarOffice`; verbatim strings kept as props | Shared, enumerable, hierarchical — the definition of node-worthy |
| `identifier` | 0..N per lot | **Node** → `Identifier {kind, value}` | Survey/patta/flat/CERSAI numbers are join keys: re-auction detection, land-record enrichment, dedupe |
| `extent` | 0..N per lot | **Properties** on `Lot` (`extent_sqft`, `total_area`, `built_up_area`, `undivided_share`, `uds_parent_extent`, …) | 1:1 with the lot, numeric/filterable, no traversal value |
| `boundary` | 0..4 per lot | **Properties** on `Lot` (`boundary_{side}`, `boundary_measurement_{side}`) | Fixed arity 4, descriptive text; adjacency strings aren't resolvable entities |
| `schedule` | 0..N per lot | **Node** → `Schedule` (label, type, extent) | Genuine 1:N child of a lot (Item A = land, B = UDS, C = building); flattening loses it today |
| `auction_terms` | 1 per lot | **Properties** on `Auction` (reserve, EMD, increment, dates, stage) | The auction event's own attributes |
| `outstanding` | 0..N per lot | **Node ref** → `LoanAccount` + amount/as_on on the rel | Account numbers recur across re-auctions — a strong same-asset signal |
| `emd_account` | 0..1 per notice | **Properties** on `Document` (or map-typed prop) | Remittance detail; notice-level; never traversed |
| `full_terms` | 0..1 per notice | **Property** on `Document` (`terms_text`) | Verbatim block, notice-level |
| `extras` | 0..~5 | **Node** → `Extra {key, value}` attached to `Lot`/`Document` | Open-ended bag; keep queryable without schema churn |

**Parent–child structure:** `Document 1─N Lot`; `Lot 1─N
Identifier/Schedule/Party-links/Boundary(≤4)`; `Document/Lot N─M
AuctionProperty` (the lot↔listing match, today price-only with reasons
`single/exact/tolerance/remainder`). Notice-level entities
(`secured_creditor`, `emd_account`, `full_terms`, `contact`) hang off the
`Document`; per-lot entities carry `lot_index`.

**Missing from extraction (should be added):** valuation amount/date (notices
increasingly state it), guideline value, property tax assessment arrears
(currently only via `extras`), latitude/longitude (declared in the guide but
prose-only), and a stated-encumbrance structured field (attr exists on
`property` but is rarely demonstrated — add an example).

---

## 3. Recommended knowledge-graph schema

### 3.1 Design principles

- **Separate the three concepts** now fused in `AuctionProperty`:
  - `Property` — the physical asset (survives re-auctions),
  - `Auction` — one sale attempt of that asset (terms, dates, outcome),
  - `Listing` — one scraped marketplace record of that auction.
  In migration terms, today's `AuctionProperty` *is* the Listing+Auction and
  keeps its label; `Property` is additive.
- **Lots become first-class** (`Lot` nodes from grounded extraction), matched
  to listings via a relationship carrying confidence — the blob and the
  price-only matcher stop being load-bearing.
- **One location spine** — the LGD gazetteer — with the marketplace hierarchy
  kept as a parallel, linked view. Verbatim strings never deleted; canonical
  refs added.
- **Provenance is a relationship to the Document** with char-span, extractor
  version and confidence — every extracted fact can answer "where in which
  document, extracted by what, verified by whom".
- **Reference data are nodes, measurements are properties.** Anything
  enumerable and shared (bank, village, SRO, identifier, account) is a node;
  anything 1:1 descriptive (extent, boundary text, dates, prices) is a
  property.

### 3.2 Schema at a glance

```
(User)-[:SAVED]->(Listing:AuctionProperty)
(Listing)-[:OF_AUCTION]->(Auction)-[:OF_PROPERTY]->(Property)
(Document)-[:ANNOUNCES]->(Auction)
(Document)-[:HAS_LOT]->(Lot)-[:MATCHED_TO {confidence,method}]->(Listing)
(Lot)-[:DESCRIBES]->(Property)

(Auction)-[:CONDUCTED_BY]->(Bank)-[:HAS_BRANCH]->(Branch)
(Auction)-[:LISTED_BY_BRANCH]->(Branch)
(Auction)-[:DEBT_ASSIGNED_FROM {trust_name, assignment_date}]->(Bank)
(Auction)-[:RECOVERS {amount_num, as_on}]->(LoanAccount)
(Party)-[:PARTY_TO {role}]->(Auction)

(Property)-[:HAS_IDENTIFIER]->(Identifier)
(Property)-[:IN_VILLAGE]->(RevenueVillage)-[:IN_TALUK]->(Taluk)-[:IN_DISTRICT]->(District)-[:IN_STATE]->(State)
(Property)-[:REGISTERED_UNDER]->(SubRegistrarOffice)-[:IN_REGISTRATION_DISTRICT]->(RegistrationDistrict)
(Property)-[:LOCATED_IN_AREA]->(Area)-[:PART_OF_CITY]->(City)-[:IN_STATE]->(State)
(Property)-[:HAS_TYPE]->(PropertyType) ; (Property)-[:HAS_ASSET_CATEGORY]->(AssetCategory)
(Lot)-[:HAS_SCHEDULE]->(Schedule)

provenance on every extracted node:
(Lot|Identifier|Schedule|Party|…)-[:EXTRACTED_FROM {char_start, char_end,
    verbatim, extractor, batch, model, confidence, verified_by, verified_at}]->(Document)
```

---

## 4. Node definitions

Existing nodes that are unchanged (`User`, `Conversation`, `Dossier`,
`Feedback`, `AnonQuota`, `VerificationToken`, category nodes, `Area`, `City`,
`State`, gazetteer nodes) are listed only where their contract changes.

### `Property` *(new)*
The physical asset. Survives re-auctions; the anchor for land-record
enrichment (EC/patta/guideline value) and price history.
- **Properties:** `property_id` (ULID), `canonical_description` (best
  `full_description`), `boundary_north/south/east/west`,
  `boundary_measurement_north/south/east/west`, `extent_sqft` (float,
  normalized), `total_area`, `built_up_area`, `carpet_area`,
  `undivided_share`, `uds_parent_extent` (verbatim strings with units),
  `construction_type`, `latitude`, `longitude`, `created_at`.
- **Unique constraint:** `property_id`.
- **Indexes:** range on `extent_sqft`; full-text on `canonical_description`
  (moves the semantic surface here over time).

### `Auction` *(new)*
One sale attempt. Today its fields live on `AuctionProperty`; the label is
introduced additively and the fields migrate.
- **Properties:** `auction_ref` (ULID), `legal_basis` (SARFAESI|DRT|IBC),
  `court_reference`, `sarfaesi_stage`, `reserve_price_num`, `emd_num`,
  `bid_increment_num`, `auction_start_dt`, `auction_end_dt`,
  `application_deadline_dt`, `inspection_dt`, `auto_extension_minutes`,
  `possession_type`, `possession_date`, `sale_terms`, `contact_phones`,
  `contact_email`, `authorised_officer`, `platform_url`, `status`
  (announced|held|sold|cancelled|unknown), `outcome_price_num` (future).
- **Unique constraint:** `auction_ref`.
- **Indexes:** `auction_end_dt`, `reserve_price_num` (the two hot filters).

### `Listing` *(= today's `AuctionProperty`; keeps the label, gains `:Listing`)*
One scraped marketplace record; retains `auction_id`, `url`, `title`,
scrape/enrichment state fields, embeddings, and the *denormalized read-model
copies* of price/date/location fields for the existing API (see migration —
these become derived, not source-of-truth).
- **Unique constraint:** `auction_id` (existing).
- **Indexes:** existing (`title`/`description` full-text, vector, datetime
  ranges) unchanged.

### `Lot` *(new)*
One lot of one notice, straight from grounded extraction; replaces
`extraction_json` as the queryable form.
- **Properties:** `lot_id` (`{storage_key}#L{lot_index}`), `lot_index`,
  `description` (the `full_description` span), `property_type_raw`,
  `possession_type`, `possession_date`, `occupancy_status`,
  `title_deed_holder`, `encumbrance`, `branch_of_lot`, extent fields
  (as on `Property`), boundary fields (as on `Property`),
  `reserve_price_num`, `emd_num` (lot-level terms for matching).
- **Unique constraint:** `lot_id`.
- **Index:** `lot_index` composite queries come via the relationship; index
  `reserve_price_num` (matching signal).

### `Party` *(replaces `Borrower`)*
A person or firm named in a notice (borrower, guarantor, partner, title
holder, mortgagor…). Role lives on the relationship, not the node.
- **Properties:** `name` (verbatim best form), `norm_name` (casefolded,
  honorific-stripped — the merge key), `address`, `kind` (person|firm|unknown).
- **Unique constraint:** none on name (homonyms are real); **key:**
  `party_id` (ULID) unique, with `norm_name` indexed for entity-resolution
  candidates. Merging identical `norm_name`+fuzzy-address is a resolution
  job, not a constraint.
- **Indexes:** `norm_name`; full-text on `name`.

### `Identifier` *(new — restores the removed `SurveyNumber` concept, generalized)*
One land/property identifier: survey (old/new), patta, chitta, khata, plot,
flat, block, floor, door (old/new), assessment, CERSAI, sale deed, property
id, approved layout — the enum in `pipeline/lookups/identifier_kinds.json`.
- **Properties:** `kind` (enum), `value` (normalized, subdivision preserved),
  `value_raw` (verbatim), `scope_code` (LGD village code when resolved, else
  `''` — survey numbers are only unique within a revenue village).
- **Unique constraint:** node key `(kind, value, scope_code)`.
- **Indexes:** `value` (lookup by number across kinds), `kind`.
- Shared across lots/properties by design: two notices carrying
  `survey_new 99/13A` in the same village is the strongest same-asset signal
  the pipeline can get.

### `LoanAccount` *(new)*
- **Properties:** `account_no` (unique), `bank_name_raw`.
- **Unique constraint:** `account_no`.
- Outstanding amounts belong on the `RECOVERS` relationship (they change per
  notice date), not the node.

### `Schedule` *(new)*
Sub-parcel of a lot (Item/Schedule A/B/C).
- **Properties:** `label`, `type` (land|building|uds|machinery|string),
  `extent`, `description`.
- **Unique constraint:** none (child node); `schedule_id` (ULID) unique.

### `SubRegistrarOffice` / `RegistrationDistrict` *(new)*
The registration hierarchy, parallel to the revenue hierarchy.
- **Properties:** `name` (canonical), `aliases` (list).
- **Unique constraint:** `name` unique per label (registration districts are
  state-scoped; single-state today, add `state_code` to the key when
  expanding).

### `Extra` *(new)*
Open-ended decision-relevant fact (`extras` class): RERA/GST, leasehold
tenure, NOC restrictions, litigation, tax dues…
- **Properties:** `key` (snake_case), `value` (string).
- **Unique constraint:** none; `extra_id` (ULID) unique. Recurring keys that
  prove valuable get promoted to schema fields — the graph shows you which,
  via `MATCH (e:Extra) RETURN e.key, count(*)`.

### `ExtractionRun` *(new, small)*
One batch execution of the extractor — pulls run metadata off `Document`.
- **Properties:** `batch` (int, unique), `model`, `prompt_version`,
  `provider`, `run_at`, `doc_count`.
- **Unique constraint:** `batch`.

### `Document` *(slimmed contract, same label)*
Keeps: file identity (`storage_key` unique — **drop the parallel `file_path`
constraint**, it already caused issue #45), content fields (`markdown`,
`blocks`, embeddings), OCR/review state, `notice_type`, `doc_type`
(sale_notice|ec|patta|valuation|court_order|other — the multi-doc-type hook),
`terms_text` (from `full_terms`), `emd_account_*` fields.
Loses (eventually): `extraction_json` blob (superseded by `Lot` subgraph),
`property_count` (derivable), per-run metadata (moves to `ExtractionRun`).

### Gazetteer nodes *(existing, now load-bearing)*
`RevenueVillage`/`Taluk`/`District`/`State` unchanged; extraction output
resolves against them (exact → alias → fuzzy-within-parent, per the July
review's P5). Add `aliases: list` to each for accumulated transliteration
variants.

### `Bank` *(existing, extended)*
Add `org_type` (bank|arc|nbfc|tribunal) and `aliases` (the
`pipeline/lookups/bank_names.json` content becomes graph data). `Branch`
gains a **node key `(bank_name, name)`** — branch names are only unique
within a bank — or equivalently a `branch_id = bank_name + '/' + name`
unique property, since Neo4j node keys can't span the `HAS_BRANCH`
relationship.

---

## 5. Relationship definitions

| Type | From → To | Properties | Cardinality | Why a relationship (not a property) |
|---|---|---|---|---|
| `OF_AUCTION` | Listing → Auction | — | N:1 (usually 1:1; N listings when portals duplicate) | Lets multiple scrape sources point at one auction event |
| `OF_PROPERTY` | Auction → Property | — | N:1 | **Replaces `SAME_PROPERTY_AS`**: re-auctions share the `Property` node; price history = `MATCH (p:Property)<-[:OF_PROPERTY]-(a:Auction) RETURN a.auction_start_dt, a.reserve_price_num ORDER BY 1` — no transitive closure |
| `ANNOUNCES` | Document → Auction | — | N:M (mega-notice announces many; corrigendum re-announces one) | Document reuse across auctions is existing reality (`HAS_DOCUMENT` is already 2,475 > 2,464) |
| `HAS_LOT` | Document → Lot | — | 1:N | Lots are the notice's children; single notice = 1-lot multi |
| `MATCHED_TO` | Lot → Listing | `confidence` (float), `method` (exact_price\|tolerance\|identifier\|borrower\|embedding\|remainder\|manual), `matched_at` | N:M in principle, 1:1 target | The match is *uncertain, dated, and explainable* — exactly what relationship properties are for; today's CSV of unmatched lots becomes `Lot` nodes with no `MATCHED_TO` (notice-only inventory) |
| `DESCRIBES` | Lot → Property | — | N:1 | Several notices' lots describe one asset over time; canonical `Property` fields are consolidated from its lots |
| `CONDUCTED_BY` | Auction → Bank | — | N:1 | (existing, re-anchored) |
| `LISTED_BY_BRANCH` | Auction → Branch | — | N:1 | (existing, re-anchored) |
| `HAS_BRANCH` | Bank → Branch | — | 1:N | existing |
| `DEBT_ASSIGNED_FROM` | Auction → Bank | `trust_name`, `assignment_date` | N:1 optional | ARC sales: seller is the ARC (`CONDUCTED_BY`), original lender still queryable ("all auctions of IndusInd-originated debt"); trust/date are facts *about the assignment*, i.e. the edge |
| `PARTY_TO` | Party → Auction | `role` (borrower\|co-borrower\|guarantor\|partner\|proprietor\|mortgagor\|legal-heir\|director), `lot_index` | N:M | Same person can be borrower in one auction, guarantor in another — role is per-edge by definition |
| `RECOVERS` | Auction → LoanAccount | `amount_num`, `as_on`, `demand_notice_date` | N:M | Outstanding changes per notice; account identity is shared (re-auction signal) |
| `HAS_IDENTIFIER` | Property → Identifier | `source_lot_id` | N:M | Identifiers are shared join keys (same survey no. across notices ⇒ same asset candidate); N:M because a parent survey number legitimately spans subdivided properties |
| `HAS_SCHEDULE` | Lot → Schedule | — | 1:N | Genuine child list, lost in today's flattening |
| `HAS_EXTRA` | Lot/Document → Extra | — | 1:N | Open bag stays queryable |
| `IN_VILLAGE` | Property → RevenueVillage | `resolution` (exact\|alias\|fuzzy), `verbatim` | N:1 | **The gazetteer hookup.** Verbatim string preserved on the edge; resolution quality queryable ("all fuzzy matches" = review queue) |
| `IN_TALUK` / `IN_DISTRICT` / `IN_STATE` | gazetteer chain | — | N:1 | existing |
| `REGISTERED_UNDER` | Property → SubRegistrarOffice | `verbatim` | N:1 | Registration ≠ revenue hierarchy; SRO is where EC/deed lookups happen (Landeed enrichment) |
| `IN_REGISTRATION_DISTRICT` | SubRegistrarOffice → RegistrationDistrict | — | N:1 | — |
| `LOCATED_IN_AREA` / `PART_OF_CITY` / `IN_STATE` | Property → Area → City → State | — | N:1 | Marketplace view kept; re-anchored from Listing to Property |
| `HAS_TYPE` / `HAS_ASSET_CATEGORY` | Property → PropertyType/AssetCategory | — | N:1 | existing, re-anchored |
| `IS_AUCTION_TYPE` | Auction → AuctionType | — | N:1 | existing, re-anchored (subsumable by `Auction.legal_basis`; keep node for filter UI) |
| `EXTRACTED_FROM` | any extracted node → Document | `char_start`, `char_end`, `verbatim`, `extractor` (langextract\|blob\|scrape\|human), `batch`, `model`, `confidence`, `verified_by`, `verified_at`, `corrected` (bool) | N:1 | **The provenance spine.** One shape for every fact; review UI reads/writes it; confidence and human verification live here, not on domain nodes |
| `PRODUCED_IN` | Lot → ExtractionRun | — | N:1 | Which prompt/model produced this — cache invalidation & eval slicing |
| `SAVED` / `OWNS` / `FOR_PROPERTY` | user features | existing | — | unchanged (re-anchor `FOR_PROPERTY` to `Property` when dossiers ship) |

Deliberately **not** relationships: boundaries (fixed arity-4 descriptive
text — properties on `Lot`/`Property`), extents (1:1 measurements), EMD
account (notice-level payment detail), contacts (auction-level strings),
`full_terms` (verbatim block on `Document`).

---

## 6. Constraints & indexes

```cypher
// ── new uniqueness ──────────────────────────────────────────────
CREATE CONSTRAINT property_id      IF NOT EXISTS FOR (p:Property)      REQUIRE p.property_id IS UNIQUE;
CREATE CONSTRAINT auction_ref      IF NOT EXISTS FOR (a:Auction)       REQUIRE a.auction_ref IS UNIQUE;
CREATE CONSTRAINT lot_id           IF NOT EXISTS FOR (l:Lot)           REQUIRE l.lot_id IS UNIQUE;
CREATE CONSTRAINT party_id         IF NOT EXISTS FOR (p:Party)         REQUIRE p.party_id IS UNIQUE;
CREATE CONSTRAINT identifier_key   IF NOT EXISTS FOR (i:Identifier)    REQUIRE (i.kind, i.value, i.scope_code) IS NODE KEY;
CREATE CONSTRAINT loan_account_no  IF NOT EXISTS FOR (l:LoanAccount)   REQUIRE l.account_no IS UNIQUE;
CREATE CONSTRAINT schedule_id      IF NOT EXISTS FOR (s:Schedule)      REQUIRE s.schedule_id IS UNIQUE;
CREATE CONSTRAINT extra_id         IF NOT EXISTS FOR (e:Extra)         REQUIRE e.extra_id IS UNIQUE;
CREATE CONSTRAINT extraction_batch IF NOT EXISTS FOR (r:ExtractionRun) REQUIRE r.batch IS UNIQUE;
CREATE CONSTRAINT sro_name         IF NOT EXISTS FOR (s:SubRegistrarOffice)   REQUIRE s.name IS UNIQUE;
CREATE CONSTRAINT regdist_name     IF NOT EXISTS FOR (r:RegistrationDistrict) REQUIRE r.name IS UNIQUE;
CREATE CONSTRAINT branch_key       IF NOT EXISTS FOR (b:Branch)        REQUIRE b.branch_id IS UNIQUE;   // bank_name + '/' + name

// ── new indexes ─────────────────────────────────────────────────
CREATE INDEX party_norm_name   IF NOT EXISTS FOR (p:Party)      ON (p.norm_name);
CREATE INDEX identifier_value  IF NOT EXISTS FOR (i:Identifier) ON (i.value);
CREATE INDEX auction_end       IF NOT EXISTS FOR (a:Auction)    ON (a.auction_end_dt);
CREATE INDEX auction_reserve   IF NOT EXISTS FOR (a:Auction)    ON (a.reserve_price_num);
CREATE INDEX lot_reserve       IF NOT EXISTS FOR (l:Lot)        ON (l.reserve_price_num);
CREATE INDEX property_extent   IF NOT EXISTS FOR (p:Property)   ON (p.extent_sqft);
CREATE FULLTEXT INDEX party_name_ft    IF NOT EXISTS FOR (p:Party)    ON EACH [p.name];
CREATE FULLTEXT INDEX property_desc_ft IF NOT EXISTS FOR (p:Property) ON EACH [p.canonical_description];

// ── cleanup ─────────────────────────────────────────────────────
DROP CONSTRAINT survey_number_unique IF EXISTS;   // orphan since 2026-05; superseded by identifier_key
DROP CONSTRAINT doc_path             IF EXISTS;   // duplicate identity; storage_key is canonical (issue #45)
```

Existing constraints (auction_id, storage_key, category names, gazetteer
composites, user/dossier ids) stay. Existing vector/full-text indexes stay on
`AuctionProperty`/`Document` until the read-model moves.

---

## 7. Gap analysis

**Current KG vs proposed — missing nodes:** `Property`, `Auction`, `Lot`,
`Identifier`, `LoanAccount`, `Schedule`, `Party` (as normalized replacement),
`SubRegistrarOffice`, `RegistrationDistrict`, `Extra`, `ExtractionRun`.

**Missing relationships:** property↔gazetteer (`IN_VILLAGE` — the single
highest-value gap: 17k canonical villages already loaded, zero edges from
properties), `MATCHED_TO` with confidence (today: implicit in
`apply_extractions` + a CSV), `RECOVERS`/`PARTY_TO {role}`/
`DEBT_ASSIGNED_FROM` (loan & party structure entirely absent),
`EXTRACTED_FROM` provenance (absent), `OF_PROPERTY` (replaces pairwise
`SAME_PROPERTY_AS`).

**Missing properties:** confidence & verbatim per fact; normalized
`extent_sqft` as float (today string mixed with units); `legal_basis`,
`court_reference`, possession, occupancy on the auction/lot;
`Bank.org_type`.

**Current LangExtract vs proposed — extraction is largely sufficient**; the
schema consumes what it already emits. Additions worth making: valuation
amount/date, guideline value, structured encumbrance (add a few-shot
example), lat/long demonstration. The `schedule` class's `description` should
be emitted as its own attr (today only the span text).

**Redundant / incorrect today:**
- `SAME_PROPERTY_AS` (replaced by shared `Property`; keep during migration as
  the seed for `Property` clustering).
- `Borrower` name-keyed nodes (replaced by `Party` + resolution).
- Boundary/door/UDS flattened strings on `AuctionProperty` (become
  `Lot`/`Property` data; listing copies retained only as a read-model until
  the API migrates).
- `Document.extraction_json` as source of truth (becomes a debug artifact).
- Orphaned `SurveyNumber`/`doc_path` constraints (drop).
- `AuctionProperty.district/village/taluk` free strings (retained verbatim,
  but queries move to gazetteer edges).
- `IS_AUCTION_TYPE` semantics duplicate `legal_basis` — keep one source
  (`legal_basis` property) and derive the category edge.

**Simplification opportunities:** one provenance shape instead of the ~15
`*_source`/`*_verified_*`/`*_at` field families scattered across
`AuctionProperty` and `Document`; `SchemaCache` regenerates against the new
schema automatically.

---

## 8. Migration plan

Additive-first: at every phase the existing API/chat tools keep working
against `AuctionProperty` unchanged. Each phase is independently shippable
and reversible.

**Phase 0 — hygiene (no data movement).**
Drop orphaned constraints (`survey_number_unique`, `doc_path`); add the new
constraints/indexes (empty labels cost nothing); add `Bank.org_type` +
`aliases`, `Branch.branch_id` backfilled from `HAS_BRANCH`.

**Phase 1 — gazetteer hookup (biggest win, smallest risk).**
Resolve existing `AuctionProperty.village/taluk/district` strings against
`RevenueVillage/Taluk/District` (exact → alias → fuzzy-within-parent);
create `IN_VILLAGE {resolution, verbatim}` edges (anchored on
`AuctionProperty` for now; re-anchored in Phase 3). Unresolved strings →
review queue. Also materialize `SubRegistrarOffice`/`RegistrationDistrict`
from the registration string fields.

**Phase 2 — graph-ize extractions (dual-write).**
Extend `load_extractions.py` to write the `Lot` subgraph (`Lot`,
`Identifier`, `Schedule`, `Party`, `LoanAccount`, `Extra`,
`EXTRACTED_FROM` spans, `ExtractionRun`) *alongside* `extraction_json`.
Port `apply_extractions.match_lots_to_listings` to emit `MATCHED_TO
{confidence, method}` edges; unmatched lots stay as notice-only inventory
(replaces `grounded_unmatched.csv`). Review UI keeps reading the blob until
parity is verified, then reads the subgraph.

**Phase 3 — Property & Auction identity.**
Create one `Auction` per `AuctionProperty` (1:1 seed) and re-anchor
`CONDUCTED_BY`/`LISTED_BY_BRANCH`/`IS_AUCTION_TYPE` (keep the old edges
until API cutover). Cluster `Property` nodes: seed from `SAME_PROPERTY_AS`
components + identifier/village agreement (`Identifier` sharing within the
same `RevenueVillage`), then `(Auction)-[:OF_PROPERTY]->(Property)` and
`(Lot)-[:DESCRIBES]->(Property)`. Re-anchor location/type edges to
`Property`. `SAME_PROPERTY_AS` retired after verification (price-history
queries switch to the `Property` spine).

**Phase 4 — canonical-path cutover (implements review P1).**
`apply_extractions` writes listing fields *from the Lot subgraph* (same
flattened read-model props, so API/UI unchanged), stamping
`enrichment_source='grounded_extraction'` as today. Then migrate API queries
one endpoint at a time to the graph shape (properties list → `Property`
spine; borrower filter → `Party.norm_name`; survey lookup → `Identifier`).

**Phase 5 — Party resolution & cleanup.**
Migrate `Borrower` → `Party` (`norm_name` computed, `HAS_BORROWER` →
`PARTY_TO {role:'borrower'}`); run conservative merges (identical norm_name +
same district). Delete `Borrower` label, drop `extraction_json` from newly
processed documents (retain for audit on old ones), remove flattened listing
props once no query reads them.

**Phase 6 — new document types.**
`doc_type` on `Document` routes to per-type extractors (EC → encumbrance
facts, valuation → valuation facts) that reuse the same pattern:
type-specific nodes + `EXTRACTED_FROM` provenance + `DESCRIBES`/`ANNOUNCES`
anchoring. No schema redesign needed — that is the test the schema was
designed to pass.

Rollback: phases 1–3 are additive (drop new labels/edges to revert); phase 4
is a write-path switch behind the existing `enrichment_source` guard; phase 5
is the only destructive step and runs last, after parity checks.

---

## 9. Future recommendations

- **Land-record enrichment:** `LandRecord` nodes (EC, patta, guideline
  value) attach to `Identifier`/`Property` exactly as sketched in
  `docs/landeed_tn_records.md` — the `Identifier` node key was designed for
  this join.
- **Outcome tracking:** `Auction.status/outcome_price_num` + a
  `RESULTED_IN` edge to a future `Sale` node turn the graph into a price
  *realization* dataset — the real intelligence moat.
- **Geo coordinates:** add `point` typed `location` on `Property` when
  lat/long extraction lands; Neo4j spatial index enables radius queries.
- **Entity-resolution loop:** treat `Party` merges and `Property` clustering
  as reviewable suggestions (same pattern as `MATCHED_TO` confidence), and
  feed reviewer decisions back as gold data — the same improvement loop the
  extraction evals already use.
- **Keep the read-model honest:** the flattened listing props are a cache of
  the graph, never hand-edited; anything human-verified writes through the
  `EXTRACTED_FROM {verified_by}` provenance so the source of truth is
  always the graph.
