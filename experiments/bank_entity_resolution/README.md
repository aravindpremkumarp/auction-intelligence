# Does Fellegi-Sunter beat the lender-name rule?

Short answer: no, and the experiment says why. What helped was a better
comparison, not a probabilistic model. The winning rule shipped as
`pipeline.entity_resolution.ocr_variant_of`.

## Running it

```bash
python experiments/bank_entity_resolution/compare.py
```

Offline against `banks.csv`, no database. Splink is optional (`pip install
splink`); without it the rule-based methods still run. To refresh the snapshot:

```bash
NEO4J_HTTP_API=1 python experiments/bank_entity_resolution/fetch_banks.py banks.csv
```

## The answer sheet

`gold.py` labels all 194 raw `bank_name` strings into 107 lenders: token-set
equality as the base, plus 14 merges it cannot reach. Scored pairwise, once per
name pair rather than once per notice, so 246 Canara Bank notices cannot drown
out the single-notice OCR damage that is the whole question.

The labels are not just one person's opinion. Nine of the 14 added merges
already carried an **approved** `(:ResolutionDecision)` from a human reviewer,
recorded before this file existed.

## Results

| method | precision | recall | F1 | missed | wrong |
|---|---|---|---|---|---|
| A. token-set equality only (the rule before this) | 1.000 | 0.721 | 0.838 | 43 | 0 |
| B. equality + fuzzy ≥ 88 auto-applied | 0.962 | 0.974 | 0.968 | 4 | 6 |
| **D. equality + one misread token (shipped)** | **1.000** | **0.838** | **0.912** | **25** | **0** |
| E. D + space-insensitive (rejected) | 0.414 | 0.987 | 0.583 | 2 | 215 |
| C. Splink @ 0.99 | 1.000 | 0.675 | 0.806 | 50 | 0 |
| C. Splink @ 0.90 | 0.271 | 0.805 | 0.406 | 30 | 333 |
| C. Splink @ 0.70 | 0.278 | 0.844 | 0.419 | 24 | 337 |

Method D is what shipped: recall up 12 points with precision still exactly 1.

## Why Splink lost

Not because Fellegi-Sunter is wrong. Because this problem does not have the
shape it needs.

**One field, no independent evidence.** FS earns its keep by weighing several
clues that fail independently — name, date of birth, postcode. Here there is
one string. Every feature derived from it (token key, first token, character
similarity) moves together, so the independence the model assumes is violated
by construction and the weights are near-duplicates of each other.

**EM has no smoothing, and 192 records is not many.** Among the pairs EM
treated as matches, `token_count` always agreed, so its m-probability for
"counts differ" went to zero and the learned weight to −170 (log2). That single
level became an infinite veto, which is why the score distribution is bimodal:
everything sits above 0.99 or below 0.9, with nothing usable in between. There
is no threshold that trades precision for recall, only a cliff.

**Term-frequency adjustments cannot reach the cases that matter.** The rarity
argument for FS is real — two notices naming a three-notice ARC is much stronger
evidence than two naming Canara Bank — but TF applies to *exact* matches of a
key. The OCR-damaged pairs have different keys by definition, so TF never sees
them.

A first attempt fed Splink one row per notice: 1,600 rows holding 194 distinct
strings, so nearly every "match" it trained on was byte-identical. EM learned
that a match means literal equality and pushed all near-matches to the non-match
side. Fixing that (one row per name) helped, and it still lost.

## What actually worked

Look at what separates a real merge from a trap in this corpus:

- OCR breaks characters **inside** a word: `Karur` → `Kanur`, `ICICI` → `IICI`,
  `Piramal` → `Pirama`, `Cholamandalam` → `Cholamandam`.
- A different company adds or swaps a **whole word**: `Asset Reconstruction
  Company (India)` vs `India SME Asset Reconstruction Company`, `Bajaj Finance`
  vs `Bajaj Housing Finance`, `Axis Bank` vs `Axis Finance`.

So drop the tokens two names share and require the single leftover token on each
side to pair up within a tiny edit budget. Whole-string similarity cannot make
this distinction at any threshold, which is why method B merges the 92.9 trap
and method D does not.

The edit budget is **relative as well as absolute**, and that is load-bearing.
Two edits inside `Cholamandalam` is noise; two edits inside `CSB` produces
`DCB`, a different bank. Without the relative bound, CSB / DCB / UCO collapse
into one lender and so do ICICI / IDBI / IICI.

## Method E, and why it is still here

E adds whole-name similarity with the spaces removed, to reach `Tamil Nadu` /
`Tamilnadu`. It gains two true pairs and 215 wrong ones.

The mechanism is worth keeping on the record, because `promote_extractions`
phase C builds `:Parcel` the same way. `AU Small Finance Bank` comes within the
budget of `Jana Small Finance Bank` — the shared suffix dominates a short
distinguishing token — and connected components then chains AU, Jana, Equitas,
Ujjivan, Utkarsh and Unity into a single lender. One weak edge, one merged blob.
That is the over-merge failure of transitive closure, reproduced on live data in
about twenty lines.

## What the rule still leaves for a human

25 pairs, all the same shapes, all correctly refused rather than guessed:

- a name carrying its own abbreviation — `... (India) Limited` vs
  `... (India) Limited (ARCIL)`, `JM Financial Asset Reconstruction Company` vs
  `JMFARC (JM)`
- a pure acronym — `PNB` vs `Punjab National Bank`, which must not reach `PNB
  Housing Finance`
- a stray prefix — `Housing CHOLAMANDALAM ...` vs `CHOLAMANDALAM ...`
- spacing inside a name — `Tamil Nadu` vs `Tamilnadu`
- an honorific — `M/s Religare Finvest Ltd.` vs `Religare Finvest Ltd`

These are the review queue's job. Each is a small, self-contained normalisation
if someone wants the recall, and each carries its own way to be wrong: stripping
a parenthesised abbreviation is safe, treating any parenthetical as noise is not.

## One thing to check

`LIC Housing Finance Ltd` and `UNICO Housing Finance Private Limited` carry an
**approved** merge decision in the graph. They are different companies. Both
auto-merge tiers refuse the pair and the fuzzy queue only proposed it, so the
merge exists solely because someone approved it. Worth re-opening.
