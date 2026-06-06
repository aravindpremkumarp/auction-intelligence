# Description completeness scoring — design

**Date:** 2026-06-05
**Stage:** Review → Description
**Status:** Spec (not yet implemented)

## Problem

In the description review stage a human must confirm, per property, that the
description we extracted from the **sales notice** is the **complete** property
description present in that notice — start to finish — and belongs to the
**correct property**. We want a `completeness` score good enough to *auto-clear*
the easy cases so humans only review the risky ones.

The score that exists today is a character-length ratio
(`pipeline/ocr_extract.py:164-167`):

```python
img_desc     = extracted.get("property_description_full") or ""
web_len      = len(record.get("description") or "")
completeness = min(web_len / max(img_len, 1), 1.0) if img_len > 0 else 1.0
```

It is unsafe as an auto-pass gate: it rewards length not content, inverts in the
common case (notice richer than website → caps at 1.0), and defaults empty
extractions to *complete* (`else 1.0`).

## Definition

> The **sales notice markdown** is the source of truth — the full legal
> property description lives there. Completeness asks one question:
> **is the extracted description (`E`) the complete property description present
> in the notice markdown (`M`) for this property — nothing missing, nothing
> truncated?** The **eauctionsindia.com** website description (`W`) is used only
> to *locate/anchor* the description and as a cheap pre-filter — it is **not**
> the yardstick for completeness (it is usually a short summary, so it cannot
> tell us whether the notice's full schedule was captured).

Two design decisions, per review feedback:

1. **No hardcoded "end-of-schedule" rule.** We do **not** score completeness by
   checking for boundary fields or any fixed structure. We ask whether `E`
   captures the complete description that is actually in `M`.
2. **Similarity is contiguous, not word-by-word.** Where we do compare texts,
   matches are scored over **continuous sentences and paragraphs**, not isolated
   words.

## Inputs (all already in Neo4j — no new extraction)

| Symbol | Source field | Meaning |
|---|---|---|
| `M` | `d.markdown` (linked `Document`) | **source of truth** — full notice OCR markdown |
| `E` | `a.extracted_description` | our extraction (`property_description_full`, `pipeline/load_enriched.py:147`) |
| `W` | `a.website_description` ?? `a.description_scraped` | eauctionsindia.com description — **anchor / pre-filter only** |
| `notice_type` | `d.notice_type` | `single` / `multi` |
| `borrowers`, `title` | `a.*` | identify *which* property when `M` is a multi-lot notice |
| `description_source` | `a.description_source` | `notice` / `human` |

> **Do not use `a.description` as `E`.** It is seeded from the *website* text
> (`scripts/load_tn_to_neo4j.py:59-60`) and only becomes the notice description
> after `apply_descriptions.py` runs. `E` is **only** `a.extracted_description`;
> a property with no `extracted_description` is "not yet extracted" → not
> auto-verifiable.

## Algorithm

Two signals: a cheap deterministic **pre-filter** that runs on everything, and
the authoritative **LLM completeness judge** that produces the score.

### 1. `text_overlap` ∈ [0,1] — cheap pre-filter (deterministic)

Contiguous-span similarity between `E` and the eauctionsindia.com text `W`,
measured over **continuous sentences/paragraphs** (not isolated words):

- Normalise both with the existing `_normalize_for_match` / `strip_field_bleed`
  (`api/review/markdown_match.py`).
- Score with `rapidfuzz.fuzz.token_set_ratio` **and** a longest-contiguous-block
  measure (`fuzz.partial_ratio_alignment` span length ÷ len(`W`)), taking the
  higher — so a whole paragraph carried over scores high, and scattered word
  hits do not.

`text_overlap` is **not** the completeness verdict. Its jobs are: (a) sort the
queue, (b) cheaply skip the obvious cases, and (c) be displayed so a reviewer
sees how close `E` is to the website text. A very low `text_overlap` is also a
"maybe wrong property / bad extraction" flag worth surfacing.

### 2. `completeness` ∈ [0,1] — LLM judge (authoritative)

A single model call per property answers the actual question. Reuses the
existing OpenRouter client pattern in `pipeline/ocr_extract.py` /
`pipeline/classify_notice.py` (temperature 0, deterministic), behind a new
`pipeline/judge_description.py`.

**Inputs to the judge:** the notice markdown `M`, the extraction `E`, and — when
`notice_type = 'multi'` — the property's `title`/`borrowers` so the judge knows
*which* lot to evaluate.

> **The website description `W` is deliberately NOT given to the judge.** It is a
> short eauctionsindia.com summary; passing it as a "reference" risks the judge
> grading completeness against that summary and declaring `E` complete when it
> merely matches the summary but missed the notice's fuller schedule — the exact
> failure we are trying to catch. Completeness is judged against `M` only. `W`
> stays a cheap pre-filter / sort key.

**Prompt contract (returns strict JSON):**

```json
{
  "complete":       true,            // is E the full description present in M?
  "completeness":   0.0-1.0,         // graded, not just boolean
  "missing_parts":  ["..."],         // what's in M but absent from E (empty if complete)
  "wrong_property": false,           // does E describe a different lot than M's target?
  "confidence":     0.0-1.0,
  "reasoning":      "one or two lines"
}
```

The judge is told explicitly: the notice is the source of truth; flag anything
in the notice's property description for *this* lot that is missing or truncated
in `E`; do **not** penalise `E` for containing *more* than the website summary;
do not invent a fixed schedule structure — judge against what `M` actually
contains.

`completeness` (the judge's graded score) is stored as
`a.description_completeness` (same field name → `scoring/auction_scorer.py` and
the queue sort keep working). `missing_parts`, `wrong_property`, `confidence`,
and `reasoning` are persisted alongside for the review UI and audit.

**Cost / caching.** One call per property, cached by
`(auction_id, sha1(M)+sha1(E))` so re-runs and unchanged rows are free —
identical to how OCR extraction is cached today. The cheap `text_overlap`
pre-filter can gate the judge if cost matters (e.g. only judge rows that aren't
already obviously good or obviously empty), but judging everything once is the
simplest and is cached thereafter.

## Auto-verify gate

A property is auto-verifiable only when **all** hold:

```
judge.complete == true
judge.completeness >= 0.85
judge.wrong_property == false
judge.confidence   >= 0.80
notice_type        == 'single'      # multi-lot notices carry wrong-lot risk
description_source != 'human'        # never overwrite a human edit
E is non-empty
```

Everything else stays in the human queue, **worst-first** (sort by
`completeness ASC, text_overlap ASC`). Auto-verified rows stamp
`verified_by='auto'` and write the judge's `reasoning` + thresholds into
`description_review_notes` for auditability. Mirrors the existing
`auto_confirm_classifications()` / `auto_confirm_markdown()` pattern in
`api/review/queries.py`.

## Worked example

Notice markdown `M` contains, for this lot: *"All that piece of land in Survey
No. 123, Naranapuram Village, Coimbatore Taluk, measuring 2400 sq.ft., bounded
on the North by Road, South by Plot 12, East by Channel, West by Plot 9,
together with the building thereon."*

- **Complete extraction** `E` = the full sentence above.
  Judge → `{complete: true, completeness: 1.0, missing_parts: [], wrong_property: false, confidence: 0.96}`
  → single notice, source `notice` → **auto-verify ✔**
- **Truncated extraction** `E` = *"…measuring 2400 sq.ft."* (the schedule in `M`
  continues with boundaries + building, which `E` dropped).
  Judge → `{complete: false, completeness: 0.55, missing_parts: ["boundaries", "building"], confidence: 0.9}`
  → **goes to a human**, with the missing parts shown. No boundary rule needed —
  the judge saw the continuation in `M` that `E` omitted.
- **Wrong lot** `E` describes a different survey number than `M`'s target lot.
  Judge → `{wrong_property: true, ...}` → **goes to a human**, regardless of
  completeness.

## Rollout

1. `pipeline/judge_description.py` — the LLM judge: builds the prompt from
   `(M, E, title, borrowers)`, calls OpenRouter (config-driven model, temp 0),
   parses + validates the JSON, caches by content hash. Pure I/O-free core
   function unit-tested with recorded fixtures.
2. Persist judge outputs (`description_completeness`, `description_complete`,
   `description_missing_parts`, `description_wrong_property`,
   `description_judge_confidence`, `description_judge_reasoning`) via
   `pipeline/load_enriched.py`; compute `text_overlap` in
   `api/review/markdown_match.py` (pure, unit-testable).
3. Surface `completeness`, `text_overlap`, `missing_parts`, and the
   wrong-property flag in `get_property()` + `list_queue()` ordering and the
   review UI meta block.
4. Add `auto_confirm_descriptions(thresholds)` query + a "Verify safe ones"
   button, gated as above.
5. **Backfill** existing properties (runs the judge once over the corpus; cached
   thereafter).

## Dry-run first

Before changing any pipeline code, run
`scripts/dryrun_description_completeness.py` against the current graph:

- It computes the cheap `text_overlap` for **every** property (free) and reports
  its distribution — useful as the pre-filter and as a sanity check.
- With `--judge-sample N` it runs the LLM judge on a random sample of `N`
  properties so we can (a) eyeball judge quality against rows you've already
  reviewed by hand, and (b) estimate the auto-clear rate and per-property cost
  before committing to a full backfill.

This validates both the judge and the thresholds on real data before anything is
written back.
