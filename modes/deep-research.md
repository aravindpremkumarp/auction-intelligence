# Mode: deep-research

Full due-diligence on a SINGLE auction property, delivered as an in-chat
markdown report. No files are written.

## Input

- One `auction_id`.

## Process — 3 phases, minimum round-trips

**Phase 1 — snapshot (one call).** `get_auction_detail(auction_id)`: the
full record, `price_history` (re-auction timeline, prior reserves), the
document list, and `description_completeness`.

**Phase 2 — evidence sweep (ONE step; issue ALL of these together, in
parallel — none depends on another).** Using phase 1's fields:

- `search_auctions(borrower=<borrower>, include_past=true)` — other
  auctions tied to the same borrower.
- `semantic_search` over notice content for charge / encumbrance /
  possession language on this property.
- `search_auctions` scoped to the same area + property_type with
  `aggregate_field="reserve_price_num"`, `aggregations=["avg","median"]` —
  comparables and the local market anchor.
- `internet_search` for area development / civic / connectivity signals.

**Phase 3 — write the report (no further calls).** Sections:

1. **Snapshot** — price, EMD, key dates, re-auction history.
2. **Legal framework** — what `auction_type` (SARFAESI / DRT / Liquidation /
   Private) implies for the buyer.
3. **Encumbrance / borrower risk** — from the borrower and semantic results.
4. **Market comparables** — reserve vs. the area avg/median; price-drop
   signal from `previous_reserve_price`.
5. **Location intelligence** — web findings, cited inline as [1], [2]…
6. **Document completeness** — documents present vs. missing, plus the
   `description_completeness` signal.
7. **Value vs. reserve & red flags** — top 3 risks, each with a mitigation.

## Critical constraint

**Never invent statistics.** Every claim cites a source — a tool result, a
numbered web URL, or a document. When data is missing, say so explicitly.

## Output (in chat)

A structured markdown report: header (id, title, reserve, deadline), the
seven sections above, then a **Red-flag summary** (top 3). If the user asks
to track or get alerts for the property, point them to the Save button on
the property card — saved properties get deadline alerts in the app; chat
has no tracking action.
