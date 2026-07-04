# Mode: report

A personalised, investor-facing brief — delivered as in-chat markdown. No
PDF / file output.

## Input — investor profile (parsed from the message)

- Risk appetite (conservative / balanced / aggressive), budget ceiling,
  preferred city/area, asset category, and any must-haves.
- Ask ONE brief clarifying question only if budget or risk is missing AND it
  would change the shortlist; otherwise state your assumptions and proceed.

## Process

1. `search_auctions` filtered to the profile (price ceiling, city/area,
   asset_category, future-only). Carry the scope on follow-ups.
2. One `search_auctions` aggregation over the same scope
   (`aggregate_field="reserve_price_num"`,
   `aggregations=["avg","median","p25"]`, `limit=0`) to anchor value.
3. Rank the candidates by fit to the profile using row facts:
   - conservative → below-median reserve, longer runway to deadline, fewer
     re-auctions, established bank;
   - balanced → best reserve-vs-median value;
   - aggressive → deepest re-auction price drops (`previous_reserve_price`
     vs `reserve_price`) and soonest deadlines (less competition time).
4. Cite the shortlist's auction_ids in ranked order in the brief — the UI
   matches panel follows your citations automatically (no tool call).

## Output (in chat)

A markdown brief:

- **Profile recap** — the assumptions you used (so the user can correct them).
- **Shortlist table** — top N (default 5): reserve, vs. area median, EMD,
  area, deadline, re-auction history, and a one-line "why it fits".
- **Top pick** — short paragraph: the facts behind the ranking, a bid-range
  *framing* (anchored on the reserve and the scope's median — never a
  guaranteed number), and the key risk to check.
- **What to verify** — 3–5 due-diligence items; point to the `deep-research`
  mode for a full work-up on any one property.

**Never invent numbers** — all figures come from `search_auctions` /
`get_auction_detail`.
