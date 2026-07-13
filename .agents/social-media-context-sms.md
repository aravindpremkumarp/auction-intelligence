# Social Media Context (bridge)

The blacktwist `*-sms` skills read this file for niche, voice, platforms, and
goals. AuctionScope keeps that context in **one** place — `.agents/product-marketing.md`
(the brand hub) — so this file is a pointer, not a second source of truth.

**When an `*-sms` skill asks for context, use `.agents/product-marketing.md`.**
The essentials, so a skill invocation doesn't have to open it:

- **Product / niche:** AuctionScope (auctionscope.in) — an AI research assistant
  for Indian bank-auction (SARFAESI) property, Tamil Nadu-first.
- **Audience:** retail bank-auction buyers and small investor syndicates — the
  active bidder and the deal hunter. Not enterprises, banks, or auction houses.
- **Platforms:** Instagram, Facebook, LinkedIn, X, YouTube — one asset set
  cross-posted, English-first (see `docs/marketing/plan.md` Move 7).
- **Content pillars (angles):** price_drop · closing_soon · cheapest · evaluate ·
  educate (see `docs/marketing/content-pillars.md`).
- **Voice:** understated, plain-spoken, lowercase, calm. The number does the
  work. Full spec: `.agents/product-marketing.md` → "## Brand Voice".
- **Honesty rule (non-negotiable):** we help people *research and evaluate*; we
  never claim legal/title diligence or certainty. Never use "due diligence",
  "advocate", "guaranteed", or hype ("viral", "10x", "secret").
- **Goal now:** organic reach → traffic to auctionscope.in and Pro trials, at
  ~zero budget. Founder-hours are the binding constraint.

For performance analysis specifically, the auctionscope-native path is the
Reporter agent (`marketing_agents/reporter.py`) + `docs/marketing/analytics-playbook.md`,
which apply these skills' method with our numbers and voice.
