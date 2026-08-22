# Bidding — deadlines, EMD, increments

The mechanics of actually taking part. All counts are from the live graph,
22 Aug 2026, over 3,262 `Auction` records.

## The deadline that matters is not the auction date

| Field | Present on |
|---|---|
| `auction_start` | 3,190 |
| `emd` | 3,158 |
| `bid_increment` | 2,484 |
| `application_deadline` | 2,425 |
| `inspection` | 1,989 |
| `auto_extension_minutes` | 1,669 |

**The application deadline is a median of 1 day before the auction** (p10: 0
days, p90: 3 days), and on 396 of 2,410 auctions it falls on the *same day*.
Miss it and the auction date is irrelevant — registration and EMD have to be
in before it closes.

So when someone asks about an upcoming auction, lead with the deadline, not
the auction date. "The auction is on the 14th, but applications and EMD close
on the 13th" is the useful sentence.

9 auctions record a deadline *after* the auction start. That is a notice
error, not a rule — say the dates in the notice disagree rather than picking
one.

## EMD is 10%, and an exception is worth flagging

Earnest Money Deposit is 10% of the reserve price on 2,977 of 3,145 auctions
where both are known — the median is exactly 10.00%, and so are the 10th and
90th percentiles.

That makes EMD a **check**, not just a number: if a listing's EMD is not
about a tenth of the reserve, say so, because it usually means one of the two
figures was misread from the notice.

EMD is refunded to unsuccessful bidders and adjusted against the price for
the winner. It is forfeited if the winner fails to pay the balance — normally
25% within 24 hours and the rest within 15 days, but **this graph does not
store payment terms**, so read them off the notice text (`sale_terms`) rather
than stating the usual schedule as fact.

### Where the EMD is paid is usually not in the notice

Only 334 of 1,628 notices (21%) name an EMD account. For the other 79%,
`get_property` reports the gap and the answer should too: "the notice doesn't
give an EMD account — you'd get those details from the auction platform when
you register."

## Increments and auto-extension

- **Bid increment**: median ₹25,000, ranging ₹500 to ₹1,00,00,000. This is
  the minimum step above the standing bid.
- **Auto-extension**: median 5 minutes, range 3–15. A bid placed in the last
  few minutes pushes the close out by that much, repeatedly. Practical
  consequence: **an auction does not end at its stated end time if bidding is
  live**, so a bidder should not plan to be elsewhere at the close.

Both are absent on roughly a quarter and a half of auctions respectively —
missing, not zero.

## Inspection

1,989 auctions (61%) give an inspection date. When there is none, say so: a
buyer bidding on a property they have not seen, with symbolic possession and
an occupant still in it, is the classic way this goes wrong. Cross-reference
the `possession-and-encumbrance` skill when possession is symbolic.

## Platform

`service_provider` on the listing names where the auction runs — BAANKNET
(1,156), bankeauctions.com / C1 India (489), bankauctions.in (256),
drt.auctiontiger.net (137), and a tail of smaller ones.

**Watch the spelling.** "Public Auction" (308) and "PublicAuction" (285) are
the same thing stored two ways — 593 combined. Any count or filter by
platform has to allow for both, and a breakdown that lists them separately is
wrong even though the numbers are real.

Registration, KYC and digital-signature requirements live on the platform,
not in this graph. Point there rather than guessing at the steps.

## What this graph cannot tell you

No bid history, no bidder counts, no results. `Auction.outcome` is only ever
"unsold" — there is no record of any property selling. Never say how
competitive an auction was, how many people bid, or what it went for.
