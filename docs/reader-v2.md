# Reader v2 — operations

The lot-first, schema-locked, code-grounded reader (`pipeline/reader/`) and how
to run it, measure it, switch to it and switch back. Plan and rationale:
`docs/extraction-pipeline-review-2026-07.md`, `docs/extraction-pipeline-audit-2026-08.md`,
and the PR series on branch `claude/inspiring-curie-frs1rb`.

## Status (2026-10-03): parked

v2 is switched off (`EXTRACT_READER=langextract` in `render.yaml`); the old
reader stays in production. On 11 notices the two readers agreed on 95% of
key values (732 of 769, `evals/gold_sprint_v1_questions.json`), so v2 did not
show an accuracy gain worth the switch, and it failed on a 40-lot notice
whose scanned table is split across rows (a whole read hit `max_tokens`).
The code stays so it can be tried again with `--reader v2` or the flag.

Kept and used by the old reader: the eval harness, stable entity ids and
carried corrections, `extraction_prev_json` + `scripts/revert_extraction.py`,
the OCR gate, stale re-reads (`--stale` on the cron), per-model cost pricing,
and lots numbered in the notice's order on every save.

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

Live runs, 2026-10-01, the 9 seed gold notices × 3 repeats (`evals/results/`).
Cost is priced at OpenRouter's DeepSeek v4.1 Flash rates ($0.027 / $0.60 per 1M
tokens in / out) for both readers, from each run's own token counts.

| | v1 LangExtract (`baseline-v1.json`) | v2 reader (`v2-verify-fixed.json` + `v2-verify-fix4-752245.json`) |
| --- | --- | --- |
| accuracy (mean of notices) | 99.7% | 99.7% |
| key-fact recall | 100% | 100% |
| lot count exact, every repeat | yes | yes |
| notices with spread ≤ 5 points | 9 / 9 | 9 / 9 |
| ungrounded stored entities | 10 | **0** |
| invented values (EXPECT_NULL) | 1 | **0** |
| tokens in / out | 1.09M / 165k | **0.87M / 104k** |
| cost, 27 reads | $0.13 | **$0.09** |
| p95 seconds per notice | 104 | **89** |

The one miss both readers share is 750348 lot 2's flat: the description
prints "Flat No.5-3", the gold (from the borrower's address) says "S-3".
`wrong-lot bindings = 3` on 750348 for both readers is the metric, not a
misbinding: two flats of one serial share their survey, patta and plot
numbers.

How v2 got there — the first live run scored 89.5%; every loss was code:

| fix | notice | before → after |
| --- | --- | --- |
| a one-lot notice is never cut to its table row | 752245 | 6/11 → 10/11 |
| price cuts keep the bid increment / property id / EMD printed after the price | 753006 | 25/30 → 30/30 |
| "35.15,000" read as Indian grouping with a misread comma (marked `_ocr_repaired`) | 750348 | lot found |
| a key-facts vote may replace a value it can locate, never delete one | 750348 | 26/33 → 32/33 |
| OA No. preferred over TRC No. for `court_reference` | 750600 | 8/9 → 9/9 |
| a unit word counts only beside the figure, never in the amount in words | 752245 | 10/11 → 11/11 (reserve was 2,88,900 crore) |

Structured-output tiers (`scripts/probe_structured_outputs.py`): strict
`json_schema` works on both deepseek/deepseek-v4.1-flash and
deepseek/deepseek-v4-pro; `json_object` works with one repair; plain text
does not.

**What these 9 notices cannot show.** None has 20+ lots, a long HTML table,
Tamil text or a poor scan — exactly where v1's documented failures
(lot under-recall, ungrounded spans, wrong lot numbers) live. Parity here
says v2 is safe to shadow; the 40-notice gold sprint decides the cutover.

## Gold set

`evals/gold_manifest.json` tags every gold notice with its strata and the failure
mode it was picked for. v1 = the 9 seed notices + the reviewer sprint (~40);
`scripts/gold_candidates.py` queues the next candidates from spot-check
"wrong" verdicts, orphaned corrections and contested / dropped key facts.
`evals/export_review_gold.py` promotes verified notices (per-lot truth,
EXPECT_NULL, description spans).

## Still to do (needs an API key and the gold sprint)

1. Done on the 9 seed notices (Results above). Repeat on the ~40-notice gold
   set once it is verified; tune only schema descriptions, RULES, few-shots,
   segment caps and consistency thresholds (`PR7`).
2. Run one shadow cycle; read `reader_shadow_report`; flip `EXTRACT_READER=v2`;
   run `reread_failing` (`PR8`).
3. Once every re-read document carries `extraction_reader='v2'`, delete the
   repair layer: `lot_chunks.extract_chunked` and retry hints, `lot_windows`,
   `reground_extractions`, `ground_missing` on the write path, `absence.no_clue`,
   `gap_fill` + `fill_gaps`, `_CLASS_ALIASES`, `renumber_lots`,
   `clear_boilerplate_possession`, `extract_routing.passes_for/char_buffer_for`,
   the `LANGEXTRACT_*` env and the `langextract` dependency, and
   `pipeline/prompts/extract_enrichment.txt` with it (`PR9`).
