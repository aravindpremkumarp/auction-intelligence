# SEO plan — the programmatic play

**Thesis:** AuctionScope's knowledge graph is a pre-built SEO moat. Thousands of
auctions tagged by city, area, asset category, property type, and bank map directly onto
the exact long-tail queries buyers search — and no listing aggregator structures them
this well. This is the single highest-leverage marketing bet.

## The search demand

High-intent, low-competition long-tail. Representative patterns (validate volumes in
Search Console / a keyword tool before committing):

- `bank auction properties in <city>` — Chennai, Coimbatore, Madurai, Kanchipuram…
- `SARFAESI auction <asset type> <city>` — flats, plots, houses, commercial
- `<bank> auction properties <city>` — SBI, HDFC, Indian Bank…
- `bank auction property under <price> <city>`
- Process queries: `how do bank auctions work in India`, `what is EMD in bank auction`,
  `encumbrance certificate for auction property`

## Programmatic landing pages (phase 1 — the moat)

Generate one indexable page per meaningful facet combination, populated from the graph:

```
/auctions/<city>/                        e.g. /auctions/chennai/
/auctions/<city>/<asset-type>/           e.g. /auctions/chennai/residential/
/auctions/bank/<bank-slug>/<city>/       e.g. /auctions/bank/sbi/chennai/
```

Each page should carry:
- A unique H1 + intro reflecting the facet ("Bank auction residential property in
  Chennai — N live auctions, reserve prices from ₹X").
- **Live data from the graph** — current count, price range, sample listings (the
  `/properties` endpoint already serves cascading facets + counts).
- Internal links to sibling facets (other cities, other asset types) — builds the mesh.
- A clear CTA into the conversational app ("Ask about Chennai auctions →").
- Structured data (`schema.org`) — `ItemList` / `Product`-style markup for the listings.

**Guardrails against thin/doorway pages** (Google penalises these):
- Only generate a page for a facet with **enough real inventory** (e.g. ≥ N live
  auctions) — skip empty combinations.
- Each page must have genuinely unique, data-backed content — not a templated stub with
  the city name swapped.
- Keep them fresh: the graph updates, so counts/prices should reflect live data.

## Technical SEO checklist

- [ ] Extend [`../../web/sitemap.xml`](../../web/sitemap.xml) to include every generated
      facet page (today it lists only `/`, privacy, terms, disclaimer). Generate it from
      the graph so it stays current. Consider a sitemap index if it grows large.
- [ ] Confirm [`../../web/robots.txt`](../../web/robots.txt) allows the new paths (it
      already `Allow: /`, disallows `/admin` and `/review` — good).
- [ ] Server-render or pre-render the facet pages — the app is a vanilla-JS SPA, so
      client-only rendering risks poor indexing. Decide: static pre-render at build/pipeline
      time vs. server-render on the API. **This is the key architectural decision.**
- [ ] Unique `<title>` + meta description per page.
- [ ] Canonical tags to avoid duplicate-content across overlapping facets.
- [ ] Open Graph / Twitter cards (an `og-image.png` already exists in `web/`).
- [ ] Submit sitemap to Google Search Console; monitor coverage + impressions.
- [ ] Page speed — facet pages must be fast (Core Web Vitals is a ranking factor). The
      `/benchmark` skill can track regressions.

## Content SEO (phase 2 — top of funnel)

Educational cornerstone content that captures searchers earlier in the journey. See
[`../content/calendar.md`](../content/calendar.md). Interlink cornerstone posts with the
relevant facet pages (e.g. the "how EMD works" post links to live Chennai auctions).

## Sequencing

1. **Decide the rendering approach** (static pre-render vs server-render) — blocks everything.
2. Ship city-level pages for the top 5–8 TN cities by inventory.
3. Add asset-type sub-pages where inventory supports them.
4. Generate the sitemap from the graph; submit to Search Console.
5. Publish cornerstone content; interlink.
6. Measure impressions → clicks → activations; expand the facets that convert.

## Measurement

- Search Console: impressions, clicks, average position, indexed-page count.
- Which facet pages drive chat sessions (tag the CTA with the source facet).
- Cost is ~zero (organic) — so the metric is *activations per indexed page* over time.
</content>
