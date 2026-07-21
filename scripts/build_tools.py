"""
scripts/build_tools.py
----------------------
Free-tool pages (engineering-as-marketing) — standalone calculators/checkers
that rank for "[thing] calculator" queries and earn links (the weakest SEO
dimension). Client-side only: vanilla JS, no backend.

First tool: the Tamil Nadu stamp duty + registration calculator
(/tools/tamil-nadu-stamp-duty-calculator) — targets "stamp duty calculator
tamil nadu" and "guideline value calculator" (both confirmed-demand).

Honesty rules (docs/marketing/copy-playbook.md, .agents/product-marketing.md):
rates are hard-coded and DO change, so the page carries a prominent "as of 2026,
confirm on TNREGINET" disclaimer, calls itself an estimate (not tax/legal
advice), cites official sources, and avoids the banned words. Brand shell,
theme, analytics, consent and email capture are reused from the other
generators so tools match the rest of the site.

Usage:
    python -m scripts.build_tools            # write pages + rebuild sitemap
    python -m scripts.build_tools --dry-run
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from scripts.build_landing_pages import (
    PAGE_CSS, CAPTURE_SCRIPT, SITE_BASE, THEME_INIT, ANALYTICS_SNIPPET, CONSENT_SCRIPT)
from scripts.build_guides import GUIDE_CSS
from scripts import seo_sitemap

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
OUT_ROOT = WEB_DIR / "tools"

UPDATED = "2026-07-21"

TOOL_CSS = """
.calc{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:20px 20px 8px;margin:0 0 18px}
.calc-row{margin:0 0 16px}
.calc-row label,.calc-label{display:block;font-weight:600;font-size:14px;margin:0 0 6px}
.calc input[type=number]{width:100%;padding:11px 13px;border:1px solid var(--border);
border-radius:var(--radius-sm);background:var(--paper);color:var(--ink);font:inherit;font-size:16px}
.calc input[type=number]:focus{outline:none;border-color:var(--accent)}
.calc-hint{margin:6px 0 0;font-size:12.5px;color:var(--muted)}
.calc-radio{display:inline-flex;align-items:center;gap:6px;font-weight:400;font-size:14px;
margin:0 16px 6px 0;cursor:pointer}
.calc-radio input{margin:0}
.calc-out{border-top:1px solid var(--border);padding-top:14px;margin-top:4px}
.calc-empty{color:var(--muted);font-size:14px;margin:2px 0 12px}
.calc-base{font-size:13.5px;margin:0 0 10px}
.calc-table{width:100%;border-collapse:collapse;font-size:15px}
.calc-table td{padding:9px 2px;border-bottom:1px solid var(--border)}
.calc-table td:last-child{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.calc-table tr.calc-total td{border-bottom:0;font-size:17px;font-weight:700;padding-top:12px}
.calc-table tr.calc-total td:last-child{color:var(--accent)}
.calc-tag{display:inline-block;font-size:11.5px;font-weight:600;color:var(--accent);
background:var(--accent-soft);border-radius:999px;padding:2px 8px;margin-left:6px}
.calc-tag.muted{color:var(--muted);background:transparent;border:1px solid var(--border)}
.muted{color:var(--muted);font-weight:400}
.disclaimer{background:var(--accent-soft);border:1px solid var(--border);border-radius:var(--radius);
padding:14px 16px;font-size:13px;color:var(--ink-soft);margin:0 0 18px}
"""

CALC_URL = f"{SITE_BASE}/tools/tamil-nadu-stamp-duty-calculator"

# The interactive bit. Vanilla JS, inline (CSP allows 'unsafe-inline' scripts).
# Charges the higher of sale value and guideline value; women buyers pay 3%
# registration instead of 4% only when that base is below Rs 10 lakh.
CALC_HTML = """
<div class="calc">
  <div class="calc-row">
    <label for="calc-value">Property sale value (₹)</label>
    <input id="calc-value" type="number" min="0" step="10000" inputmode="numeric" placeholder="e.g. 2500000">
  </div>
  <div class="calc-row">
    <label for="calc-gv">Guideline value (₹) — optional</label>
    <input id="calc-gv" type="number" min="0" step="10000" inputmode="numeric" placeholder="leave blank if unknown">
    <p class="calc-hint">Charges apply to the <strong>higher</strong> of your sale value and the government
    guideline value. <a href="https://tnreginet.gov.in/" rel="nofollow noopener" target="_blank">Look up guideline value on TNREGINET →</a></p>
  </div>
  <div class="calc-row">
    <span class="calc-label">Buyer</span>
    <label class="calc-radio"><input type="radio" name="buyer" value="standard" checked> Standard</label>
    <label class="calc-radio"><input type="radio" name="buyer" value="woman"> Woman buyer <span class="muted">(concession if value under ₹10 lakh)</span></label>
  </div>
  <div class="calc-out" id="calc-out" aria-live="polite"></div>
</div>
<script>
(function(){
  var v=document.getElementById('calc-value'), gv=document.getElementById('calc-gv'), out=document.getElementById('calc-out');
  if(!v||!gv||!out) return;
  function inr(n){ try{ return '₹' + Math.round(n).toLocaleString('en-IN'); }catch(e){ return '₹' + Math.round(n); } }
  function buyer(){ var r=document.querySelector('input[name=buyer]:checked'); return r ? r.value : 'standard'; }
  function calc(){
    var sale=parseFloat(v.value)||0, guide=parseFloat(gv.value)||0, base=Math.max(sale,guide);
    if(base<=0){ out.innerHTML='<p class="calc-empty">Enter a property value to see the stamp duty and registration charges.</p>'; return; }
    var conc = (buyer()==='woman' && base < 1000000);
    var regRate = conc ? 0.03 : 0.04;
    var stamp=base*0.07, reg=base*regRate, total=stamp+reg;
    var tag = buyer()==='woman'
      ? (conc ? '<span class="calc-tag">3% women concession applied</span>'
              : '<span class="calc-tag muted">value ₹10 lakh or more — standard 4%</span>')
      : '';
    out.innerHTML =
      '<div class="calc-base">Charged on '+inr(base)+' <span class="muted">(higher of sale value and guideline value)</span></div>'+
      '<table class="calc-table"><tbody>'+
      '<tr><td>Stamp duty <span class="muted">(7%)</span></td><td>'+inr(stamp)+'</td></tr>'+
      '<tr><td>Registration fee <span class="muted">('+(regRate*100)+'%)</span> '+tag+'</td><td>'+inr(reg)+'</td></tr>'+
      '<tr class="calc-total"><td>Total payable</td><td>'+inr(total)+'</td></tr>'+
      '</tbody></table>';
  }
  [v,gv].forEach(function(el){ el.addEventListener('input', calc); });
  Array.prototype.forEach.call(document.querySelectorAll('input[name=buyer]'), function(el){ el.addEventListener('change', calc); });
  calc();
})();
</script>
"""

FAQS = [
    {"q": "How is stamp duty calculated in Tamil Nadu?",
     "a": ("Stamp duty is 7% and the registration fee is 4% of the property value — about 11% together — "
           "charged on the higher of your sale value and the government guideline value. So even a low "
           "sale price is taxed at least on the guideline value.")},
    {"q": "What is the stamp duty concession for women in Tamil Nadu?",
     "a": ("From April 2025, a woman buyer pays 3% registration instead of 4% where the property value is "
           "below ₹10 lakh. Stamp duty stays 7%. Above ₹10 lakh the standard 4% applies. Confirm current "
           "eligibility on TNREGINET.")},
    {"q": "Does guideline value affect my stamp duty?",
     "a": ("Yes. The charge is calculated on the higher of the sale value and the guideline value — the "
           "government's minimum value for that locality — which you can look up on TNREGINET. If the "
           "guideline value is higher than your price, it becomes the base.")},
    {"q": "Do I pay stamp duty on a bank auction property?",
     "a": ("Yes. Stamp duty and registration are payable to register the sale certificate in your name "
           "after you win, so budget roughly another tenth of the value on top of your bid.")},
]

SOURCES = [
    {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
     "h": "https://tnreginet.gov.in/"},
    {"t": "Stamp duty and registration charges in Tamil Nadu (2026) — ClearTax",
     "h": "https://cleartax.in/s/stamp-duty-and-registration-charges-in-tamil-nadu"},
]


def _jsonld() -> list[dict]:
    title = "Tamil Nadu stamp duty & registration calculator"
    desc = ("Free calculator for Tamil Nadu property stamp duty (7%) and registration fee (4%, or 3% for "
            "women under ₹10 lakh), charged on the higher of sale value and guideline value.")
    return [
        {"@context": "https://schema.org", "@type": "Article",
         "headline": title, "description": desc, "datePublished": UPDATED, "dateModified": UPDATED,
         "author": {"@type": "Organization", "name": "AuctionScope"},
         "publisher": {"@type": "Organization", "name": "AuctionScope", "url": f"{SITE_BASE}/"},
         "mainEntityOfPage": CALC_URL,
         "citation": [{"@type": "CreativeWork", "name": s["t"], "url": s["h"]} for s in SOURCES]},
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": [{"@type": "Question", "name": f["q"],
                         "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in FAQS]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList",
         "itemListElement": [
             {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE_BASE}/"},
             {"@type": "ListItem", "position": 2, "name": "Stamp duty calculator", "item": CALC_URL}]},
    ]


def _head(title: str, desc: str, url: str, jsonld: list[dict]) -> str:
    blocks = "\n".join(
        f'<script type="application/ld+json">{json.dumps(b, ensure_ascii=False)}</script>' for b in jsonld)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{THEME_INIT}
{ANALYTICS_SNIPPET}
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
<style>{PAGE_CSS}{GUIDE_CSS}{TOOL_CSS}</style>
{blocks}
</head>
<body>
<header class="top"><a class="brand" href="/">auctionscope</a><a href="/">search all auctions →</a></header>
<div class="wrap">"""


FOOT = ("""<p class="note">This calculator is an estimate for general guidance, not tax or legal advice.
Rates and concessions are set by the Government of Tamil Nadu and change from time to time; your actual
charges can also vary with property type and local surcharges. Always confirm the current rates and the
guideline value on TNREGINET before you transact.</p>
</div>""" + CAPTURE_SCRIPT + CONSENT_SCRIPT + "</body></html>")


def render_calculator() -> str:
    title = "Tamil Nadu stamp duty & registration calculator"
    desc = ("Free Tamil Nadu stamp duty and registration charge calculator — 7% stamp duty + 4% "
            "registration (3% for women under ₹10 lakh), on the higher of sale value and guideline value.")
    faq_html = "".join(
        f"<details><summary>{html.escape(f['q'])}</summary><p>{html.escape(f['a'])}</p></details>" for f in FAQS)
    src_html = "".join(
        f'<li><a href="{html.escape(s["h"])}" rel="nofollow noopener" target="_blank">{html.escape(s["t"])}</a></li>'
        for s in SOURCES)
    parts = [
        _head(title, desc, CALC_URL, _jsonld()),
        '<nav class="crumb"><a href="/">home</a> / stamp duty calculator</nav>',
        '<article class="guide"><h1>Tamil Nadu stamp duty &amp; registration calculator</h1>',
        f'<p class="updated">rates as of {UPDATED}</p>',
        '<p class="lede">Estimate what it costs to register a property in Tamil Nadu. Enter the value and '
        'the calculator works out the stamp duty (7%) and registration fee (4%, or 3% for women buyers '
        'under ₹10 lakh) on the higher of your sale value and the government guideline value.</p>',
        CALC_HTML,
        '<div class="disclaimer">Rates shown are as of 2026 — 7% stamp duty and 4% registration (women '
        'buyers: 3% registration on property under ₹10 lakh). These are set by the state and change; '
        'confirm the current figures and the guideline value on '
        '<a href="https://tnreginet.gov.in/" rel="nofollow noopener" target="_blank">TNREGINET</a> before '
        'you transact. This is an estimate, not tax or legal advice.</div>',
        "<h2>How the calculation works</h2>",
        "<ul>"
        "<li><strong>Base value</strong> — the higher of your sale price and the locality's guideline "
        "value. The government won't register below the guideline value, so that's the floor.</li>"
        "<li><strong>Stamp duty</strong> — 7% of the base value.</li>"
        "<li><strong>Registration fee</strong> — 4% of the base value, or 3% for a woman buyer where the "
        "value is below ₹10 lakh (a concession from April 2025).</li>"
        "<li><strong>Total</strong> — roughly 11% of the base value, a real cost to add on top of the "
        "price.</li></ul>",
        '<p>New to any of these terms? Read the guides on '
        '<a href="/guides/stamp-duty-and-registration-charges-tamil-nadu">stamp duty &amp; registration</a> '
        'and <a href="/guides/guideline-value-tamil-nadu">guideline value</a>. Buying at auction? '
        '<a href="/">search live Tamil Nadu bank auctions</a>.</p>',
        f'<h2>Frequently asked questions</h2><div class="faq">{faq_html}</div>',
        f'<h2>Sources</h2><p class="updated">Rates change — confirm the current figures on the official '
        f'portal below.</p><ul>{src_html}</ul>',
        "</article>",
        # CTA + capture (reuse the guides' pattern inline)
        '<p style="margin:28px 0 4px"><a class="cta" href="/">browse live Tamil Nadu bank auctions →</a></p>'
        '<section class="capture"><h2>get new Tamil Nadu auctions by email</h2>'
        '<p>new listings, price drops and closing deadlines. no spam, unsubscribe anytime.</p>'
        '<form id="ac-form" data-source="tool-stamp-duty" data-label="auctions">'
        '<input id="ac-email" type="email" placeholder="you@email.com" autocomplete="email" required aria-label="your email">'
        '<button id="ac-btn" type="submit">notify me</button></form>'
        '<p id="ac-msg" class="capture-msg" role="status" aria-live="polite" hidden></p></section>',
        FOOT,
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    args = parser.parse_args(argv)

    path = OUT_ROOT / "tamil-nadu-stamp-duty-calculator" / "index.html"
    content = render_calculator()
    if args.dry_run:
        print(f"  [dry-run] would write {path.relative_to(REPO_ROOT)} ({len(content)} bytes)")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  wrote {path.relative_to(REPO_ROOT)}")
        count = seo_sitemap.write_sitemap(WEB_DIR)
        print(f"sitemap.xml rebuilt — {count} URLs total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
