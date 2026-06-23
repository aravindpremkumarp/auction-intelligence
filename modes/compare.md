# Mode: compare

Side-by-side comparison of 2–5 auction properties, delivered as a markdown
table **in your reply**. There are no files to download.

## Input

- 2 to 5 `auction_id`s — from the user, or the current matches set.

## Process

1. `score_auction(auction_id)` for EACH id — returns the 10-dimension
   framework (composite 0–100, grade, and per-dimension `score` + `rationale`).
2. `get_auction_detail(auction_id)` for EACH id — pull the comparison
   attributes: reserve price, EMD, `total_area`, city/area, bank, deadline,
   and the re-auction fields (`reauction_count`, `previous_reserve_price`).
3. `select_properties([...])` with the ids in your recommended order so the UI
   matches panel mirrors the comparison.

## 10-dimension legend (for the table rows)

| Dim | Name | Weight |
|-----|------|--------|
| A | Price Attractiveness | 20% |
| B | Location Quality | 15% |
| C | Legal Clarity | 15% |
| D | Bank Reliability | 10% |
| E | Property Condition | 10% |
| F | Timeline Urgency | 10% |
| G | Due Diligence Ease | 5% |
| H | Area Price Trend | 5% |
| I | Competition Risk | 5% |
| J | Yield Potential | 5% |

## Output (in chat)

A markdown table, one column per property:

- **Header**: composite score + grade.
- **Scoring rows**: one per dimension (A–J).
- **Attribute rows**: reserve price, EMD, price per sq.ft (when `total_area`
  is present), days to deadline, bank, re-auction count / previous reserve.

Then 2–4 sentences: best/worst on the major dimensions, the single best
overall pick with the scores that justify it, and any unique red flag per
property.

**Never invent numbers** — every cell comes from `score_auction` /
`get_auction_detail`. Missing field → write "—", don't guess.
