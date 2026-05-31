# Inline Table Editor — Design Spec

**Date:** 2026-06-01
**File touched:** `web/review.html` (single-file vanilla-JS frontend)
**Status:** Approved design, ready for implementation plan

## Problem

The review UI for auction-notice extraction has **two** table-editing surfaces that
disagree, and the powerful one is broken:

1. **Full-screen "Table Editor" modal** (`tbled-overlay`, opened via the "✎ Edit
   table" button). Renders tables correctly (honors `rowspan`/`colspan` via a
   physical-grid builder) and has the full toolbar (merge/split, B/I/U/sup/sub,
   align, add/del row+col, undo/redo, TeX). **But you cannot edit text in it:**
   - Editing only starts on **double-click**, which is undiscoverable.
   - Every single click calls `paintTbled()` → `paintTbledGrid()` →
     `wrap.innerHTML = ''`, rebuilding the whole grid. The element you are about
     to double-click is destroyed mid-gesture, so the double-click is eaten and
     editing effectively never starts.
   - It shows no view of the original scan, so the reviewer edits blind (e.g.
     OCR produced "Koi" but the notice says "Koil").

2. **Annotation-block inline preview** (`renderAnnTablePreview`). Renders a
   **flat grid that ignores `rowspan`/`colspan`**, so merged headers
   ("Location of property", "Boundaries") collapse into single misaligned
   cells. The reviewer sees a structurally different table than the modal shows.

User feedback, verbatim:
> 1. I am not able to edit text here and it would be better to see table from
>    sales notice so i know what to change
> 2. in annotation block, I dont see table in same way i saw in table editor

## Decision (from brainstorming)

- **Eliminate the full-screen modal.** Move all of its capability into the
  annotation page itself.
- The annotation page is already a two-column layout: **source scan on the left**
  (auto-scrolls to the selected block) and a **Blocks sidebar on the right**.
  That left scan is the "see the table from the sales notice" reference — **no
  separate cropped image** is added.
- When a Table block is selected, its sidebar row becomes a **single,
  span-correct, single-click-editable** table editor with the full toolbar.
- **Single-click to edit** (not double-click).

## Goal

One table editor. What you see in the sidebar matches the real merged structure
everywhere, and you fix OCR text directly inline while glancing at the scan on
the left. The fragile, modal-only, double-click flow is gone.

## Approach

**Reuse the existing span-aware table model.** The modal's logic is correct —
only its *home* (a full-screen overlay) is wrong. The following functions are
kept and retargeted to render inline instead of into the modal:

- `tbledParseHtml(html)` → 2D logical cell model `{ html, tag, rowspan, colspan, align }`
- `tbledSerialize(rows)` → `<table>` HTML
- `tbledBuildGrid()` → physical grid honoring spans (the parity fix)
- `tbledMerge` / `tbledCanMerge`, `tbledSplit` / `tbledCanSplit`
- `tbledAddRow`, `tbledAddCol`, `tbledDelRow`, `tbledDelCol`
- `tbledApplyFormat` (B/I/U/sup/sub), `tbledApplyAlign`
- `tbledToLatex`, undo/redo (`tbledUndo`/`tbledRedo`/`tbledCommit` + history stacks)

The **flat** path (`renderAnnTablePreview`, `_annParseTableHtml`,
`_annSerializeTableHtml`) is **removed** and replaced by the span-aware model.

## Components

### A. Per-block table state
A state object bound to the currently-selected Table block, mirroring today's
`tbled` object: `{ block, rows, selection, past, future }`. On select →
`rows = tbledParseHtml(block.text)`. On commit → serialize → persist.

### B. Read-only render for unselected Table rows
Every Table block in the sidebar renders a **compact, read-only, span-correct**
table using the same physical-grid renderer (no toolbar, no contenteditable).
This makes the list always show correct merged structure (fixes complaint #2
even before selection).

### C. Interactive editor for the selected Table row
The selected Table block's row additionally gets:
- A **compact toolbar**: undo/redo · align L/C/R · B/I/U/sup/sub · merge/split ·
  +row↑ +row↓ +col← +col→ · del row · del col · TeX preview toggle. (Same actions
  as the old modal toolbar — `paintTbledToolbar`.)
- **Editing on** via the interaction model below.
- Rendered into a **stable container element** inside the `ann-row` so routine
  sidebar activity does not clobber it (see "Repaint fix").

### Interaction model
- **Single-click a cell → edit immediately**: cell becomes `contenteditable`,
  focused, caret placed; type right away. (Replaces double-click.)
- **Shift-click another cell → rectangular range selection** (for merge / format
  / align). **Row/column header handles** select a whole row/column (ported from
  `paintTbledGrid`'s handle cells).
- Inline **B/I/U/sup/sub** while a cell is focused use `document.execCommand`
  on the caret's text selection (existing `tbledApplyFormat` branch); applied to
  a multi-cell range they wrap each selected cell's full content.
- **Enter** (no shift) or **click-away** commits the cell · **Esc** cancels ·
  **Ctrl/Cmd+Z** undo · **Ctrl/Cmd+Y** (or **Ctrl+Shift+Z**) redo.

## The repaint fix (core robustness — root cause of "can't edit")

Today `updateBlockOnServer` calls `paintAnnSidebar()` (`#ann-rows`
`innerHTML = ''` + full rebuild) on **every** successful save, and the modal's
`paintTbled()` rebuilds on **every** click. Either wipes the editor and the
caret. Required changes:

1. **Selection / edit changes mutate cells in place** — toggle a `.selected`
   class and set/clear `contenteditable` on existing cells. **No full
   re-render on click.**
2. **Structural ops** (merge/split/add/del row+col, undo/redo) re-render **only
   the editor's own table subtree** (its container), never the whole sidebar.
3. **Cell/structural commits persist via a silent save path.** Add a
   `{ silent: true }` option (4th arg or options object) to
   `updateBlockOnServer` that **skips `paintAnnSidebar()` and
   `paintAnnOverlay()`** — text edits don't move the bbox, so the overlay
   doesn't need repainting and the sidebar editor must not be torn down.
   `refreshMarkdownAfterChange()` still runs (debounced, separate DOM —
   `#ann-md-body`), and `ann.rev` / `mergeBlock` bookkeeping still happens.
4. **Don't re-mount the editor for the block being actively edited.** When
   `paintAnnSidebar()` *does* run (e.g. selecting a different block, reorder,
   label change), the editor mounts fresh from `block.text`; while a Table block
   is the selected/edited one, avoid rebuilding its editor subtree out from
   under an open caret.

## Removals / cleanup

- **Delete the full-screen modal**: `tbled-overlay` HTML in `tbledMountOnce`,
  the functions `tbledMountOnce` / `tbledOpen` / `tbledClose` / `tbledSave` /
  `tbledEnsureMounted`, the overlay-scoped `keydown` handler, and the
  `.tbled-overlay` (and modal-only `.tbled-*`) CSS block.
- **Remove the "✎ Edit table" button** from `renderSidebarRow` and its handler.
- **Remove the flat path**: `renderAnnTablePreview`, `_annParseTableHtml`,
  `_annSerializeTableHtml` (replaced by the span-aware model + new inline
  renderer).
- **Keep** (orthogonal to cell editing — they drive *re-extraction*, not text):
  - the "▸ edit raw HTML" `<textarea>` escape hatch,
  - the "Table grid" row/col **position** inputs (`data-grid="rows"/"cols"`),
  - the **"Table Hints"** panel (`renderTableHintsPanel`) and its +/- count steppers,
  - the **"↻ re-run extraction"** button and `reextractBlock`.

## Edge cases

- Empty / malformed table HTML → model falls back to a 1×1 cell (existing
  `tbledParseHtml` behavior).
- Cell HTML is **DOMPurify-sanitized on render** (existing behavior) — preserved
  to keep stored `<script>` from executing.
- **Switching selected block mid-edit**: commit the focused cell first, then
  mount the new block's editor.
- **409 conflict** on save → existing reload path (`loadAnnotator`) — rare,
  acceptable.
- **Very wide tables** (10-col Schedule tables): the editor grid scrolls
  horizontally inside the sidebar; the sidebar is resizable via the existing
  `#ann-resizer`.
- A block whose label is switched **away** from "Table" → its `table` field is
  cleared (existing `labelSel` handler) and the editor is not shown.

## Out of scope

- No server/API changes (`api/review/*` untouched; same PUT `text` field).
- No cropped-image pane (explicitly declined — left scan is the reference).
- No new JS test harness (see Verification).

## Verification

`web/review.html` has no JS unit harness, so verification is manual in the
browser (live review UI / `/run`):

1. Select a Table block → sidebar shows the **correct merged structure** (matches
   what the old modal showed).
2. Single-click the "Koi" cell → caret appears → type "Koil" → click away →
   value persists, **no flicker / no lost focus**.
3. Merge, split, add row/col, delete row/col, undo, redo all behave and persist.
4. B/I/U/sup/sub and align apply to the focused cell and to multi-cell ranges.
5. Reload the notice → all edits are stuck (round-tripped through the server).
6. The full-screen modal no longer exists; "✎ Edit table" button is gone; "edit
   raw HTML", "Table grid", "Table Hints", and "re-run extraction" still work.

## Known limitations (accepted; not data loss)

Surfaced during implementation review and consciously deferred as minor — none
lose committed data; the core flow (select → single-click → type → cell /
structural / undo all persist silently) is unaffected.

- **Undo history resets when the editor is remounted.** Any *non-silent* save
  while a Table block is selected — dragging its bbox/guides on the canvas,
  reordering blocks, changing its label, editing the raw-HTML textarea, or a
  *different* block's debounced text save — calls `paintAnnSidebar()`, which
  re-mounts the editor and clears its undo/redo stacks. Cell text is already
  persisted, so only undo history is lost. (Cell edits and toolbar structural
  ops persist *silently* and do NOT remount, so undo works throughout a normal
  editing session.)
- **Cross-block caret race (narrow):** typing in block A's raw-HTML textarea
  and then, within the 700 ms debounce, clicking into the selected Table's cell
  lets A's non-silent save fire and remount the editor — interrupting the caret.
  Requires editing two blocks within 700 ms; rare in practice.
- **`tbledToLatex` derives `ncols` from `tbledBuildGrid(tbled.rows)`** while
  iterating its own `rows` param; correct only because its sole caller passes
  `tbled.rows`. Harmless today; tidy if it ever gets another caller.
