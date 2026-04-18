"""Guardrail tests for the `run_cypher` agent escape hatch.

These tests exercise the Python-side defenses (regex + param coercion +
length cap). They do not need a live Neo4j — only that `run_read_query`
is monkeypatched so successful queries return a canned result without
touching the network.
"""
from __future__ import annotations

import pytest


def _patch_read_query(monkeypatch):
    calls: list[tuple[str, dict, dict]] = []

    def fake_run_read_query(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {}), {"timeout": timeout, "max_rows": max_rows}))
        return [{"ok": True}]

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake_run_read_query)
    return calls


@pytest.mark.parametrize(
    "bad_cypher",
    [
        "CREATE (n:AuctionProperty {auction_id: 'x'}) RETURN n",
        "MATCH (a:AuctionProperty) DELETE a",
        "MATCH (a:AuctionProperty) DETACH DELETE a",
        "MATCH (a:AuctionProperty) SET a.reserve_price_num = 0 RETURN a",
        "MATCH (a:AuctionProperty) REMOVE a.title RETURN a",
        "MERGE (n:City {name: 'Chennai'}) RETURN n",
        "DROP CONSTRAINT ON (a:AuctionProperty) ASSERT a.auction_id IS UNIQUE",
        "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
        "FOREACH (x IN [1,2,3] | CREATE (:N {v: x}))",
        # Case-insensitive coverage
        "create (n:Test) return n",
        "match (a:AuctionProperty) set a.x = 1 return a",
    ],
)
def test_run_cypher_rejects_writes(monkeypatch, bad_cypher):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(ValueError, match="run_cypher rejects"):
        run_cypher(bad_cypher)


@pytest.mark.parametrize(
    "bad_cypher",
    [
        "CALL apoc.create.node(['Foo'], {name: 'x'})",
        "CALL apoc.merge.node(['Foo'], {name: 'x'}, {}, {})",
        "CALL apoc.refactor.mergeNodes([n])",
        "CALL apoc.cypher.runWrite('MATCH (n) DELETE n', {})",
        "CALL db.createLabel('Foo')",
    ],
)
def test_run_cypher_rejects_write_procedures(monkeypatch, bad_cypher):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(ValueError, match="write procedures"):
        run_cypher(bad_cypher)


def test_run_cypher_allows_read_queries(monkeypatch):
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    out = run_cypher(
        "MATCH (a:AuctionProperty) RETURN count(a) AS n",
        params={"city": "Chennai"},
        description="count auctions",
    )
    assert out["returned"] == 1
    assert out["rows"] == [{"ok": True}]
    assert out["description"] == "count auctions"
    cypher, params, config = calls[0]
    assert "MATCH (a:AuctionProperty)" in cypher
    assert params == {"city": "Chennai"}
    assert config["timeout"] == 10.0
    assert config["max_rows"] == 200


def test_run_cypher_allows_apoc_read_only_procedures(monkeypatch):
    """APOC read procedures (apoc.path.*, apoc.text.*) are NOT rejected —
    only write-side ones (create/merge/refactor/runWrite) are."""
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    out = run_cypher("CALL apoc.meta.data() YIELD label RETURN label")
    assert out["returned"] == 1


def test_run_cypher_empty_and_too_long(monkeypatch):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(ValueError, match="cypher is empty"):
        run_cypher("")

    huge = "MATCH (a) RETURN a" + (" // " + "x" * 8000)
    with pytest.raises(ValueError, match="exceeds"):
        run_cypher(huge)


def test_run_cypher_coerces_params(monkeypatch):
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    run_cypher(
        "MATCH (a) WHERE a.x IN $ids RETURN a",
        params={"ids": [1, 2, 3], "name": "Chennai", "flag": True, "null_val": None},
    )
    _, params, _ = calls[0]
    assert params == {"ids": [1, 2, 3], "name": "Chennai", "flag": True, "null_val": None}


@pytest.mark.parametrize(
    "bad_params",
    [
        {"callable": lambda: None},                 # function
        {"obj": object()},                          # unsupported object
        {"nested_dict": {"x": 1}},                  # nested dict not allowed
        {"mixed_list": [1, {"a": 1}]},              # non-primitive inside list
    ],
)
def test_run_cypher_rejects_bad_params(monkeypatch, bad_params):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(ValueError):
        run_cypher("MATCH (a) RETURN a", params=bad_params)


def test_run_cypher_rejects_non_string_cypher(monkeypatch):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(ValueError, match="cypher must be a string"):
        run_cypher(123)  # type: ignore[arg-type]


def test_run_cypher_max_rows_bounded(monkeypatch):
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import run_cypher

    run_cypher("MATCH (a) RETURN a", max_rows=999)  # requested > 500
    _, _, config = calls[0]
    assert config["max_rows"] == 500  # hard-capped

    run_cypher("MATCH (a) RETURN a", max_rows=0)  # requested < 1
    _, _, config = calls[1]
    assert config["max_rows"] == 1  # floored


def test_run_cypher_wraps_neo4j_error(monkeypatch):
    """Neo4jError from the driver is surfaced as RuntimeError with the
    server message preserved — the agent sees it and can self-correct."""
    from neo4j.exceptions import Neo4jError

    def raising_run_read_query(cypher, params=None, timeout=10.0, max_rows=200):
        err = Neo4jError("Variable `foo` not defined")
        raise err

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", raising_run_read_query)
    from api.tools.cypher_tools import run_cypher

    with pytest.raises(RuntimeError, match="Neo4j error"):
        run_cypher("MATCH (a) RETURN foo")
