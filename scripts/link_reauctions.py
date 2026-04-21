"""
scripts/link_reauctions.py
--------------------------
Detect re-auctions (same property auctioned more than once) and materialise
them as `:SAME_PROPERTY_AS` relationships in Neo4j.

Rules (strong signals only; see /root/.claude/plans/... or the team plan doc):

  1. Survey-number rule  — two auctions share a SurveyNumber (survey_no +
     subdivision) AND either same Borrower OR same City+Area.
  2. Borrower+location rule — same Borrower, Bank, and Area, with total_area
     agreeing within ±10% (normalised to sq ft).

Clusters are transitive: if A matches B and B matches C, all three are
linked. Each pair in a cluster is connected with a bidirectional MERGE.

Run:
    python -m scripts.link_reauctions            # detect + write
    python -m scripts.link_reauctions --dry-run  # detect only, print stats
    python -m scripts.link_reauctions --rebuild  # drop existing edges first
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter, defaultdict
from typing import Iterable

from neo4j import GraphDatabase

from pipeline.config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
    NEO4J_DATABASE, NEO4J_BATCH_SIZE,
)


# ── Total-area parsing ───────────────────────────────────────────────────────

# Multipliers to convert various area units into sq ft.
_UNIT_TO_SQFT: dict[str, float] = {
    "sqft": 1.0, "sq ft": 1.0, "sq. ft": 1.0, "sq.ft": 1.0, "sft": 1.0,
    "ft2": 1.0, "ft^2": 1.0, "square feet": 1.0, "square foot": 1.0,
    "sqm": 10.7639, "sq m": 10.7639, "sq. m": 10.7639, "m2": 10.7639,
    "square meter": 10.7639, "square metre": 10.7639, "square meters": 10.7639,
    "sqyd": 9.0, "sq yd": 9.0, "sq. yd": 9.0, "yd2": 9.0,
    "square yard": 9.0, "square yards": 9.0,
    "cent": 435.6,
    "acre": 43560.0, "acres": 43560.0,
    "hectare": 107639.0, "hectares": 107639.0,
    "ground": 2400.0,  # Tamil Nadu convention
}

_NUMBER_RE = re.compile(r"([0-9][0-9,]*\.?[0-9]*)")


def parse_total_area_sqft(raw: str | None) -> float | None:
    """Parse a free-text area string into sq ft. Returns None if unparseable.

    Handles: "1295 Sq Ft", "120 sq m", "0.5 acres", "3 cents", "1,250 sqft".
    Strings without a recognised unit fall back to assuming sq ft if the
    number is >= 50 (anything smaller is likely in a larger unit we can't
    identify — reject rather than mismatch).
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    m = _NUMBER_RE.search(s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if num <= 0:
        return None

    # Pick the longest matching unit suffix (so "sq m" wins over "m").
    tail = s[m.end():].strip()
    best: tuple[int, float] | None = None
    for unit, mult in _UNIT_TO_SQFT.items():
        if unit in tail:
            if best is None or len(unit) > best[0]:
                best = (len(unit), mult)
    if best is not None:
        return num * best[1]
    # Bare number: only trust if it's a plausible sq-ft figure.
    if num >= 50:
        return num
    return None


def areas_agree(a: float | None, b: float | None, tolerance: float = 0.10) -> bool:
    if a is None or b is None:
        return False
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tolerance


# ── Union-find for transitive clustering ─────────────────────────────────────

class _UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self._parent: dict[str, str] = {k: k for k in keys}

    def find(self, x: str) -> str:
        p = self._parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for k in self._parent:
            out[self.find(k)].append(k)
        return out


# ── Matching ─────────────────────────────────────────────────────────────────

def _norm(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s.lower() if s else None


def find_reauction_pairs(auctions: list[dict]) -> list[tuple[str, str, str, str]]:
    """Return (a_id, b_id, match_reason, confidence) pairs.

    `auctions` is a list of dicts with keys:
        auction_id, borrower, bank, city, area, total_area,
        survey_numbers (list of {survey_no, subdivision})
    """
    pairs: list[tuple[str, str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def _add(a: str, b: str, reason: str, confidence: str) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        pairs.append((*key, reason, confidence))

    # Rule 1: survey-number buckets.
    by_survey: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in auctions:
        for sn in row.get("survey_numbers") or []:
            sn_no = _norm(sn.get("survey_no"))
            if not sn_no:
                continue
            sn_sub = _norm(sn.get("subdivision")) or ""
            by_survey[(sn_no, sn_sub)].append(row)

    for bucket in by_survey.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                # Survey match alone is not enough — require borrower OR
                # (city + area) to also align.
                ba, bb = _norm(a.get("borrower")), _norm(b.get("borrower"))
                ca, cb = _norm(a.get("city")), _norm(b.get("city"))
                aa, ab = _norm(a.get("area")), _norm(b.get("area"))
                borrower_match = ba and bb and ba == bb
                location_match = ca and cb and ca == cb and aa and ab and aa == ab
                if borrower_match or location_match:
                    _add(
                        a["auction_id"], b["auction_id"],
                        "survey_number", "high",
                    )

    # Rule 2: borrower + bank + area + total_area agreement.
    by_bba: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in auctions:
        br, bk, ar = _norm(row.get("borrower")), _norm(row.get("bank")), _norm(row.get("area"))
        if not (br and bk and ar):
            continue
        by_bba[(br, bk, ar)].append(row)

    for bucket in by_bba.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                area_a = parse_total_area_sqft(a.get("total_area"))
                area_b = parse_total_area_sqft(b.get("total_area"))
                if not areas_agree(area_a, area_b):
                    continue
                _add(
                    a["auction_id"], b["auction_id"],
                    "borrower_location", "medium",
                )

    return pairs


def expand_clusters(
    auctions: list[dict],
    pairs: list[tuple[str, str, str, str]],
) -> tuple[list[list[str]], list[tuple[str, str, str, str]]]:
    """Apply union-find to make clustering transitive, then emit every
    pair within each cluster with the best reason/confidence seen."""
    uf = _UnionFind(a["auction_id"] for a in auctions)
    pair_meta: dict[tuple[str, str], tuple[str, str]] = {}
    for a, b, reason, conf in pairs:
        uf.union(a, b)
        key = (a, b) if a < b else (b, a)
        prev = pair_meta.get(key)
        if prev is None or _confidence_rank(conf) > _confidence_rank(prev[1]):
            pair_meta[key] = (reason, conf)

    groups = uf.groups()
    clusters = [g for g in groups.values() if len(g) > 1]

    expanded: list[tuple[str, str, str, str]] = []
    for group in clusters:
        # Pick the strongest reason among any pair in the cluster as the
        # per-cluster default for pairs we didn't match directly.
        best_reason, best_conf = "borrower_location", "medium"
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                key = (group[i], group[j]) if group[i] < group[j] else (group[j], group[i])
                rc = pair_meta.get(key)
                if rc and _confidence_rank(rc[1]) > _confidence_rank(best_conf):
                    best_reason, best_conf = rc
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                key = (a, b) if a < b else (b, a)
                reason, conf = pair_meta.get(key, (best_reason, best_conf))
                expanded.append((*key, reason, conf))
    return clusters, expanded


def _confidence_rank(c: str) -> int:
    return {"high": 2, "medium": 1}.get(c, 0)


# ── Neo4j IO ─────────────────────────────────────────────────────────────────

FETCH_CANDIDATES = """
MATCH (a:AuctionProperty)
OPTIONAL MATCH (a)-[:HAS_BORROWER]->(br:Borrower)
OPTIONAL MATCH (a)-[:CONDUCTED_BY]->(bk:Bank)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
OPTIONAL MATCH (a)-[:HAS_SURVEY_NUMBER]->(s:SurveyNumber)
WITH a, br, bk, c, ar,
     collect(DISTINCT CASE WHEN s IS NULL THEN NULL ELSE
        {survey_no: s.survey_no, subdivision: coalesce(s.subdivision, '')}
     END) AS raw_surveys
RETURN a.auction_id       AS auction_id,
       a.total_area        AS total_area,
       br.name             AS borrower,
       bk.name             AS bank,
       c.name              AS city,
       ar.name             AS area,
       [x IN raw_surveys WHERE x IS NOT NULL]  AS survey_numbers
"""

DROP_EXISTING = """
MATCH ()-[r:SAME_PROPERTY_AS]->() DELETE r
"""

MERGE_PAIR = """
UNWIND $rows AS r
MATCH (a:AuctionProperty {auction_id: r.a_id})
MATCH (b:AuctionProperty {auction_id: r.b_id})
MERGE (a)-[ab:SAME_PROPERTY_AS]->(b)
  ON CREATE SET ab.match_reason = r.reason,
                ab.confidence   = r.confidence,
                ab.linked_at    = datetime()
  ON MATCH  SET ab.match_reason = r.reason,
                ab.confidence   = r.confidence,
                ab.linked_at    = datetime()
MERGE (b)-[ba:SAME_PROPERTY_AS]->(a)
  ON CREATE SET ba.match_reason = r.reason,
                ba.confidence   = r.confidence,
                ba.linked_at    = datetime()
  ON MATCH  SET ba.match_reason = r.reason,
                ba.confidence   = r.confidence,
                ba.linked_at    = datetime()
"""


def fetch_auctions(session) -> list[dict]:
    return [dict(r) for r in session.run(FETCH_CANDIDATES)]


def debug_diagnostics(auctions: list[dict]) -> None:
    """Print coverage stats + near-miss groups so we can see why matching
    isn't firing on real data. Meant for the --debug flag."""
    n = len(auctions)
    print(f"\n=== Field coverage ({n} auctions) ===")
    has_borrower = sum(1 for a in auctions if _norm(a.get("borrower")))
    has_bank     = sum(1 for a in auctions if _norm(a.get("bank")))
    has_area     = sum(1 for a in auctions if _norm(a.get("area")))
    has_city     = sum(1 for a in auctions if _norm(a.get("city")))
    has_total    = sum(1 for a in auctions if a.get("total_area"))
    has_parsed   = sum(1 for a in auctions if parse_total_area_sqft(a.get("total_area")))
    has_surveys  = sum(1 for a in auctions if a.get("survey_numbers"))

    def _pct(x: int) -> str:
        return f"{x} ({100*x/n:.1f}%)" if n else f"{x}"

    print(f"  borrower set:          {_pct(has_borrower)}")
    print(f"  bank set:              {_pct(has_bank)}")
    print(f"  area set:              {_pct(has_area)}")
    print(f"  city set:              {_pct(has_city)}")
    print(f"  total_area populated:  {_pct(has_total)}")
    print(f"  total_area parseable:  {_pct(has_parsed)}")
    print(f"  survey_numbers:        {_pct(has_surveys)}")

    # Borrower duplicates (normalised).
    by_borrower: dict[str, list[dict]] = defaultdict(list)
    for a in auctions:
        b = _norm(a.get("borrower"))
        if b:
            by_borrower[b].append(a)
    repeats = [(b, aucts) for b, aucts in by_borrower.items() if len(aucts) > 1]
    repeats.sort(key=lambda x: -len(x[1]))
    print(f"\n=== Borrowers with >1 auction: {len(repeats)} ===")
    for b, aucts in repeats[:10]:
        print(f"  '{b}' — {len(aucts)} auctions")

    # Survey-number duplicates.
    by_survey: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in auctions:
        for sn in a.get("survey_numbers") or []:
            sno = _norm(sn.get("survey_no"))
            if not sno:
                continue
            sub = _norm(sn.get("subdivision")) or ""
            by_survey[(sno, sub)].append(a)
    sv_repeats = [(k, v) for k, v in by_survey.items() if len(v) > 1]
    sv_repeats.sort(key=lambda x: -len(x[1]))
    print(f"\n=== Survey numbers on >1 auction: {len(sv_repeats)} ===")
    for (sno, sub), aucts in sv_repeats[:10]:
        print(f"  survey_no={sno!r} subdivision={sub!r} — {len(aucts)} auctions")

    # Near-misses: same (borrower, bank, area) tuples — show the data so we
    # can see WHY borrower_location didn't fire (usually total_area mismatch
    # or one side unparseable).
    by_bba: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for a in auctions:
        br, bk, ar = _norm(a.get("borrower")), _norm(a.get("bank")), _norm(a.get("area"))
        if br and bk and ar:
            by_bba[(br, bk, ar)].append(a)
    bba_repeats = [(k, v) for k, v in by_bba.items() if len(v) > 1]
    bba_repeats.sort(key=lambda x: -len(x[1]))
    print(f"\n=== (borrower, bank, area) candidates with >1 auction: {len(bba_repeats)} ===")
    for (br, bk, ar), aucts in bba_repeats[:5]:
        print(f"\n  borrower={br!r}  bank={bk!r}  area={ar!r}  ({len(aucts)} auctions)")
        for a in aucts:
            parsed = parse_total_area_sqft(a.get("total_area"))
            surveys = [f"{_norm(s.get('survey_no'))}/{_norm(s.get('subdivision')) or ''}"
                       for s in (a.get("survey_numbers") or [])]
            print(f"    {a['auction_id']}  total_area={a.get('total_area')!r} → "
                  f"{parsed if parsed else 'unparseable'}  surveys={surveys or '(none)'}")

    # Same (borrower, bank) ignoring area — catches area-name drift.
    by_bb: dict[tuple[str, str], set[str]] = defaultdict(set)
    areas_per_bb: dict[tuple[str, str], set[str]] = defaultdict(set)
    for a in auctions:
        br, bk, ar = _norm(a.get("borrower")), _norm(a.get("bank")), _norm(a.get("area"))
        if br and bk:
            by_bb[(br, bk)].add(a["auction_id"])
            if ar:
                areas_per_bb[(br, bk)].add(ar)
    area_drift = [(k, v, areas_per_bb[k])
                  for k, v in by_bb.items()
                  if len(v) > 1 and len(areas_per_bb[k]) > 1]
    area_drift.sort(key=lambda x: -len(x[1]))
    print(f"\n=== (borrower, bank) pairs with >1 auction AND differing areas: {len(area_drift)} ===")
    for (br, bk), ids, areas in area_drift[:5]:
        print(f"  borrower={br!r}  bank={bk!r}  → areas seen: {sorted(areas)}")


def write_pairs(session, pairs: list[tuple[str, str, str, str]]) -> int:
    written = 0
    for i in range(0, len(pairs), NEO4J_BATCH_SIZE):
        batch = pairs[i : i + NEO4J_BATCH_SIZE]
        rows = [
            {"a_id": a, "b_id": b, "reason": reason, "confidence": conf}
            for a, b, reason, conf in batch
        ]
        session.run(MERGE_PAIR, rows=rows)
        written += len(batch)
        print(f"  Linked pairs: [{written}/{len(pairs)}]", end="\r")
    return written


# ── CLI ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, rebuild: bool = False, debug: bool = False) -> None:
    if not (NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD):
        print("[ERROR] Neo4j credentials missing (NEO4J_URI / USERNAME / PASSWORD).")
        return

    t_start = time.time()
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            print("Fetching candidate auctions from Neo4j...")
            auctions = fetch_auctions(session)
            print(f"  {len(auctions)} auctions loaded.")

            if debug:
                debug_diagnostics(auctions)
                return

            print("Matching re-auctions...")
            pairs = find_reauction_pairs(auctions)
            clusters, expanded = expand_clusters(auctions, pairs)
            print(f"  {len(pairs)} direct pairs, "
                  f"{len(clusters)} transitive clusters, "
                  f"{len(expanded)} total pairs after expansion.")

            sizes = Counter(len(g) for g in clusters)
            if sizes:
                print("  Cluster-size histogram:")
                for size in sorted(sizes):
                    print(f"    {size} auctions: {sizes[size]} clusters")

            reasons = Counter(r[2] for r in expanded)
            if reasons:
                print("  Pair reasons:")
                for reason, n in reasons.most_common():
                    print(f"    {reason}: {n}")

            if dry_run:
                print("\n[DRY RUN] No writes performed.")
                return

            if rebuild:
                print("Dropping existing :SAME_PROPERTY_AS edges...")
                session.run(DROP_EXISTING)

            print("Writing :SAME_PROPERTY_AS edges...")
            written = write_pairs(session, expanded)
            print(f"\n  Wrote {written} pair operations "
                  f"({written * 2} directed edges merged).")

    finally:
        driver.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*50}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'='*50}")
    print("\nVerify:")
    print("  MATCH ()-[r:SAME_PROPERTY_AS]->() RETURN count(r)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Link re-auctioned properties.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect clusters and print stats; no writes.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Delete existing :SAME_PROPERTY_AS edges first.")
    parser.add_argument("--debug", action="store_true",
                        help="Print data-coverage stats and near-miss groups "
                             "so we can see why matching isn't firing.")
    args = parser.parse_args()
    run(dry_run=args.dry_run, rebuild=args.rebuild, debug=args.debug)


if __name__ == "__main__":
    main()
