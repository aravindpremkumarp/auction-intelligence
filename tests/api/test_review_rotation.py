"""
tests/api/test_review_rotation.py
---------------------------------
Unit tests for the rotation helpers in :mod:`api.review.blocks`.

These cover the bbox math that's easy to get wrong (the 90 vs 270 formulas
are easy to swap) and the round-trip invariant between forward-rotate and
un-rotate. All bboxes are normalized ``[x0,y0,x1,y1]`` with each value in
``[0,1]``.
"""
from __future__ import annotations

import math

import pytest

# Import the helpers directly from the source file so we don't trigger
# ``api.review.__init__``'s heavy imports (fastapi, jwt, cryptography) just
# to test pure-function bbox math.
import importlib.util
from pathlib import Path

_BLOCKS_PATH = Path(__file__).resolve().parents[2] / "api" / "review" / "blocks.py"
_spec = importlib.util.spec_from_file_location("_blocks_under_test", _BLOCKS_PATH)
_mod = importlib.util.module_from_spec(_spec)
# blocks.py imports from api.neo4j_client and pipeline.mineru — stub those
# so the module can load without a live DB or MinerU client.
import sys
import types
_stub_neo4j = types.ModuleType("api.neo4j_client")
_stub_neo4j.run_query = lambda *a, **k: None
_stub_neo4j.run_read_query = lambda *a, **k: None
sys.modules.setdefault("api", types.ModuleType("api"))
sys.modules["api.neo4j_client"] = _stub_neo4j
_stub_mineru = types.ModuleType("pipeline.mineru")
_stub_mineru.DEFAULT_LABEL = "Text"
_stub_mineru.MINERU_LABEL_VALUES = ["Text", "Title", "Table"]
_stub_mineru.assemble_markdown = lambda blocks: ""
sys.modules.setdefault("pipeline", types.ModuleType("pipeline"))
sys.modules["pipeline.mineru"] = _stub_mineru
_spec.loader.exec_module(_mod)

_clean_rotation = _mod._clean_rotation
_rotate_bbox_forward = _mod._rotate_bbox_forward
_un_rotate_bbox = _mod._un_rotate_bbox


# Small box hugging the top-left of the source (where "A" would be in
# an upright photo). After 90° CW rotation it should sit at the top-right.
TL_BOX = [0.1, 0.1, 0.3, 0.2]


class TestCleanRotation:
    @pytest.mark.parametrize("raw,expected", [
        (0, 0), (90, 90), (180, 180), (270, 270),
        (None, 0),
        (360, 0), (450, 90),         # mod-360
        (-90, 270), (-180, 180),     # negative wraps
        ("90", 90), ("270", 270),    # strings coerce
    ])
    def test_accepts(self, raw, expected):
        assert _clean_rotation(raw) == expected

    @pytest.mark.parametrize("raw", [45, 91, "foo", [], {}])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            _clean_rotation(raw)


class TestRotateBboxForward:
    def test_zero_is_identity(self):
        assert _rotate_bbox_forward(TL_BOX, 0) == TL_BOX

    def test_90_moves_tl_to_tr(self):
        # Top-left of the original photo ends up at top-right of a 90° CW
        # rotated photo, so a TL box maps to a TR box.
        out = _rotate_bbox_forward(TL_BOX, 90)
        # Expect: [1-y1, x0, 1-y0, x1] = [0.8, 0.1, 0.9, 0.3]
        assert _approx_eq(out, [0.8, 0.1, 0.9, 0.3])

    def test_180_flips_to_br(self):
        out = _rotate_bbox_forward(TL_BOX, 180)
        # Expect: [1-x1, 1-y1, 1-x0, 1-y0] = [0.7, 0.8, 0.9, 0.9]
        assert _approx_eq(out, [0.7, 0.8, 0.9, 0.9])

    def test_270_moves_tl_to_bl(self):
        # Top-left rotated 270° CW (= 90° CCW) ends up at bottom-left.
        out = _rotate_bbox_forward(TL_BOX, 270)
        # Expect: [y0, 1-x1, y1, 1-x0] = [0.1, 0.7, 0.2, 0.9]
        assert _approx_eq(out, [0.1, 0.7, 0.2, 0.9])

    def test_four_90s_is_identity(self):
        # Composing 4 × 90° CW rotations should return the original.
        b = list(TL_BOX)
        for _ in range(4):
            b = _rotate_bbox_forward(b, 90)
        assert _approx_eq(b, TL_BOX)


class TestUnRotateBboxRoundTrip:
    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    @pytest.mark.parametrize("bbox", [
        [0.1, 0.1, 0.3, 0.2],         # TL-anchored
        [0.7, 0.6, 0.95, 0.9],        # BR-anchored
        [0.0, 0.0, 1.0, 1.0],         # full-image
        [0.4, 0.45, 0.5, 0.55],       # near-center
    ])
    def test_forward_then_un_returns_original(self, rotation, bbox):
        rotated = _rotate_bbox_forward(bbox, rotation)
        recovered = _un_rotate_bbox(rotated, rotation)
        assert _approx_eq(recovered, bbox), (
            f"rotation={rotation} bbox={bbox} rotated={rotated} recovered={recovered}"
        )


def _approx_eq(a: list[float], b: list[float], tol: float = 1e-9) -> bool:
    return len(a) == len(b) and all(math.isclose(x, y, abs_tol=tol)
                                    for x, y in zip(a, b))
