"""Ground-truth labels for the LangExtract extraction eval.

Dependency-free (stdlib only) so it can be imported by the scorer and by tests.
Each entry: auction_id, notice_type, a flat dict of expected scalar `fields`
(None = not present / not scored), and expected `identifiers` (kind -> value).
Fixtures with the matching markdown live in evals/fixtures/<aid>.txt.

MULTI notices additionally carry a `lots` list — the PER-LOT truth, one dict per
auction lot present IN THE MARKDOWN (never the AuctionProperty count, which is a
scope-filtered subset). Each lot dict holds `reserve_price_num` (the anchor the
scorer matches on), optional `emd_num`/location keys, and an `identifiers` map.
The notice-level `fields` on a multi entry stay notice-wide only (legal_basis,
bank_name, possession_type); everything per-lot moves into `lots`. See
evals/langextract_eval.score_multi for how lots are matched and scored.

The single notices are deliberately NOT the few-shot examples (no train/test
leakage) and span the hard cases: Karnataka khata/Hobli, DRT 'Upset Price',
flat+UDS, ARC assignor/trust, and tiled-house notices. The three multi notices
(749433, 750348, 753006) each teach a distinct multi-lot failure mode: a multi
label whose markdown holds only one lot; 6 lots under 5 OCR-mangled serials with
one borrower owning two flats; and 5 lots whose serials reset per branch with two
sharing a reserve price.

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
        # MULTI notice whose markdown holds only ONE property block: the original
        # notice auctioned several (it opens "S.No.1 ... (For S.No.1)"), but MinerU
        # captured only S.No.1. Correct extraction therefore has exactly ONE lot —
        # the extractor can only find what the text holds. This is the canonical
        # "multi label, single lot in markdown" case (the property_count vs. text
        # gap), and it guards against hallucinating the absent lots.
        "aid": "749433", "notice_type": "multi",
        "fields": {"legal_basis": "SARFAESI", "bank_name": "Canara Bank"},
        "lots": [
            {"reserve_price_num": 1250000, "emd_num": 125000,
             "village": "Periyanguppam", "taluk": "Ambur", "district": "Vellore",
             "identifiers": {"survey_old": "178/1A", "survey_new": "178/88"}},
        ],
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
        # MULTI notice: 6 auction lots under 5 borrower serials — SI.No.1 alone
        # carries TWO flats (G-2 and S-3), each with its own reserve price — and
        # the serial markers are OCR-mangled ("Sri No." for Sl.No.4, "35.15,000"
        # for a reserve). So lot != borrower-serial; the reliable per-lot anchor is
        # the reserve price. Canara Bank boilerplate lists "Constructive / Symbolic
        # / Physical Possession" disjunctively and never commits, so possession_type
        # must be null (see EXPECT_NULL above), not the raw disjunction or a guess.
        "aid": "750348", "notice_type": "multi",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Canara Bank",
            "possession_type": EXPECT_NULL,
        },
        "lots": [
            {"reserve_price_num": 3515000, "emd_num": 351500,
             "village": "Varadharajapuram",
             "identifiers": {"flat": "G-2", "cersai": "400082605186"}},
            {"reserve_price_num": 2375000, "emd_num": 237500,
             "village": "Varadharajapuram",
             "identifiers": {"flat": "S-3", "cersai": "400082604611"}},
            {"reserve_price_num": 2817600, "emd_num": 281760,
             "village": "Ninnaikarai",
             "identifiers": {"survey_old": "255/2", "patta": "23262"}},
            {"reserve_price_num": 2635000, "emd_num": 263500,
             "village": "Ayappakkam",
             "identifiers": {"survey_new": "260/1C2", "patta": "5577"}},
            {"reserve_price_num": 5129600, "emd_num": 512960,
             "village": "Saidapet",
             "identifiers": {"block": "6", "survey_old": "105"}},
            {"reserve_price_num": 13440000, "emd_num": 1344000,
             "village": "Narasingapuram",
             "identifiers": {"survey_old": "245"}},
        ],
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
        # MULTI notice: 5 properties across 3 Indian Bank branches, and the
        # borrower serial RESETS per branch (1,2 / 1 / 1,2) — so the serial is not
        # a global lot number. Two lots share reserve Rs.31L (both Fortune Enclave
        # flats, G1 vs F1), so reserve alone can't key them: the unique per-lot key
        # is the PROPERTY ID (IDIB...). Money is stated in Lakhs ("Rs.70.00 Lakhs"
        # -> 7000000). Possession is Symbolic on every block. Document.markdown is
        # shared, so this is exactly what production feeds extract() for each of
        # the 5 AuctionProperty nodes backed by it.
        "aid": "753006", "notice_type": "multi",
        "fields": {
            "legal_basis": "SARFAESI", "bank_name": "Indian Bank",
            "possession_type": "symbolic",
        },
        "lots": [
            {"reserve_price_num": 7000000, "emd_num": 700000,
             "village": "Kolathuvanchery", "taluk": "Kundrathur",
             "district": "Kancheepuram",
             "identifiers": {"property_id": "IDIB7725977335", "flat": "T-2"}},
            {"reserve_price_num": 3800000, "emd_num": 380000,
             "village": "Melakuppam",
             "identifiers": {"property_id": "IDIB872950823"}},
            {"reserve_price_num": 3500000, "emd_num": 350000,
             "village": "Surapet",
             "identifiers": {"property_id": "IDIB7269020442", "flat": "F-1"}},
            {"reserve_price_num": 3100000, "emd_num": 310000,
             "village": "Paruthipattu",
             "identifiers": {"property_id": "IDIB6004239372", "flat": "G1"}},
            {"reserve_price_num": 3100000, "emd_num": 310000,
             "village": "Paruthipattu",
             "identifiers": {"property_id": "IDIB6014505979", "flat": "F1"}},
        ],
    },
]
