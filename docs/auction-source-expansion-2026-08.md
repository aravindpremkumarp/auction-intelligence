# Auction source expansion — beyond eauctionsindia.com (August 2026)

Evaluation of where else to source auction listings, why the current single
source is a standing risk, and the order to add sources in. Companion tool:
`scripts/probe_sources.py`.

---

## 1. The problem with one source

`scrapers/phase1_harvest_urls.py` and `phase2_scrape_details.py` read one site.
That site sits behind Cloudflare, and our answer to the challenge is a human:

```python
# phase1_harvest_urls.py — cf_wait()
print("🚨  CLOUDFLARE DETECTED")
print("Solve the CAPTCHA in the Chrome window that opened.")
```

Three consequences follow from that design, and they compound:

- **A rule change at the source zeroes the pipeline.** No listings means no
  notices, no OCR, no extraction, no graph updates — the whole chain in
  `docs/extraction-pipeline-review-2026-07.md` idles.
- **Recovery needs a person at a machine.** Not a redeploy, not a retry — an
  operator watching a Chrome window. `README.md:109` says it plainly:
  "Selenium scrapers for eauctionsindia.com (local only)."
- **We depend on a site we publicly compete with.** `web/compare/` ships
  `auctionscope-vs-eauctionsindia`. Being a named competitor of your only
  upstream is not a stable arrangement.

eauctionsindia is also not a primary source. It re-aggregates notices published
elsewhere, so we inherit its coverage gaps, its latency, and its transcription
errors, and we pay a CAPTCHA tax for the privilege.

## 2. The criterion that actually ranks sources

Everything downstream of `phase2` is fed by the **sale notice PDF**, not the
listing row. `extract_download_links()` hunts for `.pdf` hrefs; MinerU OCRs
them; `extract_descriptions` / `verify_and_enrich` derive the fields that make
a record worth anything; `prepare_tn_data.py` reports `downloads_complete`.

A listing without an attached notice is a row we cannot enrich. So "coverage"
counted in listings is the wrong measure. The right one is:

> **notices per adapter written** — how many enrichable records does one
> integration buy, and how much does that integration cost to build and keep
> running?

Secondary filters, in order: does it carry Tamil Nadu volume; can we fetch it
without a browser; is scraping it defensible.

## 3. The landscape

| Source | Tier | Coverage | Notes |
|---|---|---|---|
| BAANKNET (baanknet.com) | Official | PSU banks, nationwide | PSB Alliance portal; superseded eBKray as the consolidation point |
| IBAPI (ibapi.in) | Official | PSU banks | Largely the same pool BAANKNET consolidated |
| IBBI (ibbi.gov.in) | Official | Corporate liquidation | Different asset class, different buyer |
| bankeauctions.com (C1 India) | ASP | Broad — PSU, private, NBFC | Largest ASP by bank count; auctions execute here |
| AuctionTiger (eProcurement Tech) | ASP | Broad, incl. ARCs/NBFCs | POST-driven search |
| MSTC | ASP | Banks + PSU/govt assets | Also hosts an IBAPI-branded section |
| Bank portals (SBI, BoB, Canara, HDFC…) | Issuer | Per-bank, authoritative | N integrations for N banks |
| ARCs (ARCIL, EARC, JM, Phoenix) | Issuer | Post-assignment NPAs | Invisible on bank portals — genuinely unique inventory |
| Newspaper sale notices | Statutory | TN-complete by law (SARFAESI §13(8)) | Feeds the existing OCR stack directly |
| DRT / official liquidator | Statutory | Recovery-certificate sales | Fragmented, low volume |

**Tiers matter for legal exposure.** Official and statutory sources publish
notices that are *required* to be public. ASPs are commercial platforms with
terms of use — more defensible than a pure re-aggregator, since the auction
actually executes there, but not the same category as a disclosure portal.

## 4. Recommendation — BAANKNET first, bankeauctions.com second

### BAANKNET

- **One adapter ≈ all PSU banks**, which carry the bulk of SARFAESI volume in
  TN. Best coverage-per-adapter ratio available.
- **Upstream of what we scrape today.** Same properties, earlier, without a
  middleman's parsing errors.
- **Retires the competitor-dependency problem** in §1.
- **Risk:** unverified. It was unreachable from the cloud sandbox, and
  government portals are often session/viewstate-heavy ASP.NET. If it turns out
  to be browser-bound, IBAPI becomes the PSU-pool fallback and this ordering
  flips.

### bankeauctions.com

- **A different axis, not more of the same.** As the ASP, it carries
  bid-lifecycle facts a disclosure portal doesn't: status, EMD deadlines,
  extensions, sometimes outcome. That feeds `scripts/link_reauctions.py` —
  and a re-auction is the strongest available signal that a property failed to
  clear at its reserve, which is exactly the pricing intelligence the product
  sells.
- **Reaches private banks and NBFCs** that never route through BAANKNET.
- **Cheapest integration on the list** — server-rendered HTML, so plain HTTP
  plus a parser, no Selenium, no CAPTCHA loop. It can run in
  `.github/workflows/data-freshness.yml`, which the current scraper
  structurally cannot.
- **Caveat:** commercial ToS. Read them before it becomes load-bearing.

### Why this order, and only two

Scraping is not the work — **reconciliation is**. The same property will appear
on BAANKNET, on the ASP, and on eauctionsindia under three different IDs.
`phase2` currently emits whatever `<strong>` key-value pairs the page happens to
carry, a shape unique to one site; every new source needs a mapping layer into
the normalized schema `prepare_tn_data.py` produces.

Adding BAANKNET first forces that abstraction against the hardest schema while
there are only two sources to reconcile. Retrofitting it at five costs
multiples more.

### Why not the others first

- **IBAPI** — redundant with BAANKNET's pool. Worth having as a cross-check
  when BAANKNET is down, not as source #2.
- **AuctionTiger / MSTC** — legitimate, but ASP #2 and #3. Diminishing returns
  until entity resolution exists.
- **Bank portals** — worst coverage-per-adapter ratio: N fragile integrations
  for N banks. Only justified for one specific high-volume TN lender.
- **ARCs** — highest *differentiation* per row, since post-assignment NPAs are
  invisible everywhere else. But low volume and ad-hoc sites, and it doesn't
  address the single-source fragility. Natural third; cheap once adapters are
  pluggable. This is a sequencing call, not a value judgement.
- **Newspaper notices** — the only *provably* complete source for TN, since
  publication is mandated. The eventual moat. But it needs epaper acquisition
  (paywalled, licensing questions), page-region detection and Tamil OCR — a
  quarter of work, not a sprint. `pipeline/region_detect.py` and the OCR stack
  mean it isn't foreign territory, just expensive.
- **IBBI / DRT** — corporate liquidation is an adjacent product, not coverage
  of the current one.

## 5. What the probe found (and why you shouldn't trust it yet)

Run from the cloud sandbox, 2026-08-07 — **indicative only**:

| Source | Result |
|---|---|
| bankeauctions.com | HTTP 200, 131KB, server-rendered |
| MSTC | HTTP 200, 120KB, server-rendered, IBM_HTTP_Server |
| IBBI | HTTP 200, 2.8MB, server-rendered |
| AuctionTiger | HTTP 200 → redirect stub at `/EPROC/` |
| eauctionsindia | **HTTP 403, server: cloudflare** |
| BAANKNET, IBAPI | unreachable |

Two reasons to re-run locally before acting:

1. **Unreachable ≠ down.** The sandbox's egress proxy blocks hosts; that says
   nothing about the site.
2. **The IP is wrong.** Cloudflare and friends challenge datacenter ranges far
   more aggressively than residential ones. Anything learned about a source's
   defenses from CI is pessimistic and unrepresentative.

## 6. Local verification checklist

```bash
python scripts/probe_sources.py --json data/source_probe.json
```

Then, per surviving candidate:

- [ ] Follow the printed listing candidates to the **actual search/results
      page** — homepage PDF counts are a weak signal.
- [ ] Confirm results are reachable **without** a session/cookie/viewstate
      handshake, and find the pagination parameter.
- [ ] Confirm the **sale notice PDF** is linked from the detail page. If it
      isn't, the source is a lead list, not a pipeline input.
- [ ] Pull 20 TN records by hand and diff against `data/tn_auction_data.jsonl`
      — measures real overlap vs. genuinely new inventory.
- [ ] Note which fields map to the normalized schema and which have no home.
- [ ] Read the terms of use.

## 7. Then: the adapter refactor

Source #2 should land *behind* an interface, not beside the existing scripts.
Target shape:

- a source adapter yields `(listing_url, raw_fields, notice_urls)`;
- a per-source field map normalizes into the `prepare_tn_data.py` schema;
- phase1/phase2 become a driver over adapters rather than one site's scraper;
- Selenium becomes a per-adapter capability, not a global requirement — so
  cheap sources run in CI while only the expensive ones need a local browser.

The dedupe key is the open question and should be settled with real data from
two sources in hand: borrower + survey/plot number + reserve price + auction
date, resolved in the graph. `scripts/remove_duplicates.py` and
`link_reauctions.py` are the starting points.
