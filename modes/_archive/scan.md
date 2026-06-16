# Mode: scan

Discover new auctions across configured portals.

## Inputs

- Optional portal filter (default: all in `pipeline/config.py:PORTALS`)
- Optional region/state filter (default: Tamil Nadu)
- Optional `since` timestamp (default: 24h ago)

## Process

1. For each portal, invoke its scraper under `scrapers/portals/`.
2. Dedup against existing Neo4j `AuctionProperty` nodes by `auction_id` + fuzzy URL match.
3. Dedup cross-portal using rapidfuzz on `title + reserve_price + city`.
4. Feed new records into the existing 4-stage pipeline
   (`ocr_extract → lexical_graph → normalize → load_enriched`).
5. For each newly loaded property, create an `InvestmentTracker` in `DISCOVERED` state.

## Output

- Count of new auctions per portal
- Count of duplicates filtered
- List of new auction_ids for follow-up scoring
