"""
scripts/build_compare.py
------------------------
Move 6 of the SEO plan (docs/marketing/plan.md §4) — comparison / alternative
pages for high-intent "vs" and "alternative" searches.

Generates:

    /compare/                              hub — AuctionScope vs the portals
    /compare/auctionscope-vs-<competitor>  one comparison page

HONESTY IS THE POINT (docs/marketing/copy-playbook.md, .agents/product-marketing.md):
AuctionScope is not a bigger listing portal and it is not where you place a bid.
The incumbents (IBAPI, BAANKNET, bankeauctions.com) are national listing/auction
platforms — two of them official government-backed rails. AuctionScope is a
Tamil-Nadu-focused AI *search and evaluation* layer: it helps you find and
size up a property, then you bid on the official portal. Every page credits the
incumbent's real strengths, never disparages, invents no numbers, and repeats
that AuctionScope does not do legal/title diligence (only the sale notice is
held). "Words to avoid": due diligence, title-clear, guaranteed, etc.

Each page carries Article + FAQPage + BreadcrumbList JSON-LD and cites the
competitor's official site. Brand shell, theme handling and email capture are
reused from build_landing_pages so these match the guides and landing pages.

Usage:
    python -m scripts.build_compare            # write pages + rebuild sitemap
    python -m scripts.build_compare --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from scripts.build_landing_pages import PAGE_CSS, CAPTURE_SCRIPT, SITE_BASE, THEME_INIT
from scripts.build_guides import GUIDE_CSS
from scripts import seo_sitemap

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
OUT_ROOT = WEB_DIR / "compare"

UPDATED = "2026-07-21"

# Extra styles for the three-column comparison table (AuctionScope column tinted).
COMPARE_CSS = """
table.cmp{border-collapse:collapse;width:100%;margin:0 0 22px;font-size:14.5px}
table.cmp th,table.cmp td{border:1px solid var(--border);padding:10px 12px;text-align:left;vertical-align:top}
table.cmp thead th{background:var(--card);font-weight:700}
table.cmp th[scope=row]{font-weight:600;white-space:nowrap}
table.cmp .us{background:var(--accent-soft)}
table.cmp thead th.us{color:var(--accent)}
.tldr{background:var(--accent-soft);border:1px solid var(--border);border-radius:var(--radius);
padding:16px 18px;font-size:16px;margin:0 0 8px}
"""

# --------------------------------------------------------------------------- #
# Competitor profiles — facts only, each with an official source. Strengths are
# genuine; gaps describe what the tool is *not built to do*, not slurs.
# --------------------------------------------------------------------------- #
COMPETITORS = {
    "ibapi": {
        "name": "IBAPI",
        "full": "IBAPI (Indian Banks Auctions Mortgaged Properties Information)",
        "url": "https://ibapi.in/",
        "one_line": ("the official, government-backed national portal that lists mortgaged properties "
                     "banks have put up for e-auction across India"),
        "about": ("IBAPI is an initiative of the Indian Banks' Association under the Department of "
                  "Financial Services, Ministry of Finance — a common, official portal that displays "
                  "mortgaged properties available for auction from banks across India. It is free to "
                  "browse and register on, with filters by property type, state, district and city."),
        "strengths": [
            "Official and government-backed — an authoritative national source",
            "Free to browse and register",
            "Nationwide coverage across many banks",
            "Filters by property type, state, district and city",
        ],
        "gaps": [
            "Listings only — no plain-English search or synthesis across auctions",
            "No help evaluating a property: nothing on flood risk, connectivity, nearby projects or whether the reserve is fair vs the local market",
            "You read the dense sale notices yourself",
        ],
        "bids_where": "You use IBAPI to find properties, then bid on the bank's designated e-auction platform.",
        "best_for": "buyers who want the authoritative national list of what banks are auctioning, straight from the source",
        "source": {"t": "IBAPI — Indian Banks Auctions Mortgaged Properties Information (official portal)",
                    "h": "https://ibapi.in/"},
    },
    "baanknet": {
        "name": "BAANKNET",
        "full": "BAANKNET (Bank Asset Auction Network)",
        "url": "https://baanknet.com/",
        "one_line": ("the official e-auction platform of India's public sector banks for selling "
                     "non-performing assets"),
        "about": ("BAANKNET, run under the PSB Alliance, is a government-backed e-auction platform built "
                  "for India's public sector banks to sell non-performing assets. Unlike a listings-only "
                  "site, it is a full transaction platform — search to sale — with integrated KYC and "
                  "secure payment, covering property across India."),
        "strengths": [
            "Official PSB platform — you actually bid here, with integrated KYC and payments",
            "Government-backed and national in scope",
            "End-to-end: search, register and transact in one place",
            "Free to browse",
        ],
        "gaps": [
            "Built for the transaction, not for research — no plain-English search or cross-auction synthesis",
            "No evaluation of a property's location, flood risk, connectivity or price-vs-market",
            "Focused on public sector bank assets",
        ],
        "bids_where": "BAANKNET is itself the bidding platform for participating public sector banks.",
        "best_for": "bidding on public sector bank NPAs through the official channel, end to end",
        "source": {"t": "BAANKNET — Bank Asset Auction Network, PSB Alliance (official portal)",
                    "h": "https://baanknet.com/"},
    },
    "bankeauctions": {
        "name": "BankeAuctions",
        "full": "bankeauctions.com",
        "url": "https://www.bankeauctions.com/",
        "one_line": ("a long-established national e-auction service used by many public and private "
                     "sector banks for foreclosed-property auctions"),
        "about": ("bankeauctions.com is a widely used private e-auction service that many public and "
                  "private sector banks use to auction foreclosed (NPA) properties across India. It "
                  "aggregates listings and runs the online bidding for participating banks."),
        "strengths": [
            "Wide national coverage across many public and private banks",
            "An established e-auction service — you can bid through it",
            "Aggregates listings from numerous institutions",
        ],
        "gaps": [
            "A listings-and-bidding service, not a research tool — no plain-English search or synthesis",
            "No location or price evaluation to tell you whether a property is worth bidding on",
            "You still read and interpret each sale notice yourself",
        ],
        "bids_where": "bankeauctions.com hosts the online bidding for the banks that use it.",
        "best_for": "browsing and bidding on a broad national pool of bank auctions across many banks",
        "source": {"t": "bankeauctions.com — national bank e-auction service",
                    "h": "https://www.bankeauctions.com/"},
    },
    "eauctionsindia": {
        "name": "eAuctionsIndia",
        "full": "eauctionsindia.com",
        "url": "https://www.eauctionsindia.com/",
        "one_line": ("a large national aggregator that collects bank e-auction listings from hundreds of "
                     "banks into one searchable database"),
        "about": ("eauctionsindia.com aggregates bank e-auction listings — NPA, DRT and foreclosure "
                  "properties, plus vehicles and other assets — from hundreds of banks across India into a "
                  "single searchable site. It offers filters by location, bank, category and price, and "
                  "shows the key listing details: reserve price, EMD, auction date and bank contact."),
        "strengths": [
            "Very broad national inventory across hundreds of banks and many asset types",
            "Search filters by location, bank, category and price",
            "Shows the key listing details — reserve, EMD, auction date, bank contact",
            "Updated regularly across the country",
        ],
        "gaps": [
            "An aggregator of listings — no plain-English search or synthesis across auctions",
            "No evaluation of a property's location, flood risk, connectivity or price-vs-market",
            "You read and interpret each dense sale notice yourself",
            "Breadth across the whole country rather than depth on one geography",
        ],
        "bids_where": ("You register and bid with the bank (KYC and EMD) on its e-auction platform; the "
                       "site points you to the listing and the bank's details."),
        "best_for": "browsing a very broad national pool of bank auctions across many banks and asset types",
        "source": {"t": "eauctionsindia.com — national bank-auction listings aggregator",
                    "h": "https://www.eauctionsindia.com/"},
    },
}

# Shared "who AuctionScope is for / not for" so it stays consistent + honest.
US_BEST_FOR = ("buyers focused on Tamil Nadu who want to search auctions in plain English and evaluate a "
               "specific property — location, flood risk, connectivity, and whether the reserve looks fair "
               "vs the local market — before they bid")
US_NOT_FOR = ("buyers who need nationwide coverage today, or who want a single place to also place the bid "
              "— AuctionScope covers Tamil Nadu and is a research layer, not the bidding platform")


def at_a_glance(c: dict) -> str:
    """Honest three-column comparison. No invented figures; the AuctionScope
    column states its real, narrower scope and the ₹499/30-day Pro option."""
    rows = [
        ("Coverage", "Tamil Nadu SARFAESI auctions (all 38 districts)", "National, many banks"),
        ("Cost", "Free to search; optional Pro at &#8377;499 / 30 days for heavier AI use", "Free to browse"),
        ("Plain-English search", "Yes — ask in natural language", "No — filters only"),
        ("Evaluate location &amp; price (flood, connectivity, reserve vs market)",
         "Yes — AI researches the web and answers, cited", "No"),
        ("Where you place the bid", "On the official portal — AuctionScope is not a bidding platform",
         html.escape(c["bids_where"])),
        ("Best for", html.escape(US_BEST_FOR), html.escape(c["best_for"])),
    ]
    body = "".join(
        f'<tr><th scope="row">{label}</th><td class="us">{us}</td><td>{them}</td></tr>'
        for label, us, them in rows
    )
    return (
        '<table class="cmp"><thead><tr><th></th>'
        '<th class="us" scope="col">AuctionScope</th>'
        f'<th scope="col">{html.escape(c["name"])}</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _article_jsonld(title: str, desc: str, url: str, source: dict) -> dict:
    return {
        "@context": "https://schema.org", "@type": "Article",
        "headline": title, "description": desc,
        "datePublished": UPDATED, "dateModified": UPDATED,
        "author": {"@type": "Organization", "name": "AuctionScope"},
        "publisher": {"@type": "Organization", "name": "AuctionScope", "url": f"{SITE_BASE}/"},
        "mainEntityOfPage": url,
        "citation": [{"@type": "CreativeWork", "name": source["t"], "url": source["h"]}],
    }


def _faq_jsonld(faqs: list[dict]) -> dict:
    return {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
            for f in faqs
        ],
    }


def _breadcrumb_jsonld(trail: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": f"{SITE_BASE}{path}"}
            for i, (name, path) in enumerate(trail)
        ],
    }


def _head(title: str, desc: str, url: str, jsonld: list[dict]) -> str:
    blocks = "\n".join(
        f'<script type="application/ld+json">{json.dumps(b, ensure_ascii=False)}</script>'
        for b in jsonld
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{THEME_INIT}
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
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
<style>{PAGE_CSS}{GUIDE_CSS}{COMPARE_CSS}</style>
{blocks}
</head>
<body>
<header class="top"><a class="brand" href="/">auctionscope</a><a href="/">search all auctions →</a></header>
<div class="wrap">"""


FOOT = ("""<p class="note">Comparisons reflect publicly available information about each portal and were
correct to the best of our knowledge on the date shown; verify current features on each provider's site.
Auctionscope is an information and research tool, not a bank, broker or legal adviser, and does not do
legal or title diligence — always verify a property with the bank and the official sale notice before
bidding.</p>
</div>""" + CAPTURE_SCRIPT + "</body></html>")


def _cta_and_capture(source: str) -> str:
    return (
        '<p style="margin:28px 0 4px"><a class="cta" href="/">try a plain-English auction search →</a></p>'
        '<section class="capture">'
        "<h2>get new Tamil Nadu auctions by email</h2>"
        "<p>new listings, price drops and closing deadlines. no spam, unsubscribe anytime.</p>"
        f'<form id="ac-form" data-source="{html.escape(source)}" data-label="auctions">'
        '<input id="ac-email" type="email" placeholder="you@email.com" autocomplete="email" required aria-label="your email">'
        '<button id="ac-btn" type="submit">notify me</button>'
        "</form>"
        '<p id="ac-msg" class="capture-msg" role="status" aria-live="polite" hidden></p>'
        "</section>"
    )


def render_vs(key: str, c: dict) -> str:
    slug = f"auctionscope-vs-{key}"
    url = f"{SITE_BASE}/compare/{slug}"
    name = c["name"]
    title = f"AuctionScope vs {name} — an honest comparison"
    desc = (f"How AuctionScope's AI search and evaluation compares to {name}. What each does best, "
            f"who should use which, and why they work well together.")
    trail = [("Home", "/"), ("Compare", "/compare"), (f"vs {name}", f"/compare/{slug}")]

    faqs = [
        {"q": f"Is AuctionScope an alternative to {name}?",
         "a": (f"Not a replacement — a complement. {name} is where you find (and, where applicable, bid on) "
               f"listings; AuctionScope helps you search Tamil Nadu auctions in plain English and evaluate a "
               f"property's location and price before you bid. Many buyers use both.")},
        {"q": f"Can I bid on a property through AuctionScope instead of {name}?",
         "a": ("No. AuctionScope is a search and evaluation tool, not a bidding platform. You place the bid "
               "on the official portal; AuctionScope helps you decide which property is worth bidding on.")},
        {"q": f"Does AuctionScope cover the same properties as {name}?",
         "a": (f"AuctionScope focuses on Tamil Nadu SARFAESI auctions, while {name} is national. For a Tamil "
               f"Nadu property you may see it in both; AuctionScope adds plain-English search and evaluation "
               f"on top.")},
        {"q": "Does AuctionScope do legal or title verification?",
         "a": ("No. It researches location, connectivity and market context and surfaces what the sale "
               "notice says — it does not do legal or title diligence. Verify the title with the bank and a "
               "professional before bidding.")},
    ]
    jsonld = [_article_jsonld(title, desc, url, c["source"]),
              _faq_jsonld(faqs), _breadcrumb_jsonld(trail)]

    faq_html = "".join(
        f"<details><summary>{html.escape(f['q'])}</summary><p>{html.escape(f['a'])}</p></details>"
        for f in faqs
    )
    strengths = "".join(f"<li>{html.escape(s)}</li>" for s in c["strengths"])
    gaps = "".join(f"<li>{html.escape(g)}</li>" for g in c["gaps"])

    parts = [
        _head(title, desc, url, jsonld),
        f'<nav class="crumb"><a href="/">home</a> / <a href="/compare">compare</a> / vs {html.escape(name)}</nav>',
        f'<article class="guide"><h1>AuctionScope vs {html.escape(name)}</h1>',
        f'<p class="updated">last updated {UPDATED}</p>',
        f'<p class="tldr">{html.escape(name)} is {html.escape(c["one_line"])}. AuctionScope is a '
        'Tamil-Nadu-focused AI tool for searching auctions in plain English and evaluating a property '
        '(location, flood risk, connectivity, reserve vs market) before you bid. They are not either/or: '
        f'evaluate on AuctionScope, then bid through {html.escape(name)} or the bank\'s official channel.</p>',
        "<h2>At a glance</h2>",
        at_a_glance(c),
        f"<h2>What {html.escape(name)} does well</h2>",
        f"<p>{html.escape(c['about'])}</p><ul>{strengths}</ul>",
        f"<h2>Where {html.escape(name)} isn't built to help</h2>",
        f"<p>These aren't faults — they're just outside what a listing/auction portal sets out to do:</p><ul>{gaps}</ul>",
        "<h2>Where AuctionScope fits</h2>",
        "<p>AuctionScope sits a step earlier in the journey — the research and decision. You search Tamil "
        "Nadu auctions in plain English, and for any property you can ask the questions a sale notice can't "
        "answer: does the area flood, what's nearby, how far is it, and does the reserve look fair against "
        "local rates. The answers are researched from the web and cited, and every figure from our own data "
        "is grounded, never invented. It does not replace the official portal — you still bid there.</p>",
        "<h2>Who should use which</h2>",
        f'<table class="kv"><tr><th>Use {html.escape(name)}</th><td>{html.escape(c["best_for"])}.</td></tr>'
        f'<tr><th>Use AuctionScope</th><td>{html.escape(US_BEST_FOR)}.</td></tr></table>'
        f'<p>Honestly: AuctionScope is not for {html.escape(US_NOT_FOR)}. For that, {html.escape(name)} is '
        'the right tool — and the two work well together.</p>',
        f'<h2>Frequently asked questions</h2><div class="faq">{faq_html}</div>',
        f'<h2>Sources</h2><p class="updated">Verify current features on each provider\'s own site.</p>'
        f'<ul><li><a href="{html.escape(c["source"]["h"])}" rel="nofollow noopener" target="_blank">'
        f'{html.escape(c["source"]["t"])}</a></li></ul>',
        "</article>",
        _cta_and_capture(f"compare-{key}"),
        FOOT,
    ]
    return "\n".join(parts)


def render_hub(keys: list[str]) -> str:
    url = f"{SITE_BASE}/compare"
    title = "AuctionScope vs the bank-auction portals — honest comparisons"
    desc = ("How AuctionScope's AI search and evaluation compares to IBAPI, BAANKNET and bankeauctions.com "
            "— what each does best, and how they work together.")
    trail = [("Home", "/"), ("Compare", "/compare")]
    faqs = [
        {"q": "Is AuctionScope a bank auction listing portal?",
         "a": ("Not exactly. The national portals (IBAPI, BAANKNET, bankeauctions.com) list and run "
               "auctions. AuctionScope is a Tamil-Nadu-focused layer on top that lets you search in plain "
               "English and evaluate a property before you bid on the official portal.")},
        {"q": "Which bank auction portal is best?",
         "a": ("It depends on what you need. For the official national list, IBAPI; to bid on public sector "
               "bank assets, BAANKNET; for a broad multi-bank pool, bankeauctions.com; and to search and "
               "evaluate Tamil Nadu auctions in plain English before bidding, AuctionScope alongside them.")},
    ]
    jsonld = [_faq_jsonld(faqs), _breadcrumb_jsonld(trail)]
    cards = "".join(
        f'<a class="cardlink" href="/compare/auctionscope-vs-{k}">'
        f'<div class="t">AuctionScope vs {html.escape(COMPETITORS[k]["name"])}</div>'
        f'<div class="m">{html.escape(COMPETITORS[k]["one_line"].capitalize())}.</div></a>'
        for k in keys
    )
    faq_html = "".join(
        f"<details><summary>{html.escape(f['q'])}</summary><p>{html.escape(f['a'])}</p></details>"
        for f in faqs
    )
    parts = [
        _head(title, desc, url, jsonld),
        '<nav class="crumb"><a href="/">home</a> / compare</nav>',
        "<h1>AuctionScope vs the bank-auction portals</h1>",
        '<p class="lede">The national portals list and run bank auctions. AuctionScope is a '
        'Tamil-Nadu-focused AI layer for searching in plain English and evaluating a property before you '
        'bid. Honest comparisons — what each does best, and how they work together. '
        'New here? <a href="/guides">Read the bank auction guides</a> or '
        '<a href="/bank-auctions">browse auctions by city</a>.</p>',
        f'<div class="grid">{cards}</div>',
        f'<h2>Frequently asked questions</h2><div class="faq">{faq_html}</div>',
        _cta_and_capture("compare-hub"),
        FOOT,
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    args = parser.parse_args(argv)

    keys = list(COMPETITORS)
    pages = [(OUT_ROOT / "index.html", render_hub(keys))]
    pages += [(OUT_ROOT / f"auctionscope-vs-{k}" / "index.html", render_vs(k, COMPETITORS[k])) for k in keys]

    for path, content in pages:
        if args.dry_run:
            print(f"  [dry-run] would write {path.relative_to(REPO_ROOT)} ({len(content)} bytes)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"  wrote {path.relative_to(REPO_ROOT)}")

    print(f"\n{len(pages)} compare pages ({len(keys)} comparisons + hub)")
    if not args.dry_run:
        count = seo_sitemap.write_sitemap(WEB_DIR)
        print(f"sitemap.xml rebuilt — {count} URLs total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
