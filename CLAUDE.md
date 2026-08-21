## Communication style

Keep every response short and easy to understand. Write in plain, simple
English for a non-expert reader. These rules apply to all conversational
output; they do not apply to code, which must always be complete and correct.

### Response format

1. **Direct answer** — 1–2 short sentences, first thing in the response.
2. **Key points** — at most 3–5 short bullets, only if they truly help.
3. **Next action** — one line, only if there is one.

### Rules

- Answer first. Keep the whole response as short as possible — a few
  sentences is usually enough.
- Use everyday words. Avoid jargon; if a technical term is unavoidable,
  explain it in a few plain words the first time it appears.
- Short sentences. One idea per sentence.
- Never dump long lists, walls of text, or every detail you know. Share only
  what the user needs right now; they can ask for more.
- No introductions, conclusions, disclaimers, or filler ("Great question",
  "In summary", "It's worth noting").
- Never repeat what the user said or what was already covered.
- Multiple options: recommend one and say why in one line. Mention
  alternatives only if truly needed, one line each.
- Risks, blockers, or important caveats: flag in 1–2 simple sentences,
  prefixed **Risk:** / **Blocker:** / **Caveat:**. Never bury them.
- Detailed explanations only on explicit request ("explain", "why in detail",
  "walk me through").
- Missing information that materially changes the answer: ask one simple
  clarifying question instead of assuming. Otherwise state the assumption in
  one line and proceed.
- After finishing a task, report it like you would to a busy friend: what
  changed and whether it works, in 2–4 sentences. No step-by-step narration.

### Calibration

- Simple question → 1–2 sentences, no bullets, no headers.
- Task completion report → what changed, where, and that it was verified,
  in a few sentences.
- Long or multi-step work → outcome first, then only what affects the next
  decision. Everything else stays out.

## What this project is

**Auctionscope** (<https://www.auctionscope.in>) — an AI search and research
tool for Indian SARFAESI bank-auction property, focused on Tamil Nadu.

Public listings are scraped, filtered to TN, loaded into a **Neo4j** knowledge
graph, enriched by OCR + vision-LLM extraction of the source sale notices, and
served through a **PydanticAI** chat agent behind a plain-JS single-page app.

```
scrape → filter TN → load Neo4j → OCR + vision-LLM extract → classify notices →
verify/enrich → apply LangExtract descriptions → embed (3 vector indexes) →
serve agent + web UI → human review + feedback loop
```

Stack: **FastAPI** (Render) · **Neo4j Aura** · **Supabase** auth ·
**Cloudflare R2** for notices · **OpenRouter** LLMs · **Razorpay** billing ·
**Vercel** static frontend · **Logfire/OTel** tracing.

The long-form tour is [`README.md`](README.md); the graph model is
[`docs/SCHEMA.md`](docs/SCHEMA.md) and
[`config/domain_ontology.yaml`](config/domain_ontology.yaml). This file covers
what an AI assistant needs to work in the repo safely.

## Repository map

| Path | What lives there |
| --- | --- |
| `api/` | FastAPI app. `main.py` is a thin composition root (CORS, rate limit, error handlers, static mount); all endpoint logic sits in per-feature packages. |
| `api/agent.py` | The live PydanticAI agent (v1). Prompt = short role text + all of `modes/_shared.md`. |
| `api/chat/` | `router.py` (v1 chat + SSE stream), `gating.py` (tier quotas), `panel.py`, `scope_keys.py`, `suggestions.py`. |
| `api/chat/v2/` | Chat agent **v2** — tiered plan/execute loop behind `/chat/v2`, surfaced only in the admin `/lab` page. Not the default path. |
| `api/chat/deep/` | Deep-research mode (login-gated) — its own loop + checkpointer. |
| `api/tools/` | Agent tools: `cypher_tools.py` (graph search/detail/schema/raw Cypher), `web_tools.py` (Tavily). |
| `api/review/` | Admin enrichment-review surface (classification / markdown / extraction queues, block annotation, re-extract). |
| `api/auth · billing · watchlist · conversations · alerts · dossier · social · feedback · health · properties` | One package per feature, each exporting a router. |
| `api/neo4j_client.py` | The only place that talks to Neo4j. Bolt by default; HTTPS Query API when `NEO4J_HTTP_API=1`. |
| `api/observability.py`, `api/telemetry.py` | Structured `auction.obs` logs + OpenTelemetry/Logfire setup. |
| `api/model_selection.py`, `api/policy.py` | Tier → model / reasoning-effort registry and access policy. |
| `pipeline/` | Local-only enrichment: OCR (`mineru.py`, `datalab.py`, `ocr_extract.py`), notice classification, LangExtract runs, verify/enrich, entity + place resolution, embeddings, R2 storage. `run_pipeline.py` orchestrates. |
| `scrapers/` | Selenium scrapers for eauctionsindia.com. Local only, sometimes needs a human for Cloudflare. |
| `scripts/` | Data prep, backfills, migrations, static-page generators, one-offs. Run as modules: `python -m scripts.<name>`. |
| `modes/` | Agent prompt files. `_shared.md` (schema + tool-routing rules) is the real system prompt; `deep-research.md` is the one live overlay; `_archive/` holds parked specs. |
| `web/` | The SPA — **no build step**. `index.html`, `styles.css`, `app.js`, `auth.js`, `billing.js`, `dossiers.js`, plus `admin.html`, `review.html`, `lab.js`. `bank-auctions/`, `guides/`, `compare/`, `property/` are generated SEO pages. |
| `tests/` | `tests/api` (the CI gate), `tests/pipeline`, `tests/e2e` (live Razorpay + real Neo4j), `tests/scripts`, plus loose scraper probes at the top level. |
| `evals/` | pydantic-evals golden-question and conversation harnesses. |
| `docs/` | Design specs (`docs/design/`), audits, marketing playbooks (`docs/marketing/`), `SCHEMA.md`. |
| `marketing/` | Content generators + `dashboard.html` (see below). |
| `clones/`, `experiments/`, `redesign/`, `walkthrough/` | Sandboxes. Not deployed, not linted. |
| `config/` | Full dev requirements, domain ontology, `CODEBASE_OVERVIEW.txt`. |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r config/requirements.txt   # full dev set; requirements.txt is prod-only
cp .env.example .env                      # fill in real values, never commit it
uvicorn api.main:app --reload             # http://localhost:8000
```

Offline toggles: `AUTH_ENABLED=false` (skips auth/watchlist/conversations/
review/social routers), `RATELIMIT_DISABLED=1`, `NEO4J_HTTP_API=1` when Bolt
port 7687 is blocked. API docs (`/docs`) are only mounted when `APP_ENV` is
`dev` or `test`.

The frontend needs no server of its own — `API_BASE` resolves to the same
origin on localhost and to the hosted Render URL elsewhere. Edit the files in
`web/` and reload.

## Testing, lint, and CI gates

```bash
pytest tests/api -q          # the CI gate (~1,150 tests, no live DB needed)
pytest tests/pipeline -q     # pipeline unit tests
ruff check .                 # lint gate
```

`.github/workflows/ci.yml` runs four jobs on every PR: **lint** (ruff),
**audit** (`pip-audit` on `requirements.lock`), **test**
(`pytest tests/api tests/scripts tests/test_vercel_config.py`), and **e2e**
(live Razorpay test mode + a real Neo4j service container — it fails loudly
rather than skipping when secrets are missing).

`tests/api/conftest.py` stubs Neo4j, the agent, and Supabase JWT verification,
so API tests run with no credentials. Add new endpoint tests there and they
inherit those stubs.

Nightly / scheduled workflows: `golden.yml` and `golden-conversations.yml`
(agent quality — tool trajectory is the gate, plus an LLM judge),
`data-freshness.yml`, `r2-consistency.yml`, `sync-feedback.yml`,
`resolve-feedback.yml`, `ocr-ab.yml`, `resolve-entities.yml`.
`seo-pages.yml` and `content-poster.yml` are manual-dispatch and always open a
**draft PR** — generated content is never auto-published.

Run the golden eval locally with `python -m evals.run_golden` (needs
OpenRouter + Neo4j credentials).

## Conventions that matter

- **Never invent numbers.** Prices, counts, IDs and dates come from a tool call
  or a query, never from the model. Same rule in marketing copy and docs.
- **The graph is read-only from the API.** `run_cypher` rejects write clauses,
  with 10 s / 500-row caps. Writes happen in `pipeline/` and `scripts/`.
- **Talk to Neo4j only through `api/neo4j_client.py`.** No new drivers.
- **One package per feature in `api/`,** each exporting a router that
  `main.py` includes. Keep `main.py` thin.
- **Auth-gated routers must degrade.** Anything mounted only when
  `AUTH_ENABLED` is true has to leave the app bootable when it is false.
- **Feature flags ship dark.** e.g. `DOSSIERS_ENABLED=false` in prod; the test
  suite turns it on so the routes still have contract tests.
- **No frontend build step.** Vanilla JS and hand-written CSS in `web/`. Don't
  add a bundler, framework, or npm dependency to the SPA. (`clones/` is the one
  Next.js sandbox and is exempt.)
- **Generated pages are generated.** Edit the generator in `scripts/`
  (`build_guides.py`, `build_landing_pages.py`, `build_compare.py`,
  `prerender_properties.py`, `generate_property_og.py`), not the HTML in
  `web/guides/**`, `web/bank-auctions/**`, `web/compare/**`, `web/property/**`.
- **New browser origin or asset host?** Update the CSP in `vercel.json` — and
  `tests/test_vercel_config.py` guards it.
- **Dependencies:** edit `requirements.txt` (ranges), then regenerate
  `requirements.lock` (steps in its header). Render installs the lock.
  `config/requirements.txt` is the local dev superset.
- **Lint scope:** ruff runs with `E4,E7,E9,F` at 120 columns.
  `pipeline/`, `scrapers/`, `scripts/`, `scoring/`, `tracking/` carry
  per-file ignores — tighten as you touch them, don't loosen further.
- **Python 3.11** everywhere (CI, Render).
- **Secrets never land in the repo.** `.env` is local; Render and Vercel hold
  the real values.
- **Human gates stay human.** The three review gates (notice type + lot count,
  OCR quality, extraction check) exist because the downstream graph trusts
  them. Don't auto-approve them in code.
- **Commits and PRs:** short imperative subject, optionally
  `type(scope): …` (`fix(chat):`, `feat(pipeline):`). PRs are squashed with
  the number appended. Every PR needs green CI; open new PRs as drafts.

## Where to start for common tasks

| Task | Start here |
| --- | --- |
| Change what the agent knows or how it picks tools | `modes/_shared.md`, then `api/agent.py` |
| Add or change an agent tool | `api/tools/cypher_tools.py`, register in `api/agent.py`, add a case in `evals/cases.py` |
| Change chat quotas, models, or tiers | `api/model_selection.py`, `api/chat/gating.py` |
| Add an API endpoint | new/existing package under `api/`, include in `api/main.py`, test in `tests/api/` |
| Change the graph shape | `docs/SCHEMA.md` + `config/domain_ontology.yaml`, then the writer in `pipeline/` or `scripts/` |
| Fix an extraction/OCR problem | `pipeline/ocr_extract.py`, `pipeline/load_extractions.py`, `pipeline/apply_extractions.py` |
| Change the review UI | `web/review.html` + `api/review/` |
| Change the SPA | `web/app.js`, `web/styles.css`, `web/index.html` |
| Anything marketing-facing | the dashboard rule below + `docs/marketing/` |

Known-broken and parked work is tracked in [`TODOS.md`](TODOS.md) — check it
before diagnosing a failure that may already be logged there.

## Marketing dashboard (living)

`marketing/dashboard.html` is the single source of truth for marketing-system
state — a self-contained page (open it in any browser) rendering an
interactive graph of channels, agents, workflows, KPIs, roadmap and changelog.

Any PR that changes marketing — new channel/workflow/campaign, milestone
shipped, KPI or roadmap movement, strategy change — MUST update it in the
same PR:

1. Edit only the `MARKETING_DATA` block in that file (`nodes` / `edges` /
   `kpis` / `pillars` / `roadmap` / `metrics` / `risks`).
2. Prepend a `changelog` entry and bump `meta.updated`.
3. The renderer below the data block is generic — never edit it for content
   changes; new nodes/lanes/edges lay out automatically.
4. Verify: open the file in a browser (or Playwright-screenshot it) — no
   console errors, graph renders, a node click opens the panel.

Keep the data honest: statuses are live / building / ready / planned / held,
and numbers are never invented (same rule as the copy playbook).

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Clone/rebuild/reverse-engineer another website → invoke /clone-website (output
  goes to `clones/` only; see `clones/README.md`)
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health

## gstack

Use the /browse skill from gstack for all web browsing. Never use the
mcp__claude-in-chrome__* tools.

Available gstack skills: /office-hours, /plan-ceo-review, /plan-eng-review,
/plan-design-review, /design-consultation, /design-shotgun, /design-html,
/review, /ship, /land-and-deploy, /canary, /benchmark, /browse,
/connect-chrome, /qa, /qa-only, /design-review, /setup-browser-cookies,
/setup-deploy, /setup-gbrain, /retro, /investigate, /document-release,
/document-generate, /codex, /cso, /autoplan, /plan-devex-review,
/devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn
