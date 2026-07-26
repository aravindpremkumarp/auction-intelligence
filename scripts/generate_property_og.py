"""
scripts/generate_property_og.py
-------------------------------
Per-property Open Graph cards — one 1200x630 image per live auction.

Why this exists: every prerendered property page (web/property/<id>/index.html)
previously advertised the SAME picture — the generic web/og-image.png — in both
its <meta property="og:image"> and, worse, in the `image` field of its
Product / RealEstateListing JSON-LD. That told Google 664 distinct Products are
depicted by one company logo, on exactly the pages we want rich results for.

This script renders each property its own card and publishes a manifest that
prerender_properties.py reads when writing those tags.

What makes it safe to run over the whole inventory: the card carries NO
authored copy. There is no hook, no headline, no caption — every glyph is a
field off the auction record, so there is nothing for a human to review and
nothing that can drift from the notice (same contract as the stats reel).

Storage: R2, not git. 664 PNGs is ~65MB against a 12MB .git, and they churn
whenever a reserve moves. vercel.json already allowlists the R2 public host in
its img-src CSP, so nothing there needs to change.

Usage:
    python scripts/generate_property_og.py --limit 5 --no-upload   # local smoke
    python scripts/generate_property_og.py --all                   # full inventory
    python scripts/generate_property_og.py --auction-id 798444     # one property
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from marketing_agents.poster import asset_headline  # noqa: E402
from scripts.prerender_properties import (  # noqa: E402
    fmt_date,
    iter_all_live_candidates,
)

TEMPLATE = "property-og-1200x630"
# Served from the downloads bucket (same one the notice PDFs use), under its own
# prefix so cleanup_orphan_r2_objects.py can reason about it separately.
R2_PREFIX = "property-og"
MANIFEST_PATH = REPO_ROOT / "web" / "og-manifest.json"
OG_WIDTH, OG_HEIGHT = 1200, 630


def og_key(auction_id: str) -> str:
    """Deterministic R2 key. Stable across runs so a re-render overwrites in
    place and every already-published og:image URL keeps resolving."""
    safe = "".join(c for c in str(auction_id) if c.isalnum() or c in "-_")
    return f"{R2_PREFIX}/{safe}.png"


def is_ended(row: dict) -> bool:
    """Auction start already in the past. Mirrors prerender_properties.is_ended,
    but reads the /properties field name (auction_start) rather than the
    /auction/<id> detail one (auction_start_dt)."""
    start = row.get("auction_start")
    if not start:
        return False
    try:
        return datetime.fromisoformat(str(start).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def build_island(row: dict) -> dict | None:
    """A /properties row -> the card's #data island, or None when the row has
    no reserve price (the card's whole point is the number; we do not render a
    card with a blank where the price goes)."""
    reserve = row.get("reserve_price")
    if not reserve:
        return None

    city = (row.get("city") or "").strip()
    area = (row.get("area") or "").strip()
    # Locality only when it adds something past the city line above it.
    locality = "" if area.lower() == city.lower() else area

    prev = row.get("previous_reserve_price")
    ended = is_ended(row)
    dropped = bool(prev and prev > reserve)
    if ended:
        state, eyebrow = "ended", "Auction closed"
        source_line = "Reserve price and date from the bank's auction notice for that round."
    elif dropped:
        state, eyebrow = "price_drop", "Price drop"
        source_line = "Both prices from the bank's auction notices — earlier listing vs current re-auction."
    else:
        state, eyebrow = "live", "Live auction"
        source_line = "Reserve price and date from the bank's auction notice."

    return {
        "eyebrow": eyebrow,
        "state": state,
        "city": city,
        "asset_type": asset_headline(row),
        "locality": locality,
        "reserve_price": reserve,
        # Only pass an earlier price when it is genuinely higher — the template
        # hides the strike-through and the computed % without one.
        "previous_reserve_price": prev if dropped else None,
        "auction_date": fmt_date(row.get("auction_start")) or "",
        "bank": (row.get("bank_short") or row.get("bank") or "").strip(),
        "source_line": source_line,
    }


def collect_rows(client: httpx.Client, auction_id: str | None,
                 limit: int | None) -> list[dict]:
    """The /properties rows to render cards for.

    /properties has no auction_id filter (see api/properties/router.py —
    list_properties takes q/type/bank/district/price/date and nothing else), so
    a single-id request scans the live pages and stops at the match rather than
    passing a parameter the API would silently ignore.
    """
    if auction_id:
        for row in iter_all_live_candidates(client):
            if str(row.get("auction_id")) == str(auction_id):
                return [row]
        sys.exit(f"auction {auction_id} is not in live inventory "
                 "(already ended, or filtered out of /properties)")
    rows = []
    for row in iter_all_live_candidates(client):
        rows.append(row)
        if limit and len(rows) >= limit:
            break
    return rows


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="every live auction (no cap)")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of cards")
    ap.add_argument("--auction-id", help="render one property by id")
    ap.add_argument("--no-upload", action="store_true",
                    help="render locally only; skip R2 and leave the manifest alone")
    ap.add_argument("--out", default=None,
                    help="local render dir (default: a temp dir under .og_renders)")
    args = ap.parse_args(argv)

    if not (args.all or args.limit or args.auction_id):
        ap.error("pass --all, --limit N, or --auction-id ID")

    from marketing.render_social import render_batch

    with httpx.Client() as client:
        rows = collect_rows(client, args.auction_id, None if args.all else args.limit)
    print(f"{len(rows)} candidate row(s)")

    items: list[tuple[dict, str]] = []
    skipped = 0
    for row in rows:
        island = build_island(row)
        if island is None:
            skipped += 1
            continue
        items.append((island, f"{row['auction_id']}.png"))
    if skipped:
        print(f"  skipped {skipped} row(s) with no reserve price")
    if not items:
        print("nothing to render")
        return 1

    out_dir = Path(args.out) if args.out else REPO_ROOT / ".og_renders"
    written = render_batch(TEMPLATE, items, out_dir,
                           viewport=(OG_WIDTH + 40, OG_HEIGHT + 270))

    if args.no_upload:
        print(f"--no-upload: {len(written)} card(s) left in {out_dir}")
        return 0

    from pipeline.storage import public_url_for, upload_file

    manifest = load_manifest()
    for path in written:
        auction_id = path.stem
        key = og_key(auction_id)
        upload_file(path, key, content_type="image/png")
        manifest[auction_id] = public_url_for(key)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"uploaded {len(written)} card(s); manifest now has {len(manifest)} entries "
          f"→ {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
