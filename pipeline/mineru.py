"""Shared MinerU helpers.

MinerU (https://mineru.net) converts notice PDFs/images to layout-aware
markdown. The pipeline caches that markdown under
``pipeline/cache/mineru_markdown/<safe_cache_name(file_path)>.md`` so it can
be reused across stages (OCR extraction, description generation).

In addition to the markdown, MinerU's result zip ships a per-block JSON
(typically ``<basename>_content_list.json``) with bounding boxes, block
types, and reading order. We cache that JSON next to the markdown under
``pipeline/cache/mineru_blocks/`` so the annotator UI can show, edit, and
re-extract individual blocks without re-calling the OCR API.

This module exposes the helpers that more than one script needs. The
MinerU HTTP client lives in ``pipeline/mineru_api.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.config import DOWNLOADS_DIR, PIPELINE_DIR


MINERU_MARKDOWN_DIR = PIPELINE_DIR / "cache" / "mineru_markdown"
MINERU_BLOCKS_DIR   = PIPELINE_DIR / "cache" / "mineru_blocks"
MINERU_RAW_ZIPS_DIR = PIPELINE_DIR / "cache" / "mineru_raw_zips"

MINERU_SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif"}
MINERU_EXT_REMAP = {".jfif": ".jpg"}


# MinerU's content-list JSON uses lowercase snake-case block types. Map
# them to a stable canonical set used everywhere downstream (UI dropdown,
# label-color map, validation). Unknown values fall back to "Text" so an
# unfamiliar block type never blocks ingestion.
MINERU_LABEL_MAP: dict[str, str] = {
    "text":               "Text",
    "title":              "Title",
    "table":              "Table",
    "table_caption":      "TableCaption",
    "table_footnote":     "TableFootnote",
    "image":              "Image",
    "image_caption":      "ImageCaption",
    "image_footnote":     "ImageFootnote",
    "figure":             "Image",
    "figure_caption":     "ImageCaption",
    "equation":           "Equation",
    "interline_equation": "Equation",
    "inline_equation":    "Equation",
    "code":               "Code",
    "list":               "List",
    "header":             "Header",
    "footer":             "Footer",
    "page_number":        "PageNumber",
    "page_footnote":      "Footnote",
    "footnote":           "Footnote",
    "reference":          "Reference",
    "discarded":          "Discarded",
    "abandon":            "Discarded",
}

MINERU_LABEL_VALUES: list[str] = sorted(set(MINERU_LABEL_MAP.values()))
DEFAULT_LABEL = "Text"


# MinerU content-list JSON filename patterns we try, in order of preference.
# Different MinerU versions / models have used different names; we accept
# any of these to be robust across API revisions.
MINERU_BLOCKS_FILENAME_SUFFIXES = (
    "_content_list.json",   # most common (vlm + pipeline models)
    "content_list.json",
    "middle.json",
    "layout.json",
)


def safe_cache_name(path: str) -> str:
    """Normalize a path/key into a safe single-segment filename."""
    return path.replace("/", "_").replace("\\", "_").replace(":", "_")


def find_disk_path(filename: str, downloads_dir: Path = DOWNLOADS_DIR) -> Path | None:
    """Resolve a Document filename to a concrete file on disk.

    Tries ``downloads/tn_properties/`` first (current scraper layout) and
    falls back to ``downloads/``. Returns ``None`` if neither exists.
    """
    for base in (downloads_dir / "tn_properties", downloads_dir):
        p = base / filename
        if p.exists():
            return p
    return None


def cached_markdown_for_file_path(file_path: str) -> str | None:
    """Read markdown for a Document.file_path value if it has been cached."""
    p = MINERU_MARKDOWN_DIR / f"{safe_cache_name(file_path)}.md"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def cached_markdown_for_filename(filename: str) -> str | None:
    """Look up cached MinerU markdown when only the filename is known.

    Document.file_path historically carried mixed values (bare filename,
    relative ``tn_properties/...``, absolute paths) so the cache key cannot
    be derived from the filename alone. We try the common variants first,
    then fall back to scanning the cache directory for an entry whose
    safe-name ends in the safe form of the filename.
    """
    safe_filename = safe_cache_name(filename)
    candidates = [
        filename,
        f"tn_properties/{filename}",
        f"downloads/tn_properties/{filename}",
        f"downloads/{filename}",
    ]
    for cand in candidates:
        md = cached_markdown_for_file_path(cand)
        if md is not None:
            return md

    if not MINERU_MARKDOWN_DIR.exists():
        return None
    suffix = f"_{safe_filename}.md"
    matches = [p for p in MINERU_MARKDOWN_DIR.iterdir()
               if p.name == f"{safe_filename}.md" or p.name.endswith(suffix)]
    if len(matches) == 1:
        try:
            return matches[0].read_text(encoding="utf-8")
        except OSError:
            return None
    return None


# ── Per-block parsing & assembly ────────────────────────────────────────────

def _clamp01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return float(v)


def _normalize_bbox(raw: list, scale: float) -> list[float]:
    """Convert a MinerU bbox to normalized [x0,y0,x1,y1] in 0..1.

    MinerU's content-list JSON emits bboxes on a 0..1000 integer scale
    (per page). We divide by ``scale`` (default 1000) and clamp. Some
    rebuilds emit [page_width, page_height]-scale floats already — if any
    coord is > scale we assume the latter and skip the division.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return [0.0, 0.0, 0.005, 0.005]
    x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) > 2.0:
        x0 /= scale; y0 /= scale; x1 /= scale; y1 /= scale
    # ensure x0<x1, y0<y1 with min side
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0, y0, x1, y1 = _clamp01(x0), _clamp01(y0), _clamp01(x1), _clamp01(y1)
    if x1 - x0 < 0.005:
        x1 = _clamp01(x0 + 0.005)
    if y1 - y0 < 0.005:
        y1 = _clamp01(y0 + 0.005)
    return [x0, y0, x1, y1]


def _canon_label(raw: str | None) -> str:
    if not raw:
        return DEFAULT_LABEL
    return MINERU_LABEL_MAP.get(str(raw).strip().lower(), DEFAULT_LABEL)


def _block_text(entry: dict, label: str) -> str:
    """Pick the best text representation from a MinerU content-list entry."""
    if label == "Table":
        # MinerU tables ship as HTML in `table_body` (or `html`).
        return (entry.get("table_body")
                or entry.get("table_html")
                or entry.get("html")
                or entry.get("text")
                or "").strip()
    if label == "List":
        items = entry.get("list_items") or entry.get("items")
        if isinstance(items, list) and items:
            return "\n".join(f"- {str(it).strip()}" for it in items)
    if label == "Equation":
        return (entry.get("text")
                or entry.get("latex")
                or entry.get("equation")
                or "").strip()
    if label == "Image":
        # Prefer caption / alt-text; we never inline the raw image.
        return (entry.get("img_caption")
                or entry.get("caption")
                or entry.get("alt")
                or "").strip()
    return str(entry.get("text") or entry.get("md") or "").strip()


def parse_mineru_content_list(raw: list, *, scale: float = 1000.0) -> list[dict]:
    """Convert MinerU's flat content-list JSON to our canonical block shape.

    Output items:
      ``{id, page, bbox, label, text, reading_order, source, confidence,
         table, edited_at, edited_by}``

    ``id`` is left empty here; the loader assigns a stable id when writing
    to Neo4j so the same block keeps the same id across re-loads.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        label = _canon_label(entry.get("type") or entry.get("category"))
        page = int(entry.get("page_idx", entry.get("page", 0))) + 1   # 1-indexed
        bbox = _normalize_bbox(entry.get("bbox") or entry.get("poly_bbox") or [], scale)
        conf = entry.get("score") or entry.get("confidence")
        block: dict = {
            "id": "",
            "page": page,
            "bbox": bbox,
            "label": label,
            "text": _block_text(entry, label),
            "reading_order": page * 1000 + i,
            "source": "mineru",
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "table": None,
            "edited_at": None,
            "edited_by": None,
        }
        if label == "Table":
            block["table"] = {
                "format": "html",
                "rows": entry.get("rows") if isinstance(entry.get("rows"), int) else None,
                "cols": entry.get("cols") if isinstance(entry.get("cols"), int) else None,
                "row_positions": None,
                "col_positions": None,
            }
        out.append(block)
    return out


def assemble_markdown(blocks: list[dict]) -> str:
    """Reconstruct the document markdown from the canonical block list.

    Sorted by ``reading_order``. Discarded / page-number blocks are dropped
    so they don't pollute the reviewer-facing text. Tables (HTML) and other
    HTML pass through as-is — ``marked@12`` accepts HTML inside markdown.
    """
    if not blocks:
        return ""
    skip = {"Discarded", "PageNumber"}
    ordered = sorted(
        (b for b in blocks if b.get("label") not in skip),
        key=lambda b: (int(b.get("reading_order", 0)), int(b.get("page", 0))),
    )
    chunks: list[str] = []
    for b in ordered:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        label = b.get("label") or "Text"
        if label == "Title":
            # MinerU strips the leading "#"; preserve the heading marker so
            # rendered markdown keeps the original visual hierarchy.
            if not text.lstrip().startswith("#"):
                text = "# " + text
        chunks.append(text)
    return "\n\n".join(chunks)


def cached_blocks_for_file_path(file_path: str) -> list[dict] | None:
    """Read parsed-blocks JSON for a Document.file_path if cached on disk."""
    p = MINERU_BLOCKS_DIR / f"{safe_cache_name(file_path)}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("blocks"), list):
        return data["blocks"]
    return None
