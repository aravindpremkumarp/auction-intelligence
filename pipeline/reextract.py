"""
pipeline/reextract.py
---------------------
Per-block re-extraction used by the annotator UI.

Given a Document's public URL + a normalized bbox, crop that region from
the source PDF/image and run a fresh extraction on just that crop:

  * **PDF + row/col guides** -> PyMuPDF cell-text extraction (no API call).
    This is the fastest, cheapest path and gives reviewer-controlled
    structure when they've drawn explicit table guides.
  * **Otherwise** -> render the crop as a 2x PNG and submit it to MinerU
    as a single-file batch. The returned content-list JSON is normalized
    via :func:`pipeline.mineru.parse_mineru_content_list` and merged into
    one block result (largest area, then concatenated text by reading
    order if MinerU returns multiple sub-blocks for the same region).
  * **MinerU failure on PDF crops** -> PyMuPDF ``page.get_text`` over the
    full bbox so a network blip doesn't strand the reviewer.

PyMuPDF and Pillow are deferred imports so importing this module never
fails on hosts where one of them is missing — only the actual call
demands them.
"""
from __future__ import annotations

import io
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from pipeline.mineru import (
    parse_mineru_content_list,
    safe_cache_name,
)


PDF_CONTENT_TYPES = {"application/pdf"}
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/jfif"}


class ReExtractError(RuntimeError):
    pass


def _classify(content_type: str | None, url: str) -> str:
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in PDF_CONTENT_TYPES:
            return "pdf"
        if ct in IMAGE_CONTENT_TYPES or ct.startswith("image/"):
            return "image"
    ext = Path(urlparse(url).path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".jfif"):
        return "image"
    return "unknown"


def _download_source(url: str) -> tuple[bytes, str]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    kind = _classify(r.headers.get("content-type"), url)
    if kind == "unknown":
        raise ReExtractError(f"Unsupported source content-type at {url}")
    return r.content, kind


def _scale_bbox(bbox_norm: list[float], width: float, height: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox_norm
    return (x0 * width, y0 * height, x1 * width, y1 * height)


# ── PDF helpers (PyMuPDF, lazy) ─────────────────────────────────────────────

def _open_pdf(pdf_bytes: bytes):
    import fitz  # type: ignore
    return fitz.open(stream=pdf_bytes, filetype="pdf")


def _pdf_crop_to_png(pdf_bytes: bytes, page_1: int, bbox_norm: list[float]) -> bytes:
    import fitz  # type: ignore
    doc = _open_pdf(pdf_bytes)
    try:
        page = doc[max(0, page_1 - 1)]
        rect = page.rect
        x0, y0, x1, y1 = _scale_bbox(bbox_norm, rect.width, rect.height)
        clip = fitz.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
        return pix.tobytes("png")
    finally:
        doc.close()


def _pdf_get_text(pdf_bytes: bytes, page_1: int, bbox_norm: list[float]) -> str:
    import fitz  # type: ignore
    doc = _open_pdf(pdf_bytes)
    try:
        page = doc[max(0, page_1 - 1)]
        rect = page.rect
        x0, y0, x1, y1 = _scale_bbox(bbox_norm, rect.width, rect.height)
        return page.get_text("text", clip=fitz.Rect(x0, y0, x1, y1)) or ""
    finally:
        doc.close()


def _extract_table_with_guides(
    pdf_bytes: bytes, page_1: int, bbox_norm: list[float],
    row_positions: list[float], col_positions: list[float],
) -> dict:
    """Build an HTML table by reading each cell with PyMuPDF.

    Row/col positions are normalized to the bbox (0..1). We split the
    bbox into a grid using ``[0] + sorted(positions) + [1]`` and read
    each cell's text individually.
    """
    import fitz  # type: ignore
    doc = _open_pdf(pdf_bytes)
    try:
        page = doc[max(0, page_1 - 1)]
        rect = page.rect
        bx0, by0, bx1, by1 = _scale_bbox(bbox_norm, rect.width, rect.height)
        bw, bh = (bx1 - bx0), (by1 - by0)
        col_edges = [0.0] + sorted(set(col_positions or [])) + [1.0]
        row_edges = [0.0] + sorted(set(row_positions or [])) + [1.0]
        rows_html = []
        for ri in range(len(row_edges) - 1):
            cy0 = by0 + row_edges[ri]     * bh
            cy1 = by0 + row_edges[ri + 1] * bh
            cells_html = []
            for ci in range(len(col_edges) - 1):
                cx0 = bx0 + col_edges[ci]     * bw
                cx1 = bx0 + col_edges[ci + 1] * bw
                cell_rect = fitz.Rect(cx0, cy0, cx1, cy1)
                text = (page.get_text("text", clip=cell_rect) or "").strip()
                cells_html.append(f"<td>{_escape_html(text)}</td>")
            rows_html.append("<tr>" + "".join(cells_html) + "</tr>")
        html = "<table>" + "".join(rows_html) + "</table>"
        return {
            "format": "html",
            "content": html,
            "rows": len(row_edges) - 1,
            "cols": len(col_edges) - 1,
        }
    finally:
        doc.close()


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


# ── Image helpers (Pillow, lazy) ────────────────────────────────────────────

def _image_crop_to_png(img_bytes: bytes, bbox_norm: list[float]) -> bytes:
    from PIL import Image  # type: ignore
    src = Image.open(io.BytesIO(img_bytes))
    src.load()
    w, h = src.size
    x0, y0, x1, y1 = _scale_bbox(bbox_norm, w, h)
    crop = src.crop((int(x0), int(y0), int(x1), int(y1)))
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── MinerU single-block call ────────────────────────────────────────────────

def _mineru_one_shot_png(png_bytes: bytes, *, hint_name: str) -> list[dict]:
    """Submit one cropped PNG to MinerU, return the parsed block list."""
    from pipeline.mineru_api import (
        download_and_cache, poll, request_batch, upload_files,
    )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        tmp_path = Path(f.name)

    # Use a unique file_path key so the per-block re-extract doesn't
    # collide with the bulk pipeline's cache for the original document.
    file_path_key = f"reextract/{uuid.uuid4().hex}_{hint_name}.png"
    item = {
        "filename":  Path(file_path_key).name,
        "file_path": file_path_key,
        "disk_path": tmp_path,
    }
    try:
        batch_id, urls = request_batch([item])
        upload_files([item], urls)
        results = poll(batch_id, timeout_s=300)
        if not results or results[0].get("state") != "done":
            err = results[0].get("err_msg") if results else "no rows"
            raise ReExtractError(f"MinerU re-extract failed: {err}")
        zip_url = results[0].get("full_zip_url")
        if not zip_url:
            raise ReExtractError("MinerU response missing full_zip_url")
        md_path, blocks_path = download_and_cache(file_path_key, zip_url)
        if blocks_path is None:
            raise ReExtractError("MinerU re-extract returned no content-list JSON")
        import json as _json
        raw = _json.loads(blocks_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("blocks"), list):
            return raw["blocks"]
        if isinstance(raw, list):
            return parse_mineru_content_list(raw)
        return []
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _merge_mineru_blocks(blocks: list[dict], label: str) -> dict:
    """Collapse MinerU's per-region output into one block result."""
    if not blocks:
        return {"text": "", "table": None, "confidence": None}
    # Prefer the largest block (likely the actual content vs page-number noise).
    def area(b: dict) -> float:
        bbox = b.get("bbox") or [0, 0, 0, 0]
        try:
            return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        except Exception:
            return 0.0
    if label == "Table":
        table_blocks = [b for b in blocks if b.get("label") == "Table"]
        chosen = max(table_blocks or blocks, key=area)
        return {
            "text":       chosen.get("text") or "",
            "table":      chosen.get("table") or {"format": "html"},
            "confidence": chosen.get("confidence"),
        }
    chunks = [b.get("text") or "" for b in
              sorted(blocks, key=lambda b: int(b.get("reading_order", 0)))]
    text = "\n\n".join(c for c in chunks if c.strip())
    confs = [b.get("confidence") for b in blocks
             if isinstance(b.get("confidence"), (int, float))]
    return {
        "text":       text,
        "table":      None,
        "confidence": (sum(confs) / len(confs)) if confs else None,
    }


# ── Public entry point ──────────────────────────────────────────────────────

async def crop_and_reextract(
    public_url: str, page: int, bbox_norm: list[float], label: str,
    row_positions: list[float] | None = None,
    col_positions: list[float] | None = None,
) -> dict[str, Any]:
    """Re-extract a single bbox region.

    Returns ``{"text": str, "table": dict | None, "confidence": float | None}``.
    Caller is responsible for persisting the result on the block.
    """
    import asyncio

    def _run() -> dict[str, Any]:
        src_bytes, kind = _download_source(public_url)

        # PDF + table guides: stay local, skip MinerU.
        if (kind == "pdf"
                and label == "Table"
                and row_positions
                and col_positions):
            table = _extract_table_with_guides(
                src_bytes, page, bbox_norm,
                row_positions=row_positions,
                col_positions=col_positions,
            )
            return {
                "text":       table["content"],
                "table":      table,
                "confidence": None,
            }

        # Render a crop and ship it to MinerU.
        if kind == "pdf":
            png_bytes = _pdf_crop_to_png(src_bytes, page, bbox_norm)
        else:
            png_bytes = _image_crop_to_png(src_bytes, bbox_norm)

        hint = safe_cache_name(Path(urlparse(public_url).path).name or "crop")[:32] or "crop"
        try:
            blocks = _mineru_one_shot_png(png_bytes, hint_name=hint)
            return _merge_mineru_blocks(blocks, label)
        except ReExtractError:
            # Fall back to PyMuPDF text extraction for PDFs.
            if kind == "pdf":
                fallback = _pdf_get_text(src_bytes, page, bbox_norm)
                return {"text": fallback, "table": None, "confidence": None}
            raise

    # Push the blocking work to a thread so we don't hold up the FastAPI loop.
    return await asyncio.to_thread(_run)
