# Go-to-market

A focused plan for a single-market (Tamil Nadu), single-team product. The strategy is
**depth in one geography, one channel at a time** — not broad, thin coverage.

## The funnel

```
Discover ──▶ Try ──▶ Activate ──▶ Retain ──▶ Refer
  SEO,       free      first        watchlist,   brokers,
  social,    chat      grounded     re-auction   WhatsApp
  brokers    query     answer       alerts       forwards
```

- **Discover** — how a stranger first hears of us.
- **Try** — anonymous chat is already open (no signup wall) — protect that; it's the hook.
- **Activate** — the "aha" is the first *grounded* answer to a real question ("flats in
  Chennai under 40L with the notice attached"). Get them there fast.
- **Retain** — accounts + watchlist + re-auction awareness give a reason to return; a
  property's status changes over time.
- **Refer** — brokers and WhatsApp groups are the viral loop in Indian real estate.

## Channels, ranked by leverage

### 1. Programmatic SEO — the primary bet
The graph already holds thousands of city- and asset-type-tagged auctions. That is a
ready-made moat for long-tail, high-intent search ("bank auction flats in Chennai",
"SARFAESI residential auction Kanchipuram"). Full plan in [`../seo/plan.md`](../seo/plan.md).
**This is where most effort should go.**

### 2. WhatsApp / Telegram communities
Auction deals already circulate as WhatsApp forwards. The **paste-a-listing** feature is
built for exactly this behaviour — meet buyers where they already are. Seed a few active
property/auction groups, be genuinely useful, let the paste feature demonstrate value.

### 3. Founder-led content (LinkedIn + YouTube)
Brand kit already has LinkedIn assets ([`../../brand/logo/`](../../brand/logo/)). Post
real teardowns: "I analysed every Chennai bank auction under ₹50L this month — here's
what I found." Show the product doing the work. Long-form YouTube walkthroughs of the
deep-research mode build trust with high-ticket/NRI buyers.

### 4. Broker / channel-partner partnerships
Brokers are both power users and a distribution channel. A referral arrangement or a
"pro" tier for brokers turns them into a sales force.

### 5. Content SEO / blog
Educational content on the auction process (how SARFAESI works, EMD, encumbrance
certificates, bidding) captures top-of-funnel searchers before they know they need us.

## 90-day plan

**Days 0–30 — Foundation**
- Ship programmatic city × asset-type landing pages (SEO plan phase 1).
- Verify `sitemap.xml` covers the new pages; submit to Google Search Console.
- Publish 3 cornerstone educational posts (auction process, EMD, due diligence).
- Set up analytics + conversion events (chat-started, signup, watchlist-add).

**Days 31–60 — Distribution**
- Seed 3–5 WhatsApp/Telegram property groups; demo paste-a-listing.
- Launch founder LinkedIn cadence (2 posts/week: real auction teardowns).
- First YouTube walkthrough of deep-research mode.

**Days 61–90 — Loops**
- Broker referral pilot with 5–10 brokers.
- Re-auction / price-drop email or WhatsApp alerts to activate watchlist users.
- Double down on whichever channel showed the best cost-per-activation.

## Metrics that matter

| Stage | Metric | Instrument |
| --- | --- | --- |
| Discover | Organic impressions / clicks | Search Console |
| Try | Chat sessions started (anon + auth) | product analytics event |
| Activate | % of sessions reaching a grounded answer; signups | funnel event |
| Retain | Watchlist adds; 7/30-day return rate | app DB / analytics |
| Refer | Paste-a-listing uses; broker referrals | product event / manual |

**North-star:** *weekly active users who reach a grounded answer* — it captures reach,
activation, and product value in one number.

## Open decisions (need a human call)

- **Monetisation** — free product today (`billing.js` exists). Pro tier? Broker tier?
  Pay-per-deep-research? This shapes the funnel's "Refer/Retain" asks.
- **Geographic expansion** — stay TN-deep, or start a second state once SEO compounds?
- **Analytics stack** — pick one (Plausible / PostHog / GA4) before spending on channels.
</content>
