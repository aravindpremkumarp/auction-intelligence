"""Unit tests for the spot-check sampler and its statistics (pipeline/spotcheck.py).

Pure logic — no DB, no app, the way tests/api/test_review_extraction.py works.
"""
from __future__ import annotations

import pytest

from pipeline.spotcheck import (
    CORRECT,
    MIN_N,
    MISSED,
    NOT_IN_SOURCE,
    PRIORITY_FIELDS,
    UNCLEAR,
    WRONG,
    build_report,
    describe_scope,
    draw_sample,
    filter_claims,
    iter_claims,
    wilson_interval,
)


def _ents():
    return [
        {"id": "0", "cls": "secured_creditor", "text": "Canara Bank",
         "start": 0, "end": 11,
         "attrs": {"legal_basis": "SARFAESI", "bank_name": "Canara Bank",
                   "lot_index": "1"}},
        {"id": "1", "cls": "location", "text": "Sathyamangala Village",
         "start": 20, "end": 41,
         "attrs": {"village": "Sathyamangala", "taluk": None, "district": "n/a",
                   "lot_index": "1"}},
    ]


# ── claim flattening ─────────────────────────────────────────────────────────
def test_iter_claims_makes_one_span_claim_plus_one_per_attribute():
    claims = iter_claims("a.pdf", _ents())
    keys = {c.key for c in claims}
    assert "a.pdf#0#@span" in keys                 # the class assertion
    assert "a.pdf#0#legal_basis" in keys           # one claim per attribute
    assert "a.pdf#0#bank_name" in keys
    assert "a.pdf#1#village" in keys


def test_iter_claims_skips_bookkeeping_and_nullish_attributes():
    keys = {c.key for c in iter_claims("a.pdf", _ents())}
    assert "a.pdf#0#lot_index" not in keys   # bookkeeping, not a claim
    assert "a.pdf#1#taluk" not in keys       # None -> nothing was emitted
    assert "a.pdf#1#district" not in keys    # "n/a" is null-ish, same as blank


def test_claim_question_and_stratum_distinguish_span_from_attr():
    span = next(c for c in iter_claims("a.pdf", _ents()) if c.attr is None)
    attr = next(c for c in iter_claims("a.pdf", _ents()) if c.attr == "village")
    assert span.stratum == "cls:secured_creditor"
    assert attr.stratum == "attr:village"
    # A class and an attribute sharing a name must not collide into one bucket.
    assert span.stratum != attr.stratum
    assert "is a secured_creditor" in span.question
    assert "village" in attr.question


def test_entity_without_text_yields_no_span_claim():
    claims = iter_claims("a.pdf", [
        {"id": "0", "cls": "extent", "text": "   ", "attrs": {"total_area": "600"}},
    ])
    assert [c.attr for c in claims] == ["total_area"]


# ── field focus ──────────────────────────────────────────────────────────────
def _mixed_claims():
    return iter_claims("a.pdf", [
        {"id": "0", "cls": "location", "text": "X Village", "start": 0, "end": 9,
         "attrs": {"village": "X", "taluk": "T", "district": "D"}},
        {"id": "1", "cls": "borrower", "text": "Komala", "start": 20, "end": 26,
         "attrs": {}},
        {"id": "2", "cls": "extent", "text": "600 sq ft", "start": 30, "end": 39,
         "attrs": {"extent": "600", "undivided_share": "120"}},
        {"id": "3", "cls": "identifier", "text": "S.No 12", "start": 50, "end": 57,
         "attrs": {"kind": "survey_old"}},
    ])


def test_a_bare_name_matches_both_the_class_and_the_attribute_sense():
    # A reviewer asking for "extent" means the concept, not a stratum key.
    kept = filter_claims(_mixed_claims(), ["extent"])
    assert {c.stratum for c in kept} == {"cls:extent", "attr:extent"}


def test_an_explicit_prefix_narrows_to_one_sense():
    kept = filter_claims(_mixed_claims(), ["attr:extent"])
    assert {c.stratum for c in kept} == {"attr:extent"}


def test_filter_keeps_only_the_requested_fields():
    kept = filter_claims(_mixed_claims(), ["village", "borrower"])
    assert {c.stratum for c in kept} == {"attr:village", "cls:borrower"}
    # taluk/district/identifier are siblings in the same entity and must go.
    assert not any(c.attr in ("taluk", "district") for c in kept)


def test_empty_or_none_field_list_keeps_everything():
    all_claims = _mixed_claims()
    assert len(filter_claims(all_claims, [])) == len(all_claims)
    assert len(filter_claims(all_claims, None)) == len(all_claims)


def test_blank_and_padded_names_are_tolerated():
    kept = filter_claims(_mixed_claims(), ["  village ", "", "   "])
    assert {c.stratum for c in kept} == {"attr:village"}


def test_an_unknown_field_name_matches_nothing_rather_than_everything():
    # A typo must not silently widen the sample back to the whole corpus.
    assert filter_claims(_mixed_claims(), ["vilage"]) == []


def test_priority_fields_covers_the_lot_deciding_set_plus_village():
    assert "village" in PRIORITY_FIELDS
    for f in ("full_description", "property_type", "possession_type", "extent",
              "undivided_share", "borrower", "reserve_price_num"):
        assert f in PRIORITY_FIELDS


def test_focusing_lifts_per_field_depth_above_the_reporting_floor():
    """The whole point: the same budget, spent on fewer fields."""
    ents = []
    for i in range(40):
        ents.append({"id": str(i), "cls": "location", "text": f"V{i}",
                     "start": i, "end": i + 2,
                     "attrs": {"village": f"v{i}", "taluk": f"t{i}",
                               "district": f"d{i}", "street": f"s{i}",
                               "state": "TN", "latitude": "12.9"}})
    claims = iter_claims("a.pdf", ents)

    wide = draw_sample(claims, size=16, seed=5)
    narrow = draw_sample(filter_claims(claims, ["village"]), size=16, seed=5)

    from collections import Counter
    wide_village = Counter(c.stratum for c in wide)["attr:village"]
    narrow_village = Counter(c.stratum for c in narrow)["attr:village"]
    assert narrow_village == 16            # every claim is the field asked about
    assert wide_village < MIN_N            # spread thin across the sibling fields
    assert narrow_village > wide_village


# ── sampling ─────────────────────────────────────────────────────────────────
def _many_claims(n_docs=6, per_doc=12):
    ents = []
    for d in range(n_docs):
        rows = []
        for i in range(per_doc):
            rows.append({"id": str(i), "cls": f"c{i % 4}", "text": f"t{i}",
                         "start": i, "end": i + 2,
                         "attrs": {f"a{i % 3}": f"v{i}"}})
        ents.append((f"doc{d}.pdf", rows))
    out = []
    for fn, rows in ents:
        out.extend(iter_claims(fn, rows))
    return out


def test_draw_is_deterministic_for_a_seed_and_order_independent():
    claims = _many_claims()
    a = [c.key for c in draw_sample(claims, size=20, seed=42)]
    b = [c.key for c in draw_sample(list(reversed(claims)), size=20, seed=42)]
    assert a == b          # DB row order must not change the draw
    assert len(a) == 20


def test_different_seeds_draw_different_samples():
    claims = _many_claims()
    a = {c.key for c in draw_sample(claims, size=20, seed=1)}
    b = {c.key for c in draw_sample(claims, size=20, seed=2)}
    assert a != b


def test_draw_spreads_across_strata_instead_of_filling_one():
    claims = _many_claims()
    picked = draw_sample(claims, size=16, seed=7)
    strata = {c.stratum for c in picked}
    # A uniform draw would happily return 16 claims from one fat stratum.
    assert len(strata) >= 4


def test_max_per_document_stops_one_notice_dominating():
    # One huge notice plus several small ones: without the cap the big one
    # supplies nearly the whole sample and the result describes it alone.
    big = iter_claims("big.pdf", [
        {"id": str(i), "cls": "c0", "text": f"t{i}", "attrs": {"a0": f"v{i}"}}
        for i in range(200)
    ])
    small = _many_claims(n_docs=4, per_doc=4)
    picked = draw_sample(big + small, size=20, seed=3, max_per_document=5)
    from collections import Counter
    counts = Counter(c.filename for c in picked)
    assert counts["big.pdf"] <= 5


def test_draw_handles_empty_and_zero_size():
    assert draw_sample([], size=10, seed=1) == []
    assert draw_sample(_many_claims(), size=0, seed=1) == []


def test_draw_cannot_return_more_than_the_population():
    claims = _many_claims(n_docs=1, per_doc=2)
    picked = draw_sample(claims, size=500, seed=1)
    assert len(picked) == len(claims)
    assert len({c.key for c in picked}) == len(picked)   # no duplicates


# ── statistics ───────────────────────────────────────────────────────────────
def test_wilson_never_claims_certainty_from_a_perfect_small_sample():
    lo, hi = wilson_interval(20, 20)
    assert hi == pytest.approx(1.0)
    # The naive normal approximation returns [1.0, 1.0] here, asserting
    # certainty from 20 observations. That is the bug this guards.
    assert lo < 0.9


def test_wilson_widens_as_the_sample_shrinks():
    lo_big, hi_big = wilson_interval(90, 100)
    lo_small, hi_small = wilson_interval(9, 10)
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_wilson_on_empty_sample_is_maximally_uncertain():
    assert wilson_interval(0, 0) == (0.0, 1.0)


# ── report ───────────────────────────────────────────────────────────────────
def _sample(keys, stratum="attr:village", filename="a.pdf"):
    return {"id": "s1", "seed": 5, "scope": {"batch": 7},
            "items": [{"key": k, "stratum": stratum, "filename": filename}
                      for k in keys]}


def test_report_withholds_a_rate_below_the_minimum_sample():
    keys = [f"k{i}" for i in range(3)]
    verdicts = {k: {"verdict": CORRECT} for k in keys}
    rep = build_report(_sample(keys), verdicts)
    assert rep["overall"]["n"] == 3
    assert rep["overall"]["enough"] is False
    # No point estimate at all — a printed percentage gets quoted, a caveat
    # beside it does not.
    assert rep["overall"]["precision"] is None
    assert rep["overall"]["ci_low"] is None


def test_report_gives_precision_and_interval_once_there_is_enough():
    keys = [f"k{i}" for i in range(MIN_N + 2)]
    verdicts = {k: {"verdict": CORRECT} for k in keys}
    verdicts[keys[0]] = {"verdict": WRONG}
    rep = build_report(_sample(keys), verdicts)
    n = MIN_N + 2
    assert rep["overall"]["n"] == n
    assert rep["overall"]["precision"] == pytest.approx((n - 1) / n)
    assert rep["overall"]["ci_low"] < rep["overall"]["precision"]
    assert rep["overall"]["ci_high"] > rep["overall"]["precision"]


def test_unclear_is_excluded_from_precision_and_reported_separately():
    keys = [f"k{i}" for i in range(10)]
    verdicts = {k: {"verdict": CORRECT} for k in keys}
    for k in keys[:4]:
        verdicts[k] = {"verdict": UNCLEAR}
    rep = build_report(_sample(keys), verdicts)
    # Illegible source must not be charged to the model: 6 graded, all correct.
    assert rep["overall"]["n"] == 6
    assert rep["overall"]["unclear"] == 4
    assert rep["unclear_rate"] == pytest.approx(0.4)


def test_pending_items_are_not_counted_as_correct():
    keys = [f"k{i}" for i in range(10)]
    verdicts = {keys[0]: {"verdict": CORRECT}}
    rep = build_report(_sample(keys), verdicts)
    assert rep["pending"] == 9
    assert rep["overall"]["n"] == 1
    assert rep["size"] == 10


def test_missed_is_tracked_but_kept_out_of_precision():
    keys = [f"k{i}" for i in range(10)]
    verdicts = {k: {"verdict": CORRECT} for k in keys}
    verdicts[keys[0]] = {"verdict": MISSED}
    rep = build_report(_sample(keys), verdicts)
    assert rep["missed_reported"] == 1
    assert rep["overall"]["n"] == 9          # the miss is not a graded emission


def test_report_separates_wrong_from_invented():
    keys = [f"k{i}" for i in range(10)]
    verdicts = {k: {"verdict": CORRECT} for k in keys}
    verdicts[keys[0]] = {"verdict": WRONG}
    verdicts[keys[1]] = {"verdict": NOT_IN_SOURCE}
    rep = build_report(_sample(keys), verdicts)
    assert rep["overall"]["wrong"] == 1
    assert rep["overall"]["not_in_source"] == 1


def test_report_states_what_it_cannot_measure():
    rep = build_report(_sample(["k0"]), {})
    assert "recall" in rep["does_not_measure"]
    assert rep["measures"] == "precision of emitted values"


def test_report_carries_the_scope_so_the_number_is_interpretable():
    rep = build_report(_sample(["k0"]), {})
    assert "batch=7" in rep["scope"]
    assert rep["seed"] == 5


def test_weakest_strata_rank_worst_first_and_ignore_thin_buckets():
    items = []
    verdicts = {}
    # good stratum: enough observations, all correct
    for i in range(MIN_N):
        k = f"g{i}"
        items.append({"key": k, "stratum": "attr:village", "filename": "a.pdf"})
        verdicts[k] = {"verdict": CORRECT}
    # bad stratum: enough observations, half wrong
    for i in range(MIN_N):
        k = f"b{i}"
        items.append({"key": k, "stratum": "attr:possession_type",
                      "filename": "a.pdf"})
        verdicts[k] = {"verdict": WRONG if i % 2 else CORRECT}
    # thin stratum: one observation, wrong — must not be named as "weakest"
    items.append({"key": "t0", "stratum": "attr:uds", "filename": "a.pdf"})
    verdicts["t0"] = {"verdict": WRONG}

    rep = build_report({"id": "s", "seed": 1, "scope": {}, "items": items},
                       verdicts)
    names = [k for k, _ in rep["weakest"]]
    assert names[0] == "attr:possession_type"
    assert "attr:uds" not in names          # n=1 says nothing


def test_describe_scope_is_explicit_about_the_whole_corpus():
    assert describe_scope({}) == "all extracted documents"
    assert "notice_type=multi" in describe_scope({"notice_type": "multi"})


def test_scope_names_the_fields_a_focused_sample_covered():
    # Without this the report's headline reads as a claim about the whole
    # extraction, when it only ever measured two fields.
    s = describe_scope({"fields": ["village", "borrower"]})
    assert "village" in s and "borrower" in s
    # and an empty list must not fabricate a focus that wasn't applied
    assert describe_scope({"fields": []}) == "all extracted documents"
