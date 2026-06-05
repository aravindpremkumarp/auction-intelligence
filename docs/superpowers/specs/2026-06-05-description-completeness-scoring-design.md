# Description completeness scoring — design

**Date:** 2026-06-05
**Stage:** Review → Description
**Status:** Spec (not yet implemented)

## Problem

In the description review stage a human must confirm, per property, that the
description we extracted from the **sales notice** is captured **completely —
from start to finish** — and belongs to the **correct property**. We want a
`completeness` score good enough to *auto-clear* the easy cases so humans only
review the risky ones.

The score that exists today is a character-length ratio
(`pipeline/ocr_extract.py:164-167`):

```python
img_desc     = extracted.get("property_description_full") or ""
web_len      = len(record.get("description") or "")
completeness = min(web_len / max(img_len, 1), 1.0) if img_len > 0 else 1.0
```

It is unsafe as an auto-pass gate:

1. **Rewards length, not content.** A long but wrong/garbled block scores high.
2. **Inverts in the common case.** When the notice is richer than the website
   (the normal, *good* case) the ratio caps at 1.0, so good and mediocre
   extractions are indistinguishable.
3. **Empty → 1.0.** When there is no extracted text it defaults to *complete*
   (`else 1.0`) — backwards; that is the least complete case.

## Definition (corrected)

> The **sales notice** is the source of truth — the full legal property
> description lives there. The **website description** is the *reference* we
> check against to confirm we extracted that description **completely, start to
> finish**, and that it is the **right** property.

So completeness is a **recall** problem ("did the whole reference survive into
our extraction, including its tail?") plus an **end-of-schedule guard** ("did we
reach the finish even when the reference is shorter than the notice?"). The
"right property" question is a separate **anchor** signal, kept as its own
number — never blended into completeness.

## Inputs (all already in Neo4j — no new extraction)

| Symbol | Source field | Meaning |
|---|---|---|
| `W` | `a.website_description` ?? `a.description_scraped` | website **reference** description |
| `E` | `a.extracted_description` | description **extracted from the notice markdown** (`property_description_full`, `pipeline/load_enriched.py:147`) |

> **Do not use `a.description` as `E`.** It is seeded from the *website* text
> (`scripts/load_tn_to_neo4j.py:59-60`) and only becomes the notice description
> after `apply_descriptions.py` runs. Using it before that step would compare
> website-vs-website and inflate `reference_recall` to ~1.0. `E` is **only**
> `a.extracted_description`; a property with no `extracted_description` is
> "not yet extracted" → not auto-verifiable.
| `M` | `d.markdown` (linked `Document`) | full notice OCR markdown (haystack) |
| boundaries | `a.boundary_{north,south,east,west}` | tail of the legal schedule |
| doors | `a.door_numbers_{old,new}` | apartment-type tail (schedules without boundaries) |
| `notice_type` | `d.notice_type` | `single` / `multi` |
| `description_source` | `a.description_source` | `notice` / `human` |

## Algorithm

### 1. `reference_recall` ∈ [0,1] — primary, "start to finish" substance

Split `W` into sentences (drop fragments shorter than ~25 chars). For each
sentence `s`, it counts as **covered** when
`rapidfuzz.fuzz.partial_ratio(normalize(s), normalize(E)) ≥ 85`.

```
reference_recall = covered_sentences / total_sentences   (1.0 if W is empty/trivial)
```

Why **recall**, not the symmetric length ratio: the notice (`E`) is *expected*
to be a superset of the website (`W`), so extra content in `E` must not be
penalised. A truncated `E` drops the **last** sentences of `W` → those
sentences fail the match → recall falls. That is precisely the "we got cut off
before the finish" failure we care about. Reuses the normalisation already in
`api/review/markdown_match.py` (`_normalize_for_match`, `strip_field_bleed`).

### 2. `end_reached` ∈ {0, 0.6, 1.0} — schedule-tail guard

The website reference is often a short summary that omits the boundary schedule,
so `reference_recall` can be 1.0 while `E` is still truncated before the
boundaries. The boundary block is the canonical **end** of a property schedule,
and we already extract it, so its presence is strong evidence we reached the
finish:

```
n = count of non-null among {boundary_north, south, east, west}
end_reached = 1.0 if n == 4
              0.6 if n in (2, 3)
              0.0 otherwise
# Apartment/flat schedules carry door_numbers instead of boundaries:
# if door_numbers_{old|new} present and n < 2, end_reached = max(end_reached, 0.6)
```

### 3. `completeness` ∈ [0,1]

```
completeness = round(0.65 * reference_recall + 0.35 * end_reached, 2)
```

Stored back as `a.description_completeness` (same field name → nothing
downstream breaks; `scoring/auction_scorer.py` and the queue sort keep working).

### 4. `anchor_score` ∈ [0,100] — separate "correct property" signal

```
anchor_score, span = description_coverage(W, M)   # api/review/markdown_match.py
```

High `anchor_score` = the reference genuinely appears in *this* notice → right
property / right block. Low = the extraction may be from the wrong lot, or the
website text simply does not match this notice → must go to a human. Persist as
a new field `a.description_anchor_score`. **Not** folded into `completeness`.

## Auto-verify gate

A property is auto-verifiable only when **all** hold:

```
completeness   >= 0.85
anchor_score   >= 80
notice_type    == 'single'        # multi-lot notices carry wrong-lot risk
description_source != 'human'      # never overwrite a human edit
E is non-empty
```

Everything else stays in the human queue, **worst-first** (sort by
`completeness ASC, anchor_score ASC`). This mirrors the existing
`auto_confirm_classifications()` / `auto_confirm_markdown()` pattern in
`api/review/queries.py`.

## Worked example

Notice schedule (`E`, extracted): *"All that piece of land in Survey No. 123,
Naranapuram Village, Coimbatore Taluk, measuring 2400 sq.ft., bounded on the
North by Road, South by Plot 12, East by Channel, West by Plot 9."*

- `W` = *"Residential land 2400 sq ft in Naranapuram, Coimbatore."*
  → both sentences of `W` found in `E` → `reference_recall = 1.0`
- 4 boundaries present → `end_reached = 1.0`
- `completeness = 0.65*1.0 + 0.35*1.0 = 1.0`
- `description_coverage(W, M)` ≈ 92 → `anchor_score = 92`
- single notice, source `notice` → **auto-verify ✔**

Truncated variant — `E` stops at *"…measuring 2400 sq.ft."* (boundaries
dropped): `reference_recall` still ~1.0 (the short `W` had no boundaries), but
`end_reached = 0.0` → `completeness = 0.65` → **below 0.85, goes to a human.**
This is the case the length ratio misses today.

## Rollout

1. Add `score_description_completeness(W, E, boundaries, doors) -> dict` to
   `api/review/markdown_match.py` (returns `reference_recall`, `end_reached`,
   `completeness`, `anchor_score`). Pure function, unit-testable.
2. Wire it into `pipeline/ocr_extract.py::cross_reference()` (replace the length
   ratio) and persist `description_anchor_score` via `pipeline/load_enriched.py`.
3. Surface `completeness` + `anchor_score` in `get_property()` and `list_queue()`
   ordering; show both in the review UI meta block.
4. Add `auto_confirm_descriptions(thresholds)` query + a "Verify safe ones"
   button, gated as above; stamp `verified_by='auto'` and the thresholds into
   `description_review_notes` for auditability.
5. **Backfill** existing properties so old rows are rescored.

## Dry-run first

Before changing any pipeline code, run
`scripts/dryrun_description_completeness.py` against the current graph. It
computes the proposed `completeness` / `anchor_score` for every property and
reports, for a sweep of thresholds, how many would auto-clear and the
score distribution — so the thresholds above can be tuned on real data.
