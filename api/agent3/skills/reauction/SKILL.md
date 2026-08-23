# Skill: reauction

Load this when the question is about whether a property has been auctioned
before — "has this failed to sell", "is this a re-auction", "has the price
dropped", "how many times has this been listed".

## Two signals, and they answer different questions

`reauction_history` returns both. Keep them apart in your answer.

**1. `attempts` / `highest_attempt_no`** — what the sale notice itself says.
This is the notice's own statement, so it is the stronger claim. 206 auctions
in this graph sit at attempt 2 or later (144 at attempt 2, 36 at 3, 18 at 4,
trailing to 8). It answers *"has this failed before"*.

**It carries no earlier price.** An attempt-2 row shows only its own reserve.
So `attempt_no` alone can never tell you how much the price came down.

**2. `earlier_listings`** — other listings matched as the same property via
`SAME_PROPERTY_AS` (80 links across the graph). These *do* carry the earlier
price, so `price_change` can be computed. It answers *"by how much"*.

**This is an inferred link, not a stated one.** Each carries a `confidence`
of `high` or `medium`, from a pipeline match on borrower and location. Say
which when you rely on it: "a listing the pipeline matched as the same
property (high confidence)" is honest; "this property was listed before at
₹45L" states an inference as fact.

## Reading a price drop

Drops in this corpus cluster near **10%** — ₹45.58L→₹41L, ₹69L→₹62.1L,
₹52L→₹46.8L. That is the standard SARFAESI reduction when re-offering after
a failed auction, applied by rule.

So a 10% drop is **not** a bargain specific to this property, and saying "the
price has dropped 10%, this is a good deal" is misleading. What it does tell
a buyer: the property failed to attract a bid at the higher figure, which is
information about demand — and possibly about a problem with the property
worth checking in the notice (load the `diligence` skill for that).

A drop materially larger than 10% is worth remarking on; it means more than
one reduction, or a bank moving faster than convention.

## The common case

Most properties are first-time listings: no attempt above 1 and no links.
That is not a gap in the data — say "no earlier auction is recorded for this
property" and move on. Do not imply the history is missing.

## Scope

`attempt_no` comes from the notice, which may cover several lots. On a
multi-lot notice the result carries `scope: "notice"` and a `scope_note` —
the attempt describes the notice's auction, not necessarily this listing
alone. Carry that through, same as everywhere else.
