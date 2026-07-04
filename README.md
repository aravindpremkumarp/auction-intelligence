# Bank Auction Intelligence

**Production:** <https://www.auctionscope.in>

An AI intelligence platform for Indian **SARFAESI** bank-auction property. It
scrapes public auction listings, builds a **Neo4j knowledge graph** of Tamil
Nadu auctions (~3,400 properties — the live count is at `GET /stats`), enriches
each listing with OCR + vision-LLM extraction of the source sale notices, and
serves a **PydanticAI agent** behind a chat UI that lets you find, compare,
score, and track investment opportunities in natural language.

```
scrape → filter TN → load Neo4j → OCR + vision-LLM extract → classify notices →
extract descriptions → verify/enrich → normalize → embed (3 vector indexes) →
serve agent + web UI → human feedback + review loop
```

---

## What it does

- **Conversational search** over the graph — "residential auctions in Chennai
  under 30 lakhs", "what's the price range in Kanchipuram?", "which borrowers
  have more than 3 properties?". Every answer is grounded in a tool call; the
  agent never invents prices, counts, or IDs.
- **Semantic / qualitative search** over notice text and images — boundaries,
  neighbourhood, legal caveats, condition — across three vector indexes
  (description, notice markdown, notice image) ranked in one embedding space.
- **Paste-a-listing matching** — drop a WhatsApp forward or broker blurb and the
  agent anchors it to the right auction by reserve price + date.
- **Specialised modes** — deep research (7-step due diligence on one auction),
  side-by-side compare, and a personalised investment report.
- **Accounts** — Supabase auth, a saved-property **watchlist**, and persisted
  **conversations** (including per-property chats).
- **Re-auction awareness** — every result row carries `is_reauction`,
  `reauction_count`, and `previous_reserve_price` so price-drop questions are
  answered from the rows directly.
- **Enrichment review surface** — an admin UI to verify/edit the LLM-extracted
  description of each property against its source notice, re-extract regions,
  and grade notice/markdown quality.
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
              │  PydanticAI agent ─▶ OpenRouter (Gemini 2.0 Flash) │
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
  (behaviour), and `web/auth.js` (Supabase auth). Plus `admin.html` and
  `review.html` for the admin/review surfaces.
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
              classification, description extraction, verify/enrich, normalize,
              embeddings (3 vector indexes), R2 storage helpers.
scrapers/     Selenium scrapers for eauctionsindia.com (local only).
scripts/      Data-prep, migration, backfill, and one-off maintenance scripts.
scoring/      Ten-dimensional investment scoring (auction_scorer.py).
tracking/     Eight-state investment-pipeline tracker.
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
| **LLM** | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | OpenRouter (Gemini 2.0 Flash) runs the agent + OCR extraction; Google key powers `gemini-embedding-2` for semantic search. |
| **Graph** | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | Neo4j Aura. |
| **Auth** | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `AUTH_ENABLED`, `ADMIN_BOOTSTRAP_EMAIL` | Anon key is browser-safe; service-role key is server-only (admin bootstrap script). |
| **Storage** | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` | Public Cloudflare R2 bucket serving sale notices. |
| **Observability** | `LOGFIRE_TOKEN`, `LOGFIRE_ENVIRONMENT`, `OTEL_EXPORTER_OTLP_*` | Optional OpenTelemetry tracing; unset = no-op. |
| **Eval** | `EVAL_JUDGE_MODEL` | LLM-as-judge model for the golden eval. |
| **App** | `APP_BASE_URL`, `APP_ENV`, `FEEDBACK_RESOLVE_TOKEN`, `RATELIMIT_DISABLED` | CORS origins, env mode, feedback-resolve guard. |
| **Scraping** | `FINDAUCTION_EMAIL`, `FINDAUCTION_PASSWORD` | Local-only; not needed in production. |

---

## The agent

`api/agent.py` builds the PydanticAI agent. Its system prompt is a short role
statement plus the whole of `modes/_shared.md` (graph schema, enum lists,
tool-routing rules, a Cypher cheat-sheet). Two dynamic instructions augment each
turn: the rolling **active search scope** carried across turns, and a **mode
overlay** when the client requests one.

**Tools** (read-only against the graph + web):

| Tool | Purpose |
| --- | --- |
| `search_auctions` | Filter by price / EMD / city / area / type / category / bank / borrower / platform / date; `deadline_within_days` for upcoming deadlines; supports aggregates (min/max/avg/median/p25/p75) and true `total_count`. |
| `semantic_search` | Vector search across description + notice-markdown + notice-image indexes in one call. |
| `get_auction_detail` | Full record for one `auction_id`, including re-auction `price_history`. |
| `score_auction` | 10-dimension investment score (composite 0–100 + A+–F grade) for one `auction_id`; powers the `compare` / `report` modes. |
| `list_distinct` | Distinct values + per-value counts for distribution / breakdown questions. |
| `describe_schema` | Live graph introspection (labels, rels, enums, ranges); 1-hour cache. |
| `run_cypher` | Read-only Cypher escape hatch — write clauses rejected, 10 s / 500-row caps. |
| `internet_search` | Tavily web search for off-graph context (legal/RBI/locality). |

**Modes** (`modes/*.md`): `compare`, `deep-research`, `report` (plus the
default `ask`). `deep-research` and `report` are login-gated. Five further
specs — `scan`, `shortlist`, `evaluate`, `track`, `refresh` — are parked in
[`modes/_archive/`](modes/_archive/) (not wired into the UI; see that folder's
README to re-activate).

The graph is modelled around `AuctionProperty`, with `Bank`/`Branch`,
`City`/`Area`/`State`, `AssetCategory`, `PropertyType`, `Borrower`, and
`AuctionType` reference nodes, plus enrichment nodes (`Score`,
`InvestmentTracker`, `Feedback`, `User`, `Document`). See
[`config/domain_ontology.yaml`](config/domain_ontology.yaml) for the full schema.

---

## Data & enrichment pipeline

The pipeline (`pipeline/`, run locally) turns raw scraped listings into enriched
graph data. Orchestrate it with `python -m pipeline.run_pipeline` (flags:
`--pilot`, `--limit N`, `--skip-ocr`, `--skip-descriptions`, `--verify-only`,
`--legacy`).

A typical end-to-end run:

```bash
python -m scrapers.fast_eauctions_scraper   # 1. Scrape eauctionsindia.com
python -m scripts.prepare_tn_data           # 2. Clean + filter the Tamil Nadu subset
python -m scripts.load_tn_to_neo4j          # 3. Load the base graph
python -m scripts.upload_downloads_to_r2    # 4. Push sale notices to R2
python -m pipeline.run_pipeline             # 5. OCR → extract → classify →
                                            #    describe → verify → normalize → load
python -m pipeline.embed_descriptions       # 6. Embed + build the vector indexes
python -m scripts.link_reauctions           # 7. Link re-auctioned properties
uvicorn api.main:app --reload               # 8. Serve agent + web UI
```

Notable stages: **OCR/extraction** uses MinerU + a vision LLM
(`ocr_extract.py`, `mineru.py`); **notice classification** splits single- vs
multi-property notices (`classify_notice.py`); **description extraction** pulls
the per-property blurb (`extract_descriptions.py`); **verify/enrich** reconciles
scraped fields against the PDF (PDF wins, original kept as `<field>_scraped`);
and **embeddings** build three vector indexes (`property_desc_idx`,
`notice_markdown_idx`, `notice_image_idx`) consumed by `semantic_search`.

---

## API surface

Mounted in `api/main.py`. Selected endpoints:

**Public**

- `GET /health`, `GET /health/deep` — liveness + readiness (Neo4j, vector index,
  `last_enriched` freshness; `degraded` on any failure).
- `GET /stats` — public coverage + freshness snapshot.
- `GET /modes` — mode registry for the UI selector.
- `GET /properties` — browse listing with cascading facets + multi-select filters.
- `GET /auction/{id}` — full auction detail.
- `POST /chat` — run an agent turn (returns answer, tool artifacts, and the
  message history to replay next turn).
- `POST /feedback`, `GET /feedback/recent`, `PATCH /feedback/{id}/resolve`.

**Authenticated** (Supabase JWT)

- `GET|PATCH /auth/me`.
- `GET /watchlist`, `POST|DELETE /watchlist/{id}`.
- `GET|PUT|DELETE /conversations[/{id}]`.

**Admin**

- `GET|PATCH /admin/users[/{id}]`, `GET /admin/feedback`.
- `GET /review/*` — the enrichment-review surface: property/notice/markdown/
  classification queues, `verify` / `edit` / `unverify`, block-level annotation
  (`/notice/{file}/blocks…`), region re-extract, crop/rotation, reingest, and
  source streaming.

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
