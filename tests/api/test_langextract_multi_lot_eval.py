"""Offline guards for the multi-lot eval (evals/langextract_eval + langextract_gold).

These run WITHOUT the `langextract` dependency or any API key: they drive the
pure scorer (group_by_lot / score_multi / score_records) with hand-built
extraction records, so CI can prove the multi-lot metric actually discriminates
correct lot-binding from broken binding — the thing the notice-level flatten
could never see.

Two halves:
  1. gold well-formedness — every multi entry carries a per-lot `lots` list with a
     numeric reserve anchor, canonical identifier kinds, and notice-level `fields`
     kept free of per-lot keys.
  2. scorer behaviour — a correctly-bound extraction scores full; collapsing all
     lots under one index, swapping a field to the wrong lot, or hallucinating an
     extra lot each lose points and/or fail the lot-count check.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import langextract_eval as LE
from evals.langextract_gold import GOLD

_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_KINDS = set(
    json.loads((_ROOT / "pipeline" / "lookups" / "identifier_kinds.json")
               .read_text(encoding="utf-8"))["canonical"]
)
MULTI = [g for g in GOLD if g.get("lots")]
# Per-lot data must live in `lots`, never leak back into notice-level `fields`.
_PER_LOT_KEYS = {"reserve_price_num", "emd_num", "village", "taluk", "district",
                 "registration_district", "registration_sub_district",
                 "borrower_primary"}


# ── synthetic extraction records ──────────────────────────────────────────────
def _perfect_records(g: dict) -> list[dict]:
    """The ideal extraction for a gold entry: every lot correctly numbered and
    every labelled field bound to its own lot. Shape matches evals._records."""
    recs = [{"cls": "secured_creditor", "text": g["fields"].get("bank_name", ""),
             "attrs": {"legal_basis": g["fields"].get("legal_basis"),
                       "bank_name": g["fields"].get("bank_name")}}]
    poss = g["fields"].get("possession_type")
    for i, lot in enumerate(g["lots"], start=1):
        li = str(i)
        # notice-wide possession lives in `fields`; flatten reads it off lot 1's
        # property. Emit it only when it's a concrete value (EXPECT_NULL/None -> no
        # property possession, which is the correct "don't invent" behaviour).
        prop = {"property_type": "flat", "lot_index": li}
        if isinstance(poss, str):
            prop["possession_type"] = poss
        recs.append({"cls": "property", "text": "", "attrs": prop})
        recs.append({"cls": "auction_terms", "text": "",
                     "attrs": {"reserve_price_num": str(lot["reserve_price_num"]),
                               "emd_num": str(lot.get("emd_num", "")),
                               "lot_index": li}})
        loc = {k: lot[k] for k in ("village", "taluk", "district",
                                   "registration_district",
                                   "registration_sub_district") if lot.get(k)}
        if loc:
            recs.append({"cls": "location", "text": "", "attrs": {**loc, "lot_index": li}})
        for kind, val in (lot.get("identifiers") or {}).items():
            recs.append({"cls": "identifier", "text": "",
                         "attrs": {"kind": kind, "value": val, "lot_index": li}})
    return recs


def _correct(rows) -> int:
    return sum(1 for *_, ok in rows if ok)


def _by_aid(aid: str) -> dict:
    return next(g for g in MULTI if g["aid"] == aid)


# ── 1. gold well-formedness ───────────────────────────────────────────────────
def test_multi_entries_exist():
    aids = {g["aid"] for g in MULTI}
    assert {"749433", "750348", "753006"} <= aids


@pytest.mark.parametrize("g", MULTI, ids=[g["aid"] for g in MULTI])
def test_lots_wellformed(g):
    assert g["lots"], f"{g['aid']} has empty lots"
    for i, lot in enumerate(g["lots"], start=1):
        assert isinstance(lot.get("reserve_price_num"), (int, float)), \
            f"{g['aid']} lot{i} missing numeric reserve anchor"
        for kind in (lot.get("identifiers") or {}):
            assert kind in _CANONICAL_KINDS, \
                f"{g['aid']} lot{i} identifier kind {kind!r} not canonical"


@pytest.mark.parametrize("g", MULTI, ids=[g["aid"] for g in MULTI])
def test_notice_fields_have_no_per_lot_keys(g):
    leaked = _PER_LOT_KEYS & set(g["fields"])
    assert not leaked, f"{g['aid']} notice-level fields leak per-lot keys: {leaked}"


def test_749433_is_single_lot_in_markdown():
    # the "multi label, one lot in text" case — correct extraction has one lot.
    assert len(_by_aid("749433")["lots"]) == 1


# ── 2. scorer behaviour ───────────────────────────────────────────────────────
@pytest.mark.parametrize("g", MULTI, ids=[g["aid"] for g in MULTI])
def test_perfect_extraction_scores_full(g):
    rows, (n_gold, n_got, count_ok) = LE.score_records(g, _perfect_records(g))
    assert count_ok, f"{g['aid']} lot-count wrong: got {n_got} want {n_gold}"
    assert _correct(rows) == len(rows), \
        f"{g['aid']} perfect extraction had misses: " \
        f"{[r for r in rows if not r[3]]}"


def test_collapsed_lots_are_penalized():
    # Every reserve + field dumped under a single lot_index -> the model lost the
    # per-lot structure. Must score far below a correctly-bound extraction.
    g = _by_aid("750348")
    good = _perfect_records(g)
    collapsed = [{**r, "attrs": {**r["attrs"],
                                 **({"lot_index": "1"} if "lot_index" in r["attrs"] else {})}}
                 for r in good]
    good_rows, (_, _, good_ok) = LE.score_records(g, good)
    bad_rows, (n_gold, n_got, bad_ok) = LE.score_records(g, collapsed)
    assert good_ok and not bad_ok
    assert n_got == 1 and n_gold == 6
    assert _correct(bad_rows) < _correct(good_rows)


def test_field_bound_to_wrong_lot_fails():
    # Swap two lots' villages: reserves still match, but each village is now on the
    # wrong lot. The village rows must flip to misses while reserves stay correct.
    # Pick two location records with DIFFERENT villages (750348 lots 1 & 2 share
    # "Varadharajapuram", so swapping those would be a no-op).
    g = _by_aid("750348")
    recs = _perfect_records(g)
    locs = [r for r in recs if r["cls"] == "location"]
    a, b = next((x, y) for i, x in enumerate(locs) for y in locs[i + 1:]
                if x["attrs"].get("village") != y["attrs"].get("village"))
    a["attrs"]["village"], b["attrs"]["village"] = (
        b["attrs"]["village"], a["attrs"]["village"])
    rows, _ = LE.score_records(g, recs)
    village_rows = [r for r in rows if r[0].endswith(":village")]
    assert sum(1 for *_, ok in village_rows if not ok) >= 2
    assert all(ok for k, *_, ok in rows if k.endswith(":reserve"))


def test_shared_reserve_disambiguated_by_identifier():
    # 753006 lots 4 & 5 both have reserve 3100000; only the identifier tells them
    # apart. A correct extraction must still bind each village to the right lot.
    g = _by_aid("753006")
    rows, (_, _, count_ok) = LE.score_records(g, _perfect_records(g))
    assert count_ok
    assert all(ok for k, *_, ok in rows if k in ("lot4:village", "lot5:village"))


def test_hallucinated_extra_lot_flags_count():
    # Extraction invents a 2nd lot (extra reserve) for the single-lot 749433.
    g = _by_aid("749433")
    recs = _perfect_records(g)
    recs.append({"cls": "auction_terms", "text": "",
                 "attrs": {"reserve_price_num": "9999999", "lot_index": "2"}})
    _, (n_gold, n_got, count_ok) = LE.score_records(g, recs)
    assert n_gold == 1 and n_got == 2 and not count_ok


def test_mixed_type_lot_index_does_not_crash_the_run():
    # Models emit lot_index inconsistently — "2" from one call, 2 from the next.
    # flatten_records used to sort borrowers on that raw value, so one numeric
    # attribute raised TypeError and killed a whole eval run (after the API spend,
    # before any score printed) instead of costing a single field.
    g = _by_aid("750348")
    recs = _perfect_records(g) + [
        {"cls": "borrower", "text": "M/s. Poojas Enterprises", "attrs": {"lot_index": "1"}},
        {"cls": "borrower", "text": "Mrs. Pooja B", "attrs": {"lot_index": 2}},
        {"cls": "borrower", "text": "Mr. Balagopal", "attrs": {"lot_index": None}},
    ]
    rows, (_, _, count_ok) = LE.score_records(g, recs)
    assert count_ok
    assert rows


# ── 3. repeats, wrong-lot bindings, gold export (offline) ─────────────────────
def _swap_emd(recs: list[dict]) -> list[dict]:
    """Lot 1 and lot 2 exchange EMDs — both values grounded, both misbound."""
    out = [dict(r, attrs=dict(r["attrs"])) for r in recs]
    terms = [r for r in out if r["cls"] == "auction_terms"]
    terms[0]["attrs"]["emd_num"], terms[1]["attrs"]["emd_num"] = (
        terms[1]["attrs"]["emd_num"], terms[0]["attrs"]["emd_num"])
    return out


def test_wrong_lot_bindings_names_the_misbound_field():
    g = _by_aid("753006")
    assert LE.wrong_lot_bindings(g, _perfect_records(g)) == []
    wrong = LE.wrong_lot_bindings(g, _swap_emd(_perfect_records(g)))
    fields = {(tag, f) for tag, f, _v, _under in wrong}
    assert ("lot1", "emd") in fields and ("lot2", "emd") in fields


def test_grade_and_summarise_report_spread_across_repeats():
    g = _by_aid("750348")
    perfect = LE.grade(g, _perfect_records(g))
    broken = LE.grade(g, _swap_emd(_perfect_records(g)))
    assert perfect["correct"] == perfect["total"]
    assert broken["correct"] < broken["total"]
    assert perfect["key_total"] > 0 and perfect["key_correct"] == perfect["key_total"]
    s = LE.summarise(g, [perfect, broken, perfect])
    assert s["repeats"] == 3
    assert s["accuracy"]["max"] == 100.0 and s["accuracy"]["min"] < 100.0
    assert s["lot_count_ok_every_repeat"] is True
    assert s["wrong_lot_bindings"] == 2


def test_build_report_pools_fields_and_strata():
    gold = [_by_aid("753006"), _by_aid("750348")]
    runs = {}
    for g in gold:
        recs = _perfect_records(g)
        runs[g["aid"]] = [{"records": recs, "meta": {"usage": {"cost": 0.01, "calls": 1}, "seconds": 3.0},
                           "grade": LE.grade(g, recs)} for _ in range(2)]
    rep = LE.build_report(gold, runs, reader="langextract", stability="none",
                          manifest={"753006": {"strata": ["multi", "lakh_units"]},
                                    "750348": {"strata": ["multi"]}})
    assert rep["repeats"] == 2 and rep["notices"] == 2
    assert rep["overall"]["wrong_lot_bindings"] == 0
    assert rep["overall"]["lot_count_exact_every_repeat"] is True
    assert rep["overall"]["key_fact_recall"] == 100.0
    assert rep["overall"]["cost_per_correct_key_fact_usd"] is not None
    assert rep["per_stratum"]["multi"]["notices"] == 2
    assert rep["per_stratum"]["lakh_units"]["notices"] == 1
    assert rep["per_field"]["recall_by_field"]["reserve"]["pct"] == 100.0
    assert rep["per_field"]["closed_world"]["precision"] == 100.0


def test_is_key_row():
    assert LE.is_key_row("reserve_price_num") and LE.is_key_row("lot4:emd")
    assert LE.is_key_row("lot2:village") and not LE.is_key_row("lot2:id:flat")
    assert not LE.is_key_row("legal_basis")


def test_export_emits_lots_expect_null_and_spans():
    from evals.export_review_gold import (_gold_fields, _gold_lots,
                                          _description_spans, _person_absent)
    from evals.langextract_eval import flatten_records
    recs = [
        {"cls": "secured_creditor", "text": "Canara Bank", "start": 0, "end": 11,
         "attrs": {"legal_basis": "SARFAESI", "bank_name": "Canara Bank"}},
        {"cls": "full_description", "text": "Flat G-2 ...", "start": 100, "end": 400,
         "attrs": {"lot_index": "1"}},
        {"cls": "auction_terms", "text": "", "start": None, "end": None,
         "attrs": {"reserve_price_num": "3515000", "emd_num": "351500", "lot_index": "1"}},
        {"cls": "location", "text": "", "start": None, "end": None,
         "attrs": {"village": "Varadharajapuram", "lot_index": "1"}},
        {"cls": "identifier", "text": "", "start": None, "end": None,
         "attrs": {"kind": "flat", "value": "G-2", "lot_index": "1"}},
        {"cls": "full_description", "text": "Land ...", "start": 500, "end": 900,
         "attrs": {"lot_index": "2"}},
        {"cls": "auction_terms", "text": "", "start": None, "end": None,
         "attrs": {"reserve_price_num": "2817600", "lot_index": "2"}},
    ]
    lots = _gold_lots(recs)
    assert [l["reserve_price_num"] for l in lots] == [3515000, 2817600]
    assert lots[0]["emd_num"] == 351500 and lots[0]["village"] == "Varadharajapuram"
    assert lots[0]["identifiers"] == {"flat": "G-2"}
    assert _description_spans(recs) == {"1": [100, 400], "2": [500, 900]}
    cj = json.dumps({"absent:1:possession_type": {"by": "a@b.com", "at": "t"},
                     "absent:2:extent": {"by": "auto", "rule": "no_clue"}})
    absent = _person_absent(cj)
    assert absent == {("1", "possession_type")}
    fields, ids = _gold_fields(flatten_records(recs), multi=True, person_absent=absent)
    assert fields["possession_type"] == LE.EXPECT_NULL_JSON
    assert "reserve_price_num" not in fields and "village" not in fields
    assert ids == {}
    single_fields, single_ids = _gold_fields(flatten_records(recs))
    assert single_fields["reserve_price_num"] == 2817600 or single_fields["reserve_price_num"] == 3515000
    assert single_ids == {"flat": "G-2"}
