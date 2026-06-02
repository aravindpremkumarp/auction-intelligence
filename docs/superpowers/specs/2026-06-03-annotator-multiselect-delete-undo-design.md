# Annotator: Multi-Select, Delete Key & Undo/Redo — Design Spec

**Date:** 2026-06-03
**Files touched:** `web/review.html` (annotator JS + toolbar), `api/review/blocks.py`
(new `replace_blocks`), `api/review/router.py` (new `PUT /blocks` route + body model)
**Status:** Approved design, ready for implementation plan

## Problem

The per-block annotator screen (`#screen-annotator` in `web/review.html`) lets a
reviewer select, move, resize, relabel, re-extract, and delete the OCR layout
blocks overlaid on a sales-notice scan. Three interaction gaps make bulk cleanup
slow and risky. User feedback, verbatim:

> 1. when i select a block i am not able to delete them using delete button from
>    keyboard
> 2. i am not able to select multiple blocks so i should be able select multiple
>    block using right click and drag across them and when i click block and
>    press ctrl and select one more block
> 3. i must be able to undo or redo button

Today:

- **No keyboard delete.** Deletion is only the per-row `delete` button, which pops
  a `confirm('Delete this block?')` for every block.
- **Single selection only.** State is `ann.selectedId` (one id). The marquee
  (rubber-band) is wired *only* for `draw`/`crop` mode (it creates a block / crop),
  not for selecting in `select` mode.
- **No undo.** Every edit is a one-way server round-trip. A mis-drag or accidental
  delete cannot be reverted.

## Decisions (from brainstorming)

1. **Marquee trigger:** left-drag on **empty canvas** (Figma/Miro convention).
   Empty-canvas left-drag is currently unused (panning is middle-click / alt+drag),
   so there is no conflict. Left-drag on a block still moves it. **Ctrl/Cmd+click**
   toggles an individual block in/out of the selection.
2. **Delete:** **no confirmation** — undo is the safety net. A toast reports
   "deleted N — Ctrl+Z to undo". The existing row `delete` button also drops its
   `confirm()` for consistency.
3. **Undo/redo scope:** **all block changes** (add, delete, multi-delete, move,
   resize, reorder, label change, settled text edits). Document-level **crop** and
   **rotation** are **excluded** — they have their own controls and re-run OCR.
4. **Undo architecture:** **client snapshot stack + one new atomic
   `PUT /blocks` "replace" endpoint** (Approach A below). Session-local; reset on
   notice load/reload.

## Approaches considered (undo/redo)

- **A — Client snapshot stack + atomic replace endpoint (CHOSEN).** The annotator
  already holds the whole block list in `ann.blocks`, and the server stores it as
  one JSON blob guarded by `blocks_revision`. Keep an in-memory array of full
  block-list snapshots with a pointer; every successful edit pushes one. Undo/redo
  sends the target snapshot to a new `PUT /blocks` that overwrites the array
  atomically (CAS on revision, reuses `_save_doc` → markdown reassembly + verdict
  reset). Preserves block IDs exactly, makes multi-delete one call, consistent by
  construction. Cost: ~30-line backend addition reusing existing validators.
- **B — Client-side inverse ops over existing endpoints.** Rejected: re-creating a
  deleted block via `POST /blocks` assigns a **new** server id (`_new_id()`),
  breaking redo chains and selection; multi-delete undo = N re-creates = N new ids.
- **C — Server-side undo log.** Rejected as YAGNI: extra storage, concurrency, and
  complexity for a single-reviewer annotator. (Approach A keeps undo session-local,
  which is the expected behavior here.)

## Goal

In the annotator: press **Delete** to remove the selected block(s); **left-drag**
a box or **Ctrl+click** to select several at once; and **Undo/Redo** (buttons +
Ctrl+Z / Ctrl+Shift+Z) to step block edits backward and forward safely.

---

## Backend design

### New: `PUT /review/notice/{filename}/blocks` (replace-all)

A single atomic overwrite of the block array, used by **undo/redo** and by
**multi-delete**. It does NOT replace the existing granular endpoints
(`POST /blocks`, `PUT /blocks/{id}`, `DELETE /blocks/{id}`, `POST /blocks/reorder`),
which keep handling live single-item edits with their current server-side logic.

**`api/review/blocks.py`** — new `replace_blocks(...)`, plus a pure, separately
testable normalizer:

```python
def _normalize_replacement_blocks(raw_blocks: list, by_email: str) -> list[dict]:
    """Validate + canonicalize an incoming full block array.

    Preserves each block's id (assigns a fresh one only if missing/blank),
    cleans bbox, validates label, cleans/strips table to match label, and
    preserves source/confidence/edited_* as a faithful state restore. De-dups
    ids defensively. Pure — no DB access, so it is unit-testable in isolation
    (same pattern as the rotation-math tests).
    """
    out, seen = [], set()
    for raw in raw_blocks:
        bid = raw.get("id") or _new_id()
        if bid in seen:
            bid = _new_id()
        seen.add(bid)
        label = _validate_label(raw.get("label") or DEFAULT_LABEL)
        src = raw.get("source")
        conf = raw.get("confidence")
        out.append({
            "id":            bid,
            "page":          max(1, int(raw.get("page") or 1)),
            "bbox":          _clean_bbox(raw.get("bbox")),
            "label":         label,
            "text":          str(raw.get("text") or ""),
            "reading_order": int(raw.get("reading_order") or 0),
            "source":        src if src in ("mineru", "human") else "human",
            "confidence":    float(conf) if isinstance(conf, (int, float)) else None,
            "table":         _clean_table(raw.get("table")) if label == "Table" else None,
            "edited_at":     raw.get("edited_at") or _iso_now(),
            "edited_by":     raw.get("edited_by") or by_email,
        })
    return out


def replace_blocks(filename: str, raw_blocks: list,
                   expected_rev: int | None, by_email: str) -> dict:
    """Atomically replace the whole block array. CAS on blocks_revision."""
    doc, rev, _ = _load_doc(filename)
    if expected_rev is not None and int(expected_rev) != rev:
        raise BlocksConflict("blocks_revision changed; reload required")
    doc["blocks"] = _normalize_replacement_blocks(raw_blocks, by_email)
    _save_doc(filename, doc, rev)          # reassembles markdown, clears verdict
    return get_blocks(filename)
```

Notes:
- `expected_rev` is optional. The client always passes the current `ann.rev`, so a
  stale undo against a concurrently-changed doc yields a clean 409 → reload (same
  contract as `update_block`). Omitting it falls back to last-writer-wins.
- `_save_doc` already bumps `blocks_revision`, rewrites `Document.markdown`, and
  clears `markdown_verified_*` / `markdown_quality` — undo/redo and multi-delete
  inherit that behavior for free.
- Empty `blocks` array is allowed (you can delete everything; undo restores it).

**`api/review/router.py`** — new body models + route, mirroring `reorder`:

```python
class BlockReplaceItem(BaseModel):
    id: str | None = None
    page: int = 1
    bbox: list[float]
    label: str = "Text"
    text: str | None = ""
    reading_order: int = 0
    source: str | None = None
    confidence: float | None = None
    table: TableShape | None = None
    edited_at: str | None = None
    edited_by: str | None = None

class ReplaceBlocksBody(BaseModel):
    blocks: list[BlockReplaceItem]
    expected_revision: int | None = None

@router.put("/notice/{filename}/blocks", response_model=BlocksDoc)
@_wrap_block_errors
async def review_notice_replace_blocks(
    filename: str,
    body: ReplaceBlocksBody,
    admin: UserOut = Depends(get_current_admin),
) -> BlocksDoc:
    blocks = [b.model_dump(exclude_none=True) for b in body.blocks]
    return _ok_doc(block_ops.replace_blocks(
        filename, blocks, body.expected_revision, by_email=admin.email))
```

`PUT /notice/{filename}/blocks` (no `{block_id}`) does not collide with the
existing `PUT /notice/{filename}/blocks/{block_id}`. Auth/error wrapping
(`_wrap_block_errors`, `get_current_admin`) match the other block routes.

---

## Frontend design (`web/review.html`)

### 1. Selection model

Replace the single `ann.selectedId` with a set + an anchor:

- `ann.selectedIds` — `Set<string>` of selected block ids.
- `ann.anchorId` — the "primary" id (last clicked), used where a single focus is
  needed: sidebar scroll-into-view and the selected-Table inline editor (only the
  anchor block expands its editor; a multi-selection does not open N editors).

Helpers:

- `isSelected(id)` → `ann.selectedIds.has(id)`
- `setSelection(ids, anchor)` → set membership + anchor, then `paintAnnAll()`
- `selectBlock(id, { additive = false })`:
  - additive → toggle `id` in the set; anchor = id (or nearest remaining if removed)
  - else → set becomes `{id}`; anchor = id
- `clearSelection()` → empty set, `anchorId = null`

The 12 existing `ann.selectedId` reads are converted: assignment sites
(`createBlockOnServer`, etc.) call `setSelection`/`selectBlock`; the
`deleteBlockOnServer` "was it selected?" check uses `ann.selectedIds.delete(id)`;
paint sites use `isSelected(id)` for the `selected` class; the inline Table editor
keys off `ann.anchorId`.

### 2. Pointer interactions (`onStagePointerDown`, new `startSelectMarquee`)

- **Ctrl/Cmd+click on a block** (not on a resizer/guide handle) → `selectBlock(id,
  {additive:true})` and **return** (do not start a move).
- **Plain click/drag on a block** → `selectBlock(id)` (single), then existing
  move/resize/guide-drag. Starting a move/resize collapses the selection to that
  one block (keeps the well-tested move/resize code operating on a single id — **no
  group move**, out of scope).
- **Left-drag on empty canvas in `select` mode** → `startSelectMarquee(ev)`:
  reuses the marquee visuals from `startMarquee`, but on release selects every
  block on the current page whose bbox **intersects** the marquee rect (normalized
  coords from `pointerToNorm`). Ctrl/Cmd held during the marquee → add to the
  existing selection; otherwise replace it. A near-zero drag (< ~0.01 on an axis) is
  treated as a click on empty space → `clearSelection()`.
- `draw`/`crop` mode marquee behavior is unchanged.

Bbox intersection (both normalized `[x0,y0,x1,y1]`): overlap iff
`a.x0 < m.x1 && a.x1 > m.x0 && a.y0 < m.y1 && a.y1 > m.y0`.

### 3. Delete key & multi-delete

- Extend the existing global `keydown` listener. When `#screen-annotator` is
  visible and the user is **not** typing (reuse the existing `typing` guard for
  INPUT/TEXTAREA/SELECT):
  - **`Delete` or `Backspace`** → if `ann.selectedIds.size > 0`,
    `deleteSelectedBlocks()`; `preventDefault()` (stops Backspace browser-back).
- `deleteSelectedBlocks()`:
  - 1 selected → delegate to existing `deleteBlockOnServer(id)` (`DELETE
    /blocks/{id}`), which already clears selection and `pushHistory()`s on success.
    No extra push here (avoids a double undo step).
  - 2+ selected → `replaceBlocksOnServer(survivors)` (one atomic call); on success
    `clearSelection()` and `pushHistory()` once.
  - Both branches toast `deleted N — Ctrl+Z to undo`.
- The per-row `delete` button drops its `confirm()` and calls the same delete path
  (single), so it also becomes undoable.

### 4. Undo/redo

State on `ann`:

- `history: []` — array of deep block snapshots (`JSON.parse(JSON.stringify(blocks))`).
- `histPtr: -1` — index of the snapshot matching the current server state.
- `applyingHistory: false` — guard so undo/redo restores don't push new snapshots.
- `HIST_CAP = 50` — when exceeded, drop the oldest and decrement `histPtr`.

Functions:

- `initHistory()` — on successful notice load: `history = [snapshot()]; histPtr = 0`.
- `pushHistory()` — if `applyingHistory`, no-op. Else truncate `history` after
  `histPtr`, push `snapshot()`, set `histPtr = history.length - 1`, enforce
  `HIST_CAP`, then `updateUndoRedoButtons()`. Called at the **success** tail of
  every block mutation: `createBlockOnServer`, `updateBlockOnServer`,
  `deleteBlockOnServer`, `reorderBlocksOnServer`, and `deleteSelectedBlocks`
  (multi). Debounced text edits push once per *settled* save (one undo step per
  edit burst).
- `undo()` / `redo()` — bounds-check; set `applyingHistory = true`; move `histPtr`
  ∓1; `await replaceBlocksOnServer(history[histPtr])`; `applyingHistory = false`;
  `updateUndoRedoButtons()`.
- `replaceBlocksOnServer(blocks)` — `PUT /blocks` with `{ blocks, expected_revision:
  ann.rev }`. On success merge the returned doc into `ann.blocks`, bump `ann.rev`,
  and repaint markdown/sidebar/overlay. Does **not** push history itself (callers
  decide: multi-delete pushes; undo/redo does not).

Toolbar (after `+ Add block`): `↶ Undo` (`id="ann-undo"`), `↷ Redo`
(`id="ann-redo"`), both `.ghost`, `disabled` toggled by `updateUndoRedoButtons()`
(`histPtr <= 0` disables undo; `histPtr >= history.length - 1` disables redo).

Keyboard (annotator visible, not typing — so the block-text `<textarea>` keeps its
native Ctrl+Z while focused):

- **Ctrl/Cmd+Z** (no Shift) → `undo()`
- **Ctrl/Cmd+Shift+Z** or **Ctrl+Y** → `redo()`

---

## Error handling

- `replaceBlocksOnServer` 409 → `annToast('conflict, reloading')` + `loadAnnotator`
  (same as `updateBlockOnServer`). Non-2xx / thrown → toast; on an **undo/redo**
  failure, revert `histPtr` to its pre-attempt value so the buttons stay truthful.
- Empty selection + Delete → no-op (no toast).
- Undo/redo at a stack boundary → no-op (button already disabled; keyboard path
  bounds-checks).
- History is per-notice and in-memory: `initHistory()` resets it on every load, so
  switching notices or pressing Reload starts a fresh stack.

## Testing

- **Backend (pytest, mirrors `tests/api/test_review_rotation.py`):** import
  `blocks.py` in isolation with stubbed `neo4j`/`mineru` and unit-test the pure
  `_normalize_replacement_blocks`:
  - existing id is preserved; missing/blank id gets a `blk_…` id; duplicate ids are
    de-duped.
  - out-of-range bbox is clamped (`_clean_bbox`); invalid label raises `ValueError`
    (→ 400); `table` is cleared when `label != "Table"` and kept/cleaned when it is.
  - `source` outside `{mineru,human}` falls back to `human`; non-numeric
    `confidence` → `None`.
- **Frontend:** vanilla single-file JS with no JS test harness — verify via the
  `/qa` browser flow on the annotator: marquee selects the expected blocks; Ctrl+
  click toggles; Delete removes selection (no confirm) with a toast; Undo/Redo
  buttons + Ctrl+Z/Ctrl+Shift+Z step add/delete/move/relabel/text edits and
  enable/disable at the stack ends; native textarea Ctrl+Z still works while
  editing block text.

## Out of scope

- Group move/resize of a multi-selection (move/resize stay single-block).
- Undo/redo of crop and rotation (separate document-level controls; they re-run OCR).
- Server-side / cross-reload undo history.
- Batch relabel (the `replace_blocks` endpoint makes it trivial to add later, but
  it is not built now).
