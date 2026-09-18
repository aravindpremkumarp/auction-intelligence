# What `full_description_incomplete` is actually made of

Run: `python -m evals.diagnose_fd_coverage`
Date: 2026-09-18 · Corpus: 3,162 notices · LLM calls: 0

This re-validates entities already stored in `Document.extraction_json` — the
same source `extract_batch --from-graph` reads — so it costs nothing to re-run.
It reproduces the 2026-09-17 run's **1,173** flagged notices exactly, which is
what makes the breakdown below comparable to that baseline.

`summary.json` holds the numbers. Per-doc detail is ~1.4 MB and regenerable
with `--json`, so it is not kept, matching `extract_batch`'s own convention for
per-batch reports.

## Why this was run

`full_description_incomplete` is the largest issue in the corpus (1,173 of
3,162), and the obvious reading — the model truncates the property description,
so give it less text to read — implies a costly change to the extraction
architecture. This establishes what the flag contains before anyone pays for
that.

## The check being diagnosed

`validators.full_description_coverage` flags a descriptive entity only when
**both** arms fail (`pipeline/validators.py:180-182`):

```python
by_span = fd["span"][0] <= sp[0] <= sp[1] <= fd["span"][1]
by_text = txt in fd["text"]
if not (by_span or by_text):   # only then is it "escaped"
```

`by_text` compares stored entity text against stored `full_description` text.
It never touches the markdown, so a re-ingested page cannot produce this flag on
its own — a hypothesis worth ruling out explicitly, and the numbers below do.

## Findings

### 1. Span drift is not the story (6.1%)

Only 72 of 1,173 flagged notices have span fidelity below 0.7 — that is, where
`markdown[start:end]` no longer aligns with the entity text. 720 sit in the
0.9–1.0 band. Whatever is wrong with these notices, stale offsets are not it.
This matches what the code says: the `by_text` arm is drift-immune.

### 2. Nearly half the escapes are entities that were never anchored

Of 5,527 escaped details:

| failing arm | count | share |
|---|---:|---:|
| `span_outside_and_text_missing` — found elsewhere in the notice | 2,853 | 51.6% |
| `no_span_text_missing` — **no char span at all** | 2,547 | 46.1% |
| `fd_has_no_span` | 127 | 2.3% |

The second row is the same root cause as the `ungrounded` issue (1,042 notices,
the corpus's #2 problem). An entity with no span fails `by_span` by definition,
and fails `by_text` whenever the model normalised or composed its value rather
than copying it verbatim. **No change to how much text the model reads at once
affects this bucket.** Roughly half of the corpus's largest issue is a grounding
problem wearing a truncation problem's label.

### 3. Multi-lot notices are the larger and harder half

| | flagged | rate | fully repairable (§4) |
|---|---:|---:|---:|
| multi | 631 | 51.5% | 245 (38.8%) |
| single | 541 | 28.1% | 307 (56.7%) |

Any work scoped to single-lot notices reaches at most 46% of the population,
and the less-defective 46%.

### 4. The zero-cost repair clears about half

Widening a lot's `full_description` span to the envelope of its descriptive
spans and re-slicing the text from the markdown — no model call — clears:

| outcome | notices | share |
|---|---:|---:|
| every incomplete lot cleared | 553 | 47.1% |
| some lots cleared | 134 | 11.4% |
| nothing cleared | 486 | 41.4% |

**Caveat:** in 256 notices the widened block runs into terms-of-sale
boilerplate ("as is where is", EMD forfeiture, the Authorised Officer's rights).
A repair that swallows the terms block trades one defect for another, so it
needs a stop-at-terms guard. `diagnose_fd_coverage._TERMS_MARKER` carries the
same vocabulary the prompt uses to separate the two blocks.

### 5. Which fields escape

| class | escapes | share | dominant arm |
|---|---:|---:|---|
| identifier | 1,851 | 33.5% | found elsewhere (1,346) |
| location | 1,168 | 21.1% | **no span** (809) |
| boundary | 847 | 15.3% | no span (414) |
| property | 753 | 13.6% | found elsewhere (411) |
| extent | 524 | 9.5% | no span (312) |
| schedule | 384 | 6.9% | no span (220) |

Identifiers dominate, and they mostly escape because the notice states the
survey or door number **twice** — in a table or the preamble as well as in the
description — and the model anchored the copy outside the description block.

This has a consequence for any design that extracts descriptive fields from a
carved-out description slice: the second occurrence is outside that slice, so it
would not be found at all. The flag would clear while the extraction lost
detail. Worth stating plainly, because a metric that improves while the data
gets thinner is the failure mode this whole diagnosis exists to avoid.

## What follows from this

Ordered by value per unit of cost, not by what is most interesting:

1. **Ship the span-union repair with a stop-at-terms guard.** 553 notices, no
   model spend. Verify the guard on the 256 that would otherwise swallow terms
   text.
2. **Investigate why ~2,500 descriptive entities come back with no span.** This
   is the shared root of the corpus's #1 and #2 issues and nothing else on the
   roadmap addresses it.
3. **Re-baseline live** at the new `SCORE_VERSION` once either lands, so the
   next comparison is against a score computed on the same scale
   (`pipeline/validators.py:44-51`).
4. **Only then** size a two-pass extraction against what is left. On this
   evidence its reachable share is the residual of a residual, concentrated in
   multi-lot notices — the case a single-lot-first rollout explicitly defers.
