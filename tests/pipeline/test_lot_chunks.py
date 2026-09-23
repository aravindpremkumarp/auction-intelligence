"""Chunked extraction for long multi-lot notices (pipeline/lot_chunks.py)."""
from __future__ import annotations

import re

from pipeline.lot_chunks import extract_chunked, plan_chunks


def _notice(n: int) -> str:
    lots = [f"No.{i} Borrower B{i}. Property at Sy No {i}/1.\n"
            f"Reserve price: Rs.{i},00,000/- | EMD: Rs.{i}0,000/-\n"
            for i in range(1, n + 1)]
    return ("# SALE NOTICE\nCanara Bank\n\n" + "\n".join(lots)
            + "\nAuction date 24.06.2026. Bids not below the reserve price.\n")


def test_plan_cuts_after_every_k_price_lines():
    md = _notice(12)
    plan = plan_chunks(md, 12, lots_per_chunk=5)
    assert [c.lots for c in plan.chunks] == [5, 5, 2]
    # chunks tile the lot text with no gap and no overlap
    assert plan.chunks[0].start == 0
    for a, b in zip(plan.chunks, plan.chunks[1:]):
        assert a.end == b.start
    assert plan.chunks[-1].end == plan.tail_start
    assert md[plan.tail_start:].lstrip().startswith("Auction date")


def test_plan_ignores_prose_mentions_of_reserve_price():
    # "not below the reserve price" in the tail has no amount → not a lot
    assert plan_chunks(_notice(12), 12) is not None


def test_plan_declines_when_price_lines_do_not_match_the_count():
    assert plan_chunks(_notice(12), 13) is None
    assert plan_chunks(_notice(12), None) is None


def test_plan_declines_short_notices():
    assert plan_chunks(_notice(4), 4, lots_per_chunk=5) is None


def _fake_read(text: str, lots: int) -> list[dict]:
    """Stands in for the model: tags each 'No.N' block with a LOCAL index
    1..lots and one notice-level date from the tail."""
    out = []
    for local, m in enumerate(re.finditer(r"No\.(\d+)", text), 1):
        out.append({"id": "x", "cls": "borrower", "text": m.group(0),
                    "start": m.start(), "end": m.end(),
                    "attrs": {"lot_index": str(local), "real": m.group(1)}})
    d = text.find("24.06.2026")
    out.append({"id": "x", "cls": "auction_date", "text": "24.06.2026",
                "start": d, "end": d + 10, "attrs": {}})
    return out


def test_stitch_gives_global_indices_and_full_notice_offsets():
    md = _notice(12)
    plan = plan_chunks(md, 12, lots_per_chunk=5)
    ents = extract_chunked(md, plan, _fake_read)
    lots = [e for e in ents if e["cls"] == "borrower"]
    assert [e["attrs"]["lot_index"] for e in lots] == [str(i) for i in range(1, 13)]
    assert all(e["attrs"]["lot_index"] == e["attrs"]["real"] for e in lots)
    assert all(md[e["start"]:e["end"]] == e["text"] for e in ents)


def test_stitch_keeps_tail_entities_once():
    md = _notice(12)
    ents = extract_chunked(md, plan_chunks(md, 12, lots_per_chunk=5), _fake_read)
    dates = [e for e in ents if e["cls"] == "auction_date"]
    assert len(dates) == 1
    assert md[dates[0]["start"]:dates[0]["end"]] == "24.06.2026"
    assert [e["id"] for e in ents] == [str(i) for i in range(len(ents))]
