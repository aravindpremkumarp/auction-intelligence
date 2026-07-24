# How Landeed acquires land records (and what it means for our TN collector)

- **Status:** captured  <!-- captured | exploring | planned | implemented | parked -->
- **Date added:** 2026-07-24
- **Source:**
  - Terra launch — https://realtynmore.com/landeed-launches-terra-to-transform-fragmented/
  - Seed round, "Plaid for property" (Dealroom) — https://app.dealroom.co/news/note/landeed-raises-8-3m-seed-to-bring-plaid-style-title-search-to-india-s-property-market
  - Founder profile (ProptechBuzz) — https://www.proptechbuzz.com/news/sanjay-mandava-landeed-property-documentation
  - $5M raise, 10x Founders (Entrepreneur India) — https://india.entrepreneur.com/news-and-trends/property-title-search-innovator-landeed-raises-usd-5-mn/485979
  - YC-backed launch (YourStory) — https://yourstory.com/2022/09/ycombinator-backed-proptech-startup-landeed-helps-check-property-records
- **Tags:** landeed, data-acquisition, land-records, tamil-nadu, scraping,
  ocr, digilocker, buy-vs-build, due-diligence, legal-opinion
- **See also:**
  - `docs/landeed_tn_records.md` — the 22-record TN catalogue (inputs/outputs/pricing)
  - `inspiration/2026-07-06-browser-agents-for-tngis-extraction.md` — per-source
    feasibility table + buy-vs-build; the "Update 2026-07-06" section is load-bearing
  - `inspiration/2026-07-06-landlens-style-legal-opinion.md` — the end goal

## Why this exists

We keep circling the same question: to build the "collect all TN land documents
for a property" feature, do we need something Landeed has and we don't? Answer,
after researching how Landeed actually works: **no magic API exists.** Their moat
is unglamorous per-state engineering plus a human ops network. That reframes our
build as tractable (we need one state, they built 26) and confirms the hard part
is CAPTCHA/auth + certified copies, not the AI or the scoring.

Caveat on sourcing: Landeed's real acquisition method is their moat, so nothing
public says "we Selenium-drive tnreginet and solve captchas." Below separates
**confirmed** public claims from **inferred** mechanism.

## What Landeed publicly claims (confirmed)

- **Per-state automation, not one API.** Described as *"state integrations and
  automation"* / *"direct registry integrations"* pulling *"directly from official
  state portals."* India has no unified land-records API, so this is a portal-by-
  portal pipeline (TNREGINET + eServices for TN; Dharani for Telangana; Bhulekh;
  7/12 for Maharashtra; RTC for Karnataka; …).
- **A large pre-built corpus + continuous refresh.** Terra sits on *"773M+ land/
  property documents"* across 26 states + 4 UTs with *"continuous refresh
  systems."* So it is **not purely real-time** — much is pre-crawled, OCR'd,
  indexed, and periodically re-fetched; live fetch is layered on for freshness.
- **Domain-specific OCR + LLM standardization.** *"Domain-specific OCR"* for
  regional scripts and legacy scans, plus AI models to normalize each state's
  format into one schema. Same problem we solve with MinerU + LangExtract, one
  layer up.
- **DigiLocker integration.** Official, government-verified issuance/storage under
  the user's ID — the one clean official channel.
- **On-ground verification network.** They explicitly *"bridge digital records
  with physical sub-registrar workflows"* — i.e. **humans at SRO offices** placing
  orders and collecting certified copies that can't be pulled digitally.

## Inferred mechanism

```
 Per-state portal automation (headed browser, CAPTCHA handling, session mgmt)
   + domain OCR (scanned / regional-script docs)
   + LLM normalization to a unified schema
   + massive cached corpus, refreshed on a schedule
   + DigiLocker for official issuance
   + human ops network for certified copies + physical SRO steps
```

3+ years and $13M+ raised went into the boring, load-bearing parts: staying past
CAPTCHAs, absorbing per-state portal breakage, OCR'ing messy scans, and running a
physical retrieval network. No clever shortcut we're missing.

## What it means for our build

1. **"Collect all documents" = a per-portal automation + OCR + cache pipeline** —
   exactly what `docs/landeed_tn_records.md` prescribes (hit TNREGINET / eServices
   directly, cache as `LandRecord` on `AuctionProperty`, Selenium per
   `scrapers/worker.py`). We need **Tamil Nadu only** — 1 state, not 26.
2. **CAPTCHA/auth is the real cost — even Landeed leans on humans + DigiLocker.**
   Our options are unchanged: DigiLocker official issuance where available, a
   headed-browser + CAPTCHA-solve worker, or a human-in-the-loop step. Matches the
   per-source gate table in the 2026-07-06 TNGIS doc.
3. **Certified copies need an ops process, not just code.** Landeed runs a physical
   network. For v1, auto-pull the free *view* data (powers the analysis / red
   flags) and treat certified copies as an async order + deliver flow. Don't let
   "collect all documents" imply "certified."
4. **Two document classes drive the UX**: instant view lookups (EC-view, Patta/
   Chitta, FMB, Guideline Value) vs. paid/certified orders (certified EC, deed
   copies, CERSAI, patta transfer). The collector panel should show per-record
   status (queued / fetching / needs-input / ready / failed).

## Buy vs build

- **No public Landeed API** — you can't just call them for the data layer. But a
  licensed data-aggregator, DigiLocker official issuance, or reselling Landeed's
  own paid orders could beat rebuilding TN portal automation. Evaluate before
  building 4–5 CAPTCHA-fragile scrapers (this echoes the 2026-07-06 doc's
  buy-vs-build note).
- The extracted **resolution tuple** (district → taluk → village → survey +
  subdivision, SRO, patta, borrower) is what keys every portal query, so hardening
  taluk (79%) and survey-subdivision in the LangExtract schema is the prerequisite
  that pays off regardless of buy-vs-build.

## Open questions

- **CAPTCHA strategy** — auto-solve service vs. user handoff vs. licensed
  aggregator? Single biggest decision; shapes the whole collector.
- **Certified copies in v1?** Or ship instant view-data only and add the order
  pipeline in v2.
- **Which 4–5 records are must-have for v1?** Proposed: EC, Patta/Chitta, FMB,
  Guideline Value, CERSAI.
- **DigiLocker coverage for TN land records** — which of the 22 records are
  actually issuable through DigiLocker's official API vs. portal-only?

## Next step

Lock the collector into a `docs/design/` spec: the `record_type` dispatch table
(per-record keys + upstream portal + gate), the `LandRecord` Neo4j schema, the UI
status states, and the v1 record set. Then pick the CAPTCHA strategy before any
scraper code.
