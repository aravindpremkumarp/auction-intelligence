# AuctionScope — Marketing Plan v1

**Prepared by:** CMO (fractional, via Claude Code marketing-skills)
**For:** Aravind (founder) & team
**Date:** 2026-07-09
**Status:** Draft v1 — for founder review

---

## 1. Executive summary

**What this plan optimizes for:** turning a data moat into search demand. AuctionScope has built something rare — a grounded AI search + diligence layer over Indian SARFAESI bank-auction property, with ~3,400 enriched Tamil Nadu listings against a 49,342-strong national universe. The product is genuinely ahead of the category. The go-to-market is a placeholder. For the next 12 months, the entire game is converting the dataset into organic discoverability, then converting the resulting traffic into ₹499 Pro subscribers. This is a bootstrapped, organic-first plan; it does not assume paid budget.

**Three big bets, ranked by leverage:**

**Bet 1 — Programmatic SEO is the growth engine, and the dataset already exists.** The single highest-leverage asset is the graph itself. Every competitor (IBAPI, bankeauctions.com, BAANKNET, eauctionsindia) ranks on thousands of city × bank × property-type listing pages. AuctionScope ranks on ~1 — a 4-URL sitemap behind a client-rendered SPA that Google can barely index. The move: SSR/prerender the app, then generate structured, schema-marked landing pages at scale from the graph ("Bank auction flats in Chennai," "SBI auction properties in Coimbatore under ₹50L"). This is where a small team with a proprietary dataset beats better-funded incumbents, because the content compounds and the data is a moat they don't have.

**Bet 2 — Diligence is the wedge that justifies the price and the story.** "AI search over auctions" is interesting; "get an institutional-grade diligence report on this property before you bid, in the two-week window, for a fraction of a ₹5k–25k advocate" is a *reason to pay*. The deep-research report and the forthcoming Dossier "Diligence Readiness Score" are the emotionally urgent, deadline-driven value — and they're currently buried. Lead with them in positioning, the paywall, and the content.

**Bet 3 — Instrument first, or fly blind forever.** There is no web analytics installed today (no GA4/GTM/PostHog/pixel anywhere in `web/`). Every optimization below is unmeasurable until this is fixed. It is cheap, fast, and unblocks everything — it goes in week one.

**What twelve months looks like, plausibly (organic-first, bootstrapped):**
- From ~invisible to a few hundred → low-thousands of monthly organic sessions, driven by programmatic listing pages + an educational content base.
- A measurable funnel end to end (session → signup → activated search → Pro), so decisions stop being guesses.
- A repeatable Pro-conversion motion tied to the diligence value, not just a chat-count wall.
- An "auction alerts" email list as the compounding retention + re-engagement asset (currently nonexistent).
- Founder-led LinkedIn as a credibility and inbound channel (brand assets already built, dormant today).
- A defensible trust surface (About/entity/data-sources/visible pricing) in a category where trust is the whole purchase.

**90-day priorities:**
1. Install analytics + a tracking plan (GA4 + GTM) and define the funnel. `/analytics`
2. SSR/prerender the SPA and fix the canonical + metadata so the site is crawlable. `/seo-audit`
3. Ship a programmatic-SEO pilot (Chennai, top 3 property types) with `RealEstateListing`/`Product` schema. `/programmatic-seo` + `/schema`
4. Rewrite the above-the-fold to a benefit + proof headline and add "Get auction alerts for your city" email capture. `/copywriting` + `/popups`
5. Publish the first 4 educational posts targeting pre-purchase informational search (SARFAESI/EMD/diligence). `/content-strategy` + `/copywriting`
6. Sharpen the ₹499 Pro story around diligence, and stand up a real pricing + About/trust page. `/pricing` + `/copywriting`

---

## 2. Strategic frame

### What AuctionScope is, in one sentence
AI-powered search and diligence for Indian bank-auction (SARFAESI) property — it turns scattered portal listings and dense sale-notice PDFs into a searchable, grounded, analyzable graph, then puts an AI agent on top so a buyer can find and vet auctions in plain English.

### The category we're claiming
AuctionScope is **redefining an existing category** — the Indian bank-auction property portal — from "listings directory" to "search + diligence copilot." Incumbents compete on *inventory* (who has the most listings). AuctionScope competes on *decision confidence* (find the right auction, then know whether it's safe to bid, inside the deadline). The category-defining frame: portals tell you *what's* for auction; AuctionScope tells you *which one to bid on and why*. This is a wedge from commodity aggregation into a defensible, higher-value job.

### Who we're for (ICP, distilled)
- **Retail SARFAESI auction buyers in Tamil Nadu** (expanding across India as the graph grows) — individuals and small investor syndicates. Not banks, enterprises, or auction houses.
- **Stated problem:** "I can't find good auction properties easily." **Real problem:** "I can't tell a good, safe auction from a title-defect trap fast enough to bid within the two-week window."
- **What they're actually buying:** confidence on a specific property's paperwork — faster and cheaper than a ₹5k–25k property advocate.
- **The status-quo competitor is the workaround** — "spreadsheet-and-WhatsApp-and-gut," plus advocate engagements that are too slow. Not another app.
- **Search behaviour is high-intent and vernacular:** *"flats in Chennai <50L," "commercial Coimbatore," "3BHK in Chennai under 1 Cr, ready possession."*

### The business model logic
Freemium subscription. Browsing listings/filters is free; heavy AI usage and premium diligence are the upsell. **Pro = ₹499 / 30 days** (Razorpay), gating chat volume (25/day free → 1,000/day Pro) and better models. The compounding-channel thesis: the proprietary graph → programmatic SEO → organic traffic at near-zero marginal cost → free activation → Pro conversion on the diligence value. The data is both the product and the acquisition engine — that's the flywheel. Unit economics are currently unproven (see §8) because the funnel isn't instrumented; establishing CAC and retention is the top open decision (§13).

### Brand voice (the non-negotiable)
From the live product and the positioning doc (`.agents/product-marketing.md`):
- **Tone:** understated, plain-spoken, trustworthy. Lowercase, calm, no hype.
- **Grounded, always.** The product never invents prices/counts/IDs — the marketing must mirror that. No overpromising, especially no implied legal certainty. "Information platform only — always verify with the bank" is a *trust asset*, not just legal cover.
- **Vocabulary — use:** bank auction, e-auction, SARFAESI, reserve price, EMD, encumbrance, EC, patta, re-auction, due diligence, ₹ lakhs/crores, real city/bank names.
- **Vocabulary — avoid:** "distressed assets" (jargon to a retail buyer), "revolutionary"/hype, anything implying guaranteed legal outcomes.
- **Note:** a fintech reskin is designed in `redesign/` but not wired in; confirm the target voice as the "sketchbook" aesthetic is retired.

---

## 3. Current state

*Scored from materials (codebase, live-site audit, design docs) on 2026-07-09. No prior analytics exists, so data-driven sections are scored from artifacts; team should push back where they have better data.*

### Team composition (marketing surface area)
| Person | Role | Marketing surface area |
|---|---|---|
| Aravind | Founder / engineer | Everything — product, data pipeline, and (currently) all marketing |

Honest gap: there is no marketing owner. At this stage that's correct — the first "hire" is the marketing-skills stack + fCMO operating cadence, not a person. A part-time content/SEO contractor becomes worth it once the programmatic system is proven and needs volume (post-Q2). First actual hire, when funding unlocks it, should be a π-shaped Product-Marketing + Growth/Content marketer titled Manager or Lead — not a VP/CMO.

### Marketing budget (current)
- Paid acquisition: ₹0/mo (bootstrapped, by choice)
- Tooling: hosting (Vercel static + Render + Neo4j Aura + Supabase + R2 + OpenRouter) — already paid as product infra; marketing-specific tools ≈ ₹0 today
- Retainers / fCMO: ₹0 (this plan is agent-run)
- Blended CAC: **unknown** — no analytics, no paid history (top open decision, §13)
- Spend as % of ARR: not meaningful yet
- **Funding-stage tier:** Pre-seed / bootstrapped (~₹0–2K USD-equiv/mo ceiling; organic only). Implication: the 90-day plan must produce results **without any lever that needs future budget.** Every move below is founder-time + free-tier tools.

### Phase of SaaS growth
**$0–10K ARR — the "grueling early" phase.** Revenue just switched on (pricing/billing shipped 2026-06). Binding constraint is not budget; it's **distribution + proof.** Dominant near-term growth pattern will be linear (organic pages indexed per week → sessions per week), with the potential for a step-function if a programmatic cluster breaks through or a launch lands.

### What's already done (acknowledge, then build on)
| Asset | Status | Marketing leverage |
|---|---|---|
| Enriched TN auction graph (~3,400 props) | Live | The moat — fuel for programmatic SEO + proprietary-data content |
| Grounded AI agent (no hallucination) | Live | The differentiator + a trust story |
| Deep-research diligence report | Live | The paid wedge; advocate-replacement narrative |
| Document Dossier + Readiness Score | Shipped "dark" (feature-flagged) | A launch moment + higher-ACV expansion |
| ₹499 Razorpay Pro | Live | Monetization already on |
| Brand kit + LinkedIn avatar/banner | Built, unused | Founder-led channel ready to switch on |
| On-page SEO basics + OG image | Live but thin | Foundation to build the SEO surface on |
| Fintech redesign prototype | In `redesign/`, not wired | Conversion + trust upgrade waiting |

### What's in-flight (drafted but not shipped)
| Item | Status | Blocker |
|---|---|---|
| Document Dossier | Behind feature flag | Go/no-go on public launch (decision) |
| Redesign (fintech look) | Prototype only | Not wired into `web/` (time) |
| `.agents/product-marketing.md` | **Shipped today (V1)** | Needs founder correction of proof-point gaps |

### What's stuck (and needs to unstick this quarter)
| Issue | Cost of inaction | Action |
|---|---|---|
| Zero web analytics | Every decision is a guess; can't prove CAC or conversion | Install GA4+GTM (week 1) |
| SPA not crawlable; 4-URL sitemap | Invisible in the highest-intent channel for this category | SSR/prerender + programmatic pages |
| Hidden pricing despite live payments | Prospects can't self-qualify; monetization looks half-built | Pricing page + visible plan |
| No lead capture | Forfeiting the killer retention hook (auction alerts) | Email capture in week 1–2 |
| Anonymous operator | Trust deficit in a scam-sensitive vertical | About/entity/data-source page |

### Audit rubric snapshot (17-section, scored from materials)
| # | Section | Score | Note |
|---|---|---|---|
| 1 | Positioning | 2 | Founder understands it deeply; no surface expresses it. Now captured in product-marketing.md |
| 2 | Customer research | 2 | Strong lived domain depth (9-category diligence taxonomy) but no formal VOC practice or named users |
| 3 | Homepage | 2 | Works as an app; no positioning headline, thin conversion architecture |
| 4 | Sales / product pages | 1 | No pricing/feature pages; paywall exists only in-app |
| 5 | Conversion pages | 0 | None |
| 6 | Competitor comparison | 0 | None |
| 7 | Resources / content | 0 | No blog; 4-URL sitemap |
| 8 | Onboarding | 1 | Signup + first search exist; nothing designed/measured |
| 9 | Email lifecycle | 1 | Transactional auth email only; no lifecycle, no capture |
| 10 | Sales material | 0 | N/A for self-serve B2C (not a weakness) |
| 11 | Messaging | 2 | Distinctive understated voice exists; not operationalized across surfaces |
| 12 | Pricing | 2 | ₹499 live but never pressure-tested; no page; single tier |
| 13 | CRO | 0 | No tests, no instrumentation |
| 14 | GTM launches | 1 | No structured launch; Dossier shipped dark |
| 15 | Ads (paid) | 0 | None — reflects funding stage, not a failure |
| 16 | SEO | 1 | On-page basics only; SPA uncrawlable; ranks for ~nothing |
| 17 | Internationalization | 1 | India/INR, TN-only; appropriate, with national runway |

**Total: 16 / 85 (19%).**

**Shape interpretation:** This is the "**strong product, weak everything-else**" shape. The relative highs cluster on the founder-and-product axis (positioning latent, customer/domain depth, voice, a live price). The zeros cluster entirely on **distribution and conversion** (content, SEO surface, conversion/comparison pages, CRO, launches). That shape is the gap the plan closes: the first quarter is bedrock (instrument + crawlable + first pages), and Acquisition (§4) is the longest section because that's where the gap is widest.

---

## 4. Acquisition

### Current state
Effectively no acquisition engine. The app is the homepage; there are no content, listing, or comparison pages; the SPA is barely crawlable; and there's no analytics to see what little traffic exists. In a category where buyers actively *search* ("bank auction flats chennai"), being invisible in organic is the single biggest miss. Paid is off the table by choice (bootstrapped). So Acquisition = organic, and organic = exploit the dataset.

### The plan

**Move 1 — Make the app crawlable (prerequisite).** SSR or prerender `web/` so Google receives rendered HTML, not an empty shell; consolidate to one canonical host with a clean 301 (content serves non-www, sitemap references www today); ship per-route `<title>`/meta/OG. Nothing else in SEO matters until this is done. `/seo-audit`

**Move 2 — Programmatic SEO (the growth engine).** Generate high-intent landing pages at scale from the graph: city × property-type ("bank auction flats in Chennai"), city × bank ("SBI bank auction properties in Coimbatore"), price-band ("bank auction properties under ₹50 lakh in Tamil Nadu"), and per-property detail pages. Each is a real, indexable, internally-linked page backed by live data. This is idea #4 (Programmatic SEO) + #6 (Proprietary Data Content) — and it's the move incumbents can't easily copy because they lack the enriched graph. `/programmatic-seo` + `/site-architecture`

**Move 3 — Structured data everywhere.** Emit `RealEstateListing` / `Product` / `Offer` / `BreadcrumbList` JSON-LD on every listing and landing page → rich results in Google and eligibility for AI-answer citations. `/schema`

**Move 4 — Educational content for pre-purchase search.** The buyer journey starts informational before it's transactional. Publish the questions people Google *before* browsing: "How do SARFAESI bank auctions work," "What is EMD and how is it refunded," "Bank-auction property due-diligence checklist," "How to read a sale notice / encumbrance certificate." This feeds organic + becomes the raw material for AI-SEO and social. `/content-strategy` + `/copywriting`

**Move 5 — AI-SEO / answer-engine optimization.** Publish `llms.txt` and structure content as clean, citable answers so ChatGPT/Perplexity/Google AI Overviews cite AuctionScope for "bank auction property India" questions. On-brand for an AI-native product and a cheap early-mover edge. `/ai-seo`

**Move 6 — Competitor comparison / alternative pages.** Consideration-stage SEO: "AuctionScope vs bankeauctions.com," "IBAPI alternative," honest comparisons that win high-intent SERPs. Built from competitor profiles. `/competitors` + `/competitor-profiling`

**Move 7 — Founder-led LinkedIn.** Assets already exist. Cadence of 2–3 posts/week: deal teardowns ("this Chennai flat re-auctioned 3× — here's why"), SARFAESI explainers, build-in-public. Builds credibility + inbound in a niche where the founder's domain depth is a real edge. `/social` (idea #39)

**Move 8 — Directories + review sites + free discovery.** Submit to Indian SaaS/startup/AI directories and relevant review sites for dofollow backlinks to raise domain rating (so the programmatic pages actually rank), plus a considered Product Hunt / launch moment tied to the Dossier GA. `/directory-submissions` + `/launch` (ideas #78, #129)

**Move 9 — Paid layer — explicitly held.** Google Search on high-intent auction keywords is the obvious first paid test *when there's budget and instrumented conversion to measure it against.* Held until post-seed or until organic proves the funnel converts. Not in this 12-month bootstrapped plan except as a small optional test if budget appears. `/ads` + `/ad-creative`

### 90-day acquisition moves
- **Weeks 1–2:** analytics live; SEO audit; canonical/301 fix; SSR/prerender scoped.
- **Weeks 3–4:** SSR/prerender shipped; Chennai programmatic pilot (top 3 property types) live with schema; first 2 educational posts.
- **Weeks 5–8:** expand programmatic to top 5 TN cities × property-type; 4 more posts; `llms.txt`; first 5 directory submissions; LinkedIn cadence begins.
- **Weeks 9–12:** city × bank pages; first 2 comparison pages; measure indexed-page count + first organic sessions; 90-day review.

### 12-month acquisition outlook
- **Q1:** crawlable + first programmatic cluster + content base. Indexation begins.
- **Q2:** programmatic coverage across TN; content cadence; comparison library; LinkedIn compounding. First meaningful organic sessions.
- **Q3:** AI-SEO citations; expand graph/pages to a second state; launch moment (Dossier GA / Product Hunt).
- **Q4:** organic is the primary channel; optional small paid test if budget unlocked.

### Skills + tools
- **Skills:** `seo-audit`, `programmatic-seo`, `schema`, `site-architecture`, `ai-seo`, `content-strategy`, `copywriting`, `competitors`, `competitor-profiling`, `social`, `directory-submissions`, `launch`, (held: `ads`, `ad-creative`).
- **MCPs / tools:** GA4 (once installed), Google Search Console, the Neo4j graph as the page-data source, Vercel for SSR/prerender, an SEO tool (Ahrefs/Search Console) for keyword + rank tracking.

---

## 5. Activation

### Current state
Signup (Supabase) and a first grounded search exist, but nothing is designed or measured. There's no onboarding, no "first value" path, no analytics on where new users drop. The aha — a messy question → a grounded shortlist → one diligence report — happens by accident, not by design.

### The plan
**Move 1 — Instrument the activation funnel.** Define and track: landing → search/browse → signup → first grounded result → save/watchlist → deep-research view. You cannot improve activation you can't see. `/analytics`

**Move 2 — Rewrite the above-the-fold.** Retire "What property are you looking for?" as the *only* framing; add a benefit headline + proof stat ("Search 3,400+ live Tamil Nadu bank auctions — and know which are safe to bid on"). Keep the search box as the primary action. `/copywriting` + `/cro`

**Move 3 — Design first-run to aha.** Seed example queries by intent, guide a first search to a real result, and surface the deep-research report as the "wow" within the first session. `/onboarding` (ideas #96, #90)

**Move 4 — Email capture as activation, not just retention.** "Get auction alerts for your city" — a one-field capture that both activates (immediate value: alerts) and builds the retention list. This is the missing killer hook. `/popups` (idea #48)

**Move 5 — Reduce signup friction.** One-click/social signup; don't gate browsing; ask for the account at the value moment (saving a property / running deep research). `/signup` (idea #90)

### 90-day activation moves
- Weeks 1–2: funnel instrumented; email capture shipped.
- Weeks 3–4: above-the-fold rewrite live; example-query first-run.
- Weeks 5–8: signup friction pass; first activation-rate baseline read.
- Weeks 9–12: first onboarding iteration based on real drop-off data.

### 12-month activation outlook
Q1 baseline + first fixes → Q2 onboarding tested against data → Q3 activation tuned to top-quartile → Q4 activation stable as traffic scales.

### Skills + tools
`analytics`, `onboarding`, `signup`, `cro`, `copywriting`, `popups`, `ab-testing` (once traffic supports it). Tools: GA4, the email tool (Resend/Customer.io/Mailchimp), Supabase auth.

---

## 6. Retention

### Current state
Only transactional auth email exists. No lifecycle, no alerts, no re-engagement — despite auctions being *natively* deadline-driven, which makes retention email almost free to justify. Watchlist + deadline-alert scaffolding exists in the product but isn't wired to outbound.

### The plan
**Move 1 — Auction alerts (the retention engine).** City/watchlist-based new-auction digests + hard-deadline reminders + re-auction price-drop alerts. This is the single most natural retention loop in the category — the product's own data generates authentic, timely, wanted email. `/emails` (ideas #46, #52)

**Move 2 — Welcome + activation sequence.** A short founder-voiced welcome that drives the first grounded search and explains the diligence value. `/emails` (idea #51)

**Move 3 — Win-back.** Lapsed-searcher re-engagement keyed to new inventory in their saved cities. `/emails` (idea #52)

**Move 4 — Inbox placement / deliverability.** SPF/DKIM/DMARC + warmup so alerts actually land — table stakes before volume. `/emails` (idea #50)

**Move 5 — Support as marketing.** In a trust-sensitive vertical, fast, human, knowledgeable support is a retention *and* referral asset. `/churn-prevention` (idea #135)

### 90-day retention moves
- Weeks 1–2: deliverability setup; capture live (from §5).
- Weeks 3–4: welcome sequence.
- Weeks 5–8: city auction-alert digest (the core loop).
- Weeks 9–12: deadline + price-drop alerts; first win-back.

### 12-month outlook
Q1 core alerts → Q2 segmented digests + win-back → Q3 alerts tied to Pro value (richer alerts for Pro) → Q4 lifecycle stable and compounding.

### Skills + tools
`emails`, `churn-prevention`, `copywriting`, `sms` (optional, India-appropriate for deadline/price-drop). Tools: an ESP with automation (Resend + a flow layer, or Customer.io/Mailchimp), Supabase user data, the graph for alert triggers.

---

## 7. Referral

### Current state
A per-property "copy link" share button exists; no referral, affiliate, or word-of-mouth program. The audience (investors/syndicates) is inherently networked — they talk to each other — so referral has real latent potential once there's a base to refer.

### The plan
**Move 1 — Share-after-value.** Make sharing a great find (a property, a diligence summary) frictionless and branded — the natural viral moment for a deal-hunting audience. (idea #93, lightweight now)
**Move 2 — Founder as referrer-zero.** LinkedIn deal teardowns *are* referral fuel — every good public analysis is an ad for the diligence product. `/social` + `/referrals`
**Move 3 — Referral program — staged for Q2+.** Once there's a retained base and Pro conversions, a simple two-sided referral (both sides get Pro days) fits the model. `/referrals` (ideas #62, #137)

Referral is deliberately *light* in the first 90 days — you can't refer from an empty base. It's staged to switch on once Activation + Retention produce retained users (Q2).

### Skills + tools
`referrals`, `social`, `emails` (referral lifecycle), `copywriting`. Tools: Razorpay (to grant Pro-day incentives), Supabase.

---

## 8. Revenue

### Current state
Monetization is live (₹499/30 days via Razorpay, metered on chat) but the *story* is weak: pricing is hidden (no page), the gate is a soft chat-count wall rather than the emotionally urgent diligence value, and there's no analytics on conversion. The single tier and hidden price mean prospects can't self-qualify.

### The plan
**Move 1 — Make pricing visible.** A real pricing page — scannable, honest, with the free/Pro comparison and the diligence value front-and-center. `/pricing` + `/copywriting`
**Move 2 — Reframe the paywall around diligence.** Lead the upgrade moment with the deep-research report and the Dossier Readiness Score ("get the full diligence report on this property"), not "you've used your 25 chats." Trigger at the value moment (deep research / dossier), not an arbitrary count. `/paywalls` (idea #91)
**Move 3 — Pressure-test the price.** ₹499 has never been tested. Once there's traffic, test price points / annual option / a higher "diligence" tier for the Dossier. Van Westendorp / simple A/B once volume allows. `/pricing`
**Move 4 — A second tier for Dossier (Q3+).** The Dossier "Readiness Score" and future "verdicts" are higher-ACV; stage a premium diligence tier as that feature goes GA. `/pricing` + `/paywalls`

### Unit economics
| Metric | Value | Note |
|---|---|---|
| ARPC (monthly) | ~₹499 (for active payers) | 30-day passes; blended ARPC unknown without payer data |
| Blended CAC | **Unknown** | No analytics/paid history — **top open decision (§13)** |
| Annual retention | **Unknown** | No cohort data yet |
| LTV (rough) | **Unknown** | Blocked on retention |
| LTV / CAC | **Unknown** | Blocked on both above |

Every revenue projection in §10 is a range, not a promise, because CAC and retention are unmeasured. Instrumenting the funnel (week 1) is what turns these from unknowns into a model.

### Skills + tools
`pricing`, `paywalls`, `ab-testing`. Tools: Razorpay (billing source of truth), GA4 (conversion), the billing tables for cohort/retention analysis.

---

## 9. 90-day roadmap

### Weeks 1–2 — Unblock
| Move | Stage | Owner |
|---|---|---|
| Install GA4 + GTM; define tracking plan | Cross | Aravind |
| SEO audit; fix canonical + 301 | Acq | Aravind |
| "Get auction alerts" email capture | Act/Ret | Aravind |
| Email deliverability (SPF/DKIM/DMARC) | Ret | Aravind |
| Founder correction pass on product-marketing.md | Cross | Aravind |

### Weeks 3–4 — Foundation
| Move | Stage | Owner |
|---|---|---|
| SSR/prerender `web/` shipped | Acq | Aravind |
| Chennai programmatic pilot (3 property types) + schema | Acq | Aravind |
| Above-the-fold rewrite (benefit + proof) | Act | Aravind |
| Pricing page + About/trust page | Rev/Cross | Aravind |
| First 2 educational posts | Acq | Aravind |
| Welcome email sequence | Ret | Aravind |

### Weeks 5–8 — Velocity
| Move | Stage | Owner |
|---|---|---|
| Programmatic across top 5 TN cities × type | Acq | Aravind |
| 4 more educational posts; `llms.txt` | Acq | Aravind |
| City auction-alert digest live | Ret | Aravind |
| Paywall reframed around diligence | Rev | Aravind |
| First 5 directory submissions | Acq | Aravind |
| LinkedIn cadence begins (2–3/wk) | Acq | Aravind |

### Weeks 9–12 — Compound
| Move | Stage | Owner |
|---|---|---|
| City × bank pages; first 2 comparison pages | Acq | Aravind |
| Deadline + price-drop alerts | Ret | Aravind |
| First activation-rate + conversion baseline read | Cross | Aravind |
| Deep-research/Dossier launch prep | Acq/Rev | Aravind |
| 90-day review + recalibrate | Cross | Aravind |

---

## 10. 12-month outlook

**Framing.** Budget method: **capacity-based, not spend-based** — the binding constraint is founder-hours, not dollars (bootstrapped, ~₹0 marketing spend beyond free-tier tools). No revenue-% budget math applies until paid enters. Growth pattern: **linear**, driven by indexed programmatic pages/week → organic sessions/week, with a possible **step-function** if a programmatic cluster breaks through or the Dossier launch lands. Forecasts are honest ranges, not hockey sticks.

#### Q1 — Months 1–3
**Funding state:** Bootstrapped. **Focus:** Instrument, become crawlable, ship the first programmatic cluster + content base.
**Outcomes:** analytics live; SPA crawlable; Chennai programmatic pilot indexed; 6 posts; email capture + core alerts; pricing/About pages; product-marketing.md finalized.
**KPI targets:** funnel measurable end-to-end; 50–150 programmatic pages indexed; first email list (target 100+ captures); LinkedIn cadence established.
**S-curve position:** starting the Channel S-curve (organic) from zero; Product curve already strong.

#### Q2 — Months 4–6
**Funding state:** Bootstrapped (optional first small paid test only if a round/appetite appears). **Focus:** Scale programmatic across TN + content cadence + retention loop compounding.
**Outcomes:** TN-wide programmatic coverage; comparison library; win-back live; first Pro-conversion baseline against the diligence paywall; possible first content-driven organic wins.
**KPI targets:** low-thousands of programmatic pages; first few hundred monthly organic sessions; measurable free→Pro conversion rate; email list into the low thousands.
**S-curve position:** Channel (organic) climbing; begin staging Market curve (second state's data).

#### Q3 — Months 7–9
**Funding state:** Bootstrapped or post-seed. **Focus:** AI-SEO citations, second-geo expansion, launch moment.
**Outcomes:** AI-answer citations appearing; second state's auctions in the graph + pages; Dossier GA / Product Hunt launch; premium diligence tier tested.
**KPI targets:** organic as the #1 traffic source; step-function from launch; second-geo pages indexing; LTV:CAC estimable for the first time.
**S-curve position:** Market S-curve (geo expansion) begins while Channel curve still climbs.

#### Q4 — Months 10–12
**Funding state:** Bootstrapped or seed-deployed. **Focus:** Compound + decide on paid.
**Outcomes:** organic engine self-sustaining; retention loop mature; a defensible content + programmatic moat; a data-backed decision on whether/where paid earns its place.
**KPI targets:** organic sessions in the low-to-mid thousands/mo; Pro subscriber base with a known retention curve; a real LTV:CAC to gate any paid spend.
**S-curve position:** layered — Channel (organic) mature, Market (multi-geo) climbing, Product (Dossier tier) as the next curve.

---

## 11. Marketing operations stack

### The thesis
A single founder + the 47-skill marketing library + a few MCP/API connections can output the work of a multi-person marketing org. Strategy stays with the founder (and this fCMO cadence); execution is delegated to skills and, later, a contractor for volume. The plan doesn't just say *what* to do — it names *what executes each move.*

### Skills mapped to AARRR stages
| Stage | Primary skills | Supporting skills |
|---|---|---|
| Acquisition | `programmatic-seo`, `seo-audit`, `schema`, `content-strategy`, `copywriting` | `ai-seo`, `site-architecture`, `competitors`, `competitor-profiling`, `social`, `directory-submissions`, `launch` |
| Activation | `onboarding`, `cro`, `copywriting` | `signup`, `popups`, `analytics`, `ab-testing` |
| Retention | `emails`, `churn-prevention` | `sms`, `copywriting` |
| Referral | `referrals` | `social`, `emails`, `copywriting` |
| Revenue | `pricing`, `paywalls` | `ab-testing` |
| Cross-cutting | `product-marketing`, `analytics`, `marketing-plan` | `customer-research`, `marketing-ideas`, `marketing-council`, `marketing-loops`, `marketing-psychology` |

### MCPs / APIs mapped to stages
| Stage | Existing connections | fCMO tooling layer to add |
|---|---|---|
| Acquisition | Neo4j graph (page data), Vercel | Google Search Console, Ahrefs/rank tracker, GA4 |
| Activation | Supabase (auth) | GA4 events, an A/B tool |
| Retention | Supabase (users), the graph (triggers) | ESP (Resend/Customer.io/Mailchimp) |
| Revenue | Razorpay (billing source of truth) | GA4 conversion; billing→sheet for cohorts |
| Cross-cutting | — | GA4/GTM |

### A concrete example
`/product-marketing` auto-drafted the full positioning + ICP + voice + competitor context in one pass by reading the codebase — no interview, no agency brief. Every subsequent skill (`/copywriting`, `/programmatic-seo`, `/emails`) now inherits that context automatically. That's the stack working: one foundational artifact, generated in minutes, that would normally be a multi-week consultant engagement.

### Capability unlocks by funding stage
| Stage | Headcount | Tooling | Channels live |
|---|---|---|---|
| Bootstrapped (now) | Founder + skills | Free-tier GA4/GSC/ESP | Organic SEO, content, founder LinkedIn, email |
| Seed close | + part-time content/SEO contractor | Paid Ahrefs, paid ESP | + small Google Search test, + directories at volume |
| Seed deployment | + π-shaped marketing Manager/Lead | Paid A/B, richer analytics | + paid scaling, + comparison/PR |
| Series A | + performance + content ICs / niche agency | Full stack | + multi-channel paid, + multi-geo GTM |

### Team and agency model (RACI-lite)
| Function | Owned by (strategy) | Executed by |
|---|---|---|
| Growth (demand engine) | Founder + fCMO cadence | Skills now; SEO/content contractor Q2+ |
| Product marketing (story) | Founder | `product-marketing` / `copywriting` skills |
| Content (trust engine) | Founder | `content-strategy` / `copywriting` now; contractor for volume Q2+ |

First real hire (when funding unlocks): π-shaped Product-Marketing + Growth/Content marketer, Manager/Lead title — not VP/CMO.

---

## 12. Tactical idea bank

Sections 4–8 prescribe what's *being done now*. This section maps what's *possible* — the 139-idea `marketing-ideas` library filtered for AuctionScope's category (Indian bank-auction property, B2C self-serve), brand voice (grounded, no-hype), and stage (bootstrapped, $0–10K ARR). Run `/marketing-ideas` for the full library with how-to-start detail.

**Status legend:** Now (Q1, in the 90-day plan) · Q2 (post-foundation) · Q3+ (post-seed/GA/second-geo) · Q4+ (long-game) · Skip (off-brand/model).

### 12.1 Acquisition ideas
**Now (Q1):**
| # | Idea | Client note |
|---|---|---|
| 2 | SEO Audit | Fix crawlability, canonical, metadata — the prerequisite |
| 6 | Proprietary Data Content | The graph is the moat — "cheapest bank auctions in Chennai this week," data roundups |
| 7 | Internal Linking | Hub/spoke across city × bank × type pages |
| 1 | Easy Keyword Ranking | Low-competition long-tail auction terms |
| 10 | Parasite SEO | Publish on high-DR platforms while your own DR builds |
| 36/37 | Quora / Reddit keyword research | Where SARFAESI buyers ask questions |
| 39 | LinkedIn Audience | Founder-led; assets already built |
| 129 | Review Sites | List on Indian SaaS/AI review sites |
| 74 | Press Coverage | If the Dossier launch is newsworthy |

**Q2:**
| # | Idea | Client note |
|---|---|---|
| 4 | Programmatic SEO | The engine — scaled across all TN once SSR ships (starts in Q1 as pilot, scales Q2) |
| 3 | Glossary Marketing | SARFAESI/EMD/EC/patta glossary — pure informational SEO |
| 11 | Competitor Comparison Pages | vs bankeauctions.com, IBAPI alternative |
| 5 | Content Repurposing | LinkedIn teardowns → posts → guides |
| 8 | Content Refreshing | Keep listing/landing pages current as inventory changes |
| 38/44 | Reddit / Comment Marketing | Answer real buyer questions with genuine help |
| 59 | Article Quotes (HARO) | Founder as a SARFAESI expert source |

**Q3+:** #35 Community, #15 Engineering as Marketing (an EMI/reserve-price calculator, an "is this auction worth it" grader — idea #18), #78 Product Hunt (at Dossier GA), #101 Industry Interviews, #127 YouTube, #131 International/second-geo.

**Skip:** #83 Twitter Giveaways, #99 Graphic Novel, #112 Reality TV, #118 Cameo, #122 Humor — off-brand for a grounded, trust-critical financial-decision product. #23–34 paid ads — held (bootstrapped), not skipped.

### 12.2 Activation ideas
**Now:** #90 One-Click Registration, #96 Onboarding Optimization, #48 Dynamic Email Capture (the auction-alerts hook).
**Q2:** #51 Onboarding Emails, #47 Founder Welcome Email, #91 In-App Upsells (diligence).
**Q3+:** #95 Concierge Setup (for high-value syndicate users). #124 ASO — N/A until a mobile app exists.

### 12.3 Retention ideas
**Now:** #46 Reactivation Emails, #50 Inbox Placement.
**Q1–Q2:** #52 Win-back Emails, #53 Trial Reactivation (once paywall fires).
**Q2+:** #94 Offboarding Flows, #135 Support as Marketing.

### 12.4 Referral ideas
**Q2 (staged — needs a retained base first):** #62 Affiliate Program, #137 Two-Sided Referrals (both sides get Pro days).
**Q3+:** #93 Viral Loops (share-a-find), #92 Newsletter Referrals (once the alerts list is sizeable).

### 12.5 Revenue ideas
**Now/Q2:** #91 In-App Upsells (diligence-triggered paywall).
**Q4+:** #132 Price Localization (only relevant on multi-country expansion — far off).
**Skip:** #86 Lifetime Deals — damages subscription LTV math; off-brand for a recurring-value product.

### 12.6 Cross-cutting / brand foundation
**Now:** #139 Customer Language (captured in product-marketing.md — use verbatim buyer phrasing in all copy), #114 Moneyball Marketing (measure everything once analytics is live).

### Idea-bank summary
- **Acquisition:** ~18 ideas applicable now/soon (the dominant stage — correct for this phase)
- **Activation:** ~6 · **Retention:** ~6 · **Referral:** ~4 (staged Q2+) · **Revenue:** ~2 · **Cross-cutting:** 2
- **Skipped:** ~8 for brand/voice fit (giveaways, humor, reality-TV, cameo, lifetime deals) + the paid-ads cluster held (not skipped) for budget
- **What this proves:** the 90-day plan intentionally activates ~15–20% of the tactical surface area — the right slice for a bootstrapped $0–10K-ARR product whose one true edge is a dataset. As capacity unlocks (Q2 contractor, seed close), this bank is the inventory to scale activity without losing coherence.

---

## 13. Measurement, RACI, open decisions, appendix

### Measurement — the metrics that matter
**North star (proposed):** **weekly activated searchers** — unique users who complete ≥1 grounded search *and* take a value action (save/watchlist or open a deep-research report). It captures the whole thesis in one number: discovery (they found us) × activation (they got grounded value) × Pro-intent (the value action is the paywall's leading indicator). Once CAC and retention are known, add a revenue north star of **LTV : CAC** (target > 3).

**Leading indicators by AARRR stage:**
| Stage | Leading indicators |
|---|---|
| Acquisition | Indexed pages, organic sessions, GSC impressions/clicks, keyword rankings, LinkedIn reach |
| Activation | Signup rate, first-search rate, activation rate (search + value action), email captures |
| Retention | Alert open/click, week-2/week-4 return rate, list growth |
| Referral | Shares per active user, referred signups (Q2+) |
| Revenue | Free→Pro conversion, ₹ MRR, Pro retention, ARPC |

**Review cadence:**
- Weekly: founder reviews the funnel dashboard + ships the week's roadmap row.
- Monthly: cohort/retention + conversion review; recalibrate content/programmatic priorities.
- Quarterly: re-run against this plan; update `/marketing-plan`; decide on paid + hiring.

### RACI
| Domain | Responsible | Accountable | Consulted | Informed |
|---|---|---|---|---|
| Strategic plan | Founder | Founder | fCMO (skills) | Team |
| Brand voice | Founder | Founder | `product-marketing` | — |
| Product/web implementation | Founder | Founder | `cro`/`seo-audit` | — |
| SEO / content | Founder (→ contractor Q2) | Founder | `programmatic-seo`/`content-strategy` | — |
| Lifecycle email | Founder | Founder | `emails` | — |
| Pricing | Founder | Founder | `pricing` | — |
| Founder social | Founder | Founder | `social` | — |

### Open decisions blocking the plan (ranked by impact)
1. **CAC + funnel instrumentation (highest impact).** Everything revenue-related is blocked until analytics is live. Fix in week 1.
2. **SSR/prerender approach.** The programmatic SEO engine depends on a crawlable app; decide the technical path (Vercel prerender vs SSR rewrite) early — it gates Bet 1.
3. **Dossier public launch (go/no-go + timing).** It's the strongest launch moment and higher-ACV tier; decide when it leaves the feature flag.
4. **Proof points.** No testimonials/user counts captured. Start collecting 3–5 verbatim buyer quotes and a "deal found/avoided" story now — the trust surface needs them.
5. **Pricing structure.** Single ₹499 tier untested; decide appetite for a premium diligence tier + annual option once traffic exists.
6. **Entity/trust identity.** Decide what legal entity / founder identity to surface publicly (trust-critical in this vertical).
7. **Second-geo timing.** When to expand the graph beyond TN — the Market S-curve — vs deepening TN.

### Appendix — deep-dive links
**In the repo:** `.agents/product-marketing.md` (positioning/ICP/voice foundation); `README.md`, `docs/design/*` (product context); `redesign/` (fintech reskin prototype); `brand/logo/` (identity + LinkedIn assets).
**This plan:** `~/marketing-plans/auctionscope/final_plan.md` (canonical) + a repo copy proposed at `docs/marketing/plan.md` for team sharing.
**Execute deeper per stage:** `/seo-audit`, `/programmatic-seo`, `/schema`, `/analytics`, `/copywriting`, `/emails`, `/pricing`, `/onboarding`, `/content-strategy`, `/marketing-loops` (to schedule the recurring parts).

---

*AuctionScope Marketing Plan v1. Prepared 2026-07-09. Organic-first, discovery-led. For founder review and discussion. Numbers are honest ranges, not guarantees — CAC and retention are unmeasured until analytics ships in week 1.*
