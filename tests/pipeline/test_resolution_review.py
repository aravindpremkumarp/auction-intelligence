"""pipeline.resolution_review: human verdicts that outlive the next run.

The strings are the corpus's own: the OCR-damaged lender pair a human must be
able to approve, the two different ARCs they must be able to reject, and the
village aliases the resolver could not find on its own.
"""
from __future__ import annotations

from collections import Counter

import pytest

from pipeline.entity_resolution import propose_merges, resolve
from pipeline.resolution_review import (
    apply_bank_merges, bank_pair_key, decision_key, district_conflict_key,
    filter_proposals, settled_conflicts, skipped_villages, village_alias_key,
    village_aliases,
)


def _decision(kind: str, payload: dict, verdict: str) -> dict:
    return {"kind": kind, "key": decision_key(kind, payload),
            "payload": payload, "verdict": verdict}


def test_pair_key_ignores_order_and_spelling_of_the_canonical():
    # The same two lenders, named from either side and in either case,
    # must land on one stored decision.
    assert bank_pair_key("Piramal Finance Ltd", "Pirama Finance Ltd") == \
        bank_pair_key("PIRAMA FINANCE LIMITED", "Piramal Finance")


def test_approved_merge_survives_a_fresh_resolution_run():
    """The whole point: the human approves once, and every later run applies
    it before proposing anything."""
    decisions = [_decision("bank-merge",
                           {"a": "Piramal Finance Ltd", "b": "Pirama Finance Ltd"},
                           "approved")]
    # A "fresh run" — resolve from raw counts, as the script does nightly.
    res = resolve(Counter({"Piramal Finance Ltd": 9, "Pirama Finance Ltd": 1,
                           "Canara Bank": 240}))
    assert len(res["groups"]) == 3          # the rule alone cannot join them
    merged = apply_bank_merges(res, decisions)
    assert len(merged["groups"]) == 2
    top = next(g for g in merged["groups"] if "Piramal" in g["canonical"])
    assert top["count"] == 10
    assert top["merged_by_decision"] == 1
    assert merged["by_value"]["Pirama Finance Ltd"] == "Piramal Finance Ltd"
    # ...and the settled pair leaves the proposal queue.
    proposals = filter_proposals(propose_merges(res["groups"]), decisions)
    assert not any({p["a"], p["b"]} ==
                   {"Piramal Finance Ltd", "Pirama Finance Ltd"}
                   for p in proposals)


def test_rejected_pair_is_never_asked_again_and_never_merged():
    """The 92.9 trap: two real, different asset-reconstruction companies."""
    a = "Asset Reconstruction Company (India) Limited"
    b = "India SME Asset Reconstruction Company Limited"
    decisions = [_decision("bank-merge", {"a": a, "b": b}, "rejected")]
    res = resolve(Counter({a: 12, b: 4}))
    merged = apply_bank_merges(res, decisions)
    assert len(merged["groups"]) == 2       # rejected means untouched
    proposals = filter_proposals(propose_merges(res["groups"], min_score=88.0),
                                 decisions)
    assert proposals == []                  # and gone from the queue


def test_merge_chains_resolve_transitively():
    # A=B approved and B=C approved puts all three spellings in one group.
    decisions = [
        _decision("bank-merge", {"a": "ICICI Bank", "b": "IICI Bank"},
                  "approved"),
        _decision("bank-merge", {"a": "IICI Bank", "b": "ICICI Bank Ltd X"},
                  "approved"),
    ]
    res = resolve(Counter({"ICICI Bank": 50, "IICI Bank": 1,
                           "ICICI Bank Ltd X": 2}))
    merged = apply_bank_merges(res, decisions)
    assert len(merged["groups"]) == 1
    assert merged["groups"][0]["count"] == 53
    # The canonical is re-chosen by count — approving a merge is not
    # approving a spelling.
    assert merged["groups"][0]["canonical"] == "ICICI Bank"


def test_village_alias_is_scoped_to_its_taluk():
    """"Selaiyur -> X" approved in Tambaram must not fire in another taluk
    that happens to contain a similar string."""
    d = _decision("village-alias",
                  {"raw": "Selaiyur", "taluk": "Tambaram",
                   "target": "Selaiyur Village"}, "approved")
    aliases = village_aliases([d])
    assert aliases[village_alias_key("Selaiyur", "Tambaram")] == \
        "Selaiyur Village"
    assert village_alias_key("Selaiyur", "Sholinganallur") not in aliases


def test_skip_verdict_rules_a_string_out_everywhere():
    # An urban locality is not a revenue village in any taluk, and the skip
    # set holds the normalized form so any respelling of it also stays out.
    from pipeline.place_resolution import normalize_place
    d = _decision("village-skip", {"raw": "Injambakkam"}, "approved")
    skips = skipped_villages([d])
    assert normalize_place("Injambakkam") in skips
    assert normalize_place("Injambakam") in skips      # OCR drops a k
    assert normalize_place("Selaiyur") not in skips


def test_conflict_pattern_is_one_decision_for_many_notices():
    """27 notices write Kanchipuram over Pallavaram; the queue shows one row
    and one verdict settles all of them — however the district was spelt."""
    d = _decision("district-conflict",
                  {"raw": "Kanchipuram", "taluk": "Pallavaram"}, "approved")
    settled = settled_conflicts([d])
    assert district_conflict_key("Kanchipuram", "Pallavaram") in settled
    assert district_conflict_key("Kancheepuram", "Pallavaram") in settled
    assert district_conflict_key("Chengalpet", "Pallavaram") not in settled


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        decision_key("place-merge", {"a": "x", "b": "y"})


def test_branch_verdicts_are_scoped_to_their_bank():
    """Approving that Indian Bank's "Portonovo" and "Portnovo" are one office
    must not merge another bank's identically spelt branches."""
    from pipeline.resolution_review import apply_branch_merges
    d = _decision("branch-merge",
                  {"bank": "Indian Bank", "a": "Portonovo", "b": "Portnovo"},
                  "approved")
    res_indian = resolve(Counter({"Portonovo": 3, "Portnovo": 1}),
                         kind="branch")
    res_other = resolve(Counter({"Portonovo": 2, "Portnovo": 1}),
                        kind="branch")
    merged = apply_branch_merges(res_indian, [d], bank="Indian Bank")
    assert len(merged["groups"]) == 1
    assert merged["by_value"]["Portnovo"] == "Portonovo"
    untouched = apply_branch_merges(res_other, [d], bank="City Union Bank")
    assert len(untouched["groups"]) == 2


def test_decided_branch_pairs_leave_the_queue_either_way():
    from pipeline.resolution_review import filter_branch_proposals
    proposals = [
        {"score": 94.1, "a": "Portonovo", "b": "Portnovo",
         "a_count": 3, "b_count": 1, "bank": "Indian Bank"},
        {"score": 98.0, "a": "Asset Recovery Management Branch",
         "b": "Assets Recovery Management Branch",
         "a_count": 5, "b_count": 2, "bank": "Indian Overseas Bank"},
    ]
    d = _decision("branch-merge",
                  {"bank": "Indian Bank", "a": "Portonovo", "b": "Portnovo"},
                  "rejected")
    left = filter_branch_proposals(proposals, [d])
    assert [p["bank"] for p in left] == ["Indian Overseas Bank"]


def test_lot_match_key_is_a_registered_decision_kind():
    """`decision_key` is the single dispatcher every write and undo goes
    through — a kind missing here can never be recorded at all."""
    from pipeline.lot_resolution import lot_match_key
    from pipeline.resolution_review import KINDS

    assert "lot-match" in KINDS
    assert decision_key("lot-match", {"auction_id": "796269",
                                      "lot_key": "notice.jpg#3"}) == \
        lot_match_key("796269", "notice.jpg#3")
