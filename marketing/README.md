# AuctionScope — Marketing

The marketing workspace for **[AuctionScope](https://www.auctionscope.in)** — the AI
intelligence platform for Indian SARFAESI bank-auction property.

This directory is the home for positioning, go-to-market, SEO, content, and launch
assets. It lives **inside the product repo on purpose** — see the decision below.

---

## Should marketing be a separate repo?

**Decision: no — keep it here (for now).** Revisit only if a trigger below flips.

For a small team shipping a deploy-split app (Vercel front + Render back) with brand
assets already in [`brand/`](../brand/), a separate repo would mean two CI configs,
two deploy targets, and duplicated brand/copy — real overhead with little payoff. A
`marketing/` directory keeps everything in one place, reuses the existing brand, and
lets SEO/content ship alongside the product.

**Split it out only when one of these becomes true:**

| Trigger | Why it changes the answer |
| --- | --- |
| A **separate marketing team** joins who shouldn't touch app code | Access boundaries are cleaner as a repo boundary |
| The marketing site adopts a **different stack** (Next.js + CMS, Astro, Framer) | Its own build/deploy no longer fits this repo's no-build model |
| You want the **marketing site public** while the app stays private | Repo visibility is per-repo |
| Marketing ships on a **very different cadence** from the product | Decouples release trains |

Until then: everything below is the plan.

---

## What's here

```
marketing/
├── README.md                  ← you are here — hub + repo decision
├── strategy/
│   ├── positioning.md         ← who it's for, the one-liner, differentiators
│   └── go-to-market.md        ← channels, funnel, 90-day plan, metrics
├── seo/
│   └── plan.md                ← the programmatic city/asset-type SEO play
├── content/
│   └── calendar.md            ← content pillars + first-quarter calendar
├── social/
│   └── launch-kit.md          ← launch posts, WhatsApp/Telegram, founder-led
└── assets/                    ← campaign-specific exports (brand source stays in ../brand)
```

## Where things already live (don't duplicate)

- **Brand identity** — logo, wordmark, colors, LinkedIn assets → [`../brand/logo/`](../brand/logo/)
- **Product UI / landing page** → [`../web/index.html`](../web/) (the app's own hero)
- **Clean-UI redesign prototype** → [`../redesign/`](../redesign/)
- **SEO plumbing** → [`../web/robots.txt`](../web/robots.txt), [`../web/sitemap.xml`](../web/sitemap.xml)

## Start here

1. Read [`strategy/positioning.md`](strategy/positioning.md) — align on who and why.
2. Read [`strategy/go-to-market.md`](strategy/go-to-market.md) — pick the first channel.
3. The highest-leverage bet is [`seo/plan.md`](seo/plan.md) — the graph already holds
   thousands of city-tagged auctions, which is a ready-made programmatic-SEO moat.
</content>
</invoke>
