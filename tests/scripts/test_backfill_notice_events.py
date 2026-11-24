"""`:NoticeEvent` (a notice's dated events) must stay clear of `:AuctionEvent`
(the spine's merged record), which build_spine drops on every run."""
import json

from pipeline import extraction_store
from scripts import backfill_notice_events as B
from scripts import build_spine, link_reauctions


def test_timeline_rows_keeps_named_events_and_fills_every_field():
    docs = [
        {"fn": "a.pdf", "timeline": json.dumps([
            {"event": "auction_start", "date": "2026-10-15", "lot_index": "1"},
            {"date": "2026-10-01"},            # no event name: skipped
            "not a dict",
        ])},
        {"fn": "b.pdf", "timeline": "{broken"},
        {"fn": "c.pdf", "timeline": json.dumps({"event": "x"})},   # not a list
        {"fn": "d.pdf", "timeline": "[]"},
        {"fn": "e.pdf", "timeline": None},
    ]
    rows = B.timeline_rows(docs)
    assert [r["fn"] for r in rows] == ["a.pdf"]
    (ev,) = rows[0]["events"]
    assert ev == {"event": "auction_start", "date": "2026-10-15", "quote": None,
                  "lot_index": "1", "evidence": None, "source": None, "page": None}


def test_dry_run_writes_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(B, "run_query", lambda *a, **k: writes.append(a) or [])
    monkeypatch.setattr(B, "run_read_query", lambda c, *a, **k: (
        [{"n": 3}] if "count(e)" in c
        else [{"fn": "a.pdf", "timeline": json.dumps([{"event": "auction_start"}])}]))
    assert B.run(dry_run=True) == 0
    assert writes == []


def test_run_rebuilds_then_deletes_only_legacy_timeline_nodes(monkeypatch):
    writes: list = []
    monkeypatch.setattr(B, "run_query",
                        lambda c, p=None: writes.append((c, p)) or [{"n": 1}])
    monkeypatch.setattr(B, "run_read_query", lambda c, *a, **k: (
        [{"n": 1}] if "count(e)" in c
        else [{"fn": "a.pdf", "timeline": json.dumps([{"event": "auction_start"}])}]))
    assert B.run() == 0
    (write, params), (drop, _) = writes
    assert ":NoticeEvent" in write and ":AuctionEvent" not in write
    assert params["rows"][0]["fn"] == "a.pdf"
    # a spine record always has an event_id, so this can never delete one
    assert "e.event_id IS NULL" in drop and "e.event IS NOT NULL" in drop


def test_writers_keep_the_two_labels_apart():
    assert ":NoticeEvent" in extraction_store._EVENTS
    assert ":AuctionEvent" not in extraction_store._EVENTS
    assert "NoticeEvent" not in build_spine.DROP_EVENTS
    assert "NoticeEvent" not in link_reauctions.FETCH_EVENTS
