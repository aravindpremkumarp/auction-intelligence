# Bank Auction Intelligence

**Production:** <https://www.auctionscope.in>

An AI intelligence platform for Indian **SARFAESI** bank-auction property. It
scrapes public auction listings, builds a **Neo4j knowledge graph** of Tamil
Nadu auctions (~2,200 enriched; ~600 live at any time — the live count is at
`GET /stats`), enriches
each listing with OCR + vision-LLM extraction of the source sale notices, and
serves a **PydanticAI agent** behind a chat UI that lets you find, compare,
and analyze investment opportunities in natural language (saving/tracking
lives in the app UI).

```
scrape → filter TN → load Neo4j → OCR + vision-LLM extract → classify notices →
verify/enrich → apply LangExtract descriptions → serve agent + web UI →
human feedback + review loop
```

---

## What it does

- **Conversational search** over the graph — "residential auctions in Chennai
  under 30 lakhs", "what's the price range in Kanchipuram?", "which borrowers
  have more than 3 properties?". Every answer is grounded in a tool call; the
  agent never invents prices, counts, or IDs.
- **Qualitative text search** over notice content — boundaries,
  neighbourhood, legal caveats, condition — across two Lucene fulltext
  indexes (lot schedule text, property description), BM25-ranked and merged.
- **Paste-a-listing matching** — drop a WhatsApp forward or broker blurb and the
  agent anchors it to the right auction by reserve price + date.
- **Web-search enrichment** — the agent can answer questions the sale notice
  can't: locality water / groundwater, waterlogging & flood signals, existing +
  upcoming govt/private projects, transport (metro/bus/connectivity), nearby
  schools/hospitals, approximate location, and market price vs. the reserve —
  via `internet_search`, cited. (Web-researched and approximate, not legal advice.)
- **Deep-research mode** (login-gated) — a structured, cited research report on
  one auction: market comparables, location intelligence, re-auction price
  history, and notice red flags. (Seven further specs — scan/shortlist/evaluate/
  track/refresh/compare/report — are archived, not live.)
- **Accounts** — Supabase auth, a saved-property **watchlist**, and persisted
  **conversations** (including per-property chats).
- **Re-auction awareness** — every result row carries `is_reauction`,
  `reauction_count`, and `previous_reserve_price` so price-drop questions are
  answered from the rows directly.
- **Enrichment review surface** — an admin UI with a gate per pipeline stage:
  confirm each notice's type and lot count (classification), grade OCR/markdown
  quality, and check LangExtract's output — where a lot-count mismatch against
  the reviewer's count is flagged. Plus per-property description verify/edit,
  block-level annotation, and region re-extract.
- **Feedback loop** — thumbs up/down on any reply flows into Neo4j and is
  auto-synced into the repo for triage.

---

## Architecture

```
                Vercel (static web/)                Neo4j Aura
              ┌────────────────────┐            ┌──────────────┐
  Browser ───▶│ index.html  app.js │            │  knowledge   │
              │ styles.css  auth.js │            │    graph     │
              └─────────┬──────────┘            └──────▲───────┘
                        │  fetch (API_BASE)            │ Bolt / HTTPS
                        ▼                              │
              ┌────────────────────────────────────────┴──────────┐
              │            Render — FastAPI (api/main.py)          │
              │  routers: chat · properties · feedback · health    │
              │  auth-gated: auth · watchlist · conversations ·    │
              │              review                                │
              │  PydanticAI agent ─▶ OpenRouter (DeepSeek V4 Pro) │
              │  cypher tools · semantic search · web search       │
              └───┬───────────────┬───────────────┬───────────────┘
                  │               │               │
            Supabase (JWT)   Cloudflare R2    OpenRouter / Google /
            auth + JWKS      sale notices     Tavily  + Logfire (OTel)
```

- **Backend** — FastAPI. `api/main.py` is a thin composition root (CORS,
  rate-limit, exception handlers, static serving); endpoint logic lives in
  focused routers. The agent (`api/agent.py`) is a PydanticAI agent wired to
  OpenRouter, with its schema/tool-routing rules loaded from `modes/_shared.md`.
- **Frontend** — single-page app, **no build step**: vanilla JS + hand-written
  CSS, split into `web/index.html` (markup), `web/styles.css`, `web/app.js`
  (behaviour), `web/auth.js` (Supabase auth), `web/billing.js` (Razorpay
  checkout), and `web/dossiers.js` (dossier UI, dark by default). Plus
  `admin.html` and `review.html` for the admin/review surfaces.
- **Auth** — Supabase handles signup/login/reset on the client; the backend
  verifies each access token against Supabase JWKS and mirrors the user as a
  Neo4j `:User` node. Auth-gated routers are skipped entirely when
  `AUTH_ENABLED=false`, so the app boots for offline dev without Supabase.
- **Data** — Neo4j Aura (hosted graph). Sale-notice PDFs/images live in a public
  **Cloudflare R2** bucket and are linked straight from the UI.
- **Local-only tooling** — Selenium scraping and the OCR/MinerU enrichment
  pipeline run on a workstation, not in production.

---

## Repository layout

```
api/          FastAPI composition root (main.py) + routers:
              chat/ properties/ feedback/ health/ — always on;
              auth/ watchlist/ conversations/ review/ — auth-gated.
              agent.py (PydanticAI agent), neo4j_client.py, telemetry.py,
              observability.py, tools/ (Cypher + web search).
pipeline/     Enrichment pipeline: OCR/MinerU, vision-LLM extraction, notice
              classification, verify/enrich, normalize, LangExtract entity
              promotion, R2 storage helpers.
scrapers/     Selenium scrapers for eauctionsindia.com (local only).
scripts/      Data-prep, migration, backfill, and one-off maintenance scripts.
scoring/      Ten-dimensional investment scoring (auction_scorer.py) — offline
              only; not wired into the live API.
tracking/     Eight-state investment-pipeline tracker — offline only.
modes/        Agent prompt files — _shared.md (schema + rules) + per-mode specs.
evals/        pydantic-evals golden-question harness.
web/          Single-page frontend (index/styles/app/auth) + admin + review UIs.
redesign/     Standalone "Auctionscope" clean-UI prototype (vanilla HTML/CSS/JS).
config/       Full dev requirements, domain ontology + graph model, overview.
feedback/     Auto-synced snapshots of the live /feedback feed.
docs/         Design specs, plans, and the June 2026 code-review response.
tests/        pytest suites — tests/api (CI) + tests/pipeline + scraper probes.
```

A deeper, file-by-file tour lives in [`config/CODEBASE_OVERVIEW.txt`](config/CODEBASE_OVERVIEW.txt).

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate         # macOS/Linux
# .venv\Scripts\activate          # Windows

pip install -r config/requirements.txt   # full dev set (scraping + OCR + reports)
cp .env.example .env                      # then fill in real values

uvicorn api.main:app --reload
```

Open <http://localhost:8000>. The SPA resolves `API_BASE` to empty on
`localhost` (so it calls the same origin) and to the hosted Render URL otherwise.

Handy toggles for local/offline work:

- `AUTH_ENABLED=false` — boot without Supabase (skips auth/watchlist/
  conversations/review routers).
- `RATELIMIT_DISABLED=1` — drop the anonymous-chat throttle.
- `NEO4J_HTTP_API=1` — route Neo4j over Aura's HTTPS Query API when Bolt
  (port 7687) is blocked by an egress proxy.

---

## Configuration

Copy `.env.example` → `.env`. Never commit the filled-in file. Key groups:

| Group | Vars | Notes |
| --- | --- | --- |
| **LLM** | `OPENROUTER_API_KEY`, `OPENROUTER_CHAT_API_KEY`, `OPENROUTER_MODEL`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | OpenRouter runs the agent (**DeepSeek V4 Pro**, Flash on the free tier) and OCR extraction (`gemini-2.5-flash`); Google key powers LangExtract's Gemini backend. `OPENROUTER_CHAT_API_KEY` caps chat spend apart from the pipeline. |
| **Graph** | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | Neo4j Aura. |
| **Auth** | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `AUTH_ENABLED`, `ADMIN_BOOTSTRAP_EMAIL` | Anon key is browser-safe; service-role key is server-only (admin bootstrap script). |
| **Storage** | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` | Public Cloudflare R2 bucket serving sale notices. |
| **Observability** | `LOGFIRE_TOKEN`, `LOGFIRE_ENVIRONMENT`, `OTEL_EXPORTER_OTLP_*`, `AGENT3_CHATLOG`, `AGENT3_CHATLOG_MAX_CHARS` | Optional OpenTelemetry tracing; unset = no-op. `AGENT3_CHATLOG=0` stops agent3 chat transcripts being exported (default on, 4000 chars per field). |
| **Eval** | `EVAL_JUDGE_MODEL` | LLM-as-judge model for the golden eval. |
| **App** | `APP_BASE_URL`, `APP_ENV`, `FEEDBACK_RESOLVE_TOKEN`, `RATELIMIT_DISABLED` | CORS origins, env mode, feedback-resolve guard. |
| **Scraping** | `FINDAUCTION_EMAIL`, `FINDAUCTION_PASSWORD` | Local-only; not needed in production. |

---

## The agent

`api/agent.py` builds the PydanticAI agent. Its system prompt is a short role
statement plus the whole of `modes/_shared.md` (graph schema, enum lists,
tool-routing rules, a Cypher cheat-sheet). Four dynamic instructions augment each
turn: the rolling **active search scope** carried across turns, the
**matches-panel selection**, a **mode overlay** when the client requests one, and
the **live graph size**. The agent runs **DeepSeek V4 Pro** by default (a Flash
variant on the free tier).

**Tools** (read-only against the graph + web):

| Tool | Purpose |
| --- | --- |
| `search_auctions` | Filter by price / EMD / city / area / type / category / bank / borrower / platform / date; `deadline_within_days` for upcoming deadlines; supports aggregates (min/max/avg/median/p25/p75), `group_by` distributions, and true `total_count`. |
| `semantic_search` | Lucene fulltext across lot schedule text + property description, BM25-ranked and merged in one call. |
| `get_auction_detail` | Full records for one or a list of `auction_id`s (up to 10 per call), including re-auction `price_history`. |
| `describe_schema` | Live graph introspection (labels, rels, enums, ranges); 1-hour cache. |
| `run_cypher` | Read-only Cypher escape hatch — write clauses rejected, 10 s / 500-row caps. |
| `internet_search` | Tavily web search for off-graph context — locality water/flood signals, govt/private projects, connectivity, schools/hospitals, market context. Cited, approximate. |
| `query_user_dossier` | (dossiers build only) Q&A over a signed-in user's own uploaded documents for one property. |

**Modes** (`modes/*.md`): `deep-research` (login-gated) plus the default
`ask`. Seven further specs — `scan`, `shortlist`, `evaluate`, `track`,
`refresh`, `compare`, `report` — are parked in
[`modes/_archive/`](modes/_archive/) (not wired into the UI; see that folder's
README to re-activate).

The graph is modelled around `AuctionProperty`, with `Bank`/`Branch`,
`City`/`Area`/`State`, `AssetCategory`, `PropertyType`, `Borrower`, and
`AuctionType` reference nodes, plus enrichment nodes (`Score`,
`InvestmentTracker`, `Feedback`, `User`, `Document`). See
[`config/domain_ontology.yaml`](config/domain_ontology.yaml) for the full schema.

---

## Accounts, tiers & billing

- **Auth** — Supabase (client signup/login/reset; backend verifies each JWT via
  JWKS and mirrors the user as a Neo4j `:User`).
- **Tiers & quotas** (`api/model_selection.py`, `api/chat/router.py`):

  | Tier | Chats/day | Chats/month | Model | Reasoning effort | Deep-research |
  | --- | --- | --- | --- | --- | --- |
  | Anonymous | 10 | 30 | Flash | low | — |
  | Free (signed-in) | 20 | 100 | Flash | low | ✓ (login-gated) |
  | Pro | 1,000 | unlimited | **Pro** | high / xhigh | ✓ |

- **Billing** — Razorpay. Pro is a one-time **₹499**, **30-day** unlock
  (`RAZORPAY_PLAN_AMOUNT` / `RAZORPAY_PLAN_DAYS`); the **webhook is the sole
  activation path** (HMAC-verified, idempotent).
- **Deadline alerts** — `GET|POST /alerts`: 7-day-window reminders for
  saved/watchlisted properties, surfaced as a bell badge in the UI.
- **Dossier** (ships dark; `DOSSIERS_ENABLED=false`) — a private per-property
  **document locker**: a signed-in user uploads *their own* collected documents,
  which the app OCRs and auto-classifies across a 9-category taxonomy with a
  completeness tracker. It organises the user's documents; it is **not** automated
  legal/title diligence.

---

## Data & enrichment pipeline

The pipeline (`pipeline/`, run locally) turns raw scraped listings into enriched
graph data. Orchestrate it with `python -m pipeline.run_pipeline` (flags:
`--pilot`, `--limit N`, `--skip-ocr`, `--skip-descriptions` (skips the
notice-classification stage), `--verify-only`, `--legacy`).

**One command to run it all (weekly batch job):** `C:\Python314\python.exe
scripts\run_weekly_pipeline.py` chains steps 1-7 below end-to-end, with
logging to `logs\pipeline_run_<timestamp>.log`, pre-flight env-var checks, and
stop-on-first-failure behavior. Scraping (steps 1-2) stays local and
semi-manual — a visible Chrome window opens so a human can solve Cloudflare's
CAPTCHA if it appears; the run pauses and waits, then continues automatically
once solved. Pass `--skip-scrape` to start from step 3 using whatever is
already in `data/live_eauction_data.jsonl` (e.g. if you already scraped
manually earlier in the week). `scripts\run_weekly_pipeline.bat` wraps this
for Windows Task Scheduler.

The manual step list below is what `run_weekly_pipeline.py` does internally —
useful as a reference, or for running any single stage by hand:

```bash
python scrapers/phase1_harvest_urls.py      # 1. Harvest listing URLs (Cloudflare may need a human)
python -u scrapers/phase2_scrape_details.py # 1b. Scrape each URL's detail page + downloads
python -m scripts.prepare_tn_data           # 2. Clean + filter the Tamil Nadu subset
python -m scripts.load_tn_to_neo4j          # 3. Load the base graph
python -m scripts.upload_downloads_to_r2    # 4. Push sale notices to R2
python -m pipeline.run_pipeline             # 5. OCR → classify → verify →
                                            #    load → apply extractions
                                            #    (also links re-auctioned properties internally)
python -m scripts.init_graph_schema         # 6. Constraints + fulltext indexes
uvicorn api.main:app --reload               # 7. Serve agent + web UI
```

Notable stages: **OCR/extraction** uses MinerU + a vision LLM
(`ocr_extract.py`, `mineru.py`); **notice classification** splits single- vs
multi-property notices by cluster count, corrected by human review
(`classify_notice.py`); **descriptions** come from LangExtract's
`full_description` spans (`apply_extractions.py`); **verify/enrich** reconciles
scraped fields against the PDF (PDF wins, original kept as `<field>_scraped`).
There is no embedding stage: retrieval is structured filters over the
LangExtract entity graph plus two Lucene fulltext indexes
(`lot_description_ft`, `property_text_idx`) consumed by `semantic_search` —
see `docs/design/2026-08-22-retire-embeddings.md`.

### Sale notice → graph: the review workflow

Turning one sale notice into graph rows runs through three human gates in
`web/review.html`. Machines do the volume; a person confirms the few facts
everything downstream depends on.

| # | Step | Who | Where |
|---|------|-----|-------|
| 1 | Classify the notice: single- vs multi-property, from the scraped cluster count | machine | `pipeline/classify_notice.py` |
| 2 | **Gate 1** — confirm the type **and the lot count** | human | review UI, *classification* stage |
| 3 | OCR the notice into markdown (Datalab or MinerU) | machine | `scripts/ocr_with_mineru.py` |
| 4 | **Gate 2** — check OCR quality, re-OCR or annotate blocks if poor | human | review UI, *markdown* stage |
| 5 | Extract entities from the markdown with LangExtract | machine | `pipeline/load_extractions.py` |
| 6 | **Gate 3** — review the extraction; a lot-count mismatch is flagged | human | review UI, *extraction* stage |
| 7 | Resolve entities into the `:Lot` / `:Parcel` spine — *written, not yet run* | machine | `pipeline/promote_extractions.py` |
| 8 | Apply grounded fields + descriptions to `:AuctionProperty` | machine | `pipeline/apply_extractions.py` |

Step 7 has not been run against the live graph yet — `:Lot` and `:Parcel` are
still 0 nodes there, so today the workflow effectively ends at step 8, which
writes onto `:AuctionProperty` directly. See the status note at the top of
[`docs/SCHEMA.md`](docs/SCHEMA.md).

**The lot count is the thread tying gates 1 and 6 together.** At gate 1 the
reviewer confirms how many lots the notice actually sells; it is stored as
`Document.expected_lot_count` (confirming "single" implies 1, so most notices
cost no extra clicks). That number then does two jobs:

- **Before extraction** — it is injected into the LangExtract prompt, so the
  model is told how many lots to find and number (`lot_index` 1..N) instead of
  guessing.
- **After extraction** — gate 6 compares it against the distinct lots actually
  extracted and flags any mismatch, which is how a missed or invented lot gets
  caught instead of quietly reaching the graph.

Notices without a confirmed count are never flagged: no count means no claim.

The graph model these steps write into — `:Document` → `:Lot` → `:Parcel`,
and where each extracted field lands — is documented in
[`docs/SCHEMA.md`](docs/SCHEMA.md).

---

## API surface

Mounted in `api/main.py`. Selected endpoints:

**Public**

- `GET /health`, `GET /health/deep` — liveness + readiness (Neo4j, the two
  fulltext indexes, `last_enriched` freshness; `degraded` on any failure).
- `GET /stats` — public coverage + freshness snapshot.
- `GET /modes` — mode registry for the UI selector.
- `GET /properties` — browse listing with cascading facets + multi-select filters.
- `GET /auction/{id}` — full auction detail.
- `POST /chat` — run an agent turn (returns answer, tool artifacts, and the
  message history to replay next turn).
- `POST /chat/stream` — SSE streaming turn (tool progress + token streaming).
- `GET /chat/models` — tier-aware model + reasoning-effort registry.
- `GET|POST /alerts` — deadline alerts for saved/watchlisted properties.
- `POST /feedback`, `GET /feedback/recent`, `PATCH /feedback/{id}/resolve`.

**Authenticated** (Supabase JWT)

- `GET|PATCH /auth/me`.
- `GET /watchlist`, `POST|DELETE /watchlist/{id}`.
- `GET|PUT|DELETE /conversations[/{id}]`.
- `POST /billing/order`, `POST /billing/verify`, `POST /billing/webhook` —
  Razorpay Pro unlock (the webhook is the sole activation path).
- `GET|POST|DELETE /dossiers/*` — per-property document locker (mounted only
  when `DOSSIERS_ENABLED=true`).

**Admin**

- `GET|PATCH /admin/users[/{id}]`, `GET /admin/feedback`.
- `GET /review/*` — the enrichment-review surface: classification, markdown and
  extraction queues, `classify` (notice type + lot count), `verify` / `edit` /
  `unverify`, block-level annotation (`/notice/{file}/blocks…`), region
  re-extract, crop/rotation, reingest, and source streaming.

---

## Testing & evaluation

```bash
pytest tests/api -q          # FastAPI endpoint tests (the CI gate)
pytest tests/pipeline -q     # pipeline unit tests
```

- **CI** (`.github/workflows/ci.yml`) runs `pytest tests/api` on every PR and
  push to `main` against an in-memory Neo4j stub (no live DB needed) — ~260
  tests covering filters, guardrails, auth, feedback, and the review surface.
- **Golden eval** (`evals/`, `.github/workflows/golden.yml`) runs nightly: each
  catalogue question (`evals/cases.py`) goes through the real agent and is scored
  on **tool trajectory** (the gate) + a reference-free **LLM-as-judge** answer
  quality. Run locally with `python -m evals.run_golden` (needs OpenRouter +
  Neo4j creds).
- **Feedback automation** — `sync-feedback.yml` snapshots the live feed into
  `feedback/*.json` every 15 min; `resolve-feedback.yml` marks items resolved
  when a fix PR with `Resolves feedback: <uuid>` merges.

---

## Observability & health

Latency-sensitive paths emit greppable structured logs via
`api/observability.py`:

```
auction.obs neo4j.run_read_query status=ok elapsed_ms=42 rows=18 access=read
auction.obs chat.agent_run status=ok elapsed_ms=2100 mode=ask llm_calls=2 ...
```

Slow operations log at WARNING above env-tunable budgets — `OBS_SLOW_QUERY_MS`
(default 1500) for Neo4j and `OBS_SLOW_AGENT_MS` (default 12000) for the LLM
turn. `/chat` also logs a per-turn token/cache/cost summary.

**Tracing** — `pydantic-ai` is natively OpenTelemetry-instrumented. Set
`LOGFIRE_TOKEN` (from <https://logfire.pydantic.dev>) and `api/telemetry.py`
lights up a full trace per chat turn — request → agent run → every LLM call
(prompt, response, tokens, cost) → every tool call. Unset, it's a no-op. Because
the transport is OTLP, the same instrumentation can target any OTel backend
(LangSmith, Langfuse, Honeycomb) via `OTEL_EXPORTER_OTLP_*` instead.

**Agent3 chat transcripts** — every agent3 turn also emits an
`agent3.chatlog` line carrying the text of the turn: the user's question, the
answer, and each tool step (name, arguments, result) as Logfire *attributes*,
not buried in the message. That makes a turn readable where it was previously
only countable — the transcript itself lives in the Neo4j checkpoint, base64
per blob, which nobody reads:

```sql
SELECT start_timestamp,
       attributes->>'thread_'   AS thread,
       attributes->>'question'  AS question,
       attributes->>'answer'    AS answer,
       attributes->>'steps_json' AS steps
FROM records
WHERE attributes->>'op' = 'agent3.chatlog'
ORDER BY start_timestamp DESC LIMIT 20
```

This is user text leaving the box, so it has an off switch and a ceiling:
`AGENT3_CHATLOG=0` disables capture without a deploy (the token/latency lines
keep flowing), and `AGENT3_CHATLOG_MAX_CHARS` (default 4000) caps each field —
tool payloads get a quarter of that. Clipped values carry a `… (+N chars)`
suffix rather than looking complete.

Point uptime monitoring at `GET /health/deep`.

---

## Deployment

- **Frontend** → **Vercel** (serves `web/` as a static site; `vercel.json`).
- **Backend** → **Render** (Python web service; `render.yaml`, installs the slim
  root `requirements.txt`).
- **Database** → **Neo4j Aura** (hosted).
- **Auth** → **Supabase**. **Notices** → **Cloudflare R2**.
- **Tracing** (optional) → **Logfire** / any OTLP backend.

The SPA resolves the production API base at runtime, so a fresh deploy needs no
hand-edited URL.

---

## Dependencies

- `requirements.txt` — production (Render) install. Compatible-release ranges
  (`>=x,<next-major`) so deploys don't pick up surprise breaking upgrades.
- `requirements.lock` — fully pinned, transitive lock for byte-for-byte
  reproducible installs (`pip install -r requirements.lock`). Regenerate after
  editing `requirements.txt` (steps in the lock file's header).
- `config/requirements.txt` — full local-dev set (adds scraping, OCR, report
  generation, and the eval harness), same ranges where deps overlap.
