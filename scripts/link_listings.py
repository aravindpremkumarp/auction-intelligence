"""
link_listings.py — the cross-portal bridge: :SAME_LISTING_AS between copies
of one auction on different portals.

Reads every listing the way scripts/gap_report.py does, loads the human
portal-match verdicts, runs sources.match.match_listings (bank + auction day +
reserve price + borrower; spec docs/superpowers/specs/2026-09-15-portal-match-governance-design.md)
and then, in order:

  1. the safety stop — refuses to write anything if CONFIRMED links would
     merge two listings of one portal with different prices or unit numbers,
     or if a listing is both confirmed and in review against the same portal;
  2. stores the review queue (every listing waiting for a person, plus a
     daily spot-check of 10 automatic confirmations) on
     (:PipelineState {key:'link_listings'}) for the review page;
  3. drops every :SAME_LISTING_AS edge and MERGEs the new pairs both ways —
     CONFIRMED links and PENDING review pairs — with {method, confidence,
     evidence, linked_at}. Same-source pairs are never written.

    python -m scripts.link_listings --dry-run      # match, check and summarise; write nothing
    python -m scripts.link_listings --queue-only   # also store the review queue; no edges
    python -m scripts.link_listings
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.resolution_review import portal_decisions  # noqa: E402
from scripts.audit_listing_links import same_source_clusters  # noqa: E402
from scripts.gap_report import FETCH_EXISTING, graph_candidate  # noqa: E402
from sources.match import (  # noqa: E402
    MatchResult, Pair, _units_disagree, day_of, match_listings, snapshot_of,
)

BATCH = 200
SPOT_CHECK_SIZE = 10
_SPOT_CHECK_METHODS = ("four_fields", "unit_number")

DROP_EXISTING = "MATCH ()-[r:SAME_LISTING_AS]->() DELETE r"

MERGE_PAIR = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.a_id})
MATCH (b:AuctionProperty {auction_id: r.b_id})
MERGE (a)-[ab:SAME_LISTING_AS]->(b)
  SET ab.method = r.method, ab.confidence = r.confidence, ab.evidence = r.evidence, ab.linked_at = datetime()
MERGE (b)-[ba:SAME_LISTING_AS]->(a)
  SET ba.method = r.method, ba.confidence = r.confidence, ba.evidence = r.evidence, ba.linked_at = datetime()
RETURN count(*) AS n
"""

LOAD_DECISIONS = """
MATCH (r:ResolutionDecision {kind: 'portal-match'})
RETURN r.key AS key, r.kind AS kind, r.verdict AS verdict, r.payload_json AS payload_json
"""

SAVE_QUEUE = """
MERGE (s:PipelineState {key: 'link_listings'})
SET s.review_json = $rows, s.spot_check_json = $spot, s.ran_at = datetime()
"""


class LinkSafetyError(RuntimeError):
    """The safety stop fired: nothing was written."""


def pair_rows(pairs: list[Pair]) -> list[dict]:
    return [{"a_id": p.a_id, "b_id": p.b_id, "method": p.method, "confidence": p.confidence, "evidence": p.evidence}
            for p in pairs if p.a_source != p.b_source]


def match(records: list[dict], decisions: dict[str, dict] | None = None) -> MatchResult:
    """Every cross-source verdict among the graph's listings. The whole graph
    is the ``incoming`` side so listings are compared with each other."""
    return match_listings([graph_candidate(r) for r in records], [], decisions=decisions)


def find_pairs(records: list[dict], decisions: dict[str, dict] | None = None) -> list[Pair]:
    return match(records, decisions).pairs


def safety_problems(result: MatchResult, records: list[dict]) -> list[str]:
    """Why writing this result would be unsafe; empty when it is safe."""
    by_id = {r["auction_id"]: r for r in records}
    cands = {aid: graph_candidate(r) for aid, r in by_id.items()}
    problems: list[str] = []

    for cluster in same_source_clusters([p.__dict__ for p in result.pairs]):
        by_source: dict[str, list[str]] = defaultdict(list)
        for aid in cluster:
            by_source[(by_id.get(aid) or {}).get("source") or "eauctionsindia"].append(aid)
        for source, ids in sorted(by_source.items()):
            if len(ids) < 2:
                continue
            prices = {round(float(by_id[i].get("reserve_price_num") or 0)) for i in ids if i in by_id}
            if len(prices) > 1:
                problems.append(f"confirmed links would merge {source} listings {', '.join(sorted(ids))} with different reserve prices")
                continue
            if any(_units_disagree(cands[x], cands[y]) for x in ids for y in ids if x < y and x in cands and y in cands):
                problems.append(f"confirmed links would merge {source} listings {', '.join(sorted(ids))} with different unit numbers")

    confirmed_against = {(p.a_id, p.b_source) for p in result.pairs if p.confidence == "CONFIRMED"}
    for a in result.ambiguous:
        if (a.auction_id, a.other_source) in confirmed_against:
            problems.append(f"{a.auction_id} is both confirmed and in review against {a.other_source}")
    return problems


def spot_check_sample(result: MatchResult, decided_keys: set[tuple[str, str]], run_date: date,
                      size: int = SPOT_CHECK_SIZE) -> list[tuple[str, str]]:
    """(subject, other source) pairs of automatic confirmations nobody has
    looked at, sampled the same way for the same day. Per other source, like
    decisions: a subject confirmed against two portals is two separate cases,
    and settling one must not silence the other."""
    eligible = sorted({(p.a_id, p.b_source) for p in result.pairs
                       if p.confidence == "CONFIRMED" and p.method in _SPOT_CHECK_METHODS
                       and (p.a_id, p.b_source) not in decided_keys})
    return sorted(random.Random(run_date.isoformat()).sample(eligible, min(size, len(eligible))))


def listing_row(rec: dict) -> dict:
    """What a reviewer reads about one listing."""
    return {"auction_id": rec.get("auction_id"), "source": rec.get("source") or "eauctionsindia",
            "bank": rec.get("bank"), "borrower": rec.get("borrower"), "reserve": rec.get("reserve_price_num"),
            "emd": rec.get("emd_num"), "auction_day": day_of(rec.get("auction_start_dt")),
            "city": rec.get("city"), "district": rec.get("district"), "title": rec.get("title"),
            "description": (rec.get("description") or "")[:600] or None,
            "url": rec.get("url"), "public_url": rec.get("public_url")}


def review_rows(result: MatchResult, records: list[dict], spot_keys: list[tuple[str, str]]) -> list[dict]:
    """The review queue: one row per subject waiting for a person, then one per
    spot-checked (subject, other source) confirmation."""
    by_id = {r["auction_id"]: r for r in records}
    rows: list[dict] = []
    for a in result.ambiguous:
        subject = by_id.get(a.auction_id)
        if subject is None:
            continue
        rows.append({"subject": listing_row(subject), "other_source": a.other_source, "reason": a.reason,
                     "candidates": [listing_row(by_id[c]) for c in a.candidates if c in by_id],
                     "spot_check": False, "snapshot": snapshot_of(graph_candidate(subject))})
    linked: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    for p in result.pairs:
        if (p.a_id, p.b_source) in spot_keys and p.confidence == "CONFIRMED":
            linked[(p.a_id, p.b_source)].append(p)
    for aid, other in spot_keys:
        subject = by_id.get(aid)
        pairs = linked[(aid, other)]
        if subject is None or not pairs:
            continue
        rows.append({"subject": listing_row(subject), "other_source": other, "reason": "spot_check",
                     "candidates": [listing_row(by_id[p.b_id]) for p in pairs if p.b_id in by_id],
                     "spot_check": True, "snapshot": snapshot_of(graph_candidate(subject))})
    return rows


def summarize(pairs: list[Pair]) -> str:
    by = Counter((p.confidence, p.method) for p in pairs)
    lines = [f"pairs: {len(pairs)}"]
    for (conf, method), n in sorted(by.items()):
        lines.append(f"  {conf:<9} {method:<12} {n}")
    return "\n".join(lines)


def _load_decisions(run_query) -> dict[str, dict]:
    rows = run_query(LOAD_DECISIONS)
    decisions = []
    for r in rows:
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        decisions.append({"key": r.get("key"), "kind": r.get("kind"), "verdict": r.get("verdict"), "payload": payload})
    return portal_decisions(decisions)


def run(dry_run: bool = False, queue_only: bool = False) -> int:
    from api.neo4j_client import run_query

    t0 = time.monotonic()
    records = run_query(FETCH_EXISTING)
    decisions = _load_decisions(run_query)
    print(f"  {len(records)} listings, {len(decisions)} portal-match decisions fetched in {time.monotonic() - t0:.0f}s")
    result = match(records, decisions)
    print(summarize(result.pairs))
    print(f"  waiting for review: {len(result.ambiguous)}  " +
          ", ".join(f"{r} {n}" for r, n in sorted(Counter(a.reason for a in result.ambiguous).items())))

    problems = safety_problems(result, records)
    if problems:
        for problem in problems:
            print(f"  SAFETY STOP: {problem}")
        raise LinkSafetyError("; ".join(problems))

    spot = spot_check_sample(result, set(decisions), date.today())
    rows = review_rows(result, records, spot)
    if dry_run:
        print(f"[dry-run] {len(rows)} review rows, spot-check {spot}; no writes")
        return 0
    run_query(SAVE_QUEUE, {"rows": json.dumps(rows, ensure_ascii=False), "spot": json.dumps([list(k) for k in spot])})
    print(f"  review queue stored: {len(rows)} rows")
    if queue_only:
        return 0
    run_query(DROP_EXISTING)
    out = pair_rows(result.pairs)
    for i in range(0, len(out), BATCH):
        run_query(MERGE_PAIR, {"rows": out[i:i + BATCH]})
    print(f"  {len(out)} pairs written ({len(out) * 2} directed edges) in {time.monotonic() - t0:.0f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="match, check and summarise; write nothing")
    ap.add_argument("--queue-only", action="store_true", help="store the review queue but write no edges")
    args = ap.parse_args(argv)
    try:
        return run(dry_run=args.dry_run, queue_only=args.queue_only)
    except LinkSafetyError:
        return 2


if __name__ == "__main__":
    sys.exit(main())
