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
