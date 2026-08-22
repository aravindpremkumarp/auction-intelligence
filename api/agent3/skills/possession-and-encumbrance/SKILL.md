# Possession and encumbrance

What the buyer is actually getting, and what the notice does *not* tell them.
Every number below was counted on the live graph, 22 Aug 2026.

## Possession — the single most important field

`POSSESSION_IS` points a lot at one of exactly three types.

- **physical** (706 lots) — the bank holds the keys. The property is empty or
  the bank controls who is in it. This is the cleanest case for a buyer:
  possession transfers with the sale.
- **symbolic** (867 lots) — the bank has served notice and taken possession
  *on paper only*. **Someone is very likely still living or trading in the
  property.** The buyer, not the bank, will have to get them out, through the
  District Magistrate under s.14 of the SARFAESI Act. That process takes
  months and sometimes years. Symbolic is the most common type in this data,
  and it is the fact most often glossed over.
- **constructive** (412 lots) — between the two: the bank has asserted
  control without physically occupying. Treat it as symbolic unless the
  notice says otherwise.

**Say which one it is, in plain words, whenever possession comes up.** "The
bank has symbolic possession, which means the occupants are still there and
evicting them would be your job" is the sentence that matters. "Possession:
symbolic" on its own tells a non-expert nothing.

### 40% of lots say nothing at all

1,985 lots of 3,335 (60%) state a possession type. The other 1,350 do not —
1,307 where the notice is explicitly silent, 43 where nothing could be read.

A missing possession type is **not** a reason to assume physical. It is a gap
to report: "the notice does not say what kind of possession the bank has,
which you would need to establish before bidding."

## Encumbrance — read the field, then discount it

`Lot.encumbrance` exists on only 1,009 of 3,335 lots (30%). Of those 1,009:

| What it says | Lots |
|---|---|
| "Nil" / "None" / "No encumbrances" | 683 |
| "Not known" / "Unknown" / "no such information" | 213 |
| Anything else | 113 |

And the 113 are almost entirely more of the same in longer form — "Not to the
knowledge of the bank", "No other prior encumbrance ... has come to the
knowledge of the company", "There is no stay on the property".

**So: essentially no notice in this corpus discloses an actual encumbrance.**
That is a fact about bank notices, not about the properties. Sale notices
carry a standard disclaimer; they are not the output of a title search.

This changes what you are allowed to say:

- ✅ "The notice records encumbrance as Nil — but that is the bank's standard
  wording, not a title search. An encumbrance certificate from the
  sub-registrar is the only way to actually check."
- ✅ "The notice says encumbrances are not known to the bank."
- ❌ "The property is free of encumbrances." — the notice does not support
  this, and no lot in this data does.
- ❌ Treating a missing `encumbrance` field as "clear". 70% of lots have no
  value at all.

## Where the hard line is

Possession type, encumbrance wording, and the loan outstanding are **facts
recorded in the notice**. Report them, name what is missing, and explain what
each means for a buyer in practical terms.

Do **not** cross into: whether the title is good, whether the eviction will
succeed, whether a stated encumbrance is enforceable, or what someone should
do. That is legal advice, and this graph holds no title chain, no court
records and no litigation history. Say plainly that a lawyer and an
encumbrance certificate are the next step — do not hedge it into vagueness,
and do not pretend the data reaches further than it does.

## Scope, as always

Possession and encumbrance are **lot** properties. On a notice covering
several lots they describe that lot, and nothing says which lot the listing
is. `get_property` tags this; carry the tag into the sentence: "the notice
covers 5 lots, and possession is recorded per lot — for this listing it isn't
determinable from the notice alone."
