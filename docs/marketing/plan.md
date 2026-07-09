# AuctionScope — Marketing Plan v1

**Prepared by:** CMO (fractional, via Claude Code marketing-skills)
**For:** Aravind (founder) & team
**Date:** 2026-07-09
**Status:** Draft v1 — for founder review. *Numbers verified against the live Neo4j graph + code audit on 2026-07-09.*

> **Honesty rule (founder-set):** we do not do legal/title "due diligence." The only per-property document we hold is the sale notice. Copy must never claim diligence, advocate-replacement, or legal certainty. The real hero is AI web-search enrichment — "help me evaluate this auction," researched and cited, approximate.

---

## 1. Executive summary

**What this plan optimizes for:** turning a data asset into search demand, and a genuinely useful AI feature into a reason to sign up. AuctionScope has ~600 live (2,179 total) enriched Tamil Nadu bank-auction listings, natural-language search, and — the underrated part — an AI agent that can research the web to answer the questions a sale notice can't (water, flooding, nearby projects, transport, amenities, location, market price vs. reserve). The product is ahead of the category; the go-to-market is a placeholder and there are ~7 users. For the next 12 months the game is organic discoverability plus a first cohort of real users. Bootstrapped, organic-first; no paid-budget assumption.

**Three big bets, ranked by leverage:**

**Bet 1 — Programmatic SEO off the graph is the growth engine.** Competitors (IBAPI, bankeauctions.com, BAANKNET) rank on thousands of city/bank/property-type pages; AuctionScope ranks on ~1 — a 4-URL sitemap behind a client-rendered SPA Google can barely index. The move: make the app crawlable (SSR/prerender), then generate structured, schema-marked landing pages from the graph across 49 cities / 38 districts / property types / price bands. Honest sizing: hundreds-to-low-thousands of pages spanning **live and historical** auctions (only ~600 are live at a time; expired ones still have SEO value as price-history). Gate pages on the ~31% of records with "complete" descriptions to avoid thin content.

**Bet 2 — The AI evaluation feature is the wedge, and it's under-sold.** "AI search over auctions" is interesting; "ask whether this plot floods, what's being built nearby, how far it is from your area, and whether the reserve is fair — and get a cited answer in seconds" is a *reason to sign up and come back*. This is the honest, differentiated value: natural-language search **+ web-search enrichment** (groundwater, waterlogging/flood, govt/private projects, metro/bus, schools/hospitals, approximate location & travel time, market-vs-reserve), plus per-property chat and watchlist. Lead the site, the content, and the upgrade prompt with this — *not* with "diligence."

**Bet 3 — Instrument first, or fly blind forever.** There is no web analytics installed today (no GA4/GTM/PostHog/pixel in `web/`). Every optimization below is unmeasurable until this is fixed. Cheap, fast, week one.

**What twelve months looks like, plausibly (organic-first, bootstrapped):**
- From ~invisible to a few hundred → low-thousands of monthly organic sessions from programmatic pages + educational content.
- A measurable funnel end to end (session → signup → activated search/evaluation → Pro).
- A first real user cohort (from ~7 today) and a repeatable Pro-conversion motion tied to deeper research sessions.
- An "auction alerts" email list as the compounding retention asset (currently nonexistent).
- A HyperFrames-powered social engine turning daily auction data into deal + evaluation content, cross-posted across all platforms (brand assets already built).
- A trust surface (About/entity/data-sources/visible pricing) — currently anonymous.

**90-day priorities:**
1. Install analytics + a tracking plan (GA4 + GTM) and define the funnel. `/analytics`
2. Confirm the scraper/enrichment pipeline runs on a cadence — live inventory decays fast (see §13). `pipeline/`
3. SSR/prerender the SPA and fix canonical + metadata so the site is crawlable. `/seo-audit`
4. Ship a programmatic-SEO pilot (Chennai + Kanchipuram, top property types) with schema. `/programmatic-seo` + `/schema`
5. Rewrite the above-the-fold around the evaluation feature + add "auction alerts" email capture. `/copywriting` + `/popups`
6. Publish the first 4 educational posts (how bank auctions work; EMD; evaluating a location; reading a sale notice). `/content-strategy`

---

## 2. Strategic frame

### What AuctionScope is, in one sentence
The AI research assistant that helps you find and evaluate Indian bank-auction (SARFAESI) property in plain English — search the auctions, then ask the questions the sale notice can't answer and get cited, web-researched context.

### The category we're claiming
Redefining the Indian bank-auction portal from "listings directory" to "search + evaluation assistant." Incumbents compete on inventory; AuctionScope competes on *understanding a property fast* — is the location good, does it flood, what's nearby, is the price fair. Not a diligence/legal product — an evaluation copilot.

### Who we're for (ICP, distilled)
- **Retail bank-auction buyers in Tamil Nadu** (expanding) — individuals and small investor syndicates. Not banks, enterprises, or auction houses.
- **Stated problem:** "I can't find good auction properties easily." **Real problem:** "even when I find one, the notice tells me nothing about the place or whether the price is fair, and researching each by hand is slow."
- **What they're buying:** fast, plain-English evaluation of a specific auction — location, water/flood, connectivity, amenities, market-vs-reserve — plus searchable inventory.
- **The status-quo competitor is the DIY workaround** — portals + WhatsApp + manually Googling each location.
- **Search behaviour is high-intent and land/residential-heavy:** *"plots in Kanchipuram <50L," "residential Chennai," "Canara Bank auction Coimbatore."*

### The business model logic
Freemium subscription. Browsing + basic chat free; heavier AI usage is the upsell. **Pro = ₹499 / 30 days** (Razorpay): anon 10 chats/day → free 20/day → **Pro 1,000/day** + the Pro model + higher reasoning effort (longer, deeper research sessions). Deep-research mode is free but login-gated. Compounding thesis: proprietary graph → programmatic SEO → organic traffic at near-zero marginal cost → free activation on the evaluation feature → Pro conversion for heavy users. Unit economics unproven (no instrumentation) — establishing CAC + retention is the top open decision (§13).

### Brand voice (the non-negotiable)
From the live product + `.agents/product-marketing.md`:
- **Tone:** understated, plain-spoken, trustworthy. Lowercase, calm, no hype.
- **Grounded + honest about limits.** The agent never invents graph facts; web research is cited. Say "research, not legal advice" and "verify with the bank." **Never** claim diligence/advocate-replacement/legal certainty.
- **Use:** bank auction, SARFAESI, reserve price, EMD, re-auction, price drop, groundwater, waterlogging/flood, connectivity, market price, plots/land/flats, real city/bank names.
- **Avoid:** "due diligence," "advocate," "legal opinion," "title-clear," "institutional," "guaranteed," hype.
- **Visual:** the live app is already clean/fintech-styled (cobalt `#0052ff`, Inter / Bricolage Grotesque / JetBrains Mono). Visual design is **not** the gap — positioning + distribution is. (`redesign/` is a separate unwired exploration; ignore for GTM.)

---

## 3. Current state

*Scored from materials (live graph, code audit) on 2026-07-09. No analytics exists; data-driven sections scored from artifacts.*

### Team composition
| Person | Role | Marketing surface area |
|---|---|---|
| Aravind | Founder / engineer | Everything — product, data pipeline, and (currently) all marketing |

No marketing owner — correct at this stage. First "hire" is the marketing-skills stack + fCMO cadence, not a person. A part-time content/SEO contractor becomes worth it once the programmatic system is proven (post-Q2). First real hire, when funding unlocks it: π-shaped Product-Marketing + Growth/Content, Manager/Lead title.

### Marketing budget (current)
- Paid acquisition: ₹0/mo (bootstrapped by choice).
- Tooling: product infra already paid (Vercel/Render/Neo4j Aura/Supabase/R2/OpenRouter); marketing tools ≈ ₹0.
- Blended CAC: **unknown** — no analytics/paid history (top open decision, §13).
- **Funding-stage tier:** Pre-seed / bootstrapped. The 90-day plan must produce results with founder time + free-tier tools only.

### Phase of SaaS growth
**$0–10K ARR, pre-traction** (~7 users, revenue just switched on). Binding constraint is distribution + first users, not budget. Growth pattern will be linear (indexed pages/week → sessions/week), with a possible step-function if a programmatic cluster breaks through or a launch lands.

### What's already done (acknowledge, then build on)
| Asset | Status | Marketing leverage |
|---|---|---|
| Enriched TN auction graph (2,179; ~600 live) | Live | The moat — fuel for programmatic SEO + data content |
| NL search + **web-search evaluation** (`internet_search`) | Live | The hero feature — under-marketed today |
| Grounded agent (no hallucination) + per-property chat | Live | Differentiator + trust story |
| Deep-research report (market + location + price history) | Live (login-gated) | Depth feature; market honestly (not "diligence") |
| Watchlist + deadline alerts (`/alerts`) | Live | Retention hook |
| ₹499 Razorpay Pro + tiered quotas/models | Live | Monetization already on |
| Already fintech-styled UI | Live | No redesign needed for GTM |
| Brand kit + LinkedIn avatar/banner | Built, unused | Founder-led channel ready |
| Dossier (user document locker) | Built, ships dark | Future/secondary — NOT a diligence claim |

### What's in-flight
| Item | Status | Blocker |
|---|---|---|
| Dossier (user doc locker) | Behind `DOSSIERS_ENABLED=false` | Go/no-go on public launch |
| `.agents/product-marketing.md` | Corrected today | Founder to fill proof-point gaps |

### What's stuck (unstick this quarter)
| Issue | Cost of inaction | Action |
|---|---|---|
| Zero web analytics | Every decision is a guess | Install GA4+GTM (week 1) |
| Inventory decays (newest auction 2026-08-08) | Live listings → 0; SEO/alerts engine starves | Confirm scraper runs on a cadence |
| SPA not crawlable; 4-URL sitemap | Invisible in the top channel | SSR/prerender + programmatic pages |
| No price shown anywhere in UI | Prospects can't self-qualify | Pricing page + visible plan |
| No lead capture | Forfeiting the auction-alerts hook | Email capture week 1–2 |
| Anonymous operator | Trust deficit | About/entity/data-source page |

### Audit rubric snapshot (17-section, scored from materials)
| # | Section | Score | Note |
|---|---|---|---|
| 1 | Positioning | 2 | Founder gets it; no surface expresses it. Now captured (and de-over-promised) in product-marketing.md |
| 2 | Customer research | 2 | Strong lived domain depth; no formal VOC practice / named users |
| 3 | Homepage | 2 | Well-styled and functional; missing a benefit headline + the evaluation story |
| 4 | Sales / product pages | 1 | No pricing/feature pages; no price shown anywhere |
| 5 | Conversion pages | 0 | None |
| 6 | Competitor comparison | 0 | None |
| 7 | Resources / content | 0 | No blog; 4-URL sitemap |
| 8 | Onboarding | 1 | Signup + first search exist; nothing designed/measured |
| 9 | Email lifecycle | 1 | Transactional auth email only; no lifecycle, no capture |
| 10 | Sales material | 0 | N/A for self-serve B2C |
| 11 | Messaging | 2 | Distinctive understated voice + clean UI; not operationalized across surfaces |
| 12 | Pricing | 2 | ₹499 live but never tested; no page; price invisible in UI |
| 13 | CRO | 0 | No tests, no instrumentation |
| 14 | GTM launches | 1 | No structured launch; Dossier dark |
| 15 | Ads (paid) | 0 | None — reflects stage, not failure |
| 16 | SEO | 1 | On-page basics only; SPA uncrawlable; ranks for ~nothing |
| 17 | Internationalization | 1 | TN-only (full 38-district), appropriate |

**Total: 16 / 85 (19%).**

**Shape interpretation:** classic "**strong product, weak everything-else.**" The relative highs cluster on the founder-and-product axis (positioning latent, domain depth, a clean live UI, a live price + a genuinely strong evaluation feature). The zeros cluster on **distribution and conversion** (content, SEO surface, conversion/comparison pages, CRO, launches). Note the gap is *not* visual design (the app is already well-styled) — it's positioning + being found. That shape is what the plan closes: quarter one is instrument + crawlable + first pages + tell the evaluation story.

---

## 4. Acquisition

### Current state
No acquisition engine. The app is the homepage; no content/listing/comparison pages; the SPA is barely crawlable; no analytics. In a category where buyers actively search, invisibility in organic is the biggest miss. Paid is off by choice. So Acquisition = organic = exploit the dataset + tell the evaluation story + a HyperFrames-powered social engine (Move 7).

### The plan
**Move 1 — Make the app crawlable (prerequisite).** SSR/prerender `web/`; one canonical host + 301 (content on non-www, sitemap on www today); per-route meta/OG. `/seo-audit`

**Move 2 — Programmatic SEO (the engine).** Landing pages from the graph: city × property-type ("bank auction plots in Kanchipuram"), city × bank ("Canara Bank auction properties in Chennai"), price bands ("bank auction properties under ₹50 lakh in Coimbatore"), per-property pages. Live pages for the ~600 current auctions + historical/price-history pages for expired ones. Gate on the ~31% "complete" records to avoid thin content. `/programmatic-seo` + `/site-architecture`

**Move 3 — Structured data.** `RealEstateListing`/`Product`/`Offer`/`BreadcrumbList` JSON-LD on every page → rich results + AI-answer eligibility. `/schema`

**Move 4 — Educational + "evaluation" content.** The buyer journey starts informational: "How do SARFAESI auctions work," "What EMD means," and — playing to the hero feature — "How to evaluate a bank-auction location (water, flooding, connectivity)," "Is an auction price a good deal? How to compare to market." This doubles as proof of the AI feature. `/content-strategy` + `/copywriting`

**Move 5 — AI-SEO.** `llms.txt` + answer-shaped content so ChatGPT/Perplexity/AI Overviews cite AuctionScope for "bank auction property India" questions. On-brand for an AI product. `/ai-seo`

**Move 6 — Comparison pages.** "AuctionScope vs bankeauctions.com," "IBAPI alternative" — honest, high-intent SERPs. `/competitors`

**Move 7 — Social content engine (HyperFrames-powered).** The complementary distribution pillar to SEO — and a natural fit, because the whole product is already HTML/CSS with a clean design system and a daily stream of auction data to turn into content.

*Production pipeline (reuses what's already in the repo):* social templates are HTML docs that inline the `:root` design tokens from `web/styles.css` (accent `#0052ff`, Inter / Bricolage Grotesque / JetBrains Mono) and reuse the on-brand property card from `web/card-variations.html` as the slide atom, rendered exactly like `brand/logo/render.py` (Playwright `set_content` → 2× screenshot, clipped to size). Reels render via the **`hyperframes` npm package** (Apache-2.0, from HeyGen — HTML/CSS/JS frames → MP4, `render({frames, width:1080, height:1920})`; no OAuth needed). Three canonical sizes cover every platform — **1080×1080** (square), **1080×1350** (portrait carousel), **1080×1920** (reel) — and per the founder's call, **one asset set is cross-posted to Instagram, Facebook, LinkedIn, X, and YouTube** (English-first).

*Three content pillars:*
1. **Deals (auto, data-driven)** — deal-of-the-day card, **price-drop** alerts (`is_reauction` + `previous_reserve_price`), "cheapest plots/flats in [city]" carousels (`/properties` + `sort=price_asc`), "new this week" / "closing soon," and inventory stat graphics (`/stats`, facets: 49 cities / 122 banks). Generated from the graph via a HyperFrames template + a small scheduled script.
2. **Evaluate (the hero feature, in motion)** — screen-recorded reels + carousels of the AI answering real questions ("does this area flood?", "what's being built nearby?", "is the reserve fair vs market?"), cited. This is the differentiator most people can't see from a static listing — show it moving.
3. **Educate / Founder** — how SARFAESI auctions work, EMD, "how to evaluate a location," build-in-public.

*Cadence:* ~5 posts/week ≈ 3 auto (Deals) + 2 human (Evaluate/Educate), including ≥1 reel/week; repurpose one pillar into carousel + reel + static + text (per the `social` skill). *Honesty rule holds:* deal posts show reserve/EMD/date grounded in the notice; evaluation posts label web research "cited, approximate — not legal advice." *Distribution:* a scheduler (Buffer/Publer free tier) or manual. *Skills:* `content-strategy` (pillars) · `social` (calendar/hooks/repurposing) · `image` (sizes) · `video` + `hyperframes` (reels) · `ad-creative` (15-template static library) · `copywriting` (captions). *Optional later:* the HeyGen/HyperFrames **MCP** adds AI-avatar reels but needs OAuth authorization.

**Move 8 — Directories + review sites + a launch moment.** Indian SaaS/AI directories for backlinks/DR; a considered Product Hunt launch tied to a real feature milestone. `/directory-submissions` + `/launch`

**Move 9 — Paid — held.** Google Search on high-intent auction keywords is the obvious first test *when there's budget + instrumented conversion.* Not in this bootstrapped plan except an optional small test if budget appears. `/ads`

### 90-day / 12-month
- Weeks 1–2: analytics; SEO audit; canonical/301; SSR scoped; confirm scraper cadence.
- Weeks 3–4: SSR shipped; Chennai + Kanchipuram programmatic pilot + schema; first 2 posts.
- Weeks 5–8: programmatic across top TN cities × type; 4 posts; `llms.txt`; first directories; LinkedIn begins.
- Weeks 9–12: city × bank + comparison pages; measure indexed pages + first sessions; 90-day review.
- Q1 crawlable + first cluster + content → Q2 TN-wide coverage + comparison library → Q3 AI-SEO citations + second-geo + launch → Q4 organic is primary.

### Skills + tools
`seo-audit`, `programmatic-seo`, `schema`, `site-architecture`, `ai-seo`, `content-strategy`, `copywriting`, `competitors`, `social`, `directory-submissions`, `launch` (held: `ads`). Tools: GA4, Search Console, the Neo4j graph as page-data source, Vercel SSR/prerender, a rank tracker.

---

## 5. Activation

### Current state
Signup (Supabase) + first search exist, undesigned and unmeasured. Browsing/search/chat are open to logged-out users (good). The aha — ask a plain-English question and get cited context on a real property — happens by accident, not by design.

### The plan
**Move 1 — Instrument the funnel.** landing → search/browse → per-property chat / web-enriched question → signup → save/watchlist → deep-research. `/analytics`

**Move 2 — Rewrite the above-the-fold around evaluation.** Keep the search box primary; add a benefit headline + proof: e.g. "Search ~600 live Tamil Nadu bank auctions — and ask whether the location floods, what's nearby, and if the price is fair." `/copywriting` + `/cro`

**Move 3 — First-run to aha.** Seed example *evaluation* prompts ("does this area flood?", "how far from [area]?", "is the reserve fair vs market?"), guide a first grounded, cited answer. `/onboarding`

**Move 4 — Email capture as activation.** "Get auction alerts for your city" — one field, immediate value, builds the retention list. `/popups`

**Move 5 — Reduce signup friction.** One-click/social signup; don't gate browsing; ask for the account at the value moment (save / deep-research). `/signup`

### Skills + tools
`analytics`, `onboarding`, `signup`, `cro`, `copywriting`, `popups`, `ab-testing` (once traffic supports). Tools: GA4, an ESP, Supabase.

---

## 6. Retention

### Current state
Only transactional auth email. No lifecycle. But watchlist + deadline alerts exist in-product and auctions are natively deadline-driven — retention email is almost free to justify.

### The plan
**Move 1 — Auction alerts (the engine).** City/watchlist new-auction digests + deadline reminders + re-auction price-drop alerts. `/emails`
**Move 2 — Welcome sequence.** Founder-voiced; drives the first evaluation query. `/emails`
**Move 3 — Win-back.** Lapsed-searcher re-engagement on new inventory in saved cities. `/emails`
**Move 4 — Deliverability.** SPF/DKIM/DMARC + warmup before volume. `/emails`

### Skills + tools
`emails`, `churn-prevention`, `sms` (optional, deadline/price-drop). Tools: an ESP with automation, Supabase, the graph for triggers. *Note: retention is real but secondary while there are ~7 users — ship the alert capture now, scale the flows as the base grows.*

---

## 7. Referral

### Current state
Per-property "copy link" exists; no program. The audience is networked, so referral has latent potential — but you can't refer from a ~7-user base.

### The plan (staged)
- **Now (light):** share-after-value (share a good find / an evaluation) + founder LinkedIn as referrer-zero.
- **Q2+ (when there's a base):** simple two-sided referral (both sides get Pro days). `/referrals`

Referral is deliberately minimal until Activation + Retention produce retained users.

---

## 8. Revenue

### Current state
Monetization is live (₹499/30 days, metered on chat volume + model + effort) but the *story* is weak: no price shown anywhere in the UI (only inside the Razorpay overlay), the "gate" is a 429 that pushes sign-in (not pay) plus cosmetic "Paid" badges, and there's no conversion analytics.

### The plan
**Move 1 — Make pricing visible.** A real pricing page: free vs Pro, honest, with the value framed as *deeper research* (more chats/day + the Pro model + higher reasoning effort → longer, richer evaluation sessions). `/pricing` + `/copywriting`
**Move 2 — A real upgrade moment.** Replace the bare 429 with an honest "you've hit today's free limit — upgrade for more research" prompt at the value moment. `/paywalls`
**Move 3 — Pressure-test price.** ₹499 untested; once there's traffic, test price / annual option. `/pricing`
**Move 4 — Dossier as a future tier (Q3+), not a diligence claim.** If Dossier goes GA, it's a "your documents, organised" tier — market it honestly, not as diligence. `/pricing`

### Unit economics
| Metric | Value | Note |
|---|---|---|
| ARPC (monthly) | ~₹499 for active payers | 30-day passes; blended ARPC unknown |
| Blended CAC | **Unknown** | No analytics/paid history — top open decision (§13) |
| Annual retention | **Unknown** | No cohort data (pre-traction) |
| LTV / LTV:CAC | **Unknown** | Blocked on the above |

Every revenue projection in §10 is a range, not a promise — CAC and retention are unmeasured until analytics ships.

### Skills + tools
`pricing`, `paywalls`, `ab-testing`. Tools: Razorpay (billing source of truth), GA4, the billing tables for cohorts.

---

## 9. 90-day roadmap

### Weeks 1–2 — Unblock
| Move | Stage | Owner |
|---|---|---|
| Install GA4 + GTM; tracking plan | Cross | Aravind |
| Confirm scraper/enrichment runs on a cadence | Acq | Aravind |
| SEO audit; canonical + 301 | Acq | Aravind |
| "Auction alerts" email capture | Act/Ret | Aravind |
| Email deliverability (SPF/DKIM/DMARC) | Ret | Aravind |

### Weeks 3–4 — Foundation
| Move | Stage | Owner |
|---|---|---|
| SSR/prerender `web/` | Acq | Aravind |
| Chennai + Kanchipuram programmatic pilot + schema | Acq | Aravind |
| Above-the-fold rewrite (evaluation + proof) | Act | Aravind |
| Pricing page + About/trust page | Rev/Cross | Aravind |
| First 2 educational/evaluation posts | Acq | Aravind |
| Build 3 HyperFrames social templates (deal / price-drop / city-carousel) | Acq | Aravind |
| Welcome email sequence | Ret | Aravind |

### Weeks 5–8 — Velocity
| Move | Stage | Owner |
|---|---|---|
| Programmatic across top TN cities × type | Acq | Aravind |
| 4 more posts; `llms.txt` | Acq | Aravind |
| City auction-alert digest live | Ret | Aravind |
| Real upgrade prompt (replace bare 429) | Rev | Aravind |
| First 5 directory submissions | Acq | Aravind |
| Social auto-gen off the graph + first Evaluate reel; ~5 posts/wk cross-post | Acq | Aravind |

### Weeks 9–12 — Compound
| Move | Stage | Owner |
|---|---|---|
| City × bank + first comparison pages | Acq | Aravind |
| Deadline + price-drop alerts | Ret | Aravind |
| First activation + conversion baseline read | Cross | Aravind |
| 90-day review + recalibrate | Cross | Aravind |

---

## 10. 12-month outlook

**Framing.** Budget method: **capacity-based** — the constraint is founder-hours, not dollars (~₹0 marketing spend beyond free tools). Growth pattern: **linear** (indexed pages/week → sessions/week), with a possible step-function from a programmatic breakthrough or a launch. Forecasts are honest ranges. A hard dependency runs underneath all of it: **live inventory must be continuously refreshed** (see §13) or there's nothing to rank or alert on.

#### Q1 — Months 1–3 (Bootstrapped)
Focus: instrument, crawlable, first programmatic cluster + content, tell the evaluation story.
Outcomes: analytics live; scraper cadence confirmed; Chennai/Kanchipuram pilot indexed; 6 posts; email capture + core alerts; pricing/About pages.
KPIs: funnel measurable; 50–150 pages indexed; first email list (100+ captures); first activation baseline.

#### Q2 — Months 4–6 (Bootstrapped)
Focus: scale programmatic across TN + content cadence + first real user cohort.
Outcomes: TN-wide coverage; comparison library; win-back live; first Pro-conversion baseline.
KPIs: low-thousands of pages; first few hundred monthly organic sessions; measurable free→Pro rate; email list into low thousands.

#### Q3 — Months 7–9 (Bootstrapped or post-seed)
Focus: AI-SEO citations, second-geo expansion, a launch moment.
Outcomes: AI-answer citations; second state's auctions + pages; a real launch; first estimable LTV:CAC.
KPIs: organic = #1 source; launch step-function; second-geo pages indexing.

#### Q4 — Months 10–12
Focus: compound + decide on paid.
Outcomes: self-sustaining organic engine; mature alerts loop; a data-backed decision on whether paid earns its place.
KPIs: organic sessions low-to-mid thousands/mo; a Pro base with a known retention curve; a real LTV:CAC to gate paid.

---

## 11. Marketing operations stack

### The thesis
A single founder + the 47-skill marketing library + a few MCP/API connections can output the work of a multi-person marketing org. Strategy stays with the founder + fCMO cadence; execution is delegated to skills and, later, a contractor for volume.

### Skills mapped to AARRR
| Stage | Primary skills | Supporting |
|---|---|---|
| Acquisition | `programmatic-seo`, `seo-audit`, `schema`, `content-strategy`, `copywriting` | `ai-seo`, `site-architecture`, `competitors`, `social`, `image`, `video`, `ad-creative`, `directory-submissions`, `launch` |
| Activation | `onboarding`, `cro`, `copywriting` | `signup`, `popups`, `analytics`, `ab-testing` |
| Retention | `emails`, `churn-prevention` | `sms`, `copywriting` |
| Referral | `referrals` | `social`, `emails` |
| Revenue | `pricing`, `paywalls` | `ab-testing` |
| Cross-cutting | `product-marketing`, `analytics`, `marketing-plan` | `customer-research`, `marketing-ideas`, `marketing-council`, `marketing-loops` |

### A concrete example
`/product-marketing` auto-drafted the positioning/ICP/voice context from the codebase in one pass; then a ground-truth audit (Neo4j MCP + code) corrected the numbers and stripped the over-promise before any copy shipped. That's the stack working: generate fast, verify against source, keep it honest.

### Capability unlocks by funding stage
| Stage | Headcount | Tooling | Channels live |
|---|---|---|---|
| Bootstrapped (now) | Founder + skills | Free GA4/GSC/ESP; `hyperframes` (HTML→MP4) + a post scheduler | Organic SEO, content, social (IG/FB/LinkedIn/X/YouTube), email |
| Seed close | + part-time content/SEO contractor | Paid Ahrefs, paid ESP | + small Google Search test, + directories at volume |
| Seed deployment | + π-shaped marketing Manager/Lead | Paid A/B, richer analytics | + paid scaling, + comparison/PR |

---

## 12. Tactical idea bank

Sections 4–8 prescribe what's *being done*. This maps what's *possible* — the 139-idea `marketing-ideas` library filtered for AuctionScope's category (Indian bank-auction, B2C self-serve), voice (grounded, no over-promise), and stage (bootstrapped, pre-traction). Run `/marketing-ideas` for the full library.

**Legend:** Now (Q1) · Q2 · Q3+ · Skip.

### 12.1 Acquisition
**Now:** #2 SEO Audit (crawlability first) · #6 Proprietary Data Content (the graph — "cheapest live auctions in Chennai this week") · #7 Internal Linking · #1 Easy Keyword Ranking (long-tail auction terms) · #10 Parasite SEO · #36/#37 Quora/Reddit research (where buyers ask) · #39 LinkedIn Audience (founder) · #42 Short-Form Video (deal + evaluate reels via HyperFrames) · #129 Review Sites.
**Q2:** #4 Programmatic SEO (scaled) · #3 Glossary (SARFAESI/EMD/EC) · #11 Competitor Comparison · #5 Content Repurposing · #59 HARO (founder as auction/locality source).
**Q3+:** #15 Engineering-as-Marketing (a "is this reserve fair?" or EMI calculator) · #78 Product Hunt (at a real milestone) · #127 YouTube · #131 second-geo.
**Skip:** #83 Twitter Giveaways, #99 Graphic Novel, #112 Reality-TV, #118 Cameo, #122 Humor — off-brand for a grounded, trust-sensitive product. Paid ads (#23–34) held for budget, not skipped.

### 12.2 Activation
**Now:** #90 One-Click Registration · #96 Onboarding Optimization · #48 Dynamic Email Capture (auction alerts). **Q2:** #51 Onboarding Emails · #47 Founder Welcome · #91 In-App Upsells (deeper research).

### 12.3 Retention
**Now:** #46 Reactivation · #50 Inbox Placement. **Q1–Q2:** #52 Win-back · #53 Trial Reactivation. **Q2+:** #135 Support-as-Marketing.

### 12.4 Referral (staged Q2+)
#62 Affiliate · #137 Two-Sided Referrals (Pro days). **Q3+:** #93 Viral Loops (share-a-find), #92 Newsletter Referrals.

### 12.5 Revenue
**Now/Q2:** #91 In-App Upsells (limit-reached → upgrade for more research). **Skip:** #86 Lifetime Deals (damages subscription LTV).

### 12.6 Cross-cutting
**Now:** #139 Customer Language (use verbatim buyer phrasing) · #114 Moneyball Marketing (measure everything once analytics is live).

### Idea-bank summary
~18 Acquisition ideas now/soon (the dominant stage — correct here), ~6 Activation, ~6 Retention, ~4 Referral (staged), ~2 Revenue, 2 cross-cutting; ~8 skipped for voice fit + the paid cluster held for budget. The 90-day plan activates ~15–20% of the surface area — the right slice for a bootstrapped, pre-traction product whose edge is a dataset + a genuinely useful AI feature.

---

## 13. Measurement, RACI, open decisions, appendix

### Measurement
**North star (proposed):** **weekly activated users** — unique users who complete ≥1 grounded search *and* take a value action (a per-property/web-enriched evaluation question, or a save/watchlist). It captures discovery × the hero feature × retention intent in one number. Once CAC/retention exist, add **LTV : CAC** (target > 3).

**Leading indicators:**
| Stage | Indicators |
|---|---|
| Acquisition | Indexed pages, organic sessions, GSC impressions/clicks, rankings, social reach + per-pillar engagement, UTM'd link clicks → signups |
| Activation | Signup rate, first-search rate, **web-enriched-question rate**, activation rate, email captures |
| Retention | Alert open/click, week-2/week-4 return, list growth |
| Referral | Shares per active user, referred signups (Q2+) |
| Revenue | Free→Pro rate, ₹ MRR, Pro retention |

**Cadence:** weekly funnel review + ship the roadmap row; monthly cohort/conversion review; quarterly re-plan.

### RACI
| Domain | Responsible | Accountable | Consulted |
|---|---|---|---|
| Strategic plan | Founder | Founder | fCMO (skills) |
| Brand voice / honesty rule | Founder | Founder | `product-marketing` |
| Product/web implementation | Founder | Founder | `cro`/`seo-audit` |
| SEO / content | Founder (→ contractor Q2) | Founder | `programmatic-seo`/`content-strategy` |
| Lifecycle email | Founder | Founder | `emails` |
| Pricing | Founder | Founder | `pricing` |

### Open decisions (ranked by impact)
1. **Inventory freshness (highest impact).** Newest auction ends 2026-08-08 (~1 month). The scraper/enrichment pipeline must run on a cadence or live inventory (and the entire SEO + alerts engine) goes to zero. Confirm/automate the cadence before scaling SEO.
2. **CAC + funnel instrumentation.** Everything revenue-related is blocked until analytics is live. Week 1.
3. **SSR/prerender approach.** Programmatic SEO depends on a crawlable app; pick the path (Vercel prerender vs SSR) early.
4. **Proof points.** No testimonials/user counts. Start collecting 3–5 verbatim buyer quotes + a "found a good deal / avoided a bad one" story.
5. **Pricing structure.** Single untested ₹499; decide on annual + how to surface the value (deeper research).
6. **Entity/trust identity.** What legal entity / founder identity to surface publicly.
7. **Dossier launch.** If/when to leave the flag — and market it honestly (document locker, not diligence).
8. **Second-geo timing.** When to expand the graph beyond TN.

### Appendix
**Repo:** `.agents/product-marketing.md` (corrected positioning/ICP/voice); `README.md`; `modes/deep-research.md`; `brand/logo/`.
**This plan:** `docs/marketing/plan.md`.
**Execute deeper:** `/seo-audit`, `/programmatic-seo`, `/schema`, `/analytics`, `/copywriting`, `/emails`, `/pricing`, `/onboarding`, `/content-strategy`, `/marketing-loops`.

---

*AuctionScope Marketing Plan v1. Prepared 2026-07-09. Organic-first, discovery-led, honest-by-rule (no diligence over-promise). Numbers verified against the live graph; forecasts are ranges, not guarantees — CAC and retention are unmeasured until analytics ships in week 1.*
