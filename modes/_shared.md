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

Domain edges exist only FROM `AuctionProperty` — MATCH each off the `(a)`
node and comma-join; never chain them from `Bank`/`City`/etc. (e.g.
`(Bank)-[:HAS_PROPERTY_TYPE]` does not exist).

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
- `auction_start_dt`, `auction_end_dt`, `application_deadline_dt` are
  Neo4j ZONED DATETIME (UTC) — NOT strings.
- Components: `.year .month .day .hour .dayOfWeek (1=Mon..7=Sun) .quarter`.
- `datetime()` = now; `datetime() + duration({days: 7})` for +7d.
- Gaps: `duration.between(a, b).days` or
  `duration.inSeconds(a, b).seconds / 3600`.
- Calendar-day equality: `date(a.dt) = date($other)`.
- **Never** compare a DATETIME column to a raw ISO string (ZONED-vs-LOCAL
  silently returns zero); wrap it: `WHERE a.auction_start_dt >=
  datetime($iso)`. Only matters in `run_cypher` — structured tools pass
  real datetimes.
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
- **Novel shapes** none of the above express → `run_cypher`
  (`describe_schema()` first if unsure).
- **Re-presenting an already-found subset** ("top three of those"): no
  tool — cite the chosen auction_ids in your answer, best-first; the
  system updates the matches panel from your citations automatically.

## Zero-result protocol

An empty result is usually the answer, not a problem to search around.
Read the result's `hint` / `past_matches` and do exactly what it says. A
retry is worth it only when the hint points to one (e.g. `past_matches`
above 0 → retry once with `include_past=true`); a hintless zero means
report it — don't loosen filters to manufacture matches. Never rerun
a rephrased `semantic_search` once a result carries a `hint`, and never
split a list filter into per-value calls (lists already OR-match). Hard
cap: two follow-ups — then say plainly what you found nothing for and offer
the closest alternative.

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
