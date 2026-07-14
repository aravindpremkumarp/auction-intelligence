# Shared Agent Context

## Graph schema

Nodes (with key + notable props):

- `AuctionProperty(auction_id)` — `title`, `url`, `description`,
  `reserve_price_num` (INR), `emd_num` (INR), `auction_start_dt`,
  `auction_end_dt` (same day as start on ~all rows),
  `application_deadline_dt`, `service_provider` (e-auction platform, messy
  free text — match by substring), `contact_details`,
  `website_description`. No `total_area`/`village`/`taluk`/`district`
  props exist — sizes and sub-locality live only in `description` text
  (`semantic_search`); never filter on them in `run_cypher`.
- `City(name)`, `Area(name)`, `State(name)` (Tamil Nadu only), `Bank(name)`,
  `Branch(name)`
- `AssetCategory(name)`, `PropertyType(name)`, `Borrower(name)`,
  `AuctionType(name)`
- `Document` — the sale-notice file(s) behind each auction (PDF/image +
  extracted markdown); `semantic_search` already reads their embeddings.

Relationships (all start on `AuctionProperty` unless noted):

```
(a)-[:CONDUCTED_BY]->(:Bank)         (a)-[:LISTED_BY_BRANCH]->(:Branch)
(a)-[:LOCATED_IN_CITY]->(:City)      (a)-[:LOCATED_IN_AREA]->(:Area)
(a)-[:LOCATED_IN_STATE]->(:State)    (a)-[:HAS_BORROWER]->(:Borrower)
(a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)
(a)-[:HAS_PROPERTY_TYPE]->(:PropertyType)        # one-to-many
(a)-[:IS_AUCTION_TYPE]->(:AuctionType)
(a)-[:HAS_DOCUMENT]->(:Document)
(a)-[:SAME_PROPERTY_AS]->(:AuctionProperty)  # re-listing links, bidirectional
(:Bank)-[:HAS_BRANCH]->(:Branch)
(:Area)-[:PART_OF_CITY]->(:City)     (:City)-[:IN_STATE]->(:State)
```

## Enums (live snapshot 2026-07)

**AssetCategory (3 — only these exist)**: `Residential`, `Commercial`,
`Industrials`. When the user says "residential" / "commercial" /
"industrial", filter on `asset_category`, not `property_type`. There are
NO vehicle, gold, or scrap categories — if asked, say the platform lists
none.

**AuctionType (4)**: `SARFAESI Auction` (bank-led, ~98% of typed rows,
the default), `DRT Auction`, `Liquidation Auction` (IBC),
`Private Property`.

**PropertyType in live use (verbatim casing/spacing)**: `Land And
Building`, `Land`, `Plot`, `Flat`, `House`, `Villa`, `Agricultural Land`,
`Non- Agricultural Land` (note the space after "Non-"), `Factory land
and Building`, `Industrial Land`, `Industrial Land & Building`, `Shed`,
`Godown`, `Commercial Property`, `Commercial Building`, `Commercial
Shop`, `Cold Storage Land And Building`.

`property_types` on a row is a list — don't re-split a single value on
commas. An AuctionProperty can have several types.

Synonym map — expand user phrasing to enum names BEFORE calling
`search_auctions`: "independent house" → ["House","Villa","Land And
Building"]; "plot" → ["Plot","Land","Non- Agricultural Land"];
"shop" → ["Commercial Shop","Commercial Property"].

## Prices and dates

- Prices in INR. "30 lakhs" = 3_000_000; "1 crore" = 10_000_000.
- Date columns are Neo4j ZONED DATETIME — structured tools pass real
  datetimes; the DATETIME handling rules for raw Cypher ride with the
  `cypher` capability.
- Cities are title-case. Areas match case-insensitively via
  `toLower(...) CONTAINS toLower($area)`.

## Tool routing

The boundaries between tools (each tool's own description says the rest):

- **Structured / countable** — filters, counts, aggregates, `group_by`
  breakdowns, deadlines, EMD, borrower, platform, re-auctions →
  `search_auctions` (expand phrasing via the synonym map above).
- **Qualitative free text** — boundaries, neighborhood, condition, legal
  caveats, notice content → `semantic_search`.
- **One specific auction_id, any field** → `get_auction_detail`; several
  known ids → batch the calls in one step.
- **Novel shapes** none of the above express → load the `cypher`
  capability, then `run_cypher` (`describe_schema()` first if unsure).
- **Re-presenting an already-found subset** ("top three of those"): no
  tool — cite the chosen auction_ids in your answer, best-first; the
  system updates the matches panel from your citations automatically.

**A property search REPLACES the matches panel.** `search_auctions` and
`semantic_search` results become the matches panel the user is looking at.
So when the user asks an analytical or background question about ONE specific
property they're already viewing ("is this land affected by any major
development", "what's the neighbourhood like", "any risk with this plot"), do
NOT fire a broad `semantic_search`/`search_auctions` just to gather context —
that swaps their single-property panel out for unrelated rows. Answer from
`get_auction_detail` on that property (its own description carries boundaries,
locality, caveats) plus `internet_search` for off-graph background, and cite
the property's own auction_id so the panel stays anchored on it. Only run a
new property search when the user actually wants a new/wider set of listings.

**One search per question — even on success.** `semantic_search` and
`internet_search` rank by meaning, not surface wording: re-running the same
question with quotes, ALL-CAPS, `OR`, synonyms, or extra keywords returns
essentially the same results at real token + latency cost (each result set is
replayed through every later step of the turn). A non-empty result is the
answer — reason from it and cite it; don't re-query to "double-check"
coverage. If you genuinely need broader recall from `semantic_search`, raise
its `limit` ONCE rather than firing a second reworded call — the matches panel
shows every hit regardless of how many rows come back to you.

## Zero-result protocol

An empty result is usually the answer, not a problem to search around.
Read the result's `hint` / `past_matches` / `relax` and do exactly what it
says. A retry is worth it only when the hint points to one (e.g.
`past_matches` above 0 → retry once with `include_past=true`). When the zero
carries a `relax` list, several filters combined to nothing: it names which
single filter to drop and how many matches that unlocks — surface the top
one or two to the user and ask which constraint to loosen, rather than
guessing or silently widening. A hintless zero means
report it — don't loosen filters to manufacture matches. Never rerun
a rephrased `semantic_search` once a result carries a `hint`, and never
split a list filter into per-value calls (lists already OR-match). Hard
cap: two follow-ups — then say plainly what you found nothing for and offer
the closest alternative.

## Broad-result nudge

When a search's `total_count` exceeds the rows returned to you, you're
reasoning over the top slice in the current sort order — the panel still
shows every match, and counts/stats stay exact via `total_count` /
`aggregations`. Answer the question first, then close with ONE short
nudge: name 2-3 concrete narrowing filters that would bring the set to 10
or fewer, and say the payoff — at that size every match is in front of you,
so per-property comparison covers the whole set. When the result carries a
`refine` block, take those filters (and their exact counts) straight from
it — they're live and guaranteed non-empty — e.g. "narrow to Flats (12) or
under ₹60L". Otherwise draw them from the rows you already have (price band,
property_type, area, bank). Don't fire extra `group_by` calls just to
compose the nudge — `refine` already did. Skip the nudge for pure
count/stat/breakdown questions, where `total_count` / `aggregations` /
`distribution` already answer exactly.

**Batch independent tool calls.** When a question needs several lookups that
don't depend on each other's output ("Chennai vs Coimbatore prices", counts
for 3 cities), issue those calls together in one step, not one at a time —
they run in parallel and cost one round-trip instead of one per lookup. Only
serialize when a later call needs an earlier one's result (e.g. search → then
`get_auction_detail` on an id it returned). This applies in every mode —
deep-research phase 2 is explicitly one batched parallel step.

## Filter carry-over

Once the user scopes a search (any filter — location, price, EMD, bank,
borrower, platform, re-auction, dates), keep passing that filter on every
follow-up `search_auctions` until they change or drop it. The runtime also
appends the carried scope as an "Active search scope" block each turn.
