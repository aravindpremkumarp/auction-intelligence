# AuctionScope — Social Media Content System (overview)

*The front door to the marketing content engine. This is the map; the detail lives in the linked docs. Read this first, then dive where you need to.*

## The one honest idea
AuctionScope helps people **evaluate** bank-auction property — it never promises legal certainty. Every post is grounded in a real auction fact or a cited source, in a calm lowercase voice, and **a human always publishes**. The product's differentiator — ask any property anything, answered with web research — is also the content's spine.

## Document map
| Doc | What it holds |
|---|---|
| `social-overview.md` (this) | the whole system at a glance |
| `plan.md` | the 13-section AARRR marketing plan; social is Move 7 |
| `content-pillars.md` | the 8 content machines + feeds + mix + web-research tiers |
| `copy-playbook.md` | how to write — hook library, quality bar, honesty rule, `market_gap` maths |
| `content-agents.md` | the automation blueprint — the Poster + Reporter agents |
| `marketing_agents/poster.py` | the built Poster module |
| `.github/workflows/content-poster.yml` | the manual-dispatch workflow that runs it |

## 1. The 8 content pillars (angle machines)
An **angle is a machine** — a feed + a lens that produces posts forever — not a one-off idea. Full detail: `content-pillars.md`.

| # | Pillar | Feed (powers it forever) | Mix | Status |
|---|---|---|---|---|
| 1 | **Deals** | live inventory, every scrape | 35% | ✅ automated (the Poster) |
| 2 | **Education** | 3-ring real-estate syllabus (~95 topics) | 25% | spec + 1 rendered |
| 3 | **Market data** | graph aggregation queries | 15% | queries proven |
| 4 | **Real-estate news** | external + auction tie-in | 5% | needs a monitoring feed |
| 5 | **Geo spotlights** | 43 cities × 1,029 areas | 10% | data ready |
| 6 | **Evaluation walkthroughs** | the product demo as content | 10% | — |
| 7 | **Q&A** | real anonymised user questions | — | switches on with traction |
| 8 | **Build in public** | changelog / founder journey | — | — |

Data powers 1/3/5/6 (Poster-automatable). A syllabus powers 2. The outside world powers 4. Users/founder power 7/8.

## 2. Education = all of real estate, through our lens
Not auction-only — the audience for real-estate knowledge is *everyone* buying property in TN. Three rings (`content-pillars.md`):
- **Ring 1 — bank-auction core** (SARFAESI, EMD, possession, notices): unique authority.
- **Ring 2 — TN essentials** (patta/chitta/EC + TNREGINET, DTCP vs CMDA, guideline value, land units): the moat vs national portals; prime AI-search citation material.
- **Ring 3 — general wisdom** (rent-vs-buy, appreciation drivers, road width, facing): broadest reach; grounded in real user questions.

**Anchor rule:** every Ring-2/3 post ends by tying back to evaluation ("…and here's how to check it"), so education funnels rather than drifting into content-farm territory. **Honesty (education edition):** teach how to *verify*, never certify; cite official sources.

## 3. The star angle — `market_gap` (below-market, computed)
The most attractive auction fact — *below market price* — made a defensible calculation, not a slogan (`copy-playbook.md`):
- **Flat** (summation method): `fair_value = built_up × construction_rate + UDS × land_rate`, vs reserve. Construction ₹2,300–2,500/sqft standard (cited, depreciated for older flats), land rate web-sourced, both areas used.
- **Land/plot**: `land_extent × area_land_rate` vs reserve.
- **Guardrails**: never divide by UDS; normalise units (acre/cent/ground/sq.m→sqft); drop the gap on ambiguous data; frame a large gap as "ask *why*" (possession, litigation), not "steal."
- **Prerequisite**: extent lives only as free text in notices today → needs an extraction step before the Poster can automate this angle.

## 4. The copy system (how every post is written)
`copy-playbook.md`, translated from the installed `social` / `copywriting` / `copy-editing` skills into AuctionScope's data:
- **Hook database** — `marketing/hooks.json`: ~90 curated hooks across all 8 pillars, each on 3 surfaces (caption/reel/card), mechanism-tagged, budget- and honesty-gated in CI. Human swipe file: `hook-database.md`.
- **Quality bar** — Clarity · Prove-It (a real figure) · So-What · Voice · Honesty.
- **Honesty rule** — banned words (`due diligence`, `guaranteed`, `title-clear`…) enforced *in code* in `validate_drafts()`.

## 5. The three formats (all real, all brand-matched)
Rendered on the design system (cobalt `#0052ff`, Bricolage Grotesque / Inter / JetBrains Mono):

| Format | Size | Pipeline |
|---|---|---|
| **Static post** | 1080×1080 | HTML → Chromium screenshot |
| **Carousel** | 4–5 × 1080×1080 | same, multi-slide |
| **Reel** | 1080×1920 | **HyperFrames** → GSAP motion graphics → MP4 — **all 8 pillars covered by 6 templates** (deal / stats / education / geo / news / evaluate), each **dark + light themed** (islands carry `theme`; auto-generated deal reels alternate). Deals + stats auto-generate via the Poster; the rest are a 2-minute island edit (`marketing/samples/reels/`). CI renders to a workflow artifact (silent — add trending audio in-app) |

Reels are genuine motion (animated counters, badge stamps, kinetic type), not slideshows. One asset set covers all platforms.

## 6. The automation — "The Poster" robot
`marketing_agents/poster.py` + `.github/workflows/content-poster.yml`:
- **Pipeline**: `--prepare` (live data → prompt) → *engine* → `--finalize` (validate → stage in `marketing/outputs/<date>/`).
- **Engines**: **Claude Max subscription by default** (₹0/run, via the `CLAUDE_CODE_OAUTH_TOKEN` secret), **OpenRouter fallback** (workflow dropdown).
- **Web-research tiers**: no-research (Deals) · research-verified (Education/Geo/`market_gap` — web claims must carry a source URL) · research-driven (News — search first, post only if warranted).
- **Guardrails**: Tier-1 only (drafts + stages, never publishes); `AGENTS_ENABLED` kill switch; opens a "content-review" GitHub issue as the notification.

## 7. Distribution
One asset set → cross-posted to Instagram, Facebook, LinkedIn, X, YouTube (English-first). A human reviews the staged folder and publishes (scheduler or by hand). **Nothing auto-posts.**

## 8. Build status
| Built & merged | Spec'd (not built) | Needs a human action |
|---|---|---|
| Poster module + workflow + tests | `market_gap` extractor (extent/UDS/possession) | add `CLAUDE_CODE_OAUTH_TOKEN` secret |
| Copy playbook + 8 pillars | evaluate-reel auto-fill (needs product Q&A) | land/plot quoting conventions |
| HyperFrames skills (20) + hook system | News monitoring feed | wire Reporter metrics CSV export |
| Static-card auto-render + **reel auto-gen** (deal + stats reels, CI artifact) | baked BGM / R2 hosting for reels | |
| Agent B Reporter + GA4 analytics | `hook_mechanism` column in metrics CSV | |

**The honest one-liner:** the full chain is now automated — live data → honest copy with scored hooks → static cards + **retention-engineered reels rendered in CI** — and a human still publishes every post. The remaining stitching: the `market_gap` extractor and the evaluate-reel's product-Q&A feed.
