"""
scripts/prerender_properties.py
--------------------------------
Move 1 of the SEO plan (docs/marketing/plan.md §4) — "make the app crawlable."

Generates static, crawlable HTML for individual property pages
(web/property/<id>/index.html) by cloning web/index.html — the SPA shell —
and swapping in per-property <title>/description/canonical/OG tags plus a
small static content block. Everything else (every <script>, the entire app
body) is byte-identical to the live shell, so the page is still the exact
same interactive app once JS boots; app.js removes the static block right
before it renders its own state (see the ssr-property hook near the end of
app.js). This is a "real content first, then the app takes over" pattern,
not dynamic rendering — bots and JS users see equivalent content.

Scope (v1, pilot): live, description-complete auctions in a small set of
pilot cities. Programmatic city/bank/type landing pages (Move 2 — new
routes that don't exist in the SPA today) are a separate, larger follow-up;
this script only prerenders the EXISTING /property/<id> route. Historical/
expired-auction pages (real SEO value per the plan, price-history content)
are also a deliberate follow-up, not v1 — this pass covers live inventory.

Usage:
    python -m scripts.prerender_properties --city Chennai --city Kanchipuram
    python -m scripts.prerender_properties --city Chennai --limit 10 --dry-run
    python -m scripts.prerender_properties --all --limit 500   # full run, later
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from pathlib import Path

import httpx

API_BASE = "https://auction-api-w68b.onrender.com"
SITE_BASE = "https://www.auctionscope.in"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
TEMPLATE_PATH = WEB_DIR / "index.html"
OUT_ROOT = WEB_DIR / "property"
SITEMAP_PATH = WEB_DIR / "sitemap.xml"

# Requests stay well under PUBLIC_READ_LIMIT ("60/minute", api/auth/rate_limit.py).
REQUEST_INTERVAL_S = 1.1

_STATIC_SITEMAP_URLS = [
    ("/", "daily", "1.0"),
    ("/privacy-policy", "yearly", "0.3"),
    ("/terms-of-service", "yearly", "0.3"),
    ("/disclaimer", "yearly", "0.3"),
]


def fmt_money(num: float | None) -> str | None:
    """Port of app.js's price formatter (₹ Cr / ₹ L) so copy matches the live app."""
    if num is None:
        return None
    if num >= 1e7:
        val = f"{num / 1e7:.2f}".rstrip("0").rstrip(".")
        return f"₹ {val} Cr"
    if num >= 1e5:
        val = f"{num / 1e5:.2f}".rstrip("0").rstrip(".")
        return f"₹ {val} L"
    return f"₹ {num:,.0f}"


def fmt_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y")
    except (ValueError, TypeError):
        return None


def fetch_json(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    resp = client.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


_PAGE_SIZE = 200         # API max per page (_PROPERTIES_MAX_LIMIT in api/properties/router.py)
_LIVE_CHECK_BUDGET = 30  # small — live-range completeness measured ~0%, see below


def iter_candidates(client: httpx.Client, city: str):
    """Auctions in one city: a small live-range sample first, then the
    expired range, paginated.

    Verified empirically: a 200-record live-only Chennai sample measured 0%
    description_complete (the newest scrape batch hasn't reached the
    offline OCR/enrichment pipeline yet), while the just-expired range
    measured ~20% — enrichment lags scraping by design (see
    docs/marketing/plan.md §13 open decision #1). So this checks only a
    small budget of live candidates (cheap to include any that do qualify)
    before moving to the expired range, which is where most of the yield
    actually is. A v1 pilot needs pages from both anyway: live pages for
    current inventory, expired ones for the "price-history" SEO value the
    plan already calls out. build_ssr_block()/seo_title()/seo_description()
    render expired ones honestly ("this auction has closed"), never as
    still-biddable.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    live = fetch_json(client, "/properties", {
        "district": city, "sort": "upcoming", "date_from": now_iso,
        "limit": _LIVE_CHECK_BUDGET, "offset": 0,
    })
    yield from live.get("results", [])
    time.sleep(REQUEST_INTERVAL_S)

    offset = 0
    while True:
        expired = fetch_json(client, "/properties", {
            "district": city, "sort": "date_desc", "date_to": now_iso,
            "limit": _PAGE_SIZE, "offset": offset,
        })
        results = expired.get("results", [])
        if not results:
            return
        yield from results
        offset += _PAGE_SIZE
        if offset >= expired.get("total", 0):
            return
        time.sleep(REQUEST_INTERVAL_S)


def fetch_detail(client: httpx.Client, auction_id: str) -> dict | None:
    try:
        return fetch_json(client, f"/auction/{auction_id}")
    except httpx.HTTPStatusError as e:
        print(f"  ! {auction_id}: {e.response.status_code}, skipping", file=sys.stderr)
        return None


def is_ended(fields: dict) -> bool:
    """Mirrors app.js's toCard() ended check (web/app.js) — auction_start_dt
    in the past. Drives the honest "this auction has closed" framing below;
    never state or imply an ended auction is still biddable."""
    from datetime import datetime, timezone
    start = fields.get("auction_start_dt")
    if not start:
        return False
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def build_ssr_block(fields: dict, rel: dict, ended: bool) -> str:
    """The static, crawlable content block — real facts only, same honesty
    rule as the marketing copy (docs/marketing/copy-playbook.md): every
    figure comes from the record, nothing invented. Ended auctions are
    framed as closed/reference-only, never as still-biddable."""
    title = html.escape(fields.get("title") or "Bank auction property")
    city = html.escape((rel.get("city") or {}).get("name") or fields.get("district") or "")
    area = html.escape((rel.get("area") or {}).get("name") or "")
    bank = html.escape((rel.get("bank") or {}).get("name") or "")
    ptypes = ", ".join(rel.get("property_types") or [])
    reserve = fmt_money(fields.get("reserve_price_num"))
    emd = fmt_money(fields.get("emd_num"))
    deadline = fmt_date(fields.get("application_deadline_dt") or fields.get("auction_start_dt"))
    description = html.escape((fields.get("description") or "").strip())

    facts = []
    if ptypes:
        facts.append(f"{html.escape(ptypes)} in {area}, {city}" if area else f"{html.escape(ptypes)} in {city}")
    if bank:
        facts.append(f"bank: {bank}")
    if reserve:
        facts.append(("closing reserve: " if ended else "reserve price: ") + html.escape(reserve))
    if emd:
        facts.append(f"EMD: {html.escape(emd)}")
    if deadline:
        facts.append((f"auction date: {html.escape(deadline)}") if ended else f"deadline: {html.escape(deadline)}")
    facts_line = " &middot; ".join(facts)

    desc_html = f"<p>{description}</p>" if description else ""

    status_line = (
        '<p style="background:var(--paper-2,#eef0f3);padding:8px 12px;border-radius:8px;'
        'font-size:13px;margin:0 0 16px;">'
        "this auction has closed &mdash; kept here for reference. reserve price and terms "
        "reflect that auction round; check auctionscope for current live listings in this area."
        "</p>"
        if ended else ""
    )
    cta_text = ("see current live tamil nadu bank auctions on auctionscope" if ended
                else "browse all live Tamil Nadu bank auctions on Auctionscope")

    return (
        '<div id="ssr-property" style="max-width:720px;margin:0 auto;'
        'padding:32px 20px;font-family:var(--font-body,Inter,sans-serif);'
        'color:var(--ink,#0a0b0d);line-height:1.55;">'
        f"<h1 style=\"font-size:22px;margin:0 0 10px;\">{title}</h1>"
        f'<p style="color:var(--ink-soft,#33373e);font-size:14px;margin:0 0 16px;">{facts_line}</p>'
        f"{status_line}"
        f"{desc_html}"
        '<p style="margin-top:20px;font-size:13px;">'
        f'<a href="/" style="color:var(--accent,#0052ff);">{cta_text}</a>'
        "</p>"
        '<p style="color:var(--muted,#5b616e);font-size:12px;margin-top:16px;">'
        "Auctionscope is an information platform only &mdash; always verify auction details, "
        "possession type and EMD with the bank before bidding."
        "</p>"
        "</div>"
    )


def seo_title(fields: dict, rel: dict, ended: bool) -> str:
    ptypes = (rel.get("property_types") or [None])[0] or "property"
    city = (rel.get("city") or {}).get("name") or ""
    reserve = fmt_money(fields.get("reserve_price_num"))
    label = "past bank auction" if ended else "bank auction"
    parts = [f"{ptypes} {label}" + (f" in {city}" if city else "")]
    if reserve:
        parts.append(f"{reserve} reserve")
    title = " — ".join(parts) + " | Auctionscope"
    return title[:70]


def seo_description(fields: dict, rel: dict, ended: bool) -> str:
    ptypes = (rel.get("property_types") or [None])[0] or "property"
    city = (rel.get("city") or {}).get("name") or ""
    bank = (rel.get("bank") or {}).get("name") or ""
    reserve = fmt_money(fields.get("reserve_price_num"))
    deadline = fmt_date(fields.get("application_deadline_dt") or fields.get("auction_start_dt"))
    verb = "Closed bank auction" if ended else "Bank auction"
    bits = [f"{verb} {ptypes.lower()}" + (f" in {city}" if city else "")]
    if bank:
        bits.append(f"conducted by {bank}")
    if reserve:
        bits.append(f"reserve {reserve}")
    if deadline and not ended:
        bits.append(f"deadline {deadline}")
    desc = ", ".join(bits) + ". Ask AuctionScope's AI whether the location, price and paperwork check out."
    return desc[:158]


def render_page(template: str, auction_id: str, fields: dict, rel: dict) -> str:
    url = f"{SITE_BASE}/property/{auction_id}"
    ended = is_ended(fields)
    title = html.escape(seo_title(fields, rel, ended))
    desc = html.escape(seo_description(fields, rel, ended))

    out = template
    out = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", out, count=1)
    out = re.sub(
        r'<meta name="description" content="[^"]*">',
        f'<meta name="description" content="{desc}">', out, count=1,
    )
    out = re.sub(
        r'<link rel="canonical" href="[^"]*">',
        f'<link rel="canonical" href="{url}">', out, count=1,
    )
    out = re.sub(r'<meta property="og:title" content="[^"]*">',
                 f'<meta property="og:title" content="{title}">', out, count=1)
    out = re.sub(r'<meta property="og:description" content="[^"]*">',
                 f'<meta property="og:description" content="{desc}">', out, count=1)
    out = re.sub(r'<meta property="og:url" content="[^"]*">',
                 f'<meta property="og:url" content="{url}">', out, count=1)
    out = re.sub(r'<meta name="twitter:title" content="[^"]*">',
                 f'<meta name="twitter:title" content="{title}">', out, count=1)
    out = re.sub(r'<meta name="twitter:description" content="[^"]*">',
                 f'<meta name="twitter:description" content="{desc}">', out, count=1)

    ssr_block = build_ssr_block(fields, rel, ended)
    out = out.replace("<body>\n", f"<body>\n{ssr_block}\n", 1)
    return out


def build_sitemap(generated_ids: list[str]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, freq, prio in _STATIC_SITEMAP_URLS:
        lines += ["  <url>", f"    <loc>{SITE_BASE}{path}</loc>",
                  f"    <changefreq>{freq}</changefreq>", f"    <priority>{prio}</priority>", "  </url>"]
    for aid in sorted(set(generated_ids)):
        lines += ["  <url>", f"    <loc>{SITE_BASE}/property/{aid}</loc>",
                  "    <changefreq>weekly</changefreq>", "    <priority>0.7</priority>", "  </url>"]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", action="append", default=[],
                         help="Pilot city to prerender (repeatable). Defaults to Chennai + Kanchipuram.")
    parser.add_argument("--limit", type=int, default=25, help="Max properties per city.")
    parser.add_argument("--all", action="store_true",
                         help="Ignore --city; use every city seen live (full run — use once the pilot is validated).")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files, just report what would happen.")
    args = parser.parse_args(argv)

    cities = args.city or ["Chennai", "Kanchipuram"]
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    generated: list[str] = []
    skipped_incomplete = 0
    skipped_error = 0

    with httpx.Client() as client:
        if args.all:
            stats = fetch_json(client, "/stats")
            print(f"--all requested but city enumeration isn't wired yet "
                  f"({stats.get('total_auctions')} total auctions) — falling back to --city list.",
                  file=sys.stderr)
        for city in cities:
            print(f"== {city} ==")
            found_for_city = 0
            checked = 0
            for row in iter_candidates(client, city):
                if found_for_city >= args.limit:
                    break
                checked += 1
                auction_id = row["auction_id"]
                time.sleep(REQUEST_INTERVAL_S)
                detail = fetch_detail(client, auction_id)
                if detail is None:
                    skipped_error += 1
                    continue
                fields = detail.get("fields", {})
                rel = detail.get("relationships", {})
                if not fields.get("description_complete"):
                    skipped_incomplete += 1
                    continue
                page = render_page(template, auction_id, fields, rel)
                out_path = OUT_ROOT / auction_id / "index.html"
                if args.dry_run:
                    print(f"  [dry-run] would write {out_path.relative_to(REPO_ROOT)}")
                else:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(page, encoding="utf-8")
                    print(f"  wrote {out_path.relative_to(REPO_ROOT)}")
                generated.append(auction_id)
                found_for_city += 1
            print(f"  {found_for_city}/{args.limit} found after checking {checked} candidates")

    print(f"\n{len(generated)} pages generated, {skipped_incomplete} skipped "
          f"(incomplete description), {skipped_error} skipped (fetch error)")

    if not args.dry_run and generated:
        # Preserve any previously-generated property URLs already on disk so
        # re-running with a narrower --city list doesn't shrink the sitemap.
        existing_ids = [p.parent.name for p in OUT_ROOT.glob("*/index.html")] if OUT_ROOT.exists() else []
        SITEMAP_PATH.write_text(build_sitemap(sorted(set(existing_ids) | set(generated))), encoding="utf-8")
        print(f"wrote {SITEMAP_PATH.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
