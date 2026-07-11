# AuctionScope — Content-Ops Agents (blueprint)

*Companion to `docs/marketing/plan.md`. Status: **Agent A ("Poster") is built** — `marketing_agents/poster.py` + the manual-dispatch workflow `.github/workflows/content-poster.yml`. Agent B (Reporter) and Agent C (Replier) remain specs.*

## In one sentence
Hire two tireless "interns" (scheduled Claude jobs) to do the repetitive marketing chores — one drafts daily social posts from our auction data, one writes a weekly report — so the founder's time goes to building, not busywork. **They draft and stage; a human always hits "publish."**

## Why this exists
A viral thread ("Claude AI agents as a service — $15k/mo") pitched wiring Claude into a content pipeline. The **income claims are unverified marketing copy** and selling this as a service is a second company — not for us at ~7 users. But the *pattern* (trigger → Claude reads → writes → files output → notifies, on a schedule, unattended) is genuinely useful **applied to ourselves**, because the marketing plan's binding constraint is founder-hours, not money. This doc adapts that pattern to AuctionScope using tools we already have.

**No conflict with our skills.** This is exactly what the installed `marketing-loops` skill is for — these two agents are its **content-repurposing loop** and **weekly-marketing-review loop**, made concrete. We follow that skill's guardrails (below).

## Stack decision: native, not n8n
The thread uses n8n (a paid external automation tool). We don't need it — this repo already runs scheduled Claude/LLM jobs:
- `.github/workflows/golden.yml` — nightly cron that calls the LLM via `python -m evals.run_golden`, with all model keys passed as `env:` from repo secrets. **Our agents copy this exact shape.**
- `.github/workflows/sync-feedback.yml` — every-15-min cron that hits our API and commits the result. `data-freshness.yml` — weekly cron that opens a GitHub issue.

Reusing these means: no new subscription, ~$5–30/month in LLM cost, and every agent automatically inherits the 47 marketing skills, `.agents/product-marketing.md` (voice/positioning), and the **honesty rule** (never claim "diligence"/legal certainty).

---

## Agent A — "The Poster" (daily draft generator)
*This is the automation half of the social content engine in plan §4 Move 7 — and the `marketing-loops` content-repurposing loop.*

- **Runs:** **after each data refresh (scrape), not on a fixed daily clock** — since the founder scrapes ~weekly/biweekly, fresh data in → content out. This avoids posting a "deal" that closed days ago. Trigger via manual "run now" right after a scrape, or a `workflow_dispatch` the scrape step fires.
- **Reads:** `GET /stats` (live counts) and `GET /properties` for post fodder — new/upcoming auctions, **price drops** (`is_reauction` + `previous_reserve_price`), **cheapest-by-city** (`sort=price_asc`), "closing soon."
- **Writes:** 3–5 post drafts + carousel copy in our voice (brand block pulled from `product-marketing.md`; taboo list = the honesty rule). Optionally renders the visuals via the existing HTML pipeline (`brand/logo/render.py` + `web/card-variations.html` + `web/styles.css` tokens; reels via the `hyperframes` npm package).
- **Files output (Tier 1, staged only):** commits drafts to `marketing/outputs/YYYY-MM-DD/` and opens/refreshes a **"content review" GitHub issue** as the notification.
- **Does NOT post.** A human reviews the folder, tweaks, and publishes via a scheduler (Buffer/Publer) or by hand.
- **Prompt:** adapt the thread's Agent-1/Agent-2 prompts — same JSON output contract, but the brand/taboo/theme blocks come from `product-marketing.md`, and every "deal" claim must be grounded in the sale-notice fields (reserve/EMD/date), never invented.

## Agent B — "The Reporter" (weekly marketing report)
*The `marketing-loops` weekly-marketing-review loop.*

- **Runs:** Friday afternoon cron.
- **Reads:** `GET /stats` + (once analytics ships) GA4 / Search Console exports + social metrics. Until analytics exists, it works from whatever numbers are available and says so.
- **Writes:** a one-page report — what worked, what didn't (with a hypothesis), the week's pattern, and **3–4 concrete next-week actions** (not "post more" but "3 price-drop posts Tue–Thu"), plus a green/yellow/red flag. Rule: every observation ends in an action.
- **Files output:** commits the report + opens a GitHub issue. Read-only on the outside world — safe to run unattended.

## Agent C — "The Replier" (parked)
Comment/DM triage (classify → draft reply or escalate). Deferred until there's actual posting cadence and comment volume (Q2+). Sketch only; will also stage, never auto-post.

---

## Guardrails (from `marketing-loops/references/loop-guardrails.md`)
- **Tier 1 = safe to run unattended:** read, analyze, **draft, stage**. Both agents' normal output is Tier 1.
- **Tier 2 = human-gated:** publish, send, spend. **Never automated in v1** — always a staging folder + human approval.
- **Kill switch (required):** a global `AGENTS_ENABLED` flag the workflows check, plus the ability to disable the GitHub Action directly. "A loop you can't stop fast is a liability."
- **Test before trusting:** run 20+ sample inputs (including weird ones — empty data, huge inputs, off-topic) before the first scheduled run.
- **No PII** in committed outputs or logs.

## How it gets built (later — not in this change)
A workflow shaped like `golden.yml`:
```
on: { schedule: [{cron: "0 3 * * 1-5"}], workflow_dispatch: {} }   # weekday mornings
env: { OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}, ... }
run: python -m marketing_agents.poster      # (module to be written)
```
…or the same logic as a scheduled Claude Code Routine. Either way it reuses existing env conventions and the `/properties` + `/stats` endpoints.

**Agent A is now implemented this way:** `marketing_agents/poster.py` (data fetch → grounded LLM drafts → honesty-rule validation → staged outputs in `marketing/outputs/<date>/`) run by `.github/workflows/content-poster.yml` (manual **Run workflow** button; commits drafts `[skip ci]` and opens/refreshes a `content-review` issue). Unit tests: `tests/marketing_agents/`. Kill switch: repo Actions variable `AGENTS_ENABLED=false`.

## Parked idea: selling this as a service
The thread's real pitch is running this *for other people* at a setup fee + monthly retainer. If our own two agents prove out over 2–3 months, packaging "content agents for property/finance experts" could be a later venture — but it is a **separate business**, and chasing it now (pre-traction, ~7 users) would split focus. Revisit post-traction. Not on any current roadmap.
