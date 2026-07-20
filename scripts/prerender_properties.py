"""
scripts/prerender_properties.py
--------------------------------
Move 1 of the SEO plan (docs/marketing/plan.md §4) — "make the app crawlable."

Generates static, crawlable HTML for individual property pages
(web/property/<id>/index.html) by cloning web/index.html — the SPA shell —
and swapping in per-property <title>/description/canonical/OG tags,
schema.org JSON-LD (Move 3 — see property_jsonld), plus a small static
content block. Everything else (every <script>, the entire app
body) is byte-identical to the live shell, so the page is still the exact
same interactive app once JS boots; app.js removes the static block right
before it renders its own state (see the ssr-property hook near the end of
app.js). This is a "real content first, then the app takes over" pattern,
not dynamic rendering — bots and JS users see equivalent content.

Scope (v1, pilot): auctions with a real description in a small set of pilot
cities, live inventory first. Programmatic city/bank/type landing pages
(Move 2 — new routes that don't exist in the SPA today) are a separate,
larger follow-up; this script only prerenders the EXISTING /property/<id>
route.

Content gate: fields.description, NOT fields.description_complete. The
latter is an enrichment-pipeline judge flag (was the notice-PDF OCR
extraction verified complete) that lags scraping by design — it measured
~0% on live inventory in testing. fields.description itself already falls
back to the site-scraped text before that judging ever runs (verified: 20/20
live Chennai auctions had substantial description text despite
description_complete being unset), so gating on real content length covers
live inventory immediately instead of waiting on the enrichment backlog.

Usage:
    python -m scripts.prerender_properties --city Chennai --city Kanchipuram
    python -m scripts.prerender_properties --city Chennai --limit 10 --dry-run
    python -m scripts.prerender_properties --all            # every live auction, all cities
    python -m scripts.prerender_properties --all --limit 200  # global safety cap
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

import httpx

from scripts import seo_sitemap

API_BASE = "https://auction-api-w68b.onrender.com"
SITE_BASE = "https://www.auctionscope.in"
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
TEMPLATE_PATH = WEB_DIR / "index.html"
OUT_ROOT = WEB_DIR / "property"

# Requests stay well under PUBLIC_READ_LIMIT ("60/minute", api/auth/rate_limit.py).
REQUEST_INTERVAL_S = 1.1

# Below this, a description is a stub/placeholder, not worth a page. Any real
# scraped notice text clears this by a wide margin (median observed: ~800 chars).
MIN_DESCRIPTION_LEN = 60


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


def iso_date(iso: str | None) -> str | None:
    """YYYY-MM-DD for schema.org date fields (Offer.priceValidUntil). None if unparseable."""
    if not iso:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def fetch_json(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    resp = client.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


_PAGE_SIZE = 200  # API max per page (_PROPERTIES_MAX_LIMIT in api/properties/router.py)


def iter_candidates(client: httpx.Client, city: str):
    """Auctions in one city, paginated, live-first (the "upcoming" sort's
    ordering — CASE WHEN in api/properties/router.py puts future
    auction_start_dt before past, not a live-only filter, so this naturally
    spills into expired auctions if a city needs more than its live count
    to satisfy --limit).

    Real content (fields.description) is near-universal regardless of
    enrichment status — see the module docstring — so a single pass here is
    enough; no separate live/expired querying needed.
    """
    offset = 0
    while True:
        data = fetch_json(client, "/properties", {
            "district": city, "sort": "upcoming", "limit": _PAGE_SIZE, "offset": offset,
        })
        results = data.get("results", [])
        if not results:
            return
        yield from results
        offset += _PAGE_SIZE
        if offset >= data.get("total", 0):
            return
        time.sleep(REQUEST_INTERVAL_S)


def iter_all_live_candidates(client: httpx.Client):
    """Every LIVE auction across all districts, paginated, soonest-first.

    `date_from=now` filters to auctions whose auction_start_dt is still in the
    future (the same live-only filter build_landing_pages uses); omitting
    `district` spans all of Tamil Nadu, not just the pilot cities. This is what
    `--all` walks: the full live inventory, so every upcoming auction with real
    content gets a crawlable page — not a per-city sample.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    offset = 0
    while True:
        data = fetch_json(client, "/properties", {
            "sort": "upcoming", "date_from": now_iso,
            "limit": _PAGE_SIZE, "offset": offset,
        })
        results = data.get("results", [])
        if not results:
            return
        yield from results
        offset += _PAGE_SIZE
        if offset >= data.get("total", 0):
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


def slugify(name: str) -> str:
    """Match scripts/build_landing_pages.slugify so a property's breadcrumb links
    to the same /bank-auctions/<slug> hub the landing builder emits."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def jsonld_description(fields: dict, rel: dict, ended: bool, cap: int = 600) -> str:
    """Prefer the real notice text (richer for AI/snippets) over the 158-char
    meta description, collapsed to one line and capped. Falls back to the meta
    description when there's no usable notice text."""
    raw = " ".join((fields.get("description") or "").split())
    if len(raw) >= MIN_DESCRIPTION_LEN:
        return raw[:cap].rstrip()
    return seo_description(fields, rel, ended)


def postal_address(fields: dict, rel: dict) -> dict | None:
    """City-level PostalAddress from the record. Area (neighbourhood) is left in
    the title/description rather than forced into a street field it doesn't fit."""
    city = (rel.get("city") or {}).get("name") or fields.get("district")
    if not city:
        return None
    return {
        "@type": "PostalAddress",
        "addressLocality": city,
        "addressRegion": "Tamil Nadu",
        "addressCountry": "IN",
    }


def breadcrumb_trail(auction_id: str, fields: dict, rel: dict, name: str) -> dict:
    """Home › Bank Auctions › <City> (only when that hub page exists) › this page.
    The top /bank-auctions hub always exists; the city crumb is added only when
    web/bank-auctions/<slug>/ was generated, so no crumb ever links to a 404."""
    url = f"{SITE_BASE}/property/{auction_id}"
    crumbs = [("Home", f"{SITE_BASE}/"), ("Bank Auctions", f"{SITE_BASE}/bank-auctions")]
    city = (rel.get("city") or {}).get("name") or fields.get("district")
    if city and (WEB_DIR / "bank-auctions" / slugify(city)).is_dir():
        crumbs.append((city, f"{SITE_BASE}/bank-auctions/{slugify(city)}"))
    crumbs.append((name, url))
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": link}
            for i, (n, link) in enumerate(crumbs)
        ],
    }


def property_jsonld(auction_id: str, fields: dict, rel: dict, ended: bool) -> list[dict]:
    """schema.org JSON-LD for a property page — Move 3 of the SEO plan
    (docs/marketing/plan.md §4): structured data for price rich results and
    AI-answer eligibility.

    Honesty rule (docs/marketing/copy-playbook.md) carries into the markup:
    every value comes from the record, nothing invented, and an ended auction
    is never implied to still be biddable.

      · LIVE auction with a reserve price → Product + Offer (the shape Google
        renders a price for), additionally typed as a RealEstateListing. The
        reserve is the Offer.price (the floor / minimum bid).
      · ENDED auction (or one with no reserve figure) → a plain
        RealEstateListing describing the historical page, with NO live Offer —
        emitting an in-stock Offer on a closed round would misrepresent it.
      · BreadcrumbList is always emitted.

    Returns the list of top-level nodes; render_page serialises each into its
    own <script type="application/ld+json"> block (same convention as the
    landing-page builder, scripts/build_landing_pages.py).
    """
    url = f"{SITE_BASE}/property/{auction_id}"
    name = fields.get("title") or "Bank auction property"
    desc = jsonld_description(fields, rel, ended)
    bank = (rel.get("bank") or {}).get("name") or ""
    ptype = (rel.get("property_types") or [None])[0]
    reserve = fields.get("reserve_price_num")
    emd = fields.get("emd_num")
    address = postal_address(fields, rel)

    # Honest extra facts that don't fit price/description — every value from the record.
    extra: list[dict] = [{"@type": "PropertyValue", "name": "Sale type", "value": "SARFAESI bank e-auction"}]
    if ptype:
        extra.append({"@type": "PropertyValue", "name": "Property type", "value": ptype})
    if emd:
        extra.append({"@type": "PropertyValue", "name": "EMD (earnest money deposit)",
                      "value": int(emd), "unitText": "INR"})
    auction_date = fmt_date(fields.get("auction_start_dt"))
    if auction_date:
        extra.append({"@type": "PropertyValue", "name": "Auction date", "value": auction_date})

    blocks: list[dict] = []

    if not ended and reserve:
        offer: dict = {
            "@type": "Offer",
            # The reserve is the floor / minimum bid, not a fixed sale price — said plainly.
            "description": "Reserve price — the minimum bid for this bank e-auction.",
            "price": int(reserve),
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock",
            "url": url,
        }
        starts = iso_date(fields.get("auction_start_dt"))
        ends = iso_date(fields.get("application_deadline_dt") or fields.get("auction_start_dt"))
        if ends:
            offer["priceValidUntil"] = ends
            offer["availabilityEnds"] = ends
        if starts:
            offer["availabilityStarts"] = starts
        if bank:
            offer["seller"] = {"@type": "Organization", "name": bank}
        if address:
            offer["availableAtOrFrom"] = {"@type": "Place", "name": name, "address": address}
        product: dict = {
            "@context": "https://schema.org",
            "@type": "Product",
            "additionalType": "https://schema.org/RealEstateListing",
            "name": name,
            "description": desc,
            "sku": auction_id,
            "url": url,
            "image": f"{SITE_BASE}/og-image.png",
            "additionalProperty": extra,
            "offers": offer,
        }
        if ptype:
            product["category"] = ptype
        blocks.append(product)
    else:
        # Closed round (or no reserve figure): describe the historical listing with
        # no live Offer. Location + facts still attach via about/additionalProperty.
        listing: dict = {
            "@context": "https://schema.org",
            "@type": "RealEstateListing",
            "name": name,
            "description": desc,
            "url": url,
            "additionalProperty": extra,
        }
        if reserve:
            extra.insert(0, {"@type": "PropertyValue", "name": "Closing reserve price",
                             "value": int(reserve), "unitText": "INR"})
        if address:
            listing["about"] = {"@type": "Place", "name": name, "address": address}
        blocks.append(listing)

    blocks.append(breadcrumb_trail(auction_id, fields, rel, name))
    return blocks


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

    # Move 3 — structured data. Inject one <script> per JSON-LD node just before
    # </head> (json.dumps handles escaping, so this is not run through html.escape).
    jsonld = "\n".join(
        f'<script type="application/ld+json">{json.dumps(b, ensure_ascii=False)}</script>'
        for b in property_jsonld(auction_id, fields, rel, ended)
    )
    out = out.replace("</head>", f"{jsonld}\n</head>", 1)

    ssr_block = build_ssr_block(fields, rel, ended)
    out = out.replace("<body>\n", f"<body>\n{ssr_block}\n", 1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", action="append", default=[],
                         help="Pilot city to prerender (repeatable). Defaults to Chennai + Kanchipuram.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Max pages. Per-city default: 25. In --all mode a global cap; "
                              "omit or pass 0 for unbounded (every live auction).")
    parser.add_argument("--all", action="store_true",
                         help="Prerender ALL live inventory across every district (not just "
                              "--city). Cityless + live-only; see iter_all_live_candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files, just report what would happen.")
    args = parser.parse_args(argv)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    generated: list[str] = []
    counts = {"incomplete": 0, "error": 0}

    def emit(client: httpx.Client, row: dict) -> bool:
        """Fetch detail, gate on real content, and write the page. Returns True
        only when a page was generated (so callers can count successes)."""
        auction_id = row["auction_id"]
        time.sleep(REQUEST_INTERVAL_S)
        detail = fetch_detail(client, auction_id)
        if detail is None:
            counts["error"] += 1
            return False
        fields = detail.get("fields", {})
        rel = detail.get("relationships", {})
        if len((fields.get("description") or "").strip()) < MIN_DESCRIPTION_LEN:
            counts["incomplete"] += 1
            return False
        page = render_page(template, auction_id, fields, rel)
        out_path = OUT_ROOT / auction_id / "index.html"
        if args.dry_run:
            print(f"  [dry-run] would write {out_path.relative_to(REPO_ROOT)}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(page, encoding="utf-8")
            print(f"  wrote {out_path.relative_to(REPO_ROOT)}")
        generated.append(auction_id)
        return True

    with httpx.Client() as client:
        if args.all:
            cap = args.limit if (args.limit and args.limit > 0) else None
            print(f"== all live inventory (all districts)"
                  f"{f', cap {cap}' if cap else ''} ==")
            checked = 0
            for row in iter_all_live_candidates(client):
                if cap is not None and len(generated) >= cap:
                    print(f"  reached cap {cap} — stopping (more live inventory remains)")
                    break
                checked += 1
                emit(client, row)
            print(f"  {len(generated)} generated after checking {checked} live candidates")
        else:
            per_city = args.limit if (args.limit and args.limit > 0) else 25
            cities = args.city or ["Chennai", "Kanchipuram"]
            for city in cities:
                print(f"== {city} ==")
                found_for_city = 0
                checked = 0
                for row in iter_candidates(client, city):
                    if found_for_city >= per_city:
                        break
                    checked += 1
                    if emit(client, row):
                        found_for_city += 1
                print(f"  {found_for_city}/{per_city} found after checking {checked} candidates")

    print(f"\n{len(generated)} pages generated, {counts['incomplete']} skipped "
          f"(description too short), {counts['error']} skipped (fetch error)")

    if not args.dry_run and generated:
        # Shared builder scans the whole tree (property + landing pages), so a
        # narrow --city re-run never shrinks the sitemap or drops the
        # bank-auctions/** landing pages another script generated.
        count = seo_sitemap.write_sitemap(WEB_DIR)
        print(f"sitemap.xml rebuilt — {count} URLs total")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
