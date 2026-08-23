"""
tests/api/test_block_source_provenance.py
-----------------------------------------
Tripwire for the blocks 400 that has now bricked the annotator twice.

``Block.source`` is pure provenance — the annotator only renders it as a pill
and checks whether it equals "human". But it used to be declared as a closed
``Literal``, so any value the literal had not caught up with raised a pydantic
``ValidationError``. ``_wrap_block_errors`` maps ``ValueError`` to HTTP 400,
and ``BlocksDoc`` validates the block LIST — so one unlisted block 400s the
whole document and the annotator shows "load failed (400)".

It happened twice, both times locking reviewers out of precisely the notices
that most needed hand-correction:

* ``"datalab"``  — 661 notices, stamped by ``pipeline/datalab.py``.
  Fixed by widening the literal.
* ``"datalab-patchfix"`` — 61 notices, stamped by
  ``scripts/fix_missing_regions.py``. Widening had not helped: the literal was
  the bug.

So the read model is now an open ``str``, and the allowlist lives on the write
path as ``blocks.KNOWN_BLOCK_SOURCES``. These tests pin both halves: reads
must never reject a provenance, and every value real code writes must still be
a known one.
"""
from __future__ import annotations

import pytest

from api.review.blocks import KNOWN_BLOCK_SOURCES, _clean_source
from api.review.router import Block


def _block(**over):
    base = {"id": "blk_1", "bbox": [0.1, 0.1, 0.5, 0.5], "label": "Text"}
    base.update(over)
    return base


@pytest.mark.parametrize("source", KNOWN_BLOCK_SOURCES)
def test_block_accepts_every_real_provenance(source):
    """Each value is written by real code, so each must round-trip."""
    assert Block.model_validate(_block(source=source)).source == source


def test_block_defaults_to_mineru():
    assert Block.model_validate(_block()).source == "mineru"


def test_read_model_never_rejects_an_unknown_source():
    """The regression itself: a provenance the code has not seen before must
    load, not 400 the document. Losing the pill's accuracy is survivable;
    locking a reviewer out of the notice is not."""
    blk = Block.model_validate(_block(source="some-future-engine"))
    assert blk.source == "some-future-engine"


def test_patchfix_blocks_load_alongside_datalab_blocks():
    """The exact shape that broke: one document mixing Datalab blocks with the
    patch-fix blocks scripts/fix_missing_regions.py appends."""
    from api.review.router import BlocksDoc

    doc = BlocksDoc.model_validate({
        "filename": "notice.jpg",
        "blocks": [
            _block(id="blk_a", source="datalab"),
            _block(id="blk_b", source="datalab-patchfix"),
        ],
    })
    assert [b.source for b in doc.blocks] == ["datalab", "datalab-patchfix"]


# ── write path: the allowlist that replaced the literal ─────────────────────

@pytest.mark.parametrize("source", KNOWN_BLOCK_SOURCES)
def test_clean_source_preserves_every_known_provenance(source):
    """A full replace (undo/redo) must not relabel machine OCR as human —
    that would brand it reviewer-verified, the opposite of the truth. This is
    what silently inverted the review signal for the Datalab blocks."""
    assert _clean_source(source) == source


@pytest.mark.parametrize("raw", [None, "", "   ", 7, [], {}, "tesseract"])
def test_clean_source_falls_back_to_human(raw):
    """The write allowlist stays closed: replace-all is not a passthrough for
    arbitrary provenance. Missing/blank is a reviewer-drawn block."""
    assert _clean_source(raw) == "human"


def test_every_writer_stamps_a_known_source():
    """Guard the real contract. If a new writer appears, it belongs in
    KNOWN_BLOCK_SOURCES — the read path tolerates it either way, but the
    allowlist is how we keep track of what provenance actually means."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    written: set[str] = set()
    for rel in ("pipeline/mineru.py", "pipeline/datalab.py",
                "scripts/fix_missing_regions.py", "api/review/blocks.py"):
        src = (root / rel).read_text()
        written |= set(re.findall(r'"source"\]?\s*[:=]\s*"([a-z0-9-]+)"', src))

    unknown = written - set(KNOWN_BLOCK_SOURCES)
    assert not unknown, (
        f"{sorted(unknown)} is written to a block's `source` but is not in "
        "KNOWN_BLOCK_SOURCES — add it there (the read path already tolerates "
        "it; this list is the record of what provenance values mean)."
    )


def test_datalab_parser_output_validates():
    """Guard the actual contract: whatever parse_datalab_blocks stamps as
    `source` must be a value the response model accepts."""
    from pipeline.datalab import parse_datalab_blocks

    doc = {
        "children": [{
            "block_type": "Page",
            "bbox": [0, 0, 100, 100],
            "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            "children": [{
                "block_type": "Text",
                "html": "<p>reserve price Rs. 10,00,000</p>",
                "bbox": [10, 10, 90, 30],
                "polygon": [[10, 10], [90, 10], [90, 30], [10, 30]],
                "children": [],
            }],
        }],
    }
    blocks = parse_datalab_blocks(doc)
    assert blocks, "parser produced no blocks — fixture shape drifted"
    for b in blocks:
        b.setdefault("id", "blk_x")
        Block.model_validate(b)
