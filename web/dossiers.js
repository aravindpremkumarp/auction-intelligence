/*
 * web/dossiers.js
 * ---------------
 * "My Dossiers" — a private, per-property document locker.
 *
 * Self-contained screen module loaded after app.js, so it reuses app.js's
 * globals: authFetch, API_BASE, escapeHtml, go, isSignedIn. It owns the
 * `data-screen="dossiers"` panel and an internal list/detail view state; the
 * outer screen routing stays in app.js (go('dossiers') calls Dossiers.render()).
 *
 * All endpoints are auth + ownership gated server-side (see api/dossier); the
 * frontend just renders what it's allowed to see and never trusts a stale id.
 */
(function () {
  'use strict';

  var esc = (typeof escapeHtml === 'function')
    ? escapeHtml
    : function (s) {
        return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) {
          return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
      };
  var base = function () { return (typeof API_BASE !== 'undefined' && API_BASE) || ''; };
  var signedIn = function () {
    return !!(window.Auth && window.Auth.getUser && window.Auth.getUser());
  };

  // Internal view state for the screen.
  var state = { view: 'list', id: null };

  function body() { return document.getElementById('dossiers-body'); }

  // ── API helpers ─────────────────────────────────────────────────────────
  function api(path, opts) {
    return authFetch(base() + '/dossiers' + path, opts);
  }
  async function listDossiers() {
    var r = await api('', {});
    if (!r.ok) throw new Error('list ' + r.status);
    return (await r.json()).dossiers || [];
  }
  async function getDossier(id) {
    var r = await api('/' + encodeURIComponent(id), {});
    if (r.status === 404) return null;
    if (!r.ok) throw new Error('get ' + r.status);
    return await r.json();
  }
  async function createDossier(payload) {
    var r = await api('', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      var detail = '';
      try { detail = (await r.json()).detail; } catch (e) {}
      throw new Error(detail || ('create ' + r.status));
    }
    return await r.json();
  }
  async function deleteDossier(id) {
    var r = await api('/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!r.ok && r.status !== 204) throw new Error('delete ' + r.status);
  }
  async function uploadDoc(id, file, consent) {
    var fd = new FormData();
    fd.append('file', file);
    fd.append('consent', consent ? 'true' : 'false');
    var r = await api('/' + encodeURIComponent(id) + '/documents', { method: 'POST', body: fd });
    if (!r.ok) {
      var detail = '';
      try { detail = (await r.json()).detail; } catch (e) {}
      var err = new Error(detail || ('upload ' + r.status));
      err.status = r.status;
      throw err;
    }
    return await r.json();
  }
  async function docUrl(id, docId) {
    var r = await api('/' + encodeURIComponent(id) + '/documents/' + encodeURIComponent(docId) + '/url', {});
    if (!r.ok) throw new Error('url ' + r.status);
    return await r.json();
  }
  async function deleteDoc(id, docId) {
    var r = await api('/' + encodeURIComponent(id) + '/documents/' + encodeURIComponent(docId), { method: 'DELETE' });
    if (!r.ok && r.status !== 204) throw new Error('doc delete ' + r.status);
  }

  // ── small view helpers ──────────────────────────────────────────────────
  function scoreClass(score) {
    if (score >= 80) return 'good';
    if (score >= 40) return 'mid';
    return 'low';
  }
  function propLabel(prop) {
    if (!prop) return 'Untitled property';
    return prop.label || prop.auction_id || prop.id || 'Untitled property';
  }
  function propSub(prop) {
    if (!prop) return '';
    if (prop.kind === 'auction_property') {
      return prop.auction_id ? ('Auction · ' + prop.auction_id) : 'Auction property';
    }
    var bits = [];
    if (prop.survey_no) bits.push('Survey ' + prop.survey_no);
    if (prop.sub_registrar) bits.push(prop.sub_registrar);
    if (prop.address) bits.push(prop.address);
    return bits.join(' · ') || 'Your property';
  }
  function fmtDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }
  function statusBadge(status) {
    var s = status || 'processing';
    var cls = s === 'ready' ? 'good' : (s === 'failed' ? 'low' : 'mid');
    return '<span class="dossier-status ' + cls + '">' + esc(s) + '</span>';
  }
  // doc_type id -> human label, sourced from the server-built checklist so the
  // taxonomy stays single-sourced on the backend.
  function docTypeLabeller(checklist) {
    var map = {};
    (checklist && checklist.categories || []).forEach(function (cat) {
      (cat.doc_types || []).forEach(function (d) { map[d.doc_type] = d.label; });
    });
    return function (id) {
      if (!id || id === 'unknown') return 'Unclassified';
      return map[id] || id.replace(/_/g, ' ');
    };
  }

  // ── LIST view ─────────────────────────────────────────────────────────────
  function signInPrompt() {
    return '<div class="dossier-empty">'
      + '<div class="dossier-empty-title">Sign in to use your dossier</div>'
      + '<div class="dossier-empty-sub">A dossier is your private locker for a property’s documents — '
      + 'sale deed, EC, patta and more — with a readiness checklist and AI answers about your own papers.</div>'
      + '<button class="btn" id="dossier-signin" style="max-width:280px;margin:18px auto 0;">sign in</button>'
      + '</div>';
  }

  function dossierCardHtml(d) {
    var r = d.readiness || { score: 0, have: 0, total: 10 };
    return '<button class="dossier-card" data-open="' + esc(d.id) + '">'
      + '<div class="dossier-card-main">'
      + '<div class="dossier-card-title">' + esc(d.title || propLabel(d.property)) + '</div>'
      + '<div class="dossier-card-sub">' + esc(propSub(d.property)) + '</div>'
      + '<div class="dossier-card-meta">' + (d.doc_count || 0) + ' document' + ((d.doc_count === 1) ? '' : 's')
      + (d.updated_at ? ' · updated ' + esc(fmtDate(d.updated_at)) : '') + '</div>'
      + '</div>'
      + '<div class="dossier-score ' + scoreClass(r.score) + '">'
      + '<div class="dossier-score-num">' + r.score + '</div>'
      + '<div class="dossier-score-lbl">' + r.have + '/' + r.total + ' must-haves</div>'
      + '</div>'
      + '</button>';
  }

  async function renderList() {
    var el = body();
    if (!el) return;
    if (!signedIn()) { el.innerHTML = signInPrompt(); return; }
    el.innerHTML = '<div class="dossier-loading">loading your dossiers…</div>';
    var rows;
    try { rows = await listDossiers(); }
    catch (e) { el.innerHTML = '<div class="dossier-empty"><div class="dossier-empty-title">Couldn’t load dossiers</div>'
      + '<div class="dossier-empty-sub">' + esc(String(e.message || e)) + '</div></div>'; return; }

    var head = '<div class="dossier-head">'
      + '<div><h2>your dossiers</h2><div class="mono dossier-count">' + rows.length + ' propert' + (rows.length === 1 ? 'y' : 'ies') + '</div></div>'
      + '<button class="btn" id="dossier-new" style="width:auto;padding:8px 16px;">+ new dossier</button>'
      + '</div>';

    if (!rows.length) {
      el.innerHTML = head + '<div class="dossier-empty">'
        + '<div class="dossier-empty-title">No dossiers yet</div>'
        + '<div class="dossier-empty-sub">Create one for a property you’re vetting, upload its documents, '
        + 'and we’ll classify them and track what’s missing.</div></div>';
      return;
    }
    el.innerHTML = head + '<div class="dossier-list">' + rows.map(dossierCardHtml).join('') + '</div>';
  }

  // ── DETAIL view ─────────────────────────────────────────────────────────
  function missingHtml(checklist) {
    var missing = (checklist && checklist.missing_minimum) || [];
    if (!missing.length) {
      return '<div class="dossier-allset">✅ All must-have documents are present.</div>';
    }
    var items = missing.map(function (m) {
      var link = m.go_get_it;
      var how = link ? '<div class="dossier-how">' + esc(link.how || '')
        + (link.url ? ' <a href="' + esc(link.url) + '" target="_blank" rel="noopener">' + esc(link.portal || 'portal') + ' ↗</a>' : '')
        + '</div>' : '';
      return '<li><span class="dossier-miss-name">' + esc(m.label) + '</span>' + how + '</li>';
    }).join('');
    return '<div class="dossier-missing"><div class="dossier-section-title">Missing must-haves ('
      + missing.length + ')</div><ul class="dossier-miss-list">' + items + '</ul></div>';
  }

  function documentsHtml(d, label) {
    var docs = d.documents || [];
    var rows = docs.map(function (doc) {
      var conf = (typeof doc.doc_type_confidence === 'number')
        ? ' · ' + Math.round(doc.doc_type_confidence * 100) + '%' : '';
      var typeLine = (doc.status === 'ready')
        ? '<span class="dossier-doc-type">' + esc(label(doc.doc_type)) + '</span>' + conf
        : '';
      var actions = (doc.status === 'ready')
        ? '<button class="dossier-link" data-view="' + esc(doc.id) + '">view</button>' : '';
      return '<div class="dossier-doc">'
        + '<div class="dossier-doc-main">'
        + '<div class="dossier-doc-name">' + esc(doc.filename || 'document') + '</div>'
        + '<div class="dossier-doc-sub">' + statusBadge(doc.status) + ' ' + typeLine + '</div>'
        + '</div>'
        + '<div class="dossier-doc-actions">' + actions
        + '<button class="dossier-link danger" data-deldoc="' + esc(doc.id) + '">remove</button></div>'
        + '</div>';
    }).join('');
    if (!docs.length) {
      rows = '<div class="dossier-doc-empty">No documents uploaded yet.</div>';
    }
    return '<div class="dossier-section-title">Documents (' + docs.length + ')</div>'
      + '<div class="dossier-docs">' + rows + '</div>';
  }

  function uploaderHtml() {
    return '<div class="dossier-upload">'
      + '<input type="file" id="dossier-file" accept=".pdf,.png,.jpg,.jpeg,.webp" />'
      + '<label class="dossier-consent"><input type="checkbox" id="dossier-consent" /> '
      + 'I consent to this document being sent to our OCR provider for analysis.</label>'
      + '<button class="btn" id="dossier-upload-btn" disabled style="width:auto;padding:8px 16px;">upload &amp; analyze</button>'
      + '<div class="dossier-upload-msg" id="dossier-upload-msg"></div>'
      + '</div>';
  }

  function categoriesHtml(checklist) {
    var cats = (checklist && checklist.categories) || [];
    var blocks = cats.map(function (cat) {
      var items = (cat.doc_types || []).map(function (dt) {
        var mark = dt.present ? '✓' : '·';
        return '<li class="' + (dt.present ? 'have' : 'miss') + '"><span class="dossier-tick">' + mark + '</span>'
          + esc(dt.label) + (dt.conditional ? ' <span class="dossier-cond">(if applicable)</span>' : '') + '</li>';
      }).join('');
      return '<details class="dossier-cat"' + (cat.have ? ' open' : '') + '>'
        + '<summary><span class="dossier-cat-name">' + esc(cat.label) + '</span>'
        + '<span class="dossier-cat-count">' + cat.have + '/' + cat.total + '</span></summary>'
        + '<ul class="dossier-cat-list">' + items + '</ul></details>';
    }).join('');
    return '<div class="dossier-section-title">Full checklist</div>' + blocks;
  }

  function renderDetailFrom(d) {
    var el = body();
    if (!el) return;
    if (!d) { state.view = 'list'; state.id = null; renderList(); return; }
    state.view = 'detail';
    state.id = d.id;
    var cl = d.checklist || {};
    var score = (cl.score && cl.score.score) || 0;
    var have = (cl.score && cl.score.have) || 0;
    var total = (cl.score && cl.score.total) || 10;
    var label = docTypeLabeller(cl);

    el.innerHTML =
      '<button class="dossier-back" id="dossier-back">← all dossiers</button>'
      + '<div class="dossier-detail-head">'
      + '<div class="dossier-detail-titles">'
      + '<div class="dossier-detail-title">' + esc(d.title || propLabel(d.property)) + '</div>'
      + '<div class="dossier-detail-sub">' + esc(propSub(d.property)) + '</div>'
      + '</div>'
      + '<div class="dossier-score big ' + scoreClass(score) + '">'
      + '<div class="dossier-score-num">' + score + '</div>'
      + '<div class="dossier-score-lbl">' + have + '/' + total + ' must-haves</div>'
      + '</div>'
      + '</div>'
      + missingHtml(cl)
      + '<div class="dossier-card-box">' + documentsHtml(d, label) + uploaderHtml() + '</div>'
      + '<div class="dossier-card-box">' + categoriesHtml(cl) + '</div>'
      + '<button class="dossier-link danger dossier-del" id="dossier-delete">delete this dossier</button>';
  }

  async function renderDetail(id) {
    var el = body();
    if (!el) return;
    el.innerHTML = '<div class="dossier-loading">loading…</div>';
    var d;
    try { d = await getDossier(id); }
    catch (e) { el.innerHTML = '<button class="dossier-back" id="dossier-back">← all dossiers</button>'
      + '<div class="dossier-empty"><div class="dossier-empty-title">Couldn’t load dossier</div></div>'; return; }
    renderDetailFrom(d);
  }

  // ── create modal ──────────────────────────────────────────────────────────
  function openCreateModal() {
    closeModal();
    var bd = document.createElement('div');
    bd.className = 'dossier-modal-backdrop';
    bd.id = 'dossier-modal';
    bd.innerHTML =
      '<div class="dossier-modal" role="dialog" aria-modal="true">'
      + '<h3>New dossier</h3>'
      + '<label class="dossier-field"><span>Title <em>(optional)</em></span>'
      + '<input type="text" id="dm-title" maxlength="200" placeholder="e.g. Plot in Avadi" /></label>'
      + '<div class="dossier-field"><span>This dossier is for</span>'
      + '<div class="dossier-radio"><label><input type="radio" name="dm-kind" value="auction" checked /> An auction (by ID)</label>'
      + '<label><input type="radio" name="dm-kind" value="other" /> Another property</label></div></div>'
      + '<div id="dm-auction-fields"><label class="dossier-field"><span>Auction ID</span>'
      + '<input type="text" id="dm-auction-id" placeholder="auction_id" /></label></div>'
      + '<div id="dm-other-fields" style="display:none;">'
      + '<label class="dossier-field"><span>Property name</span><input type="text" id="dm-label" maxlength="200" placeholder="e.g. Plot 4, Anna Nagar" /></label>'
      + '<label class="dossier-field"><span>Survey no. <em>(optional)</em></span><input type="text" id="dm-survey" maxlength="120" /></label>'
      + '<label class="dossier-field"><span>Sub-registrar <em>(optional)</em></span><input type="text" id="dm-subreg" maxlength="200" /></label>'
      + '<label class="dossier-field"><span>Address <em>(optional)</em></span><input type="text" id="dm-address" maxlength="500" /></label>'
      + '</div>'
      + '<div class="dossier-modal-err" id="dm-err"></div>'
      + '<div class="dossier-modal-actions">'
      + '<button class="btn ghost" id="dm-cancel" style="width:auto;padding:6px 14px;">cancel</button>'
      + '<button class="btn" id="dm-create" style="width:auto;padding:6px 14px;">create</button>'
      + '</div></div>';
    document.body.appendChild(bd);

    function setKind() {
      var kind = bd.querySelector('input[name="dm-kind"]:checked').value;
      bd.querySelector('#dm-auction-fields').style.display = kind === 'auction' ? '' : 'none';
      bd.querySelector('#dm-other-fields').style.display = kind === 'other' ? '' : 'none';
    }
    bd.querySelectorAll('input[name="dm-kind"]').forEach(function (r) { r.addEventListener('change', setKind); });
    bd.addEventListener('click', function (e) { if (e.target === bd) closeModal(); });
    bd.querySelector('#dm-cancel').addEventListener('click', closeModal);
    bd.querySelector('#dm-create').addEventListener('click', submitCreate);
    var t = bd.querySelector('#dm-title'); if (t) t.focus();
  }
  function closeModal() {
    var ex = document.getElementById('dossier-modal');
    if (ex) ex.remove();
  }
  async function submitCreate() {
    var bd = document.getElementById('dossier-modal');
    if (!bd) return;
    var err = bd.querySelector('#dm-err');
    var kind = bd.querySelector('input[name="dm-kind"]:checked').value;
    var title = bd.querySelector('#dm-title').value.trim();
    var payload = {};
    if (title) payload.title = title;
    if (kind === 'auction') {
      var aid = bd.querySelector('#dm-auction-id').value.trim();
      if (!aid) { err.textContent = 'Enter an auction ID.'; return; }
      payload.auction_id = aid;
    } else {
      var lbl = bd.querySelector('#dm-label').value.trim();
      if (!lbl) { err.textContent = 'Enter a property name.'; return; }
      payload.user_property = {
        label: lbl,
        survey_no: bd.querySelector('#dm-survey').value.trim() || null,
        sub_registrar: bd.querySelector('#dm-subreg').value.trim() || null,
        address: bd.querySelector('#dm-address').value.trim() || null,
      };
    }
    var btn = bd.querySelector('#dm-create');
    btn.disabled = true; err.textContent = '';
    try {
      var d = await createDossier(payload);
      closeModal();
      renderDetailFrom(d);
    } catch (e) {
      btn.disabled = false;
      err.textContent = (String(e.message).indexOf('404') >= 0 || /not found/i.test(e.message))
        ? 'No auction found with that ID.' : ('Could not create: ' + e.message);
    }
  }

  // ── upload handling ───────────────────────────────────────────────────────
  async function doUpload() {
    var fileEl = document.getElementById('dossier-file');
    var consentEl = document.getElementById('dossier-consent');
    var msg = document.getElementById('dossier-upload-msg');
    var btn = document.getElementById('dossier-upload-btn');
    if (!fileEl || !fileEl.files || !fileEl.files[0]) { msg.textContent = 'Choose a file first.'; return; }
    if (!consentEl || !consentEl.checked) { msg.textContent = 'Please tick the consent box to proceed.'; return; }
    var file = fileEl.files[0];
    btn.disabled = true;
    msg.className = 'dossier-upload-msg';
    msg.textContent = 'Uploading & analyzing “' + file.name + '”… this can take a moment.';
    try {
      var d = await uploadDoc(state.id, file, true);
      renderDetailFrom(d);
      var newMsg = document.getElementById('dossier-upload-msg');
      if (newMsg) {
        var uploaded = (d.documents || []).filter(function (x) { return x.id === d.uploaded_document_id; })[0];
        if (uploaded && uploaded.status === 'failed') {
          newMsg.className = 'dossier-upload-msg err';
          newMsg.textContent = 'Uploaded, but we couldn’t read this document. You can remove it and try a clearer scan.';
        } else {
          newMsg.className = 'dossier-upload-msg ok';
          newMsg.textContent = 'Added ✓';
        }
      }
    } catch (e) {
      btn.disabled = false;
      msg.className = 'dossier-upload-msg err';
      msg.textContent = e.message || 'Upload failed.';
    }
  }

  // ── event delegation on the screen body ───────────────────────────────────
  function onBodyClick(e) {
    var t = e.target.closest('[data-open],[data-view],[data-deldoc],#dossier-new,#dossier-back,#dossier-delete,#dossier-signin,#dossier-upload-btn');
    if (!t) return;
    if (t.id === 'dossier-signin') {
      if (window.Auth && window.Auth.openLoginModal) window.Auth.openLoginModal();
      return;
    }
    if (t.id === 'dossier-new') { openCreateModal(); return; }
    if (t.id === 'dossier-back') { state.view = 'list'; state.id = null; renderList(); return; }
    if (t.id === 'dossier-upload-btn') { doUpload(); return; }
    if (t.id === 'dossier-delete') { confirmDeleteDossier(); return; }
    if (t.hasAttribute('data-open')) { state.id = t.getAttribute('data-open'); state.view = 'detail'; renderDetail(state.id); return; }
    if (t.hasAttribute('data-view')) { viewDoc(t.getAttribute('data-view')); return; }
    if (t.hasAttribute('data-deldoc')) { removeDoc(t.getAttribute('data-deldoc')); return; }
  }
  function onBodyChange(e) {
    if (e.target && (e.target.id === 'dossier-file' || e.target.id === 'dossier-consent')) {
      var fileEl = document.getElementById('dossier-file');
      var consentEl = document.getElementById('dossier-consent');
      var btn = document.getElementById('dossier-upload-btn');
      if (btn) btn.disabled = !(fileEl && fileEl.files && fileEl.files[0] && consentEl && consentEl.checked);
    }
  }

  async function viewDoc(docId) {
    try {
      var info = await docUrl(state.id, docId);
      window.open(info.url, '_blank', 'noopener');
    } catch (e) { /* surface quietly */ }
  }
  async function removeDoc(docId) {
    if (!window.confirm('Remove this document? This deletes the uploaded file.')) return;
    try { await deleteDoc(state.id, docId); await renderDetail(state.id); }
    catch (e) {}
  }
  async function confirmDeleteDossier() {
    if (!window.confirm('Delete this dossier and all its documents? This cannot be undone.')) return;
    try { await deleteDossier(state.id); state.view = 'list'; state.id = null; renderList(); }
    catch (e) {}
  }

  // ── public entry ──────────────────────────────────────────────────────────
  function render() {
    if (!signedIn()) { state.view = 'list'; state.id = null; renderList(); return; }
    if (state.view === 'detail' && state.id) renderDetail(state.id);
    else renderList();
  }

  // Wire delegation once the DOM is ready.
  function init() {
    var el = body();
    if (!el) return;
    el.addEventListener('click', onBodyClick);
    el.addEventListener('change', onBodyChange);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });
    if (window.Auth && window.Auth.onAuthChange) {
      window.Auth.onAuthChange(function () {
        // Re-render if the dossiers screen is currently visible.
        var screen = document.querySelector('.screen.on');
        if (screen && screen.dataset.screen === 'dossiers') { state.view = 'list'; state.id = null; render(); }
      });
    }
    // Hard-reload on /dossiers: app.js runs go('dossiers') before this script
    // loads (so its render hook is skipped). Render now if we're already on it.
    var active = document.querySelector('.screen.on');
    if (active && active.dataset.screen === 'dossiers') render();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Dossiers = { render: render };
})();
