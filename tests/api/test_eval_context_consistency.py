"""The eval's per-notice prompt context must agree with the gold it scores.

evals/fixtures/notice_context.json carries the reviewer's ``expected_lot_count``
for each gold notice, and the eval sends it into the prompt ("EXACTLY N lots").
When it disagrees with the gold's own lot count the eval is measuring a prompt
that tells the model the wrong answer — for months it said 2 lots for 749433
(gold: 1), 5 for 750348 (gold: 6) and 2 for 755527 (gold: single). Every gold
notice with a context entry must match, and the strata manifest must know
every gold notice.
"""
from __future__ import annotations

import json

import pytest

from evals import langextract_eval as LE
from evals.langextract_gold import GOLD


def _gold_lot_count(g: dict) -> int:
    return len(g["lots"]) if g.get("lots") else 1


@pytest.mark.parametrize("g", GOLD, ids=[g["aid"] for g in GOLD])
def test_context_lot_count_matches_gold(g):
    ctx = LE.load_notice_context()
    c = ctx.get(g["aid"])
    if not c or c.get("expected_lot_count") is None:
        pytest.skip("no context for this notice")
    assert c["expected_lot_count"] == _gold_lot_count(g), (
        f"{g['aid']}: context says {c['expected_lot_count']} lot(s), "
        f"gold has {_gold_lot_count(g)}")


def test_manifest_covers_every_gold_notice():
    manifest = LE.load_manifest()
    vocab = set(json.loads(LE.GOLD_MANIFEST.read_text(encoding="utf-8"))
                ["strata_vocabulary"])
    for g in GOLD:
        entry = manifest.get(g["aid"])
        assert entry, f"{g['aid']} missing from evals/gold_manifest.json"
        assert entry["strata"], f"{g['aid']} has no strata"
        assert (g["notice_type"] in entry["strata"]), (
            f"{g['aid']}: strata must include its notice_type")
        unknown = set(entry["strata"]) - vocab
        assert not unknown, f"{g['aid']}: strata outside the vocabulary: {unknown}"


def test_expect_null_round_trips_through_json():
    g = LE._decode_gold({"aid": "x", "fields": {"possession_type": LE.EXPECT_NULL_JSON,
                                                  "village": "Kelambakkam"}})
    assert g["fields"]["possession_type"] is LE.EXPECT_NULL
    assert g["fields"]["village"] == "Kelambakkam"
