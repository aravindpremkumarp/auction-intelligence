# Source Adapters and the Auction Spine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest BAANKNET and bankeauctions.com beside eauctionsindia through one adapter interface, match the same auction across portals, and give the agent one merged record per auction — the nine-field property core with provenance, photos included.

**Architecture:** A new `sources/` package owns every portal quirk behind `harvest()` / `normalize()` / `fetch_document()`. Normalized rows feed the existing loader as `:AuctionProperty` branches; a pure matcher pairs them across portals; `build_spine.py` recreates `:AuctionEvent` hubs from the branches every run. agent3 moves onto the spine in three stages, ids first. No `:Parcel`.

**Tech Stack:** Python 3.11, `requests` + BeautifulSoup (already deps), Neo4j 5 (existing driver in `pipeline/config.py`), Cloudflare R2 via `pipeline/storage.py`, pytest. Spec: `docs/superpowers/specs/2026-09-12-source-adapters-design.md`. Branch: `claude/confident-bell-v64xx5`.

---

## Background the engineer needs

- Today's flow is `scrapers/phase1_harvest_urls.py → phase2_scrape_details.py → scripts/prepare_tn_data.py → scripts/load_tn_to_neo4j.py → scripts/upload_downloads_to_r2.py → pipeline/run_pipeline.py → pipeline/embed_descriptions` (`scripts/run_weekly_pipeline.py:6-13`). Only the first two steps change portal; everything after `prepare_tn_data` reads the normalized JSONL shape at `prepare_tn_data.py:161-206` — that shape is the contract every adapter must produce a superset of.
- The graph key is `AuctionProperty.auction_id` (unique, `load_tn_to_neo4j.py:55`). agent3's answer gate treats a bare six-digit token in the 600 000–999 999 band as an id (`api/agent3/common.py:495-563`); `api/agent3/artifacts.py:40` and `api/chat/v2/middleware/answer_gate.py:30` carry copies of the regex.
- `:Document` is MERGEd on bare `filename` (`scripts/upload_downloads_to_r2.py:60-75`); the docstring at `:122` claiming `(auction_id, filename)` is stale. Adapters must emit filenames unique across listings.
- `pipeline/classify_document.py` classifies *user-uploaded dossier* files and is called only from `pipeline/dossier_ingest.py`; it does not sort scraped PDFs. Document roles come from the adapter (portal label / bundle file name), never from a model.
- `scripts/link_reauctions.py` rejects same-calendar-day pairs (`:458-461`) — that is what keeps cross-portal copies of one auction out of `SAME_PROPERTY_AS`.
- CI runs an enumerated test list (`.github/workflows/ci.yml:70-80`); `tests/pipeline/` is opt-in per file and `tests/scrapers/` is not run at all. New tests must be added to the list explicitly.
- Portal contracts, traps and verified shapes: `docs/source-recon-2026-09.md`. Re-run `python scripts/probe_source_apis.py` before starting; both must report `ADAPTER VIABLE`.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `sources/__init__.py` | `ADAPTERS` registry, `get_adapter(name)` | Create |
| `sources/base.py` | `DocRef`, `MediaRef`, `Listing`, `SourceAdapter` Protocol, `Listing.to_row()` | Create |
| `sources/normalize.py` | `clean_price`, `parse_date` (three formats), `make_auction_id`, `asset_category_for`, `doc_role_for(label)` | Create |
| `sources/lookups/asset_categories.json` | per-source raw → `:AssetCategory` name | Create |
| `sources/http.py` | one `requests.Session`, UA, retry/backoff, `SOURCE_REQUEST_DELAY_S` | Create |
| `sources/download.py` | `.part` + `Content-Length` download; zip member extraction; Referer support | Create |
| `sources/eauctionsindia.py` | adapter over `data/live_eauction_data.jsonl`; bare ids | Create |
| `sources/baanknet.py` | property-filter + auction-detail adapter; `bn-` ids; documents + media | Create |
| `sources/bankeauctions.py` | DataTables + detail slug + NIT zip adapter; `be-` ids | Create |
| `sources/match.py` | `find_same_listing_pairs(rows, existing)` | Create |
| `sources/merge.py` | `merge_event(cluster) -> dict` with provenance, `has_photos`, `core_complete` | Create |
| `scripts/harvest_sources.py` | CLI: raw + normalized JSONL, downloads, summary | Create |
| `scripts/gap_report.py` | dry match against the live graph; per-portal new / matched / core fields / photos | Create |
| `scripts/link_listings.py` | writes `SAME_LISTING_AS` between `:AuctionProperty` (stage-2 bridge) | Create |
| `scripts/build_spine.py` | recreates `:AuctionEvent` + `LISTS` / `ANNOUNCES` / `DEPICTS` | Create |
| `scripts/prepare_tn_data.py` | shim over the eauctionsindia adapter; still writes `tn_auction_data.jsonl` | Modify |
| `scripts/load_tn_to_neo4j.py` | `--input`, `--dry-run`, source props, media refs, insert filter | Modify |
| `scripts/upload_downloads_to_r2.py` | per-source dirs, `doc_role`, live-listing photo mirror, fix docstring `:122` | Modify |
| `scripts/init_graph_schema.py` | indexes on `AuctionProperty(source)`, `(source, source_id)`, `AuctionEvent(event_id)` unique, `Media(content_sha256)` | Modify |
| `pipeline/match_confidence.py` | grades for `notice_bytes`, `boundaries`, `bucket_only` | Modify |
| `pipeline/run_pipeline.py` | stage 5b `build_spine` | Modify |
| `api/agent3/common.py`, `artifacts.py`, `api/chat/v2/middleware/answer_gate.py` | prefixed ids | Modify |
| `api/agent3/find_properties.py`, `get_property.py`, `reauction_history.py` | canonical filter, `also_on`, `has_photos`, `photos`, `publications`; chain-based attempts | Modify |
| `api/properties/router.py`, `api/tools/cypher_tools.py` | browse/detail parity | Modify |
| `api/places.py` | `portal_district` fallback | Modify |
| `web/app.js` | photos strip + "also listed on" in the detail panel | Modify |
| `docs/SCHEMA.md`, `README.md`, `.github/workflows/data-freshness.yml`, `scripts/run_weekly_pipeline.py` | schema delta, pipeline order | Modify |
| `tests/sources/test_*.py` | pure-function tests | Create |
| `tests/api/test_agent3_ids.py` | prefixed ids through `guarded_ids` and the answer gate | Create |
| `.github/workflows/ci.yml` | add `tests/sources` and the new api test to the `test` job | Modify |

---

## Task 1: Schema doc first

**Files:** Modify `docs/SCHEMA.md`.

- [x] Add a "Sources and the spine" section: `source*` props on `:AuctionProperty`, id prefixes, `:AuctionEvent` (fields, `provenance`, `core_complete`), `:Media`, `Document.doc_role`, `LISTS` / `ANNOUNCES` / `DEPICTS`, `SAME_LISTING_AS` (bridge), `SAME_PROPERTY_AS` now between events.
- [x] Mark `:Parcel` as *retiring* with the three-step order; leave the existing section in place until step 3.
- [x] Record the nine-field core with today's baseline numbers (from the spec) so the KPI has a starting point in the doc.

## Task 2: `sources/base.py`, `normalize.py`, `http.py`, `download.py`

**Files:** Create the four modules; Create `tests/sources/test_normalize.py`, `tests/sources/test_download.py`.

- [x] Move `clean_price` / `parse_date` out of `scripts/prepare_tn_data.py:30-75` into `sources/normalize.py`; import them back into `prepare_tn_data.py` so nothing else changes yet.
- [x] `parse_date` accepts `DD-MM-YYYY HHMM AM/PM` (existing), ISO 8601 with `Z`, `15 Sep 2026 11:00` and `12 Sep 2026`; returns naive ISO `YYYY-MM-DDTHH:MM:SS` in IST for all four so `load_tn_to_neo4j`'s `datetime()` cast is unchanged.
- [x] `make_auction_id(prefix, native)`; `doc_role_for(label)` keyword map (`sale notice|proclamation → sale_notice`, `tender`, `terms`, `affidavit`, `property details → property_details`, `publication|dinakaran|hindu|express|<paper>-<city>-<date> → publication`, else `unknown`).
- [x] `sources/download.py`: `download(url, dest, *, referer=None)` with `.part` + `Content-Length` verification mirroring `phase2_scrape_details.py:87-163`; `extract_zip_members(zip_path, dest_dir, rename)`.
- [x] Tests: every date format; price with `₹`, mojibake `â‚¹`, commas; `doc_role_for` on the seven bundle names from the recon; a `FakeResponse` truncation test like `tests/scrapers/test_download_file.py`.

## Task 3: eauctionsindia adapter + shim

**Files:** Create `sources/eauctionsindia.py`, `tests/sources/test_eauctionsindia.py`; Modify `scripts/prepare_tn_data.py`.

- [x] `harvest()` yields records from `data/live_eauction_data.jsonl`; `normalize()` reproduces `prepare_tn_data.py:120-206` exactly (both key spellings, TN filter, `unwanted_cats`), plus `source`, `source_id`, bare `auction_id`, `documents` with `doc_role=unknown`, empty `media`.
- [x] `prepare_tn_data.py` becomes: adapter → `to_row()` → `data/tn_auction_data.jsonl` + `data/listings/eauctionsindia.jsonl`; download validation unchanged.
- [x] Regression: run old and new on the same input; `diff` must be empty except added keys. Keep the old script under `scripts/legacy/prepare_tn_data_v1.py` for that comparison, delete in a later release.

## Task 4: BAANKNET adapter

**Files:** Create `sources/baanknet.py`, `tests/sources/test_baanknet.py`, `sources/lookups/asset_categories.json`.

- [x] Query the live `:AssetCategory` and `:PropertyType` names first; fill the lookup for BAANKNET's `propertyType` / `propertySubType` and bankeauctions' `row[12]` / `row[13]`.
- [x] `harvest(state="Tamil Nadu")`: `stateId` via `GET /common/states?countryId=101` by name (never hard-code 31 without the lookup), `POST property-filter` pages of 50, then `GET auction/detail/{auctionId}` per row; `limit` honoured; polite delay.
- [x] `normalize()`: the field map in the spec table; `documents` from `auctionDocuments[]` (`.pdf` only, `doc_role_for(description)`, filename `bn-{basename}`); `media` from `propertyMedia[]` (`filetype` 1/2, `ismainimage`) falling back to the row's `photos[]`.
- [x] Tests from the recorded shapes: a row `_source`, an `auction/detail` payload with two documents and three media items; assert ids, dates, roles, main image, and that vehicle/gold rows are dropped.

## Task 5: bankeauctions adapter

**Files:** Create `sources/bankeauctions.py`, `tests/sources/test_bankeauctions.py`.

- [x] `harvest()`: `POST /home/liveAuctionDatatable/?state=24` with paging in the body, dedupe on `row[1]`, stop when a page adds nothing new; detail page via the slug; keep the raw row and the detail HTML.
- [x] `normalize()`: positional row → fields; detail page → reserve, EMD, increment, extension, inspection window, press-release and offer dates, borrower; `documents` = NIT zip (`doc_role=bundle`, `needs_referer=True`) + the three `/public/uploads/bank/` PDFs as `tender`; `row[12] != "Immovable"` → `None`.
- [x] `fetch_document()`: zip with `Referer` = detail URL, extract members, name each `be-{rowId}-{slug(name)}.pdf`, assign `doc_role_for(member name)`.
- [x] Tests: slug builder on the three recon rows; row parsing; detail-page text parsing on a recorded snippet; member routing on the seven Omkara names and the four Hinduja names; the captcha `<img>` is never a photo.

## Task 6: `scripts/harvest_sources.py`

**Files:** Create.

- [x] `--source all|baanknet|bankeauctions|eauctionsindia --state "Tamil Nadu" --limit N --no-download --no-media`.
- [x] Writes `data/raw/<source>/<YYYY-MM-DD>.jsonl` (verbatim, append) and `data/listings/<source>.jsonl` (rewritten per run); downloads documents to `downloads/<source>/`; photos of *live* listings to `downloads/<source>/media/`.
- [x] Prints per source: rows, live rows, documents fetched, photos fetched, failures. Exit non-zero if a source yields zero rows.
- [x] Run `--limit 20` against both live portals; keep the summary in the PR body.

## Task 7: matcher + gap report

**Files:** Create `sources/match.py`, `scripts/gap_report.py`, `tests/sources/test_match.py`, `tests/sources/test_gap_report.py`; Modify `pipeline/match_confidence.py`, `tests/pipeline/test_match_confidence.py`.

- [x] `find_same_listing_pairs(incoming, existing)` per the spec: bucket (bank key via `pipeline/entity_resolution.org_key`, auction calendar day; reserve checked pairwise with `price_agreement.compare_prices` so the 1% tolerance applies), then `notice_bytes` / `boundaries` / `identifier` / `borrower` / `bucket_only`. Same-source pairs never emitted. Two refinements found on the live graph: a listing with no published reserve price (hundreds of recent eauctionsindia rows) still lands in its bucket and can match on evidence, never on the bucket alone; and neighbour numbers inside a boundary clause ("north by Plot No 28") are not the property's identifiers.
- [x] Grades: a separate `SAME_LISTING_CONFIDENCE` table + `listing_confidence_for()` in `pipeline/match_confidence.py`, not rows in `MATCH_CONFIDENCE` — the names overlap (`borrower` is PROBABLE on the bridge, INFERRED on `IS_LOT`) and the existing guard test asserts `MATCH_CONFIDENCE` holds only `IS_LOT` reasons. `tests/pipeline/test_match_confidence.py` stays green and gains two tests tying the table to `sources.match.METHODS`.
- [x] `gap_report.py`: reads `data/listings/*.jsonl`, fetches every listing from Neo4j read-only (`execute_read`; `--existing-json` / `--save-existing` for offline runs), runs the matcher, prints per portal: rows, already loaded, new, matched by confidence (+ ambiguous INFERRED), the nine core fields the portal fills on matched listings, photos gained, and the core-completeness histogram of new listings. `--json` dump. A graph listing the pipeline has not read yet is credited with what its description text states, so "fills" means the portal states something the graph states nowhere.
- [x] Tests: the `bn-351743` / `bn-351740` same-source trap; BAANKNET + eauctionsindia on borrower; notice-bytes CONFIRMED; boundaries CONFIRMED; identifier PROBABLE; unpriced graph listing; disagreeing prices; 1% tolerance; two new portals matching each other while the graph never matches itself.

**Result on the 2026-09-12 `--limit 20` harvest** (16 + 16 rows against 6,327 graph listings; every pair spot-checked in the graph):

| portal | new | matched | CONFIRMED / PROBABLE / INFERRED | fills on matched | photos |
|---|---|---|---|---|---|
| BAANKNET | 9 | 7 | 1 / 6 / 0 | possession +6, reserve price +5, extent +1 | 16 of 16 (9 new, 7 matched) |
| bankeauctions | 3 | 13 | 5 / 8 / 0 | reserve price +7 | 0 |

New BAANKNET listings average 6.9 of 9 core fields before any notice is read. The reserve-price fills are real: the matched eauctionsindia rows carry no price at all ("not published"), which is also why the bucket had to admit unpriced listings.

## Task 8: loader, R2, schema

**Files:** Modify `scripts/load_tn_to_neo4j.py`, `scripts/upload_downloads_to_r2.py`, `scripts/init_graph_schema.py`, `pipeline/storage.py`, `sources/base.py`; Create `tests/scripts/test_load_tn_sources.py`, `tests/scripts/test_upload_sources.py`, `tests/e2e/test_load_sources.py`.

- [x] Loader: `--input` (repeatable glob; default `data/listings/*.jsonl`, fallback `data/tn_auction_data.jsonl`; later files win on a repeated id); `--dry-run` parses, filters and prints the first rows without opening a driver; SETs `source`, `source_id`, `source_url`, `source_rank`, `fetched_at`, `last_seen_at`, `portal_district`, `pincode`, `borrower_address`, `possession_type`, `extent_raw`, `bid_increment_num`, `inspection_*`, `auction_status`, `document_roles` / `document_urls` (parallel to `downloads_list`), `photo_urls`; `MERGE (md:Media {url})` with `kind`, `is_main`, `label`, `source` and `(a)-[:HAS_MEDIA]->(md)`; insert filter is now "≥1 document found or ≥1 media"; `--backfill-source` runs the idempotent `BACKFILL_SOURCE_QUERY` (`WHERE a.source IS NULL`). A legacy row sanitises to `eauctionsindia`, rank 3. `sources.base.ID_PREFIX` is the one map of id prefixes so the loader and R2 upload never import an adapter.
- [x] R2 upload: `locate_local_file(filename, source)` tries `downloads/<source>/` first; `:Document` gets `source` and `doc_role` from the listing's `document_roles` (position-matched to `downloads_list`; `coalesce` on match so a set value is never overwritten); `process_photo` mirrors each un-mirrored `:Media {kind:'image'}` of a live listing to `media/{auction_id}/{sha256}.{ext}` (`storage.media_object_key`) and sets `content_sha256`, `r2_key`, `public_url`, `content_type`, `uploaded_at`; `--no-photos`; the stale "keyed by (auction_id, filename)" docstring now says what the MERGE does.
- [x] Schema: `media_url_unique`, `event_id_unique`, `auction_source_idx`, `auction_source_id_idx`, `media_sha_idx` in `init_graph_schema.py` (and `media_url_unique` in the loader's own constraint list, since its batch MERGEs on it).
- [x] Load check: Bolt is blocked from this sandbox and there is no Docker, so the local-Neo4j load runs in CI instead — `tests/e2e/test_load_sources.py` loads one inline row per source through the loader's own `run_batch` into the `e2e` job's `neo4j:5.26` container and checks three sources side by side, prefixed ids under the unique constraint, the `:Media` nodes, idempotent reload, and that the backfill touches only source-less nodes. Every new Cypher (`BATCH_QUERY`, backfill, media fetch/update, document upsert) was also `EXPLAIN`ed against the live Aura over the HTTPS Query API, and `--dry-run` was run on the 2026-09-12 harvest files (32 rows: baanknet 16, bankeauctions 16, all loadable). Nothing was written to the live graph.

## Task 9: spine

**Files:** Create `sources/merge.py`, `scripts/build_spine.py`, `scripts/link_listings.py`, `tests/sources/test_merge.py`; Modify `pipeline/run_pipeline.py`, `scripts/link_reauctions.py`, `api/places.py`.

- [ ] `merge_event(cluster)`: per-field ranking from Decision 5; `provenance`; price/EMD `agreement`; `has_photos`; `core_complete` 0–9; deterministic `event_id`.
- [ ] `build_spine.py`: fetch branches + `SAME_LISTING_AS` clusters, drop all `:AuctionEvent`, recreate nodes and `LISTS` / `ANNOUNCES` / `DEPICTS`; idempotent; `--dry-run` prints counts and the `core_complete` histogram.
- [ ] `link_listings.py`: mirrors `link_reauctions.py:564-602`, writes `SAME_LISTING_AS {method, confidence, linked_at}` bidirectional.
- [ ] `link_reauctions.py`: candidates become events; same-day rule kept; `attempt_no` and `previous_reserve` stamped along the chain.
- [ ] `run_pipeline.py`: stage 5a `link_listings`, stage 5b `build_spine`, after `link_reauctions`.
- [ ] `api/places.py::district_effective` → `coalesce(a.revenue_district, a.portal_district, city.name)`.
- [ ] Tests: ranking picks notice over portal for extent and portal over notice for auction status; `has_photos` true with one image media; `core_complete` counts nine; same input → same `event_id`.

## Task 10: agent3 stage 1 — ids

**Files:** Modify `api/agent3/common.py`, `api/agent3/artifacts.py`, `api/chat/v2/middleware/answer_gate.py`; Create `tests/api/test_agent3_ids.py`.

- [ ] One `ID_LIKE` in `common.py` matching bare six digits *or* `(?:bn|be)-\d{4,8}`; `guarded_ids` applies the band and currency guards to bare ids only; `artifacts.py` and `answer_gate.py` import it instead of carrying copies.
- [ ] Tests: `bn-358394` and `841207` both extracted; `₹6,50,000` and `bn-1234` inside a price context still rejected; existing gate tests unchanged.

## Task 11: agent3 stage 2 — read through the canonical listing

**Files:** Modify `api/agent3/find_properties.py`, `api/agent3/get_property.py`, `api/agent3/reauction_history.py`, `api/properties/router.py`, `api/tools/cypher_tools.py`, `web/app.js`.

- [ ] `find_properties`: base filter keeps only the lowest-`source_rank` branch per `SAME_LISTING_AS` cluster; `_ROW_PROJECTION` adds `source`, `also_on`, `has_photos`; `total_count` / refine / relax share the filter.
- [ ] `get_property`: `other_listings` (source, url, reserve), `photos` (main first), `publications` (newspaper docs) from the branches.
- [ ] `reauction_history`: read the event chain; keep `SAME_PROPERTY_AS` output shape.
- [ ] `GET /properties` and `GET /auction/{id}`: same filter and fields; `web/app.js` detail panel renders a photo strip and "Also listed on" links.
- [ ] `evals/smoke_agent3.py` passes; a manual `find_properties(bank="State Bank of India")` returns one row per cluster.

## Task 12: agent3 stage 3 — read the spine

**Files:** Modify `api/agent3/find_properties.py`, `api/agent3/get_property.py`, `api/agent3/common.py` (`LOT_OF_LISTING`).

- [ ] Tools `MATCH (e:AuctionEvent)`; rows carry `core_complete`, `provenance`, `listed_on`; `get_property` walks branches from the event.
- [ ] The system prompt tells the model a row is "merged from N sources" and to quote `core_complete` when asked how well a property is known.
- [ ] `evals/` golden conversations updated where row shapes changed.

## Task 13: Parcel retirement (staged, own PRs)

**Files:** Modify `pipeline/promote_extractions.py`, `api/agent3/identifiers.py`, later a delete script.

- [ ] Step 1: `skip_parcels` default `True`; nothing new is written.
- [ ] Step 2: `find_by_identifier` keeps only the Lot path; `attempt_no` reads the chain (done in Task 9).
- [ ] Step 3, one release later: `MATCH (p:Parcel) DETACH DELETE p` behind a script with `--dry-run`, after confirming no query in `api/` references `:Parcel`.

## Task 14: docs, orchestrator, CI

**Files:** Modify `README.md`, `.github/workflows/data-freshness.yml`, `scripts/run_weekly_pipeline.py`, `.github/workflows/ci.yml`.

- [ ] Pipeline order everywhere: `harvest_sources → gap_report → load_tn_to_neo4j → upload_downloads_to_r2 → run_pipeline → embed_descriptions`.
- [ ] `ci.yml` `test` job: add `tests/sources` and `tests/api/test_agent3_ids.py` with the one-line justification the file asks for.
- [ ] README: three sources, the spine, the nine-field core and its baseline.

## Task 15: Full verification

**Files:** none (verification only).

- [ ] `ruff check .` clean; `pytest tests/sources tests/api -q` green; `pytest tests/pipeline/test_match_confidence.py -q` green.
- [ ] `python scripts/probe_source_apis.py` → both `ADAPTER VIABLE`.
- [ ] `harvest_sources --source all --limit 20` → three listing files, documents and photos on disk; `gap_report` prints per-portal new / matched / core-fields / photos.
- [ ] Local Neo4j: load, `link_listings`, `build_spine --dry-run` then real; `MATCH (e:AuctionEvent) RETURN e.core_complete, count(*)` shows a histogram; any SBI / Union Bank row present on two portals has a `SAME_LISTING_AS` edge and one event.
- [ ] agent3: one row per cluster, `has_photos` populated, prefixed ids survive the answer gate; `GET /auction/bn-…` returns `photos` and `other_listings`; the web detail panel shows them.

## Self-Review (completed by plan author)

- Every task names its files and its test; no task depends on a later one except Task 12 on 11 and Task 13 on 9.
- The eauctionsindia path is byte-compatible through Task 3, so the weekly run keeps working while the rest lands.
- Nothing writes to the graph before Task 8, and the gap report (Task 7) runs against the live graph read-only first.
- The one rule the spec calls "never" — a wrong match hiding a listing — is upheld: branches are never merged away, the spine is rebuilt from them, and INFERRED matches are shown, not merged.
