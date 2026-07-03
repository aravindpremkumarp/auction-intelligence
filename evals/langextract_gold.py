"""Ground-truth labels for the LangExtract extraction eval.

Dependency-free (stdlib only) so it can be imported by the scorer and by tests.
Each entry: auction_id, notice_type, a flat dict of expected scalar `fields`
(None = not present / not scored), and expected `identifiers` (kind -> value).
Fixtures with the matching markdown live in evals/fixtures/<aid>.txt.

These 6 notices are deliberately NOT the two few-shot examples (no train/test
leakage) and span the hard cases: Karnataka khata/Hobli, DRT 'Upset Price',
flat+UDS, ARC assignor/trust, and tiled-house notices.

EXPECT_NULL is a distinct sentinel from None: None means "not scored" (we have
no opinion), EXPECT_NULL means "this field must come back empty" — e.g. a
possession clause that lists all three types disjunctively ("Constructive /
Symbolic / Physical Possession", sometimes literally an unfilled template:
"(mention whichever is applicable)") has no single correct answer, so the
"don't invent" rule says leave it null rather than emit the raw disjunction or
guess one.
"""
from __future__ import annotations

EXPECT_NULL = object()

GOLD = [
    {
        "aid": "737508", "notice_type": "single",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Canara Bank",
            "possession_type": "constructive",
            "reserve_price_num": 13400000, "emd_num": 1340000,
            "village": "Sathyamangala", "taluk": None, "district": None,
            "hobli": "Kasaba", "borrower_primary": "Komala",
        },
        "identifiers": {"khata": "1394", "property_id": "151600602700601459"},
    },
    {
        "aid": "750600", "notice_type": "single",
        "fields": {
            "legal_basis": "DRT", "bank_name": "Indian Bank",
            "reserve_price_num": 20000000, "emd_num": None,
            "village": "Nynarkuppam", "taluk": "Cheiyur", "district": "Kancheepuram",
            "court_reference": "419/2017", "borrower_primary": "Sai Baba",
        },
        "identifiers": {"survey_old": "183/1B"},
    },
    {
        "aid": "758158", "notice_type": "single",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Deutsche Bank",
            "possession_type": "physical",
            "reserve_price_num": 7510000, "emd_num": 751000,
            "village": "Nerkundram", "taluk": "Ambattur", "district": "Thiruvallur",
            "registration_district": "Chennai South",
            "registration_sub_district": "Virugambakkam",
            "borrower_primary": "Hygiene Enviro",
        },
        "identifiers": {"flat": "C-314"},
    },
    {
        "aid": "747290", "notice_type": "single",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Omkara",
            "assignor_bank": "IndusInd", "trust_name": "Omkara PS 06/2021-22",
            "possession_type": "physical", "reserve_price_num": 3300000,
            "village": "Puliyur", "taluk": "Egmore-Nungambakkam", "district": "Chennai",
            "registration_district": "Central Chennai",
            "registration_sub_district": "Kodambakkam",
            "borrower_primary": "Elango",
        },
        "identifiers": {"flat": "E", "sale_deed": "4845/2005"},
    },
    {
        "aid": "749433", "notice_type": "multi",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Canara Bank",
            "reserve_price_num": 1250000, "emd_num": 125000,
            "village": "Periyanguppam", "taluk": "Ambur", "district": "Vellore",
            "borrower_primary": "Jayraj",
        },
        "identifiers": {"survey_old": "178/1A", "survey_new": "178/88"},
    },
    {
        "aid": "755527", "notice_type": "single",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "DCB Bank",
            "possession_type": "symbolic",
            "reserve_price_num": 12000000, "emd_num": 1200000,
            "village": "Anaiyur", "taluk": "Madurai North", "district": "Madurai",
            "registration_district": "Madurai South",
            "borrower_primary": "Jagadeesan",
        },
        "identifiers": {"survey_old": "47/5", "plot": "60"},
    },
    {
        # Canara Bank boilerplate: "the Constructive / Symbolic / Physical
        # Possession of which has been taken" — the notice never commits to one
        # value (no per-lot resolution either), so possession_type must be null,
        # not the raw disjunction and not a guess. See EXPECT_NULL above.
        "aid": "750348", "notice_type": "multi",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Canara Bank",
            "possession_type": EXPECT_NULL,
        },
    },
    {
        # ARC (ARCIL) flat: assignor_bank present via a "Selling Bank" column
        # rather than prose ("... vide Assignment Agreement ..."), and the flat
        # is described across Schedule A/B/C with floor+block in Schedule C —
        # a structure none of the few-shot examples demonstrate. Regression
        # target for the flat/floor/block misplacement + miss found in review.
        "aid": "752245", "notice_type": "single",
        "fields": {
            # The notice names the seller by its full legal name ("Asset
            # Reconstruction Company (India) Limited"); "Asset Reconstruction"
            # substring-matches both that and any ARCIL short form.
            "legal_basis": "SARFAESI", "bank_name": "Asset Reconstruction",
            "assignor_bank": "Bajaj Housing Finance",
            "trust_name": "Arcil-Retail Loan Portfolio-042",
            "possession_type": "physical",
            "reserve_price_num": 2889000,
            # district deliberately unscored: the notice states only the
            # REGISTRATION District of Coimbatore (OCR: "Colmbatore") — the
            # revenue district is never named, so per the verbatim/no-invention
            # rule the correct extraction is district=None.
            "village": "Kalapatty", "taluk": "Coimbatore North",
            "district": None, "borrower_primary": "Dhayanandh",
        },
        "identifiers": {"floor": "First", "block": "A"},
    },
    {
        # Indian Bank, one combined notice bundling 5 unrelated
        # borrowers/properties (not lots of one property) — Document.markdown is
        # shared, so this is what production actually feeds to extract() for
        # each of the 5 AuctionProperty nodes backed by it. First block only.
        "aid": "753006", "notice_type": "multi",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Indian Bank",
            "possession_type": "symbolic",
            "reserve_price_num": 7000000,
            "village": "Kolathuvanchery", "taluk": "Kundrathur",
            "district": "Kancheepuram", "borrower_primary": "Sakthivel",
        },
        "identifiers": {"flat": "T-2", "floor": "Third"},
    },
]
