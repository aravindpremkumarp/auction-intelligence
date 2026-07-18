# Extraction-pipeline review — sales notice → structured entities (July 2026)

A critical, end-to-end review of the pipeline that turns a raw sale notice
(PDF/image) into structured entities (survey numbers, village/taluk/district,
property type, extent, boundaries, measurements, UDS, possession, …), plus a
proposed target architecture. Findings are ranked by impact; each carries the
reasoning behind it.

---

## 1. The pipeline as built

```
scrape (tn_auction_data.jsonl + downloads)
  └─ MinerU OCR (scripts/ocr_with_mineru.py)
       → markdown + blocks caches  [+ preclean upscale, region_detect bands]
       → ocr_health (intrinsic OCR score) + score_markdown (coverage score)

  PATH A — canonical (feeds AuctionProperty fields)
  ├─ Stage 1    ocr_extract.py       per (auction, file) text-LLM →
  │                                  {verifiable, enrichment, extras} JSON
  ├─ Stage 1.3  classify_notice.py   cluster count + LLM → single/multi
  ├─ Stage 1.4  extract_descriptions single/multi prompts → description or schedules[]
  ├─ Stage 1.45 apply_descriptions   lot↔listing match by reserve price
  ├─ Stage 1.5  verify_and_enrich    scraped vs PDF, merge, conflicts
  └─ Stage 4    load_enriched        Neo4j (+ Stage 5 re-auction linking, embeddings)

  PATH B — grounded (feeds the review UI only)
  └─ load_extractions.py  LangExtract → Document.extraction_json
       (15 entity classes, char-span grounding, lot_index, validators,
        gold-set eval, batch feedback reports)
```

## 2. What is genuinely good (keep these)

- **The Path-B design is close to best-in-class.** Span-grounded entities,
  `lot_index` on every per-lot entity, `full_description` as verbatim
  source-of-truth with a coverage validator, adjacency vs measurement split
  on boundaries, UDS parent-extent rules, the possession-disjunction rule
  ("emit nothing when the notice doesn't commit"), enum-drift normalization
  for identifier kinds, and a canonical field catalogue that both the prompt
  and the LangExtract guide derive from. This is the right extraction model.
- **Label-free validators + gold evals + batch feedback reports** — a real
  improvement loop (fix top recurring issue → re-gate) that most extraction
  pipelines never build.
- **OCR failure-mode engineering**: `ocr_health` detects the *actual* MinerU
  vlm pathologies (repetition loops, token leaks, truncation, foreign-script
  hallucination), `preclean` fixes the low-resolution trigger, and
  `region_detect` fixes full-page-grid collapse. Reactive, but well-aimed.
- **Human-override protection everywhere** (`description_source='human'`,
  `notice_type_overridden`, verified rows never clobbered) and idempotent,
  cached, resumable stages.
- **Classifier-vs-cluster disagreement as a review predicate** rather than a
  stored flag, and model routing on the classifier's markdown-based verdict
  (the cluster count is scope-filtered and lies for multi-lot notices —
  `extract_routing.py`'s docstring gets this exactly right).

## 3. Highest-impact problems, ranked

### P1 — Two parallel extraction systems; the better one isn't canonical

Path A (flat JSON blobs from `extract_auction.txt`) is what actually writes
`AuctionProperty` fields. Path B (grounded entities) only populates the review
UI. Path B is strictly superior — grounded, per-lot, validated, eval-gated —
yet its output never becomes the user-facing data. Meanwhile Path A's prompt
(`extract_auction.txt`) has **no lot structure at all**: one flat
`verifiable`/`enrichment` object per file, with the website description as the
only hint about *which* lot to describe.

**Consequence:** the fields users query (village, boundaries, extent, UDS…)
come from the weakest extractor in the repo, and every improvement to Path B
(examples, validators, evals) buys nothing in production.

**Fix (single biggest win):** make grounded extraction the one canonical
pass. Derive everything downstream from `extraction_json`:
`full_description` per lot → the property description (replaces Stages
1.3/1.4/1.45), grounded `location/extent/identifier/boundary/auction_terms`
attrs → the verifiable + enrichment fields (replaces Stage 1's blob), then
verify against scraped as today. One extraction, one schema, one review
surface, one eval harness.

### P2 — Multi-lot handling in the canonical path is structurally lossy

Three independent defects compound:

1. `ocr_extract.process_record` merges per-file results with
   `{**merged, **cached}` — last non-null key wins across files. For a
   multi-lot notice the model returns *one* lot's fields; which lot depends
   on the website-description hint. Cross-lot contamination (lot 2's listing
   carrying lot 1's boundaries/village) is silent and undetectable.
2. `verify_and_enrich.consolidate` has no concept of lots (`additional_lots`
   from the catalogue never appears in Path A's prompt, and `flatten_enrichment`
   emits one set of boundary/village scalars per auction).
3. `apply_descriptions` matches lot↔listing **by reserve price only**, with a
   length tie-break on duplicates. Same-price lots (common in tract auctions:
   ten plots at ₹12.5L each) are assigned arbitrarily; lots whose price the
   scrape missed go to `unmatched.csv` and die.

**Fix:** lots become first-class. Extraction always emits `lots[]` (a single
notice is a 1-lot multi). Lot↔listing linkage becomes a small assignment
problem over multiple signals — reserve price (±1%), EMD, survey/flat/door
identifiers, borrower name (fuzzy), extent, plus embedding similarity of
description text — solved greedily or with Hungarian matching, with a
confidence score stored on the link and low-confidence links queued for
review. Price stays the strongest signal; it just stops being the only one.

### P3 — Four LLM passes read the same markdown

classify (1.3) + describe (1.4) + Stage-1 blob + Path-B extraction = up to
four model calls per document, each with its own prompt, cache, parser and
failure modes. The single/multi classification exists mainly to route to a
cheaper model — but once extraction always emits `lots[]`, the *classifier's
output is a by-product of extraction* (`len(lots)`), and the description is a
by-product too (`full_description` spans). Keep cost routing, but drive it
from something free: markdown length + lot-marker count heuristics, falling
back to the big model when the cheap model returns lot-count inconsistencies.

**Consequence of the status quo:** 4× the surface for parse failures, cache
drift and prompt skew; classification disagreements that could not exist by
construction; and real money (the multi path burns 32k max_tokens on
documents already processed by the other three passes).

### P4 — The LLM does arithmetic and normalization inside the prompt

"572.34 Lakh → 57234000", column-unit multipliers, date → ISO. LLM arithmetic
is a known top error source in exactly this shape of task (unit conversion of
Indian number formats), and once converted, the error is unrecoverable — the
verbatim string is gone. The grounded path already fixed this philosophically
(verbatim spans); finish the job:

**Fix:** extract **verbatim value + unit** only; convert in deterministic
code (a `parse_money("Rs. 572.34 Lakh")`, `parse_area("2 acres 3 cents")`,
`parse_date` with dd/mm bias for Indian notices). Code is testable, fixes are
retroactive over cached extractions (no re-extraction cost), and the raw
string survives for audit. Same for sq.ft normalization (`extent_sqft`) —
cents/acres/ares/sq.m conversion tables belong in Python, not in the model.

### P5 — No gazetteer: place fields are freeform strings

`normalize.py` title-cases and strips suffixes; alias tables are hand-grown.
Tamil Nadu's administrative hierarchy is *enumerable*: the Local Government
Directory (LGD) publishes canonical district → taluk → village lists with
codes, and transliteration variance (Tiruvallur/Thiruvallur/Thiruvallore,
Kancheepuram/Kanchipuram) plus OCR noise is precisely what a
constrained-vocabulary match kills. This is the cheapest large accuracy win
in the repo:

**Fix:** resolve every extracted village/taluk/district against the LGD
gazetteer (exact → alias → fuzzy-within-parent, i.e. only match villages
belonging to the already-resolved taluk/district — hierarchy makes fuzzy
matching safe). Store the LGD code + canonical name alongside the verbatim
string. Unresolvable values are themselves a quality flag (often an OCR error
or a village/taluk slot swap). This also fixes the current risk that
`rapidfuzz` at threshold-85 over a flat alias list merges two genuinely
distinct nearby villages.

### P6 — Single OCR engine, no second opinion, no direct-vision fallback

Everything downstream eats MinerU's markdown. The health scorer *detects*
hallucination but the recovery path (region re-ingest) still re-runs MinerU.
Two structural gaps:

- **No independent channel.** Frontier VLMs (Gemini 2.5 / Claude) read these
  notices directly at high accuracy. For the ~65+ health-flagged docs and for
  any doc where extraction validators score low, send the *original page
  images* to the extraction model alongside (or instead of) the markdown.
  OCR-then-text-LLM is a lossy two-hop chain; the second hop can't recover
  what the first dropped. A hybrid — markdown for grounding offsets, images
  for reading — is the best of both.
- **Bilingual notices.** The foreign-script check rightly whitelists Tamil,
  but there's no language-aware handling: when the schedule text is in Tamil
  (common in district-paper notices), MinerU's Tamil OCR quality and the
  English-centred prompts are both unmeasured. Add a script-share metric per
  document, route Tamil-heavy docs to the vision path, and add 2–3 Tamil
  fixtures to the gold set so the gap is at least measured.

### P7 — Hand-rolled JSON parsing instead of structured outputs

Every Path-A stage does `text.find("{") … rfind("}")` with regex fence
stripping. OpenRouter (and Gemini) support `response_format: json_schema` /
schema-constrained decoding; that removes the whole class of parse-failure
retries and silently-truncated JSON (32k `max_tokens` multi responses that
cut off mid-schedule currently fail shape checks at best, or lose trailing
lots at worst — `normalize_schedules` would happily accept the surviving
prefix, which is how a count-mismatch becomes the only symptom of a truncated
extraction). Pydantic-validate at the boundary; reject-and-retry on schema
violation, not on `json.loads` luck.

### P8 — State is fragmented across three substrates with unstable keys

File caches (keyed by `safe_cache_name(file_path)` over *historically mixed*
path formats — `cached_markdown_for_filename` has a four-candidate fallback
plus a directory scan that silently returns `None` on ambiguity), JSONL
outputs (append-only `extracted.jsonl` whose processed-set is re-derived by
re-reading it), and Neo4j status enums (`description_extraction_status`,
`extraction_batch`, …). Three sources of truth that already disagree (the
loader had to drop the `doc_path` constraint over exactly this, issue #45).

**Fix:** one manifest, content-addressed. Key every artifact by
`sha256(file_bytes)` + a `(prompt_version, model, params)` tuple — this also
fixes the quieter bug that **caches never invalidate when a prompt changes**
(today that's manual directory bumps like `notice_descriptions_v3`). Whether
the manifest lives in SQLite or entirely in Neo4j matters less than there
being exactly one.

### P9 — Merge and comparison policies hide real conflicts

- `consolidate`: first-non-null-wins across files, conflicts only *logged*.
  A notice PDF should outrank a photographed annexure; there is no source
  ranking, and pdf-vs-pdf conflicts never become structured data the way
  scraped-vs-pdf ones do.
- `fields_agree`: substring containment (`ns in np or np in ns`) means
  "State Bank" agrees with "State Bank of India" (good) but also
  "Chennai" agrees with "Chennai … anything", and one-sided-missing returns
  True — fine as a *conflict* test, but it silently treats "PDF couldn't
  read it" as verified.
- `_norm_date` guesses dd/mm for `xx-xx-yyyy` — correct for India, but
  ambiguous dates (≤12 day) are never flagged as such.

These are second-order next to P1/P2, but the fix falls out of the redesign:
per-field provenance + confidence + a typed conflict record, ranked by source
quality (doc type, ocr_health score).

## 4. Target architecture

```
1  INGEST        content-hash manifest; dedup identical notices across
                 auctions BEFORE any model call (extract_batch already
                 dedups — promote that to the front of the pipeline)
2  TRIAGE        per-doc: resolution, script share (Latin/Tamil), page count,
                 born-digital-PDF vs scan → routing decisions
3  READ          MinerU (markdown + blocks) as primary; preclean/region_detect
                 as today; ocr_health inline. Health-flagged or Tamil-heavy
                 docs additionally rendered to page images.
4  EXTRACT       ONE grounded pass (LangExtract schema, always lots[]):
                 - text mode: markdown (cheap, grounded offsets)
                 - vision mode: markdown + page images for flagged docs
                 - cost routing by length/lot-marker heuristic, not a
                   separate classifier call
                 - schema-constrained decoding; verbatim values + units only
5  NORMALIZE     deterministic: money/date/area parsers, identifier-kind
                 canonicalization (exists), survey-number parsing,
                 LGD gazetteer resolution with hierarchy-constrained fuzzy
6  VALIDATE      validators.py (exists) + gazetteer failures + arithmetic
                 checks (EMD/reserve ratio exists; add extent vs boundary-
                 dimension consistency) → per-doc score; low scores loop to
                 vision-mode re-extract before humans see them
7  LINK          lot ↔ scraped-listing assignment on multi signals with
                 stored confidence; unmatched lots become *new* rows
                 (today a lot the scraper missed is simply lost — for an
                 "intelligence" product, notice-only lots are inventory)
8  VERIFY        scraped vs notice per field; typed conflicts; source-ranked
                 merge; per-field provenance + confidence persisted
9  LOAD          Neo4j; description = full_description span (verbatim, no
                 second LLM); embeddings as today
10 IMPROVE       gold-set gate in CI; batch feedback reports (exists);
                 review-UI corrections auto-exported as few-shot candidates
                 (export_review_gold exists — close the loop into examples)
```

Stage answers to the specific design questions:

- **Merge:** classify (1.3) + describe (1.4) + Stage-1 blob + Path-B → one
  extraction stage (4). Apply-descriptions (1.45) merges into LINK (7).
- **Split:** normalization out of the prompt into its own deterministic stage
  (5); verification split into validate-against-self (6) and
  verify-against-scrape (8) — they answer different questions.
- **Reorder:** dedup moves before extraction; validation moves before human
  review (today validators run in batch reports, after load).
- **OCR errors:** prevent (preclean/region — exists), detect (ocr_health —
  exists), *recover via an independent channel* (vision-mode re-extract —
  missing), and absorb residuals with gazetteer + deterministic parsers that
  tolerate character noise.
- **Ambiguity:** the possession rule is the model to generalize — never
  guess, emit nothing + keep verbatim; ambiguous dates/units get an explicit
  `ambiguous` flag instead of a silent bias.
- **Tables:** MinerU's HTML tables pass through to the LLM — fine. Add one
  deterministic assist: when a notice is a per-lot grid (one `<tr>` per lot),
  parse rows in code and feed the extractor per-row context; row structure is
  exactly the lot boundary the model otherwise has to rediscover.
- **Multiple properties:** lots[] always; multi-signal linkage; notice-only
  lots become inventory (7).
- **Entity relationships:** keep `lot_index` grouping; promote
  Borrower/Bank/Village to graph nodes keyed by canonical (gazetteer/alias)
  identity so "borrowers with >3 properties" stops depending on string
  equality of names.

## 5. Model & technique notes

- **Structured outputs everywhere** (P7). Biggest robustness-per-hour change.
- **Implicit prompt caching** (already exploited on Gemini Flash) makes the
  single big canonical prompt affordable; batch warm runs, as today.
- **Extract-then-verify beats extract-harder:** a cheap second call that
  *checks* a flagged extraction against the source (or the image) is more
  effective than more few-shots on the first call. Wire it to validator
  scores so it only runs on the ~10–15% that need it.
- **Self-consistency for money fields on flagged docs** (2-of-3 vote on
  reserve/EMD) is cheap insurance where a wrong digit is maximally harmful.
- **Fine-tuning is premature** at ~2.2k docs, but the growing
  reviewer-verified gold set is exactly the asset that later distils the
  classifier/extractor into a cheaper model — keep investing in it.

## 6. Suggested sequencing (highest impact first)

1. **Canonicalize Path B** (P1+P3): derive descriptions + fields from
   `extraction_json`; retire Stages 1, 1.3, 1.4 after a shadow-run comparison
   on the corpus (the batch-report harness is already the diff tool).
2. **Deterministic parsers + verbatim-only prompts** (P4) — retroactive over
   cached extractions, zero LLM cost.
3. **LGD gazetteer resolution** (P5) — bounded effort, large recall/precision
   win on the fields users filter by.
4. **Multi-signal lot linkage + notice-only lots as inventory** (P2).
5. **Vision-mode re-extract for health-flagged docs** (P6).
6. **Structured outputs + content-hash manifest** (P7, P8) as enabling infra
   alongside the above.

Everything in 1–3 reuses assets that already exist in the repo (Path B,
validators, evals, alias tables); the redesign is mostly *promotion and
deletion*, not new invention — which is the strongest sign the codebase has
already discovered the right architecture at its edges.
