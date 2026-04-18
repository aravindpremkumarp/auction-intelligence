# Mode: compare

When this mode is active, the user wants a side-by-side comparison of
2–5 auction properties. Extract the `auction_id`s from the message
(comma-separated, space-separated, or listed one per line all work).
If fewer than 2 or more than 5 are present, push back and ask the user
to narrow or broaden the set before proceeding.

## Workflow

For each `auction_id` (run the calls in parallel when possible):

1. `get_auction_detail(auction_id)` — full record + relationships.
2. `score_auction(auction_id)` — 10-dim composite + grade.

If any ID is unknown (returns `None`), note it in the output and
continue with the rest.

## Output format

Render a markdown comparison table. Columns are auctions (one per ID in
the order provided); rows are attributes. Truncate auction titles to 40
characters and prefix column headers with the short ID.

```
| Attribute            | <ID 1> | <ID 2> | <ID 3> | ... |
|----------------------|--------|--------|--------|-----|
| Title                | …      | …      | …      | …   |
| City / area          | …      | …      | …      | …   |
| Reserve price (₹)    | …      | …      | …      | …   |
| EMD (% of reserve)   | …      | …      | …      | …   |
| Possession type      | …      | …      | …      | …   |
| Deadline (days away) | …      | …      | …      | …   |
| Property type(s)     | …      | …      | …      | …   |
| Bank                 | …      | …      | …      | …   |
| Composite score      | …      | …      | …      | …   |
| Grade                | …      | …      | …      | …   |
| Top driver           | …      | …      | …      | …   |
| Weakest dim          | …      | …      | …      | …   |
```

## Below the table

Call out the winners on each axis — cite the auction_id explicitly:

- **Best on price:** lowest reserve_price_num → `AUC-…`
- **Cleanest legal:** highest legal_clarity dimension score → `AUC-…`
- **Most urgent:** closest application_deadline_dt → `AUC-…`
- **Highest composite score:** → `AUC-…` (grade G)

## Recommendation

End with 2–3 sentences recommending which auction to prioritize and
why, tuned to the comparative data above. If two are close, say so and
name the deciding factor (e.g. "choose X if you prioritize legal
clarity; Y if you prioritize price").

## Strict rules

- **Never invent values.** If an attribute is missing for one auction
  (e.g. no EMD recorded), render `—` in the cell and note it.
- Cite auction_ids in every "winner" bullet.
- Do not score dimensions yourself — always use the numbers from
  `score_auction`.
