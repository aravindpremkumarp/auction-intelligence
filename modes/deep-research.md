# Mode: deep-research

Full due-diligence on a SINGLE auction property, delivered as an in-chat
markdown report. No files are written.

## Input

- One `auction_id`.

## Process (7 steps — map each to a real tool, never invent data)

1. **Snapshot** — `get_auction_detail(auction_id)` for the full record
   + `price_history` (re-auction timeline, prior reserves).
2. **Legal framework** — read `auction_type` (SARFAESI / DRT / Liquidation /
   Private) from the detail and explain what it implies for the buyer.
3. **Encumbrance / borrower risk** — `search_auctions(borrower=borrower_name)`
   for other auctions tied to the same borrower; `semantic_search` over notice
   content for charge / encumbrance / possession language.
4. **Market comparables** — `search_auctions` scoped to the same area +
   property_type, plus its avg/median aggregation over that scope; weigh
   the reserve against peers and use the re-auction fields
   (`previous_reserve_price`) for any price-drop signal.
5. **Location intelligence** — `internet_search` for area development / civic /
   connectivity signals. Cite every claim inline as [1], [2]…
6. **Document completeness** — from `get_auction_detail`, note which documents
   are present vs. missing and the `description_completeness` signal.
7. **Value vs. reserve & red flags** — compare reserve to the area
   average/median (step 4's aggregation), then list the top 3 risks, each
   with a mitigation.

## Critical constraint

**Never invent statistics.** Every claim cites a source — a tool result, a
numbered web URL, or a document. When data is missing, say so explicitly.

## Output (in chat)

A structured markdown report: header (id, title, reserve, deadline), the
seven sections above, then a **Red-flag summary** (top 3). If the user asks
to track or get alerts for the property, point them to the Save button on
the property card — saved properties get deadline alerts in the app; chat
has no tracking action.
