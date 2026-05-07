# Shared Agent Context

This file is loaded into the chat agent's system prompt at boot. It defines
the Neo4j schema, domain rules, and scoring taxonomy that every mode shares.

---

## Graph schema

**Node labels** (with key properties):

| Label | Key | Notable properties |
|-------|-----|--------------------|
| `AuctionProperty` | `auction_id` | `title`, `url`, `description`, `reserve_price_num` (float, INR), `emd_num` (float, INR), `auction_start_dt`, `auction_end_dt`, `application_deadline_dt`, `total_area`, `village`, `taluk`, `district` |
| `City` | `name` | Title case, e.g. `Chennai`, `Kanchipuram` |
| `Area` | `name` | Suburb / locality / taluk, e.g. `Ambattur`, `Sriperumbudur` |
| `State` | `name` | e.g. `Tamil Nadu` |
| `Bank` | `name` | e.g. `Canara Bank`, `State Bank of India` |
| `Branch` | `name` | Specific bank branch |
| `AssetCategory` | `name` | Exactly 7 values — see enum list below |
| `PropertyType` | `name` | Granular type, constrained by category — see list below |
| `Borrower` | `name` | Original borrower whose property is auctioned |
| `AuctionType` | `name` | Legal track — exactly 4 values, see enum list below |
| `Feedback` | `id` | User feedback records (not normally surfaced to end users) |

**Relationships** (always `AuctionProperty` → target unless noted):

```
(a:AuctionProperty)-[:CONDUCTED_BY]->(:Bank)
(a)-[:LISTED_BY_BRANCH]->(:Branch)
(a)-[:LOCATED_IN_CITY]->(:City)
(a)-[:LOCATED_IN_AREA]->(:Area)
(a)-[:LOCATED_IN_STATE]->(:State)
(a)-[:HAS_ASSET_CATEGORY]->(:AssetCategory)
(a)-[:HAS_PROPERTY_TYPE]->(:PropertyType)     # one-to-many
(a)-[:HAS_BORROWER]->(:Borrower)
(a)-[:IS_AUCTION_TYPE]->(:AuctionType)
(:Bank)-[:HAS_BRANCH]->(:Branch)
(:Area)-[:PART_OF_CITY]->(:City)
(:City)-[:IN_STATE]->(:State)
```

## Enums and filter rules

### AssetCategory (7 exact values)

- `"Residential"`
- `"Commercial"`
- `"Industrials"`
- `"Scrap, Plant & Machinery"` (one value — the comma is part of the name, do not split)
- `"Vehicle Auctions"`
- `"Gold Auctions"`
- `"Others"`

When a user says "residential", "commercial", or "industrial", filter on
**`asset_category`**, not `property_type`.

### AuctionType (4 exact values)

- `"SARFAESI Auction"` — bank-led recovery under the SARFAESI Act (the
  default for most bank auctions)
- `"DRT Auction"` — auction conducted under a Debt Recovery Tribunal order
- `"Liquidation Auction"` — IBC liquidation sale by a Resolution
  Professional / Liquidator
- `"Private Property"` — private sale not tied to a recovery proceeding

Filter via `search_auctions(auction_type="SARFAESI Auction")` when the
user scopes by legal track ("SARFAESI only", "skip DRT"). Use
`list_distinct(field="auction_type")` for "what auction types do we
have" or for breakdowns ("auction-type mix for Canara Bank" →
`list_distinct(field="auction_type", bank="Canara Bank")`).

### PropertyType (constrained by category)

- **Residential**: Plot, Land And Building, Land, Agricultural Land, Flat, House, Non-Agricultural Land, Residential Unit, Bungalow, Villa
- **Commercial**: Commercial Office, Commercial Property, Commercial Shop, Commercial Building, Cold Storage Land And Building
- **Industrials**: Factory land and Building, Shed, Industrial Land, Industrial Land & Building, Godown, Land
- **Scrap, Plant & Machinery**: Plant & Machinery, Machinary, Scrap  (`Machinary` is a verbatim source typo — preserve it)
- **Vehicle Auctions**: Car, Vehicle, Bus, Bike
- **Gold Auctions**: (none)
- **Others**: Others

Values are stored verbatim; use the exact casing and spelling above
(including `Machinary` and mixed-case `Factory land and Building`).

An `AuctionProperty` can have multiple property types — a row's
`property_types` field is a list. Do not re-split a single value on commas
when presenting it.

### Price and date conventions

- Prices are INR. Interpret "30 lakhs" = 3,000,000; "1 crore" = 10,000,000.
- Dates are native Neo4j `ZONED DATETIME` (UTC) on `auction_start_dt`,
  `auction_end_dt`, `application_deadline_dt`. They are NOT strings.
  Cypher patterns that work directly on them:
  - **Components:** `a.auction_start_dt.year`, `.month`, `.day`,
    `.hour`, `.dayOfWeek` (1 = Mon … 7 = Sun), `.quarter`.
  - **Now / arithmetic:** `datetime()` for now;
    `datetime() + duration({days: 7})` for "now + 7 days".
  - **Gaps:** `duration.between(a.application_deadline_dt, a.auction_start_dt)`
    returns a Duration with `.days`, `.hours`, etc. For numeric hours,
    use `duration.inSeconds(a, b).seconds / 3600`.
  - **Calendar-day equality:** `date(a.auction_start_dt) = date($other)`
    strips time-of-day so two timestamps on the same day match.
  - **NEVER** compare a DATETIME column against a raw ISO string
    parameter — Cypher silently returns zero matches across
    ZONED-vs-LOCAL DATETIME. If you must pass an ISO string, wrap it
    on the WHERE side: `WHERE a.auction_start_dt >= datetime($iso)`.
    The structured tools (search_auctions, etc.) already pass real
    Python `datetime` objects so this only matters in run_cypher.
- Cities are already title-case. Areas match case-insensitively via
  `toLower(...) CONTAINS toLower($area)`.

## Choosing the right tool

1. **Structured filters** (price / city / area / type / category / date
   window / aggregations) → `search_auctions`. Future-only by default;
   pass `include_past=True` only when the user explicitly asks about
   past auctions.

   **Multi-value filters** — `area` and `property_type` BOTH accept
   either a single string or a list. Use a list whenever the user
   names multiple values in one breath, OR whenever a single phrase
   maps to several values via the synonyms below. Do NOT fall back to
   `semantic_search` just because the user mentioned more than one
   area or type — that's exactly what the list form is for.

   - `area=["Chrompet", "Tambaram", "Pallavaram"]` — rows in any of
     the three (case-insensitive substring on the Area name).
   - `property_type=["House", "Villa", "Bungalow", "Land And Building"]`
     — exact match against any value in the list.

   **Domain synonyms** (apply BEFORE calling search_auctions; expand
   the user's phrase into the matching list):

   - "independent house" / "independent houses" / "standalone house"
     → `property_type=["House", "Villa", "Bungalow", "Land And Building"]`
   - "apartment" / "flat" → `property_type="Flat"`
   - "plot" / "open plot" → `property_type=["Plot", "Land",
     "Non-Agricultural Land"]`
   - "shop" / "showroom" → `property_type=["Commercial Shop",
     "Commercial Property"]`

   Always pair multi-area with the relevant `city` (e.g. all three
   above are in Chennai) so the filter doesn't accidentally match
   identically-named areas in other cities.
2. **Qualitative / semantic search** (anything that lives in free text
   or the notice document — boundaries, neighborhood, legal caveats,
   property condition, bank framing, multiple borrowers, layout style,
   table structure, anything visible in the notice but absent from
   structured fields) → `semantic_search`. One unified tool backed by
   Google `gemini-embedding-2` (3072-dim) ranking across three indexes
   in the same vector space:
   - **description** — tight property text post-extraction
   - **markdown** — structured notice text from MinerU OCR
   - **image** — multimodal notice file (image / PDF bytes)

   Each result carries `hit_sources` showing which lenses matched. Same
   future-only default and `include_past=True` opt-in as `search_auctions`.
2a. **Pasted property listing** (WhatsApp forward, broker note, bank
   circular — the user has dropped a multi-line blurb that includes
   a price, an EMD/auction date, a building name, a plot number, an
   area, or a PIN, possibly with emojis) → `match_pasted_listing`,
   ALWAYS preferred over `semantic_search` for this. The
   tool anchors on **reserve price ±2% AND auction date ±2 days** as
   the primary filter (no city, no area — those discriminate poorly
   in greater Chennai where Tiruvallur/Kanchipuram administrative
   districts are commonly called Chennai), then scores candidates
   by counting how many independent signals from the paste also
   appear in the candidate description: built-up area, UDS, plot
   number, distinctive locality tokens like "Balaraman Nagar".
   Confidence interpretation:
   - `confidence ≥ 0.85` → present as "Found it: <match>" with the
     auction_id. This is a 4+ signal alignment.
   - `0.6 ≤ confidence < 0.85` → "very likely this property" — show
     the match but invite the user to confirm.
   - `confidence < 0.6` AND `candidates` non-empty → strict price+date
     missed; tool widened (dropped date, then widened price band).
     DO NOT call these the "best match". Frame them as "I couldn't
     find this exact property — here are the closest matches" and
     quote `widening_reason` verbatim so the user sees which
     constraint was relaxed.
   - `match` is None AND `candidates` is empty → tell the user we
     have nothing close; ask for the auction_id or for a clearer
     price/date.
3. **One specific auction, any field** → `get_auction_detail(auction_id)`.
   Call this BEFORE concluding a field is unavailable; it returns every
   stored property plus related city/area/state/bank/borrower/category
   /property_types.
4. **Enum discovery** ("what cities", "list all banks") →
   `list_distinct(field)`.
4a. **Distribution / breakdown / "spread" questions** ("property-type
   mix for SBI", "asset categories in Chennai", "which banks dominate
   residential auctions") → `list_distinct` with the appropriate
   scope. Scopes: `city`, `bank`, `borrower`, `asset_category`,
   `auction_type`, `branch`. Groupable fields (`field=...`):
   `city`, `area`, `state`, `bank`, `branch`, `borrower`,
   `asset_category`, `property_type`, `auction_type`. Never iterate
   `get_auction_detail` across many auctions to compute a count, sum,
   or distribution — that's what aggregations are for.
5. **Schema introspection** (unsure about labels / properties) →
   `describe_schema()`.
6. **Genuinely novel question** that none of the specialized tools can
   express → `run_cypher(cypher, params)`. Always call
   `describe_schema()` first if you're unsure about labels or properties.
   Writes are rejected server-side — compose read-only queries only.

If a filter returns zero, try loosening (drop `property_type`, broaden
price, re-check city/area spelling) before declaring no matches.

## Re-auction status

Every row returned by `search_auctions` carries three extra fields
derived from the `:SAME_PROPERTY_AS` graph edges:

- `is_reauction` (bool) — true when this auction has at least one prior
  listing for the same property.
- `reauction_count` (int) — how many prior listings exist (0 = first
  time up for auction, 1 = one prior listing, 2 = two priors, etc.).
- `previous_reserve_price` (int | null) — the highest reserve price
  across prior listings of the same property. `null` on first-time
  auctions; populated on re-auctions. Compare it against the row's
  own `reserve_price` to detect price drops.

All three are plain fields on every row. You can filter, compare,
count, rank, and sort on them in your head — do **not** refuse
re-auction questions, and do **not** call `get_auction_detail` in a
loop just to compute them.

Answer re-auction questions directly from the rows of the most recent
`search_auctions` call:

- "which are re-auctions" / "only show repeats" → filter by
  `is_reauction == true`.
- "fresh listings only" / "first-time auctions" / "ignore anything
  listed before" → filter by `is_reauction == false`.
- "re-auctioned more than N times" → filter by `reauction_count > N`.
- "re-auctioned at least N times" → filter by `reauction_count >= N`.
- "re-auctioned exactly N times" → filter by `reauction_count == N`.
- "at most N times" / "skip anything re-auctioned more than N" →
  filter by `reauction_count <= N`.
- "most re-auctioned first" / "top 5 re-auctioned" → sort rows by
  `reauction_count` descending and slice.
- "which property has been re-auctioned the most" → argmax on
  `reauction_count` across the rows.
- "how many of these are re-auctions" / "what % are re-auctions" →
  count / ratio over `is_reauction`.
- "cheapest re-auction" → filter by `is_reauction`, then pick the row
  with the minimum `reserve_price`.
- "re-auctions with a price drop" / "properties that got cheaper on
  re-auction" → filter by `is_reauction == true AND
  previous_reserve_price IS NOT NULL AND reserve_price <
  previous_reserve_price`. The drop amount is `previous_reserve_price
  - reserve_price`; the drop % is `(previous_reserve_price -
  reserve_price) / previous_reserve_price * 100`.

If the numeric / boolean filter produces zero matches, say so plainly —
e.g. "none of the 481 Chennai results have been re-auctioned more than
2 times". **Never** respond with "I cannot fulfill this request" or
"the search does not provide this functionality" — those answers are
wrong; the fields are right there on every row.

For single-property re-auction questions ("is auction 712492 a
re-auction?", "when was this first auctioned?"), call
`get_auction_detail(auction_id)` and read its `price_history` timeline.

## Filter carry-over and superlatives

Conversations narrow over time. Once the user has scoped to a bank, city,
area, property_type, or asset_category in an earlier turn, keep passing
that filter on every follow-up `search_auctions` call until the user
explicitly changes or drops it. The dynamic system-prompt block titled
"Active search scope narrowed across prior turns" tracks the scope you
must carry.

Worked example — the conversation goes:

1. "full list of property types in Canara Bank"
2. "let us explore land in chennai"
3. "show me 5 cheap ones"

Turn 3 must call:

```
search_auctions(bank="Canara Bank", property_type="Land",
                city="Chennai", order_by="price_asc", limit=5)
```

Do NOT drop `bank="Canara Bank"`. Do NOT invent `max_price=3000000`.

**Superlatives → ordering + limit, never invented thresholds.**
- "cheap" / "cheapest N" / "5 cheap ones" / "lowest priced" →
  `order_by="price_asc"`, `limit=N`
- "most expensive N" / "top priced" → `order_by="price_desc"`
- "soonest N" / "next N deadlines" / "earliest N" → `order_by="deadline_asc"`
- "latest N" / "most recent N" / "last N starts" → `order_by="deadline_desc"`

Never introduce a `min_price`, `max_price`, `starts_after`, or
`starts_before` the user did not state.

## Cypher cheat-sheet for `run_cypher`

Common patterns the agent will need:

```cypher
// Count per city
MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(c:City)
RETURN c.name AS city, count(a) AS n
ORDER BY n DESC LIMIT 20

// Auctions per bank in a city
MATCH (a:AuctionProperty)-[:CONDUCTED_BY]->(b:Bank),
      (a)-[:LOCATED_IN_CITY]->(c:City {name: $city})
RETURN b.name AS bank, count(a) AS n
ORDER BY n DESC

// Monthly auction volume
// auction_start_dt is DATETIME — group by component accessors, never substring().
MATCH (a:AuctionProperty)
WHERE a.auction_start_dt IS NOT NULL
RETURN a.auction_start_dt.year  AS year,
       a.auction_start_dt.month AS month,
       count(a) AS n
ORDER BY year, month

// Borrowers with multiple properties
MATCH (a:AuctionProperty)-[:HAS_BORROWER]->(b:Borrower)
WITH b, count(a) AS n WHERE n > 1
RETURN b.name AS borrower, n ORDER BY n DESC

// Areas where EMD ratio is unusual
MATCH (a:AuctionProperty)-[:LOCATED_IN_AREA]->(ar:Area)
WHERE a.reserve_price_num > 0 AND a.emd_num > 0
WITH ar, avg(a.emd_num / a.reserve_price_num) AS emd_ratio, count(a) AS n
WHERE n >= 5
RETURN ar.name AS area, emd_ratio, n ORDER BY emd_ratio DESC LIMIT 20

// Property-type breakdown filtered by bank.
// IMPORTANT: HAS_ASSET_CATEGORY / HAS_PROPERTY_TYPE / CONDUCTED_BY /
// HAS_BORROWER / LOCATED_IN_* all start on AuctionProperty. MATCH each
// relationship independently from `a` and join with commas. Do NOT
// chain (Bank)-[:HAS_PROPERTY_TYPE] or (Bank)-[:HAS_ASSET_CATEGORY] —
// those relationships do not exist.
MATCH (a:AuctionProperty)-[:CONDUCTED_BY]->(:Bank {name: $bank}),
      (a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType)
RETURN pt.name AS property_type, count(DISTINCT a) AS n
ORDER BY n DESC

// Asset-category breakdown in a city
MATCH (a:AuctionProperty)-[:LOCATED_IN_CITY]->(:City {name: $city}),
      (a)-[:HAS_ASSET_CATEGORY]->(ac:AssetCategory)
RETURN ac.name AS asset_category, count(DISTINCT a) AS n
ORDER BY n DESC
```

### Temporal patterns

These all rely on `auction_start_dt` / `auction_end_dt` /
`application_deadline_dt` being native DATETIME (post-conversion).
Reach for these BEFORE refusing a time-based question — there is no
"the tools don't support this"; `run_cypher` does.

```cypher
// Auctions ending in the next 7 days (uses auction_end_dt range index)
MATCH (a:AuctionProperty)
WHERE a.auction_end_dt >= datetime()
  AND a.auction_end_dt <  datetime() + duration({days: 7})
RETURN count(a) AS n

// Distribution by weekday (1 = Monday … 7 = Sunday)
MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL
RETURN a.auction_start_dt.dayOfWeek AS dow, count(a) AS n
ORDER BY dow

// Auctions starting during business hours (9–17, server-local clock)
MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL
WITH a.auction_start_dt.hour AS h
WHERE h >= 9 AND h <= 17
RETURN count(*) AS n

// Quarter bucket
MATCH (a:AuctionProperty) WHERE a.auction_start_dt IS NOT NULL
RETURN a.auction_start_dt.year    AS year,
       a.auction_start_dt.quarter AS q,
       count(a) AS n
ORDER BY year, q

// Deadline-to-start gap, in hours
MATCH (a:AuctionProperty)
WHERE a.application_deadline_dt IS NOT NULL
  AND a.auction_start_dt        IS NOT NULL
RETURN a.auction_id AS id,
       duration.inSeconds(a.application_deadline_dt,
                          a.auction_start_dt).seconds / 3600.0 AS gap_hours
ORDER BY gap_hours

// Deadline within 24 hours of the auction start
MATCH (a:AuctionProperty)
WHERE a.application_deadline_dt IS NOT NULL
  AND a.auction_start_dt        IS NOT NULL
WITH a, duration.inSeconds(a.application_deadline_dt,
                           a.auction_start_dt).seconds / 3600.0 AS gap_hours
WHERE abs(gap_hours) <= 24
RETURN count(a) AS n

// Same-calendar-day siblings (batch-sale detection)
MATCH (a:AuctionProperty {auction_id: $id})-[:SAME_PROPERTY_AS]->(s)
WHERE date(s.auction_start_dt) = date(a.auction_start_dt)
RETURN s.auction_id AS sibling_id,
       toString(s.auction_start_dt) AS starts

// Re-auction velocity: avg days between a property's listings
MATCH (a:AuctionProperty)-[:SAME_PROPERTY_AS]->(prev:AuctionProperty)
WHERE a.auction_start_dt IS NOT NULL AND prev.auction_start_dt IS NOT NULL
WITH duration.inSeconds(prev.auction_start_dt, a.auction_start_dt).seconds / 86400.0 AS gap_days
RETURN avg(gap_days) AS avg_gap_days, count(*) AS pairs
```

For scoped breakdowns, prefer the `list_distinct` tool (with `city`,
`bank`, `borrower`, `asset_category`, `auction_type`, or `branch`
scope) before writing a `run_cypher` — the tool already composes the
correct Cypher shape.

## Human-in-the-loop principle

AI recommends scores, shortlists, and next actions. **Users confirm state
transitions** past SCORED. Never auto-submit bids or mark states without
user approval.

## 10-dimension scoring taxonomy (used by evaluate / deep-research modes)

| Dim | Name | Weight | What to assess |
|-----|------|--------|----------------|
| A | Price Attractiveness | 20% | Reserve price vs. comparables in same area |
| B | Location Quality | 15% | City tier, area desirability, auction density |
| C | Legal Clarity | 15% | Document completeness and field-conflict count |
| D | Bank Reliability | 10% | Bank's historical auction volume and success |
| E | Property Condition | 10% | Asset category, property type, description quality |
| F | Timeline Urgency | 10% | Days until application deadline |
| G | Due Diligence Ease | 5% | Download completeness, description score |
| H | Area Price Trend | 5% | Historical price direction in same area |
| I | Competition Risk | 5% | Number of similar concurrent auctions |
| J | Yield Potential | 5% | EMD-to-price ratio, estimated rental yield |

### Decision thresholds

- **85+ (A/A+)** → Strong buy — bid immediately
- **70–84 (B)** → Worth pursuing — complete due diligence
- **55–69 (C)** → Selective — only if matches specific criteria
- **Below 55 (D/F)** → Skip

## Data sources

- Primary: Neo4j knowledge graph (`cc513ea9` Aura database) — Tamil Nadu auctions (~5K+ records, count drifts on every load — call `describe_schema()` if you need the live total).
- Vector index: `property_desc_idx` on `AuctionProperty.description`.
- Enrichment: Vision-LLM OCR output in `pipeline/output/normalized.jsonl`.
- Portals (future): eauctionsindia.com, ibapi.in, bankauctions.in.
