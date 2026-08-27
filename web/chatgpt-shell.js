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

    // ── 3. the full list, in the answer ────────────────────────────────────
    //
    // This replaces the matches drawer. The drawer was the last of the old
    // shared panel — one surface, refilled on every turn — and while it
    // existed the panel/chat split #404 set out to remove still existed:
    // scroll to an older answer and the drawer beside it belonged to the
    // newest question.
    //
    // Expanding in place cannot drift, because the list is a child of the
    // answer that produced it. Built imperatively on click rather than
    // rendered with the turn: renderChat rebuilds the whole log's innerHTML
    // once per animation frame while an answer streams, and several hundred
    // rows through that loop is a real cost for a list nobody is reading yet.
    // The trade is that a new turn collapses an open list — which is what
    // asking a new question means anyway.
    log.addEventListener('click', function (e) {
      var btn = e.target.closest('.tm-all');
      if (!btn) return;
      var strip = btn.closest('.turn-matches');
      if (!strip) return;
      var open = strip.querySelector('.tm-all-list');
      if (open) {
        open.remove();
        btn.setAttribute('aria-expanded', 'false');
        return;
      }
      var html = (typeof window.allMatchesRowsHtml === 'function')
        ? window.allMatchesRowsHtml(btn.dataset.i) : '';
      if (!html) return;
      strip.insertAdjacentHTML('beforeend', html);
      btn.setAttribute('aria-expanded', 'true');
      // The rows are .prop[data-id] like every other card, so the app's own
      // click and save wiring picks them up; it skips anything already wired.
      if (typeof window.wireCardClicks === 'function') window.wireCardClicks();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
