"""Targeted re-read and the key-facts vote, with a scripted model."""
from __future__ import annotations

from pipeline.reader import stability as ST
from pipeline.reader.convert import to_entities
from pipeline.reader.examples import SERIAL_ROWS_NOTICE, SERIAL_ROWS_SEGMENT, SERIAL_ROWS_TEXT
from pipeline.reader.schema import KeyFacts, KeyFactsRead, LotRead, NoticeRead, SegmentRead
from pipeline.reader.segment import chunks, segment
from pipeline.reader.tables import parse_tables

MD = SERIAL_ROWS_TEXT


def _base():
    seg = segment(MD)
    lots = SegmentRead.model_validate(SERIAL_ROWS_SEGMENT).lots
    conv = to_entities(NoticeRead.model_validate(SERIAL_ROWS_NOTICE),
                       [(seg.segments[0], lots[0]), (seg.segments[1], lots[1])], MD,
                       tables=parse_tables(MD))
    return seg, conv.entities, conv.not_stated


def _kf(lot: int, **over) -> dict:
    base = {1: dict(reserve_price={"status": "found", "quote": "Rs.26,83,000/-"},
                    emd={"status": "found", "quote": "Rs.2,68,300/-"},
                    auction_start={"status": "found", "quote": "11-08-2026 between 2.00 PM", "event": "auction_start", "iso": "2026-08-11T14:00"},
                    property_type={"status": "found", "quote": "house site"},
                    possession={"status": "found", "quote": "Type of possession: - Physical"},
                    extent_quote="1987.50 sq.feet", village="punjai kalamangalam", taluk="Modakurichi"),
            2: dict(reserve_price={"status": "found", "quote": "Rs.9,85,000/-"},
                    emd={"status": "found", "quote": "Rs.98,500/-"},
                    auction_start={"status": "found", "quote": "11-08-2026 between 2.00 PM", "event": "auction_start", "iso": "2026-08-11T14:00"},
                    property_type={"status": "not_stated"},
                    possession={"status": "found", "quote": "Type of possession: - Physical"},
                    extent_quote="Total Extent 1065 Sq.Ft.", village="Vadivilliputhiryenthal", taluk="Manamadurai")}[lot]
    base.update(over)
    return base


def test_uncertain_names_missing_keys_with_a_clue_and_contested_fields():
    seg, ents, ns = _base()
    u = ST.uncertain(ents, seg, MD, ns)
    # lot 2's property type is missing and its text has type words -> re-read
    assert "property_type" in u.get("2", [])
    t1 = next(e for e in ents if e["cls"] == "auction_terms" and e["attrs"]["lot_index"] == "1")
    t1["attrs"]["evidence"] = "CONTESTED"; t1["attrs"]["rule"] = "emd_num:emd_ratio_off"
    u = ST.uncertain(ents, seg, MD, ns)
    assert {"reserve_price", "emd"} <= set(u["1"])


def test_reread_replaces_only_the_named_fields():
    seg, ents, _ = _base()
    ch = chunks(seg, MD)[0]
    calls = []

    def call(system, user, schema, model=None):
        calls.append((schema.__name__, user))
        return schema.model_validate({"property_type": {"status": "found", "quote": "Natham"},
                                      "property_type_norm": "land"})
    before = [e for e in ents if e["attrs"].get("lot_index") == "2"]
    out, rep = ST.reread(ents, "2", ["property_type", "property_type_norm"], seg, ch, MD, call)
    assert calls[0][0] == "FieldReread" and "Fields needed: property_type" in calls[0][1]
    prop2 = [e for e in out if e["cls"] == "property" and e["attrs"]["lot_index"] == "2"]
    assert len(prop2) == 1 and prop2[0]["attrs"]["property_type"] == "Natham"
    assert prop2[0]["attrs"]["method"] == "field_reread"
    assert prop2[0]["attrs"]["possession_type"] == "physical"     # merged, not lost
    others_before = [e for e in before if e["cls"] != "property"]
    others_after = [e for e in out if e["attrs"].get("lot_index") == "2" and e["cls"] != "property"]
    assert len(others_before) == len(others_after)


def test_verify_agreement_marks_verified():
    seg, ents, _ = _base()
    ch = chunks(seg, MD)[0]
    reads = []

    def call(system, user, schema, model=None):
        reads.append(schema.__name__)
        return KeyFactsRead.model_validate({"lots": [_kf(1), _kf(2)]})
    rep = ST.verify(ents, seg, ch, MD, call)
    assert reads == ["KeyFactsRead"] and rep["contested"] == 0 and rep["reads"] == 1
    t1 = next(e for e in ents if e["cls"] == "auction_terms" and e["attrs"]["lot_index"] == "1")
    assert t1["attrs"]["evidence"] == "VERIFIED" and "reserve_price_num" in t1["attrs"]["verified"]


def test_verify_majority_replaces_a_value_only_when_it_is_on_the_page():
    seg, ents, _ = _base()
    ch = chunks(seg, MD)[0]
    t1 = next(e for e in ents if e["cls"] == "auction_terms" and e["attrs"]["lot_index"] == "1")
    t1["attrs"]["emd_num"] = "268000"                      # first read wrong by a digit
    n = {"i": 0}

    def call(system, user, schema, model=None):
        n["i"] += 1
        return KeyFactsRead.model_validate({"lots": [_kf(1), _kf(2)]})   # both checks say 268300
    rep = ST.verify(ents, seg, ch, MD, call)
    assert n["i"] == 2 and rep["voted"] == 1
    assert t1["attrs"]["emd_num"] == "268300" and t1["attrs"]["method"] == "verify_vote"


def test_verify_no_majority_is_contested():
    seg, ents, _ = _base()
    ch = chunks(seg, MD)[0]
    answers = [KeyFactsRead.model_validate({"lots": [_kf(1, emd={"status": "found", "quote": "Rs.2,68,000/-"}), _kf(2)]}),
               KeyFactsRead.model_validate({"lots": [_kf(1, emd={"status": "found", "quote": "Rs.2,69,300/-"}), _kf(2)]})]

    def call(system, user, schema, model=None):
        return answers.pop(0)
    rep = ST.verify(ents, seg, ch, MD, call)
    assert rep["contested"] == 1
    t1 = next(e for e in ents if e["cls"] == "auction_terms" and e["attrs"]["lot_index"] == "1")
    assert t1["attrs"]["evidence"] == "CONTESTED" and "emd_num:vote_disagreed" in t1["attrs"]["rule"]
    assert t1["attrs"]["emd_num"] == "268300"              # first read kept
