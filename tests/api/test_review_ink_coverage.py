"""
tests/api/test_review_ink_coverage.py
-------------------------------------
Unit tests for ``ink_coverage`` in :mod:`api.review.blocks` — the read behind
the annotator's Ink tab.

The measurement itself is covered in tests/pipeline/test_ink_coverage.py; what
matters here is the shape the endpoint hands the UI: base64 grids sized to the
tile grid, for a PDF notice as much as a scan, and a *verdict with a reason*
(never an error) on every page that can't be measured — no image, a source
neither Pillow nor PyMuPDF opens, an unreachable or oversized fetch.

DB-free: blocks.py is imported in isolation with stubbed neo4j/mineru, same
pattern as test_review_rotation.py, and ``_load_doc`` / ``requests.get`` are
monkeypatched per test.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

_BLOCKS_PATH = Path(__file__).resolve().parents[2] / "api" / "review" / "blocks.py"
_spec = importlib.util.spec_from_file_location("_blocks_under_test_ink", _BLOCKS_PATH)
_mod = importlib.util.module_from_spec(_spec)

_STUB_KEYS = ("api.neo4j_client", "pipeline.mineru", "pipeline")
_saved = {k: sys.modules.get(k) for k in _STUB_KEYS}

if "api.neo4j_client" not in sys.modules:
    _stub_neo4j = types.ModuleType("api.neo4j_client")
    _stub_neo4j.run_query = lambda *a, **k: None
    _stub_neo4j.run_read_query = lambda *a, **k: None
    sys.modules["api.neo4j_client"] = _stub_neo4j
if "pipeline" not in sys.modules:
    sys.modules["pipeline"] = types.ModuleType("pipeline")
if "pipeline.mineru" not in sys.modules:
    _stub_mineru = types.ModuleType("pipeline.mineru")
    _stub_mineru.DEFAULT_LABEL = "Text"
    _stub_mineru.MINERU_LABEL_VALUES = ["Text", "Title", "Table"]
    _stub_mineru.assemble_markdown = lambda blocks: ""
    sys.modules["pipeline.mineru"] = _stub_mineru

try:
    _spec.loader.exec_module(_mod)
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

pytest.importorskip("PIL", reason="ink coverage needs Pillow")

URL = "https://r2.example/notices/n1.png"
W = H = 400


def _page(ink_boxes) -> bytes:
    """A white page whose given 0..1 boxes carry lines of text-like marks."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in ink_boxes:
        y = y0 * H
        while y + 7 <= y1 * H:
            x = x0 * W
            while x + 16 <= x1 * W:
                d.rectangle([x, y, x + 16, y + 7], fill="black")
                x += 22
            y += 12
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _block(x0, y0, x1, y1, page=1, text="x"):
    return {"page": page, "bbox": [x0, y0, x1, y1], "label": "Text", "text": text}


@pytest.fixture
def stub_doc(monkeypatch):
    """Install a (doc, rev, meta) for ``_load_doc`` and a fetch for ``requests``."""
    def _install(blocks, *, body=None, filename="n1.png", url=URL, exc=None):
        meta = {"filename": filename, "public_url": url,
                "ocr_health_score": 55, "ocr_health_flags": ["missing-region"]}
        monkeypatch.setattr(_mod, "_load_doc",
                            lambda f: ({"blocks": blocks}, 7, meta))

        class _Resp:
            content = body or b""

            def raise_for_status(self):
                return None

        import requests
        def _get(*a, **k):
            if exc:
                raise exc
            return _Resp()
        monkeypatch.setattr(requests, "get", _get)
    return _install


def test_map_carries_base64_grids_sized_to_the_tile_grid(stub_doc):
    page = _page([(0.05, 0.10, 0.95, 0.55), (0.05, 0.60, 0.95, 0.95)])
    stub_doc([_block(0.03, 0.08, 0.97, 0.57)], body=page)

    out = _mod.ink_coverage("n1.png")

    assert out["flag"] is True                      # the lower band is unread
    assert out["blocks_revision"] == 7
    assert out["ocr_health_flags"] == ["missing-region"]
    ink = base64.b64decode(out["ink_b64"])
    covered = base64.b64decode(out["covered_b64"])
    n = out["tile_w"] * out["tile_h"]
    assert len(ink) == len(covered) == n
    # The UI's rule — inked and not covered — has to find the unread band.
    thresh = out["ink_min"] * 255
    unread = [i for i, v in enumerate(ink) if v >= thresh and not covered[i]]
    assert unread
    assert min(i // out["tile_w"] for i in unread) > out["tile_h"] * 0.5


def test_a_fully_covered_page_reports_no_flag_and_still_ships_a_map(stub_doc):
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    stub_doc([_block(0.05, 0.05, 0.95, 0.95)], body=page)

    out = _mod.ink_coverage("n1.png")

    assert out["flag"] is False
    assert out["uncovered_ratio"] == 0.0
    assert out["ink_b64"] and out["covered_b64"]


@pytest.mark.parametrize("kwargs, expected", [
    ({"url": None}, "no-public-url"),
    # Not a page in any renderer we have — unlike .pdf, which measures below.
    ({"filename": "n1.docx"}, "unsupported-source"),
])
def test_unmeasurable_sources_answer_with_a_reason_not_an_error(
        stub_doc, kwargs, expected):
    stub_doc([_block(0.1, 0.1, 0.9, 0.9)], body=_page([(0.1, 0.1, 0.9, 0.9)]),
             **kwargs)

    out = _mod.ink_coverage("n1.png")

    assert out["flag"] is False
    assert out["uncovered_ratio"] is None
    assert out["details"]["skipped"] == expected
    assert "ink_b64" not in out


def _as_pdf(raster: bytes) -> bytes:
    """``raster`` wrapped as a one-page PDF, rendering back to its own size."""
    fitz = pytest.importorskip("fitz", reason="PDF coverage needs PyMuPDF")
    from pipeline.ink_coverage import PDF_RENDER_SCALE
    doc = fitz.open()
    try:
        pg = doc.new_page(width=W / PDF_RENDER_SCALE, height=H / PDF_RENDER_SCALE)
        pg.insert_image(pg.rect, stream=raster)
        return doc.tobytes()
    finally:
        doc.close()


def test_a_pdf_notice_is_measured_like_a_scan(stub_doc):
    """The bug behind this: the endpoint refused every PDF outright, so the
    Ink tab on a PDF notice read "not measurable — unsupported-source" and no
    reviewer could see what the missing-region flag was scored on."""
    body = _as_pdf(_page([(0.05, 0.10, 0.95, 0.55), (0.05, 0.60, 0.95, 0.95)]))
    stub_doc([_block(0.03, 0.08, 0.97, 0.57)], body=body, filename="n1.pdf",
             url="https://r2.example/notices/n1.pdf")

    out = _mod.ink_coverage("n1.pdf")

    assert out["details"].get("skipped") is None
    assert out["flag"] is True                      # the lower band is unread
    ink = base64.b64decode(out["ink_b64"])
    covered = base64.b64decode(out["covered_b64"])
    assert len(ink) == len(covered) == out["tile_w"] * out["tile_h"]
    thresh = out["ink_min"] * 255
    unread = [i for i, v in enumerate(ink) if v >= thresh and not covered[i]]
    assert unread
    assert min(i // out["tile_w"] for i in unread) > out["tile_h"] * 0.5


def test_a_pdf_page_the_file_does_not_have_says_so(stub_doc):
    """Still a reason, never an error — the multi-page page picker can ask for
    a page the source ran out of."""
    stub_doc([_block(0.1, 0.1, 0.9, 0.9, page=3)],
             body=_as_pdf(_page([(0.1, 0.1, 0.9, 0.9)])), filename="n1.pdf",
             url="https://r2.example/notices/n1.pdf")

    out = _mod.ink_coverage("n1.pdf", 3)

    assert out["flag"] is False
    assert out["uncovered_ratio"] is None
    assert out["details"]["skipped"] == "page-out-of-range"


def test_a_failed_fetch_is_reported_not_raised(stub_doc):
    import requests
    stub_doc([_block(0.1, 0.1, 0.9, 0.9)], exc=requests.ConnectionError("boom"))

    out = _mod.ink_coverage("n1.png")

    assert out["flag"] is False
    assert out["details"]["skipped"].startswith("fetch-failed")


def test_an_oversized_source_is_refused_before_it_is_decoded(stub_doc,
                                                             monkeypatch):
    """The endpoint pulls the source into the API process, so the size cap is
    the thing standing between a bad ``public_url`` and the worker's memory."""
    monkeypatch.setattr(_mod, "INK_MAP_MAX_BYTES", 16)
    stub_doc([_block(0.1, 0.1, 0.9, 0.9)], body=_page([(0.1, 0.1, 0.9, 0.9)]))

    out = _mod.ink_coverage("n1.png")

    assert out["details"]["skipped"] == "source-too-large"
    assert "ink_b64" not in out


def test_a_page_with_no_blocks_is_unscorable_not_all_missing(stub_doc):
    """"No blocks" must never read as 100% missing — that is a different
    failure, visible upstream, and flagging it here would bury it."""
    stub_doc([], body=_page([(0.1, 0.1, 0.9, 0.9)]))

    out = _mod.ink_coverage("n1.png")

    assert out["flag"] is False
    assert out["uncovered_ratio"] is None
    assert out["details"]["skipped"] == "no-blocks"


def test_page_argument_selects_the_page_measured(stub_doc):
    """A block on page 1 says nothing about page 2, so asking for page 2 is
    unscorable rather than "everything on page 2 is missing"."""
    stub_doc([_block(0.05, 0.05, 0.95, 0.95, page=1)],
             body=_page([(0.1, 0.1, 0.9, 0.9)]))

    assert _mod.ink_coverage("n1.png", 1)["uncovered_ratio"] == 0.0
    out2 = _mod.ink_coverage("n1.png", 2)
    assert out2["page"] == 2
    assert out2["details"]["skipped"] == "no-blocks-on-page"
