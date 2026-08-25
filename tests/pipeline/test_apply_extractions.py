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


def test_write_lot_matches_sets_key_and_decision(monkeypatch):
    calls = []

    def _cap(cypher, params=None):
        calls.append((cypher, params))
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_lot_matches(
        [{"aid": "a1", "lot_key": "notice.jpg#3", "reason": "exact"}])
    assert n == 1
    assert len(calls) == 3   # set resolved_lot_key, delete stale, merge new

    set_cypher, set_params = calls[0]
    assert "resolved_lot_key" in set_cypher
    assert "lot_resolved_at" in set_cypher
    row = set_params["rows"][0]
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
    for name in ("write_fields", "write_descriptions", "write_lot_matches"):
        monkeypatch.setattr(
            AX, name,
            (lambda key: lambda rows: seen.setdefault(key, rows) and len(rows))(name))
    AX.run()
    return {k: {r["aid"] for r in seen.get(k, [])}
            for k in ("write_fields", "write_descriptions", "write_lot_matches")}


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


def test_the_rivals_still_get_their_fields(monkeypatch, tmp_path):
    """The field write is deliberately outside this gate — narrowing it is a
    separate question, and this fix must not quietly change it."""
    work = [{
        "filename": "n.pdf",
        "extraction_json": json.dumps(
            [ent("location", "", {"lot_index": "1", "village": "Padur"})]
            + _lot_ents("1", "Flat A schedule", 5000000)
            + _lot_ents("2", "Flat B schedule", 7000000)),
        "corrections_json": None,
        "listings": [
            {"aid": "rivalA", "price": 5000000, "emd": None, "borrowers": []},
            {"aid": "rivalB", "price": 5000000, "emd": None, "borrowers": []},
        ],
    }]
    got = _run_capturing(monkeypatch, tmp_path, work)
    assert got["write_fields"] == {"rivalA", "rivalB"}
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
