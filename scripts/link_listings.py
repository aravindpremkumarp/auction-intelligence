"""
link_listings.py — the cross-portal bridge: :SAME_LISTING_AS between copies
of one auction on different portals.

Reads every listing the way scripts/gap_report.py does (bank, reserve, day,
borrower, boundaries, identifiers, notice fingerprints), runs
sources.match.find_same_listing_pairs across sources, drops every existing
:SAME_LISTING_AS edge and MERGEs the new pairs both ways with
{method, confidence, linked_at} — the same shape scripts/link_reauctions.py
gives :SAME_PROPERTY_AS. Same-source pairs are never written.

    python -m scripts.link_listings --dry-run
    python -m scripts.link_listings
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gap_report import FETCH_EXISTING, graph_candidate  # noqa: E402
from sources.match import Pair, find_same_listing_pairs  # noqa: E402

BATCH = 200

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


def pair_rows(pairs: list[Pair]) -> list[dict]:
    return [{"a_id": p.a_id, "b_id": p.b_id, "method": p.method, "confidence": p.confidence, "evidence": p.evidence}
            for p in pairs if p.a_source != p.b_source]


def find_pairs(records: list[dict]) -> list[Pair]:
    """Every cross-source pair among the graph's listings. The whole graph is
    the ``incoming`` side so listings are compared with each other; nothing
    is 'existing' here."""
    return find_same_listing_pairs([graph_candidate(r) for r in records], [])


def summarize(pairs: list[Pair]) -> str:
    by = Counter((p.confidence, p.method) for p in pairs)
    lines = [f"pairs: {len(pairs)}"]
    for (conf, method), n in sorted(by.items()):
        lines.append(f"  {conf:<9} {method:<12} {n}")
    return "\n".join(lines)


def run(dry_run: bool = False) -> int:
    from api.neo4j_client import run_query

    t0 = time.monotonic()
    records = run_query(FETCH_EXISTING)
    print(f"  {len(records)} listings fetched in {time.monotonic() - t0:.0f}s")
    pairs = find_pairs(records)
    print(summarize(pairs))
    if dry_run:
        print("[dry-run] no writes")
        return 0
    run_query(DROP_EXISTING)
    rows = pair_rows(pairs)
    for i in range(0, len(rows), BATCH):
        run_query(MERGE_PAIR, {"rows": rows[i:i + BATCH]})
    print(f"  {len(rows)} pairs written ({len(rows) * 2} directed edges) in {time.monotonic() - t0:.0f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="match and summarise, write nothing")
    args = ap.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
