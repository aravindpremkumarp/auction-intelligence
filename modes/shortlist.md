# Mode: shortlist

Filter scored auctions against an `InvestorProfile` to produce a shortlist.

## Input

- `profile_name` — references `InvestorProfile` node in Neo4j
- Optional score threshold (default: matches profile's risk_tolerance)

## Process

1. Load `InvestorProfile` (budget range, preferred cities, property types, risk tolerance).
2. Query `InvestmentTracker` nodes where `state = 'SCORED'` and composite_score meets threshold:
   - conservative → ≥ 80
   - moderate → ≥ 70
   - aggressive → ≥ 55
3. Filter by profile criteria (budget, city, property_type).
4. Return candidate list; **ask the user** to confirm each transition to `SHORTLISTED`.

## Output

Ranked table with explicit confirmation prompt per row.
