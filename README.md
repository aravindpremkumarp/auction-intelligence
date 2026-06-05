# Bank Auction Intelligence

Production: <https://www.auctionscope.in>

A FastAPI + Neo4j app with a chat UI over a knowledge graph of ~3,391 Tamil Nadu bank auctions.

- **Backend**: FastAPI + pydantic-ai agent, queries Neo4j Aura. Endpoint logic
  is split into focused routers (`api/chat`, `api/properties`, `api/feedback`,
  `api/health`, …); `api/main.py` is just the composition root.
- **Frontend**: Single-page app, no build step — vanilla JS + hand-written CSS,
  split into `web/index.html` (markup), `web/styles.css`, `web/app.js`
  (behaviour), and `web/auth.js` (Supabase auth).
- **Local-only tools**: Selenium scraping, PDF/image OCR (not run in production).

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r config/requirements.txt   # full dev requirements
cp .env.example .env                      # then fill in real values

uvicorn api.main:app --reload
```

Open <http://localhost:8000>.

## Deployment

- **Frontend** → Vercel (serves `web/` as static site; see `vercel.json`).
- **Backend** → Render (Python web service; see `render.yaml`).
- **Database** → Neo4j Aura (already hosted).

Before first deploy, after the Render backend is live, edit the production URL inside `web/index.html` (search for `REPLACE-WITH-RENDER-URL`) and push — Vercel auto-redeploys.

## Project layout

```
api/         FastAPI composition root (main.py) + routers:
             chat/ properties/ feedback/ health/ auth/ watchlist/
             conversations/ review/ — plus agent.py, neo4j_client.py,
             observability.py, and tools/ (Cypher + web search).
pipeline/    Shared config, embeddings, enrichment/OCR pipeline
web/         Single-page frontend: index.html (markup), styles.css,
             app.js (behaviour), auth.js (Supabase auth)
config/      requirements.txt (full dev) + YAML configs
scrapers/    Selenium scrapers (local only)
scripts/     One-off maintenance scripts
```

## Dependencies

- `requirements.txt` — production (Render) install. Pinned with compatible-release
  ranges (`>=x,<next-major`) so deploys don't pick up surprise breaking upgrades.
- `requirements.lock` — fully pinned, transitive lock for byte-for-byte
  reproducible installs (`pip install -r requirements.lock`). Regenerate after
  editing `requirements.txt` (steps are in the lock file's header).
- `config/requirements.txt` — full local-dev set (adds scraping + OCR + report
  generation), same ranges where deps overlap.

## Observability

Latency-sensitive paths emit greppable structured logs via `api/observability.py`:

```
auction.obs neo4j.run_read_query status=ok elapsed_ms=42 rows=18 access=read
auction.obs chat.agent_run status=error elapsed_ms=9120 mode=ask err=...
```

Slow operations log at WARNING above env-tunable budgets — `OBS_SLOW_QUERY_MS`
(default 1500) for Neo4j and `OBS_SLOW_AGENT_MS` (default 12000) for the LLM
turn. `/chat` also logs a per-turn summary (`chat turn ok mode=… tool_calls=…`).

## Health & data freshness

- `GET /health` — cheap liveness probe.
- `GET /health/deep` — Neo4j connectivity, auction count, vector-index presence,
  and `last_enriched` (freshness). Returns `status: degraded` on any failure —
  point uptime monitoring/alerting here.
- `GET /stats` — public coverage + freshness snapshot (total/upcoming auctions,
  `last_enriched`) powering the UI's "data updated …" indicator.
