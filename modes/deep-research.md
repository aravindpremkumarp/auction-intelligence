# Mode: deep-research

Full due diligence on a single auction property, adapted from career-ops
sourced interview prep.

## Input

- `auction_id`

## Process (7 steps)

1. **Legal framework check** — SARFAESI compliance indicators, DRT status from `auction_type`.
2. **Encumbrance risk** — assess from `possession_type`, borrower history (other auctions tied to same borrower).
3. **Market comparables** — Neo4j: same area, same property_type, last 6 months of auctions.
4. **Location intelligence** — web-sourced area development signals (cite sources).
5. **Document completeness audit** — list downloaded files, identify gaps.
6. **Estimated value vs. reserve** — compute area avg price, flag % delta.
7. **Red flag summary** — top 3 risks with mitigation notes.

## Critical Constraint

**Never invent statistics.** All claims must cite a source (Neo4j query, web URL, document page). When data is missing, explicitly say so.

## Output

Markdown report at `reports/output/dd_{auction_id}.md`, transition tracker to `RESEARCHING` (requires user confirmation).
