# Product Marketing Context

*Last updated: 2026-07-09*

> **Status: V1, corrected against the live graph (Neo4j) + a full code audit.** Numbers reflect the actual data on 2026-07-09. Sections flagged **[VERIFY]** need your real numbers (proof points, current traffic). **Honesty rule for all copy: we do NOT do legal/title "due diligence" — the only per-property document we hold is the sale notice. Never claim diligence, advocate-replacement, or legal certainty.**

## Product Overview
**One-liner:** The AI research assistant that helps you find and evaluate Indian bank-auction (SARFAESI) property in plain English.
**What it does:** Turns scattered bank-auction listings and dense sale-notice PDFs into a searchable, structured graph, then puts an AI agent on top. You search in natural language, and — this is the differentiator — you can ask the questions the sale notice can't answer (water, flooding, nearby projects, transport, amenities, approximate location & travel time, market price vs. reserve) and the assistant researches the web and answers, cited. Every graph answer is grounded (never invents prices, counts, or IDs).
**Product category:** Indian bank-auction / SARFAESI property portal — how buyers search: "bank auction properties," "e-auction property Tamil Nadu," "Canara Bank auction Chennai." Sub-category: AI search + evaluation assistant.
**Product type:** SaaS (web app over a proprietary data graph). Data asset: **2,179 enriched Tamil Nadu auctions (~600 live at any time)** across 49 cities / all 38 TN districts.
**Business model:** Freemium subscription. Browsing listings/filters and basic chat are free; heavier AI usage is the upsell. **Pro = ₹499 / 30 days** (Razorpay). Tiers: anonymous 10 chats/day, free (signed-in) 20/day, **Pro 1,000/day** + the Pro model + higher reasoning effort. Deep-research mode is free but login-gated.

## Target Audience
**Target customers:** Retail bank-auction buyers in Tamil Nadu (expanding). Individuals and small investor syndicates — **not** enterprises, banks, or auction houses.
**Decision-maker:** The buyer/investor themselves (self-serve, single decision-maker).
**Primary use case:** Find auction properties matching real criteria, then quickly evaluate a specific one (location, water/flood risk, connectivity, amenities, market price vs. reserve) before deciding to bid.
**Jobs to be done:**
- "Find me real auctions matching what I want" (e.g., *plots in Kanchipuram under ₹50L*, *residential in Chennai*) without trawling clunky government portals.
- "Help me evaluate this property" — ask about groundwater, waterlogging/flood history, nearby govt/private projects, metro/bus/schools/hospitals, approximate location and travel time from my area, and whether the reserve looks fair vs. market. (Researched from the web, cited, approximate — not legal advice.)
- "Keep an eye on it" — save to a watchlist and get deadline alerts before the auction closes.
**Use cases:** natural-language search; browse + filter by city/bank/type/price/date; per-property chat ("ask about this property"); web-enriched Q&A on a listing; deep-research report on one property; re-auction price-drop hunting; watchlist + deadline tracking.

## Personas
Primarily a single B2C persona (self-serve buyer). Two behavioral variants:
| Persona | Cares about | Challenge | Value we promise |
|---------|-------------|-----------|------------------|
| **The active bidder** (individual investor / small syndicate) | Winning a specific property, not overpaying, understanding the location before bidding | Scattered listings; the notice tells you almost nothing about the *place*; hard deadline | Fast, plain-English evaluation of a property with web-researched context |
| **The deal hunter** (browses for opportunities) | Finding underpriced / re-auctioned deals across the state | Listings are un-searchable; can't filter by real criteria; PDFs are dense | Searchable, structured inventory + market-vs-reserve context + price history |

## Problems & Pain Points
**Core problem:** Bank-auction listings are scattered across clunky portals and locked inside dense sale-notice PDFs — so buyers can't search by real criteria, and the notice alone tells you almost nothing about the *property's location and context* (water, flooding, connectivity, what's being built nearby, whether the price is fair).
**Why alternatives fall short:**
- Government/incumbent portals (eauctionsindia, IBAPI, BAANKNET): listings-only, no real search, no context, no help evaluating.
- DIY research: you'd manually Google each location for water, flooding, projects, connectivity, comparable prices — slow and scattered.
**What it costs them:** Time (hours of manual research per property) and money (bidding blind on a location you don't understand, or overpaying vs. market).
**Emotional tension:** Deadline pressure + uncertainty about a place you may never have visited.

## Competitive Landscape
**Direct:** eauctionsindia.com, bankeauctions.com, BankAuctions.in, eAuctionDekho, FindAuction — listing aggregators. Fall short: listings-only, weak/no search, no AI, no location context, no price history.
**Secondary:** IBAPI, BAANKNET — official source portals. Fall short: government UX, no filtering/synthesis, no evaluation help.
**Indirect (the real competitor):** the DIY workaround — portals + WhatsApp groups + manually Googling each location + gut. Falls short: slow, scattered, doesn't scale across many auctions.

## Differentiation
**Key differentiators (hero → supporting):**
- **Natural-language search + AI web-search enrichment** — ask what the notice can't tell you: groundwater/water availability, waterlogging & flood history, existing + upcoming govt/private projects, transport (metro/bus/connectivity), nearby schools/hospitals, approximate location & travel time from your places, and **market price vs. the reserve**. Researched from the web and cited. (Approximate, grounded — not legal advice.)
- **Grounded answers** — graph answers come from tool calls; the agent never invents prices, counts, or IDs.
- **Per-property chat** — open any listing and ask questions about that specific property.
- **Market + price-history context** — reserve vs. area comparables; re-auction price-drop signals (`is_reauction`, `previous_reserve_price`).
- **Watchlist + deadline alerts** — save properties and get reminded before the deadline.
- **Enriched data** — every listing OCR'd from its sale notice; semantic search over description + notice text + notice image.
**How we do it differently:** we make the auctions *searchable* and then help you *evaluate the location and price* with AI web research, in plain English.
**Why that's better:** faster, better-informed bidding decisions without hours of manual research.
**Why customers choose us:** the only place you can search these auctions in plain English **and** get instant, cited context on the place and the price.

## Objections
| Objection | Response |
|-----------|----------|
| "Can I trust an AI here?" | Graph answers are grounded in the data; web-researched context is cited so you can check the source. We're explicit that it's research, not legal advice. |
| "Isn't this just another listings site?" | Listings are free elsewhere; the value is plain-English search plus instant, cited context on the location and whether the price is fair. |
| "Does it do the legal/title checks?" | No — and we say so. We help you *evaluate and shortlist*; title/EC/advocate work is still yours to do. That honesty is the point. |
**Anti-persona:** Not for enterprises/banks/auction houses; not for vehicle/goods auctions; not (yet) for buyers outside Tamil Nadu.

## Switching Dynamics
**Push:** Auctions are un-searchable; the notice tells you nothing about the place; manual research is slow.
**Pull:** Ask a plain-English question, get a grounded shortlist and instant cited context on any property.
**Habit:** Existing portals + WhatsApp + manual Googling; "this is just how it's done."
**Anxiety:** "Is the info current / can I trust it?" — answered by grounding + citations + "verify with the bank" transparency.

## Customer Language
**How they describe the problem (verbatim / observed):**
- Search intent as typed: *"flats in Chennai <50L," "commercial Coimbatore," "plots near Madurai," "residential in Chennai under 30 lakhs."*
- Evaluation questions (the hero use case): *"is there groundwater here?", "does this area flood?", "any metro coming nearby?", "how far is it from [my area]?", "is the reserve price fair vs market?"*
**How they describe us (verbatim from product):**
- "bank auctions · ai-powered search"
**Words to use:** bank auction, e-auction, SARFAESI, reserve price, EMD, re-auction, price drop, groundwater, waterlogging/flood, connectivity, market price, ₹ lakhs/crores, real city/bank names, plots/land/flats.
**Words to avoid:** "due diligence," "advocate replacement," "legal opinion," "title-clear," "guaranteed," "institutional-grade," hype/"revolutionary." (We help evaluate; we don't do legal diligence.)
**Glossary:**
| Term | Meaning |
|------|---------|
| SARFAESI | The Act under which banks auction NPA/defaulted-loan property |
| Reserve price | Minimum bid the bank will accept |
| EMD | Earnest Money Deposit required to bid |
| Re-auction | A property re-listed after a failed auction, often at a lower reserve |
| Deep-research mode | A deeper, cited research report on one property (market + location + price history + notice red flags) |

## Brand Voice
**Tone:** Understated, plain-spoken, trustworthy. Lowercase, calm, no hype.
**Style:** Direct, concrete, grounded in real data; transparent about limits ("information platform only — always verify with the bank"; "research, not legal advice").
**Personality:** Grounded · precise · practical · calm · honest. **The live app is already clean/fintech-styled** (cobalt-blue accent `#0052ff`, Inter / Bricolage Grotesque / JetBrains Mono) — market it as the modern, trustworthy tool it already is. (The `redesign/` folder is a separate, unwired exploration; ignore for positioning.)

## Proof Points
**[VERIFY — add your real traffic/revenue numbers]**
**Metrics:** 2,179 enriched TN auctions (~600 live at any time); 49 cities, all 38 TN districts; 122 banks; median live reserve ≈ ₹40L. Data is land/plot-heavy (Land & Building, Land, Plot lead; then Flat, House) and ~97% residential. Top banks by volume: Canara, Equitas, Cholamandalam. Enrichment: every listing has a description; ~31% are marked "complete" (quality is improving). **[Need: user count, Pro subscribers, MRR, retention — currently pre-traction: ~7 registered users.]**
**Customers:** None public yet. **[Need: early users, quotes, a "found a good deal / avoided a bad one" story.]**
**Testimonials:** None captured yet. **[Highest-value gap — collect 3–5 verbatim buyer quotes.]**
**Value themes:**
| Theme | Proof |
|-------|-------|
| Ask anything about a property, get cited context | `internet_search` (Tavily) enrichment: water, flood, projects, transport, amenities, location, market price |
| Grounded, no-hallucination graph answers | Tool-call architecture; never invents prices/counts/IDs |
| Real data from the actual notices | OCR + vision-LLM enrichment of every sale notice; 3 vector indexes |

## Goals
**Business goal:** Grow from pre-traction (~7 users) toward real usage — organic-first, discovery-led. Near-term (90 days): **get discovered** (from ~invisible in search to ranking on the auction dataset) and land the first cohort of real users.
**Conversion action:** Primary = free signup + first grounded search/evaluation (activation). Revenue = upgrade to ₹499 Pro (more chats/day + Pro model + higher reasoning effort → longer, deeper research sessions). **[Add: capture "auction alerts" email at top of funnel — not built yet.]**
**Current metrics:** Unknown — **no web analytics installed yet** (GA4/GTM/PostHog absent from `web/`). Instrumenting the funnel is the first measurement step. **[VERIFY / provide once tracked.]**
