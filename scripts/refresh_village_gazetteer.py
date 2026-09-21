"""
scripts/refresh_village_gazetteer.py
------------------------------------
Diff an authoritative revenue-village list against the :RevenueVillage
gazetteer, and add what is missing.

WHY THIS EXISTS
---------------
The gazetteer is the reference every place resolves against, and it is
incomplete in exactly the places the corpus cares about most. Measured on the
live graph:

  * 1,649 lots carry a village the resolver could not place inside its taluk
    (`place_status = 'unmatched'`), and for 1,482 of them the name matches
    nothing anywhere in the district.
  * Real, well-known villages are simply absent — Thirumullaivoyal and
    Paruthipattu (Avadi), Selaiyur (Tambaram), Madakulam (Madurai South),
    Okkiyam Thoraipakkam — under any spelling fold this module's normalizer
    can reach. It is not a stale taluk mapping either: Sembakkam and
    Chitlapakkam sit under Tambaram correctly while Selaiyur, next door in the
    same taluk, is missing.
  * The urban taluks are the thin ones. Chennai district holds 48 villages
    across 16 taluks, 11 of which hold none; Avadi has 21 against Ponneri's
    198; Pallavaram has 6 against Maduranthagam's 192.

No loader for this data exists in the repository, so there is no way to re-run
whatever produced it, and no record of which source or vintage it came from.
This script is that missing loader: it takes a list from a named source, says
exactly what it would change, and writes with provenance so the next person
does not have to guess.

WHAT IT WILL NOT DO
-------------------
Additive only. Nothing is deleted, renamed, or re-parented — a village present
in the graph but absent from the incoming list is REPORTED, never removed,
because a short input file must not be able to quietly empty the gazetteer.

And not every absent-looking village is an addition. A name the fold does not
match may be a village the taluk already holds, spelled differently, and those
are reported as `same, respelled` rather than written. This is not tidiness:
two near-identical names in one taluk score within `FUZZY_MARGIN` of each other,
so the resolver can no longer choose between them and refuses a village it
places correctly today. Adding them would make resolution worse, not better.
On LGD's Tamil Nadu export that bucket holds 3,081 of 8,260 apparent additions,
so it is the difference between the file improving the gazetteer and degrading
it.

Taluks are matched, never created: a taluk the graph does not hold means the
input names places this pipeline has no hierarchy for, which is a finding to
look at rather than a row to invent (same rule as promote_extractions' place
writes — a miss stays visible).

SOURCE
------
The fetch is deliberately NOT built in. The Tamil Nadu sources worth pulling
(`eservices.tn.gov.in`'s district -> taluk -> village dropdowns, which is the
same revenue register this gazetteer mirrors, and LGD's bulk download at
`lgdirectory.gov.in/downloadDirectory.do`) each need a live session from a
network that can reach them, and a scraper written against an endpoint nobody
has probed is a scraper that breaks on first contact. So this takes a CSV and
does not care who produced it:

    district,taluk,village[,village_code,lgd_village_code,name_ta]

Common export headers are recognised without configuration — LGD's
"District Name" / "Sub-District Name" / "Village Name" included.

USAGE
-----
    # what would change, nothing written
    python -m scripts.refresh_village_gazetteer --from-csv tn_villages.csv

    # the thin taluks first
    python -m scripts.refresh_village_gazetteer --from-csv tn_villages.csv \
        --district Chennai --district Tiruvallur --district Chengalpattu

    # write, stamping every new node with where it came from
    python -m scripts.refresh_village_gazetteer --from-csv tn_villages.csv \
        --source "TN eServices 2026-09" --apply

Auth: NEO4J_URI/USERNAME/PASSWORD(/DATABASE), over the HTTPS Query API.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict

from pipeline.place_resolution import (Gazetteer, already_held_as,
                                      normalize_place)
from scripts.score_ink_coverage import nq

BATCH = 500

#: Header spellings seen on the exports worth feeding this. The key is what the
#: script calls the column; the values are matched case- and space-insensitively
#: so "Sub-District Name" and "subdistrict_name" both land on `taluk`.
_HEADER_ALIASES = {
    "district": ("district", "district name", "districtname", "revenue district"),
    "taluk": ("taluk", "taluka", "tehsil", "sub district", "subdistrict",
              "sub-district", "sub district name", "subdistrict name",
              "sub-district name", "block"),
    "village": ("village", "village name", "villagename", "revenue village"),
    # `village_code` is the within-taluk revenue serial the graph already holds
    # on 17,164 nodes — three digits, "008", "060", restarting in every taluk.
    # LGD's code is a different thing: a six-digit national identifier from its
    # own register, on a scheme that shares nothing with this one (LGD calls
    # Ariyalur 610 where the graph calls it 17). They are kept in separate
    # properties, because one column holding two numbering schemes is a column
    # no consumer can read — a source's code must never land in `village_code`
    # unless it IS that serial.
    "village_code": ("village code", "villagecode", "village_code",
                     "revenue village code", "village serial"),
    "lgd_village_code": ("lgd code", "village lgd code", "lgd village code",
                         "lgd_village_code"),
    "name_ta": ("name_ta", "tamil name", "village name tamil", "name (tamil)"),
}


def _header_key(raw: str) -> str | None:
    """Which of our columns this header spells, or None for one we ignore."""
    folded = " ".join(str(raw or "").strip().lower().replace("_", " ").split())
    for key, aliases in _HEADER_ALIASES.items():
        if folded == key or folded in aliases:
            return key
    return None


def read_source_csv(path: str) -> list[dict]:
    """Rows of {district, taluk, village, village_code?, lgd_village_code?,
    name_ta?}.

    A row missing any of the three required names is skipped rather than
    guessed at — half a hierarchy cannot be diffed against a hierarchy.
    """
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return out
        cols = {i: _header_key(h) for i, h in enumerate(header)}
        if not {"district", "taluk", "village"} <= set(v for v in cols.values() if v):
            raise SystemExit(
                f"{path}: need district, taluk and village columns; found "
                f"{[h for h in header]}")
        for row in reader:
            rec = {}
            for i, value in enumerate(row):
                key = cols.get(i)
                if key and str(value).strip():
                    rec[key] = str(value).strip()
            if {"district", "taluk", "village"} <= rec.keys():
                out.append(rec)
    return out


def load_graph_villages() -> dict[tuple[str, str], dict[str, str]]:
    """{(district, taluk): {normalized village key: official name}}.

    Keyed on the resolver's own fold (``normalize_place``) so the diff asks the
    question the resolver asks — "could this name be looked up here?" — rather
    than a stricter one that would report a spelling variant as missing and add
    a duplicate beside the village that is already there.

    The fold is necessary but not sufficient for that: it collapses doubled
    consonants and the Th/T axis, so it catches Morrai against Morai, and misses
    Authukurichi against Athukurichi. ``diff`` puts the names it misses through
    ``already_held_as`` before calling any of them new.
    """
    rows = nq("""
        MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk)-[:IN_DISTRICT]->(d:District)
        RETURN d.name, t.name, v.name
    """)
    out: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for district, taluk, village in rows:
        out[(district, taluk)].setdefault(normalize_place(village), village)
    return out


def load_graph_taluks() -> Gazetteer:
    """The graph's taluk hierarchy, as the resolver's own index.

    A ``Gazetteer`` rather than a folded dict, because the fold alone is too
    strict for an official export. Measured on LGD's Tamil Nadu
    village-to-gram-panchayat file (20,277 rows), a folded-exact lookup rejects
    2,277 of them — 11% — naming 38 taluks the graph does in fact hold under
    another spelling: Ulundurpettai/Ulundurpet, Virudhachalam/Vridhachalam,
    Sirkali/Sirkazhi, Orathanadu/Orathanad, Mathavaram/Madhavaram. Every one is
    the same taluk. ``Gazetteer.taluk`` is what the rest of the pipeline resolves
    notices with — aliases, then the fold, then similarity behind FUZZY_MIN and
    FUZZY_MARGIN — so using it here asks the input the same question the
    resolver will later ask of the corpus, instead of a stricter one no source
    passes. With ``resolve_taluk``'s district scoping and the eleven aliases
    that export earned, the 2,277 become 3.
    """
    rows = nq("MATCH (t:Taluk)-[:IN_DISTRICT]->(d:District) RETURN d.name, t.name")
    return Gazetteer(districts=sorted({district for district, _ in rows}),
                     taluks=[(taluk, district) for district, taluk in rows])


def resolve_taluk(gaz: Gazetteer, rec: dict) -> tuple[str, str] | None:
    """``(official district, official taluk)`` for one input row, or None.

    An official export names its district, and that is worth more here than
    anywhere else in the pipeline. ``Gazetteer.taluk`` deliberately answers on
    the taluk name alone, because a notice's district is often not worth
    trusting — which costs it two things this caller can have back:

    * A name that folds onto two taluks is refused outright (``_t_dupes``).
      Tirupathur (its own district) and Thiruppattur (Sivagangai) are the
      documented pair, and LGD lists both — 204 rows that no global alias can
      rescue, because, as ``TALUK_ALIASES`` puts it, "the district decides, and
      this table cannot see it". Here the row states the district, so look the
      taluk up *inside* it first and the ambiguity never arises.
    * A similarity hit is free to land in another district than the row claims,
      which is the failure the script's taluk rule exists to prevent — a
      property filed into the wrong district entirely. So the claimed district
      is resolved too ("The Nilgiris" and "Sivaganga" are the graph's
      "Nilgiris" and "Sivagangai") and a disagreement is reported, not written.
    """
    claimed = gaz.district(rec["district"])

    # Inside the stated district, on the resolver's own fold: unambiguous by
    # construction, so this runs before anything global.
    if claimed:
        taluk = gaz.names_a_taluk(rec["taluk"], claimed)
        if taluk:
            return claimed, taluk

    hit = gaz.taluk(rec["taluk"])
    if not hit:
        return None
    taluk, district = hit
    if claimed and normalize_place(claimed) != normalize_place(district):
        return None
    return district, taluk


def diff(source_rows: list[dict],
         graph: dict[tuple[str, str], dict[str, str]],
         taluks: Gazetteer,
         districts: set[str] | None = None,
         only_taluks: set[str] | None = None) -> dict:
    """Split the incoming rows into what is new, known, variant, and unplaceable.

    ``districts`` / ``only_taluks`` are official names, folded here, so a run
    can be pointed at the thin taluks without editing the input file.

    A name the fold does not match may still be a village this taluk already
    holds, spelled differently. Those come back under ``variants`` and are never
    written: adding one would be worse than doing nothing, for the reason the
    module docstring gives.
    """
    want_d = {normalize_place(x) for x in districts} if districts else None
    want_t = {normalize_place(x) for x in only_taluks} if only_taluks else None

    missing: list[dict] = []
    variants: list[dict] = []
    present = 0
    unknown_taluk: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str, str]] = set()

    for rec in source_rows:
        hit = resolve_taluk(taluks, rec)
        if not hit:
            unknown_taluk[f'{rec["taluk"]} [{rec["district"]}]'] += 1
            continue
        district, taluk = hit
        if want_d and normalize_place(district) not in want_d:
            continue
        if want_t and normalize_place(taluk) not in want_t:
            continue
        key = normalize_place(rec["village"])
        if not key:
            continue
        # One row per (taluk, folded name): a source that lists a village twice
        # (two hamlets, one name) must not write it twice.
        dedupe = (district, taluk, key)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        pool = graph.get((district, taluk), {})
        if key in pool:
            present += 1
            continue
        # The fold missed, so ask the resolver's own question before calling it
        # new: does this taluk already hold a village the resolver would read as
        # this one? On LGD's Tamil Nadu export 3,081 of 8,260 apparent additions
        # answer yes — Authukurichi against the held Athukurichi, Devanur
        # against Dhevanur, Periakrishnapuram against Periyakrishnapuram.
        # Writing those is worse than skipping them: the two names land in one
        # taluk scoring within FUZZY_MARGIN of each other, so the resolver stops
        # being able to choose and refuses a village it resolves correctly
        # today. The fold alone cannot see this, which is why it is not enough.
        near = already_held_as(rec["village"], pool)
        if near:
            variants.append({"district": district, "taluk": taluk,
                             "village": rec["village"],
                             "held": near[0],
                             "score": round(near[1], 1),
                             "village_code": rec.get("village_code"),
                             "lgd_village_code":
                                 rec.get("lgd_village_code")})
            continue
        missing.append({"district": district, "taluk": taluk,
                        "village": rec["village"],
                        "village_code": rec.get("village_code"),
                        "lgd_village_code": rec.get("lgd_village_code"),
                        "name_ta": rec.get("name_ta")})

    # Reported, never acted on: the input being short of the graph is normal
    # (a district-scoped export), and deleting on that basis would be a way to
    # lose the gazetteer to a truncated download.
    # A village matched as a variant IS listed by the source, under its other
    # spelling, so it is accounted for — counting it graph-only would report the
    # export as short of the graph by 3,081 villages it in fact names.
    in_source: set[tuple[str, str, str]] = {
        (m["district"], m["taluk"], normalize_place(m["village"])) for m in missing
    } | {k for k in seen} | {
        (v["district"], v["taluk"], normalize_place(v["held"])) for v in variants
    }
    only_in_graph = sum(
        1 for (district, taluk), names in graph.items()
        for key in names
        if (not want_d or normalize_place(district) in want_d)
        and (not want_t or normalize_place(taluk) in want_t)
        and (district, taluk, key) not in in_source)

    return {"missing": missing, "variants": variants, "present": present,
            "unknown_taluk": dict(unknown_taluk), "only_in_graph": only_in_graph}


_WRITE = """
UNWIND $rows AS row
// MATCH the taluk, never MERGE it: an input naming a taluk this graph does not
// hold is a finding, not a node to invent (see the module docstring).
MATCH (t:Taluk {name: row.taluk})-[:IN_DISTRICT]->(d:District {name: row.district})
MERGE (v:RevenueVillage {name: row.village,
                         taluk_code: t.taluk_code,
                         district_code: t.district_code})
ON CREATE SET v.village_code     = row.village_code,
              v.lgd_village_code = row.lgd_village_code,
              v.name_ta          = row.name_ta,
              v.source           = $source,
              v.loaded_at    = datetime()
MERGE (v)-[:IN_TALUK]->(t)
RETURN count(*) AS written
"""


def write(missing: list[dict], source: str) -> int:
    written = 0
    for i in range(0, len(missing), BATCH):
        rows = nq(_WRITE, {"rows": missing[i:i + BATCH], "source": source})
        written += rows[0][0] if rows else 0
    return written


def run(*, csv_path: str, apply: bool, source: str,
        districts: set[str] | None, only_taluks: set[str] | None,
        report_path: str | None) -> dict:
    source_rows = read_source_csv(csv_path)
    print(f"{len(source_rows)} row(s) read from {csv_path}")

    graph = load_graph_villages()
    taluks = load_graph_taluks()
    print(f"graph holds {sum(len(v) for v in graph.values())} village(s) "
          f"across {len(taluks.taluks)} taluk(s)")

    result = diff(source_rows, graph, taluks, districts, only_taluks)
    missing = result["missing"]

    by_taluk: dict[tuple[str, str], int] = defaultdict(int)
    for m in missing:
        by_taluk[(m["district"], m["taluk"])] += 1

    variants = result["variants"]

    print(f"\nalready present : {result['present']}")
    print(f"same, respelled : {len(variants)}  (a village this taluk already "
          f"holds under another spelling — reported, never added)")
    print(f"missing         : {len(missing)}  across {len(by_taluk)} taluk(s)")
    print(f"only in graph   : {result['only_in_graph']}  (reported, never removed)")
    if result["unknown_taluk"]:
        print(f"unknown taluk   : {sum(result['unknown_taluk'].values())} row(s) "
              f"naming {len(result['unknown_taluk'])} taluk(s) this graph has no "
              f"hierarchy for")
        for name, n in sorted(result["unknown_taluk"].items(),
                              key=lambda kv: -kv[1])[:10]:
            print(f"                  {name}  x{n}")

    if by_taluk:
        print("\nwould add, worst first:")
        for (district, taluk), n in sorted(by_taluk.items(), key=lambda kv: -kv[1])[:20]:
            have = len(graph.get((district, taluk), {}))
            print(f"  {n:5d}  {district} / {taluk}   (has {have})")

    if variants:
        print("\nheld under another spelling, closest first "
              "(source name ~ name already held):")
        for v in sorted(variants, key=lambda v: -v["score"])[:10]:
            print(f"  {v['score']:5.1f}  {v['village']} ~ {v['held']}"
                  f"   [{v['district']}/{v['taluk']}]")

    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump({"missing": missing,
                       "variants": variants,
                       "present": result["present"],
                       "only_in_graph": result["only_in_graph"],
                       "unknown_taluk": result["unknown_taluk"]},
                      fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {report_path}")

    summary = {"read": len(source_rows), "present": result["present"],
               "variants": len(variants),
               "missing": len(missing), "taluks": len(by_taluk),
               "only_in_graph": result["only_in_graph"],
               "unknown_taluk_rows": sum(result["unknown_taluk"].values())}

    if not apply:
        print("\n[dry-run] nothing written — re-run with --apply")
        return summary

    written = write(missing, source)
    print(f"\nadded {written} village(s), stamped source={source!r}")
    summary["written"] = written
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-csv", required=True, metavar="PATH",
                    help="district,taluk,village[,village_code,lgd_village_code,name_ta]")
    ap.add_argument("--district", action="append", default=None,
                    help="limit to this district (repeatable)")
    ap.add_argument("--taluk", action="append", default=None,
                    help="limit to this taluk (repeatable)")
    ap.add_argument("--source", default=None,
                    help="provenance stamped on every node added; required with --apply")
    ap.add_argument("--report", default=None, metavar="PATH",
                    help="write the full missing list as JSON")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    if args.apply and not args.source:
        ap.error("--apply needs --source, so the rows can be traced later")

    run(csv_path=args.from_csv, apply=args.apply,
        source=args.source or "dry-run",
        districts=set(args.district) if args.district else None,
        only_taluks=set(args.taluk) if args.taluk else None,
        report_path=args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
