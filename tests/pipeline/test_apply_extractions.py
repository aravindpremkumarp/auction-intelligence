"""Unit tests for pipeline/apply_extractions.py (pure logic + write guards)."""
from __future__ import annotations

import json

import pipeline.apply_extractions as AX


# ── helpers ──────────────────────────────────────────────────────────────────

def ent(cls, text="", attrs=None, id=None):
    return {"id": id or "0", "cls": cls, "text": text,
            "start": 0, "end": len(text), "attrs": attrs or {}}


# ── parse_money ──────────────────────────────────────────────────────────────

def test_parse_money_int_passthrough():
    assert AX.parse_money(950000) == 950000


def test_parse_money_string_with_grouping():
    assert AX.parse_money("Rs. 12,50,000/-") == 1250000


def test_parse_money_garbage_and_none():
    assert AX.parse_money(None) is None
    assert AX.parse_money("not a price") is None
    assert AX.parse_money(True) is None


# ── entities_with_corrections ────────────────────────────────────────────────

def test_corrections_override_text():
    ents = json.dumps([{"id": "3", "cls": "full_description",
                        "text": "wrong text", "attrs": {}}])
    corr = json.dumps({"3": {"value": "reviewer fixed text", "by": "a@b.c"}})
    out = AX.entities_with_corrections(ents, corr)
    assert out[0]["text"] == "reviewer fixed text"
    assert out[0]["corrected"] is True


def test_corrections_bad_json_tolerated():
    ents = json.dumps([{"id": "0", "cls": "location", "attrs": {}}])
    assert AX.entities_with_corrections(ents, "{not json") != []
    assert AX.entities_with_corrections("{not json", None) == []


# ── group_lots ───────────────────────────────────────────────────────────────

def test_group_lots_builds_flat_fields():
    ents = [
        ent("location", "Situated at X",
            {"village": "Padur", "taluk": "Chengalpattu",
             "district": "Kancheepuram", "lot_index": "1"}),
        ent("location", "Reg clause",
            {"registration_district": "Chengalpattu",
             "registration_sub_district": "Thiruporur", "lot_index": "1"}),
        ent("boundary", "Plot No.4",
            {"side": "north", "adjacency": "Plot No.4",
             "measurement": "40 Feet", "lot_index": "1"}),
        ent("extent", "1200 sq.ft",
            {"total_area": "1200 sq.ft", "lot_index": "1"}),
        ent("extent", "509 sq.ft UDS",
            {"undivided_share": "509 sq.ft UDS", "lot_index": "1"}),
        ent("identifier", "Door No 2/22",
            {"kind": "door_old", "value": "2/22", "lot_index": "1"}),
        ent("identifier", "Door No 2/79/C",
            {"kind": "door_new", "value": "2/79/C", "lot_index": "1"}),
        ent("auction_terms", "Rs.9,50,000",
            {"reserve_price_num": 950000, "lot_index": "1"}),
        ent("full_description", "All that piece and parcel...",
            {"lot_index": "1"}),
    ]
    lots = AX.group_lots(ents)
    assert set(lots) == {"1"}
    lot = lots["1"]
    f = lot["fields"]
    assert f["village"] == "Padur"
    assert f["registration_sub_district"] == "Thiruporur"
    assert f["boundary_north"] == "Plot No.4"
    assert f["boundary_measurement_north"] == "40 Feet"
    assert f["total_area"] == "1200 sq.ft"
    assert f["undivided_share"] == "509 sq.ft UDS"
    assert f["door_numbers_old"] == "2/22"
    assert f["door_numbers_new"] == "2/79/C"
    assert lot["reserve"] == 950000
    assert lot["description"] == "All that piece and parcel..."


def test_group_lots_separates_lot_indexes():
    ents = [
        ent("location", "", {"village": "A", "lot_index": "1"}),
        ent("location", "", {"village": "B", "lot_index": "2"}),
        ent("auction_terms", "", {"reserve_price_num": 100000, "lot_index": "1"}),
        ent("auction_terms", "", {"reserve_price_num": 200000, "lot_index": "2"}),
    ]
    lots = AX.group_lots(ents)
    assert lots["1"]["fields"]["village"] == "A"
    assert lots["2"]["fields"]["village"] == "B"
    assert lots["2"]["reserve"] == 200000


def test_group_lots_missing_lot_index_defaults_to_1():
    ents = [ent("location", "", {"village": "A"})]
    assert AX.group_lots(ents)["1"]["fields"]["village"] == "A"


def test_group_lots_boundary_falls_back_to_text():
    ents = [ent("boundary", "Road", {"side": "east"})]
    assert AX.group_lots(ents)["1"]["fields"]["boundary_east"] == "Road"


def test_group_lots_first_non_null_wins():
    ents = [
        ent("location", "", {"village": "First"}),
        ent("location", "", {"village": "Second"}),
    ]
    assert AX.group_lots(ents)["1"]["fields"]["village"] == "First"


def test_group_lots_carries_its_own_index():
    """lot_index travels with the lot record through match_lots_to_listings
    unchanged — it's what write_lot_matches uses to rebuild the SAME
    lot_key promote_extractions.py wrote onto the :Lot graph node."""
    ents = [
        ent("location", "", {"village": "A", "lot_index": "1"}),
        ent("location", "", {"village": "B", "lot_index": "2"}),
    ]
    lots = AX.group_lots(ents)
    assert lots["1"]["lot_index"] == "1"
    assert lots["2"]["lot_index"] == "2"


# ── match_lots_to_listings ───────────────────────────────────────────────────

def _lot(reserve, desc="d", emd=None, borrowers=None):
    return {"description": desc, "fields": {"village": "V"},
            "reserve": reserve, "emd": emd,
            "borrower_tokens": AX._name_tokens(borrowers or "")}


def test_match_single_lot_applies_to_all_listings():
    lots = {"1": _lot(500000)}
    listings = [{"aid": "a1", "price": 500000}, {"aid": "a2", "price": None}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert [m[2] for m in matches] == ["single", "single"]
    assert unmatched == []


def test_match_exact_and_tolerance():
    lots = {"1": _lot(500000), "2": _lot(1000000)}
    listings = [{"aid": "a1", "price": 500000},
                {"aid": "a2", "price": 1004000}]   # within 1%
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    reasons = {m[0]["aid"]: m[2] for m in matches}
    assert reasons == {"a1": "exact", "a2": "tolerance"}
    assert unmatched == []


def test_match_same_price_lots_are_ambiguous_not_guessed():
    lots = {"1": _lot(1250000), "2": _lot(1250000)}
    listings = [{"aid": "a1", "price": 1250000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_match_unique_remainder_pairs_last_lot_and_listing():
    lots = {"1": _lot(500000), "2": _lot(None)}
    listings = [{"aid": "a1", "price": 500000},
                {"aid": "a2", "price": 750000}]    # no price match anywhere
    matches, _ = AX.match_lots_to_listings(lots, listings)
    reasons = {m[0]["aid"]: m[2] for m in matches}
    assert reasons["a2"] == "remainder"


def test_match_no_price_no_emd_multi_lot_unmatched():
    lots = {"1": _lot(500000), "2": _lot(600000)}
    listings = [{"aid": "a1", "price": None}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "no_listing_price"


def test_match_emd_rescues_listing_without_reserve_price():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": None, "emd": 60000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    assert matches[0][1]["reserve"] == 600000
    assert matches[0][2] == "emd"


def test_match_emd_tolerance():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": None, "emd": 60300}]  # within 1%
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][2] == "emd_tolerance"


def test_match_emd_rescues_price_that_matches_no_lot():
    # portal price is a 10x typo, but its EMD still pins the lot
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": 5_000_000, "emd": 50000}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][1]["reserve"] == 500000
    assert matches[0][2] == "emd"


def test_match_equal_emds_stay_ambiguous():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=50000)}
    listings = [{"aid": "a1", "price": None, "emd": 50000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_match_price_still_wins_over_emd():
    # price matches lot 1 exactly; a misleading emd points at lot 2
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": 500000, "emd": 60000}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][1]["reserve"] == 500000
    assert matches[0][2] == "exact"


def test_match_borrower_separates_equal_reserve_prices():
    # two lots, same reserve — EMD ties too (10% of reserve); borrower decides
    lots = {"1": _lot(500000, emd=50000, borrowers="Smt. J. Ida Priscilla"),
            "2": _lot(500000, emd=50000, borrowers="Sri. E. Rajendran")}
    listings = [{"aid": "a1", "price": 500000, "emd": 50000,
                 "borrowers": ["Mrs Ida Priscilla W/o Moses"]},
                {"aid": "a2", "price": 500000, "emd": 50000,
                 "borrowers": ["Mr E Rajendran"]}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    got = {m[0]["aid"]: (m[1]["reserve"], m[2]) for m in matches}
    assert got["a1"][1] == "borrower"
    assert got["a2"][1] == "borrower"
    assert matches[0][1] is not matches[1][1]   # different lots


def test_match_borrower_alone_can_pair_when_money_is_missing():
    lots = {"1": _lot(None, borrowers="Musthafa M"),
            "2": _lot(None, borrowers="Sabeena A")}
    listings = [{"aid": "a1", "price": None, "emd": None,
                 "borrowers": ["Mr/Mrs Musthafa M"]}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    assert matches[0][2] == "borrower"


def test_match_shared_borrower_stays_ambiguous():
    # sibling lots of one borrower — same money, same name: never guess
    lots = {"1": _lot(500000, borrowers="Ramayee Chellammal"),
            "2": _lot(500000, borrowers="Ramayee Prakash")}
    listings = [{"aid": "a1", "price": 500000, "borrowers": ["Ramayee"]}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_match_ocr_fused_borrower_name_still_hits():
    lots = {"1": _lot(500000, borrowers="SGayathri"),
            "2": _lot(500000, borrowers="Karuppan")}
    listings = [{"aid": "a1", "price": 500000, "borrowers": ["Mrs S Gayathri"]}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][2] == "borrower"


def test_match_identifier_separates_lots_sharing_borrower_and_money():
    # sibling lots: same borrower, same reserve — survey number decides
    l1 = _lot(500000, borrowers="Ramayee")
    l1["id_tokens"] = AX._id_tokens("S.F.No. 491/1")
    l2 = _lot(500000, borrowers="Ramayee")
    l2["id_tokens"] = AX._id_tokens("S.F.No. 203/2A")
    lots = {"1": l1, "2": l2}
    listings = [{"aid": "a1", "price": 500000, "borrowers": ["Ramayee"],
                 "id_text": "Vacant land in S F No.203/2A Kanakkampalayam"}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    assert matches[0][1] is l2
    assert matches[0][2] == "identifier"


def test_id_tokens_normalize_separators_and_drop_years_pincodes():
    toks = AX._id_tokens("R.S. No. 32-12B, dated 12.05.2026, Cuddalore-608502")
    assert "32/12b" in toks
    assert not any(t in ("2026", "608502") for t in toks)
    # date fragments normalize but full years/pincodes as bare tokens are gone
    assert AX._id_tokens("built in 2019 pin 641604") == set()


def test_match_strongest_identifier_overlap_wins_over_shared_land():
    """Shaped after auction 682880: two flats in one building, same reserve
    (₹45L), same borrower, and schedules quoting the SAME land underneath —
    survey number, neighbouring plots, parcel measurements. Every candidate
    therefore shares tokens with the listing, so 'has any overlap' matches
    both. The assessment number is assigned per flat and is the only
    discriminator: the right lot is the one overlapping MOST.
    """
    shared = "S.No.68/5C Plot No.15 North by Plot No.14 South by Plot No.16"
    l1 = _lot(4500000, borrowers="SRK Building Mall")
    l1["id_tokens"] = AX._id_tokens(shared + " Assessment No.115/025/00207")
    l2 = _lot(4500000, borrowers="SRK Building Mall")
    l2["id_tokens"] = AX._id_tokens(shared + " Assessment No.115/025/00209")
    lots = {"1": l1, "2": l2}
    listings = [{"aid": "682880", "price": 4500000, "emd": 450000,
                 "borrowers": ["M/s.SRK Building Mall"],
                 "id_text": shared + " Assessment No. 115/025/00209"}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    assert matches[0][1] is l2
    assert matches[0][2] == "identifier"


def test_sole_claimants_drops_a_lot_two_listings_both_claim():
    """From the 12-lot PNB notice, which carries 19 listings: the surplus
    listings pile onto whichever lots they most resemble, and 802424 and
    802425 both landed on lot 3 — which 802413 already owns on an exact
    price. At most one can be right, so none of the rivals may assert a
    lot-scoped key; the lot only one listing claims is untouched.
    """
    solo, contested = _lot(1813000), _lot(3240000)
    a = {"aid": "802420"}
    b, c = {"aid": "802424"}, {"aid": "802425"}
    matches = [(a, solo, "exact"), (b, contested, "identifier"),
               (c, contested, "identifier")]
    kept = AX.sole_claimants(matches)
    assert [m[0]["aid"] for m in kept] == ["802420"]


def test_sole_claimants_keeps_distinct_lots_that_merely_look_alike():
    """Sibling flats are equal by value — same price, same fields — so the
    check has to be per lot RECORD, not per equal-looking dict, or two
    correct matches onto two indistinguishable lots would cancel out."""
    twin_a, twin_b = _lot(4500000), _lot(4500000)
    assert twin_a == twin_b            # equal by value, distinct records
    matches = [({"aid": "1"}, twin_a, "identifier"),
               ({"aid": "2"}, twin_b, "identifier")]
    assert len(AX.sole_claimants(matches)) == 2


def test_match_equal_identifier_overlap_stays_ambiguous():
    """Only a STRICTLY strongest overlap decides. Two lots quoting the same
    land and nothing unit-specific tie, and a tie must never be guessed."""
    shared = "S.No.68/5C Plot No.15 North by Plot No.14"
    l1 = _lot(4500000)
    l1["id_tokens"] = AX._id_tokens(shared)
    l2 = _lot(4500000)
    l2["id_tokens"] = AX._id_tokens(shared)
    lots = {"1": l1, "2": l2}
    listings = [{"aid": "a1", "price": 4500000, "id_text": shared}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_group_lots_harvests_identifiers_from_the_schedule_text():
    """The assessment number lives in the schedule prose, not in an
    `identifier` entity — without harvesting full_description it never
    becomes a token and can never separate sibling flats."""
    ents = [ent("full_description",
                "Flat B4 ... Assessment No.115/025/00209 Old Assessment "
                "No.115/347430 in S.No.68/5C", {"lot_index": "1"})]
    toks = AX.group_lots(ents)["1"]["id_tokens"]
    assert "115/025/00209" in toks
    assert "115/347430" in toks
    assert "68/5c" in toks


def test_match_identifier_does_not_fire_on_no_overlap():
    l1 = _lot(500000)
    l1["id_tokens"] = AX._id_tokens("491/1")
    l2 = _lot(500000)
    l2["id_tokens"] = AX._id_tokens("203/2A")
    lots = {"1": l1, "2": l2}
    listings = [{"aid": "a1", "price": 500000, "id_text": "no ids here"}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_name_tokens_drop_honorifics_and_initials():
    toks = AX._name_tokens("Smt. P. Karnagi W/o. Mr. Pavadai (Borrower)")
    assert toks == {"karnagi", "pavadai"}


def test_group_lots_captures_borrower_tokens():
    ents = [ent("borrower", "Sri. Ganeshkumar S/o. Mr. Pavadai",
                {"role": "guarantor", "lot_index": "1"})]
    lots = AX.group_lots(ents)
    assert "ganeshkumar" in lots["1"]["borrower_tokens"]


def test_group_lots_captures_emd():
    ents = [ent("auction_terms", "",
                {"reserve_price_num": "500000", "emd_num": "50000",
                 "lot_index": "1"})]
    lots = AX.group_lots(ents)
    assert lots["1"]["reserve"] == 500000
    assert lots["1"]["emd"] == 50000


# ── write behavior ───────────────────────────────────────────────────────────

def test_write_descriptions_overwrites_all_but_backs_up_human(monkeypatch):
    """Notice text is the sole source: no human/verified guard remains, and a
    human-entered description is stashed once into description_human_backup."""
    captured = {}

    def _cap(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_descriptions([{"aid": "a1", "desc": "D"}])
    assert n == 1
    assert "description_verified" not in captured["cypher"]
    assert "description_human_backup" in captured["cypher"]
    # backup only fills once — a second run must not clobber the stash
    assert "description_human_backup IS NULL" in captured["cypher"]
    # a reviewer's correction outranks the automated write
    assert "<> 'reviewer'" in captured["cypher"]


def test_write_fields_sets_provenance(monkeypatch):
    captured = {}

    def _cap(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_fields([{"aid": "a1", "filename": "f.pdf",
                          "props": {"village": "Padur"}}])
    assert n == 1
    assert "grounded_extraction" in captured["cypher"]
    assert captured["params"]["rows"][0]["props"] == {"village": "Padur"}


def test_write_lot_matches_links_the_lot_and_records_the_decision(monkeypatch):
    calls = []

    def _cap(cypher, params=None):
        calls.append((cypher, params))
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_lot_matches(
        [{"aid": "a1", "lot_key": "notice.jpg#3", "reason": "exact"}])
    assert n == 1
    assert len(calls) == 3   # link the lot, delete stale decision, merge new

    link_cypher, link_params = calls[0]
    # Phase 4: the edge IS the resolution — the string it replaced is gone.
    assert "MERGE (a)-[r:IS_LOT]->(l)" in link_cypher
    assert "resolved_lot_key" not in link_cypher
    # MATCH, not MERGE, on the lot: a listing whose lot does not exist yet
    # must come back unwritten rather than conjuring an empty :Lot.
    assert "MATCH (l:Lot {lot_key: row.lot_key})" in link_cypher
    row = link_params["rows"][0]
    assert row["lot_key"] == "notice.jpg#3"
    assert row["decision_key"] == "lot-match:a1|notice.jpg#3"

    delete_cypher, delete_params = calls[1]
    assert "DETACH DELETE" in delete_cypher
    assert delete_params["rows"][0]["aid"] == "a1"

    merge_cypher, merge_params = calls[2]
    assert "MERGE (r:ResolutionDecision" in merge_cypher
    assert "system:apply_extractions" in merge_cypher
    payload = json.loads(merge_params["rows"][0]["payload"])
    assert payload == {"auction_id": "a1", "lot_key": "notice.jpg#3",
                       "method": "exact"}


def test_write_lot_matches_empty_is_a_noop(monkeypatch):
    monkeypatch.setattr(
        AX, "run_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    assert AX.write_lot_matches([]) == 0


def test_human_decided_lot_matches_excludes_automated_verdicts(monkeypatch):
    """Only a person's decided_by (not a 'system:' prefix) counts — the
    result gates write_lot_matches from ever touching a human's pick."""
    rows = [
        {"payload_json": json.dumps({"auction_id": "a1", "lot_key": "x#1"})},
        {"payload_json": json.dumps({"auction_id": "a2", "lot_key": "y#1"})},
    ]

    def _fake_read(cypher, params=None, **kw):
        assert "NOT r.decided_by STARTS WITH 'system:'" in cypher
        return rows

    monkeypatch.setattr(AX, "run_read_query", _fake_read)
    assert AX.human_decided_lot_matches() == {"a1", "a2"}


# ── run(): the description write is gated the same way the lot key is ────────

def _lot_ents(lot_index, desc, reserve):
    """A minimal lot: one schedule span plus its reserve price."""
    return [
        ent("full_description", desc, {"lot_index": lot_index}),
        ent("auction_terms", "", {"lot_index": lot_index,
                                  "reserve_price_num": reserve}),
    ]


def _run_capturing(monkeypatch, tmp_path, work):
    """Run the pipeline against `work`, returning what each write received."""
    seen = {}
    # keep the unmatched-CSV side effect out of the repo
    monkeypatch.setattr(AX, "UNMATCHED_CSV", tmp_path / "unmatched.csv")
    monkeypatch.setattr(AX, "fetch_work", lambda limit=None: work)
    monkeypatch.setattr(AX, "human_decided_lot_matches", lambda: set())
    for name in ("write_fields", "clear_unsafe_fields", "write_descriptions",
                 "write_lot_matches", "clear_stale_lot_matches",
                 "write_price_findings", "revert_withheld_descriptions"):
        monkeypatch.setattr(
            AX, name,
            # tuple-index, not `and`: an empty rows list is falsy and would
            # short-circuit to [], which run() then compares against an int
            (lambda key: lambda rows: (seen.setdefault(key, rows), len(rows))[1])(name))
    AX.run()
    return {k: {r["aid"] for r in seen.get(k, [])}
            for k in ("write_fields", "clear_unsafe_fields",
                      "write_descriptions", "write_lot_matches")}


def test_rival_listings_on_a_multi_lot_notice_get_no_description(monkeypatch, tmp_path):
    """Two listings claiming one lot cannot both be that property, so neither
    may be handed its schedule — the portal's own text is the honest fallback.

    Reproduces the four counted cases (794656, 811144, 837423, 837424) where a
    listing carried a neighbouring lot's description while resolved_lot_key
    was NULL, because only the lot-key write was gated.
    """
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            # both tie on lot 1's reserve, so both claim it
            {"aid": "rivalA", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "rivalB", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "clean", "price": 7000000, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_descriptions"] == {"clean"}
    assert got["write_lot_matches"] == {"clean"}


def test_rivals_get_consensus_fields_and_lose_contested_ones(monkeypatch, tmp_path):
    """The field write used to sit outside the rivalry gate entirely — a
    contested listing was handed its guessed lot's fields as plain fact. Now
    it splits by value (2026-08-31 decision, option C): a field every lot
    agrees on is a notice-fact and still flows to the rivals; a field only
    one lot carries names that lot, so it is withheld AND queued for clearing
    off any earlier run's write."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            # 'Padur' on lot 1 only — contested. Both lots name taluk 'Salem'
            # — consensus, so it survives the gate.
            [ent("location", "", {"lot_index": "1", "village": "Padur"}),
             ent("location", "", {"lot_index": "1", "taluk": "Salem"}),
             ent("location", "", {"lot_index": "2", "taluk": "Salem"})]
            + _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            {"aid": "rivalA", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "rivalB", "price": 5000000, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_fields"] == {"rivalA", "rivalB"}          # taluk only
    assert got["clear_unsafe_fields"] == {"rivalA", "rivalB"}   # village out
    assert got["write_descriptions"] == set()


def test_a_single_lot_notice_keeps_every_listings_description(monkeypatch, tmp_path):
    """Every listing on a one-lot notice legitimately claims the only lot, so
    sole_claimants drops all of them. Applying the gate there would strip
    descriptions from the least ambiguous notices in the corpus."""
    work = [{
        "filename": "solo.pdf",
        "extraction_json": json.dumps(_lot_ents("1", "The only schedule", 900000)),
        "corrections_json": None,
        "listings": [
            {"aid": "one", "price": 900000, "emd": None, "borrowers": []},
            {"aid": "two", "price": 900000, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_descriptions"] == {"one", "two"}
    # ...and a single-lot notice never needs a lot key
    assert got["write_lot_matches"] == set()


# ── description_verdict: the two ways a description can be about the wrong lot ─

_SCHEDULE = ("All that piece and parcel of land in Semmar Village, "
             "Villupuram District, Survey No 45/2, extent 1168 sq ft")


def test_a_description_matching_its_portal_text_is_published():
    assert AX.description_verdict(_SCHEDULE, _SCHEDULE, sole_claimant=True) is None


def test_a_rival_claimant_is_withheld_before_overlap_is_even_considered():
    """The rivalry gate runs first: a perfect textual match cannot rescue a lot
    two listings both claim, because at most one of them is that property."""
    assert AX.description_verdict(_SCHEDULE, _SCHEDULE,
                                  sole_claimant=False) == "claimed_by_several"


def test_a_description_about_another_state_is_withheld():
    """The shape of 840337: a Chennai listing handed a Chhattisgarh schedule.
    It is its lot's sole claimant, so only the overlap gate can catch it."""
    portal = ("THE ENTIRE SECOND FLOOR residential portion, Old Door No 33/6, "
              "Varadha Muthiappan Street, George Town, Chennai 600001")
    notice = ("Land and double storied residential building at Plot No 10, "
              "Kh No 40/36, Mouza Dung, Durg, Chhattisgarh, area 1305 sq ft")
    assert AX.description_overlap(portal, notice) < AX.MIN_DESCRIPTION_OVERLAP
    assert AX.description_verdict(notice, portal,
                                  sole_claimant=True) == "diverges_from_portal"


def test_a_notice_that_merely_adds_detail_is_still_published():
    """The point of reading the notice is the detail the portal's blurb omits;
    the guard must not treat that as disagreement."""
    portal = "Land in Semmar Village, Villupuram District, Survey No 45/2"
    notice = _SCHEDULE + ", bounded north by road and south by channel"
    assert AX.description_overlap(portal, notice) >= AX.MIN_DESCRIPTION_OVERLAP
    assert AX.description_verdict(notice, portal, sole_claimant=True) is None


def test_a_listing_with_no_portal_text_is_not_gated_on_overlap():
    """Silence is not disagreement. Gating here would strip descriptions from
    rows whose portal text was simply never scraped."""
    assert AX.description_verdict(_SCHEDULE, None, sole_claimant=True) is None
    assert AX.description_verdict(_SCHEDULE, "   ", sole_claimant=True) is None


def test_overlap_is_symmetric_so_a_long_notice_cannot_pass_on_length():
    a, b = "alpha beta gamma", "beta gamma " + " ".join(f"w{i}" for i in range(50))
    assert AX.description_overlap(a, b) == AX.description_overlap(b, a)


def test_run_withholds_a_diverging_description_but_keeps_the_fields(monkeypatch, tmp_path):
    """End to end through run(): the single listing on this notice IS its lot's
    sole claimant, so only the overlap gate stands between it and a description
    about a different property."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            [ent("location", "", {"lot_index": "1", "village": "Dung"})]
            + _lot_ents("1", "Land at Mouza Dung, Durg, Chhattisgarh, 1305 sq ft",
                        900000)),
        "corrections_json": None,
        "listings": [{
            "aid": "chennai", "price": 900000, "emd": None, "borrowers": [],
            "portal": "Second floor flat, Varadha Muthiappan Street, George Town, Chennai",
        }],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_descriptions"] == set()
    assert got["write_fields"] == {"chennai"}


# ── revert: withholding must also undo what an earlier run published ─────────

def test_revert_restores_the_portal_text_and_relabels_the_source(monkeypatch):
    captured = {}

    def _cap(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.revert_withheld_descriptions(
        [{"aid": "a1", "reason": "diverges_from_portal"}])
    assert n == 1
    assert "a.description = a.website_description" in captured["cypher"]
    assert "a.description_source = 'website'" in captured["cypher"]
    assert captured["params"]["rows"][0]["reason"] == "diverges_from_portal"


def test_revert_only_touches_rows_this_pipeline_published(monkeypatch):
    """A reviewer's text outranks every automated write, revert included, and
    a row that never carried a notice description has nothing to undo."""
    captured = {}
    monkeypatch.setattr(AX, "run_query",
                        lambda c, p=None: captured.setdefault("cypher", c) and [])
    AX.revert_withheld_descriptions([{"aid": "a1", "reason": "x"}])
    assert "a.description_source = 'notice'" in captured["cypher"]


def test_revert_skips_a_listing_with_no_portal_text_to_fall_back_on(monkeypatch):
    """Blanking a description is its own kind of wrong — the Cypher requires a
    non-empty website_description, and run() reports the residue."""
    captured = {}
    monkeypatch.setattr(AX, "run_query",
                        lambda c, p=None: captured.setdefault("cypher", c) and [])
    AX.revert_withheld_descriptions([{"aid": "a1", "reason": "x"}])
    assert "a.website_description IS NOT NULL" in captured["cypher"]
    assert "trim(a.website_description) <> ''" in captured["cypher"]


def test_revert_empty_is_a_noop(monkeypatch):
    monkeypatch.setattr(
        AX, "run_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    assert AX.revert_withheld_descriptions([]) == 0


def _run_capturing_reverts(monkeypatch, tmp_path, work):
    """run(), returning {aid: reason} for everything queued to revert."""
    seen = {}
    monkeypatch.setattr(AX, "UNMATCHED_CSV", tmp_path / "unmatched.csv")
    monkeypatch.setattr(AX, "fetch_work", lambda limit=None: work)
    monkeypatch.setattr(AX, "human_decided_lot_matches", lambda: set())
    for name in ("write_fields", "write_descriptions", "write_lot_matches",
                 "clear_stale_lot_matches", "write_price_findings"):
        monkeypatch.setattr(AX, name, lambda rows: len(rows))
    monkeypatch.setattr(
        AX, "revert_withheld_descriptions",
        lambda rows: (seen.update({r["aid"]: r["reason"] for r in rows}), len(rows))[1])
    AX.run()
    return seen


def test_a_withheld_listing_is_queued_for_revert(monkeypatch, tmp_path):
    """The gate stops the next write; the revert is what fixes the 126 rows
    published before the gates existed."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            {"aid": "rivalA", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "rivalB", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "clean", "price": 7000000, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing_reverts(monkeypatch, tmp_path, work)
    assert got == {"rivalA": "claimed_by_several", "rivalB": "claimed_by_several"}


def test_an_unmatched_listing_is_reverted_too(monkeypatch, tmp_path):
    """No lot means no description this run, so anything an earlier run left
    behind is now unbacked by any match."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            {"aid": "nomoney", "price": None, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing_reverts(monkeypatch, tmp_path, work)
    assert got == {"nomoney": "unmatched_no_listing_price"}


def test_a_published_listing_is_never_queued_for_revert(monkeypatch, tmp_path):
    work = [{
        "filename": "solo.pdf",
        "extraction_json": json.dumps(_lot_ents("1", "The only schedule", 900000)),
        "corrections_json": None,
        "listings": [{"aid": "one", "price": 900000, "emd": None, "borrowers": []}],
    }]
    assert _run_capturing_reverts(monkeypatch, tmp_path, work) == {}


# ── DESCRIPTION_OVERLAP_REVIEWED_CORRECT: a human's call beats the heuristic ──

def test_a_reviewed_listing_publishes_despite_low_overlap(monkeypatch, tmp_path):
    """840337's own case: notice text is correct (Durg, Chhattisgarh), the
    portal's scraped text is wrong (a mismatched Chennai flat). A human
    confirmed this by reading the actual notice, so the overlap gate — which
    cannot tell 'our text is wrong' apart from 'their text is wrong' — must
    not override that."""
    assert "840337" in AX.DESCRIPTION_OVERLAP_REVIEWED_CORRECT
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            [ent("location", "", {"lot_index": "1", "village": "Dung"})]
            + _lot_ents("1", "Land at Mouza Dung, Durg, Chhattisgarh, 1305 sq ft",
                        900000)),
        "corrections_json": None,
        "listings": [{
            "aid": "840337", "price": 900000, "emd": None, "borrowers": [],
            "portal": "Second floor flat, Varadha Muthiappan Street, George Town, Chennai",
        }],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_descriptions"] == {"840337"}


def test_the_review_override_does_not_excuse_a_rival_claimant(monkeypatch, tmp_path):
    """The reviewed fact is about the TEXT, not about which lot the listing
    resolved to — if 840337 were still fighting another listing over the same
    lot, that conflict is unaffected by the override."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            {"aid": "840337", "price": 5000000, "emd": None, "borrowers": [],
             "portal": "unrelated text"},
            {"aid": "rival", "price": 5000000, "emd": None, "borrowers": [],
             "portal": "unrelated text"},
        ],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert "840337" not in got["write_descriptions"]


# ── a key this run did not re-derive must not survive it ────────────────────
# write_lot_matches only ever SET. A listing that stopped resolving kept its
# old key forever, and the key still RESOLVED, so nothing noticed. 750335 held
# CB17767669373793.jpg#2 after it stopped matching lot 2; a later run gave lot
# 2 to 750336, and the notice ended with two listings claiming one property —
# the outcome sole_claimants exists to prevent, reached by leaving a key behind
# rather than by writing a bad one. These pin the shape of the clear.

def _clear_src() -> str:
    import inspect
    return inspect.getsource(AX.clear_stale_lot_matches)


def _run_src() -> str:
    import inspect
    return inspect.getsource(AX.run)


def test_clear_only_touches_keys_from_this_document():
    """lot_key embeds the filename. 12 listings link to two documents, and a
    pass over one must not wipe a key the other legitimately wrote."""
    src = _clear_src()
    assert "STARTS WITH (row.filename + '#')" in src


def test_clear_removes_the_resolution_decision_too():
    """A decision outliving the value it justified would be re-applied by the
    review app's next 'Apply my decisions' run."""
    src = _clear_src()
    assert "DETACH DELETE r" in src
    assert "ResolutionDecision" in src


def test_clear_never_touches_a_human_decision():
    """Two guards: the caller filters human_decided out of the rows, and the
    delete itself only removes verdicts a system wrote."""
    assert "aid not in human_decided" in _run_src()
    assert "r.decided_by STARTS WITH 'system:'" in _clear_src()


def test_every_unresolved_listing_on_the_document_is_a_clear_candidate():
    """Not just the contested ones — an unmatched listing, or one whose lot
    vanished from the extraction, is equally stale."""
    src = _run_src()
    assert "aid not in resolved_this_doc" in src


def test_the_clear_runs_after_the_write():
    """Order matters: a listing that moved from one lot to another this run
    must not be cleared by its own new key's document pass."""
    src = _run_src()
    assert src.index("write_lot_matches(lot_key_rows)") < \
           src.index("clear_stale_lot_matches(stale_key_rows)")


# ── price agreement wiring ───────────────────────────────────────────────────

def _price_src() -> str:
    import inspect
    return inspect.getsource(AX.write_price_findings)


def test_run_checks_every_matched_pair_for_price_agreement():
    assert "check_document(matches)" in _run_src()


def test_run_writes_the_price_findings_it_collects():
    """A finding only collected is a finding nobody sees."""
    src = _run_src()
    assert "price_rows.append" in src
    assert "write_price_findings(price_rows)" in src


def test_every_writer_run_calls_is_stubbed_by_the_test_helpers():
    """The guard against the hang this suite already caught once.

    A writer added to run() but not to the helpers' stub lists reaches the
    live database mid-test and blocks. Reading the names out of run() itself
    means a future writer fails here rather than hanging.
    """
    import re
    called = set(re.findall(r"\b(write_\w+|clear_\w+|revert_\w+)\(", _run_src()))
    stubbed = set(re.findall(r'"(write_\w+|clear_\w+|revert_\w+)"',
                             _read_own_source()))
    assert called and called <= stubbed, f"not stubbed: {sorted(called - stubbed)}"


def _read_own_source() -> str:
    from pathlib import Path
    return Path(__file__).read_text(encoding="utf-8")


def test_price_findings_are_rebuilt_not_merged():
    """A verdict must not outlive the extraction that justified it.

    Clearing before writing is what makes a corrected price drop its flag,
    rather than the listing keeping a stale accusation forever — the same
    defect write_lot_matches had.
    """
    src = _price_src()
    assert "REMOVE a.price_agreement" in src
    clear_at = src.index("REMOVE a.price_agreement")
    assert clear_at < src.index("SET a.price_agreement =")


# ── phase 4: the edge is the resolution ──────────────────────────────────────

def _link_src() -> str:
    import inspect
    return inspect.getsource(AX.write_lot_matches)


def test_the_matcher_writes_no_string_at_all():
    """Phase 4 removed resolved_lot_key. Leaving a write behind would put the
    graph back to two sources of truth that only mostly agree.

    The Cypher only — the docstring names the retired property on purpose,
    to say what this replaced and why.
    """
    import re
    cypher = "\n".join(re.findall(r'"""(.*?)"""', _link_src(), re.S)[1:])
    assert "resolved_lot_key" not in cypher
    assert "lot_resolved_at" not in cypher
    assert "MERGE (a)-[r:IS_LOT]->(l)" in cypher


def test_a_missing_lot_is_reported_rather_than_swallowed():
    """After Phase 4 the edge IS the resolution, so a row with no :Lot is a
    listing left UNRESOLVED — not merely a key that fails to dereference."""
    src = _link_src()
    assert "missing = len(rows) - written" in src
    assert "promote_extractions" in src


def test_clearing_drops_the_edge_not_a_property():
    src = _clear_src()
    assert "DELETE r" in src
    assert "REMOVE a.resolved_lot_key" not in src


def test_clearing_still_only_touches_this_documents_lots():
    """12 listings link to two notices. The filename guard the key version
    carried, now expressed against the lot instead of a string prefix."""
    assert "l.lot_key STARTS WITH (row.filename + '#')" in _clear_src()


def test_apply_no_longer_calls_the_retired_link_step():
    """link_lots derived the edge FROM the string; there is no string left."""
    import inspect
    assert "link_lots" not in inspect.getsource(AX.run)


# ── single-lot notices are linked too ────────────────────────────────────────

def test_a_single_lot_notice_with_one_listing_is_linked():
    """These used to be skipped: scope_of() reads them as lot-scoped without
    an edge, so the link added nothing.

    After Phase 4 the edge is the ONLY statement that a listing IS a given
    lot, so skipping left 1,009 properties unable to answer "which lot?" even
    where the answer is unambiguous.
    """
    lot = _lot(reserve=100)
    matches, _ = AX.match_lots_to_listings({"1": lot}, [{"aid": "a"}])
    assert [m[2] for m in matches] == ["single"]
    assert {m[0]["aid"] for m in AX.sole_claimants(matches)} == {"a"}


def test_two_listings_on_a_single_lot_notice_are_still_contested():
    """One lot cannot be both of them.

    No special case is needed for this — `sole_claimants` drops a match whose
    lot another listing also claims, exactly as on a multi-lot notice. Two
    notices in the corpus carry 2 and 17 listings against one lot.
    """
    lot = _lot(reserve=100)
    matches, _ = AX.match_lots_to_listings({"1": lot},
                                           [{"aid": "a"}, {"aid": "b"}])
    assert len(matches) == 2
    assert AX.sole_claimants(matches) == []


def test_the_single_lot_skip_is_gone():
    """The guard that made single-lot notices a special case."""
    import inspect
    src = inspect.getsource(AX.run)
    assert "if len(lots) > 1:" not in src


# ── explain_lot_match ────────────────────────────────────────────────────────

def _xlot(lot_index, reserve, emd=None, borrowers=None):
    """`_lot` plus the lot_index explain_lot_match reports back."""
    return dict(_lot(reserve, emd=emd, borrowers=borrowers),
                lot_index=lot_index, id_tokens=set())


def test_explain_reports_the_tier_the_writer_actually_used():
    """The queue's reason must name the key that decided, not a guess.

    `lot_resolution.resolve_lot` — what the queue used to call — knows only
    reserve price and borrower name, so a listing the portal published without
    a price could only ever read as unresolvable to it, however cleanly its
    EMD names one lot.
    """
    lots = {"1": _xlot("1", reserve=100, emd=10),
            "2": _xlot("2", reserve=200, emd=20)}
    out = AX.explain_lot_match(lots, [{"aid": "a", "price": None, "emd": 20}])
    assert out["a"]["outcome"] == "linked"
    assert out["a"]["tier"] == "emd"
    assert out["a"]["lot_index"] == "2"
    assert "EMD" in out["a"]["reason"]


def test_explain_tier_is_the_first_key_that_hit_not_the_deciding_one():
    """Faithful to the writer's own label, which is the contract here.

    Reserve price hits both lots and EMD then narrows to one, but the matcher
    records 'exact' — the first key that produced any hit. explain_lot_match
    reports what the writer decided; it does not re-derive a nicer story.
    """
    lots = {"1": _xlot("1", reserve=100, emd=10),
            "2": _xlot("2", reserve=100, emd=20)}
    out = AX.explain_lot_match(lots, [{"aid": "a", "price": 100, "emd": 20}])
    assert out["a"]["lot_index"] == "2" and out["a"]["tier"] == "exact"


def test_explain_separates_a_contested_lot_from_a_genuine_tie():
    """The distinction the old queue could not draw.

    Both listings match lot 1 exactly, so `sole_claimants` refuses to write
    either edge. That is NOT 'ambiguous' — the notice is perfectly clear, two
    portal listings are fighting over one lot — and the reviewer's job is to
    separate two rows, not to pick from N lots.
    """
    lots = {"1": _xlot("1", reserve=100), "2": _xlot("2", reserve=999)}
    out = AX.explain_lot_match(lots, [{"aid": "a", "price": 100},
                                      {"aid": "b", "price": 100}])
    assert out["a"]["outcome"] == out["b"]["outcome"] == "rival"
    assert out["a"]["rivals"] == ["b"] and out["b"]["rivals"] == ["a"]
    assert "b" in out["a"]["reason"]


def test_explain_calls_a_real_tie_ambiguous_with_no_rivals():
    lots = {"1": _xlot("1", reserve=100), "2": _xlot("2", reserve=100)}
    out = AX.explain_lot_match(lots, [{"aid": "a", "price": 100}])
    assert out["a"]["outcome"] == "unmatched"
    assert out["a"]["tier"] is None and out["a"]["rivals"] == []
    assert "tie" in out["a"]["reason"]


def test_explain_covers_every_listing_exactly_once():
    """No listing may fall out of the queue silently."""
    lots = {"1": _xlot("1", reserve=100), "2": _xlot("2", reserve=200)}
    listings = [{"aid": "a", "price": 100}, {"aid": "b", "price": 200},
                {"aid": "c", "price": 100}, {"aid": "d", "price": None}]
    out = AX.explain_lot_match(lots, listings)
    assert set(out) == {"a", "b", "c", "d"}


def test_explain_never_disagrees_with_the_writer():
    """The whole point: one matcher, two callers.

    Whatever `explain_lot_match` calls 'linked' is exactly what `run()` would
    write, because both go through `match_lots_to_listings` + `sole_claimants`.
    """
    lots = {"1": _xlot("1", reserve=100), "2": _xlot("2", reserve=200),
            "3": _xlot("3", reserve=300)}
    listings = [{"aid": "a", "price": 100}, {"aid": "b", "price": 200},
                {"aid": "c", "price": 200}, {"aid": "d", "price": 999}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    written = {m[0]["aid"] for m in AX.sole_claimants(matches)}
    explained = {aid for aid, v in AX.explain_lot_match(lots, listings).items()
                 if v["outcome"] == "linked"}
    assert explained == written


def test_explain_prose_exists_for_every_matcher_reason():
    """A reason string the matcher can return but the UI cannot phrase would
    surface a bare token like 'emd_tolerance' to a reviewer."""
    import inspect
    src = inspect.getsource(AX.match_lots_to_listings)
    for reason in ("single", "exact", "tolerance", "emd", "emd_tolerance",
                   "borrower", "identifier", "remainder", "ambiguous", "none",
                   "no_listing_price", "no_lots"):
        assert f'"{reason}"' in src, f"{reason} no longer produced by matcher"
        assert reason in AX._EXPLAIN_TEXT, f"{reason} has no reviewer prose"


# ── consensus_and_contested / the field-write gate ───────────────────────────

def _flot(lot_index, fields, reserve=None):
    return {"lot_index": lot_index, "description": None, "fields": fields,
            "reserve": reserve, "emd": None,
            "borrower_tokens": set(), "id_tokens": set()}


def test_consensus_keeps_what_every_lot_agrees_on():
    """A value identical on every lot is a notice-fact: true for a listing
    whichever lot it turns out to be, so it survives the gate."""
    lots = {"1": _flot("1", {"village": "Kannankurichi", "taluk": "Salem",
                             "door_numbers_new": "12A"}),
            "2": _flot("2", {"village": "Kannankurichi", "taluk": "Salem",
                             "door_numbers_new": "14B"})}
    consensus, contested = AX.consensus_and_contested(lots)
    assert consensus == {"village": "Kannankurichi", "taluk": "Salem"}
    assert contested == {"door_numbers_new"}


def test_a_key_only_some_lots_carry_is_contested():
    """Present on one lot and absent on another is not agreement — writing it
    to an unresolved listing asserts the lot that has it."""
    lots = {"1": _flot("1", {"village": "X", "total_area": "890 sq.ft"}),
            "2": _flot("2", {"village": "X"})}
    consensus, contested = AX.consensus_and_contested(lots)
    assert consensus == {"village": "X"}
    assert contested == {"total_area"}


def test_single_lot_notice_has_no_contested_keys():
    lots = {"1": _flot("1", {"village": "X", "total_area": "890 sq.ft"})}
    consensus, contested = AX.consensus_and_contested(lots)
    assert consensus == {"village": "X", "total_area": "890 sq.ft"}
    assert contested == set()


def test_confirmed_listing_still_gets_its_lot_s_full_fields():
    """The gate narrows only unresolved listings — a sole claimant keeps the
    exact behaviour it always had."""
    lots = {"1": _flot("1", {"village": "V", "total_area": "1000"}, reserve=100),
            "2": _flot("2", {"village": "V", "total_area": "2000"}, reserve=200)}
    matches, _ = AX.match_lots_to_listings(
        lots, [{"aid": "a", "price": 100}, {"aid": "b", "price": 200}])
    sole = {id(m[0]) for m in AX.sole_claimants(matches)}
    assert all(id(m[0]) in sole for m in matches)
