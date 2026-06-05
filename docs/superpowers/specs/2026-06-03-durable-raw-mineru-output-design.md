# Durable Raw MinerU Output — Design Spec

**Date:** 2026-06-03
**Files touched:** `pipeline/load_markdowns_to_neo4j.py` (capture raw in the loader),
`api/review/blocks.py` (capture raw in `reingest_notice`), `scripts/backfill_markdown_raw.py`
(new backfill), plus tests under `tests/pipeline/` and `tests/api/`.
**Status:** Approved design, ready for implementation plan

## Problem

A reviewer asked to "store MinerU's `full.md` so we get all the labels MinerU
provides." Investigating the request surfaced a conflation and one real gap.

**The conflation.** `full.md` is *already* stored — cached on disk at
`pipeline/cache/mineru_markdown/<safe>.md` and loaded verbatim into
`Document.markdown`. And `full.md` is **not** where labels live: markdown carries
no block labels, only text, HTML tables, and `![]()` image links. The block
labels (Text/Title/Table/Image/Header/Footer/Discarded/…) come from MinerU's
`content_list.json`, which is *also* already cached verbatim on disk at
`pipeline/cache/mineru_blocks/<safe>.json` and loaded — in normalized form — into
`Document.blocks`.

**The real gap (Gap A): the raw MinerU output is not durable in the DB.**
`Document.markdown` starts life as the raw `full.md`, but the first time a reviewer
edits any block, `_save_doc` (`api/review/blocks.py:307`) overwrites it with the
*re-assembled* markdown (`assemble_markdown`), which drops Image blocks and any
empty-text block. After that, the original `full.md` survives only in the on-disk
cache — and that cache is a pipeline-machine artifact, not something the production
API host carries. So in production, after the first edit, the original raw MinerU
output is effectively gone. The verbatim `content_list.json` is likewise only on
disk; Neo4j holds just the lossy normalized `Document.blocks`.

## Goal & scope

Preserve the raw MinerU output durably, in the database, so that **reviewer edits
and re-ingests can never lose it**. This is durability only.

In scope:

- Persist raw `full.md` and raw `content_list.json` onto the `Document` (Neo4j).
- Capture on every full-document OCR run **going forward**.
- **Backfill** the existing corpus from the on-disk cache before it ages out.

Explicitly **not** in scope (considered and declined during brainstorming):

- *Lossless blocks* — carrying dropped per-block fields (image `content`,
  `img_path`, `sub_type`, raw label) onto `Document.blocks`.
- *Images visible in markdown* — re-representing images/QR/logos in the assembled
  markdown instead of dropping them.
- *Decode QR payload* — reading the bank-website / e-auction URLs the QR codes
  encode.
- Any UI, R2 archival, image-byte preservation, or change to `_save_doc`'s
  reassembly of `d.markdown`.

## Decisions (from brainstorming)

1. **Storage: two new Neo4j properties on `Document`** (not R2, not disk zips).
   The whole app reads from Neo4j, so this lands in prod automatically, survives
   edits, and is queryable with zero new infra. Sizing makes this a non-issue:
   raw `content_list.json` is median ~6.4 KB (max 44 KB) and `full.md` median
   ~4 KB (max 34 KB); across all ~2,624 Documents that is roughly **~30 MB**
   total (~200 MB absolute worst case).
2. **Capture semantics: written on every full-document OCR run, and by nothing
   else.** Block edits and single-block re-extract never touch the raw copy.
3. **Scope: backfill + going-forward.** The on-disk cache still holds raw
   artifacts for most existing docs, so backfill is cheap and recovers them.

## Approaches considered (storage)

- **A — Two Neo4j properties on `Document` (CHOSEN).** `markdown_raw` +
  `blocks_raw`, written once per OCR run, never clobbered by edits. Backfill from
  the disk cache. Pros: in prod automatically, survives edits, queryable, no new
  infra, fits the all-from-Neo4j architecture; ~30 MB is negligible. Cons: bounded
  node growth — requires the discipline of never SELECT-ing these fields in hot
  queries.
- **B — R2 archive (cold storage).** Upload raw artifacts to R2 next to the
  source; store keys on the Document. Rejected: new plumbing (upload + backfill
  creds), fetch-to-view, not queryable — overkill for ~30 MB of text. (Only earns
  its keep if we later want to archive the actual image bytes.)
- **C — Keep raw zips on disk (`keep_zip=True`).** One-flag change in
  `download_and_cache`. Rejected: a disk artifact on the pipeline machine, not in
  prod and not tied to the Document — it does **not** meet the durability goal.

## Data model — three new `Document` properties

| Property | Type | Meaning |
|---|---|---|
| `markdown_raw` | string | Verbatim `full.md` from the MinerU run that produced the current blocks |
| `blocks_raw` | string | Verbatim `content_list.json` (the raw MinerU array, as cached on disk) from that same run |
| `markdown_raw_at` | datetime | When raw was captured; doubles as the "raw exists" marker |

Names parallel the existing `markdown` / `blocks` / `markdown_source` /
`markdown_model` convention. `markdown_model` already records the model, so no new
provenance field is needed. `blocks_raw` is the raw content-list array as written
to disk by `download_and_cache` (`json.dumps(blocks_raw, ensure_ascii=False)`) —
semantically verbatim MinerU output.

## Capture semantics — the core invariant

Raw is written on **every full-document MinerU OCR run**, and by **nothing else**:

- ✅ Bulk loader (`load_markdowns_to_neo4j`) and `reingest_notice` → write raw.
- ❌ Block edits (`_save_doc`, `api/review/blocks.py:307`) and single-block
  re-extract (`re_extract_block`, `api/review/blocks.py:604`, which calls
  `_save_doc`) → never touch raw.

So an edit can never clobber the raw copy (the goal), and a fresh full OCR refreshes
it so it stays consistent with the new `Document.blocks`.

**Invariant:** `markdown_raw` / `blocks_raw` / `markdown_raw_at` always correspond
to the MinerU run that produced the current `Document.blocks` at the last full OCR.

## Capture points — two edits

1. **`pipeline/load_markdowns_to_neo4j.py`** (covers the bulk command *and*
   `scripts/ocr_missing_markdowns.py`, which calls `write_markdowns`). The per-doc
   row already carries the raw `full.md` text (read at line ~231). Additionally
   read the raw content-list file (`BLOCKS_DIR / f"{safe_name(fp)}.json"`) as text,
   add `markdown_raw` + `blocks_raw` to the row payload, and extend the
   `write_markdowns` Cypher (`SET d.markdown … = row.markdown`, line ~127) with:

   ```cypher
   SET d.markdown_raw    = row.markdown_raw,
       d.blocks_raw      = row.blocks_raw,
       d.markdown_raw_at = datetime()
   ```

   When the content-list file is missing, leave `blocks_raw` null (markdown still
   captured); `markdown_raw` mirrors `row.markdown`.

2. **`api/review/blocks.py:reingest_notice`** (the only direct writer that bypasses
   the loader, line ~721). After `download_and_cache` returns `md_path` +
   `blocks_path` (line ~932), read both raw texts and add the three fields to the
   existing `SET d.markdown = $markdown …` (line ~1000). A crop/rotation re-ingest
   therefore refreshes `markdown_raw` to *that* run's output — intended, since the
   resulting `Document.blocks` also come from that run, keeping the invariant.

`_save_doc` and `re_extract_block` are deliberately left untouched.

## Backfill — one script

New `scripts/backfill_markdown_raw.py`, mirroring the existing
`scripts/backfill_blocks.py` pattern. For every `Document` where
`markdown_raw IS NULL` and a disk-cache entry exists, read
`pipeline/cache/mineru_markdown/<safe>.md` + `pipeline/cache/mineru_blocks/<safe>.json`
and `SET` the three properties. Idempotent; `--force` overwrites; reuses
`safe_name` and the existing cache-directory constants.

## Hot-query hygiene

`markdown_raw` / `blocks_raw` are **never** selected by `get_blocks()` or any
queue/list query — they stay out of every hot path so node growth has no
read-cost impact. Exposing the raw (a `GET …/raw` endpoint or an annotator
"view raw" toggle) is an easy follow-up and is out of scope here.

## Testing

- **Loader:** the per-doc row includes `markdown_raw` (= `full.md` text) and
  `blocks_raw` (= raw content-list text) read from the cache; missing content-list
  yields null `blocks_raw` without failing the row.
- **Reingest:** `reingest_notice`'s write includes the three raw fields.
- **Regression guard (the crux):** a block edit through `_save_doc` updates
  `markdown` but leaves `markdown_raw` / `blocks_raw` **unchanged**.
- **Backfill:** sets the fields when null, skips when already set, overwrites under
  `--force`.

Follow the existing `tests/pipeline/test_*` and `tests/api/test_review_*` patterns.

## Bonus this unlocks

With `blocks_raw` preserved, the normalization + assembly
(`parse_mineru_content_list` → `assemble_markdown`) can be re-run later with
improved logic **without paying for another MinerU call** — useful if any of the
out-of-scope gaps (lossless blocks, image markers) are picked up in future.
