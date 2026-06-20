"""Ground-truth labels for the LangExtract extraction eval.

Dependency-free (stdlib only) so it can be imported by the scorer and by tests.
Each entry: auction_id, notice_type, a flat dict of expected scalar `fields`
(None = not present / not scored), and expected `identifiers` (kind -> value).
Fixtures with the matching markdown live in evals/fixtures/<aid>.txt.

These 6 notices are deliberately NOT the two few-shot examples (no train/test
leakage) and span the hard cases: Karnataka khata/Hobli, DRT 'Upset Price',
flat+UDS, ARC assignor/trust, and tiled-house notices.
"""
from __future__ import annotations

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
]
