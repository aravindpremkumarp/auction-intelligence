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

from scripts.build_landing_pages import (
    PAGE_CSS, CAPTURE_SCRIPT, SITE_BASE, slugify, THEME_INIT, ANALYTICS_SNIPPET, CONSENT_SCRIPT)
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
.grouphead{margin:36px 0 4px;padding-top:18px;border-top:1px solid var(--border);font-size:19px}
.grouphead:first-of-type{border-top:0;padding-top:0}
.groupintro{color:var(--muted);font-size:13.5px;margin:0 0 14px;max-width:70ch}
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
    {
        "slug": "what-is-a-sarfaesi-bank-auction",
        "title": "What is a SARFAESI bank auction — and why banks hold them",
        "h1": "What is a SARFAESI bank auction?",
        "description": ("A SARFAESI bank auction is how a bank sells a property whose owner defaulted on a "
                        "secured loan. What the SARFAESI Act allows, and what it means for buyers."),
        "updated": "2026-07-21",
        "answer": ("A SARFAESI bank auction is a public e-auction where a bank sells a property that was "
                   "mortgaged against a loan the borrower stopped repaying. The Securitisation and "
                   "Reconstruction of Financial Assets and Enforcement of Security Interest Act, 2002 lets "
                   "the bank recover its dues by selling the security — without going to court first."),
        "body": """
<h2>Why these auctions exist</h2>
<p>When someone takes a secured loan — a home loan, a loan against property, a business loan backed by
real estate — the property is pledged to the lender. If the borrower stops repaying and the loan
becomes a non-performing asset (an "NPA"), the SARFAESI Act lets the bank enforce that security and
sell the property to recover what it is owed. The auction is the sale step.</p>

<h2>What SARFAESI lets a bank do</h2>
<ul>
<li>Issue a demand notice under Section 13(2) giving the borrower 60 days to clear the dues.</li>
<li>If the dues stay unpaid, take possession of the secured property under Section 13(4) — symbolically
or physically.</li>
<li>Value the property, publish a sale notice, and sell it by public auction or tender.</li>
</ul>
<p>Crucially, the bank does this <strong>without first going to court</strong> — that is the power the Act
grants secured creditors. A borrower can still challenge the action before the Debts Recovery Tribunal.</p>

<h2>What it means if you are buying</h2>
<p>You are buying from the bank as the enforcing creditor, not from the original owner, and the sale is
on an <em>as-is-where-is</em> basis — the bank makes no promise about condition, occupancy or dues. That
makes a few checks essential before you bid: whether possession is symbolic or physical, what
encumbrances exist, and whether the reserve price is genuinely below the local market. Running those
checks on a specific listing is what AuctionScope is built for.</p>
""",
        "faqs": [
            {"q": "What does SARFAESI stand for?",
             "a": ("The Securitisation and Reconstruction of Financial Assets and Enforcement of Security "
                   "Interest Act, 2002 — the law that lets banks and other secured creditors enforce their "
                   "security and sell mortgaged property to recover unpaid loans.")},
            {"q": "Can a borrower stop a SARFAESI auction?",
             "a": ("A borrower can clear the outstanding dues before the sale, or challenge the bank's "
                   "action before the Debts Recovery Tribunal. Because of this, always confirm a listing is "
                   "still going ahead with the bank before committing money.")},
            {"q": "Is a SARFAESI auction the same as a court auction?",
             "a": ("No. SARFAESI lets the bank sell the security without a court order first, whereas a "
                   "court auction is conducted under a court's directions. This page covers bank e-auctions "
                   "under SARFAESI.")},
        ],
        "related": ["EMD", "reserve price", "as-is-where-is", "NPA", "symbolic vs physical possession"],
    },
    {
        "slug": "bank-auction-process-step-by-step",
        "title": "How to buy a bank auction property — the process step by step",
        "h1": "How to buy a bank auction property",
        "description": ("How to buy a property at a bank auction, step by step — from the sale notice and "
                        "EMD to bidding, payment and possession, and where a buyer enters."),
        "updated": "2026-07-21",
        "answer": ("To buy a bank auction property you go through a fixed process: the bank publishes a "
                   "sale notice, you submit the EMD before its deadline, bid in the online auction, and — "
                   "if you win — pay 25% immediately and the balance within 15 days, then receive the sale "
                   "certificate. A buyer usually enters at the sale-notice stage; here is each step."),
        "body": """
<h2>The steps</h2>
<ol>
<li><strong>Default and NPA.</strong> The borrower stops repaying and the loan is classified a
non-performing asset.</li>
<li><strong>Demand notice — Section 13(2).</strong> The bank demands the dues, giving 60 days to pay.</li>
<li><strong>Possession — Section 13(4).</strong> If unpaid, the bank takes symbolic or physical
possession of the property.</li>
<li><strong>Valuation and reserve price.</strong> The bank values the property and sets a reserve — the
minimum bid.</li>
<li><strong>Sale notice.</strong> The auction is advertised, with at least 30 days' notice to the
borrower before the sale. This is the public listing you see.</li>
<li><strong>EMD.</strong> Interested bidders submit the earnest money deposit and KYC before the
deadline to get bidding access.</li>
<li><strong>E-auction.</strong> Bidding runs online for a set window; the highest bid above the reserve
wins.</li>
<li><strong>Payment.</strong> The winner pays 25% (the EMD counts towards this) immediately or the next
working day, and the balance within 15 days.</li>
<li><strong>Sale certificate and possession.</strong> On full payment the bank issues a sale
certificate; the buyer then takes possession and registers the certificate.</li>
</ol>

<h2>Where a buyer enters</h2>
<p>As a buyer you almost always join at step 5 — when the sale notice is published. Everything before it
is between the bank and the borrower. Your window to research a property, arrange the EMD and check the
reserve against the market is the gap between the sale notice and the EMD deadline, which is often only
a couple of weeks.</p>

<h2>The step most buyers underestimate</h2>
<p>Steps 8 and 9 move fast: a 15-day balance-payment clock and a possession step that can be smooth
(physical possession) or slow (symbolic possession, where an occupant may still be in the property).
Knowing which one applies <em>before</em> you bid is exactly the kind of check worth doing on the
specific listing.</p>
""",
        "faqs": [
            {"q": "How long does the whole bank auction process take?",
             "a": ("From demand notice to sale it commonly runs a few months, but the part that matters to "
                   "a buyer — sale notice to auction — is often just two to four weeks. Exact dates are in "
                   "each sale notice.")},
            {"q": "At what stage can I, as a buyer, take part?",
             "a": ("From the sale-notice stage onwards. You register, pay the EMD before its deadline, and "
                   "then bid in the online auction on the scheduled date.")},
            {"q": "When do I actually get the property?",
             "a": ("After you pay the full amount and the bank issues the sale certificate. Taking physical "
                   "possession is quick when the bank already holds it, and slower when possession is only "
                   "symbolic and an occupant remains.")},
        ],
        "related": ["EMD", "reserve price", "sale notice", "possession", "sale certificate"],
    },
    {
        "slug": "what-is-reserve-price-in-a-bank-auction",
        "title": "What is the reserve price in a bank auction?",
        "h1": "What is the reserve price?",
        "description": ("The reserve price is the minimum bid a bank will accept at auction. What it is, how "
                        "banks set it, and why it is not the same as market value."),
        "updated": "2026-07-21",
        "answer": ("The reserve price is the lowest bid a bank will accept for a property at auction — the "
                   "floor, not a fixed selling price. The bank sets it from a valuation. Bids must meet or "
                   "beat it; if none do, the property usually goes to re-auction, often at a lower reserve."),
        "body": """
<h2>How banks set the reserve</h2>
<p>Before the auction the bank gets the property valued and sets the reserve price at or near that
valuation. It is the minimum the bank is willing to accept, chosen to recover its dues while still
attracting bidders. The sale notice always states it.</p>

<h2>Reserve price is not market value</h2>
<p>This is the key point for buyers. A reserve can sit <strong>below</strong> local market rates —
because the bank wants a quick, certain recovery, not the last rupee — but it can also be set close to
or above what the area actually commands, especially for hard-to-value or distressed property. A low
reserve is a reason to investigate, not proof of a bargain.</p>

<h2>What the reserve tells you — and what it doesn't</h2>
<ul>
<li>It tells you the minimum you must bid.</li>
<li>It does <em>not</em> tell you the property is worth that, more, or less.</li>
<li>It does not account for dues, occupancy or repairs you may inherit.</li>
</ul>
<p>To know whether a reserve is genuinely attractive, you have to compare it to the real local rate for
that property type and area, and factor in what comes with the property. That comparison — reserve
versus local ₹/sqft — is one of the checks AuctionScope helps you run before you bid.</p>
""",
        "faqs": [
            {"q": "Is the reserve price the final price I pay?",
             "a": ("No. It is the minimum opening bid. The final price is whatever the winning bid reaches "
                   "after competitive bidding, which can be at or above the reserve.")},
            {"q": "Does a low reserve mean the property is cheap?",
             "a": ("Not on its own. A reserve below market can signal value, but you have to compare it to "
                   "the real local rate and account for dues, condition and possession. Treat a low reserve "
                   "as a prompt to check, not a conclusion.")},
            {"q": "What happens if no one bids the reserve?",
             "a": ("The property typically goes to a re-auction, often with a reduced reserve. The bank is "
                   "not obliged to sell below its reserve.")},
        ],
        "related": ["EMD", "market value", "re-auction", "as-is-where-is"],
    },
    {
        "slug": "symbolic-vs-physical-possession",
        "title": "Symbolic vs physical possession in bank auctions",
        "h1": "Symbolic vs physical possession",
        "description": ("The single biggest risk check in a bank auction: whether the bank holds physical "
                        "possession or only symbolic possession, and what it means for you as the buyer."),
        "updated": "2026-07-21",
        "answer": ("Symbolic (constructive) possession means the bank holds the property on paper but an "
                   "occupant may still be inside; physical possession means the bank has actual, vacant "
                   "control. If you buy under symbolic possession, getting the occupant out can become your "
                   "task — so this is a check to make before you bid."),
        "body": """
<h2>The two kinds of possession</h2>
<table class="kv">
<tr><th>Type</th><th>What it means</th></tr>
<tr><td>Symbolic (constructive)</td><td>The bank has taken possession on paper — usually by affixing a
notice — but the borrower or a tenant may still physically occupy the property.</td></tr>
<tr><td>Physical</td><td>The bank has actual, vacant possession, often obtained through the District
Magistrate under Section 14. The property can be handed over clear.</td></tr>
</table>

<h2>Why it matters so much to a buyer</h2>
<p>If you buy a property that the bank holds only <strong>symbolically</strong>, you may take on the job
of getting the existing occupant out after the sale — a process that can take time and effort, even
though the law provides routes for it. If the bank holds <strong>physical</strong> possession, you
typically get a vacant property. Two identical-looking listings at the same reserve can be very
different purchases because of this one field.</p>

<h2>How to check</h2>
<p>The possession notice and the sale notice usually indicate which type applies, but the wording can be
easy to miss. Confirm it explicitly — with the bank if needed — before you commit an EMD. On
AuctionScope you can ask, in plain language, what a specific listing says about possession, so you are
not guessing from dense notice text.</p>
""",
        "faqs": [
            {"q": "Is it safe to buy a property under symbolic possession?",
             "a": ("It can be, but you should go in knowing you may have to get the occupant out yourself "
                   "after the sale. Factor that time and effort into your decision, and confirm the "
                   "possession status with the bank before bidding.")},
            {"q": "How do I get physical possession after buying?",
             "a": ("When the bank already holds physical possession, it hands over the property after the "
                   "sale certificate. Under symbolic possession, the buyer may need to pursue possession "
                   "through the routes the law provides. Confirm the specifics with the bank.")},
            {"q": "Where is possession type stated?",
             "a": ("Usually in the possession notice and the sale notice, though the wording can be terse. "
                   "If it is unclear, ask the bank directly before you pay the EMD.")},
        ],
        "related": ["as-is-where-is", "sale notice", "Section 14", "EMD"],
    },
    {
        "slug": "what-does-as-is-where-is-mean",
        "title": "What 'as-is-where-is' means in a bank auction",
        "h1": "What does 'as-is-where-is' mean?",
        "description": ("Bank auctions sell property 'as-is-where-is' and 'as-is-what-is'. What these terms "
                        "put on the buyer, and the checks they make necessary."),
        "updated": "2026-07-21",
        "answer": ("'As-is-where-is' means you buy the property in exactly the condition and situation it is "
                   "in — the bank gives no warranty about its state, area, occupancy, dues or title. "
                   "'As-is-what-is' extends that to whatever rights and liabilities come with it. The onus "
                   "of checking is on the buyer."),
        "body": """
<h2>Three phrases, one idea</h2>
<ul>
<li><strong>As-is-where-is</strong> — you take the property in its current physical state and location,
with no repairs or promises from the bank.</li>
<li><strong>As-is-what-is</strong> — you take whatever it legally is: its rights, its measurements, its
attached liabilities.</li>
<li><strong>Whatever-there-is</strong> — sometimes added to underline that the bank sells only whatever
interest it can pass, nothing more.</li>
</ul>

<h2>What the bank is <em>not</em> promising</h2>
<p>Under these terms the bank does not warrant the built-up area or extent, the condition of the
structure, whether it is occupied, what property tax or utility arrears are outstanding, or that the
title is free of every claim. The sale notice usually says known encumbrances are stated "to the best
of the bank's knowledge" — which is not the same as a guarantee.</p>

<h2>What this means you should check</h2>
<p>Because the risk sits with you, the pre-bid checks matter: verify the extent and property type,
inspect or research the location and condition, confirm possession status, and look into encumbrances
and outstanding dues. None of this is exotic — it is the standard homework an <em>as-is-where-is</em>
sale demands, and it is the work AuctionScope is designed to make faster on a specific listing.</p>
""",
        "faqs": [
            {"q": "Does 'as-is-where-is' mean the bank guarantees nothing?",
             "a": ("Effectively yes — the bank makes no promise about condition, area, occupancy or dues, "
                   "and states known encumbrances only to the best of its knowledge. The responsibility to "
                   "check sits with the buyer.")},
            {"q": "What is the difference between 'as-is-where-is' and 'as-is-what-is'?",
             "a": ("'As-is-where-is' covers the physical state and location; 'as-is-what-is' covers the "
                   "legal reality — the rights and liabilities attached. In practice they appear together "
                   "and both put the onus of checking on the buyer.")},
            {"q": "Can I inspect the property before bidding?",
             "a": ("Sale notices often allow an inspection on a stated date, but access can be limited when "
                   "a property is occupied. Use whatever inspection is offered, and research everything you "
                   "cannot see in person.")},
        ],
        "related": ["encumbrances", "possession", "reserve price", "sale notice"],
    },
    {
        "slug": "how-to-read-a-bank-auction-sale-notice",
        "title": "How to read a bank auction sale notice, field by field",
        "h1": "How to read a bank auction sale notice",
        "description": ("A sale notice packs every fact you need into dense text. A field-by-field guide to "
                        "what each part means and what to check."),
        "updated": "2026-07-21",
        "answer": ("A bank auction sale notice states the property, the reserve price, the EMD, the auction "
                   "date and time, the EMD and bid-submission deadlines, the conducting bank and contact, "
                   "and known encumbrances and possession status. Reading each field carefully is how you "
                   "avoid surprises after you bid."),
        "body": """
<h2>The fields that matter</h2>
<table class="kv">
<tr><th>Field</th><th>What to look for</th></tr>
<tr><td>Property description</td><td>Extent/area, property type, survey or door number, boundaries. Check
the extent basis — for a flat, built-up area and undivided share (UDS) of land are different numbers.</td></tr>
<tr><td>Reserve price</td><td>The minimum bid — compare it to local market rates, not just accept it.</td></tr>
<tr><td>EMD amount and deadline</td><td>How much to deposit and the last date/time to do it. Miss it and
you cannot bid.</td></tr>
<tr><td>Auction date, time, platform</td><td>When and where the online auction runs, and any bid-increment
rules.</td></tr>
<tr><td>Possession status</td><td>Symbolic or physical — a major risk field (see the possession guide).</td></tr>
<tr><td>Known encumbrances / dues</td><td>Stated "to the best of the bank's knowledge" — treat as a
starting point, not a complete list.</td></tr>
<tr><td>Inspection date</td><td>When you can view the property, if allowed.</td></tr>
<tr><td>Contact / authorised officer</td><td>Who to call to confirm details before you bid.</td></tr>
</table>

<h2>The traps</h2>
<ul>
<li><strong>Extent units.</strong> Notices mix acres, cents, grounds and square feet — convert carefully
before comparing prices.</li>
<li><strong>Flats vs land.</strong> A flat's notice may state both built-up area and UDS; confusing them
distorts any value comparison.</li>
<li><strong>"To the best of knowledge".</strong> The encumbrance line is a disclaimer, not a clean bill.</li>
</ul>

<h2>Turning a notice into a decision</h2>
<p>The information is all there, but it is dense and easy to misread under a deadline. AuctionScope reads
the notice for you and lets you ask plain-language questions — what the extent is, whether the reserve
looks low for the area, what the possession line says — so you decide from facts, not from squinting at
fine print.</p>
""",
        "faqs": [
            {"q": "What is the most important field in a sale notice?",
             "a": ("There isn't one — the reserve price, EMD deadline, possession status and extent all "
                   "matter together. Possession status and the true extent are the two most often "
                   "misread, and both can change whether a listing is a good buy.")},
            {"q": "What does 'to the best of the bank's knowledge' mean on encumbrances?",
             "a": ("It means the bank is listing the dues and claims it is aware of, without guaranteeing "
                   "the list is complete. You should check encumbrances independently rather than rely on "
                   "it alone.")},
            {"q": "Why do extents use units like cent, ground and are?",
             "a": ("They are traditional land-area units used across Tamil Nadu. Convert them to a common "
                   "unit like square feet before comparing a reserve to local rates, or the comparison will "
                   "be wrong.")},
        ],
        "related": ["reserve price", "EMD", "possession", "encumbrances", "UDS"],
    },
    {
        "slug": "bank-auction-re-auctions-and-price-drops",
        "title": "Re-auctions and price drops in bank auctions",
        "h1": "Re-auctions and price drops",
        "description": ("When a property doesn't sell, banks re-auction it — often at a lower reserve. Why "
                        "re-auctions happen and how to read a falling reserve."),
        "updated": "2026-07-21",
        "answer": ("A re-auction happens when a property gets no qualifying bid, or the winner fails to pay. "
                   "Banks often re-list it with a reduced reserve to attract buyers. A property on its "
                   "second or third round can signal a genuine discount — or a reason it isn't selling."),
        "body": """
<h2>Why a property comes back</h2>
<ul>
<li><strong>No qualifying bid.</strong> If no one bids at or above the reserve, the sale fails and the
bank re-lists.</li>
<li><strong>Winner defaults.</strong> If the winning bidder does not pay the balance in time, their EMD
is forfeited and the property returns to auction.</li>
<li><strong>Process objections.</strong> Occasionally a sale is set aside and re-run.</li>
</ul>

<h2>How the reserve moves</h2>
<p>To attract interest on a re-auction, banks frequently <strong>cut the reserve</strong> from the
previous round. That falling reserve is why re-auctions can be where the real discounts appear — a
property that started above market may, a round or two later, sit genuinely below it.</p>

<h2>Read a price drop both ways</h2>
<p>A lower reserve can mean opportunity — or it can mean the property has a problem keeping bidders away:
a stubborn occupant, an access or title complication, or a location that isn't moving. The reserve
history tells you the price is falling; it doesn't tell you why. Comparing the current reserve to local
rates, and checking possession and encumbrances, is how you tell a bargain from a trap — and re-auction
listings, which carry the previous reserve, are exactly where that comparison pays off.</p>
""",
        "faqs": [
            {"q": "Are re-auction properties cheaper?",
             "a": ("Often the reserve is lower than the first round, so they can be. But a falling reserve "
                   "can also reflect a problem that is putting bidders off, so check why it hasn't sold "
                   "before treating it as a discount.")},
            {"q": "Why would a property be auctioned more than once?",
             "a": ("Usually because it received no bid at or above the reserve, or the previous winner "
                   "failed to pay and forfeited their EMD. The bank then re-lists it, commonly at a reduced "
                   "reserve.")},
            {"q": "How do I know a property is a re-auction?",
             "a": ("The notice or listing history shows earlier rounds and their reserves. On AuctionScope, "
                   "re-auction listings carry the previous reserve, so you can see the price movement "
                   "directly.")},
        ],
        "related": ["reserve price", "EMD", "market value", "possession"],
    },
    {
        "slug": "how-to-pay-after-winning-a-bank-auction",
        "title": "How payment works after you win a bank auction",
        "h1": "How payment works after you win",
        "description": ("Win the auction and a fast payment clock starts: 25% almost immediately, the "
                        "balance within 15 days, then the sale certificate. What to be ready for."),
        "updated": "2026-07-21",
        "answer": ("When you win, you pay 25% of the sale price — the EMD counts towards it — immediately or "
                   "by the next working day, and the balance 75% within 15 days (the bank may extend this "
                   "in writing). On full payment the bank issues a sale certificate, which you then "
                   "register. Miss the deadlines and you forfeit the deposit."),
        "body": """
<h2>The payment clock</h2>
<ol>
<li><strong>25% immediately.</strong> On being declared the winner, you pay 25% of the sale amount
(including the EMD you already deposited) on the same day or the next working day.</li>
<li><strong>Balance 75% within 15 days.</strong> The rest is due within fifteen days of the sale being
confirmed. The bank can extend this period in writing, but you cannot assume an extension.</li>
<li><strong>Sale certificate.</strong> Once you have paid in full, the authorised officer issues a sale
certificate in your name.</li>
<li><strong>Registration.</strong> You pay stamp duty and register the sale certificate to complete your
title record.</li>
</ol>

<h2>What happens if you miss a deadline</h2>
<p>If you fail to pay the 25% or the balance in time, the deposit — including your EMD — is forfeited to
the bank, and the property can be re-auctioned. This is the main way buyers lose real money, so line up
your funds <em>before</em> you bid, not after.</p>

<h2>Plan the money before you bid</h2>
<p>Because the timeline is short and unforgiving, treat financing as a pre-bid task: know where the 25%
and the balance are coming from, and confirm the exact amounts and dates in the sale notice and with the
bank. A property being a good deal on paper matters little if you cannot fund it inside the window.</p>
""",
        "faqs": [
            {"q": "How much do I pay immediately after winning?",
             "a": ("25% of the sale price, with your EMD counting towards it, on the same day or the next "
                   "working day. The balance 75% follows within 15 days unless the bank grants a written "
                   "extension.")},
            {"q": "What is a sale certificate?",
             "a": ("It is the document the bank's authorised officer issues after you pay in full, "
                   "recording that the property has been sold to you. You register it, paying stamp duty, "
                   "to complete your title record.")},
            {"q": "What if I can't pay the balance in 15 days?",
             "a": ("Unless the bank extends the period in writing, missing it means your deposit — EMD "
                   "included — is forfeited and the property may be re-auctioned. Arrange funds before you "
                   "bid.")},
        ],
        "related": ["EMD", "sale certificate", "reserve price", "re-auction"],
    },
    {
        "slug": "encumbrances-and-dues-in-a-bank-auction",
        "title": "Encumbrances and dues in a bank auction",
        "h1": "Encumbrances and dues: what you might inherit",
        "description": ("A bank auction can come with baggage — unpaid property tax, utility arrears, "
                        "society dues or other claims. What to check before you bid."),
        "updated": "2026-07-21",
        "answer": ("An encumbrance is any charge or claim on a property — a mortgage, unpaid tax, utility "
                   "arrears or society dues. In an as-is-where-is sale the bank lists only what it knows, "
                   "and some of these dues can pass to you as the buyer, so checking them independently is "
                   "essential before bidding."),
        "body": """
<h2>What counts as an encumbrance</h2>
<ul>
<li>Existing mortgages or charges beyond the bank's own.</li>
<li>Unpaid property tax to the local body.</li>
<li>Electricity and water arrears.</li>
<li>Housing-society or maintenance dues on a flat.</li>
<li>Attachments, liens or pending claims on the property.</li>
</ul>

<h2>Who pays them?</h2>
<p>The sale notice usually lists known encumbrances "to the best of the bank's knowledge" and often
states that outstanding statutory dues, utility arrears and society dues are to be borne by the buyer.
That means a low reserve can hide a real cost: you could clear the auction and then face arrears that
reduce or erase the saving. Read this section of the notice closely, and never assume the listed dues
are the whole story.</p>

<h2>How to check</h2>
<p>Pull the encumbrance certificate (EC) for the property from TNREGINET to see registered charges, and
ask the local body, the utilities and — for a flat — the society about outstanding amounts. Where the
sale notice is vague, ask the bank's authorised officer directly. Building this into your pre-bid
routine turns "as-is-where-is" from a risk into a known quantity, which is the mindset AuctionScope is
built to support.</p>
""",
        "faqs": [
            {"q": "Do I inherit unpaid dues on a bank auction property?",
             "a": ("Often yes. Sale notices commonly place outstanding property tax, utility arrears and "
                   "society dues on the buyer. Check the notice and verify the amounts independently before "
                   "you bid.")},
            {"q": "What is an encumbrance certificate?",
             "a": ("An EC, available from TNREGINET in Tamil Nadu, lists the registered charges and "
                   "transactions on a property over a period. It is a core check for spotting existing "
                   "mortgages or claims.")},
            {"q": "Does the bank guarantee there are no encumbrances?",
             "a": ("No. It lists what it knows, to the best of its knowledge, and the sale is as-is-where-is. "
                   "You should verify encumbrances and dues yourself rather than rely on that line.")},
        ],
        "related": ["as-is-where-is", "EC / encumbrance certificate", "reserve price", "sale notice"],
    },
    {
        "slug": "is-buying-a-bank-auction-property-safe",
        "title": "Is buying a bank auction property safe?",
        "h1": "Is buying a bank auction property safe?",
        "description": ("Bank auctions are legitimate and can offer value, but they carry real risks. An "
                        "honest look at what is safe, what isn't, and how to reduce the risk."),
        "updated": "2026-07-21",
        "answer": ("Buying at a bank auction is legitimate — banks sell under the SARFAESI Act and issue a "
                   "sale certificate — and it can offer value. The risks are real too: as-is-where-is "
                   "sales, possible occupants, inherited dues and a fast payment clock. The safety comes "
                   "from checking each of these before you bid, not from the process itself."),
        "body": """
<h2>What makes it legitimate</h2>
<p>The sale is backed by law: the bank enforces its security under the SARFAESI Act and, on full payment,
issues a sale certificate recording the transfer to you. Thousands of properties change hands this way.
In that sense the process is sound.</p>

<h2>Where the real risks are</h2>
<table class="kv">
<tr><th>Risk</th><th>How to reduce it</th></tr>
<tr><td>As-is-where-is — no warranty on condition or area</td><td>Inspect where allowed; verify the
extent and property type from the notice.</td></tr>
<tr><td>Symbolic possession — occupant still inside</td><td>Confirm possession status before bidding;
plan for the time it may take to get vacant possession.</td></tr>
<tr><td>Inherited dues — tax, utilities, society</td><td>Pull the EC and check arrears with the local
body, utilities and society.</td></tr>
<tr><td>Fast payment clock — 25% then balance in 15 days</td><td>Arrange financing before you bid, not
after you win.</td></tr>
<tr><td>Reserve that isn't actually a discount</td><td>Compare the reserve to real local rates for that
area and type.</td></tr>
</table>

<h2>The honest bottom line</h2>
<p>A bank auction is neither a sure bargain nor a trap — it is a legitimate sale that rewards
preparation. The buyers who do well are the ones who treat every listing as something to verify:
possession, encumbrances, extent and price. That verification is precisely what AuctionScope is built to
make faster, so you can tell a real opportunity from a costly one before you commit an EMD.</p>
""",
        "faqs": [
            {"q": "Are bank auctions genuine?",
             "a": ("Yes. Banks sell under the SARFAESI Act and issue a sale certificate on full payment. "
                   "The process is legitimate; the risks are about the specific property, not the "
                   "mechanism.")},
            {"q": "What is the biggest risk in a bank auction?",
             "a": ("For most buyers it is possession — buying a property the bank holds only symbolically, "
                   "where an occupant remains — closely followed by inherited dues. Both are checkable "
                   "before you bid.")},
            {"q": "How can I reduce the risk?",
             "a": ("Confirm possession status, pull the encumbrance certificate and check arrears, verify "
                   "the extent, compare the reserve to local rates, and line up financing before bidding. "
                   "Preparation is what makes an auction purchase safe.")},
        ],
        "related": ["as-is-where-is", "possession", "encumbrances", "reserve price", "EMD"],
    },
    {
        "slug": "common-bank-auction-myths",
        "title": "Common bank auction myths, and the reality",
        "h1": "Common bank auction myths",
        "description": ("Bank auctions attract a lot of half-truths. Six common myths about SARFAESI "
                        "auctions and what is actually true."),
        "updated": "2026-07-21",
        "answer": ("Common myths — that auctions are always cheap, always risky, only for cash buyers, or "
                   "that the reserve is the market value — are each half-true at best. The reality is that "
                   "a bank auction is a legitimate sale whose value depends entirely on the specific "
                   "property and the checks you run."),
        "body": """
<h2>Myth vs reality</h2>
<table class="kv">
<tr><th>Myth</th><th>Reality</th></tr>
<tr><td>"Auction property is always cheap."</td><td>Sometimes the reserve is below market, sometimes not.
Only a comparison to local rates tells you.</td></tr>
<tr><td>"The reserve price is the market value."</td><td>The reserve is a floor set from a valuation, not
a market price. It can sit above or below what the area commands.</td></tr>
<tr><td>"Auctions are only for cash buyers."</td><td>You need the EMD upfront and a fast balance payment,
but many buyers arrange financing — provided they plan it before bidding.</td></tr>
<tr><td>"Buying at auction is always risky."</td><td>The sale is legal and documented; the risks are
specific and checkable — possession, dues, condition.</td></tr>
<tr><td>"The bank guarantees clear title."</td><td>Sales are as-is-where-is, with encumbrances stated
only to the best of the bank's knowledge. You verify title and charges yourself.</td></tr>
<tr><td>"If I win, the property is immediately mine and vacant."</td><td>You pay in full, get a sale
certificate, register it — and vacant possession depends on whether the bank held physical or only
symbolic possession.</td></tr>
</table>

<h2>The thread running through all of them</h2>
<p>Every myth collapses into the same truth: a bank auction is neither magic nor a scam. What separates a
good purchase from a bad one is not the process but the homework on the individual listing — reserve
versus market, possession status, encumbrances and extent. Making that homework fast is what AuctionScope
is for.</p>
""",
        "faqs": [
            {"q": "Is auction property always cheaper than the market?",
             "a": ("No. The reserve can be below market, but it can also be at or above it. You have to "
                   "compare the reserve to real local rates for that area and property type before calling "
                   "it cheap.")},
            {"q": "Do banks guarantee a clear title at auction?",
             "a": ("No. Sales are as-is-where-is and the bank states known encumbrances only to the best of "
                   "its knowledge. Checking title and charges is the buyer's responsibility.")},
            {"q": "Can I use a home loan to buy at a bank auction?",
             "a": ("Many buyers arrange financing, but the EMD is due upfront and the balance within a "
                   "short window, so you must have funding lined up before you bid rather than assume it "
                   "afterwards.")},
        ],
        "related": ["reserve price", "as-is-where-is", "possession", "EMD"],
    },
    {
        "slug": "bank-auction-glossary",
        "title": "Bank auction glossary — key terms explained",
        "h1": "Bank auction glossary",
        "description": ("The jargon of SARFAESI bank auctions in plain language — EMD, reserve price, "
                        "encumbrance, possession, sale certificate, NPA and more."),
        "updated": "2026-07-21",
        "answer": ("A plain-language glossary of the terms you meet in a bank auction — SARFAESI, NPA, "
                   "reserve price, EMD, as-is-where-is, symbolic and physical possession, encumbrance, "
                   "sale certificate and UDS — so a sale notice reads clearly instead of cryptically."),
        "body": """
<h2>The terms</h2>
<table class="kv">
<tr><th>Term</th><th>Meaning</th></tr>
<tr><td>SARFAESI Act</td><td>The 2002 law letting banks enforce security and sell mortgaged property to
recover unpaid loans, without going to court first.</td></tr>
<tr><td>NPA (non-performing asset)</td><td>A loan the borrower has stopped repaying, which lets the bank
begin recovery.</td></tr>
<tr><td>Reserve price</td><td>The minimum bid the bank will accept — a floor, not the market value.</td></tr>
<tr><td>EMD (earnest money deposit)</td><td>A refundable deposit, commonly about 10% of the reserve, paid
to be allowed to bid.</td></tr>
<tr><td>As-is-where-is</td><td>You buy the property in its current condition and situation, with no
warranty from the bank.</td></tr>
<tr><td>Symbolic possession</td><td>The bank holds the property on paper; an occupant may still be
inside.</td></tr>
<tr><td>Physical possession</td><td>The bank has actual, vacant control of the property.</td></tr>
<tr><td>Encumbrance</td><td>A charge or claim on the property — mortgage, tax, arrears or lien.</td></tr>
<tr><td>EC (encumbrance certificate)</td><td>A record of registered charges and transactions on a
property; in Tamil Nadu, available from TNREGINET.</td></tr>
<tr><td>Sale certificate</td><td>The document issued after full payment, recording the sale to the
buyer.</td></tr>
<tr><td>UDS (undivided share)</td><td>A flat owner's share of the land under the building — separate from
the built-up area, and needed to value a flat correctly.</td></tr>
<tr><td>Authorised officer</td><td>The bank official who conducts the sale and issues the sale
certificate.</td></tr>
</table>

<h2>Reading a notice with the jargon decoded</h2>
<p>Most of a sale notice is just these terms strung together. Once they are clear, the notice tells a
simple story: what is being sold, for what minimum, with what deposit, in what condition, and with what
attached. When a term is still unclear on a specific listing, you can ask AuctionScope in plain
language rather than guessing.</p>
""",
        "faqs": [
            {"q": "What does EMD stand for?",
             "a": ("Earnest money deposit — a refundable deposit, commonly around 10% of the reserve price, "
                   "that you pay to take part in a bank auction. See the dedicated EMD guide for how "
                   "refunds and forfeiture work.")},
            {"q": "What is UDS in a flat?",
             "a": ("Undivided share — a flat owner's proportional share of the land the building sits on. "
                   "It is separate from the built-up area and is needed to value a flat correctly.")},
            {"q": "What is an authorised officer?",
             "a": ("The bank official who conducts the SARFAESI sale, handles the auction, and issues the "
                   "sale certificate to the winning bidder on full payment.")},
        ],
        "related": ["EMD", "reserve price", "as-is-where-is", "encumbrances", "UDS", "sale certificate"],
    },

    # ---- Ring 2 — TN property-buying essentials (research-verified; sources cited) ----
    {
        "slug": "patta-and-chitta-in-tamil-nadu",
        "title": "Patta and Chitta in Tamil Nadu — what they are and how to get them",
        "h1": "Patta and Chitta explained",
        "description": ("Patta and Chitta are the core land-ownership records in Tamil Nadu. What each one "
                        "shows, the difference, and how to view them free on the Revenue e-Services portal."),
        "updated": "2026-07-21",
        "answer": ("Patta is a Tamil Nadu revenue record that establishes who owns a piece of land — the "
                   "owner's name, patta number, survey and sub-division numbers, and extent. Chitta is a "
                   "related extract showing the land's type (wet or dry) and area. Both can be viewed on "
                   "the state's Revenue e-Services portal."),
        "body": """
<h2>What each record is</h2>
<table class="kv">
<tr><th>Record</th><th>What it shows</th></tr>
<tr><td>Patta</td><td>A revenue document establishing ownership of a land parcel — owner name, patta
number, survey number with sub-divisions, and the extent of the land.</td></tr>
<tr><td>Chitta</td><td>An extract from the village revenue accounts giving ownership details and the
land classification — whether it is <em>nanjai</em> (wet/irrigated) or <em>punjai</em> (dry).</td></tr>
</table>
<p>In Tamil Nadu these are now typically issued together, and are a first-line check that the person
selling a piece of land is recorded as its owner.</p>

<h2>How to get them</h2>
<p>You can view and download Patta and Chitta from the Revenue Department's e-Services portal
(eservices.tn.gov.in). You enter the district, taluk, village and survey/sub-division number, verify
with an OTP, and the record is shown. The service is online and low-cost — see the sources below for the
official portal.</p>

<h2>What to watch for — especially at auction</h2>
<ul>
<li><strong>Land, not flats.</strong> Patta is a record of land. For a flat, the relevant land interest
is the undivided share (UDS); an individual flat does not have its own patta the way a plot does.</li>
<li><strong>Name match.</strong> Check the patta name against the seller — or, at a bank auction, against
the borrower whose property is being sold.</li>
<li><strong>One record among several.</strong> Patta shows ownership in the revenue records; it is not by
itself proof of a clean chain of title. Pair it with the encumbrance certificate and the parent
documents.</li>
</ul>
<p>At a bank auction the notice may give only limited documents, so cross-checking patta and the
encumbrance certificate where you can is part of sizing up a listing — the kind of check AuctionScope is
built to help you run.</p>
""",
        "faqs": [
            {"q": "What is the difference between patta and chitta?",
             "a": ("Patta establishes ownership of the land — owner, survey number and extent. Chitta is an "
                   "extract showing the land's classification and area, such as whether it is wet or dry "
                   "land. In Tamil Nadu they are usually issued together.")},
            {"q": "How can I check patta online in Tamil Nadu?",
             "a": ("Through the Revenue Department's e-Services portal at eservices.tn.gov.in — enter the "
                   "district, taluk, village and survey number, verify with an OTP, and view the record. "
                   "See the official link in the sources.")},
            {"q": "Does a flat have a patta?",
             "a": ("Not in the way a plot does. Patta records land; a flat owner holds an undivided share "
                   "(UDS) of the land under the building rather than a separate patta for the flat itself.")},
        ],
        "related": ["chitta", "UDS", "encumbrance certificate", "FMB", "sale deed"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },
    {
        "slug": "adangal-and-a-register-tamil-nadu",
        "title": "Adangal and A-Register in Tamil Nadu — the land-use records",
        "h1": "Adangal and the A-Register",
        "description": ("Beyond patta, Tamil Nadu keeps cultivation and classification records — the Adangal "
                        "and the A-Register. What they record and why a buyer checks them."),
        "updated": "2026-07-21",
        "answer": ("The Adangal (Village Account No. 2) is a Tamil Nadu revenue record of how a piece of land "
                   "is used — cultivation, crops, irrigation and possession. The A-Register is the village's "
                   "settlement record of land classification, extent and tax. Together they add detail that "
                   "patta alone does not."),
        "body": """
<h2>What each record covers</h2>
<ul>
<li><strong>Adangal (Village Account No. 2).</strong> Maintained by the village administration, it records
the actual use of the land — who is cultivating it, the crops grown, the source of irrigation, and the
nature of possession. It is most relevant for agricultural land.</li>
<li><strong>A-Register.</strong> The village's permanent settlement register, recording each survey
number's classification, extent and the tax assessed on it — a reference for what the land officially
is.</li>
</ul>

<h2>Why a buyer looks at them</h2>
<p>Patta tells you who owns the land; the Adangal and A-Register tell you more about <em>what</em> the
land is and how it is being used. For agricultural or semi-urban parcels that can matter — the
classification affects what the land can be used for, and the possession entry can flag a gap between the
recorded owner and whoever is actually on the ground. At a bank auction, where possession is already a
key risk, that cross-check is useful.</p>

<h2>How to get them</h2>
<p>Both are available through the Revenue Department's e-Services portal (eservices.tn.gov.in), using the
district, taluk, village and survey number. As with all these records, treat what you pull as one input
and confirm anything material against the current official record — see the sources below.</p>
""",
        "faqs": [
            {"q": "What is the Adangal?",
             "a": ("The Adangal, also called Village Account No. 2, is a Tamil Nadu revenue record of land "
                   "use — cultivation, crops, irrigation source and possession — maintained at the village "
                   "level, and most relevant for agricultural land.")},
            {"q": "What is the A-Register?",
             "a": ("The A-Register is the village settlement record listing each survey number's "
                   "classification, extent and assessed tax — a reference for what a parcel of land "
                   "officially is.")},
            {"q": "Where can I get the Adangal online?",
             "a": ("From the Revenue Department's e-Services portal at eservices.tn.gov.in, using the "
                   "district, taluk, village and survey number. See the official link in the sources.")},
        ],
        "related": ["patta", "chitta", "land classification", "possession"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },
    {
        "slug": "encumbrance-certificate-tamil-nadu",
        "title": "Encumbrance certificate (EC) in Tamil Nadu — how to get it on TNREGINET",
        "h1": "The encumbrance certificate (EC)",
        "description": ("An EC lists the registered transactions on a property — sales, mortgages, charges. "
                        "What it shows, what it doesn't, and how to apply on TNREGINET."),
        "updated": "2026-07-21",
        "answer": ("An encumbrance certificate is an official record from the Sub-Registrar of every "
                   "registered transaction on a property over a period — sales, mortgages, gifts and "
                   "charges. In Tamil Nadu you apply for it on the TNREGINET portal. It is the core check "
                   "for existing loans or claims, but it only covers registered documents."),
        "body": """
<h2>What an EC shows</h2>
<p>The encumbrance certificate, issued by the Sub-Registrar's office, lists the registered transactions
recorded against a property for the date range you request — sales, mortgages, gifts, leases and other
charges. It is how you spot an existing home loan on the property, a prior sale, or a registered charge
that could affect what you are buying.</p>

<h2>What it does <em>not</em> show</h2>
<ul>
<li>Unregistered transactions or oral arrangements.</li>
<li>Pending court cases or disputes.</li>
<li>Unpaid property tax or utility arrears (those are separate checks).</li>
</ul>
<p>So a clean EC is reassuring but not the whole picture — read it alongside the parent documents, patta
and, for dues, the local body and utilities.</p>

<h2>How to apply on TNREGINET</h2>
<ol>
<li>Register and log in at the TNREGINET portal (tnreginet.gov.in).</li>
<li>Go to <strong>E-Services → Encumbrance Certificate → Search and Apply EC</strong>.</li>
<li>Enter the zone, district and sub-registrar office, the village and survey/sub-division number, the
date range, and the property details.</li>
<li>Pay the search fee and submit. Once the Sub-Registrar verifies the records, the signed EC is usually
uploaded to your account within a few working days.</li>
</ol>
<p>Search fees are modest — a small charge per year searched plus a computerization fee for records from
the late 1980s onward. Exact fees and processing times change, so confirm them on the portal (sources
below).</p>

<h2>The bank-auction angle</h2>
<p>A SARFAESI sale notice lists known encumbrances only "to the best of the bank's knowledge". Pulling
the EC yourself, where the survey details allow, is how you check that statement rather than take it on
trust before committing an EMD — exactly the kind of verification AuctionScope is designed to support.</p>
""",
        "faqs": [
            {"q": "How do I get an encumbrance certificate in Tamil Nadu?",
             "a": ("Apply online on the TNREGINET portal: register, go to E-Services → Encumbrance "
                   "Certificate → Search and Apply EC, enter the property and date range, pay the search "
                   "fee, and the signed EC is uploaded to your account in a few working days.")},
            {"q": "Does an EC guarantee a property is dispute-free?",
             "a": ("No. An EC covers registered transactions only — it does not show unregistered deals, "
                   "pending litigation, or unpaid tax and utility dues. Use it alongside the parent "
                   "documents and other checks.")},
            {"q": "How much does an EC cost in Tamil Nadu?",
             "a": ("A small search fee per year of records requested, plus a computerization fee for "
                   "post-1980s records. The exact amounts change, so confirm the current fee on TNREGINET.")},
        ],
        "related": ["patta", "sale deed", "encumbrances", "TNREGINET", "as-is-where-is"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
        ],
    },
    {
        "slug": "fmb-sketch-tamil-nadu",
        "title": "FMB sketch — what it is and how to check it (Tamil Nadu)",
        "h1": "What is an FMB sketch?",
        "description": ("An FMB sketch is the official survey map of a land parcel's shape and boundaries. "
                        "What an FMB sketch is, how to read it, and how to get it in Tamil Nadu."),
        "updated": "2026-07-21",
        "answer": ("An FMB — Field Measurement Book — sketch is the Tamil Nadu survey department's scaled "
                   "drawing of a land parcel, showing its actual shape, measurements and boundaries on all "
                   "sides. Unlike patta or the EC, it is about the physical extent of the land, and it is "
                   "how you check that the plot matches what is being sold."),
        "body": """
<h2>What the FMB is</h2>
<p>The Field Measurement Book is a register maintained by the Tamil Nadu survey and settlement department
holding ground measurements of every land parcel. The FMB <em>sketch</em> is the scaled map drawn from
those measurements — typically at 1:1000 or 1:2000 — showing the parcel's shape, orientation and
boundary lines on each side.</p>

<h2>FMB vs EC vs patta — they answer different questions</h2>
<table class="kv">
<tr><th>Record</th><th>Answers</th></tr>
<tr><td>Patta / Chitta</td><td>Who owns it, and what class of land it is.</td></tr>
<tr><td>Encumbrance certificate</td><td>What registered transactions and charges sit on it.</td></tr>
<tr><td>FMB sketch</td><td>Where its boundaries are and what shape and extent it actually has.</td></tr>
</table>

<h2>Why it matters before buying</h2>
<p>The FMB is how you check that the boundaries and extent on paper match the ground — that the plot is
the shape and size the sale describes, and that it is not encroached or mismatched with a neighbouring
survey number. For a land or plot purchase at auction, where you are buying <em>as-is-where-is</em> and
the notice states an extent, comparing that extent to the FMB is a direct sanity check on what you are
paying for.</p>

<h2>How to read an FMB sketch — what to check</h2>
<p>You don't need to be a surveyor to get value from an FMB. Run through four things:</p>
<ul>
<li><strong>Survey / sub-division number</strong> — it must match the number on the patta and the sale
notice. A mismatch means you may be looking at the wrong parcel.</li>
<li><strong>Shape and boundaries</strong> — the four sides and the parcel's shape should match what's on
the ground and what the seller describes.</li>
<li><strong>Extent</strong> — the measured area should tally with the extent stated in the notice once
you convert to a common unit. A gap here is a red flag.</li>
<li><strong>Neighbours and encroachment</strong> — adjoining survey numbers are marked; check the plot
isn't overlapping or encroached.</li>
</ul>

<h2>How to get an FMB sketch</h2>
<p>The FMB sketch is available through the Tamil Nadu land-records e-Services, keyed to the district,
taluk, village and survey number. Confirm the current access route and any fee on the official portal —
see the sources below.</p>
""",
        "faqs": [
            {"q": "What is an FMB sketch?",
             "a": ("A Field Measurement Book (FMB) sketch is the Tamil Nadu survey department's scaled "
                   "drawing of a land parcel, showing its actual shape, measurements and boundaries. It "
                   "reflects the physical layout of the land, not its ownership.")},
            {"q": "How do I read an FMB sketch?",
             "a": ("Check four things: the survey/sub-division number matches the patta and notice; the "
                   "shape and boundaries match the ground; the measured extent tallies with the stated "
                   "extent; and the plot isn't encroaching on or overlapping neighbouring survey numbers.")},
            {"q": "How is the FMB different from the encumbrance certificate?",
             "a": ("The FMB shows the parcel's measurements and boundaries; the EC shows its registered "
                   "transaction and charge history. One is about the physical land, the other about its "
                   "legal record — you check both.")},
            {"q": "Why check the FMB before buying at auction?",
             "a": ("Because an as-is-where-is sale states an extent without warranty. Comparing that extent "
                   "and the boundaries to the FMB confirms the plot is the shape and size described and "
                   "isn't encroached or mismatched.")},
        ],
        "related": ["patta", "extent", "survey number", "as-is-where-is"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },
    {
        "slug": "stamp-duty-and-registration-charges-tamil-nadu",
        "title": "Stamp duty and registration charges in Tamil Nadu",
        "h1": "Stamp duty and registration charges",
        "description": ("What it costs to register a property in Tamil Nadu — the stamp duty and registration "
                        "percentages, how they're calculated on guideline value, and the auction angle."),
        "updated": "2026-07-21",
        "answer": ("In Tamil Nadu a sale is commonly charged 7% stamp duty plus 4% registration fee — about "
                   "11% together — calculated on the higher of the sale value and the government guideline "
                   "value. Rates and concessions change, so confirm the current figures and the guideline "
                   "value on TNREGINET before you budget."),
        "body": """
<h2>The headline rates</h2>
<table class="kv">
<tr><th>Charge</th><th>Rate (as of 2026)</th></tr>
<tr><td>Stamp duty</td><td>7% of the property value</td></tr>
<tr><td>Registration fee</td><td>4% of the property value</td></tr>
<tr><td>Combined</td><td>~11% of the property value</td></tr>
</table>
<p>These are among the higher property-transfer charges in the country, and they are a real cost on top
of the price you pay — worth building into any purchase maths.</p>

<h2>What the percentage is charged on</h2>
<p>The charge is calculated on the <strong>higher</strong> of two figures: the actual sale/agreement
value, and the <strong>guideline value</strong> — the government-fixed minimum value for that locality,
which you can look up on TNREGINET. So even a low purchase price is taxed at least on the guideline
value.</p>

<h2>Concessions</h2>
<p>From 1 April 2025, Tamil Nadu offers a registration concession for women buyers where the property
value is below ₹10 lakh — 3% registration instead of 4%. Concessions and thresholds change, so verify
current eligibility on the official portal.</p>

<h2>The bank-auction angle</h2>
<p>Winning a SARFAESI auction is not the end of the cost. Stamp duty and registration are payable to
register the sale certificate in your name — so a reserve that looks like a bargain still carries roughly
another tenth of the value in charges. Factor guideline value and these percentages into whether a
listing is genuinely below market. AuctionScope helps you compare a reserve to local rates; the
registration cost is the piece to add on top.</p>
""",
        "faqs": [
            {"q": "What are the stamp duty and registration charges in Tamil Nadu?",
             "a": ("As of 2026, commonly 7% stamp duty and 4% registration fee — about 11% combined — on "
                   "the higher of the sale value and the guideline value. Confirm current rates on "
                   "TNREGINET, as they change.")},
            {"q": "What is guideline value?",
             "a": ("The government-fixed minimum value for a locality, used as the floor for calculating "
                   "stamp duty and registration. You can look it up for a specific property on the "
                   "TNREGINET portal.")},
            {"q": "Do I pay stamp duty on a bank auction property?",
             "a": ("Yes. Stamp duty and registration are payable to register the sale certificate in your "
                   "name after you win, so budget roughly another tenth of the value on top of your bid.")},
        ],
        "related": ["guideline value", "sale certificate", "reserve price", "TNREGINET"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
            {"t": "Stamp duty and registration charges in Tamil Nadu (2026) — ClearTax",
             "h": "https://cleartax.in/s/stamp-duty-and-registration-charges-in-tamil-nadu"},
        ],
    },
    {
        "slug": "verify-property-documents-before-buying-tamil-nadu",
        "title": "How to verify property documents before buying in Tamil Nadu",
        "h1": "Verifying property documents in Tamil Nadu",
        "description": ("The document checklist for a Tamil Nadu property — sale deed and parent documents, "
                        "patta, EC, FMB, guideline value and tax receipts — and how it applies at auction."),
        "updated": "2026-07-21",
        "answer": ("Before buying property in Tamil Nadu, check the chain of title through the sale deed and "
                   "its parent documents, confirm ownership via patta/chitta, pull the encumbrance "
                   "certificate for registered charges, verify boundaries against the FMB, look up the "
                   "guideline value, and check tax receipts. At auction the same checks apply to the extent "
                   "you can run them."),
        "body": """
<h2>The core checks</h2>
<table class="kv">
<tr><th>Document</th><th>What it confirms</th></tr>
<tr><td>Sale deed + parent (mother) documents</td><td>The chain of title — how ownership passed down to
the current owner over successive transactions.</td></tr>
<tr><td>Patta / Chitta</td><td>That the seller is recorded as the owner, and the land class.</td></tr>
<tr><td>Encumbrance certificate (EC)</td><td>Registered charges — existing loans, mortgages, prior
sales.</td></tr>
<tr><td>FMB sketch</td><td>That the boundaries and extent match the ground.</td></tr>
<tr><td>Guideline value</td><td>A price sanity-check and the basis for stamp duty.</td></tr>
<tr><td>Property tax and utility receipts</td><td>That dues are paid up, or what arrears exist.</td></tr>
</table>

<h2>How this changes at a bank auction</h2>
<p>At a SARFAESI auction you are buying from the bank, and the sale notice usually gives only a limited
document set. You often cannot get the seller's full paperwork — but you can still do a lot: pull the EC
and patta from the survey details, compare the stated extent to the FMB, and look up the guideline
value. After you win and pay in full, the <strong>sale certificate</strong> the bank issues becomes your
title document, which you then register.</p>

<h2>Turn a checklist into a decision</h2>
<p>Each of these records answers one question — ownership, charges, boundaries, price, dues — and no
single one is the whole story. The skill is pulling them together for a specific property under an
auction deadline. That is exactly what AuctionScope is built to speed up: ask, in plain language, what a
listing's documents and numbers say, so you go into a bid informed rather than rushed. None of this is
legal advice — for a high-value purchase, have the documents reviewed by a professional too.</p>
""",
        "faqs": [
            {"q": "What documents should I check before buying property in Tamil Nadu?",
             "a": ("The sale deed and its parent documents for the chain of title, patta/chitta for "
                   "ownership, the encumbrance certificate for registered charges, the FMB for boundaries, "
                   "the guideline value for price, and tax receipts for dues.")},
            {"q": "Can I verify documents for a bank auction property?",
             "a": ("Partly. The bank usually shares a limited set, but from the survey details you can "
                   "often still pull the EC and patta, compare the extent to the FMB, and check the "
                   "guideline value. After winning, the sale certificate becomes your title document.")},
            {"q": "Is a clean encumbrance certificate enough on its own?",
             "a": ("No. An EC covers registered transactions only. Combine it with the parent documents, "
                   "patta, FMB and tax receipts — and for a big purchase, a professional review — for a "
                   "fuller picture.")},
        ],
        "related": ["sale deed", "patta", "encumbrance certificate", "FMB", "guideline value", "sale certificate"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },
    {
        "slug": "guideline-value-tamil-nadu",
        "title": "Guideline value in Tamil Nadu — what it is and how to check it",
        "h1": "Guideline value explained",
        "description": ("Guideline value is the government's minimum value for a property, and the floor for "
                        "stamp duty. What it is, how to check it free on TNREGINET, and why it matters at "
                        "auction."),
        "updated": "2026-07-21",
        "answer": ("Guideline value is the minimum value the Tamil Nadu government fixes for every street and "
                   "survey number — the legal floor below which a property cannot be registered. Stamp duty "
                   "is charged on the higher of the sale price and the guideline value. You can look it up "
                   "free on TNREGINET, without logging in."),
        "body": """
<h2>What it is</h2>
<p>The guideline value — also called government value — is the minimum value the Registration Department
assigns to each street, survey number and locality. No property can be registered below it during a
sale, which is how the state guards against under-declaration. It is revised periodically; the
department carried out an annual revision in January 2026.</p>

<h2>Why it matters to a buyer</h2>
<ul>
<li><strong>It sets your tax floor.</strong> Stamp duty and registration are charged on the higher of the
sale price and the guideline value — so even a low purchase price is taxed at least on the guideline
value.</li>
<li><strong>It is a price sanity-check.</strong> Comparing a property's asking or reserve price to its
guideline value gives a rough read on whether the price is in a sensible range, though guideline value
is a floor and not the true market rate.</li>
</ul>

<h2>How to check it</h2>
<p>Go to the TNREGINET portal (tnreginet.gov.in), open the Guideline Value option, choose the zone and
sub-registrar office, and search by street name or survey number. No login is needed and the lookup is
free — see the sources below.</p>

<h2>The bank-auction angle</h2>
<p>At auction, guideline value works two ways. It tells you the minimum on which you will pay ~11% stamp
duty and registration when you register the sale certificate — a real cost to add to your bid. And
alongside the actual local market rate, it helps you judge whether a reserve is genuinely low. AuctionScope
helps you compare a reserve to local rates; the guideline value is a second reference point to pull in.</p>
""",
        "faqs": [
            {"q": "What is guideline value in Tamil Nadu?",
             "a": ("The minimum value the government fixes for each street and survey number, below which a "
                   "property cannot be registered. It is the floor used to calculate stamp duty and "
                   "registration charges.")},
            {"q": "How do I check the guideline value of a property?",
             "a": ("On the TNREGINET portal (tnreginet.gov.in): open Guideline Value, pick the zone and "
                   "sub-registrar office, and search by street name or survey number. It is free and needs "
                   "no login.")},
            {"q": "Is guideline value the same as market value?",
             "a": ("No. Guideline value is a government-set floor for registration and tax; market value is "
                   "what the property actually trades at, which can be higher. Use guideline value as a "
                   "reference, not the market price.")},
        ],
        "related": ["stamp duty", "market value", "reserve price", "TNREGINET"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
            {"t": "TNREGINET guideline value (2026) — ClearTax",
             "h": "https://cleartax.in/s/tnreginet-guideline-value"},
        ],
    },
    {
        "slug": "sale-deed-and-parent-documents-tamil-nadu",
        "title": "Sale deed and parent documents — the chain of title",
        "h1": "Sale deed and parent documents",
        "description": ("The sale deed transfers ownership; the parent documents show how title reached the "
                        "seller. Why both matter, and how it works when you buy at auction."),
        "updated": "2026-07-21",
        "answer": ("A sale deed is the registered document that transfers ownership of a property to the "
                   "buyer. Parent (or mother) documents are the earlier deeds that show how ownership passed "
                   "down to the current seller — the chain of title. Checking that chain is unbroken is a "
                   "core pre-purchase step."),
        "body": """
<h2>The two things you are checking</h2>
<ul>
<li><strong>The sale deed</strong> is the instrument that actually transfers ownership. To be valid it is
executed on stamp paper and registered at the sub-registrar's office under the Registration Act.</li>
<li><strong>Parent / mother documents</strong> are the earlier registered deeds — previous sale deeds,
partition deeds, gift deeds, inheritance records — that trace ownership backwards over time. Together
they form the <em>chain of title</em>.</li>
</ul>

<h2>Why the chain matters</h2>
<p>A current sale deed tells you the last transfer; the parent documents tell you whether that transfer
rested on solid ground. A gap or inconsistency in the chain — a missing link, an unexplained jump in
ownership — is a warning sign worth resolving before you buy. This is where the encumbrance certificate
helps too: it lists the registered transactions that should line up with the deeds.</p>

<h2>How it works at a bank auction</h2>
<p>At a SARFAESI auction you usually cannot get the seller's full document set — the bank shares a
limited pack. What changes is the end state: once you win and pay in full, the bank issues a
<strong>sale certificate</strong>, and that becomes your title document, which you register like a sale
deed. Where the survey details allow, you can still pull the encumbrance certificate to sanity-check the
history. Reading a limited document set under a deadline is exactly the situation AuctionScope is built
to make faster.</p>
""",
        "faqs": [
            {"q": "What is a parent document in a property sale?",
             "a": ("A parent or mother document is an earlier registered deed — a previous sale, partition, "
                   "gift or inheritance record — that shows how ownership passed down to the current seller. "
                   "The set of them forms the chain of title.")},
            {"q": "What is the difference between a sale deed and a sale certificate?",
             "a": ("A sale deed transfers a property in an ordinary sale between buyer and seller. A sale "
                   "certificate is what a bank issues to the winning bidder after a SARFAESI auction; you "
                   "register it, and it serves as your title document.")},
            {"q": "Why check the chain of title?",
             "a": ("Because a valid current deed can still rest on a broken history. Tracing the parent "
                   "documents, alongside the encumbrance certificate, is how you check ownership passed "
                   "down cleanly to the seller.")},
        ],
        "related": ["encumbrance certificate", "patta", "sale certificate", "registration"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
        ],
    },
    {
        "slug": "patta-transfer-and-mutation-tamil-nadu",
        "title": "Patta transfer (mutation) in Tamil Nadu — after you buy",
        "h1": "Patta transfer and mutation",
        "description": ("Registering a sale deed doesn't update the land record. Patta transfer — mutation — "
                        "puts your name in the revenue record. How to do it, and why it matters after an "
                        "auction."),
        "updated": "2026-07-21",
        "answer": ("Registering the sale deed transfers ownership, but it does not change the revenue "
                   "record. Patta transfer, or mutation, updates the patta into the buyer's name. In Tamil "
                   "Nadu you apply on the Revenue e-Services portal; a village officer inspects and the "
                   "tahsildar approves. Skip it and utilities, tax and resale get complicated."),
        "body": """
<h2>Why registration alone isn't enough</h2>
<p>A registered sale deed makes you the owner in the registration records — but the <em>revenue</em>
record, the patta, still shows the previous owner until you apply to change it. That second step is
mutation, or patta transfer.</p>

<h2>What goes wrong if you skip it</h2>
<ul>
<li>Utility connections (electricity, water) can't move to your name.</li>
<li>The property-tax demand stays against the seller.</li>
<li>A future resale, building approval or loan against the property can stall.</li>
</ul>

<h2>How to apply</h2>
<p>You apply through the Revenue Department's e-Services portal (eservices.tn.gov.in). A village
administrative officer conducts a field inspection to verify the buyer, seller and boundaries, and the
tahsildar reviews it and approves the mutation, issuing the updated patta. Documents usually needed
include the registered sale deed (or sale certificate), the encumbrance certificate, property-tax
receipts, ID proof and the existing patta. Routine transfers commonly close in a few weeks, though it
varies; you can track the application status on the same portal. Confirm current fees and steps on the
official portal — see the sources.</p>

<h2>The bank-auction angle</h2>
<p>After you win a SARFAESI auction, pay in full and register the sale certificate, patta transfer is the
step that puts the land record in your name. Building it into your post-purchase plan — with the sale
certificate and encumbrance certificate ready — avoids the utility and tax headaches later.</p>
""",
        "faqs": [
            {"q": "Does patta change automatically after registration?",
             "a": ("No. Registering the sale deed does not update the patta. You have to apply separately "
                   "for patta transfer (mutation) to get the revenue record into your name.")},
            {"q": "How do I apply for patta transfer in Tamil Nadu?",
             "a": ("Through the Revenue e-Services portal (eservices.tn.gov.in). A village officer inspects "
                   "and the tahsildar approves, using your sale deed or sale certificate, the encumbrance "
                   "certificate, tax receipts, ID and existing patta. You can track status online.")},
            {"q": "Do I need patta transfer after a bank auction?",
             "a": ("Yes. Once you have paid in full and registered the sale certificate, apply for patta "
                   "transfer so the land record reflects your ownership — otherwise utilities, tax and "
                   "resale stay tied to the previous owner.")},
        ],
        "related": ["patta", "sale certificate", "encumbrance certificate", "property tax"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
            {"t": "Apply for Online Patta Transfer, Tamil Nadu — National Government Services Portal",
             "h": "https://services.india.gov.in/service/detail/apply-for-online-patta-transfer-tamil-nadu"},
        ],
    },
    {
        "slug": "sale-agreement-vs-sale-deed",
        "title": "Sale agreement vs sale deed — what's the difference?",
        "h1": "Sale agreement vs sale deed",
        "description": ("A sale agreement is a promise to sell; a sale deed is the actual transfer. Confusing "
                        "the two is a common and costly mistake. How they differ, and where auctions fit."),
        "updated": "2026-07-21",
        "answer": ("A sale agreement (agreement to sell) is a promise to transfer a property on agreed terms "
                   "in future — it does not make you the owner. A sale deed is the registered document that "
                   "actually transfers ownership. Paying on an agreement without the deed being executed "
                   "and registered is a common trap."),
        "body": """
<h2>The core difference</h2>
<table class="kv">
<tr><th></th><th>Sale agreement</th><th>Sale deed</th></tr>
<tr><td>What it is</td><td>A promise to sell on agreed terms, often with a token/advance.</td><td>The
actual transfer of ownership.</td></tr>
<tr><td>Ownership</td><td>Stays with the seller.</td><td>Passes to the buyer.</td></tr>
<tr><td>Registration</td><td>Not always registered.</td><td>Executed on stamp paper and registered at the
sub-registrar's office.</td></tr>
</table>

<h2>Why the distinction matters</h2>
<p>An agreement to sell sets out conditions — price, timeline, what happens if either side backs out — but
you are not the owner until the sale deed is executed and registered. Handing over large sums on an
agreement, without the deed following, is one of the more common ways buyers get into trouble. Treat the
agreement as a step towards the deed, not a substitute for it.</p>

<h2>Where auctions fit</h2>
<p>A bank auction does not use a sale agreement in this sense. You bid, pay, and the bank issues a
<strong>sale certificate</strong> that transfers the property to you — the certificate, once registered,
plays the role the sale deed does in an ordinary sale. So at auction the thing to track is the payment
timeline and the sale certificate, not an agreement to sell.</p>
""",
        "faqs": [
            {"q": "Does a sale agreement transfer ownership?",
             "a": ("No. A sale agreement is a promise to sell on agreed terms; ownership passes only when "
                   "the sale deed is executed and registered. Until then the seller remains the owner.")},
            {"q": "Is a sale agreement registered?",
             "a": ("Not always — practices vary. The sale deed, by contrast, must be executed on stamp "
                   "paper and registered at the sub-registrar's office to transfer ownership.")},
            {"q": "Do bank auctions use a sale agreement?",
             "a": ("No. At a SARFAESI auction you pay and receive a sale certificate, which — once "
                   "registered — transfers the property. There is no separate agreement-to-sell step.")},
        ],
        "related": ["sale deed", "sale certificate", "registration", "EMD"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
        ],
    },
    {
        "slug": "how-to-check-rera-project-tamil-nadu",
        "title": "How to check a RERA-registered project in Tamil Nadu (TNRERA)",
        "h1": "Checking a project on TNRERA",
        "description": ("For a new or under-construction project, TNRERA registration is a key check. How to "
                        "verify a project and promoter free on the official TNRERA portal."),
        "updated": "2026-07-21",
        "answer": ("TNRERA is Tamil Nadu's real-estate regulator. Any project with more than 8 units or over "
                   "500 sq m must register before it is marketed, and only registered projects can legally "
                   "take booking amounts. You can verify a project and its promoter free on the official "
                   "TNRERA portal, no login needed."),
        "body": """
<h2>What TNRERA registration tells you</h2>
<p>Under the Real Estate (Regulation and Development) Act, a Tamil Nadu project with more than 8 units or
a plot over 500 square metres must be registered with TNRERA before it is advertised or sold, and only
a registered project can legally accept booking money. A valid registration is a baseline signal that a
new project is on the regulator's record.</p>

<h2>How to check</h2>
<ol>
<li>Go to the TNRERA portal (rera.tn.gov.in).</li>
<li>Open Registered Projects / Project Status.</li>
<li>Search by project name, promoter (developer) name, district, or registration number.</li>
<li>Read the status — Registered, Pending, Rejected or Revoked — and the promoter's other projects and
any orders against them.</li>
</ol>
<p>No login is needed for a basic search. See the official portal in the sources below.</p>

<h2>How this relates to auctions — honestly</h2>
<p>RERA is aimed at <strong>new and under-construction</strong> projects, so it is most useful when you
are buying from a builder. Bank auctions are usually resale of an individual, already-built property, so
TNRERA often will not apply to a specific auction listing. It belongs in your toolkit for the wider
property search rather than for most auction lots — and knowing when a check does <em>not</em> apply is
as useful as knowing when it does.</p>
""",
        "faqs": [
            {"q": "How do I check if a project is RERA registered in Tamil Nadu?",
             "a": ("On the TNRERA portal (rera.tn.gov.in), open Registered Projects or Project Status and "
                   "search by project name, developer, district or registration number. The status shows "
                   "as Registered, Pending, Rejected or Revoked.")},
            {"q": "Which projects must register with TNRERA?",
             "a": ("A Tamil Nadu project with more than 8 units or a plot over 500 square metres must "
                   "register before it is marketed, and only registered projects can legally accept booking "
                   "amounts.")},
            {"q": "Does RERA apply to bank auction properties?",
             "a": ("Usually not. RERA covers new and under-construction projects, while bank auctions are "
                   "typically resale of an already-built individual property, so TNRERA often will not "
                   "apply to a specific auction lot.")},
        ],
        "related": ["TNRERA", "builder verification", "sale deed", "guideline value"],
        "sources": [
            {"t": "TNRERA — Tamil Nadu Real Estate Regulatory Authority (official portal)",
             "h": "https://rera.tn.gov.in/"},
        ],
    },
    {
        "slug": "home-loan-for-a-bank-auction-property",
        "title": "Can you get a home loan for a bank auction property?",
        "h1": "Financing a bank auction property",
        "description": ("Yes, but it's harder than a normal purchase, and the clock is tight. How auction "
                        "financing works, why possession type matters, and why pre-approval is essential."),
        "updated": "2026-07-21",
        "answer": ("You can finance a bank auction property, but it is harder than an ordinary purchase and "
                   "the timeline is unforgiving. Lenders want a clear title, a sale certificate and usually "
                   "physical possession; symbolic-possession lots are hard to fund. Because the balance is "
                   "due in 15–30 days, arrange pre-approval before you bid."),
        "body": """
<h2>It's possible — but with stricter rules</h2>
<p>Many lenders do finance auction purchases, but they apply tougher conditions than for a normal home
loan. Common requirements include a clear title with no active tribunal or court case, a valid sale
certificate from the selling bank, and a healthy credit score. Most lenders strongly prefer — and often
require — that the property is in <strong>physical</strong> possession.</p>

<h2>Possession type decides a lot</h2>
<p>Very few lenders will fund a property held only under <strong>symbolic</strong> possession, because the
occupancy risk is unresolved. The practical rule buyers use: treat a symbolic-possession lot as something
to buy with your own funds, not a loan. This is one more reason the possession field on the notice
matters before you bid.</p>

<h2>The timeline problem</h2>
<p>After you win, 25% is due almost immediately and the balance within 15–30 days. With a pre-approval in
place, disbursal can complete inside that window; without one, loan processing can run longer and blow
past the deadline — at which point your deposit is forfeited. So the financing has to be lined up
<em>before</em> the auction, not started after you win.</p>

<h2>A practical tip</h2>
<p>The bank that is selling the property is often the most willing to finance it — it already knows the
title and possession status and can move faster. Whatever the lender, confirm your eligibility and the
exact timeline first. AuctionScope helps you weigh a listing's reserve, possession and location up front;
the financing plan is the piece to have ready alongside it.</p>
""",
        "faqs": [
            {"q": "Can I get a home loan to buy a bank auction property?",
             "a": ("Often yes, but with stricter conditions than a normal purchase — a clear title, a sale "
                   "certificate, usually physical possession, and a good credit score. Symbolic-possession "
                   "properties are hard to finance and are best bought with your own funds.")},
            {"q": "Why does pre-approval matter for an auction?",
             "a": ("Because the balance is due within 15–30 days of winning. With pre-approval, disbursal "
                   "can fit that window; without it, processing can run longer and cause you to miss the "
                   "deadline and forfeit your deposit.")},
            {"q": "Which lender is most likely to finance an auction property?",
             "a": ("Frequently the bank selling the property — it already knows the title and possession "
                   "status and can process faster. Confirm eligibility and timelines with any lender before "
                   "you bid.")},
        ],
        "related": ["EMD", "symbolic vs physical possession", "sale certificate", "reserve price"],
        "sources": [
            {"t": "Will a bank give a loan on an auction property? — eauctionsindia.com",
             "h": "https://www.eauctionsindia.com/blog-details/will-bank-give-loan-on-auction-property"},
            {"t": "Buying a bank auction property in India (2026) — BasicHomeLoan",
             "h": "https://www.basichomeloan.com/blog/home-loans/buy-bank-auction-property-in-india"},
        ],
    },
    {
        "slug": "power-of-attorney-property-sales",
        "title": "Power of attorney in property sales — why a GPA is not a title",
        "h1": "Power of attorney and property title",
        "description": ("A general power of attorney does not transfer ownership — the Supreme Court settled "
                        "this. Why GPA 'sales' are a red flag and what actually transfers title."),
        "updated": "2026-07-21",
        "answer": ("A general power of attorney (GPA) authorises someone to act for the owner — it does not "
                   "transfer ownership. The Supreme Court held in the Suraj Lamp case (2011) that title to "
                   "immovable property passes only by a registered sale deed. A property being 'sold' on a "
                   "GPA is a title red flag to check carefully."),
        "body": """
<h2>What a power of attorney is — and isn't</h2>
<p>A power of attorney lets an agent act on the owner's behalf, including signing a sale deed. It is a
tool of agency, not a transfer of ownership. A common trap is a "GPA sale" — where property is handed
over on a general power of attorney plus an agreement to sell, without a registered sale deed. That does
not make the buyer the owner.</p>

<h2>What the Supreme Court held</h2>
<p>In <em>Suraj Lamp &amp; Industries v. State of Haryana</em> (2011), the Supreme Court held that a
GPA / agreement-to-sell / will arrangement does <strong>not</strong> convey title or create an interest
in immovable property. Under the Transfer of Property Act and the Registration Act, title passes only
through a registered sale deed. The court has since reiterated these limits.</p>

<h2>The risks of a GPA transaction</h2>
<ul>
<li><strong>No title.</strong> The buyer does not become the legal owner.</li>
<li><strong>Revocation.</strong> A GPA can lapse — for instance on the principal's death — unless coupled
with an interest.</li>
<li><strong>Fraud.</strong> GPA chains are a known vector for misuse and disputed ownership.</li>
</ul>

<h2>Why it matters at auction</h2>
<p>A bank auction itself transfers property through a sale certificate, not a GPA, so the mechanism is
sound. But if a property's <em>history</em> — its chain of title — includes a GPA "sale", that is a weak
link worth scrutinising, because ownership may never have passed cleanly. Spotting that kind of flag in
the documents is part of the pre-bid homework AuctionScope is built to speed up. None of this is legal
advice; for a high-value purchase, have the title examined by a professional.</p>
""",
        "faqs": [
            {"q": "Can property be sold through a power of attorney?",
             "a": ("A power of attorney can let an agent sign a sale deed, but a GPA by itself does not "
                   "transfer ownership. The Supreme Court held in Suraj Lamp (2011) that title passes only "
                   "through a registered sale deed.")},
            {"q": "Is a GPA sale valid in India?",
             "a": ("A 'GPA sale' does not convey title or create an interest in the property. It is a red "
                   "flag — a registered sale deed is the instrument that actually transfers ownership.")},
            {"q": "What are the risks of buying on a GPA?",
             "a": ("You may not become the legal owner, the GPA can lapse (for example on the principal's "
                   "death), and GPA chains carry a higher fraud and dispute risk. Insist on a registered "
                   "sale deed.")},
        ],
        "related": ["sale deed", "chain of title", "sale certificate", "encumbrance certificate"],
        "sources": [
            {"t": "Agreement to sell, power of attorney and will — Supreme Court reiterates limits on transfer of title (SCC Online)",
             "h": "https://www.scconline.com/blog/post/2025/10/07/agreement-to-sell-power-of-attorney-and-will-supreme-court-reiterates-limits-on-transfer-of-title/"},
            {"t": "GPA in property sales: why they are not legally valid in India — BasicHomeLoan",
             "h": "https://www.basichomeloan.com/blog/home-loans/gpa-in-property-sales"},
        ],
    },
    {
        "slug": "property-tax-and-arrears-tamil-nadu",
        "title": "Property tax and arrears in Tamil Nadu — what a buyer checks",
        "h1": "Property tax and arrears",
        "description": ("Unpaid property tax can follow the property. How Tamil Nadu property tax works, how "
                        "to check arrears online, and why it matters before you buy at auction."),
        "updated": "2026-07-21",
        "answer": ("Property tax in Tamil Nadu is charged by the urban local body — in Chennai, the Greater "
                   "Chennai Corporation. It is due half-yearly, with a penalty for late payment. Unpaid "
                   "arrears can end up as the buyer's problem in an as-is-where-is sale, so check them "
                   "online before you bid."),
        "body": """
<h2>How it works</h2>
<p>Urban local bodies levy property tax on buildings within their limits. In Chennai the Greater Chennai
Corporation collects it, half-yearly, with due dates of 30 September and 31 March and a penalty
(commonly 1% per month) on delayed payment after a short grace period. Other cities and towns have their
own local-body portals.</p>

<h2>Why it matters to a buyer</h2>
<p>In an <em>as-is-where-is</em> auction sale, the notice often places outstanding dues — property tax
included — on the buyer. So arrears against a property are effectively a hidden cost you could inherit,
eating into any saving from a low reserve. Checking them before you bid turns a nasty surprise into a
known number.</p>

<h2>How to check arrears online</h2>
<p>For a Chennai property, you can look up dues on the Greater Chennai Corporation's property-tax page
using the zone, division and bill number (or the old property-tax number), which shows the arrears
breakdown, penalty and total payable. For properties elsewhere in Tamil Nadu, use the relevant local
body's portal. See the sources below, and confirm the current figures on the official site.</p>

<h2>After you buy</h2>
<p>Once you own the property, get the property-tax record transferred to your name (linked to patta
transfer / mutation) so future demands come to you and the record is clean. AuctionScope helps you size
up a listing's price and risks; outstanding tax is one of the dues to fold into that view.</p>
""",
        "faqs": [
            {"q": "Do I inherit unpaid property tax on a bank auction property?",
             "a": ("Often yes. As-is-where-is sale notices commonly place outstanding property tax on the "
                   "buyer, so check the arrears before bidding and factor them into the price.")},
            {"q": "How do I check property tax arrears in Chennai?",
             "a": ("On the Greater Chennai Corporation property-tax page, using the zone, division and bill "
                   "number (or the old property-tax number). It shows the arrears breakdown, penalty and "
                   "total payable. Other Tamil Nadu cities use their own local-body portals.")},
            {"q": "When is property tax due in Tamil Nadu?",
             "a": ("Typically half-yearly — 30 September and 31 March in Chennai — with a penalty for late "
                   "payment after a short grace period. Confirm current dates and rates on the local-body "
                   "portal.")},
        ],
        "related": ["encumbrances", "patta transfer", "as-is-where-is", "sale certificate"],
        "sources": [
            {"t": "Property tax online payment — Greater Chennai Corporation (official)",
             "h": "https://chennaicorporation.gov.in/gcc/online-payment/property-tax/"},
        ],
    },
    {
        "slug": "building-approval-and-occupancy-certificate-tamil-nadu",
        "title": "Building approval and occupancy certificate in Tamil Nadu (CMDA / DTCP)",
        "h1": "Building approval and the occupancy certificate",
        "description": ("An unapproved building or missing occupancy certificate is a real risk — and blocks "
                        "loans. What CMDA/DTCP approval and an OC mean, and how to check them."),
        "updated": "2026-07-21",
        "answer": ("In Tamil Nadu, building plans and layouts are approved by DTCP (statewide) or CMDA (the "
                   "Chennai metro area), and an occupancy certificate confirms the building was completed to "
                   "the approved plan. An unapproved layout or missing OC blocks home loans and can cause "
                   "registration and regularisation trouble — so both are worth checking."),
        "body": """
<h2>Who approves what</h2>
<p>The Directorate of Town and Country Planning (DTCP) handles planning approval across Tamil Nadu, while
the Chennai Metropolitan Development Authority (CMDA) approves layouts and larger buildings within the
Chennai metropolitan area. Approvals are handled through the state's online planning-permission portal.</p>

<h2>What an occupancy certificate is</h2>
<p>The occupancy certificate (OC), issued by the civic authority, confirms that a building was constructed
in line with its approved plan and is legally safe to occupy. For a flat especially, the OC is a key
document — its absence signals the building may deviate from what was sanctioned.</p>

<h2>Why unapproved property is risky</h2>
<ul>
<li><strong>No loan.</strong> Banks routinely require verifiable DTCP or CMDA approval before sanctioning
a home loan — no approval, no loan.</li>
<li><strong>Registration and regularisation issues.</strong> An unauthorised layout can face building-
permission refusal and, in disputed cases, registration hurdles.</li>
<li><strong>Penalties and uncertainty.</strong> Deviations can attract regularisation costs later.</li>
</ul>

<h2>What to check — and the auction angle</h2>
<p>Ask for certified copies of the layout approval, the building-plan sanction and the occupancy
certificate, and cross-check them against the actual structure. At a bank auction you are buying
<em>as-is-where-is</em>, and an unapproved building or missing OC is exactly the kind of issue the notice
will not resolve for you — and it may also be why you cannot finance the purchase. Flagging it before you
bid is part of the homework AuctionScope is built to make faster.</p>
""",
        "faqs": [
            {"q": "What is the difference between CMDA and DTCP approval?",
             "a": ("DTCP handles planning approval across Tamil Nadu, while CMDA approves layouts and larger "
                   "buildings within the Chennai metropolitan area. Both signal that a layout or building "
                   "plan is authorised.")},
            {"q": "Why does an occupancy certificate matter?",
             "a": ("The OC confirms a building was completed to its approved plan and is safe to occupy. Its "
                   "absence suggests the structure may deviate from what was sanctioned, and it can block "
                   "loans and cause disputes.")},
            {"q": "Can I get a loan on an unapproved property?",
             "a": ("Generally no. Banks routinely require verifiable DTCP or CMDA approval before "
                   "sanctioning a home loan, so an unapproved layout or building usually cannot be "
                   "financed.")},
        ],
        "related": ["occupancy certificate", "home loan", "as-is-where-is", "sale certificate"],
        "sources": [
            {"t": "Tamil Nadu Single Window Portal for Planning Permission (official)",
             "h": "https://onlineppa.tn.gov.in/"},
        ],
    },
    {
        "slug": "prohibited-property-and-poramboke-land-tamil-nadu",
        "title": "Prohibited property and poramboke land in Tamil Nadu",
        "h1": "Prohibited property and poramboke land",
        "description": ("Some land simply cannot be sold or registered — government, poramboke and "
                        "prohibited-list property. What Section 22-A means and why it matters at auction."),
        "updated": "2026-07-21",
        "answer": ("Under Section 22-A of the Registration Act, Tamil Nadu maintains a list of prohibited "
                   "properties — government and local-authority land, poramboke, and religious, wakf and "
                   "Bhoodan land — that Sub-Registrars must refuse to register. Poramboke is government "
                   "land for public use; a document over it does not create valid title, even if somehow "
                   "registered."),
        "body": """
<h2>What Section 22-A does</h2>
<p>Section 22-A of the Registration Act (as amended in Tamil Nadu) lets the government maintain a
<strong>prohibited properties list</strong>. Sub-Registrars must refuse to register instruments relating
to property belonging to the state or a local authority, poramboke land, and certain religious, wakf and
Bhoodan properties, unless a competent authority's sanction is produced.</p>

<h2>What poramboke means</h2>
<p>Poramboke is government land set aside for public purposes — water bodies, tanks, roads, canals and
the like. Most poramboke cannot be assigned or sold; some categories, such as Natham poramboke, may be
regularised under specific rules. Courts in Tamil Nadu have repeatedly held that a patta granted over
poramboke reserved for a public purpose is invalid.</p>

<h2>Registration does not change classification</h2>
<p>This is the key trap: even if a document over such land somehow gets registered, registration does not
change the land's government classification. It stays poramboke, and the "owner" holds no valid title. A
suspiciously cheap plot is exactly where this risk hides.</p>

<h2>The bank-auction angle</h2>
<p>Banks auction property they hold as security, which is normally private land — but the classification
is still worth confirming, especially for a low-priced plot. The chitta and A-register show the land
classification, and the prohibited-properties position can be checked with the registration department.
Confirming a listing is ordinary, registrable private land before you bid is part of the homework
AuctionScope is built to speed up.</p>
""",
        "faqs": [
            {"q": "Can poramboke land be bought or sold?",
             "a": ("Mostly no. Poramboke is government land for public use; most of it cannot be assigned or "
                   "sold, and a patta granted over public-purpose poramboke has been held invalid. Some "
                   "categories may be regularised under specific rules.")},
            {"q": "What is Section 22-A of the Registration Act?",
             "a": ("A provision under which Tamil Nadu maintains a prohibited-properties list — government, "
                   "poramboke, and certain religious, wakf and Bhoodan land — that Sub-Registrars must "
                   "refuse to register without competent sanction.")},
            {"q": "Does registering a document make poramboke land mine?",
             "a": ("No. Registration does not change the land's government classification. Even a registered "
                   "document over poramboke does not create valid title.")},
        ],
        "related": ["land classification", "chitta", "A-register", "patta"],
        "sources": [
            {"t": "The Registration (Tamil Nadu Amendment) Act — Section 22-A (PRS India)",
             "h": "https://prsindia.org/files/bills_acts/acts_states/tamil-nadu/2009/2009TN2.pdf"},
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
        ],
    },
    {
        "slug": "legal-heir-and-succession-certificate-tamil-nadu",
        "title": "Legal heir certificate vs succession certificate in Tamil Nadu",
        "h1": "Legal heir and succession certificates",
        "description": ("For inherited property, which document do you need — a Varisu (legal heir) "
                        "certificate or a court succession certificate? The difference, and the patta step "
                        "that still remains."),
        "updated": "2026-07-21",
        "answer": ("A legal heir (Varisu) certificate, issued by the Tahsildar, identifies a deceased "
                   "person's heirs and is used to transfer property, pensions and utilities. A succession "
                   "certificate comes from a civil court and is needed for debts, securities and disputed "
                   "cases. Neither, on its own, transfers land — the patta still has to be changed."),
        "body": """
<h2>The two certificates</h2>
<table class="kv">
<tr><th></th><th>Legal heir (Varisu) certificate</th><th>Succession certificate</th></tr>
<tr><td>Issued by</td><td>Tahsildar / revenue authorities (via e-Sevai)</td><td>Civil court, under the
Indian Succession Act, 1925</td></tr>
<tr><td>Establishes</td><td>Who the legal heirs are</td><td>The heirs' authority over the deceased's
debts and securities</td></tr>
<tr><td>Typical use</td><td>Transfer of property, pension, PF, insurance, utilities</td><td>Bank
securities, shares, contested or complex estates</td></tr>
</table>
<p>A legal heir certificate does not decide each heir's share or settle a dispute between heirs. Where a
will is contested, an heir won't cooperate, or securities are involved, a succession certificate (or
probate) is the route.</p>

<h2>The step people miss for immovable property</h2>
<p>Tamil Nadu keeps the heir certificate and the land record separate. Obtaining the Varisu certificate
is only half the job — the <strong>patta still has to be transferred</strong> under the Tamil Nadu Patta
Pass Book Act, 1983, and for a flat the undivided share and the apartment-association records must line
up. So the certificate alone does not make an heir the owner of record.</p>

<h2>Why a buyer cares — the auction angle</h2>
<p>When property has passed by inheritance, <em>all</em> the legal heirs generally need to be party to a
sale — a missing or disputing heir is a title risk. At a bank auction the borrower's ownership should be
established, but if inheritance sits anywhere in a property's chain of title, it is worth checking the
heirs are accounted for. Spotting that kind of gap is part of the pre-bid review AuctionScope is built to
help with. This is general information, not legal advice.</p>
""",
        "faqs": [
            {"q": "What is the difference between a legal heir certificate and a succession certificate?",
             "a": ("A legal heir (Varisu) certificate, from the Tahsildar, identifies the heirs and is used "
                   "for property, pension and utility transfers. A succession certificate, from a civil "
                   "court, covers debts and securities and disputed or complex estates.")},
            {"q": "Does a legal heir certificate make me the owner of the property?",
             "a": ("No. In Tamil Nadu the heir certificate and the land record are separate — the patta "
                   "still has to be transferred under the Patta Pass Book Act, 1983, and for a flat the "
                   "UDS and association records must align.")},
            {"q": "Why do all heirs matter when buying inherited property?",
             "a": ("Because a sale of inherited property generally needs every legal heir as a party. A "
                   "missing or disputing heir is a title risk, so confirm the heirs are accounted for "
                   "before you buy.")},
        ],
        "related": ["chain of title", "patta transfer", "sale deed", "UDS"],
        "sources": [
            {"t": "Legal heir certificate in Tamil Nadu — process and eligibility (Nobroker, 2026)",
             "h": "https://www.nobroker.in/blog/legal-heir-certificate-tamilnadu/"},
            {"t": "Tamil Nadu e-Sevai — Government of Tamil Nadu citizen services (official portal)",
             "h": "https://tnesevai.tn.gov.in/"},
        ],
    },
    {
        "slug": "agricultural-land-purchase-rules-tamil-nadu",
        "title": "Agricultural land purchase rules in Tamil Nadu",
        "h1": "Buying agricultural land in Tamil Nadu",
        "description": ("Who can buy farmland in Tamil Nadu, the land-ceiling limits, and what it takes to "
                        "use agricultural land for building — with the auction angle."),
        "updated": "2026-07-21",
        "answer": ("In Tamil Nadu any resident Indian, including a non-agriculturist, can buy agricultural "
                   "land, subject to land-ceiling limits under the 1961 Land Reforms Act. NRIs and OCIs "
                   "cannot buy farmland (they may inherit it). Using agricultural land for building needs a "
                   "change-of-land-use conversion first."),
        "body": """
<h2>Who can buy</h2>
<p>Unlike some states, Tamil Nadu does not bar non-agriculturists from buying farmland — any resident
Indian can purchase agricultural land. NRIs and OCIs, however, cannot buy agricultural land under FEMA /
RBI rules, though they may inherit it.</p>

<h2>The ceiling limit</h2>
<p>The Tamil Nadu Land Reforms (Fixation of Ceiling on Land) Act, 1961 caps how much agricultural land a
person or family can hold — an individual limit in the region of 59.95 acres, and for a family of five
around 15 standard acres. The "standard acre" is an equalised measure, so the raw acreage a ceiling
allows varies with land quality.</p>

<h2>Using farmland for building</h2>
<p>Agricultural land cannot simply be built on. Converting it to non-agricultural use goes through the
Tamil Nadu Change of Land Use Rules, 2017 — with a scrutiny fee and a conversion charge (reported around
3% of the market value) — after which the authority issues a conversion order. Skipping this leaves any
construction unauthorised.</p>

<h2>The bank-auction angle</h2>
<p>If an auction lot is agricultural land, two things matter: confirm the classification from the chitta
and adangal, and factor in that turning it into a building plot needs conversion — a cost and a
timeline, not a formality. A cheap agricultural parcel is not the same buy as a ready residential plot.
Checking the classification before you bid is part of what AuctionScope helps you do; confirm current
fees and rules on the official portal, and treat this as general information, not legal advice.</p>
""",
        "faqs": [
            {"q": "Can a non-agriculturist buy agricultural land in Tamil Nadu?",
             "a": ("Yes. Any resident Indian, including a non-agriculturist, can buy agricultural land in "
                   "Tamil Nadu, subject to the land-ceiling limits. NRIs and OCIs cannot buy farmland, "
                   "though they may inherit it.")},
            {"q": "Is there a limit on how much farmland I can own?",
             "a": ("Yes. Under the 1961 Land Reforms Act there is a ceiling — around 59.95 acres for an "
                   "individual and about 15 standard acres for a family of five, measured in equalised "
                   "'standard acres'.")},
            {"q": "Can I build on agricultural land I buy at auction?",
             "a": ("Not without converting it to non-agricultural use first, under the Change of Land Use "
                   "Rules, 2017, which carries fees and a conversion charge. Factor that cost and time into "
                   "the price of an agricultural lot.")},
        ],
        "related": ["land classification", "chitta", "adangal", "guideline value"],
        "sources": [
            {"t": "Land Reforms — Government of Tamil Nadu (official portal)",
             "h": "https://landreforms.tn.gov.in/LandReforms.html"},
            {"t": "Agriculture land purchase rules in Tamil Nadu (2026) — Nobroker",
             "h": "https://www.nobroker.in/blog/agriculture-land-purchase-rules-in-tamil-nadu/"},
        ],
    },
    {
        "slug": "transfer-electricity-and-water-connection-tamil-nadu",
        "title": "Transferring electricity and water connections after buying (Tamil Nadu)",
        "h1": "Transferring utilities after you buy",
        "description": ("After buying, move the electricity and water connections into your name. Why it "
                        "matters, what you need, and the auction-arrears trap to check first."),
        "updated": "2026-07-21",
        "answer": ("Once you own a property in Tamil Nadu, transfer the electricity connection (TANGEDCO) "
                   "and the water/sewer connection (the local body, or Chennai Metrowater in Chennai) into "
                   "your name, using your sale deed or sale certificate and the patta/tax records. Clear "
                   "any arrears first — at auction, unpaid utility dues can land on the buyer."),
        "body": """
<h2>Why transfer the connections</h2>
<p>Utilities stay in the previous owner's name until you apply to change them. Transferring the
electricity and water connections puts the billing — and the responsibility — in your name, and is
usually needed alongside the property-tax and patta transfer for a clean record.</p>

<h2>What you need</h2>
<ul>
<li><strong>Electricity (TANGEDCO / TNEB).</strong> Apply for a name transfer with your sale deed or sale
certificate, proof of ownership (patta / tax receipt) and the latest paid bill.</li>
<li><strong>Water and sewerage.</strong> In Chennai this is the Chennai Metropolitan Water Supply and
Sewerage Board (Metrowater); elsewhere it is the local municipality or panchayat. The documents are
similar.</li>
</ul>

<h2>The arrears trap — check before you buy</h2>
<p>Unpaid electricity or water dues can attach to the service and become the new occupier's problem, and
an <em>as-is-where-is</em> auction notice may place them on the buyer. So the time to check utility
arrears is <strong>before</strong> you bid — an unexpectedly large backlog eats into a low reserve. After
you buy, clear any dues and complete the transfers.</p>

<h2>The bank-auction angle</h2>
<p>Winning the auction is not the finish line: patta transfer, property-tax mutation and utility
transfers are the steps that actually put the property fully in your name. Building them into your
post-purchase plan — and checking arrears up front — avoids surprises. AuctionScope helps you weigh a
listing's price and dues before you commit; confirm current procedures with the boards' official
portals.</p>
""",
        "faqs": [
            {"q": "How do I transfer the electricity connection after buying a property?",
             "a": ("Apply to TANGEDCO (TNEB) for a name transfer with your sale deed or sale certificate, "
                   "proof of ownership such as the patta or tax receipt, and the latest paid bill. Confirm "
                   "the current process on the board's portal.")},
            {"q": "Do unpaid utility bills pass to the buyer at auction?",
             "a": ("They can. Utility arrears attach to the service, and an as-is-where-is auction notice "
                   "may place them on the buyer — so check electricity and water dues before bidding and "
                   "clear them after buying.")},
            {"q": "Who handles water connection transfer in Chennai?",
             "a": ("The Chennai Metropolitan Water Supply and Sewerage Board (Metrowater). Outside Chennai "
                   "it is the local municipality or panchayat. The documents needed are similar to the "
                   "electricity transfer.")},
        ],
        "related": ["property tax", "patta transfer", "encumbrances", "as-is-where-is"],
        "sources": [
            {"t": "TANGEDCO — Tamil Nadu Generation and Distribution Corporation (official portal)",
             "h": "https://www.tangedco.gov.in/"},
            {"t": "Chennai Metro Water Supply and Sewerage Board (official portal)",
             "h": "https://chennaimetrowater.tn.gov.in/"},
        ],
    },
    {
        "slug": "carpet-area-built-up-and-super-built-up",
        "title": "Carpet area vs built-up vs super built-up — what you're actually buying",
        "h1": "Carpet, built-up and super built-up area",
        "description": ("For a flat, the area quoted can mean very different things. What carpet, built-up "
                        "and super built-up area are, and what RERA changed."),
        "updated": "2026-07-21",
        "answer": ("Carpet area is the net usable floor space inside a flat. Built-up area adds the walls "
                   "and balcony (roughly 10–20% more), and super built-up area adds a share of common "
                   "spaces (another 25–30%). Since RERA, new flats must be priced on carpet area — but an "
                   "auction notice may still state built-up area and undivided share."),
        "body": """
<h2>The three areas</h2>
<table class="kv">
<tr><th>Term</th><th>What it covers</th></tr>
<tr><td>Carpet area (RERA)</td><td>The net usable floor space you can actually use — inside the flat,
excluding balcony and common areas. The most honest measure.</td></tr>
<tr><td>Built-up area</td><td>Carpet area plus the walls and balcony — typically 10–20% more than carpet
area.</td></tr>
<tr><td>Super built-up area</td><td>Built-up area plus a share of common spaces (lift, lobby, stairs,
amenities) — often 25–30% above built-up. The "saleable" area builders once quoted.</td></tr>
</table>

<h2>What RERA changed</h2>
<p>Under the Real Estate (Regulation and Development) Act, 2016, builders must price and sell new flats on
<strong>carpet area</strong>, not the inflated super built-up figure, and there are limits on how much
the delivered carpet area may vary from what was promised. That transparency applies to new,
RERA-registered projects.</p>

<h2>Why it matters at auction</h2>
<p>A bank-auction flat is usually a resale, and the sale notice often states the <strong>built-up
area</strong> and the <strong>undivided share (UDS)</strong> of land rather than a RERA carpet area. If
you compare that figure to a carpet-area price elsewhere, you are not comparing like with like — built-up
overstates usable space. Knowing which area a notice quotes is essential to value a flat correctly, and
it feeds directly into the flat-valuation method (built-up × construction rate + UDS × land rate).
AuctionScope helps you read what a listing actually states so the comparison is honest.</p>
""",
        "faqs": [
            {"q": "What is the difference between carpet area and built-up area?",
             "a": ("Carpet area is the net usable floor space inside a flat, excluding walls, balcony and "
                   "common areas. Built-up area adds the walls and balcony, so it is typically 10–20% "
                   "larger than the carpet area.")},
            {"q": "Does RERA require carpet-area pricing?",
             "a": ("Yes. Under RERA, builders must price and sell new flats based on carpet area rather than "
                   "super built-up area, with limits on how much the delivered carpet area may vary. This "
                   "applies to RERA-registered projects.")},
            {"q": "Which area does a bank auction notice use?",
             "a": ("Often the built-up area and the undivided share of land, not a RERA carpet area. Check "
                   "which one is stated before comparing the price to carpet-area figures elsewhere, or the "
                   "comparison will be misleading.")},
        ],
        "related": ["UDS", "built-up area", "TNRERA", "reserve price"],
        "sources": [
            {"t": "RERA carpet area vs carpet area — Godrej Properties",
             "h": "https://www.godrejproperties.com/blog/difference-between-rera-carpet-area-and-carpet-area"},
            {"t": "TNRERA — Tamil Nadu Real Estate Regulatory Authority (official portal)",
             "h": "https://rera.tn.gov.in/"},
        ],
    },
    {
        "slug": "gift-and-settlement-deeds-tamil-nadu",
        "title": "Gift and settlement deeds in Tamil Nadu — the family-transfer route",
        "h1": "Gift and settlement deeds",
        "description": ("Family property transfers use gift or settlement deeds at concessional stamp duty. "
                        "How they differ, the 1%+1% family rate, and what a buyer should watch for."),
        "updated": "2026-07-21",
        "answer": ("A gift deed transfers property without payment; a settlement deed does the same but can "
                   "carry conditions, such as the giver keeping a life interest. Among family members in "
                   "Tamil Nadu, both attract concessional stamp duty and registration of about 1% + 1% "
                   "(with caps), versus the usual 7% + 4%."),
        "body": """
<h2>Gift vs settlement</h2>
<ul>
<li><strong>Gift deed</strong> — transfers ownership out of love and affection, with no money changing
hands. Once registered and accepted, it is generally unconditional.</li>
<li><strong>Settlement deed</strong> — also a no-consideration transfer, but it can attach conditions:
for example, a parent settling a house on a child while retaining the right to live in it for life. It is
often used to divide ancestral property or settle claims among heirs during the owner's lifetime.</li>
</ul>

<h2>The family concession</h2>
<p>Gift, settlement, release and partition deeds executed <strong>among family members</strong> in Tamil
Nadu are charged at a concessional rate — around 1% stamp duty and 1% registration fee (subject to caps)
— against the standard 7% and 4% for a normal sale. The concession applies to close blood relatives
(parents, children, siblings, spouse, grandparents); transfers outside that circle are taxed at the full
rate.</p>

<h2>What a buyer should watch for</h2>
<p>When a property's chain of title includes a gift or settlement, two things are worth checking. First,
that it was properly executed and registered — a valid deed is a clean link in the chain. Second, and
especially for <strong>settlement deeds</strong>, whether any <em>condition or retained right</em> (like
a life interest) survives, because that can limit what a later seller could actually pass on. At a bank
auction the sale certificate transfers the property, but a conditional settlement in its history is the
kind of flag worth reading carefully — which is what AuctionScope helps you do. This is general
information, not legal advice.</p>
""",
        "faqs": [
            {"q": "What is the difference between a gift deed and a settlement deed?",
             "a": ("Both transfer property without payment. A gift deed is generally unconditional once "
                   "registered; a settlement deed can carry conditions, such as the giver retaining a life "
                   "interest, and is often used within families to divide property.")},
            {"q": "What is the stamp duty on a family gift or settlement in Tamil Nadu?",
             "a": ("For transfers among close family members, gift, settlement, release and partition deeds "
                   "attract a concessional rate of about 1% stamp duty and 1% registration (with caps), "
                   "versus 7% and 4% for a normal sale. Confirm current rates on TNREGINET.")},
            {"q": "Should I worry about a settlement deed in a property's history?",
             "a": ("Check whether it carried a condition or retained right, such as a life interest, that "
                   "could limit what a later seller can pass on. A properly registered, unconditional "
                   "transfer is a clean link in the chain of title.")},
        ],
        "related": ["sale deed", "chain of title", "stamp duty", "encumbrance certificate"],
        "sources": [
            {"t": "Gift deed vs settlement deed — key differences and stamp duty charges (VNCT Global)",
             "h": "https://vnctglobal.com/gift-deed-vs-settlement-deed-key-differences/"},
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
        ],
    },
    {
        "slug": "selling-a-minors-property-court-permission",
        "title": "Selling a minor's property — why court permission matters",
        "h1": "A minor's property and court permission",
        "description": ("A guardian cannot sell a minor's immovable property without the court's permission "
                        "— and a sale that skips it is voidable. Why this is a title risk to check."),
        "updated": "2026-07-21",
        "answer": ("Under the Hindu Minority and Guardianship Act, 1956, a natural guardian cannot sell, "
                   "gift or transfer a minor's immovable property without the prior permission of the "
                   "court. A sale done without it is voidable at the minor's instance — they can challenge "
                   "it even years later, after reaching majority."),
        "body": """
<h2>The rule</h2>
<p>Section 8 of the Hindu Minority and Guardianship Act, 1956 lets a natural guardian manage a minor's
estate for the minor's benefit — but bars the guardian from transferring the minor's immovable property
by sale, gift or exchange <strong>without the previous permission of the court</strong>.</p>

<h2>What happens if permission is skipped</h2>
<p>A transfer made in breach of Section 8 is <strong>voidable at the instance of the minor</strong> or
anyone claiming under them. The Supreme Court has been clear that a minor's property cannot be sold
without prior court permission. Crucially, the minor can challenge such a sale within the limitation
period that starts running only <em>after</em> they attain majority — so the risk can sit dormant in a
property's history for years.</p>

<h2>Why a buyer must care</h2>
<p>If anywhere in a property's chain of title a minor's share was sold without court permission, that link
is shaky — the now-adult former minor could seek to undo it. It is one of the latent title defects that a
clean-looking recent deed does not reveal on its own; it surfaces only by tracing the parent documents
and the ages of past owners.</p>

<h2>The auction angle</h2>
<p>A bank auction transfers the property by sale certificate, but it does not cleanse a defect buried in
the earlier chain of title. Where a property's history hints at a minor's interest, it is worth
scrutinising — the sort of flag AuctionScope helps you surface in the documents before you bid. For a
high-value purchase, have the title examined by a professional; this is general information, not legal
advice.</p>
""",
        "faqs": [
            {"q": "Can a guardian sell a minor's property?",
             "a": ("Not freely. Under the Hindu Minority and Guardianship Act, 1956, a natural guardian "
                   "cannot sell, gift or transfer a minor's immovable property without the prior permission "
                   "of the court.")},
            {"q": "Is a sale of a minor's property without court permission valid?",
             "a": ("It is voidable at the minor's instance — they can challenge it, within a limitation "
                   "period that begins after they attain majority. So such a sale is not safely final.")},
            {"q": "Why is a minor's interest a title risk when buying?",
             "a": ("Because a past sale of a minor's share without court permission can be undone years "
                   "later. It is a latent defect that a recent deed won't show — you find it only by "
                   "tracing the chain of title.")},
        ],
        "related": ["chain of title", "sale deed", "parent documents", "legal heirs"],
        "sources": [
            {"t": "Section 8, The Hindu Minority and Guardianship Act, 1956 (Indian Kanoon)",
             "h": "https://indiankanoon.org/doc/137252432/"},
            {"t": "Permission for sale of a minor's property — LegalKart",
             "h": "https://www.legalkart.com/legal-blog/permission-for-sale-of-minor-property"},
        ],
    },
    {
        "slug": "nri-buying-property-in-india",
        "title": "NRIs and OCIs buying property in India — the FEMA rules",
        "h1": "NRIs and OCIs buying property in India",
        "description": ("NRIs and OCIs can buy residential and commercial property freely, but not "
                        "agricultural land. The FEMA rules on what you can buy, funding and repatriation."),
        "updated": "2026-07-21",
        "answer": ("Under FEMA, an NRI or OCI can buy residential and commercial property in India without "
                   "RBI approval, with no limit on how many. They cannot buy agricultural land, farmhouses "
                   "or plantation property (they may inherit these). Funding must be through Indian banking "
                   "channels, and repatriation on sale follows specific rules."),
        "body": """
<h2>What NRIs and OCIs can and can't buy</h2>
<table class="kv">
<tr><th>Allowed (general permission)</th><th>Not allowed (without RBI approval)</th></tr>
<tr><td>Residential property; commercial property; any number of them</td><td>Agricultural land;
farmhouses; plantation property</td></tr>
</table>
<p>Residential and commercial purchases need no prior RBI approval — they fall under the general-permission
route of the Foreign Exchange Management Act. Agricultural land, farmhouses and plantations are off
limits for purchase, though they can be inherited.</p>

<h2>Funding and repatriation</h2>
<p>Payment must come through normal Indian banking channels — an NRE, NRO or FCNR(B) account, not foreign
cash. When you later sell, how much you can take back out of India depends on the source of the original
funds: broadly, money brought in through NRE / FCNR(B) can be repatriated up to the amount originally
invested, within limits on the number of residential properties. Compliance matters — FEMA penalties can
run to several times the transaction value.</p>

<h2>The bank-auction angle</h2>
<p>An NRI or OCI can bid at a bank auction for a residential or commercial property on the same
general-permission basis — but the agricultural-land bar still applies, so the lot's classification
matters (see the agricultural-land guide). Fund the EMD and balance through the right account, and factor
repatriation rules into the plan if you may sell later. AuctionScope helps you check a listing's type and
numbers; the FEMA compliance is the layer to add. This is general information, not legal or tax advice.</p>
""",
        "faqs": [
            {"q": "Can an NRI buy property in India?",
             "a": ("Yes — an NRI or OCI can buy residential and commercial property under FEMA's "
                   "general-permission route, with no RBI approval needed and no cap on the number. "
                   "Agricultural land, farmhouses and plantations cannot be bought, only inherited.")},
            {"q": "Can an NRI buy a bank auction property?",
             "a": ("Yes, for residential or commercial lots, on the same general-permission basis — but not "
                   "agricultural land. Fund the EMD and balance through an NRE/NRO/FCNR(B) account and keep "
                   "to FEMA rules.")},
            {"q": "Can an NRI take the sale proceeds out of India?",
             "a": ("Within limits. Repatriation depends on the original source of funds — money brought in "
                   "via NRE or FCNR(B) can broadly be repatriated up to the amount invested, subject to "
                   "FEMA conditions. Take professional advice for your case.")},
        ],
        "related": ["agricultural land", "EMD", "reserve price", "sale certificate"],
        "sources": [
            {"t": "Reserve Bank of India — acquisition and transfer of immovable property (FEMA) (official)",
             "h": "https://www.rbi.org.in/"},
            {"t": "NRI guide to buying property in India — rules, taxes and FEMA (2026) — SBNRI",
             "h": "https://sbnri.com/blog/real_estate/nri-guide-buying-property-india-rules-taxes-fema-2"},
        ],
    },
    {
        "slug": "co-ownership-and-joint-property",
        "title": "Co-ownership and joint property — a co-owner can sell only their share",
        "h1": "Co-ownership and joint property",
        "description": ("A co-owner can sell only their own undivided share, not the whole property, without "
                        "the others' consent. Why joint ownership is a title risk when you buy."),
        "updated": "2026-07-21",
        "answer": ("Under Section 44 of the Transfer of Property Act, 1882, a co-owner may sell only their "
                   "own undivided share in a jointly owned property — not the whole property, and not a "
                   "specific portion, without the other co-owners' consent or a formal partition. Buying "
                   "from one co-owner alone gets you only their share."),
        "body": """
<h2>How co-ownership works</h2>
<p>When several people jointly own a property, each holds an <strong>undivided share</strong> in the whole
— no single co-owner owns a specific, marked-off portion until a lawful partition divides it. That is the
key to what any one of them can sell.</p>

<h2>What one co-owner can and can't do</h2>
<ul>
<li><strong>Can</strong> sell their own undivided share to someone else.</li>
<li><strong>Cannot</strong> sell the entire property, or a specific demarcated part, without the consent
of all the other co-owners or a completed partition.</li>
</ul>
<p>The Supreme Court has confirmed this: a co-owner selling the whole undivided property without the
others' consent cannot bind them, and the non-consenting owners can seek cancellation of the sale deed,
an injunction, or a partition suit.</p>

<h2>Why this is a buyer's risk</h2>
<p>If you buy a jointly owned property but only one co-owner signs, you may end up owning just that
person's fractional share — not the whole property — and inherit a dispute with the rest. Confirming that
<em>every</em> co-owner is a party to the sale, or that the seller's share was cleanly partitioned, is a
core title check.</p>

<h2>The auction angle</h2>
<p>A bank auction sells the security the bank holds, and the sale certificate should reflect that — but
if joint ownership sits in a property's chain of title, it is worth checking that past transfers had all
co-owners on board. A half-signed sale upstream is exactly the kind of latent defect worth surfacing
before you bid, which is what AuctionScope helps you do. This is general information, not legal advice.</p>
""",
        "faqs": [
            {"q": "Can one co-owner sell a jointly owned property?",
             "a": ("A co-owner can sell only their own undivided share, not the whole property, without the "
                   "consent of all the other co-owners or a formal partition. Section 44 of the Transfer of "
                   "Property Act governs this.")},
            {"q": "What do I get if I buy from just one co-owner?",
             "a": ("Only that co-owner's undivided share — not the whole property — and potentially a "
                   "dispute with the remaining owners. Confirm every co-owner is a party to the sale before "
                   "buying.")},
            {"q": "What can other co-owners do if their share is sold without consent?",
             "a": ("They can approach a civil court to cancel the sale deed, seek an injunction against "
                   "further transfer, or file a partition suit to divide the property formally.")},
        ],
        "related": ["chain of title", "sale deed", "legal heirs", "partition"],
        "sources": [
            {"t": "Laws related to co-ownership of a joint property — LawRato",
             "h": "https://lawrato.com/indian-kanoon/property-law/laws-related-to-co-ownership-of-a-joint-property-552"},
            {"t": "Sale of undivided share in property under Indian law — Patra's Law Chambers",
             "h": "https://patraslawchambers.com/sale-of-undivided-share-in-property-under-indian-law/"},
        ],
    },
    {
        "slug": "town-survey-ts-number-urban-land-records",
        "title": "Town survey (TS) number and urban land records in Tamil Nadu",
        "h1": "Town survey (TS) numbers",
        "description": ("In towns and cities, land is identified by a TS number, not a rural survey number. "
                        "What it is and why you need it to pull the right records."),
        "updated": "2026-07-21",
        "answer": ("In urban Tamil Nadu, land is identified by a Town Survey (TS) number under the Town "
                   "Survey Land Register, the urban equivalent of the rural survey and sub-division number. "
                   "You need the correct TS or survey number to pull the encumbrance certificate, patta and "
                   "FMB for the right parcel."),
        "body": """
<h2>Rural vs urban identifiers</h2>
<p>Rural land is identified by a <strong>survey number</strong> and sub-division. In surveyed towns and
cities, the same role is played by the <strong>Town Survey (TS) number</strong>, recorded in the Town
Survey Land Register — the urban land-record system. A single property may also carry an <em>old</em>
survey number from before the town survey, which is why records sometimes ask for both.</p>

<h2>Why the right number matters</h2>
<p>Every record you pull is keyed to the survey identifier: the encumbrance certificate, the patta and the
FMB sketch all need the correct TS or survey number to return the right parcel. Search with the wrong or
outdated number and you either get nothing or, worse, records for the wrong plot. Sale notices and old
deeds may quote an old survey number, so matching it to the current TS number is part of getting clean
records.</p>

<h2>How to find and use it</h2>
<p>Urban land records, including those keyed to the TS number, are available through the Revenue
Department's e-Services portal, using the district, taluk/town, village or ward and the survey details.
Confirm the current identifier before you rely on any record — see the source below.</p>

<h2>The auction angle</h2>
<p>For an urban flat or plot at auction, the sale notice's survey details are your key to pulling the EC,
patta and FMB independently. Getting the TS / survey number right is a small step that makes every other
check reliable — the kind of groundwork AuctionScope helps you do quickly on a specific listing.</p>
""",
        "faqs": [
            {"q": "What is a TS number in Tamil Nadu?",
             "a": ("A Town Survey number — the identifier for a parcel of land in a surveyed town or city, "
                   "recorded in the Town Survey Land Register. It is the urban equivalent of the rural "
                   "survey and sub-division number.")},
            {"q": "Why do I need the correct survey or TS number?",
             "a": ("Because the encumbrance certificate, patta and FMB are all keyed to it. The wrong or "
                   "outdated number returns nothing, or records for the wrong plot, so matching old and "
                   "current numbers matters.")},
            {"q": "Where can I look up urban land records?",
             "a": ("Through the Revenue Department's e-Services portal, using the district, taluk/town, "
                   "ward or village and the survey details. Confirm the current identifier before relying "
                   "on a record.")},
        ],
        "related": ["survey number", "encumbrance certificate", "patta", "FMB"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },

    # ---- Ring 2 close-out: money (TDS) + land literacy (units, UDS, FSI, setbacks) ----
    {
        "slug": "tds-on-property-purchase",
        "title": "TDS on property purchase — the 1% the buyer deducts (Sec 194-IA)",
        "h1": "TDS on property purchase",
        "description": ("Buy property over ₹50 lakh and you, the buyer, must deduct 1% TDS and file Form "
                        "26QB. How Section 194-IA works, and how it applies to an auction win."),
        "updated": "2026-07-21",
        "answer": ("Under Section 194-IA, when you buy immovable property (other than agricultural land) for "
                   "₹50 lakh or more, you — the buyer — must deduct 1% TDS from the payment, deposit it, and "
                   "file Form 26QB. You need the seller's PAN and your own; no TAN is required. It applies "
                   "to an auction purchase too."),
        "body": """
<h2>The rule in short</h2>
<ul>
<li><strong>Rate:</strong> 1% of the sale consideration or the stamp-duty (guideline) value, whichever is
higher.</li>
<li><strong>Threshold:</strong> the property value must be ₹50 lakh or more. Below that, no TDS.</li>
<li><strong>Who deducts:</strong> the buyer, from the amount paid to the seller.</li>
<li><strong>Excluded:</strong> agricultural land.</li>
</ul>

<h2>How you pay and file it</h2>
<p>You deposit the deducted amount and file <strong>Form 26QB</strong> — an online challan-cum-statement —
within seven days of the end of the month in which the deduction was made. You need the buyer's and the
seller's PAN (no TAN is required). If the seller does not provide a PAN, TDS is deducted at a much higher
rate. After depositing, you download <strong>Form 16B</strong> and give it to the seller as proof.</p>

<h2>The bank-auction angle</h2>
<p>Where a SARFAESI purchase crosses the ₹50 lakh threshold, the same obligation applies — the "seller"
for TDS purposes is worked out per the transaction, so confirm how the bank handles it and who the
payment is made to before you complete. It is a compliance step, not an extra tax on you (it counts
towards the seller's tax), but missing it carries interest and penalties. Factor it into your post-win
checklist alongside stamp duty and registration. This is general information, not tax advice — confirm
your case with a professional and the Income Tax Department's portal.</p>
""",
        "faqs": [
            {"q": "When is the TDS due after buying a property?",
             "a": ("You must deposit the deducted 1% and file Form 26QB within seven days from the end of "
                   "the month in which the deduction (payment to the seller) was made. Late filing attracts "
                   "interest and a penalty, so don't leave it.")},
            {"q": "Who deducts TDS on a property purchase?",
             "a": ("The buyer. Under Section 194-IA you deduct 1% from the payment to the seller when the "
                   "property value is ₹50 lakh or more, deposit it, and file Form 26QB. You need PAN but "
                   "not a TAN.")},
            {"q": "Does TDS apply to a bank auction property?",
             "a": ("Yes, where the purchase is ₹50 lakh or more and the property is not agricultural land. "
                   "Confirm with the bank how it is handled and to whom payment is made before completing.")},
            {"q": "What if the seller has no PAN?",
             "a": ("Then TDS must be deducted at a much higher rate (20%), and the seller cannot claim "
                   "credit for it. Always obtain the seller's PAN.")},
        ],
        "related": ["stamp duty", "guideline value", "sale certificate", "agricultural land"],
        "sources": [
            {"t": "TDS on purchase of immovable property (Section 194-IA) — Income Tax Department (official)",
             "h": "https://www.incometaxindia.gov.in/w/tds-purchase-of-immovable-property"},
            {"t": "Section 194-IA: how to file TDS on sale of property — ClearTax",
             "h": "https://cleartax.in/s/how-to-file-tds-on-sale-property"},
        ],
    },
    {
        "slug": "land-area-units-and-conversions-tamil-nadu",
        "title": "Land-area units in Tamil Nadu — cent, ground, are and how to convert",
        "h1": "Land-area units and conversions",
        "description": ("Sale notices mix cent, ground, are, acre and square feet. A conversion table and why "
                        "getting the unit right is essential before comparing prices."),
        "updated": "2026-07-21",
        "answer": ("Tamil Nadu land is measured in a mix of units — cent, ground, are, acre and square feet. "
                   "1 cent is 435.6 sq ft, 1 ground is 2,400 sq ft, 1 are is about 1,076 sq ft, and 1 acre "
                   "is 43,560 sq ft (100 cents). Convert everything to a common unit before comparing a "
                   "reserve to local ₹/sq ft rates."),
        "body": """
<h2>The common units</h2>
<table class="kv">
<tr><th>Unit</th><th>In square feet</th><th>Note</th></tr>
<tr><td>1 square metre</td><td>10.76 sq ft</td><td>Metric; used in notices and FMB</td></tr>
<tr><td>1 square yard (gaj)</td><td>9 sq ft</td><td></td></tr>
<tr><td>1 cent</td><td>435.6 sq ft</td><td>One-hundredth of an acre — common across Tamil Nadu</td></tr>
<tr><td>1 ground</td><td>2,400 sq ft</td><td>Common in Chennai (≈ 5.5 cents)</td></tr>
<tr><td>1 are</td><td>1,076.4 sq ft</td><td>= 100 sq m</td></tr>
<tr><td>1 acre</td><td>43,560 sq ft</td><td>= 100 cents</td></tr>
<tr><td>1 hectare</td><td>1,07,639 sq ft</td><td>= 100 ares = 2.47 acres</td></tr>
</table>

<h2>Why the unit matters</h2>
<p>A reserve price only tells you something once you put it on a per-square-foot basis and compare it to
local rates. If a notice quotes the extent in <em>cents</em> or <em>grounds</em> and you compare it to a
₹/sq ft rate without converting, the comparison is meaningless — and the errors are large (a ground is
2,400 sq ft, a cent is 435.6). Older documents may also use traditional units that vary by locality, so
confirm the exact unit the notice means.</p>

<h2>The bank-auction angle</h2>
<p>Auction notices routinely state extents in cent, ground or are, especially for land. Converting to a
single unit is the first step before you judge whether a reserve is genuinely below the local market —
the reserve-vs-rate check that decides whether a lot is worth bidding on. AuctionScope reads the extent a
notice states so you are comparing like with like; if a unit is ambiguous, confirm it against the record.</p>
""",
        "faqs": [
            {"q": "How many square feet is one cent?",
             "a": ("One cent is 435.6 square feet — one-hundredth of an acre. An acre is 43,560 square feet, "
                   "or 100 cents.")},
            {"q": "How big is a ground in Tamil Nadu?",
             "a": ("A ground is 2,400 square feet, a unit common in Chennai — roughly 5.5 cents. Convert it "
                   "to square feet before comparing a price per square foot.")},
            {"q": "Why do I need to convert units before comparing prices?",
             "a": ("Because a reserve only means something per square foot. Comparing a cent- or "
                   "ground-based extent to a ₹/sq ft rate without converting gives a wildly wrong answer, "
                   "since the units differ by hundreds or thousands of square feet.")},
        ],
        "related": ["extent", "reserve price", "guideline value", "UDS"],
        "sources": [
            {"t": "e-Services of Land Records — Revenue Department, Government of Tamil Nadu (official portal)",
             "h": "https://eservices.tn.gov.in/eservicesnew/index.html"},
        ],
    },
    {
        "slug": "undivided-share-uds-in-a-flat",
        "title": "Undivided share (UDS) in a flat — the land you actually own",
        "h1": "Undivided share (UDS) in a flat",
        "description": ("When you buy a flat, you own the apartment plus an undivided share of the land. What "
                        "UDS is, why a low UDS matters, and how it affects value."),
        "updated": "2026-07-21",
        "answer": ("The undivided share (UDS) is the portion of the land under a building that a flat owner "
                   "holds — undivided, in common with the other owners. It is registered as part of the "
                   "sale, sized roughly in proportion to the flat, and it is where a flat's land value and "
                   "redevelopment potential actually sit."),
        "body": """
<h2>What UDS is</h2>
<p>A flat is two things at once: the built apartment, and a share of the land the building stands on. That
land share is the <strong>undivided share</strong> — "undivided" because no owner holds a marked-off
piece of ground; each holds a fraction of the whole plot in common. It is recorded in the sale deed and
is a real, registered land interest.</p>

<h2>How it is sized — and why a low UDS matters</h2>
<p>UDS is normally allocated in proportion to a flat's area against the total built area, so a bigger flat
carries a larger share. Two flats that look identical inside can have different UDS if the builder
allocated it differently. It matters because:</p>
<ul>
<li><strong>Land holds the value.</strong> Buildings depreciate; land appreciates. A higher UDS means you
own more of the appreciating asset.</li>
<li><strong>Redevelopment.</strong> If the building is ever redeveloped, compensation typically follows
the UDS, not the flat's built area.</li>
<li><strong>Valuation.</strong> A flat's fair value combines the built-up area (at a construction rate)
and the UDS (at the land rate) — so UDS is half the sum.</li>
</ul>

<h2>The bank-auction angle</h2>
<p>An auction notice for a flat usually states <em>both</em> the built-up area and the UDS — and confusing
them, or ignoring a thin UDS, distorts any value comparison. Checking the UDS is central to deciding
whether a flat's reserve is genuinely attractive. AuctionScope reads what the notice states so the
built-up and land components are kept separate in the maths.</p>
""",
        "faqs": [
            {"q": "What is undivided share (UDS) in a flat?",
             "a": ("It is the portion of the land under a building that a flat owner holds in common with "
                   "the other owners — undivided, registered as part of the sale, and sized roughly in "
                   "proportion to the flat.")},
            {"q": "Why does a low UDS matter?",
             "a": ("Because land, not the building, holds and grows the value, and redevelopment "
                   "compensation usually follows the UDS. A thin UDS means less of the appreciating asset "
                   "and a weaker position in any redevelopment.")},
            {"q": "How does UDS affect a flat's value?",
             "a": ("A flat's fair value combines its built-up area at a construction rate with its UDS at "
                   "the land rate. The UDS is the land half of that sum, so it is central to valuing the "
                   "flat correctly.")},
        ],
        "related": ["carpet area", "built-up area", "reserve price", "sale deed"],
        "sources": [
            {"t": "TNREGINET — Registration Department, Government of Tamil Nadu (official portal)",
             "h": "https://tnreginet.gov.in/"},
            {"t": "TNRERA — Tamil Nadu Real Estate Regulatory Authority (official portal)",
             "h": "https://rera.tn.gov.in/"},
        ],
    },
    {
        "slug": "fsi-far-in-tamil-nadu",
        "title": "FSI / FAR in Tamil Nadu — how much you can build",
        "h1": "FSI (Floor Space Index)",
        "description": ("FSI decides how much floor area you can build on a plot. What it means, the Tamil "
                        "Nadu limits, and why it affects a plot's value."),
        "updated": "2026-07-21",
        "answer": ("Floor Space Index (FSI), also called Floor Area Ratio (FAR), is the ratio of total "
                   "buildable floor area to plot area. In Tamil Nadu, non-high-rise buildings up to 18.3 m "
                   "are generally allowed an FSI of about 2 — so a 2,400 sq ft plot permits roughly 4,800 "
                   "sq ft of construction, subject to setbacks."),
        "body": """
<h2>What FSI means</h2>
<p>FSI (or FAR) is the total covered floor area you may build across all floors, divided by the plot area.
An FSI of 2 on a 2,400 sq ft plot allows about 4,800 sq ft of floor space in total. It is set by the
development rules and is the single number that caps how much building a plot can carry.</p>

<h2>The Tamil Nadu position</h2>
<p>For ordinary non-high-rise buildings (up to about 18.3 m in height), Tamil Nadu generally permits an
FSI of around 2 under the Tamil Nadu Combined Development and Building Rules, administered by CMDA in the
Chennai metropolitan area and by DTCP elsewhere. The exact permissible FSI varies with the building type,
road width and zone, so treat 2 as a common benchmark, not a universal figure.</p>

<h2>Why it affects value</h2>
<p>Two plots of the same size can be worth different amounts if their permissible FSI differs — more FSI
means more saleable floor area, which is why FSI feeds directly into what a plot is worth to a builder.
And the headline FSI is only achievable after leaving the required setbacks, so the usable build is
always a little less than the raw number suggests.</p>

<h2>The bank-auction angle</h2>
<p>For a plot or a redevelopment-minded purchase at auction, the permissible FSI shapes what you could do
with it — and therefore what it is worth. It is not stated in the sale notice, so it is a check to run
against the local rules for that zone. AuctionScope helps you weigh a listing's price and location;
confirm the buildable potential with the planning authority. This is general information, not
professional advice.</p>
""",
        "faqs": [
            {"q": "What is FSI or FAR?",
             "a": ("Floor Space Index (FSI), also called Floor Area Ratio (FAR), is the total buildable "
                   "floor area divided by the plot area. An FSI of 2 means you can build twice the plot "
                   "area in total floor space.")},
            {"q": "What is the FSI limit in Tamil Nadu?",
             "a": ("For non-high-rise buildings up to about 18.3 m, Tamil Nadu generally allows an FSI of "
                   "around 2, under the Combined Development and Building Rules. The exact figure varies "
                   "with building type, road width and zone.")},
            {"q": "Why does FSI matter when buying a plot?",
             "a": ("Because it caps how much can be built, and therefore what the plot is worth to develop. "
                   "More permissible FSI means more saleable floor area — though the required setbacks mean "
                   "you rarely build the full headline figure.")},
        ],
        "related": ["setbacks", "building approval", "guideline value", "reserve price"],
        "sources": [
            {"t": "Floor Space Index (FSI) in Tamil Nadu (TNCDBR / CMDA) — Chennai Realties",
             "h": "https://www.chennairealties.in/floor-space-index-fsi-tamil-nadu/"},
            {"t": "Tamil Nadu Single Window Portal for Planning Permission (official)",
             "h": "https://onlineppa.tn.gov.in/"},
        ],
    },
    {
        "slug": "building-setbacks-tamil-nadu",
        "title": "Building setbacks in Tamil Nadu — the open space you must leave",
        "h1": "Building setbacks",
        "description": ("Setbacks are the mandatory open space around a building. The Tamil Nadu front, side "
                        "and rear requirements, and why deviations are a risk."),
        "updated": "2026-07-21",
        "answer": ("A setback is the open space you must leave between a building and the plot boundary. In "
                   "Tamil Nadu the front setback runs from about 1.5 to 6 m depending on the road width, "
                   "side setbacks about 1 to 1.5 m, and the rear setback about 1.5 m. They shape how much "
                   "of a plot you can actually build on."),
        "body": """
<h2>What setbacks are for</h2>
<p>Setbacks are the required gaps between a building and its plot boundaries, on the front, sides and
rear. They exist for light, ventilation, privacy, emergency access and the look of the street, and they
are part of what the planning authority checks when approving a building.</p>

<h2>The Tamil Nadu requirements</h2>
<table class="kv">
<tr><th>Setback</th><th>Typical requirement</th></tr>
<tr><td>Front</td><td>About 1.5 m to 6 m, depending on the width of the abutting road</td></tr>
<tr><td>Side</td><td>About 1 m to 1.5 m, depending on plot width</td></tr>
<tr><td>Rear</td><td>About 1.5 m</td></tr>
</table>
<p>These are common figures under the Tamil Nadu Combined Development and Building Rules; the exact
requirement varies with plot size, building height and use, so confirm them for the specific plot.</p>

<h2>Why they matter to a buyer</h2>
<p>Setbacks reduce the footprint you can build on, so the permissible FSI is only achievable <em>after</em>
leaving them — the usable build is always smaller than the plot suggests. They also flag risk: a building
that has been constructed <strong>into</strong> its setbacks is a deviation from the approved plan, which
can mean regularisation costs, penalties, or trouble with approvals and loans.</p>

<h2>The bank-auction angle</h2>
<p>For a built property at auction, setback deviations are one of the things an <em>as-is-where-is</em>
sale will not warn you about — and they tie back to the occupancy-certificate and approval checks. For a
plot, the setbacks decide how much you can actually build. Either way it is a check worth making before
you bid. AuctionScope helps you weigh the listing; confirm the building's compliance with the planning
authority. General information, not professional advice.</p>
""",
        "faqs": [
            {"q": "What is a setback in building rules?",
             "a": ("The mandatory open space you must leave between a building and its plot boundary — front, "
                   "sides and rear — for light, ventilation, access and privacy. Planning authorities check "
                   "setbacks when approving a building.")},
            {"q": "What are the setback requirements in Tamil Nadu?",
             "a": ("Commonly a front setback of about 1.5 to 6 m (by road width), side setbacks of about 1 "
                   "to 1.5 m, and a rear setback of about 1.5 m — varying with plot size, height and use. "
                   "Confirm for the specific plot.")},
            {"q": "Why do setbacks matter when buying?",
             "a": ("They reduce the buildable footprint, so the usable build is smaller than the plot "
                   "suggests. A building constructed into its setbacks is a deviation from the approved "
                   "plan, which can bring regularisation costs, penalties or loan and approval trouble.")},
        ],
        "related": ["FSI", "building approval", "occupancy certificate", "as-is-where-is"],
        "sources": [
            {"t": "Setback spaces for building plan approval (Tamil Nadu) — Chennai Realties",
             "h": "https://www.chennairealties.in/setback-spaces-for-building-plan-approval/"},
            {"t": "Tamil Nadu Single Window Portal for Planning Permission (official)",
             "h": "https://onlineppa.tn.gov.in/"},
        ],
    },
]


def _article_jsonld(g: dict, url: str) -> dict:
    node = {
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
    # Ring 2/3 facts are research-verified — cite them (content-pillars.md). The
    # cited sources also raise AI-search citation odds (the ai-seo skill).
    if g.get("sources"):
        node["citation"] = [
            {"@type": "CreativeWork", "name": s["t"], "url": s["h"]} for s in g["sources"]
        ]
    return node


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
{THEME_INIT}
{ANALYTICS_SNIPPET}
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
</div>""" + CAPTURE_SCRIPT + CONSENT_SCRIPT + "</body></html>")


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

    sources_block = ""
    if g.get("sources"):
        items = "".join(
            f'<li><a href="{html.escape(s["h"])}" rel="nofollow noopener" target="_blank">'
            f'{html.escape(s["t"])}</a></li>'
            for s in g["sources"]
        )
        sources_block = ("<h2>Sources</h2><p class=\"updated\">Rates, fees and procedures change — "
                         "confirm the current position on the official portals below before you act.</p>"
                         f"<ul>{items}</ul>")

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
        sources_block,
        "</article>",
        _cta_and_capture(f"guide-{slug}"),
        FOOT,
    ]
    return "\n".join(parts)


# Hub layout: guides grouped into labelled sections so the /guides page reads as
# a structured library, not a wall of cards. Order here is the display order.
# Every guide slug must appear in exactly one group (main() asserts it), so a new
# guide is never silently dropped from the hub.
GUIDE_GROUPS = [
    {"title": "Bank auction basics",
     "intro": "Start here — how a SARFAESI bank auction works, from the sale notice to possession.",
     "slugs": [
        "what-is-a-sarfaesi-bank-auction", "bank-auction-process-step-by-step",
        "what-is-emd-in-a-bank-auction", "what-is-reserve-price-in-a-bank-auction",
        "symbolic-vs-physical-possession", "what-does-as-is-where-is-mean",
        "how-to-read-a-bank-auction-sale-notice", "bank-auction-re-auctions-and-price-drops",
        "how-to-pay-after-winning-a-bank-auction", "encumbrances-and-dues-in-a-bank-auction",
        "is-buying-a-bank-auction-property-safe", "common-bank-auction-myths", "bank-auction-glossary"]},
    {"title": "Documents & land records",
     "intro": "The records that prove ownership, boundaries and history — and how to pull each in Tamil Nadu.",
     "slugs": [
        "patta-and-chitta-in-tamil-nadu", "adangal-and-a-register-tamil-nadu",
        "encumbrance-certificate-tamil-nadu", "fmb-sketch-tamil-nadu",
        "sale-deed-and-parent-documents-tamil-nadu", "town-survey-ts-number-urban-land-records",
        "verify-property-documents-before-buying-tamil-nadu"]},
    {"title": "Money, tax & financing",
     "intro": "What a purchase really costs — guideline value, stamp duty, TDS and financing an auction win.",
     "slugs": [
        "guideline-value-tamil-nadu", "stamp-duty-and-registration-charges-tamil-nadu",
        "tds-on-property-purchase", "home-loan-for-a-bank-auction-property"]},
    {"title": "Title, ownership & inheritance",
     "intro": "The chain-of-title checks — deeds, powers of attorney, co-owners, heirs and land you can't buy.",
     "slugs": [
        "sale-agreement-vs-sale-deed", "power-of-attorney-property-sales",
        "co-ownership-and-joint-property", "selling-a-minors-property-court-permission",
        "legal-heir-and-succession-certificate-tamil-nadu", "gift-and-settlement-deeds-tamil-nadu",
        "prohibited-property-and-poramboke-land-tamil-nadu"]},
    {"title": "Approvals & land literacy",
     "intro": "What's approved and what can be built — plan approvals, area terms, UDS, FSI and setbacks.",
     "slugs": [
        "building-approval-and-occupancy-certificate-tamil-nadu", "how-to-check-rera-project-tamil-nadu",
        "carpet-area-built-up-and-super-built-up", "undivided-share-uds-in-a-flat",
        "fsi-far-in-tamil-nadu", "building-setbacks-tamil-nadu",
        "land-area-units-and-conversions-tamil-nadu"]},
    {"title": "After you buy & who can buy",
     "intro": "Transfers once you own it, and the rules for NRI and agricultural-land buyers.",
     "slugs": [
        "patta-transfer-and-mutation-tamil-nadu", "property-tax-and-arrears-tamil-nadu",
        "transfer-electricity-and-water-connection-tamil-nadu", "nri-buying-property-in-india",
        "agricultural-land-purchase-rules-tamil-nadu"]},
]


def _guide_card(g: dict) -> str:
    return (f'<a class="cardlink" href="/guides/{g["slug"]}">'
            f'<div class="t">{html.escape(g["h1"])}</div>'
            f'<div class="m">{html.escape(g["description"])}</div></a>')


def render_hub(guides: list[dict]) -> str:
    url = f"{SITE_BASE}/guides"
    trail = [("Home", "/"), ("Guides", "/guides")]
    desc = ("Plain-language guides to Tamil Nadu bank auctions — how SARFAESI e-auctions work, "
            "EMD, reserve price, possession and what to check before you bid.")
    jsonld = [_breadcrumb_jsonld(trail)]
    by_slug = {g["slug"]: g for g in guides}

    sections = []
    for grp in GUIDE_GROUPS:
        cards = "".join(_guide_card(by_slug[s]) for s in grp["slugs"] if s in by_slug)
        sections.append(
            f'<h2 class="grouphead">{html.escape(grp["title"])}</h2>'
            f'<p class="groupintro">{html.escape(grp["intro"])}</p>'
            f'<div class="grid">{cards}</div>'
        )

    parts = [
        _head("Bank auction guides — Tamil Nadu", desc, url, jsonld),
        '<nav class="crumb"><a href="/">home</a> / guides</nav>',
        "<h1>Bank auction guides</h1>",
        '<p class="lede">Plain-language guides to buying property at Tamil Nadu bank auctions — '
        'the process, the jargon, and what to check before you bid, grouped by topic. '
        'Looking for listings? <a href="/bank-auctions">Browse bank auctions by city</a>.</p>',
        *sections,
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

    # Guard: every guide is placed in exactly one hub group (no orphans, no dupes,
    # no group pointing at a slug that doesn't exist) — so the grouped hub always
    # shows the full set. A new guide fails the build until it's grouped.
    grouped = [s for grp in GUIDE_GROUPS for s in grp["slugs"]]
    assert len(grouped) == len(set(grouped)), "a guide is listed in more than one hub group"
    assert set(grouped) == set(slugs), (
        f"hub grouping mismatch — ungrouped: {sorted(set(slugs) - set(grouped))}; "
        f"unknown in groups: {sorted(set(grouped) - set(slugs))}")

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
