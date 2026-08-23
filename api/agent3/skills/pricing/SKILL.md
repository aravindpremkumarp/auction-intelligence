# Skill: pricing

Load this when the question is about whether a price is reasonable — "is
this a good price", "is this cheap", "how does this compare", "what's it
worth", "price per square foot".

## The one thing to say every time

**This graph has no sold prices.** `Auction.outcome` is only ever "unsold".
So `benchmark_price` compares a bank's *reserve* (its floor) against other
banks' *reserves*. It cannot tell anyone what a property is worth, what one
sold for, or what to bid. Say that plainly — do not soften it into "roughly
market rate".

## Reading a result

`comparisons` walks widening rings: same area → same city → same district →
same property type statewide. Each ring reports `median_psf`, `p25_psf`,
`p75_psf`, `comparables` (n), and `subject_percentile`.

- **Quote the tightest ring that qualified**, and name it. "88th percentile
  of Coimbatore listings" is a claim someone can check; "expensive" is not.
- **Percentile direction:** higher = more expensive per sqft than its
  comparables. An 88th percentile means only 12% of comparable listings are
  priced higher per sqft.
- **A ring is only summarised at 5+ comparables.** Anything thinner appears
  in `rings_too_thin` with its count. Do not compute your own median from a
  handful of rows to fill the gap.
- **Area rings are usually thin, and that is expected** — only 36 of 417
  areas have 5+ priceable listings. Landing on city is the normal case, not
  a degraded one; do not apologise for it.

## When it refuses — and it usually does

`priced: false` with a `reason`. Report the reason; it is the answer, not an
error. The common ones:

- **Multi-lot notice.** The reserve price belongs to the listing, the extent
  to a lot. When a notice covers several lots, nothing says which lot the
  price refers to, so a price per sqft would be invented. This refuses
  roughly 70% of listings — of 2,750 with a reserve price, only 832 sit on a
  single-lot notice with a usable extent.
- **No usable extent**, or a price per sqft outside ₹10–₹100,000, which
  means the notice's own extent or price is wrong.

If the user still wants a comparison, `find_properties` with
`area_sqft_min`/`area_sqft_max` and a price range finds similar listings
without claiming a per-sqft figure.

## What not to do

- Never present a percentile as a valuation or a recommendation to bid.
- Never average across rings, or mix a ring's median with the subject's own
  figure to produce a new number.
- Never compare a reserve price to an asking price, a guideline value, or a
  circle rate — none of those are in this graph.
