# Mode: compare

Side-by-side comparison of 2–5 auction properties, delivered as a markdown
table **in your reply**. There are no files to download.

## Input

- 2 to 5 `auction_id`s — from the user, or the current matches set.

## Process

1. `get_auction_detail(auction_id)` for EACH id — pull the comparison
   attributes: reserve price, EMD, `total_area` when present, city/area,
   bank, deadline, auction type, and the re-auction fields
   (`reauction_count`, `previous_reserve_price`, `price_history`).
2. ONE `search_auctions` aggregation per distinct area (batch the calls) —
   `area=<area>, aggregate_field="reserve_price_num",
   aggregations=["avg","median"], limit=0` — so each reserve can be framed
   against its local market.
3. Cite the auction_ids in your recommended order in the reply — the UI
   matches panel follows your citations automatically (no tool call).

## Output (in chat)

A markdown table, one column per property:

- **Attribute rows**: reserve price, reserve vs. area median (from step 2),
  EMD, price per sq.ft (when `total_area` is present), days to deadline,
  bank, auction type, re-auction count / previous reserve (note any price
  drop %).

Then 2–4 sentences: strongest and weakest value against local comparables,
soonest deadline pressure, the single best overall pick with the facts that
justify it, and any unique red flag per property (e.g. multiple failed
re-auctions, unusually high EMD ratio).

**Never invent numbers** — every cell comes from `get_auction_detail` /
`search_auctions`. Missing field → write "—", don't guess.
