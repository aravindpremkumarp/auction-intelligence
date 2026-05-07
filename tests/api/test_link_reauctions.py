"""Tests for scripts/link_reauctions — the re-auction matcher. These cover
the pure Python pieces (total-area parser, area normaliser, description
tokenisation + Jaccard, pair finder, union-find expansion) without hitting
Neo4j."""
from __future__ import annotations

import pytest


# Realistic descriptions — re-auction rounds share the distinctive tokens
# (address, measurements, borrower, property type) and differ only in
# round-specific details like dates and reserve prices.
DESC_A1 = (
    "All that piece of flat bearing door number 42/1 situated at "
    "Varadharajapuram Adyar Chennai admeasuring 1295 sq ft with boundaries "
    "north by road east by plot 43 south by drain west by plot 41"
)
DESC_A2 = (
    "All the piece of flat door no 42/1 situated at Varadharajapuram Adyar "
    "Chennai admeasuring 1295 square feet bounded north by road east plot 43 "
    "south by drain west plot 41 property owned by Alice Co"
)
DESC_UNRELATED = (
    "Industrial shed door number 17 located at Ambattur Chennai "
    "admeasuring 4000 sq ft used for manufacturing garments"
)


# ── Area parsing / normalisation ─────────────────────────────────────────────

def test_parse_total_area_sqft_basic_units() -> None:
    from scripts.link_reauctions import parse_total_area_sqft

    assert parse_total_area_sqft("1295 Sq Ft") == pytest.approx(1295.0)
    assert parse_total_area_sqft("1,295 sqft") == pytest.approx(1295.0)
    assert parse_total_area_sqft("120 sq m") == pytest.approx(1291.67, rel=0.01)
    assert parse_total_area_sqft("0.5 acre") == pytest.approx(21780.0)
    assert parse_total_area_sqft("3 cents") == pytest.approx(1306.8)
    # Bare large number falls back to sq ft.
    assert parse_total_area_sqft("1295") == pytest.approx(1295.0)


def test_parse_total_area_rejects_unparseable_small_bare_numbers() -> None:
    from scripts.link_reauctions import parse_total_area_sqft

    assert parse_total_area_sqft("5") is None
    assert parse_total_area_sqft("") is None
    assert parse_total_area_sqft(None) is None
    assert parse_total_area_sqft("no numbers here") is None


def test_areas_agree_within_tolerance() -> None:
    from scripts.link_reauctions import areas_agree

    assert areas_agree(1000.0, 1050.0) is True
    assert areas_agree(1000.0, 1099.0) is True
    assert areas_agree(1000.0, 1200.0) is False
    assert areas_agree(None, 1000.0) is False
    assert areas_agree(1000.0, 0.0) is False


def test_normalize_area_strips_trailing_dots_and_taluk_suffix() -> None:
    from scripts.link_reauctions import normalize_area

    assert normalize_area("Vedasandur.") == "vedasandur"
    assert normalize_area("Vedasandur") == "vedasandur"
    assert normalize_area("Poonamallee Taluk") == "poonamallee"
    assert normalize_area("Sriperumbudur Taluk.") == "sriperumbudur"
    assert normalize_area("  Chennai  ") == "chennai"
    assert normalize_area(None) is None
    assert normalize_area("") is None


# ── Description tokenisation + Jaccard ───────────────────────────────────────

def test_tokenize_description_drops_stopwords_and_short_tokens() -> None:
    from scripts.link_reauctions import tokenize_description

    tokens = tokenize_description("The property at door no 42 in Adyar.")
    # "the", "at", "no", "in" are filtered out (stopwords or <3 chars).
    # "property" is also a sale-notice stopword.
    assert "adyar" in tokens
    assert "door" in tokens
    assert "42" in tokens
    assert "the" not in tokens
    assert "at" not in tokens
    assert "property" not in tokens


def test_tokenize_description_returns_none_when_empty() -> None:
    from scripts.link_reauctions import tokenize_description

    assert tokenize_description(None) is None
    assert tokenize_description("") is None
    assert tokenize_description("the and for") is None  # all stopwords


def test_jaccard_similar_descriptions_score_high() -> None:
    from scripts.link_reauctions import tokenize_description, jaccard

    a = tokenize_description(DESC_A1)
    b = tokenize_description(DESC_A2)
    sim = jaccard(a, b)
    assert sim >= 0.5, f"expected high similarity, got {sim:.3f}"


def test_jaccard_unrelated_descriptions_score_low() -> None:
    from scripts.link_reauctions import tokenize_description, jaccard

    a = tokenize_description(DESC_A1)
    c = tokenize_description(DESC_UNRELATED)
    sim = jaccard(a, c)
    assert sim < 0.3, f"expected low similarity, got {sim:.3f}"


# ── Rule: borrower + location + description ─────────────────────────────────

def test_borrower_location_desc_rule_fires_on_similar_description() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A1,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A2,
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1
    a, b, reason, conf = pairs[0]
    assert {a, b} == {"A", "B"}
    assert reason == "borrower_location_desc"
    assert conf in ("medium", "high")


def test_borrower_location_desc_rule_rejects_dissimilar_descriptions() -> None:
    """Same borrower + bank + area but distinct parcels (unrelated
    descriptions) — must NOT merge."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A1,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_UNRELATED,
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_borrower_location_desc_rule_skips_when_description_missing() -> None:
    """Missing description on either side → don't match. Conservative."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": None,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A2,
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_borrower_location_desc_rule_rejects_disagreeing_total_area() -> None:
    """If total_area is parseable on both sides, a big mismatch kills
    the link even when description overlap is high."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "description": DESC_A1,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1400 sqft",
            "description": DESC_A2,
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_same_day_auctions_are_treated_as_batch_sale_not_reauction() -> None:
    """Two auctions on the same calendar day with identical borrower,
    bank, area, and description are a batch sale (sibling parcels), not
    a re-auction."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A1,
            "auction_start_dt": "2026-05-07T10:00:00",
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A2,
            "auction_start_dt": "2026-05-07T14:30:00",  # same day, later slot
        },
    ]
    assert find_reauction_pairs(auctions) == []


def test_different_day_auctions_can_still_match() -> None:
    """Sanity: the date gate must let through genuine re-auction pairs
    that happen on different days."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A1,
            "auction_start_dt": "2026-01-15T10:00:00",
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A2,
            "auction_start_dt": "2026-05-07T10:00:00",
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1


def test_missing_date_does_not_block_match() -> None:
    """If either date is missing we can't PROVE same-day; fall back to
    the description-similarity verdict so we don't lose real re-auctions
    to date gaps."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A1,
            "auction_start_dt": None,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": DESC_A2,
            "auction_start_dt": "2026-05-07T10:00:00",
        },
    ]
    assert len(find_reauction_pairs(auctions)) == 1


def test_is_same_auction_day_helper() -> None:
    from scripts.link_reauctions import _is_same_auction_day

    assert _is_same_auction_day("2026-05-07T10:00:00", "2026-05-07T14:30:00") is True
    assert _is_same_auction_day("2026-05-07T10:00:00", "2026-05-08T10:00:00") is False
    assert _is_same_auction_day(None, "2026-05-07T10:00:00") is False
    assert _is_same_auction_day("2026-05-07T10:00:00", None) is False
    assert _is_same_auction_day("", "2026-05-07T10:00:00") is False


def test_borrower_location_desc_respects_area_normalisation() -> None:
    """'Vedasandur.' and 'Vedasandur' should end up in the same candidate
    group after area normalisation."""
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Vedasandur.", "total_area": None,
            "description": DESC_A1,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Vedasandur", "total_area": None,
            "description": DESC_A2,
        },
    ]
    pairs = find_reauction_pairs(auctions)
    assert len(pairs) == 1


def test_sim_threshold_is_configurable() -> None:
    from scripts.link_reauctions import find_reauction_pairs

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": "flat at door no 42 adyar",
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": None,
            "description": "flat door no 42 adyar chennai",
        },
    ]
    # Loose threshold → matches.
    assert find_reauction_pairs(auctions, sim_threshold=0.3)
    # Extremely strict threshold → rejects.
    assert find_reauction_pairs(auctions, sim_threshold=0.99) == []


# ── Transitive clustering ────────────────────────────────────────────────────

def test_expand_clusters_is_transitive() -> None:
    """Three auctions of the same parcel — pairwise rule-2 matches should
    produce one cluster with all three pairs."""
    from scripts.link_reauctions import find_reauction_pairs, expand_clusters

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "description": DESC_A1,
        },
        {
            "auction_id": "B", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "description": DESC_A1,
        },
        {
            "auction_id": "C", "borrower": "Alice Co", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1020 sqft",
            "description": DESC_A2,
        },
    ]
    pairs = find_reauction_pairs(auctions)
    clusters, expanded = expand_clusters(auctions, pairs)

    assert len(clusters) == 1
    assert set(clusters[0]) == {"A", "B", "C"}
    assert len(expanded) == 3
    expanded_pairs = {(a, b) for a, b, _, _ in expanded}
    assert expanded_pairs == {("A", "B"), ("A", "C"), ("B", "C")}


def test_group_boilerplate_tokens_demotes_shared_scaffolding() -> None:
    """For a 4+ member group, tokens appearing in >=75% of members are
    boilerplate. Rare tokens survive."""
    from scripts.link_reauctions import tokenize_description, group_boilerplate_tokens

    # 5 descriptions, all with "coimbatore registration district taluk"
    # (boilerplate). Each has a unique village identifier.
    common = "coimbatore registration district vadavalli taluk commercial building"
    sets = [
        tokenize_description(f"{common} kaliannanpudur village residential"),
        tokenize_description(f"{common} kaliannanpudur village residential"),
        tokenize_description(f"{common} vadavalli village land"),
        tokenize_description(f"{common} thirumurgan nagar commercial"),
        tokenize_description(f"{common} vedapatti village agricultural"),
    ]
    bp = group_boilerplate_tokens(sets)
    # Scaffolding present in all 5 (and therefore >=75%) gets marked.
    assert "coimbatore" in bp
    assert "registration" in bp
    assert "district" in bp
    assert "vadavalli" in bp  # even the sub-registration district is scaffolding here
    # Village-specific tokens (in only 1-2 of 5) survive.
    assert "kaliannanpudur" not in bp
    assert "thirumurgan" not in bp
    assert "vedapatti" not in bp


def test_group_boilerplate_not_applied_to_small_groups() -> None:
    """In a 2 or 3-auction group we can't tell boilerplate from signal,
    so the subtraction set must be empty."""
    from scripts.link_reauctions import tokenize_description, group_boilerplate_tokens

    sets2 = [
        tokenize_description("coimbatore registration vadavalli door 42"),
        tokenize_description("coimbatore registration vadavalli door 42"),
    ]
    assert group_boilerplate_tokens(sets2) == set()

    sets3 = [
        tokenize_description("coimbatore registration vadavalli door 42"),
        tokenize_description("coimbatore registration vadavalli door 43"),
        tokenize_description("coimbatore registration vadavalli door 44"),
    ]
    assert group_boilerplate_tokens(sets3) == set()


def test_batch_sale_parcels_do_not_cross_link_via_boilerplate() -> None:
    """The Sri Venkateshwar regression: 4 distinct parcels by one borrower,
    each auctioned on two different days. Boilerplate is heavy (shared
    registration district language). Without boilerplate demotion the
    matcher merges Lot 1 with Lot 5 because their descriptions share the
    scaffolding. After the fix, only same-lot re-auctions link."""
    from scripts.link_reauctions import find_reauction_pairs, expand_clusters

    scaffold = (
        "Coimbatore Registration District Vadavalli Sub-Registration "
        "District Coimbatore Taluk property"
    )
    lots = [
        # Lot 1 — Kaliannanpudur, Survey No 123/4A
        ("L1_jan", "2026-01-30T15:00:00",
         f"{scaffold} Kaliannanpudur residential land Survey No 123/4A"),
        ("L1_mar", "2026-03-17T15:00:00",
         f"Property Lot 1 {scaffold} Kaliannanpudur residential land Survey No 123/4A"),
        # Lot 5 — Vadavalli Village, Survey No 567/8B
        ("L5_jan", "2026-01-30T15:00:00",
         f"{scaffold} Vadavalli Village residential land Survey No 567/8B"),
        ("L5_mar", "2026-03-17T15:00:00",
         f"Property Lot 5 {scaffold} Vadavalli Village residential land Survey No 567/8B"),
    ]
    auctions = [
        {
            "auction_id": aid, "borrower": "Ventures Co", "bank": "ARC",
            "city": "Coimbatore", "area": "Vadavalli", "total_area": None,
            "description": desc, "auction_start_dt": date,
        }
        for aid, date, desc in lots
    ]
    pairs = find_reauction_pairs(auctions)
    clusters, expanded = expand_clusters(auctions, pairs)

    # Expect two separate clusters, one per Lot, each of size 2.
    cluster_sets = [frozenset(c) for c in clusters]
    assert frozenset({"L1_jan", "L1_mar"}) in cluster_sets
    assert frozenset({"L5_jan", "L5_mar"}) in cluster_sets
    # Critically: Lot 1 auctions must NOT cluster with Lot 5 auctions.
    linked_pairs = {(a, b) for a, b, _, _ in expanded}
    assert ("L1_jan", "L5_mar") not in linked_pairs
    assert ("L1_mar", "L5_jan") not in linked_pairs
    assert ("L1_jan", "L5_jan") not in linked_pairs
    assert ("L1_mar", "L5_mar") not in linked_pairs


def test_expand_clusters_drops_same_day_pairs_emitted_transitively() -> None:
    """Union-find merges A-B-C when A↔B (diff day) and B↔C (diff day)
    both match. But if A and C happen to fall on the same calendar day
    they're batch siblings, not re-auctions, and must be filtered from
    the emitted pair list even though they share a cluster."""
    from scripts.link_reauctions import expand_clusters

    auctions = [
        {"auction_id": "A", "auction_start_dt": "2026-01-30T10:00:00"},
        {"auction_id": "B", "auction_start_dt": "2026-03-17T10:00:00"},
        {"auction_id": "C", "auction_start_dt": "2026-01-30T14:00:00"},
    ]
    # Pretend the matcher produced these two direct pairs.
    direct_pairs = [
        ("A", "B", "borrower_location_desc", "medium"),
        ("B", "C", "borrower_location_desc", "medium"),
    ]
    clusters, expanded = expand_clusters(auctions, direct_pairs)
    assert len(clusters) == 1
    assert set(clusters[0]) == {"A", "B", "C"}
    emitted = {(a, b) for a, b, _, _ in expanded}
    # A-B and B-C must survive; A-C must be dropped (same day).
    assert ("A", "B") in emitted
    assert ("B", "C") in emitted
    assert ("A", "C") not in emitted


def test_expand_clusters_lone_auction_produces_no_pairs() -> None:
    from scripts.link_reauctions import find_reauction_pairs, expand_clusters

    auctions = [
        {
            "auction_id": "A", "borrower": "Alice", "bank": "SBI",
            "city": "Chennai", "area": "Adyar", "total_area": "1000 sqft",
            "description": DESC_A1,
        },
    ]
    pairs = find_reauction_pairs(auctions)
    clusters, expanded = expand_clusters(auctions, pairs)
    assert clusters == []
    assert expanded == []
