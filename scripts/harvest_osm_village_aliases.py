"""Confirm unmatched village spellings against OpenStreetMap, and keep only
the ones two independent sources agree on.

A listing is ``place_village_status = 'unmatched'`` when its notice names a
village the gazetteer does not hold under that spelling in the (resolved)
taluk. Most are the same village spelled another way — "Karmuthampatti" for
the register's "Karumathampatty" — but loose similarity cannot be trusted at
this scope: "Madipakkam" scores close to "Madurapakam" and is a different
place, in a different taluk. So similarity alone never decides here.

Instead the notice's spelling is searched on OpenStreetMap (Nominatim), and a
result counts only when OSM itself places it in the same taluk and it names a
village the register holds there:

* ``osm-tamil`` — the OSM place's Tamil name (``name:ta``) is exactly the
  register village's Tamil name (``RevenueVillage.name_ta``), and exactly one
  village in the taluk carries it. Two sources, written in the script the
  name actually belongs to, agree; transliteration drift cannot fake that.
* ``osm-english`` — no Tamil name to compare, but OSM's English name folds
  (``normalize_place``) to exactly one register village in the taluk, the
  place is a settlement (not a road or a lake), and the notice's spelling is
  itself close to that village (rapidfuzz >= 75). Weaker, and labelled so.

A third outcome is not an alias at all:

* ``osm-urban`` — OSM holds the name as an urban place (suburb, neighbourhood,
  town, city district …) in the same taluk, and the register — which carries
  the whole LGD village list — holds nothing close to it there. Pammal,
  Madipakkam, Thirumullaivoyal: real places, but localities of a town, not
  revenue villages. The listing is not placed anywhere; it is only marked
  ``not-a-revenue-village``, the status a human "skip" verdict already gives,
  so the review queue stops asking about it.

Anything else — no hit, a hit in another taluk, a hit naming no register
village, a register name shared by two villages, results pointing at
different villages, a hamlet (which belongs to some revenue village we cannot
name) — is left unmatched. Nothing is written to the graph: the output is a
lookup file, ``pipeline/lookups/village_aliases_osm.json``, which
scripts/resolve_places reads after the human decisions, and every entry
carries the OSM object it rests on so any one can be checked or pulled.

Nominatim's usage policy: at most one request a second, an identifying
User-Agent, and results cached — a re-run asks nothing it has asked before.

Run:
    NEO4J_HTTP_API=1 python -m scripts.harvest_osm_village_aliases --limit 20
    NEO4J_HTTP_API=1 python -m scripts.harvest_osm_village_aliases
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

from api.neo4j_client import run_read_query
from pipeline.place_resolution import normalize_place
from pipeline.resolution_review import village_alias_key

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "pipeline" / "lookups" / "village_aliases_osm.json"
CACHE = ROOT / "data" / "osm_village_cache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = ("auction-intelligence-gazetteer/1.0 "
              "(village name reconciliation; github.com/aravindpremkumarp)")
MIN_INTERVAL_S = 1.1
#: OSM place types that are a settlement. Roads, lakes, temples and shops that
#: carry a village's name are not evidence the village is there.
SETTLEMENT = {"village", "hamlet", "suburb", "town", "neighbourhood", "quarter",
              "isolated_dwelling", "locality", "city_district", "city",
              "municipality"}
ENGLISH_MIN = 75
#: Register villages at least this close to the notice's spelling are
#: candidates the notice might mean; two of them and OSM cannot choose.
NEAR_TWIN = 90
#: The settlement types that are part of a town rather than a revenue village.
URBAN = {"suburb", "neighbourhood", "quarter", "town", "city_district", "city",
         "municipality"}
#: A register village this close to the notice's spelling means the name may
#: yet be a village, so it is never declared urban.
URBAN_NEAR_REGISTER = 85
#: Names the register holds under a different name altogether, which neither
#: spelling nor the Tamil script can connect — so they are never declared
#: urban. Mahabalipuram is the register's Mamallapuram (Tirukalukundram).
NEVER_URBAN = {"mahabalipuram"}


def fold_ta(s: str | None) -> str:
    """A Tamil name compared as written: NFC, no spacing or joiners.

    Also without the register's own village code — 1,897 of its Tamil names
    carry one ("071  புஞ்சை புளியம்பட்டி") — and without a bracketed
    disambiguator OSM adds ("ஆத்தூர் (சேலம்)"), neither of which is the name."""
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r"^\s*\d+\s*", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    # Sandhi: Tamil doubles a hard consonant where two words join — OSM writes
    # "புஞ்சைப் புளியம்பட்டி", the register "புஞ்சை புளியம்பட்டி". The same
    # name either way, so the joining consonant is dropped before comparing.
    s = re.sub(r"([கசடதபற])்\s+(?=\1)", "", s)
    return re.sub(r"[\s​-‍﻿.-]+", "", s)


_ROMAN = re.compile(r"(?<![a-z])(i{1,3}|iv|v|vi{1,3}|ix|x)(?![a-z])")


def sub_number(name: str) -> tuple[str, ...]:
    """The numbers a name carries, arabic or roman ("Elavur II", "Vichoor-2").

    Numbered sub-villages are distinct places — the resolver's own fuzzy guard
    refuses a match whose digits differ (pipeline/place_resolution._digits) —
    so an alias may not map "Elavur II" onto plain "Elavur"."""
    low = (name or "").lower()
    return tuple(sorted(re.findall(r"\d+", low) + _ROMAN.findall(low)))


def is_tamil(s: str | None) -> bool:
    return bool(s) and any("஀" <= ch <= "௿" for ch in s)


def load_register() -> dict[str, list[dict]]:
    """``{taluk: [{name, name_ta}]}`` from the gazetteer."""
    rows = run_read_query(
        "MATCH (v:RevenueVillage)-[:IN_TALUK]->(t:Taluk) "
        "RETURN t.name AS taluk, v.name AS name, v.name_ta AS name_ta",
        {}, max_rows=100_000, timeout=180.0)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["taluk"]].append({"name": r["name"], "name_ta": r["name_ta"]})
    return out


def load_unmatched() -> list[dict]:
    """Distinct (village spelling, taluk, district) the resolver left
    unmatched, with how many listings carry each."""
    rows = run_read_query(
        "MATCH (p:AuctionProperty {place_village_status: 'unmatched'}) "
        "WHERE p.village IS NOT NULL AND p.revenue_taluk IS NOT NULL "
        "RETURN p.village AS village, p.revenue_taluk AS taluk, "
        "       p.revenue_district AS district, count(*) AS n",
        {}, max_rows=20_000, timeout=180.0)
    merged: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (normalize_place(r["village"]), r["taluk"])
        m = merged.setdefault(k, {"village": r["village"].strip(), "taluk": r["taluk"],
                                  "district": r["district"], "n": 0})
        m["n"] += r["n"]
    return sorted(merged.values(), key=lambda x: -x["n"])


class Nominatim:
    def __init__(self, cache_path: Path = CACHE):
        self.path = cache_path
        self.cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self._last = 0.0

    def search(self, q: str) -> list[dict]:
        if q in self.cache:
            return self.cache[q]
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = requests.get(NOMINATIM, headers={"User-Agent": USER_AGENT}, timeout=60,
                         params={"q": q, "format": "jsonv2", "addressdetails": 1,
                                 "namedetails": 1, "limit": 8, "countrycodes": "in"})
        r.raise_for_status()
        self.cache[q] = r.json()
        return self.cache[q]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False))


def judge(item: dict, results: list[dict], register: list[dict]) -> dict | None:
    """The one register village the OSM results confirm, or None. Pure."""
    from rapidfuzz import fuzz
    taluk = item["taluk"]
    by_ta: dict[str, list[str]] = defaultdict(list)
    by_en: dict[str, list[str]] = defaultdict(list)
    for v in register:
        if v.get("name_ta"):
            by_ta[fold_ta(v["name_ta"])].append(v["name"])
        by_en[normalize_place(v["name"])].append(v["name"])
    # A register holding two villages close to the notice's spelling ("Veerapandi"
    # and "Veerapandi." in Coimbatore North) is a near-twin OSM cannot settle:
    # OSM may simply map one of them. Only a spelling with at most one register
    # village this close is judged at all.
    key = normalize_place(item["village"])
    twins = {v["name"] for v in register
             if fuzz.ratio(key, normalize_place(v["name"])) >= NEAR_TWIN}
    if len(twins) > 1:
        return None
    found: dict[str, dict] = {}
    for x in results:
        a = x.get("address") or {}
        if (a.get("state") or "").lower() != "tamil nadu":
            continue
        if normalize_place(a.get("county") or "") != normalize_place(taluk):
            continue                                  # OSM must agree on the taluk
        if x.get("addresstype") not in SETTLEMENT:
            continue
        nd = x.get("namedetails") or {}
        ta = nd.get("name:ta") or (nd.get("name") if is_tamil(nd.get("name")) else None)
        target, rule = None, None
        if ta and len(set(by_ta.get(fold_ta(ta), []))) == 1 and len(by_ta[fold_ta(ta)]) == 1:
            target, rule = by_ta[fold_ta(ta)][0], "osm-tamil"
        else:
            for en in (nd.get("name:en"), nd.get("name")):
                if en and not is_tamil(en):
                    hits = by_en.get(normalize_place(en), [])
                    close = fuzz.ratio(normalize_place(item["village"]),
                                       normalize_place(en)) >= ENGLISH_MIN
                    if len(hits) == 1 and close:
                        target, rule = hits[0], "osm-english"
                        break
        if target and twins and target not in twins:
            target = None                 # the one close register name is another village
        if target and sub_number(item["village"]) != sub_number(target):
            target = None                 # "Elavur II" is not "Elavur"
        if target:
            prev = found.get(target)
            if not prev or (prev["rule"] == "osm-english" and rule == "osm-tamil"):
                found[target] = {"target": target, "rule": rule,
                                 "osm": f"{x.get('osm_type')}/{x.get('osm_id')}",
                                 "osm_name": nd.get("name"), "osm_name_ta": ta}
    if len(found) == 1:
        return next(iter(found.values()))
    if found:                                         # OSM points two ways
        return None
    return _urban(item, results, register)


def _urban(item: dict, results: list[dict], register: list[dict]) -> dict | None:
    """``osm-urban`` when every same-taluk settlement OSM returns for the name
    is urban and no register village in the taluk is close to it."""
    from rapidfuzz import fuzz
    taluk = item["taluk"]
    here = [x for x in results
            if (x.get("address") or {}).get("state", "").lower() == "tamil nadu"
            and normalize_place((x.get("address") or {}).get("county") or "")
            == normalize_place(taluk)
            and x.get("addresstype") in SETTLEMENT]
    if not here or any(x.get("addresstype") not in URBAN for x in here):
        return None
    key = normalize_place(item["village"])
    if key in {normalize_place(n) for n in NEVER_URBAN}:
        return None
    if any(fuzz.ratio(key, normalize_place(v["name"])) >= URBAN_NEAR_REGISTER
           for v in register):
        return None
    # Nor when OSM's Tamil name is a register village's — that is the village,
    # and the English spellings merely failed to meet.
    tas = {fold_ta((x.get("namedetails") or {}).get("name:ta")) for x in here} - {""}
    if tas & {fold_ta(v.get("name_ta")) for v in register if v.get("name_ta")}:
        return None
    x = here[0]
    nd = x.get("namedetails") or {}
    return {"target": None, "rule": "osm-urban",
            "osm": f"{x.get('osm_type')}/{x.get('osm_id')}",
            "osm_name": nd.get("name"), "osm_name_ta": nd.get("name:ta"),
            "osm_type": x.get("addresstype")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    register = load_register()
    items = load_unmatched()[: args.limit]
    nom = Nominatim()
    existing = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else {}
    out = dict(existing)
    counts = defaultdict(int)
    try:
        for i, it in enumerate(items, 1):
            key = village_alias_key(it["village"], it["taluk"])
            verdict = None
            for q in (f"{it['village']}, {it['district']}, Tamil Nadu",
                      f"{it['village']}, {it['taluk']}, Tamil Nadu"):
                verdict = judge(it, nom.search(q), register.get(it["taluk"], []))
                if verdict:
                    break
            if verdict:
                out[key] = {**verdict, "raw": it["village"], "taluk": it["taluk"],
                            "listings": it["n"]}
                counts[verdict["rule"]] += 1
            else:
                counts["unconfirmed"] += 1
            if i % 25 == 0:
                nom.save()
                print(f"  [{i}/{len(items)}] {dict(counts)}", flush=True)
    finally:
        nom.save()
        Path(args.out).write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False,
                                             indent=1) + "\n")
    listings = sum(v["listings"] for k, v in out.items() if k not in existing)
    print(f"done — {dict(counts)}; {len(out) - len(existing)} new alias(es) "
          f"covering {listings} listing(s); written to {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
