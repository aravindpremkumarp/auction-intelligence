# Mode: evaluate

Score a single auction or a batch against the 10 dimensions (see `_shared.md`).

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
