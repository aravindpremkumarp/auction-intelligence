# Shared Evaluation Framework

All modes share this scoring taxonomy. When evaluating an auction, reason across
these ten dimensions before recommending.

## The 10 Scoring Dimensions

| Dim | Name | Weight | What to assess |
|-----|------|--------|---------------|
| A | Price Attractiveness | 20% | Reserve price vs. comparable properties in same area |
| B | Location Quality | 15% | City tier, area desirability, auction density |
| C | Legal Clarity | 15% | Possession type (Physical > Symbolic > Constructive), document completeness, clean survey numbers |
| D | Bank Reliability | 10% | Bank's historical auction volume and success |
| E | Property Condition | 10% | Asset category, property type, description quality |
| F | Timeline Urgency | 10% | Days until application deadline |
| G | Due Diligence Ease | 5% | Download completeness, description score |
| H | Area Price Trend | 5% | Historical price direction in same area |
| I | Competition Risk | 5% | Number of similar concurrent auctions |
| J | Yield Potential | 5% | EMD-to-price ratio, estimated rental yield |

## Decision Thresholds

- **85+ (A/A+)** → Strong buy — bid immediately
- **70–84 (B)** → Worth pursuing — complete due diligence
- **55–69 (C)** → Selective — only if matches specific criteria
- **Below 55 (D/F)** → Skip

## Human-in-the-Loop Principle

AI recommends scores, shortlists, and next actions. **Users confirm state
transitions** past SCORED. Never auto-submit bids or mark states without user
approval.

## Data Sources

- Primary: Neo4j knowledge graph (`cc513ea9` database) — 3,391 Tamil Nadu records
- Enrichment: Vision LLM OCR output in `pipeline/output/normalized.jsonl`
- Portals: eauctionsindia.com, ibapi.in, bankauctions.in, bankeauctions.com, findauction.in
