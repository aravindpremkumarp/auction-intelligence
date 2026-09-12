"""Load one listing from each portal into the real Neo4j this lane talks to
(the CI job's neo4j:5.26 container) through the loader's own batch, and
check what Task 8 promised: three sources side by side, prefixed ids under
the same unique constraint, a :Media node per photo, and the one-off
source backfill touching only nodes that predate the adapters.

Rows are inline and prefixed ``e2e-`` so the test cleans up after itself
and can never collide with a real listing.
"""
from __future__ import annotations

import os

import pytest
from neo4j import GraphDatabase

from scripts import load_tn_to_neo4j as ld

_PREFIX = "e2e-src-"


def _row(aid, source, **extra):
    base = {"auction_id": aid, "url": f"https://x/{aid}", "title": "t", "description": "d",
            "reserve_price_num": 1000000.0, "auction_start_dt": "2026-09-24T11:00:00", "bank_name": "E2E Test Bank",
            "state": "E2E State", "downloads_list": [f"{aid}.pdf"], "downloads_found": [f"{aid}.pdf"],
            "downloads_complete": True, "property_types": ["House"]}
    if source:
        base["source"] = source
    return {**base, **extra}


@pytest.fixture
def session():
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
                                  auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]))
    db = os.environ.get("NEO4J_DATABASE") or None
    with driver.session(database=db) as s:
        yield s
        s.run("MATCH (a:AuctionProperty) WHERE a.auction_id STARTS WITH $p "
              "OPTIONAL MATCH (a)-[:HAS_MEDIA]->(m:Media) DETACH DELETE a, m", p=_PREFIX)
        s.run("MATCH (b:Bank {name: 'E2E Test Bank'}) DETACH DELETE b")
        s.run("MATCH (st:State {name: 'E2E State'}) DETACH DELETE st")
    driver.close()


def test_three_sources_load_side_by_side_with_media(session):
    ld.create_constraints(session)
    rows = [ld.sanitise(r) for r in (
        _row(f"{_PREFIX}841207", None),                                   # legacy: no source key
        _row(f"{_PREFIX}bn-359826", "baanknet", source_id="359826", source_rank=1, possession_type="symbolic",
             auction_status="live", district="Tirunelveli", fetched_at="2026-09-12T14:29:44+00:00",
             downloads_list=[], downloads_found=[],
             media=[{"url": f"https://cdn/{_PREFIX}119740.jpg", "kind": "image", "is_main": True},
                    {"url": f"https://cdn/{_PREFIX}v.mp4", "kind": "video", "is_main": False}]),
        _row(f"{_PREFIX}be-236961", "bankeauctions", source_id="236961", source_rank=2,
             documents=[{"url": "https://be/x.pdf", "filename": f"{_PREFIX}be-236961.pdf", "doc_role": "sale_notice"}]),
    )]
    assert ld.run_batch(session, rows) == 3

    got = {r["aid"]: r for r in session.run(
        "MATCH (a:AuctionProperty) WHERE a.auction_id STARTS WITH $p "
        "OPTIONAL MATCH (a)-[:HAS_MEDIA]->(m:Media) "
        "RETURN a.auction_id AS aid, a.source AS source, a.source_rank AS rank, a.source_id AS sid, "
        "a.possession_type AS possession, a.document_roles AS roles, a.photo_urls AS photos, "
        "a.last_seen_at IS NOT NULL AS seen, toString(a.fetched_at) AS fetched, "
        "collect(m {.url, .kind, .is_main, .source}) AS media", p=_PREFIX)}
    assert {v["source"] for v in got.values()} == {"eauctionsindia", "baanknet", "bankeauctions"}
    assert (got[f"{_PREFIX}841207"]["rank"], got[f"{_PREFIX}841207"]["sid"]) == (3, f"{_PREFIX}841207")
    bn = got[f"{_PREFIX}bn-359826"]
    assert (bn["rank"], bn["sid"], bn["possession"], bn["seen"]) == (1, "359826", "symbolic", True)
    assert bn["fetched"].startswith("2026-09-12T14:29:44")
    assert bn["photos"] == [f"https://cdn/{_PREFIX}119740.jpg"]
    assert sorted(m["kind"] for m in bn["media"]) == ["image", "video"]
    assert all(m["source"] == "baanknet" for m in bn["media"])
    assert got[f"{_PREFIX}be-236961"]["roles"] == ["sale_notice"]

    # loading again is idempotent under the unique constraint: still three nodes, two media
    assert ld.run_batch(session, rows) == 3
    n = session.run("MATCH (a:AuctionProperty) WHERE a.auction_id STARTS WITH $p RETURN count(a) AS n", p=_PREFIX).single()["n"]
    m = session.run("MATCH (m:Media) WHERE m.url CONTAINS $p RETURN count(m) AS n", p=_PREFIX).single()["n"]
    assert (n, m) == (3, 2)


def test_backfill_stamps_only_nodes_without_a_source(session):
    session.run("CREATE (:AuctionProperty {auction_id: $a, url: 'https://x/old'}), "
                "(:AuctionProperty {auction_id: $b, source: 'baanknet', source_rank: 1})",
                a=f"{_PREFIX}old", b=f"{_PREFIX}new")
    n = session.run(ld.BACKFILL_SOURCE_QUERY, source="eauctionsindia", rank=3).single()["n"]
    assert n >= 1
    rows = {r["aid"]: (r["source"], r["rank"], r["sid"], r["url"]) for r in session.run(
        "MATCH (a:AuctionProperty) WHERE a.auction_id IN [$a, $b] "
        "RETURN a.auction_id AS aid, a.source AS source, a.source_rank AS rank, a.source_id AS sid, a.source_url AS url",
        a=f"{_PREFIX}old", b=f"{_PREFIX}new")}
    assert rows[f"{_PREFIX}old"] == ("eauctionsindia", 3, f"{_PREFIX}old", "https://x/old")
    assert rows[f"{_PREFIX}new"] == ("baanknet", 1, None, None)
