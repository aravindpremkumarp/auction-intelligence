"""Phase A — resolve scraped place names onto the canonical geography.

    python -m pipeline.resolve_places [--dry-run] [--report]

If Bolt (port 7687) is blocked in your environment — Claude Code on the web,
or any HTTP-only egress proxy — prefix with NEO4J_HTTP_API=1 to route through
Aura's HTTPS Query API instead.

Needs NO LangExtract. This runs against the scraped :City / :Area names alone
and still gives ~94% of the un-extracted backlog correct district rollups, so
it ships independently of the promotion loader.

WHAT IT DOES NOT DO: nothing is deleted or renamed. :City and :Area keep their
labels, their names and their LOCATED_IN_CITY / LOCATED_IN_AREA edges, so every
existing API query keeps working. Each node merely gains a second :PlaceAlias
label and one :ALIAS_OF edge pointing at the canonical node it names.

WHY BOTTOM-UP. Tamil Nadu split several districts in 2019 — Chengalpattu out of
Kancheepuram, Ranipet and Tirupathur out of Vellore (note their district codes
35/36/37, past the original numbering). The scraped `City` field kept the OLD
district names while `Area` holds the actual taluk, so matching City -> District
by name mis-files 240+ properties: 73 listed under "Kanchipuram" sit in
Chengalpattu Taluk, 20 under "Tiruvallur" are in Ambattur (Chennai), and so on.

So City is a SEARCH FILTER, never the stored answer:

  1. anchor   — take a coarse district from City, used only to narrow candidates
  2. taluk    — an explicit "Taluk"/"Tk" in the text outranks a village match
                (133 Area names match BOTH a village and a taluk, because taluk
                HQs share their village's name; only 26 say so in the text)
  3. village  — matched WITHIN the anchor district. Village is the finest level
                and therefore the least unique: 1,150 of 15,122 village names
                are duplicated across 3,192 villages, one appearing 22 times.
                Never match a village name globally.
  4. taluk    — coarser fallback, unscoped
  5. city     — district-level last resort, only where the area resolved nothing

The stored district is always DERIVED upward from whatever resolved, never read
off the City field. Where a scoped match still returns more than one candidate
the alias is marked `ambiguous = true` rather than guessed — a visible backlog
instead of a silent wrong answer.
"""
from __future__ import annotations

import argparse
import re
import unicodedata

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import get_logger

log = get_logger(__name__)

# Suffixes that name the administrative level rather than the place. Stripped
# for matching but kept in `name_raw`, because "Chengalpattu Taluk" vs
# "Chengalpattu" is exactly the signal step 2 relies on.
_LEVEL_SUFFIX_RE = re.compile(
    r"\s*\b(taluk|taluka|tk|dist(?:rict)?|village|vill|post|po)\b\.?\s*$",
    re.IGNORECASE,
)
_TALUK_MARKER_RE = re.compile(r"\b(taluk|taluka|tk)\b", re.IGNORECASE)


def norm_place(name: str | None) -> str | None:
    """Canonical form for matching: unicode-folded, depunctuated, lowercased.

    Collapses the noise seen in both sources — a trailing period
    ("Kundrathur."), doubled spaces, and the level suffix ("Sriperumbudur
    Taluk") — so that the 3-4 spellings of one place converge on one key.
    """
    if not name:
        return None
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    prev = None
    while prev != s:            # "X Village Taluk" -> "X"
        prev = s
        s = _LEVEL_SUFFIX_RE.sub("", s).strip()
    return s.lower() or None


def says_taluk(name: str | None) -> bool:
    """True when the raw text explicitly names the taluk level."""
    return bool(name) and bool(_TALUK_MARKER_RE.search(str(name)))


# ── step 0: label + normalize ────────────────────────────────────────────────

_LABEL_CYPHER = """
MATCH (n)
WHERE (n:City OR n:Area) AND n.name IS NOT NULL
RETURN elementId(n) AS eid, labels(n) AS labels, n.name AS name
"""

_SET_ALIAS = """
UNWIND $rows AS row
MATCH (n) WHERE elementId(n) = row.eid
SET n:PlaceAlias,
    n.name_norm = row.name_norm,
    n.name_raw  = coalesce(n.name_raw, n.name),
    n.source    = coalesce(n.source, 'scraped')
"""


def label_aliases(dry_run: bool) -> int:
    rows = run_read_query(_LABEL_CYPHER, max_rows=20_000)
    payload = []
    for r in rows:
        nn = norm_place(r["name"])
        if nn:
            payload.append({"eid": r["eid"], "name_norm": nn})
    if payload and not dry_run:
        run_query(_SET_ALIAS, {"rows": payload})
    log.info("labelled %d City/Area nodes as :PlaceAlias", len(payload))
    return len(payload)


# ── step 1: anchor district from City (a filter, never the answer) ───────────

_ANCHOR = """
MATCH (p:AuctionProperty)-[:LOCATED_IN_CITY]->(c:City)
WITH p, c
MATCH (d:District) WHERE d.name IS NOT NULL AND toLower(d.name) = toLower(c.name)
SET p._anchor_district_code = d.district_code
RETURN count(*) AS n
"""


def anchor_districts(dry_run: bool) -> int:
    if dry_run:
        return 0
    rows = run_query(_ANCHOR)
    n = rows[0]["n"] if rows else 0
    log.info("anchored %d properties to a candidate district", n)
    return n


# ── steps 2-5: resolve, finest first, scoped ─────────────────────────────────

# Explicit taluk in the text wins outright — it is the notice telling us the
# level, which beats any inference from name shape.
_RESOLVE_EXPLICIT_TALUK = """
MATCH (a:Area:PlaceAlias)
WHERE a.name_norm IS NOT NULL AND a.says_taluk = true
  AND NOT (a)-[:ALIAS_OF]->()
MATCH (t:Taluk) WHERE toLower(trim(t.name)) = a.name_norm
WITH a, collect(DISTINCT t) AS hits
WHERE size(hits) = 1
UNWIND hits AS t1
MERGE (a)-[r:ALIAS_OF]->(t1)
SET r.method = 'exact', r.level = 'taluk', r.resolved_at = datetime()
RETURN count(*) AS n
"""

# Village scoped to the anchor district. Without the scope this is wrong for
# the 19% of village names that are duplicated nationally.
_RESOLVE_VILLAGE_SCOPED = """
MATCH (p:AuctionProperty)-[:LOCATED_IN_AREA]->(a:Area:PlaceAlias)
WHERE a.name_norm IS NOT NULL AND NOT (a)-[:ALIAS_OF]->()
  AND p._anchor_district_code IS NOT NULL
MATCH (v:RevenueVillage)
WHERE toLower(trim(v.name)) = a.name_norm
  AND v.district_code = p._anchor_district_code
WITH a, collect(DISTINCT v) AS hits
FOREACH (v1 IN CASE WHEN size(hits) = 1 THEN hits ELSE [] END |
  MERGE (a)-[r:ALIAS_OF]->(v1)
  SET r.method = 'exact', r.level = 'village', r.resolved_at = datetime())
FOREACH (_ IN CASE WHEN size(hits) > 1 THEN [1] ELSE [] END |
  SET a.ambiguous = true, a.ambiguous_count = size(hits))
RETURN sum(CASE WHEN size(hits) = 1 THEN 1 ELSE 0 END) AS n
"""

_RESOLVE_TALUK = """
MATCH (a:Area:PlaceAlias)
WHERE a.name_norm IS NOT NULL AND NOT (a)-[:ALIAS_OF]->()
MATCH (t:Taluk) WHERE toLower(trim(t.name)) = a.name_norm
WITH a, collect(DISTINCT t) AS hits
WHERE size(hits) = 1
UNWIND hits AS t1
MERGE (a)-[r:ALIAS_OF]->(t1)
SET r.method = 'exact', r.level = 'taluk', r.resolved_at = datetime()
RETURN count(*) AS n
"""

_RESOLVE_AREA_DISTRICT = """
MATCH (a:Area:PlaceAlias)
WHERE a.name_norm IS NOT NULL AND NOT (a)-[:ALIAS_OF]->()
MATCH (d:District) WHERE toLower(trim(d.name)) = a.name_norm
WITH a, collect(DISTINCT d) AS hits
WHERE size(hits) = 1
UNWIND hits AS d1
MERGE (a)-[r:ALIAS_OF]->(d1)
SET r.method = 'exact', r.level = 'district', r.resolved_at = datetime()
RETURN count(*) AS n
"""

# City is the last resort and resolves only for properties whose Area gave
# nothing — otherwise the pre-2019 district names would override a correct,
# finer answer.
_RESOLVE_CITY = """
MATCH (p:AuctionProperty)-[:LOCATED_IN_CITY]->(c:City:PlaceAlias)
WHERE c.name_norm IS NOT NULL AND NOT (c)-[:ALIAS_OF]->()
  AND NOT (p)-[:LOCATED_IN_AREA]->(:PlaceAlias)-[:ALIAS_OF]->()
MATCH (t) WHERE (t:District OR t:Taluk) AND toLower(trim(t.name)) = c.name_norm
WITH c, collect(DISTINCT t) AS hits
WHERE size(hits) = 1
UNWIND hits AS t1
MERGE (c)-[r:ALIAS_OF]->(t1)
SET r.method = 'exact',
    r.level  = CASE WHEN t1:District THEN 'district' ELSE 'taluk' END,
    r.resolved_at = datetime()
RETURN count(*) AS n
"""

_MARK_SAYS_TALUK = """
UNWIND $rows AS row
MATCH (n) WHERE elementId(n) = row.eid
SET n.says_taluk = row.says_taluk
"""

_STEPS: tuple[tuple[str, str], ...] = (
    ("explicit taluk", _RESOLVE_EXPLICIT_TALUK),
    ("village (district-scoped)", _RESOLVE_VILLAGE_SCOPED),
    ("taluk", _RESOLVE_TALUK),
    ("area -> district", _RESOLVE_AREA_DISTRICT),
    ("city (last resort)", _RESOLVE_CITY),
)


def mark_says_taluk(dry_run: bool) -> None:
    rows = run_read_query(
        "MATCH (a:Area) WHERE a.name IS NOT NULL "
        "RETURN elementId(a) AS eid, a.name AS name", max_rows=20_000)
    payload = [{"eid": r["eid"], "says_taluk": says_taluk(r["name"])}
               for r in rows]
    if payload and not dry_run:
        run_query(_MARK_SAYS_TALUK, {"rows": payload})


def resolve(dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, cypher in _STEPS:
        if dry_run:
            counts[name] = 0
            print(f"  [dry-run] {name}")
            continue
        rows = run_query(cypher)
        n = (rows[0].get("n") if rows else 0) or 0
        counts[name] = n
        print(f"  {name}: {n} resolved")
    return counts


# ── report ───────────────────────────────────────────────────────────────────

_REPORT = """
MATCH (a:PlaceAlias)
RETURN count(a) AS aliases,
       sum(CASE WHEN exists((a)-[:ALIAS_OF]->()) THEN 1 ELSE 0 END) AS resolved,
       sum(CASE WHEN a.ambiguous THEN 1 ELSE 0 END) AS ambiguous
"""

_COVERAGE = """
MATCH (p:AuctionProperty)
OPTIONAL MATCH (p)-[:LOCATED_IN_AREA]->(:PlaceAlias)-[:ALIAS_OF]->(viaArea)
OPTIONAL MATCH (p)-[:LOCATED_IN_CITY]->(:PlaceAlias)-[:ALIAS_OF]->(viaCity)
WITH p, viaArea, viaCity
RETURN count(p) AS properties,
       sum(CASE WHEN viaArea IS NOT NULL THEN 1 ELSE 0 END) AS via_area,
       sum(CASE WHEN viaCity IS NOT NULL THEN 1 ELSE 0 END) AS via_city,
       sum(CASE WHEN viaArea IS NOT NULL OR viaCity IS NOT NULL
                THEN 1 ELSE 0 END) AS resolved_either,
       sum(CASE WHEN viaArea IS NULL AND viaCity IS NULL
                THEN 1 ELSE 0 END) AS unresolved
"""


def report() -> None:
    rows = run_read_query(_REPORT)
    if rows:
        r = rows[0]
        print(f"\naliases: {r['aliases']} · resolved: {r['resolved']} · "
              f"ambiguous: {r['ambiguous']}")
    rows = run_read_query(_COVERAGE)
    if rows:
        r = rows[0]
        total = r["properties"] or 1
        print(f"properties: {r['properties']}")
        print(f"  via area          {r['via_area']:>5}")
        print(f"  via city          {r['via_city']:>5}")
        print(f"  resolved (either) {r['resolved_either']:>5} "
              f"({100 * r['resolved_either'] // total}%)")
        print(f"  unresolved        {r['unresolved']:>5}")


def run(dry_run: bool = False, want_report: bool = False) -> int:
    print("phase A — place resolution")
    label_aliases(dry_run)
    mark_says_taluk(dry_run)
    anchor_districts(dry_run)
    resolve(dry_run)
    if want_report and not dry_run:
        report()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print coverage after resolving")
    args = ap.parse_args()
    return run(dry_run=args.dry_run, want_report=args.report)


if __name__ == "__main__":
    raise SystemExit(main())
