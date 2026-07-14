"""
scripts/build_landing_pages.py
------------------------------
Move 2 of the SEO plan (docs/marketing/plan.md §4) — programmatic SEO
landing pages off the auction graph (the "growth engine" bet).

Generates standalone, static, on-brand landing pages under
web/bank-auctions/** for high-intent location queries:

    /bank-auctions/                      top hub  ("bank auctions in Tamil Nadu")
    /bank-auctions/<city>/               city hub ("bank auctions in Chennai")
    /bank-auctions/<city>/<type>/        city x type ("bank auction plots in Chennai")

These are NEW routes with no matching SPA screen, so they are deliberately
NOT the app shell (unlike scripts/prerender_properties.py's /property/<id>
pages): a standalone page is lighter/faster (better Core Web Vitals) and its
crawlable content is never stripped by the app booting. Each page links out
to individual /property/<id> pages and deep-links into the app to search.

Anti-thin-content (the skill's #1 rule): a page is only written when it has
>= MIN_LISTINGS live listings, and every page carries genuinely unique,
computed data — live count, reserve-price range + median, and the mix of
banks — not just swapped variables. Pages below the gate are skipped (left
out of the sitemap = effectively noindex).

Honesty rule (docs/marketing/copy-playbook.md) applies to every string:
real figures only, "evaluate"/"research" never "due diligence"/legal
certainty, and a SARFAESI disclaimer on every page.

Usage:
    python -m scripts.build_landing_pages                       # pilot: Chennai + Kanchipuram
    python -m scripts.build_landing_pages --city Coimbatore
    python -m scripts.build_landing_pages --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

from scripts.prerender_properties import fmt_money, fmt_date
from scripts import seo_sitemap

API_BASE = "https://auction-api-w68b.onrender.com"
SITE_BASE = "https://www.auctionscope.in"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
OUT_ROOT = WEB_DIR / "bank-auctions"

REQUEST_INTERVAL_S = 1.1
MIN_LISTINGS = 5          # thin-content gate: skip a page with fewer live listings
MAX_LISTED = 48           # cap listings shown per page (keeps the page focused)

PILOT_CITIES = ["Chennai", "Kanchipuram"]

# Curated indexable property types → (url slug, display plural, one-line lens).
# Only high-volume types get pages; long-tail types (Villa, Godown, …) are
# left out to avoid thin, near-empty pages.
TYPE_PAGES = {
    "Plot":              ("plots", "Plots", "vacant residential plots"),
    "Flat":              ("flats", "Flats", "apartments and flats"),
    "Land":              ("land", "Land", "land parcels"),
    "Land And Building": ("land-and-building", "Land &amp; Building", "land-with-building lots"),
    "House":             ("houses", "Houses", "independent houses"),
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def fetch_json(client: httpx.Client, params: dict) -> dict:
    resp = client.get(f"{API_BASE}/properties", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_live(client: httpx.Client, city: str, property_type: str | None) -> list[dict]:
    """Live (future auction_start) listings for a city, optionally one type."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    params = {"district": city, "sort": "upcoming", "date_from": now_iso,
              "limit": 200, "offset": 0}
    if property_type:
        params["property_type"] = property_type
    return fetch_json(client, params).get("results", [])


# ── stats + rendering ───────────────────────────────────────────────────────

def price_stats(rows: list[dict]) -> dict:
    prices = [r["reserve_price"] for r in rows
              if isinstance(r.get("reserve_price"), (int, float)) and r["reserve_price"] > 0]
    if not prices:
        return {}
    return {"min": min(prices), "max": max(prices), "median": statistics.median(prices),
            "count": len(prices)}


def bank_mix(rows: list[dict], top: int = 4) -> list[str]:
    counts: dict[str, int] = {}
    for r in rows:
        b = r.get("bank_short") or r.get("bank")
        if b:
            counts[b] = counts.get(b, 0) + 1
    return [b for b, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


PAGE_CSS = """
:root{--ink:#0a0b0d;--ink-soft:#33373e;--muted:#5b616e;--paper:#f6f7f9;
--card:#fff;--border:#dee0e4;--accent:#0052ff;--accent-soft:#e8f0ff;--radius:12px}
@media(prefers-color-scheme:dark){:root{--ink:#f5f7fa;--ink-soft:#c2c7d0;
--muted:#8b909b;--paper:#0a0b0d;--card:#16181d;--border:#2a2d34;--accent:#5b8bff;
--accent-soft:#10203a}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.55}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:960px;margin:0 auto;padding:24px 20px 64px}
header.top{display:flex;align-items:center;justify-content:space-between;
padding:14px 20px;border-bottom:1px solid var(--border)}
.brand{font-weight:700;font-size:19px;letter-spacing:-.01em;color:var(--ink)}
nav.crumb{font-size:13px;color:var(--muted);margin:20px 0 8px}
nav.crumb a{color:var(--muted)}
h1{font-size:28px;line-height:1.2;letter-spacing:-.02em;margin:.2em 0 .4em}
.lede{font-size:16px;color:var(--ink-soft);max-width:70ch}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0}
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:12px 16px;min-width:130px}
.stat .n{font-size:20px;font-weight:700}.stat .l{font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin:8px 0 28px}
.cardlink{display:block;background:var(--card);border:1px solid var(--border);
border-radius:var(--radius);padding:14px 16px;color:var(--ink)}
.cardlink:hover{border-color:var(--accent);text-decoration:none}
.cardlink .t{font-weight:600;font-size:14px;margin-bottom:6px;display:-webkit-box;
-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cardlink .m{font-size:12.5px;color:var(--muted)}
.cardlink .p{font-size:15px;font-weight:700;color:var(--accent);margin-top:6px}
h2{font-size:18px;margin:30px 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--accent-soft);border:1px solid var(--border);border-radius:999px;
padding:6px 13px;font-size:13px}
.cta{display:inline-block;background:var(--accent);color:#fff;border-radius:10px;
padding:11px 20px;font-weight:600;margin:8px 0 4px}.cta:hover{text-decoration:none;opacity:.92}
.note{color:var(--muted);font-size:12.5px;margin-top:28px;border-top:1px solid var(--border);padding-top:16px}
"""


def page_head(title: str, desc: str, url: str, jsonld: list[dict]) -> str:
    blocks = "\n".join(
        f'<script type="application/ld+json">{json.dumps(b, ensure_ascii=False)}</script>'
        for b in jsonld
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Auctionscope">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{SITE_BASE}/og-image.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
{blocks}
</head>
<body>
<header class="top"><a class="brand" href="/">auctionscope</a><a href="/">search all auctions →</a></header>
<div class="wrap">"""


PAGE_FOOT = """<p class="note">Auctionscope is an information platform only. Every listing here is a
bank e-auction conducted under the SARFAESI Act — always verify the reserve price, EMD,
possession type and encumbrances with the bank before bidding. Web-researched context is
approximate and not legal advice.</p>
</div></body></html>"""


def listing_card(row: dict) -> str:
    aid = row["auction_id"]
    title = html.escape(row.get("title") or "Bank auction property")
    area = html.escape(row.get("area") or row.get("city") or "")
    bank = html.escape(row.get("bank_short") or row.get("bank") or "")
    reserve = fmt_money(row.get("reserve_price"))
    deadline = fmt_date(row.get("auction_start"))
    meta = " · ".join(x for x in [area, bank, (f"ends {deadline}" if deadline else "")] if x)
    price = f'<div class="p">{html.escape(reserve)}</div>' if reserve else ""
    return (f'<a class="cardlink" href="/property/{aid}">'
            f'<div class="t">{title}</div><div class="m">{html.escape(meta)}</div>{price}</a>')


def breadcrumb_jsonld(trail: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name,
             "item": f"{SITE_BASE}{path}"}
            for i, (name, path) in enumerate(trail)
        ],
    }


def itemlist_jsonld(rows: list[dict]) -> dict:
    return {
        "@context": "https://schema.org", "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": f"{SITE_BASE}/property/{r['auction_id']}",
             "name": r.get("title") or "Bank auction property"}
            for i, r in enumerate(rows)
        ],
    }


def stat_block(stats: dict) -> str:
    if not stats:
        return ""
    cells = [("live listings", str(stats["count"]))]
    if stats.get("min"):
        cells.append(("reserve from", fmt_money(stats["min"])))
    if stats.get("median"):
        cells.append(("median reserve", fmt_money(stats["median"])))
    if stats.get("max"):
        cells.append(("up to", fmt_money(stats["max"])))
    return ('<div class="stats">'
            + "".join(f'<div class="stat"><div class="n">{html.escape(n)}</div>'
                      f'<div class="l">{l}</div></div>' for l, n in cells)
            + "</div>")


# ── page builders ───────────────────────────────────────────────────────────

def render_city_type(city: str, type_name: str, rows: list[dict],
                     sibling_types: list[tuple[str, str]],
                     other_cities: list[tuple[str, str]]) -> str:
    slug, plural, lens = TYPE_PAGES[type_name]
    city_slug = slugify(city)
    url = f"{SITE_BASE}/bank-auctions/{city_slug}/{slug}"
    stats = price_stats(rows)
    banks = bank_mix(rows)
    plural_txt = re.sub(r"&amp;", "&", plural).lower()

    title = f"Bank Auction {re.sub('&amp;', '&', plural)} in {city} | Auctionscope"[:70]
    desc = (f"{stats.get('count', len(rows))} live bank-auction {plural_txt} in {city}, "
            f"Tamil Nadu")
    if stats.get("min") and stats.get("max"):
        desc += f" — reserve prices {fmt_money(stats['min'])} to {fmt_money(stats['max'])}"
    desc = (desc + ". Search, compare and evaluate each with AuctionScope's AI.")[:158]

    trail = [("Home", "/"), ("Bank Auctions", "/bank-auctions"),
             (city, f"/bank-auctions/{city_slug}"),
             (re.sub("&amp;", "&", plural), f"/bank-auctions/{city_slug}/{slug}")]

    lede = (f"There are <strong>{stats.get('count', len(rows))} live {lens}</strong> up for "
            f"bank auction in {city} right now")
    if stats.get("min") and stats.get("median"):
        lede += (f", with reserve prices from {fmt_money(stats['min'])} to "
                 f"{fmt_money(stats['max'])} (median {fmt_money(stats['median'])})")
    if banks:
        lede += f", listed by banks including {html.escape(', '.join(banks))}"
    lede += (". These are SARFAESI e-auctions — the notice gives you a price, but not whether "
             "the area floods, what's nearby, or if the reserve is fair. That's what you can ask "
             "AuctionScope about each one.")

    shown = rows[:MAX_LISTED]
    parts = [page_head(title, desc, url,
                       [breadcrumb_jsonld(trail), itemlist_jsonld(shown)])]
    parts.append('<nav class="crumb"><a href="/">home</a> / '
                 f'<a href="/bank-auctions">bank auctions</a> / '
                 f'<a href="/bank-auctions/{city_slug}">{html.escape(city.lower())}</a> / '
                 f'{plural.lower()}</nav>')
    parts.append(f"<h1>Bank Auction {re.sub('&amp;', '&', plural)} in {city}</h1>")
    parts.append(f'<p class="lede">{lede}</p>')
    parts.append(stat_block(stats))
    parts.append(f'<a class="cta" href="/">Search these live on AuctionScope →</a>')
    parts.append('<h2>Live listings</h2><div class="grid">'
                 + "".join(listing_card(r) for r in shown) + "</div>")

    if sibling_types:
        parts.append(f"<h2>Other property types in {city}</h2><div class=\"chips\">"
                     + "".join(f'<a class="chip" href="{p}">{html.escape(n)}</a>'
                               for n, p in sibling_types) + "</div>")
    if other_cities:
        label = re.sub("&amp;", "&", plural)
        parts.append(f"<h2>{label} in other cities</h2><div class=\"chips\">"
                     + "".join(f'<a class="chip" href="{p}">{html.escape(n)}</a>'
                               for n, p in other_cities) + "</div>")
    parts.append(PAGE_FOOT)
    return "".join(parts)


def render_city_hub(city: str, all_rows: list[dict],
                    type_links: list[tuple[str, str]],
                    other_cities: list[tuple[str, str]]) -> str:
    city_slug = slugify(city)
    url = f"{SITE_BASE}/bank-auctions/{city_slug}"
    stats = price_stats(all_rows)
    banks = bank_mix(all_rows, top=5)

    title = f"Bank Auctions in {city} — live SARFAESI property | Auctionscope"[:70]
    desc = (f"{stats.get('count', len(all_rows))} live bank-auction properties in {city}, "
            f"Tamil Nadu — plots, flats, land and houses. Search and evaluate each with "
            f"AuctionScope's AI.")[:158]
    trail = [("Home", "/"), ("Bank Auctions", "/bank-auctions"),
             (city, f"/bank-auctions/{city_slug}")]

    lede = (f"<strong>{stats.get('count', len(all_rows))} live bank-auction properties</strong> "
            f"in {city} right now across every property type")
    if stats.get("min") and stats.get("max"):
        lede += f", with reserves from {fmt_money(stats['min'])} to {fmt_money(stats['max'])}"
    if banks:
        lede += f", from banks including {html.escape(', '.join(banks))}"
    lede += (". Pick a property type below, or search and ask AuctionScope whether any given "
             "auction's location and price actually check out.")

    parts = [page_head(title, desc, url, [breadcrumb_jsonld(trail)])]
    parts.append('<nav class="crumb"><a href="/">home</a> / '
                 f'<a href="/bank-auctions">bank auctions</a> / {html.escape(city.lower())}</nav>')
    parts.append(f"<h1>Bank Auctions in {city}</h1>")
    parts.append(f'<p class="lede">{lede}</p>')
    parts.append(stat_block(stats))
    parts.append('<a class="cta" href="/">Search all auctions on AuctionScope →</a>')
    if type_links:
        parts.append('<h2>Browse by property type</h2><div class="chips">'
                     + "".join(f'<a class="chip" href="{p}">{html.escape(n)}</a>'
                               for n, p in type_links) + "</div>")
    sample = all_rows[:MAX_LISTED]
    parts.append('<h2>Latest live listings</h2><div class="grid">'
                 + "".join(listing_card(r) for r in sample) + "</div>")
    if other_cities:
        parts.append('<h2>Auctions in other cities</h2><div class="chips">'
                     + "".join(f'<a class="chip" href="{p}">{html.escape(n)}</a>'
                               for n, p in other_cities) + "</div>")
    parts.append(PAGE_FOOT)
    return "".join(parts)


def render_top_hub(city_links: list[tuple[str, str, int]]) -> str:
    url = f"{SITE_BASE}/bank-auctions"
    total = sum(c for _, _, c in city_links)
    title = "Bank Auctions in Tamil Nadu — live SARFAESI property | Auctionscope"[:70]
    desc = (f"Live bank-auction property across Tamil Nadu — {total}+ listings in "
            f"{len(city_links)} cities. Search plots, flats and land and evaluate each with "
            f"AuctionScope's AI.")[:158]
    trail = [("Home", "/"), ("Bank Auctions", "/bank-auctions")]

    parts = [page_head(title, desc, url, [breadcrumb_jsonld(trail)])]
    parts.append('<nav class="crumb"><a href="/">home</a> / bank auctions</nav>')
    parts.append("<h1>Bank Auctions in Tamil Nadu</h1>")
    parts.append('<p class="lede">Live SARFAESI bank-auction property across Tamil Nadu — '
                 'plots, flats, land and houses, listed by banks and reconstruction companies. '
                 'Pick a city, then ask AuctionScope whether any given auction\'s location, '
                 'water/flood risk and price actually check out.</p>')
    parts.append('<a class="cta" href="/">Search all auctions on AuctionScope →</a>')
    parts.append('<h2>Browse by city</h2><div class="chips">'
                 + "".join(f'<a class="chip" href="{p}">{html.escape(n)} ({c})</a>'
                           for n, p, c in city_links) + "</div>")
    parts.append(PAGE_FOOT)
    return "".join(parts)


def write(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would write {path.relative_to(REPO_ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  wrote {path.relative_to(REPO_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", action="append", default=[],
                         help="Pilot city (repeatable). Defaults to Chennai + Kanchipuram.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cities = args.city or PILOT_CITIES

    written_city_type: dict[str, list[tuple[str, str]]] = {}  # type_name -> [(city, url)]
    city_meta: list[tuple[str, str, int]] = []                # (city, url, live_count)
    # First pass: fetch everything + decide which pages clear the gate.
    plan: dict[str, dict] = {}

    with httpx.Client() as client:
        for city in cities:
            print(f"== {city} ==")
            time.sleep(REQUEST_INTERVAL_S)
            all_rows = fetch_live(client, city, None)
            if len(all_rows) < MIN_LISTINGS:
                print(f"  only {len(all_rows)} live — skipping {city} entirely")
                continue
            type_rows: dict[str, list[dict]] = {}
            for type_name in TYPE_PAGES:
                time.sleep(REQUEST_INTERVAL_S)
                rows = fetch_live(client, city, type_name)
                if len(rows) >= MIN_LISTINGS:
                    type_rows[type_name] = rows
                    written_city_type.setdefault(type_name, []).append(
                        (city, f"/bank-auctions/{slugify(city)}/{TYPE_PAGES[type_name][0]}"))
                else:
                    print(f"  {type_name}: {len(rows)} live (< {MIN_LISTINGS}) — skipped")
            plan[city] = {"all": all_rows, "types": type_rows}
            city_meta.append((city, f"/bank-auctions/{slugify(city)}", len(all_rows)))

    if not plan:
        print("No cities cleared the thin-content gate — nothing generated.")
        return 0

    # Second pass: render with cross-links limited to pages that actually exist.
    for city, data in plan.items():
        city_slug = slugify(city)
        type_links = [(re.sub("&amp;", "&", TYPE_PAGES[t][1]),
                       f"/bank-auctions/{city_slug}/{TYPE_PAGES[t][0]}")
                      for t in data["types"]]
        # city hub
        other_cities = [(c, u) for c, u, _ in city_meta if c != city]
        write(OUT_ROOT / city_slug / "index.html",
              render_city_hub(city, data["all"], type_links, other_cities), args.dry_run)
        # city x type
        for type_name, rows in data["types"].items():
            slug = TYPE_PAGES[type_name][0]
            siblings = [(n, u) for n, u in type_links if not u.endswith(f"/{slug}")]
            others = [(c, u) for c, u in written_city_type.get(type_name, []) if c != city]
            write(OUT_ROOT / city_slug / slug / "index.html",
                  render_city_type(city, type_name, rows, siblings, others), args.dry_run)

    # top hub
    write(OUT_ROOT / "index.html", render_top_hub(city_meta), args.dry_run)

    n_pages = 1 + len(city_meta) + sum(len(d["types"]) for d in plan.values())
    print(f"\n{n_pages} landing pages "
          f"({'would be ' if args.dry_run else ''}generated) across {len(plan)} cities")

    if not args.dry_run:
        count = seo_sitemap.write_sitemap(WEB_DIR)
        print(f"sitemap.xml rebuilt — {count} URLs total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
