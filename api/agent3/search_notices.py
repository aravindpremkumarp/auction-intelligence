"""
api/agent3/search_notices.py
------------------------------
Free-text search over what the sale notice actually says — not vector
search. `Lot.description_embedding` exists as an index name
(`lot_description_embedding`) but holds **zero vectors**; `AuctionProperty
.description_embedding` is populated (2,179 of 2,964) but is the wrong tool
for this job — a fulltext match on "borewell" or "disputed pathway" is a
precise term-presence question, not a semantic-similarity one, and Lucene
answers it exactly where an embedding would only answer it approximately.

Two indexes, both live:
  lot_description_ft   Lot.full_description        (the schedule text)
  property_text_idx    AuctionProperty.title+description (the portal blurb)

Handles: "borewell", "shed on agricultural land", "disputed pathway", "north
facing corner plot", "tiled roof" — anything a buyer would ask by describing
the property in words rather than by filter.
"""
from __future__ import annotations

import re

from api.agent3.common import ToolInputError, clamp_limit, scope_note, scope_of, tool
from api.neo4j_client import run_read_query

_MIN_QUERY_CHARS = 3
_SNIPPET_CHARS = 240

#: Characters Lucene's query parser treats as operators. Stripped from bare
#: words; left alone inside a "quoted phrase" so the caller can ask for an
#: exact phrase when order matters ("corner plot" vs corner AND plot).
_LUCENE_SPECIALS_RE = re.compile(r'[+\-!(){}\[\]^"~*?:\\/]')
_PHRASE_RE = re.compile(r'"([^"]+)"')


def _build_lucene_query(text: str) -> str | None:
    """Free text -> a Lucene query that requires every term (AND).

    OR is Lucene's default when terms are just space-separated, which on this
    corpus is nearly useless: "north facing corner plot" as plain terms
    matches 2,824 of 3,335 lots, because almost every lot mentions at least
    one of those common words. AND-joining brought the same query down to 2 —
    verified against the live graph. Quoted phrases pass through untouched so
    "corner plot" can be asked for as an exact phrase.
    """
    phrases = _PHRASE_RE.findall(text or "")
    remainder = _PHRASE_RE.sub(" ", text or "")
    bare = [t for t in _LUCENE_SPECIALS_RE.sub(" ", remainder).split() if t]
    parts = [f'"{p}"' for p in phrases if p.strip()] + bare
    return " AND ".join(parts) if parts else None


_LOT_CYPHER = """
CALL db.index.fulltext.queryNodes('lot_description_ft', $q) YIELD node AS l, score
MATCH (l)<-[:HAS_LOT]-(:Document)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
WITH a, l, score
MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(anylot:Lot)
WITH a, l, score, count(DISTINCT anylot) AS lot_count
RETURN a.auction_id AS auction_id, 'lot' AS source, score,
       left(l.full_description, $snippet_chars) AS snippet, lot_count
ORDER BY score DESC LIMIT $limit
"""

_LISTING_CYPHER = """
CALL db.index.fulltext.queryNodes('property_text_idx', $q) YIELD node AS a, score
WITH a, score
MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(anylot:Lot)
WITH a, score, count(DISTINCT anylot) AS lot_count
RETURN a.auction_id AS auction_id, 'listing' AS source, score,
       left(a.description, $snippet_chars) AS snippet, lot_count
ORDER BY score DESC LIMIT $limit
"""


@tool
def search_notices(query: str, limit: int = 10) -> dict:
    """Search the actual text of sale notices and listing descriptions.

    Use this for qualitative, free-text questions a filter cannot express:
    boundaries, neighbourhood detail, construction condition, encumbrance
    wording, or anything you would look for by reading the notice rather than
    by filtering a field — "borewell", "disputed pathway", "north facing
    corner plot", "tiled roof". For anything structured or countable
    (location, price, extent, possession, bank), use `find_properties`
    instead — it is exact where this is approximate, and it returns counts
    this tool cannot.

    Every term you pass is required (AND) unless quoted as an exact phrase
    with "double quotes" — plain space-separated terms behave like an OR on
    this corpus and return mostly noise (verified: "north facing corner
    plot" as bare terms matches 2,824 of 3,335 lots; AND-joined, 2).

    This is NOT vector/semantic search — lot description embeddings are
    empty in this graph. A word that never appears in any notice returns
    zero, even if a synonym would have matched; try the synonym before
    concluding nothing exists.

    Each result carries `notice_lot_count` and `scope`: `lot` when the
    matched notice covers exactly this one lot (the text is safely this
    property's own), `notice` when it covers several (the text describes the
    notice, which this listing shares with others — say so, don't present it
    as this specific property's own description).
    """
    q = _build_lucene_query(query)
    if q is None or len(q.replace('"', "").replace(" AND ", "")) < _MIN_QUERY_CHARS:
        raise ToolInputError(
            f"query={query!r} has nothing searchable in it after removing "
            f"punctuation — use real words from the notice.")
    limit = clamp_limit(limit, default=10)
    params = {"q": q, "limit": limit, "snippet_chars": _SNIPPET_CHARS}

    lot_rows = run_read_query(_LOT_CYPHER, params, timeout=15.0, max_rows=limit)
    listing_rows = run_read_query(_LISTING_CYPHER, params, timeout=15.0, max_rows=limit)

    # A lot match and a listing match can name the same auction_id — keep the
    # higher-scoring hit and note both matched, rather than showing the
    # listing twice.
    merged: dict[str, dict] = {}
    for row in [*lot_rows, *listing_rows]:
        aid = row["auction_id"]
        lot_count = row.get("lot_count") or 0
        shaped = {
            "auction_id": aid,
            "matched_in": row["source"],
            "snippet": row["snippet"],
            "score": round(float(row["score"]), 3),
            "notice_lot_count": lot_count,
            "scope": scope_of(lot_count),
        }
        note = scope_note("this snippet", lot_count)
        if note:
            shaped["scope_note"] = note
        existing = merged.get(aid)
        if existing is None:
            merged[aid] = shaped
        elif shaped["score"] > existing["score"]:
            shaped["also_matched_in"] = existing["matched_in"]
            merged[aid] = shaped
        else:
            existing["also_matched_in"] = shaped["matched_in"]

    results = sorted(merged.values(), key=lambda r: r["score"], reverse=True)[:limit]
    out: dict = {"query_parsed": q, "results": results, "result_count": len(results)}
    if not results:
        out["hint"] = (
            "No notice text matches. This is a literal term search, not "
            "semantic — try a plain synonym (e.g. 'well' instead of "
            "'borewell') before concluding the graph has nothing on this.")
    return out
