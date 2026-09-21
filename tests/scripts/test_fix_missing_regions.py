"""Page selection for the missing-region patch fixer.

The document's `missing-region` verdict is its worst page
(`pipeline.ink_coverage.score_document_ink`), so a multi-page PDF can be
flagged on page 4 while page 1 measures perfectly clean. The fixer used to
measure page 1 only and reported "re-measured clean" for exactly those
notices, which is how two flagged PDFs sat unfixable in the markdown queue.
These tests pin the page walk and the PDF/raster crop split.
"""
from __future__ import annotations

import pytest

import scripts.fix_missing_regions as fx


PDF = b"%PDF-1.4 fake"
PNG = b"\x89PNG\r\n\x1a\n fake"


def _blocks(*pages: int) -> list[dict]:
    return [{"id": f"blk_{p}", "page": p, "bbox": [0.0, 0.0, 0.5, 0.5]}
            for p in pages]


def _measure(per_page: dict[int, tuple[float, bool]]):
    """Stand in for score_ink_coverage: page -> (uncovered_ratio, flag)."""
    def fake(image_bytes, blocks, *, page=1):
        ratio, flag = per_page.get(page, (0.0, False))
        return {"uncovered_ratio": ratio, "flag": flag,
                "details": {"patch_bbox": [0.6, 0.9, 1.0, 1.0], "page": page}}
    return fake


# ── flagged_pages ───────────────────────────────────────────────────────────

def test_finds_the_flagged_page_of_a_multipage_pdf(monkeypatch):
    """The regression: page 1 clean, the dropped region on page 4."""
    monkeypatch.setattr(fx, "score_ink_coverage", _measure({
        1: (0.01, False), 2: (0.02, False), 3: (0.03, False), 4: (0.30, True),
    }))

    found = fx.flagged_pages(PDF, _blocks(1, 2, 3, 4))

    assert [p for p, _ in found] == [4]


def test_returns_every_flagged_page_worst_first(monkeypatch):
    """One page per run would leave the flag up and re-clear the sign-off."""
    monkeypatch.setattr(fx, "score_ink_coverage", _measure({
        1: (0.01, False), 2: (0.13, True), 3: (0.11, False), 4: (0.30, True),
    }))

    found = fx.flagged_pages(PDF, _blocks(1, 2, 3, 4))

    assert [p for p, _ in found] == [4, 2]


def test_clean_document_yields_no_pages(monkeypatch):
    monkeypatch.setattr(fx, "score_ink_coverage", _measure({
        1: (0.01, False), 2: (0.02, False),
    }))

    assert fx.flagged_pages(PDF, _blocks(1, 2)) == []


def test_a_page_without_a_patch_is_not_fixable(monkeypatch):
    """Flagged but no located patch: there is nothing to crop."""
    def fake(image_bytes, blocks, *, page=1):
        return {"uncovered_ratio": 0.3, "flag": True, "details": {}}
    monkeypatch.setattr(fx, "score_ink_coverage", fake)

    assert fx.flagged_pages(PDF, _blocks(1)) == []


def test_raster_is_measured_on_page_one_only(monkeypatch):
    """A PNG has one page whatever the blocks claim — never render page 3."""
    seen: list[int] = []

    def fake(image_bytes, blocks, *, page=1):
        seen.append(page)
        return {"uncovered_ratio": 0.3, "flag": True,
                "details": {"patch_bbox": [0.1, 0.1, 0.2, 0.2]}}
    monkeypatch.setattr(fx, "score_ink_coverage", fake)

    found = fx.flagged_pages(PNG, _blocks(1, 3))

    assert seen == [1]
    assert [p for p, _ in found] == [1]


# ── _crop_png: the split Pillow cannot cross ────────────────────────────────

def test_pdf_crops_through_fitz_with_the_page(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(fx, "_pdf_crop_to_png",
                        lambda b, p, box: calls.append((p, box)) or b"pdfpng")
    monkeypatch.setattr(fx, "_image_crop_to_png",
                        lambda b, box: pytest.fail("Pillow cannot open a PDF"))

    assert fx._crop_png(PDF, 4, [0.6, 0.9, 1.0, 1.0]) == b"pdfpng"
    assert calls == [(4, [0.6, 0.9, 1.0, 1.0])]


def test_raster_crops_through_pillow(monkeypatch):
    monkeypatch.setattr(fx, "_image_crop_to_png", lambda b, box: b"imgpng")
    monkeypatch.setattr(fx, "_pdf_crop_to_png",
                        lambda b, p, box: pytest.fail("not a PDF"))

    assert fx._crop_png(PNG, 1, [0.1, 0.1, 0.2, 0.2]) == b"imgpng"
