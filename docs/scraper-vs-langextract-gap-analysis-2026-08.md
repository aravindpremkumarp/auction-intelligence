# Gap analysis: web-scraper entities vs LangExtract entities

*2026-08-10 · corpus: 2,822 listings, 1,518 extracted notices, 2,801 listings
reachable from an extracted notice. Lot↔listing pairing reuses
`pipeline/apply_extractions.match_lots_to_listings`; analysis scripts lived in
the session scratchpad, methodology notes inline below.*

The portal (scraper side) is **directionally reliable on money, dates and
bank identity** (97–98% agreement with the notice) but has five systematic
defect classes: stale re-auction dates, a month-off deadline cluster, 10×
price errors, missing borrowers, and a geography field that disagrees with
the notice ~9% of the time even after transliteration normalization. Two
fields that look wrong at first — branch and contact phone — are actually
*different entities* on the two sides (schema drift, not extraction error).

## Per-entity verdict

| Entity | Compared | Agreement | Real error class | Verdict |
|---|---|---|---|---|
| Reserve price | 2,354 | 97.7% | 18 near (rounding), 54 mismatch — 17 exactly 10×, 1 exactly 100× | trust scraper; 10× errors need review on BOTH sides |
| EMD | 2,362 | 96.5% | 83 mismatch, several 10×; both sides ~10%-of-reserve for >90% of lots | trust scraper, flag mismatches |
| Auction start date | 2,407 | 97.9% | 50 mismatch — dominated by re-auction staleness (below) | trust scraper for CURRENT round, notice for original round |
| Auction end date | 2,158 | 98.5% | 32 mismatch | same |
| Application deadline | 1,797 | **90.3%** | 175 mismatch; visible month-off-by-one cluster (06-28 vs 07-28) | worst date field; needs re-scrape or notice precedence |
| Bank | 2,716 | 97.3% | 74 mismatch: OCR typos on the notice side ("IICI Bank"), legal-vs-brand names ("Repco" vs "The Repatriates Co-operative…"), a few genuinely different (DRT vs actual creditor) | trust scraper (portal picklist is clean); langextract needs a bank-alias table |
| Branch | 1,512 | 43.2% | — | **not an error: different meaning.** Portal = office conducting the sale ("ZONAL OFFICE, CUDDALORE"); notice = borrower's loan branch ("Portonovo"). Keep both, rename fields |
| Borrower | 2,109 | 91.3% | 183 fuzzy misses — mostly OCR/typo variants of the same person ("Musthala"/"Musthafa", "SGayathri"/"S Gayathri") | plus **630 listings where scraper has no borrower at all** and the notice names them |
| Geography (city) | 2,719 | 91.1%* | 242 listings where the portal city appears nowhere in the notice's geography | *after phonetic normalization + alias table; raw agreement was 76.5% — transliteration variants (Kanchipuram/Kancheepuram, Trichy/Tiruchirappalli) accounted for 62% of raw "errors" |
| Geography (area) | 2,719 | 78.2%* | 594 absent — portal "area" often names the taluk of the *branch*, not the property | weakest scraper field after branch |
| Contact phones | 2,460 | 76.6% | — | **mostly schema drift:** portal number = service-provider/helpdesk; notice numbers = authorized officer. Overlap only when the portal lists the officer |
| Description | 875 judged | 77.6% complete | 196 incomplete, 22 describe the wrong property; 50 web descriptions are <50% the length of the notice description | confirms the known problem; grounded descriptions already replace these where extraction exists |
| Property type | 2,822 | see PR #366 | portal Land/Plot default wrong 46–65% | already normalized; langextract canonical |
| Asset category | 2,822 | — | 97.7% "Residential" — form default, no signal | already derived from normalized type |
| Auction type | 2,176 | — | **646 listings missing**; langextract `legal_basis` (SARFAESI etc.) covers 98.3% of notices | fill from langextract |
| Outstanding dues | 0 | — | scraper has no field; notice has it for 95.6% of docs | langextract-only entity, already promoted |
| Boundaries / extent / survey nos / UDS | 0 | — | scraper never had these; grounded pipeline writes them | langextract-only |

## The five real defect classes on the scraper side

1. **Re-auction staleness.** When an auction is re-scheduled the portal
   updates only the start date: 14 listings have start *after* end
   (e.g. start 2026-09-29, end 2026-06-29, deadline 2026-06-28). The
   linked notice PDF describes the earlier round, so start-date
   "mismatches" cluster on the same day-of-month three months apart.
   Consequence: end/deadline on re-auctioned listings are silently wrong.

2. **Deadline month-off cluster.** 9.7% of deadlines disagree; a visible
   subgroup is exactly one month early (scraper 06-28, notice 07-28) —
   consistent with a parse of an updated notice date that kept the old month
   or a stale field after extension.

3. **10× money errors.** 18 of 55 reserve-price disagreements are exactly a
   factor of 10 or 100 — lakh/crore conversion or a dropped digit. Both
   sides produce them (OCR can drop a zero too), so these ~20 listings are
   the highest-value human-review queue in the corpus: a wrong reserve price
   is the single most damaging field for users.

4. **Missing borrowers.** 630 listings (22%) have no Borrower node while the
   linked notice names borrower + guarantors, roles and addresses. Where both
   exist, 8.7% disagree — nearly all near-miss spellings, either side may be
   the corrupted one.

5. **Geography by branch, not by property.** The portal's city/area is
   entered per listing office; multi-lot and rural notices put the property
   in a different district/taluk entirely. After normalizing transliteration,
   242 listings (8.9%) have a portal city that appears nowhere in the
   notice's geography, and 594 (21.8%) an unmatched area. The langextract
   `location` entities (village/taluk/district + registration hierarchy,
   96.3% doc coverage) are the property-true geography.

## Schema drift (structure, not values)

- **Branch and contact mean different things per side** (see table). Rename
  rather than reconcile: `listing_branch` / `loan_branch`,
  `portal_contact` / `officer_contact`.
- **`:SurveyNumber` label exists with zero nodes** — dead schema; survey
  numbers live as langextract `identifier` entities (kind=survey_old/new,
  14,600 entities) and promoted lot facts.
- **Scraper taxonomy nodes** (`:AssetCategory`, `:PropertyType`,
  `:AuctionType`) are portal picklists, already demoted to provenance by the
  property-type work; `:AuctionType` is the one still worth reading — but
  langextract `legal_basis` supersedes it too.
- **LangExtract's own drift:** ~30 docs carry model-invented entity classes
  (`extraction_text`, `class`, `type`, a `bororrower` typo) that loaders
  silently drop — same population as the zero-score docs.
- **Transliteration is the cross-cutting blocker.** Bank names, borrower
  names and every geography level need a shared alias/phonetic-normalization
  layer before any cross-source comparison is trustworthy; without it,
  62% of raw geography "errors" were spelling.

## Recommended precedence per field (scraper ⊕ langextract)

| Field | Canonical source | Why |
|---|---|---|
| Reserve, EMD, dates (current round) | scraper | portal reflects re-schedules; notice describes original round |
| Reserve, EMD (validation) | cross-check | 10×/mismatch → review queue, don't auto-pick |
| Property type, asset category | langextract | done (PR #366) |
| Geography of the property | langextract | portal geo is branch-anchored |
| Borrowers, outstanding, boundaries, extent, identifiers, description | langextract | scraper missing or incomplete |
| Bank | scraper name + langextract alias check | picklist beats OCR |
| Branch, contact | keep both, rename | different entities |
| Auction type / legal basis | langextract, scraper fallback | 646-listing gap |

*Companion to `docs/extraction-pipeline-review-2026-07.md` and the
property-type normalization in PR #366.*
