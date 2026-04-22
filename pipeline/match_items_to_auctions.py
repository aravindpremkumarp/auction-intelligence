"""
pipeline/match_items_to_auctions.py
-----------------------------------
Stage 2b: for every AuctionProperty linked to a multi-property notice, pick the
`property_items` entry that best matches this auction and persist:

  AuctionProperty.matched_item_no          (string or null)
  AuctionProperty.matched_item_marker      (string or null)
  AuctionProperty.matched_item_text        (string or null)
  AuctionProperty.matched_item_page        (int or null)
  AuctionProperty.matched_item_confidence  ("high" | "medium" | "low" | "none")
  AuctionProperty.matched_item_document    (notice filename the match came from)
  AuctionProperty.matched_at               (datetime)

Matching signal weights (scored per candidate item, highest score wins):
  + reserve_price_num exact (<=0.1% diff)        +50
  + reserve_price_num within 1%                  +35
  + emd_num exact                                +20
  + emd_num within 5%                            +10
  + survey_no overlap (any survey_no matches)    +15
  + borrower_name fuzzy match (substring / token overlap, >=0.6) +15
  + village / area string overlap                +5

Confidence buckets on top score S:
  S >= 50 -> "high"
  S >= 25 -> "medium"
  S >  0  -> "low"
  else     -> "none"   (no fields set, apart from the run timestamp)

Run:
    python -m pipeline.match_items_to_auctions [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from typing import Any


# ── Neo4j selectors ─────────────────────────────────────────────────────────

SELECT_CANDIDATES = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)
WHERE d.property_items_json IS NOT NULL AND d.property_items_json <> '[]'
OPTIONAL MATCH (a)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
WITH a, d, collect(DISTINCT {survey_no: s.survey_no, subdivision: s.subdivision}) AS survey_numbers
RETURN a.auction_id           AS auction_id,
       a.borrower_name        AS borrower_name,
       a.reserve_price_num    AS reserve_price_num,
       a.emd_num              AS emd_num,
       a.village              AS village,
       a.area                 AS area,
       a.old_survey_numbers_json AS old_surveys_json,
       a.new_survey_numbers_json AS new_surveys_json,
       survey_numbers         AS survey_numbers,
       d.filename             AS notice_filename,
       d.property_items_json  AS items_json
"""


WRITE_MATCH = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.auction_id})
SET a.matched_item_no         = r.matched_item_no,
    a.matched_item_marker     = r.matched_item_marker,
    a.matched_item_text       = r.matched_item_text,
    a.matched_item_page       = r.matched_item_page,
    a.matched_item_confidence = r.matched_item_confidence,
    a.matched_item_document   = r.matched_item_document,
    a.matched_at              = datetime(r.matched_at)
RETURN count(a) AS updated
"""


# ── Scoring helpers ─────────────────────────────────────────────────────────

_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    return {t.lower() for t in _TOKEN.findall(s) if len(t) > 2}


def _num_close(a: Any, b: Any, tol: float) -> bool:
    try:
        x = float(a); y = float(b)
    except (TypeError, ValueError):
        return False
    if x == 0 or y == 0:
        return x == y
    return abs(x - y) / max(abs(x), abs(y)) <= tol


def _parse_json_list(s: Any) -> list[dict]:
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        val = json.loads(s)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _collect_surveys(auction: dict) -> set[str]:
    sn: set[str] = set()
    for sv in (auction.get("survey_numbers") or []):
        no = sv.get("survey_no") if isinstance(sv, dict) else None
        if no:
            sn.add(str(no).strip().lower())
    for src in (_parse_json_list(auction.get("old_surveys_json")),
                _parse_json_list(auction.get("new_surveys_json"))):
        for sv in src:
            no = sv.get("survey_no") if isinstance(sv, dict) else None
            if no:
                sn.add(str(no).strip().lower())
    return sn


def score_item(auction: dict, item: dict) -> int:
    s = 0

    ap = auction.get("reserve_price_num")
    ip = item.get("reserve_price_num")
    if ap and ip:
        if _num_close(ap, ip, 0.001): s += 50
        elif _num_close(ap, ip, 0.01): s += 35

    ae = auction.get("emd_num")
    ie = item.get("emd_num")
    if ae and ie:
        if _num_close(ae, ie, 0.001): s += 20
        elif _num_close(ae, ie, 0.05): s += 10

    auction_surveys = _collect_surveys(auction)
    item_surveys = {
        str((sv.get("survey_no") or "")).strip().lower()
        for sv in (item.get("survey_numbers") or [])
        if isinstance(sv, dict) and sv.get("survey_no")
    }
    if auction_surveys & item_surveys:
        s += 15

    ab = auction.get("borrower_name")
    ib = item.get("borrower_name")
    if ab and ib:
        at, bt = _tokens(ab), _tokens(ib)
        if at and bt:
            overlap = len(at & bt) / max(1, len(at | bt))
            if overlap >= 0.6:
                s += 15
            elif overlap >= 0.3:
                s += 7

    av = (auction.get("village") or "").strip().lower()
    iv = (item.get("village") or "").strip().lower()
    if av and iv and (av == iv or av in iv or iv in av):
        s += 5

    aa = (auction.get("area") or "").strip().lower()
    ia = (item.get("area") or "").strip().lower()
    if aa and ia and (aa == ia or aa in ia or ia in aa):
        s += 5

    return s


def confidence_bucket(score: int) -> str:
    if score >= 50: return "high"
    if score >= 25: return "medium"
    if score > 0:   return "low"
    return "none"


# ── Matching ────────────────────────────────────────────────────────────────

def choose_match(auction: dict, items: list[dict]) -> dict | None:
    if not items:
        return None
    scored = [(score_item(auction, it), it) for it in items]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_item = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    # If best and runner-up tie, the match is ambiguous — downgrade to low.
    if best_score > 0 and best_score == runner_up:
        return {
            "matched_item_no":         str(best_item.get("item_no") or ""),
            "matched_item_marker":     best_item.get("item_marker"),
            "matched_item_text":       best_item.get("item_text"),
            "matched_item_page":       best_item.get("page_number"),
            "matched_item_confidence": "low",
            "_score":                  best_score,
        }
    return {
        "matched_item_no":         str(best_item.get("item_no") or ""),
        "matched_item_marker":     best_item.get("item_marker"),
        "matched_item_text":       best_item.get("item_text"),
        "matched_item_page":       best_item.get("page_number"),
        "matched_item_confidence": confidence_bucket(best_score),
        "_score":                  best_score,
    }


# ── Runner ──────────────────────────────────────────────────────────────────

def run(limit: int | None, dry_run: bool) -> None:
    from pipeline.config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE
    from neo4j import GraphDatabase

    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("[ERROR] Neo4j credentials missing; cannot run matcher.")
        return

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            rows = list(s.run(SELECT_CANDIDATES))
    finally:
        driver.close()

    if limit:
        rows = rows[:limit]

    print(f"Matcher: {len(rows)} auction-document candidate(s)")

    now = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    conf_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}

    for r in rows:
        auction = dict(r)
        items = _parse_json_list(auction.get("items_json"))
        match = choose_match(auction, items)
        conf = match["matched_item_confidence"] if match else "none"
        conf_counts[conf] = conf_counts.get(conf, 0) + 1
        out.append({
            "auction_id":              auction["auction_id"],
            "matched_item_no":         (match or {}).get("matched_item_no"),
            "matched_item_marker":     (match or {}).get("matched_item_marker"),
            "matched_item_text":       (match or {}).get("matched_item_text"),
            "matched_item_page":       (match or {}).get("matched_item_page"),
            "matched_item_confidence": conf,
            "matched_item_document":   auction.get("notice_filename"),
            "matched_at":              now,
        })

    print(f"  Match confidence: {conf_counts}")

    if dry_run:
        print("  Dry run — not writing to Neo4j.")
        return

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            result = s.run(WRITE_MATCH, rows=out)
            print(f"  AuctionProperty updated: {int(result.single()['updated'])}")
    finally:
        driver.close()


def main():
    p = argparse.ArgumentParser(description="Match each auction to its property item in a multi-property notice")
    p.add_argument("--limit", type=int, default=None, help="Process only first N candidates")
    p.add_argument("--dry-run", action="store_true", help="Score and log without writing to Neo4j")
    args = p.parse_args()
    run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
