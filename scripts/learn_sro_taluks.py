"""
scripts/learn_sro_taluks.py
---------------------------
Learn which taluk each sub-registrar office (SRO), and each PIN code, points
to, and which taluks trade villages, from the lots already placed, and write
``pipeline/lookups/sro_taluks.json``, ``pipeline/lookups/pin_taluks.json`` and
``pipeline/lookups/taluk_neighbours.json``.

74% of lots quote the SRO the land registers at ("Chengalpet Joint-II SRO"),
and many write the property's PIN code, while many of those give no usable
taluk. Neither boundary is a taluk boundary, so each table is learned, never
assumed: an office or a PIN names a taluk only when at least :data:`MIN_LOTS`
placed lots carry it and at least :data:`MIN_SHARE` of them sit in one taluk.
The rest point nowhere. A lot's PIN is the one its own property text gives
(``promote_extractions.lot_pin``); a lot giving two gives none.

Only lots placed by the notice's own place fields or a human teach it
(:data:`LEARN_FROM`). A lot a table itself placed never does, so re-running
this after a resolution pass cannot feed on its own answers.

Two taluks are neighbours when at least :data:`MIN_LOTS` placed lots name one
and sit in the other, inside one district: the 2019–2021 splits (Kundrathur
out of Sriperumbudur) and notices still using the old taluk.

The SRO and PIN tables are read by
``pipeline.place_resolution.taluk_hint_place``, the neighbours by
``neighbour_taluk_place`` — for lots (``promote_extractions.lot_place``) and
listings (``resolve_places``).

Usage::

    python -m scripts.learn_sro_taluks           # dry run: counts + sample
    python -m scripts.learn_sro_taluks --apply   # write every table
    python -m scripts.learn_sro_taluks --apply --only neighbours
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable

from pipeline.place_resolution import PIN_TALUKS, SRO_TALUKS, TALUK_NEIGHBOURS, sro_key

#: Placed lots an office needs before it names a taluk, and the share of them
#: that must sit in that one taluk.
MIN_LOTS = 3
MIN_SHARE = 0.9
#: Place sources that teach the tables. Not ``sro-taluk`` / ``city-taluk`` /
#: ``pin-taluk`` / ``district-sound`` / ``district-fuzzy`` / ``neighbour-taluk``: those are the
#: tables' own answers.
LEARN_FROM = frozenset({"taluk", "district", "state", "tamil-sound",
                        "human-alias", "osm-tamil", "osm-english"})
#: Spellings kept per office, so a reader can see what folded together.
SPELLINGS_KEPT = 5


def learn(pairs: Iterable[tuple[str, str]], key=sro_key,
          keep_spellings: bool = True) -> dict[str, dict]:
    """``pairs``: ``(hint, taluk)`` per placed lot — an SRO by default, keyed
    by :func:`sro_key`. Returns ``{key: {taluk, lots, share[, spellings]}}``
    for every hint that names one taluk."""
    taluks: dict[str, Counter] = defaultdict(Counter)
    spellings: dict[str, Counter] = defaultdict(Counter)
    for hint, taluk in pairs:
        k = key(hint) if hint else ""
        if k and taluk:
            taluks[k][taluk] += 1
            spellings[k][" ".join(str(hint).split())] += 1
    table = {}
    for k, counts in sorted(taluks.items()):
        lots = sum(counts.values())
        taluk, top = counts.most_common(1)[0]
        if lots >= MIN_LOTS and top / lots >= MIN_SHARE:
            table[k] = {"taluk": taluk, "lots": lots, "share": round(top / lots, 3)}
            if keep_spellings:
                table[k]["spellings"] = [s for s, _ in
                                         spellings[k].most_common(SPELLINGS_KEPT)]
    return table


def learn_pins(pairs: Iterable[tuple[str, str]]) -> dict[str, dict]:
    """``pairs``: ``(pin, taluk)`` per placed lot; see :func:`learn`."""
    return learn(pairs, key=lambda pin: str(pin).strip(), keep_spellings=False)


def learn_neighbours(pairs: Iterable[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """``pairs``: ``(taluk the notice names, taluk the lot sits in)`` per placed
    lot, both official and of one district. Returns ``{taluk: {neighbour:
    lots}}``, both ways round, for every pair at least :data:`MIN_LOTS` lots
    carry."""
    counts = Counter((named, placed) for named, placed in pairs
                     if named and placed and named != placed)
    table: dict[str, dict[str, int]] = defaultdict(dict)
    for (named, placed), lots in sorted(counts.items()):
        if lots >= MIN_LOTS:
            for a, b in ((named, placed), (placed, named)):
                table[a][b] = table[a].get(b, 0) + lots
    return {t: dict(sorted(row.items())) for t, row in sorted(table.items())}


def placed_lots() -> list[dict]:
    """``{sro, pin, taluk, named_taluk}`` for every lot a trusted source
    placed; ``named_taluk`` is the taluk its notice names, when that resolves
    inside the lot's own district."""
    from api.neo4j_client import run_read_query
    from pipeline.apply_extractions import entities_with_corrections
    from pipeline.promote_extractions import build_lots, gazetteer, lot_pin

    gaz = gazetteer()
    placed = {r["k"]: (r["t"], r["d"]) for r in run_read_query(
        "MATCH (l:Lot) WHERE l.place_status = 'resolved' "
        "  AND l.place_source IN $sources AND l.taluk IS NOT NULL "
        "RETURN l.lot_key AS k, l.taluk AS t, l.district AS d",
        {"sources": sorted(LEARN_FROM)}, max_rows=100_000, timeout=120.0)}
    docs = run_read_query(
        "MATCH (d:Document) WHERE d.extraction_json IS NOT NULL "
        "  AND d.stitched_into IS NULL "
        "RETURN d.filename AS filename, d.extraction_json AS ej, "
        "       d.extraction_corrections_json AS cj",
        max_rows=100_000, timeout=300.0)
    out = []
    for d in docs:
        _, lots = build_lots(entities_with_corrections(d["ej"], d["cj"]),
                             d["filename"])
        for rec in lots:
            if rec["lot_key"] not in placed:
                continue
            taluk, district = placed[rec["lot_key"]]
            loc = rec.get("location") or {}
            named = gaz.taluk(loc["taluk"]) if loc.get("taluk") else None
            out.append({"sro": loc.get("registration_sub_district"),
                        "pin": lot_pin(rec), "taluk": taluk,
                        "named_taluk": named[0] if named and named[1] == district else None})
    return out


def _write(path, table: dict) -> None:
    path.write_text(json.dumps(table, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the tables")
    ap.add_argument("--only", action="append", choices=("sro", "pin", "neighbours"),
                    help="write only this table (repeatable); default every table")
    args = ap.parse_args(argv)

    lots = placed_lots()
    sro_pairs = [(r["sro"], r["taluk"]) for r in lots if r["sro"]]
    pin_pairs = [(r["pin"], r["taluk"]) for r in lots if r["pin"]]
    sros, pins = learn(sro_pairs), learn_pins(pin_pairs)
    neighbours = learn_neighbours((r["named_taluk"], r["taluk"]) for r in lots
                                  if r["named_taluk"])
    offices = len({sro_key(s) for s, _ in sro_pairs} - {""})
    print(f"{len(sro_pairs)} placed lot(s) quote an SRO; {offices} office(s); "
          f"{len(sros)} name one taluk ({sum(r['lots'] for r in sros.values())} "
          f"of those lots)")
    print(f"{len(pin_pairs)} placed lot(s) give one PIN; "
          f"{len({p for p, _ in pin_pairs})} PIN(s); {len(pins)} name one taluk")
    for row in list(sros.values())[:10]:
        print(f"  {row['spellings'][0]!r} -> {row['taluk']} "
              f"({row['lots']} lots, {row['share']:.0%})")
    for pin, row in list(pins.items())[:5]:
        print(f"  PIN {pin} -> {row['taluk']} ({row['lots']} lots, {row['share']:.0%})")
    print(f"{sum(len(row) for row in neighbours.values()) // 2} pair(s) of "
          f"neighbouring taluks over {len(neighbours)} taluk(s)")
    for taluk, row in list(neighbours.items())[:5]:
        print(f"  {taluk} <-> {', '.join(f'{t} ({n})' for t, n in row.items())}")
    if not args.apply:
        print("[dry-run] nothing written")
        return 0
    only = set(args.only or ("sro", "pin", "neighbours"))
    for name, path, table in (("sro", SRO_TALUKS, sros), ("pin", PIN_TALUKS, pins),
                              ("neighbours", TALUK_NEIGHBOURS, neighbours)):
        if name in only:
            _write(path, table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
