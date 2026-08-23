# Skill: diligence

Load this when the question is a deep, single-property pass — "tell me
everything about this," "should I worry about this one," "is this a safe
bid," "walk me through the notice." Not for a quick filter or a one-fact
lookup; those go straight to `find_properties` / `get_property`.

## What to do

1. Call `get_property(auction_ids=[id], depth="full")`.
2. Present in this order: what the property is (type, location, extent) →
   identity (survey/patta numbers) → physical (boundaries, road access) →
   legal/financial (possession, encumbrance, secured loan) → bidding
   mechanics (EMD, dates) → **`gaps`, last and named explicitly**.
3. If `scope` is `"notice"`, say so up front — "the notice covers N lots;
   here is what it says, not necessarily only about this specific one" —
   before reciting any lot detail.

## Reading the gaps — what each one actually means for a buyer

`gaps` is a checklist, not a footnote. Weight them:

- **No survey number** — the land cannot be checked against revenue
  records at all. The most serious gap; lead with it.
- **No patta number** — ownership cannot be verified against the revenue
  register from this notice alone.
- **Possession not stated** — this is NOT the same as "physical
  possession." Silence here means the buyer doesn't know if they're
  buying a vacant property or one they'll have to evict someone from.
  Never round an unstated possession up to "presumably physical."
- **No encumbrance clause** — read as *unstated*, never as "no
  encumbrance." A notice that says nothing about encumbrance has not
  cleared the property; it has just not addressed the question.
- **No boundary schedule** — the extent cannot be cross-checked against
  the four sides, which is the usual way a Tamil Nadu buyer sanity-checks
  a stated area.
- **No inspection date / no EMD account** — practical blockers to
  actually bidding, not diligence risk; mention but don't dwell.

## Possession, in plain terms

- **physical** — the bank holds the property; a buyer can generally take
  it over without further process.
- **symbolic** — paper possession only. The buyer may need to go through
  an eviction/possession process to actually occupy it.
- **constructive** — a narrower middle ground; read the notice's own
  wording rather than assuming either of the above.

## What NOT to do

- Don't compute a size, price, or possession status from a `notice`-scoped
  lot list — see the `identifiers`/core-rule-2 discipline; a diligence
  answer that overclaims scope is worse than a shorter, honest one.
- Don't offer a legal opinion or say a property is "safe to buy" — report
  the facts and the named gaps, and let the user weigh them.
- Don't skip the gaps section because "nothing important is missing" —
  say that explicitly instead of omitting the section.
