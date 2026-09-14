# Stitch sibling pages of one sale notice — design

Date: 2026-09-14. Branch: `claude/langextraction-single-lot-0a8ce3`.

## Problem

Some banks publish one sale notice as two image files, page 1 and page 2, and
the portal attaches both files to every listing the notice covers. Today each
file is its own `:Document` with its own OCR markdown, its own classification
count and its own LangExtract run. That breaks extraction in three ways:

1. Page 2 has no preamble, so the model does not know it is reading the tail
   of a bigger notice. `liq-2…` was counted at 10 lots by the reviewer and
   extracted as 1.
2. Page 1 is classified against the listing count of the whole notice while
   only holding part of the lots (the `hdfc-1` / `hdfc-2` case from May).
3. A lot that runs across the page break is split in two, or dropped.

Today's data: 14 groups, every one exactly 2 pages, covering 173 listings.
The portal's own file order on the listing (`AuctionProperty.downloads_list`)
puts page 1 first in all 14, matching the `-1` / `-2` markers in the names.

## Decision

**Option A: stitch onto page 1.** Page 1 (the *leader*) gets a second text
field holding page 1 followed by page 2. Extraction runs once on that text and
is stored on the leader only. Page 2 (the *follower*) gets a pointer to the
leader and leaves every extraction-stage queue and pass. Nothing about page 2's
own OCR markdown, blocks or annotate view changes.

Rejected: splitting the stitched result back per page (a straddling lot becomes
two half lots, and lot keys need a shared prefix), and a new `:Notice` node
(every consumer keys on `Document.filename` today; too much blast radius).

## Detection rule

A **page group** is the ordered list of Documents attached to one listing that:

- all hold markdown,
- have pairwise different content (`content_sha256`, else markdown hash — byte
  twins are already handled by `pipeline/notice_twins.py` and must not be
  stitched to themselves),
- are attached to exactly the same set of listings (every listing linked to the
  leader is linked to every follower and vice versa),
- are ordered by their position in the listing's `downloads_list`
  (the portal's order). All listings in the set must agree on that order.

A candidate that fails the same-listing-set or same-order test is printed as
**ambiguous** and skipped. One known ambiguous shape is a notice plus a
corrigendum on one listing (listing 819272: one page classified single, the
other multi). The reviewer decides by name.

## Data model (properties on `:Document`)

Leader only:

| property | type | meaning |
|---|---|---|
| `stitched_markdown` | string | page texts joined in order with `"\n\n"` |
| `stitched_pages` | list of string | filenames in page order, leader first |
| `stitched_page_offsets` | list of int | start offset of each page in `stitched_markdown` |
| `stitched_at` | datetime | when the text above was built |
| `stitched_expected_lot_count` | int or null | sum of the pages' `expected_lot_count`; null unless every page has a count |

Follower only:

| property | type | meaning |
|---|---|---|
| `stitched_into` | string | the leader's filename |

Nothing else on the follower changes. Its `markdown`, blocks, classification
fields and any old `extraction_json` stay as they are, so unstitching is a
property removal, not a restore.

Reads pick the stitched text with `coalesce(d.stitched_markdown, d.markdown)`
and the group count with
`coalesce(d.stitched_expected_lot_count, d.expected_lot_count)`. A Document
without stitch fields behaves exactly as today. That coalesce is the whole
integration contract: no consumer needs to know a stitch happened unless it
reads a follower.

## Pipeline behaviour

**`pipeline/notice_pages.py`** (new, DB-free, unit-tested like `notice_twins`):

- `page_groups(rows) -> (groups, ambiguous)`: rows are
  `{listing, filename, content_key, position}` from one Cypher read; returns
  ordered filename lists plus a list of `(filenames, reason)` for skipped ones.
- `stitch_pages(pages) -> {markdown, offsets, expected_lot_count}`: pages are
  `{filename, markdown, expected_lot_count}` in order.

**`scripts/stitch_sibling_pages.py`** (new):

- `--dry-run` (default): prints each group as `leader <- follower …`, the
  stitched length, the summed lot count, and the ambiguous list with reasons.
  Writes nothing.
- `--apply`: writes the leader and follower properties for every unambiguous
  group. Groups whose leader's stitched text would be unchanged are skipped.
  When the text changes on a leader that already holds an extraction, the
  script stamps `extraction_stale_at` (same contract as
  `verify_classification`), so `reset_langextract_and_extract.py --stale`
  re-runs it. Any prior extraction on a follower is left in place but is no
  longer reachable from the queue.
- `--only <leader>` / `--skip <filename>` narrow or exclude by name, which is
  how an ambiguous group is forced or a wrong one is kept out.
- `--unstitch <leader>`: removes the stitch properties from the leader and
  follower(s) and stamps `extraction_stale_at` on both.
- Re-runnable: a page whose `markdown_loaded_at` or `markdown_reextracted_at`
  is newer than the leader's `stitched_at` rebuilds the stitched text on the
  next `--apply`.

**Extraction (`pipeline/load_extractions.py`,
`scripts/reset_langextract_and_extract.py`):** every Document read that feeds
`LX.extract` adds `AND d.stitched_into IS NULL` and selects
`coalesce(d.stitched_markdown, d.markdown) AS md` and the coalesced lot count.
The twins grouping keys on that `md`, so two byte-identical stitched leaders
still share one call. The write path is unchanged: the result lands on the
leader (and its byte twins), never on a follower.

**Window size (`pipeline/extract_routing.char_buffer_for`):** default ceiling
rises from 30000 to 64000 characters. The largest stitched pair is 39k; at
30k the stitch would be cut back into two windows by LangExtract itself.
`pipeline/lot_windows.py` stays as the repair for anything still over the
ceiling. Existing tests that assume 30000 pass the ceiling explicitly.

**Apply and promote (`pipeline/apply_extractions.py`,
`pipeline/promote_extractions.py`):** skip Documents with `stitched_into`.
Lot keys stay `<leader filename>#<lot_index>`; nothing else changes because
the leader already links every listing in the group.

## Review UI and API

- **Extraction queue** (`api/review/extraction.py` list query): excludes
  followers (`d.stitched_into IS NULL`). The leader row's lot badge compares
  the extracted count to the coalesced group count.
- **Extraction detail** (`get_extraction`): returns
  `coalesce(d.stitched_markdown, d.markdown)` as `markdown`, plus
  `stitched_pages` and `stitched_page_offsets`. The page renders a thin
  "page 2: <filename>" divider at each offset after the first. Re-anchoring in
  `_build_fields` already runs against whatever `markdown` it is handed.
- **Detail for a follower**: returns `stitched_into` and no fields; the page
  shows one line, "This is page N of <leader>. Review it there", with a link.
- **Stale badge**: `extraction_stale()` gains a fourth input,
  `extraction_stale_at`, and is true when that is newer than `extraction_at`.
  The queue and detail queries return it. This is the gap that let today's
  notice look fresh after its classification changed.
- **Classification queue**: unchanged. Reviewers keep counting lots per page;
  the stitch script sums the counts. A follower's card gets a small
  "page N of <leader>" note so nobody re-classifies it as its own notice.

## Failure handling

- Missing markdown on any page: the group is ambiguous ("page has no
  markdown") and skipped, never half-stitched.
- Lot counts missing on one page: stitched text is still written;
  `stitched_expected_lot_count` stays null, and the prompt gets the leader's
  own count via the coalesce, as today.
- A follower gets a new listing later that the leader lacks: the same-listing
  test fails on the next dry run and the group is reported, not silently kept.
- Unstitch is always available and leaves both pages exactly as pre-stitch
  Documents plus a stale stamp.

## Backfill order

1. `stitch_sibling_pages.py --dry-run`, review the 14 groups and the ambiguous
   list.
2. `--apply` (writes 14 leaders and 14 followers, stamps stale on leaders).
3. `reset_langextract_and_extract.py --stale` (also picks up the 8 rows already
   flagged stale by classification changes).
4. Apply and promote as usual; re-verify the leaders in the extraction queue.

Each DB-writing step waits for explicit approval, per the project rule.

## Testing

- `tests/pipeline/test_notice_pages.py`: ordering by position, same-listing-set
  rejection, order-disagreement rejection, byte twins not grouped, offsets and
  count sum, null count when one page lacks it.
- `tests/scripts/test_stitch_sibling_pages.py`: dry run writes nothing;
  unchanged text skipped; changed text stamps stale; unstitch clears fields.
- `tests/pipeline/test_load_extractions_twins.py` (extend): fetch Cypher
  excludes followers and coalesces the stitched text and count.
- `tests/api/test_extraction_stale_on_classify.py` (extend): `extraction_stale`
  true on a newer `extraction_stale_at`; queue query returns it.
- `tests/api`: queue excludes followers; follower detail returns the pointer;
  leader detail returns offsets.
- `tests/pipeline/test_extract_routing.py`: new default ceiling.

## Out of scope

- Groups with three or more pages are handled by the same code but none exist
  today, so no backfill case covers them.
- Detecting pages at scrape time. The script runs after OCR; a scraper hook
  can call the same `page_groups` later.
- The `hdfc-1` / `hdfc-2` stale-edge cleanup from May is a separate data fix
  and is not changed by this design.
