# Mode: compare

Side-by-side comparison of 2–5 auction properties.

## Input

- List of auction_ids (2 to 5)

## Process

1. Fetch each property (full node + relationships) from Neo4j.
2. Score each (call `score_auction` if not already scored).
3. Build comparison matrix across all 10 scoring dimensions + key attributes:
   - Reserve price, EMD, possession type, location, deadline
4. Highlight:
   - Best and worst on each dimension
   - Price per sq.ft. (when area is available)
   - Unique risk factors

## Output

Markdown table saved to `reports/output/compare_{timestamp}.md`.
