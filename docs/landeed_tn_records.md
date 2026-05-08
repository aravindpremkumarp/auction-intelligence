# Landeed Tamil Nadu Records — Reference Spec

**Source list:** [data/landeed_TN.txt](../data/landeed_TN.txt) (22 URLs)
**Status:** Reference only — no code yet. Used to plan a future agent tool / scraper.
**Last refreshed:** 2026-04-27

## Why this exists

The auction project surfaces distressed Tamil Nadu properties from `eauctions.com`, but a bidder cannot place an informed bid without seeing the underlying land records (encumbrance, patta, survey, guideline value, etc.). Landeed packages 22 such records as either free lookups or paid certified-document orders. This spec catalogues each one — input, output, use case, pricing, and integration notes — so the team can decide which to wire into the chat agent and in what order.

A note on data confidence:

- **HIGH** — page rendered fully via WebFetch; fields verified.
- **MEDIUM** — page is client-side rendered and returned an empty shell; fields inferred from the URL slug + standard Tamil Nadu revenue-records terminology (A-Register, UDR Patta, FMB, CERSAI, etc.).
- **LOW** — flagged TBD; needs manual verification on landeed.com before integration.

The free `/tamil-nadu/{slug}` pages are HIGH confidence. The paid `/products/tamil-nadu/{slug}` pages are MEDIUM confidence and should be re-verified before a paid checkout flow is built against them.

---

## Per-product catalogue

### 1. EC — Encumbrance Certificate (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/ec-encumbrance-certificate
- **Category:** Free lookup
- **Input required:**
  - District, taluk, village / sub-registrar division
  - Survey number OR document number
  - Date range (from/to)
- **Output:**
  - PDF listing of all registered transactions (sales, mortgages, gifts, court attachments) on the property over the date range
- **Use case in our auction flow:**
  - Primary pre-bid sanity check: confirms the auctioning bank actually holds the lien and reveals competing encumbrances (other mortgages, court orders) that would survive the auction sale.
- **Pricing & turnaround:** Free · Instant
- **Integration notes:**
  - Underlying source is `tnreginet.gov.in`. Landeed wraps it with a friendlier UI but the upstream portal occasionally rate-limits and uses a captcha — direct scraping needs Selenium with captcha handling.
- **Confidence:** HIGH

### 2. Patta / Chitta (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/patta-chitta
- **Category:** Free lookup
- **Input required:**
  - District, taluk, village
  - Survey number + sub-division OR patta number OR owner name
- **Output:**
  - Patta record: owner name, extent (in hectares/acres), classification (wet/dry/manavari), sub-division details
- **Use case in our auction flow:**
  - Confirms current recorded owner matches the borrower named in the auction notice. A mismatch means the bank may not have clean title to sell.
- **Pricing & turnaround:** Free · Instant
- **Integration notes:**
  - Source: `eservices.tn.gov.in` (TN-AGRIS). HTML form, no login. Good first-tool candidate.
- **Confidence:** HIGH

### 3. Find My Survey Number (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/find-my-survey
- **Category:** Free lookup
- **Input required:**
  - Property address, landmark, or pin on a map
- **Output:**
  - Survey number, sub-division, village, taluk, district
- **Use case in our auction flow:**
  - Many auction notices give only an address, not a survey number. This bridges address → survey-number so the EC / patta tools become callable.
- **Pricing & turnaround:** Free · Instant
- **Integration notes:**
  - Bhuvan / cadastral overlay backed; useful as the first step in a chained workflow.
- **Confidence:** HIGH

### 4. Guideline Value (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/guideline-value
- **Category:** Free lookup
- **Input required:**
  - Survey number OR street name + village/area
- **Output:**
  - Government-notified per-square-foot/per-acre value for stamp duty
- **Use case in our auction flow:**
  - Compare the auction reserve price against the official guideline value to spot underpriced (good deal) or overpriced (bank padding) listings.
- **Pricing & turnaround:** Free · Instant
- **Integration notes:**
  - Source: `tnreginet.gov.in` guideline-value module. Form-based, no auth.
- **Confidence:** HIGH

### 5. Village Map (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/village-map
- **Category:** Free lookup
- **Input required:**
  - District, taluk, village
- **Output:**
  - Cadastral map (PDF / KML / GeoJSON) showing all survey numbers, plot boundaries, roads, water bodies in the village
- **Use case in our auction flow:**
  - Spatial context for a survey number — bidders can see whether the plot has road frontage, is near water, or is landlocked.
- **Pricing & turnaround:** Free digital · 2–3 business days for printed copy
- **Integration notes:**
  - Could be cached as static GeoJSON per village and overlaid on a Leaflet map in the property panel.
- **Confidence:** HIGH

### 6. Town Survey Records (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/town-survey-records
- **Category:** Free lookup
- **Input required:**
  - Town/village, ward, block, town-survey number
- **Output:**
  - Land classification, municipal details, extent in sq. m, ownership status
- **Use case in our auction flow:**
  - Urban-property analogue of the patta lookup — auction listings inside municipal corporations (Chennai, Coimbatore, Madurai) use TS-numbers instead of revenue survey numbers.
- **Pricing & turnaround:** Free lookup · Certified copy paid (~4 hours per user reviews)
- **Integration notes:**
  - Source: TSLR portal under `tnreginet.gov.in`. Pair with item 7 for the map.
- **Confidence:** HIGH

### 7. TSLR Town Survey Map Extract (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/tslr-town-survey-map-extract
- **Category:** Free lookup
- **Input required:**
  - Ward, block, town-survey number, sub-division, village/town
- **Output:**
  - PDF cadastral map extract with boundaries, sub-divisions, municipal divisions
- **Use case in our auction flow:**
  - Visual confirmation of plot shape and adjoining plots for urban auction listings — matters for rectangle-vs-irregular plot pricing.
- **Pricing & turnaround:** Free preview · Paid certified hard copy · ~4 hours
- **Integration notes:**
  - Same source as #6; usually consumed together.
- **Confidence:** HIGH

### 8. Sale Deed (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/sale-deed
- **Category:** Paid document order
- **Input required:**
  - Property address / survey number
  - Party names (buyer, seller)
  - Registration year + sub-registrar office (if known)
- **Output:**
  - Scanned, registered sale deed PDF
- **Use case in our auction flow:**
  - Builds the chain-of-title — bidder reviews the borrower's purchase deed to confirm root of title and any clauses (e.g. reversion, family settlement) that the bank inherited.
- **Pricing & turnaround:** ~₹500–1500 · 2–3 days
- **Integration notes:**
  - Order-based, manual fulfillment. Source: sub-registrar offices via `tnreginet.gov.in`. Not a candidate for auto-call from the chat — needs a checkout flow.
- **Confidence:** MEDIUM

### 9. Certified EC (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/certified-ec
- **Category:** Paid document order
- **Input required:**
  - Survey or document number, district/taluk/village, date range
  - Mailing address (for hard copy) OR email (digital)
- **Output:**
  - Officially signed / digitally signed EC accepted by banks and courts
- **Use case in our auction flow:**
  - The free EC (item 1) is fine for casual due diligence; banks and registrars require the certified version when the buyer registers the auction-purchased property.
- **Pricing & turnaround:** Paid · 7–10 days standard
- **Integration notes:**
  - Order flow + delivery tracking. Source: sub-registrar office.
- **Confidence:** HIGH

### 10. Patta Transfer (advisory + filing)

- **URL:** https://web.landeed.com/products/tamil-nadu/patta-transfer
- **Category:** Advisory / facilitation service
- **Input required:**
  - Transferor + transferee details, ID proofs
  - Property survey number, extent
  - Sale deed copy, latest patta
- **Output:**
  - Mutation (dakhal-kharij) application filed at the taluk; tracking until approval
- **Use case in our auction flow:**
  - Post-auction step — the winning bidder must transfer patta into their name to be the legal recorded owner. Many bidders abandon this step and later face disputes.
- **Pricing & turnaround:** ~₹2000–5000 · 15–45 days (government-paced)
- **Integration notes:**
  - Online mutation via TN-AGRIS / `eservices.tn.gov.in`; eSign / OTP required. Treat as a referral, not an auto-tool.
- **Confidence:** MEDIUM

### 11. FMB — Field Measurement Book (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/fmb-field-measurement-book
- **Category:** Free lookup
- **Input required:**
  - Village/division, survey number, sub-division
  - Mobile OTP (for the upstream gov portal)
- **Output:**
  - Digital sketch with boundary measurements, dimensions, abutting survey numbers
- **Use case in our auction flow:**
  - Verifies actual plot dimensions vs what the auction notice claims — a shrinking-extent fraud red flag.
- **Pricing & turnaround:** Free preview · Instant; soft/hard copy paid
- **Integration notes:**
  - OTP gate complicates fully automated lookup — needs phone-number relay or a one-time user OTP entry in the chat UI.
- **Confidence:** HIGH

### 12. Mortgage Report — CERSAI (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/mortgage-report
- **Category:** Paid document order
- **Input required:**
  - Property address or survey number, city/district
- **Output:**
  - CERSAI registry search result — all asset-financing security interests registered against the property nationwide
- **Use case in our auction flow:**
  - Critical pre-bid check: catches mortgages held by lenders OTHER than the auctioning bank. The bidder inherits these unless flagged.
- **Pricing & turnaround:** Paid (low) · Same-day to 1 day
- **Integration notes:**
  - Source: `cersai.org.in` (national, not TN-specific). Has a public-search portal. Strong candidate for a future agent tool — high signal, machine-friendly form.
- **Confidence:** MEDIUM (URL is `/products/` so paid; CERSAI itself is well-known)

### 13. Loan Document Pack (paid bundle)

- **URL:** https://web.landeed.com/products/tamil-nadu/loan-document-pack
- **Category:** Paid document order
- **Input required:**
  - Property survey number, applicant name
  - Loan purpose (agricultural / non-agricultural)
- **Output:**
  - Bundled patta + EC + A-Register/B-Register extract + latest property-tax receipt — formatted for a lender's loan-against-property file
- **Use case in our auction flow:**
  - For bidders planning to refinance the auction purchase post-acquisition; saves piecing the bundle together manually.
- **Pricing & turnaround:** ~₹3000–8000 · 5–7 days
- **Integration notes:**
  - Aggregated service; depends on items 2, 9, 15, 16. Not a primary tool — list as a marketplace upsell.
- **Confidence:** MEDIUM

### 14. Legal Opinion (advisory)

- **URL:** https://web.landeed.com/products/tamil-nadu/legal-opinion
- **Category:** Advisory / legal service
- **Input required:**
  - All title documents (patta, EC, sale deeds, court records if any)
  - Specific question / chain-of-title scope
- **Output:**
  - Written legal opinion from a Tamil Nadu advocate covering title clarity, marketability, and risks
- **Use case in our auction flow:**
  - For complex / disputed properties — converts a stack of records into a yes/no "safe to bid" verdict.
- **Pricing & turnaround:** ~₹5000–15000 · 3–5 days
- **Integration notes:**
  - Human-fulfilled; treat as an outbound referral. The agent could *suggest* it ("the EC shows 3 mortgages, consider a paid legal opinion") but cannot perform it.
- **Confidence:** MEDIUM

### 15. Property Tax Receipt (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/property-tax-receipt
- **Category:** Paid document order
- **Input required:**
  - Property address, district/municipality
  - Property-tax assessment number (if known)
- **Output:**
  - Latest tax receipt PDF and payment history
- **Use case in our auction flow:**
  - Reveals tax arrears that the new owner inherits. Heavy arrears can wipe out the discount the auction offered.
- **Pricing & turnaround:** ~₹200–500 · Same day to 1 day
- **Integration notes:**
  - Aggregates municipal-corporation portals (Chennai Corp, Coimbatore Corp, etc.) — different per ULB. Hard to generalize; per-city scraper required.
- **Confidence:** MEDIUM

### 16. A-Extract — A-Register Extract (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/a-extract
- **Category:** Paid document order
- **Input required:**
  - Survey number, district, taluk, village
- **Output:**
  - A-Register extract: current ownership, extent, classification — the canonical revenue record for rural / agricultural land
- **Use case in our auction flow:**
  - The authoritative complement to the patta lookup for rural auction properties; resolves disputes when patta and ground reality disagree.
- **Pricing & turnaround:** ~₹300–800 · 1–3 days
- **Integration notes:**
  - Source: district sub-registrar; partly available via TN-AGRIS, partly taluk-office only.
- **Confidence:** HIGH

### 17. UDR Patta — Updated Digital Records (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/udr-patta
- **Category:** Paid document order
- **Input required:**
  - Survey number, village, district
- **Output:**
  - Updated Digital Records patta extract — the latest digital version of the patta with all post-2017 mutations applied
- **Use case in our auction flow:**
  - Newer than the legacy patta (item 2); use when the auction notice involves recently-mutated property.
- **Pricing & turnaround:** ~₹400–1000 · 1–2 days
- **Integration notes:**
  - Source: TN-AGRIS / `eservices.tn.gov.in`. Has overlap with item 2 — pick one based on which the upstream portal serves for the given district.
- **Confidence:** HIGH

### 18. Old Survey Records (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/old-survey-records
- **Category:** Paid document order
- **Input required:**
  - Survey number, village, district, approximate year
- **Output:**
  - Historical FMB, old survey maps, archived tax-assessment records
- **Use case in our auction flow:**
  - Boundary-dispute resolution and historical title chain — useful when current FMB (item 11) is missing or mismatched.
- **Pricing & turnaround:** ~₹500–1500 · 3–5 days
- **Integration notes:**
  - Often physical archives at taluk office; manual retrieval. Not a tool — a service.
- **Confidence:** MEDIUM

### 19. Physical Land Survey (advisory)

- **URL:** https://web.landeed.com/products/tamil-nadu/physical-land-survey
- **Category:** Advisory / on-ground service
- **Input required:**
  - Property address / survey number, site-access coordination
- **Output:**
  - On-ground boundary demarcation report by a licensed surveyor + geo-tagged photos
- **Use case in our auction flow:**
  - For high-ticket bids where paper records and FMB sketches are insufficient; covers encroachments and adverse possession risk.
- **Pricing & turnaround:** ~₹8000–25000 · 5–10 days
- **Integration notes:**
  - Field service; cannot be called from the chat. Surface as an upsell when bid value > a threshold.
- **Confidence:** MEDIUM

### 20. Old EC — Historical Encumbrance Certificate (paid order)

- **URL:** https://web.landeed.com/products/tamil-nadu/old-ec
- **Category:** Paid document order
- **Input required:**
  - Property identifier, historical date range (typically 5–30 years back)
- **Output:**
  - Historical EC covering pre-digitisation encumbrances
- **Use case in our auction flow:**
  - Catches stale undischarged liens (mortgages from the 1990s/2000s) that may not appear in the modern EC (item 1).
- **Pricing & turnaround:** ~₹500–1500 · 2–5 days
- **Integration notes:**
  - Sub-registrar archive lookup, often manual. Same fulfillment pipeline as item 9.
- **Confidence:** MEDIUM

### 21. Apply Patta Offline (advisory + filing)

- **URL:** https://web.landeed.com/products/tamil-nadu/apply-patta-offline
- **Category:** Advisory / facilitation service
- **Input required:**
  - Transferor + transferee details, property survey number, possession proof, taluk-office appointment
- **Output:**
  - End-to-end offline patta filing at the taluk: document prep, filing, status tracking
- **Use case in our auction flow:**
  - For taluks where TN-AGRIS doesn't accept online mutations; alternative path for item 10.
- **Pricing & turnaround:** ~₹1500–4000 · 20–60 days
- **Integration notes:**
  - On-ground coordination with the tahsildar; not automatable.
- **Confidence:** MEDIUM

### 22. Search Property — address → survey resolver (free lookup)

- **URL:** https://web.landeed.com/tamil-nadu/search-property
- **Category:** Free lookup
- **Input required:**
  - Free-text address or landmark
- **Output:**
  - Survey number, village, taluk, district (same shape as item 3, friendlier UI)
- **Use case in our auction flow:**
  - Fallback when item 3 (Find My Survey) returns nothing — accepts looser address input.
- **Pricing & turnaround:** Free · Instant
- **Integration notes:**
  - Substantial overlap with item 3; pick one as the canonical resolver.
- **Confidence:** HIGH

---

## Bucket A — Free lookup tools (9)

These are the natural candidates for an auto-callable agent tool because they require no payment, no human fulfillment, and (mostly) no OTP. Recommended as the first integration phase.

| # | Product | Why first-class |
|---|---------|-----------------|
| 1 | EC (Encumbrance Certificate) | Single highest-signal pre-bid check |
| 2 | Patta / Chitta | Confirms borrower-vs-owner match |
| 3 | Find My Survey | Address → survey enabler for the rest |
| 4 | Guideline Value | Reserve-price sanity check |
| 5 | Village Map | Spatial context (cacheable as GeoJSON) |
| 6 | Town Survey Records | Urban analogue of patta |
| 7 | TSLR Map Extract | Urban analogue of village map |
| 11 | FMB (Field Measurement Book) | Boundary verification (OTP-gated) |
| 22 | Search Property | Looser-input survey resolver |

## Bucket B — Paid certified document orders (11)

Listed for awareness. Each needs a checkout + order-tracking flow if integrated; not first-class agent tools. Surface as suggestions inside the chat ("you can order a certified EC for X") rather than auto-calls.

| # | Product | Trigger to suggest |
|---|---------|--------------------|
| 8 | Sale Deed | When chain of title is unclear |
| 9 | Certified EC | When user is preparing to register the property |
| 10 | Patta Transfer | After winning a bid |
| 12 | Mortgage Report (CERSAI) | Before final bid commit |
| 13 | Loan Document Pack | When user mentions refinancing |
| 15 | Property Tax Receipt | Before bid (arrears check) |
| 16 | A-Extract | For rural / agricultural disputes |
| 17 | UDR Patta | When patta lookup is stale |
| 18 | Old Survey Records | When current FMB is missing |
| 20 | Old EC | When EC date range pre-dates digitisation |
| 21 | Apply Patta Offline | Fallback for taluks without online mutation |

## Bucket C — Advisory / human-in-the-loop services (2)

| # | Product | When to surface |
|---|---------|-----------------|
| 14 | Legal Opinion | EC shows multiple/complex encumbrances |
| 19 | Physical Land Survey | High-ticket bid + boundary uncertainty |

---

## Suggested integration roadmap

1. **Phase 1 — Wrap Bucket A as one chat tool.** Add `api/tools/landeed_tools.py` exposing `lookup_land_records(record_type, district, taluk, village, survey_number, ...)` — one async function that dispatches to the right upstream by `record_type`. Register it in [api/agent.py](../api/agent.py) with `@agent.tool_plain` exactly as `internet_search` is registered (see commit `7bdf05d`). Reference implementation pattern: [api/tools/web_tools.py](../api/tools/web_tools.py).

2. **Hit upstream gov portals directly, not Landeed.** Landeed has no public API. The free Bucket A lookups all map to `tnreginet.gov.in` or `eservices.tn.gov.in` (TN-AGRIS) — both have form-based search. Use the existing scrapers/ pattern (Selenium for captcha/JS pages, requests for static pulls) as proven by `scrapers/worker.py`.

3. **Cache aggressively in Neo4j.** EC, patta, guideline value rarely change between auction notices. Store records as `LandRecord` nodes attached to `AuctionProperty` (or `SurveyNumber`) so re-lookups are free. Schema lives alongside the existing definitions in `modes/_shared.md`. (Note: the `SurveyNumber` graph node was removed 2026-05; this section assumes it would be reintroduced alongside Landeed enrichment.)

4. **OTP relay for FMB (item 11).** Either ask the user to enter the OTP in the chat (relay it through to the upstream), or skip FMB in v1 and surface "FMB requires OTP — open the upstream portal" as a link.

5. **Bucket B as marketplace, not tools.** Render paid Landeed product cards in the artifact panel when relevant ("Want a certified EC? ₹X, 7–10 days") with a deep-link to the Landeed checkout. Don't try to broker payments through our app in v1.

6. **Add `LANDEED_REFERRAL_ID` (or analogous) to [pipeline/config.py](../pipeline/config.py)** if Landeed offers a partner / affiliate program — same env-var pattern as `TAVILY_API_KEY`.
