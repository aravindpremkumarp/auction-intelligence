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
> description lives there. The **website description** (scraped from
> **eauctionsindia.com**) is the *reference* we check against. For each property
> we compare the notice description to the eauctionsindia.com description on
> **similar words** and **length** to judge whether we captured the notice
> description **completely, start to finish**, and whether it is the **right**
> property.

So completeness combines three signals:
1. **Word similarity** — how much of the eauctionsindia.com description's wording
   also appears in the notice extraction ("did we miss content the website had").
2. **Length adequacy** — is the notice description long enough relative to the
   eauctionsindia.com one ("is it suspiciously shorter → probably partial").
3. **End-of-schedule guard** — did we reach the legal tail (boundaries), which a
   short website reference can't confirm on its own.

The **"right property"** question is a separate **anchor** signal, kept as its
own number — never blended into completeness.

## Inputs (all already in Neo4j — no new extraction)

| Symbol | Source field | Meaning |
|---|---|---|
| `W` | `a.website_description` ?? `a.description_scraped` | **eauctionsindia.com** reference description (scraped) |
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

All three components are computed, **displayed** alongside the property, and
**blended** into one `completeness` number (so the queue can sort/auto-approve
on a single value while a reviewer can still see *why* it scored that way).

### 1. `word_similarity` ∈ [0,1] — "similar words"

Split `W` into sentences (drop fragments shorter than ~25 chars). For each
sentence `s`, it counts as **covered** when
`rapidfuzz.fuzz.partial_ratio(normalize(s), normalize(E)) ≥ 85`.

```
word_similarity = covered_sentences / total_sentences   (1.0 if W is empty/trivial)
```

This measures how much of the eauctionsindia.com wording also shows up in the
notice extraction. It's **recall** (W's words found in E), not a symmetric
match: the notice `E` is *expected* to contain at least as much as the website
`W`, so extra content in `E` must not be penalised here — that's what
`length_adequacy` is for. Reuses the normalisation already in
`api/review/markdown_match.py` (`_normalize_for_match`, `strip_field_bleed`).

### 2. `length_adequacy` ∈ [0,1] — "more or less length"

```
length_ratio    = len(E) / max(len(W), 1)              # notice vs eauctionsindia.com
length_adequacy = min(length_ratio / 0.9, 1.0)         # full credit once E ≥ 0.9·W
```

A notice description much **shorter** than the website one is a strong "partial
extraction" signal → `length_adequacy` drops. Once the notice is about as long
as (or longer than) the website text, it gets full credit — being *longer* is
expected and good, so it is capped at 1.0, never rewarded for sheer length (the
bug in the old score).

### 3. `end_reached` ∈ {0, 0.6, 1.0} — schedule-tail guard

The eauctionsindia.com reference is often a short summary that omits the boundary
schedule, so words+length can look fine while `E` is still truncated before the
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

### 4. `completeness` ∈ [0,1] — blended

```
completeness = round(0.50 * word_similarity
                   + 0.20 * length_adequacy
                   + 0.30 * end_reached, 2)
```

Word similarity leads (it most directly answers "did we capture the
description"), length is a lighter corroborating signal, and the end-of-schedule
guard is weighted enough to pull a truncated notice below the auto-verify line on
its own. Stored back as `a.description_completeness` (same field name → nothing
downstream breaks; `scoring/auction_scorer.py` and the queue sort keep working).

### 5. `anchor_score` ∈ [0,100] — separate "correct property" signal

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

- `W` (eauctionsindia.com) = *"Residential land 2400 sq ft in Naranapuram,
  Coimbatore."*
  → its wording is found in `E` → `word_similarity = 1.0`
- `len(E)/len(W)` ≈ 2.3 → `length_adequacy = 1.0` (notice is richer, capped)
- 4 boundaries present → `end_reached = 1.0`
- `completeness = 0.50*1.0 + 0.20*1.0 + 0.30*1.0 = 1.0`
- `description_coverage(W, M)` ≈ 92 → `anchor_score = 92`
- single notice, source `notice` → **auto-verify ✔**

Truncated variant — `E` stops at *"…measuring 2400 sq.ft."* (boundaries
dropped): `word_similarity` still ~1.0 and `length_adequacy` ~1.0 (the short `W`
had no boundaries), but `end_reached = 0.0` → `completeness = 0.70` → **below
0.85, goes to a human.** This is the truncation case words+length alone miss.

## Rollout

1. Add `score_description_completeness(W, E, boundaries, doors) -> dict` to
   `api/review/markdown_match.py` (returns `word_similarity`, `length_adequacy`,
   `end_reached`, `completeness`, `anchor_score`). Pure function, unit-testable.
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
