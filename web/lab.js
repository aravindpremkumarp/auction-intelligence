/* ============================================================================
 * lab.js — the admin diagnostics inspector for /lab
 *
 * /lab serves the ordinary app shell with chat v2 forced on. This file adds
 * the thing the normal UI deliberately hides: what the agent actually did.
 *
 * That is the point of the page. Phase 3 is tuning — the slow tail, the
 * answer-gate fire rate, whether the prompt cache is being hit — and none of
 * it is visible from a chat bubble. Guessing at those is how you tune the
 * wrong thing.
 *
 * Loads on every page and no-ops off /lab, so index.html needs one script tag
 * rather than a duplicated page.
 * ==========================================================================*/
(function () {
  'use strict';

  if (!/^\/lab\/?$/.test(location.pathname)) return;

  var API = (typeof window !== 'undefined' && window.API_BASE) || '';
  var turns = [];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var n = function (v) { return typeof v === 'number' ? v.toLocaleString('en-IN') : '—'; };

  // ── the panel ─────────────────────────────────────────────────────────────
  function mount() {
    var el = document.createElement('aside');
    el.id = 'lab-inspector';
    el.innerHTML =
      '<div class="lab-head">' +
        '<span class="lab-badge">internal</span>' +
        '<span class="lab-title">chat v2 inspector</span>' +
        '<button class="lab-toggle" id="lab-toggle" aria-label="collapse">–</button>' +
      '</div>' +
      '<div class="lab-body" id="lab-body">' +
        '<div class="lab-empty">Ask something. Every turn is broken down here — ' +
        'the plan, the tier, tokens, cache hits, and the answer-gate verdict.</div>' +
      '</div>';
    document.body.appendChild(el);
    document.getElementById('lab-toggle').addEventListener('click', function () {
      el.classList.toggle('collapsed');
      this.textContent = el.classList.contains('collapsed') ? '+' : '–';
    });
    return el;
  }

  function denied(reason) {
    var el = document.createElement('div');
    el.id = 'lab-denied';
    el.innerHTML =
      '<div class="lab-denied-card">' +
        '<div class="lab-denied-title">Admin only</div>' +
        '<div class="lab-denied-sub">' + esc(reason) + '</div>' +
        '<a class="lab-denied-link" href="/">← back to the app</a>' +
      '</div>';
    document.body.appendChild(el);
  }

  // ── rendering one turn ────────────────────────────────────────────────────
  function turnHtml(t, index) {
    var u = t.usage || {};
    var cacheRate = u.input_tokens ? Math.round((u.cached_tokens || 0) / u.input_tokens * 100) : 0;
    var gate = t.gate;
    var gateOk = !gate || gate.ok;

    var calls = (t.plan || []).map(function (c) {
      return '<li class="lab-call' + (c.error ? ' bad' : '') + '">' +
        '<code>' + esc(c.tool) + '</code>' +
        '<span class="lab-ms">' + n(c.ms) + ' ms</span>' +
        (c.tier > 1 ? '<span class="lab-tier">tier ' + esc(c.tier) + '</span>' : '') +
        (c.error ? '<div class="lab-err">' + esc(c.error) + '</div>' : '') +
        '<div class="lab-args">' + esc(JSON.stringify(c.args || {})) + '</div>' +
        '</li>';
    }).join('');

    return '<details class="lab-turn"' + (index === 0 ? ' open' : '') + '>' +
      '<summary>' +
        '<span class="lab-q">' + esc((t.question || '').slice(0, 60)) + '</span>' +
        '<span class="lab-pill">' + (u.seconds != null ? u.seconds + 's' : '—') + '</span>' +
        '<span class="lab-pill">tier ' + esc(u.tier != null ? u.tier : '?') + '</span>' +
        (gateOk ? '' : '<span class="lab-pill warn">gate</span>') +
      '</summary>' +
      '<div class="lab-grid">' +
        '<div><b>' + n(u.llm_calls) + '</b><span>model calls</span></div>' +
        '<div><b>' + n(u.input_tokens) + '</b><span>input tokens</span></div>' +
        '<div><b>' + cacheRate + '%</b><span>cached</span></div>' +
        '<div><b>' + n(u.output_tokens) + '</b><span>output tokens</span></div>' +
      '</div>' +
      (calls ? '<div class="lab-sec">plan</div><ul class="lab-calls">' + calls + '</ul>'
             : '<div class="lab-sec">plan</div><div class="lab-none">no tools — answered directly</div>') +
      '<div class="lab-sec">scope carried forward</div>' +
      '<pre class="lab-pre">' + esc(JSON.stringify((t.scope || {}).filters || {}, null, 1)) + '</pre>' +
      '<div class="lab-sec">answer gate' +
        (gateOk ? '<span class="lab-ok"> clean</span>'
                : '<span class="lab-warn"> flagged</span>') + '</div>' +
      (gateOk
        ? '<div class="lab-none">every id and amount traced back to a tool result</div>'
        : '<div class="lab-gate">' + esc(gate.reason || '') +
          '<div class="lab-note">Report-only — the answer still shipped. ' +
          'Price-band labels like “under ₹30L” are the known false positive.</div></div>') +
      '</details>';
  }

  function render(body) {
    if (!turns.length) return;
    body.innerHTML = turns.map(turnHtml).join('');
  }

  // ── boot ──────────────────────────────────────────────────────────────────
  function boot() {
    var fetcher = (window.Auth && window.Auth.fetchWithAuth)
      ? window.Auth.fetchWithAuth : fetch;

    fetcher(API + '/auth/me').then(function (r) {
      if (!r || !r.ok) throw new Error('sign in from the main app, then return here');
      return r.json();
    }).then(function (me) {
      if (!me || me.role !== 'admin') {
        throw new Error('this page needs an admin account; you are signed in as ' +
                        (me && me.role ? me.role : 'a guest'));
      }
      var el = mount();
      var body = document.getElementById('lab-body');
      window.addEventListener('chatv2:turn', function (e) {
        turns.unshift(e.detail || {});
        turns = turns.slice(0, 20);   // a session's worth, not a memory leak
        render(body);
        el.classList.remove('collapsed');
      });
    }).catch(function (err) {
      denied(err && err.message ? err.message : 'not authorised');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
