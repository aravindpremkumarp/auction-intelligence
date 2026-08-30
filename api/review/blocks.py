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


#: Every ``source`` value real code stamps onto a block. Writers:
#: ``pipeline/mineru.py`` ("mineru"), ``pipeline/datalab.py`` ("datalab"),
#: ``scripts/fix_missing_regions.py`` ("datalab-patchfix"), and this module
#: ("human", plus the engine name on re-extract).
#:
#: This is an allowlist for the WRITE path only. The read path deliberately
#: accepts any string (see ``Block.source`` in api/review/router.py): a
#: provenance value this list has not caught up with must never 400 the whole
#: annotator payload, which is exactly how both the "datalab" and the
#: "datalab-patchfix" outages happened.
KNOWN_BLOCK_SOURCES = ("mineru", "datalab", "datalab-patchfix", "human")

#: OCR engines a reviewer can drive a full-document re-ingest with. Datalab
#: returns a nested layout tree that decomposes a notice into Title / Text /
#: Table / Footer blocks; MinerU's vlm reads a fully-ruled notice as ONE giant
#: ``<table>`` (the ``table-collapse`` health flag), so a re-ingest hard-wired
#: to MinerU can only ever hand such a notice back as a single Table block.
REINGEST_ENGINES = ("datalab", "mineru")


def _clean_engine(engine: str | None) -> str:
    """Normalize a requested OCR engine, falling back to the pipeline default.

    An unknown / missing value resolves to ``config.DESCRIPTION_OCR_ENGINE``
    (datalab today) so the annotator button, the per-block re-extract and the
    bulk pipeline all agree on what "no explicit choice" means.
    """
    from pipeline.config import DESCRIPTION_OCR_ENGINE
    e = (engine or "").strip().lower()
    if e in REINGEST_ENGINES:
        return e
    fallback = (DESCRIPTION_OCR_ENGINE or "").strip().lower()
    return fallback if fallback in REINGEST_ENGINES else "datalab"


class BlocksConflict(RuntimeError):
    """Raised when the optimistic-lock CAS fails (HTTP 409 in the router)."""


class BlocksNotFound(RuntimeError):
    """Raised when the Document or the block id doesn't exist."""


class BlocksWouldEmptyDoc(ValueError):
    """Raised when an edit would leave a Document with no blocks at all.

    ``_save_doc`` rebuilds ``d.markdown`` from the block list, so persisting an
    empty one blanks the notice's text as well — the reviewer loses the parse
    and the document reads as never-OCR'd ("backfill required"). No edit is
    worth that, and a reviewer who wants a block gone can delete every block
    but the last. Subclasses ``ValueError`` so the router's existing handler
    maps it to HTTP 400 with this message.
    """


class BlocksUpstreamError(RuntimeError):
    """Raised when the OCR/extraction upstream (MinerU or network) fails.

    Mapped to HTTP 502 by the router so the reviewer sees the real reason
    ("MINERU_API_KEY not set", a timeout, a 4xx from MinerU) instead of a
    bare 500.
    """


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


def _clean_source(raw: Any) -> str:
    """Normalize a block's ``source`` provenance for the write path.

    The allowlist stays closed here so a replace-all cannot be used to write
    arbitrary provenance. Anything unrecognized becomes ``"human"`` — which is
    what a reviewer-drawn block looks like when the client sends no source.

    That fallback is lossy in one direction: it brands machine OCR as
    human-verified, inverting the review signal. So a new writer must be added
    to :data:`KNOWN_BLOCK_SOURCES` — ``test_every_writer_stamps_a_known_source``
    fails until it is. Note this is NOT what the annotator 400 was about: the
    read model accepts any string precisely so a missing entry here can never
    take a document offline.
    """
    return raw if raw in KNOWN_BLOCK_SOURCES else "human"


def _clean_crop_bbox(raw: Any) -> list[float] | None:
    """Validate a Document-level crop bbox.

    Returns ``None`` to clear the field. Otherwise returns a normalized
    ``[x0, y0, x1, y1]`` in ``[0, 1]`` with a stricter min-size floor than
    block bboxes (2%) — accidental tiny crops would brick re-ingest.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        raise ValueError("crop bbox must be a 4-element list [x0,y0,x1,y1] or null")
    x0, y0, x1, y1 = (float(raw[0]), float(raw[1]),
                      float(raw[2]), float(raw[3]))
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0, y0, x1, y1 = (_clamp01(x0), _clamp01(y0),
                      _clamp01(x1), _clamp01(y1))
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        raise ValueError("crop bbox must be at least 2% of the source on each axis")
    return [x0, y0, x1, y1]


MAX_CROP_REGIONS = 12


def _clean_crop_regions(raw: Any) -> list[dict] | None:
    """Validate a multi-region crop list: ``[{bbox: [x0,y0,x1,y1], page: int}]``.

    Returns ``None`` to clear (``None`` or ``[]`` input). Each bbox gets the
    same 2%-per-axis floor as the single crop. v1 constraint: every region
    must sit on the SAME page — re-ingest flattens the source to one page, so
    cross-page regions would silently lose pages. Regions are returned sorted
    top-to-bottom then left-to-right, which later defines document order.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("crop regions must be a list of {bbox, page} or null")
    if not raw:
        return None
    if len(raw) > MAX_CROP_REGIONS:
        raise ValueError(f"at most {MAX_CROP_REGIONS} crop regions are supported")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each crop region must be an object {bbox, page}")
        bbox = _clean_crop_bbox(item.get("bbox"))
        if bbox is None:
            raise ValueError("each crop region needs a bbox")
        out.append({"bbox": bbox, "page": _clean_crop_page(item.get("page"))})
    pages = {r["page"] for r in out}
    if len(pages) > 1:
        raise ValueError("all crop regions must be on the same page")
    out.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return out


def _merge_region_blocks(per_region: list[tuple[dict, list[dict]]],
                         page: int) -> list[dict]:
    """Merge per-region MinerU output into one document block list.

    ``per_region`` is ``[(region, blocks), ...]`` where each region is
    ``{"bbox": [x0,y0,x1,y1]}`` in full-image coords and its blocks carry
    bboxes normalized WITHIN that region (0..1, as MinerU returned for the
    crop). Region-local bboxes are remapped into full-image coords and
    clamped to their region; ``reading_order`` is region-major
    (``region_idx * 1000 + position``) so the reviewer's top-to-bottom
    region order defines document order; every block lands on ``page``.
    Pure — no DB access — so the remap math is unit-testable.
    """
    merged: list[dict] = []
    for ri, (region, blocks) in enumerate(per_region):
        rx0, ry0, rx1, ry1 = region["bbox"]
        rw, rh = (rx1 - rx0), (ry1 - ry0)
        for j, blk in enumerate(blocks):
            bx0, by0, bx1, by1 = blk["bbox"]
            blk["bbox"] = [
                min(max(rx0 + bx0 * rw, rx0), rx1),
                min(max(ry0 + by0 * rh, ry0), ry1),
                min(max(rx0 + bx1 * rw, rx0), rx1),
                min(max(ry0 + by1 * rh, ry0), ry1),
            ]
            blk["page"] = page
            blk["reading_order"] = ri * 1000 + j
            merged.append(blk)
    return merged


def _clean_crop_page(raw: Any) -> int:
    """Normalize a 1-indexed page number for ``crop_page``.

    Defaults to ``1`` for ``None``/missing/invalid input. The crop is
    document-level but for multi-page PDFs we need to know which page
    the bbox is meant for so re-ingest crops the right page.
    """
    if raw is None:
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


_VALID_ROTATIONS = (0, 90, 180, 270)


def _clean_rotation(raw: Any) -> int:
    """Validate a Document-level rotation. Returns one of 0/90/180/270.

    ``None`` / missing maps to ``0``. Any other value raises ``ValueError``
    so the router surfaces a 400 instead of silently persisting garbage.
    """
    if raw is None:
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    n = n % 360
    if n not in _VALID_ROTATIONS:
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    return n


def _rotate_bbox_forward(bbox: list[float], rotation: int) -> list[float]:
    """Map a normalized ``[x0,y0,x1,y1]`` from raw coords to ``rotation``-CW
    rotated coords.

    Used when re-ingesting a notice with a saved crop: the crop is stored
    in raw-orientation coords, but the cropper sees the post-rotation
    image, so we forward-map before passing it through.
    """
    x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]),
                      float(bbox[2]), float(bbox[3]))
    r = int(rotation) % 360
    if r == 0:
        return [x0, y0, x1, y1]
    if r == 90:
        # CW: (x, y) → (1-y, x).
        return [1.0 - y1, x0, 1.0 - y0, x1]
    if r == 180:
        return [1.0 - x1, 1.0 - y1, 1.0 - x0, 1.0 - y0]
    # 270 CW: (x, y) → (y, 1-x).
    return [y0, 1.0 - x1, y1, 1.0 - x0]


def _un_rotate_bbox(bbox: list[float], rotation: int) -> list[float]:
    """Inverse of :func:`_rotate_bbox_forward` — map rotated coords back to raw.

    Re-ingest sends a rotated image to MinerU, which returns bboxes in
    rotated coords; we un-rotate them before storing so block bboxes
    stay in canonical raw-orientation coords.
    """
    return _rotate_bbox_forward(bbox, (360 - int(rotation)) % 360)


def _pdf_page_count(pdf_bytes: bytes) -> int:
    """Count pages in a PDF without loading any rendering machinery.

    Used by ``reingest_notice`` to decide whether rotation would discard
    pages 2+ of a multi-page PDF. Lazy-imports PyMuPDF so callers that
    don't need PDF parsing don't pay the import cost.
    """
    import fitz  # type: ignore
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return int(doc.page_count or 0)
    finally:
        doc.close()


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
        OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
        WITH d, count(DISTINCT a) AS prop_count
        RETURN d.blocks                       AS blocks_json,
               coalesce(d.blocks_revision, 0) AS rev,
               d.filename                     AS filename,
               d.file_path                    AS file_path,
               d.public_url                   AS public_url,
               d.storage_key                  AS storage_key,
               d.notice_type                  AS notice_type,
               d.markdown                     AS markdown,
               d.markdown_model               AS markdown_model,
               d.crop_bbox                    AS crop_bbox,
               d.crop_page                    AS crop_page,
               d.crop_regions                 AS crop_regions_json,
               d.rotation                     AS rotation,
               prop_count                     AS property_count,
               size(d.markdown)               AS markdown_length,
               d.ocr_health_score             AS ocr_health_score,
               d.ocr_health_flags             AS ocr_health_flags,
               d.parse_quality_score          AS parse_quality_score,
               d.ink_uncovered_ratio          AS ink_uncovered_ratio,
               d.markdown_quality             AS markdown_quality,
               (d.markdown_verified_at IS NOT NULL) AS markdown_verified,
               toString(d.markdown_reextracted_at)  AS markdown_reextracted_at
        """,
        {"filename": filename},
        max_rows=1,
    )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    r = rows[0]
    doc = _parse_doc_blob(r.get("blocks_json"))
    raw_crop = r.get("crop_bbox")
    crop_bbox: list[float] | None
    try:
        crop_bbox = _clean_crop_bbox(raw_crop) if raw_crop else None
    except ValueError:
        # A persisted-but-malformed crop_bbox shouldn't break loading.
        crop_bbox = None
    crop_page = _clean_crop_page(r.get("crop_page")) if crop_bbox else None
    try:
        rotation = _clean_rotation(r.get("rotation"))
    except ValueError:
        # Stale/corrupt persisted value shouldn't brick the loader.
        rotation = 0
    crop_regions: list[dict] | None = None
    raw_regions = r.get("crop_regions_json")
    if raw_regions:
        try:
            crop_regions = _clean_crop_regions(json.loads(raw_regions))
        except (json.JSONDecodeError, ValueError, TypeError):
            # A persisted-but-malformed region list shouldn't break loading.
            crop_regions = None
    meta = {
        "filename":       r.get("filename"),
        "file_path":      r.get("file_path"),
        "public_url":     r.get("public_url"),
        "storage_key":    r.get("storage_key"),
        "notice_type":    r.get("notice_type"),
        "markdown":       r.get("markdown"),
        "markdown_model": r.get("markdown_model"),
        # Queue-parity metadata: the annotator renders the same badge strip the
        # markdown queue does, so a reviewer who opened a notice can still see
        # its type / OCR health / lot count without going back.
        "property_count":          r.get("property_count"),
        "markdown_length":         r.get("markdown_length"),
        "ocr_health_score":        r.get("ocr_health_score"),
        "ocr_health_flags":        r.get("ocr_health_flags"),
        "parse_quality_score":     r.get("parse_quality_score"),
        "ink_uncovered_ratio":     r.get("ink_uncovered_ratio"),
        "markdown_quality":        r.get("markdown_quality"),
        "markdown_verified":       bool(r.get("markdown_verified")),
        "markdown_reextracted_at": r.get("markdown_reextracted_at"),
        "crop_bbox":      crop_bbox,
        "crop_page":      crop_page,
        "crop_regions":   crop_regions,
        "rotation":       rotation,
    }
    return doc, int(r.get("rev") or 0), meta


def _save_doc(filename: str, doc: dict, expected_rev: int) -> int:
    """Optimistic-lock write. Returns the new revision number.

    Raises :class:`BlocksConflict` when the revision moved under our feet,
    so the caller can re-read and re-apply.
    """
    if not doc.get("blocks"):
        raise BlocksWouldEmptyDoc(
            f"refusing to save {filename} with no blocks: the markdown is "
            f"rebuilt from the block list, so this would also erase the text")
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
    """Return the full annotator payload for a notice.

    Per-block OCR-health is attached at the API serialization layer
    (``_ok_doc`` / ``_ok_block`` in the router), not here, so every block the
    API emits carries an up-to-date verdict without this read path needing to
    know about it.
    """
    doc, rev, meta = _load_doc(filename)
    return {
        **meta,
        "schema_version":    int(doc.get("schema_version") or 1),
        "source_dims":       doc.get("source_dims") or [],
        "blocks":            doc.get("blocks") or [],
        "blocks_revision":   rev,
        "backfill_required": not bool(doc.get("blocks")),
    }


#: Cap on the source image the coverage map will pull from R2. Notices are
#: scans of a newspaper page — a few MB at most; anything past this is not a
#: notice and shouldn't be downloaded into the API process to find out.
INK_MAP_MAX_BYTES = 32 * 1024 * 1024

#: Source extensions the coverage map can read. PDFs are included: the measure
#: rasterizes the requested page itself (pipeline/ink_coverage.py's
#: ``_render_pdf_page``), so a PDF notice gets the same map as a scan. Anything
#: outside this list is a source neither Pillow nor PyMuPDF opens, and is
#: answered with a reason rather than a failed decode.
INK_MAP_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
                ".pdf")


def ink_coverage(filename: str, page: int = 1) -> dict:
    """Coverage map for one page of a notice: which ink no block covers.

    The annotator renders this as the green/red overlay behind the
    ``missing-region`` flag, so it is measured by the same code the flag is —
    :func:`pipeline.ink_coverage.coverage_map` — against the CURRENTLY STORED
    blocks. That means it answers the reviewer's real question ("is my box
    fixing this?") rather than replaying the verdict stored at ingest time,
    which may predate their edits.

    Both source kinds work: a raster is measured as fetched, a PDF is rendered
    to the requested page by the measure itself.

    Returns the verdict plus base64 tile grids (see ``coverage_map``), or a
    verdict carrying ``details.skipped`` when the page can't be measured — no
    blocks on it, an unreachable or unreadable source, a page the PDF doesn't
    have. Unscorable is never an error: "we can't measure this" is a real
    answer the UI shows.
    """
    import base64

    from pipeline.ink_coverage import coverage_map

    doc, rev, meta = _load_doc(filename)
    page = max(1, int(page or 1))
    out: dict = {
        "filename": meta.get("filename") or filename,
        "page": page,
        "blocks_revision": rev,
        "public_url": meta.get("public_url"),
        "ocr_health_score": meta.get("ocr_health_score"),
        "ocr_health_flags": meta.get("ocr_health_flags") or [],
        "uncovered_ratio": None,
        "patch_ratio": None,
        "flag": False,
        "details": {},
    }
    url = meta.get("public_url")
    if not url:
        out["details"] = {"skipped": "no-public-url"}
        return out
    name = (meta.get("filename") or filename).lower()
    if not name.endswith(INK_MAP_EXTS):
        out["details"] = {"skipped": "unsupported-source"}
        return out

    import requests
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        img = resp.content
    except requests.RequestException as e:
        out["details"] = {"skipped": f"fetch-failed: {type(e).__name__}"}
        return out
    if len(img) > INK_MAP_MAX_BYTES:
        out["details"] = {"skipped": "source-too-large"}
        return out

    m = coverage_map(img, doc.get("blocks") or [], page=page)
    out.update({k: v for k, v in m.items() if k not in ("ink", "covered")})
    if "ink" in m:
        out["ink_b64"] = base64.b64encode(m["ink"]).decode()
        out["covered_b64"] = base64.b64encode(m["covered"]).decode()
    return out


def set_crop(filename: str, raw_bbox: Any, raw_page: Any = None) -> dict:
    """Persist (or clear) the Document-level ``crop_bbox`` and ``crop_page``.

    The crop is stored as Neo4j list/int properties on the Document node,
    NOT inside the ``blocks`` JSON, so it doesn't churn ``blocks_revision``.
    Existing block bboxes remain normalized to the full source — the
    crop only affects what the re-ingest pipeline ships to MinerU and how
    the annotator clips the displayed page.

    ``raw_page`` is 1-indexed; for multi-page PDFs it tells re-ingest which
    page the bbox is meant for. Ignored when ``raw_bbox`` is ``None``.
    """
    crop_bbox = _clean_crop_bbox(raw_bbox)
    if crop_bbox is None:
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            REMOVE d.crop_bbox, d.crop_page
            SET d.crop_bbox_set_at = NULL
            RETURN d.filename AS filename
            """,
            {"filename": filename},
        )
    else:
        crop_page = _clean_crop_page(raw_page)
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            SET d.crop_bbox        = $crop_bbox,
                d.crop_page        = $crop_page,
                d.crop_bbox_set_at = datetime()
            RETURN d.filename AS filename
            """,
            {"filename": filename, "crop_bbox": crop_bbox, "crop_page": crop_page},
        )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    return get_blocks(filename)


def set_crop_regions(filename: str, raw_regions: Any) -> dict:
    """Persist (or clear) the Document-level multi-region crop list.

    Stored as a JSON string property (Neo4j can't hold nested lists), NOT in
    the blocks JSON, so it doesn't churn ``blocks_revision`` — same contract
    as the single ``crop_bbox``. When regions are saved they take precedence
    over ``crop_bbox`` at re-ingest: each region is cropped and OCR'd
    separately (one MinerU batch) and the per-region blocks are merged back
    into one document. Pass ``null``/``[]`` to clear.
    """
    regions = _clean_crop_regions(raw_regions)
    if regions is None:
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            REMOVE d.crop_regions
            SET d.crop_regions_set_at = NULL
            RETURN d.filename AS filename
            """,
            {"filename": filename},
        )
    else:
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            SET d.crop_regions        = $regions_json,
                d.crop_regions_set_at = datetime()
            RETURN d.filename AS filename
            """,
            {"filename": filename,
             "regions_json": json.dumps(regions, ensure_ascii=False)},
        )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    return get_blocks(filename)


def set_rotation(filename: str, raw_rotation: Any) -> dict:
    """Persist (or clear) the Document-level ``rotation``.

    Rotation is degrees clockwise ∈ {0, 90, 180, 270}, stored as a Neo4j
    int property. Like crop, this is NOT in the blocks JSON, so it doesn't
    churn ``blocks_revision``. The re-ingest pipeline applies the
    rotation to the source before shipping it to MinerU, so OCR runs on
    an upright image; block bboxes from MinerU are then un-rotated back
    to raw-orientation coords so storage stays canonical.

    If a crop is already saved, the caller's NEW rotation changes the
    canonical-vs-displayed mapping. Existing block bboxes are already in
    raw-orientation coords, so they don't need touching — but the saved
    ``crop_bbox`` was drawn against whatever orientation was on screen at
    the time, so we leave it untouched here (it stays in raw-orientation
    coords as a Document property, same as blocks). The re-ingest
    pipeline forward-rotates it before applying the crop.
    """
    new_rotation = _clean_rotation(raw_rotation)
    if new_rotation == 0:
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            REMOVE d.rotation, d.rotation_set_at
            RETURN d.filename AS filename
            """,
            {"filename": filename},
        )
    else:
        rows = run_query(
            """
            MATCH (d:Document {filename: $filename})
            SET d.rotation        = $rotation,
                d.rotation_set_at = datetime()
            RETURN d.filename AS filename
            """,
            {"filename": filename, "rotation": new_rotation},
        )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    return get_blocks(filename)


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


def _normalize_replacement_blocks(raw_blocks: Any, by_email: str) -> list[dict]:
    """Validate + canonicalize a full incoming block array for replace_blocks.

    Pure (no DB access) so it is unit-testable in isolation. Preserves each
    block's id (assigns a fresh one only when missing/blank), de-dups ids,
    cleans bbox, validates label, and strips/cleans ``table`` to match the
    label. ``source`` / ``confidence`` / ``edited_*`` are preserved so an undo
    restores a faithful prior state.
    """
    if not isinstance(raw_blocks, list):
        raise ValueError("blocks must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ValueError("each block must be an object")
        bid = raw.get("id") or _new_id()
        if bid in seen:
            bid = _new_id()
        seen.add(bid)
        label = _validate_label(raw.get("label") or DEFAULT_LABEL)
        src = raw.get("source")
        conf = raw.get("confidence")
        out.append({
            "id":            bid,
            "page":          max(1, int(raw.get("page") or 1)),
            "bbox":          _clean_bbox(raw.get("bbox")),
            "label":         label,
            "text":          str(raw.get("text") or ""),
            "reading_order": int(raw.get("reading_order") or 0),
            # Allowlisted against KNOWN_BLOCK_SOURCES; anything else becomes
            # "human", which brands machine OCR as human-verified — so a new
            # writer must be registered there. See _clean_source.
            "source":        _clean_source(src),
            "confidence":    (float(conf)
                              if isinstance(conf, (int, float))
                              and not isinstance(conf, bool) else None),
            "table":         _clean_table(raw.get("table")) if label == "Table"
                             else None,
            # MinerU-provenance fields (archived image URL + previously-dropped
            # content-list fields). Preserved verbatim so an undo/redo restores
            # a faithful prior state; None on human-added blocks.
            "img_path":       raw.get("img_path"),
            "img_url":        raw.get("img_url"),
            "text_level":     raw.get("text_level"),
            "sub_type":       raw.get("sub_type"),
            "table_caption":  raw.get("table_caption"),
            "table_footnote": raw.get("table_footnote"),
            "edited_at":     raw.get("edited_at") or _iso_now(),
            "edited_by":     raw.get("edited_by") or by_email,
        })
    return out


def replace_blocks(filename: str, raw_blocks: Any,
                   expected_rev: int | None, by_email: str) -> dict:
    """Atomically replace the entire block array (undo/redo + multi-delete).

    CAS on ``blocks_revision`` via ``expected_rev`` when provided (the client
    always passes the current rev, so a stale write yields a clean 409 →
    reload). Reuses ``_save_doc`` so markdown is reassembled and the markdown
    verdict cleared, exactly like the granular endpoints.
    """
    doc, rev, _ = _load_doc(filename)
    if expected_rev is not None and int(expected_rev) != rev:
        raise BlocksConflict("blocks_revision changed; reload required")
    doc["blocks"] = _normalize_replacement_blocks(raw_blocks, by_email)
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
    # Reviewer's per-block engine choice from the annotator; Datalab is the
    # default (matches the bulk pipeline), MinerU is the opt-in alternate.
    engine = (body.get("engine") or "datalab").strip().lower()
    if engine not in ("datalab", "mineru"):
        engine = "datalab"

    if not meta.get("public_url"):
        raise BlocksNotFound("Document has no public_url; cannot crop source")

    # Lazy import — keeps cold-start fast for the rest of the review UI.
    from pipeline.reextract import crop_and_reextract
    try:
        result = await crop_and_reextract(
            public_url=meta["public_url"],
            page=page,
            bbox_norm=bbox,
            label=label,
            row_positions=row_positions,
            col_positions=col_positions,
            engine=engine,
        )
    except Exception as e:
        # MinerU error, network failure, timeout, or a bad crop — surface the
        # reason as a 502 instead of letting it become a bare 500.
        raise BlocksUpstreamError(
            f"extraction failed: {type(e).__name__}: {e}"
        ) from e

    # Refresh after the (potentially long) network call to minimize the
    # window where a concurrent edit could collide.
    doc, rev, _ = _load_doc(filename)
    blk = next((b for b in doc["blocks"] if b.get("id") == block_id), None)
    if blk is None:
        raise BlocksNotFound(f"block not found: {block_id}")

    blk["bbox"]   = bbox
    blk["label"]  = label
    blk["text"]   = result.get("text") or ""
    # Record the engine that actually ran, not a hardcoded one — `engine`
    # defaults to datalab above, so hardcoding "mineru" mislabelled the
    # provenance of every default re-extraction.
    blk["source"] = engine
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
    # Stamp the auto-fix marker so the annotator's one-time auto re-extract
    # never fires again on this block — even if it comes back still flagged
    # (the reviewer then adjusts the box + Re-ingest). Manual re-runs leave it
    # untouched, so a reviewer can always re-run by hand.
    if body.get("auto"):
        blk["auto_reextract_at"] = _iso_now()
    _save_doc(filename, doc, rev)
    # _save_doc left the doc in `pending` (the standard "blocks changed →
    # markdown verdict is stale" behaviour). Additionally stamp a
    # re-extracted marker so the markdown queue card can show a "re-extracted"
    # pill — lets the reviewer see at a glance that this notice has been
    # touched without moving it out of the pending bucket.
    run_query(
        """
        MATCH (d:Document {filename: $filename})
        SET d.markdown_reextracted_at = datetime(),
            d.markdown_reextracted_by = $by
        """,
        {"filename": filename, "by": by_email},
    )
    # The reassembled markdown changed; refresh the coverage + OCR-health
    # scores so the queue counters reflect the new state. Best-effort.
    fp = meta.get("file_path")
    if fp:
        try:
            from pipeline.score_markdown import score_freshly_loaded
            score_freshly_loaded([fp])
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "re-scoring after re-extract failed for %s", filename
            )
        try:
            from pipeline.ocr_health import score_freshly_loaded as score_health
            score_health([fp])
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "OCR-health scoring after re-extract failed for %s", filename
            )
    return blk


def reingest_notice_safe(filename: str, by_email: str,
                         engine: str | None = None) -> None:
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
        reingest_notice(filename, by_email, engine=engine)
        log.info("reingest succeeded for %s (engine=%s)",
                 filename, _clean_engine(engine))
    except Exception:
        log.exception("reingest background task failed for %s (engine=%s)",
                      filename, _clean_engine(engine))


def _persist_reingest_result(filename: str, *, markdown: str, blocks_json: str,
                             markdown_raw: str | None,
                             blocks_raw: str | None,
                             mineru_zip_url: str | None = None,
                             markdown_source: str = "mineru",
                             markdown_model: str = "mineru-vlm",
                             parse_quality: float | None = None) -> None:
    """Persist a fresh full-document re-ingest.

    Writes the working ``markdown`` + ``blocks`` AND the durable raw copy
    (``markdown_raw`` = full.md, ``blocks_raw`` = content_list.json). Bumps
    ``blocks_revision`` and clears the markdown verdict, same as before. The raw
    fields are written here — a full OCR run — and NEVER by the edit paths
    (``_save_doc`` / ``re_extract_block``), so a reviewer edit can't lose them. A
    crop/rotation re-ingest refreshes the raw copy to that run's output, which is
    correct: the resulting blocks come from that run too.

    ``mineru_zip_url`` stamps the archived full-zip URL (and ``mineru_zip_at``)
    when this run archived to R2; None leaves any prior value untouched.

    ``markdown_source`` / ``markdown_model`` record WHICH engine produced this
    text ('mineru' + 'mineru-vlm', or 'datalab' + 'datalab-<tier>'), so the
    queue and any later audit can tell a Datalab re-ingest from a MinerU one.
    ``parse_quality`` is Datalab's own 0-5 verdict; None (always, on the MinerU
    path — it has no equivalent signal) leaves any prior score untouched.
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
            d.mineru_zip_url      = coalesce($mineru_zip_url, d.mineru_zip_url),
            d.mineru_zip_at       = CASE WHEN $mineru_zip_url IS NULL
                                        THEN d.mineru_zip_at ELSE datetime() END,
            d.blocks_revision     = coalesce(d.blocks_revision, 0) + 1,
            d.markdown_loaded_at  = datetime(),
            d.markdown_source     = $markdown_source,
            d.markdown_model      = $markdown_model,
            d.parse_quality_score = coalesce($parse_quality, d.parse_quality_score),
            d.parse_quality_at    = CASE WHEN $parse_quality IS NULL
                                        THEN d.parse_quality_at ELSE datetime() END,
            d.markdown_verified_at = NULL,
            d.markdown_verified_by = NULL,
            d.markdown_quality     = NULL
        """,
        {"filename": filename, "markdown": markdown, "blocks_json": blocks_json,
         "markdown_raw": markdown_raw, "blocks_raw": blocks_raw,
         "mineru_zip_url": mineru_zip_url,
         "markdown_source": markdown_source, "markdown_model": markdown_model,
         "parse_quality": parse_quality},
    )


def _reingest_multi_region(*, filename: str, fp: str, src_filename: str,
                           disk, regions: list[dict],
                           applied_rotation: int,
                           effective_page: int,
                           engine: str = "mineru",
                           notice_type: str | None = None) -> dict:
    """OCR every crop region with ``engine`` and merge the results.

    ``disk`` is the source on local disk — the original image/PDF, or the
    rotation-flattened PNG when ``applied_rotation`` is set (region bboxes
    are already forward-rotated to match). Raises on any region failure so
    the document is never left with a partial merge.

    MinerU takes all regions in ONE batch (one batch_id, one poll) and archives
    each region's zip to R2. Datalab has no batch endpoint, so each crop is a
    separate convert call, run concurrently at the bulk pipeline's fan-out.
    Both branches produce the same canonical per-region block lists, so the
    merge below is engine-agnostic.
    """
    import json as _json
    import logging
    import os
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path

    from pipeline.load_markdowns_to_neo4j import _assign_block_ids
    from pipeline.mineru import (
        MINERU_BLOCKS_DIR, MINERU_MARKDOWN_DIR, assemble_markdown,
        parse_mineru_content_list, safe_cache_name, write_mineru_meta,
    )

    log = logging.getLogger(__name__)
    src_bytes = disk.read_bytes()
    ext = ".png" if applied_rotation else Path(src_filename).suffix.lower()
    parse_quality: float | None = None
    datalab_mode: str | None = None

    # 1) Crop one PNG per region (regions are pre-sorted top-to-bottom).
    items: list[dict] = []
    crop_tmps: list[Path] = []
    try:
        for i, region in enumerate(regions):
            if ext == ".pdf":
                from pipeline.reextract import _pdf_crop_to_png
                png = _pdf_crop_to_png(src_bytes, region["page"], region["bbox"])
            else:
                from pipeline.reextract import _image_crop_to_png
                png = _image_crop_to_png(src_bytes, region["bbox"])
            fd, tmp_name = tempfile.mkstemp(suffix=".png",
                                            prefix=f"reingest_r{i}_")
            with os.fdopen(fd, "wb") as out:
                out.write(png)
            crop_tmps.append(Path(tmp_name))
            items.append({
                # ``::r{i}`` namespaces each region's cache/archive key under
                # the document without colliding with its main key.
                "filename":  f"{Path(src_filename).stem}_r{i}.png",
                "file_path": f"{fp}::r{i}",
                "disk_path": Path(tmp_name),
            })

        per_region: list[tuple[dict, list[dict]]] = []
        region_mds: list[str] = []
        raw_lists: list[list] = []
        merged_img_map: dict[str, str] = {}

        if engine == "datalab":
            # 2a) One convert call per crop, fanned out. Ordered results —
            # reading order across regions is the saved (top-to-bottom) order.
            from concurrent.futures import ThreadPoolExecutor

            from pipeline import datalab_api
            from pipeline.config import (
                DATALAB_PIPELINE_CONCURRENCY, datalab_mode_for,
            )
            from pipeline.datalab import parse_datalab_blocks

            datalab_mode = datalab_mode_for(notice_type)
            workers = max(1, min(DATALAB_PIPELINE_CONCURRENCY, len(items)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(
                    lambda it: datalab_api.run_file(
                        it["disk_path"], output_format="json",
                        mode=datalab_mode),
                    items,
                ))
            for i, (region, result) in enumerate(zip(regions, results)):
                md_text, tree, _imgs = datalab_api.extract_payload(result)
                blocks = parse_datalab_blocks(tree)
                if not blocks:
                    raise RuntimeError(
                        f"Datalab returned no blocks for crop region "
                        f"{i + 1}/{len(regions)}")
                # Datalab's own 0-5 parse verdict; keep the worst region's,
                # since the merged document is only as good as its weakest band.
                q = datalab_api.parse_quality(result)
                if q is not None:
                    parse_quality = q if parse_quality is None \
                        else min(parse_quality, q)
                region_mds.append((md_text or assemble_markdown(blocks)).strip())
                # The canonical blocks ARE the durable raw record for Datalab
                # (there is no zip / content-list), matching what the bulk
                # Datalab re-OCR writes to ``blocks_raw``.
                raw_lists.append(blocks)
                per_region.append((region, blocks))
        else:
            # 2b) One MinerU batch for all regions: one batch_id, one poll.
            from pipeline.mineru_api import (
                archive_zip_to_r2, download_zip, parse_zip_payload,
                poll, request_batch, upload_files,
            )

            batch_id, urls = request_batch(items)
            upload_files(items, urls)
            results = poll(batch_id)

            by_data_id = {r.get("data_id"): r for r in results}
            for i, (region, item) in enumerate(zip(regions, items)):
                row = by_data_id.get(safe_cache_name(item["file_path"])[:128])
                if row is None or row.get("state") != "done":
                    err = row.get("err_msg") if row else "no result row"
                    raise RuntimeError(
                        f"MinerU failed on crop region "
                        f"{i + 1}/{len(regions)}: {err}")
                zip_url = row.get("full_zip_url")
                zip_bytes = download_zip(zip_url) if zip_url else None
                if not zip_bytes:
                    raise RuntimeError(
                        f"could not download result zip for region {i + 1}")
                # Keep the region's full output (zip + image crops) in R2 and
                # collect its img_map so merged blocks resolve img_url.
                meta = archive_zip_to_r2(item["file_path"], zip_bytes)
                merged_img_map.update(meta.get("img_map") or {})
                md_text, blocks_raw = parse_zip_payload(zip_bytes)
                if blocks_raw is None:
                    raise RuntimeError(
                        f"region {i + 1} returned no content-list JSON")
                region_blocks = parse_mineru_content_list(
                    blocks_raw, img_map=merged_img_map)
                # Same rule the Datalab branch above applies: a region that
                # parses to nothing is a failed region. Letting it through
                # merges to an empty document and persists that over the real
                # block layer, while the region's markdown still lands.
                if not region_blocks:
                    raise RuntimeError(
                        f"MinerU returned no parseable blocks for crop region "
                        f"{i + 1}/{len(regions)}")
                region_mds.append((md_text or "").strip())
                raw_lists.append(blocks_raw)
                per_region.append((region, region_blocks))
    finally:
        for p in crop_tmps:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # 3) Merge into full-image coords; un-rotate back to raw orientation.
    blocks = _merge_region_blocks(per_region, page=effective_page)
    if applied_rotation:
        for blk in blocks:
            blk["bbox"] = _un_rotate_bbox(blk["bbox"], applied_rotation)
    _assign_block_ids(blocks)

    # 4) Refresh the on-disk caches with the merged result so downstream
    # cache readers (loader, description stages) see what Neo4j holds.
    new_md = "\n\n".join(md for md in region_mds if md)
    safe = safe_cache_name(fp)
    try:
        MINERU_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        (MINERU_MARKDOWN_DIR / f"{safe}.md").write_text(
            new_md, encoding="utf-8")
        MINERU_BLOCKS_DIR.mkdir(parents=True, exist_ok=True)
        (MINERU_BLOCKS_DIR / f"{safe}.json").write_text(
            _json.dumps({"schema_version": 1, "blocks": blocks},
                        ensure_ascii=False),
            encoding="utf-8")
        write_mineru_meta(fp, {
            "zip_url": None,   # no single zip covers a multi-region run
            "img_map": merged_img_map,
            "archived_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        })
    except OSError:
        log.exception("cache refresh failed for %s (multi-region)", filename)

    doc = {"schema_version": 1, "blocks": blocks}
    _persist_reingest_result(
        filename,
        markdown=new_md,
        blocks_json=_json.dumps(doc, ensure_ascii=False),
        markdown_raw=new_md,
        blocks_raw=_json.dumps(
            [b for lst in raw_lists for b in lst], ensure_ascii=False),
        mineru_zip_url=None,
        markdown_source=engine,
        markdown_model=(f"datalab-{datalab_mode}" if engine == "datalab"
                        else "mineru-vlm"),
        parse_quality=parse_quality,
    )
    try:
        from pipeline.score_markdown import score_freshly_loaded
        score_freshly_loaded([fp])
    except Exception:
        log.exception("re-scoring after multi-region reingest failed for %s",
                      filename)
    try:
        from pipeline.ocr_health import score_freshly_loaded as score_health
        score_health([fp])
    except Exception:
        log.exception("OCR-health scoring after multi-region reingest failed "
                      "for %s", filename)
    return get_blocks(filename)


def reingest_notice(filename: str, by_email: str,
                    engine: str | None = None) -> dict:
    """Re-run the full OCR pipeline for a single Document with ``engine``.

    ``engine`` is ``"datalab"`` or ``"mineru"`` (the reviewer's choice in the
    annotator toolbar); anything else resolves to the pipeline default via
    :func:`_clean_engine`. The engine matters: MinerU's vlm reads a
    fully-ruled notice as one giant ``<table>`` — the ``table-collapse``
    health flag — so re-ingesting such a notice through MinerU returns a
    single Table block no matter how many times it is pressed, while Datalab
    returns the same page decomposed into Title / Text / Table blocks.

    Used for backfilling notices that predate the per-block JSON cache.
    On a local dev machine the source file is usually on disk under
    ``downloads/``. On the production API host it isn't — the canonical
    source is the R2 ``public_url`` — so this falls back to streaming the
    R2 bytes to a tempfile before handing them to the engine. Lazy-imports
    the bulk pipeline helpers so the import only fires when the reviewer
    asks for it.
    """
    engine = _clean_engine(engine)
    rows = run_read_query(
        """
        MATCH (d:Document {filename: $filename})
        RETURN d.file_path    AS file_path,
               d.filename     AS filename,
               d.public_url   AS public_url,
               d.notice_type  AS notice_type,
               d.crop_bbox    AS crop_bbox,
               d.crop_page    AS crop_page,
               d.crop_regions AS crop_regions_json,
               d.rotation     AS rotation
        """,
        {"filename": filename},
        max_rows=1,
    )
    if not rows:
        raise BlocksNotFound(f"Document not found: {filename}")
    fp = rows[0]["file_path"]
    src_filename = rows[0]["filename"]
    public_url = rows[0].get("public_url")
    notice_type = rows[0].get("notice_type")
    raw_crop = rows[0].get("crop_bbox")
    try:
        crop_bbox = _clean_crop_bbox(raw_crop) if raw_crop else None
    except ValueError:
        crop_bbox = None
    crop_page = _clean_crop_page(rows[0].get("crop_page")) if crop_bbox else 1
    # Multi-region crop list; when present it takes precedence over the
    # single crop_bbox. Malformed persisted JSON degrades to None.
    crop_regions: list[dict] | None = None
    raw_regions = rows[0].get("crop_regions_json")
    if raw_regions:
        try:
            crop_regions = _clean_crop_regions(json.loads(raw_regions))
        except (json.JSONDecodeError, ValueError, TypeError):
            crop_regions = None
    try:
        rotation = _clean_rotation(rows[0].get("rotation"))
    except ValueError:
        rotation = 0

    from pipeline.mineru import MINERU_SUPPORTED_EXTS, find_disk_path

    import logging
    import os
    import tempfile
    from pathlib import Path

    log = logging.getLogger(__name__)

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

    # If a Document-level rotation is set, apply it BEFORE the crop and
    # BEFORE shipping to MinerU. We replace ``disk`` with a PNG of the
    # rotated source so the rest of the pipeline (crop, MinerU) consumes
    # the upright image. For PDFs we flatten the chosen page to a PNG
    # (matches what crop already does for PDFs); ``applied_rotation_page``
    # tells the post-OCR step which page everything came from.
    #
    # Multi-page PDFs without a crop are a special case: applying rotation
    # would force us to flatten the whole doc to one page (losing pages
    # 2+ of OCR). We skip rotation in that case to preserve the existing
    # multi-page behavior; the reviewer can set a crop to scope re-ingest
    # to one page if they need rotation. The CSS rotation in the UI is
    # unaffected.
    applied_rotation: int = 0
    applied_rotation_page: int = max(
        1, crop_regions[0]["page"] if crop_regions
        else (crop_page if crop_bbox else 1))
    rotation_tmp_path: Path | None = None
    if rotation != 0:
        ext = Path(src_filename).suffix.lower()
        try:
            src_bytes = disk.read_bytes()
            rotated_png: bytes | None = None
            if ext in {".jpg", ".jpeg", ".png", ".webp", ".jfif"}:
                from pipeline.reextract import _image_rotate_to_png
                rotated_png = _image_rotate_to_png(src_bytes, rotation)
            elif ext == ".pdf":
                if (crop_bbox is None and crop_regions is None
                        and _pdf_page_count(src_bytes) > 1):
                    log.info(
                        "skipping rotation for %s: multi-page PDF without "
                        "crop would discard pages 2+ of OCR",
                        filename,
                    )
                else:
                    from pipeline.reextract import _pdf_rotate_page_to_png
                    rotated_png = _pdf_rotate_page_to_png(
                        src_bytes, applied_rotation_page, rotation,
                    )
            else:
                log.info(
                    "skipping rotation for %s: unsupported source extension %s",
                    filename, ext,
                )
            if rotated_png is not None:
                fd, tmp_name = tempfile.mkstemp(
                    suffix=".png", prefix="reingest_rot_",
                )
                with os.fdopen(fd, "wb") as out:
                    out.write(rotated_png)
                rotation_tmp_path = Path(tmp_name)
                disk = rotation_tmp_path
                applied_rotation = rotation
                # The crop bbox is stored in raw-orientation coords; the
                # crop step below now sees a rotated image, so forward-map
                # the bbox into the rotated frame. ``crop_page`` stays as
                # the user-facing page — the crop step picks the image
                # branch (ext forced to .png), which doesn't consume it,
                # but the post-process uses it to set ``blk['page']``.
                if crop_bbox is not None:
                    crop_bbox = _rotate_bbox_forward(crop_bbox, applied_rotation)
                # Same for every multi-crop region: crop + remap happen in
                # the rotated frame; block bboxes are un-rotated back to
                # raw coords after the merge. Region ORDER stays as saved
                # (raw-frame top-to-bottom) so reading order is stable.
                if crop_regions:
                    crop_regions = [
                        {**r, "bbox": _rotate_bbox_forward(r["bbox"],
                                                           applied_rotation)}
                        for r in crop_regions
                    ]
        except Exception:
            log.exception(
                "rotation apply failed for %s; falling back to unrotated",
                filename,
            )

    # Multi-region crop: each saved region is cropped and OCR'd separately in
    # ONE MinerU batch, then the per-region block lists are remapped into
    # full-image coords and merged. This is how a bordered notice whose
    # full-page OCR collapses into a single giant Table (or degenerates into
    # repetition loops) gets a faithful decomposition: prose regions come
    # back as Text blocks, the table region as a clean Table. Takes
    # precedence over the single crop_bbox. All-or-nothing: one failed
    # region aborts the re-ingest so a band of the document can't silently
    # vanish.
    if crop_regions:
        try:
            return _reingest_multi_region(
                filename=filename, fp=fp, src_filename=src_filename,
                disk=disk, regions=crop_regions,
                applied_rotation=applied_rotation,
                effective_page=applied_rotation_page,
                engine=engine, notice_type=notice_type,
            )
        finally:
            for p in (tmp_path, rotation_tmp_path):
                if p is not None:
                    try:
                        p.unlink()
                    except FileNotFoundError:
                        pass

    # If a Document-level crop is set, apply it BEFORE shipping to MinerU.
    # Images crop straight through Pillow; PDFs crop via PyMuPDF rendering the
    # selected page through the bbox clip. We track ``applied_crop`` so the
    # post-OCR step can remap block bboxes back to full-image coords (existing
    # blocks already use full-image coords, so all bboxes in Neo4j stay in
    # the same coordinate system regardless of crop).
    applied_crop: list[float] | None = None
    applied_crop_page: int = 1
    crop_tmp_path: Path | None = None
    if crop_bbox is not None:
        # When rotation flattened the source to a PNG, the crop step must
        # treat it as an image regardless of the original extension.
        ext = ".png" if applied_rotation else Path(src_filename).suffix.lower()
        try:
            src_bytes = disk.read_bytes()
            cropped_png: bytes | None = None
            if ext in {".jpg", ".jpeg", ".png", ".webp", ".jfif"}:
                from pipeline.reextract import _image_crop_to_png
                cropped_png = _image_crop_to_png(src_bytes, crop_bbox)
            elif ext == ".pdf":
                from pipeline.reextract import _pdf_crop_to_png
                cropped_png = _pdf_crop_to_png(src_bytes, crop_page, crop_bbox)
            else:
                log.info(
                    "skipping crop for %s: unsupported source extension %s",
                    filename, ext,
                )
            if cropped_png is not None:
                fd, tmp_name = tempfile.mkstemp(
                    suffix=".png", prefix="reingest_crop_",
                )
                with os.fdopen(fd, "wb") as out:
                    out.write(cropped_png)
                crop_tmp_path = Path(tmp_name)
                disk = crop_tmp_path
                applied_crop = crop_bbox
                applied_crop_page = crop_page
        except Exception:
            log.exception(
                "crop apply failed for %s; falling back to uncropped",
                filename,
            )

    datalab_mode: str | None = None
    try:
        if engine == "datalab":
            # Datalab writes into the SAME on-disk cache the MinerU path uses
            # (markdown + pre-normalized canonical blocks), so everything below
            # — load_blocks_for, the bbox remap, the persist — is unchanged.
            from pipeline import datalab_api
            from pipeline.config import datalab_mode_for
            datalab_mode = datalab_mode_for(notice_type)
            md_path, blocks_path = datalab_api.run_and_cache(
                fp, disk, mode=datalab_mode,
            )
            if md_path is None:
                raise RuntimeError(
                    "reingest succeeded but no markdown was produced")
        else:
            from pipeline.mineru_api import (
                download_and_cache, poll, request_batch, upload_files,
            )
            # When we cropped or rotated, the source on disk is a PNG; send
            # the .png filename so MinerU's extension hint matches the bytes.
            # Cache key (file_path) stays the original — the new result
            # authoritatively replaces the previous cached blocks for this
            # document.
            if applied_crop:
                mineru_filename = Path(src_filename).stem + "_crop.png"
            elif applied_rotation:
                mineru_filename = Path(src_filename).stem + "_rot.png"
            else:
                mineru_filename = src_filename
            item = {
                "filename":  mineru_filename,
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
            # archive_to_r2: a reviewer re-ingest is a full-document OCR run, so
            # keep MinerU's complete output (zip + image crops) and the meta
            # sidecar.
            md_path, blocks_path = download_and_cache(
                fp, zip_url, archive_to_r2=True)
            if md_path is None:
                raise RuntimeError(
                    "reingest succeeded but no markdown was produced")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        if rotation_tmp_path is not None:
            try:
                rotation_tmp_path.unlink()
            except FileNotFoundError:
                pass
        if crop_tmp_path is not None:
            try:
                crop_tmp_path.unlink()
            except FileNotFoundError:
                pass

    # Reuse the loader's parsing so we share one code path. On the MinerU path
    # the archive meta (written by download_and_cache above) carries the image
    # map used to resolve each block's img_url and the archived zip URL stamped
    # below. Datalab archives no zip and caches its blocks pre-normalized (so
    # load_blocks_for never consults img_map): reading the sidecar there would
    # only surface a PREVIOUS MinerU run's zip_url and stamp it on this one.
    from pipeline.load_markdowns_to_neo4j import load_blocks_for, read_parse_quality
    from pipeline.mineru import read_mineru_meta
    archive_meta = {} if engine == "datalab" else read_mineru_meta(fp)
    # A run whose content-list is missing or yields nothing is a FAILED run,
    # not a document that legitimately has no blocks. load_blocks_for returns
    # None there, and coercing that to [] used to persist an empty layer over
    # the real one — invisibly, because markdown and blocks_raw are read from
    # different files and both survive, so the result looks like a successful
    # re-ingest with a document that simply has no blocks. Fail instead: the
    # caller logs it, blocks_revision never advances (which is exactly how the
    # frontend detects failure), and the reviewer can press Re-ingest again.
    # This mirrors the guard the Datalab multi-region branch already has.
    blocks = load_blocks_for(fp, img_map=archive_meta.get("img_map") or {})
    if not blocks:
        raise BlocksUpstreamError(
            f"{engine} re-ingest produced no parseable blocks for {filename}; "
            f"keeping the existing block layer")

    # The page the user was viewing when they set rotation/crop. Whenever
    # we flattened the source to a single-page PNG (via either step) the
    # blocks need their page set to this user-facing page.
    effective_page = (applied_crop_page if applied_crop
                      else applied_rotation_page)

    if applied_crop is not None and blocks:
        cx0, cy0, cx1, cy1 = applied_crop
        cw = cx1 - cx0
        ch = cy1 - cy0
        for blk in blocks:
            bx0, by0, bx1, by1 = blk["bbox"]
            rx0 = cx0 + bx0 * cw
            ry0 = cy0 + by0 * ch
            rx1 = cx0 + bx1 * cw
            ry1 = cy0 + by1 * ch
            # Clamp to the crop region so OCR edge artifacts can't leak
            # outside the visually-cropped view.
            blk["bbox"] = [
                min(max(rx0, cx0), cx1),
                min(max(ry0, cy0), cy1),
                min(max(rx1, cx0), cx1),
                min(max(ry1, cy0), cy1),
            ]

    # Un-rotate bboxes from rotated-image coords back to raw-orientation
    # so storage stays canonical (all bboxes in raw, full-image coords).
    if applied_rotation and blocks:
        for blk in blocks:
            blk["bbox"] = _un_rotate_bbox(blk["bbox"], applied_rotation)

    # When we flattened to a single PNG (crop or rotation), MinerU sees
    # one page and labels everything page 1 — retag with the page the
    # user was working on.
    if (applied_crop or applied_rotation) and blocks:
        for blk in blocks:
            blk["page"] = effective_page

    doc = {"schema_version": 1, "blocks": blocks}
    blocks_json = json.dumps(doc, ensure_ascii=False)
    new_md = md_path.read_text(encoding="utf-8")
    try:
        blocks_raw = blocks_path.read_text(encoding="utf-8") if blocks_path else None
    except (OSError, UnicodeDecodeError):
        blocks_raw = None
    _persist_reingest_result(
        filename,
        markdown=new_md,
        blocks_json=blocks_json,
        markdown_raw=new_md,
        blocks_raw=blocks_raw,
        mineru_zip_url=archive_meta.get("zip_url"),
        markdown_source=engine,
        markdown_model=(f"datalab-{datalab_mode}" if engine == "datalab"
                        else "mineru-vlm"),
        parse_quality=(read_parse_quality(fp) if engine == "datalab" else None),
    )
    # Refresh the coverage + OCR-health scores: the markdown just changed,
    # so any prior verdict is stale. Best-effort — a scoring failure must
    # not undo the successful re-ingest.
    try:
        from pipeline.score_markdown import score_freshly_loaded
        score_freshly_loaded([fp])
    except Exception:
        log.exception("re-scoring after reingest failed for %s", filename)
    try:
        from pipeline.ocr_health import score_freshly_loaded as score_health
        score_health([fp])
    except Exception:
        log.exception("OCR-health scoring after reingest failed for %s", filename)
    return get_blocks(filename)
