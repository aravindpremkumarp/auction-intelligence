# Mode: refresh

Re-scrape and update existing data for auctions nearing deadline or marked
`RESEARCHING`/`BID_READY`. Catches post-listing amendments (revised reserve
price, withdrawn listings, new documents).

## Input

- Optional filter: `state`, `days_until_deadline`, or explicit auction_ids
- Default: all auctions with `state in (RESEARCHING, BID_READY)` and
  deadline within 14 days

## Process

1. Re-fetch property page from its original portal.
2. Diff against stored `AuctionProperty` node properties.
3. Re-run OCR only on new/changed document downloads (cache hits for unchanged).
4. Update Neo4j with any deltas; log changes to `refresh_log.tsv`.
5. If material change (price delta > 10% or withdrawal), flag for user review.

## Output

Change report per auction. Material changes trigger notifications (Phase 6 alert system).
