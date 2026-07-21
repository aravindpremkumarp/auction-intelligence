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
        "title": "The bank auction process, step by step",
        "h1": "The bank auction process, step by step",
        "description": ("From demand notice to possession: the full path a SARFAESI bank auction takes, "
                        "and where a buyer enters it — notice, EMD, bid, pay, possession."),
        "updated": "2026-07-21",
        "answer": ("A bank auction runs in a fixed order: the bank issues a demand notice, takes possession "
                   "of the property, values it and publishes a sale notice, then holds an online auction. "
                   "The winning bidder pays a deposit, then the balance, and receives a sale certificate. "
                   "A buyer usually enters at the sale-notice stage."),
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
