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

### The hook database (the arsenal)
The curated per-pillar hook list lives in **`marketing/hooks.json`** (human rendering: `docs/marketing/hook-database.md`) — ~90 stop-test-passing concepts, each expressed on all three surfaces at once (caption / reel first-frame / card headline), tagged by mechanism, budget-checked and honesty-scanned in CI (`TestHookDatabase`). The Poster injects the relevant pillar's entries into its prompt as the HOOK ARSENAL and adapts from them before inventing; humans swipe from the same file. Edit the JSON, run `python marketing/gen_hook_doc.py`, and the tests keep every entry within the on-screen budgets.

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

Angle mapping: `price_drop` → contrast / process · `closing_soon` → countdown / callout · `cheapest` → callout / hidden · `market_gap` → contrast / myth *(computed stat only — see below)* · evaluation & education posts → question / mistake / myth / hidden.

### The `market_gap` angle (reserve vs estimated fair value — computed, never asserted)
The single most attractive fact about an auction is that the reserve can sit **below local market rates**. But this is the biggest honesty trap in the whole playbook: it is a **claim only when computed**. The reserve alone proves nothing — banks sometimes set reserves *above* what buyers will pay (that's *why* re-auctions exist). Swipe lines (feed the `contrast` / `myth` mechanisms):
- `₹{fair}L is what an equivalent {area} flat pencils out to. this one's reserve: ₹{now}L.`
- `{gap}% below our fair-value estimate for {area}. a gap that size is a reason to look closer, not to assume.`
- `auction ≠ 50% off. here's the real gap for this {area} {type}, computed: {gap}%.`

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

**This is wired, not aspirational.** The compressed hook (`image_headline`) is burned onto the card as its visual headline for **both static formats and the carousel cover** — not only reels. The static templates (`deal-of-the-day-1080`, `price-drop-1080x1350`) and the carousel (`city-carousel-1080x1350`) each render a `headline` field as the hero line, with the property title stepping down to a supporting line. When no hook is supplied the templates fall back to the old title-led layout, so nothing regresses. The Poster emits a ready-to-render card island per image draft (`marketing/outputs/<date>/cards/*.json`) and prints the exact `render_social.py` command in `review.md` — see "How the Poster uses this."

---

## Part 2 — Headline Formulas (for the card image, `image_headline` ≤ 8 words)

From the `copywriting` skill's headline families, cut to auction length. These become the `headline` field burned onto the static card and the carousel cover (same mechanism as the caption hook, compressed) — not the caption itself. Because it's a published surface, `image_headline` is honesty-scanned (banned words drop the draft) and length-capped (≤ 64 chars so it fits the card) in `validate_drafts()`.

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

## Part 6 — The full post: every text layer (not just the caption)

A published post is more than the image + a caption. Each layer is copy we must write. **Covered** = the Poster emits it today; **new** = add it.

| Layer | Status | Rule |
|---|---|---|
| **Caption** | covered (`post`) | first line **is** the hook — it must land before the "…more" fold (IG ~125, LinkedIn ~210, YT ~100, X full-280 chars). One caption, trimmed per platform. |
| **Hashtags** | covered (`hashtags`) | see the mix below; count per platform. |
| **Pinned first comment** | **new** | the link + the honest disclaimer + an engagement question (details below). |
| **Alt text** | **new** | ≤125 chars describing the image for screen readers — accessibility, and a small SEO signal. |
| **Video / Shorts title** | **new** | ≤70 visible chars, keyword-first — Shorts/YouTube are *searchable* ("bank auction flat Kanchipuram ₹21.8L"). |
| **Location / geo tag** | **new** | always tag the **city/area** — we are a *local* product; this is free local discovery. |
| **Cover frame / slide-1** | **new** | name the scroll-stopper: the reel's cover frame (the reveal + badge) or carousel slide 1. |
| **Link placement** | **new** | IG/LinkedIn suppress in-body links → link goes in the **pinned comment** (or bio); YouTube → description; X → inline is fine. |
| **Audio track** (reels) | **new** | a trending/ambient track lifts reach; source via the HyperFrames `media-use` skill. |

### Hashtag strategy (per platform)
Counts from `social/references/platform-limits.md`: **Instagram/TikTok/Shorts 3–5 · LinkedIn 3–5 · Facebook/X 1–2 · YouTube 3–5** (first 3 show above the title). Build each set from four buckets: **1 category** (`#bankauction`), **1–2 niche** (`#chennairealestate`, `#sarfaesi`), **1 geo** (`#kanchipuram` — matches the location tag), **1 branded** (`#auctionscope`). No `#` prefix in the data; the publisher adds it. Never keyword-stuff — YouTube ignores *all* hashtags past 15.

### Pinned first comment (the honest workhorse)
The first comment we pin does three jobs the caption shouldn't carry:
1. **The link** — `auctionscope.in/property/{id}` (and the sale-notice URL), because in-body links kill reach on IG/LinkedIn.
2. **The honest disclaimer** — "not legal advice; a bank e-auction under SARFAESI — verify reserve, EMD, possession type and encumbrances with the bank before bidding." This is also where **education/market_gap posts carry their source URLs** (the research-verified tier's citation requirement).
3. **An engagement prompt** — one genuine question ("seen a re-auction like this — did you bid or walk away?"). Comments in the first hour drive distribution.

The pinned comment is bound by the **same honesty rule** as the caption (banned words, no legal certainty).

### Worked example — the Kanchipuram price-drop, all layers
- **Caption:** `₹27L → ₹21.8L. this kanchipuram flat didn't sell last round, so the bank cut the reserve 19%.\n\nre-auctions are where the quiet deals sit — but a cut that size is a reason to look closer, not to assume.\n\nbids close today (11 jul). check the sale notice, possession type, and the area's flood/water data first.\n\n→ details + link in the first comment.`
- **Hashtags (IG/Shorts):** `bankauction · kanchipuram · sarfaesi · chennairealestate · auctionscope` → **(X/FB trim to)** `bankauction · kanchipuram`
- **Pinned comment:** `full notice + details → auctionscope.in/property/800979\n\nnot legal advice — this is a SARFAESI bank e-auction; verify reserve, EMD, possession type and encumbrances with the bank before bidding.\n\nseen a re-auction like this — what made you bid or walk away?`
- **Alt text:** `Deal card: a Kanchipuram flat, bank-auction reserve cut from ₹27 lakh to ₹21.8 lakh (19% lower), bids close 11 July.`
- **Video title (Shorts):** `Bank auction flat in Kanchipuram — reserve ₹21.8L | AuctionScope`
- **Location tag:** `Kanchipuram, Tamil Nadu` · **Cover frame:** the ₹21.8L reveal with the −19% badge.

### Poster schema additions (to build these automatically)
The draft JSON gains: `pinned_comment` (string, **required** — a draft without it is dropped, since it carries the link + disclaimer layer), `alt_text` (≤125), `video_title` (≤70), `location_tag` (string). **Reel fields (built):** `needs_reel`, `reel_hook` {line1 ≤18 chars with a figure — the video's FIRST FRAME; line2 ≤28 chars — the gap}, `reel_context_lines` (exactly 2, ≤30 chars, must not resolve the gap), `engagement_question` (ends "?", mirrors the pinned comment's question), `save_line` (≤40 chars, factual save reason). The prompt scores the 3 hook candidates against the stop test and compresses the strongest into `reel_hook` — automatic hook selection. All reel lines pass the honesty scan; gates in `validate_drafts()` drop violations. `post` and `hashtags` already exist. `pinned_comment` is validated for banned words like `post` is.

---

## Part 7 — The hook feedback loop (how we learn which hooks work)

A hook system without measurement is a style guide; with measurement it's a machine that improves. Every staged draft now records its `hook_mechanism`, so performance can be attributed to the *mechanism*, not just the post:

1. **Per post, record on publish:** mechanism · platform · pillar. (It's already in `drafts.json`.)
2. **Read the platform's stop-rate proxy:** reels → 3-second views ÷ reach and completion rate; carousels → swipe-through past slide 1; captions → expands/"see more" where visible; everywhere → saves and profile taps (GA4 funnel events pick up from there).
3. **Weekly (Agent B "Reporter", `content-agents.md`):** correlate mechanism ↔ stop-rate ↔ saves ↔ site visits. Output is an action, not a chart: *"`mistake` hooks doubled `contrast` on reels — next week 3 mistake-led education posts."*
4. **Kill / scale rules** (from the `social` skill's iteration table): 5+ posts of one mechanism under the account median → bench that mechanism for 2 weeks. A mechanism that wins twice in a row → lead the next batch with it (variety cap still applies).

The report→prompt bias hook is live: `step_prepare` injects the newest `report.json`'s per-angle/format table into the drafting prompt ("bias toward what the report scales; variety cap still applies"). Until analytics volume exists (~pre-traction), run the loop qualitatively: which drafts did the founder *choose to publish*, and which hook alternative did he swap in? `hook_alternatives` in each draft makes that choice visible — that editorial signal is the first training data.

---

## How the Poster uses this
`build_prompt()` injects a condensed version of Part 1 (the stop test, the 8 mechanisms with swipe lines, the 5-line skeleton, the 3-variant discipline) and Part 3 (the quality bar) so every automated draft is anchored on these structures. The objective ★ checks are enforced in `validate_drafts()` — a draft that fails is dropped, not fixed: Prove It (has a figure) · Honesty (no banned words, caption **and** headline) · hook length (first line ≤ 100 chars) · headline length (≤ 64 chars when it needs an image) · no throat-clearing openers · mechanism variety (max 2 per batch). Each draft carries `hook_mechanism` + two `hook_alternatives`, surfaced in `review.md` so the human editor can swap hooks in one glance.

**Caption → card, one hook.** For every image draft, `draft_to_island()` maps the draft + its grounded source fields into the exact `#data` island a `marketing/templates/` card expects, with the compressed hook as the `headline`. `--finalize` writes these to `marketing/outputs/<date>/cards/*.json` and prints the `render_social.py` command in `review.md`, so the same hook that leads the caption also leads the static card / carousel cover. Every figure on the card comes from the auction's own fields; missing facts (no EMD, no comparable) are blanked, never invented (the binder clears empty slots and the templates hide the wrappers).

**The carousel is the one multi-property surface, so it inverts the split.** A card or a reel belongs to one auction, and the model chooses which. A carousel is a city — "the N cheapest `<asset>` in `<city>`" — so `select_carousel()` picks the slides *first*, from the price-ascending live page, before the model sees anything: the group is the city with the most live lots of one type (≥ `MIN_CAROUSEL_SLIDES`, capped at `MAX_CAROUSEL_SLIDES`), and every slide's price, bank and date is its own row's. The model is handed those slides as a brief and writes only the cover hook + caption. `validate_carousel()` then gates that free text with the caption rules **plus four grounding checks the single-auction path doesn't need**: it must name the city on the slides, must not name another city from the pool, every `₹` it quotes must fall inside the span of figures actually on screen (±5% for the voice's rounding), and any count it states must be the real slide count. A failure drops the carousel copy only — the batch still ships.

Two claims the selection is careful about, because both are easy to make false:
- **"cheapest"** holds only because the page is sorted `price_asc`: a price-ascending prefix restricted to one city *is* that city's cheapest, in order.
- **"this week"** is computed, not assumed — `_week_label()` says `this week` only when every selected auction is within 7 days, and `right now` otherwise.

**One hook, now four surfaces.** `--auction-id <id>` stages the full kit for a single property: card + a 6-slide `property-carousel-1080x1350` + caption. The carousel cover is the *same* `image_headline`, so the hook that leads the caption leads the card and the swipe post too, with no extra model output and no extra gate to write. Its slide 5 ("check before you bid" — encumbrance certificate, possession status, notice terms, site visit) is fixed template copy and is never model-written: that list is the honesty position, not marketing. The kit is off in a normal batch by design — five drafts would put 30 extra slides through a gate that publishes about five posts a week.

**The surface with no copy at all.** `property-og-1200x630` is the exception to this whole document: no hook, no headline, no caption, every glyph a field off the record (`scripts/generate_property_og.py`). That is precisely why it can run across the entire inventory unreviewed, and why it is the only per-property asset that scales. It exists because all 664 prerendered property pages were declaring the same picture — the generic site logo — in `og:image`, `twitter:image`, and the `image` field of their Product / RealEstateListing JSON-LD. Copy rules do not apply to it; the honesty rules still do (an ended auction goes neutral and reads "Auction closed", and a drop percentage is computed from the two notices rather than authored).

Everything else here is for the human reviewer and for writing by hand. Keep this file and the code in sync: if you add a mechanism here, add it to the prompt and the `HOOK_MECHANISMS` tuple; if you add a card template, map its angle in `ANGLE_TEMPLATE` and give it a `headline` slot.
