# Reader v2 — operations

The lot-first, schema-locked, code-grounded reader (`pipeline/reader/`) and how
to run it, measure it, switch to it and switch back. Plan and rationale:
`docs/extraction-pipeline-review-2026-07.md`, `docs/extraction-pipeline-audit-2026-08.md`,
and the PR series on branch `claude/inspiring-curie-frs1rb`.

## What it does, in one line each

| step | module | guarantee |
| --- | --- | --- |
| OCR gate | `pipeline/load_extractions --min-ocr` | a page under health 90 is not read; it is stamped `extraction_skipped_reason='ocr_health'` for re-OCR |
| segment | `reader/segment.py` | lots cut by code (table rows, serials, numbered paragraphs, price lines); a reserve-count guard refuses a cut that hides a lot; doubt = whole read |
| read | `reader/llm.py`, `reader/prompt.py`, `reader/schema.py` | temperature 0, seed, max_tokens, finish_reason checked; the model fills a form of verbatim quotes and may say `not_stated` |
| prove | `reader/ground.py`, `reader/tables.py` | every quote located inside its own lot (and table cell); unlocatable = dropped, never stored; fuzzy matches must keep every digit |
| parse | `reader/normalize.py` | money / date / area / possession by code; OCR-broken figures are `ILLEGIBLE`, never a guessed number |
| rules | `reader/consistency.py` | EMD vs reserve, dates, UDS vs parent, extent vs description, span inside its lot → `CONTESTED` |
| verify | `reader/stability.py` | re-read only uncertain fields; a short key-facts check on every lot; majority vote; disagreement stays `CONTESTED` |
| receipt | `reader/convert.py`, `reader/provenance.py` | page, block, source, method, evidence word, `inherited_from` on every entity |
| store | `pipeline/extraction_store.py` | previous read kept, reviewer corrections carried or orphaned (never misapplied), keep-better gate, `:ExtractionRun`, `:AuctionEvent` |

## Running it

```bash
# one notice, from a markdown file, with the routed model (needs OPENROUTER_API_KEY)
python -m pipeline.reader evals/fixtures/750348.txt

# which structured-output tier each model supports (record the table below)
python -m scripts.probe_structured_outputs

# the gold eval, three repeats, results kept for comparison
python -m evals.langextract_eval --reader langextract --repeats 3 --out evals/results/baseline-v1.json
python -m evals.langextract_eval --reader v2 --repeats 3 --stability verify --out evals/results/v2-verify.json
python -m evals.langextract_eval --rescore evals/results/v2-verify.json      # re-grade, no calls

# a shadow cycle on real notices (writes v1, stores v2 beside it), then the report
EXTRACT_READER=shadow python -m pipeline.load_extractions --limit 30 --workers 6
NEO4J_HTTP_API=1 python -m scripts.reader_shadow_report

# cutover: every new notice is read by v2
EXTRACT_READER=v2            # render.yaml, cron auction-extract

# the failing half of the corpus, through the keep-better gate (dry run first)
NEO4J_HTTP_API=1 python -m scripts.reread_failing --dry-run
NEO4J_HTTP_API=1 python -m scripts.reread_failing --reader v2 --concurrency 4

# rollback: env only, then put the previous reads back if needed
EXTRACT_READER=langextract
NEO4J_HTTP_API=1 python -m scripts.revert_extraction --reader v2 --dry-run
```

## Acceptance bar (gold v1 ≥ 40 notices, 3 repeats, v2 vs `baseline-v1.json`)

- Lot count exact on 100% of multi notices, every repeat.
- Wrong-lot bindings = 0 (every entity inside its own segment; `score_multi` finds none bound to a neighbour).
- Key-fact recall ≥ 95% mean, worst repeat ≥ 92%; closed-world precision ≥ 97%; invented EXPECT_NULL = 0; inferred place hierarchy = 0.
- 0 ungrounded stored entities; `markdown[start:end] == text` for all; dropped key facts ≤ 2%.
- Per-notice score spread ≤ 5 points on ≥ 90% of notices; `CONTESTED` key facts ≤ 3%.
- Cost per correctly extracted key fact ≤ v1's; per notice ≤ 2× v1; p95 wall time ≤ v1's chunked path.

## Results

| run | date | notes |
| --- | --- | --- |
| structured-output tiers | — | not yet run (`scripts/probe_structured_outputs.py`) |
| `baseline-v1.json` | — | not yet run; needs `OPENROUTER_API_KEY` |
| `v2-verify.json` | — | not yet run |
| shadow cycle | — | not yet run; `render.yaml` is set to `shadow` for the next cron cycle |

## Gold set

`evals/gold_manifest.json` tags every gold notice with its strata and the failure
mode it was picked for. v1 = the 9 seed notices + the reviewer sprint (~40);
`scripts/gold_candidates.py` queues the next candidates from spot-check
"wrong" verdicts, orphaned corrections and contested / dropped key facts.
`evals/export_review_gold.py` promotes verified notices (per-lot truth,
EXPECT_NULL, description spans).

## Still to do (needs an API key and the gold sprint)

1. Run the probe, the v1 baseline and the v2 eval; fill the Results table; tune
   only schema descriptions, RULES, few-shots, segment caps and consistency
   thresholds (`PR7`).
2. Run one shadow cycle; read `reader_shadow_report`; flip `EXTRACT_READER=v2`;
   run `reread_failing` (`PR8`).
3. Once every re-read document carries `extraction_reader='v2'`, delete the
   repair layer: `lot_chunks.extract_chunked` and retry hints, `lot_windows`,
   `reground_extractions`, `ground_missing` on the write path, `absence.no_clue`,
   `gap_fill` + `fill_gaps`, `_CLASS_ALIASES`, `renumber_lots`,
   `clear_boilerplate_possession`, `extract_routing.passes_for/char_buffer_for`,
   the `LANGEXTRACT_*` env and the `langextract` dependency, and
   `pipeline/prompts/extract_enrichment.txt` with it (`PR9`).
