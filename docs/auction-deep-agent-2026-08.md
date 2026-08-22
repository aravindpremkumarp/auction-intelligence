# The auction deep agent — clean-slate design (Aug 2026)

A purpose-built agent for Tamil Nadu bank-auction property, designed against
the **current** graph and owing nothing to the pydantic-ai chat agent. New
tools, new instructions, new skills, new package. `/chat/v1`, `/chat/v2` and
`/chat/deep` keep running untouched.

Status: **steps 1–4 built and smoke-tested** (see §10). Tools, evals,
instructions, three skills and the agent harness are in `api/agent3/`; the
eval catalogue scores 36/36 against the live graph and 143 unit tests pass.
The agent has now been driven by a real model against the live graph
(`evals/smoke_agent3.py`, 6/6) with grounded, scope-honest answers and
working server-side memory. It is still not wired to any request path.

**Open issue, not blocking step 5:** prompt cache runs at **17% of input**
across a clean smoke run (10,752 of 63,017 tokens) — better than the 0% first
reported here, which was an artefact of counting only the final model call,
but still short of §6's "above 50%" gate. Reasoning effort was investigated
as a second suspect and **cleared** — see §10 step 4b.

---

## 1. Why a clean slate — and what the evidence actually says

The premise for this rewrite is that the deep loop was slow and expensive
because it inherited instructions built for the v1 pydantic-ai agent. That is
**half right, and the half that is wrong matters**, because building on the
wrong diagnosis would reproduce the cost.

What `docs/chat-loop-ab-2026-08.md` measured on 21 Aug — deep loop at 148.7 s
median, 16,911 fresh prompt tokens per turn against the tiered loop's 3,566:

| Cause | Instruction-driven? | Fix lives in |
|---|---|---|
| 9 harness tool schemas in every prompt (`ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`, `task`) | No | §6 — drop `create_deep_agent` |
| Prompt cache at 24%, **zero** on the answer call | No | §6 — stable prefix, pinned |
| Two strictly sequential model calls on a provider running at ~8 tok/s | No | §6 — parallel tool execution |
| `modes/_shared.md` at ~2,600 tokens re-sent every turn, most of it irrelevant to the question asked | **Yes** | §5 — thin core + skills |
| Whole transcript re-sent each turn, including 500-row tool payloads | Partly | §6 — trim + sink |

So the instruction inheritance is a real cost — roughly 2.6k tokens of
always-on prompt where a typical turn needs maybe 400 of it — but it is not
the 149 seconds. The design below fixes all five, and §9 says how we will know
which one paid.

The stronger reason for the clean slate is not cost at all: **`modes/_shared.md`
is now wrong.** It states "No `total_area`/`village`/`taluk`/`district` props
exist — sizes and sub-locality live only in `description` text; never filter on
them." Every clause of that is false against today's graph. An agent told that
will refuse questions it can answer exactly.

---

## 2. The graph as it is — verified 21 Aug 2026

Two layers, joined by `HAS_DOCUMENT`.

### Layer 1 — the portal listing (`AuctionProperty`, 2,964)

What the website scraped, and what the UI keys on. `auction_id` is the id in
every URL, the matches panel and the saved-property flow.

Coverage of the fields worth filtering on:

| Field | Filled | Field | Filled |
|---|---|---|---|
| `description` | 2,964 | `district` | 2,302 |
| `auction_start_dt` (ZONED DATETIME) | 2,964 | `taluk` | 2,235 |
| `reserve_price_num` | 2,750 | `village` | 2,494 |
| `emd_num` | ~2,900 | `revenue_village` | 1,051 |
| `total_area` (raw string) | 2,132 | `registration_sub_district` | 2,103 |
| `description_embedding` | 2,179 | `boundary_north` (+S/E/W) | 2,190 |
| `undivided_share` | 487 | `door_numbers_new` | 512 |

Relationships: `CONDUCTED_BY→Bank`, `LISTED_BY_BRANCH→Branch`,
`LOCATED_IN_CITY/AREA/STATE/DISTRICT/TALUK/REVENUE_VILLAGE`,
`HAS_BORROWER→Borrower`, `HAS_ASSET_CATEGORY`, `HAS_PROPERTY_TYPE` (many),
`IS_AUCTION_TYPE`, `IS_PARCEL→Parcel`, `SAME_PROPERTY_AS` (80 re-listing
links), `HAS_DOCUMENT→Document`.

### Layer 2 — the sale notice (`Document` 1,628 → `Lot` 3,335 → `Auction` 3,262)

This is the layer the current agent cannot see, and it is where the answer to
almost every serious buyer question lives.

**`Lot`** — one schedule item in a notice:

| Field | Filled | Field | Filled |
|---|---|---|---|
| `full_description` | 3,251 | `district` | 2,869 |
| `asset_category` | 3,292 | `taluk` | 2,337 |
| `property_type` | 3,289 | `village` | 1,368 |
| `encumbrance` (free text) | 1,009 | `road_width_ft` | 914 |
| `address` | 1,075 | `frontage_ft` | 607 |
| `construction_type` | 188 | `latitude`/`longitude` | 171 |
| `occupancy_status` | 33 | `landmark` | 86 |

**Lot edges, with lot coverage:**

- `HAS_EXTENT→Measurement` — **2,993 lots (90%) carry a normalised
  `sqft_norm`.** 2,975 have a `is_headline` extent. Kinds: `extent` 2,602,
  `total` 2,500, `built_up` 684, `uds` 614, `uds_parent` 553,
  `super_built_up` 237, `carpet` 31. Units seen: `sq_ft`, `sq_m`, `cent`,
  `ground`, `acre`, `are`, `hectare`.
  **Caveat: `sqft_norm` has bad outliers** — median 1,471, p90 10,977, max
  15,571,959,480. Any tool touching it must band-limit (see §4.1).
- `MENTIONS_IDENTIFIER→Identifier` — **3,215 lots (96%)**. Kinds by volume:
  `survey_old` 3,548, `survey_new` 2,242, `patta` 814, `plot` 548,
  `door_old` 504, `door_new` 455, `sale_deed` 425, `approved_layout` 354,
  `property_id` 338, `flat` 263, `assessment_old/new` 372, `block` 147,
  `cersai` 143.
- `HAS_BOUNDARY→Boundary` — 2,595 lots, 10,329 boundaries. Carries `side`,
  `adjacency_raw`, `measurement_ft`, `road_width_ft`, `access_kind`
  (`plot` 3,827, `road` 1,939, `street` 655, `pathway` 435, `channel` 48).
- `POSSESSION_IS→PossessionType` — 1,985 lots. Values: `physical`,
  `symbolic`, `constructive`. Rel carries `taken_on`.
- `SECURES→LoanAccount` — 1,609 lots. Rel carries `outstanding_num`,
  `demand_notice_date`, `as_on`.
- `TITLE_HELD_BY→Borrower` 908 · `HAS_PARTY→Borrower` (rel `role`) 8,642
- `HAS_SCHEDULE→Schedule` 1,893 · `IN_TALUK` / `IN_DISTRICT` /
  `IN_REVENUE_VILLAGE` · `IS_PARCEL→Parcel` · `HAS_FACT→Fact`

**`Auction`** — one sale event for one lot. 3,262 nodes:

`reserve_price_num` 3,216 · `emd_num` 3,158 · `bid_increment_num` 2,484 ·
`inspection_dt` 1,989 · `auction_start_dt` 3,190 · `auction_end_dt` ·
`application_deadline_dt` · `auto_extension_minutes` · `sarfaesi_stage` ·
**`attempt_no` 1–8, with 206 auctions at attempt ≥ 2** · `outcome` (only
`unsold` populated today).

**`Document`** — the notice itself: `markdown`, `sale_terms`,
`notice_type`, `parse_quality_score`, `public_url`, plus edges
`ISSUED_BY→Bank`, `HOSTED_ON→Platform` (BAANKNET 558, BANKEAUCTIONS 136,
BANKAUCTIONS 130, AUCTIONBAZAAR 72, AUCTIONTIGER 66, …),
`EMD_PAYABLE_TO→EMDAccount` (account_no, ifsc, mode_of_payment),
`HAS_CONTACT→Contact` (phone, email), `SIGNED_BY→Officer` (rel `role`),
`UNDER_FRAMEWORK→LegalFramework` (SARFAESI / DRT / IBC / other),
`USES_TERMS→TermsTemplate`, `CASE_REF→CaseReference`,
`DEBT_ASSIGNED_FROM→Bank`, `UNDER_TRUST→Trust`.

**Reference geography is loaded**: `District` 38, `Taluk` 316,
`RevenueVillage` 17,164, `City` 50, `Area` 1,084 — all with codes and
`name_ta`. Village-level questions are now answerable by join, not by string
matching in prose.

### The join, and its one sharp edge

- 2,964 / 2,964 listings have a document; **2,939 reach at least one lot.**
- A document fans out: 966 listings sit on a 1-lot notice, the rest on
  notices with 2–30 lots (mean 4.4).
- **There is no clean 1:1 listing→lot link.** `IS_PARCEL` via `Parcel` does
  not disambiguate either (same 4.4 mean).

So: lot facts are **notice-level context** for a listing, not per-listing
truth, unless the notice has one lot. Every tool that surfaces a lot fact
must say which of the two it is. This is a correctness rule, not a nicety —
"this property is 2,400 sqft with physical possession" is a lie if the notice
had six lots and we picked one.

### What is NOT available (do not design for it)

- **`Lot.description_embedding` is empty.** The `lot_description_embedding`
  vector index exists with **0 vectors**. Lot free-text search must use the
  `lot_description_ft` fulltext index (verified working), not vectors.
- `Document.markdown_embedding` is only 340 / 1,628.
- `Auction.outcome` only ever says `unsold` — **we cannot answer "did it
  sell" or "what did it fetch".** Sold prices do not exist in this graph.
- `Lot.occupancy_status` at 33 rows is not a filter, it is a footnote.
- Geo coordinates on 171 lots — no distance/radius search.
- Dirty dates: `Auction.auction_start_dt` is a **STRING** (`2026-06-29T11:00`)
  and at least one row is the literal `"12:00"`. Only 516 auctions start on or
  after today. `AuctionProperty.auction_start_dt` is a proper ZONED DATETIME
  and is the one to filter on.

Live indexes to build on: `property_desc_idx` (vector, AuctionProperty),
`property_text_idx` (fulltext, title+description), `lot_description_ft`
(fulltext), `identifier_raw_ft` (fulltext), `party_name_ft` (fulltext).

---

## 3. What the agent is for

One sentence: **take a buyer from "show me flats in Coimbatore" to "here is
everything the sale notice says about this specific lot, what it is worth per
square foot against comparables, what is unresolved, and what you must do by
when" — without them learning the schema.**

Three question classes it must cover, in rising depth:

1. **Find** — filters, counts, breakdowns, deadlines. Answer in one tool call.
2. **Compare** — price per sqft against comparables, re-auction price drops,
   bank/area patterns. Two or three calls, run in parallel.
3. **Diligence** — one property, everything the notice says: schedule,
   extent, survey numbers, boundaries and access, possession, encumbrance,
   outstanding loan, parties, EMD mechanics, contacts, and **the named gaps**.
   This is where a skill and a subagent earn their cost.

Non-goals, stated so they are not rebuilt later: no sold prices, no market
valuation, no litigation or title-chain lookup, no alerts or tracking, no
distance search.

---

## 4. Tool surface

Six graph tools + one web tool. `api/chat/agent3/tools/`. No import from
`api/tools/cypher_tools.py`, `api/chat/v2/tools.py` or `api/policy.py` — this
package owns its own surface, which is the point of the clean slate.

Shared conventions, applied by decorator so they cannot drift:

- **Errors return as data** (`{"error": ..., "valid_values": [...]}`), never
  raise. The deepagents tool node re-raises and kills the turn.
- **Every row carries `auction_id`.** That is the panel's key and the answer's
  citation.
- **Heavy payloads never reach the model.** Panel rows go to a per-turn sink;
  the model sees a slice plus `total_count`. In a checkpointed transcript an
  unsplit payload is re-billed on every later turn.
- **Every lot-derived value is tagged** `"scope": "lot"` (notice had one lot,
  it is this property) or `"scope": "notice"` (fanned out, treat as context).
- Enum values live in the docstring, rendered from live constants.

### 4.1 `find_properties(...)` — the workhorse

Returns listings. Filters span both layers; lot-layer filters resolve through
`HAS_DOCUMENT` and are documented as notice-level.

```
find_properties(
  # place
  city=None, area=None, district=None, taluk=None, revenue_village=None,
  # what
  asset_category=None,          # Residential | Commercial | Industrials
  property_type=None,           # list; 23 live values, see docstring
  auction_type=None,            # SARFAESI | DRT | Liquidation | Private Property
  # money (INR)
  reserve_price_min=None, reserve_price_max=None,
  emd_min=None, emd_max=None,
  # time
  auction_from=None, auction_to=None, deadline_before=None,
  upcoming_only=False,
  # who
  bank=None, branch=None, borrower=None, platform=None,
  # NEW — notice-layer filters, none of which the current agent can express
  area_sqft_min=None, area_sqft_max=None,   # headline extent, band-limited
  extent_kind="headline",                   # headline|total|built_up|uds|carpet
  possession=None,                          # physical|symbolic|constructive
  road_width_ft_min=None,
  access_kind=None,                         # road|street|pathway|plot|channel
  has_encumbrance_note=None,                # bool
  outstanding_max=None,                     # secured loan outstanding
  attempt_no=None, reauction_only=False,    # attempt_no >= 2 (206 auctions)
  identifier=None, identifier_kind=None,    # survey/patta/door/plot number
  legal_framework=None,                     # SARFAESI|DRT|IBC|other
  # shape
  sort="deadline", limit=20, group_by=None,
)
```

Returns:

```
{ rows: [...],            # limit rows, each with auction_id + why-it-matched
  total_count: int,       # exact, over the whole filter set
  aggregations: {...},    # count, reserve min/avg/max, ₹/sqft when askable
  distribution: {...},    # when group_by is set
  refine: [{filter, value, count}],   # live, non-empty narrowings
  hint / relax: ...,      # on zero results, which single filter to drop
  scope_notes: [...] }    # any lot filter that was notice-level
```

Design notes:

- `sqft` filters clamp to `1 <= sqft_norm <= 500000` and prefer
  `is_headline`; anything outside is excluded and counted in `scope_notes`.
  Without this the 15.5-billion-sqft row poisons every average.
- `refine` and `relax` are computed in the same round-trip, not by extra
  calls. This is what stops the agent firing follow-up searches.
- `group_by` handles the whole "breakdown" class — city, area, district,
  bank, property_type, price band, month, attempt_no — without Cypher.

### 4.2 `get_property(auction_ids: list[str], depth="standard")`

The diligence tool. One call, up to 5 ids.

- `depth="standard"` — listing fields, bank/branch/platform, dates, EMD,
  contacts, and a **lot summary** (count, headline extents, possession mix).
- `depth="full"` — every lot of the notice: schedule text, extent by kind,
  identifiers by kind, boundaries by side with access and road width,
  possession + `taken_on`, encumbrance text, `SECURES` outstanding and demand
  notice date, parties by role, plus document-level `sale_terms`, EMD account,
  officer, framework, case reference, `public_url`.
- Always returns a **`gaps` list**: named things the notice does not say
  ("no patta number", "possession not stated", "no encumbrance clause").
  The gaps are the diligence product. An agent that only reports what is
  present reads as if nothing is missing.

### 4.3 `search_notices(query, ...)`

Free-text over what the notice actually says, via `lot_description_ft` and
`property_text_idx` (both live). **Not vector** — lot embeddings are empty.

Handles: "borewell", "shed on agricultural land", "disputed pathway",
"north facing corner plot", "tiled roof". Returns matched snippets +
`auction_id`, so the answer can quote the notice.

### 4.4 `find_by_identifier(value, kind=None)`

Survey number, patta, door number, plot number, flat number, CERSAI id →
listings. Backed by `identifier_raw_ft` over 10,253 identifiers on 96% of
lots. Normalises the usual noise (`S.No.`, `Survey No`, `/`, `-`, spacing).

This is the single highest-value new capability: *"is 123/4B in Sriperumbudur
in any auction notice"* is a question the current agent cannot answer at all,
and one a buyer or a broker asks constantly.

### 4.5 `benchmark_price(...)`

The "is this priced right" tool, and the reason `sqft_norm` matters.

Given an `auction_id` or a filter set, returns ₹/sqft for the subject and for
comparables at widening rings (same area → same city → same district → same
property type statewide), with n, median, p25, p75 at each ring, and the
subject's percentile. Refuses to output a ring with n < 5 rather than
computing a percentile off three rows. Explicitly labelled **reserve price
per sqft, not market value** — reserve is a bank's floor, and we have no sold
prices (§2).

### 4.6 `reauction_history(auction_id)`

`SAME_PROPERTY_AS` links + `Auction.attempt_no` + `sarfaesi_stage` → the
attempt chain for a property, with reserve price at each attempt and the drop
between them. 206 auctions are at attempt ≥ 2; a falling reserve across
attempts is the strongest buy signal the graph holds.

### 4.7 `run_cypher(cypher, params)` + `internet_search(query)`

`run_cypher` stays as the read-only escape hatch, but is **only reachable
after the `cypher` skill is loaded** — the skill carries the schema and the
date-string rules, so they cost nothing on the 95% of turns that never use it.
`internet_search` is off-graph context only, and async (the current deep loop
wrapped it synchronously and silently fed the model a coroutine repr for
weeks).

---

## 5. Instructions — thin core, everything else on demand

The single biggest instruction change: **`modes/_shared.md`'s ~2,600 tokens
become a ~600-token core plus skills the agent loads only when it needs
them.** Nothing about extent normalisation is in the prompt when someone asks
"how many auctions in Salem".

`api/chat/agent3/instructions.md` — the always-on core, and nothing else:

1. **Who and what** (3 lines) — auction intelligence over Tamil Nadu bank
   auction notices; every claim traceable to a notice.
2. **The data in six lines** — a listing is a portal row keyed by
   `auction_id`; behind it is a sale notice with one or more lots; lot facts
   are per-property only when the notice has one lot, otherwise context.
3. **Four hard rules** — ground every number in tool output and cite
   `auction_id`; never state a lot fact as property fact without its scope
   tag; no market valuation, no litigation/title-chain, no sold prices, no
   tracking (point at the Save button); say what is missing, not just what is
   there.
4. **Routing table** — one line per tool, plus: *load a skill before deep
   work.*
5. **Answer shape** — direct answer, then evidence, then gaps, then one
   narrowing nudge. No headers on a one-line answer.

Enums, synonym maps, date rules, extent kinds, identifier kinds: **all in tool
docstrings or skills**, never in the core prompt. The v2 lesson holds — schema
next to the parameter is what fixed the `property_type`/`asset_category`
confusion — and a docstring rides in the tool schema, which is cached.

The core must be **byte-stable across turns**. It is the cache prefix; the
per-turn variable part (today's date, graph size) goes at the *end* of the
system block, after the stable text.

---

## 6. Harness — and how the cost gets fixed

**Drop `create_deep_agent`. Build on `create_agent` with explicit
middleware.** This is the biggest single change and it is a direct response to
the A/B: `create_deep_agent` unconditionally binds nine tools we cannot use
and cannot remove (verified against 0.7.7, `subagents=[]` does not suppress
`task`, `HarnessProfile(excluded_tools=…)` does not either). We keep the parts
of the Deep Agents pattern that earned their place — planning, subagents,
skills, durable transcript — by composing them ourselves:

| Deep Agents feature | How we get it | Why not the default |
|---|---|---|
| Transcript memory | Neo4j `BaseCheckpointSaver` (reuse `api/checkpointer.py`, the one piece worth keeping) | It works, and it won the memory argument in the A/B |
| Subagents | One `consult(brief, ids)` tool delegating to a named worker | `task` delegates to a blank clone of its parent |
| Skills | `api/agent3/skills.py` — our own loader | **Corrected.** `SkillsMiddleware` was measured and rejected: see below |
| Filesystem | **Not bound** | 6 tool schemas per prompt for a chat agent that never writes a file |
| Shell (`execute`) | **Not bound** | Inert today only because the default backend has no `execute`; not a guarantee to rely on |
| Todo list | **Not bound** | The plan is the answer's outline; a second fuzzy copy costs a call |

**Why not `SkillsMiddleware` — measured, not assumed.** The original
version of this section specified deepagents' `SkillsMiddleware`. Measured
against 0.7.7 before building step 4, it fails on its own terms:

- **+36 MB RSS.** `create_agent` alone imports at 68.3 MB; adding
  `SkillsMiddleware` takes it to 104.5 MB — identical to importing all of
  `deepagents`, because the package `__init__` pulls `create_deep_agent`, the
  filesystem middleware and the subagent middleware regardless of which
  submodule is requested.
- **It requires the very tools this section drops.** `backend` is a REQUIRED
  constructor argument, and the middleware binds no tools of its own. It
  loads a skill by instructing the model to call `read_file` on a path — so
  using it means binding `ls`, `read_file`, `write_file`, `edit_file`,
  `delete`, `glob`, `grep`, `execute`, measured at **~2,611 tokens** of
  schema per prompt.
- **Plus 464 tokens** of its own boilerplate system prompt, most of it
  irrelevant here (a quantum-computing example, "Executing Skill Scripts").
- **Plus one model call per skill load**, since progressive disclosure runs
  through a tool call rather than direct injection.

That is ~3,075 tokens/turn against a 676-token core instruction file — 4.5x
the whole prompt §5 exists to shrink, to obtain a feature we can get in ~70
lines. `api/agent3/skills.py` matches a trigger, reads the file and injects
the text: no tool bound, no round-trip, and nothing paid on turns that load
no skill. For scale: our entire useful tool surface (all four graph tools)
measures ~1,744 tokens — less than deepagents' filesystem overhead alone.

Middleware, kept deliberately short:

- `ModelRetryMiddleware` — 2 retries, 5xx/timeout only.
- `ModelCallLimitMiddleware(run_limit=6)`, `ToolCallLimitMiddleware(10)`.
- `ToolErrorMiddleware` — ValueError/TypeError back as data.
- `SummarizationMiddleware` — **now warranted**, unlike in the tiered loop:
  a checkpointed transcript does grow. Trim tool messages older than N turns
  to their `auction_id` list.
- `AnswerGate` (custom, `after_model`) — every ₹ amount, count, sqft and
  `auction_id` in the draft must appear in this turn's tool results; one
  repair call, then degrade to raw rows. Plus the scope rule: a lot fact
  stated without its tag is a gate failure.
- `IntentGate` (custom, `before_agent`) — regex tier first. Protects the
  people in the data: "every defaulter in Coimbatore with addresses" is one
  cheap query and the reason this lives in code, not the prompt.
- `InjectionEnvelope` (custom) — pasted broker blurbs and WhatsApp forwards
  wrapped as data.

**Parallel tool execution is mandatory.** The A/B's 149 s was two strictly
sequential calls on an ~8 tok/s provider. Independent calls in one step
(three cities, subject + comparables) must dispatch together.

**Cache discipline, pinned by test.** The A/B found the answer call hitting
zero cache. A test asserts the system block's leading bytes are identical
across two turns of the same thread, and telemetry reports cached share per
call. If cached share on the answer call is not above 50% in the first run,
that is the bug to fix before reading any cost number.

Subagents, two to start:

- `diligence` — one property, `depth="full"`, writes the dossier and the gaps.
- `comparables` — benchmark ring walk, so its 4–6 lookups stay out of the
  main transcript.

Both share the turn's tool sink, so their graph hits reach the matches panel
and the eval trajectory like any other.

---

## 7. Skills

`api/chat/agent3/skills/<name>/SKILL.md`, loaded on demand. Each is the
knowledge a good auction analyst has, written once:

| Skill | Loaded when | Carries |
|---|---|---|
| `diligence` | one property, deep | The dossier order, the gap checklist, what a missing patta or symbolic possession means for a buyer |
| `pricing` | "is this a good price" | Ring walk, `is_headline` vs `uds` vs `built_up`, the n<5 refusal, reserve ≠ market |
| `extent` | any area question | Unit conversions (cent/ground/are/acre → sqft), the outlier band, why UDS is not floor area |
| `identifiers` | a survey/patta/door number appears | Tamil Nadu numbering, old vs new survey, normalisation, what `cersai` means |
| `possession-and-encumbrance` | title/risk questions | physical vs symbolic vs constructive, `taken_on`, how to read an encumbrance clause, and the hard line at "not legal advice" |
| `bidding` | "how do I bid" | EMD account and mode, increments, auto-extension, inspection, application deadline, platform quirks |
| `reauction` | attempt ≥ 2, or "has this failed before" | `attempt_no`, `SAME_PROPERTY_AS`, reading a price drop |
| `cypher` | nothing else expresses it | Full schema, the string-date trap, read-only guardrails |

Cost shape: a skill is ~300–800 tokens and loads on maybe 20% of turns.
Against ~2,600 always-on today, the expected steady-state saving is roughly
**2,000 input tokens per turn**, and — more importantly — the deep knowledge
gets *longer*, not shorter, because it no longer competes for prompt space.

---

## 8. What each design choice is worth

| Change | Attacks | Expected |
|---|---|---|
| `create_agent`, no filesystem/shell/todo | 9 tool schemas/prompt | ~1,500–2,500 input tok/turn |
| Thin core + skills | 2.6k always-on prompt | ~2,000 input tok/turn |
| Stable prefix + cache test | 24% cache, 0% on answer call | the affordability case, or a known bug |
| Parallel dispatch | 149 s sequential | the latency, directly |
| Sink + summarization | transcript re-billing | grows with turn count |

Honest note: the provider's throughput swung 3.4–9.7 tok/s minutes apart
during the A/B. **No latency claim from a single run is worth reporting.**
Every number above needs n≥3.

---

## 9. Evals — new, because the old catalogue cannot see this layer

`evals/cases.py`'s 68 cases are all listing-layer. Keep them as the
regression gate, and add:

- **`lot_facts`** (~15) — extent in a named unit, survey number lookup, road
  width, boundary side, possession type, encumbrance presence, outstanding.
  Scored on the value, not the trajectory.
- **`scope_honesty`** (~8) — a lot fact from a multi-lot notice must be
  tagged as notice-level. **A confident un-tagged answer is a fail.** This is
  the new failure mode the new data introduces, and nothing else catches it.
- **`gaps`** (~6) — asked about a property whose notice omits patta or
  possession, the answer must name the omission.
- **`diligence`** (~5) — full dossier, judged against a hand-written
  reference for coverage and for absence of invention.
- **`refusal`** — extended: sold price, market value, "did it sell", distance
  search. All four are now *plausible-sounding* questions the graph cannot
  answer, which makes them the likeliest hallucinations.

Gate to ship: no regression on the 68, ≥90% on `lot_facts`, **100% on
`scope_honesty`**, and median turn under 30 s at n≥3.

---

## 10. Build order

1. ~~Schema brief + `find_properties` + `get_property` + evals for both.~~
   **Done.** `api/agent3/{enums,common,find_properties,get_property}.py`,
   `evals/agent3_cases.py`, `evals/run_agent3.py`, 58 unit tests. Live run:
   capability 10/10, lot_facts 6/6, scope_honesty 6/6, gaps 5/5 in 79s.
   The two tool docstrings cost ~1,035 tokens and ride in the cached tool
   schema, against the ~2,600 always-on prompt tokens they replace.
2. ~~`search_notices`, `find_by_identifier` — the two new-capability tools.~~
   **Done.** `api/agent3/{search_notices,find_by_identifier,identifiers}.py`
   — the survey/patta/door resolution and Lucene escaping moved into
   `identifiers.py`, shared by `find_properties`' `identifier=` filter and
   this standalone tool, so the two cannot answer the same survey number
   differently. 36 unit tests, 36/36 on the live eval catalogue (13
   capability, 8 lot_facts, 8 scope_honesty, 7 gaps).

   Two things worth knowing before touching either tool:
   - **Bare fulltext terms OR, they don't AND** — Lucene's default for
     space-separated terms. Verified live: `north facing corner plot` as
     bare terms matches 2,824 of 3,335 lots; AND-joined, 2.
     `search_notices` builds an AND query itself; quoted phrases pass
     through as exact-phrase queries.
   - **Several portal listings can share one sale-notice `Document`.**
     Verified live: auction_ids 744314 and 744316 point at the identical
     `Document` node (same `storage_key`). A survey-number or free-text
     match on that document is one finding across several listings, not
     several unrelated hits — both new tools group by the matched
     identifier/snippet rather than returning one row per listing.
   - **A Neo4j 5.x gotcha, hit once and now pinned by a test:** a `UNION`
     branch inside `CALL {}` cannot `RETURN` a column with the same name as
     a variable the outer query already imported into that `CALL` (here,
     `score` from `db.index.fulltext.queryNodes(...) YIELD ..., score`).
     It raises `Variable 'score' already declared in outer scope` — a
     parse error, not a wrong-empty-result, so it was caught before this
     shipped. Fixed by aliasing inside the branches and back on the way out
     (`identifiers.py::_DETAIL_CYPHER`).
3. ~~Instructions core + `diligence`, `extent`, `identifiers` skills.~~
   **Done.** `api/agent3/instructions.md` (676 tokens — a ~74% cut from
   `modes/_shared.md`'s ~2,600) plus three on-demand skills under
   `api/agent3/skills/`. `tests/api/test_agent3_instructions.py` pins the
   same class of drift `test_mode_files.py` catches for v1 — a prompt
   citing a tool that isn't built (`benchmark_price`, `reauction_history`,
   `run_cypher` are explicitly checked-for and forbidden until step 5/6
   land), a skill nothing routes to, and enum/conversion values quoted in
   prose that no longer match the live source (`enums.py`,
   `pipeline/measures.py`, `common.py`'s sqft band). It caught two real
   drift bugs on first run: a routing-phrase line-wrap that made the
   `identifiers` skill look orphaned, and the skill missing the
   `property_id` identifier kind. No live-graph eval needed here — this
   step touches no Cypher.
4. ~~Harness on `create_agent`, checkpointer reused, cache test.~~
   **Done.** `api/agent3/agent.py` (builds the graph), `loop.py` (one turn),
   `skills.py` (our loader — see §6). Four tools bound and nothing else;
   `Neo4jSaver` reused unchanged. Three bugs found by compiling the graph
   rather than reasoning about it:
   - **`ToolErrorMiddleware()` with no handler raises** — it requires
     `on_error`. Now supplied, and it deliberately does NOT echo internal
     exception messages: a driver error can carry a URI or credential, so
     only `ValueError`/`TypeError` (whose text we authored, naming valid
     values) pass through; everything else surfaces as its type name.
   - **`ModelRetryMiddleware` defaults to `retry_on=(Exception,)`** — it
     retried a deterministic `NotImplementedError` three times with backoff
     before failing anyway. On a real deploy a 4xx (bad key, malformed
     request) would burn three calls and ~7s per turn to reach the same
     error. Replaced with a predicate: 5xx/429/timeout/connection retry,
     everything else surfaces immediately.
   - **The stock LangChain fakes cannot drive this graph** —
     `GenericFakeChatModel` and `FakeMessagesListChatModel` both raise
     `NotImplementedError` under `create_agent` (no async tool-calling
     path), so a scripted model lives in the test file. Without it the
     graph could not be exercised at all.

   The cache assertion is made against the **compiled graph**, not the file:
   two turns of one thread must send a byte-identical system message. That
   is deliberately stricter than checking `instructions.md` is constant —
   the deep loop's docs claimed subagents were off for weeks because the
   assertion inspected what was passed in rather than what the harness
   assembled.
4b. ~~Smoke run: real model, live graph.~~ **Done.**
   `evals/smoke_agent3.py` — five cases plus a same-thread follow-up,
   deliberately small enough to run on every harness change. Final: 6/6.
   Verified grounded (the Coimbatore counts were checked against the graph
   and matched exactly: 35 listings, ₹11 lakh–₹22.5 crore, ~₹2 crore mean),
   scope-honest in prose, and correctly refusing the sold-price question
   with the reason.

   It found two bugs that 135 unit tests could not, both now fixed and
   regression-tested:
   - **Memory was off and silent.** `run_turn`'s `checkpointer` defaulted to
     `None`, so the follow-up answered "this is the start of our
     conversation". A memoryless agent is indistinguishable from a working
     one until the second question. Memory is now opt-OUT (`_DEFAULT`
     sentinel); `checkpointer=None` still means a deliberately memoryless
     run. The smoke check that let this through only asserted a non-empty
     answer — it now looks for amnesia markers.
   - **Integer ids cost three model calls per turn.** An `auction_id` looks
     like a number, so the model sent `auction_ids: 744314`; pydantic
     rejects at the schema boundary, *before* the errors-as-data decorator
     can return anything the model could learn from, so it retried the same
     call three times before guessing the list-of-strings form. That alone
     blew the 6-call limit on the scope case. The id parameters now accept
     `int` and coerce.

   Fixing memory surfaced a third: `api/chat/deep/checkpointer.py` could not
   be imported from `api/agent3` at all, because `api/chat/__init__.py`
   imports the FastAPI router. The saver moved to **`api/checkpointer.py`**
   — generic infrastructure that never belonged under the chat package, and
   the same reason `api/policy.py` and `api/model_selection.py` already sit
   outside it. Five call sites updated; the existing 16 checkpointer tests
   pass unchanged.

   **Still open after the smoke run**, recorded rather than fixed:
   - **Prompt cache: 17%, not the 0% first reported.** The first figure was
     wrong because `_usage_of` read only the final model call, so cache hits
     on a turn's earlier calls were invisible. Corrected to sum every call
     in the turn (see below), a clean run shows 10,752 cached of 63,017
     input tokens — and on the same-thread follow-up, 35%. Still short of
     §6's "above 50% or that is the bug to fix" gate, so this stays open,
     but it is partial engagement rather than none.
   - **Reasoning effort: investigated, and the hypothesis was wrong.**
     Every turn inherits `reasoning: {effort: "high"}` from
     `OPENROUTER_CHAT_REASONING_EFFORT` (a hardcoded default in
     `pipeline/config.py`), and this was initially recorded here as "a large
     part of the 60–140 s turns". Measured, same two questions at each
     setting:

     | effort | simple | multi-step |
     |---|---|---|
     | off | 42.4 s | 48.0 s |
     | low | 44.5 s | 56.9 s |
     | high | 44.0 s | 47.0 s |

     All six runs: 2 model calls, correct, scope-honest. **The differences
     are inside the noise**, and the 60–140 s figures from the smoke run
     were provider throughput variance — the same 3.4–9.7 tok/s swing the
     loop A/B documented on identical prompts minutes apart, not reasoning.

     The toggle itself is sound (verified: `off` → 0 reasoning tokens,
     `low` → 42, `high` → 68 on a fixed arithmetic prompt), so this is a
     real cost with no measured latency benefit — but the cost is small
     against per-turn output of 141–278 tokens, and at n=1 per cell this is
     not evidence enough to change a default shared with v1 and v2.
     **Left alone deliberately.** If it is revisited, it needs n≥3 across
     more question shapes, and it should be measured as cost, not latency.

4c. **Token accounting, corrected twice.** Asked whether the smoke run
   reports usage, it did — inaccurately. `_usage_of` had been written to read
   only the FINAL model message, over-correcting away from the loop A/B's
   bug of summing the whole returned list (which re-bills history: with a
   checkpointer that list is the entire conversation, and it reported 49,550
   input tokens against an actual 29,877). Reading only the last message
   fails the other way: a turn that thinks, calls a tool and then answers
   makes three model calls and only the third was counted.

   The correct boundary is the tail since the last human message — every
   call in this turn, nothing older. Both failure directions now have a
   test. The smoke run also prints a per-run total, which is the number
   worth quoting for cost.

   The re-run surfaced a harness bug of its own: thread ids were fixed per
   case (`smoke-scope`), and checkpoints live in Neo4j and outlive the
   process, so the second run resumed the first run's conversation and
   answered "I already answered this above" — correct, a fine demonstration
   that memory works, and a worthless smoke test, since no tool ran. Threads
   are now prefixed per run.

   Clean run after both fixes: **6/6, every turn 2 model / 1 tool call**,
   63,017 input and 2,347 output tokens across six turns.

4d. **Where the tokens actually are, and the cache dead end.** Chasing the
   17% cache figure produced a more useful answer than fixing it.

   **The cache is not ours to fix.** Five back-to-back requests with an
   IDENTICAL prefix returned `0% · 71% · 71% · 0% · 0%` — nothing changed on
   our side between the third and fourth. That 71% is the load-bearing
   result: it proves the prefix IS correctly cacheable, so §6's byte-stable
   work did its job. The hit *rate* is provider-side eviction. Two
   structural facts also depress our number: skill turns and non-skill turns
   are two different prefixes competing for cache (a perfect 3-for-3
   correlation in the smoke run), and a six-turn run spread over minutes is
   near worst-case — an interleaved different-shaped request was observed
   evicting an entry that had been hitting seconds earlier. **Recorded and
   dropped.** Do not re-open without provider-side evidence.

   **The tokens are in the rows, not the prompt.** Measured on one
   `find_properties` call: 3,281 tokens, of which `rows` is 3,006 — **92%**.
   Per field across 20 rows: `title` 404, `url` 295, dates 445. The stable
   prefix (system + tool schemas) is only ~2,420, so row payload is a bigger
   lever than perfect caching would ever have been.

   Two changes, both measured:
   - **`url` stripped from the model's rows** (295 tok / 10% of row cost) —
     the model cites by `auction_id` and the UI builds links from the panel
     row. Stripped in `_for_model`, NOT in `_shape_row`: the sink and the
     model are fed from the same shaped rows, so trimming in the shaper
     would have silently taken the link away from the matches panel too.
   - **Default sample 20 → 10 rows** (`DEFAULT_MODEL_ROWS`). `total_count`,
     `aggregations` and `distribution` remain exact over every match, and
     the panel still receives up to `PANEL_ROW_CAP`.

   One `find_properties` payload: **3,281 → 1,644 tokens, 50%**. Smoke run
   still 6/6 with answers no worse. **End-to-end movement is much smaller —
   63,017 → 60,179 input tokens across six turns (4.5%) — because only one
   of the six cases calls `find_properties`, and the saving lands once per
   search rather than once per turn.** The 50% is the honest figure for a
   search; 4.5% is the honest figure for this particular suite.

5. `benchmark_price`, `reauction_history` + `pricing`, `reauction` skills.
6. `AnswerGate` + `scope_honesty` evals; remaining skills.
7. Full eval run, n≥3 on latency, then decide about un-gating.

Steps 1–2 are worth building alone: they answer questions no current surface
can, and they are testable without any agent at all.
