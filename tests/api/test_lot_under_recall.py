"""Guards for the lot_under_recall heuristic (pipeline/validators).

The check compares serial-number markers in the source text against the number
of lots actually extracted, to catch a multi-lot notice whose later lots were
missed. Its whole value depends on only counting REAL lot numbering: Indian
auction notices cite survey numbers in the same "S.No." shape as a lot header,
and every single-lot notice carries several, so a loose pattern flagged notices
with nothing missing. Pure test — no langextract / API key.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import _LOT_MARKER, validate


def E(cls, lot="1", text="", **attrs):
    a = dict(attrs)
    a["lot_index"] = lot
    return SimpleNamespace(extraction_class=cls, extraction_text=text,
                           attributes=a, char_interval=None)


def _markers(text):
    """Distinct lot numbers, mirroring how validate() counts them."""
    return {int(m) for m in _LOT_MARKER.findall(text)}


# ── what must NOT read as a lot number ───────────────────────────────────────
def test_survey_number_with_subdivision_is_not_a_lot():
    assert _markers("Patta No.44 Village Natham S. No.48/30, 48/31") == set()


def test_old_survey_number_with_letter_suffix_is_not_a_lot():
    assert _markers("Natham Old.S No.48/3A1AM,48/3A1AL of land") == set()


def test_prefixed_survey_families_are_not_lots():
    # R.S. (re-survey) and T.S. (town survey) carry no subdivision, so only the
    # prefix letter distinguishes them from a lot header.
    assert _markers("On the South by: R.S. No. 102, On the East by: Plot No. 9") == set()
    assert _markers("comprised in T.S.No 45 of the said village") == set()


def test_bare_table_header_is_not_a_lot():
    assert _markers("<tr><th>S. No.</th><th>Borrower(s)</th></tr>") == set()


# ── what MUST read as a lot number ───────────────────────────────────────────
def test_real_serial_listing_is_counted():
    assert _markers("S. No. 1 Ramesh; S. No. 2 Suresh; S. No. 3 Priya") == {1, 2, 3}


def test_item_numbering_is_counted():
    assert _markers("Item No. 1 and Item No. 2") == {1, 2}


def test_back_references_are_deduplicated():
    # the contact block restates each lot's number; counting the repeats made a
    # correctly-extracted 2-lot notice look under-recalled.
    text = ("S.No.1; Borrower: Mr A ... S.No.2: Borrower: Mr K ... "
            "For S.No.1: Rajesh - Mob 98851 ... For S.No.2: Nisha - Mob 98852")
    assert _markers(text) == {1, 2}


# ── end-to-end through validate() ────────────────────────────────────────────
def _codes(ex, src):
    return {i["code"] for i in validate(ex, source_text=src)["issues"]}


def test_single_lot_notice_citing_survey_numbers_is_not_flagged():
    src = ("All that land in Thokkavadi Village, Patta No.44 Village Natham "
           "S. No.48/30, 48/31 measuring 0-01.5 Ares, Natham Old.S "
           "No.48/3A1AM,48/3A1AL, bounded on the South by R.S. No. 102.")
    assert "lot_under_recall" not in _codes([E("property")], src)


def test_genuine_under_recall_still_flags():
    # three numbered lots in the source, only one extracted.
    src = "S. No. 1 Ramesh; S. No. 2 Suresh; S. No. 3 Priya"
    assert "lot_under_recall" in _codes([E("property")], src)


def test_fully_extracted_multi_lot_notice_is_not_flagged():
    src = "S. No. 1 Ramesh; S. No. 2 Suresh; S. No. 3 Priya"
    ex = [E("property", lot="1"), E("property", lot="2"), E("property", lot="3")]
    assert "lot_under_recall" not in _codes(ex, src)
