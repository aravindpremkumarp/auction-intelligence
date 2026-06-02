# Durable Raw MinerU Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist MinerU's raw `full.md` and raw `content_list.json` onto each `Document` (Neo4j) so reviewer edits and re-ingests can never lose the original OCR output.

**Architecture:** Add three write-once `Document` properties (`markdown_raw`, `blocks_raw`, `markdown_raw_at`). Capture them at the two full-document OCR write paths — the bulk loader and `reingest_notice` — and never at edit paths (`_save_doc`, `re_extract_block`). A one-time backfill copies the artifacts already sitting in the on-disk cache onto existing Documents. Neo4j is schemaless, so no migration is required.

**Tech Stack:** Python 3, Neo4j (Cypher via `api.neo4j_client.run_query`), pytest. Spec: `docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md`. Branch: `claude/durable-raw-mineru-output`.

---

## Background the engineer needs

- MinerU OCR produces two artifacts per notice, both cached on disk:
  - `pipeline/cache/mineru_markdown/<safe>.md` — the raw `full.md` (text + HTML tables + `![](images/…)` links).
  - `pipeline/cache/mineru_blocks/<safe>.json` — the raw `content_list.json` array (every field MinerU emits, written verbatim by `download_and_cache`).
  - `<safe>` is `file_path` with `/`, `\`, `:` replaced by `_` — produced by `safe_name()` (loader) / `safe_cache_name()` (pipeline). They are identical implementations.
- `Document.markdown` starts as the raw `full.md` but is **overwritten by the re-assembled markdown** (`assemble_markdown`, which drops images) the first time a reviewer edits a block via `_save_doc`. `Document.blocks` only ever holds the *normalized* block list, never the raw content-list. That loss-on-edit is the gap this plan closes.
- Tests are DB-free. `api.neo4j_client` lazy-inits its driver, so importing the modules under test never touches a database. `tests/api/conftest.py` swaps `api.neo4j_client` for an in-memory stub; `tests/pipeline/` has no conftest and uses the real (lazy, import-safe) client. In both cases we `monkeypatch` the module-level `run_query` / `run_read_query` name to capture calls.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `pipeline/load_markdowns_to_neo4j.py` | Disk→Neo4j loader; gains `read_raw_artifacts()` helper + raw fields in `write_markdowns` and the row payload | Modify |
| `api/review/blocks.py` | Review writes; gains `_persist_reingest_result()` helper that captures raw, called by `reingest_notice` | Modify |
| `scripts/backfill_markdown_raw.py` | One-time backfill of raw fields from the disk cache | Create |
| `tests/pipeline/test_markdown_raw_capture.py` | `read_raw_artifacts` + loader write-path tests | Create |
| `tests/pipeline/test_backfill_markdown_raw.py` | backfill fetch-filter + write-param tests | Create |
| `tests/api/test_review_raw_capture.py` | reingest-persist test + `_save_doc`/`_load_doc` hygiene guards | Create |

---

## Task 1: `read_raw_artifacts` helper in the loader

**Files:**
- Modify: `pipeline/load_markdowns_to_neo4j.py` (add helper after `safe_name`, ~line 61)
- Test: `tests/pipeline/test_markdown_raw_capture.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/pipeline/test_markdown_raw_capture.py`:

```python
"""Durable raw-MinerU capture: read_raw_artifacts + loader write path.

Reads raw full.md + content_list.json off the on-disk cache, and verifies
write_markdowns persists them into the new Document properties. DB-free:
run_query is monkeypatched to capture; cache dirs redirected to tmp_path.
See docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.
"""
from __future__ import annotations

import pipeline.load_markdowns_to_neo4j as L


def _redirect(tmp_path, monkeypatch):
    md_dir = tmp_path / "md"
    bl_dir = tmp_path / "blocks"
    md_dir.mkdir()
    bl_dir.mkdir()
    monkeypatch.setattr(L, "MD_DIR", md_dir)
    monkeypatch.setattr(L, "BLOCKS_DIR", bl_dir)
    return md_dir, bl_dir


def test_read_raw_artifacts_none_when_absent(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    md_raw, bl_raw = L.read_raw_artifacts("notices/x.jpg")
    assert md_raw is None
    assert bl_raw is None


def test_read_raw_artifacts_reads_both_verbatim(tmp_path, monkeypatch):
    md_dir, bl_dir = _redirect(tmp_path, monkeypatch)
    fp = "notices/x.jpg"
    safe = L.safe_name(fp)
    (md_dir / f"{safe}.md").write_text("# RAW\n![](images/abc.jpg)", encoding="utf-8")
    (bl_dir / f"{safe}.json").write_text('[{"type":"image"}]', encoding="utf-8")
    md_raw, bl_raw = L.read_raw_artifacts(fp)
    assert md_raw == "# RAW\n![](images/abc.jpg)"
    assert bl_raw == '[{"type":"image"}]'


def test_read_raw_artifacts_blocks_none_when_only_md(tmp_path, monkeypatch):
    md_dir, _ = _redirect(tmp_path, monkeypatch)
    fp = "notices/y.jpg"
    (md_dir / f"{L.safe_name(fp)}.md").write_text("only md", encoding="utf-8")
    md_raw, bl_raw = L.read_raw_artifacts(fp)
    assert md_raw == "only md"
    assert bl_raw is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/pipeline/test_markdown_raw_capture.py -v`
Expected: FAIL — `AttributeError: module 'pipeline.load_markdowns_to_neo4j' has no attribute 'read_raw_artifacts'`.

- [ ] **Step 3: Implement the helper**

In `pipeline/load_markdowns_to_neo4j.py`, immediately after the `safe_name` function (ends ~line 61), add:

```python
def read_raw_artifacts(file_path: str) -> tuple[str | None, str | None]:
    """Return ``(markdown_raw, blocks_raw)`` verbatim from the on-disk MinerU
    cache for ``file_path``.

    ``markdown_raw`` is the raw ``full.md``; ``blocks_raw`` is the raw
    ``content_list.json`` text (the array MinerU emitted, as written to disk by
    ``pipeline.mineru_api.download_and_cache``). Either is ``None`` when its
    cache file is missing or unreadable. Used by the loader and the backfill
    script so both read the cache the same way.
    """
    md_p = MD_DIR / f"{safe_name(file_path)}.md"
    bl_p = BLOCKS_DIR / f"{safe_name(file_path)}.json"
    md_raw: str | None = None
    bl_raw: str | None = None
    if md_p.exists():
        try:
            md_raw = md_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            md_raw = None
    if bl_p.exists():
        try:
            bl_raw = bl_p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            bl_raw = None
    return md_raw, bl_raw
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/pipeline/test_markdown_raw_capture.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/load_markdowns_to_neo4j.py tests/pipeline/test_markdown_raw_capture.py
git commit -m "feat(pipeline): read_raw_artifacts helper for durable MinerU raw" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Loader persists `markdown_raw` + `blocks_raw`

**Files:**
- Modify: `pipeline/load_markdowns_to_neo4j.py` — `write_markdowns` Cypher (~line 124) and the `payloads.append({...})` in `main()` (~line 251)
- Test: `tests/pipeline/test_markdown_raw_capture.py` (append one test)

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_markdown_raw_capture.py`:

```python
def test_write_markdowns_sets_raw_fields(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(L, "run_query", _capture)
    row = {"file_path": "notices/x.jpg", "markdown": "MD",
           "markdown_raw": "MD", "blocks_raw": "[1]",
           "blocks_json": None, "model": None}
    L.write_markdowns([row], "mineru", "mineru-vlm")
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.blocks_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["rows"][0]["markdown_raw"] == "MD"
    assert captured["params"]["rows"][0]["blocks_raw"] == "[1]"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/pipeline/test_markdown_raw_capture.py::test_write_markdowns_sets_raw_fields -v`
Expected: FAIL — `assert 'd.markdown_raw' in captured["cypher"]` fails (the Cypher has no raw fields yet).

- [ ] **Step 3: Add the raw fields to the `write_markdowns` Cypher**

In `pipeline/load_markdowns_to_neo4j.py`, find the `SET` block in `write_markdowns` and insert the three raw lines right after `d.markdown_loaded_at  = datetime(),`:

```python
    cypher = """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown            = row.markdown,
            d.markdown_source     = $source,
            d.markdown_model      = coalesce(row.model, $model),
            d.markdown_loaded_at  = datetime(),
            d.markdown_raw        = coalesce(row.markdown_raw, d.markdown_raw),
            d.blocks_raw          = coalesce(row.blocks_raw, d.blocks_raw),
            d.markdown_raw_at     = CASE WHEN row.markdown_raw IS NULL
                                        THEN d.markdown_raw_at ELSE datetime() END,
            d.blocks              = CASE
                WHEN row.blocks_json IS NULL THEN d.blocks
                ELSE row.blocks_json END,
            d.blocks_revision     = CASE
                WHEN row.blocks_json IS NULL THEN coalesce(d.blocks_revision, 0)
                ELSE coalesce(d.blocks_revision, 0) END
    """
```

(`coalesce(row.markdown_raw, d.markdown_raw)` never clobbers an existing raw copy with a null; `blocks_raw` is the same. The timestamp updates only when a fresh `markdown_raw` is supplied.)

- [ ] **Step 4: Add the raw fields to the row payload in `main()`**

Find the `payloads.append({...})` block (~line 251) and change it to read the raw blocks text and pass both new keys:

```python
        _, blocks_raw = read_raw_artifacts(fp)
        payloads.append({
            "file_path":    fp,
            "markdown":     text,
            "markdown_raw": text,
            "blocks_raw":   blocks_raw,
            "blocks_json":  blocks_json,
            "model":        PRECLEAN_MODEL_TAG if is_precleaned(fp) else None,
        })
```

(`text` is the raw `full.md` already read at ~line 231, so it *is* `markdown_raw`. `read_raw_artifacts(fp)` supplies `blocks_raw`; its redundant re-read of the markdown file is negligible for this batch script and keeps one shared reader.)

- [ ] **Step 5: Run the full Task-1+2 test file to verify it passes**

Run: `python -m pytest tests/pipeline/test_markdown_raw_capture.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add pipeline/load_markdowns_to_neo4j.py tests/pipeline/test_markdown_raw_capture.py
git commit -m "feat(pipeline): loader persists markdown_raw + blocks_raw" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Capture raw on `reingest_notice`; guard the edit paths

**Files:**
- Modify: `api/review/blocks.py` — add `_persist_reingest_result()` above `def reingest_notice` (~line 721); replace the inline `run_query` in `reingest_notice` (~lines 994–1011)
- Test: `tests/api/test_review_raw_capture.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_review_raw_capture.py`:

```python
"""Durable raw-MinerU capture on the review side.

Asserts the full-OCR reingest persist writes markdown_raw/blocks_raw, and the
crucial invariant that an edit (_save_doc) NEVER writes the raw fields, and that
the hot read query (_load_doc) never selects them. Imports api.review.blocks
under the conftest neo4j stub; run_query is monkeypatched to capture.
See docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.
"""
from __future__ import annotations

import inspect

import api.review.blocks as B


def test_persist_reingest_writes_raw(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 1}]

    monkeypatch.setattr(B, "run_query", _capture)
    B._persist_reingest_result(
        "n.jpg", markdown="MD", blocks_json="{}",
        markdown_raw="RAWMD", blocks_raw="[RAW]",
    )
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.blocks_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["markdown_raw"] == "RAWMD"
    assert captured["params"]["blocks_raw"] == "[RAW]"


def test_save_doc_never_writes_raw(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 2}]

    monkeypatch.setattr(B, "run_query", _capture)
    B._save_doc("n.jpg", {"schema_version": 1, "blocks": []}, 0)
    assert "markdown_raw" not in captured["cypher"]
    assert "blocks_raw" not in captured["cypher"]


def test_load_doc_query_excludes_raw_fields():
    src = inspect.getsource(B._load_doc)
    assert "markdown_raw" not in src
    assert "blocks_raw" not in src
```

- [ ] **Step 2: Run the tests to verify the right ones fail**

Run: `python -m pytest tests/api/test_review_raw_capture.py -v`
Expected: `test_persist_reingest_writes_raw` FAILS (`AttributeError: ... has no attribute '_persist_reingest_result'`). `test_save_doc_never_writes_raw` and `test_load_doc_query_excludes_raw_fields` PASS already (they lock current behavior — that is intended; they are regression guards).

- [ ] **Step 3: Add the `_persist_reingest_result` helper**

In `api/review/blocks.py`, directly above `def reingest_notice(filename: str, by_email: str) -> dict:` (~line 721), add:

```python
def _persist_reingest_result(filename: str, *, markdown: str, blocks_json: str,
                             markdown_raw: str | None,
                             blocks_raw: str | None) -> None:
    """Persist a fresh full-document MinerU re-ingest.

    Writes the working ``markdown`` + ``blocks`` AND the durable raw copy
    (``markdown_raw`` = full.md, ``blocks_raw`` = content_list.json). Bumps
    ``blocks_revision`` and clears the markdown verdict, same as before. The raw
    fields are written here — a full OCR run — and NEVER by the edit paths
    (``_save_doc`` / ``re_extract_block``), so a reviewer edit can't lose them. A
    crop/rotation re-ingest refreshes the raw copy to that run's output, which is
    correct: the resulting blocks come from that run too.
    """
    run_query(
        """
        MATCH (d:Document {filename: $filename})
        SET d.markdown            = $markdown,
            d.blocks              = $blocks_json,
            d.markdown_raw        = coalesce($markdown_raw, d.markdown_raw),
            d.blocks_raw          = coalesce($blocks_raw, d.blocks_raw),
            d.markdown_raw_at     = CASE WHEN $markdown_raw IS NULL
                                        THEN d.markdown_raw_at ELSE datetime() END,
            d.blocks_revision     = coalesce(d.blocks_revision, 0) + 1,
            d.markdown_loaded_at  = datetime(),
            d.markdown_source     = 'mineru',
            d.markdown_model      = 'mineru-vlm',
            d.markdown_verified_at = NULL,
            d.markdown_verified_by = NULL,
            d.markdown_quality     = NULL
        """,
        {"filename": filename, "markdown": markdown, "blocks_json": blocks_json,
         "markdown_raw": markdown_raw, "blocks_raw": blocks_raw},
    )
```

- [ ] **Step 4: Call the helper from `reingest_notice`**

In `reingest_notice`, find this block (~lines 994–1011):

```python
    doc = {"schema_version": 1, "blocks": blocks}
    blocks_json = json.dumps(doc, ensure_ascii=False)
    new_md = md_path.read_text(encoding="utf-8")
    run_query(
        """
        MATCH (d:Document {filename: $filename})
        SET d.markdown            = $markdown,
            d.blocks              = $blocks_json,
            d.blocks_revision     = coalesce(d.blocks_revision, 0) + 1,
            d.markdown_loaded_at  = datetime(),
            d.markdown_source     = 'mineru',
            d.markdown_model      = 'mineru-vlm',
            d.markdown_verified_at = NULL,
            d.markdown_verified_by = NULL,
            d.markdown_quality     = NULL
        """,
        {"filename": filename, "markdown": new_md, "blocks_json": blocks_json},
    )
```

Replace it with (note `blocks_path` is already in scope from `download_and_cache` at ~line 932):

```python
    doc = {"schema_version": 1, "blocks": blocks}
    blocks_json = json.dumps(doc, ensure_ascii=False)
    new_md = md_path.read_text(encoding="utf-8")
    blocks_raw = blocks_path.read_text(encoding="utf-8") if blocks_path else None
    _persist_reingest_result(
        filename,
        markdown=new_md,
        blocks_json=blocks_json,
        markdown_raw=new_md,
        blocks_raw=blocks_raw,
    )
```

- [ ] **Step 5: Run the tests to verify all pass**

Run: `python -m pytest tests/api/test_review_raw_capture.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add api/review/blocks.py tests/api/test_review_raw_capture.py
git commit -m "feat(review): capture raw MinerU output on reingest (markdown_raw/blocks_raw)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Backfill script for existing Documents

**Files:**
- Create: `scripts/backfill_markdown_raw.py`
- Test: `tests/pipeline/test_backfill_markdown_raw.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/pipeline/test_backfill_markdown_raw.py`:

```python
"""Backfill script: fetch filter + write params for durable raw capture."""
from __future__ import annotations

import scripts.backfill_markdown_raw as BF


def test_fetch_pending_filters_null_when_not_force(monkeypatch):
    captured = {}

    def _cap(cypher, **kwargs):
        captured["cypher"] = cypher
        return []

    monkeypatch.setattr(BF, "run_read_query", _cap)
    BF.fetch_pending(None, force=False)
    assert "d.markdown_raw IS NULL" in captured["cypher"]


def test_fetch_pending_no_filter_when_force(monkeypatch):
    captured = {}

    def _cap(cypher, **kwargs):
        captured["cypher"] = cypher
        return []

    monkeypatch.setattr(BF, "run_read_query", _cap)
    BF.fetch_pending(None, force=True)
    assert "d.markdown_raw IS NULL" not in captured["cypher"]


def test_write_raw_passes_all_fields(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(BF, "run_query", _capture)
    BF.write_raw("notices/x.jpg", "MD", "[1]")
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["markdown_raw"] == "MD"
    assert captured["params"]["blocks_raw"] == "[1]"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/pipeline/test_backfill_markdown_raw.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backfill_markdown_raw'`.

- [ ] **Step 3: Create the backfill script**

Create `scripts/backfill_markdown_raw.py`:

```python
"""Populate Document.markdown_raw + blocks_raw from the on-disk MinerU cache.

Durability backfill: copies the raw full.md and content_list.json that the
pipeline already cached on disk onto each Document, so a reviewer edit can never
lose the original MinerU output. Free — reads the cache, no MinerU calls. See
docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.

Usage::

    python -m scripts.backfill_markdown_raw                 # only docs missing raw
    python -m scripts.backfill_markdown_raw --force         # overwrite existing
    python -m scripts.backfill_markdown_raw --limit 50
    python -m scripts.backfill_markdown_raw --dry-run

Idempotent: skips Documents that already have markdown_raw unless --force.
"""
from __future__ import annotations

import argparse
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.load_markdowns_to_neo4j import read_raw_artifacts


def fetch_pending(limit: int | None, force: bool) -> list[dict]:
    cond = "" if force else "AND d.markdown_raw IS NULL"
    cypher = f"""
        MATCH (d:Document)
        WHERE d.file_path IS NOT NULL AND d.file_path <> ''
          {cond}
        RETURN d.filename  AS filename,
               d.file_path AS file_path
        ORDER BY d.markdown_loaded_at DESC
    """
    rows = run_read_query(cypher, max_rows=20_000)
    return rows[:limit] if limit else rows


def write_raw(file_path: str, markdown_raw: str, blocks_raw: str | None) -> None:
    run_query(
        """
        MATCH (d:Document {file_path: $file_path})
        SET d.markdown_raw    = $markdown_raw,
            d.blocks_raw      = coalesce($blocks_raw, d.blocks_raw),
            d.markdown_raw_at = datetime()
        """,
        {"file_path": file_path, "markdown_raw": markdown_raw,
         "blocks_raw": blocks_raw},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite Documents that already have markdown_raw")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N documents")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pending = fetch_pending(args.limit, args.force)
    print(f"{len(pending)} Documents to consider")

    wrote = 0
    no_cache = 0
    failed = 0
    for i, row in enumerate(pending, 1):
        fp = row["file_path"]
        md_raw, bl_raw = read_raw_artifacts(fp)
        if md_raw is None:
            no_cache += 1
            continue
        if args.dry_run:
            blen = "-" if bl_raw is None else len(bl_raw)
            print(f"  [{i}] DRY {row['filename']}: md={len(md_raw)}B blocks={blen}B")
            wrote += 1
            continue
        try:
            write_raw(fp, md_raw, bl_raw)
            wrote += 1
            if i % 200 == 0:
                print(f"  [{i}/{len(pending)}] wrote={wrote} no_cache={no_cache}")
        except Exception as e:
            failed += 1
            print(f"  [{i}] write-fail {row['filename']}: {e}")

    print(f"\nDone. wrote={wrote}  no_cache={no_cache}  failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/pipeline/test_backfill_markdown_raw.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_markdown_raw.py tests/pipeline/test_backfill_markdown_raw.py
git commit -m "feat(scripts): backfill_markdown_raw for durable raw output" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run every test touched/added by this plan**

Run:
```bash
python -m pytest tests/pipeline/test_markdown_raw_capture.py tests/pipeline/test_backfill_markdown_raw.py tests/api/test_review_raw_capture.py -v
```
Expected: PASS (10 passed total).

- [ ] **Step 2: Run the existing review + pipeline suites to confirm no regression**

Run:
```bash
python -m pytest tests/api tests/pipeline -q
```
Expected: all green. If `tests/api` requires extra service env not present locally, narrow to the review/markdown tests:
```bash
python -m pytest tests/api/test_review_replace_blocks.py tests/api/test_review_rotation.py tests/pipeline -q
```

- [ ] **Step 3: (Optional, needs DB access) Dry-run the backfill**

Run: `python -m scripts.backfill_markdown_raw --dry-run --limit 5`
Expected: prints up to 5 `DRY …: md=<N>B blocks=<N>B` lines and a `Done. wrote=… no_cache=…` summary. No writes. Skip this step if the machine has no Neo4j connection — the unit tests are the gate.

- [ ] **Step 4: Final commit if anything was adjusted during verification**

```bash
git add -A
git commit -m "test: verify durable raw MinerU capture end-to-end" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
(Skip if Steps 1–3 produced no file changes.)

---

## Post-merge operational note (not a code task)

After this lands and a deploy carries the new write paths, run the backfill once against production to capture raw for the existing corpus before the disk cache ages out:

```bash
python -m scripts.backfill_markdown_raw          # then re-run with --force only if needed
```

Going-forward capture needs no migration — Neo4j is schemaless and the new properties appear on the next OCR run.

## Self-Review (completed by plan author)

- **Spec coverage:** Data model (3 props) → Tasks 2/3/4 write all three. Capture-on-OCR invariant → Task 2 (loader) + Task 3 (reingest). "Never on edits" → Task 3 `test_save_doc_never_writes_raw`. Hot-query hygiene → Task 3 `test_load_doc_query_excludes_raw_fields`. Backfill → Task 4. Crop/rotation refresh semantics → documented in `_persist_reingest_result` docstring. No spec requirement is left without a task.
- **Placeholder scan:** none — every code/test step shows complete content and exact commands.
- **Type/name consistency:** `read_raw_artifacts(file_path) -> (md_raw, bl_raw)` used identically in loader Task 2 and backfill Task 4; `_persist_reingest_result(filename, *, markdown, blocks_json, markdown_raw, blocks_raw)` defined and called with the same keywords; property names `markdown_raw` / `blocks_raw` / `markdown_raw_at` identical across loader, reingest, and backfill Cypher.
- **Regression guards noted honestly:** the two `_save_doc` / `_load_doc` guards in Task 3 pass on current code by design — they lock the invariant against future drift, not red-green TDD.
