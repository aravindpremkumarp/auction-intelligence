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


def test_the_spine_is_built_from_bridged_listings(session):
    """The whole Task 9 path against a real Neo4j: two portals' copies of one
    auction are loaded, link_listings bridges them, build_spine merges them
    into one :AuctionEvent with LISTS / DEPICTS, and the event chain stamps
    it as attempt 1 of a chain of 1. All three scripts read the same NEO4J_*
    this lane runs on."""
    from scripts import build_spine as bs
    from scripts import link_listings as ll
    from scripts import link_reauctions as lr

    ld.create_constraints(session)
    day = "2026-09-24T11:00:00"
    rows = [ld.sanitise(r) for r in (
        _row(f"{_PREFIX}841207", None, bank_name="E2E Spine Bank", reserve_price_num=4626500.0, auction_start_dt=day,
             borrower_name="Mr. N. Mariappan", title="House at Tirunelveli, S.No 381/5A, total extent 1215 sq.ft",
             city="Tirunelveli"),
        _row(f"{_PREFIX}bn-359826", "baanknet", source_id="359826", source_rank=1, bank_name="E2E Spine Bank",
             reserve_price_num=4626500.0, auction_start_dt=day, borrower_name="N MARIAPPAN",
             title="D no 81A, S.No.381 BY 5A, naranammalpuram village, total extent 1215 sqft",
             possession_type="symbolic", auction_status="live", district="Tirunelveli",
             downloads_list=[], downloads_found=[],
             media=[{"url": f"https://cdn/{_PREFIX}spine.jpg", "kind": "image", "is_main": True}]),
        _row(f"{_PREFIX}be-1", "bankeauctions", source_id="1", source_rank=2, bank_name="E2E Spine Bank",
             reserve_price_num=9900000.0, auction_start_dt=day, borrower_name="Someone Else"),
    )]
    assert ld.run_batch(session, rows) == 3
    try:
        assert ll.run() == 0
        assert bs.run() == 0
        assert lr.run_events() == 0

        got = session.run(
            "MATCH (a:AuctionProperty)-[:LISTS]->(e:AuctionEvent) WHERE a.auction_id STARTS WITH $p "
            "OPTIONAL MATCH (m:Media)-[:DEPICTS]->(e) "
            "RETURN e.event_id AS eid, e.listing_ids AS ids, e.core_complete AS core, e.has_photos AS photos, "
            "e.confidence AS conf, e.possession_type AS possession, e.attempt_no AS attempt, e.chain_size AS chain, "
            "count(DISTINCT m) AS media", p=_PREFIX)
        by_event = {r["eid"]: r for r in got}
        merged = next(r for r in by_event.values() if len(r["ids"]) == 2)
        assert sorted(merged["ids"]) == sorted([f"{_PREFIX}841207", f"{_PREFIX}bn-359826"])
        assert merged["conf"] == "PROBABLE" and merged["photos"] is True and merged["media"] == 1
        assert merged["possession"] == "symbolic" and merged["core"] >= 6
        assert (merged["attempt"], merged["chain"]) == (1, 1)
        single = next(r for r in by_event.values() if r["ids"] == [f"{_PREFIX}be-1"])
        assert single["conf"] == "SINGLE" and single["photos"] is False

        bridge = session.run(
            "MATCH (a:AuctionProperty {auction_id: $a})-[r:SAME_LISTING_AS]->(b:AuctionProperty {auction_id: $b}) "
            "RETURN r.method AS method, r.confidence AS confidence",
            a=f"{_PREFIX}bn-359826", b=f"{_PREFIX}841207").single()
        assert bridge and bridge["method"] in ("identifier", "borrower") and bridge["confidence"] == "PROBABLE"
    finally:
        session.run("MATCH (e:AuctionEvent) WHERE any(i IN e.listing_ids WHERE i STARTS WITH $p) DETACH DELETE e", p=_PREFIX)
        session.run("MATCH (b:Bank {name: 'E2E Spine Bank'}) DETACH DELETE b")
