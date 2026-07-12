# AuctionScope — Copy Playbook

*The single source of exceptional copy. Two audiences read it: **you**, when writing a post by hand, and **the Poster robot**, which has the essentials injected into its prompt (`marketing_agents/poster.py` → `build_prompt`). Both write from the same frameworks so the voice is one voice.*

*Translated from three installed skills — `social` (hook formulas), `copywriting` (headline formulas), `copy-editing` (the quality bar) — into AuctionScope's data (reserve price, EMD, re-auction, ₹ lakhs, real cities/banks) and voice. Generic framework → auction-specific swipe line.*

*This playbook covers **how** to write. **What** to write about comes from the pillar system in `content-pillars.md` — 8 angle machines (deals, education, market data, news, geo, evaluations, Q&A, build-in-public), each a feed that generates posts indefinitely.*

---

## The voice, in one breath
Lowercase, calm, plain-spoken. **The number does the work** — lead with it, don't decorate it. One idea per post. Honest framing always ("reserve price", "bank auction", "ends <date>"). We help people **research and evaluate**; we never claim legal certainty. Full voice: `.agents/product-marketing.md`.

**Never use** (hard rule, enforced in code): *due diligence · advocate · legal opinion · title-clear · guaranteed · institutional · revolutionary*. We help evaluate; we don't do legal diligence.

---

## Part 1 — The Hook Library

The first line decides whether anyone reads the rest. Every AuctionScope hook is built on one real auction fact. Pick the family that matches the auction's angle.

### Price-drop hooks (angle: `price_drop` — a re-auction at a lower reserve)
The strongest content we have. The drop *is* the story.
- `₹{prev}L → ₹{now}L. Same {city} plot, {n}% lower reserve after a failed auction.`
- `This {city} {type} didn't sell at ₹{prev}L. Bank's new reserve: ₹{now}L.`
- `Reserve cut {n}% on a {city} {type}. Re-auctions are where the deals hide.`
- `Second time listed, ₹{drop}L cheaper. Auction ends {date}.`

### Closing-soon hooks (angle: `closing_soon` — deadline is near)
Urgency, but honest urgency — the deadline is a fact, not a manufactured scarcity line.
- `{city} {type}, reserve ₹{now}L. Bids close {date}.`
- `{n} days left on this one: ₹{now}L reserve, {city}.`
- `Closing {date}: a {type} in {city} at ₹{now}L reserve.`

### Cheapest-in-city hooks (angle: `cheapest`)
The number leads, full stop.
- `₹{now}L for a {type} in {city}. Cheapest live bank auction there right now.`
- `Lowest reserve in {city} this week: ₹{now}L. {type}, ends {date}.`
- `{type} in {city}, reserve ₹{now}L. Here's what the sale notice says.`

### Market-gap hooks (angle: `market_gap` — reserve ₹/sqft vs local asking rate)
The single most attractive fact about an auction is that the reserve can sit **below local market rates**. But this is the biggest honesty trap in the whole playbook: it is a **claim only when computed**, never asserted. The reserve alone proves nothing — banks sometimes set reserves *above* what buyers will pay (that's *why* re-auctions exist). So we compute the gap on a per-sqft basis, then hook on the real number.
- `₹{auction_rate}/sqft reserve. {area} {type}s list at ₹{mkt_low}–{mkt_high}/sqft. that's ~{gap}% under — and a gap that size is worth understanding before you bid.`
- `This {area} {type}'s reserve works out to ₹{auction_rate}/sqft against a neighbourhood asking ~₹{mkt_mid}. big gaps are a reason to investigate, not to assume.`
- `{gap}% below {area} asking rates, on paper. we show you the paper — and what the notice doesn't.`

> **How the gap is computed (and when we do NOT claim one).**
>
> **The denominator depends on property type — this is the crux.** A flat has *two* areas and they are not interchangeable:
> - **Flat / apartment:** divide by the **built-up area** (or super-built-up, matching what the market quote uses) — **never the UDS**. A flat notice states *both*, e.g. *"built-up area 671 sq.ft … undivided share of the land admeasuring 393 sq.ft."* Market ₹/sqft for flats is quoted on built-up, so the denominator is **671, not 393**. Using the UDS here inflated one real case (`811123`, reserve ₹25.5L) from a true **₹3,800/sqft** to a false **₹6,489/sqft** — which would have hidden a genuine gap. Capture the UDS separately for context, but never divide by it. If a flat notice gives *only* UDS (no built-up), the built-up rate isn't computable → drop the gap.
> - **Land / plot / land & building:** divide by the **land extent** directly.
>
> Then: `auction ₹/sqft = reserve ÷ (the right extent for the type)`. `market ₹/sqft = current listed asking rate for that area + property type` (web research, cited). `gap% = (market − auction) ÷ market`. Segment correctly: an Ambattur *flat* rate ≠ an Ambattur *land* rate.
>
> **Also drop the gap when:** the unit is ambiguous or unconverted (acre / cent / ground / sq.m must normalise to sqft); the property **type disagrees with the described asset** (a "flat" described as vacant land); or the market rate rests on a single stale listing.
>
> **Basis caveat:** portals often quote **super-built-up**, notices often give **built-up** or **carpet** — these differ ~10–30%, so treat every gap as approximate and never over-precise.
>
> **Frame honestly, always:** say **"listed asking rates"** (portals show asking, not transacted prices), give a **range not a point**, and add **"approximate — verify extent, condition, and possession."** Capture and surface **possession type** (symbolic vs physical) — a symbolic-possession flat with a big gap is the textbook "ask why." A *large* gap (say >40%) is a **reason to investigate** (possession, UDS-only sale, encumbrances, litigation), not a guaranteed bargain — say so. This angle needs the **research-verified tier** (`content-pillars.md`): the market rate is web-sourced and must carry a source.

### Evaluation hooks (the hero feature — use for education/awareness posts)
This is what actually makes AuctionScope different: ask the property anything, answered with web research. Don't sell the auction, sell the *checking*.
- `Before you bid on a {city} plot: is there groundwater? does it flood? any metro coming? we check all three.`
- `A sale notice tells you the price. It won't tell you the travel time to your office. we will.`
- `"is the reserve fair vs market?" — the one question every bidder should ask. ask it here.`
- `{live} live auctions in Tamil Nadu. Ask any of them: flood risk, connectivity, nearby projects, distance from you.`

> **Grounding rule:** every `{placeholder}` must be filled from a real auction's fields. Never invent a number, city, date, or drop %. If the fact isn't in the data, don't write the hook.

---

## Part 2 — Headline Formulas (for the deal-card image, `image_headline` ≤ 8 words)

From the `copywriting` skill's headline families, cut to auction length. These go on the HyperFrames static/carousel card, not the caption.

| Family | Formula | Auction example |
|---|---|---|
| Outcome | *{outcome} without {pain}* | `Bid smarter, not blind` |
| Problem | *{question on the pain}* | `Is this reserve fair?` |
| Proof / number | *{number} {people} {outcome}* | `{live} live TN auctions` |
| Differentiation | *The {category} that {differentiator}* | `Auctions you can actually research` |
| Price-drop | *₹{prev}L → ₹{now}L* | `₹45L → ₹38L` |

Keep it to the number + one idea. A headline that needs a comma usually needs cutting.

---

## Part 3 — The Quality Bar (before a draft is "good")

Trimmed from the `copy-editing` **Seven Sweeps** to the five that matter for a short auction post. Run each draft through these. The robot enforces the objective ones (★) in code; you judge the rest.

1. **Clarity** — one idea, readable in one pass. If a sentence needs re-reading, cut it.
2. **★ Prove It** — the post must contain a concrete figure (a ₹ price or a date). No number = no post. This is what "the number does the work" means, enforced.
3. **So What** — every claim answers "so what?" for a bidder. "Reserve ₹38L" → *so what?* → "₹7L below its last listing." Add the bridge.
4. **Voice** — lowercase, calm, no hype, no corporate cliché, one emoji max. Read it aloud; if it sounds like a brochure, rewrite.
5. **★ Honesty** — none of the banned words; nothing implying legal certainty or a guaranteed outcome. Enforced as a hard drop.

---

## Part 4 — Anti-patterns (what generic AI copy looks like — and our fix)

| ❌ Generic slop | ✅ AuctionScope |
|---|---|
| "🔥🔥 AMAZING investment opportunity!! Don't miss out!!" | "₹38L reserve, {city}. Was ₹45L last round. Ends Aug 1." |
| "This property offers incredible value and potential." | "₹7L below its last listing. Same plot, second auction." |
| "Guaranteed clean title, fully verified!" | *(banned — we can't say this; we help you check, we don't certify)* |
| "Unlock your dream home today!" | "A {type} in {city} you can actually research before bidding." |
| Vague, no number, no date, no place | Always: one number, one place, honest frame |

The tell of AI slop is **adjectives doing the work** ("amazing", "incredible", "unlock"). Our copy makes the **noun and the number** do the work.

---

## Part 5 — Worked examples

**Price-drop (strong):**
> ₹45L → ₹38L. This Coimbatore plot didn't sell last round, so the bank cut the reserve 15%. Re-auctions are where the quiet deals are. Ends 1 Aug — check the notice + flood/water data before you bid. auctionscope.in

*Why it works: number leads · the drop is the story · honest "reserve"/"notice" framing · points at the real feature (checking) · no hype.*

**Closing-soon (fine, not great):**
> A residential plot in Chennai, reserve ₹40L, auction closing soon. Worth a look. auctionscope.in

*Fails "Prove It" softly (no date) and "So What" (why worth a look?). Fix: add the date, add the bridge — "closing 1 Aug · ₹40L is under the ₹48L area median."*

**Evaluation/awareness (strong, no single auction):**
> a sale notice gives you a price. it won't tell you if the area floods, how far it is from your office, or whether ₹40L is fair vs the market. that's the part we do. 542 live auctions in tamil nadu, all askable. auctionscope.in

---

## How the Poster uses this
`build_prompt()` injects a condensed version of Part 1 (hooks by angle) and Part 3 (the quality bar) so every automated draft is anchored on these structures. The two ★ checks (Prove It = has a figure; Honesty = no banned words) are enforced in `validate_drafts()` — a draft that fails is dropped, not published. Everything else here is for the human reviewer and for writing by hand. Keep this file and the code in sync: if you add a hook family here, add it to the prompt.
