# Bank Auction Intelligence

A FastAPI + Neo4j app with a chat UI over a knowledge graph of ~3,391 Tamil Nadu bank auctions.

- **Backend**: FastAPI + pydantic-ai agent, queries Neo4j Aura.
- **Frontend**: Single-page HTML + Alpine.js + Tailwind (CDN, no build step).
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
api/         FastAPI app + agent + Cypher tools
pipeline/    Shared config, embeddings
web/         Single-page frontend (index.html)
config/      requirements.txt (full dev) + YAML configs
scrapers/    Selenium scrapers (local only)
scripts/     One-off maintenance scripts
```
