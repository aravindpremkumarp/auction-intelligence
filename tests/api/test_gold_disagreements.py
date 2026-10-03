"""evals/gold_disagreements: agreed values become gold, differences become
questions, and answers fill the gaps."""
from evals.gold_disagreements import apply_answer, diff


def _rec(cls, text, **attrs):
    return {"cls": cls, "text": text, "attrs": attrs}


def test_single_agree_and_disagree():
    v1 = {"fields": {"bank_name": "Canara Bank", "village": "Ambur"}, "identifiers": {}, "lots": []}
    v2 = {"fields": {"bank_name": "Canara Bank", "village": "Amboor"}, "identifiers": {}, "lots": []}
    rec1 = [_rec("location", "village Ambur", village="Ambur")]
    rec2 = [_rec("location", "village Amboor", village="Amboor")]
    agreed, qs = diff("1", v1, v2, rec1, rec2)
    assert agreed["fields"] == {"bank_name": "Canara Bank"}
    assert [q["key"] for q in qs] == ["village"]
    assert qs[0]["quote_a"] == "village Ambur" and qs[0]["quote_b"] == "village Amboor"

    entry = {"fields": dict(agreed["fields"]), "identifiers": {}}
    apply_answer(entry, qs[0], {"choice": "b"})
    assert entry["fields"]["village"] == "Amboor"


def test_lots_paired_by_reserve_and_extra_lot_asked():
    v1 = {"fields": {}, "identifiers": {}, "lots": [
        {"reserve_price_num": 100, "emd_num": 10}, {"reserve_price_num": 200, "emd_num": 20}]}
    v2 = {"fields": {}, "identifiers": {}, "lots": [
        {"reserve_price_num": 200, "emd_num": 25}]}
    rec1 = [_rec("auction_terms", "Rs 100", reserve_price_num=100, lot_index="1"),
            _rec("auction_terms", "Rs 200", reserve_price_num=200, lot_index="2")]
    rec2 = [_rec("auction_terms", "Rs 200", reserve_price_num=200, lot_index="1")]
    agreed, qs = diff("2", v1, v2, rec1, rec2)
    keys = sorted(q["key"] for q in qs)
    assert keys == ["emd_num", "lot_count", "real_lot"]
    assert agreed["lots"] == [{"reserve_price_num": 200}]

    entry = {"fields": {}, "identifiers": {}, "lots": [dict(x) for x in agreed["lots"]]}
    for q in qs:
        ans = {"lot_count": {"choice": "a"}, "emd_num": {"choice": "other", "value": "20"},
               "real_lot": {"choice": "a"}}[q["key"]]
        apply_answer(entry, q, ans)
    assert entry["lot_count"] == 2
    assert {"reserve_price_num": 200, "emd_num": 20} in entry["lots"]
    assert {"reserve_price_num": 100, "emd_num": 10} in entry["lots"]
