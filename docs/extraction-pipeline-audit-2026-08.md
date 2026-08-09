# Document-extraction pipeline — end-to-end audit (August 2026)

A full trace of how information flows from a sale-notice document through
scrape → OCR → LLM extraction → transformation → Neo4j → API → UI, what is
preserved and lost at every hop, and a prioritized improvement plan. This
builds on `docs/extraction-pipeline-review-2026-07.md` (the July review): §5
scores each of its P1–P9 findings against the code as of today, and the rest
of this document covers what that review did not — storage identity, the
review/annotation surfaces, the serving API, and the public UI.

Every claim below was verified by direct code read; citations are
`file:line`.

---

## 1. The pipeline as built (August 2026)

```
INGEST    phase1_harvest_urls → phase2_scrape_details (Selenium + requests)
          → prepare_tn_data (TN filter, price/date cleaning)
          → load_tn_to_neo4j (:AuctionProperty + dimension nodes)
          → upload_downloads_to_r2 (:Document {filename} + R2 notices/{aid}/{fn})

OCR       Datalab (default; config.py:147) or MinerU vlm → markdown + blocks caches
          → load_markdowns_to_neo4j (d.markdown, d.blocks, d.markdown_raw/blocks_raw)
          → ocr_health (intrinsic score) + score_markdown (coverage score)
          → region_detect + auto_region_reingest (table-collapse remediation)

EXTRACT   PATH A (canonical fields):
            Stage 1    ocr_extract (extract_auction.txt → flat verifiable/enrichment blob)
            Stage 1.3  classify_notice (cluster count + LLM single/multi)
            Stage 1.4  extract_descriptions (single/multi prompts → description/schedules[])
            Stage 1.45 apply_descriptions (lot↔listing by reserve price)
            Stage 1.5  verify_and_enrich (scraped vs PDF, conflicts)
            Stage 4    load_enriched (SET a += verified_fields)
            Stage 4.5  apply_extractions (grounded values overwrite blob values)  ← NEW
          PATH B (grounded):
            load_extractions (LangExtract, 15 classes, char-span grounding
            → Document.extraction_json + extraction_score)
            promote_extractions (:Lot/:Parcel/:Boundary/:Measurement spine)   ← NEW
            resolve_places (PlaceAlias → RevenueVillage/Taluk/District)       ← NEW

REVIEW    /review (descriptions, classification, markdown, block annotator)
          /review/extraction (grounded field review, corrections, verify)

SERVE     FastAPI: GET /properties, /auction/{id}; PydanticAI agent
          (search_auctions, semantic_search, get_auction_detail, run_cypher)

UI        web/app.js + web/property/{id} (public) · web/review.html +
          web/review_extraction.html (admin) · web/dossiers.js (private docs)
```

Since the July review, three of its recommendations have partially landed:
Stage 4.5 makes grounded extraction feed user-facing fields
(`pipeline/run_pipeline.py:112-120`), the `:Lot`/`:Parcel` graph spine plus
geography resolution shipped (#360, `docs/SCHEMA.md`), and Datalab was added
as a second OCR engine with an A/B harness and targeted re-OCR (#340–#344).
The audit below shows each landed *partially* — with specific seams where
data is still lost.

---

## 2. End-to-end traces (verified hop by hop)

### 2.1 Trace A — reserve price

| Hop | Function (file:line) | Field name | Context preserved | Context lost |
|---|---|---|---|---|
| Scrape | `scrape_detail`, scrapers/phase2_scrape_details.py:206-218 | `"Reserve Price"` (raw ₹ string) | `_scraped_at`, `_worker`, listing `URL` | **document source URL** (:239-252), HTTP headers |
| Clean | `clean_price`, scripts/prepare_tn_data.py:30-42,162-163 | `reserve_price_raw` + `reserve_price_num` | raw string kept | `_scraped_at`/`_worker` die (:155-197) |
| Base load | scripts/load_tn_to_neo4j.py:80-81 | `AuctionProperty.reserve_price_num` (+`_raw`) | listing `url` | `downloads_missing` diagnostics |
| OCR | `write_markdowns`, pipeline/load_markdowns_to_neo4j.py:162-181 | free text in `Document.markdown` | page/bbox/confidence in parallel `d.blocks` JSON | page boundaries in markdown (`assemble_markdown`, pipeline/mineru.py:353-379 joins blocks `\n\n`) |
| LLM (legacy) | ocr_extract + prompts/extract_auction.txt:14,61 | `verifiable.reserve_price_num` (LLM does "Rs. 45 lakh"→4500000) | per-(auction,file) cache | offsets, page, confidence — none produced |
| LLM (grounded) | `_entities`, pipeline/load_extractions.py:76-83 | `auction_terms.attrs.reserve_price_num` | **char offsets `start`/`end`**, `extraction_model`, `extraction_batch` | page/bbox (offsets are markdown-only); per-entity confidence (none) |
| Verify | `compare_and_resolve`, pipeline/verify_and_enrich.py:270-296 | `verified_fields.reserve_price_num` | `field_conflicts`, `*_scraped` mirror on conflict, per-field `provenance` in JSONL | losing file's value (log-only, :144-149); lakh/crore semantics (`_to_float` :211) |
| Graph write | `VERIFIED_UPSERT_QUERY`, pipeline/load_enriched.py:60-72 | `a.reserve_price_num` (overwritten) | `verification_status`, `verified_at` | **per-field `provenance` never SET** — which PDF supplied the price is lost |
| Promote | pipeline/promote_extractions.py:277-292,421-424 | `:Auction {lot_key}.reserve_price_num` | `lot_key = filename#lot_index` | char offsets dropped (`build_lots` reads only cls/attrs/text); node unread by serving API |
| API | api/tools/cypher_tools.py:467,965; api/properties/router.py:249 | `reserve_price` / `fields.reserve_price_num` | detail carries `_raw`, `_scraped`, `field_conflicts`, `verification_status` | document projection drops markdown/blocks/extraction (:950-955); NULL-`public_url` docs invisible (:945-946) |
| UI | `renderDetail`, web/app.js:1951,1997-2003 | `formatINR(f.reserve_price_num)` | `price_history` across re-auctions | raw value, conflict flag, verification status, source-notice link — **in the payload, never rendered** |

### 2.2 Trace B — property description

| Hop | Function (file:line) | What happens |
|---|---|---|
| Scrape | phase2_scrape_details.py:222-235 | website description; field-bleed strip **not** applied (legacy scraper had it, utils.py:19-32) |
| Clean | prepare_tn_data.py:160 | truncated at literal `'Province/State :'` only |
| Base load | load_tn_to_neo4j.py:78-79 | written twice: `a.description` + `a.website_description` (permanent pre-notice snapshot) |
| LLM extract | extract_descriptions.py:331-365; prompts :42/:68 | prompt demands verbatim text **"joined with single spaces"** — table/line structure destroyed at generation; cache has no model/timestamp/offsets |
| Apply | apply_descriptions.py:58-104 | lot↔listing matched **by reserve price only**; ties → longest description copied to every equal-priced listing (:69-72,81-82); source file/schedule not recorded |
| Grounded overwrite | apply_extractions.py:138-192,295-311 | `full_description` spans joined; same price key but ties refused as `ambiguous` (contradictory tie policy vs 1.45); **char offsets dropped** (`group_lots` reads text/attrs only :130-134) |
| API | cypher_tools.py:965; properties/router.py:248-256 | detail keeps description + variants; listing/search rows drop it entirely; semantic_search keeps 300-char excerpt |
| UI | app.js:2020-2035,150,313 | header stripped, **Asset/Security IDs regex-deleted** (:2025), whitespace collapsed *before caching both variants* — "View original" (:2031) shows the cleaned string; `<table>` flattened to pipe text, rowspan/colspan lost (:313-323) |

**Cross-cutting:** the only positional grounding that ever exists (char
offsets in `extraction_json`) never survives past the `:Document` node.
Page/bbox/confidence live exclusively in `Document.blocks`, which only the
review surfaces read. And the reserve price doubles as the join key that
assigns descriptions to properties — any reserve disagreement corrupts the
description assignment.

---

## 3. What is genuinely good (keep and build on)

- **Grounded extraction core.** Char-span grounding persisted per entity
  (load_extractions.py:80-81), `lot_index` on per-lot entities, `kind_raw`
  preserved through normalization (:71-75), review-status preservation on
  re-extraction (:144-146).
- **Raw-vs-normalized discipline on the promote path.** `Measurement.raw` +
  `value` + `unit` + `sqft_norm` + `norm_method`; `Boundary.adjacency_raw`;
  `Identifier.value_raw` + `as_written` (promote_extractions.py:225-267).
  This is the model the rest of the pipeline should converge on.
- **Deterministic measures library** — longest-alias-first unit matching,
  setback-vs-road adjacency, headline-extent selection, `is_length_valid`
  flagging (pipeline/measures.py).
- **Human-override protection on descriptions** (`description_source='human'`
  / `description_verified` guards in both appliers) and idempotent, cached,
  resumable stages nearly everywhere.
- **OCR failure-mode engineering** — ocr_health's targeted pathology
  detectors, preclean, region_detect, health-gated re-ingest persistence
  (auto_region_reingest.py:207-222).
- **The review tooling depth** — block annotator with CAS revisions, undo/redo,
  per-block re-extract with honest outcome toasts, span-correct table editor;
  extraction review with three-way click-to-source (mark ↔ card ↔ bbox).
- **Agent grounding architecture** — `ui_rows` metadata channel keeps bulk
  rows out of model context; panel sync intersects cited ids with ids tools
  actually returned (api/chat/panel.py:68-98) as an anti-hallucination guard.
- **Label-free validators + gold evals + batch feedback reports** (validators.py,
  evals/) — a real improvement loop.

---

## 4. Information-loss inventory

Severity: **S1** unrecoverable without re-fetching/re-paying · **S2**
recoverable only by re-run/re-derivation · **S3** exists but untraceable from
where consumers look · **S4** presentation loss.

### S1 — permanently unrecoverable

1. **Document source URLs discarded at download.** `download_urls` is
   consumed and never stored (phase2_scrape_details.py:239-252); `:Document`
   carries no source URL, size, hash, or download timestamp
   (upload_downloads_to_r2.py:60-75). No checksums anywhere in `scrapers/`.
   A corrupted or wrong file can be neither detected nor re-fetched.
2. **Filename collisions silently substitute wrong documents.** Flat local
   dir + existence-only skip (phase2:103-105): a second, different
   `sale_notice.pdf` from another bank is never downloaded. In the graph,
   `MERGE (doc:Document {filename})` is a **global** key
   (upload_downloads_to_r2.py:62) and `coalesce()` freezes the first R2
   location forever (:69-73). Re-issued/corrected notices with the same name
   are also invisible. `scripts/dedupe_documents.py` exists because this key
   design already caused issue #45.
3. **Datalab — the default OCR engine — discards confidence, image crops, and
   its raw payload.** `"confidence": None` hardcoded (datalab.py:241); the
   base64 `images` map is dropped (`_images`, datalab_api.py:191) and
   `parse_datalab_blocks` is called without `img_map` (:192) so `img_url` is
   always None; only canonical blocks are cached (:201-204) — polygons, tree
   ids, unmapped fields require re-paying the API. MinerU (the fallback)
   archives everything; the default engine is strictly lossier.
4. **Empty OCR results poison the cache.** `md_path.write_text(markdown or "")`
   (datalab_api.py:200) + existence-only skip (scripts/ocr_with_mineru.py:131,258)
   → a Datalab whiff is never retried.
5. **Re-runs overwrite prior versions, no history — three writers.**
   `SET d.extraction_json = $j` on `--force` (load_extractions.py:139);
   block edits overwrite blocks+markdown in place (blocks.py:395-401;
   `blocks_revision` is a CAS counter, not a version store); reingest
   persists **without CAS** (blocks.py:892-915), silently destroying edits
   made during the 30 s–5 min background job. Description edits snapshot the
   machine text only on the *first* human edit (queries.py:535-539).
6. **`extract_batch --neo4j` bills the LLM then discards the entities** —
   only `{aid, score, issues, fields, stats}` is written; `res.extractions`
   is never persisted (extract_batch.py:164-170).
7. **Multi-page collapse on crop/rotation reingest.** After a crop the source
   is flattened to one PNG and every block is retagged to `effective_page`
   (blocks.py:1392-1394) — a crop on page 2 of a 3-page PDF discards pages 1
   and 3 from OCR output entirely.
8. **Scrape freshness dies at the TN bridge** — `_scraped_at`/`_worker`
   omitted from the clean record (prepare_tn_data.py:155-197); no per-auction
   freshness timestamp ever reaches Neo4j.

### S2 — recoverable only by re-derivation

9. **Grounding offsets dropped at both graph promotion hops** — persisted in
   `extraction_json` but `apply_extractions.group_lots` (:130-134) and
   `promote_extractions.build_lots` (:215-307) read only `cls/attrs/text`.
   No `:Lot`/`:Boundary`/`:Measurement`/`AuctionProperty` value can be traced
   to its source span.
10. **Lot geography collected then discarded** — 14 location keys gathered
    (promote_extractions.py:216-223), only lat/long/landmark hoisted
    (:330-336). Consequence: `IN_REVENUE_VILLAGE` appears exactly once
    repo-wide — the MATCH at promote_extractions.py:592 — so **Phase C
    identifier-based parcel merging is dead code**; every parcel is a
    singleton; cross-notice re-auction detection via shared survey numbers
    can never fire.
11. **Validator diagnostics discarded after scoring** — only `["score"]`
    stored (load_extractions.py:135,140); ocr_health `details` (which token
    leaked, collapse ratios) never persisted (ocr_health.py:326-339).
12. **Raw values overwritten in place by normalize.py** (normalize.py:264-284),
    while Stage 4.5 re-injects unnormalized place strings *after* Stage 4
    (apply_extractions.py:147) — the two paths fight, and door numbers change
    type (list vs comma-joined string) depending on which stage last wrote.
13. **`SET a +=` silently destroys scraped values** — `*_scraped` mirrors
    exist only for fields that *conflicted* (verify_and_enrich.py:289-292);
    a wrong PDF value filling an empty field leaves no trail
    (load_enriched.py:64).
14. **Multi-file merge conflicts survive only as log lines**; the winner is
    filesystem-glob-order dependent (verify_and_enrich.py:91,144-149).
15. **Equal-priced lots**: apply_descriptions copies the longest description
    to every equal-priced listing (:69-82); apply_extractions refuses ties —
    contradictory policies for the same problem.
16. **Promotion never deletes** — a re-extraction that removes a
    boundary/measurement leaves the stale node in place
    (promote_extractions.py:376-407); `:Fact {key,value}` is globally shared.
17. **Stale embeddings** — `embed_markdowns` reads the disk cache, not
    `d.markdown` (embed_markdowns.py:101-105); block edits and description
    rewrites never invalidate `markdown_embedding`/`description_embedding`.
18. **Correction races and discards** — `save_field_correction` is an
    unguarded read-modify-write (api/review/extraction.py:196-214);
    `verify_extraction` accepts `$notes` and never SETs them (:219-227);
    review notes everywhere overwrite instead of append (queries.py:544-546).

### S3 — exists but untraceable from where consumers look

19. **Per-field file provenance computed then dropped at load** —
    `provenance {field: filename}` built (verify_and_enrich.py:143-189)
    but `VERIFIED_UPSERT_QUERY` never writes it (load_enriched.py:60-72).
20. **Auctions whose downloads failed never enter the graph** — silent gate
    (load_tn_to_neo4j.py:240) on top of `download_file` swallowing every
    failure into `None` (phase2:115-117).
21. **All Document OCR/extraction provenance dies at the serving API** —
    `_DETAIL_CYPHER` projects documents to `{filename, public_url,
    content_type, doc_type}` (cypher_tools.py:950-955); markdown, blocks,
    extraction_json, scores, and model tags are invisible to the agent and
    public API. Docs with NULL `public_url` are filtered out entirely
    (:945-946) — indistinguishable from "no notice exists".
    `semantic_search` returns `p` only — never *which* document/passage
    matched (:884-890).
22. **Block API round-trips strip MinerU provenance** — the `Block` response
    model omits `img_path/img_url/text_level/sub_type/table_caption/
    table_footnote` (api/review/router.py:359-382), and a client full
    `PUT /blocks` (the undo/redo path) permanently erases them from storage.
23. **Provenance fabrication/gaps** — description caches carry no
    model/timestamp/prompt version; no `prompt_version` exists anywhere
    (repo-wide grep: zero hits); classify cache-hits without a stored model
    are stamped with the *current* model (classify_notice.py:279).
24. **Dossier OCR blocks discarded** (dossier_ingest.py:142) — private-doc
    Q&A can only cite whole-document text, no page/offset.

### S4 — presentation loss (end users)

25. **"View original" lies** (app.js:2022-2033) — IDs regex-deleted and
    whitespace collapsed before both variants are cached.
26. **Table fidelity destroyed on public pages** (app.js:313-323).
27. **No provenance for end users at all** — no notice link, no source URL,
    no confidence, no verified indicator on the public property page; the
    portal `url` is fetched and cached but never rendered (app.js:582, 2053,
    1751-1777, 1913-2057). Extraction-review PDF pane renders max 5 pages
    (review_extraction.html:514); overlapping spans silently unpaintable
    (:314); field↔bbox linking requires byte-equality of a client-side
    markdown reassembly (:564).

### Cross-cutting patterns

1. **Provenance is built, then dropped at the last hop** (items 9, 19, 21, 11).
2. **Existence-only idempotency freezes bad states** (items 2, 4, truncated
   downloads) — no size/hash validation at any hop.
3. **MERGE/SET-without-delete + no version chain** is the universal write
   pattern (items 5, 16) — timestamps everywhere, history nowhere.
4. **Identity keys are too weak** — filename as global Document key, reserve
   price as lot key, list index as entity id. Each collision converts a
   dedup into silent corruption.
5. **The default OCR engine is lossier than the fallback** (item 3).

---

## 5. Status of the July 2026 review (P1–P9)

| # | Finding | Status | Evidence |
|---|---|---|---|
| P1 | Grounded path isn't canonical | **Partial** | Stage 4.5 now applies grounded fields/descriptions to `AuctionProperty` (run_pipeline.py:112-120) and the Lot/Parcel spine landed (#360). But Stage 1 blob + Stages 1.3/1.4/1.45 still run and still write first; grounded writes bypass normalization (apply_extractions.py:147) and carry no human guard on fields (write_fields, :275-292). |
| P2 | Multi-lot linkage lossy | **Partial** | apply_extractions refuses ties instead of guessing (:233-246) — but matching is still reserve-price-only; apply_descriptions still guesses (longest-wins :69-82); unmatched lots still die in CSVs rather than becoming inventory. |
| P3 | Four LLM passes per document | **Open** | classify_notice + extract_descriptions + ocr_extract + LangExtract all still run (run_pipeline.py:43-120). |
| P4 | LLM does arithmetic/normalization | **Partial** | measures.py landed for the promote path; but extract_auction.txt still demands integer-rupee conversion (:14), `_to_float` has no lakh/crore handling (verify_and_enrich.py:205-216), `parse_money` trusts bare numbers as rupees (apply_extractions.py:60-66), and `parse_quantity` drops the tail of compound measures ("2 Acres 35 Cents" → 2 acres, measures.py:99-103). |
| P5 | No gazetteer | **Partial** | resolve_places.py + RevenueVillage/Taluk/District hierarchy landed. But lots never link to it (`IN_REVENUE_VILLAGE` has no writer → parcel merge dead), apply_extractions writes verbatim strings onto AuctionProperty, and the canonical geography nodes have no loader in the repo. |
| P6 | Single OCR engine, no vision fallback | **Partial** | Datalab added + A/B harness + targeted re-OCR (#340–#344). But no direct-vision extraction fallback exists (every LLM stage reads flat markdown only), and Datalab-as-default loses confidence/crops/raw payload (§4 item 3) with no preclean on its path (ocr_with_mineru.py:244-295). |
| P7 | Hand-rolled JSON parsing | **Open** | All Path-A stages still fence-strip + brace-slice (classify_notice.py:116-136, extract_descriptions.py:65-81, ocr_extract.py:86-102); no `finish_reason` check anywhere — a truncated-yet-parseable multi `schedules` array passes silently. |
| P8 | Fragmented state, unstable keys | **Open** | Caches still keyed by `safe_cache_name(file_path)` with collision potential (`/`,`:`,`\` → `_`); no content hashes; no manifest; caches never invalidate on prompt change; no prompt versioning. |
| P9 | Merge policies hide conflicts | **Open** | consolidate still first-non-null with log-only conflicts and glob-order-dependent winners; `fields_agree` substring test unchanged; ambiguous dd/mm dates unflagged. |

---

## 6. Confidence, validation & auditability (cross-cut)

**Signals produced:** per-block OCR confidence (MinerU only; Datalab always
null), `ocr_health_score`+flags, `markdown_quality_score` (coverage vs
website), `notice_type_confidence` (LLM self-report), `extraction_score`
(label-free validator), the description-completeness judge
(`description_completeness`/`judge_confidence`/`text_overlap`/`wrong_property`,
scripts/persist_description_scores.py:9-66), `IS_PARCEL.confidence` enum.
**Signals absent:** per-entity extraction confidence (LangExtract emits
none), description-extraction confidence (none anywhere — the cache is a bare
string), per-field confidence on `AuctionProperty` (none), the entire
Datalab OCR path (confidence hardcoded null).

**Not combined, thresholded, or propagated.** Validation "never blocks
anything" (validators.py usage — score only sorts the review queue,
load_extractions.py:133-135). No score gates promotion
(promote_extractions.py:19-24, deliberately — but nothing records that a
promoted value came from a 40-score extraction either). Nothing reaches end
users (§4 item 27).

**Audit trail:** reviewer identity/time recorded everywhere
(`*_verified_by/_at`, block `edited_by/at`, correction `{by, at}`), but
notes overwrite (queries.py:544-546), `verify_extraction` drops notes
entirely (extraction.py:219-227), only the first description edit snapshots
the machine text, and correction keys are positional (entity `id` = list
index, load_extractions.py:77) yet **survive `--force` re-extraction**
(:141-147) — stale corrections then silently apply to the wrong entities at
promotion. `verify_classification` brands every confirm as `overridden=true`
(queries.py:966), conflating verified with edited.

**Feedback loop:** for grounded extraction it is genuinely closed —
`evals/export_review_gold.py` snapshots every verified Document with
reviewer corrections applied (:43-58, :100-109), and
`evals/langextract_eval.load_gold` merges it with the hand-labelled seed,
reviewer gold winning on collision (langextract_eval.py:45-55). Corrections
deliberately do **not** become few-shots (train/test leakage,
evals/langextract_gold.py:16-17) — that design is right. The real gaps: no
equivalent loop exists for description edits, classification overrides, or
block edits (export_review_gold queries only
`extraction_review_status='verified'`), and nothing protects verified rows
against `write_fields` (apply_extractions.py:275-292 has no human guard —
only descriptions are guarded).

---

## 7. Performance & operations (cross-cut)

- **Review queues are unindexed full scans, run twice per page.**
  `list_queue` is a whole-`:AuctionProperty` scan with the WHERE applied
  *after* `collect(DISTINCT …)` aggregation (queries.py:157-183), followed by
  a near-identical count query (:189-209); no index exists on
  `description_verified`/`description_source`/`extraction_review_status`
  (init_graph_schema.py:117-155 covers dates/prices/geography only).
  `list_notice_queue` ships full `d.markdown` per row over Bolt just to
  fuzzy-sort properties in Python and then pops it (queries.py:76,321,359).
  `list_markdown_queue` returns full markdown for up to 200 rows — a multi-MB
  response (queries.py:1322) — plus `website_descriptions` that feed dead
  code (`_attach_markdown_highlights` is only called by tests,
  queries.py:1256). The extraction queue returns `extraction_json` for up to
  2000 rows and JSON-parses every blob server-side just to count fields
  (api/review/extraction.py:173-191, 283-292).
- **Annotator write amplification**: every debounced textarea autosave
  re-reads and re-writes the *entire* blocks JSON and re-assembles the entire
  markdown (blocks.py:322-341, 389-409) — O(document) per keystroke burst on
  a JSON-string property.
- `/properties` costs 8 Neo4j queries per call (count + rows + 6 facets,
  properties/router.py:223-274), uncached; zero-result agent turns can fire
  ~15 sequential relax probes (cypher_tools.py:549-561).
- **Batch loaders skip failed batches silently** — up to `NEO4J_BATCH_SIZE`
  rows lost per error with only a printed line (load_enriched.py:205-207,
  271-273; load_tn_to_neo4j.py:268-270); no dead-letter file.
- **extract_batch has no per-doc error handling and no resume** — one
  exception kills the run and re-billing everything (extract_batch.py:80-86,
  147-170). Fatal aborts in classify/describe fire *before* the Neo4j status
  write and after up to 50 in-flight calls complete and bill
  (classify_notice.py:329-336; extract_descriptions.py:385-388).
- **No quality-drift time-series** — `extraction_score`/`extraction_batch`/
  `extraction_model` exist per Document precisely so score changes can be
  attributed to model changes (load_extractions.py:9-12), but nothing
  aggregates them; drift analysis is a manual `extract_batch --from-graph`
  run into a local timestamped dir. Pipeline observability is
  stderr/print + one tee'd logfile (obs.py; run_weekly_pipeline.py) — no
  metrics backend, no alerting.
- **In-process caches are per-worker** (schema cache, property count,
  Tavily) — multi-worker deployments serve inconsistent values.
- **Cost accounting prices the wrong model** — langextract_run hardcodes
  Gemini Flash prices while routing sends multis to DeepSeek Pro
  (langextract_run.py:33-35 vs config.py:114-119).
- **Concurrency hazards**: reingest persist has no CAS (blocks.py:892-915);
  extraction corrections have no CAS (extraction.py:196-214); table-editor
  saves carry no base revision (review.html:5437-5445).

---

## 8. Improvement plan

Each item: current → problem → why it matters → recommendation → benefit →
complexity → priority.

### 8.1 CRITICAL

**R1. Content-addressed document identity + source provenance at ingest.**
- *Current:* `:Document` merged globally on `filename`
  (upload_downloads_to_r2.py:62); download URL, bytes-hash, size, and
  download time never captured (phase2:239-252); existence-only skips.
- *Problem:* collisions silently link the wrong notice to an auction and
  freeze it forever; re-issued notices invisible; corrupted/CF-challenge
  downloads undetectable; nothing can be re-fetched.
- *Why it matters:* every downstream stage — OCR, extraction, review,
  serving — inherits a wrong or stale document with no way to notice. This
  is the root of the traceability chain; if identity is wrong here, all
  grounding above it is decorative.
- *Recommendation:* at download, record `{source_url, sha256, size_bytes,
  content_type (magic-byte sniffed), downloaded_at}`; key `:Document` by
  `sha256` (keep `filename` as a display property); skip-checks compare hash,
  not existence; store the source URL and hash on the node and as R2 object
  metadata. Backfill by hashing existing local/R2 objects.
- *Benefit:* collision-proof identity, refresh detection for re-issued
  notices, corruption detection, re-fetchability, and a stable join key for
  every cache/manifest (enables R13).
- *Complexity:* Medium (scraper change + backfill script + loader key
  migration; `dedupe_documents.py` already contains most graph surgery
  patterns needed).
- *Priority:* **Critical**

**R2. Stable entity IDs + extraction versioning (stop corrections
misapplying).**
- *Current:* entity `id` = list index (load_extractions.py:77);
  `--force` re-extraction rewrites `extraction_json` while preserving
  `extraction_corrections_json` (:141-147); promotion overlays corrections by
  that index (promote_extractions.py:507-508).
- *Problem:* after any re-extraction, reviewer corrections silently attach to
  the wrong entities — human input corrupting machine output.
- *Why it matters:* this actively destroys the highest-value data in the
  system (human corrections) at exactly the moment quality improves
  (re-extraction with a better model/prompt).
- *Recommendation:* id = `sha1(cls|start|end|text)[:12]` (content-derived,
  stable across identical re-extractions); on re-extraction, re-anchor
  corrections by matching old entity text/span, mark unmatched corrections
  `orphaned` for review instead of silently applying; keep prior
  `extraction_json` versions (append `extraction_history` entries or a
  `:ExtractionRun` node per batch with the full payload).
- *Benefit:* corrections survive re-extraction correctly; extraction becomes
  re-runnable without fear; diffable history per document.
- *Complexity:* Medium.
- *Priority:* **Critical**

**R3. Carry grounding through promotion (span + page + bbox references on
promoted values).**
- *Current:* char offsets die at both promotion hops (§4 item 9); page/bbox
  live only in `Document.blocks`; the extraction UI reconstructs field→block
  linkage client-side and gives up on any byte mismatch
  (review_extraction.html:564).
- *Problem:* no user-facing or graph value can be traced to a location in the
  source document; the review UI's click-to-source breaks silently after any
  block edit.
- *Why it matters:* this is the single gap between "we extracted X" and "we
  can show you X in the notice" — the core of source traceability the product
  needs for trust, review speed, and dispute resolution.
- *Recommendation:* (a) compute the span→block mapping **server-side at
  extraction-load time** (offsets are into the same markdown assembled from
  blocks; walk blocks in reading order accumulating lengths) and persist per
  entity: `{block_id, page, bbox}`; (b) when flattening
  (apply_extractions/promote_extractions), write a compact provenance map
  alongside values — e.g. `AuctionProperty.field_provenance_json =
  {village: {doc: sha, entity_id, page, bbox}}` and
  `Lot.*_src` / `HAS_BOUNDARY {entity_id}` edge props; (c) re-map or
  invalidate spans on markdown re-ingest (stale flag exists,
  extraction.py:111-124 — make it invalidate offsets, not just badge).
- *Benefit:* every field can highlight its source region in any surface;
  extraction review stops depending on byte-equality; the public UI can ship
  "see it in the notice" (R9).
- *Complexity:* Medium-High (mapping is straightforward; schema additions and
  UI consumption are the bulk).
- *Priority:* **Critical**

**R4. Finish P1 — one grounded extraction pass, canonical.**
- *Current:* four LLM passes (classify, describe, blob-extract, LangExtract);
  grounded output wins only by running last (Stage 4.5), and each pass keeps
  its own cache/parser/failure modes.
- *Problem:* production fields still originate from the weakest extractor
  when a document lacks `extraction_json`; four times the parse-failure and
  cache-drift surface; classification disagreement exists by construction;
  real money burned on redundant reads.
- *Why it matters:* July P1's argument stands, and half the migration is
  already done (Stage 4.5, promote, review UI); the current halfway state is
  the worst of both — double writes, contradictory tie policies, unnormalized
  overwrites.
- *Recommendation:* make LangExtract coverage a pipeline gate (extract every
  doc with markdown), derive `notice_type` from `len(lots)`, derive the
  description from `full_description` spans (already done in 4.5), then
  retire Stage 1/1.3/1.4 after a shadow-run diff on the corpus using the
  existing batch-report harness. Route grounded field writes through the same
  normalizers as Stage 4 (fix §4 item 12) and add the human guard to
  `write_fields`.
- *Benefit:* one schema, one review surface, one eval harness; every prompt/
  example improvement reaches production; large cost reduction.
- *Complexity:* High (mostly deletion + a shadow-run comparison, but touches
  the orchestrator and review queues).
- *Priority:* **Critical**

### 8.2 HIGH

**R5. Multi-signal lot↔listing matching with stored confidence.**
- *Current:* reserve price only, two contradictory tie policies
  (apply_descriptions.py:69-82 guesses; apply_extractions.py:233-246 refuses);
  unmatched lots die in CSVs.
- *Problem:* equal-priced lots (uniform plot layouts) either get one lot's
  description copied everywhere or get nothing; notice-only lots are lost
  inventory.
- *Recommendation:* assignment over reserve (±1%) + EMD + identifiers
  (survey/flat/door) + borrower fuzzy + extent + description-embedding
  similarity, greedy or Hungarian; store `{method, confidence}` on the
  listing↔lot link; queue low-confidence links for review; unmatched lots
  become `:Lot`-only inventory rows (they're already in the graph via
  promote — surface them).
- *Benefit:* correct multi-lot assignment (the biggest field-accuracy defect
  class for tract auctions); recovered inventory.
- *Complexity:* Medium. *Priority:* **High**

**R6. Stop the Datalab metadata bleed; preclean parity.**
- *Current:* §4 item 3 + no preclean on the Datalab path
  (ocr_with_mineru.py:244-295); empty results cached (item 4).
- *Recommendation:* persist Datalab confidences if the payload has any
  (inspect; if not, note engine limitation on the block), keep the `images`
  map (upload to R2 like MinerU's, reuse `img_map` sidecar), cache the raw
  JSON tree beside canonical blocks, run `preclean_if_needed` before submit,
  and never write empty markdown — record a `failed` marker that the bulk
  stage retries.
- *Benefit:* the default engine stops being the lossy one; re-parse without
  re-pay; review annotator gets crops/confidence for the majority of the
  corpus.
- *Complexity:* Low-Medium. *Priority:* **High**

**R7. Un-dead the parcel spine: link lots to resolved geography.**
- *Current:* `IN_REVENUE_VILLAGE` has no writer → Phase C merge dead; lot
  location attrs discarded (promote_extractions.py:216-223 vs :330-336);
  grounded place strings bypass the gazetteer (apply_extractions.py:147).
- *Recommendation:* in promote, resolve each lot's `village/taluk/district`
  through the same PlaceAlias/RevenueVillage machinery (resolve_places) and
  write `(:Lot)-[:IN_REVENUE_VILLAGE]->(:RevenueVillage)`; route
  apply_extractions place fields through normalize + alias resolution before
  SET; add the canonical-geography loader (or commit the dataset provenance —
  it currently has no loader in-repo).
- *Benefit:* cross-notice parcel merging and price-history-per-parcel start
  working — the entire point of the Phase C design; place fields stop
  regressing to raw spellings.
- *Complexity:* Medium. *Priority:* **High**

**R8. Field-level audit integrity in review.**
- *Current:* `write_fields` has no human guard (apply_extractions.py:275-292);
  correction saves race (extraction.py:196-214); `verify_extraction` drops
  notes (:219-227); notes overwrite everywhere; reingest persist has no CAS
  (blocks.py:892-915); only first description edit snapshots.
- *Recommendation:* guard grounded field writes with a per-field
  human-override check (mirror the description guard); add a corrections
  revision (CAS like `blocks_revision`); persist notes append-only
  (`[{by, at, note}]`); CAS the reingest persist and surface reingest
  failures as a status property; snapshot every description edit
  (append-only history list or `:ReviewEvent` nodes).
- *Benefit:* human work can never be silently destroyed — the precondition
  for scaling the review team.
- *Complexity:* Low-Medium (all localized). *Priority:* **High**

**R9. Surface provenance in the serving API and public UI.**
- *Current:* `_DETAIL_CYPHER` document projection drops everything
  (cypher_tools.py:950-955); NULL-`public_url` docs invisible (:945-946);
  semantic_search can't say which document matched (:884-890); public UI
  renders no notice link, no verified badge, no portal URL (app.js:1751-2057);
  "View original" shows a cleaned string (app.js:2022-2033).
- *Recommendation:* extend the detail documents projection with
  `{markdown_available, ocr_health_score, extraction_review_status,
  extraction_score, page_count}` and always include docs (flag
  `public_url: null` rather than filtering); make semantic_search return
  `{matched_doc: filename/public_url, lens}` per hit; render on the property
  page: source-notice link (public bucket URL already exists), a
  "verified ✓ / auto-extracted" badge from `description_verified` +
  `verification_status`, and the portal listing link; keep the true original
  description behind the toggle and move ID-stripping to display-format only.
- *Benefit:* end users can finally trace a claim to the notice; the agent can
  cite documents instead of 300-char excerpts; deep-research stops reporting
  false "no documents" gaps.
- *Complexity:* Low-Medium. *Priority:* **High**

**R10. Deterministic Indian-format money/date parsing at every boundary.**
- *Current:* LLM converts lakh/crore (extract_auction.txt:14); `_to_float`
  strips units ("1.5 Cr" → 1.5, verify_and_enrich.py:205-216) causing false
  conflicts or false verification; `parse_quantity` drops compound tails
  ("2 Acres 35 Cents", measures.py:99-103); dd/mm ambiguity never flagged.
- *Recommendation:* one shared `parse_money` (lakh/crore/units), `parse_date`
  (dd/mm bias + `ambiguous` flag for day ≤ 12), and an additive-compound mode
  for `parse_quantity` (sum "X acres Y cents" unless "out of" phrasing);
  prompts extract verbatim strings + unit only. Retroactive over cached
  extractions — zero LLM cost.
- *Benefit:* removes the top structured-field error class; verification stops
  producing false conflicts on the most financially sensitive fields.
- *Complexity:* Low. *Priority:* **High**

**R11. OCR robustness follow-through.**
- *Current:* health `details` never persisted (ocr_health.py:326-339);
  region re-ingest is page-1/horizontal/upright-only
  (auto_region_reingest.py:88-99); no direct-vision extraction fallback;
  markdown is page-blind everywhere (mineru.py:353-379).
- *Recommendation:* persist health `details`; add page markers to assembled
  markdown (e.g. `<!-- page:2 -->` — also fixes offset→page mapping in R3);
  extend crop re-ingest to per-page operation on multi-page PDFs; for
  health-flagged or low-validator-score docs, re-extract in vision mode
  (page images + markdown to the extraction model) — the July P6 item that
  still has no channel.
- *Benefit:* recovery path for the ~worst decile instead of detection-only.
- *Complexity:* Medium. *Priority:* **High**

### 8.3 MEDIUM

**R12. Structured outputs + finish_reason checks (P7).** Schema-constrained
decoding (OpenRouter `response_format`) with Pydantic validation at the
boundary for classify/describe/blob stages; check `finish_reason` before
accepting any parse; keep LangExtract's guide-based prompting but re-test
`LANGEXTRACT_USE_SCHEMA` per provider. *Benefit:* kills the silent-truncation
class (a truncated multi `schedules` array currently passes as a valid
shorter list). *Complexity:* Low-Medium.

**R13. One content-addressed manifest for pipeline state (P8).** Key every
artifact by `(sha256, stage, prompt_version, model)`; store prompt_version
(a hash of the prompt file) with every LLM output — today caches never
invalidate on prompt change and provenance is fabricated on cache hits
(classify_notice.py:279). SQLite or Neo4j — one substrate, not three.
*Complexity:* Medium.

**R14. Embedding invalidation.** Clear `markdown_embedding` on block edits/
reingest and `description_embedding` on description writes (or re-embed
inline); make `embed_markdowns` read `d.markdown`, not the disk cache
(embed_markdowns.py:101-105). *Complexity:* Low.

**R15. Persist validator issues; wire scores into gates.** Store
`extraction_issues` (codes) alongside `extraction_score`
(load_extractions.py:135-141); optionally gate auto-promotion of
low-score docs behind review, or trigger the R11 vision re-extract.
*Complexity:* Low.

**R16. Conflict-as-data (P9).** Typed conflict records `{field, values:
[{value, source_doc, kind}], resolved_by}` instead of log lines; rank sources
(notice PDF > annexure image) using doc_type + ocr_health; deterministic
winner ordering (sort files, don't glob). *Complexity:* Medium.

**R17. Review UX gaps.** Extraction queue pagination past 500
(review_extraction.html:241); in-UI re-extraction for stale docs (today the
badge prescribes a CLI command, :304); stale-highlight indicators on
description-stage offsets (only the extraction surface has `stale`); render
>5 PDF pages on demand (:514); paint overlapping spans (nested `<mark>` or
side-list) rather than dropping them (:314); fix the markdown stats/bulk
score mismatch (`markdown_quality_score` vs `ocr_health_score`,
queries.py:1174-1181 vs :1235-1239); route the extraction overlay through the
hash router (review.html:6028-6046). *Complexity:* Low each.

**R18. API/serving hygiene.** Allowlist the public detail projection instead
of `properties(a)` (cypher_tools.py:965 — internal audit fields are public
today and future sensitive props leak by default); parse `extras_json` (the
`extras` parse at :1002-1007 targets a property that doesn't exist); fix the
stale prompt claim in modes/_shared.md:12-14 (denies fields that exist);
cache `/properties` facets; report `dropped_ids` in `get_auctions_by_ids`.
*Complexity:* Low each.

**R19. Ingestion refresh + failure visibility.** Revisit-and-diff scraped
listings (today `downloads_complete=true` nodes are never re-scraped —
price/date corrections on the portal never land, load_tn_to_neo4j.py:225);
load auctions whose downloads failed with a `document_status='missing'`
marker instead of silently dropping them (load_tn_to_neo4j.py:240); record
download failures (URL + error) instead of `None` (phase2:115-117); magic-byte
sniff instead of extension coercion (phase2:98-101). *Complexity:* Medium.

**R20. Operational hardening.** Dead-letter files for failed Neo4j batches
(load_enriched.py:205-207); per-doc try/except + resume in extract_batch
(:80-86, 147-170); indexes on the review-workflow properties the queues
filter by (`description_verified`, `extraction_review_status`,
`ocr_health_score`) and single-pass count+rows queries; fix the cost report
to price per routed model (langextract_run.py:33-35); retry HTTP errors in
MinerU `download_zip` (mineru_api.py:220-222); persist a per-batch
`extraction_score` aggregate (mean/p50 by `extraction_batch`) so quality
drift is a query, not a manual run. *Complexity:* Low each.

### 8.4 LOW

**R21. Dead code / dead data cleanup.** `_attach_markdown_highlights` never
called (queries.py:1256-1275 — `MarkdownRow.highlights` always empty);
`ocr_extract.cross_reference` reads fields that don't exist so
`description_completeness` is always 1.0 (ocr_extract.py:161-176);
`lot_description_embedding` and `lot_location_idx` indexes have no writers
(init_graph_schema.py:162-186); `normalize.py` dead normalizers +
empty `property_types.json` + `"Machinary"` typo polluting
`bank_names.json` aliases; `list_fields_enr = set()` dead branch
(verify_and_enrich.py:128); `_anchor_district_code` scratch field persisted
(resolve_places.py:127); `PENALTY["repetition"]=0` misleading
(ocr_health.py:125-126).

**R22. Misc correctness.** Datalab page fallback mislabels pages
(datalab.py:140-144); bbox sentinel `[0,0,0.005,0.005]` indistinguishable
from real tiny block (mineru.py:181-182); `data_id` 128-char truncation
collisions (mineru_api.py:76); `safe_cache_name` collisions (`a/b.pdf` ≡
`a_b.pdf`); ISO-string staleness comparison (extraction.py:120-124);
`unverify_extraction` fabricates 'edited' (:231-240); `verify_classification`
unconditional `overridden=true` (queries.py:966).

---

## 9. Target architecture

The July target (§4 of that doc) remains correct; this refines it with the
identity/provenance layer this audit showed missing, and sequences around
what has already shipped.

```
1  INGEST      capture {source_url, sha256, size, sniffed type, downloaded_at}
               per document; :Document keyed by sha256; hash-verified skips;
               failed downloads recorded as :Document {status:'missing'}
2  TRIAGE      per-doc: resolution, script share, page count, born-digital vs
               scan → engine + mode + preclean routing (both engines)
3  READ        Datalab/MinerU → canonical blocks (+confidence, crops, raw
               payload archived for BOTH engines); markdown assembled WITH
               page markers; ocr_health inline, details persisted
4  EXTRACT     ONE grounded pass (LangExtract schema, always lots[]):
               stable content-derived entity ids; span→block/page/bbox map
               computed server-side and persisted per entity; vision-mode
               re-extract for health/validator-flagged docs; runs recorded as
               :ExtractionRun {batch, model, prompt_version} with history
5  NORMALIZE   deterministic parse_money/parse_date/parse_area (compound-
               aware); gazetteer resolution for all place fields (lots AND
               listings); raw + normalized + method stored side by side
6  VALIDATE    validators (exists) + gazetteer misses + arithmetic checks;
               issues persisted; low scores loop to vision re-extract before
               humans see them
7  LINK        lot ↔ listing on multi signals, {method, confidence} stored on
               the edge; ties and orphans queued; notice-only lots = inventory
8  VERIFY      typed conflict records; source-ranked merge; per-field
               provenance {doc_sha, entity_id, page, bbox} persisted ON the
               written value's node
9  LOAD        Neo4j as today (Lot/Parcel spine now live: lots linked
               IN_REVENUE_VILLAGE so Phase C parcel merge actually fires);
               embeddings invalidated on content change
10 REVIEW      corrections keyed by stable entity id with CAS + append-only
               notes/history; verified state guards ALL writers (fields and
               descriptions); corrections auto-exported as few-shot
               candidates (export_review_gold exists — close the loop)
11 SERVE       detail responses carry a document panel {public_url,
               ocr_health, review_status, page_count} and per-field
               provenance refs; semantic_search cites the matched document
12 PRESENT     public page: notice link + verified badge + "view in notice"
               (bbox highlight via the R3 provenance chain); true original
               text preserved; tables rendered from HTML, not flattened
```

### Data model deltas (concrete)

- `:Document {sha256 (key), filename, source_url, size_bytes, content_type,
  downloaded_at, ...}` — filename demoted to display.
- `:ExtractionRun {run_id, batch, model, prompt_version, at}` with
  `(:Document)-[:EXTRACTED_BY]->(:ExtractionRun)`; `extraction_json` history
  retained per run.
- Entity records: `{id: sha1(cls|span|text), cls, text, start, end, block_id,
  page, bbox, attrs}` — the four new fields are the R3 provenance chain.
- `AuctionProperty.field_provenance_json` (or per-field `*_src` props):
  `{field: {doc: sha256, entity_id, page, bbox, method, confidence}}`.
- Listing↔lot link: `(:AuctionProperty)-[:MATCHES_LOT {method, confidence,
  matched_at}]->(:Lot)` replacing implicit price-join at apply time.
- Review events append-only: `(:Document|:AuctionProperty)-[:HAS_REVIEW_EVENT]->
  (:ReviewEvent {kind, by, at, before, after, note})` — replaces
  overwrite-in-place notes and one-shot snapshots.
- `(:Lot)-[:IN_REVENUE_VILLAGE]->(:RevenueVillage)` — the missing edge that
  activates parcel merging.

### UI data model (what a property page consumes)

```json
{
  "auction_id": "...",
  "fields": { "reserve_price_num": 4500000, "village": "Kelambakkam", ... },
  "field_provenance": {
    "village":  {"doc": "sha256:...", "page": 1, "bbox": [..], "source": "notice",
                  "verified": true, "verified_by": "...", "confidence": 0.92},
    "reserve_price_num": {"doc": "sha256:...", "page": 1, "bbox": [..],
                  "source": "notice+scrape-agree"}
  },
  "description": {"text": "...", "source": "notice", "verified": true,
                   "spans": [{"doc": "sha256:...", "start": 210, "end": 1480,
                              "page": 1}]},
  "documents": [{"sha256": "...", "filename": "...", "public_url": "...",
                  "pages": 2, "ocr_health_score": 92,
                  "extraction_review_status": "verified"}],
  "price_history": [ ... as today ... ]
}
```

This shape powers: a "source" chip per field (click → notice viewer at
page/bbox), a verified badge, a document panel, and — for the review UI —
the same provenance objects deep-link into the annotator, replacing the
byte-equality client-side reconstruction.

### Sequencing (highest leverage first)

1. **R1 + R2** (identity + stable entity ids/versioning) — the substrate
   every other fix keys on; small enough to ship independently.
2. **R8 + R10** (audit integrity + deterministic parsing) — low complexity,
   directly protects human work and money fields.
3. **R3** (grounding through promotion) then **R9** (surface it) — the
   traceability chain end to end.
4. **R4** (single extraction pass) with shadow-run diff — the big
   simplification; R5 (multi-signal linking) rides on it.
5. **R7** (geography/parcels), **R6** (Datalab parity), **R11** (vision
   re-extract) — accuracy and structure follow-through.
6. R12–R22 opportunistically alongside.
