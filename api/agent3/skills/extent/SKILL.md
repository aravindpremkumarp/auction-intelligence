# Skill: extent

Load this when the question involves an area, size, or unit conversion —
"how big," "in acres," "convert this to sqft," or when comparing sizes
across lots that use different units.

## Unit conversion table (exact, matches the pipeline's own normalisation)

| Unit | To sqft |
|---|---|
| `sq_ft` | 1 |
| `sq_yard` | 9 |
| `sq_m` | 10.76391 |
| `cent` | 435.6 |
| `ground` | 2,400 (a Chennai-specific unit, still common in TN notices) |
| `are` | 1,076.391 |
| `acre` | 43,560 |
| `hectare` | 107,639.104 |

`sqft_norm` on a `Measurement` node is already converted to this scale —
you don't need to convert it again. Convert only when a user asks for a
different unit ("how many cents is that").

## The plausible band — and why it exists

Extent values in this graph have real outliers: the minimum recorded is
0.0, the maximum is 15,571,959,480 sqft, against a median of 1,471 and a
p90 of 10,977. `find_properties` and `get_property` both clamp to
**1–500,000 sqft** and exclude anything outside it, counting the exclusion
rather than silently averaging it in. If a returned value looks absurd,
it's already been filtered — don't try to "fix" a value yourself; report
what the tool gave you.

## `extent_kind` — pick the right one, don't average across kinds

A lot can carry several extent rows, tagged by kind: `extent`, `total`,
`built_up`, `uds`, `uds_parent`, `super_built_up`, `carpet`. The tools
prefer the one marked `is_headline` — that's the size that describes the
lot itself. Never substitute a different kind for it, and never average
two kinds together (built-up + carpet is not a real number).

**The UDS trap, specifically.** A flat sits on a larger plot and owns only
an *undivided share* of it (`uds`). The parent plot's own extent
(`uds_parent`) belongs to the whole plot, not the flat. A 760 sqft flat
sitting on a 2,257 sqft parent plot is a 760 sqft flat — quoting the
parent extent as the flat's own area is a documented real error in this
corpus and exactly what `is_headline` selection exists to prevent. If a
lot's only extents are `uds`/`uds_parent` with no `total`/`extent`/
`built_up`, say the usable area is not stated rather than substituting the
parent size.

## Reporting a range vs a single number

If the property is `scope: "lot"`, report its `headline_sqft` directly.
If it's `scope: "notice"`, report the range across the notice's lots
(`sqft_range` / `notice_area_sqft_range`) and say plainly that it spans
several lots — don't pick the largest, the smallest, or an average and
present it as this property's own size.
