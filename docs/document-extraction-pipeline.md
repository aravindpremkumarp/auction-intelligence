# The document extraction pipeline

How a bank auction sale notice becomes structured graph data: ten stages, what
each reads and writes, and how to run or debug any one of them.

Entry point: `python -m pipeline.document_pipeline`
Implementation: [`pipeline/document_pipeline.py`](../pipeline/document_pipeline.py)
Architecture rationale: [`extraction-pipeline-review-2026-07.md`](extraction-pipeline-review-2026-07.md)

---

## Why there are two orchestrators

`pipeline/run_pipeline.py` drives the **legacy** path — one flat
`{verifiable, enrichment}` blob per file from a text prompt (Stage 1), a
single/multi classifier (1.3), a description extractor (1.4), and a
reserve-price matcher that assigns descriptions to listings (1.45).

`pipeline/document_pipeline.py` drives the **grounded** path that replaced it:
one span-anchored LangExtract pass per document, deterministic unit parsing in
Python instead of LLM arithmetic, and promotion into the `:Lot`/`:Parcel` spine.
Those modules all existed and were individually solid; what was missing was
anything that knew the order and the flag coupling between them. That is what
this orchestrator owns — it contains no extraction logic of its own.

The legacy orchestrator is still there because retiring it needs a shadow-run
comparison across the corpus (review doc §6.1), not because both paths are
wanted long-term.

---

## The stages

| # | Stage | Module | Reads | Writes |
|---|-------|--------|-------|--------|
| 1 | `ocr` | `scripts.ocr_with_mineru` | notice file (jpg/png/pdf) | layout markdown, cached on disk |
| 2 | `load-markdown` | `pipeline.load_markdowns_to_neo4j` | markdown + block caches | `:Document.markdown`, `.blocks` |
| 3 | `ocr-health` | `pipeline.ocr_health` | `:Document.markdown` | intrinsic OCR pathology score |
| 4 | `score-markdown` | `pipeline.score_markdown` | `:Document.markdown` | read-coverage score |
| 5 | `extract` | `pipeline.load_extractions` | `:Document.markdown` | `.extraction_json`, `.extraction_score` |
| 6 | `apply` | `pipeline.apply_extractions` | `.extraction_json` | `:AuctionProperty` fields |
| 7 | `places` | `pipeline.resolve_places` | scraped `:City` / `:Area` | `:PlaceAlias`, `:ALIAS_OF` |
| 8 | `promote` | `pipeline.promote_extractions` | `.extraction_json` + geography | `:Lot`, then `:Parcel` |
| 9 | `embed-markdown` | `pipeline.embed_markdowns` | `:Document.markdown` | `notice_markdown_idx` |
| 10 | `embed-description` | `pipeline.embed_descriptions` | property descriptions | `property_desc_idx` |

Stages 1–4 are **read and triage**: get the notice into text, then measure how
well that worked. `ocr-health` catches the failure modes the OCR engine actually
exhibits (repetition loops, token leaks, truncation, foreign-script
hallucination); `score-markdown` measures how much of the notice was captured at
all. Both are scores on the `:Document`, so a bad read is visible before anything
downstream trusts it.

Stage 5 is the **one extraction pass**. Every entity carries a character span
into the markdown, so any value can be traced back to the text that produced it,
and `pipeline/validators.py` scores the result without needing ground truth.

Stages 6–8 are **normalization and promotion**. Unit conversion happens in
`pipeline/measures.py` — deterministic Python, not prompt arithmetic, because
41% of extents carried no normalized sq.ft figure when the model was asked to do
it and the misses were almost entirely non-sq-ft units. Fixes there apply
retroactively to cached extractions at zero LLM cost.

Stages 9–10 build the vector indexes `semantic_search` consumes.

---

## Two couplings the orchestrator enforces

**`places` before `promote`.** Phase A resolves scraped City/Area names onto the
canonical Tamil Nadu geography, and phase B resolves each `:Lot`'s location
against that. Run in the other order and lots land with unresolved geography and
stay that way until the next full promote — a silent staleness, not an error.

**A narrowed run skips parcels.** Phase C answers "which lots are the same
physical parcel?", which is a join over every identifier in the corpus. Answered
from a `--limit 50` slice it merges parcels that are only unique because the
other ~2,150 documents were not loaded. So `--limit` and `--filename` force
`--skip-parcels`, and the run prints a reminder at the end. To get parcels, run
the pipeline unnarrowed, or run `python -m pipeline.promote_extractions` alone
afterwards.

Both are unit-tested (`tests/pipeline/test_document_pipeline.py`) rather than
documented-and-hoped-for.

---

## Running it

```bash
python -m pipeline.document_pipeline              # every stage, whole corpus
python -m pipeline.document_pipeline --list       # stage table, then exit
python -m pipeline.document_pipeline --plan       # resolved plan + exact commands
```

Selecting stages:

```bash
--from extract              # start here, run to the end
--to apply                  # stop after this stage
--from extract --to promote # a window
--only extract,promote      # exactly these (ignores --from/--to)
--skip ocr,embed-markdown   # subtract from whatever survived
```

Stage keys are always ordered by the pipeline, never by the order you type them:
`--only promote,places` still runs `places` first.

Scope and behaviour:

```bash
--limit 50            # cap documents per stage (forces --skip-parcels)
--filename notices/x/n.jpg   # one document (forces --skip-parcels)
--force               # redo cached work, where the stage supports it
--dry-run             # pass --dry-run to stages that support it (no writes)
--http-api            # NEO4J_HTTP_API=1 for every stage — Bolt-blocked environments
--continue-on-error   # keep going past a failed stage (default: stop)
--skip-preflight      # skip the env-var check
```

Flags are dropped for stages that don't accept them — `resolve_places` has no
`--limit`, so a limited run simply runs it whole rather than aborting on an
unrecognized argument. `--plan` shows exactly what each stage will receive.

---

## Failure and resume

Each stage runs as its own subprocess, so a stage that dies (OOM on a
300-page notice, an OpenRouter 429 storm) takes its own process down, not the
run. By default the pipeline stops at the first failure.

Every run writes a manifest to `pipeline/output/runs/run_<timestamp>.json`,
updated after every stage — so a run killed mid-flight still leaves something to
resume from:

```bash
python -m pipeline.document_pipeline --resume
```

`--resume` restarts at the first stage of the most recent run that did not
succeed. A stage that failed is re-run rather than skipped: it may have written
partial output, and every stage is idempotent, so re-running is the safe default.
If the last run completed, `--resume` runs the full plan.

The manifest records each stage's exact argv, exit code and wall time, so
reproducing a failed stage by hand is a copy-paste out of `stages[].argv` in the
newest `pipeline/output/runs/run_*.json` (the directory is gitignored — these
are local run artifacts).

---

## Pre-flight

Credentials for the **whole plan** are checked before the first stage starts —
the alternative is discovering a missing embedding key after a two-hour OCR
batch. Neo4j accepts either naming convention (`NEO4J_USERNAME`/`NEO4J_PASSWORD`
or the Aura-style `CLIENT_ID`/`CLIENT_SECRET`), and the read stage's key depends
on `DESCRIPTION_OCR_ENGINE`: `datalab` (default) needs `DATALAB_API_KEY`,
`mineru` needs `MINERU_API_KEY`. Only the stages actually in the plan are
checked, so `--only apply` doesn't demand an OCR key.

---

## Debugging one stage

Every stage is a normal module with its own CLI — the orchestrator adds nothing
you can't do by hand:

```bash
python -m pipeline.document_pipeline --plan --only extract   # get the command
python -m pipeline.load_extractions --filename notices/x/n.jpg --force
```

Useful narrowings:

- **One document, end to end:** `--filename <Document.filename>` (parcels skipped).
- **Re-extract without re-reading:** `--from extract` reuses cached markdown.
- **See writes before making them:** `--dry-run` on `apply`, `places`, `promote`.
- **Bolt blocked?** `--http-api` routes every stage through Aura's HTTPS Query API.

---

## Tests

```bash
pytest tests/pipeline/test_document_pipeline.py -q
```

53 tests, no Neo4j and no API keys: the orchestrator's decision layer (plan
resolution, flag coupling, pre-flight, resume) is pure, and `run_plan` is tested
with `subprocess.run` stubbed, since what matters there is the manifest and the
stop/continue policy rather than the child process.

---

## What this does not fix

The orchestrator wires up what exists; it does not close the open items from the
review doc. Still outstanding: multi-signal lot↔listing linkage (P2 — matching
is still reserve-price-first), vision-mode re-extraction for health-flagged
documents (P6), schema-constrained decoding instead of hand-rolled JSON parsing
(P7), and the content-addressed manifest that would make caches invalidate on a
prompt change (P8). The run manifest here is per-run bookkeeping, not the
per-artifact content hash P8 describes.
