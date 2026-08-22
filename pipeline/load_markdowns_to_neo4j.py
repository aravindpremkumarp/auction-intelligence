"""
pipeline/load_markdowns_to_neo4j.py
-----------------------------------
Load every cached MinerU markdown into the corresponding :Document.markdown
property in Neo4j.

Complements pipeline/embed_markdowns.py (which writes the 3072-dim vector).
Storing the raw markdown on the Document node lets the agent and graph
queries surface the full notice text — bank, borrowers, schedule, terms —
not just retrieve Documents by similarity.

The on-disk cache at pipeline/cache/mineru_markdown/ remains the canonical
artifact for re-running OCR; the Neo4j copy is a derived projection meant
for serving and ad-hoc Cypher queries.

Each markdown write also stamps minimal provenance — markdown_source,
markdown_model, markdown_loaded_at — so downstream reviewers can tell
which OCR backend produced the text. Pass --backfill-only to stamp the
provenance fields on every Document that already has markdown but no
provenance recorded (one-shot use after this change ships).

Run:  python -m pipeline.load_markdowns_to_neo4j

Safe to re-run. Documents that already have d.markdown set are skipped
unless --force is passed. Pure local-file + Neo4j-write — no API calls,
finishes in seconds.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

from api.neo4j_client import run_query, run_read_query
from pipeline.mineru import (
    MINERU_BLOCKS_DIR,
    PRECLEAN_MODEL_TAG,
    is_precleaned,
    parse_mineru_content_list,
    read_mineru_meta,
    safe_cache_name,
)
REPO_ROOT = Path(__file__).resolve().parent.parent
MD_DIR = REPO_ROOT / "pipeline" / "cache" / "mineru_markdown"
BLOCKS_DIR = MINERU_BLOCKS_DIR

# MinerU is the only producer of cached markdowns under MD_DIR. The model
# tag mirrors MinerU's "model_version: vlm" request param in
# scripts/ocr_with_mineru.py:107, so future backends can drop in a
# different value via --markdown-model.
DEFAULT_MARKDOWN_SOURCE = "mineru"
DEFAULT_MARKDOWN_MODEL = "mineru-vlm"


def safe_name(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


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
            # blocks_raw is verbatim JSON meant to be re-parsed; decode strictly
            # so a bad byte yields None rather than a silently corrupted blob.
            bl_raw = bl_p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            bl_raw = None
    return md_raw, bl_raw


def _new_block_id() -> str:
    return f"blk_{secrets.token_hex(6)}"


def _assign_block_ids(blocks: list[dict]) -> list[dict]:
    """Stamp a stable random id on each block. The parser leaves id blank."""
    for b in blocks:
        if not b.get("id"):
            b["id"] = _new_block_id()
    return blocks


def load_blocks_for(file_path: str,
                    img_map: dict[str, str] | None = None) -> list[dict] | None:
    """Read the cached content-list JSON for ``file_path`` and convert it
    to our canonical block shape. Returns ``None`` if no cache exists or
    the file is unreadable.

    ``img_map`` ({image-basename -> R2 URL}) lets each block resolve its
    archived image URL; when omitted it is read from the meta sidecar so
    callers (e.g. reingest) get image URLs without threading the map through.
    """
    p = BLOCKS_DIR / f"{safe_name(file_path)}.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [blocks-parse-fail] {p.name}: {e}")
        return None
    if isinstance(raw, dict) and isinstance(raw.get("blocks"), list):
        # Already normalized (e.g. a re-serialize from earlier run).
        return _assign_block_ids(raw["blocks"])
    if isinstance(raw, list):
        if img_map is None:
            img_map = read_mineru_meta(file_path).get("img_map") or {}
        return _assign_block_ids(parse_mineru_content_list(raw, img_map=img_map))
    return None


def read_parse_quality(file_path: str) -> float | None:
    """Datalab's own parse-quality score (0–5) from the cached blocks sidecar.

    ``pipeline.datalab_api.run_and_cache`` stores it next to the blocks; the
    MinerU path writes a bare list and has no equivalent signal, so this
    returns ``None`` there (and for any pre-existing sidecar written before
    the field was added). ``None`` never overwrites a stored score.
    """
    p = BLOCKS_DIR / f"{safe_name(file_path)}.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    v = raw.get("parse_quality_score")
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
            RETURN d.file_path AS file_path
        """
    else:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
              AND d.markdown IS NULL
            RETURN d.file_path AS file_path
        """
    return run_read_query(cypher, max_rows=20_000)


def write_markdowns(rows: list[dict], source: str, model: str) -> None:
    """Write markdown + (optional) blocks JSON to each Document.

    ``rows`` items carry: ``file_path``, ``markdown``, optional
    ``blocks_json`` (string, JSON-encoded ``{"schema_version":1,"blocks":[...]}``),
    and optional ``model`` (per-row override of the default ``$model``
    parameter — used when stage1 pre-cleaned the source so the tag
    reflects what produced this row, not the batch-wide default).
    Documents without a blocks payload keep their existing ``d.blocks``
    (or ``NULL``) — we never clobber blocks the reviewer may have edited.
    Optional ``mineru_zip_url`` stamps the archived MinerU zip URL (and
    ``mineru_zip_at``); absent/None leaves any existing value untouched.
    Optional ``parse_quality_score`` (Datalab only) is stamped the same way —
    a row without one keeps whatever score the Document already carries, so a
    MinerU re-load never wipes a Datalab verdict.
    """
    cypher = """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown            = row.markdown,
            d.parse_quality_score = coalesce(row.parse_quality_score,
                                             d.parse_quality_score),
            d.parse_quality_at    = CASE WHEN row.parse_quality_score IS NULL
                                        THEN d.parse_quality_at ELSE datetime() END,
            d.markdown_source     = $source,
            d.markdown_model      = coalesce(row.model, $model),
            d.markdown_loaded_at  = datetime(),
            d.markdown_raw        = coalesce(row.markdown_raw, d.markdown_raw),
            d.blocks_raw          = coalesce(row.blocks_raw, d.blocks_raw),
            d.markdown_raw_at     = CASE WHEN row.markdown_raw IS NULL
                                        THEN d.markdown_raw_at ELSE datetime() END,
            d.mineru_zip_url      = coalesce(row.mineru_zip_url, d.mineru_zip_url),
            d.mineru_zip_at       = CASE WHEN row.mineru_zip_url IS NULL
                                        THEN d.mineru_zip_at ELSE datetime() END,
            d.blocks              = CASE
                WHEN row.blocks_json IS NULL THEN d.blocks
                ELSE row.blocks_json END,
            d.blocks_revision     = CASE
                WHEN row.blocks_json IS NULL THEN coalesce(d.blocks_revision, 0)
                ELSE coalesce(d.blocks_revision, 0) END
    """
    run_query(cypher, {"rows": rows, "source": source, "model": model})


def backfill_provenance(source: str, model: str) -> int:
    """Stamp provenance on every Document that already has markdown but no
    markdown_source recorded. One-shot use after this change first ships.
    Returns the number of Documents stamped.
    """
    cypher = """
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''
          AND d.markdown_source IS NULL
        SET d.markdown_source     = $source,
            d.markdown_model      = $model,
            d.markdown_loaded_at  = coalesce(d.markdown_loaded_at, datetime())
        RETURN count(d) AS n
    """
    rows = run_query(cypher, {"source": source, "model": model})
    return int(rows[0]["n"]) if rows else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite even if d.markdown is already set")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (staged rollout)")
    ap.add_argument("--write-batch", type=int, default=200,
                    help="rows per Neo4j UNWIND write")
    ap.add_argument("--markdown-source", default=DEFAULT_MARKDOWN_SOURCE,
                    help="value to stamp as d.markdown_source")
    ap.add_argument("--markdown-model", default=DEFAULT_MARKDOWN_MODEL,
                    help="value to stamp as d.markdown_model")
    ap.add_argument("--backfill-only", action="store_true",
                    help="skip the load step; only stamp provenance on "
                         "existing Documents that already have markdown")
    args = ap.parse_args()

    if args.backfill_only:
        stamped = backfill_provenance(args.markdown_source, args.markdown_model)
        print(f"Backfilled provenance on {stamped} Documents "
              f"(source={args.markdown_source}, model={args.markdown_model})")
        return 0

    pending = fetch_pending(args.force)
    if args.limit:
        pending = pending[:args.limit]
    total = len(pending)

    if total == 0:
        print("nothing to load")
        stamped = backfill_provenance(args.markdown_source, args.markdown_model)
        if stamped:
            print(f"Stamped provenance on {stamped} pre-existing Documents")
        return 0

    print(f"Loading markdown for {total} Documents from {MD_DIR}")
    print(f"(source={args.markdown_source}, model={args.markdown_model})")
    print("(re-runs skip Documents that already have d.markdown; pass "
          "--force to overwrite)")

    payloads: list[dict] = []
    done = 0
    missing = 0
    failed = 0
    blocks_loaded = 0
    blocks_missing = 0

    written_file_paths: list[str] = []

    def safe_write(rows: list[dict]) -> bool:
        for attempt in range(3):
            try:
                write_markdowns(rows, args.markdown_source, args.markdown_model)
                written_file_paths.extend(r["file_path"] for r in rows)
                return True
            except Exception as e:
                if attempt < 2:
                    print(f"  [neo4j retry {attempt + 1}] {type(e).__name__}: {e}")
                    time.sleep(2)
                else:
                    print(f"  [neo4j FAIL] {e}")
                    return False
        return False

    t0 = time.time()
    for r in pending:
        fp = r["file_path"]
        md_path = MD_DIR / f"{safe_name(fp)}.md"
        if not md_path.exists():
            missing += 1
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  [read-fail] {md_path.name}: {e}")
            failed += 1
            continue
        if not text.strip():
            missing += 1
            continue

        # One meta read per doc: feeds both the block image URLs and the
        # Document-level archived zip URL.
        meta = read_mineru_meta(fp)
        blocks = load_blocks_for(fp, img_map=meta.get("img_map") or {})
        if blocks is not None:
            blocks_json = json.dumps(
                {"schema_version": 1, "blocks": blocks},
                ensure_ascii=False,
            )
            blocks_loaded += 1
        else:
            blocks_json = None
            blocks_missing += 1

        # markdown_raw == text (the raw full.md already read above); only
        # blocks_raw is new here, so discard the helper's first return value.
        _, blocks_raw = read_raw_artifacts(fp)
        payloads.append({
            "file_path":      fp,
            "markdown":       text,
            "markdown_raw":   text,
            "blocks_raw":     blocks_raw,
            "blocks_json":    blocks_json,
            "model":          PRECLEAN_MODEL_TAG if is_precleaned(fp) else None,
            "mineru_zip_url": meta.get("zip_url"),
            "parse_quality_score": read_parse_quality(fp),
        })
        done += 1

        if len(payloads) >= args.write_batch:
            if safe_write(payloads):
                payloads = []

        if done % 500 == 0 or done == total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{total}]  rate={rate:.0f}/s  "
                  f"(missing_md={missing}, failed={failed})", flush=True)

    if payloads:
        safe_write(payloads)

    # Catch any Documents that already had markdown set but no provenance
    # (e.g. from a previous loader run before this change shipped).
    stamped = backfill_provenance(args.markdown_source, args.markdown_model)
    backfill_note = f"  backfilled_existing={stamped}" if stamped else ""

    scored = 0
    if written_file_paths:
        try:
            from pipeline.score_markdown import score_freshly_loaded
            scored = score_freshly_loaded(written_file_paths)
        except Exception as e:
            print(f"  [score-fail] {type(e).__name__}: {e}")
        try:
            from pipeline.ocr_health import score_freshly_loaded as score_health
            score_health(written_file_paths)
        except Exception as e:
            print(f"  [health-fail] {type(e).__name__}: {e}")

    print(f"\nLoaded {done} markdowns  missing_md={missing}  failed={failed}"
          f"  blocks_loaded={blocks_loaded}  blocks_missing={blocks_missing}"
          f"  scored={scored}{backfill_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
