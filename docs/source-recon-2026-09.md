# Source recon — BAANKNET and bankeauctions.com (September 2026)

Hands-on follow-up to `docs/auction-source-expansion-2026-08.md`, which ranked
these two as source #2 and #3 but could not reach BAANKNET at all. Both are now
verified end to end. Reproduce with:

```bash
python scripts/probe_source_apis.py --json data/source_api_probe.json
```

---

## 1. Verdict

Both sources are adapter-viable today, and **neither needs a browser, a login,
or a CAPTCHA**. Both expose a JSON search endpoint and a downloadable sale
notice PDF — the artifact the whole enrichment chain is fed by.

This is a materially better position than the August evaluation assumed. That
doc warned BAANKNET might be "session/viewstate-heavy ASP.NET" and that IBAPI
would have to become the PSU fallback if so. It isn't: it is a Next.js front
end over a public REST API, and it is the *cheapest* of the two to integrate,
not the riskiest.

| | BAANKNET | bankeauctions.com |
|---|---|---|
| Transport | JSON REST, no auth | JSON (legacy DataTables), no auth |
| TN rows | 6,864 property records | 195 reported / **160 unique** live |
| TN live | ~800–1,100 (sampled, see §4) | 160 |
| Lenders | PSU banks | NBFC / HFC / small-finance / ARC |
| Sale notice PDF | 10/10 sampled auctions | yes, on every detail page |
| Notice format | mixed (scans + text) | text-layer PDF in the sample |
| Cost to run | HTTP + JSON, CI-able | HTTP + one HTML parse, CI-able |

Today's scraper, for contrast, needs Selenium and a human to clear Cloudflare
on a local machine (`scrapers/phase1_harvest_urls.py:cf_wait`).

## 2. BAANKNET contract

Base: `https://baanknet.com/api/v1`. All calls below are unauthenticated GET/POST.

**Search** — `POST /property/detail/property-filter`

```json
{"search": {"stateId": 31}, "sort": {"type": "mostrecent"},
 "range": "", "page": 1, "limit": 50}
```

- `search` is an **object**, not a string; `stateId` goes inside it. Passing
  `state` at the top level is silently ignored and you get the national list —
  an easy way to think you have a TN feed when you don't.
- Tamil Nadu is `stateId: 31`. Get the list from
  `GET /common/states?countryId=101` (India is 101; TN comes back as
  `{"id": 31, "name": "Tamil Nadu", "code": "33"}` — the **code is not the id**,
  and `stateId: 33` quietly returns Tripura).
- `page` and `limit` work; `currentPage` and `size` are ignored.
- Response is Elasticsearch-shaped (`_index: psba_property`, `_source` holds the
  record). `total` caps at 10,000 nationally, so treat the unfiltered count as a
  floor; per-state counts are under the cap and appear genuine.
- Sort options: `mostrecent`, `pricelowtohigh`, `pricehightolow`, `relevance`.
  Other filters the UI builds: `cityId`, `pincode`, `propertyTypeId`,
  `propertySubtypeId`, `bankId`, `budget`, `area`, `globalSearch`.

**One search row already carries 85 fields**, including everything phase2
currently scrapes plus more: `propertyDetailId`, `auctionId`, `bankName`,
`borrowerName`, `borrowerAddress`, `guarantorName`, `propertyTitle`,
`propertyType`/`propertySubType`, `propertyPossessionType` (symbolic vs
physical), `propTypeOfAction` ("Under SARFAESI"), `districtName`, `cityName`,
`pincode`, `propertyPrice`, `auctionPrice`, `auctionStartTime`/`auctionEndTime`,
`emdStartTime`/`emdEndTime`, `inspectionStart`/`inspectionEnd`, `photos`.

**Auction detail** — `GET /auction/detail/{auctionId}`

Adds `reservePrice`, `emd`, `incrementPrice`, auto-extension rules, inspection
contact name and mobile, authorised-officer name and designation, and:

```json
"auctionDocuments": [
  {"description": "SALE NOTICE", "filename": "SALE NOTICE.pdf",
   "url": "https://cdn.baanknet.com/Production/.../379330.pdf", "size": 1654041}
]
```

10 of 10 sampled TN auctions carried at least one PDF. Descriptions are
free-text and inconsistent (`SALE NOTICE`, `sale notice`, `Web sale notice`,
`Paper publication Chennai`, and once a borrower's trade name), so an adapter
should take every PDF and let `pipeline/classify_document.py` sort them out
rather than match on the label.

**Property detail** — `GET /property/detail/{propertyDetailId}` — borrower and
guarantor blocks, carpet area, branch. Note the two endpoints are keyed on
*different* ids that happen to share a range; do not use one id for both.

## 3. bankeauctions.com contract

**Search** — `POST https://bankeauctions.com/home/liveAuctionDatatable/?state=24`
with form body `iDisplayStart=0&iDisplayLength=10&sEcho=1`.

The split is the trap: **filters ride the query string, paging rides the POST
body.** Filters: `state`, `city_name`, `bank_id`, `propertytype`,
`property_sub_type`, `reservePriceMinRange`, `reservePriceMaxRange`,
`search_input`, `budget_search`. Tamil Nadu is `state=24` (from the homepage
`<option>` list — unrelated to BAANKNET's numbering).

Two quirks an adapter has to absorb:

- **Page size is pinned to 10** whatever `iDisplayLength` says.
- **Consecutive pages overlap by one row.** A full TN sweep returns 195 rows of
  which 160 are unique, reproducibly. So dedupe by row id, and treat
  `iTotalRecords` as an over-count rather than a target.

Rows are positional arrays: `[logo, id, lender, description, city, auction date,
reserve price, EMD, action type, …, detail id, …, category, subcategory, …]`.
The description is the full schedule text **including boundaries** — the same
material our lot-schedule Lucene index is built on.

**Detail page** — slug built from the row:
`https://bankeauctions.com/{category}-{subcategory}-{city}-{col10}`, e.g.
`immovable-land-tirupathur-235813`. Server-rendered HTML carrying reserve price,
EMD, bid increment, auto-extension rules, inspection window, press-release date,
offer-submission dates, auction window, borrower name, and "View NIT Documents"
links to PDFs under `/public/uploads/bank/`.

Verified one of those PDFs: 5 pages, real `%PDF`, and its text layer opens
"TENDER DOCUMENT FOR E AUCTION … in exercise of its power under Section 13(2)
of the Securitisation … Act, 2002" — a genuine SARFAESI notice, and a *text*
PDF rather than a scan, which is cheaper for the extraction stack than what we
usually get. Do not count the root-level `Terms___Condition.pdf` or the user
agreement as notices; only `/public/uploads/bank/` links are per-auction.

`robots.txt` is absent on bankeauctions (404) and permissive on BAANKNET
(`User-agent: * / Disallow:` — nothing disallowed).

## 4. Coverage, and how much of it is new

**BAANKNET, Tamil Nadu:** 6,864 property records. Sampling evenly across the
result set (the default sort is most-recent-first, so page 1 alone badly
over-states it), 12–16% have an open auction window → roughly **800–1,100 live**.
Two runs gave 824 and 1,121; treat it as "several hundred to about a thousand"
until counted exactly. Live lenders: Canara, Indian Bank, SBI, IOB, BoB,
Central Bank, PNB, BoM. Districts skew Chennai, then Coimbatore, Salem, Erode,
Namakkal, Tiruvallur.

The other ~5,800 records are not waste. They are closed or past auctions with
reserve prices, dates and lender attached — exactly the material
`scripts/link_reauctions.py` needs, and the product's price-drop intelligence
is built on re-auction history.

**bankeauctions.com, Tamil Nadu:** 160 unique live auctions, 156 SARFAESI and
4 DRT. Lenders: Equitas SFB (42), HDB Financial (21), Repco Home Finance (16),
Hinduja Housing (13), Aadhar Housing (13), Ujjivan SFB (9), Federal Bank (6),
SBI (6), Grihum, Kotak, IndusInd, Deutsche, TNIIC, and three ARCs (CFM, ACRE,
Omkara). Cities spread away from Chennai: Erode, Tirupur, Thiruvallur,
Coimbatore, Thiruvarur, Kanchipuram, Thanjavur.

**Overlap between the two is structurally tiny.** Only 11 of 160 bankeauctions
rows (7%) come from a lender that also appears in the BAANKNET TN sample — SBI,
Union Bank, South Indian Bank. The other 93% are NBFCs, housing-finance
companies, small-finance banks and ARCs that never route through a PSU-alliance
portal. These two sources are complements, not substitutes, which is the case
for doing both rather than picking one.

## 5. What this changes

The August ordering (BAANKNET first, bankeauctions second) still holds, and for
the same reason: BAANKNET is the larger and structurally harder schema, so
building the adapter interface against it first is what stops the abstraction
from being retrofitted later. What changes is the risk profile — BAANKNET is no
longer the uncertain one. Its API is richer than the HTML we parse today, so
the adapter interface should be shaped around *structured records*, with
HTML-scraping as one adapter's private problem rather than the common case.

`scrapers/phase2_scrape_details.py` emits whatever `<strong>` key-value pairs a
page happens to carry. Neither of these sources produces that shape. The
normalization layer described in §7 of the August doc is now the blocking
piece, not the scraping.

## 6. Open questions before writing an adapter

- **Dedupe key.** Still the open question the August doc flagged, and now
  answerable with real data: three sources, three id spaces
  (`propertyDetailId` / bankeauctions row id / eauctionsindia URL). Borrower
  name + reserve price + auction date is the obvious candidate; BAANKNET's
  `propertyUniqueId` and `customerId` may make it easier within that source.
- **Overlap with what we already hold.** Not measurable from this container —
  `data/tn_auction_data.jsonl` is gitignored and local-only. Pull 20 TN records
  from each source locally and diff before sizing the win.
- **Re-auction history.** BAANKNET keys auctions separately from properties, so
  a property with several auctions over time should be discoverable. No
  endpoint for "all auctions of a property" was found; worth one more look,
  because it would hand us re-auction linkage directly instead of inferring it.
- **Rate limits.** ~60 requests to each host at roughly 1/s drew no throttling,
  no CAPTCHA, no blocks. That is not a guarantee; a full 6,864-record sweep is
  a different load profile and should back off politely.
- **Terms of use.** BAANKNET is an official PSB Alliance portal publishing
  notices that SARFAESI §13(8) requires to be public. bankeauctions.com is a
  commercial platform (C1 India) — read its terms before it becomes
  load-bearing. Different tiers, different exposure.
- **Datacenter IP caveat, inverted.** These results come from a cloud sandbox,
  which is the *pessimistic* environment. A local or residential run should do
  at least as well, never worse.
