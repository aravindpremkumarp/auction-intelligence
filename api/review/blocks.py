"""
api/review/blocks.py
--------------------
Per-block reviewer operations on a Document.

``Document.blocks`` is a JSON-encoded string with shape::

    {"schema_version": 1, "blocks": [<block>, ...]}

Every mutating call uses a read-modify-write CAS on ``Document.blocks_revision``
so two reviewers can't silently stomp each other. On any block change we
also re-assemble ``Document.markdown`` from the (sorted) block list and
clear the markdown verification flags — same pattern as the classification
flip in :mod:`api.review.queries`. A previously "good" markdown is
re-queued by the next review pass.

The re-extract path crops the source PDF/image to the block's bbox and
calls :mod:`pipeline.reextract`; it does NOT call MinerU here so the
import cost only kicks in when a reviewer presses the button.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from api.neo4j_client import run_query, run_read_query
from pipeline.mineru import (
    DEFAULT_LABEL,
    MINERU_LABEL_VALUES,
    assemble_markdown,
)


class BlocksConflict(RuntimeError):
    """Raised when the optimistic-lock CAS fails (HTTP 409 in the router)."""


class BlocksNotFound(RuntimeError):
    """Raised when the Document or the block id doesn't exist."""


# ── helpers ─────────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return f"blk_{secrets.token_hex(6)}"


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _clean_bbox(raw: Any) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        raise ValueError("bbox must be a 4-element list [x0,y0,x1,y1]")
    x0, y0, x1, y1 = (float(raw[0]), float(raw[1]),
                      float(raw[2]), float(raw[3]))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0, y0, x1, y1 = (_clamp01(x0), _clamp01(y0),
                      _clamp01(x1), _clamp01(y1))
    if x1 - x0 < 0.005:
        x1 = _clamp01(x0 + 0.005)
    if y1 - y0 < 0.005:
        y1 = _clamp01(y0 + 0.005)
    return [x0, y0, x1, y1]


def _validate_label(label: str) -> str:
    if not isinstance(label, str):
        raise ValueError("label must be a string")
    if label not in MINERU_LABEL_VALUES:
        raise ValueError(
            f"label '{label}' is not one of {MINERU_LABEL_VALUES}"
        )
    return label


def _clean_table(table: Any) -> dict | None:
    """Validate optional table grid hints."""
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ValueError("table must be an object or null")
    out: dict = {
        "format":        table.get("format") or "html",
        "rows":          None,
        "cols":          None,
        "row_positions": None,
        "col_positions": None,
    }
    if isinstance(table.get("rows"), int):
        out["rows"] = max(0, table["rows"])
    if isinstance(table.get("cols"), int):
        out["cols"] = max(0, table["cols"])
    for key in ("row_positions", "col_positions"):
        val = table.get(key)
        if isinstance(val, list):
            cleaned = sorted({_clamp01(float(v)) for v in val
                              if isinstance(v, (int, float))})
            out[key] = cleaned or None
    return out


def _empty_doc() -> dict:
    return {"schema_version": 1, "blocks": []}


def _parse_doc_blob(raw: str | None) -> dict:
    if not raw:
        return _empty_doc()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_doc()
    if isinstance(obj, list):
        # Older callers may have stored just the array.
        return {"schema_version": 1, "blocks": obj}
    if isinstance(obj, dict) and isinstance(obj.get("blocks"), list):
        return obj
    return _empty_doc()


# ── core read/write ─────────────────────────────────────────────────────────

def _load_doc(filename: str) -> tuple[dict, int, dict]:
    """Fetch the parsed blocks doc, current revision, and document metadata.

    Returns ``(doc, revision, meta)``. Raises :class:`BlocksNotFound` if
    the Document doesn't exist.
    """
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $filename})
        RETURN d.blocks                       AS blocks_json,
               coalesce(d.blocks_revision, 0) AS rev,
               d.filename                     AS filename,
               d.file_path                    AS file_path,
               d.public_url                   AS public_url,
               d.storage_key                  AS storage_key,
               d.notice_type                  AS notice_type,
               d.markdown                     AS markdown
        """,
        {"filename": filename},
        max_rows=1,
    )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    r = rows[0]
    doc = _parse_doc_blob(r.get("blocks_json"))
    meta = {
        "filename":    r.get("filename"),
        "file_path":   r.get("file_path"),
        "public_url":  r.get("public_url"),
        "storage_key": r.get("storage_key"),
        "notice_type": r.get("notice_type"),
        "markdown":    r.get("markdown"),
    }
    return doc, int(r.get("rev") or 0), meta


def _save_doc(filename: str, doc: dict, expected_rev: int) -> int:
    """Optimistic-lock write. Returns the new revision number.

    Raises :class:`BlocksConflict` when the revision moved under our feet,
    so the caller can re-read and re-apply.
    """
    blocks_json = json.dumps(doc, ensure_ascii=False)
    new_md = assemble_markdown(doc.get("blocks") or [])
    rows = run_query(
        """
        MATCH (d:Document {filename: $filename})
        WHERE coalesce(d.blocks_revision, 0) = $expected_rev
        SET d.blocks               = $blocks_json,
            d.markdown             = $markdown,
            d.blocks_revision      = $expected_rev + 1,
            d.markdown_loaded_at   = datetime(),
            d.markdown_verified_at = NULL,
            d.markdown_verified_by = NULL,
            d.markdown_quality     = NULL
        RETURN d.blocks_revision AS rev
        """,
        {
            "filename":     filename,
            "expected_rev": int(expected_rev),
            "blocks_json":  blocks_json,
            "markdown":     new_md,
        },
    )
    if not rows:
        raise BlocksConflict("blocks_revision changed; reload required")
    return int(rows[0]["rev"])


def _max_reading_order(blocks: list[dict]) -> int:
    if not blocks:
        return 0
    return max(int(b.get("reading_order", 0)) for b in blocks)


# ── public API used by the router ───────────────────────────────────────────

def get_blocks(filename: str) -> dict:
    """Return the full annotator payload for a notice."""
    doc, rev, meta = _load_doc(filename)
    return {
        **meta,
        "schema_version":    int(doc.get("schema_version") or 1),
        "source_dims":       doc.get("source_dims") or [],
        "blocks":            doc.get("blocks") or [],
        "blocks_revision":   rev,
        "backfill_required": not bool(doc.get("blocks")),
    }


def create_block(filename: str, body: dict, by_email: str) -> dict:
    """Append a new block (marquee-drawn). Server assigns id + reading_order."""
    doc, rev, _ = _load_doc(filename)
    label = _validate_label(body.get("label") or DEFAULT_LABEL)
    bbox  = _clean_bbox(body.get("bbox"))
    page  = int(body.get("page") or 1)
    text  = body.get("text") or ""
    blocks = doc["blocks"]
    reading_order = body.get("reading_order")
    reading_order = (int(reading_order) if isinstance(reading_order, int)
                     else _max_reading_order(blocks) + 1)
    new_block = {
        "id":            _new_id(),
        "page":          max(1, page),
        "bbox":          bbox,
        "label":         label,
        "text":          str(text),
        "reading_order": reading_order,
        "source":        "human",
        "confidence":    None,
        "table":         _clean_table(body.get("table")) if label == "Table"
                         else None,
        "edited_at":     _iso_now(),
        "edited_by":     by_email,
    }
    blocks.append(new_block)
    doc["blocks"] = blocks
    _save_doc(filename, doc, rev)
    return new_block


def update_block(filename: str, block_id: str, patch: dict, by_email: str) -> dict:
    """Apply a partial update to one block."""
    doc, rev, _ = _load_doc(filename)
    blocks = doc["blocks"]
    blk = next((b for b in blocks if b.get("id") == block_id), None)
    if blk is None:
        raise BlocksNotFound(f"block not found: {block_id}")
    if "bbox" in patch and patch["bbox"] is not None:
        blk["bbox"] = _clean_bbox(patch["bbox"])
    if "label" in patch and patch["label"] is not None:
        blk["label"] = _validate_label(patch["label"])
        if blk["label"] == "Table" and not blk.get("table"):
            blk["table"] = {"format": "html", "rows": None, "cols": None,
                            "row_positions": None, "col_positions": None}
        elif blk["label"] != "Table":
            blk["table"] = None
    if "text" in patch and patch["text"] is not None:
        blk["text"] = str(patch["text"])
        blk["source"] = "human"
    if "reading_order" in patch and patch["reading_order"] is not None:
        blk["reading_order"] = int(patch["reading_order"])
    if "table" in patch:
        blk["table"] = _clean_table(patch["table"])
        if blk["table"] is not None:
            blk["label"] = "Table"
    blk["edited_at"] = _iso_now()
    blk["edited_by"] = by_email
    _save_doc(filename, doc, rev)
    return blk


def delete_block(filename: str, block_id: str) -> bool:
    doc, rev, _ = _load_doc(filename)
    blocks = doc["blocks"]
    new_blocks = [b for b in blocks if b.get("id") != block_id]
    if len(new_blocks) == len(blocks):
        raise BlocksNotFound(f"block not found: {block_id}")
    doc["blocks"] = new_blocks
    _save_doc(filename, doc, rev)
    return True


def reorder_blocks(filename: str, order: list[dict], by_email: str) -> dict:
    """Bulk update of ``reading_order``. ``order`` is a list of
    ``{id, reading_order}`` dicts."""
    if not isinstance(order, list):
        raise ValueError("order must be a list")
    doc, rev, _ = _load_doc(filename)
    by_id = {b.get("id"): b for b in doc["blocks"]}
    ts = _iso_now()
    touched = 0
    for item in order:
        if not isinstance(item, dict):
            continue
        bid = item.get("id")
        ro  = item.get("reading_order")
        b = by_id.get(bid)
        if b is None or not isinstance(ro, int):
            continue
        if b.get("reading_order") != ro:
            b["reading_order"] = int(ro)
            b["edited_at"] = ts
            b["edited_by"] = by_email
            touched += 1
    if touched == 0:
        return get_blocks(filename)
    _save_doc(filename, doc, rev)
    return get_blocks(filename)


async def re_extract_block(filename: str, block_id: str,
                           body: dict, by_email: str) -> dict:
    """Re-run extraction on a single block via the MinerU crop pipeline.

    Lazy-imports :mod:`pipeline.reextract` so the heavy deps (PyMuPDF,
    Pillow) are only loaded when this endpoint is actually used.
    """
    doc, rev, meta = _load_doc(filename)
    blk = next((b for b in doc["blocks"] if b.get("id") == block_id), None)
    if blk is None:
        raise BlocksNotFound(f"block not found: {block_id}")

    bbox  = _clean_bbox(body.get("bbox") or blk["bbox"])
    label = _validate_label(body.get("label") or blk["label"])
    row_positions = body.get("row_positions") or (
        (blk.get("table") or {}).get("row_positions") if blk.get("table") else None
    )
    col_positions = body.get("col_positions") or (
        (blk.get("table") or {}).get("col_positions") if blk.get("table") else None
    )
    page = int(body.get("page") or blk.get("page") or 1)

    if not meta.get("public_url"):
        raise BlocksNotFound("Document has no public_url; cannot crop source")

    # Lazy import — keeps cold-start fast for the rest of the review UI.
    from pipeline.reextract import crop_and_reextract
    result = await crop_and_reextract(
        public_url=meta["public_url"],
        page=page,
        bbox_norm=bbox,
        label=label,
        row_positions=row_positions,
        col_positions=col_positions,
    )

    # Refresh after the (potentially long) network call to minimize the
    # window where a concurrent edit could collide.
    doc, rev, _ = _load_doc(filename)
    blk = next((b for b in doc["blocks"] if b.get("id") == block_id), None)
    if blk is None:
        raise BlocksNotFound(f"block not found: {block_id}")

    blk["bbox"]   = bbox
    blk["label"]  = label
    blk["text"]   = result.get("text") or ""
    blk["source"] = "mineru"
    if result.get("confidence") is not None:
        blk["confidence"] = float(result["confidence"])
    if label == "Table":
        existing = blk.get("table") or {}
        out_table = result.get("table") or {}
        blk["table"] = {
            "format":        out_table.get("format") or existing.get("format") or "html",
            "rows":          out_table.get("rows") or existing.get("rows"),
            "cols":          out_table.get("cols") or existing.get("cols"),
            "row_positions": row_positions,
            "col_positions": col_positions,
        }
    else:
        blk["table"] = None
    blk["edited_at"] = _iso_now()
    blk["edited_by"] = by_email
    _save_doc(filename, doc, rev)
    return blk


def reingest_notice_safe(filename: str, by_email: str) -> None:
    """Background-task wrapper around :func:`reingest_notice`.

    Exceptions are logged and swallowed — the foreground request has
    already returned 202, so a thrown error here can't be surfaced via
    HTTP. The frontend detects success by polling ``GET .../blocks``
    and watching ``blocks_revision``; if the rev doesn't advance, the
    reviewer can simply press Re-ingest again.
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        reingest_notice(filename, by_email)
        log.info("reingest succeeded for %s", filename)
    except Exception:
        log.exception("reingest background task failed for %s", filename)


def reingest_notice(filename: str, by_email: str) -> dict:
    """Re-run the full MinerU pipeline for a single Document.

    Used for backfilling notices that predate the per-block JSON cache.
    On a local dev machine the source file is usually on disk under
    ``downloads/``. On the production API host it isn't — the canonical
    source is the R2 ``public_url`` — so this falls back to streaming the
    R2 bytes to a tempfile before handing them to MinerU. Lazy-imports
    the bulk pipeline helpers so the import only fires when the reviewer
    asks for it.
    """
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $filename})
        RETURN d.file_path  AS file_path,
               d.filename   AS filename,
               d.public_url AS public_url
        """,
        {"filename": filename},
        max_rows=1,
    )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    fp = rows[0]["file_path"]
    src_filename = rows[0]["filename"]
    public_url = rows[0].get("public_url")

    from pipeline.mineru import MINERU_SUPPORTED_EXTS, find_disk_path
    from pipeline.mineru_api import (
        request_batch, upload_files, poll, download_and_cache,
    )

    import os
    import tempfile
    from pathlib import Path

    disk = find_disk_path(src_filename)
    tmp_path: Path | None = None
    if disk is None:
        # Fall back to R2 — the canonical source on the production host.
        if not public_url:
            raise BlocksNotFound(
                f"source file not on disk and no public_url: {src_filename}"
            )
        ext = Path(src_filename).suffix.lower() or ".bin"
        if ext not in MINERU_SUPPORTED_EXTS:
            raise ValueError(f"unsupported source extension: {ext}")
        import requests
        try:
            resp = requests.get(public_url, timeout=120, stream=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"failed to fetch source from R2: {e}")
        fd, tmp_name = tempfile.mkstemp(suffix=ext, prefix="reingest_")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        out.write(chunk)
        finally:
            resp.close()
        disk = tmp_path

    try:
        item = {
            "filename":  src_filename,
            "file_path": fp,
            "disk_path": disk,
        }
        batch_id, urls = request_batch([item])
        upload_files([item], urls)
        results = poll(batch_id)
        if not results or results[0].get("state") != "done":
            err = (results[0].get("err_msg") if results else "no rows")
            raise RuntimeError(f"MinerU reingest failed: {err}")
        zip_url = results[0].get("full_zip_url")
        md_path, blocks_path = download_and_cache(fp, zip_url)
        if md_path is None:
            raise RuntimeError("reingest succeeded but no markdown was produced")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    # Reuse the loader's parsing so we share one code path.
    from pipeline.load_markdowns_to_neo4j import load_blocks_for
    blocks = load_blocks_for(fp) or []

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
    return get_blocks(filename)
