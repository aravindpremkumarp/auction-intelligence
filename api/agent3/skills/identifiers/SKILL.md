# Skill: identifiers

Load this when a survey, patta, door, plot, flat, block, or CERSAI number
appears in the conversation — either the user gives one, or a tool result
surfaces one worth explaining.

## Kinds, and what each one is

- **`survey_old` / `survey_new`** — the revenue survey number. Old and new
  numbering schemes coexist in Tamil Nadu after re-surveys; the same
  parcel can carry both. This is the number that ties a notice to a
  specific piece of land in the revenue register — the single most
  important identifier for verification.
- **`patta`** — the ownership record number (Tamil Nadu land-ownership
  document). A notice without one is a real diligence gap (see the
  `diligence` skill), not a minor omission.
- **`door_old` / `door_new`** — the municipal door number for a built
  structure. Only meaningful for buildings, not vacant land.
- **`plot`** — the layout plot number, used in approved layouts rather
  than raw survey numbers.
- **`flat`** — the unit/flat number within an apartment building.
- **`sale_deed`** — the registered sale-deed number, if the notice cites
  the borrower's original purchase document.
- **`approved_layout`** — the layout approval reference (e.g. a
  DTCP/CMDA/local-body approval number).
- **`assessment_old` / `assessment_new`** — the property-tax assessment
  number.
- **`property_id`** — an internal reference id the notice or the auction
  platform assigns to the property, separate from any revenue identifier;
  useful for cross-checking the notice against the platform listing, not
  for verifying title.
- **`block`, `cersai`, `floor`, `ward_no`, `chitta`, `khata`** — narrower
  identifiers; `cersai` is the central registry number for the security
  interest itself, worth surfacing when a user asks about the charge
  rather than the land.

## Normalising a user's query

Survey numbers arrive noisy — `S.No 45/2`, `Survey No. 45-2`, `45/2A` all
refer to comparable things. `find_by_identifier` and `find_properties`'
`identifier=` filter already phrase-quote and fulltext-match the value, so
pass the number close to as given; don't over-clean it into a canonical
form that might not match the notice's own spelling.

## Reading a match

`find_by_identifier` groups every listing that shares one matched
identifier value. Two things follow from that:

- **Several listings can share one match.** A survey number matching two
  or more `auction_id`s usually means those listings share the same
  underlying sale notice, not that the number is ambiguous — say "this
  survey number is on N listings from the same notice," not "I found
  conflicting matches."
- **Scope still applies.** A match on a multi-lot notice is tagged
  `scope: "notice"` — the identifier is confirmed to be *in that notice*,
  not confirmed to belong to *this specific listing* if several lots
  share the notice. Carry the same scope discipline as everywhere else.

## Zero matches

A zero-result identifier lookup means this graph's notices don't mention
that number — it is a **graph gap**, not proof the property doesn't
exist. Say exactly that; never imply the property is fictitious.
