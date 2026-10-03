"""read_notice end to end with a scripted model."""
from __future__ import annotations

import pytest

from pipeline.reader import EmptyRead, read_notice
from pipeline.reader.examples import SERIAL_ROWS_NOTICE, SERIAL_ROWS_SEGMENT, SERIAL_ROWS_TEXT
from pipeline.reader.llm import Truncated
from pipeline.reader.schema import KeyFactsRead, LotRead, NoticeRead, SegmentRead

KF = {"lots": [
    dict(reserve_price={"status": "found", "quote": "Rs.26,83,000/-"}, emd={"status": "found", "quote": "Rs.2,68,300/-"},
         auction_start={"status": "found", "quote": "11-08-2026 between 2.00 PM", "event": "auction_start", "iso": "2026-08-11T14:00"},
         property_type={"status": "found", "quote": "house site"}, possession={"status": "found", "quote": "Type of possession: - Physical"},
         extent_quote="1987.50 sq.feet", village="punjai kalamangalam", taluk="Modakurichi"),
    dict(reserve_price={"status": "found", "quote": "Rs.9,85,000/-"}, emd={"status": "found", "quote": "Rs.98,500/-"},
         auction_start={"status": "found", "quote": "11-08-2026 between 2.00 PM", "event": "auction_start", "iso": "2026-08-11T14:00"},
         property_type={"status": "not_stated"}, possession={"status": "found", "quote": "Type of possession: - Physical"},
         extent_quote="Total Extent 1065 Sq.Ft.", village="Vadivilliputhiryenthal", taluk="Manamadurai")]}


def _scripted(log):
    def call(system, user, schema, model=None):
        log.append((schema.__name__, model))
        if schema is SegmentRead:
            return SegmentRead.model_validate(SERIAL_ROWS_SEGMENT)
        if schema is NoticeRead:
            return NoticeRead.model_validate(SERIAL_ROWS_NOTICE)
        if schema is KeyFactsRead:
            return KeyFactsRead.model_validate(KF)
        return schema.model_validate({})           # FieldReread: still not stated
    return call


def test_reads_a_two_lot_notice_clean():
    log = []
    r = read_notice(SERIAL_ROWS_TEXT, call=_scripted(log), model_id="fake")
    kinds = [k for k, _ in log]
    assert kinds[:2] == ["SegmentRead", "NoticeRead"]
    assert "KeyFactsRead" in kinds and "LotBoundaries" not in kinds
    lots = {e["attrs"].get("lot_index") for e in r.entities} - {None}
    assert lots == {"1", "2"}
    assert all(e["start"] is not None for e in r.entities)
    assert all(SERIAL_ROWS_TEXT[e["start"]:e["end"]] == e["text"] for e in r.entities)
    assert len({e["id"] for e in r.entities}) == len(r.entities)
    assert r.segmentation["strategy"] == "serial" and r.segmentation["lots"] == 2
    assert ("2", "property_type") in r.marks
    assert r.telemetry["key_facts"]["filled"] == 13 and r.telemetry["dropped"] == 0
    assert r.telemetry["key_facts"]["verified"] >= 10
    assert r.prompt_hash.startswith("v2-s") and r.model == "fake"
    assert any(x["event"] == "auction_start" and x["evidence"] == "INHERITED" for x in r.timeline)


def test_stability_none_makes_no_check_calls():
    log = []
    read_notice(SERIAL_ROWS_TEXT, call=_scripted(log), model_id="fake", stability="none")
    assert [k for k, _ in log] == ["SegmentRead", "NoticeRead"]


def test_empty_read_on_a_money_chunk_retries_then_raises():
    log = []

    def call(system, user, schema, model=None):
        log.append((schema.__name__, model))
        if schema is SegmentRead:
            return SegmentRead(lots=[])
        return schema.model_validate({})
    with pytest.raises(EmptyRead):
        read_notice(SERIAL_ROWS_TEXT, call=call, model_id="fake", stability="none")
    assert [m for k, m in log if k == "SegmentRead"] == [None, "deepseek/deepseek-v4-pro"]


def test_truncation_halves_the_chunk():
    md = SERIAL_ROWS_TEXT
    seen = []

    def call(system, user, schema, model=None):
        if schema is SegmentRead:
            seen.append(len(user))
            if len(seen) == 1:
                raise Truncated("too long")
            lots = SERIAL_ROWS_SEGMENT["lots"]
            which = [0] if "MR. GOKULNATH" in user else [1]
            return SegmentRead.model_validate({"lots": [lots[i] for i in which]})
        if schema is NoticeRead:
            return NoticeRead.model_validate(SERIAL_ROWS_NOTICE)
        return schema.model_validate({})
    r = read_notice(md, call=call, model_id="fake", stability="none")
    assert len(seen) == 3                                     # 1 truncated + 2 halves
    assert {e["attrs"].get("lot_index") for e in r.entities} - {None} == {"1", "2"}
