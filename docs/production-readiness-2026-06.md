# Production-readiness review — June 2026

Full-codebase audit (backend, frontend, pipeline, CI/infra) against the goal of
a production-ready app. Findings were verified against the source — file:line
references are exact at the time of writing. Builds on
[`improvements-2026-06.md`](improvements-2026-06.md); items already tracked
there are not repeated unless still open.

**Verdict:** the architecture is sound and several hard things are already done
well (Cypher write-guardrails, JWT verification with JWKS caching, offline test
stubs, structured op logs, locked dependencies, XSS-safe markdown rendering).
What separates this from "production ready" is concentrated in four areas:
**one exposed endpoint, missing rate limits / cost controls, missing outbound
timeouts, and a free-tier single-instance deploy with no CI quality gates.**

---

## P0 — security & money (fix this week)

### 1. `GET /feedback/recent` is unauthenticated and leaks user chat data

`api/feedback/router.py:167-186` — no auth dependency. Anyone can fetch every
feedback item, and each record includes `question`, `answer`,
`context_turns` (the user's full chat exchange), `session_id`, `user_agent`,
and `page_url`. This is private user conversation content served to anonymous
callers. (The committed `feedback/*.json` snapshots make the same data public
in the repo — see item 14.)

**Fix:** require admin (`Depends(get_current_admin)`) or the resolve token.
The `sync-feedback.yml` workflow is the only legitimate anonymous consumer —
have it send `X-Resolve-Token` (or a dedicated read token) instead.

### 2. No rate limiting on public read endpoints

`api/properties/router.py` — `/properties` (line 172), `/stats` (line 272),
`/auction/{id}` (line 296) have no throttle. `/stats` is O(N) over the whole
graph; `/properties` runs full facet scans. A dumb scraper loop can saturate
the single free-tier instance and your Aura quota.

**Fix:** apply the same slowapi limiter used on auth routes, e.g.
`30/minute` per IP on these three.

### 3. No LLM spend controls for authenticated users

Anonymous chat is capped (10/hr), but a logged-in user can loop `/chat`
indefinitely — every turn costs OpenRouter tokens, with no per-user budget,
no daily spend alert, no aggregate tracking (`api/chat/router.py` logs token
counts per turn but nothing accumulates them).

**Fix (incremental):**
1. Per-user daily turn cap (e.g. 100/day) checked in the chat router — cheap,
   one Neo4j counter on `:User`.
2. A Logfire alert on daily token spend (the per-turn usage is already logged;
   the Logfire MCP/dashboard can alert on it).
3. Later: monthly token budget per tier.

### 4. Missing timeouts on outbound calls

- `api/auth/supabase_jwt.py:34-39` — `PyJWKClient` has no `request_timeout`.
  If Supabase JWKS hangs, **every authenticated request hangs with it** (the
  1-hour key cache hides this until the first refresh after an outage).
  Fix: `PyJWKClient(..., timeout=10)`.
- The PydanticAI/OpenRouter call has no explicit timeout — a hung model run
  pins a worker indefinitely. Fix: set an http client timeout on the model
  (60–90 s) so `/chat` fails fast instead of wedging.
- `api/review/router.py:1081` — the notice-source proxy uses `timeout=60`
  and fetches whatever URL is stored on the Document node. Reduce the timeout
  and allowlist the R2 public domain before fetching (defence-in-depth against
  a poisoned `public_url`).

### 5. Resolve-token comparison is not constant-time

`api/feedback/router.py:202-203` — `x_resolve_token == expected`. Use
`secrets.compare_digest()`. Two-line fix; do it while touching item 1.

---

## P1 — production hardening (this month)

### 6. CI gates almost nothing

`.github/workflows/ci.yml` runs only `pytest tests/api -q`. No linter, no type
checker, no dependency audit, and **no config files for any of them exist**
(no `pyproject.toml` at all).

**Fix:** add a `pyproject.toml` with `ruff` (lint + format) and pytest config,
add `ruff check` + `pip-audit` steps to CI. Adopt mypy gradually if at all.
Also: CI and Render both install from the ranged `requirements.txt`; deploys
should use `requirements.lock` (`render.yaml:7`) so prod is byte-reproducible —
the lock file exists but nothing uses it.

### 7. Zero tests for watchlist, conversations, and the properties endpoints

`tests/api/` has ~290 tests but none for `api/watchlist/`,
`api/conversations/`, or the `/properties` facet/filter endpoint (only the
agent-tool side of search is covered). The chat router has partial coverage
(history trim, active filters, rate limit) but no full-turn test with a stubbed
agent.

**Fix:** the conftest stubs already support this — add
`test_watchlist.py`, `test_conversations.py`, `test_properties_endpoint.py`.

### 8. Deploy posture: free tier, wrong health check, no staging, no rollback plan

`render.yaml` — `plan: free` (instance **spins down on idle → ~30-60 s cold
start for the first user**, and sleeps mid-conversation), single instance,
`healthCheckPath: /` (serves the SPA — Render sees 200 even when Neo4j is
down; point it at `/health`). Workflows hardcode the prod URL
(`sync-feedback.yml:18`). No staging service; rollback is manual via the
Render dashboard.

**Fix:** upgrade to a paid plan (the cold-start alone is disqualifying for
production), set `healthCheckPath: /health`, wire external uptime monitoring
at `/health/deep` (still open from the June review), and document the
rollback procedure. A staging service can wait until traffic justifies it.

### 9. Event-loop blocking: sync Neo4j driver inside async endpoints

`run_query()` is synchronous but is called from `async def` endpoints
(auth, feedback, conversations routers). Each call blocks the event loop, so
one slow query stalls *all* concurrent requests on the worker.

**Fix:** either make those endpoints plain `def` (FastAPI runs them in the
threadpool) or wrap calls in `asyncio.to_thread()`. Mechanical change,
meaningful under load.

### 10. Frontend caching is unconfigured

`vercel.json` sets no cache headers. Without a build step the assets aren't
fingerprinted, so do **not** mark them immutable; instead:
`index.html` → `max-age=0, must-revalidate`; `app.js` / `styles.css` →
`max-age=300, stale-while-revalidate=86400` (or add a `?v=` query param bumped
on deploy for long-lived caching). Also add a fetch timeout (AbortController)
to the browse/chat calls so a down API doesn't hang the UI forever.

### 11. In-memory rate limiting won't survive scaling

The anon-chat counter (`api/chat/router.py:49-65`) and slowapi default storage
are per-process and unlocked (concurrent first-hits can slip past the cap).
Fine on one instance; switches to Redis-backed storage the day you run two.
Add a `threading.Lock` around the counter now (3 lines), note the Redis
migration in the scaling plan.

---

## P2 — pipeline & data operations

The pipeline runs locally so nothing here takes prod down, but it's where data
quality (the moat) is made.

12. **Silent failure swallowing + print-only logging.** ~200 `print()` calls,
    zero `logging`, and several `except: pass` blocks that drop malformed
    records invisibly (`pipeline/ocr_extract.py:44-46,240-243`,
    `classify_notice.py:263-265`, `extract_descriptions.py:305-307`).
    Fix: module-level loggers + always log the record id and error on skip.
13. **No cost metering on batch LLM runs.** Vision-LLM extraction over
    thousands of notices has no token/cost accounting and no budget abort.
    Fix: accumulate token counts per stage, print a cost summary, add an
    optional `--max-cost` guard.
14. **Committed feedback snapshots contain user chat content.**
    `feedback/all.json` includes `context_turns`, `session_id`, `user_agent`
    in a public(?) repo. Fix: sanitize in `sync-feedback.yml` (strip
    `context_turns`/`user_agent`) or stop committing and fetch on demand.
15. **LLM output not schema-validated before graph load.** Extraction caches
    are written without checking required keys
    (`pipeline/extract_descriptions.py:172-320`); multi-file merges keep
    "first non-null wins" without logging conflicts
    (`verify_and_enrich.py:128-187`, ironic given how good the PDF-vs-scraped
    conflict tracking is). Fix: validate before cache write; log merge
    conflicts.
16. **Three scraper variants with ~50 % overlap** (`scrapers/scraper.py`,
    `eauctions_scraper.py`, `fast_eauctions_scraper.py`) and 8 dead `_*.py`
    scripts in `scripts/`. Fix: delete the non-canonical scrapers (the README
    names `fast_eauctions_scraper` as the entry point), move dead scripts to
    `scripts/legacy/`.
17. **Data freshness is fully manual.** No scheduler re-scrapes or re-enriches;
    `/stats` exposes staleness but nothing acts on it. Fix: a weekly cron
    (GitHub Action or workstation cron) for scrape→load→link, with the
    enrichment stages still run manually.
18. **Evals:** 40 golden cases, no negative/zero-result cases, no run-over-run
    regression tracking. Fix: add ~10 edge cases; persist per-run scores so a
    model swap is comparable.

---

## P3 — polish

19. Client-side guard on `/admin` and `/review` pages (currently anyone gets
    the page shell; the APIs behind them are protected, so this is cosmetic —
    redirect to `/` when not admin).
20. Startup validation of required env vars (fail fast with a clear message
    instead of a mid-request crash).
21. `chat` exception handler returns 500 for everything
    (`api/chat/router.py:483-488`) — distinguish 503 (LLM/graph down,
    retryable) from 500.
22. Split `cypher_tools.py` (1,536 lines) — already tracked in the June doc;
    still open, and the duplication between `search_auctions` and
    `_properties_filter_cypher` (`api/properties/router.py:45-112`) belongs to
    the same refactor.
23. Minor a11y: `aria-label` on re-auction history rows (`web/app.js:1070`).

**Dropped after verification:** the "Cypher injection in `describe_schema`"
finding (labels come from `db.labels()` — exploiting it requires already
controlling the DB schema; harmless to backtick-escape, not urgent), and
"`/chat` is untested" (it has partial coverage; the gap is a full-turn test).

**Still blocked:** the Supabase RLS/advisor audit from the June review — the
available Supabase access is scoped to a different project, so it still needs
a manual run of the security advisors on the live Auctionscope project.

---

## Suggested sequencing

| Week | Work |
| --- | --- |
| 1 | P0 items 1–5 (one small PR each; 1+5 together) |
| 2 | CI gates + lock-file deploy (6) · missing router tests (7) |
| 3 | Render plan + health check + uptime monitoring (8) · async fix (9) |
| 4 | Frontend caching/timeouts (10–11) · feedback snapshot sanitization (14) |
| ongoing | Pipeline logging/validation (12, 15) · scraper cleanup (16) · freshness cron (17) |
