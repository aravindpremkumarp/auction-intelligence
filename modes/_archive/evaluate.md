# Mode: evaluate

Score a single auction or a batch against the 10-dimension taxonomy below.

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

### Decision thresholds

- **85+ (A/A+)** → Strong buy — bid immediately
- **70–84 (B)** → Worth pursuing — complete due diligence
- **55–69 (C)** → Selective — only if matches specific criteria
- **Below 55 (D/F)** → Skip

## Single evaluation

Input: `auction_id`

Steps:
1. Call `scoring.auction_scorer.score_and_persist(auction_id)`.
2. Report composite score, grade, and per-dimension rationale.
3. Transition `InvestmentTracker` to `SCORED` (no confirmation needed).
4. If grade ≥ B, suggest (do not execute) transition to `SHORTLISTED`.

## Batch evaluation

Input: filter criteria (price range, city, property_type, deadline window)

Steps:
1. Use agent tool `search_auctions` to fetch candidates.
2. Score each in parallel (Phase 5: batch conductor).
3. Return ranked table: auction_id, title, score, grade, top 3 dimension drivers.

## Output format

```
auction_id | title (truncated) | score | grade | top driver | weakest dim
```
