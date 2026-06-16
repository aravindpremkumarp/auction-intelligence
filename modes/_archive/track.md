# Mode: track

Update the pipeline state for an auction. Enforces the 8-state workflow.

## States

```
DISCOVERED → SCORED → SHORTLISTED → RESEARCHING → BID_READY → BID_SUBMITTED → WON/LOST → COMPLETED
```

## Input

- `auction_id`
- `new_state` (one of the 8 states)
- Optional `notes`

## Process

1. Validate transition via `tracking.auction_tracker.VALID_TRANSITIONS`.
2. If target state is in `CONFIRMATION_REQUIRED`, return `confirmation_required`
   and wait for user explicit yes/no.
3. On confirmation, call `tracking.auction_tracker.transition(..., confirmed=True)`.
4. Appends to `tracking/auction_pipeline.tsv` and updates Neo4j.

## Reporting

- `pipeline_summary()` — state distribution across all tracked auctions
- `list_by_state(state)` — all auctions currently in a given state
