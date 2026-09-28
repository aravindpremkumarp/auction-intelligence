"""
scripts/learn_sro_taluks.py
---------------------------
Learn which taluk each sub-registrar office (SRO), and each PIN code, points
to, from the lots already placed, and write ``pipeline/lookups/sro_taluks.json``
and ``pipeline/lookups/pin_taluks.json``.

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

The tables are read by ``pipeline.place_resolution.taluk_hint_place`` — for
lots (``promote_extractions.lot_place``) and listings (``resolve_places``).

Usage::

    python -m scripts.learn_sro_taluks           # dry run: counts + sample
    python -m scripts.learn_sro_taluks --apply   # write both tables
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable

from pipeline.place_resolution import PIN_TALUKS, SRO_TALUKS, sro_key

#: Placed lots an office needs before it names a taluk, and the share of them
#: that must sit in that one taluk.
MIN_LOTS = 3
MIN_SHARE = 0.9
#: Place sources that teach the tables. Not ``sro-taluk`` / ``city-taluk`` /
#: ``pin-taluk`` / ``district-sound``: those are the hints' own answers.
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


def placed_lots() -> list[dict]:
    """``{sro, pin, taluk}`` for every lot a trusted source placed."""
    from api.neo4j_client import run_read_query
    from pipeline.apply_extractions import entities_with_corrections
    from pipeline.promote_extractions import build_lots, lot_pin

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
    out = []
    for d in docs:
        _, lots = build_lots(entities_with_corrections(d["ej"], d["cj"]),
                             d["filename"])
        for rec in lots:
            taluk = taluk_of.get(rec["lot_key"])
            if taluk:
                out.append({"sro": (rec.get("location") or {}).get("registration_sub_district"),
                            "pin": lot_pin(rec), "taluk": taluk})
    return out


def _write(path, table: dict) -> None:
    path.write_text(json.dumps(table, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write both tables")
    args = ap.parse_args(argv)

    lots = placed_lots()
    sro_pairs = [(r["sro"], r["taluk"]) for r in lots if r["sro"]]
    pin_pairs = [(r["pin"], r["taluk"]) for r in lots if r["pin"]]
    sros, pins = learn(sro_pairs), learn_pins(pin_pairs)
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
    if not args.apply:
        print("[dry-run] nothing written")
        return 0
    _write(SRO_TALUKS, sros)
    _write(PIN_TALUKS, pins)
    return 0


if __name__ == "__main__":
    sys.exit(main())
