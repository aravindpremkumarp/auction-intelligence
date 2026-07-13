# Analytics Playbook — reading social performance

*The measurement companion to `docs/marketing/copy-playbook.md`. Where the copy
playbook governs what the Poster writes, this governs what the Reporter
(`marketing_agents/reporter.py`, Agent B in `content-agents.md`) concludes.*

Distilled from the blacktwist `performance-analyzer-sms`,
`content-pattern-analyzer-sms`, and `optimization-advisor-sms` skills (MIT,
installed under `.agents/skills/`) into auction-specific rules — the same way
`copy-playbook.md` distils the `social` / `copywriting` skills. The skills are
the method; this file is how we apply it to AuctionScope. Voice comes from
`.agents/product-marketing.md` (the single source of truth).

---

## Part 1 — The one rule: benchmark against yourself

Never judge a post against industry averages ("the average Instagram reel gets
X%"). At ~7 users and a young account, the only meaningful benchmark is **our
own baseline**. A 5% engagement rate is good or bad only relative to *our*
median. The Reporter computes that baseline every run and grades every post
against it — nothing is a "top performer" in the abstract.

## Part 2 — Engagement rate is the currency, not likes

```
engagement rate (ER) = (likes + comments + reposts + saves) / impressions × 100
```

A post at **8% ER from 500 impressions beats one at 2% from 10,000** — it
resonated harder even though fewer people saw it. Always rank by ER, never by
raw likes. Reach (impressions) and resonance (ER) are different problems:

| Symptom | Reading | Fix lives in |
|---|---|---|
| High impressions, low ER | reaching people, not connecting | the hook / angle (copy-playbook) |
| Low impressions, high ER | connects, but not being seen | cadence, format, platform |
| Both low | wrong angle or wrong platform | pillar mix (content-pillars) |

## Part 3 — The three metric tiers

Read metrics in this order; don't jump to conversion before reach is healthy.

1. **Reach** — impressions, profile visits. Are we being seen?
2. **Engagement** — likes, comments, reposts, saves → the ER above. Comments and
   saves weigh more than likes: a comment is work, a save is intent to return.
3. **Conversion** — link clicks, profile visits → the only tier tied to the
   business (traffic to auctionscope.in). A post can have great ER and zero
   clicks; that's a content-vs-CTA gap worth naming.

## Part 4 — Segment by our pillars, not generic buckets

Every post is tagged with an **angle** (our content pillars) and a **format**,
so the analysis maps straight onto decisions we can act on:

- **angle:** `price_drop` · `closing_soon` · `cheapest` · `evaluate` · `educate`
- **format:** `static` · `carousel` · `reel`

The Reporter's per-angle / per-format tables are the payoff: "evaluate reels
average 14% ER, closing_soon statics 2%" is a scheduling decision, not a vanity
number. This is the auction-specific value the generic skills can't give.

## Part 5 — Every observation ends in an action

The hard rule from `optimization-advisor-sms`, and from Agent B's spec: a
finding with no action is noise. Not *"engagement is down"* but *"evaluate
reels carry 3× the ER of statics — shift next week to 2 reels + 1 carousel,
drop the daily static."* Actions must be:

1. **Tied to a finding** in this week's data, not generic best-practice.
2. **Concrete enough to do this week** — a count and a slot, not "post more."
3. **Ranked** by expected impact.

## Part 6 — Honesty rule (the analytics version)

The Poster never invents a price; the Reporter never invents a metric.

- Every number in the report is **computed in Python from the metrics CSV**, not
  estimated by the model. The model interprets; it does not count.
- If the sample is small (< ~10 posts) or a trend is thin, **say so** — the
  report carries a `caveats` line for exactly this.
- Banned in reports: `guaranteed`, `viral`, `explosive`, `skyrocket`, `10x`,
  `hack`, `secret`. We report what happened; we don't sell it back to ourselves.
- The finalize step drops any claim referencing a post that isn't in the data.

## Part 7 — The weekly report shape

One page, always these sections (enforced by `reporter.py`):

1. **Flag** — 🟢 / 🟡 / 🔴 + one-line reason.
2. **Headline** — the single most important thing this week.
3. **The numbers** — baseline ER, avg impressions/comments, conversion totals, trend.
4. **What worked** — top performers by ER, each with *why* (hook/angle/format/timing).
5. **What didn't** — bottom performers, each with a *hypothesis* (framed as a learning).
6. **The pattern** — the clearest signal across the angle/format/platform tables.
7. **Next week** — 3–4 ranked, concrete actions.
8. **Caveats** — sample-size / missing-data honesty.

## How to run it

```bash
# Export a CSV from your platforms (or hand-fill the template):
#   post_id,date,platform,angle,format,impressions,likes,comments,reposts,saves,link_clicks,profile_visits
# See marketing/samples/post-metrics-example.csv for the shape.

python -m marketing_agents.reporter --metrics posts.csv --dry-run   # see the computed analysis, no LLM
python -m marketing_agents.reporter --metrics posts.csv             # full report (OpenRouter), staged to marketing/outputs/<date>/
```

Needs ≥ 5 posts (the analyzer floor — fewer isn't a signal). Read-only on the
outside world, so it's safe to run unattended on the Friday cron once wired up
(same workflow shape as the Poster; kill switch `AGENTS_ENABLED=false`).

## Where this fits

```
content-pillars.md  →  what we post           (angle tags come from here)
copy-playbook.md    →  how the Poster writes   →  poster.py
analytics-playbook  →  how the Reporter reads  →  reporter.py   ← you are here
product-marketing.md →  voice + honesty rule    (both agents read it)
```

Once we've run this for a few weeks, its findings should feed back into
`content-pillars.md` (double down on the pillars that earn ER) and the Poster's
angle priorities — closing the marketing loop.
