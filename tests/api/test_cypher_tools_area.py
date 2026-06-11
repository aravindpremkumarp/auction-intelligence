"""Verify the `area` filter added to search_auctions — the fix for feedback
item 19224426 ("I am only able to filter by city where area/village I am not
able to"). Uses the existing LOCATED_IN_AREA relationship with a case-
insensitive CONTAINS match so users can type "ambattur" or "Ambattur".
"""
from __future__ import annotations


def _patch_run_query(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        return []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    monkeypatch.setattr(
        ct, "run_read_query",
        lambda cypher, params=None, timeout=10.0, max_rows=200: fake_run_query(cypher, params),
    )
    return calls


def test_area_filter_produces_case_insensitive_contains(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", area="ambattur", limit=0)

    cypher, params = calls[0]
    assert "(a)-[:LOCATED_IN_AREA]->(ar:Area)" in cypher
    assert "any(x IN $area WHERE toLower(ar.name) CONTAINS toLower(x))" in cypher
    assert params["area"] == ["ambattur"]
    assert params["city"] == ["Chennai"]


def test_area_filter_combines_with_property_type(monkeypatch) -> None:
    """Area + property_type together must keep the HAS_PROPERTY_TYPE edge
    (the merged fix for the shared-taxonomy bug)."""
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(area="Sriperumbudur", property_type="Flat", limit=0)

    cypher, params = calls[0]
    assert "(a)-[:LOCATED_IN_AREA]->(ar:Area)" in cypher
    assert "(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)" in cypher
    assert "pt.name IN $property_type" in cypher
    assert params["property_type"] == ["Flat"]


def test_multi_area_and_multi_property_type(monkeypatch) -> None:
    """List inputs must produce list params and any-match Cypher (area)
    plus IN-list Cypher (property_type) — the 'independent houses in
    Chrompet/Tambaram/Pallavaram' shape."""
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(
        city="Chennai",
        area=["Chrompet", "Tambaram", "Pallavaram"],
        property_type=["House", "Villa", "Bungalow", "Land And Building"],
        limit=0,
    )

    cypher, params = calls[0]
    assert "any(x IN $area WHERE toLower(ar.name) CONTAINS toLower(x))" in cypher
    assert "pt.name IN $property_type" in cypher
    assert params["area"] == ["Chrompet", "Tambaram", "Pallavaram"]
    assert params["property_type"] == ["House", "Villa", "Bungalow", "Land And Building"]


def test_no_area_means_no_area_clause(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", limit=0)

    cypher, params = calls[0]
    assert "LOCATED_IN_AREA" not in cypher  # no area filter requested
    assert "area" not in params
