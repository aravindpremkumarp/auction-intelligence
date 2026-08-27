/* ============================================================================
 * chatgpt-shell.js — turns the chat screen on /lab into the ChatGPT-style
 * shell prototyped in redesign/chatgpt-shell/index.html.
 *
 * Everything visual lives in chatgpt-shell.css, gated on the data-shell
 * attribute this file sets. Loads on every page like lab.js and no-ops
 * unless the shell is on, so index.html needs one script tag rather than a
 * second copy of the app.
 *
 * Three jobs the CSS cannot do on its own:
 *   1. Decide when the shell is on, before first paint (no flash of the old
 *      layout, same trick the theme bootstrap in index.html uses).
 *   2. Compose the empty state. ChatGPT stacks greeting, then the input,
 *      then the suggestions; the app's DOM has the input as a sibling AFTER
 *      the log. So the input and the suggestion row get moved into
 *      .chat-empty while the thread is empty and moved back out once it has
 *      messages.
 *   3. Run the matches drawer. The third grid column becomes a right-hand
 *      drawer so the thread can take ChatGPT's full centred width; this
 *      wires its toggle and mirrors the match count onto it.
 *
 * It never touches app.js state — no sends, no thread handling, no fetches.
 * Removing the two tags from index.html removes the shell completely.
 * ==========================================================================*/
(function () {
  'use strict';

  // ── 1. is the shell on? ───────────────────────────────────────────────────
  // /lab always. Anywhere else opt in with ?shell=chatgpt (sticks, so you can
  // click around the app in it) and out with ?shell=0.
  var ON = (function () {
    try {
      var v = new URLSearchParams(location.search).get('shell');
      if (v === 'chatgpt' || v === '1') localStorage.setItem('cgshell', '1');
      else if (v === '0' || v === 'off') localStorage.removeItem('cgshell');
      if (/^\/lab\/?$/.test(location.pathname)) return true;
      return localStorage.getItem('cgshell') === '1';
    } catch (_) {
      // Private mode: /lab still gets the shell, the sticky flag just doesn't.
      return /^\/lab\/?$/.test(location.pathname);
    }
  })();

  if (!ON) return;
  document.documentElement.setAttribute('data-shell', 'chatgpt');

  var GREETING = 'What are you looking for today?';
  var PLACEHOLDER = 'Ask anything';

  function boot() {
    var pane = document.querySelector('.transcript-pane');
    var empty = document.getElementById('chat-empty');
    var log = document.getElementById('chat-log');
    if (!pane || !empty || !log) return;   // markup moved; leave the page alone

    var input = pane.querySelector('.chat-input');
    var suggest = empty.querySelector('.landing-suggest');
    var title = empty.querySelector('.chat-empty-title');
    if (title) title.textContent = GREETING;
    // "refine…" reads as a follow-up prompt; the shell shows this box before
    // there is anything to refine.
    var field = input && input.querySelector('textarea');
    if (field) field.placeholder = PLACEHOLDER;

    // ── 2. empty state vs thread ───────────────────────────────────────────
    // Order matters both ways: the composer is moved OUT before the empty
    // state is marked hidden, and moved IN only after it is marked visible,
    // so it is never a child of a display:none container even for a frame.
    var placed = null;   // 'empty' | 'thread'

    function toThread() {
      if (placed === 'thread') return;
      if (input) pane.appendChild(input);
      if (suggest) empty.appendChild(suggest);
      pane.setAttribute('data-cg', 'thread');
      placed = 'thread';
    }

    function toEmpty() {
      if (placed === 'empty') return;
      pane.setAttribute('data-cg', 'empty');
      if (input) empty.appendChild(input);
      if (suggest) empty.appendChild(suggest);   // after the input, as in ChatGPT
      placed = 'empty';
    }

    function sync() { (log.children.length ? toThread : toEmpty)(); }

    sync();
    new MutationObserver(sync).observe(log, { childList: true });

    // ── 3. matches drawer ──────────────────────────────────────────────────
    var head = pane.querySelector('.transcript-head');
    var results = document.querySelector('.results-pane');
    var countEl = document.getElementById('results-count');
    if (!head || !results) return;

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cg-matches-btn';
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/></svg>' +
      '<span>Matches</span><span class="cg-count"></span>';
    head.appendChild(btn);

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'cg-drawer-close';
    close.setAttribute('aria-label', 'Close matches');
    close.innerHTML =
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    results.appendChild(close);

    function setDrawer(open) {
      if (open) document.documentElement.setAttribute('data-matches', 'open');
      else document.documentElement.removeAttribute('data-matches');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    btn.addEventListener('click', function () {
      setDrawer(document.documentElement.getAttribute('data-matches') !== 'open');
    });
    close.addEventListener('click', function () { setDrawer(false); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') setDrawer(false);
    });

    // A turn's cards are inline now, so the "N matches" chip only appears when
    // there are more than fit. app.js already repoints the panel at that turn;
    // the drawer has to be opened too, or the chip looks like it did nothing.
    // Capture phase: app.js's own chip handler calls stopPropagation(), so a
    // bubbling listener here would never fire.
    log.addEventListener('click', function (e) {
      if (e.target.closest('.matches-chip')) setDrawer(true);
    }, true);

    // Mirror the count app.js writes into #results-count. Reading it beats
    // re-deriving the number here — one source of truth, and it cannot drift
    // when the matching logic changes.
    function syncCount() {
      if (!countEl) return;
      var t = (countEl.textContent || '').trim();
      var m = t.match(/\d[\d,]*/);
      btn.querySelector('.cg-count').textContent = m ? m[0] : '';
    }
    syncCount();
    if (countEl) new MutationObserver(syncCount).observe(countEl, {
      childList: true, characterData: true, subtree: true
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
