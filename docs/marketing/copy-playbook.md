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

> **How the gap is computed (and when we do NOT claim one).** The method differs by property type.
>
> **Flat / apartment — value it as land + building (the summation method), not one ₹/sqft.** A flat is two assets: the **construction** (built-up area — a depreciating asset) and the **land** (its UDS — an appreciating asset). Estimate the fair value of an equivalent flat, then compare to the reserve:
> `fair_value = built_up_sqft × construction_rate + uds_sqft × land_rate`
> - `construction_rate` = current build cost/sqft — Chennai 2026 ≈ **basic ₹2,000 / standard ₹2,300–2,500 / premium ₹3,000** (source: Chennai construction-cost guides). **Depreciate for an older flat** — construction is not new-build.
> - `land_rate` = the area's **land** ₹/sqft, web-sourced and cited — the *land* rate, **not** the new-flat asking rate.
> - **Both built-up and UDS are used** (as multipliers), so UDS is essential here — just never a *divisor*. The notice states both, e.g. *"built-up area 671 sq.ft … undivided share of the land 393 sq.ft."* If built-up is absent (UDS only), construction can't be valued → drop the gap.
> - Worked example (built-up 1000, UDS 300, standard build): `1000×₹2,300 + 300×₹5,000 = ₹23L + ₹15L =` **₹38L** fair value for an equivalent new flat.
> `gap% = (fair_value − reserve) ÷ fair_value`. This beats "reserve ÷ built-up vs new-flat asking rate": asking rates bake in builder margin and overstate the gap (why the earlier per-sqft method spat out a suspicious ~60% on Ambattur).
>
> **Land / plot / land & building:** `fair_value = land_extent × area_land_rate` (web-sourced, cited); `gap% = (fair_value − reserve) ÷ fair_value`. Normalise units first (acre / cent / ground / sq.m → sqft). Segment correctly: an Ambattur *land* rate ≠ an Ambattur *flat* rate.
>
> **Drop the gap when:** the extent unit is ambiguous or unconverted; the property **type disagrees with the described asset** (a "flat" described as vacant land); for a flat, built-up is missing; or a rate rests on a single stale listing.
>
> **Frame honestly, always:** call it an **"estimated fair value vs reserve"**, give a **range not a point**, and add **"approximate — verify extent, age/condition, and possession."** Capture and surface **possession type** (symbolic vs physical) — a symbolic-possession flat with a big gap is the textbook "ask why." A *large* gap (say >40%) is a **reason to investigate** (possession, UDS-only sale, encumbrances, litigation, an old/depreciated building), not a guaranteed bargain — say so. This angle needs the **research-verified tier** (`content-pillars.md`): the land/construction rates are web-sourced and must carry a source.

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
