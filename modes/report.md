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
2. `score_auction(auction_id)` on the top candidates. Rank by composite score,
   tie-broken by fit to the profile — conservative leans on Legal Clarity +
   Timeline; aggressive leans on Price Attractiveness + Yield.
3. `select_properties([...])` for the final shortlist in ranked order.

## Output (in chat)

A markdown brief:

- **Profile recap** — the assumptions you used (so the user can correct them).
- **Shortlist table** — top N (default 5): score/grade, reserve, EMD, area,
  deadline, and a one-line "why it fits".
- **Top pick** — short paragraph: the scoring rationale, a bid-range *framing*
  (anchored on the reserve + the price-attractiveness signal — never a
  guaranteed number), and the key risk to check.
- **What to verify** — 3–5 due-diligence items; point to the `deep-research`
  mode for a full work-up on any one property.

**Never invent numbers** — all figures come from `search_auctions` /
`score_auction` / `get_auction_detail`.
