// Cookie notice — minimal, informational banner (not a consent gate).
//
// GA4 (index.html) fires unconditionally regardless of this banner's state —
// this is deliberate per the founder's call: keep 100% of funnel data at our
// current traffic, favor a low-friction notice over a blocking consent gate.
// Dismissing (or ignoring — analytics events themselves imply the user saw
// the page) just stops the banner from reappearing; it does not toggle
// tracking. See web/privacy-policy.html §4 for the actual disclosure.
(function () {
  'use strict';

  var DISMISS_KEY = 'ascope_cookie_notice_dismissed';

  try {
    if (localStorage.getItem(DISMISS_KEY)) return;
  } catch (e) {
    return; // no storage (private mode / blocked) — don't nag every load
  }

  function ensureStyles() {
    if (document.getElementById('cookie-notice-styles')) return;
    var css = '' +
      '.cookie-notice{position:fixed;left:12px;right:12px;bottom:12px;z-index:85;' +
      'display:flex;align-items:center;gap:14px;flex-wrap:wrap;' +
      'max-width:640px;margin:0 auto;padding:12px 16px;' +
      'background:var(--card);border:1px solid var(--border);border-radius:var(--radius);' +
      'box-shadow:var(--shadow-lg);font-family:var(--font-body);}' +
      '.cookie-notice p{margin:0;flex:1 1 240px;font-size:13px;line-height:1.45;color:var(--ink-soft);}' +
      '.cookie-notice a{color:var(--accent);text-decoration:underline;}' +
      '.cookie-notice button{flex:0 0 auto;padding:8px 16px;background:var(--accent);' +
      'border:1px solid transparent;border-radius:var(--radius-sm);color:var(--on-accent);' +
      'font-family:var(--font-body);font-size:13px;font-weight:600;cursor:pointer;}' +
      '.cookie-notice button:hover{background:var(--accent-hover);}' +
      '@media (max-width: 640px){' +
      '.cookie-notice{bottom:calc(46px + env(safe-area-inset-bottom, 0px) + 10px);}' +
      '}';
    var style = document.createElement('style');
    style.id = 'cookie-notice-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function dismiss() {
    try { localStorage.setItem(DISMISS_KEY, '1'); } catch (e) { /* best-effort */ }
    var el = document.getElementById('cookie-notice');
    if (el) el.remove();
  }

  function show() {
    ensureStyles();
    var el = document.createElement('div');
    el.id = 'cookie-notice';
    el.className = 'cookie-notice';
    el.setAttribute('role', 'region');
    el.setAttribute('aria-label', 'cookie notice');
    el.innerHTML =
      '<p>we use cookies for analytics, to understand how people use auctionscope. ' +
      'see our <a href="/privacy-policy">privacy policy</a>.</p>' +
      '<button type="button">got it</button>';
    el.querySelector('button').addEventListener('click', dismiss);
    document.body.appendChild(el);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', show);
  } else {
    show();
  }
})();
