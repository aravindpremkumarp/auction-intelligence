"""Rebuild every notice's dated events as `:NoticeEvent`.

`pipeline/extraction_store` used to write a notice's timeline (inspection,
EMD deadline, auction) as `:AuctionEvent` — the same label the spine uses for
its merged record per auction. `scripts/build_spine.py` drops every
`:AuctionEvent` on each run, so each pipeline run wiped the timeline nodes
too, and they only came back for notices read again afterwards.

The writer now uses `:NoticeEvent`. This is the one-off that catches the
graph up:

1. rebuilds each document's events from `Document.extraction_timeline_json`
   — the same list the nodes were first written from, so nothing is guessed;
2. deletes what is left under the old name: `:AuctionEvent` nodes with no
   `event_id` (every spine record has one) that carry a timeline `event`.

Idempotent: a second run rewrites the same nodes. Spine records are never
touched.

Run:  NEO4J_HTTP_API=1 python -m scripts.backfill_notice_events --dry-run
      NEO4J_HTTP_API=1 python -m scripts.backfill_notice_events
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query

load_dotenv()

BATCH = 200

FETCH_TIMELINES = """
MATCH (d:Document)
WHERE d.extraction_timeline_json IS NOT NULL
RETURN d.filename AS fn, d.extraction_timeline_json AS timeline
"""

COUNT_LEGACY = """
MATCH (e:AuctionEvent)
WHERE e.event_id IS NULL AND e.event IS NOT NULL
RETURN count(e) AS n
"""

DROP_LEGACY = """
MATCH (e:AuctionEvent)
WHERE e.event_id IS NULL AND e.event IS NOT NULL
DETACH DELETE e
RETURN count(e) AS n
"""

# Same shape pipeline/extraction_store._EVENTS writes, one document per row.
WRITE_EVENTS = """
UNWIND $rows AS row
MATCH (d:Document {filename: row.fn})
OPTIONAL MATCH (d)-[:HAS_EVENT]->(old)
DETACH DELETE old
WITH DISTINCT d, row
UNWIND row.events AS ev
MERGE (ne:NoticeEvent {filename: d.filename, event: ev.event, lot_index: coalesce(ev.lot_index, '')})
SET ne.date = ev.date, ne.quote = ev.quote, ne.evidence = ev.evidence, ne.source = ev.source,
    ne.page = ev.page
MERGE (d)-[:HAS_EVENT]->(ne)
RETURN count(*) AS n
"""

_FIELDS = ("event", "date", "quote", "lot_index", "evidence", "source", "page")


def timeline_rows(docs: list[dict]) -> list[dict]:
    """`{fn, events}` per document whose stored timeline holds an event.

    An unreadable blob or an entry without an `event` name is skipped, never
    guessed at; every event carries every field so the Cypher reads no
    missing key.
    """
    rows: list[dict] = []
    for d in docs:
        try:
            timeline = json.loads(d.get("timeline") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(timeline, list):
            continue
        events = [{k: ev.get(k) for k in _FIELDS}
                  for ev in timeline if isinstance(ev, dict) and ev.get("event")]
        if events:
            rows.append({"fn": d["fn"], "events": events})
    return rows


def run(dry_run: bool = False) -> int:
    docs = run_read_query(FETCH_TIMELINES, max_rows=100_000, timeout=120.0)
    rows = timeline_rows(docs)
    events = sum(len(r["events"]) for r in rows)
    legacy = run_read_query(COUNT_LEGACY, timeout=60.0)
    n_legacy = int(legacy[0]["n"]) if legacy else 0
    print(f"  {len(docs)} documents with a stored timeline; "
          f"{len(rows)} hold {events} event(s)")
    print(f"  {n_legacy} timeline node(s) still labelled :AuctionEvent")
    if dry_run:
        print("[dry-run] no writes")
        return 0
    for i in range(0, len(rows), BATCH):
        run_query(WRITE_EVENTS, {"rows": rows[i:i + BATCH]})
        print(f"  rebuilt {min(i + BATCH, len(rows))}/{len(rows)}", end="\r")
    dropped = run_query(DROP_LEGACY)
    print(f"\n  {events} :NoticeEvent written; "
          f"{int(dropped[0]['n']) if dropped else 0} legacy node(s) deleted")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    return run(dry_run=ap.parse_args(argv).dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
