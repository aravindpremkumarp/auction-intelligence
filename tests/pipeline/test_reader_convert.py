"""The form -> extraction_json: every value located inside its lot, the
checklist filled, inherited facts marked, nothing invented."""
from __future__ import annotations

from pipeline.key_entities import key_checklist
from pipeline.reader.convert import to_entities
from pipeline.reader.examples import (SERIAL_ROWS_NOTICE, SERIAL_ROWS_SEGMENT,
                                      SERIAL_ROWS_TEXT, SINGLE_NOTICE, SINGLE_SEGMENT,
                                      SINGLE_TEXT)
from pipeline.reader.schema import LotRead, NoticeRead, SegmentRead
from pipeline.reader.segment import Segment, segment
from pipeline.reader.tables import parse_tables
from pipeline.validators import validate_stored

LOT_CLASSES = {"borrower", "property", "full_description", "location", "identifier", "extent",
               "boundary", "schedule", "auction_terms", "outstanding", "extras"}


def _convert(text, seg_dict, notice_dict, expected=None):
    seg = segment(text, expected)
    lots = SegmentRead.model_validate(seg_dict).lots
    assert len(seg.segments) == len(lots) or seg.whole, seg
    pairs = [(seg.segments[i] if not seg.whole else seg.segments[0], lot) for i, lot in enumerate(lots)]
    conv = to_entities(NoticeRead.model_validate(notice_dict), pairs, text,
                       tables=parse_tables(text), prompt_hash="v2-test",
                       head_end=seg.head_end or None, tail_start=seg.tail_start if not seg.whole else None)
    return seg, conv


def test_serial_rows_example_converts_clean():
    seg, conv = _convert(SERIAL_ROWS_TEXT, SERIAL_ROWS_SEGMENT, SERIAL_ROWS_NOTICE)
    ents = conv.entities
    assert seg.strategy == "serial" and len(seg.segments) == 2
    assert all(e["start"] is not None for e in ents)                          # 0 ungrounded
    assert all(SERIAL_ROWS_TEXT[e["start"]:e["end"]] == e["text"] for e in ents)
    for e in ents:
        if e["cls"] in LOT_CLASSES:
            s = seg.segments[int(e["attrs"]["lot_index"]) - 1]
            assert s.start <= e["start"] and e["end"] <= s.end, (e["cls"], e["text"])
        for k in ("evidence", "anchor", "source", "method", "reader", "schema_version", "prompt_hash"):
            assert k in e["attrs"], (e["cls"], k)
    v = validate_stored(ents, SERIAL_ROWS_TEXT)
    # lot_under_recall counts "S.No.281/26" survey numbers as lot markers — the
    # review queue ignores that code (api/review/extraction.py); nothing else may fire.
    assert {i["code"] for i in v["issues"]} <= {"lot_under_recall"}, v["issues"]
    keys = key_checklist(ents, None, 2)
    lot1 = keys["lots"][0]["cells"]
    assert all(c["status"] == "filled" for c in lot1.values()), lot1
    lot2 = keys["lots"][1]["cells"]
    assert lot2["property_type"]["status"] == "missing" and ("2", "property_type") in conv.not_stated
    assert sum(1 for k, c in lot2.items() if c["status"] == "filled") == 6
    # the shared auction date reached both lots, marked as inherited
    terms = [e for e in ents if e["cls"] == "auction_terms"]
    assert [t["attrs"]["auction_start_dt"] for t in terms] == ["2026-08-11T14:00"] * 2
    assert all("auction_start_dt" in t["attrs"]["inherited"] for t in terms)
    assert all(t["attrs"]["inherited_from"] == "notice.shared" for t in terms)
    assert {x["evidence"] for x in conv.timeline if x["event"] == "auction_start"} == {"INHERITED"}
    assert [t["attrs"]["reserve_price_num"] for t in terms] == ["2683000", "985000"]
    assert [t["attrs"]["emd_num"] for t in terms] == ["268300", "98500"]
    # the full description covers every granular span of its lot
    for li in ("1", "2"):
        fd = next(e for e in ents if e["cls"] == "full_description" and e["attrs"]["lot_index"] == li)
        for e in ents:
            if e["attrs"].get("lot_index") == li and e["cls"] in ("identifier", "extent", "boundary", "location"):
                assert fd["start"] <= e["start"] and e["end"] <= fd["end"], (li, e["cls"], e["text"])
    loc2 = next(e for e in ents if e["cls"] == "location" and e["attrs"]["lot_index"] == "2")
    assert "district" not in loc2["attrs"] and loc2["attrs"]["registration_district"] == "Virudhunagar"
    assert conv.dropped == []


def test_single_example_converts_clean():
    seg, conv = _convert(SINGLE_TEXT, SINGLE_SEGMENT, SINGLE_NOTICE, expected=1)
    ents = conv.entities
    assert seg.whole
    assert validate_stored(ents, SINGLE_TEXT)["score"] == 100
    keys = key_checklist(ents, None, 1)
    assert all(c["status"] == "filled" for c in keys["lots"][0]["cells"].values())
    prop = next(e for e in ents if e["cls"] == "property")
    assert prop["attrs"]["possession_type"] == "physical" and "inherited" not in prop["attrs"]
    sc = next(e for e in ents if e["cls"] == "secured_creditor")
    assert sc["attrs"]["bank_name"] == "Bank of Baroda" and sc["attrs"]["notice_date"] == "2026-03-26"
    assert sc["attrs"]["source"] == "header"
    scheds = [e for e in ents if e["cls"] == "schedule"]
    assert len(scheds) == 2 and scheds[0]["text"].startswith("Item No 1")
    assert any(e["cls"] == "contact" and e["attrs"]["phones"] == "04328-225080" for e in ents)
    assert conv.dropped == []


def test_unlocatable_quotes_are_dropped_and_inferred_places_removed():
    seg_dict = {"lots": [{**SINGLE_SEGMENT["lots"][0],
                          "identifiers": SINGLE_SEGMENT["lots"][0]["identifiers"] + [
                              {"kind": "patta", "value": "999", "quote": "Patta No 999 nowhere"}],
                          "location": {**SINGLE_SEGMENT["lots"][0]["location"], "state": "Tamil Nadu"},
                          "reserve_price": {"status": "found", "quote": "Rs.9,5O,000/-"}}]}
    seg, conv = _convert(SINGLE_TEXT, seg_dict, SINGLE_NOTICE, expected=1)
    assert any(d["cls"] == "identifier" and d["quote"].startswith("Patta No 999") for d in conv.dropped)
    loc = next(e for e in conv.entities if e["cls"] == "location")
    assert "state" not in loc["attrs"] and loc["attrs"]["inferred_dropped"] == "state"
    terms = next(e for e in conv.entities if e["cls"] == "auction_terms")
    assert "reserve_price_num" not in terms["attrs"]
    assert terms["attrs"]["reserve_price_state"] == "illegible" and terms["attrs"]["evidence"] == "ILLEGIBLE"
    assert ("1", "reserve_price") in conv.illegible


def test_whole_read_windows_are_fenced_by_descriptions():
    md = SERIAL_ROWS_TEXT
    lots = SegmentRead.model_validate(SERIAL_ROWS_SEGMENT).lots
    whole = Segment(0, len(md), 0)
    conv = to_entities(None, [(whole, lots[0]), (whole, lots[1])], md, tables=parse_tables(md))
    terms = [e for e in conv.entities if e["cls"] == "auction_terms"]
    assert [t["attrs"]["lot_index"] for t in terms] == ["1", "2"]
    assert [t["attrs"]["reserve_price_num"] for t in terms] == ["2683000", "985000"]
    b2 = [e for e in conv.entities if e["cls"] == "borrower" and e["attrs"]["lot_index"] == "2"]
    assert b2 and b2[0]["start"] > md.index("Description of the Immovable Property: All that piece and parcel of the Erode")


def test_a_figure_the_model_called_illegible_is_still_repaired_by_code():
    lot = {**SINGLE_SEGMENT["lots"][0],
           "reserve_price": {"status": "illegible", "quote": "Rs.9,50,000/-"}}
    seg, conv = _convert(SINGLE_TEXT, {"lots": [lot]}, SINGLE_NOTICE, expected=1)
    t = next(e for e in conv.entities if e["cls"] == "auction_terms")
    assert t["attrs"]["reserve_price_num"] == "950000"
    assert "reserve_price_state" not in t["attrs"] and t["attrs"]["evidence"] != "ILLEGIBLE"
