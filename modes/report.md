# Mode: report

When this mode is active, the user wants a **personalized investment
brief** for ONE auction, tuned to their investor profile.

## Input parsing

Expect the user's message to contain (in any order):

- An `auction_id` (required).
- An investor profile. Parse these hints from free text:
  - `budget_min`, `budget_max` (INR — "under 50 lakhs", "30L to 1.2 crore").
  - `preferred_cities` (list — "Chennai", "Kanchipuram and Coimbatore").
  - `preferred_property_types` (list — "flat or plot").
  - `risk_tolerance` — one of `conservative`, `moderate`, `aggressive`
    (infer from words: "safe", "low risk" → conservative; "yield",
    "high upside" → aggressive).

If the `auction_id` is missing, ask for it. If the profile is missing
ANY field, proceed with a neutral default and note that assumption in
the "Profile" section of the report.

## Workflow

1. Run the full `deep-research` workflow (steps 1–7 from
   `modes/deep-research.md`). The resulting scoring + comparables +
   risks feed this mode.
2. Re-weight the scoring dimensions in your prose interpretation based
   on `risk_tolerance` — do NOT mutate the raw scores returned by
   `score_auction`, just emphasize different rationales:
   - `conservative` → lean on `legal_clarity`, `bank_reliability`,
     `due_diligence_ease`, `property_condition`.
   - `moderate` → balanced; use the composite score as-is.
   - `aggressive` → emphasize `price_attractiveness`, `yield_potential`,
     `area_price_trend`.
3. Compute "fit" against the profile:
   - `budget_fit`: `in range` if `budget_min ≤ reserve_price ≤ budget_max`,
     `below` if cheaper, `over` if pricier (and by how much).
   - `city_fit`: `yes` if the auction's city is in `preferred_cities`
     (case-insensitive); otherwise `no`.
   - `type_fit`: `yes` if any `property_type` intersects
     `preferred_property_types`; otherwise `no`.

## Output format

```
# Investment brief — <auction_id>

## Profile
- Budget: ₹X – ₹Y  (or "not specified → assumed flexible")
- Preferred cities: …  (or "any")
- Preferred property types: …  (or "any")
- Risk tolerance: …  (or "moderate — inferred")

## Summary
<One paragraph tailored to the profile — do not just restate the
deep-research summary. Lead with whether this property fits the
investor's stated constraints.>

## Key facts
<same block as deep-research>

## Scoring (composite: XX / 100 — grade G)
<same structure as deep-research, but the "top drivers" and "weakest
dimensions" narrative should reference the risk-tolerance lens from
step 2 above. E.g. for a conservative investor, flag a weak
legal_clarity score prominently even if the composite is high.>

## Fit to your profile
- Budget fit: <in range / below / over by ₹X>
- City fit: <yes / no — explain>
- Property-type fit: <yes / no — explain>
- Risk alignment: <how this property matches the user's tolerance>

## Comparables
<Top 3 similar auctions that are ALSO within the investor's budget
(filter the comparables from find_similar_properties). If none, say
"no comparables within budget".>

## Risks
<Top 3 risks from deep-research, re-ranked by the risk-tolerance lens.
Conservative investors see legal and documentation risks first;
aggressive investors see price and competition risks first.>

## Recommendation
One of: **Strong buy** / **Worth pursuing** / **Selective** / **Skip**.
Follow with 2–3 sentences justifying the choice AGAINST THE PROFILE —
not the generic composite score. End with one concrete next action.
```

## Strict rules

- **Never invent profile inputs.** If the user hasn't given a budget,
  do not assume one — say "not specified" and proceed.
- **Never auto-mutate state.** This mode produces a report only. If the
  user follows up asking to shortlist or transition the tracker, tell
  them that requires explicit confirmation and is not wired yet.
- Cite every number back to a tool call.
- Keep the total output under ~600 words.
