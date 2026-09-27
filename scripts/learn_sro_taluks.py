"""
scripts/learn_sro_taluks.py
---------------------------
Learn which taluk each sub-registrar office (SRO) points to, from the lots
already placed, and write ``pipeline/lookups/sro_taluks.json``.

74% of lots quote the SRO the land registers at ("Chengalpet Joint-II SRO"),
and many of those give no usable taluk. SRO and taluk boundaries are not the
same, so the table is learned, never assumed: an office names a taluk only
when at least :data:`MIN_LOTS` placed lots quote it and at least
:data:`MIN_SHARE` of them sit in one taluk. The rest point nowhere.

Only lots placed by the notice's own place fields or a human teach it
(:data:`LEARN_FROM`). A lot the table itself placed never does, so re-running
this after a resolution pass cannot feed on its own answers.

The table is read by ``pipeline.place_resolution.taluk_hint_place`` — for
lots (``promote_extractions.lot_place``) and listings (``resolve_places``).

Usage::

    python -m scripts.learn_sro_taluks           # dry run: counts + sample
    python -m scripts.learn_sro_taluks --apply   # write the table
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable

from pipeline.place_resolution import SRO_TALUKS, sro_key

#: Placed lots an office needs before it names a taluk, and the share of them
#: that must sit in that one taluk.
MIN_LOTS = 3
MIN_SHARE = 0.9
#: Place sources that teach the table. Not ``sro-taluk`` / ``city-taluk``:
#: those are this table's own answers.
LEARN_FROM = frozenset({"taluk", "district", "state", "tamil-sound",
                        "human-alias", "osm-tamil", "osm-english"})
#: Spellings kept per office, so a reader can see what folded together.
SPELLINGS_KEPT = 5


def learn(pairs: Iterable[tuple[str, str]]) -> dict[str, dict]:
    """``pairs``: ``(sro, taluk)`` per placed lot. Returns ``{sro_key:
    {taluk, lots, share, spellings}}`` for every office that names a taluk."""
    taluks: dict[str, Counter] = defaultdict(Counter)
    spellings: dict[str, Counter] = defaultdict(Counter)
    for sro, taluk in pairs:
        key = sro_key(sro)
        if key and taluk:
            taluks[key][taluk] += 1
            spellings[key][" ".join(str(sro).split())] += 1
    table = {}
    for key, counts in sorted(taluks.items()):
        lots = sum(counts.values())
        taluk, top = counts.most_common(1)[0]
        if lots >= MIN_LOTS and top / lots >= MIN_SHARE:
            table[key] = {"taluk": taluk, "lots": lots,
                          "share": round(top / lots, 3),
                          "spellings": [s for s, _ in
                                        spellings[key].most_common(SPELLINGS_KEPT)]}
    return table


def placed_pairs() -> list[tuple[str, str]]:
    """``(sro, taluk)`` for every lot a trusted source placed."""
    from api.neo4j_client import run_read_query
    from pipeline.apply_extractions import entities_with_corrections
    from pipeline.promote_extractions import build_lots

    taluk_of = {r["k"]: r["t"] for r in run_read_query(
        "MATCH (l:Lot) WHERE l.place_status = 'resolved' "
        "  AND l.place_source IN $sources AND l.taluk IS NOT NULL "
        "RETURN l.lot_key AS k, l.taluk AS t",
        {"sources": sorted(LEARN_FROM)}, max_rows=100_000, timeout=120.0)}
    docs = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
        "  AND d.stitched_into IS NULL "
        "RETURN d.filename AS filename, d.extraction_json AS ej, "
        "       d.extraction_corrections_json AS cj",
        max_rows=100_000, timeout=300.0)
    pairs = []
    for d in docs:
        _, lots = build_lots(entities_with_corrections(d["ej"], d["cj"]),
                             d["filename"])
        for rec in lots:
            sro = (rec.get("location") or {}).get("registration_sub_district")
            taluk = taluk_of.get(rec["lot_key"])
            if sro and taluk:
                pairs.append((sro, taluk))
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the table")
    args = ap.parse_args(argv)

    pairs = placed_pairs()
    table = learn(pairs)
    offices = len({sro_key(s) for s, _ in pairs} - {""})
    covered = sum(row["lots"] for row in table.values())
    print(f"{len(pairs)} placed lot(s) quote an SRO; {offices} office(s); "
          f"{len(table)} name one taluk ({covered} of those lots)")
    for key, row in list(table.items())[:15]:
        print(f"  {row['spellings'][0]!r} -> {row['taluk']} "
              f"({row['lots']} lots, {row['share']:.0%})")
    if not args.apply:
        print("[dry-run] nothing written")
        return 0
    SRO_TALUKS.write_text(json.dumps(table, ensure_ascii=False, indent=1,
                                     sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SRO_TALUKS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
