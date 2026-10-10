# Bank Auction Intelligence (AuctionScope)

**Production:** <https://www.auctionscope.in>

An AI **search and evaluation layer** for Tamil Nadu bank-auction property
(SARFAESI, DRT, liquidation). It pulls listings from three auction portals,
reads the sale notice behind each listing with OCR + grounded LLM extraction,
resolves lenders and places onto canonical identities, and serves a chat agent
that answers from a **Neo4j knowledge graph** rather than from memory. It is
not a bidding platform and it does not do legal or title diligence: you find
and size up a property here, then bid on the official portal.

```
harvest 3 portals → load graph → OCR notices → classify → extract (LangExtract)
   → human review gates → promote :Lot / apply to listings → resolve entities
   → link re-auctions + build the spine → serve agent3 + web UI → feedback
```

Live corpus size is published at `GET /stats`. On 2026-09-12 the graph held
2,964 listings, 1,625 extracted notices and 3,393 lots (`docs/SCHEMA.md`).

---

## What it does

- **Conversational search** — "residential plots in Coimbatore over 2,000 sqft
  where the bank has physical possession", "which of these had a failed
  earlier auction?". Every number comes from a tool call; the agent cites
  `auction_id`s and says what the notice *doesn't* state.
- **Reads the notice, not just the portal row** — extent, survey/patta/door
  numbers, boundaries, possession, encumbrance, secured debt, EMD account,
  authorised officer. Each value is scope-tagged `lot` (this property's own
  fact) or `notice` (shared across a multi-lot notice).
- **Free-text search over notice wording** — "borewell", "disputed pathway",
  "north facing corner plot" — via two Lucene fulltext indexes.
- **Identifier lookup** — paste a survey, patta, door, plot or CERSAI number.
- **Re-auction awareness** — price history, attempt number and the previous
  reserve on every row; `reauction_history` for the full chain.
- **Price benchmark** — ₹/sqft against comparable single-lot notices
  (refuses when the notice can't support it, and says why).
- **Web-search enrichment** — locality, connectivity, flood/water signals,
  schools/hospitals, via Tavily; cited and marked approximate.
- **Three portals, one auction** — eauctionsindia, BAANKNET and
  bankeauctions.com copies are bridged (`SAME_LISTING_AS`) and merged into one
  `:AuctionEvent` with per-field provenance and a `core_complete` 0–9 score.
- **Accounts & Pro** — Supabase auth, watchlist, saved conversations,
  deadline alerts, and a one-time ₹499 / 30-day Pro unlock (Razorpay) that
  opens the full notice detail and a larger chat quota.
- **Enrichment review surface** — admin UI with a human gate per pipeline
  stage (classification, OCR markdown, extraction), an entity-resolution
  queue, a village-spelling queue, a pipeline funnel, and a seeded random
  **spot-check audit** that measures extraction precision.
- **Programmatic SEO** — static city / type landing pages, prerendered
  property pages with JSON-LD and per-property OG cards, long-form guides,
  comparison pages, `llms.txt`.
- **Content-ops agents** — a Poster drafts social posts from live data and a
  Reporter writes the weekly metrics report. Both stage only; a human
  publishes.

---

## Architecture

```
                Vercel (static web/)                        Neo4j Aura
              ┌──────────────────────────┐              ┌────────────────┐
  Browser ───▶│ index.html  app.js       │              │ knowledge graph│
              │ styles.css  auth.js      │              │ + chat         │
              │ review/admin/social/     │              │   transcripts  │
              │ spotcheck · SEO pages    │              └───────▲────────┘
              └────────────┬─────────────┘                      │ Bolt / HTTPS
                           │ fetch (API_BASE)                   │
                           ▼                                    │
              ┌─────────────────────────────────────────────────┴──────────┐
              │              Render — FastAPI (api/main.py)                 │
              │  public: health · properties · chat/agent3 · feedback ·     │
              │          alerts · chat (v1) · chat/v2 · chat/deep           │
              │  auth-gated: auth · billing · watchlist · conversations ·   │
              │              review · review/extraction · review/spotcheck ·│
              │              social · dossiers (flag)                       │
              │  agent3: LangChain create_agent + LangGraph, 7 tools,       │
              │          on-demand skills, answer gate, Neo4j checkpointer  │
              ├────────────────────────────────────────────────────────────┤
              │  Render cron `auction-extract` — pipeline.load_extractions │
              │  every 6 h (LangExtract over pending notices)              │
              └───┬──────────────┬──────────────┬──────────────┬───────────┘
                  │              │              │              │
            Supabase (JWT)  Cloudflare R2   OpenRouter /   Razorpay ·
            auth + JWKS     notices, photos Tavily · Google  Logfire (OTel)
                            OG cards, reels (LangExtract)
```

- **Backend** — FastAPI. `api/main.py` is a thin composition root (CORS,
  security headers, rate limit, exception handlers, static routes); logic
  lives in focused routers. Chat is `api/agent3/`. The earlier loops
  (`api/agent.py` pydantic-ai v1, `api/chat/v2` tiered, `api/chat/deep` Deep
  Agents) stay mounted for evals and the admin `/lab` comparison surface.
- **Frontend** — single-page app, **no build step**: vanilla JS + hand-written
  CSS (`web/index.html`, `app.js`, `styles.css`, `auth.js`, `billing.js`,
  `dossiers.js`, `lab.js`, `consent.js`). Separate pages for admin
  (`admin.html`), review (`review.html` + the iframed
  `review_extraction.html`), spot-check (`spotcheck.html`) and social
  review (`social.html`). Generated SEO pages live under `web/property/`,
  `web/bank-auctions/`, `web/guides/`, `web/compare/`.
- **Auth** — Supabase on the client; the backend verifies each access token
  against Supabase JWKS and mirrors the user as a Neo4j `:User`. Auth-gated
  routers are skipped when `AUTH_ENABLED=false`, so the app boots offline.
- **Paywall** — `api/entitlements.py` redacts `/auction/{id}` server-side to
  an allowlist of free fields; locked panels return counts, never values.
- **Data** — Neo4j Aura. Sale notices, portal photos, OG cards and rendered
  reels live in a public **Cloudflare R2** bucket; private dossier uploads in
  a separate private bucket.
- **Local-only tooling** — the Selenium scraper, portal harvesters, OCR runs,
  graph loaders and most backfill scripts run on a workstation. Extraction
  also runs on a Render cron.

---

## Repository layout

```
api/            FastAPI composition root + routers
  agent3/       THE chat agent: tools, skills/, instructions.md, loop, gates,
                manifest (turn-owned property cards), chatlog, ownership
  chat/         v1 router (pydantic-ai), gating (quota + model tiers),
                v2/ tiered loop, deep/ Deep Agents loop — admin / eval only
  agent.py      pydantic-ai v1 agent (used by evals + deep-research mode)
  review/       enrichment review: queues, extraction.py, spotcheck.py,
                blocks (annotator), grounding, markdown_match
  properties/ health/ feedback/ alerts/ auth/ billing/ watchlist/
  conversations/ social/ dossier/
  entitlements.py  places.py  canonical.py  policy.py  checkpointer.py
  neo4j_client.py  telemetry.py  observability.py  tools/ (v1 tools)
pipeline/       Notice enrichment: OCR clients (datalab, mineru), notice
                twins + stitching, classify_notice, LangExtract
                (langextract_examples, load_extractions, extract_entry),
                gap_fill / absence / widen_descriptions / keep_better,
                validators + key_entities, promote_extractions,
                apply_extractions, spotcheck, place/entity resolution,
                reader/ (reader v2, parked), lookups/ (gazetteers)
sources/        Portal adapters (eauctionsindia, baanknet, bankeauctions),
                normalize, match (cross-portal bridge), merge (spine)
scrapers/       Selenium scraper for eauctionsindia (local only)
scripts/        Loaders, R2 upload, resolvers, SEO generators, OG cards,
                backfills, audits, run_weekly_pipeline.py
modes/          v1 prompt files: _shared.md + deep-research.md (+ _archive/)
evals/          Golden questions, conversations, agent3 tool catalogue,
                LangExtract gold set + eval, ContextGem A/B, gold sprint
marketing/      dashboard.html (marketing system of record), templates/
                (cards + HyperFrames reels), render_social.py, render_reel.py,
                research/ (Instagram/X pulls, local only), outputs/ (staged)
marketing_agents/  poster.py (Agent A), reporter.py (Agent B)
web/            SPA + admin/review/social/spotcheck pages + generated SEO pages
config/         Full dev requirements, domain ontology, graph model
docs/           SCHEMA.md, design docs, audits, marketing playbooks,
                superpowers/ specs + plans
tests/          api/ (CI gate), pipeline/, scripts/, sources/, e2e/,
                marketing_agents/, scraper probes
experiments/    Spikes kept for their findings (deepagent-chat, bank ER)
inspiration/    Research notes on adjacent products and land-record sources
clones/ redesign/ brand/ walkthrough/   UI prototypes and brand assets
.agents/ .claude/   Vendored agent skills, hooks and settings
```

`docs/SCHEMA.md` is the graph reference (every extracted class and where it
lands, geography, provenance, sources and the spine). `TODOS.md` tracks open
items by priority.

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate         # macOS/Linux
# .venv\Scripts\activate          # Windows

pip install -r config/requirements.txt   # full dev set (scraping + OCR + evals)
cp .env.example .env                      # then fill in real values

uvicorn api.main:app --reload
```

Open <http://localhost:8000>. The SPA resolves `API_BASE` to the same origin on
`localhost` and to the hosted Render URL otherwise. API docs (`/docs`) are on
only when `APP_ENV` is `dev` or `test`.

Handy toggles:

- `AUTH_ENABLED=false` — boot without Supabase (skips auth/billing/
  watchlist/conversations/review/social routers).
- `RATELIMIT_DISABLED=1` — drop the anonymous-chat throttle and quota.
- `NEO4J_HTTP_API=1` — route Neo4j over Aura's HTTPS Query API when Bolt
  (port 7687) is blocked by an egress proxy. Every script accepts it.
- `?loop=tiered` / `?loop=deep` on the app — switch an admin's chat loop for
  comparison (sticks in localStorage); `/lab` shows the inspector.
- `DOSSIERS_ENABLED=true` + `?dossiers=1` — preview the dark-shipped dossier
  feature.

---

## Configuration

Copy `.env.example` → `.env`. Never commit the filled-in file. Key groups:

| Group | Vars | Notes |
| --- | --- | --- |
| **LLM** | `OPENROUTER_API_KEY`, `OPENROUTER_CHAT_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_MODEL_CHAT`, `OPENROUTER_MODEL_CHAT_FLASH`, `OPENROUTER_CHAT_REASONING_EFFORT`, `FREE_TIER_REASONING_EFFORT`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | OpenRouter runs chat (DeepSeek V4 **Pro** for paid, **Flash** for free/anon) and the batch pipeline; `OPENROUTER_CHAT_API_KEY` caps chat spend separately. Google key backs LangExtract's Gemini provider. |
| **Graph** | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, `NEO4J_HTTP_API` | Neo4j Aura. |
| **Auth** | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `AUTH_ENABLED`, `ADMIN_BOOTSTRAP_EMAIL` | Anon key is browser-safe; service-role key is server-only (`scripts/create_admin.py`). |
| **Quota** | `CHAT_ANON_DAILY_LIMIT`, `CHAT_ANON_MONTHLY_LIMIT`, `CHAT_FREE_DAILY_LIMIT`, `CHAT_FREE_MONTHLY_LIMIT`, `CHAT_PAID_DAILY_LIMIT`, `QUOTA_IP_SALT`, `RATELIMIT_DISABLED` | Durable day + month windows, per account or hashed IP. |
| **Billing** | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_PLAN_AMOUNT`, `RAZORPAY_PLAN_CURRENCY`, `RAZORPAY_PLAN_DAYS`, `RAZORPAY_WEBHOOK_TTL_DAYS` | Pro unlock. The webhook is the sole activation path. |
| **Storage** | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL`, `R2_PRIVATE_BUCKET` | Public bucket for notices/photos/cards; private bucket for dossiers. |
| **OCR / extraction** | `DATALAB_API_KEY`, `MINERU_API_KEY`, `DESCRIPTION_OCR_ENGINE`, `DATALAB_MODE_SINGLE`, `DATALAB_MODE_MULTI`, `DATALAB_PIPELINE_CONCURRENCY`, `EXTRACT_READER`, `LANGEXTRACT_PROVIDER`, `PIPELINE_LLM_TOKEN_BUDGET` | Datalab is the default bulk OCR engine (`fast` for single-lot notices, `accurate` for multi). `EXTRACT_READER` is `langextract` (prod), `v2` or `shadow`. |
| **Dossiers** | `DOSSIERS_ENABLED`, `OPENROUTER_MODEL_DOC_CLASSIFY`, `DOSSIER_MAX_FILE_MB`, `DOSSIER_MAX_PAGES` | Ships dark. |
| **Observability** | `LOGFIRE_TOKEN`, `LOGFIRE_ENVIRONMENT`, `OTEL_EXPORTER_OTLP_*`, `AGENT3_CHATLOG`, `AGENT3_CHATLOG_MAX_CHARS`, `OBS_SLOW_QUERY_MS`, `OBS_SLOW_AGENT_MS` | Optional tracing; unset = structured logs only. |
| **App** | `APP_BASE_URL`, `APP_ENV`, `FEEDBACK_RESOLVE_TOKEN` | CORS origins, env mode, feedback-resolve guard. |
| **Eval** | `EVAL_JUDGE_MODEL`, `EVAL_MIN_CONVO_PASS` | LLM-as-judge model; conversation pass gate. |
| **Marketing research** | `INSTAGRAM_*`, `X_BEARER_TOKEN`, `RESEARCH_OUT_DIR` | Local only. |

---

## The chat agent (agent3)

`api/agent3/` is the chat for every visitor (default since 2026-09-19;
`docs/auction-deep-agent-2026-08.md`). It runs on LangChain's `create_agent`
over LangGraph, with the transcript checkpointed in Neo4j under a `thread_id`
(`api/checkpointer.py`), so the client round-trips neither history nor a
scope object. The system prompt is `api/agent3/instructions.md`, kept
byte-identical across turns for prompt caching; per-turn material (loaded
skills, the matches panel) rides on the human message.

**Tools** (six graph tools + web):

| Tool | Purpose |
| --- | --- |
| `find_properties` | Any find / count / break-down. Filters reach into the notice (extent, possession, road width, encumbrance, identifiers, re-auction attempt). Returns `refine` and `relax` suggestions in the same call. |
| `get_property` | Full detail for one listing: schedule, extent, boundaries, possession, loan, EMD account, parties, and `gaps` (what the notice omits). |
| `search_notices` | AND-joined Lucene search over lot schedule text (`lot_description_ft`) and the portal blurb (`property_text_idx`). |
| `find_by_identifier` | Survey, patta, door, plot or CERSAI number. |
| `benchmark_price` | ₹/sqft against comparable single-lot notices; refuses with a reason otherwise. |
| `reauction_history` | The re-auction chain, attempt numbers and previous reserves. |
| `internet_search` | Tavily, for off-graph context only; sources become citation chips. |

**Skills** (`api/agent3/skills/`, loaded by trigger phrase, no tool call):
`bidding`, `diligence`, `extent`, `identifiers`,
`possession-and-encumbrance`, `pricing`, `reauction`.

**Gates.** The answer gate re-reads a draft against rules checked in code
(no invented numbers, scope honesty, no valuations) and makes the model
rewrite, at most one repair call; `GateOut.repairs` counts how often. An
intent gate refuses bulk personal-data harvesting before any token is
spent. A turn is capped at six model calls.

**Endpoints.** `POST /chat/agent3`, `POST /chat/agent3/stream` (SSE:
status / delta / manifest / final), `GET /chat/agent3/{thread}/history`,
`GET /chat/agent3/{thread}/manifests` (turn-owned property cards),
`DELETE /chat/agent3/{thread}`. Threads carry an owner so one visitor cannot
read another's conversation.

**Other loops (admin / eval only).**

| Loop | Endpoint | Memory | Status |
| --- | --- | --- | --- |
| v1 pydantic-ai (`api/agent.py`, `modes/_shared.md`) | `POST /chat`, `/chat/stream` | transcript round-tripped by client | Retired from the UI; still powers `evals/run_golden`, `run_conversations`, and the `deep-research` mode spec |
| Tiered plan → execute → synthesize (`api/chat/v2`) | `POST /chat/v2` | `scope` summary | Admin only, `/lab` |
| Deep Agents ReAct (`api/chat/deep`) | `POST /chat/deep` | Neo4j transcript | Admin only, `/lab` |

The A/B that chose agent3 is in `docs/chat-loop-ab-2026-08.md` and
`docs/auction-deep-agent-2026-08.md` §10 (15.8 s median turn vs 25 s tiered
and 149 s deep). The UI still offers an **Ask / Deep research** picker; on
agent3 a deep pass is the `diligence` skill.

**Tiers & quotas** (`api/chat/gating.py`, `api/model_selection.py`):

| Tier | Chats/day | Chats/month | Model | Reasoning effort |
| --- | --- | --- | --- | --- |
| Anonymous | 10 | 30 | Flash | low (server-capped) |
| Free (signed-in) | 10 | 300 | Flash | low (server-capped) |
| Pro | 100 | unlimited | Pro (or Flash) | user-selectable up to xhigh |

Every tier sees the same graph through chat; Pro buys turns and the full
property page. Pro is a one-time **₹499**, **30-day** unlock via Razorpay;
the HMAC-verified, idempotent webhook is the only thing that activates it.

---

## Pipelines

There are five, each run on its own cadence. The weekly data run and the
notice enrichment run on a workstation; extraction, entity resolution and
the audits also run unattended.

### 1. Weekly data pipeline (workstation)

`scripts/run_weekly_pipeline.py` chains the steps below with per-stage env
pre-flight checks, a log under `logs/`, and stop-on-first-failure.
`--skip-scrape` starts at step 3. `scripts/run_weekly_pipeline.bat` wraps it
for Windows Task Scheduler. Scraping needs a visible Chrome so a human can
clear Cloudflare's CAPTCHA; the run waits and continues.

```bash
python scrapers/phase1_harvest_urls.py       # 1. eauctionsindia listing URLs (Selenium)
python -u scrapers/phase2_scrape_details.py  # 2. detail pages + notice downloads
python -m scripts.prepare_tn_data            # 3. eauctionsindia → data/listings/eauctionsindia.jsonl
python -m scripts.harvest_sources            # 3b. BAANKNET + bankeauctions (plain HTTPS, no browser)
                                             #     raw → data/raw/<source>/, normalized → data/listings/
python -m scripts.gap_report                 # 3c. read-only: what each portal adds
python -m scripts.load_tn_to_neo4j           # 4. every data/listings/*.jsonl → :AuctionProperty (+:Media)
python -m scripts.upload_downloads_to_r2     # 5. notices + live listings' photos → R2
python -m pipeline.run_pipeline              # 6. graph-side stages (below)
python -m scripts.init_graph_schema          #    constraints + fulltext indexes (idempotent, additive)
```

> **Caveat:** `run_weekly_pipeline.py` still lists a seventh stage,
> `pipeline.embed_descriptions`, which was removed when embeddings were
> retired (`docs/design/2026-08-22-retire-embeddings.md`). The orchestrator
> will mark that final stage FAILED after everything else has succeeded; the
> data is fine. Drop the stage or ignore the final status until it is removed.

`pipeline.run_pipeline` (flags `--pilot`, `--limit N`, `--skip-classify`)
runs, in order:

| Stage | Module | What it does |
| --- | --- | --- |
| 1.3 | `pipeline.classify_notice` | Tag each `:Document` single/multi from how many listings link to it; a reviewer's override is never overwritten |
| 4.4 | `pipeline.promote_extractions` | Grounded extractions → `:Lot` (and the derived `:Parcel` layer, being retired) |
| 4.5 | `pipeline.apply_extractions` | Per-lot values, descriptions and agreement verdicts → `:AuctionProperty`; `write_lot_matches` decides which lot a listing *is* (reserve → EMD → borrower → identifiers; refuses a lot two listings claim) |
| 5 | `scripts.link_reauctions` | `:SAME_PROPERTY_AS` across re-listings |
| 5a | `scripts.link_listings` | `:SAME_LISTING_AS` across portals (`sources/match.py`); only CONFIRMED / PROBABLE edges bridge |
| 5b | `scripts.build_spine` | One `:AuctionEvent` per cluster, rebuilt from the branches every run, with per-field provenance and `core_complete` |
| 5c | `scripts.link_reauctions --events` | Chain re-auctioned events; stamp `attempt_no` / `previous_reserve` |
| 6 | `describe_schema(refresh=True)` | Refresh the `:SchemaCache` node |

**Sources.** Each portal is an adapter in `sources/` producing one `Listing`
shape. Ids: eauctionsindia stays bare, BAANKNET is `bn-…`, bankeauctions is
`be-…`. Merge rank: BAANKNET 1, bankeauctions 2, eauctionsindia 3.
`api/canonical.py` keeps one copy per bridged cluster in every list and
reports the rest as `also_on`. Design: `docs/superpowers/specs/2026-09-12-source-adapters-design.md`;
recon: `docs/source-recon-2026-09.md`.

### 2. Notice enrichment (OCR → classify → extract → review → graph)

Turning one sale notice into graph rows passes through three human gates in
`web/review.html`. Machines do the volume; a person confirms the few facts
everything downstream depends on.

| # | Step | Who | Where |
|---|------|-----|-------|
| 1 | Classify single- vs multi-property from the scraped cluster count | machine | `pipeline/classify_notice.py` |
| 2 | **Gate 1** — confirm the type **and the lot count** (`Document.expected_lot_count`) | human | review UI, *classification* |
| 3 | OCR the notice into layout-aware markdown | machine | `scripts/ocr_missing_markdowns.py` (Datalab default, MinerU option) |
| 4 | **Gate 2** — check OCR quality; re-OCR, crop, rotate or annotate blocks | human | review UI, *markdown* |
| 5 | Extract grounded entities with LangExtract (every value carries its character span) | machine | `pipeline/load_extractions.py` — Render cron every 6 h, `--stale` re-reads; or the review UI's re-run |
| 6 | **Gate 3** — clear the per-lot key-entity checklist (reserve price, auction date, property type, location, extent, full description, possession); lot-count mismatch flagged | human | review UI, *extraction* |
| 7 | Resolve entities into `:Lot` | machine | `pipeline/promote_extractions.py` |
| 8 | Apply grounded fields + descriptions to `:AuctionProperty` | machine | `pipeline/apply_extractions.py` |

What makes the extraction step trustworthy, beyond the gates:

- **One page is read once.** Portals name uploads by the millisecond, so one
  notice against six lots is six files with identical bytes. OCR keys on
  `content_sha256` and extraction on the markdown hash
  (`pipeline/notice_twins.py`); copies get the result, not a second bill.
  Two-sheet notices are joined so the model sees the whole notice
  (`scripts/stitch_sibling_pages.py`, kept fresh by `pipeline/stitch_refresh.py`).
- **OCR health gate.** `pipeline/ocr_health.py` scores markdown for
  repetition loops, token leaks, truncation, foreign script, table collapse
  and near-empty pages; a page under 90 is stamped for re-OCR instead of
  being read. `scripts/reocr_low_health_datalab.py` re-OCRs with a
  strict-improvement gate.
- **Reads only ever improve a notice.** `pipeline/keep_better.py` refuses a
  re-read that loses a fact; `extraction_prev_json` + `scripts/revert_extraction.py`
  undo one. Reviewer corrections are carried across re-reads by stable
  entity ids (`pipeline/extraction_ids.py`).
- **Repairs without a full re-read.** `scripts/fill_gaps.py` asks a lean
  prompt for just a lot's missing key facts; `pipeline/absence.py` marks a
  fact *not in the notice* so no later run pays for it again;
  `pipeline/widen_descriptions.py` stretches a description over the details
  its span stopped short of, with no model call.
- **Lot-aware chunking** for long multi-lot notices (`pipeline/lot_chunks.py`,
  `lot_windows.py`), per-notice-type model routing (`pipeline/extract_routing.py`),
  and a retry ladder for half-read lots.
- **Reader v2** (`pipeline/reader/`, `docs/reader-v2.md`) — a lot-first,
  schema-locked, code-grounded reader. **Parked** since 2026-10-03: it agreed
  with the current reader on 95% of key values and failed on a 40-lot
  scanned table. `EXTRACT_READER=v2|shadow` turns it back on.
- **Gate 3 is machine-judged in the funnel.** A notice advances past
  extraction when it is *clean* (every key cell filled or marked absent, no
  validator issue, extracted from the current markdown, lot count matching
  the reviewer's), not when someone clicks verify. The verify flag records a
  human read and feeds the eval gold set.

The funnel (`GET /review/pipeline`) counts documents clearing each stage:
scraped → classified → classification reviewed → OCR'd → OCR reviewed →
entities extracted → extraction clean → entities resolved → resolution
reviewed. Each stage page breaks held-back notices down by check and opens
the queue filtered to that worklist.

**Spot-check audit** (`web/spotcheck.html`, `api/review/spotcheck.py`,
`pipeline/spotcheck.py`) is the honest measuring stick: a seeded random
sample of atomic claims (span and attribute) over a stated population,
stored on its own `:SpotCheckSample` node and never written back to a
document, reported with a Wilson interval. It measures precision; recall is
measured by the gold set (below).

The graph model all of this writes — `:Document` → `:Lot`, where each
extracted field lands, provenance, boundaries — is in `docs/SCHEMA.md`.

### 3. Entity resolution (lenders, branches, places, lots)

LangExtract stores names as the notice printed them, so the corpus held 199
spellings for ~130 lenders and village names that match several revenue
villages. Resolution gives each its one identity and keeps the raw string
for audit.

| Pass | Script | Writes |
| --- | --- | --- |
| Lenders | `scripts/resolve_bank_names.py` | `Document.bank_canonical`; safe rule = normalized token-set equality + one misread OCR token; lookalikes go to a review list |
| Branches | `scripts/resolve_branches.py` | `Document.branch_canonical`, scoped per bank |
| Places | `scripts/resolve_places.py` (+ `pipeline/resolve_places.py` for scraped City/Area) | `revenue_district / taluk / village`, `LOCATED_IN_*` edges, conflict flags; bottom-up and district-scoped so the 2019 district splits don't mis-file properties |
| Lot matches | `scripts/resolve_lots.py` | Applies a reviewer's lot-match decisions (`:ResolutionDecision`) to listings |

Gazetteers in `pipeline/lookups/`: LGD towns, India Post PIN→taluk, SRO→taluk,
Census 2011 taluk lineage, taluk neighbours, OSM village aliases
(refreshed by `scripts/refresh_village_gazetteer.py` and friends).

The review UI's **resolution queue** (`/review/resolution`) shows lookalike
pairs and conflicts; the **village queue** (`/review/resolution/villages`)
takes one verdict per spelling per taluk, including "pick several" for split
villages. "Apply my decisions" runs the resolvers; the Sunday
`resolve-entities.yml` workflow is the safety net that applies stored
verdicts if nobody presses it. `resolve-scorecard.yml` publishes a read-only
weekly scorecard (linkage, places, agreement, contested fields, queue size)
and diffs it against last week's artifact.

### 4. SEO and content pipeline

All generators read the live API and write static HTML into `web/`; the
sitemap is rebuilt from the filesystem by `scripts/seo_sitemap.py`.

| Output | Script | Notes |
| --- | --- | --- |
| `/property/<id>/` | `scripts/prerender_properties.py` | SPA shell + per-property title/OG/JSON-LD + static content block; `--refresh` restates pages whose auctions closed |
| `/bank-auctions/<city>/<type>/` | `scripts/build_landing_pages.py` | Written only with enough live listings; real computed figures only |
| `/guides/<slug>/` | `scripts/build_guides.py` | Long-form guides with Article + FAQ JSON-LD |
| `/compare/auctionscope-vs-<portal>` | `scripts/build_compare.py` | Honest comparison pages |
| Per-property OG cards | `scripts/generate_property_og.py` | 1200×630 PNGs to R2 + `web/og-manifest.json`; no authored copy |
| `/llms.txt`, `robots.txt`, `sitemap.xml` | hand-maintained / `seo_sitemap.py` | |

`seo-pages.yml` (manual dispatch, after a scrape) renders OG cards, refreshes
closed-auction pages, regenerates property + landing pages for the given
cities (or `all_live`), and opens a **draft PR** on `automated/seo-pages`. It
never pushes to main. Copy rules: `docs/marketing/copy-playbook.md`.

### 5. Content-ops agents

Spec: `docs/marketing/content-agents.md`. Both agents **draft and stage
only**; nothing is ever posted automatically.

- **Poster** (`marketing_agents/poster.py`, `content-poster.yml`, manual
  dispatch after a data refresh). Reads `/stats` and `/properties`, writes
  3–5 drafts (price drops, closing soon, cheapest-by-city, a city carousel)
  through `--prepare` → engine → `--finalize`. Engines: Claude Code on the
  founder's Max subscription (default, `CLAUDE_CODE_OAUTH_TOKEN`) or
  OpenRouter. `validate_drafts()` enforces the honesty rule (a figure in
  every post, banned words, hook length). Cards render via
  `marketing/render_social.py` (Playwright), reels via
  `marketing/render_reel.py` (HyperFrames, pinned), both uploaded to R2.
  Drafts land in `marketing/outputs/<date>/`, a "content-review" issue is
  opened, and `web/social.html` (`/social/*` API, admin) is where a human
  approves or rejects each item.
- **Reporter** (`marketing_agents/reporter.py`). Turns an exported
  post-metrics CSV + `/stats` into a one-page weekly report; every number is
  computed in Python and the LLM only interprets. Not yet on a schedule.
- `marketing/dashboard.html` is the living system-of-record for channels,
  agents, KPIs and roadmap; any marketing change updates its data block in
  the same PR (see `CLAUDE.md`).

---

## API surface

Mounted in `api/main.py`. Selected endpoints:

**Public**

- `GET /health`, `GET /health/deep` — liveness + readiness (Neo4j, fulltext
  indexes, `last_enriched` freshness).
- `GET /stats` — coverage + freshness snapshot.
- `GET /properties` — browse with cascading facets; `GET /auction/{id}`
  (free fields only unless Pro); `GET /auction/{id}/notice`.
- `POST /chat/agent3`, `POST /chat/agent3/stream`, `GET …/{thread}/history`,
  `GET …/{thread}/manifests`, `DELETE …/{thread}`.
- `GET /chat/models` — tier-aware model + reasoning-effort registry;
  `GET /modes`; `GET /suggestions`.
- `GET|POST /alerts`, `POST /alerts/subscribe` — deadline reminders.
- `POST /feedback`, `GET /feedback/recent`, `PATCH /feedback/{id}/resolve`.
- Legacy / admin loops: `POST /chat`, `/chat/stream`, `/chat/v2[/stream]`,
  `/chat/deep[/stream]`.

**Authenticated** (Supabase JWT)

- `GET|PATCH /auth/me`; `GET /watchlist`, `POST|DELETE /watchlist/{id}`;
  `GET|PUT|DELETE /conversations[/{id}]`.
- `POST /billing/order`, `POST /billing/verify`, `POST /billing/webhook`.
- `/dossiers/*` — private per-property document locker (only when
  `DOSSIERS_ENABLED=true`).

**Admin**

- `GET|PATCH /admin/users[/{id}]`, `GET /admin/feedback`.
- `/review/*` — classification, markdown and property queues; `classify`,
  `verify` / `edit` / `unverify`; block annotation (`/notice/{file}/blocks…`),
  crop, rotation, ink coverage, source streaming; `/pipeline[/{stage}]`
  funnel; `/resolution`, `/resolution/villages`, `decide` / `undo` / `apply`.
- `/review/extraction/*` — the extraction queue, per-field edits,
  `add-field`, `key-absent`, `rerun`, `bulk-confirm`, `stats`.
- `/review/spotcheck/*` — draw a sample, step through items, record
  verdicts, report.
- `/social/*` — staged batches, item status, proxied assets and reels.

---

## Testing & evaluation

```bash
pytest tests/api -q            # FastAPI endpoint + agent3 unit tests (the CI gate)
pytest tests/pipeline -q       # pipeline unit tests (not all green; CI picks a subset)
pytest tests/sources -q        # portal adapters, matcher, spine
ruff check .                   # lint (correctness rules only; see pyproject.toml)
```

- **CI** (`.github/workflows/ci.yml`) on every PR and push to `main`:
  `ruff`, `pip-audit` over `requirements.lock`, and pytest over `tests/api`,
  `tests/scripts`, `tests/test_vercel_config.py`, `tests/sources` and a named
  set of `tests/pipeline` modules, against an in-memory Neo4j stub. A
  separate `e2e` job runs the live Razorpay test-mode flow against a real
  Neo4j service container and goes red when the `RAZORPAY_*` secrets are
  missing. `TODOS.md` records one known-red test on `main`.
- **Agent evals** (`evals/`):
  - `python -m evals.run_agent3` — the agent3 tool catalogue against the live
    graph (no model; a failure is a tool or data bug). `smoke_agent3.py`
    drives a real model.
  - `python -m evals.run_golden` — single-turn golden questions through the
    v1 agent, scored on tool trajectory + LLM-as-judge (`golden.yml`, manual).
  - `python -m evals.run_conversations` — multi-turn narrowing, carry-over,
    no-stale-scope (`golden-conversations.yml`, manual).
- **Extraction evals**:
  - `python -m evals.langextract_eval --reader langextract|v2 --repeats 3` —
    field accuracy against the hand-labelled gold set
    (`evals/langextract_gold.py`, `evals/fixtures/`), with repeats because a
    single run cannot tell a prompt change from noise.
  - `evals/export_review_gold.py` turns reviewer-verified notices into gold;
    `evals/gold_sprint_v1.md` is the 40-notice verification sprint.
  - `evals/contextgem_eval.py` — A/B of LangExtract vs a two-stage
    segment-then-extract workflow (findings in `evals/CONTEXTGEM_FINDINGS.md`).
  - `scripts/ocr_ab.py` / `ocr-ab.yml` — MinerU vs Datalab side by side on
    pasted notice URLs.

---

## Automation (GitHub Actions + Render cron)

| Workflow | Trigger | Does |
| --- | --- | --- |
| `ci.yml` | PR, push to main | lint, audit, tests, e2e |
| `data-freshness.yml` | Mondays 06:00 UTC | opens/updates an issue when `/stats.last_enriched` is older than 14 days |
| `r2-consistency.yml` | Mondays 06:30 UTC | fails when a `:Document` points at a missing R2 object |
| `resolve-entities.yml` | Sundays 21:30 UTC | applies stored verdicts: lenders → branches → places |
| `resolve-scorecard.yml` | Sundays 22:00 UTC | read-only trust scorecard, diffed against last week |
| `resolve-feedback.yml` | merged PR | marks feedback resolved from `Resolves feedback: <uuid>` in the PR body |
| `sync-feedback.yml` | manual | snapshots `/feedback/recent` into `feedback/*.json` (was every 15 min; that exhausted the Actions budget) |
| `golden.yml`, `golden-conversations.yml` | manual (nightly schedule paused) | agent evals |
| `seo-pages.yml` | manual, after a scrape | regenerate SEO pages → draft PR |
| `content-poster.yml` | manual, after a scrape | Poster drafts → staged commit + review issue |
| `ocr-ab.yml` | manual | OCR engine comparison |
| `cleanup-duplicate-docs.yml` | manual | duplicate `:Document` report / cleanup |
| Render cron `auction-extract` | every 6 h | `pipeline.load_extractions --workers 24 --max-seconds 19800 --stale` |

`AGENTS_ENABLED=false` (repo Actions variable) is the kill switch for the
SEO and Poster workflows.

---

## Observability & health

Latency-sensitive paths emit greppable structured logs via
`api/observability.py`:

```
auction.obs neo4j.run_read_query status=ok elapsed_ms=42 rows=18 access=read
auction.obs chat.agent_run status=ok elapsed_ms=2100 mode=ask llm_calls=2 ...
```

Slow operations log at WARNING above `OBS_SLOW_QUERY_MS` (default 1500) and
`OBS_SLOW_AGENT_MS` (default 12000). Every chat turn logs a token / cache /
cost summary.

**Tracing.** Set `LOGFIRE_TOKEN` and `api/telemetry.py` emits a full
OpenTelemetry trace per turn (request → agent → each LLM and tool call).
Unset, it's a no-op. The transport is OTLP, so `OTEL_EXPORTER_OTLP_*` can
point at any backend instead.

**agent3 chat transcripts.** Each turn also emits an `agent3.chatlog` line
with the question, answer and every tool step as attributes, with `outcome`
`ok` / `error` / `cancelled`, so a turn is readable in Logfire without opening
the Neo4j checkpoint. `AGENT3_CHATLOG=0` switches capture off;
`AGENT3_CHATLOG_MAX_CHARS` (default 4000) caps each field.

Point uptime monitoring at `GET /health/deep`.

---

## Deployment

- **Frontend** → **Vercel**, serving `web/` statically (`vercel.json`: CSP,
  cache headers, SPA rewrites for `/chat`, `/property/:id`, `/watchlist`,
  `/dossiers`, `/lab`).
- **Backend** → **Render** (`render.yaml`): web service `auction-api`
  (starter plan, Singapore, `pip install -r requirements.lock`,
  `preDeployCommand: python -m scripts.init_auth_schema`, health check
  `/health`) and cron `auction-extract`.
- **Database** → **Neo4j Aura**. **Auth** → **Supabase**.
  **Files** → **Cloudflare R2**. **Payments** → **Razorpay**.
  **Tracing** (optional) → **Logfire** / any OTLP backend.

Browser page loads on `api.auctionscope.in` or `*.onrender.com` are
301-redirected to `www.auctionscope.in` so the Supabase session lives on one
origin; API routes answer on every host.

---

## Dependencies

- `requirements.txt` — production ranges (`>=x,<next-major`).
- `requirements.lock` — fully pinned transitive lock used by Render and CI;
  regenerate after editing `requirements.txt` (steps in the lock's header).
- `config/requirements.txt` — full local-dev set (scraping, OCR, reports,
  evals).
- Python 3.11 in production and CI (`render.yaml`, `ci.yml`).
