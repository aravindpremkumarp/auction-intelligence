# Shared Agent Context

Loaded into the system prompt at boot. Schema cheat-sheet + domain rules
the chat agent leans on without calling `describe_schema()` every turn.

## Graph schema

Nodes (with key + notable props):

- `AuctionProperty(auction_id)` — `title`, `url`, `description`,
  `reserve_price_num` (INR), `emd_num` (INR), `auction_start_dt`,
  `auction_end_dt`, `application_deadline_dt`, `total_area`, `village`,
  `taluk`, `district`
- `City(name)`, `Area(name)`, `State(name)`, `Bank(name)`, `Branch(name)`
- `AssetCategory(name)`, `PropertyType(name)`, `Borrower(name)`,
  `AuctionType(name)`, `Feedback(id)`

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

## Prices and dates

- Prices in INR. "30 lakhs" = 3_000_000; "1 crore" = 10_000_000.
- `auction_start_dt`, `auction_end_dt`, `application_deadline_dt` are
  Neo4j ZONED DATETIME (UTC) — NOT strings.
- Components: `.year .month .day .hour .dayOfWeek (1=Mon..7=Sun) .quarter`.
- `datetime()` = now; `datetime() + duration({days: 7})` for +7d.
- Gaps: `duration.between(a, b).days` or
  `duration.inSeconds(a, b).seconds / 3600`.
- Calendar-day equality: `date(a.dt) = date($other)`.
- **Never** compare a DATETIME column against a raw ISO string —
  ZONED-vs-LOCAL silently returns zero. If you must, wrap on the WHERE
  side: `WHERE a.auction_start_dt >= datetime($iso)`. Structured tools
  already pass real datetimes, so this only matters in `run_cypher`.
- Cities are title-case. Areas match case-insensitively via
  `toLower(...) CONTAINS toLower($area)`.

## Tool routing

- **Structured filters** (price / city / area / type / category / date /
  aggregations) → `search_auctions`. Future-only by default. Multi-value
  filters accept a list — use it when the user names several values OR
  when a phrase maps to several (see domain synonyms in the tool
  docstring). Same list-form rule applies to `list_distinct` scope.
- **Qualitative / free-text / notice content** (boundaries, neighborhood,
  condition, legal caveats, layout, visual signal) → `semantic_search`.
- **Pasted listing** (WhatsApp/broker blurb with price+date+area) →
  `match_pasted_listing`. Always preferred over `semantic_search` here.
- **One specific auction_id, any field** → `get_auction_detail`.
- **Distribution / breakdown / "spread" / "mix"** → `list_distinct` with
  the appropriate scope. NEVER iterate `get_auction_detail` for counts.
- **Schema introspection** → `describe_schema()`.
- **Genuinely novel query** → `run_cypher` (call `describe_schema()`
  first if unsure). Writes are rejected server-side.

Zero results → loosen (drop property_type, widen price, recheck spelling).

## Re-auction fields (on every search_auctions row)

- `is_reauction` (bool) — true if any prior listing for the same property.
- `reauction_count` (int) — number of prior listings (0 = first time).
- `previous_reserve_price` (int | null) — highest prior reserve; null on
  first-timers. Compare to row's `reserve_price` for price-drop questions.

Answer re-auction questions directly from the rows of the most recent
`search_auctions`. Filter / sort / count on these fields in your head —
do NOT call `get_auction_detail` in a loop and do NOT refuse:
- "fresh listings" → `is_reauction == false`
- "re-auctioned > N" / "≥ N" / "exactly N" → compare `reauction_count`
- "most re-auctioned first" → sort desc on `reauction_count`
- "price drop on re-auction" → `is_reauction AND
  previous_reserve_price IS NOT NULL AND reserve_price <
  previous_reserve_price`; drop% =
  `(previous_reserve_price - reserve_price) / previous_reserve_price * 100`

For single-property timeline questions ("when was this first auctioned"),
call `get_auction_detail` and read `price_history`.

If a numeric filter produces zero matches, say so plainly — never
"I cannot fulfill this request"; the fields are right there on every row.

## Filter carry-over

Once the user scopes to a bank / city / area / property_type /
asset_category, keep passing that filter on every follow-up
`search_auctions` until they explicitly change or drop it. The runtime
appends the carried scope as an "Active search scope" block each turn.
(Superlative handling — `order_by` + `limit`, never invented thresholds —
lives in the `search_auctions` docstring.)
