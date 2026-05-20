# Review queue: stages, uniform statuses, range filters

Date: 2026-05-20
Status: Approved — ready for implementation plan

## Goal

Restructure the `/review` queue UI so that reviewers can move through the pipeline
the way the data actually flows — **classification → markdown → description** — with
a uniform 3-state review model and range-based filters instead of single-value
sliders. The current toolbar mixes four sibling "groups" (property, sales notice,
classification, markdown), each with its own status vocabulary; this makes it hard
to ask "what's left in stage X for single-property notices today?".

The work supports the team's in-flight goal: verifying that property descriptions
extracted from sales notices match the descriptions scraped from auction websites.
That comparison happens in the *description* stage; *classification* and *markdown*
are the upstream gates whose outputs feed it.

## Non-goals

- No Neo4j schema changes. All status mappings happen in Cypher WHERE clauses.
- No redesign of the detail screen (`#screen-detail`) or annotator (`#screen-annotator`).
  Only the queue toolbar (`#screen-queue`) and its server-side filters change.
- No new score field for the description stage. Completeness exists in data today,
  but adding a description-score from/to filter is out of scope for v1.

## Top-of-screen layout

Three stacked toolbar rows replace today's flat row of "Group by" pills.

```
┌────────────────────────────────────────────────────────────────────────┐
│ Stage:   [classification]  [markdown]  [description]                   │
├────────────────────────────────────────────────────────────────────────┤
│ Group: [property] [sales notice]                                       │
│ Status: [pending] [verified] [edited] [all]                            │
├────────────────────────────────────────────────────────────────────────┤
│ Auction date:  from [____] to [____]  [clear]                          │
│ Score:         from [0]    to [100]   [clear]   (classif. + markdown)  │
│ Notice type:   [all] [single] [multi] [unclassified]                   │
│ Search: [_________________]                        [Reload]            │
├────────────────────────────────────────────────────────────────────────┤
│ Stats pills: ⓟ pending  ✓ verified  ✎ edited  ▢ total                  │
├────────────────────────────────────────────────────────────────────────┤
│ Bulk-confirm: [ Confirm all N in range ]   (classif. + markdown only)  │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   < queue table / cards / gallery for the chosen stage+group >         │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

State persists in URL hash (`#stage=markdown&group=notice&status=pending&...`) and
`localStorage` (`reviewQueueState` key, JSON-encoded snapshot of `queueState`).
Default landing on a fresh session: `stage=description, group=property,
status=pending, date_from=today, score_from=0, score_to=100, notice_type=all`.

Each stage remembers its last-used sub-group across sessions (e.g., a reviewer
who prefers the notice gallery in classification but the property table in
description gets both back automatically). Per-stage sub-group defaults on a
brand-new session: description → `property`; classification → `sales notice`;
markdown → `sales notice`.

## Uniform 3-state status model

Every stage exposes the same status vocabulary: **pending / verified / edited / all**.
Each stage maps the standard names to its underlying Neo4j flags.

| Stage          | pending                                | verified                                                         | edited                                                          |
|----------------|----------------------------------------|------------------------------------------------------------------|-----------------------------------------------------------------|
| description    | `description_verified = false`         | `description_verified = true` AND `description_source = 'notice'` | `description_verified = true` AND `description_source = 'human'` |
| classification | classification not verified            | classification verified AND `notice_type_overridden = false`     | classification verified AND `notice_type_overridden = true`      |
| markdown       | `markdown_quality IS NULL`             | `markdown_quality = 'good'`                                      | `markdown_quality = 'bad'`                                       |

Semantic across stages: *pending* = needs review · *verified* = human accepted
as-is · *edited* = human changed it.

### What goes away

- Classification status pills `disagreement` and `auto-confirm` (they were filters
  dressed as statuses).
- Markdown status pills `good`, `bad`, `unscored` (rolled into the 3-state model;
  `bad` now reads as *edited*, `unscored` is one shape of *pending*).
- The two `<input type="range">` sliders for classifier confidence and markdown
  score.

The `auto_confirmable` count and disagreement-aware workflow don't disappear from
the system — they're now expressed via the score range filter plus the bulk-confirm
button (see below).

## Stage × group views

| Stage          | property sub-group                                                                                                                                                                                                                                                | sales notice sub-group                                                                  |
|----------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| description    | **Existing** `#queue-by-property` table.                                                                                                                                                                                                                          | **Existing** `#queue-by-notice` card list.                                               |
| classification | **New** property list — one row per `Property`. Columns: title · borrower · auction date · its notice's classification (single / multi · confidence · overridden flag) · status pill. Clicking a row deep-links to the notice's classification gallery card. | **Existing** classification gallery + cards (with `gallery ⇄ cards` view toggle).        |
| markdown       | **New** property list — one row per `Property`. Columns: title · borrower · auction date · its notice's markdown score · quality pill · status pill. Clicking a row opens the notice's markdown side-by-side review.                                          | **Existing** markdown side-by-side notice list.                                          |

The two new property-list views are thin wrappers over existing queries — both join
`Property → Document` and project the same property columns plus stage-specific
status. They share the property list's pagination + reload behavior.

## Score range filter (replaces sliders)

Two number inputs mirroring the auction-date filter.

| Stage          | Field                          | Bounds      | UI label                |
|----------------|--------------------------------|-------------|-------------------------|
| classification | `notice_type_confidence × 100` | 0–100       | "Score" (shown as %)    |
| markdown       | `markdown_quality_score`       | 0–100       | "Score"                 |
| description    | —                              | (hidden)    | —                       |

UI inputs are always 0–100 integers. The classification API takes
`confidence_min` / `confidence_max` as 0–1 floats, so the frontend divides by
100 before sending. The markdown API stays in 0–100 floats — no conversion.

- Both inputs default to `from=0, to=100` (everything visible).
- Empty input on either side = unbounded on that side.
- Clearing both = no score filter.

### Bulk-confirm

Visible only in classification and markdown stages. Acts on whatever falls inside
the visible filtered range AND is currently `pending`. Button label tracks state:
`Confirm all 23 in [90, 100]`. Clicking it calls the stage's bulk-confirm endpoint
with both `min` and `max` parameters.

## Notice-type filter

Top-level pill group, visible across all three stages.

```
Notice type:  [all] [single] [multi] [unclassified]
```

- `single` → `Document.notice_type = 'single'`
- `multi` → `Document.notice_type = 'multi'`
- `unclassified` → `Document.notice_type IS NULL`
- `all` → no filter

Applies to every queue + stats endpoint. Stats pill counts respect the active
notice-type scope.

## API surface

### Modified endpoints

- `GET /review/classification/queue`
  - status enum → `pending | verified | edited | all` (drop `disagreement`, `auto-confirm`)
  - add `confidence_max: float | None` (0–1, like `confidence_min`)
  - add `notice_type: 'single' | 'multi' | 'unclassified' | 'all'`
- `GET /review/markdown/queue`
  - status enum → `pending | verified | edited | all` (drop `good`, `bad`, `unscored`)
  - add `score_max: float | None` (0–100)
  - add `notice_type`
- `POST /review/classification/bulk-confirm`
  - add `confidence_max: float = 1.0`
- `POST /review/markdown/bulk-confirm`
  - add `score_max: float = 100.0`
- `GET /review/stats`, `GET /review/notice/stats`, `GET /review/classification/stats`,
  `GET /review/markdown/stats`
  - all return `{ pending, verified, edited, total }` (markdown stats lose
    `good`, `bad`, `unscored`, `auto_confirmable` — the last is replaced by a
    live in-range count derived from the filtered queue)
  - all accept `notice_type`
- Every existing queue endpoint accepts `notice_type` as a filter.

### New endpoints

- `GET /review/classification/queue/by-property`
  Returns a paginated list of `Property` rows joined to their `Document`, projecting
  property identity columns plus classification status columns. Accepts the standard
  filters (`status`, `date_from`, `date_to`, `confidence_min`, `confidence_max`,
  `notice_type`, `q`, `page`, `size`).
- `GET /review/markdown/queue/by-property`
  Same shape, projecting markdown status columns. Accepts the standard filters
  with `score_min` / `score_max` instead of confidence.

## URL routing

Single `#stage=…&group=…&status=…&notice_type=…&date_from=…&date_to=…&score_from=…&score_to=…&q=…&page=…`
hash format. Back-compat:
- Old `#queue` → defaults
- Old `#detail/<id>` → unchanged (detail screen)
- Old `#notice/<filename>` → unchanged (annotator screen)
- Old `#gallery/<filename>` → equivalent to `#stage=classification&group=notice&...`
  with a focused gallery card

## Files touched

- **Frontend** — [web/review.html](../../../web/review.html)
  - Replace the flat `data-group` pill row with two rows: stage tabs + sub-group pills.
  - `queueState` gets `stage`, `subgroup`, `noticeType`, `scoreFrom`, `scoreTo`;
    drop the slider-bound `autoConfMin` / `mdScoreThreshold`.
  - Collapse `status-buttons-desc` / `status-buttons-class` / `status-buttons-md`
    into one shared `data-status` pill row.
  - Remove the two `<input type="range">` sliders and their handlers; add four
    `<input type="number" min=0 max=100>` cells for score from/to (one pair per
    score-enabled stage, sharing the same DOM but hidden when stage doesn't expose a score).
  - Two new render functions for the property-list views inside classification +
    markdown stages.
  - Bulk-confirm button text + click handler updated to send `min` + `max`.
- **Backend** — [api/review/router.py](../../../api/review/router.py),
  [api/review/queries.py](../../../api/review/queries.py)
  - Schema model updates for new query params + uniform stats shape.
  - Cypher WHERE clauses for new status mappings, notice-type filter, score-range filter.
  - Two new endpoints + their `queries.py` helpers for the by-property views.
- **Tests** — [tests/api/test_review_classification.py](../../../tests/api/test_review_classification.py)
  extended; new analogous file for markdown if one doesn't exist; both cover the
  uniform status enum and `_max` params.

## Open items deliberately deferred

- Description-stage score filter (would map to `completeness`). Add later if reviewers
  ask for it; not required to support the in-flight verification work.
- A separate "edited via re-extracted blocks" status bucket inside markdown. Today
  re-extraction clears `markdown_quality` back to NULL, which surfaces as *pending* —
  arguably correct (the new markdown deserves a fresh look). Revisit only if we get
  feedback the loss of visibility hurts.
