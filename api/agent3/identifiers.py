"""
api/agent3/identifiers.py
--------------------------
Survey/patta/door/plot number lookup — shared by `find_properties`'s
`identifier=` filter and the standalone `find_by_identifier` tool.

Backed by the `identifier_raw_ft` fulltext index over 10,253 identifiers on
3,215 lots (96%). An Identifier is reachable two ways, and both are walked:

    (Lot)-[:MENTIONS_IDENTIFIER]->(Identifier)     15,484 rels — the lot text
    (Parcel)-[:HAS_IDENTIFIER]->(Identifier)        14,608 rels — the parcel
    (AuctionProperty)-[:IS_PARCEL]->(Parcel)

A survey number is full of characters Lucene treats as query syntax
(`123/4B`, `S.No 45-2`), so the value is phrase-quoted rather than escaped
term by term — that sidesteps every operator at once.
"""
from __future__ import annotations

from api.agent3 import enums
from api.agent3.common import require_enum
from api.neo4j_client import run_read_query


def escape_lucene(text: str) -> str:
    """Phrase-quote a value for a fulltext query."""
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').strip() + '"'


def resolve_identifier(value: str, kind: str | None = None,
                       limit: int = 200) -> list[str]:
    """Survey / patta / door / plot number -> matching auction_ids."""
    kind = require_enum(kind, enums.IDENTIFIER_KINDS, "identifier_kind")
    rows = run_read_query(_RESOLVE_CYPHER, {"q": escape_lucene(value), "kind": kind,
                                            "limit": int(limit)},
                          timeout=15.0, max_rows=limit)
    return [r["auction_id"] for r in rows if r.get("auction_id")]


_RESOLVE_CYPHER = """
CALL db.index.fulltext.queryNodes('identifier_raw_ft', $q) YIELD node AS i
WHERE $kind IS NULL OR i.kind = $kind
CALL {
  WITH i
  MATCH (i)<-[:MENTIONS_IDENTIFIER]-(:Lot)<-[:HAS_LOT]-(:Document)
        <-[:HAS_DOCUMENT]-(a:AuctionProperty)
  RETURN a.auction_id AS auction_id
  UNION
  WITH i
  MATCH (i)<-[:HAS_IDENTIFIER]-(:Parcel)<-[:IS_PARCEL]-(a:AuctionProperty)
  RETURN a.auction_id AS auction_id
}
RETURN DISTINCT auction_id LIMIT $limit
"""

#: The rich lookup: which identifier(s) matched, on which lot, on which
#: listing — so a "is this survey number in any notice" question doesn't
#: need a follow-up get_property just to see what matched.
_DETAIL_CYPHER = """
CALL db.index.fulltext.queryNodes('identifier_raw_ft', $q) YIELD node AS i, score
WHERE $kind IS NULL OR i.kind = $kind
CALL {
  WITH i, score
  MATCH (i)<-[:MENTIONS_IDENTIFIER]-(l:Lot)<-[:HAS_LOT]-(:Document)
        <-[:HAS_DOCUMENT]-(a:AuctionProperty)
  WITH a, i, score, l
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(anylot:Lot)
  WITH a, i, score, l, count(DISTINCT anylot) AS lot_count
  RETURN a.auction_id AS auction_id, i.kind AS matched_kind,
         i.value_raw AS matched_value, score AS match_score, l.lot_key AS lot_key,
         l.property_type AS lot_property_type, lot_count
  UNION
  WITH i, score
  MATCH (i)<-[:HAS_IDENTIFIER]-(:Parcel)<-[:IS_PARCEL]-(a:AuctionProperty)
  OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(anylot:Lot)
  WITH a, i, score, count(DISTINCT anylot) AS lot_count
  RETURN a.auction_id AS auction_id, i.kind AS matched_kind,
         i.value_raw AS matched_value, score AS match_score, NULL AS lot_key,
         NULL AS lot_property_type, lot_count
}
OPTIONAL MATCH (a:AuctionProperty {auction_id: auction_id})-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(b:Bank)
RETURN auction_id, matched_kind, matched_value, match_score AS score, lot_key,
       lot_property_type, lot_count, c.name AS city, b.name AS bank,
       // Phase 2: the lot comes from the edge, not the string beside it. A
       // key is "<filename>#<lot_index>" and lot_index is the model's own
       // numbering, so a re-extraction renumbers the lots and a stale key
       // still RESOLVES — to a different property. The edge names the node.
       a.title AS title, [(a)-[:IS_LOT]->(_lot:Lot) | _lot.lot_key][0] AS resolved_lot_key
ORDER BY score DESC LIMIT $limit
"""


def resolve_identifier_detail(value: str, kind: str | None = None,
                              limit: int = 20) -> list[dict]:
    kind = require_enum(kind, enums.IDENTIFIER_KINDS, "identifier_kind")
    return run_read_query(_DETAIL_CYPHER, {"q": escape_lucene(value), "kind": kind,
                                           "limit": int(limit)},
                          timeout=15.0, max_rows=limit)
