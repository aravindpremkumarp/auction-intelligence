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


# ── the auto-resolver is retired ────────────────────────────────────────────
# resolve_lots used to auto-match undecided listings on reserve price +
# borrower name and write `resolved_lot_key` as `system:auto`. It had no
# rivalry gate, and the corpus shows what that cost: of the 100 keys it ever
# wrote, 96 sat on a lot another listing also claimed — 116 listings across
# 50 lots, one lot claimed by seven. Two listings cannot both be that lot.
#
# pipeline/apply_extractions.py::write_lot_matches is the single resolver now:
# more evidence, read off the live extraction, and `sole_claimants` refuses to
# write a lot two listings claim. These pin the retirement so a future edit
# cannot quietly restore a second writer.

def _source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2]
            / "scripts" / "resolve_lots.py").read_text(encoding="utf-8")


def _code_only() -> str:
    """Source with docstrings stripped. The retirement is EXPLAINED in prose
    that names `system:auto`, so a naive substring check on the whole file
    would fail on its own documentation."""
    import ast
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def test_resolve_lots_no_longer_writes_an_auto_match():
    code = _code_only()
    assert "system:auto" not in code, "the auto-match writer is back"
    assert "def write_back" not in _source()


def test_resolve_lots_does_not_call_the_matching_rule():
    """resolve_lot stays in pipeline/lot_resolution.py — api/review reads it to
    SHOW a reviewer the candidates. This script must not act on it."""
    assert "from pipeline.lot_resolution import resolve_lot" not in _source()


def test_applying_decided_matches_survives():
    """The half that must keep working: a human's pick in the review queue only
    reaches AuctionProperty through this."""
    from scripts import resolve_lots
    assert callable(resolve_lots.apply_decided)
    assert callable(resolve_lots.run)


def test_the_second_legacy_resolver_is_gone():
    from pathlib import Path
    p = (Path(__file__).resolve().parents[2]
         / "scripts" / "resolve_lots_from_extraction.py")
    assert not p.exists(), "a third resolver would reintroduce rival writers"


def test_apply_extractions_is_the_only_remaining_writer():
    """Grep the tree: exactly one place may SET resolved_lot_key from a match
    it computed itself. resolve_lots writes only what was already decided."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    writers = set()
    for path in list((root / "scripts").glob("*.py")) + list((root / "pipeline").glob("*.py")):
        if "resolved_lot_key = row.lot_key" in path.read_text(encoding="utf-8"):
            writers.add(path.name)
    assert writers == {"apply_extractions.py", "resolve_lots.py"}, writers
