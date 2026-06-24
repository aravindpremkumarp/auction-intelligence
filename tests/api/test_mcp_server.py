"""
Tests for the Tier-1 MCP connector (api/mcp_server.py).

The wrapper functions are thin adapters over api.tools.cypher_tools /
scoring.auction_scorer and are importable WITHOUT the optional `mcp` package,
so their logic — especially the ChatGPT-connector search/fetch shaping and the
public/read-only tool surface — is covered in CI (which installs the lock,
where `mcp` is not yet pinned). A final test exercises FastMCP registration and
is skipped unless `mcp` is installed.
"""
from __future__ import annotations

import pytest


def test_tool_surface_is_public_and_readonly() -> None:
    from api import mcp_server
    names = set(mcp_server.tool_names())
    # The core public read-only surface is present...
    assert {
        "search_auctions", "semantic_search", "match_pasted_listing",
        "get_auction_detail", "score_auction", "list_distinct",
        "upcoming_auctions", "borrower_lookup", "describe_schema",
        "search", "fetch",
    } <= names
    # ...and the per-user / metered tools are deliberately NOT exposed.
    assert not ({"watch_property", "list_alerts", "query_user_dossier",
                 "internet_search", "select_properties"} & names)


def test_run_cypher_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import mcp_server
    monkeypatch.setenv("MCP_EXPOSE_CYPHER", "false")
    assert "run_cypher" not in mcp_server.tool_names()
    monkeypatch.setenv("MCP_EXPOSE_CYPHER", "true")
    assert "run_cypher" in mcp_server.tool_names()


def test_search_shapes_chatgpt_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import mcp_server
    monkeypatch.setenv("MCP_PUBLIC_SITE_BASE", "https://example.test")
    monkeypatch.setattr(
        mcp_server.T, "semantic_search",
        lambda *a, **k: {"results": [
            {"auction_id": "TN-1", "address": "12 Beach Rd, Chennai"},
            {"id": "TN-2"},          # title falls back to "Auction TN-2"
            {"score": 0.4},          # no id → skipped, not crashed
        ], "returned": 3},
    )
    out = mcp_server.search("plot near the beach", limit=5)
    assert [r["id"] for r in out["results"]] == ["TN-1", "TN-2"]
    assert out["results"][0]["title"] == "12 Beach Rd, Chennai"
    assert out["results"][0]["url"] == "https://example.test/property/TN-1"
    assert out["results"][1]["title"] == "Auction TN-2"


def test_fetch_shapes_chatgpt_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import mcp_server
    monkeypatch.setattr(
        mcp_server.T, "get_auction_detail",
        lambda aid: {"auction_id": aid, "city": "Kanchipuram", "reserve_price": 2500000},
    )
    doc = mcp_server.fetch("TN-9")
    assert doc["id"] == "TN-9"
    assert "Kanchipuram" in doc["text"]
    assert doc["metadata"]["reserve_price"] == 2500000

    # A missing auction degrades to a well-formed doc, it doesn't raise.
    monkeypatch.setattr(mcp_server.T, "get_auction_detail", lambda aid: None)
    miss = mcp_server.fetch("nope")
    assert miss["metadata"] == {}
    assert "not found" in miss["title"].lower()


def test_semantic_search_degrades_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import mcp_server

    def _boom(*a, **k):
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(mcp_server.T, "semantic_search", _boom)
    out = mcp_server.semantic_search("anything")
    assert out["results"] == []
    assert "embedding backend down" in out["error"]


def test_score_auction_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import mcp_server

    class _Score:
        def to_dict(self) -> dict:
            return {"auction_id": "TN-3", "composite_score": 72.0, "grade": "B", "dimensions": []}

    monkeypatch.setattr(
        mcp_server, "_score_auction", lambda aid: _Score() if aid == "TN-3" else None
    )
    assert mcp_server.score_auction("TN-3")["grade"] == "B"
    assert mcp_server.score_auction("missing") is None


def test_transport_security_defaults_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from api import mcp_server

    # Unset → DNS-rebinding protection ON, seeded with the known deployment hosts
    # (so the connector isn't 421'd in prod) plus localhost for dev.
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    sec = mcp_server._transport_security()
    assert sec.enable_dns_rebinding_protection is True
    assert "api.auctionscope.in" in sec.allowed_hosts
    assert "localhost:*" in sec.allowed_hosts

    # Override replaces the host list (and accepts origins).
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, mcp.example.com:*")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://chat.openai.com")
    sec2 = mcp_server._transport_security()
    assert sec2.allowed_hosts == ["mcp.example.com", "mcp.example.com:*"]
    assert sec2.allowed_origins == ["https://chat.openai.com"]
    assert "api.auctionscope.in" not in sec2.allowed_hosts


def test_build_mcp_registers_tools() -> None:
    pytest.importorskip("mcp")
    import asyncio
    from api import mcp_server
    server = mcp_server.build_mcp()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"search_auctions", "semantic_search", "search", "fetch", "score_auction"} <= names
