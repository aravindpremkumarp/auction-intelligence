"""pipeline/absence: "not in the notice" decided without a reviewer."""
from __future__ import annotations

import json

import pipeline.absence as A
import pipeline.gap_fill as G
from pipeline.key_entities import key_checklist, key_marks

MD = ("SALE NOTICE. Lot 1: land at Sy No 12/1, Village V. "
      "Reserve price Rs.9,50,000/-.\n")


def _e(cls, text, lot="1", **attrs):
    s = MD.index(text)
    return {"cls": cls, "text": text, "start": s, "end": s + len(text),
            "attrs": {"lot_index": lot, **attrs}}


def _stored():
    return [_e("full_description", "land at Sy No 12/1, Village V"),
            _e("location", "Village V"),
            _e("property", "land", property_type="land"),
            _e("auction_terms", "Reserve price Rs.9,50,000/-",
               reserve_price_num="950000", auction_start_dt="2026-10-01")]


def test_no_clue_only_for_optional_facts():
    assert A.no_clue("land at Sy No 12/1", "possession_type")
    assert A.no_clue("land at Sy No 12/1", "extent")
    assert not A.no_clue("1200 sq.ft. land", "extent")
    assert not A.no_clue("symbolic <b>possession</b>", "possession_type")
    # a fact every notice states is never "no clue" — it goes to a read
    assert not A.no_clue("nothing here", "reserve_price")
    assert not A.no_clue("nothing here", "full_description")


def test_plan_marks_gaps_with_no_clue_and_skips_marked_ones():
    todo, marks = G.plan(MD, _stored())
    assert todo == {}
    assert marks == {("1", "possession_type"): A.RULE_NO_CLUE,
                     ("1", "extent"): A.RULE_NO_CLUE}
    todo, marks = G.plan(MD, _stored(), skip={("1", "extent")})
    assert ("1", "extent") not in marks


def test_not_found_is_absent_for_optional_and_unfound_for_must_have():
    m = A.new_marks({("1", "extent"): A.RULE_NOT_FOUND,
                     ("2", "reserve_price"): A.RULE_NOT_FOUND,
                     ("3", "possession_type"): A.RULE_NO_CLUE})
    assert set(m) == {"absent:1:extent", "unfound:2:reserve_price",
                      "absent:3:possession_type"}
    assert all(v["by"] == A.AUTO for v in m.values())


def test_auto_absent_counts_done_but_unfound_stays_missing():
    corr = {**A.new_marks({("1", "extent"): A.RULE_NO_CLUE}),
            "unfound:1:possession_type": {"by": "auto", "rule": "not_found"}}
    cells = key_checklist(_stored(), corr)["lots"][0]["cells"]
    assert cells["extent"]["status"] == "absent"
    assert cells["extent"]["auto"] == A.RULE_NO_CLUE
    assert cells["possession_type"]["status"] == "missing"
    assert cells["possession_type"]["unfound"] is True
    # the gap-filler skips both
    assert A.skip(corr) == {("1", "extent"), ("1", "possession_type")}


def test_a_persons_absent_mark_wins_and_survives_clearing():
    corr = {"absent:1:extent": {"by": "rev@x", "at": "t"},
            "unfound:1:extent": {"by": "auto"},
            "absent:1:possession_type": {"by": "auto", "rule": "no_clue"}}
    assert key_marks(corr)[("1", "extent")]["kind"] == "absent"
    assert A.drop_auto(corr) == {"absent:1:extent": {"by": "rev@x", "at": "t"}}


def test_write_marks_never_overwrites_a_persons_mark(monkeypatch):
    stored = {"c": json.dumps({"absent:1:extent": {"by": "rev@x"}})}
    written = {}
    import api.neo4j_client as N
    import pipeline.key_entities as K
    monkeypatch.setattr(N, "run_read_query", lambda q, p=None, **k: [stored])
    monkeypatch.setattr(N, "run_query",
                        lambda q, p=None, **k: written.update(json.loads(p["c"])))
    monkeypatch.setattr(K, "stamp_key_scores", lambda fns: None)
    n = A.write_marks("f", A.new_marks({("1", "extent"): A.RULE_NO_CLUE,
                                        ("1", "possession_type"): A.RULE_NO_CLUE}))
    assert n == 1
    assert written["absent:1:extent"] == {"by": "rev@x"}
    assert written["absent:1:possession_type"]["by"] == "auto"


def test_a_fact_stated_once_in_the_header_is_not_no_clue():
    # "physical possession ... taken" once, above every lot: lot 2's own text
    # never says it, but its excerpt carries the header, so it is read.
    head = "Whereas the physical possession of the properties has been taken. "
    lot1 = "Lot 1: land at Sy No 1, Village A, 1000 sq.ft. " + "x" * 4000
    lot2 = "Lot 2: land at Sy No 2, Village B, 900 sq.ft. " + "y" * 4000
    md = head + lot1 + lot2

    def e(cls, text, lot):
        s = md.index(text)
        return {"cls": cls, "text": text, "start": s, "end": s + len(text),
                "attrs": {"lot_index": lot}}
    ents = [e("full_description", "land at Sy No 1, Village A, 1000 sq.ft.", "1"),
            e("full_description", "land at Sy No 2, Village B, 900 sq.ft.", "2")]
    text, back, _ = G.excerpt(md, ents, "2", n_lots=2)
    assert "physical possession" in text
    i = text.index("physical")
    s, _ = back(i, i + 8)
    assert md[s:s + 8] == "physical"
    todo, marks = G.plan(md, ents)
    assert ("2", "possession_type") not in marks
    assert "possession_type" in todo["2"]
