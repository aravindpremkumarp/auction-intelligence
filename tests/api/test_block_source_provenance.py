"""
tests/api/test_block_source_provenance.py
-----------------------------------------
Tripwire for the Datalab blocks 400.

``pipeline/datalab.py:parse_datalab_blocks`` stamps every block it produces
with ``source="datalab"``. The API's ``Block`` response model declared
``Literal["mineru", "human"]``, so FastAPI rejected the whole blocks payload
for any Datalab-OCR'd notice and the annotator showed "load failed (400)" —
with no way to review or correct those documents.

This was silent for a while because the two OCR engines split cleanly by
document: MinerU-blocked notices validated fine, and only the Datalab ones
(661 documents at the time of the fix, written by
scripts/reocr_low_health_datalab.py and the --engine datalab path of
scripts/ocr_missing_markdowns.py) broke.

Widening the literal is the correct fix rather than rewriting stored blocks:
"datalab" is genuine provenance the annotator should be able to surface.
"""
from __future__ import annotations

import pytest

from api.review.router import Block


def _block(**over):
    base = {"id": "blk_1", "bbox": [0.1, 0.1, 0.5, 0.5], "label": "Text"}
    base.update(over)
    return base


@pytest.mark.parametrize("source", ["mineru", "datalab", "human"])
def test_block_accepts_every_real_provenance(source):
    """Each value is written by real code, so each must round-trip."""
    assert Block.model_validate(_block(source=source)).source == source


def test_block_defaults_to_mineru():
    assert Block.model_validate(_block()).source == "mineru"


def test_block_still_rejects_unknown_source():
    """The literal must stay closed — a typo'd engine name should not
    silently persist as provenance."""
    with pytest.raises(ValueError):
        Block.model_validate(_block(source="tesseract"))


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
