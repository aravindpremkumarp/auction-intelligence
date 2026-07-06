# Zillow-style "price history" timeline on listing pages

- **Status:** captured  <!-- captured | exploring | planned | implemented | parked -->
- **Date added:** 2026-07-06
- **Source:** https://www.zillow.com (example — replace with the real link)
- **Tags:** ui, review, data-display

> This is an example entry to show how to fill out `TEMPLATE.md`. Delete it
> once real inspirations exist.

## What it is

Zillow shows a compact vertical timeline of every price change and status
event (listed, price cut, pending, sold) on a property, each row with a date,
the event, and the amount.

## What caught my eye

The at-a-glance history builds trust — you instantly see whether a property is
fresh or stale and how motivated the seller is. It's dense but scannable.

## How I plan to integrate it

- Touches the **review UI** and whatever we store for a property's event
  history.
- We already scrape auction listings over time; we could surface a similar
  timeline of "seen at price X on date Y", opening bid changes, and postponement
  events.
- Do differently: auctions care about *upcoming sale date* and *postponements*
  more than sale price history, so weight the timeline toward those.
- Open question: do we currently persist per-scrape snapshots we could
  reconstruct a timeline from, or would we need to start recording deltas?

## Assets

- `./assets/2026-07-06-example-timeline.png` (none yet — placeholder)

## Next step

Confirm whether historical snapshots exist in the pipeline; if so, write a
`docs/design/` spec for an auction event timeline.
