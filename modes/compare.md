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

## 10-dimension scoring taxonomy

| Dim | Name | Weight | What to assess |
|-----|------|--------|----------------|
| A | Price Attractiveness | 20% | Reserve price vs. comparables in same area |
| B | Location Quality | 15% | City tier, area desirability, auction density |
| C | Legal Clarity | 15% | Document completeness and field-conflict count |
| D | Bank Reliability | 10% | Bank's historical auction volume and success |
| E | Property Condition | 10% | Asset category, property type, description quality |
| F | Timeline Urgency | 10% | Days until application deadline |
| G | Due Diligence Ease | 5% | Download completeness, description score |
| H | Area Price Trend | 5% | Historical price direction in same area |
| I | Competition Risk | 5% | Number of similar concurrent auctions |
| J | Yield Potential | 5% | EMD-to-price ratio, estimated rental yield |

## Output

Markdown table saved to `reports/output/compare_{timestamp}.md`.
