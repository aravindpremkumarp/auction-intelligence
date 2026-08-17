"""pipeline.entity_resolution: which name strings mean the same institution.

Every case here is a real string from the corpus — the merges that must happen,
and the near-misses that must not.
"""
from __future__ import annotations

from collections import Counter

from pipeline.entity_resolution import (
    canonical_label, normalize, org_key, propose_merges, resolve,
)


def test_case_and_legal_form_do_not_change_identity():
    same = [
        ("Canara Bank", "CANARA BANK"),
        ("State Bank of India", "State Bank Of India"),
        ("Repco Home Finance Limited", "Repco Home Finance Ltd."),
        ("City Union Bank Limited", "City Union Bank"),
        ("Equitas Small Finance Bank", "Equitas small finance Bank Ltd"),
        ("The Karur Vysya Bank Ltd", "Karur Vysya Bank Limited"),
    ]
    for a, b in same:
        assert org_key(a) == org_key(b), f"{a!r} vs {b!r}"


def test_different_banks_stay_apart():
    """The containment trap. Each left name is a substring of its right name,
    and all four are separate institutions — which is why the key compares
    token SETS instead of asking whether one name contains the other."""
    different = [
        ("Bank of India", "State Bank of India"),
        ("Indian Bank", "The South Indian Bank Ltd"),
        ("Union Bank of India", "City Union Bank Ltd"),
        ("Asset Reconstruction Company (India) Limited",
         "India SME Asset Reconstruction Company Limited"),
    ]
    for a, b in different:
        assert org_key(a) != org_key(b), f"{a!r} merged with {b!r}"


def test_html_entity_from_a_table_cell():
    # Extraction pulls names out of table HTML; "&amp;" reaching the key as the
    # token "amp" would split one lender into two.
    assert org_key("Cholamandalam Investment &amp; Finance Company Limited") == \
           org_key("Cholamandalam Investment and Finance Company Limited")


def test_normalize_is_conservative():
    assert normalize("  Canara   Bank  ") == "canara bank"
    assert normalize("M/s. ICICI Bank Ltd.") == "m s icici bank ltd"
    assert normalize("") == ""


def test_canonical_label_prefers_the_common_mixed_case_spelling():
    variants = {
        "CHOLAMANDALAM INVESTMENT AND FINANCE COMPANY LIMITED": 29,
        "Cholamandalam Investment and Finance Company Limited": 33,
        "Cholamandalam investment and Finance Company Limited": 14,
    }
    assert canonical_label(variants) == \
        "Cholamandalam Investment and Finance Company Limited"
    # On a tie, the shouted banner spelling loses to the typeset one.
    assert canonical_label({"CANARA BANK": 5, "Canara Bank": 5}) == "Canara Bank"


def test_resolve_groups_and_maps_every_variant():
    values = Counter({
        "Canara Bank": 240, "CANARA BANK": 1,
        "Bank of India": 14, "State Bank of India": 56,
    })
    res = resolve(values)
    assert len(res["groups"]) == 3          # the two Canaras merge, the rest do not
    top = res["groups"][0]
    assert top["canonical"] == "Canara Bank" and top["count"] == 241
    assert top["merged"] == 1
    assert res["by_value"]["CANARA BANK"] == "Canara Bank"
    assert res["by_value"]["Bank of India"] == "Bank of India"


def test_propose_merges_finds_ocr_damage_and_stays_advisory():
    # "Pirama" is "Piramal" with a letter dropped by OCR: a different token set,
    # so only similarity can surface it — as a proposal, never a merge.
    res = resolve(Counter({"Piramal Finance Ltd": 9, "Pirama Finance Ltd": 1,
                           "Canara Bank": 240}))
    pairs = propose_merges(res["groups"])
    assert any({p["a"], p["b"]} == {"Piramal Finance Ltd", "Pirama Finance Ltd"}
               for p in pairs)
    # Proposing is not merging: the groups are still separate.
    assert len(res["groups"]) == 3
    # An unrelated name is not dragged in.
    assert not any("Canara Bank" in (p["a"], p["b"]) for p in pairs)


def test_resolve_rejects_unsupported_kinds():
    # Place names need hierarchy rules (same village only if taluk and district
    # agree), so they must not silently fall through to the org rule.
    try:
        resolve(Counter({"Chennai": 1}), kind="place")
    except ValueError as e:
        assert "place" in str(e)
    else:
        raise AssertionError("expected ValueError for kind='place'")
