"""
scripts/link_register_copies.py
-------------------------------
Link each register row the LGD load added as a second copy of a village the
original register already held: ``(copy)-[:COPY_OF]->(original)``.

The village register was loaded twice. The original (17,119 rows) carries a
Tamil name and a village code for every village; the LGD village-to-gram-
panchayat load (2026-09-21) added 5,179 rows the first did not hold under
that spelling. Most are real villages the first lacked. About half are the
same village spelled another way — "Arasur" for Arasoor, "Sevur" for Cheyur
(சேவூர்) — and a second node for one place costs three ways: the matcher
sees near-twins and refuses on the tie, lots of one village split across
two nodes (so parcels cannot join them), and the queue offers one place twice.

Deleting the copies is wrong: a spelling that exists only in the copy is how
65 listings are placed today. So the copy stays, linked, and the gazetteer
reads its spelling as the original (pipeline/place_resolution.Gazetteer
``village_copies``): a notice spelled like the copy lands on the original.

A copy is only linked on the original's Tamil name, which is independent of
both English spellings, and only when all of this holds, in the same taluk:

* the copy's sound key (``sound_key``) matches the original's Tamil name read
  into Latin — at least :data:`TAMIL_MIN`, or :data:`TAMIL_WITH_ENGLISH` when
  the two English spellings also share one sound key;
* it leads every other original of the taluk (by English and Tamil key) by
  :data:`MARGIN`;
* both carry the same part (I/II, A/B, 1/2), numbers, leading initials
  ("V.", "P.V.") and qualifier (a forest "R.F.", "(Ct)", "(North)", "(Then)");
* the original's name is held once in the taluk.

Usage::

    python -m scripts.link_register_copies            # dry run: counts + sample
    python -m scripts.link_register_copies --apply
    python -m scripts.link_register_copies --unlink   # remove every COPY_OF
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict

from pipeline.place_resolution import (
    _sub_numbers, normalize_place, sound_key, tamil_latin, village_part,
)

#: Tamil-name sound match needed on its own, and when the English spellings
#: already share a sound key (then the Tamil name only has to agree).
TAMIL_MIN = 95.0
TAMIL_WITH_ENGLISH = 90.0
#: Lead over the next-best original of the taluk, by any key.
MARGIN = 8.0

_INITIALS = re.compile(r"^\s*((?:[A-Za-z]{1,3}\s*\.\s*)+)")
_QUALIFIER = re.compile(
    r"\((?!\s*\d{3}\s*\))[^)]*\)|\br\.?\s*f\b\.?|\b(?:north|south|east|west|then|vada|ct)\b",
    re.I)


def _initials(name: str) -> str:
    m = _INITIALS.match(name or "")
    return re.sub(r"[^a-z]", "", m.group(1).lower()) if m else ""


def _qualifiers(name: str) -> frozenset[str]:
    return frozenset(re.sub(r"[^a-z]", "", q.lower()) for q in _QUALIFIER.findall(name or ""))


def _shape(name: str) -> tuple:
    part = village_part(name)
    return (part[1] if part else None, _sub_numbers(name), _initials(name), _qualifiers(name))


def find_copies(rows: list[dict]) -> list[dict]:
    """``rows``: ``{name, taluk, source, name_ta}`` for every register village
    (``source`` set on an LGD-added row). Returns one ``{copy, original,
    taluk, rule, score, margin}`` per LGD row that copies an original."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []
    originals: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if not r.get("source"):
            originals[r["taluk"]].append(r)
    held = Counter((r["taluk"], r["name"]) for rs in originals.values() for r in rs)
    keys = {}
    for taluk, rs in originals.items():
        for o in rs:
            keys[(taluk, o["name"])] = (
                sound_key(o["name"]),
                sound_key(tamil_latin(o["name_ta"])) if o.get("name_ta") else "")

    out = []
    for r in rows:
        if not r.get("source"):
            continue
        key, shape = sound_key(r["name"]), _shape(r["name"])
        if not key:
            continue
        scored = []
        for o in originals.get(r["taluk"], []):
            en, ta = keys[(r["taluk"], o["name"])]
            scored.append((fuzz.ratio(key, ta) if ta else 0.0,
                           max(fuzz.ratio(key, en), fuzz.ratio(key, ta) if ta else 0.0),
                           en == key, o))
        if not scored:
            continue
        scored.sort(key=lambda t: -t[0])
        ta_score, _any, same_en, o = scored[0]
        need = TAMIL_WITH_ENGLISH if same_en else TAMIL_MIN
        runner = max((t[1] for t in scored[1:]), default=0.0)
        if (ta_score < need or ta_score - runner < MARGIN
                or _shape(o["name"]) != shape
                or held[(r["taluk"], o["name"])] != 1
                or keys[(r["taluk"], o["name"])][1][:1] != key[:1]):
            continue
        out.append({"copy": r["name"], "original": o["name"], "taluk": r["taluk"],
                    "rule": "tamil+english" if same_en else "tamil",
                    "score": round(float(ta_score), 1),
                    "margin": round(float(ta_score - runner), 1)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unlink", action="store_true")
    args = ap.parse_args(argv)
    from scripts.score_ink_coverage import nq

    if args.unlink:
        nq("MATCH (:RevenueVillage)-[r:COPY_OF]->(:RevenueVillage) DELETE r")
        nq("MATCH (v:RevenueVillage) WHERE v.copy_of IS NOT NULL REMOVE v.copy_of")
        print("every COPY_OF link removed")
        return 0
    rows = [{"name": n, "taluk": t, "source": s, "name_ta": ta} for n, t, s, ta in nq(
        "MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk) "
        "RETURN v.name, t.name, v.source, v.name_ta")]
    copies = find_copies(rows)
    added = sum(1 for r in rows if r["source"])
    print(f"{len(rows)} register rows, {added} added by the LGD load; "
          f"{len(copies)} are copies of an original "
          f"({Counter(c['rule'] for c in copies)})")
    for c in copies[:15]:
        print(f"  {c['copy']!r} -> {c['original']!r} in {c['taluk']} "
              f"({c['rule']}, {c['score']}, +{c['margin']})")
    if not args.apply:
        print("[dry-run] nothing written")
        return 0
    for i in range(0, len(copies), 500):
        nq("""
            UNWIND $rows AS row
            MATCH (c:RevenueVillage {name: row.copy})-[:IN_TALUK]->(t:Taluk {name: row.taluk})
            WHERE c.source IS NOT NULL
            MATCH (o:RevenueVillage {name: row.original})-[:IN_TALUK]->(t)
            WHERE o.source IS NULL
            MERGE (c)-[r:COPY_OF]->(o)
            SET r.rule = row.rule, r.score = row.score, r.margin = row.margin,
                r.linked_at = datetime(), c.copy_of = o.name
        """, {"rows": copies[i:i + 500]})
    print(f"linked {len(copies)} copies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
