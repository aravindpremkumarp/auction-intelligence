"""Apply grounded extractions (Document.extraction_json) to :AuctionProperty.

This is the bridge that makes the grounded LangExtract path (populated by
pipeline/load_extractions.py, reviewed via /review/extraction) the source of
the user-facing property fields — replacing the flat per-file blob path for
every Document that has an extraction.

Per Document:
  1. Parse extraction_json ([{id, cls, text, start, end, attrs}]) and overlay
     reviewer corrections (extraction_corrections_json {field_id: {value,..}} —
     a correction replaces the entity's text).
  2. Group entities by lot_index into per-lot records: description
     (full_description spans), location attrs, boundaries (adjacency +
     measurement per side), extent/UDS, door numbers, reserve price.
  3. Match lots to the Document's linked AuctionProperty listings:
       - 1 lot        -> every listing (the 'single' case)
       - N lots       -> reserve price exact, then ±1%, then, if exactly one
                         lot and one listing remain, pair them
     Unmatched listings are logged to data/grounded_unmatched.csv.
  4. Write fields onto AuctionProperty. The description write respects the
     same human guard as apply_descriptions (never clobber description_source=
     'human' or description_verified=true). Enrichment fields use the same
     property names as pipeline/load_enriched.flatten_enrichment so the API
     and UI keep working unchanged; only non-null values are written (SET +=).

Run:  python -m pipeline.apply_extractions [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from api.neo4j_client import run_query, run_read_query
from pipeline.obs import get_logger

log = get_logger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UNMATCHED_CSV = REPO_ROOT / "data" / "grounded_unmatched.csv"

PRICE_TOLERANCE_PCT = 1.0
WRITE_CHUNK = 200

_SIDES = ("north", "south", "east", "west")


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── pure: entity parsing ─────────────────────────────────────────────────────

def parse_money(v) -> int | None:
    """Coerce an attr value to integer rupees. The prompt already asks for
    integer rupees; this only absorbs strings with grouping ('12,50,000',
    'Rs. 950000/-'). No unit arithmetic here — a bare number is trusted as
    rupees, anything unparseable is None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0)))
    except ValueError:
        return None


def entities_with_corrections(extraction_json: str,
                              corrections_json: str | None) -> list[dict]:
    """Parse the stored entity list; a reviewer correction replaces text."""
    try:
        ents = json.loads(extraction_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(ents, list):
        return []
    try:
        corr = json.loads(corrections_json or "{}")
    except json.JSONDecodeError:
        corr = {}
    out = []
    for i, e in enumerate(ents):
        if not isinstance(e, dict):
            continue
        e = dict(e)
        fid = e.get("id") or str(i)
        c = corr.get(fid) if isinstance(corr, dict) else None
        if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"].strip():
            e["text"] = c["value"].strip()
            e["corrected"] = True
        out.append(e)
    return out


def _first(d: dict, cur):
    return cur if cur not in (None, "") else d


def group_lots(entities: list[dict]) -> dict[str, dict]:
    """Group grounded entities into per-lot records.

    Returns {lot_index: {description, fields{...flat props...}, reserve}}.
    First-non-null wins within a lot (mirrors the merge policy elsewhere in
    the pipeline); descriptions concatenate in entity order.
    """
    lots: dict[str, dict] = {}

    def lot(li: str) -> dict:
        return lots.setdefault(li, {
            "description_parts": [],
            "fields": {},
            "reserve": None,
            "doors_old": [],
            "doors_new": [],
        })

    for e in entities:
        cls = e.get("cls")
        attrs = e.get("attrs") or {}
        li = str(attrs.get("lot_index") or "1")
        text = (e.get("text") or "").strip()
        rec = lot(li)
        f = rec["fields"]

        if cls == "full_description":
            if text:
                rec["description_parts"].append(text)

        elif cls == "location":
            for k in ("village", "taluk", "district",
                      "registration_district", "registration_sub_district"):
                v = attrs.get(k)
                if v not in (None, "") and k not in f:
                    f[k] = str(v).strip()

        elif cls == "extent":
            for src, dst in (("undivided_share", "undivided_share"),
                             ("total_area", "total_area")):
                v = attrs.get(src)
                if v not in (None, "") and dst not in f:
                    f[dst] = str(v).strip()

        elif cls == "boundary":
            side = str(attrs.get("side") or "").strip().lower()
            if side in _SIDES:
                adj = attrs.get("adjacency")
                val = str(adj).strip() if adj not in (None, "") else text
                if val and f.get(f"boundary_{side}") in (None, ""):
                    f[f"boundary_{side}"] = val
                meas = attrs.get("measurement")
                if meas not in (None, "") and \
                        f.get(f"boundary_measurement_{side}") in (None, ""):
                    f[f"boundary_measurement_{side}"] = str(meas).strip()

        elif cls == "identifier":
            kind = str(attrs.get("kind") or "").strip().lower()
            val = attrs.get("value")
            val = str(val).strip() if val not in (None, "") else text
            if val:
                if kind == "door_old":
                    rec["doors_old"].append(val)
                elif kind == "door_new":
                    rec["doors_new"].append(val)

        elif cls == "auction_terms":
            r = parse_money(attrs.get("reserve_price_num"))
            if r is not None and rec["reserve"] is None:
                rec["reserve"] = r

    # finalize
    out: dict[str, dict] = {}
    for li, rec in lots.items():
        f = dict(rec["fields"])
        if rec["doors_old"]:
            f["door_numbers_old"] = ", ".join(dict.fromkeys(rec["doors_old"]))
        if rec["doors_new"]:
            f["door_numbers_new"] = ", ".join(dict.fromkeys(rec["doors_new"]))
        out[li] = {
            "description": "\n\n".join(rec["description_parts"]) or None,
            "fields": f,
            "reserve": rec["reserve"],
        }
    return out


# ── pure: lot ↔ listing matching ─────────────────────────────────────────────

def match_lots_to_listings(lots: dict[str, dict],
                           listings: list[dict]) -> tuple[list[tuple[dict, dict, str]],
                                                          list[tuple[dict, str]]]:
    """Assign each listing to at most one lot.

    listings: [{aid, price}]. Returns (matches, unmatched) where matches is
    [(listing, lot, reason)] and unmatched is [(listing, reason)].
    reason ∈ 'single' | 'exact' | 'tolerance' | 'remainder' | 'ambiguous' | 'none'.
    """
    lot_list = list(lots.values())
    if not lot_list:
        return [], [(l, "no_lots") for l in listings]

    if len(lot_list) == 1:
        return [(l, lot_list[0], "single") for l in listings], []

    matches: list[tuple[dict, dict, str]] = []
    unmatched: list[tuple[dict, str]] = []
    taken: set[int] = set()
    pending: list[dict] = []

    for listing in listings:
        price = listing.get("price")
        if price is None:
            unmatched.append((listing, "no_listing_price"))
            continue
        exact = [i for i, lo in enumerate(lot_list)
                 if lo["reserve"] is not None and lo["reserve"] == price]
        if len(exact) == 1:
            matches.append((listing, lot_list[exact[0]], "exact"))
            taken.add(exact[0])
            continue
        if len(exact) > 1:
            # identical reserve prices — assignment would be a guess
            unmatched.append((listing, "ambiguous"))
            continue
        tol = abs(price) * PRICE_TOLERANCE_PCT / 100.0
        near = [i for i, lo in enumerate(lot_list)
                if lo["reserve"] is not None and abs(lo["reserve"] - price) <= tol]
        if len(near) == 1:
            matches.append((listing, lot_list[near[0]], "tolerance"))
            taken.add(near[0])
            continue
        if len(near) > 1:
            unmatched.append((listing, "ambiguous"))
            continue
        pending.append(listing)

    # unique remainder: exactly one unmatched listing and one untaken lot
    free = [i for i in range(len(lot_list)) if i not in taken]
    if len(pending) == 1 and len(free) == 1:
        matches.append((pending[0], lot_list[free[0]], "remainder"))
    else:
        unmatched.extend((l, "none") for l in pending)

    return matches, unmatched


# ── Neo4j I/O ────────────────────────────────────────────────────────────────

def fetch_work(limit: int | None = None) -> list[dict]:
    """Documents with a grounded extraction + their linked listings."""
    return run_read_query(
        "MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document) "
        "WHERE d.extraction_json IS NOT NULL "
        "RETURN d.filename AS filename, "
        "       d.extraction_json AS extraction_json, "
        "       d.extraction_corrections_json AS corrections_json, "
        "       collect({aid: a.auction_id, price: a.reserve_price_num}) AS listings "
        "ORDER BY d.filename"
        + (f" LIMIT {int(limit)}" if limit else ""),
        max_rows=20_000, timeout=120.0)


def write_fields(rows: list[dict]) -> int:
    """SET += per-listing grounded fields (non-null only, built per row)."""
    if not rows:
        return 0
    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            SET a += row.props,
                a.enrichment_source = 'grounded_extraction',
                a.grounded_source_file = row.filename,
                a.grounded_applied_at = datetime($at)
            RETURN a.auction_id AS aid
        """, {"rows": batch, "at": now_iso})
        written += len(res) if res else 0
    return written


def write_descriptions(rows: list[dict]) -> int:
    """Description write with the same human guard as apply_descriptions."""
    if not rows:
        return 0
    written = 0
    for batch in chunked(rows, WRITE_CHUNK):
        res = run_query("""
            UNWIND $rows AS row
            MATCH (a:AuctionProperty {auction_id: row.aid})
            WHERE coalesce(a.description_verified, false) = false
              AND coalesce(a.description_source, '') <> 'human'
            SET a.description = row.desc,
                a.description_source = 'notice'
            RETURN a.auction_id AS aid
        """, {"rows": batch})
        written += len(res) if res else 0
    return written


# ── main ─────────────────────────────────────────────────────────────────────

def run(limit: int | None = None, dry_run: bool = False) -> int:
    work = fetch_work(limit)
    print(f"Documents with grounded extraction: {len(work)}")

    field_rows: list[dict] = []
    desc_rows: list[dict] = []
    unmatched_out: list[dict] = []
    stats = defaultdict(int)

    for w in work:
        ents = entities_with_corrections(w["extraction_json"],
                                         w.get("corrections_json"))
        if not ents:
            stats["empty_extraction"] += 1
            continue
        lots = group_lots(ents)
        listings = [l for l in (w.get("listings") or []) if l.get("aid")]
        matches, unmatched = match_lots_to_listings(lots, listings)
        for listing, lot, reason in matches:
            stats[f"match_{reason}"] += 1
            if lot["fields"]:
                field_rows.append({"aid": listing["aid"],
                                   "filename": w["filename"],
                                   "props": lot["fields"]})
            if lot["description"]:
                desc_rows.append({"aid": listing["aid"],
                                  "desc": lot["description"]})
        for listing, reason in unmatched:
            stats[f"unmatched_{reason}"] += 1
            unmatched_out.append({"aid": listing["aid"],
                                  "filename": w["filename"],
                                  "price": listing.get("price"),
                                  "reason": reason,
                                  "lot_reserves": [lo["reserve"]
                                                   for lo in lots.values()]})

    print(f"  match/unmatch stats: {dict(stats)}")
    print(f"  field rows: {len(field_rows)}  description rows: {len(desc_rows)}  "
          f"unmatched: {len(unmatched_out)}")

    if unmatched_out and not dry_run:
        UNMATCHED_CSV.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_CSV.open("w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["auction_id", "file", "listing_price", "reason",
                         "lot_reserves"])
            for u in unmatched_out:
                wr.writerow([u["aid"], u["filename"], u["price"], u["reason"],
                             ";".join(str(r) for r in u["lot_reserves"])])
        print(f"  unmatched logged to {UNMATCHED_CSV}")

    if dry_run:
        print("  DRY RUN — nothing written")
        return 0

    nf = write_fields(field_rows)
    nd = write_descriptions(desc_rows)
    print(f"  wrote fields to {nf} listings, descriptions to {nd} listings "
          f"(human-verified rows skipped)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents")
    ap.add_argument("--dry-run", action="store_true",
                    help="report matches/fields without writing to Neo4j")
    args = ap.parse_args()
    return run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
