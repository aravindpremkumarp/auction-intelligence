"""Tests for graph_property_count_async — the live AuctionProperty count the
agent injects so "how many properties" answers track the real graph size
instead of a hardcoded number. Verifies the query result, the in-process
cache (TTL + refresh bypass), and the graceful-degradation paths (empty /
failed read serve the last good value, or None on a cold cache)."""
from __future__ import annotations

import asyncio


def _clear_cache():
    import api.tools.cypher_tools as ct
    ct._PROPERTY_COUNT_CACHE.clear()


def test_reads_live_count(monkeypatch):
    _clear_cache()
    import api.tools.cypher_tools as ct

    async def fake(cypher, params=None, timeout=10.0, max_rows=200):
        assert "count(a)" in cypher and "AuctionProperty" in cypher
        return [{"n": 889}]

    monkeypatch.setattr(ct, "run_read_query_async", fake)
    assert asyncio.run(ct.graph_property_count_async()) == 889


def test_cached_within_ttl_then_refresh(monkeypatch):
    _clear_cache()
    import api.tools.cypher_tools as ct

    calls = {"n": 0}

    async def fake(cypher, params=None, timeout=10.0, max_rows=200):
        calls["n"] += 1
        return [{"n": 889}]

    monkeypatch.setattr(ct, "run_read_query_async", fake)

    assert asyncio.run(ct.graph_property_count_async()) == 889
    assert calls["n"] == 1
    # Second call within TTL is served from cache — no new query.
    assert asyncio.run(ct.graph_property_count_async()) == 889
    assert calls["n"] == 1
    # refresh=True bypasses the cache.
    assert asyncio.run(ct.graph_property_count_async(refresh=True)) == 889
    assert calls["n"] == 2


def test_none_on_cold_cache_failure(monkeypatch):
    _clear_cache()
    import api.tools.cypher_tools as ct

    async def boom(cypher, params=None, timeout=10.0, max_rows=200):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(ct, "run_read_query_async", boom)
    assert asyncio.run(ct.graph_property_count_async()) is None


def test_none_on_empty_result(monkeypatch):
    _clear_cache()
    import api.tools.cypher_tools as ct

    async def empty(cypher, params=None, timeout=10.0, max_rows=200):
        return []

    monkeypatch.setattr(ct, "run_read_query_async", empty)
    assert asyncio.run(ct.graph_property_count_async()) is None


def test_serves_last_good_value_on_transient_failure(monkeypatch):
    _clear_cache()
    import api.tools.cypher_tools as ct

    async def ok(cypher, params=None, timeout=10.0, max_rows=200):
        return [{"n": 889}]

    monkeypatch.setattr(ct, "run_read_query_async", ok)
    assert asyncio.run(ct.graph_property_count_async(refresh=True)) == 889

    async def boom(cypher, params=None, timeout=10.0, max_rows=200):
        raise RuntimeError("transient blip")

    monkeypatch.setattr(ct, "run_read_query_async", boom)
    # A failed re-read after a good one keeps serving the cached count.
    assert asyncio.run(ct.graph_property_count_async(refresh=True)) == 889
