"""scripts/link_register_copies.find_copies: which LGD-added register rows are
the same village as an original, on the original's Tamil name."""
from __future__ import annotations

from scripts.link_register_copies import find_copies


def _orig(name, ta, taluk="Vandavasi"):
    return {"name": name, "taluk": taluk, "source": None, "name_ta": ta}


def _lgd(name, taluk="Vandavasi"):
    return {"name": name, "taluk": taluk, "source": "LGD 2026-09-21", "name_ta": None}


def _pairs(rows):
    return {(c["copy"], c["original"]) for c in find_copies(rows)}


def test_a_respelling_the_tamil_name_confirms_is_a_copy():
    rows = [_orig("Arasoor", "ஆராசூர்"), _orig("Kilkodungalur", "கீழ்க்கொடுங்காலூர்"),
            _lgd("Arasur"),
            # the English spellings disagree, the Tamil name decides
            _orig("Cheyur", "சேவூர்", "Avinashi"), _lgd("Sevur", "Avinashi")]
    out = {c["copy"]: c for c in find_copies(rows)}
    assert out["Arasur"]["original"] == "Arasoor" and out["Arasur"]["rule"] == "tamil+english"
    assert out["Sevur"]["original"] == "Cheyur" and out["Sevur"]["rule"] == "tamil"


def test_a_different_village_a_forest_or_a_census_town_is_never_a_copy():
    rows = [_orig("Sithathur", "சித்தாத்தூர்", "Gudiyatham"), _lgd("Chendathur", "Gudiyatham"),
            _orig("Thachampattu", "தச்சம்பட்டு", "Tiruvannamalai"),
            _lgd("Thachchampattu R.F.", "Tiruvannamalai"),
            _orig("Periyagaram", "பெரியகரம்", "Tirupathur"), _lgd("Periagaram (Ct)", "Tirupathur")]
    assert _pairs(rows) == set()


def test_parts_numbers_and_initials_must_match():
    rows = [_orig("Thiruninravur", "திருநின்றவூர்", "Avadi"), _lgd("Thiruninravur A", "Avadi"),
            _orig("Elavur", "எளாவூர்", "Gummidipoondi"), _lgd("Elavur II", "Gummidipoondi"),
            _orig("Kenippattu", "கெனிப்பட்டு", "Vanur"), _lgd("V.Kenippattu", "Vanur")]
    assert _pairs(rows) == set()


def test_two_close_originals_or_a_shared_original_name_is_refused():
    close = [_orig("Bahdoor", "பாதூர்"), _orig("Padur", "படூர்"), _lgd("Badur")]
    assert _pairs(close) == set()
    shared = [_orig("Veppampattu", "(087) வேப்பம்பட்டு", "Tiruvallur"),
              _orig("Veppampattu", "(088) வேப்பம்பட்டு", "Tiruvallur"),
              _lgd("Veppambaattu", "Tiruvallur")]
    assert _pairs(shared) == set()


def test_only_the_same_taluk_counts():
    assert _pairs([_orig("Arasoor", "ஆராசூர்", "Vandavasi"), _lgd("Arasur", "Chetpet")]) == set()
