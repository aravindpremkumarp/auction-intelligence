# Keep MinerU's Full Output — Design Spec

**Date:** 2026-06-20
**Status:** Implemented
**Files touched:** `pipeline/storage.py`, `pipeline/mineru.py`, `pipeline/mineru_api.py`,
`pipeline/load_markdowns_to_neo4j.py`, `api/review/blocks.py`, `scripts/ocr_with_mineru.py`,
plus tests under `tests/pipeline/` and `tests/api/`.

## Problem

A MinerU OCR run returns a result **zip**, but the pipeline only ever read two
members out of it — `full.md` (→ `Document.markdown` / `markdown_raw`) and
`*_content_list.json` (→ `Document.blocks` / `blocks_raw`) — and discarded the
rest. Everything else MinerU emits was thrown away:

- the **`images/` folder** — the cropped JPGs of every figure **and every table**
  (referenced by each content-list entry's `img_path`),
- per-block fields the normalizer dropped: `img_path`, `text_level`, `sub_type`,
  `table_caption`, `table_footnote`,
- MinerU's intermediate artifacts (`*_middle.json`, `*_model.json`, layout/spans
  PDFs).

The earlier "durable raw MinerU output" work (2026-06-03) deliberately scoped
these out, noting an R2 archive "only earns its keep if we later want to archive
the actual image bytes." This is that follow-up: **keep everything MinerU gives
us so it can be used if required.**

## Decisions

1. **Archive the complete result zip to R2** (public bucket, key
   `mineru/raw_zips/<safe>.zip`). The zip is a verbatim, future-proof copy of
   100% of MinerU's output — nothing can be lost to a later normalization change.
   R2 (not Neo4j) because the zip is multi-MB binary; Neo4j stays text-only.
2. **Extract the image/table crops to R2** (`mineru/images/<safe>/<basename>`)
   and **wire the dropped per-block fields onto `Document.blocks`** so the data
   is usable now, not just archived. Each block gains `img_path` (verbatim),
   `img_url` (the archived crop's public URL), `text_level`, `sub_type`,
   `table_caption`, `table_footnote`.
3. **Going forward only.** Capture on every full-document OCR run and reviewer
   re-ingest. Existing notices are **not** re-OCR'd (their original zips were
   already discarded; recovering them would cost MinerU credits) — they keep the
   `markdown_raw` / `blocks_raw` text already preserved.
4. **Best-effort archival.** An R2 misconfig or upload error is logged and
   yields a partial/empty result; it never fails the OCR run itself.

## Data model

**New `Document` properties**

| Property | Type | Meaning |
|---|---|---|
| `mineru_zip_url` | string | Public URL of the archived complete MinerU result zip |
| `mineru_zip_at`  | datetime | When the zip was archived |

**New per-block fields** (inside `Document.blocks` JSON)

| Field | Source |
|---|---|
| `img_path` | verbatim content-list `img_path` (e.g. `images/<hash>.jpg`) |
| `img_url` | archived R2 URL of that crop (None when the run didn't archive) |
| `text_level` | content-list `text_level` (heading level) |
| `sub_type` | content-list `sub_type` |
| `table_caption` / `table_footnote` | content-list fields, list-joined to a string |

## Flow

`download_and_cache(..., archive_to_r2=True)` is the single choke point where
the zip is in hand. When the flag is set it calls `archive_zip_to_r2`, which
uploads the zip + every `images/` member and writes a sidecar
`pipeline/cache/mineru_meta/<safe>.json` = `{zip_url, img_map, archived_at}`
(`img_map` keyed by crop basename — globally unique for MinerU).

- **Bulk pipeline** (`scripts/ocr_with_mineru.py`) and **reviewer re-ingest**
  (`api/review/blocks.py:reingest_notice`) pass `archive_to_r2=True`.
- The **loader** (`load_markdowns_to_neo4j`) reads the sidecar, passes `img_map`
  into `parse_mineru_content_list` (resolves each `img_url`), and stamps
  `mineru_zip_url` on the Document. Re-ingest reuses the same `load_blocks_for`
  path, so both get image URLs from one code path.
- **Single-block re-extract** leaves `archive_to_r2` off (default) — it is not a
  full-document run and must not spawn per-crop uploads.

The new block fields are preserved through `_normalize_replacement_blocks`
(undo/redo) and surface to the annotator automatically via `get_blocks`
(pass-through). The raw-capture invariant from 2026-06-03 is unchanged: edits
never touch `markdown_raw` / `blocks_raw` / the archive.

## Out of scope

- Re-OCR backfill of the existing corpus (per decision 3).
- Re-representing images in the assembled markdown, or decoding QR payloads.
- An annotator UI that renders `img_url` — the data is now present; surfacing it
  visually is an easy follow-up.
