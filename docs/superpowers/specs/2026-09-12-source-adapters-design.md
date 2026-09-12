# Source Adapters and the Auction Spine — Design Spec

**Date:** 2026-09-12
**Files touched:** `sources/` (new package: adapter interface, three adapters,
matcher, merge), `scripts/harvest_sources.py`, `scripts/gap_report.py`,
`scripts/build_spine.py`, `scripts/link_listings.py` (new),
`scripts/load_tn_to_neo4j.py` (source props, `--input`, `--dry-run`),
`scripts/upload_downloads_to_r2.py` (per-source download dirs, media),
`pipeline/run_pipeline.py` (spine stage), `pipeline/match_confidence.py`
(new match methods), `api/agent3/*` (ids, then read the spine — staged),
`api/places.py`, `docs/SCHEMA.md`, tests under `tests/sources/`.
**Status:** Fully agreed 2026-09-12. Implementation plan:
`docs/superpowers/plans/2026-09-12-source-adapters.md`. Recon it rests on:
`docs/source-recon-2026-09.md`.

## Problem

Every listing we hold comes from one portal, and the graph is shaped around
that portal's quirks:

- `scrapers/phase2_scrape_details.py:252-266` emits whatever `<strong>` pairs
  eauctionsindia renders; `scripts/prepare_tn_data.py:120-206` maps that one
  shape; the key names drift (`Reserve Price` vs `ReservePrice`, two spellings
  of the deadline) and each drift has cost us listings.
- `:AuctionProperty.auction_id` is the last path segment of an eauctionsindia
  URL (`prepare_tn_data.py:163`), and agent3 hard-codes that six-digit shape
  (`api/agent3/common.py:506-507`).
- Nothing in the graph or the JSONL says where a listing came from. There is
  no `source` property anywhere.
- The portal sits behind Cloudflare and needs a human at a Chrome window. The
  graph shows it: of 2,964 listings, **91** have an auction still ahead.
- eauctionsindia carries no property photos. Buyers decide with their eyes.

Two portals verified in `docs/source-recon-2026-09.md` (BAANKNET,
bankeauctions.com) need no browser, no login, no CAPTCHA, hand over the
sale-notice documents, and — BAANKNET on every record — photos. They cannot
be added beside the current scripts: three id spaces, three record shapes,
and the same auction under three ids would triple-count every average the
agent quotes.

## Goal & scope

Three things the new portals buy, in order of value:

1. **Coverage** — properties eauctionsindia never carried. 93% of
   bankeauctions' Tamil Nadu lenders (small-finance banks, housing-finance
   companies, NBFCs, ARCs) never appear on the PSU portal.
2. **Depth** — core fields we do not get from eauctionsindia. Two of them
   (possession, built-up area) BAANKNET gives structured, without OCR.
3. **Photos and video** — BAANKNET has 3–5 photos on 100% of records and
   video on a minority; bankeauctions occasionally ships photos inside its
   document bundle.

All three hang off one step: match each incoming record against what we
hold. No match → new property. Match → attach and fill what is empty.

### The property core — nine fields

The fields that decide whether a property is *worth looking at*, agreed as
the product's first priority. Baseline coverage measured on the live graph
on 2026-09-12 (2,964 listings; "upcoming" = the 91 with an auction ahead):

| # | Field | Grain | Baseline all / upcoming | Source today | New portals add |
|---|---|---|---|---|---|
| 1 | property type | property | 100% / 100% | portal + notice | BAANKNET type + sub-type, structured |
| 2 | location | property | 100% district; 95% village-or-taluk | portal city + notice village/taluk/district | BAANKNET district, city, pincode; bankeauctions SRO + village in schedule text |
| 3 | extent (UDS / built-up / total) | property | 94% / 100% | notice only | **BAANKNET carpet / built-up as fields** |
| 4 | measurement (boundary lengths) | property | **40% / 59%** | notice only | bankeauctions schedule text |
| 5 | possession type | property | **61% / 76%** | notice only (1,377 lots say "not stated") | **BAANKNET Symbolic / Physical as a field** |
| 6 | boundaries (all four sides) | property | 80% / 82% | notice only | bankeauctions schedule text |
| 7 | reserve price | auction | 100% / 100% | every portal | every portal |
| 8 | auction date | auction | 100% / 100% | every portal | every portal |
| 9 | has photos | property | **0%** | — | **BAANKNET 100% of records**; bankeauctions sometimes, inside the NIT bundle |

Today 26% of listings have fields 1–8 complete, average 6.7 of 8. A per-spine
**`core_complete` (0–9)** is the KPI for the whole effort.

Secondary, kept when present and never required: occupancy (vacant /
tenanted — the most valuable and the hardest; give it an honest `unknown`),
ownership type (freehold / leasehold / UDS-only), encumbrances and
litigation, access and road width, land classification, building facts
(age, floors, facing), bid mechanics (EMD account, increment, extension),
video.

### Scope

In: the adapter interface; adapters for BAANKNET, bankeauctions.com and a
thin one over the existing eauctionsindia scrape output; the cross-portal
matcher; the `:AuctionEvent` spine with merged facts and provenance; media as
a branch; a gap report; agent3 reading the spine (staged). Tamil Nadu only.

Out: retiring Selenium for eauctionsindia; running adapters from CI (the OCR
stage downstream is workstation-only, `data-freshness.yml` header); a
newspaper *source* of our own (bankeauctions' bundles hand us publications
per auction, which is where the newspaper branch starts); UI beyond photos
and "also listed on" links.

## Decisions

1. **One `:AuctionEvent` per auction is the centre; every source is a branch
   under it.** The spine carries *one merged record* — the nine-field core
   plus whatever secondary fields any branch supplied — with a `provenance`
   map naming the branch behind each field. The agent's default view is the
   merged record; branches stay so the merge can be rebuilt when a matcher
   improves. The spine is **derived, never hand-edited**: the build step
   recreates it from the branches every run.

2. **One `:AuctionProperty` per portal listing, kept as-is.** A wrong match
   must never hide a listing, so branches are never merged away. Existing
   nodes are untouched except for new `source*` properties.

3. **Prefixed ids for new portals** — `bn-<auctionId>` for BAANKNET,
   `be-<rowId>` for bankeauctions. eauctionsindia ids stay bare so URLs,
   watchlists and `:InvestmentTracker` keep working. BAANKNET ids (359826)
   and bankeauctions ids (235813) are six digits too, so bare ids would
   collide with each other and with the 600 000–999 999 band agent3 guards.

4. **No `:Parcel`.** Judged too risky: a wrong survey-number join glues two
   properties together, and `Auction.attempt_no` sat on top of it.
   Re-auction history is a chain of events instead (§ Re-auctions). Survey
   and door numbers stay as *evidence* in the matcher, not as a node.
   Retirement is staged: stop writing, stop reading, delete last.

5. **Per-field source ranking, by field type**, not one global order:

   | Field type | Priority |
   |---|---|
   | property facts (core 1–6, 9; parties, encumbrance) | notice extraction > BAANKNET > bankeauctions > eauctionsindia |
   | auction lifecycle (dates, status, extension, EMD window) | BAANKNET > bankeauctions > notice > eauctionsindia |
   | price & EMD | portal and notice agree → CONFIRMED; disagree → keep notice, flag for review |
   | media | BAANKNET > notice-embedded > any other |

   The notice is the authority for what the property *is*; a portal is the
   authority for what the auction *is doing now*; a printed notice cannot
   change after publication.

6. **Gap report before anything writes to the graph.** Harvest + match
   against the live graph, then print per portal: new properties, matched
   properties, and how many of the nine core fields each fills. Real numbers
   calibrate the matcher and prove the value before agent3 changes.

7. **Adapters own their source's quirks; nothing else knows them.** No
   `<strong>`-pair heuristics, DataTables offsets, `stateId` traps or
   Referer tricks leak past `sources/`.

8. **Media: store links and hashes for everything; mirror what the product
   shows.** Photos of live listings are copied to R2 (~150 KB each); videos
   stay CDN links until they are used. If the CDN ever closes, mirrored
   photos survive.

## Data model

```
(:AuctionEvent {event_id, bank, reserve_price_num, auction_start_dt,
                district, property_type, possession_type, extent_sqft,
                extent_kind, boundaries_json, measurement_json, has_photos,
                core_complete: 0..9, provenance: {field: branch},
                confidence: CONFIRMED|PROBABLE|INFERRED, attempt_no,
                built_at})
   ◄─[:LISTS]────── (:AuctionProperty {auction_id, source, source_id, source_url, source_rank})
   ◄─[:LISTS]────── (:AuctionProperty …)              one per portal that carried it
   ◄─[:ANNOUNCES]── (:Document {filename, source, doc_role, content_sha256})─[:HAS_LOT]─►(:Lot …)
   ◄─[:ANNOUNCES]── (:Document {doc_role:"publication", newspaper, published_on})
   ◄─[:DEPICTS]──── (:Media {url, kind: image|video, is_main, source, content_sha256, r2_key})

(:AuctionEvent)-[:SAME_PROPERTY_AS {method, confidence}]-(:AuctionEvent)   re-auction chain
```

`event_id` is deterministic — `sha1(bank_key | reserve_rupees | auction_day)`
shortened — so a rebuild produces the same id for the same auction and
watchlists pointing at events survive a rerun.

**`:AuctionProperty`** gains `source` ∈ {eauctionsindia, baanknet,
bankeauctions}, `source_id`, `source_url`, `source_rank` (baanknet 1,
bankeauctions 2, eauctionsindia 3), `fetched_at`, `last_seen_at`,
`portal_district`, `pincode`, `borrower_address`, `possession_type`,
`extent_raw`, `bid_increment_num`, `inspection_start_dt`, `inspection_end_dt`,
`auction_status`, `photo_urls`. Existing nodes get `source="eauctionsindia"`
and `source_rank=3` backfilled.

**`:Document`** gains `source` and `doc_role` ∈ {sale_notice, tender, terms,
affidavit, publication, property_details, proclamation, unknown} — set by
the adapter from the portal's own label or the bundle file name, never by a
model. It stays MERGEd on bare `filename`
(`scripts/upload_downloads_to_r2.py:62`); BAANKNET names every notice
`SALE NOTICE.pdf`, so adapters emit `f"{id_prefix}{basename(url)}"`
(`bn-379330.pdf`) and, for bundle members, `f"{id_prefix}{rowId}-{slug(name)}.pdf"`.
R2 key scheme unchanged (`pipeline/storage.py:77-83`).

**`:Media`** is new. Keyed by `content_sha256` once fetched, by `url` before.
`kind` from BAANKNET's `filetype` (1 image, 2 video) or the file extension;
`is_main` from BAANKNET's flag, else the first image. Notice-embedded photos
come from MinerU's image blocks (`Document.blocks[].label == "Image"` with a
bbox ≥ 4% of the page and aspect > 0.85 — page scans are ≈ 0.7) and carry
`source="notice"`. R2 namespace `media/{event_id}/{sha}.{ext}`.

`api/places.py::district_effective` gains one fallback:
`coalesce(a.revenue_district, a.portal_district, city.name)`.

## Adapter contract — `sources/`

A new top-level package that never imports `scrapers/utils.py` (it drags in
`undetected_chromedriver` at import time, `scrapers/utils.py:7-10`).

```python
@dataclass(frozen=True)
class DocRef:
    url: str            # absolute; for a bundle member, the zip url + "#" + member name
    filename: str       # unique across listings (see Data model)
    label: str | None   # the portal's own text or the bundle file name
    doc_role: str       # sale_notice | tender | terms | affidavit | publication | property_details | proclamation | unknown
    needs_referer: bool # bankeauctions zips refuse a bare GET

@dataclass(frozen=True)
class MediaRef:
    url: str; kind: str; is_main: bool; label: str | None

@dataclass
class Listing:          # superset of prepare_tn_data's `clean` dict — same key names
    source: str; source_id: str; auction_id: str; source_url: str; source_rank: int
    title: str; description: str
    reserve_price_raw: str; reserve_price_num: float | None
    emd_raw: str; emd_num: float | None
    auction_start_dt: str | None; auction_end_dt: str | None; application_deadline_dt: str | None
    state: str; district: str; city: str; area: str; pincode: str
    asset_category: str; property_type_raw: str; property_types: list[str]; auction_type: str
    bank_name: str; branch_name: str; borrower_name: str; borrower_address: str
    possession_type: str; extent_raw: str; bid_increment_num: float | None
    inspection_start_dt: str | None; inspection_end_dt: str | None; auction_status: str
    service_provider: str; contact_details: str
    documents: list[DocRef]; media: list[MediaRef]
    fetched_at: str
    def to_row(self) -> dict: ...   # JSON for data/listings/<source>.jsonl; downloads_list = [d.filename …]

class SourceAdapter(Protocol):
    name: str; id_prefix: str; source_rank: int
    def harvest(self, *, state: str, limit: int | None) -> Iterator[dict]: ...  # network; raw records
    def normalize(self, raw: dict) -> Listing | None: ...                         # pure; unit-tested
    def fetch_document(self, ref: DocRef, dest: Path) -> Path: ...                # handles Referer, zip members
```

`harvest()` and `fetch_document()` are the only network calls; `normalize()`
is pure so every mapping is testable from an inline dict, which is the house
style (`tests/pipeline/` tests real corpus values, no fixture files).

| | `sources/baanknet.py` | `sources/bankeauctions.py` | `sources/eauctionsindia.py` |
|---|---|---|---|
| harvest | `POST /api/v1/property/detail/property-filter` with `{"search":{"stateId":31},"sort":{"type":"mostrecent"},"range":"","page":p,"limit":50}`, then `GET /api/v1/auction/detail/{auctionId}` per row | `POST /home/liveAuctionDatatable/?state=24`, body `iDisplayStart=S&iDisplayLength=10&sEcho=1`; page size pinned to 10, consecutive pages overlap by one row → dedupe on `row[1]`; then GET the slug `{row[12]}-{row[13]}-{row[4]}-{row[10]}` | reads `data/live_eauction_data.jsonl`; phase1/phase2 untouched |
| source_id | `auctionId` | `row[1]` | URL tail (bare) |
| documents | `auctionDocuments[]` → `doc_role` from `description` (`sale notice`, `terms`, `paper publication …`) | the **NIT zip** under `/public/uploads/event_auction/` fetched with the detail page as `Referer`; members routed by name (`Sale Proclamation`, `Tender Document`, `Terms and Conditions`, `Affidavit`, `Property Details`, `<lender>-<paper>-<city>-<date>` → publication); the three loose `/public/uploads/bank/` PDFs as `tender` | `downloads_list` as today, `doc_role=unknown` |
| media | `propertyMedia[]` (`filetype` 1/2, `ismainimage`), `photos[]` from the row | none on the page; photos, if any, are inside the bundle's *Property Details* PDF and come out of MinerU image blocks | none |
| dates | ISO 8601 `Z` | `15 Sep 2026 11:00`, `12 Sep 2026` | `DD-MM-YYYY HHMM AM/PM` |
| TN filter | `stateId=31` (the id, not `code` 33 — that is Tripura) | `state=24` | `"tamil nadu" in Province/State` |
| asset filter | `propertyType` ∉ vehicle/gold | `row[12] == "Immovable"` (SBI lists vehicles here too) | existing `unwanted_cats` set |

Shared helpers in `sources/normalize.py`: `clean_price` and `parse_date` move
out of `scripts/prepare_tn_data.py:30-75` (behaviour on the old format kept
byte-for-byte), `parse_date` learns the two new formats,
`make_auction_id(prefix, native)`, `asset_category_for(source, raw)` backed by
`sources/lookups/asset_categories.json` (fill from the live `:AssetCategory`
names before writing it). `sources/http.py`: one `requests.Session`, retry
with backoff, `SOURCE_REQUEST_DELAY_S` default 1.0. `sources/download.py`:
`.part` + `Content-Length` check mirroring `phase2_scrape_details.py:87-163`
without the Selenium cookie jar, into `downloads/<source>/`; zip members
extracted to the same directory under their unique filename.

## Matching — `sources/match.py`

Pure function over normalized rows plus the graph's existing listings. Only
across *different* sources. Bucket key, then evidence:

- **Bucket:** canonical bank (`pipeline/entity_resolution.org_key`) +
  reserve price rounded to the rupee (`pipeline/lot_resolution._round_reserve`,
  1% tolerance via `pipeline/price_agreement.compare_prices`) + auction
  **calendar day**.
- **Within a bucket**, strongest first:

  | method | evidence | confidence |
  |---|---|---|
  | `notice_bytes` | both sides' `:Document.content_sha256` equal (`pipeline/notice_twins.source_key`) | CONFIRMED |
  | `boundaries` | ≥3 of 4 boundary neighbours equal after normalisation | CONFIRMED |
  | `identifier` | same survey / door number on both sides | PROBABLE |
  | `borrower` | `token_set_ratio ≥ 90` (as `lot_resolution.py:47`) | PROBABLE |
  | `bucket_only` | nothing beyond the bucket | INFERRED |

- **Trap the bucket alone would fall into:** BAANKNET `bn-351743` and
  `bn-351740` — same borrower, bank, day and reserve, two different
  properties (a same-day batch sale). Same source ⇒ never matched; across
  sources an INFERRED match is shown as "possibly the same", not merged into
  one spine.

The new methods are added to `pipeline/match_confidence.py` so
`tests/pipeline/test_match_confidence.py` stays green.

## Merge — `sources/merge.py` → `scripts/build_spine.py`

For each match cluster (or singleton) build one `:AuctionEvent`:

1. Collect candidate values per field from every branch: portal listings,
   the notice's `:Lot` (via the existing `IS_LOT` edge, or the single lot of
   a single-lot notice), media.
2. Pick per field by the ranking in Decision 5; record the winner's branch in
   `provenance`.
3. For price and EMD, also record `agreement` — CONFIRMED when portal and
   notice agree within tolerance, else the disagreement for review (this is
   `price_agreement.py` fed cleaner inputs).
4. `has_photos` = any `:Media {kind:"image"}` under the event.
   `core_complete` = count of the nine core fields with a value.

`build_spine.py` drops and recreates all `:AuctionEvent` nodes and `LISTS` /
`ANNOUNCES` / `DEPICTS` edges — a pure function of the branches, which is
what makes a matcher improvement a rerun rather than a migration. Runs as
stage 5b in `pipeline/run_pipeline.py`, after `link_reauctions`.

## Re-auctions without Parcel

`scripts/link_reauctions.py` already rejects same-calendar-day pairs
(`:458-461`), so two portals' copies of one auction never become
`SAME_PROPERTY_AS`. It is re-pointed to link **events**, not listings: two
events on different days for the same property (borrower + bank + area +
description Jaccard, as today, plus boundaries/identifier evidence from the
matcher). `attempt_no` is the position in that chain, `previous_reserve` the
prior event's price. `api/agent3/reauction_history.py` reads the chain
instead of `Auction.attempt_no`.

Parcel retirement, in order: `promote_extractions` stops writing it
(`skip_parcels` becomes the default); `find_by_identifier` keeps only its
Lot path; `attempt_no` moves to the chain; the 3,265 nodes are deleted only
after a release in which nothing read them.

## Pipeline order

```
harvest_sources  (3 adapters → data/raw/<source>/<date>.jsonl, data/listings/<source>.jsonl, downloads/<source>/)
→ gap_report      (dry: new / matched / core fields filled / photos, per portal)
→ load_tn_to_neo4j --input data/listings/*.jsonl   (branches: listings, documents, media refs)
→ upload_downloads_to_r2   (documents + live-listing photos)
→ run_pipeline    (classify → OCR/extract → promote → apply → link_reauctions → build_spine)
→ embed_descriptions
```

`prepare_tn_data.py` becomes a shim over the eauctionsindia adapter and keeps
writing `data/tn_auction_data.jsonl` for one release.

## agent3 — staged

All seven tools `MATCH (a:AuctionProperty)`. Moving them onto the spine is
the largest part of the job, so it is staged:

1. **Ids first.** `ID_LIKE` / `ID_BAND` in `api/agent3/common.py:506-507`, the
   copy in `artifacts.py:40`, and `answer_gate.py:30` accept `(?:bn|be)-\d{4,8}`
   beside bare six digits; the band check applies to bare ids only. One
   pattern exported from `common.py`, the other two import it.
2. **Read through the spine's canonical listing.** `find_properties` adds one
   base-filter clause so only the branch with the lowest `source_rank` per
   event survives; `total_count`, rows, refine and relax share the filter, so
   they agree. Rows gain `source`, `also_on`, `has_photos`. `get_property`
   adds `other_listings`, `photos`, `publications`. `GET /properties` and
   `GET /auction/{id}` do the same; the web detail panel shows photos and
   "also listed on".
3. **Read the spine.** `find_properties` and `get_property` `MATCH
   (e:AuctionEvent)`; rows carry `core_complete` and `provenance`; the model
   is told "merged from N sources" in one field instead of N rows.

The product is usable after stage 2; stage 3 is when the completeness score
becomes something the agent can quote.

## Testing

- `tests/sources/` — pure-function tests from inline dicts: each adapter's
  `normalize` on the recorded shapes in `docs/source-recon-2026-09.md`;
  `parse_date` on all three formats; the bankeauctions slug builder and
  bundle-member routing by file name; document-link selection that excludes
  the root T&C PDFs; media kind/main-image mapping; the matcher on the
  `bn-351743` / `bn-351740` trap and on a constructed cross-portal pair; the
  merge's per-field ranking, `has_photos` and `core_complete`.
- Regression: `prepare_tn_data` before and after on the same
  `live_eauction_data.jsonl` — identical output apart from added keys.
- `tests/api/` — `guarded_ids` accepts `bn-358394`, still rejects `₹6,50,000`.
- Add `tests/sources` to the enumerated `test` job in `.github/workflows/ci.yml`
  (`tests/scrapers/` is in no CI job today; `tests/pipeline/` is opt-in per file).
- Live: `harvest_sources --source baanknet --limit 20` and `--source
  bankeauctions --limit 20` (bundle fetch included); `gap_report` prints the
  numbers per portal; `probe_source_apis.py` still passes.

## Open questions

- Overlap with data we already hold could not be measured from the sandbox
  (`data/tn_auction_data.jsonl` is local-only). The gap report answers it on
  the first local run.
- Whether BAANKNET exposes "all auctions of a property" — it would hand us
  re-auction chains directly for PSU banks. Not found in the recon; one more
  look before writing the chain logic.
- Occupancy (vacant / tenanted) deserves its own extraction field with an
  honest `unknown` default; not part of this spec.
- Rate limits: ~60 requests per host at ~1/s drew nothing. A full 6,864-record
  BAANKNET sweep plus 4 photos each is a different load; the delay is
  configurable, and photos are mirrored for live listings only.
- Terms of use: BAANKNET is an official PSB Alliance portal publishing
  statutorily public notices; bankeauctions.com is a commercial platform
  (C1 India). Read its terms before the adapter becomes load-bearing.
