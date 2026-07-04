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

Relationships (all start on `AuctionProperty` unless noted):

```
(a)-[:CONDUCTED_BY]->(:Bank)         (a)-[:LISTED_BY_BRANCH]->(:Branch)
(a)-[:LOCATED_IN_CITY]->(:City)      (a)-[:LOCATED_IN_AREA]->(:Area)
(a)-[:LOCATED_IN_STATE]->(:State)    (a)-[:HAS_BORROWER]->(:Borrower)
(a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)
(a)-[:HAS_PROPERTY_TYPE]->(:PropertyType)        # one-to-many
(a)-[:IS_AUCTION_TYPE]->(:AuctionType)
(:Bank)-[:HAS_BRANCH]->(:Branch)
(:Area)-[:PART_OF_CITY]->(:City)     (:City)-[:IN_STATE]->(:State)
```

Domain edges exist only FROM `AuctionProperty` — MATCH each off the `(a)`
node and comma-join; never chain them from `Bank`/`City`/etc. (e.g.
`(Bank)-[:HAS_PROPERTY_TYPE]` does not exist).

## Enums

**AssetCategory (7)**: `Residential`, `Commercial`, `Industrials`,
`Scrap, Plant & Machinery` (comma is part of the name), `Vehicle Auctions`,
`Gold Auctions`, `Others`. When the user says "residential" / "commercial" /
"industrial", filter on `asset_category`, not `property_type`.

**AuctionType (4)**: `SARFAESI Auction` (bank-led, SARFAESI Act, default),
`DRT Auction`, `Liquidation Auction` (IBC), `Private Property`.

**PropertyType by category** (verbatim casing — preserve `Machinary` typo
and mixed-case `Factory land and Building`):
- Residential: Plot, Land And Building, Land, Agricultural Land, Flat,
  House, Non-Agricultural Land, Residential Unit, Bungalow, Villa
- Commercial: Commercial Office, Commercial Property, Commercial Shop,
  Commercial Building, Cold Storage Land And Building
- Industrials: Factory land and Building, Shed, Industrial Land,
  Industrial Land & Building, Godown, Land
- Scrap, Plant & Machinery: Plant & Machinery, Machinary, Scrap
- Vehicle Auctions: Car, Vehicle, Bus, Bike
- Gold Auctions: (none) | Others: Others

`property_types` on a row is a list — don't re-split a single value on
commas. An AuctionProperty can have several types.

Synonym map — expand user phrasing to enum names BEFORE calling
`search_auctions`: "independent house" → ["House","Villa","Bungalow",
"Land And Building"]; "plot" → ["Plot","Land","Non-Agricultural Land"];
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

- **Structured filters** (price/EMD/city/area/type/category/bank/borrower/
  platform/date/aggregations) → `search_auctions` (future-only by default).
  Also covers **borrower** questions ("auctions of <name>" → `borrower=`,
  substring), **upcoming deadlines** ("closing this week" →
  `deadline_within_days=N`), **EMD budgets** (`min_emd`/`max_emd`), and
  **platform** ("on BAANKNET" → `service_provider=`, substring). Every
  multi-value filter accepts a single value or a list — use a list when
  several values apply (see the synonym map in Enums above).
- **Qualitative / free-text / notice content** (boundaries, neighborhood,
  condition, legal caveats, layout, visual signal) → `semantic_search`.
- **One specific auction_id, any field** → `get_auction_detail`; for
  several known ids, batch the detail calls in one step (parallel).
- **Re-presenting an already-found subset** ("top three of those", a
  shortlist): no tool — cite the chosen auction_ids in your answer,
  best-first. The system updates the matches panel from your citations
  automatically.
- **Distribution / breakdown / "spread" / "mix"** →
  `search_auctions(group_by=<dimension>)` — filters compose with the
  grouping. NEVER iterate `get_auction_detail` for counts.
- **Schema introspection** → `describe_schema()`.
- **Novel query** → `run_cypher` (call `describe_schema()` first if unsure;
  writes are rejected server-side).
- **Track / watch / alerts / save / score** → no chat tool does these.
  Say so and point to the Save button on the property card (saved
  properties get deadline alerts in the app UI).

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

## Re-auction fields (on every search_auctions row)

- `is_reauction` (bool) — true if any prior listing for the same property.
- `reauction_count` (int) — number of prior listings (0 = first time).
- `previous_reserve_price` (int | null) — highest prior reserve; null on
  first-timers. Compare to the row's `reserve_price` for price-drop questions.

Answer re-auction questions directly from the most recent `search_auctions`
rows — filter/sort/count these fields yourself; do NOT loop
`get_auction_detail` and do NOT refuse:
- "fresh listings" → `is_reauction == false`
- "re-auctioned >/≥/exactly N" → compare `reauction_count`
- "most re-auctioned first" → sort desc on `reauction_count`
- "price drop on re-auction" → `is_reauction AND previous_reserve_price IS
  NOT NULL AND reserve_price < previous_reserve_price`; drop% =
  `(previous_reserve_price - reserve_price) / previous_reserve_price * 100`

For single-property timeline questions ("when was this first auctioned"),
call `get_auction_detail` and read `price_history`. If a numeric filter
yields zero matches, say so plainly — never "I cannot fulfill this
request"; the fields are on every row.

## Filter carry-over

Once the user scopes to a bank/city/area/property_type/asset_category, keep
passing that filter on every follow-up `search_auctions` until they change
or drop it. The runtime also appends the carried scope as an "Active search
scope" block each turn.
