"""scripts/resolve_lots.py: the pure decision -> resolved_lot_key mapping.

`apply_decided`/`run` talk to Neo4j directly and aren't exercised here (same
as the other scripts/resolve_*.py drivers); `_approved_lot_keys` is plain
logic over an already-loaded decision list, so it's tested like one.
"""
from __future__ import annotations

from scripts.resolve_lots import _approved_lot_keys


def test_only_approved_lot_match_decisions_count():
    decisions = [
        {"kind": "lot-match", "verdict": "approved",
         "payload": {"auction_id": "796269", "lot_key": "notice.jpg#3"}},
        {"kind": "lot-match", "verdict": "rejected",
         "payload": {"auction_id": "700001", "lot_key": "notice2.jpg#1"}},
        {"kind": "bank-merge", "verdict": "approved",
         "payload": {"a": "X", "b": "Y"}},
    ]
    assert _approved_lot_keys(decisions) == {"796269": "notice.jpg#3"}


def test_a_decision_missing_either_field_is_skipped_not_an_error():
    decisions = [
        {"kind": "lot-match", "verdict": "approved",
         "payload": {"auction_id": "796269"}},
        {"kind": "lot-match", "verdict": "approved", "payload": {}},
    ]
    assert _approved_lot_keys(decisions) == {}


def test_no_decisions_is_an_empty_map():
    assert _approved_lot_keys([]) == {}
