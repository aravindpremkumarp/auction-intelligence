# Annotator Multi-Select, Delete Key & Undo/Redo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add keyboard-Delete, multi-block selection (marquee + Ctrl/Cmd+click), and undo/redo to the per-block annotator in `web/review.html`.

**Architecture:** Client keeps a session-local stack of full block-list snapshots; undo/redo and multi-delete commit through one new atomic `PUT /blocks` "replace" endpoint (CAS on `blocks_revision`, reuses `_save_doc`). Selection becomes a `Set` of ids plus an "anchor" id. Live single edits keep using the existing granular endpoints.

**Tech Stack:** FastAPI + Neo4j (`api/review/blocks.py`, `api/review/router.py`), pytest for backend; single-file vanilla JS (`web/review.html`) for frontend (no JS test harness — verified in the browser).

**Spec:** `docs/superpowers/specs/2026-06-03-annotator-multiselect-delete-undo-design.md`

**Branch:** `claude/annotator-multiselect-delete-undo` (already created off `origin/main`; the spec is already committed on it).

**How to run the app (for frontend verification):**
```bash
RATELIMIT_DISABLED=1 uvicorn api.main:app --reload
# open http://localhost:8000 → log in as admin → Review queue → open a notice → "annotate"
# to reach the annotator screen (#screen-annotator) with blocks overlaid on the scan.
```
**How to run backend tests:** `pytest tests/api/test_review_replace_blocks.py -v`

---

## Task 1: Backend — `_normalize_replacement_blocks` + `replace_blocks`

**Files:**
- Modify: `api/review/blocks.py` (add two functions after `reorder_blocks`, ~line 542)
- Test: `tests/api/test_review_replace_blocks.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_review_replace_blocks.py`. The import harness (stubbing `api.neo4j_client` / `pipeline.mineru` so importing `blocks.py` stays cheap and DB-free) is copied verbatim from `tests/api/test_review_rotation.py` so we can unit-test the **pure** normalizer in isolation:

```python
"""
tests/api/test_review_replace_blocks.py
---------------------------------------
Unit tests for the pure block-array normalizer behind the replace-all
endpoint (``_normalize_replacement_blocks`` in :mod:`api.review.blocks`).

Covers id preservation/assignment, de-dup, bbox clamping, label validation,
table stripping, and source/confidence fallbacks — the logic an undo/redo or
multi-delete restore depends on. DB-free: blocks.py is imported in isolation
with stubbed neo4j/mineru, same pattern as test_review_rotation.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BLOCKS_PATH = Path(__file__).resolve().parents[2] / "api" / "review" / "blocks.py"
_spec = importlib.util.spec_from_file_location("_blocks_under_test_replace", _BLOCKS_PATH)
_mod = importlib.util.module_from_spec(_spec)

_STUB_KEYS = ("api.neo4j_client", "pipeline.mineru", "pipeline")
_saved = {k: sys.modules.get(k) for k in _STUB_KEYS}

if "api.neo4j_client" not in sys.modules:
    _stub_neo4j = types.ModuleType("api.neo4j_client")
    _stub_neo4j.run_query = lambda *a, **k: None
    _stub_neo4j.run_read_query = lambda *a, **k: None
    sys.modules["api.neo4j_client"] = _stub_neo4j
if "pipeline" not in sys.modules:
    sys.modules["pipeline"] = types.ModuleType("pipeline")
if "pipeline.mineru" not in sys.modules:
    _stub_mineru = types.ModuleType("pipeline.mineru")
    _stub_mineru.DEFAULT_LABEL = "Text"
    _stub_mineru.MINERU_LABEL_VALUES = ["Text", "Title", "Table"]
    _stub_mineru.assemble_markdown = lambda blocks: ""
    sys.modules["pipeline.mineru"] = _stub_mineru

try:
    _spec.loader.exec_module(_mod)
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

_normalize = _mod._normalize_replacement_blocks


def _blk(**over):
    base = {"id": "blk_keepme", "page": 1, "bbox": [0.1, 0.1, 0.5, 0.5],
            "label": "Text", "text": "hi", "reading_order": 3,
            "source": "human", "confidence": 0.9}
    base.update(over)
    return base


def test_preserves_existing_id():
    out = _normalize([_blk(id="blk_abc")], "me@x.com")
    assert out[0]["id"] == "blk_abc"


def test_assigns_id_when_missing():
    out = _normalize([_blk(id=None)], "me@x.com")
    assert out[0]["id"].startswith("blk_")


def test_dedupes_duplicate_ids():
    out = _normalize([_blk(id="blk_dup"), _blk(id="blk_dup")], "me@x.com")
    assert out[0]["id"] == "blk_dup"
    assert out[1]["id"] != "blk_dup"
    assert out[1]["id"].startswith("blk_")


def test_clamps_out_of_range_bbox():
    out = _normalize([_blk(bbox=[-0.2, 0.5, 1.4, 0.9])], "me@x.com")
    assert out[0]["bbox"][0] == 0.0
    assert out[0]["bbox"][2] == 1.0


def test_invalid_label_raises():
    with pytest.raises(ValueError):
        _normalize([_blk(label="Bogus")], "me@x.com")


def test_table_cleared_for_non_table_label():
    out = _normalize([_blk(label="Text", table={"format": "html"})], "me@x.com")
    assert out[0]["table"] is None


def test_table_kept_for_table_label():
    out = _normalize([_blk(label="Table", table={"format": "html", "rows": 2})],
                     "me@x.com")
    assert out[0]["table"] is not None
    assert out[0]["table"]["rows"] == 2


def test_source_fallback_and_confidence_none():
    out = _normalize([_blk(source="weird", confidence="nan")], "me@x.com")
    assert out[0]["source"] == "human"
    assert out[0]["confidence"] is None


def test_edited_by_defaults_to_caller_when_missing():
    out = _normalize([{"bbox": [0.1, 0.1, 0.2, 0.2], "label": "Text"}], "me@x.com")
    assert out[0]["edited_by"] == "me@x.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_review_replace_blocks.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_normalize_replacement_blocks'`.

- [ ] **Step 3: Add the implementation to `api/review/blocks.py`**

Insert immediately after the `reorder_blocks` function (which ends with `return get_blocks(filename)` around line 542), before `async def re_extract_block`:

```python
def _normalize_replacement_blocks(raw_blocks: Any, by_email: str) -> list[dict]:
    """Validate + canonicalize a full incoming block array for replace_blocks.

    Pure (no DB access) so it is unit-testable in isolation. Preserves each
    block's id (assigns a fresh one only when missing/blank), de-dups ids,
    cleans bbox, validates label, and strips/cleans ``table`` to match the
    label. ``source`` / ``confidence`` / ``edited_*`` are preserved so an undo
    restores a faithful prior state.
    """
    if not isinstance(raw_blocks, list):
        raise ValueError("blocks must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ValueError("each block must be an object")
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
            "confidence":    (float(conf)
                              if isinstance(conf, (int, float))
                              and not isinstance(conf, bool) else None),
            "table":         _clean_table(raw.get("table")) if label == "Table"
                             else None,
            "edited_at":     raw.get("edited_at") or _iso_now(),
            "edited_by":     raw.get("edited_by") or by_email,
        })
    return out


def replace_blocks(filename: str, raw_blocks: Any,
                   expected_rev: int | None, by_email: str) -> dict:
    """Atomically replace the entire block array (undo/redo + multi-delete).

    CAS on ``blocks_revision`` via ``expected_rev`` when provided (the client
    always passes the current rev, so a stale write yields a clean 409 →
    reload). Reuses ``_save_doc`` so markdown is reassembled and the markdown
    verdict cleared, exactly like the granular endpoints.
    """
    doc, rev, _ = _load_doc(filename)
    if expected_rev is not None and int(expected_rev) != rev:
        raise BlocksConflict("blocks_revision changed; reload required")
    doc["blocks"] = _normalize_replacement_blocks(raw_blocks, by_email)
    _save_doc(filename, doc, rev)
    return get_blocks(filename)
```

(`Any`, `_new_id`, `_validate_label`, `DEFAULT_LABEL`, `_clean_bbox`, `_clean_table`, `_iso_now`, `_load_doc`, `_save_doc`, `BlocksConflict`, `get_blocks` are all already defined/imported in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_review_replace_blocks.py -v`
Expected: PASS — all 9 tests green.

- [ ] **Step 5: Commit**

```bash
git add api/review/blocks.py tests/api/test_review_replace_blocks.py
git commit -m "feat(review): add replace_blocks + pure block-array normalizer"
```

---

## Task 2: Backend — `PUT /blocks` replace-all route

**Files:**
- Modify: `api/review/router.py` (add body models near `ReorderBody` ~line 388; add route after the reorder route ~line 863)

- [ ] **Step 1: Add the request body models**

In `api/review/router.py`, immediately after the `ReorderBody` class (ends ~line 388, before `class ReExtractBody`), add:

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
```

- [ ] **Step 2: Add the route**

Immediately after the `review_notice_reorder_blocks` route (ends ~line 863, before the `@router.post(".../re-extract"...)` route), add:

```python
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

`PUT /notice/{filename}/blocks` (no `{block_id}`) does not collide with the existing `PUT /notice/{filename}/blocks/{block_id}`. `BaseModel`, `TableShape`, `BlocksDoc`, `_wrap_block_errors`, `_ok_doc`, `block_ops`, `UserOut`, `Depends`, `get_current_admin` are already imported/defined in this file.

- [ ] **Step 3: Verify the app imports and the route is registered**

Run:
```bash
python -c "from api.main import app; \
paths = [r.path + ' ' + ','.join(r.methods) for r in app.routes if getattr(r,'path','').endswith('/notice/{filename}/blocks')]; \
print('\n'.join(paths))"
```
Expected output includes both:
```
/review/notice/{filename}/blocks GET
/review/notice/{filename}/blocks POST
/review/notice/{filename}/blocks PUT
```
(POST = create, PUT = replace-all; the `{block_id}` PUT/DELETE are on a different path.)

- [ ] **Step 4: Commit**

```bash
git add api/review/router.py
git commit -m "feat(review): expose PUT /blocks replace-all route"
```

---

## Task 3: Frontend — selection model (Set + anchor)

Convert the annotator from a single `ann.selectedId` to `ann.selectedIds` (a `Set`) + `ann.anchorId`, with helper functions. **Single-block selection still behaves exactly as before** after this task — multi-select interactions come in Task 4.

**Files:**
- Modify: `web/review.html` (annotator state + ~12 `selectedId` sites + `selectBlock`)

- [ ] **Step 1: Replace the `selectedId` state field**

Find (~line 2336, inside `const ann = {`):
```javascript
    selectedId: null,
```
Replace with:
```javascript
    selectedIds: new Set(),   // ids of all selected blocks
    anchorId: null,           // "primary" selected id (scroll target, table editor)
```

- [ ] **Step 2: Add selection helpers**

Immediately **before** `function selectBlock(id) {` (~line 3128), add:
```javascript
  function isSelected(id) { return ann.selectedIds.has(id); }

  function clearSelection() {
    if (!ann.selectedIds.size && !ann.anchorId) return;
    ann.selectedIds = new Set();
    ann.anchorId = null;
    paintAnnAll();
  }
```

- [ ] **Step 3: Rewrite `selectBlock` to be set-aware**

Replace the whole function (~lines 3128–3138):
```javascript
  function selectBlock(id) {
    if (ann.selectedId === id) return;
    ann.selectedId = id;
    paintAnnAll();
    requestAnimationFrame(() => {
      const row = document.querySelector(`#ann-rows .ann-row[data-id="${id}"]`);
      if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      const blk = document.querySelector(`#ann-page-wrap-inner .ann-block[data-id="${id}"]`);
      if (blk) blk.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
    });
  }
```
with:
```javascript
  function selectBlock(id, opts) {
    opts = opts || {};
    if (opts.additive) {
      if (ann.selectedIds.has(id)) {
        ann.selectedIds.delete(id);
        if (ann.anchorId === id) {
          ann.anchorId = ann.selectedIds.values().next().value || null;
        }
      } else {
        ann.selectedIds.add(id);
        ann.anchorId = id;
      }
    } else {
      ann.selectedIds = new Set([id]);
      ann.anchorId = id;
    }
    paintAnnAll();
    requestAnimationFrame(() => {
      if (!ann.anchorId) return;
      const row = document.querySelector(`#ann-rows .ann-row[data-id="${ann.anchorId}"]`);
      if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      const blk = document.querySelector(`#ann-page-wrap-inner .ann-block[data-id="${ann.anchorId}"]`);
      if (blk) blk.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
    });
  }
```

- [ ] **Step 4: Reset selection on notice load**

Find (~line 2381, in `loadAnnotator`):
```javascript
    ann.selectedId = null;
```
Replace with:
```javascript
    ann.selectedIds = new Set();
    ann.anchorId = null;
```

- [ ] **Step 5: Convert the overlay paint sites (`renderBlockDiv`)**

Find (~line 2751):
```javascript
    if (ann.selectedId === b.id) el.classList.add('selected');
```
Replace with:
```javascript
    if (isSelected(b.id)) el.classList.add('selected');
```

Find (~line 2757):
```javascript
    if (ann.selectedId === b.id) {
```
Replace with (resize handles + table guides are single-block affordances → only when exactly one block is selected):
```javascript
    if (ann.selectedIds.size === 1 && isSelected(b.id)) {
```

Find (~line 2798):
```javascript
      } else if (ann.selectedId === b.id) {
```
Replace with:
```javascript
      } else if (ann.selectedIds.size === 1 && isSelected(b.id)) {
```

- [ ] **Step 6: Convert the sidebar paint sites (`paintAnnSidebar` / `renderSidebarRow`)**

Find (~line 2940):
```javascript
    const sel = ann.selectedId && ann.blocks.find(b => b.id === ann.selectedId);
```
Replace with (the inline table editor mounts only for the anchor):
```javascript
    const sel = ann.anchorId && ann.blocks.find(b => b.id === ann.anchorId);
```

Find (~line 2958):
```javascript
    if (ann.selectedId === b.id) row.classList.add('selected');
```
Replace with:
```javascript
    if (isSelected(b.id)) row.classList.add('selected');
```

Find (~line 2999):
```javascript
      if (ann.selectedId === b.id) {
```
Replace with (only the anchor Table block opens the full inline editor; other selected tables show the read-only preview):
```javascript
      if (ann.anchorId === b.id) {
```

- [ ] **Step 7: Convert the create/delete selection bookkeeping**

Find (~line 2999's neighbor in `createBlockOnServer`, ~line 3385):
```javascript
      ann.selectedId = blk.id;
```
Replace with:
```javascript
      ann.selectedIds = new Set([blk.id]);
      ann.anchorId = blk.id;
```

Replace the whole `deleteBlockOnServer` function (~lines 3468–3482) — set-aware bookkeeping **and** a boolean return (used by Task 5); behavior is otherwise identical:
```javascript
  async function deleteBlockOnServer(id) {
    try {
      const r = await authFetch(
        API + '/review/notice/' + encodeURIComponent(ann.filename)
            + '/blocks/' + encodeURIComponent(id),
        { method: 'DELETE' }
      );
      if (!r.ok) { annToast('delete failed (' + r.status + ')', 'err'); return false; }
      ann.blocks = ann.blocks.filter(b => b.id !== id);
      ann.selectedIds.delete(id);
      if (ann.anchorId === id) ann.anchorId = ann.selectedIds.values().next().value || null;
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      paintAnnAll();
      return true;
    } catch (e) { annToast('delete error: ' + e, 'err'); return false; }
  }
```

- [ ] **Step 8: Verify in the browser (single-select unchanged)**

Start the app (`RATELIMIT_DISABLED=1 uvicorn api.main:app --reload`), open a notice's annotator. Confirm, with no regression vs. before:
- Clicking a block highlights exactly that block (overlay + its sidebar row), shows its resize handles, and scrolls its sidebar row into view.
- Clicking another block moves the highlight.
- Selecting a Table block still opens the inline table editor in its sidebar row.
- The row `delete` button still deletes (confirm dialog still present — removed in Task 5).
- No console errors; no remaining bare `ann.selectedId` references: `grep -nE "selectedId\b" web/review.html` returns **nothing** (the `\b` excludes the new `selectedIds`).

- [ ] **Step 9: Commit**

```bash
git add web/review.html
git commit -m "refactor(review): annotator selection as a Set + anchor id"
```

---

## Task 4: Frontend — marquee select + Ctrl/Cmd+click toggle

**Files:**
- Modify: `web/review.html` (`onStagePointerDown`; add `startSelectMarquee`)

- [ ] **Step 1: Add Ctrl/Cmd+click toggle and empty-canvas marquee to `onStagePointerDown`**

Replace the whole function (~lines 3141–3165):
```javascript
  function onStagePointerDown(ev) {
    const wrap = $('ann-page-wrap-inner');
    const target = ev.target.closest('.resizer, .grid-row, .grid-col, .ann-block');
    const inCrop = ann.mode === 'crop';
    const inDraw = ann.mode === 'draw';
    if ((inDraw || inCrop) &&
        (!target || target.classList.contains('ann-overlay'))) {
      return startMarquee(ev);
    }
    if (!target) return;
    const blockEl = target.closest('.ann-block');
    if (!blockEl) return;
    const id = blockEl.dataset.id;
    selectBlock(id);
    if (target.classList.contains('resizer')) {
      ev.preventDefault();
      return startResize(ev, id, target.dataset.handle);
    }
    if (target.classList.contains('grid-row') || target.classList.contains('grid-col')) {
      ev.preventDefault();
      return startGuideDrag(ev, id, target.dataset.guide, parseInt(target.dataset.idx, 10));
    }
    ev.preventDefault();
    return startMove(ev, id);
  }
```
with:
```javascript
  function onStagePointerDown(ev) {
    const target = ev.target.closest('.resizer, .grid-row, .grid-col, .ann-block');
    const inCrop = ann.mode === 'crop';
    const inDraw = ann.mode === 'draw';
    if ((inDraw || inCrop) &&
        (!target || target.classList.contains('ann-overlay'))) {
      return startMarquee(ev);
    }
    // Select mode + left-drag on empty canvas → rubber-band multi-select.
    if (!inDraw && !inCrop && ev.button === 0 && !target) {
      return startSelectMarquee(ev);
    }
    if (!target) return;
    const blockEl = target.closest('.ann-block');
    if (!blockEl) return;
    const id = blockEl.dataset.id;
    // Ctrl/Cmd+click toggles a block in/out of the selection (no move/resize).
    if ((ev.ctrlKey || ev.metaKey) &&
        !target.classList.contains('resizer') &&
        !target.classList.contains('grid-row') &&
        !target.classList.contains('grid-col')) {
      ev.preventDefault();
      return selectBlock(id, { additive: true });
    }
    selectBlock(id);
    if (target.classList.contains('resizer')) {
      ev.preventDefault();
      return startResize(ev, id, target.dataset.handle);
    }
    if (target.classList.contains('grid-row') || target.classList.contains('grid-col')) {
      ev.preventDefault();
      return startGuideDrag(ev, id, target.dataset.guide, parseInt(target.dataset.idx, 10));
    }
    ev.preventDefault();
    return startMove(ev, id);
  }
```

- [ ] **Step 2: Add `startSelectMarquee`**

Immediately **after** the existing `startMarquee` function (ends ~line 3323, with its `window.addEventListener('pointerup', onUp);` and closing `}`), add:
```javascript
  // Select-mode rubber-band: drag a box on empty canvas to select every block
  // on the current page whose bbox intersects it. Ctrl/Cmd held during the
  // drag adds to the existing selection instead of replacing it.
  function startSelectMarquee(ev) {
    const wrap = $('ann-page-wrap-inner');
    const overlay = wrap && wrap.querySelector('.ann-overlay');
    if (!overlay) return;
    const additive = ev.ctrlKey || ev.metaKey;
    const start = pointerToNorm(ev);
    const marquee = document.createElement('div');
    marquee.className = 'ann-marquee';
    overlay.appendChild(marquee);
    let end = start.slice();
    const onMove = (e) => {
      end = pointerToNorm(e);
      const x0 = Math.min(start[0], end[0]), y0 = Math.min(start[1], end[1]);
      const x1 = Math.max(start[0], end[0]), y1 = Math.max(start[1], end[1]);
      marquee.style.left   = (x0 * 100) + '%';
      marquee.style.top    = (y0 * 100) + '%';
      marquee.style.width  = ((x1 - x0) * 100) + '%';
      marquee.style.height = ((y1 - y0) * 100) + '%';
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      marquee.remove();
      const mx0 = Math.min(start[0], end[0]), my0 = Math.min(start[1], end[1]);
      const mx1 = Math.max(start[0], end[0]), my1 = Math.max(start[1], end[1]);
      // Tiny drag = a click on empty space → clear (unless additive).
      if (mx1 - mx0 < 0.01 && my1 - my0 < 0.01) {
        if (!additive) clearSelection();
        return;
      }
      const hits = ann.blocks.filter(b =>
        (b.page || 1) === ann.page &&
        b.bbox[0] < mx1 && b.bbox[2] > mx0 &&
        b.bbox[1] < my1 && b.bbox[3] > my0);
      const next = additive ? new Set(ann.selectedIds) : new Set();
      for (const b of hits) next.add(b.id);
      ann.selectedIds = next;
      ann.anchorId = hits.length ? hits[hits.length - 1].id
                   : (additive ? ann.anchorId : null);
      paintAnnAll();
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }
```
(`.ann-marquee` CSS already exists — reused from the draw/crop marquee. `pointerToNorm`, `ann.page`, `clearSelection`, `paintAnnAll` are all defined.)

- [ ] **Step 3: Verify in the browser**

Reload the annotator. Confirm:
- **Left-drag on empty canvas** draws a box; on release, every block whose bbox overlaps the box is highlighted (overlay + sidebar rows). No resize handles appear when 2+ are selected.
- A tiny click-drag on empty space clears the selection.
- **Ctrl/Cmd+click** on a block adds it to / removes it from the selection without disturbing the others.
- **Ctrl/Cmd+left-drag** on empty canvas adds the boxed blocks to the existing selection.
- Plain click on a block still selects just that one; drag on a block still moves it; resize handles still work on a singly-selected block.
- No console errors.

- [ ] **Step 4: Commit**

```bash
git add web/review.html
git commit -m "feat(review): marquee + ctrl-click multi-select in the annotator"
```

---

## Task 5: Frontend — Delete key, multi-delete, replace endpoint, drop confirm

**Files:**
- Modify: `web/review.html` (add `replaceBlocksOnServer` + `deleteSelectedBlocks`; extend the global `keydown`; drop the row `delete` confirm)

- [ ] **Step 1: Add `replaceBlocksOnServer`**

Immediately **before** `async function deleteBlockOnServer(id)` (~line 3468), add:
```javascript
  // Atomic full-array overwrite — used by multi-delete and undo/redo. Returns
  // true on a clean save, false on any failure. Does NOT push undo history
  // itself; callers decide (multi-delete pushes; undo/redo does not).
  async function replaceBlocksOnServer(blocks) {
    try {
      const r = await authFetch(
        API + '/review/notice/' + encodeURIComponent(ann.filename) + '/blocks',
        { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ blocks, expected_revision: ann.rev }) }
      );
      if (r.status === 409) { annToast('conflict, reloading', 'err'); await loadAnnotator(ann.filename); return false; }
      if (!r.ok) { annToast('save failed (' + r.status + ')', 'err'); return false; }
      const doc = await r.json();
      ann.blocks = (doc.blocks || []).map(b => ({ ...b, bbox: b.bbox.slice() }));
      ann.rev = doc.blocks_revision || ann.rev;
      $('ann-rev').textContent = 'rev ' + ann.rev;
      const live = new Set(ann.blocks.map(b => b.id));
      ann.selectedIds = new Set(Array.from(ann.selectedIds).filter(id => live.has(id)));
      if (ann.anchorId && !live.has(ann.anchorId)) {
        ann.anchorId = ann.selectedIds.values().next().value || null;
      }
      refreshMarkdownAfterChange();
      paintAnnAll();
      return true;
    } catch (e) { annToast('save error: ' + e, 'err'); return false; }
  }

  // Delete every selected block. 1 → existing DELETE endpoint; 2+ → one atomic
  // replace with the survivors. No confirm — undo is the safety net.
  async function deleteSelectedBlocks() {
    const ids = Array.from(ann.selectedIds);
    if (!ids.length) return;
    if (ids.length === 1) {
      const ok = await deleteBlockOnServer(ids[0]);
      if (!ok) return;
    } else {
      const survivors = ann.blocks.filter(b => !ann.selectedIds.has(b.id));
      const ok = await replaceBlocksOnServer(survivors);
      if (!ok) return;
      clearSelection();
      pushHistory();
    }
    annToast('deleted ' + ids.length + ' — Ctrl+Z to undo');
  }
```
> Note: `pushHistory` is defined in Task 6. Between this commit and Task 6, a **multi**-delete would throw `ReferenceError: pushHistory is not defined` *after* the server already saved. Do Task 6 immediately after Task 5 (or temporarily test only single-delete here). Single-delete routes through `deleteBlockOnServer`, which gets its `pushHistory()` in Task 6, so it is safe now.

- [ ] **Step 2: Add the annotator Delete-key handler**

Find the end of the gallery-shortcuts block in the global `keydown` listener — the line (~line 2306):
```javascript
      if (e.key === '/') {
        e.preventDefault();
        $('q').focus();
        return;
      }
    }
```
Immediately **after** that closing `}` (which closes `if (inGalleryView() ...)`), and **before** the `// Detail-screen shortcuts (unchanged)` comment, insert:
```javascript

    // Annotator-screen shortcuts (Delete selection — undo/redo keys in Task 6)
    if (!$('screen-annotator').classList.contains('hidden') && !typing) {
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (ann.selectedIds.size) { e.preventDefault(); deleteSelectedBlocks(); }
        return;
      }
    }
```
(`typing` is already computed at the top of this handler.)

- [ ] **Step 3: Drop the row `delete` confirm**

Find (~line 3106):
```javascript
    btnDel.addEventListener('click', () => {
      if (!confirm('Delete this block?')) return;
      deleteBlockOnServer(b.id);
    });
```
Replace with:
```javascript
    btnDel.addEventListener('click', () => { deleteBlockOnServer(b.id); });
```

- [ ] **Step 4: Verify in the browser**

Reload the annotator. Confirm:
- Select one block, press **Delete** → it's removed, toast "deleted 1 — Ctrl+Z to undo", no confirm dialog.
- Marquee-select 3 blocks, press **Delete** → all three removed in one go, toast "deleted 3 — …", `rev` increments by 1 (single atomic save). Reloading the page shows them still gone (persisted).
- **Backspace** behaves the same and does not navigate the browser back.
- The row `delete` button removes a block with no confirm dialog.
- Pressing Delete while a sidebar `<textarea>`/input is focused does **not** delete blocks (still edits text).
- No console errors (single-delete; avoid multi-delete until Task 6 is in, or accept the post-save ReferenceError noted above).

- [ ] **Step 5: Commit**

```bash
git add web/review.html
git commit -m "feat(review): Delete key + multi-delete via replace endpoint, drop confirm"
```

---

## Task 6: Frontend — undo/redo (history stack, buttons, shortcuts)

**Files:**
- Modify: `web/review.html` (history state + helpers; `pushHistory` hooks in 4 server calls; `initHistory` in `loadAnnotator`; toolbar buttons + wiring; undo/redo keyboard shortcuts)

- [ ] **Step 1: Add history state to the `ann` object**

Find (~line 2336, the lines just added in Task 3):
```javascript
    selectedIds: new Set(),   // ids of all selected blocks
    anchorId: null,           // "primary" selected id (scroll target, table editor)
```
Replace with:
```javascript
    selectedIds: new Set(),   // ids of all selected blocks
    anchorId: null,           // "primary" selected id (scroll target, table editor)
    history: [],              // session-local stack of full block-list snapshots
    histPtr: -1,              // index in history matching current server state
    applyingHistory: false,   // guard: undo/redo restores must not push history
```

- [ ] **Step 2: Add the history helpers**

Immediately **before** `async function replaceBlocksOnServer(blocks)` (added in Task 5, ~line 3468), add:
```javascript
  // ── Undo / redo (session-local snapshot stack) ───────────────────────────
  const HIST_CAP = 50;

  function annSnapshot() {
    return ann.blocks.map(b => ({ ...b, bbox: b.bbox.slice() }));
  }

  function initHistory() {
    ann.history = [annSnapshot()];
    ann.histPtr = 0;
    updateUndoRedoButtons();
  }

  function pushHistory() {
    if (ann.applyingHistory) return;
    ann.history = ann.history.slice(0, ann.histPtr + 1);  // drop redo tail
    ann.history.push(annSnapshot());
    if (ann.history.length > HIST_CAP) ann.history.shift();
    ann.histPtr = ann.history.length - 1;
    updateUndoRedoButtons();
  }

  async function annUndo() {
    if (ann.applyingHistory) return;   // ignore overlapping undo/redo in flight
    if (ann.histPtr <= 0) return;
    const prev = ann.histPtr;
    ann.histPtr -= 1;
    ann.applyingHistory = true;
    const ok = await replaceBlocksOnServer(ann.history[ann.histPtr]);
    ann.applyingHistory = false;
    if (!ok) ann.histPtr = prev;       // revert pointer on failure
    updateUndoRedoButtons();
  }

  async function annRedo() {
    if (ann.applyingHistory) return;   // ignore overlapping undo/redo in flight
    if (ann.histPtr >= ann.history.length - 1) return;
    const prev = ann.histPtr;
    ann.histPtr += 1;
    ann.applyingHistory = true;
    const ok = await replaceBlocksOnServer(ann.history[ann.histPtr]);
    ann.applyingHistory = false;
    if (!ok) ann.histPtr = prev;
    updateUndoRedoButtons();
  }

  function updateUndoRedoButtons() {
    const u = $('ann-undo'), r = $('ann-redo');
    if (u) u.disabled = ann.histPtr <= 0;
    if (r) r.disabled = ann.histPtr >= ann.history.length - 1;
  }
```

- [ ] **Step 3: Initialize history on notice load**

In `loadAnnotator`, find (~line 2419):
```javascript
      await loadAnnSource(doc.public_url);
      paintAnnAll();
```
Replace with:
```javascript
      await loadAnnSource(doc.public_url);
      initHistory();
      paintAnnAll();
```

- [ ] **Step 4: Push history after each successful mutation**

Four insertions — each adds `pushHistory();` at the success tail of a server-call, **before** its repaint. Because `pushHistory` no-ops while `ann.applyingHistory` is true, undo/redo restores won't create new entries.

(a) In `createBlockOnServer`, find (~line 3388):
```javascript
      refreshMarkdownAfterChange();
      paintAnnAll();
    } catch (e) { annToast('add error: ' + e, 'err'); }
```
Replace with:
```javascript
      refreshMarkdownAfterChange();
      pushHistory();
      paintAnnAll();
    } catch (e) { annToast('add error: ' + e, 'err'); }
```

(b) In `updateBlockOnServer`, find (~line 3349):
```javascript
      mergeBlock(updated);
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
```
Replace with:
```javascript
      mergeBlock(updated);
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      pushHistory();
```
(This fires for label, text — including the inline table editor's silent saves — bbox move/resize, and order-input edits routed through `update`.)

(c) In `deleteBlockOnServer` (as rewritten in Task 3), find:
```javascript
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      paintAnnAll();
      return true;
```
Replace with:
```javascript
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      pushHistory();
      paintAnnAll();
      return true;
```

(d) In `reorderBlocksOnServer`, find (~line 3496):
```javascript
      ann.rev = doc.blocks_revision || ann.rev;
      $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      paintAnnAll();
```
Replace with:
```javascript
      ann.rev = doc.blocks_revision || ann.rev;
      $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      pushHistory();
      paintAnnAll();
```

- [ ] **Step 5: Add the toolbar buttons**

In the annotator toolbar, find (~line 567):
```html
      <button id="ann-add" class="ghost">+ Add block</button>
```
Replace with:
```html
      <button id="ann-add" class="ghost">+ Add block</button>
      <button id="ann-undo" class="ghost" title="undo (Ctrl+Z)" disabled>↶ Undo</button>
      <button id="ann-redo" class="ghost" title="redo (Ctrl+Shift+Z)" disabled>↷ Redo</button>
```

- [ ] **Step 6: Wire the buttons**

Find (~line 3580):
```javascript
  $('ann-add').addEventListener('click', () => {
```
Immediately **before** that line, add:
```javascript
  $('ann-undo').addEventListener('click', annUndo);
  $('ann-redo').addEventListener('click', annRedo);
```

- [ ] **Step 7: Add the undo/redo keyboard shortcuts**

Find the annotator keydown block added in Task 5:
```javascript
    // Annotator-screen shortcuts (Delete selection — undo/redo keys in Task 6)
    if (!$('screen-annotator').classList.contains('hidden') && !typing) {
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (ann.selectedIds.size) { e.preventDefault(); deleteSelectedBlocks(); }
        return;
      }
    }
```
Replace with:
```javascript
    // Annotator-screen shortcuts (Delete selection, Undo/Redo)
    if (!$('screen-annotator').classList.contains('hidden') && !typing) {
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (ann.selectedIds.size) { e.preventDefault(); deleteSelectedBlocks(); }
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z') && !e.shiftKey) {
        e.preventDefault(); annUndo(); return;
      }
      if ((e.ctrlKey || e.metaKey) &&
          (((e.key === 'z' || e.key === 'Z') && e.shiftKey) || e.key === 'y' || e.key === 'Y')) {
        e.preventDefault(); annRedo(); return;
      }
    }
```

- [ ] **Step 8: Verify in the browser**

Reload the annotator. Confirm:
- On load, both toolbar buttons are **disabled** (`↶ Undo` / `↷ Redo`).
- Add a block (drag in "+ Add block" mode) → Undo enables. Press **Undo** (or Ctrl+Z) → the block disappears, Redo enables. Press **Redo** (or Ctrl+Shift+Z, or Ctrl+Y) → it reappears.
- Move a block, then Undo → it returns to its prior position. Relabel a block, Undo → label reverts.
- Multi-select 3 blocks, Delete, then Undo → all three reappear **with their original ids/labels/text** (the inline editor still works on them). Redo → gone again.
- Edit a block's text, click away, Undo → text reverts. While the textarea is focused, **Ctrl+Z does native text undo** (does not fire block-level undo).
- After an undo, making a new edit drops the redo tail (Redo becomes disabled).
- Buttons disable correctly at both ends of the stack; `rev` advances on each undo/redo.
- No console errors.

- [ ] **Step 9: Commit**

```bash
git add web/review.html
git commit -m "feat(review): undo/redo for annotator block edits (stack + buttons + shortcuts)"
```

---

## Final verification

- [ ] Backend tests green: `pytest tests/api/test_review_replace_blocks.py -v` and the existing block tests: `pytest tests/api/test_review_rotation.py tests/api/test_review_markdown.py -q`.
- [ ] `grep -n "ann.selectedId\b" web/review.html` returns nothing (all converted).
- [ ] Full manual pass of the three features per the per-task browser checks above.
- [ ] Optionally run the project's `/qa` skill against the annotator for a structured regression sweep.

## Notes for the implementer

- **DRY:** `replaceBlocksOnServer` is the single write path for multi-delete and undo/redo; do not duplicate its fetch logic.
- **YAGNI:** No group move/resize, no crop/rotation undo, no server-side history — explicitly out of scope (see spec).
- **Ordering matters:** Task 5 references `pushHistory` (defined in Task 6). Run them back-to-back; do not ship Task 5 to users on its own.
- **Coordinate system:** block `bbox` and the marquee rect are both normalized `[x0,y0,x1,y1]` in `[0,1]` full-image coords, so the intersection test needs no conversion.
