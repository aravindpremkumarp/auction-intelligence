# Inline Table Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken full-screen Table Editor modal and the flat inline table preview with ONE span-correct, single-click-editable table editor that lives inline in the annotation sidebar.

**Architecture:** Reuse the existing span-aware table model + operations (`tbled*` functions in `web/review.html`); change only *where* they render — from a full-screen overlay to a container inside the selected Table block's sidebar row. Fix the root-cause bug where every click/save rebuilds the DOM and destroys the editing caret, by (a) updating selection in place, (b) re-rendering only the editor's own subtree on structural ops, and (c) adding a `silent` save path to `updateBlockOnServer` that skips the sidebar/overlay repaint.

**Tech Stack:** Vanilla JS + DOM APIs in a single file (`web/review.html`), `DOMParser`/`DOMPurify` for table HTML, FastAPI backend (untouched — same `PUT /review/notice/{filename}/blocks/{id}` with a `text` field).

---

## Testing approach (read first)

`web/review.html` is a single-file vanilla-JS frontend with **no JS test harness**, and the approved spec (`docs/superpowers/specs/2026-06-01-inline-table-editor-design.md`) explicitly scopes a harness OUT. So the standard write-failing-test-first loop does not apply here. Instead, **every task ends with an explicit manual browser verification** plus a commit. Two ways to run the verification:

- **Live review UI** the user already runs (it serves `web/review.html` against the API). Open a notice that has a **Table** block (e.g. a Schedule-A table with merged "Location of property" / "Boundaries" headers).
- If you have the project run skill, use it: invoke `/run` (or the `gstack` / `browse` skill) to open the review page and drive the checks.

A "smoke check" (page loads, no console errors) is part of every task. Treat a red console error as a failing test — stop and fix before committing.

> **Line numbers** below reflect the file as of 2026-06-01. They drift as you edit. Always locate code by **function name / selector**, using the line number only as a hint.

---

## File structure

All changes are in **one file**: `web/review.html`. No new files; no backend changes.

Logical regions of `web/review.html` touched:

- **CSS** (`<style>`, ~lines 280–357): remove the flat `.ann-tbl*` preview styles and the `.tbled-overlay` modal styles; add `.tbled-inline` styles; keep `.table-editor` (grid-positions), `.ann-tbl-html-toggle`, and `.ann-table-hints`.
- **Server call** `updateBlockOnServer` (~3563): add a `silent` option.
- **Sidebar row** `renderSidebarRow` (~2962) + `paintAnnSidebar` (~2948): swap the Table branch to read-only-preview (unselected) / inline-editor (selected); remove the "✎ Edit table" button.
- **Flat preview** `renderAnnTablePreview`/`_annParseTableHtml`/`_annSerializeTableHtml` (~3130–3342): delete.
- **Table model + editor** `tbled*` (~4096–4796): keep the model/ops; refactor `tbledBuildGrid(rows)`; retarget the paint functions to an inline container; change interaction to single-click-edit + in-place selection; add `tbledMountInline`, `tbledRenderReadonly`, `tbledRepaintSelection`, `tbledPersist`; delete the modal lifecycle (`tbledMountOnce`/`tbledOpen`/`tbledClose`/`tbledSave`/`tbledEnsureMounted` + overlay keydown).

---

## Task 1: Add a `silent` save path to `updateBlockOnServer`

**Files:**
- Modify: `web/review.html` — `updateBlockOnServer` (~3563–3585)

**Why:** On success, `updateBlockOnServer` calls `paintAnnSidebar()` + `paintAnnOverlay()`, which wipe and rebuild the sidebar — destroying any inline editor and its caret. Inline cell/structural commits must persist WITHOUT that repaint.

- [ ] **Step 1: Add the `opts` parameter and gate the repaints**

Replace the function signature and the success branch. Find:

```javascript
  async function updateBlockOnServer(id, patch, beforeSnapshot) {
    try {
      const r = await authFetch(
        API + '/review/notice/' + encodeURIComponent(ann.filename)
            + '/blocks/' + encodeURIComponent(id),
        { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch) }
      );
      if (r.status === 409) { annToast('conflict, reloading', 'err'); await loadAnnotator(ann.filename); return false; }
      if (!r.ok) { annToast('save failed (' + r.status + ')', 'err'); rollback(id, beforeSnapshot); return false; }
      const updated = await r.json();
      mergeBlock(updated);
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      paintAnnSidebar();
      paintAnnOverlay();
      return true;
    } catch (e) {
      annToast('save error: ' + e, 'err');
      rollback(id, beforeSnapshot);
      return false;
    }
  }
```

Replace with:

```javascript
  // opts.silent: skip the sidebar/overlay repaint after a successful save.
  // Used by the inline table editor, whose DOM + caret must survive a save.
  // (Text edits never move the bbox, so the overlay doesn't need repainting.)
  async function updateBlockOnServer(id, patch, beforeSnapshot, opts) {
    opts = opts || {};
    try {
      const r = await authFetch(
        API + '/review/notice/' + encodeURIComponent(ann.filename)
            + '/blocks/' + encodeURIComponent(id),
        { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch) }
      );
      if (r.status === 409) { annToast('conflict, reloading', 'err'); await loadAnnotator(ann.filename); return false; }
      if (!r.ok) { annToast('save failed (' + r.status + ')', 'err'); rollback(id, beforeSnapshot); return false; }
      const updated = await r.json();
      mergeBlock(updated);
      ann.rev += 1; $('ann-rev').textContent = 'rev ' + ann.rev;
      refreshMarkdownAfterChange();
      if (!opts.silent) {
        paintAnnSidebar();
        paintAnnOverlay();
      }
      return true;
    } catch (e) {
      annToast('save error: ' + e, 'err');
      rollback(id, beforeSnapshot);
      return false;
    }
  }
```

- [ ] **Step 2: Smoke-verify**

Open the review UI, open a notice, edit a **non-table** block's text (existing `textarea` path), confirm it still saves and the sidebar still repaints (existing callers pass no `opts`, so behavior is unchanged). No console errors.

- [ ] **Step 3: Commit**

```bash
git add web/review.html
git commit -m "feat(review): add silent save option to updateBlockOnServer"
```

---

## Task 2: Parameterize `tbledBuildGrid(rows)`

**Files:**
- Modify: `web/review.html` — `tbledBuildGrid` (~4148) and its call sites

**Why:** `tbledBuildGrid` currently reads the global `tbled.rows`. The read-only preview (Task 3) needs to build a physical grid from an arbitrary parsed table, not the active editor's. Make `rows` a parameter.

- [ ] **Step 1: Change the signature**

Find `function tbledBuildGrid() {` and its body line `const rows = tbled.rows;`. Replace the header + that line:

```javascript
  // Build a 2D "physical" grid (visual layout) from a logical rows model.
  // `rows` defaults to the active editor's rows so existing callers can pass
  // nothing; the read-only preview passes its own parsed rows.
  function tbledBuildGrid(rows) {
    const physical = [];
    rows = rows || tbled.rows;
```

(Delete the now-duplicate `const rows = tbled.rows;` line that followed `const physical = [];`.)

- [ ] **Step 2: Pass `tbled.rows` explicitly at the active-editor call sites**

These callers operate on the active editor and currently rely on the global. Update each `tbledBuildGrid()` call to `tbledBuildGrid(tbled.rows)`:

- in `tbledToLatex` (~4255): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledApplyFormat` (~4296): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledApplyAlign` (~4316): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledCanMerge` (~4339): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledMerge` (~4361): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledCanSplit` (~4402): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledSplit` (~4412): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledAddRow` (~4434): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledDelRow` (~4475): `const grid = tbledBuildGrid(tbled.rows);`
- in `tbledDelCol` (~4492): `const grid = tbledBuildGrid(tbled.rows);`
- in `paintTbledGrid` (~4619): `const grid = tbledBuildGrid(tbled.rows);`
- in `paintTbledStatus` (~4743): `const grid = tbledBuildGrid(tbled.rows);`

> Use Grep for `tbledBuildGrid()` to confirm none are missed.

- [ ] **Step 3: Smoke-verify**

The modal still opens via "✎ Edit table" (not removed yet) and behaves exactly as before (merge/split/add/del work). No console errors. This is a pure refactor — behavior must be identical.

- [ ] **Step 4: Commit**

```bash
git add web/review.html
git commit -m "refactor(review): parameterize tbledBuildGrid(rows)"
```

---

## Task 3: Add a read-only span-correct table renderer

**Files:**
- Modify: `web/review.html` — add `tbledRenderReadonly` near the other `tbled*` helpers (e.g. right after `tbledBuildGrid`)

**Why:** Unselected Table rows must render with correct `rowspan`/`colspan` (complaint #2) without any editing affordances.

- [ ] **Step 1: Add the renderer**

```javascript
  // Build a static <table> DOM (spans honored, no listeners, not editable)
  // from a parsed logical rows model. Used for UNSELECTED Table rows so the
  // sidebar always shows the same structure the editor shows.
  function tbledRenderReadonly(rows) {
    const grid = tbledBuildGrid(rows);
    const tbl = document.createElement('table');
    tbl.className = 'tbled-grid readonly';
    for (let r = 0; r < grid.nRows; r++) {
      const tr = document.createElement('tr');
      for (let c = 0; c < grid.nCols; c++) {
        const ph = grid.physical[r][c];
        if (!ph) { tr.appendChild(document.createElement('td')); continue; }
        if (!ph.primary) continue; // covered by an upstream span
        const cell = rows[ph.lr][ph.li];
        const td = document.createElement(cell.tag === 'th' ? 'th' : 'td');
        if ((cell.rowspan || 1) > 1) td.setAttribute('rowspan', String(cell.rowspan));
        if ((cell.colspan || 1) > 1) td.setAttribute('colspan', String(cell.colspan));
        if (cell.align) td.style.textAlign = cell.align;
        const dirty = cell.html || '';
        td.innerHTML = window.DOMPurify
          ? window.DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } })
          : dirty;
        tr.appendChild(td);
      }
      tbl.appendChild(tr);
    }
    const wrap = document.createElement('div');
    wrap.className = 'tbled-inline-readonly';
    wrap.appendChild(tbl);
    return wrap;
  }
```

- [ ] **Step 2: Smoke-verify**

No behavior change yet (function is not wired in). Confirm the page still loads with no console errors (the function parses fine).

- [ ] **Step 3: Commit**

```bash
git add web/review.html
git commit -m "feat(review): add read-only span-correct table renderer"
```

---

## Task 4: Build the inline editor core (mount + retargeted paint)

**Files:**
- Modify: `web/review.html` — the `tbled` state object (~4102), add `tbledMountInline`; retarget `paintTbledToolbar` / `paintTbledGrid` / `paintTbledTex` / `paintTbledStatus` / `paintTbled` to render into `tbled.container` instead of fixed modal element IDs.

**Why:** The editor must render inside a sidebar container, not a full-screen overlay. This task makes the paint functions container-relative and adds the mount entry point. (Interaction changes come in Task 5; for now keep the existing click/dblclick handlers so the editor is verifiable in isolation.)

- [ ] **Step 1: Extend the `tbled` state object**

Find the `const tbled = {...}` block (~4102) and replace it with:

```javascript
  // Single active inline table editor (only the selected Table block has one).
  const tbled = {
    block:       null,   // the live block object this editor is bound to
    blockId:     null,   // stable id for re-finding the block after a save
    container:   null,   // the .tbled-inline div inside the selected sidebar row
    rows:        [],     // 2D array of cell objects { html, tag, rowspan, colspan, align }
    past:        [],
    future:      [],
    selection:   null,   // { r0, c0, r1, c1 } in physical-grid coords or null
    showTex:     false,
  };
```

- [ ] **Step 2: Add `tbledMountInline` (builds the container skeleton + first paint)**

Add near `tbledParseHtml` (replace the old `tbledOpen`/`tbledClose` region in Task 8; for now just add this new function):

```javascript
  // Mount the inline editor into `container` (a .tbled-inline div that already
  // lives inside the selected Table block's sidebar row). Builds the toolbar /
  // grid / tex / status skeleton once, then paints.
  function tbledMountInline(b, container) {
    tbled.block     = b;
    tbled.blockId   = b.id;
    tbled.container = container;
    tbled.rows      = tbledParseHtml(b.text || '');
    tbled.past      = [];
    tbled.future    = [];
    tbled.selection = null;
    tbled.showTex   = false;
    container.innerHTML = '';
    const toolbar = document.createElement('div');
    toolbar.className = 'tbled-toolbar';
    const gridWrap = document.createElement('div');
    gridWrap.className = 'tbled-grid-wrap';
    const tex = document.createElement('div');
    tex.className = 'tbled-tex-panel';
    tex.style.display = 'none';
    const status = document.createElement('div');
    status.className = 'tbled-status';
    container.appendChild(toolbar);
    container.appendChild(gridWrap);
    container.appendChild(tex);
    container.appendChild(status);
    // Structural undo/redo via keyboard, but only when NOT typing in a cell
    // (so in-cell Ctrl+Z stays browser text-undo).
    container.addEventListener('keydown', (e) => {
      const editing = document.activeElement
        && document.activeElement.classList
        && document.activeElement.classList.contains('editing');
      if (editing) return;
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key === 'z') {
        e.preventDefault(); tbledUndo(); return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'y' ||
          (e.shiftKey && e.key.toLowerCase() === 'z'))) {
        e.preventDefault(); tbledRedo(); return;
      }
    });
    paintTbled();
  }
```

- [ ] **Step 3: Retarget the paint functions to `tbled.container`**

Replace `paintTbledToolbar`'s first two lines. Find:

```javascript
  function paintTbledToolbar() {
    const bar = document.getElementById('tbled-toolbar');
    bar.innerHTML = '';
```

Replace with:

```javascript
  function paintTbledToolbar() {
    if (!tbled.container) return;
    const bar = tbled.container.querySelector('.tbled-toolbar');
    if (!bar) return;
    bar.innerHTML = '';
```

Replace `paintTbledGrid`'s first two lines. Find:

```javascript
  function paintTbledGrid() {
    const wrap = document.getElementById('tbled-grid-wrap');
    wrap.innerHTML = '';
```

Replace with:

```javascript
  function paintTbledGrid() {
    if (!tbled.container) return;
    const wrap = tbled.container.querySelector('.tbled-grid-wrap');
    if (!wrap) return;
    wrap.innerHTML = '';
```

Replace `paintTbledTex`'s element lookup. Find:

```javascript
  function paintTbledTex() {
    const panel = document.getElementById('tbled-tex-panel');
    if (!tbled.showTex) { panel.style.display = 'none'; return; }
```

Replace with:

```javascript
  function paintTbledTex() {
    if (!tbled.container) return;
    const panel = tbled.container.querySelector('.tbled-tex-panel');
    if (!panel) return;
    if (!tbled.showTex) { panel.style.display = 'none'; return; }
```

Replace `paintTbledStatus`'s element lookup. Find:

```javascript
  function paintTbledStatus() {
    const status = document.getElementById('tbled-status');
    const grid = tbledBuildGrid(tbled.rows);
```

Replace with:

```javascript
  function paintTbledStatus() {
    if (!tbled.container) return;
    const status = tbled.container.querySelector('.tbled-status');
    if (!status) return;
    const grid = tbledBuildGrid(tbled.rows);
```

> `paintTbled()` itself (calls the four above) needs no change.

- [ ] **Step 4: Smoke-verify (temporary wiring)**

To verify in isolation before the sidebar rewrite, temporarily make the "✎ Edit table" button mount inline instead of opening the modal. In `renderSidebarRow` find:

```javascript
      btnEditTable.addEventListener('click', () => {
        tbledEnsureMounted();
        tbledOpen(b);
      });
```

Temporarily replace with:

```javascript
      btnEditTable.addEventListener('click', () => {
        let c = row.querySelector('.tbled-inline');
        if (!c) { c = document.createElement('div'); c.className = 'tbled-inline'; row.appendChild(c); }
        tbledMountInline(b, c);
      });
```

Open a Table block, click "✎ Edit table" → the editor (toolbar + grid + status) renders **inline in the sidebar row** with correct merged structure. Existing click=select / dblclick=edit still work for now. No console errors.

> Keep this temporary wiring; Task 7 replaces it with the real select-driven mount.

- [ ] **Step 5: Commit**

```bash
git add web/review.html
git commit -m "feat(review): render table editor inline via tbledMountInline"
```

---

## Task 5: Single-click editing + in-place selection + autosave on blur

**Files:**
- Modify: `web/review.html` — `paintTbledGrid` cell handlers (~4659–4713); add `tbledRepaintSelection` and `tbledPersist`.

**Why:** This is the core fix for "can't edit text": single-click enters edit immediately, and selection changes update classes **in place** (no rebuild) so the caret survives. Edits autosave silently.

- [ ] **Step 1: Add `tbledPersist` (silent save of the current model)**

Add near `tbledMountInline`:

```javascript
  // Serialize the active editor and save it WITHOUT repainting the sidebar
  // (so the editor + caret survive). Re-points tbled.block to the merged
  // server object so later saves snapshot fresh state.
  function tbledPersist() {
    if (!tbled.blockId) return;
    const live = ann.blocks.find(b => b.id === tbled.blockId) || tbled.block;
    if (!live) return;
    const html = tbledSerialize(tbled.rows);
    const snapshot = { ...live, bbox: live.bbox.slice() };
    live.text = html;
    tbled.block = live;
    updateBlockOnServer(tbled.blockId, { text: html }, snapshot, { silent: true })
      .then(() => {
        const fresh = ann.blocks.find(b => b.id === tbled.blockId);
        if (fresh) tbled.block = fresh;
      });
  }
```

- [ ] **Step 2: Add `tbledRepaintSelection` (toggle `.selected` classes in place)**

Add near `tbledPersist`. This reads `data-r0/c0/r1/c1` written by the grid renderer (Step 3) and toggles highlight without rebuilding:

```javascript
  // Update which cells/handles look selected, mutating classes in place so an
  // open contenteditable caret is never destroyed. Also refreshes the toolbar
  // (merge/split enablement depends on the selection) — the toolbar has no
  // caret so rebuilding it is safe.
  function tbledRepaintSelection() {
    if (!tbled.container) return;
    const rect = tbledRectFromSelection(tbled.selection);
    tbled.container.querySelectorAll('td[data-r0], th[data-r0]').forEach(td => {
      const r0 = +td.dataset.r0, c0 = +td.dataset.c0,
            r1 = +td.dataset.r1, c1 = +td.dataset.c1;
      const sel = !!rect && !(r1 < rect.r0 || r0 > rect.r1 || c1 < rect.c0 || c0 > rect.c1);
      td.classList.toggle('selected', sel);
    });
    const full = rect && tbled.container.querySelector('.tbled-grid');
    tbled.container.querySelectorAll('.tbled-handle-cell.col').forEach((th, i) => {
      th.classList.toggle('col-selected',
        !!rect && rect.c0 <= i && rect.c1 >= i && rect.r0 === 0 && full
        && rect.r1 === (tbledBuildGrid(tbled.rows).nRows - 1));
    });
    tbled.container.querySelectorAll('tr > .tbled-handle-cell:not(.col):not(.tbled-corner)').forEach((rh, i) => {
      rh.classList.toggle('row-selected',
        !!rect && rect.r0 <= i && rect.r1 >= i && rect.c0 === 0
        && rect.c1 === (tbledBuildGrid(tbled.rows).nCols - 1));
    });
    paintTbledToolbar();
    paintTbledStatus();
  }
```

- [ ] **Step 3: Tag rendered cells with physical coords + replace cell handlers**

In `paintTbledGrid`, the data-cell branch currently creates `td`, sets spans/align/html, adds `.selected`, then wires `click` / `dblclick` / `blur` / `keydown`. Replace from `const cell = tbled.rows[ph.lr][ph.li];` through the end of the `td.addEventListener('keydown', ...)` block with:

```javascript
        const cell = tbled.rows[ph.lr][ph.li];
        const td = document.createElement(cell.tag === 'th' ? 'th' : 'td');
        const rs = cell.rowspan || 1, cs = cell.colspan || 1;
        if (rs > 1) td.setAttribute('rowspan', String(rs));
        if (cs > 1) td.setAttribute('colspan', String(cs));
        if (cell.align) td.style.textAlign = cell.align;
        // Physical footprint, used by tbledRepaintSelection for in-place highlight.
        td.dataset.r0 = String(r);  td.dataset.c0 = String(c);
        td.dataset.r1 = String(r + rs - 1); td.dataset.c1 = String(c + cs - 1);
        const dirty = cell.html || '';
        td.innerHTML = window.DOMPurify
          ? window.DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } })
          : dirty;
        if (tbledIsCellSelected(r, c)) td.classList.add('selected');

        // Commit a cell that is currently being edited (used before we move
        // focus/selection elsewhere).
        const commitEditing = () => {
          if (!td.classList.contains('editing')) return;
          const fresh = td.innerHTML;
          td.removeAttribute('contenteditable');
          td.classList.remove('editing');
          if (fresh !== cell.html) { cell.html = fresh; tbledPersist(); }
        };

        td.addEventListener('click', (e) => {
          if (e.shiftKey && tbled.selection) {
            // Extend a range for merge/format/align — never enter edit.
            const active = tbled.container.querySelector('.editing');
            if (active) active.blur();
            tbled.selection = { r0: tbled.selection.r0, c0: tbled.selection.c0, r1: r, c1: c };
            tbledRepaintSelection();
            return;
          }
          // Plain click → select this single cell AND start editing immediately.
          const active = tbled.container.querySelector('.editing');
          if (active && active !== td) active.blur();
          tbled.selection = { r0: r, c0: c, r1: r, c1: c };
          tbledRepaintSelection();
          if (!td.classList.contains('editing')) {
            td.setAttribute('contenteditable', 'true');
            td.classList.add('editing');
            td.focus();
          }
        });
        td.addEventListener('blur', commitEditing);
        td.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') { td.blur(); }
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); td.blur(); }
        });
        tr.appendChild(td);
```

> This removes the old `dblclick` handler entirely (single-click now edits) and routes blur through `commitEditing` → `tbledPersist()` (silent save, no rebuild).

- [ ] **Step 4: Verify editing + persistence**

In the review UI, open a Table block, mount the editor (temp "✎ Edit table" button from Task 4), then:
1. **Single-click** the "Koi" cell → caret appears immediately → type to make it "Koil" → click another cell → "Koil" persists; the grid does **not** flicker/rebuild and you don't lose focus.
2. **Shift-click** a second cell → a rectangular range highlights (no edit), and the Merge button enables when valid.
3. Reload the notice → "Koil" is still there (round-tripped through the server).
4. No console errors.

- [ ] **Step 5: Commit**

```bash
git add web/review.html
git commit -m "feat(review): single-click cell editing with in-place selection + silent autosave"
```

---

## Task 6: Persist structural ops + undo/redo

**Files:**
- Modify: `web/review.html` — `tbledCommit` (~4118), `tbledUndo` (~4129), `tbledRedo` (~4137)

**Why:** Merge/split/add/del/undo/redo change the model; they must (a) re-render only the editor subtree (safe — no caret), and (b) silently persist. `tbledCommit` already calls `paintTbled()` (which now renders into the container). Add persistence.

- [ ] **Step 1: Persist on structural commit**

Find `tbledCommit`:

```javascript
  function tbledCommit(nextRows, opts) {
    opts = opts || {};
    if (!opts.skipHistory) {
      tbled.past.push(tbledCloneRows(tbled.rows));
      if (tbled.past.length > TBLED_HIST_CAP) tbled.past.shift();
      tbled.future = [];
    }
    tbled.rows = nextRows;
    paintTbled();
  }
```

Replace the last two lines (`tbled.rows = nextRows;` / `paintTbled();`) so it persists:

```javascript
    tbled.rows = nextRows;
    paintTbled();
    tbledPersist();
  }
```

- [ ] **Step 2: Persist on undo/redo**

Find `tbledUndo` and `tbledRedo` and add `tbledPersist();` after their `paintTbled();` lines:

```javascript
  function tbledUndo() {
    if (!tbled.past.length) return;
    tbled.future.push(tbledCloneRows(tbled.rows));
    tbled.rows = tbled.past.pop();
    tbled.selection = null;
    paintTbled();
    tbledPersist();
  }

  function tbledRedo() {
    if (!tbled.future.length) return;
    tbled.past.push(tbledCloneRows(tbled.rows));
    tbled.rows = tbled.future.pop();
    tbled.selection = null;
    paintTbled();
    tbledPersist();
  }
```

- [ ] **Step 3: Verify**

In the editor: select a cell/range, click **Merge**, **Split**, **+ Row ↓**, **+ Col →**, **🗑 Row**, **🗑 Col**, then **Undo**/**Redo**. Each updates the grid and the change survives a reload. Apply **B**/**align** to a selected range and confirm it persists. No console errors.

- [ ] **Step 4: Commit**

```bash
git add web/review.html
git commit -m "feat(review): persist table structural ops and undo/redo"
```

---

## Task 7: Wire the sidebar (read-only unselected, inline editor selected) + remove the flat preview

**Files:**
- Modify: `web/review.html` — `renderSidebarRow` Table branch (~3005–3008 and the `btnEditTable` block ~3061–3068, ~3116–3121); `paintAnnSidebar` (~2948); delete `renderAnnTablePreview`/`_annParseTableHtml`/`_annSerializeTableHtml` (~3130–3342).

**Why:** Replace the flat editable preview with: read-only span-correct render for unselected Table rows, and a mounted inline editor for the selected one. Remove the now-unused modal opener.

- [ ] **Step 1: Replace the table preview block in `renderSidebarRow`**

Find:

```javascript
    let textArea = null;
    if (b.label === 'Table') {
      const tableWrap = renderAnnTablePreview(b);
      if (tableWrap) row.appendChild(tableWrap);
    }
```

Replace with:

```javascript
    let textArea = null;
    if (b.label === 'Table') {
      if (ann.selectedId === b.id) {
        // Selected Table → full inline editor, mounted after the row lands in
        // the DOM (focus() needs an attached element). paintAnnSidebar does the
        // mount; here we just leave the container placeholder.
        const ed = document.createElement('div');
        ed.className = 'tbled-inline';
        row.appendChild(ed);
      } else {
        // Unselected Table → read-only span-correct preview.
        row.appendChild(tbledRenderReadonly(tbledParseHtml(b.text || '')));
      }
    }
```

- [ ] **Step 2: Remove the "✎ Edit table" button (creation + handler)**

Find and delete the creation block (inside the `if (b.label === 'Table')` actions area, ~3061–3068):

```javascript
    let btnEditTable = null;
    if (b.label === 'Table') {
      btnEditTable = document.createElement('button');
      btnEditTable.className = 'primary';
      btnEditTable.textContent = '✎ Edit table';
      btnEditTable.title = 'Open full-screen editor (cell merge/split, formatting, TeX)';
      actions.appendChild(btnEditTable);
    }
```

And delete the handler block (~3116–3121, including the temporary wiring you added in Task 4 Step 4 — whichever is currently there):

```javascript
    if (btnEditTable) {
      btnEditTable.addEventListener('click', () => {
        ...
      });
    }
```

- [ ] **Step 3: Mount the inline editor in `paintAnnSidebar`**

Find `paintAnnSidebar`:

```javascript
  function paintAnnSidebar() {
    const root = $('ann-rows');
    root.innerHTML = '';
    const sorted = ann.blocks.slice().sort((a, b) => a.reading_order - b.reading_order);
    if (!sorted.length) {
      root.innerHTML = '<div class="empty">no blocks yet. press “+ Add block” and marquee-draw a region.</div>';
      return;
    }
    const total = sorted.length;
    for (let i = 0; i < total; i++) {
      root.appendChild(renderSidebarRow(sorted[i], i + 1, total, sorted));
    }
  }
```

Replace with (adds the post-append mount of the selected Table editor):

```javascript
  function paintAnnSidebar() {
    const root = $('ann-rows');
    root.innerHTML = '';
    const sorted = ann.blocks.slice().sort((a, b) => a.reading_order - b.reading_order);
    if (!sorted.length) {
      root.innerHTML = '<div class="empty">no blocks yet. press “+ Add block” and marquee-draw a region.</div>';
      return;
    }
    const total = sorted.length;
    for (let i = 0; i < total; i++) {
      root.appendChild(renderSidebarRow(sorted[i], i + 1, total, sorted));
    }
    // Mount the inline table editor into the selected Table block's row (it
    // must be in the DOM first so cell .focus() works).
    const sel = ann.selectedId && ann.blocks.find(b => b.id === ann.selectedId);
    if (sel && sel.label === 'Table') {
      const rowEl = root.querySelector(`.ann-row[data-id="${sel.id}"]`);
      const cont = rowEl && rowEl.querySelector('.tbled-inline');
      if (cont) tbledMountInline(sel, cont);
    } else {
      tbled.block = null; tbled.blockId = null; tbled.container = null;
    }
  }
```

- [ ] **Step 4: Delete the flat preview functions**

Delete `_annParseTableHtml` (~3130), `_annSerializeTableHtml` (~3165), and `renderAnnTablePreview` (~3177–3342) in full. Grep for `renderAnnTablePreview`, `_annParseTableHtml`, `_annSerializeTableHtml` afterwards to confirm zero remaining references.

- [ ] **Step 5: Verify the full select-driven flow**

1. Open a notice with several blocks including a Table. Unselected Table rows show a **read-only** table with correct merged headers.
2. **Click** the Table block (in the sidebar or on the scan) → its row expands into the full editor (toolbar + grid + status), and the scan on the left scrolls to that block.
3. Single-click a cell → edit; merge/split/add/del/undo/redo; align/format. All persist; reload confirms.
4. Select a **different** block → the editor unmounts cleanly (no leftover editor, no console error).
5. No "✎ Edit table" button anywhere. No console errors.

- [ ] **Step 6: Commit**

```bash
git add web/review.html
git commit -m "feat(review): inline table editor on select; remove flat preview + Edit-table button"
```

---

## Task 8: Remove the full-screen modal + CSS cleanup + inline CSS

**Files:**
- Modify: `web/review.html` — delete modal functions (`tbledOpen` ~4197, `tbledClose` ~4209, `tbledMountOnce` ~4519, `tbledSave` ~4763, `tbledEnsureMounted` ~4794) and the modal keydown handler; remove CSS for `.ann-tbl*` (~283–307) and `.tbled-overlay/.tbled-header/.tbled-body` (~325–357 minus the grid styles we keep); add `.tbled-inline` CSS.

**Why:** The modal is now unreachable dead code; its CSS and the flat-preview CSS are unused. Add styling so the inline editor looks right in the narrow sidebar.

- [ ] **Step 1: Delete the modal lifecycle functions**

Delete these functions in full (they are no longer referenced after Task 7):
- `tbledOpen(b)` (~4197–4207)
- `tbledClose()` (~4209–4213)
- `tbledMountOnce()` (~4519–4552) — includes the `#tbled-overlay` HTML string and the document-level `keydown` listener with `tbledClose`/Esc/undo/redo.
- `tbledSave()` (~4763–4790)
- `tbledEnsureMounted()` (~4794–4796)

Grep for `tbled-overlay`, `tbledOpen`, `tbledClose`, `tbledMountOnce`, `tbledSave`, `tbledEnsureMounted` and confirm zero references remain.

> Keep ALL of: `tbledParseHtml`, `tbledSerialize`, `tbledBuildGrid`, `tbledCloneRows`, `tbledCommit`, `tbledUndo`, `tbledRedo`, `tbledRectFromSelection`, `tbledIsCellSelected`, `tbledApplyFormat`, `tbledApplyAlign`, `tbledCanMerge`, `tbledMerge`, `tbledCanSplit`, `tbledSplit`, `tbledAddRow`, `tbledAddCol`, `tbledDelRow`, `tbledDelCol`, `tbledToLatex`, `tbledToolbarBtn`, `paintTbled*`, `tbledMountInline`, `tbledPersist`, `tbledRepaintSelection`, `tbledRenderReadonly`.

- [ ] **Step 2: Remove the flat-preview CSS**

Delete CSS lines for the flat preview — the block from the comment `/* Rendered table preview with hover-triggered +/× affordances ... */` through `.ann-tbl-toolbar .hint { ... }` (~283–307). **Keep** `.table-editor` rules (~280–282, the grid-positions UI) and `.ann-tbl-html-toggle` (~308) and `.ann-table-hints*` (~309–324).

- [ ] **Step 3: Remove the modal CSS and add inline CSS**

Delete the modal-only CSS: the comment block + `.tbled-overlay`, `.tbled-overlay.open`, `.tbled-header*`, `.tbled-body` (~325–334 and 342). **Keep and reuse** the grid styles `.tbled-toolbar*`, `.tbled-grid*`, `.tbled-tex`, `.tbled-status` (~335–357) — the inline editor uses these same class names. Then add inline-scoping styles right after `.tbled-status` (~357):

```css
  /* Inline table editor — lives in the selected Table block's sidebar row.
     Reuses .tbled-toolbar / .tbled-grid / .tbled-tex / .tbled-status. */
  .tbled-inline { margin-top: 6px; border: 1.5px solid var(--ink); background: #fff; }
  .tbled-inline .tbled-toolbar { padding: 5px 6px; gap: 3px; border-bottom: 1.5px solid var(--ink); }
  .tbled-inline .tbled-toolbar button { padding: 2px 6px; font-size: 12px; min-width: 26px; }
  .tbled-inline .tbled-grid-wrap { padding: 8px; overflow: auto; max-height: 360px; }
  .tbled-inline .tbled-grid td { padding: 4px 6px; font-size: 11px; min-width: 44px; }
  .tbled-inline .tbled-status { padding: 4px 7px; border-top: 1px solid rgba(0,0,0,0.15); }
  .tbled-inline .tbled-tex-panel { padding: 0 8px 8px; }
  /* Read-only preview for unselected Table rows. */
  .tbled-inline-readonly { margin-top: 6px; overflow: auto; max-height: 320px; border: 1.5px solid var(--ink); background: #fff; padding: 8px; }
  .tbled-inline-readonly .tbled-grid.readonly td,
  .tbled-inline-readonly .tbled-grid.readonly th { border: 1px solid var(--ink); padding: 3px 6px; font-size: 11px; font-family: 'IBM Plex Mono', monospace; vertical-align: top; }
  .tbled-inline-readonly .tbled-grid.readonly th { background: #f3f0e7; font-weight: 700; }
```

- [ ] **Step 4: Verify**

1. Page loads; no console error about a missing `#tbled-overlay` / `#tbled-grid-wrap` / `tbledOpen`.
2. Inline editor renders with a tidy toolbar + grid that fits the sidebar (resize the sidebar via the divider; the grid scrolls horizontally for wide tables).
3. Read-only unselected Table rows render with bordered, header-shaded merged cells.
4. Full edit flow from Task 7 still works.

- [ ] **Step 5: Commit**

```bash
git add web/review.html
git commit -m "refactor(review): remove full-screen table modal + dead CSS; style inline editor"
```

---

## Task 9: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full acceptance checklist (from the spec)**

In the review UI against a notice with a Schedule-style Table block (merged "Location of property" / "Boundaries" headers):

1. Unselected Table rows show the **correct merged structure** (complaint #2 fixed) — compare to the scan on the left.
2. Select the Table block → inline editor shows the same merged structure; scan auto-scrolls to the block.
3. **Single-click "Koi" → type "Koil" → click away** → persists; **no flicker, no lost focus** (complaint #1 fixed).
4. Merge, split, add row/col, delete row/col, undo, redo — all behave and persist.
5. B/I/U/sup/sub and align L/C/R apply to the focused cell and to a shift-selected range.
6. Toggle **TeX** preview → LaTeX shows; **copy** works.
7. "edit raw HTML", "Table grid" row/col positions, "Table Hints" +/- steppers, and "↻ re-run extraction" all still work.
8. **Reload the notice** → every edit is stuck.
9. The full-screen modal is gone; "✎ Edit table" button is gone.
10. Browser console: zero errors across the whole flow.

- [ ] **Step 2: Check for orphaned references**

Run Grep for each removed identifier and confirm zero hits in `web/review.html`:
`renderAnnTablePreview`, `_annParseTableHtml`, `_annSerializeTableHtml`, `tbledOpen`, `tbledClose`, `tbledMountOnce`, `tbledSave`, `tbledEnsureMounted`, `tbled-overlay`, `tbled-grid-wrap"` (the modal id), `Edit table`.

- [ ] **Step 3: Final commit (if any cleanup was needed)**

```bash
git add web/review.html
git commit -m "chore(review): final cleanup for inline table editor"
```

---

## Self-review notes (author)

- **Spec coverage:** unified inline editor (Tasks 4–7), span-correct rendering everywhere (Task 3 read-only + Task 4 editor reuse `tbledBuildGrid`), single-click edit (Task 5), repaint/clobber fix via in-place selection + silent save (Tasks 1, 5, 6), modal + flat-preview removal (Tasks 7, 8), kept tools (raw-HTML toggle, Table grid positions, Table Hints, re-run extraction) untouched (verified Task 8 keeps their CSS; Task 7 only swaps the preview line). Manual verification matches the spec's acceptance list (Task 9). No server changes (none in plan).
- **Type/identifier consistency:** new functions `tbledMountInline`, `tbledPersist`, `tbledRepaintSelection`, `tbledRenderReadonly`; `tbled` fields `block`/`blockId`/`container`/`rows`/`past`/`future`/`selection`/`showTex` used consistently; `tbledBuildGrid(rows)` always passed `tbled.rows` (editor) or a parsed model (read-only). Cell datasets `r0/c0/r1/c1` written in `paintTbledGrid` and read in `tbledRepaintSelection`.
- **No placeholders:** every code step shows full code or an exact find/replace.
