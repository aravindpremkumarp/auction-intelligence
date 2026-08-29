"""pipeline.entity_resolution: which name strings mean the same institution.

Every case here is a real string from the corpus — the merges that must happen,
and the near-misses that must not.
"""
from __future__ import annotations

from collections import Counter

from pipeline.entity_resolution import (
    canonical_label, normalize, ocr_variant_of, org_key, propose_merges,
    resolve,
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


def test_resolve_absorbs_one_misread_token():
    """"Pirama" is "Piramal" with a letter dropped by OCR. The token set differs,
    so the equality rule cannot see it — the second auto-merge tier can."""
    res = resolve(Counter({"Piramal Finance Ltd": 9, "Pirama Finance Ltd": 1,
                           "Canara Bank": 240}))
    assert len(res["groups"]) == 2
    assert res["by_value"]["Pirama Finance Ltd"] == "Piramal Finance Ltd"
    # The unrelated name keeps its own group.
    assert res["by_value"]["Canara Bank"] == "Canara Bank"


def test_every_ocr_pair_in_the_corpus_merges():
    """The four the review queue used to carry, plus the two OCR pairs found
    while labelling the corpus. All are one damaged token."""
    pairs = [
        ("ICICI Bank Limited", "IICI Bank Limited"),
        ("The Karur Vysya Bank Ltd", "The Kanur Vysya Bank Ltd"),
        ("The Karur Vysya Bank Ltd", "The Karur Vyssa Bank Ltd."),
        ("Manappuram Home Finance Ltd", "Manapouram Home Finance Ltd"),
        ("IFL Home Finance Limited", "IFIL Home Finance Limited"),
        ("Hinduja Housing Finance Limited", "Hindu Housing Finance Limited"),
        ("Vistaar Financial Services Private Limited",
         "Vista Financial Services Private Limited"),
        ("Cholamandalam Investment and Finance Company Limited",
         "CHOLAMANDAM INVESTMENT AND FINANCE COMPANY LIMITED"),
    ]
    for a, b in pairs:
        assert ocr_variant_of(a, b), f"{a!r} should absorb {b!r}"


def test_a_whole_extra_or_swapped_word_is_a_different_lender():
    """Where the second tier must stop. Each pair scores high on plain string
    similarity, and every one is two separate institutions."""
    different = [
        # The 92.9-similarity trap the review queue exists for.
        ("Asset Reconstruction Company (India) Limited",
         "India SME Asset Reconstruction Company Limited"),
        # A lending arm and its housing-finance sibling, four times over.
        ("Bajaj Finance Limited", "Bajaj Housing Finance Ltd"),
        ("Aditya Birla Capital Limited", "Aditya Birla Housing Finance Limited"),
        ("Hero Fincorp Limited", "Hero Housing Finance Limited"),
        ("Tata Capital Limited", "Tata Capital Housing Finance Ltd"),
        ("ICICI Bank Limited", "ICICI Home Finance Company Ltd."),
        ("SMFG India Credit Co. Ltd.", "SMFG India Home Finance Co. Ltd."),
        ("Punjab National Bank", "PNB Housing Finance Limited"),
        ("Axis Bank Ltd", "Axis Finance Limited"),
        ("Shriram Finance Limited", "Shriram Asset Reconstruction Private Limited"),
        ("Hinduja Housing Finance Limited", "Hinduja Leyland Finance Limited"),
        # One swapped word, and they are different public sector banks.
        ("Bank of India", "Bank of Baroda"),
        ("Punjab National Bank", "Punjab & Sind Bank"),
    ]
    for a, b in different:
        assert not ocr_variant_of(a, b), f"{a!r} wrongly merged with {b!r}"


def test_short_acronyms_need_a_relative_edit_budget():
    """Two edits is noise inside "Cholamandalam" and a different bank inside
    "CSB". Without the relative budget these merge into one lender."""
    assert not ocr_variant_of("CSB Bank Limited", "DCB Bank Ltd")
    assert not ocr_variant_of("UCO Bank", "DCB Bank Ltd")
    assert not ocr_variant_of("ICICI Bank Limited", "IDBI Bank Ltd")
    # ...and the genuine one-letter damage on the same-length token still lands.
    assert ocr_variant_of("ICICI Bank Limited", "IICI Bank Limited")


def test_propose_merges_stays_advisory_for_what_the_rules_cannot_decide():
    # Similarity alone cannot tell these two ARCs apart, so they must reach a
    # human as a proposal and never as a merge.
    res = resolve(Counter({
        "Asset Reconstruction Company (India) Limited": 28,
        "India SME Asset Reconstruction Company Limited": 2}))
    assert len(res["groups"]) == 2
    pairs = propose_merges(res["groups"])
    assert any({p["a"], p["b"]} == {
        "Asset Reconstruction Company (India) Limited",
        "India SME Asset Reconstruction Company Limited"} for p in pairs)


def test_branch_names_never_get_the_ocr_tier():
    """"ARM I" and "ARM II" are one edit apart and are two Chennai offices, so
    the second tier is org-only. kind="branch" must merge on equality alone."""
    res = resolve(Counter({"ARMB I, Chennai": 3, "ARMB II, Chennai": 2}),
                  kind="branch")
    assert len(res["groups"]) == 2


def test_resolve_rejects_unsupported_kinds():
    # Place names need hierarchy rules (same village only if taluk and district
    # agree), so they must not silently fall through to the org rule.
    try:
        resolve(Counter({"Chennai": 1}), kind="place")
    except ValueError as e:
        assert "place" in str(e)
    else:
        raise AssertionError("expected ValueError for kind='place'")


def test_branch_key_ignores_word_order_and_the_word_branch():
    """Canara Bank's Trichy recovery office, six ways in the corpus."""
    variants = ["ARM Branch Trichy", "ARM Trichy", "ARM Branch, Trichy",
                "Trichy ARM Branch", "ARM TRICHY", "ARM TRICHY BRANCH"]
    from pipeline.entity_resolution import branch_key
    keys = {branch_key(v) for v in variants}
    assert len(keys) == 1
    # "Anna Salai Branch" vs "Branch Anna Salai" — the scraped graph held
    # these as two different offices of CanFin Homes.
    assert branch_key("Anna Salai Branch") == branch_key("Branch Anna Salai")


def test_branch_key_keeps_numbered_offices_apart():
    # ARMB I and Specialized ARM II are different Chennai offices.
    from pipeline.entity_resolution import branch_key
    assert branch_key("ARMB I, Chennai") != branch_key("ARMB II, Chennai")


def test_resolve_kind_branch_uses_the_branch_rule():
    values = Counter({"ARM Branch Trichy": 4, "Trichy ARM Branch": 2,
                      "Thousand Lights Branch": 3, "Thousand Lights": 1})
    res = resolve(values, kind="branch")
    assert len(res["groups"]) == 2
    assert res["by_value"]["Trichy ARM Branch"] == "ARM Branch Trichy"
