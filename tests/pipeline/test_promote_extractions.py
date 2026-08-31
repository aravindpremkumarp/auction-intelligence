"""Unit tests for pipeline/promote_extractions.py (pure entity -> graph shaping).

No Neo4j here — build_lots() is the whole decision layer, so testing it covers
the promotion logic without a database.
"""
from __future__ import annotations

import pytest

import pipeline.promote_extractions as P


def ent(cls, text="", **attrs):
    return {"id": "0", "cls": cls, "text": text, "start": 0,
            "end": len(text), "attrs": attrs}


def build(entities, filename="notices/x/n.jpg"):
    return P.build_lots(entities, filename)


# ── identifier value normalization ───────────────────────────────────────────

def test_value_norm_strips_spacing_and_case():
    assert P.value_norm("S.F No 256 / 1F") == P.value_norm("sf no 256/1f")


def test_value_norm_preserves_subdivision():
    # 72/1B and 72/1B1 are different parcels — the suffix must survive
    assert P.value_norm("72/1B") != P.value_norm("72/1B1")


def test_value_norm_empty():
    assert P.value_norm("") is None
    assert P.value_norm(None) is None


# ── lot splitting ────────────────────────────────────────────────────────────

def test_single_lot_defaults_to_index_one():
    _, lots = build([ent("full_description", "All that piece and parcel")])
    assert len(lots) == 1
    assert lots[0]["lot_index"] == "1"
    assert lots[0]["lot_key"] == "notices/x/n.jpg#1"


def test_multi_lot_splits_by_lot_index():
    _, lots = build([
        ent("property", "A", lot_index="1", property_type="land"),
        ent("property", "B", lot_index="2", property_type="flat"),
        ent("identifier", "S.No 1", lot_index="2", kind="survey_old", value="1/2"),
    ])
    assert [rec["lot_index"] for rec in lots] == ["1", "2"]
    assert len(lots[0]["identifiers"]) == 0
    assert len(lots[1]["identifiers"]) == 1


# ── possession: silence is a fact ────────────────────────────────────────────

def test_possession_recorded_when_stated():
    _, lots = build([ent("property", "x", possession_type="Symbolic")])
    p = lots[0]["props"]
    assert p["possession_type"] == "symbolic"
    assert p["possession_stated"] is True


def test_possession_refusal_is_explicit_not_null():
    # a notice printing the boilerplate disjunction emits no possession_type;
    # that is a refusal to commit, not a failed extraction
    _, lots = build([ent("property", "x", property_type="land")])
    p = lots[0]["props"]
    assert "possession_type" not in p
    assert p["possession_stated"] is False


def test_garbage_possession_value_is_not_stored():
    _, lots = build([ent("property", "x",
                         possession_type="Constructive / Symbolic / Physical")])
    assert "possession_type" not in lots[0]["props"]
    assert lots[0]["props"]["possession_stated"] is False


# ── extents ──────────────────────────────────────────────────────────────────

def test_cent_extent_is_converted():
    _, lots = build([ent("extent", "0.20 cents", total_area="0.20 cents")])
    m = lots[0]["measurements"][0]
    assert m["kind"] == "total"
    assert m["unit"] == "cent"
    assert m["sqft_norm"] == pytest.approx(87.12)
    assert m["norm_method"] == "converted"


def test_stated_sqft_is_not_marked_converted():
    _, lots = build([ent("extent", "2180 sq.ft", total_area="2180 sq.ft")])
    assert lots[0]["measurements"][0]["norm_method"] == "stated"


def test_extent_sqft_attr_is_taken_as_bare_number():
    _, lots = build([ent("extent", "x", extent_sqft="9583.00")])
    m = lots[0]["measurements"][0]
    assert (m["kind"], m["unit"], m["sqft_norm"]) == ("extent", "sq_ft", 9583.0)


def test_uds_parent_is_kept_but_never_headline():
    _, lots = build([ent("extent", "x", undivided_share="509 sq.ft",
                         uds_parent_extent="80854 sq.ft",
                         built_up_area="950 sq.ft")])
    kinds = {m["kind"] for m in lots[0]["measurements"]}
    assert {"uds", "uds_parent", "built_up"} <= kinds
    assert lots[0]["headline_kind"] == "built_up"


# ── boundaries: road width and access kind ───────────────────────────────────

def test_road_width_is_lifted_out_of_adjacency():
    _, lots = build([ent("boundary", "x", side="north",
                         adjacency="23 Feet wide East-West Road",
                         measurement="30 feet")])
    b = lots[0]["boundaries"]["north"]
    assert b["access_kind"] == "road"
    assert b["road_width_ft"] == 23.0
    assert b["measurement_ft"] == 30.0


def test_derived_lot_road_width_is_the_widest_side():
    _, lots = build([
        ent("boundary", "x", side="north", adjacency="20 feet Road"),
        ent("boundary", "x", side="south", adjacency="30 feet Road",
            measurement="45 feet"),
    ])
    p = lots[0]["props"]
    assert p["road_width_ft"] == 30.0
    assert p["frontage_ft"] == 45.0


def test_setback_does_not_count_as_road_frontage():
    # "LAND LEFT BY ROAD" is reserved for widening — it reduces the parcel
    _, lots = build([ent("boundary", "x", side="east",
                         adjacency="30 FT LAND LEFT BY ROAD")])
    assert lots[0]["boundaries"]["east"]["access_kind"] == "setback"
    assert "road_width_ft" not in lots[0]["props"]


def test_area_written_into_a_measurement_is_flagged():
    _, lots = build([ent("boundary", "x", side="west",
                         adjacency="Plot No.6", measurement="19 Sq.Ft")])
    b = lots[0]["boundaries"]["west"]
    assert b["is_length_valid"] is False
    assert b["measurement_ft"] is None


def test_absent_measurement_is_still_valid():
    _, lots = build([ent("boundary", "x", side="north", adjacency="Road")])
    assert lots[0]["boundaries"]["north"]["is_length_valid"] is True


# ── parties ──────────────────────────────────────────────────────────────────

def test_party_role_is_preserved():
    _, lots = build([
        ent("borrower", "Smt. P. Karnagi", role="borrower"),
        ent("borrower", "Sri. Ganeshkumar", role="guarantor"),
    ])
    roles = {p["name"]: p["role"] for p in lots[0]["parties"]}
    assert roles["Sri. Ganeshkumar"] == "guarantor"


def test_unknown_role_falls_back_to_borrower():
    _, lots = build([ent("borrower", "X", role="beneficiary")])
    assert lots[0]["parties"][0]["role"] == "borrower"


# ── notice level ─────────────────────────────────────────────────────────────

def test_notice_level_creditor_fields():
    notice, _ = build([ent("secured_creditor", "Indian Bank",
                           legal_basis="SARFAESI", bank_name="Indian Bank",
                           branch="Portonovo",
                           auction_platform_url="https://baanknet.com")])
    assert notice["legal_basis"] == "SARFAESI"
    assert notice["bank_name"] == "Indian Bank"


def test_terms_block_is_hashed_for_dedup():
    text = "1. EMD shall be forfeited. 2. As is where is."
    n1, _ = build([ent("full_terms", text)])
    n2, _ = build([ent("full_terms", text)], filename="other.jpg")
    assert n1["terms_hash"] == n2["terms_hash"]


def test_notice_level_extras_do_not_land_on_a_lot():
    notice, lots = build([
        ent("extras", "RERA no", key="rera_no", value="TN/1/2024"),
        ent("full_description", "desc"),
    ])
    assert notice["facts"] == [{"key": "rera_no", "value": "TN/1/2024"}]
    assert lots[0]["facts"] == []


def test_lot_level_extras_attach_to_the_lot():
    _, lots = build([ent("extras", "road", key="road_access",
                         value="30ft", lot_index="1")])
    assert lots[0]["facts"] == [{"key": "road_access", "value": "30ft"}]


# ── placeholder scrubbing ────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", ["NULL", "null", "N/A", "-", "  "])
def test_placeholder_values_are_dropped(junk):
    _, lots = build([ent("property", "x", encumbrance=junk,
                         property_type="land")])
    assert "encumbrance" not in lots[0]["props"]


# ── platform naming (cross-source join) ──────────────────────────────────────

@pytest.mark.parametrize("url,name", [
    ("https://baanknet.com", "BAANKNET"),
    ("https://www.mstcecommerce.com/", "MSTCECOMMERCE"),
])
def test_platform_name_from_url(url, name):
    assert P.platform_name_of(url) == name


def test_platform_name_of_none():
    assert P.platform_name_of(None) is None


# ── phase C: parcel grouping ─────────────────────────────────────────────────

def _edge(lot, ident, village="v1"):
    return {"lot_key": lot, "ident": ident, "village": village}


def _parcel_of(rows):
    return {r["lot_key"]: r["parcel_id"] for r in rows}


def test_lot_with_no_shared_identifier_is_its_own_parcel():
    rows = P.parcel_groups([], ["a", "b"])
    assert _parcel_of(rows) == {"a": "lot-a", "b": "lot-b"}
    assert all(r["method"] == "singleton" for r in rows)


def test_two_lots_sharing_an_identifier_merge():
    rows = P.parcel_groups(
        [_edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1")],
        ["a", "b"])
    assert _parcel_of(rows) == {"a": "auto-a", "b": "auto-a"}
    assert all(r["method"] == "identifier" for r in rows)


def test_merge_is_transitive_across_a_chain():
    """A-B share one number, B-C another: all three are one parcel.

    The pairwise MERGE this replaced put B in two parcels at once.
    """
    rows = P.parcel_groups([
        _edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1"),
        _edge("b", "patta:990"), _edge("c", "patta:990"),
    ], ["a", "b", "c"])
    assert _parcel_of(rows) == {"a": "auto-a", "b": "auto-a", "c": "auto-a"}


def test_every_lot_belongs_to_exactly_one_parcel():
    rows = P.parcel_groups([
        _edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1"),
        _edge("c", "survey_old:12/1"),
    ], ["a", "b", "c", "d"])
    assert len(rows) == 4
    assert len({r["lot_key"] for r in rows}) == 4


def test_same_identifier_in_another_village_does_not_merge():
    """Survey numbers repeat across the state — village scoping is the guard."""
    rows = P.parcel_groups(
        [_edge("a", "survey_old:12/1", "v1"),
         _edge("b", "survey_old:12/1", "v2")],
        ["a", "b"])
    assert _parcel_of(rows) == {"a": "lot-a", "b": "lot-b"}


def test_parcel_id_is_stable_regardless_of_edge_order():
    forward = P.parcel_groups(
        [_edge("b", "patta:9"), _edge("a", "patta:9")], ["a", "b"])
    reverse = P.parcel_groups(
        [_edge("a", "patta:9"), _edge("b", "patta:9")], ["b", "a"])
    assert _parcel_of(forward) == _parcel_of(reverse) == {
        "a": "auto-a", "b": "auto-a"}


def test_merged_parcel_records_what_it_merged_on():
    rows = P.parcel_groups(
        [_edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1")],
        ["a", "b"])
    assert rows[0]["evidence"] == ["survey_old:12/1"]
    assert rows[0]["lot_count"] == 2


def test_singleton_carries_no_evidence():
    rows = P.parcel_groups([], ["a"])
    assert rows[0]["evidence"] is None
    assert rows[0]["lot_count"] == 1


# ── sibling units must not merge into one parcel ─────────────────────────────

def test_two_flats_on_one_survey_number_stay_separate():
    """The bug this rule exists for.

    Every flat in a project shares a survey number and a village, so the
    land-only grouping folded them into one :Parcel — and the attempt
    numbering then read them as repeat auctions of a single property.
    """
    rows = P.parcel_groups(
        [_edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1")],
        ["a", "b"],
        {"a": {"flat:g-2"}, "b": {"flat:s-3"}})
    assert _parcel_of(rows) == {"a": "lot-a", "b": "lot-b"}
    assert all(r["method"] == "singleton" for r in rows)


def test_the_same_flat_auctioned_twice_still_merges():
    """The rule must not cost us the case parcels exist for."""
    rows = P.parcel_groups(
        [_edge("a", "survey_old:12/1"), _edge("b", "survey_old:12/1")],
        ["a", "b"],
        {"a": {"flat:g-2"}, "b": {"flat:g-2"}})
    assert _parcel_of(rows) == {"a": "auto-a", "b": "auto-a"}


def test_plot_numbers_split_a_layout():
    """Adjacent plots in one layout share the parent survey number."""
    rows = P.parcel_groups(
        [_edge(k, "survey_new:45/3") for k in ("a", "b", "c")],
        ["a", "b", "c"],
        {"a": {"plot:9"}, "b": {"plot:10"}, "c": {"plot:11"}})
    assert len({r["parcel_id"] for r in rows}) == 3


def test_lots_naming_no_unit_still_merge_on_land_alone():
    """No unit evidence either side means nothing to split on."""
    rows = P.parcel_groups(
        [_edge("a", "patta:990"), _edge("b", "patta:990")], ["a", "b"], {})
    assert _parcel_of(rows) == {"a": "auto-a", "b": "auto-a"}


def test_a_silent_lot_does_not_bridge_two_different_flats():
    """`""` is a signature, not a wildcard.

    If a lot naming no unit matched anything, flat 1 and flat 2 would merge
    transitively through it — the exact fold the rule is meant to prevent.
    """
    rows = P.parcel_groups(
        [_edge(k, "survey_old:12/1") for k in ("a", "b", "c")],
        ["a", "b", "c"],
        {"a": {"flat:1"}, "c": {"flat:2"}})
    assert len({r["parcel_id"] for r in rows}) == 3


def test_unit_signature_is_order_independent():
    units = {"a": {"flat:1", "door_new:7"}, "b": {"door_new:7", "flat:1"}}
    assert (P.unit_signature(units, "a") == P.unit_signature(units, "b")
            != "")


def test_unit_signature_of_an_unknown_lot_is_empty():
    assert P.unit_signature({"a": {"flat:1"}}, "b") == ""
    assert P.unit_signature(None, "a") == ""


def test_floor_alone_is_not_treated_as_a_unit_identifier():
    """It qualifies a unit rather than naming one, and is rarely stated."""
    assert "floor" not in P.UNIT_IDENTIFIER_KINDS
    assert "flat" in P.UNIT_IDENTIFIER_KINDS


def test_attempt_numbering_groups_a_day_rather_than_a_row():
    """Same-day lots are a batch sale, not attempt 1 followed by attempt 2."""
    q = P._ATTEMPT_NO
    assert "left(a.auction_start_dt, 10) AS day" in q
    assert "collect(DISTINCT a) AS sameday" in q
    # the whole point: idx counts days, so a same-day pair shares an attempt_no
    assert "collect(sameday) AS rounds" in q
    assert "ORDER BY a.auction_start_dt" not in q


def test_attempt_numbering_avoids_date_on_a_string_column():
    """auction_start_dt is a string, and a few carry a bare time.

    date('12:00') raises rather than returning null, which aborts the whole
    statement and leaves every attempt_no unwritten.
    """
    assert "date(a.auction_start_dt)" not in P._ATTEMPT_NO


# ── phase B2: the listing→lot relationship ───────────────────────────────────

def _link_src() -> str:
    import inspect
    return inspect.getsource(P.link_lots)


def test_the_edge_is_built_from_the_key_it_replaces():
    """Phase 1 is additive: the string still decides, the edge only mirrors it.

    Deriving the edge from anything else would make the two disagree before a
    single reader had moved over, which is the opposite of the point.
    """
    assert "MATCH (l:Lot {lot_key: a.resolved_lot_key})" in P._LINK_LOTS
    assert "MERGE (a)-[r:IS_LOT]->(l)" in P._LINK_LOTS


def test_relinking_does_not_duplicate():
    """MERGE, not CREATE — the step is run on every promote."""
    assert "CREATE (a)-[" not in P._LINK_LOTS
    assert "MERGE (a)-[r:IS_LOT]->(l)" in P._LINK_LOTS


def test_an_edge_whose_key_moved_is_dropped():
    """A claim nothing supports is exactly what the string version got wrong.

    A listing that stops resolving, or resolves elsewhere, must not keep an
    edge to the lot it used to name.
    """
    q = P._UNLINK_LOTS
    assert "a.resolved_lot_key IS NULL OR a.resolved_lot_key <> l.lot_key" in q
    assert "DELETE r" in q


def test_stale_edges_are_dropped_before_new_ones_are_written():
    """Order matters: linking first would leave a listing holding two edges
    for the length of the run."""
    src = _link_src()
    assert src.index("_UNLINK_LOTS") < src.index("_LINK_LOTS")


def test_a_key_pointing_at_no_lot_is_counted_not_hidden():
    """Under the string these fail silently, one read at a time. Phase 1's
    job is to make the number visible."""
    assert "NOT EXISTS { MATCH (:Lot {lot_key: a.resolved_lot_key}) }" in \
        P._DANGLING_KEYS


def test_a_dry_run_writes_nothing():
    src = _link_src()
    head = src[:src.index("if dry_run")]
    assert "write(" not in head, "a dry run must not reach a writer"


def test_linking_runs_even_when_parcels_are_skipped():
    """--skip-parcels is about the parcel layer, not about leaving the
    listing→lot link stale."""
    import inspect
    src = inspect.getsource(P.run)
    assert src.index("link_lots(dry_run)") < src.index("if not skip_parcels")


def test_linking_runs_after_the_lots_are_written():
    """apply_extractions derives the key before the :Lot exists, so the edge
    can only be made once phase B has created the nodes.

    rindex, not index: run() calls link_lots twice — once for the
    --links-only early return, once in the pipeline. The pipeline call is the
    later one, and it is the one that has to follow phase B.
    """
    import inspect
    src = inspect.getsource(P.run)
    assert src.index("phase B done") < src.rindex("link_lots(dry_run)")


def test_links_only_returns_before_promoting_anything():
    """The edges are derived from keys and lots already in the graph, so
    rebuilding them must not require re-promoting the corpus."""
    import inspect
    src = inspect.getsource(P.run)
    assert src.index("if links_only") < src.index("docs = fetch_documents")
    assert src.index("link_lots(dry_run)") < src.index("docs = fetch_documents")
