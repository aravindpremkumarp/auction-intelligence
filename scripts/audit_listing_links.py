"""
audit_listing_links.py — would the matcher's strong pairs merge distinct properties?

build_spine unions listings over CONFIRMED / PROBABLE :SAME_LISTING_AS edges.
A portal never lists one auction twice, so a cluster holding two listings of
the same portal is either duplicate postings of one unit or distinct
properties merged into one event. This prints every such cluster from a
gap report's JSON with each member's price, borrower and unit numbers, for a
person to tell which. Clusters are taken over every strong pair across all
portals, so a chain such as bn↔ea1, bn↔be, be↔ea2 that joins two
eauctionsindia listings through other portals is caught too.

    python -m scripts.gap_report --existing-json graph.json --json gap.json
    python -m scripts.audit_listing_links gap.json --existing-json graph.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.canonical import BRIDGE_GRADES  # noqa: E402
from sources.match import UNIT_FAMILIES, extract_identifiers  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def same_source_clusters(pairs: list[dict]) -> list[list[str]]:
    """Clusters over strong pairs that hold 2+ listings of one source, each
    sorted, largest cluster first."""
    parent: dict[str, str] = {}
    source: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p.get("confidence") not in BRIDGE_GRADES:
            continue
        source[p["a_id"]], source[p["b_id"]] = p["a_source"], p["b_source"]
        parent[find(p["a_id"])] = find(p["b_id"])
    clusters: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        clusters[find(x)].append(x)
    out = [sorted(m) for m in clusters.values() if len({source[x] for x in m}) < len(m)]
    return sorted(out, key=lambda m: (-len(m), m))


def _members(data_dir: Path, existing_json: Path | None) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for path in sorted((data_dir / "listings").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                by_id[row["auction_id"]] = {"price": row.get("reserve_price_num"), "borrower": row.get("borrower_name"),
                                            "text": " ".join(t for t in (row.get("title"), row.get("description")) if t)}
    if existing_json:
        for rec in json.loads(existing_json.read_text(encoding="utf-8")):
            by_id.setdefault(rec["auction_id"], {"price": rec.get("reserve_price_num"), "borrower": rec.get("borrower"),
                                                 "text": rec.get("description") or ""})
    return by_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="gap_report --json output")
    ap.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    ap.add_argument("--existing-json", default=None, help="the graph listings the report was run against")
    args = ap.parse_args(argv)

    pairs = json.loads(Path(args.report).read_text(encoding="utf-8"))["pairs"]
    clusters = same_source_clusters(pairs)
    info = _members(Path(args.data_dir), Path(args.existing_json) if args.existing_json else None)
    print(f"clusters with 2+ listings of one portal: {len(clusters)}  (listings: {sum(len(c) for c in clusters)})")
    for members in clusters:
        print(f"== {len(members)} listings")
        for aid in members:
            m = info.get(aid, {})
            units = sorted(f"{f} {v}" for f, v in extract_identifiers(m.get("text")) if f in UNIT_FAMILIES)
            print(f"   {aid:<12} Rs {m.get('price')}  {m.get('borrower')!r}  units: {', '.join(units) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
