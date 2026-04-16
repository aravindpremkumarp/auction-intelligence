# Mode: report

Generate investor-facing reports.

## Report types

### 1. Property Investment Brief
Deep-dive for a single auction: scoring breakdown, comparables, risk factors,
recommended bid range. Output: `reports/output/brief_{auction_id}.pdf`.

### 2. Area Market Report
All auctions in a city/area: price trends, bank distribution, type mix.
Output: `reports/output/area_{city}_{YYYYMMDD}.pdf`.

### 3. Portfolio Shortlist
Top N scored auctions matching an InvestorProfile, side-by-side.
Output: `reports/output/shortlist_{profile}_{YYYYMMDD}.pdf`.

### 4. Due Diligence Checklist
Generated from `deep-research` mode output: document gaps, legal checks, action items.
Output: `reports/output/dd_{auction_id}.pdf`.

## Tech

- Markdown templates in `reports/templates/`
- Rendered to PDF via `weasyprint` (Python-native — no Node.js dependency)
- Never invent statistics — all tables must be generated from Neo4j queries
