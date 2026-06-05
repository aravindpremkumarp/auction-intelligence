# Code-review response — June 2026

This note tracks the architecture/ops feedback from the June review and what was
done about it. Each item is marked **Done**, **Partial**, or **Deferred** with a
short rationale so the deferred work has a clear, honest paper trail.

## 1. Frontend was a 4,700-line single file — **Partial (the big win is done)**

`web/index.html` was 4,719 lines (metadata + CSS + markup + all behaviour).

Done in this PR:

- Extracted the ~1,570-line `<style>` block → **`web/styles.css`**.
- Extracted the ~2,540-line inline app script → **`web/app.js`** (kept as a
  *classic* script — the page uses inline `onclick=` handlers, so the functions
  must stay global; an ES module would break them).
- `web/index.html` is now **~600 lines** of markup. Load order preserved
  (`auth.js` → `app.js`); served on Vercel from the filesystem and on
  Render/local via explicit `/styles.css` + `/app.js` routes in `api/main.py`.
- Verified with `node --check web/app.js`.

Deferred: splitting `app.js` further into `chat.js` / `properties.js` /
`watchlist.js` / `detail.js`. The script is one classic-scope file with shared
top-level state and cross-section calls; splitting it safely needs browser-based
QA (the no-build app has no JS test harness), so it's best done as a focused
follow-up with `/qa`. The internal `/* ===== SECTION ===== */` banners already
mark the seams.

## 2. Backend files too large — **Partial**

`api/main.py` was 1,031 lines. Done: split into the structure the review
suggested, matching the existing `auth/` / `watchlist/` router convention:

- `api/chat/router.py` — `/chat`, `/modes`, rolling-scope + history-trim helpers,
  anon throttle.
- `api/properties/router.py` — `/properties`, `/auction/{id}`, `/stats`, filter
  & facet builders.
- `api/feedback/router.py` — `/feedback*`, `/admin/feedback`, models + helpers.
- `api/health/router.py` — `/health`, `/health/deep`.
- `api/main.py` is now a ~135-line composition root (middleware, exception
  handlers, router wiring, static serving).

Deferred: splitting `api/tools/cypher_tools.py` (1,564 lines) into
`search.py` / `semantic.py` / `schema.py` / `run_cypher.py` / `matching.py`.
**Rationale:** the test suite monkeypatches `api.tools.cypher_tools.run_query`
and `…run_read_query` as module globals across ~9 test files. Moving the
functions to submodules silently breaks that patch point (each submodule would
import its own `run_query`), so the split must be done *together with* a
coordinated test-monkeypatch update. Doing that in the same PR as everything
else risked the only safety net we have for this module, so it's split out.

## 3. Dependencies not pinned — **Done**

- `requirements.txt` / `config/requirements.txt` now use compatible-release
  ranges (`>=x,<next-major`) on every dependency — no more bare names.
- Added **`requirements.lock`**: the full transitive set pinned exactly, for
  reproducible installs. Header documents how to regenerate.
- Validated: a clean venv install of the ranged `requirements.txt` resolves and
  the API test suite passes (284 passed); `config/requirements.txt` and
  `requirements.lock` both resolve (dry-run).

## 4. Stronger ops posture — **Partial**

Done:

- `api/observability.py`: a dependency-free `timed()` context manager emitting
  greppable structured logs (`auction.obs <op> status=… elapsed_ms=… …`),
  WARNING above env-tunable budgets.
- Wired into `api/neo4j_client.py` (per-query latency + row counts) and the
  `/chat` agent round-trip (LLM latency + a per-turn `tool_calls` summary;
  errors already logged with the failing input).
- `/health/deep` now also reports `last_enriched` freshness → point uptime
  monitoring/alerting at it (`status: degraded` on any failure).

Deferred (needs infra, not code): wiring uptime monitoring + alerting against
`/health/deep`, an LLM/web-search budget control, and a feedback/error
dashboard. The logs are now shaped to feed a log drain when one is added.

## 5. Data quality is the moat — **Partial**

Done: `GET /stats` + `/health/deep` surface ingestion freshness
(`last_enriched`, from the `verified_at` the enrichment pipeline stamps), and the
UI shows a best-effort "data updated …" indicator.

Deferred (product work): user-visible per-field **provenance** (the graph already
stores `*_scraped` mirrors + `verification_status` + `field_conflicts` from
`pipeline/load_enriched.py` — surface them in the detail pane), confidence
scores, "last checked" for live auctions, stale-listing detection, and
legal/financial disclaimers.

## 6. Supabase RLS / service-role audit — **Deferred (notes below)**

Reviewed in-repo (no live-project changes made):

- The **service-role key is not used in any request path** — only in
  `scripts/create_admin.py`, an offline admin-bootstrap script. Good: no
  service-role escalation reachable from the API surface.
- Request auth is Supabase-JWT verification (`api/auth/supabase_jwt.py`) →
  Neo4j `:User` mirror; app data lives in **Neo4j, not Supabase tables**, so
  RLS exposure is limited to the auth schema.

To finish the audit (needs the live project, so left for an explicit pass):
run the Supabase security advisors (`get_advisors type=security`) and confirm
RLS is on for any custom tables + that `auth.users` isn't exposed via PostgREST.

## Suggested next PRs

1. Split `cypher_tools.py` + update the test monkeypatch points together.
2. Split `app.js` into modules behind a `/qa` browser pass.
3. Surface field provenance/confidence in the property detail pane.
4. Wire uptime + alerting to `/health/deep`; add an LLM-cost budget guard.
5. Run the Supabase security-advisor audit on the live project.
