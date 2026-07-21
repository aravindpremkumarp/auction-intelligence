"""Datalab (Marker) content parser.

Datalab's hosted convert API (``output_format=json``) returns a nested block
*tree* rather than MinerU's flat content-list. This module flattens that tree
into the **same canonical block shape** the pipeline already uses for MinerU
(see :func:`pipeline.mineru.parse_mineru_content_list`), so Datalab output can
flow through ``assemble_markdown`` / the Neo4j loader / the annotator unchanged.

The JSON tree looks like::

    {"block_type": "Document", "children": [
        {"block_type": "Page", "bbox": [0, 0, W, H], "children": [
            {"block_type": "SectionHeader", "html": "<h1>SALE NOTICE</h1>",
             "bbox": [x0, y0, x1, y1], "polygon": [[...], ...]},
            {"block_type": "TableGroup", "children": [
                {"block_type": "Table",   "html": "<table>...</table>", ...},
                {"block_type": "Caption", "html": "<p>Schedule A</p>", ...}]},
            ...
        ]},
        ...
    ]}

Two things differ from MinerU and are handled here:

1. **Coordinates are page pixels, not a 0..1000 grid.** Each block's bbox is
   normalized by its *page* bbox (the Page block's width/height) to land in
   0..1 — feeding raw pixels into ``pipeline.mineru._normalize_bbox`` (which
   divides by a fixed 1000) would squash every box to the top-left corner.
2. **Text arrives as HTML per block.** Tags are stripped to plain text for
   normal blocks; table markup is kept verbatim (the canonical shape stores
   table HTML in ``text``, and ``assemble_markdown`` passes HTML through).

The block *type* vocabulary also differs (Datalab: ``SectionHeader``,
``ListItem``, ``Picture``, ``PageHeader`` …), mapped to the canonical set via
:data:`DATALAB_LABEL_MAP`. Group wrappers (``TableGroup`` / ``PictureGroup`` /
``FigureGroup`` / ``ListGroup``) are descended into so their members surface as
individual blocks, and a generic ``Caption`` inside a ``TableGroup`` is emitted
as ``TableCaption`` (vs ``ImageCaption`` under a picture/figure group).
"""
from __future__ import annotations

import html as _html
import re

from pipeline.mineru import (
    _coerce_text_level,
    _normalize_bbox,
    _resolve_img_url,
)


# Marker block_type (lowercased) -> canonical label. The canonical set matches
# pipeline.mineru.MINERU_LABEL_VALUES so both engines project identically.
# ``caption`` is disambiguated at walk time by its parent group (see
# ``_canon_label``); the value here is the picture/figure default.
DATALAB_LABEL_MAP: dict[str, str] = {
    "text":            "Text",
    "textinlinemath":  "Text",
    "complexregion":   "Text",
    "handwriting":     "Text",
    "tableofcontents":  "Text",
    "sectionheader":   "Title",
    "title":           "Title",
    "table":           "Table",
    "form":            "Table",
    "picture":         "Image",
    "figure":          "Image",
    "caption":         "ImageCaption",
    "footnote":        "Footnote",
    "pageheader":      "Header",
    "pagefooter":      "Footer",
    "equation":        "Equation",
    "code":            "Code",
    "listitem":        "List",
    "list":            "List",
    "reference":       "Reference",
}
DEFAULT_LABEL = "Text"

# Group wrappers: not emitted themselves; we descend into their children.
_GROUP_TYPES = {"figuregroup", "tablegroup", "listgroup", "picturegroup"}
# Structural containers: descend, no parent-group context carried down.
_STRUCTURAL = {"document", "page"}
# Intra-block primitives / cells: never emitted at layout level.
_SKIP = {"line", "span", "tablecell"}


# ── HTML → text ─────────────────────────────────────────────────────────────

_BR_RE      = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END  = re.compile(r"</(p|div|li|h[1-6]|tr)\s*>", re.I)
_TAG_RE     = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    """Flatten a block's HTML to plain text.

    Line-ish tags (``<br>``, closing ``</p>/</li>/</h1..6>/</tr>``) become
    newlines so multi-line blocks keep their shape; everything else is dropped
    and HTML entities are unescaped. Used for non-table blocks only — tables
    keep their raw HTML.
    """
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ── Tree walk ───────────────────────────────────────────────────────────────

def _canon_label(block_type: str | None, parent_group: str | None) -> str:
    key = (block_type or "").strip().lower()
    if key == "caption":
        # A caption's kind is only knowable from the group that wraps it.
        if (parent_group or "").lower() == "tablegroup":
            return "TableCaption"
        return "ImageCaption"
    return DATALAB_LABEL_MAP.get(key, DEFAULT_LABEL)


def _iter_pages(doc) -> list[dict]:
    """Return the ordered list of Page blocks from a Datalab json payload.

    Accepts the Document node (``{"block_type": "Document", "children": [...]}``),
    a bare children list, or a single Page — whatever the API version hands back.
    """
    if isinstance(doc, list):
        candidates = doc
    elif isinstance(doc, dict):
        if (doc.get("block_type") or "").lower() == "page":
            return [doc]
        candidates = doc.get("children") or []
    else:
        return []
    pages = [c for c in candidates
             if isinstance(c, dict) and (c.get("block_type") or "").lower() == "page"]
    # Fall back to treating every dict child as a page-like container if the
    # payload isn't wrapped in explicit Page nodes.
    return pages or [c for c in candidates if isinstance(c, dict)]


def _walk(block: dict, parent_group: str | None, acc: list[tuple[dict, str | None]]) -> None:
    """DFS a page subtree, collecting (leaf_block, parent_group) in reading order."""
    bt = (block.get("block_type") or "").strip().lower()
    if bt in _STRUCTURAL or bt in _GROUP_TYPES:
        pg = block.get("block_type") if bt in _GROUP_TYPES else None
        for ch in (block.get("children") or []):
            if isinstance(ch, dict):
                _walk(ch, pg, acc)
        return
    if bt in _SKIP or not bt:
        return
    acc.append((block, parent_group))


def _bbox_from_block(block: dict) -> list[float] | None:
    """Pull ``[x0,y0,x1,y1]`` from a block's ``bbox`` or, failing that, the
    min/max of its ``polygon`` corners. ``None`` when neither is usable."""
    b = block.get("bbox")
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        try:
            return [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        except (TypeError, ValueError):
            pass
    poly = block.get("polygon")
    if isinstance(poly, list) and poly:
        xs = [float(p[0]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [float(p[1]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2]
        if xs and ys:
            return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _normalize_pixel_bbox(raw: list[float] | None, page_bbox: list[float] | None) -> list[float]:
    """Normalize a page-pixel bbox to 0..1 using the page's own dimensions.

    Divides by the page width/height (from the Page block's bbox), then reuses
    ``pipeline.mineru._normalize_bbox`` (scale=1.0) for the shared clamp / axis-
    order / min-size finalization so both engines produce identical bbox shapes.
    """
    if raw is None:
        return [0.0, 0.0, 0.005, 0.005]
    px0, py0, px1, py1 = (page_bbox or [0.0, 0.0, 1.0, 1.0])
    w = (px1 - px0) or 1.0
    h = (py1 - py0) or 1.0
    norm = [
        (raw[0] - px0) / w,
        (raw[1] - py0) / h,
        (raw[2] - px0) / w,
        (raw[3] - py0) / h,
    ]
    # Coords are already 0..1 here, so _normalize_bbox's >2.0 pixel-detection
    # branch is skipped and only its clamp/order/min-size tail runs.
    return _normalize_bbox(norm, scale=1.0)


def parse_datalab_blocks(doc, *, img_map: dict[str, str] | None = None) -> list[dict]:
    """Convert a Datalab ``output_format=json`` payload to canonical blocks.

    ``doc`` is the value of the completed response's ``json`` field (the
    Document node). Returns a list of blocks in reading order, each with the
    exact keys :func:`pipeline.mineru.parse_mineru_content_list` produces
    (``source`` is ``"datalab"``). ``img_map`` — a ``{image-basename -> URL}``
    map from an R2 archive run — resolves each picture block's ``img_url``;
    absent, ``img_url`` stays ``None``.
    """
    out: list[dict] = []
    gi = 0
    for page_idx, page in enumerate(_iter_pages(doc)):
        page_bbox = _bbox_from_block(page)
        leaves: list[tuple[dict, str | None]] = []
        for ch in (page.get("children") or []):
            if isinstance(ch, dict):
                _walk(ch, None, leaves)
        page_no = page_idx + 1  # 1-indexed, matching the MinerU path
        for block, parent_group in leaves:
            label = _canon_label(block.get("block_type"), parent_group)
            html = block.get("html") or ""
            if label == "Table":
                text = html.strip()
                table = {"format": "html", "rows": None, "cols": None,
                         "row_positions": None, "col_positions": None}
            else:
                text = _strip_html(html)
                table = None
            imgs = block.get("images")
            img_path = next(iter(imgs), None) if isinstance(imgs, dict) and imgs else None
            out.append({
                "id": "",
                "page": page_no,
                "bbox": _normalize_pixel_bbox(_bbox_from_block(block), page_bbox),
                "label": label,
                "text": text,
                "reading_order": page_no * 1000 + gi,
                "source": "datalab",
                "confidence": None,
                "table": table,
                "img_path": img_path,
                "img_url": _resolve_img_url(img_path, img_map),
                "text_level": _coerce_text_level(block.get("heading_level")),
                "sub_type": None,
                "table_caption": None,
                "table_footnote": None,
                "edited_at": None,
                "edited_by": None,
            })
            gi += 1
    return out
