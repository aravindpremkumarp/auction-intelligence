"""
scripts/build_guides.py
-----------------------
Move 4 of the SEO plan (docs/marketing/plan.md §4) — the educational content
hub, Ring 1 (bank-auction core) of the syllabus in docs/marketing/content-pillars.md.

Generates standalone, crawlable long-form guide pages:

    /guides/                 hub — lists every guide
    /guides/<slug>/          one guide

Each guide page carries Article + FAQPage + BreadcrumbList JSON-LD so it is
eligible for article rich results and — the real point of Move 4/5 — extractable
and citable by AI search (ChatGPT / Perplexity / AI Overviews). The renderer is
generic: adding a topic is just another entry in GUIDES below.

Content rules (same as the rest of the site, docs/marketing/copy-playbook.md):
lowercase-calm voice in the chrome, no invented numbers, banned words
("guaranteed", "due diligence", "title-clear") avoided, and the anchor rule —
every guide ties back to evaluation and links to the product.

Brand CSS and the email-capture behaviour are imported from build_landing_pages
so guides stay visually identical to the programmatic landing pages and there is
one source of truth for both.

Usage:
    python -m scripts.build_guides            # write pages + rebuild sitemap
    python -m scripts.build_guides --dry-run  # report only
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from scripts.build_landing_pages import PAGE_CSS, CAPTURE_SCRIPT, SITE_BASE, slugify
from scripts import seo_sitemap

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
OUT_ROOT = WEB_DIR / "guides"

# Prose styles layered on top of the shared PAGE_CSS token system.
GUIDE_CSS = """
article.guide{max-width:72ch}
article.guide p{margin:0 0 16px;font-size:16px}
article.guide h2{font-size:20px;margin:32px 0 12px}
article.guide h3{font-size:16px;margin:24px 0 8px}
article.guide ul,article.guide ol{margin:0 0 16px;padding-left:22px}
article.guide li{margin:0 0 8px}
.answer{background:var(--accent-soft);border:1px solid var(--border);
border-radius:var(--radius);padding:16px 18px;font-size:16px;margin:0 0 8px}
.updated{color:var(--muted);font-size:12.5px;margin:6px 0 24px}
.faq{margin:12px 0 8px}
.faq details{border:1px solid var(--border);border-radius:var(--radius);
background:var(--card);padding:2px 16px;margin:0 0 10px}
.faq summary{font-weight:600;font-size:15px;cursor:pointer;padding:14px 0}
.faq details p{margin:0 0 14px}
table.kv{border-collapse:collapse;margin:0 0 18px;width:100%}
table.kv td,table.kv th{border:1px solid var(--border);padding:9px 12px;
text-align:left;font-size:14.5px;vertical-align:top}
table.kv th{background:var(--card);white-space:nowrap}
"""


# --------------------------------------------------------------------------- #
# Content. One dict per guide. `body` and answers are trusted authored HTML/text
# (not user input), so they are emitted verbatim; titles/descriptions are escaped.
# --------------------------------------------------------------------------- #
GUIDES = [
    {
        "slug": "what-is-emd-in-a-bank-auction",
        "title": "What is EMD in a bank auction — and how refunds work",
        "h1": "What is EMD in a bank auction?",
        "description": ("EMD (earnest money deposit) is the refundable deposit you pay to bid in a "
                        "bank e-auction. What it costs, when it is refunded, and when it is forfeited."),
        "updated": "2026-07-21",
        "answer": ("EMD — earnest money deposit — is a refundable deposit you pay to take part in a "
                   "bank e-auction. It is commonly around 10% of the property's reserve price, though "
                   "the exact figure is set in each sale notice. Lose the auction and it is refunded; "
                   "win and it counts towards your purchase; win but fail to pay the balance and you "
                   "forfeit it."),
        "body": """
<h2>Why banks ask for an EMD</h2>
<p>A bank e-auction under the SARFAESI Act sells a property a borrower pledged as security and then
defaulted on. The earnest money deposit is how the bank filters serious bidders from browsers: you
put money down before the auction to show you intend to bid and can pay. It is a deposit, not a fee —
for everyone except a defaulting winner, it comes back.</p>

<h2>How much is the EMD?</h2>
<p>The amount is fixed per property and stated in the sale notice. In practice it is commonly about
<strong>10% of the reserve price</strong> (the minimum bid), but treat that as a rule of thumb, not a
guarantee — always read the figure in the notice for the specific property. On a property with a
₹25,00,000 reserve, a 10% EMD is ₹2,50,000.</p>

<h2>How and when you pay it</h2>
<ul>
<li><strong>Before the auction.</strong> The notice sets a last date and time to submit the EMD — miss
it and you cannot bid.</li>
<li><strong>To the account in the notice.</strong> Payment is usually by online transfer (NEFT / RTGS)
to the account the sale notice specifies, or by demand draft, along with your KYC documents.</li>
<li><strong>Once cleared,</strong> the bank or the auction platform issues your login to bid in the
online auction.</li>
</ul>

<h2>What happens to your EMD after the auction</h2>
<table class="kv">
<tr><th>If you…</th><th>Then your EMD…</th></tr>
<tr><td>Do not win</td><td>Is refunded, typically to the same account within a few working days, without interest.</td></tr>
<tr><td>Win the auction</td><td>Is adjusted towards the sale price — it becomes part of your payment, not an extra cost.</td></tr>
<tr><td>Win but fail to pay the balance</td><td>Is forfeited to the bank, and the property can be re-auctioned.</td></tr>
</table>
<p>Under the Security Interest (Enforcement) Rules, a winning bidder pays 25% of the sale price
(the EMD counts towards this) immediately or by the next working day, and the balance 75% within
15 days — a period the bank may extend in writing. Exact timelines and payment terms vary by notice,
so confirm them with the bank before you bid.</p>

<h2>Before you commit an EMD, check the property</h2>
<p>An EMD is real money at risk on a property sold <em>as-is-where-is</em>: the bank makes no promise
about its condition, occupancy or encumbrances. Before you put money down, it is worth checking the
things a sale notice does not spell out — whether the reserve is actually below local market rates,
what the location is like, whether possession is physical or only symbolic, and what dues might ride
along with the property. That verification step is exactly what AuctionScope is built to help with.</p>
""",
        "faqs": [
            {"q": "Is the EMD refundable?",
             "a": ("Yes. If you do not win the auction, the EMD is refunded — normally to the same "
                   "account you paid from, within a few working days, without interest. You only lose "
                   "it if you win and then fail to pay the balance within the stipulated time.")},
            {"q": "How much EMD do I need to pay for a bank auction?",
             "a": ("It is set per property in the sale notice and is commonly about 10% of the reserve "
                   "price, but the exact amount is whatever the notice states — always check it for the "
                   "specific property.")},
            {"q": "Does the EMD count towards the purchase price if I win?",
             "a": ("Yes. For the winning bidder the EMD is adjusted against the sale price — it becomes "
                   "part of the 25% due immediately, not an additional cost on top of your bid.")},
            {"q": "Can I lose my EMD?",
             "a": ("Only if you win the auction and then fail to pay the balance within the time the "
                   "rules and the notice allow. In that case the deposit is forfeited to the bank and "
                   "the property may be re-auctioned.")},
        ],
        "related": ["reserve price", "SARFAESI", "as-is-where-is", "symbolic vs physical possession"],
    },
]


def _article_jsonld(g: dict, url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": g["title"],
        "description": g["description"],
        "datePublished": g["updated"],
        "dateModified": g["updated"],
        # No named author (the operator identity is undecided — plan §13); an
        # Organization author is the honest attribution rather than a fabricated person.
        "author": {"@type": "Organization", "name": "AuctionScope"},
        "publisher": {"@type": "Organization", "name": "AuctionScope", "url": f"{SITE_BASE}/"},
        "mainEntityOfPage": url,
    }


def _faq_jsonld(g: dict) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
            for f in g["faqs"]
        ],
    }


def _breadcrumb_jsonld(trail: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
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
<meta name="twitter:title" content="{html.escape(title)}">
<meta name="twitter:description" content="{html.escape(desc)}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{PAGE_CSS}{GUIDE_CSS}</style>
{blocks}
</head>
<body>
<header class="top"><a class="brand" href="/">auctionscope</a><a href="/">search all auctions →</a></header>
<div class="wrap">"""


FOOT = ("""<p class="note">Auctionscope is an information platform, not a bank, broker or legal
adviser. Bank e-auctions run under the SARFAESI Act; always verify the reserve price, EMD, possession
type, encumbrances and payment terms in the official sale notice and with the bank before bidding.</p>
</div>""" + CAPTURE_SCRIPT + "</body></html>")


def _cta_and_capture(source: str) -> str:
    return (
        '<p style="margin:28px 0 4px"><a class="cta" href="/">browse live Tamil Nadu bank auctions →</a></p>'
        '<section class="capture">'
        "<h2>get new auctions by email</h2>"
        "<p>new listings, price drops and closing deadlines for Tamil Nadu bank auctions. "
        "no spam, unsubscribe anytime.</p>"
        f'<form id="ac-form" data-source="{html.escape(source)}" data-label="auctions">'
        '<input id="ac-email" type="email" placeholder="you@email.com" autocomplete="email" required aria-label="your email">'
        '<button id="ac-btn" type="submit">notify me</button>'
        "</form>"
        '<p id="ac-msg" class="capture-msg" role="status" aria-live="polite" hidden></p>'
        "</section>"
    )


def render_guide(g: dict) -> str:
    slug = g["slug"]
    url = f"{SITE_BASE}/guides/{slug}"
    trail = [("Home", "/"), ("Guides", "/guides"), (g["h1"], f"/guides/{slug}")]
    jsonld = [_article_jsonld(g, url), _faq_jsonld(g), _breadcrumb_jsonld(trail)]

    faq_html = "".join(
        f"<details><summary>{html.escape(f['q'])}</summary><p>{html.escape(f['a'])}</p></details>"
        for f in g["faqs"]
    )
    chips = "".join(f'<span class="chip">{html.escape(t)}</span>' for t in g.get("related", []))
    chips_block = f'<h2>Related terms</h2><div class="chips">{chips}</div>' if chips else ""

    parts = [
        _head(g["title"], g["description"], url, jsonld),
        '<nav class="crumb"><a href="/">home</a> / <a href="/guides">guides</a> / '
        f'{html.escape(g["h1"].lower())}</nav>',
        f'<article class="guide"><h1>{html.escape(g["h1"])}</h1>',
        f'<p class="updated">last updated {html.escape(g["updated"])}</p>',
        f'<p class="answer">{html.escape(g["answer"])}</p>',
        g["body"],
        f'<h2>Frequently asked questions</h2><div class="faq">{faq_html}</div>',
        chips_block,
        "</article>",
        _cta_and_capture(f"guide-{slug}"),
        FOOT,
    ]
    return "\n".join(parts)


def render_hub(guides: list[dict]) -> str:
    url = f"{SITE_BASE}/guides"
    trail = [("Home", "/"), ("Guides", "/guides")]
    desc = ("Plain-language guides to Tamil Nadu bank auctions — how SARFAESI e-auctions work, "
            "EMD, reserve price, possession and what to check before you bid.")
    jsonld = [_breadcrumb_jsonld(trail)]
    cards = "".join(
        f'<a class="cardlink" href="/guides/{g["slug"]}"><div class="t">{html.escape(g["h1"])}</div>'
        f'<div class="m">{html.escape(g["description"])}</div></a>'
        for g in guides
    )
    parts = [
        _head("Bank auction guides — Tamil Nadu", desc, url, jsonld),
        '<nav class="crumb"><a href="/">home</a> / guides</nav>',
        "<h1>Bank auction guides</h1>",
        '<p class="lede">Plain-language guides to buying property at Tamil Nadu bank auctions — '
        'the process, the jargon, and what to check before you bid.</p>',
        f'<div class="grid">{cards}</div>',
        _cta_and_capture("guides-hub"),
        FOOT,
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    args = parser.parse_args(argv)

    # Guard: slugs must be unique and match the canonical slugify of the title's intent.
    slugs = [g["slug"] for g in GUIDES]
    assert len(slugs) == len(set(slugs)), "duplicate guide slug"

    written = []
    pages = [(OUT_ROOT / "index.html", render_hub(GUIDES))]
    pages += [(OUT_ROOT / g["slug"] / "index.html", render_guide(g)) for g in GUIDES]
    for path, content in pages:
        if args.dry_run:
            print(f"  [dry-run] would write {path.relative_to(REPO_ROOT)} ({len(content)} bytes)")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"  wrote {path.relative_to(REPO_ROOT)}")
        written.append(path)

    print(f"\n{len(written)} guide pages ({len(GUIDES)} guides + hub)")
    if not args.dry_run:
        count = seo_sitemap.write_sitemap(WEB_DIR)
        print(f"sitemap.xml rebuilt — {count} URLs total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
