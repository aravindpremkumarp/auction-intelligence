# Product Marketing Context

*Last updated: 2026-07-09*

> **Status: V1 auto-drafted from the codebase** (README, `web/index.html`, `docs/design/*`, billing + pricing code). Sections flagged **[VERIFY]** are inferences or gaps — correct them and re-run `/product-marketing`. Proof points and current metrics especially need your real numbers.

## Product Overview
**One-liner:** AI-powered search and diligence for Indian bank-auction (SARFAESI) property.
**What it does:** Scrapes scattered public bank-auction listings, OCRs the source sale-notice PDFs with a vision LLM, and structures everything into a knowledge graph — then puts an AI agent in front of it so a buyer can find, compare, and vet distressed-property auctions in plain English. Every answer is grounded in the data (the agent never invents prices, counts, or IDs).
**Product category:** Bank-auction / SARFAESI property portal — how buyers search: "bank auction properties," "e-auction property Tamil Nadu," "SBI auction flats Chennai." Sub-category: AI property search + diligence.
**Product type:** SaaS (web app over a proprietary data graph). Data moat: ~3,400 enriched Tamil Nadu auctions today, deduped against a 49,342-listing national universe.
**Business model:** Freemium subscription. Free = 25 AI chats/day; **Pro = ₹499 / 30 days** (Razorpay), 1,000 chats/day + better models. Browsing listings/filters is free; heavy AI use + premium diligence is the upsell.

## Target Audience
**Target customers:** Retail SARFAESI auction buyers in Tamil Nadu (expanding). Individuals and small investor syndicates — **not** enterprises, banks, or auction houses.
**Decision-maker:** The buyer/investor themselves (self-serve, single decision-maker; consumer purchase, not committee).
**Primary use case:** Find and vet a specific bank-auction property fast enough to bid confidently inside the hard ~2-week auction window — faster and cheaper than hiring a property advocate.
**Jobs to be done:**
- "Find me real auctions matching what I want" (e.g., *3BHK in Chennai under 1 Cr, ready possession*) without trawling clunky government portals.
- "Tell me if this property is a good, safe bet" — diligence on encumbrances, title, red flags, price history — before I put money down.
- "Warn me before the auction closes" — track properties and their deadlines / re-auction price drops.
**Use cases:** natural-language search; browse + filter by city/bank/type/price/date; paste a WhatsApp/broker listing to identify the auction; deep-research due-diligence report on one property; re-auction price-drop hunting; watchlist + deadline tracking.

## Personas
Primarily a single B2C persona (self-serve buyer). Two behavioral variants worth naming:
| Persona | Cares about | Challenge | Value we promise |
|---------|-------------|-----------|------------------|
| **The active bidder** (individual investor / small syndicate) | Winning a specific property, not overpaying, not getting burned by a title defect | ~2-week deadline; advocate is too slow/expensive; diligence scattered across portals | Confidence on the paperwork faster and cheaper than an advocate |
| **The deal hunter** (browses for opportunities) | Finding underpriced/re-auctioned distressed deals across the state | Listings are un-searchable; can't filter by real criteria; PDFs are dense | Searchable, structured, analyzable inventory with price history |

## Problems & Pain Points
**Core problem:** Bank-auction listings in India are scattered across clunky government/portal sites, locked inside dense PDF sale notices, and effectively un-searchable — so buyers can't filter by real criteria, can't read the legal fine print at scale, and can't tell a good deal from a title-defect trap.
**Why alternatives fall short:**
- Government/incumbent portals (eauctionsindia, IBAPI, BAANKNET): listings-only, no real search, no diligence, no synthesis.
- Property advocate: ~₹5k–25k and days-to-weeks — often **too slow for a ~2-week auction window**.
- DIY across portals (EC from TNREGINET; Patta/Chitta/FMB from TN eServices): scattered, manual, no synthesis.
**What it costs them:** Money (overpaying, or advocate fees), and worse — bidding on a property with hidden encumbrances, title defects, or litigation. Many "wing it / skip diligence" and eat the risk.
**Emotional tension:** Fear of an expensive, irreversible mistake under deadline pressure; doubt about whether the paperwork is clean; the stress of racing a clock.

## Competitive Landscape
**Direct:** eauctionsindia.com, bankeauctions.com, BankAuctions.in, eAuctionDekho, FindAuction — listing aggregators. Fall short: listings-only, weak/no search, no AI diligence, no price history, no grounding.
**Secondary:** IBAPI (Indian Banks' Association) and BAANKNET (official NPA portal) — authoritative source portals. Fall short: government UX, no filtering/synthesis, no help deciding.
**Indirect (the real competitor):** "**the spreadsheet-and-WhatsApp-and-gut workaround**" + hiring a property advocate. Falls short: slow, expensive, manual, no synthesis, doesn't scale to comparing many auctions.

## Differentiation
**Key differentiators:**
- **Grounded AI search** — natural-language questions answered from tool calls over a knowledge graph; never hallucinates prices/counts/IDs.
- **Diligence, not just listings** — a deep-research report (legal framework by auction type, encumbrance/borrower risk, comparables, location intel, document completeness, top-3 red flags) that replaces a ₹5k–25k advocate engagement inside the auction window. Plus the forthcoming **Dossier "Diligence Readiness Score."**
- **Enriched data** — every listing OCR'd from its actual sale notice; semantic search over description, notice text, and notice images.
- **Re-auction / price-history awareness** — `is_reauction`, `reauction_count`, `previous_reserve_price` on every row.
**How we do it differently:** We turn the PDF-and-portal mess into a structured, searchable, analyzable graph and put a grounded agent on top.
**Why that's better:** Faster, cheaper, and safer decisions on distressed property under deadline.
**Why customers choose us:** The only place you can *search* these auctions in plain English **and** get real diligence before you bid.

## Objections
| Objection | Response |
|-----------|----------|
| "Is the data accurate / can I trust an AI here?" | Every answer is grounded in the source sale notice; we show the data and link the official notice. **[VERIFY: make source-transparency visible in UI]** |
| "Isn't this just another listings site?" | Listings are free; the value is grounded search + a diligence report that replaces a slow, costly advocate. |
| "₹499 — why pay when portals are free?" | Portals give you raw listings; we save you the advocate fee and the risk of a bad title inside a 2-week deadline. |
**Anti-persona:** Not for enterprises/banks/auction houses; not for vehicle/goods auctions; not (yet) for buyers outside Tamil Nadu.

## Switching Dynamics
**Push:** Auctions are un-searchable; advocates are too slow/expensive; diligence is scattered; fear of a title-defect mistake.
**Pull:** Ask a messy human question, get a grounded shortlist of real auctions, then one click into a diligence report — inside the deadline.
**Habit:** Existing portals + WhatsApp groups + gut; "this is just how it's done."
**Anxiety:** "Can I trust an AI with a property decision?"; "Is the info current?" — answered by grounding + "always verify with the bank" transparency.

## Customer Language
**How they describe the problem (verbatim / observed):**
- "I've seen the pain" — collecting documents, paying advocates, struggling.
- Search intent as typed: *"flats in Chennai <50L," "commercial Coimbatore," "plots near Madurai," "3BHK in Chennai under 1 Cr, ready possession."*
**How they describe us (verbatim from product):**
- "bank auctions · ai-powered search"
**Words to use:** bank auction, e-auction, SARFAESI, reserve price, EMD, encumbrance, EC, patta, re-auction, reserve price drop, due diligence, city/bank names, ₹ lakhs/crores.
**Words to avoid:** "distressed assets" (jargon to a retail buyer), hype/"revolutionary," anything that overpromises legal certainty (compliance: we're an information platform, not legal advice).
**Glossary:**
| Term | Meaning |
|------|---------|
| SARFAESI | The Act under which banks auction NPA/defaulted-loan property |
| Reserve price | Minimum bid the bank will accept |
| EMD | Earnest Money Deposit required to bid |
| EC | Encumbrance Certificate (charges/liens on a property) |
| Re-auction | A property re-listed after a failed auction, often at a lower reserve |
| Dossier | Per-property document locker + diligence-readiness feature |

## Brand Voice
**Tone:** Understated, plain-spoken, trustworthy. Lowercase, calm, no hype (current site: "try asking," "or browse all properties").
**Style:** Direct and concrete; grounded in real data; transparent about limits ("information platform only — always verify auction details with the bank").
**Personality:** Grounded · precise · practical · calm · on-your-side. **[VERIFY: a fintech reskin is designed in `redesign/` — confirm the target voice as you move off the current "sketchbook" aesthetic.]**

## Proof Points
**[VERIFY — these need your real numbers; the codebase shows no analytics/testimonials yet]**
**Metrics:** ~3,400 enriched Tamil Nadu auctions live (via `GET /stats`); scraped/deduped against 49,342 national listings; grounded-answer architecture (3 vector indexes). **[Need: user count, Pro subscribers, MRR, retention.]**
**Customers:** None public yet. **[Need: early users, quotes, a case of a good/avoided deal.]**
**Testimonials:** None captured yet. **[Highest-value gap — collect 3–5 verbatim quotes from real bidders ASAP.]**
**Value themes:**
| Theme | Proof |
|-------|-------|
| Grounded, no-hallucination answers | Tool-call architecture; agent never invents prices/counts/IDs |
| Diligence that replaces a ₹5k–25k advocate | Deep-research report + forthcoming Dossier readiness score |
| Real data from the actual notices | OCR + vision-LLM enrichment of every sale notice |

## Goals
**Business goal:** Grow from post-MVP to revenue at scale — organic-first, discovery-led. Near-term (90 days): **get discovered** (go from ~invisible in search to ranking on the auction dataset).
**Conversion action:** Primary = free signup + first grounded search (activation). Revenue = upgrade to ₹499 Pro. **[Add: capture "auction alerts" email as top-of-funnel — not built yet.]**
**Current metrics:** Unknown — **no web analytics installed yet** (GA4/GTM/PostHog absent from `web/`). Instrumenting the funnel is the first measurement step. **[VERIFY / provide once tracked.]**
