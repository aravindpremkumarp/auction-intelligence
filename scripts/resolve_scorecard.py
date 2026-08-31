"""How much of the corpus is trustworthy right now — and which way it moved.

READ-ONLY. Writes no graph data; the only file it touches is the JSON
snapshot you point --json at.

Why this exists
---------------
Every number in the 2026-08-31 resolve plan was computed by hand, once, in a
session. Nothing recomputed them, so a regression would surface as a user
complaint rather than a signal — and each fix that plan proposes had no way to
prove it worked. This turns that one-off audit into a standing measurement.

Deliberately NOT another audit: `scripts/audit_lot_links.py` answers "is the
lot-key migration safe to run" in depth. This answers "what fraction of what
we serve is trustworthy today", in metrics that stay comparable run over run.

What it reports
---------------
  LINKAGE     listings tied to their notice lot — the switch that turns a
              notice detail into a fact about THIS property (api/agent3/
              common.py::scope_of), so it gates every metric below it
  PLACES      lots resolved onto the official gazetteer, and the taluk
              strings blocking the rest — the biggest reviewer time sink, and
              the gate on parcel enrichment
  AGREEMENT   the two-witness checks: prices and areas that disagree
  FIELDS      notice-derived fields sitting on listings whose lot is NOT
              confirmed (the contested-field gate's remaining work)
  QUEUE       what a human still has to look at

Every metric carries `value`, `total` and `pct` where a share makes sense, so
a diff between two snapshots is meaningful without re-deriving anything.

Usage:
    NEO4J_HTTP_API=1 python -m scripts.resolve_scorecard
    NEO4J_HTTP_API=1 python -m scripts.resolve_scorecard --json card.json
    NEO4J_HTTP_API=1 python -m scripts.resolve_scorecard \
        --json card.json --compare last.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from api.neo4j_client import run_read_query


def q(cypher: str, params: dict | None = None, max_rows: int = 20_000):
    return run_read_query(cypher, params, max_rows=max_rows, timeout=120.0)


def one(cypher: str, params: dict | None = None) -> dict:
    rows = q(cypher, params, max_rows=1)
    return dict(rows[0]) if rows else {}


def metric(value: int, total: int | None = None, *, note: str = "") -> dict:
    """One comparable number. `pct` is of `total` when a share makes sense.

    Stored rather than computed at print time so a snapshot read months later
    means the same thing even if the denominator's definition moves.
    """
    out: dict = {"value": int(value)}
    if total is not None:
        out["total"] = int(total)
        out["pct"] = round(value / total * 100, 1) if total else 0.0
    if note:
        out["note"] = note
    return out


def collect() -> dict:
    """Every metric, in one pass per section."""
    card: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "sections": {}}

    # ── LINKAGE ──────────────────────────────────────────────────────────────
    # Aggregations only. A bare `count { ... }` beside them becomes a GROUPING
    # KEY, so the query returns one row per distinct link count and `one()`
    # silently reports the first group as the whole corpus — this read 2,804
    # of 2,804 (100%) before the stray expression came out.
    r = one("""
        MATCH (a:AuctionProperty)
        RETURN count(a) AS total,
               sum(CASE WHEN EXISTS { (a)-[:IS_LOT]->(:Lot) }
                        THEN 1 ELSE 0 END) AS linked
    """)
    total, linked = int(r.get("total") or 0), int(r.get("linked") or 0)
    # Why each unlinked listing is unlinked, in the shape the review queue
    # shows it — so a movement here names the fix that moved it.
    why = one("""
        MATCH (a:AuctionProperty) WHERE NOT (a)-[:IS_LOT]->(:Lot)
        OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(d:Document)
        WITH a, max(count { (d)-[:HAS_LOT]->(:Lot) }) AS lots
        RETURN sum(CASE WHEN lots IS NULL OR lots = 0 THEN 1 ELSE 0 END) AS no_lots,
               sum(CASE WHEN lots = 1 THEN 1 ELSE 0 END) AS single_lot,
               sum(CASE WHEN lots > 1 THEN 1 ELSE 0 END) AS multi_lot
    """)
    card["sections"]["linkage"] = {
        "listings_linked_to_a_lot": metric(linked, total),
        "unlinked_notice_has_no_lots": metric(int(why.get("no_lots") or 0), total),
        "unlinked_on_a_single_lot_notice": metric(
            int(why.get("single_lot") or 0), total,
            note="invisible to the review queue, which only lists lot_count > 1"),
        "unlinked_on_a_multi_lot_notice": metric(
            int(why.get("multi_lot") or 0), total),
    }

    # ── PLACES ───────────────────────────────────────────────────────────────
    r = one("""
        MATCH (l:Lot)
        RETURN count(l) AS total,
               sum(CASE WHEN EXISTS { (l)-[:IN_REVENUE_VILLAGE]->() }
                        THEN 1 ELSE 0 END) AS placed
    """)
    lots_total, placed = int(r.get("total") or 0), int(r.get("placed") or 0)
    # Rows vs distinct strings is the whole automation argument: a reviewer
    # judges strings, and the queue shows rows.
    blocked = one("""
        MATCH (a:AuctionProperty)
        WHERE a.place_village_status IS NOT NULL
          AND a.place_village_status <> 'resolved'
        RETURN count(*) AS rows,
               count(DISTINCT toLower(trim(coalesce(a.village, '?')))) AS village_strings
    """)
    # Scoped to 'no-parent-taluk' on purpose: those rows are blocked BY the
    # taluk, so this is the alias table's actual worklist. Counting taluk
    # strings across every unresolved status mixes in rows whose taluk
    # resolved fine and inflates the number a reviewer would plan against.
    taluks = one("""
        MATCH (a:AuctionProperty)
        WHERE a.place_village_status = 'no-parent-taluk' AND a.taluk IS NOT NULL
        RETURN count(*) AS rows,
               count(DISTINCT toLower(trim(a.taluk))) AS taluk_strings
    """)
    rows_blocked = int(blocked.get("rows") or 0)
    card["sections"]["places"] = {
        "lots_on_the_gazetteer": metric(
            placed, lots_total,
            note="gates parcel matching: it only fires for placed lots"),
        "unresolved_place_rows": metric(rows_blocked),
        "rows_blocked_by_their_taluk": metric(int(taluks.get("rows") or 0)),
        "distinct_taluk_strings_blocking_them": metric(
            int(taluks.get("taluk_strings") or 0),
            note="the alias table's worklist: one decision settles many rows"),
        "distinct_village_strings_unresolved": metric(
            int(blocked.get("village_strings") or 0)),
    }

    # ── AGREEMENT ────────────────────────────────────────────────────────────
    # Both checks are rebuilt by apply_extractions each pass, so an absent
    # flag means "agrees or not comparable", never "not yet checked".
    price = one("""
        MATCH (a:AuctionProperty)
        RETURN count(a) AS total,
               sum(CASE WHEN a.price_agreement IS NOT NULL THEN 1 ELSE 0 END) AS flagged,
               sum(CASE WHEN a.price_agreement_severity = 'critical'
                        THEN 1 ELSE 0 END) AS critical
    """)
    area = one("""
        MATCH (a:AuctionProperty)
        RETURN sum(CASE WHEN a.area_agreement IS NOT NULL THEN 1 ELSE 0 END) AS flagged,
               sum(CASE WHEN a.area_agreement_severity = 'critical'
                        THEN 1 ELSE 0 END) AS critical
    """)
    # Has each check ever written? `write_*_findings` clears every flag and
    # rewrites, so "the corpus agrees" and "the pipeline has not run since
    # this check shipped" both look like 0 findings — and the comparable
    # denominator alone cannot separate them (a live 0 of 2,176 read as
    # "measured, clean" when the check had simply never run). db.propertyKeys
    # is append-only and never garbage-collected, so a key's presence is a
    # durable record that the writer has run at least once.
    ever = one("""
        CALL db.propertyKeys() YIELD propertyKey
        WITH collect(propertyKey) AS keys
        RETURN 'price_agreement' IN keys AS price, 'area_agreement' IN keys AS area
    """)

    # The denominator each check could reach, so a real 0 says how far it
    # looked rather than just "none".
    comparable = one("""
        MATCH (a:AuctionProperty)-[:IS_LOT]->(l:Lot)
        OPTIONAL MATCH (l)-[e:HAS_EXTENT]->(m:Measurement) WHERE e.is_headline
        OPTIONAL MATCH (l)-[:OFFERED_IN]->(au:Auction)
        WITH a, l,
             max(CASE WHEN m.sqft_norm IS NOT NULL THEN 1 ELSE 0 END) AS has_extent,
             max(CASE WHEN au.reserve_price_num IS NOT NULL THEN 1 ELSE 0 END) AS has_price
        RETURN sum(CASE WHEN a.total_area IS NOT NULL AND has_extent = 1
                        THEN 1 ELSE 0 END) AS area_pairs,
               sum(CASE WHEN a.reserve_price_num IS NOT NULL AND has_price = 1
                        THEN 1 ELSE 0 END) AS price_pairs
    """)
    def check(flagged: int, pairs: int, has_run: bool, what: str) -> dict:
        m = metric(flagged, pairs,
                   note=f"of the confirmed pairs where both sides carry a {what}")
        if not has_run:
            m["not_yet_run"] = True
            m["note"] = ("NOT MEASURED — this check has never written; "
                         "run pipeline.apply_extractions")
        return m

    card["sections"]["agreement"] = {
        "price_disagreements": check(
            int(price.get("flagged") or 0), int(comparable.get("price_pairs") or 0),
            bool(ever.get("price")), "price"),
        "price_disagreements_critical": metric(
            int(price.get("critical") or 0),
            note="a clean power-of-ten gap — a dropped zero, not a judgement call"),
        "area_disagreements": check(
            int(area.get("flagged") or 0), int(comparable.get("area_pairs") or 0),
            bool(ever.get("area")), "size"),
        "area_disagreements_critical": metric(int(area.get("critical") or 0)),
    }

    # ── FIELDS ───────────────────────────────────────────────────────────────
    # The contested-field gate stops new writes and clears old ones on the
    # next run; this is what is still sitting there.
    fields = one("""
        MATCH (a:AuctionProperty) WHERE NOT (a)-[:IS_LOT]->(:Lot)
        RETURN count(*) AS unlinked,
               sum(CASE WHEN a.grounded_source_file IS NOT NULL
                        THEN 1 ELSE 0 END) AS with_notice_fields,
               sum(CASE WHEN a.village IS NOT NULL THEN 1 ELSE 0 END) AS with_village,
               sum(CASE WHEN a.description_source = 'notice'
                        THEN 1 ELSE 0 END) AS with_notice_description
    """)
    card["sections"]["fields"] = {
        "unlinked_carrying_notice_fields": metric(
            int(fields.get("with_notice_fields") or 0),
            int(fields.get("unlinked") or 0),
            note="cleared on the next apply_extractions run where contested"),
        "unlinked_carrying_a_village": metric(
            int(fields.get("with_village") or 0),
            int(fields.get("unlinked") or 0)),
        "unlinked_carrying_a_notice_description": metric(
            int(fields.get("with_notice_description") or 0),
            int(fields.get("unlinked") or 0)),
    }

    # ── QUEUE ────────────────────────────────────────────────────────────────
    # What a human still has to look at, counted the way the review page
    # counts it, minus what a verdict has already settled.
    decided = one("""
        MATCH (r:ResolutionDecision)
        WHERE r.verdict IN ['approved', 'rejected']
        RETURN count(r) AS n
    """)
    queue = one("""
        MATCH (p:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_LOT]->(l:Lot)
        WITH p, count(l) AS lot_count
        WHERE lot_count > 1 AND NOT (p)-[:IS_LOT]->(:Lot)
        RETURN count(DISTINCT p) AS n
    """)
    card["sections"]["queue"] = {
        "lot_matches_awaiting_review": metric(int(queue.get("n") or 0)),
        "verdicts_banked": metric(int(decided.get("n") or 0)),
    }
    return card


def _flat(card: dict) -> dict[str, dict]:
    return {f"{sec}.{name}": m
            for sec, metrics in card.get("sections", {}).items()
            for name, m in metrics.items()}


def render(card: dict, previous: dict | None = None) -> None:
    prev = _flat(previous) if previous else {}
    for sec, metrics in card["sections"].items():
        print(f"\n{'─' * 72}\n{sec.upper()}\n{'─' * 72}")
        for name, m in metrics.items():
            # A metric whose check has never written is not a zero — showing
            # it as one invites reading "clean" off a number nobody computed.
            share = ("      —" if m.get("not_yet_run") else
                     f"{m['value']:>7,} / {m['total']:,} ({m['pct']}%)"
                     if "total" in m else f"{m['value']:>7,}")
            delta = ""
            was = prev.get(f"{sec}.{name}")
            if was is not None:
                d = m["value"] - was["value"]
                # A bare number, no good/bad colouring: whether a metric
                # rising is progress depends on the metric, and the caller
                # knows which they were trying to move.
                delta = f"   {d:+,} since {previous.get('generated_at', '?')[:10]}"
            print(f"  {name:<42} {share}{delta}")
            if m.get("note"):
                print(f"  {'':<42} {m['note']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", metavar="PATH",
                    help="write this run's snapshot for a later --compare")
    ap.add_argument("--compare", metavar="PATH",
                    help="an earlier snapshot to show movement against")
    args = ap.parse_args(argv)

    previous = None
    if args.compare:
        try:
            with open(args.compare, encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, ValueError) as exc:
            # A missing or damaged baseline must not lose this run's numbers.
            print(f"  (ignoring --compare {args.compare}: {exc})")

    card = collect()
    print(f"\nresolve scorecard · {card['generated_at'][:19]}Z")
    render(card, previous)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(card, fh, indent=2, ensure_ascii=False, default=str)
        print(f"\n  snapshot written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
