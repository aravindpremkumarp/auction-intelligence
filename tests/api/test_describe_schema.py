"""Tests for describe_schema — verifies the composite shape returned from
the multi-query schema introspector. Each underlying Cypher call is
stubbed and matched by prefix so the test is robust to whitespace."""
from __future__ import annotations


def _install_schema_stub(monkeypatch):
    import api.tools.cypher_tools as ct

    def fake_run_read_query(cypher, params=None, timeout=10.0, max_rows=200):
        c = " ".join(cypher.split())  # normalize whitespace
        if c.startswith("CALL db.labels()"):
            return [{"label": "AuctionProperty"}, {"label": "City"}]
        if c.startswith("CALL db.relationshipTypes()"):
            return [{"t": "LOCATED_IN_CITY"}, {"t": "CONDUCTED_BY"}]
        if "count(n)" in c and ":`AuctionProperty`" in c:
            return [{"n": 3391}]
        if "count(n)" in c and ":`City`" in c:
            return [{"n": 180}]
        if "count(r)" in c and ":`LOCATED_IN_CITY`" in c:
            return [{"n": 3391}]
        if "count(r)" in c and ":`CONDUCTED_BY`" in c:
            return [{"n": 3391}]
        if "keys(n)" in c and ":`AuctionProperty`" in c:
            return [{"props": ["auction_id", "title", "reserve_price_num"]}]
        if "keys(n)" in c and ":`City`" in c:
            return [{"props": ["name"]}]
        if "(n:AssetCategory)" in c:
            return [{"v": "Residential"}, {"v": "Commercial"}]
        if "(n:PropertyType)" in c:
            return [{"v": "Flat"}, {"v": "Plot"}]
        if "min(a.reserve_price_num)" in c:
            return [{
                "rp_min": 10000.0, "rp_max": 500000000.0,
                "rp_p50": 3000000.0, "rp_p95": 20000000.0,
                "emd_min": 1000.0, "emd_max": 50000000.0, "emd_p50": 300000.0,
                "start_min": "2025-01-01T00:00:00",
                "start_max": "2026-12-31T00:00:00",
                "dl_min": "2025-01-01T00:00:00",
                "dl_max": "2026-12-31T00:00:00",
            }]
        return []

    monkeypatch.setattr(ct, "run_read_query", fake_run_read_query)
    # Isolate the live-compute path: force a live compute (no durable node) and
    # swallow the write so the test never reaches a real Neo4j.
    monkeypatch.setattr(ct, "_read_schema_cache_node", lambda: None)
    monkeypatch.setattr(ct, "_write_schema_cache_node", lambda dynamic: None)


def test_describe_schema_shape(monkeypatch):
    # Clear the in-process cache first — other tests may have populated it.
    import api.tools.cypher_tools as ct
    ct._SCHEMA_CACHE.clear()

    _install_schema_stub(monkeypatch)
    from api.tools.cypher_tools import describe_schema

    out = describe_schema(refresh=True)

    labels = {n["label"]: n for n in out["node_labels"]}
    assert "AuctionProperty" in labels
    assert labels["AuctionProperty"]["count"] == 3391
    assert "auction_id" in labels["AuctionProperty"]["sample_properties"]

    rel_types = {r["type"] for r in out["relationships"]}
    assert {"LOCATED_IN_CITY", "CONDUCTED_BY"}.issubset(rel_types)

    assert "Residential" in out["enums"]["asset_category"]
    assert "Flat" in out["enums"]["property_type"]

    assert out["numeric_ranges"]["reserve_price_num"]["min"] == 10000.0
    assert out["numeric_ranges"]["reserve_price_num"]["p95"] == 20000000.0
    assert out["date_ranges"]["auction_start_dt"]["min"] == "2025-01-01T00:00:00"

    # cypher_patterns moved off the system prompt onto describe_schema so the
    # agent fetches them on demand for run_cypher composition. The structure
    # the chat agent relies on is {rules: [...], examples: [{purpose, cypher}, ...]}.
    patterns = out["cypher_patterns"]
    assert isinstance(patterns["rules"], list) and len(patterns["rules"]) >= 5
    assert isinstance(patterns["examples"], list) and len(patterns["examples"]) >= 10
    for ex in patterns["examples"]:
        assert set(ex.keys()) == {"purpose", "cypher"}
        assert ex["purpose"] and ex["cypher"]
    # Spot-check that the load-bearing rules survived the move.
    rules_text = " ".join(patterns["rules"]).lower()
    assert "match each relationship independently" in rules_text
    assert "zoned datetime" in rules_text
    assert "iso string" in rules_text
    # Spot-check a key example shape so a regression doesn't quietly drop it.
    purposes = {ex["purpose"].lower() for ex in patterns["examples"]}
    assert any("count auctions per city" in p for p in purposes)
    assert any("re-auction velocity" in p for p in purposes)


def test_describe_schema_cached(monkeypatch):
    import api.tools.cypher_tools as ct
    ct._SCHEMA_CACHE.clear()

    call_count = {"n": 0}

    def tracking_run_read_query(cypher, params=None, timeout=10.0, max_rows=200):
        call_count["n"] += 1
        # Minimal valid schema to populate the cache.
        c = " ".join(cypher.split())
        if c.startswith("CALL db.labels()"):
            return [{"label": "AuctionProperty"}]
        if c.startswith("CALL db.relationshipTypes()"):
            return [{"t": "LOCATED_IN_CITY"}]
        if "count(n)" in c:
            return [{"n": 3391}]
        if "count(r)" in c:
            return [{"n": 3391}]
        if "keys(n)" in c:
            return [{"props": ["auction_id"]}]
        if "(n:AssetCategory)" in c or "(n:PropertyType)" in c:
            return []
        if "min(a.reserve_price_num)" in c:
            return [{}]
        return []

    monkeypatch.setattr(ct, "run_read_query", tracking_run_read_query)
    # Force the live-compute path so the call counting measures introspection
    # queries, not the durable node read.
    monkeypatch.setattr(ct, "_read_schema_cache_node", lambda: None)
    monkeypatch.setattr(ct, "_write_schema_cache_node", lambda dynamic: None)

    from api.tools.cypher_tools import describe_schema

    describe_schema()
    first_calls = call_count["n"]
    assert first_calls > 0

    # Second call within TTL hits the cache — no new Cypher queries.
    describe_schema()
    assert call_count["n"] == first_calls

    # refresh=True bypasses the cache.
    describe_schema(refresh=True)
    assert call_count["n"] > first_calls


def test_describe_schema_reads_durable_node(monkeypatch):
    """A warm :SchemaCache node serves describe_schema in one read — zero live
    introspection queries — and cypher_patterns are re-attached fresh from code."""
    import api.tools.cypher_tools as ct
    ct._SCHEMA_CACHE.clear()

    canned_dynamic = {
        "node_labels": [
            {"label": "AuctionProperty", "count": 42, "sample_properties": ["auction_id"]},
        ],
        "relationships": [{"type": "LOCATED_IN_CITY", "count": 42}],
        "enums": {"asset_category": ["Residential"], "property_type": ["Flat"]},
        "numeric_ranges": {"reserve_price_num": {"min": 1.0, "p50": 2.0, "p95": 3.0, "max": 4.0}},
        "date_ranges": {"auction_start_dt": {"min": "2025-01-01", "max": "2026-12-31"}},
        "date_capabilities": {"type": "ZONED DATETIME (UTC)"},
    }

    def boom_run_read_query(*args, **kwargs):
        raise AssertionError("ran a live introspection query despite a warm durable node")

    # Serve the durable node; any fall-through to live compute would call
    # run_read_query and fail the test.
    monkeypatch.setattr(ct, "_read_schema_cache_node", lambda: dict(canned_dynamic))
    monkeypatch.setattr(ct, "run_read_query", boom_run_read_query)

    from api.tools.cypher_tools import describe_schema

    out = describe_schema()  # no refresh → durable read path

    assert out["node_labels"][0]["count"] == 42
    assert out["enums"]["asset_category"] == ["Residential"]
    # cypher_patterns are NOT stored in the node — they're re-attached from code,
    # so editing the rules/examples is never shadowed by a stale cache.
    assert "cypher_patterns" not in canned_dynamic
    assert isinstance(out["cypher_patterns"]["rules"], list)
    assert len(out["cypher_patterns"]["rules"]) >= 5
    assert len(out["cypher_patterns"]["examples"]) >= 10
