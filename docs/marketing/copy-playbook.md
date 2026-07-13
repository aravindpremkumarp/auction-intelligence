# AuctionScope — Copy Playbook

*The single source of exceptional copy. Two audiences read it: **you**, when writing a post by hand, and **the Poster robot**, which has the essentials injected into its prompt (`marketing_agents/poster.py` → `build_prompt`). Both write from the same frameworks so the voice is one voice.*

*Translated from three installed skills — `social` (hook formulas), `copywriting` (headline formulas), `copy-editing` (the quality bar) — into AuctionScope's data (reserve price, EMD, re-auction, ₹ lakhs, real cities/banks) and voice. Generic framework → auction-specific swipe line.*

*This playbook covers **how** to write. **What** to write about comes from the pillar system in `content-pillars.md` — 8 angle machines (deals, education, market data, news, geo, evaluations, Q&A, build-in-public), each a feed that generates posts indefinitely.*

---

## The voice, in one breath
Lowercase, calm, plain-spoken. **The number does the work** — lead with it, don't decorate it. One idea per post. Honest framing always ("reserve price", "bank auction", "ends <date>"). We help people **research and evaluate**; we never claim legal certainty. Full voice: `.agents/product-marketing.md`.

**Never use** (hard rule, enforced in code): *due diligence · advocate · legal opinion · title-clear · guaranteed · institutional · revolutionary*. We help evaluate; we don't do legal diligence.

---

## Part 1 — The Hook System

The first line decides whether anyone reads the rest. It is engineered, not decorated.

Our old hooks had one quiet flaw: they were **specific and complete**. `₹45L → ₹38L. Same plot, 15% lower reserve after a failed auction.` gives the fact *and* the explanation in one breath — the reader learns everything and keeps scrolling, satisfied. Generic AI slop has the opposite flaw: incomplete and vague ("You won't BELIEVE this deal!"). The formula that stops the scroll is **specific but incomplete**: the concrete fact creates credibility, the withheld meaning creates pull. Give the number; hold back the *why*, the *how*, or the *what it costs you*.

*(Distilled July 2026 from scroll-stopping hook mechanics — pattern interrupt, curiosity gap, loss aversion, self-relevance — cross-checked against the installed `social` / `copywriting` skills, and kept subordinate to the honesty rule. Power-word hype was reviewed and deliberately rejected: adjectives never carry our hooks.)*

### The stop test (every hook passes all four)

1. **STOP** — breaks the feed's pattern. A ₹ figure, a contrast, a sharp question. Never a warm-up sentence.
2. **YOU** — the target buyer feels addressed: their city, their budget band, their risk, their identity ("hunting a plot in Coimbatore…").
3. **GAP** — opens one specific question that the body answers. Never resolve the hook inside the hook.
4. **TRUE** — every word from real auction fields. Deadlines are facts, never hype. The gap you open must be *closed by a fact later in the post* — if the facts can't cash the hook's promise, shrink the hook; never inflate the body. (This is the honesty rule applied to attention.)

### Form (enforced in code)

- The hook is **its own first line, ≤ 100 characters**, followed by a blank line. Instagram folds captions at ~125 chars; a hook that dies at the "…more" fold never happened.
- Never open with throat-clearing: *did you know · attention · imagine · are you looking for · introducing · we're excited · don't miss · hurry · last chance*. (Banned as openers in `validate_drafts()`.)
- One emoji max, never as the opener.

### The 8 mechanisms (rotate — max 2 drafts per mechanism per batch)

A mechanism is *why* a hook stops someone. The old hook families survive inside them as swipe lines. Vary mechanisms across a batch and across the week: any shape repeated daily becomes wallpaper, and wallpaper gets scrolled past.

| # | Mechanism | Why it stops | Auction swipe lines (grounded) |
|---|---|---|---|
| 1 | `contrast` — price shock | Two numbers that shouldn't both be true | `₹45L → ₹38L. same {city} plot, two months apart.` · `the bank wanted ₹{prev}L for this in march. today: ₹{now}L.` |
| 2 | `question` — the self-test | An open question the reader must answer about *themselves* | `would you bid ₹{now}L on a plot you've only seen as a PDF?` · `how far is this ₹{now}L flat from your office? the notice will never tell you.` |
| 3 | `mistake` — loss aversion | Losses loom ~2× larger than gains; a real risk names real money | `a ₹{now}L plot is not a deal if the area floods every november.` · `win the auction, miss the payment window, forfeit the ₹{emd}L EMD. the timeline is in the notice.` |
| 4 | `hidden` — information asymmetry | "The list is public and nobody reads it" makes the reader an insider | `banks in TN are selling {live} properties right now. the list is public. almost nobody reads it.` |
| 5 | `myth` — belief violation | Contradicting a held belief demands resolution | `you don't need ₹{now}L in cash to bid — the EMD here is ₹{emd}L.` · `auction ≠ 50% off. here's the real gap, from our data.` *(only with a computed stat)* |
| 6 | `callout` — identity selector | Self-relevance is the #1 scroll filter; call the buyer, not the property | `hunting a plot in {city} under ₹{X}L? a bank just listed one at ₹{now}L.` |
| 7 | `countdown` — honest deadline | Time pressure that is a *fact*, plus an unanswered stake | `{n} days left. someone gets this {city} {type} at ₹{now}L. did anyone check the flood map?` |
| 8 | `process` — how-it-works curiosity | A visible outcome with an invisible cause | `why is this flat ₹7L cheaper the second time the bank auctions it? (routine, not a glitch.)` |

Angle mapping: `price_drop` → contrast / process · `closing_soon` → countdown / callout · `cheapest` → callout / hidden · evaluation & education posts → question / mistake / myth / hidden.

> **Grounding rule:** every `{placeholder}` must be filled from a real auction's fields. Never invent a number, city, date, or drop %. If the fact isn't in the data, don't write the hook.

### The 5-line skeleton (retention after the stop)

A hook buys ~2 seconds. The body re-earns attention line by line:

1. **Hook** — stop test, own line, ≤100 chars.
2. **Stakes** — the "so what" for a bidder (why this gap matters to *you*).
3. **Payoff** — the facts; the number does the work. *Closes the hook's loop.*
4. **Context** — vs last listing, vs area, what to check before bidding.
5. **CTA** — one soft action (`ask it on auctionscope.in`, `read the notice first`).

### The 3-variant discipline

Never keep the first hook. Write **3 candidate hooks using 3 different mechanisms**, judge them against the stop test, lead with the winner. The Poster does this in-prompt and returns the runners-up in `hook_alternatives`, so the human editor can swap in one glance. Writing by hand? Same rule — the second and third hooks are usually where the good one hides.

### Hook surfaces (one hook, four formats)

| Surface | Budget | Note |
|---|---|---|
| Caption line 1 | ≤ 100 chars | survives the "…more" fold |
| Card `image_headline` | ≤ 8 words | the hook compressed, same mechanism |
| Reel first frame | ≤ 8 words on screen at 0.5s | motion starts immediately (`lib/motion.js` handles this); the text IS the scroll-stopper |
| Carousel slide 1 | one line + visual | slide 1 is a poster, not a paragraph; the gap resolves on slides 2+ |

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

Keep it to the number + one idea. A headline that needs a comma usually needs cutting. The headline is the post's hook **compressed** — same mechanism, fewer words — never a second, competing idea.

---

## Part 3 — The Quality Bar (before a draft is "good")

Trimmed from the `copy-editing` **Seven Sweeps** to the five that matter for a short auction post. Run each draft through these. The robot enforces the objective ones (★) in code; you judge the rest.

1. **Clarity** — one idea, readable in one pass. If a sentence needs re-reading, cut it.
2. **★ Prove It** — the post must contain a concrete figure (a ₹ price or a date). No number = no post. This is what "the number does the work" means, enforced.
3. **So What** — every claim answers "so what?" for a bidder. "Reserve ₹38L" → *so what?* → "₹7L below its last listing." Add the bridge.
4. **Voice** — lowercase, calm, no hype, no corporate cliché, one emoji max. Read it aloud; if it sounds like a brochure, rewrite.
5. **★ Honesty** — none of the banned words; nothing implying legal certainty or a guaranteed outcome. Enforced as a hard drop.
6. **★ The Stop Test** — the first line passes STOP / YOU / GAP / TRUE (Part 1). The objective slices are enforced in code: hook ≤ 100 chars, no throat-clearing openers, max 2 drafts per mechanism per batch. The judgment slice — *does it actually open a gap?* — is yours.

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

**Informative vs. magnetic (same auction, same facts):**
> ❌ *informative — resolves itself:* ₹45L → ₹38L. Same Coimbatore plot, 15% lower reserve after a failed auction.
>
> ✅ *magnetic — opens a gap:*
> ₹45L → ₹38L. same coimbatore plot, two months apart.
>
> nothing about the plot changed. what changed is that nobody bid last round, so the bank cut the reserve — that's routine in re-auctions, and it's where the quiet deals hide. ends 1 Aug. before anyone bids: check the notice, the flood history, and whether ₹38L is actually under market. that's the part we do. auctionscope.in

*The ❌ version is true, concrete, and finished — the reader nods and scrolls. The ✅ version holds back the "why" for line two (GAP), then closes the loop with facts (TRUE). Hook: 52 chars, `contrast` mechanism.*

**Price-drop (strong):**
> ₹45L → ₹38L. This Coimbatore plot didn't sell last round, so the bank cut the reserve 15%. Re-auctions are where the quiet deals are. Ends 1 Aug — check the notice + flood/water data before you bid. auctionscope.in

*Why it works: number leads · the drop is the story · honest "reserve"/"notice" framing · points at the real feature (checking) · no hype. To pass the new stop test, split it: hook on its own line, the explanation below.*

**Closing-soon (fine, not great):**
> A residential plot in Chennai, reserve ₹40L, auction closing soon. Worth a look. auctionscope.in

*Fails "Prove It" softly (no date) and "So What" (why worth a look?). Fix: add the date, add the bridge — "closing 1 Aug · ₹40L is under the ₹48L area median."*

**Evaluation/awareness (strong, no single auction):**
> a sale notice gives you a price. it won't tell you if the area floods, how far it is from your office, or whether ₹40L is fair vs the market. that's the part we do. 542 live auctions in tamil nadu, all askable. auctionscope.in

---

## Part 6 — The hook feedback loop (how we learn which hooks work)

A hook system without measurement is a style guide; with measurement it's a machine that improves. Every staged draft now records its `hook_mechanism`, so performance can be attributed to the *mechanism*, not just the post:

1. **Per post, record on publish:** mechanism · platform · pillar. (It's already in `drafts.json`.)
2. **Read the platform's stop-rate proxy:** reels → 3-second views ÷ reach and completion rate; carousels → swipe-through past slide 1; captions → expands/"see more" where visible; everywhere → saves and profile taps (GA4 funnel events pick up from there).
3. **Weekly (Agent B "Reporter", `content-agents.md`):** correlate mechanism ↔ stop-rate ↔ saves ↔ site visits. Output is an action, not a chart: *"`mistake` hooks doubled `contrast` on reels — next week 3 mistake-led education posts."*
4. **Kill / scale rules** (from the `social` skill's iteration table): 5+ posts of one mechanism under the account median → bench that mechanism for 2 weeks. A mechanism that wins twice in a row → lead the next batch with it (variety cap still applies).

Until analytics volume exists (~pre-traction), run the loop qualitatively: which drafts did the founder *choose to publish*, and which hook alternative did he swap in? `hook_alternatives` in each draft makes that choice visible — that editorial signal is the first training data.

---

## How the Poster uses this
`build_prompt()` injects a condensed version of Part 1 (the stop test, the 8 mechanisms with swipe lines, the 5-line skeleton, the 3-variant discipline) and Part 3 (the quality bar) so every automated draft is anchored on these structures. The objective ★ checks are enforced in `validate_drafts()` — a draft that fails is dropped, not fixed: Prove It (has a figure) · Honesty (no banned words) · hook length (first line ≤ 100 chars) · no throat-clearing openers · mechanism variety (max 2 per batch). Each draft carries `hook_mechanism` + two `hook_alternatives`, surfaced in `review.md` so the human editor can swap hooks in one glance. Everything else here is for the human reviewer and for writing by hand. Keep this file and the code in sync: if you add a mechanism here, add it to the prompt and the `HOOK_MECHANISMS` tuple.
