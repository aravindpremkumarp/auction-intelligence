/*
 * web/billing.js
 * --------------
 * Razorpay paid-tier ("Pro") checkout for the static frontend.
 *
 * Flow (mirrors the backend in api/billing): POST /billing/order to mint a
 * Razorpay order, open Razorpay Checkout with it, then POST /billing/verify on
 * success. Per the backend design the WEBHOOK is the source of truth for
 * activation — verify-on-return is only for UX — so when verify reports
 * "pending" we poll /auth/me a few times until the webhook flips the tier.
 *
 * Exposes window.Billing.openCheckout(); the account dropdown in auth.js calls
 * it. Uses Auth.fetchWithAuth so the bearer token (and silent refresh) come for
 * free, and Auth.me() to refresh the cached user once the plan activates.
 */
(function () {
  'use strict';

  var API = (typeof window !== 'undefined' && window.API_BASE) || '';
  var RZP_SDK = 'https://checkout.razorpay.com/v1/checkout.js';
  var _sdkPromise = null;

  // Analytics: fire the GA4 `upgrade_success` conversion once per confirmed
  // activation — the plan can be confirmed by either the verify call or the
  // webhook poll, so dedupe. No PII.
  var _upgradeTracked = false;
  function trackUpgradeSuccess() {
    if (_upgradeTracked) return;
    _upgradeTracked = true;
    if (window.track) window.track('upgrade_success', { plan: 'pro' });
  }

  function loadSdk() {
    if (window.Razorpay) return Promise.resolve();
    if (_sdkPromise) return _sdkPromise;
    _sdkPromise = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = RZP_SDK;
      s.onload = function () { resolve(); };
      s.onerror = function () { _sdkPromise = null; reject(new Error('Could not load the payment library. Check your connection and try again.')); };
      document.head.appendChild(s);
    });
    return _sdkPromise;
  }

  function fetchAuth(url, opts) {
    if (window.Auth && window.Auth.fetchWithAuth) return window.Auth.fetchWithAuth(url, opts);
    return fetch(url, opts);
  }

  async function safeJson(res) {
    try { return await res.json(); } catch (_) { return null; }
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ── Result modal ────────────────────────────────────────────────────────
  function ensureStyles() {
    if (document.getElementById('billing-modal-styles')) return;
    var css = '' +
      '.billing-backdrop{position:fixed;inset:0;background:rgba(15,23,42,0.45);' +
      'backdrop-filter:saturate(180%) blur(2px);display:grid;place-items:center;z-index:10000;}' +
      '.billing-modal{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);' +
      'box-shadow:var(--shadow-lg);padding:24px 26px;max-width:380px;width:92%;text-align:center;' +
      'font-family:var(--font-body);color:var(--ink);position:relative;}' +
      '.billing-modal .x-close{position:absolute;top:10px;right:12px;background:transparent;border:none;' +
      'font-size:20px;line-height:1;cursor:pointer;padding:4px 8px;color:var(--muted);border-radius:var(--radius-xs);}' +
      '.billing-modal .x-close:hover{color:var(--ink);background:var(--paper-2);}' +
      '.billing-modal .glyph{font-size:34px;line-height:1;margin:2px 0 10px;}' +
      '.billing-modal.ok .glyph{color:var(--good,#16a34a);}' +
      '.billing-modal.error .glyph{color:var(--danger,#dc2626);}' +
      '.billing-modal.pending .glyph{color:var(--accent);}' +
      '.billing-modal h3{font-family:var(--font-body);font-size:19px;font-weight:700;margin:0 0 8px;letter-spacing:-0.01em;}' +
      '.billing-modal p{font-family:var(--font-body);font-size:13.5px;color:var(--ink-soft);margin:0;line-height:1.5;}' +
      '.billing-modal .spin{display:inline-block;width:26px;height:26px;border:3px solid var(--border-strong);' +
      'border-top-color:var(--accent);border-radius:50%;animation:billing-spin 0.8s linear infinite;margin:2px 0 12px;}' +
      '@keyframes billing-spin{to{transform:rotate(360deg);}}' +
      '.billing-modal button.done{margin-top:16px;padding:9px 18px;background:var(--accent);border:1px solid transparent;' +
      'border-radius:var(--radius-sm);color:var(--on-accent);font-family:var(--font-body);font-size:14px;font-weight:600;cursor:pointer;}' +
      '.billing-modal button.done:hover{background:var(--accent-hover);}';
    var style = document.createElement('style');
    style.id = 'billing-modal-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function closeResult() {
    var b = document.getElementById('billing-backdrop');
    if (b) b.remove();
  }

  // kind: 'pending' | 'ok' | 'error'. Pending shows a spinner and no dismiss
  // button (we don't want the user closing it mid-activation); ok/error show a
  // Done button + ✕.
  function showResult(kind, title, body) {
    ensureStyles();
    closeResult();
    var glyph = kind === 'ok' ? '<div class="glyph">✓</div>'
      : kind === 'error' ? '<div class="glyph">⚠</div>'
      : '<div class="spin"></div>';
    var bd = document.createElement('div');
    bd.id = 'billing-backdrop';
    bd.className = 'billing-backdrop';
    var box = document.createElement('div');
    box.className = 'billing-modal ' + kind;
    box.innerHTML =
      (kind === 'pending' ? '' : '<button class="x-close" aria-label="Close">×</button>') +
      glyph +
      '<h3></h3><p></p>' +
      (kind === 'pending' ? '' : '<button class="done">Done</button>');
    box.querySelector('h3').textContent = title;
    box.querySelector('p').textContent = body;
    bd.appendChild(box);
    document.body.appendChild(bd);
    var x = box.querySelector('.x-close');
    if (x) x.onclick = closeResult;
    var done = box.querySelector('.done');
    if (done) done.onclick = closeResult;
  }

  // ── Checkout ──────────────────────────────────────────────────────────────
  async function openCheckout() {
    var user = window.Auth && window.Auth.getUser ? window.Auth.getUser() : null;
    if (!user) { if (window.Auth && window.Auth.openLoginModal) window.Auth.openLoginModal(); return; }
    if (user.tier === 'paid') { showResult('ok', "You're already Pro ✦", 'Your plan is active.'); return; }

    try {
      var orderRes = await fetchAuth(API + '/billing/order', { method: 'POST' });
      if (!orderRes.ok) {
        var d = await safeJson(orderRes);
        throw new Error((d && d.detail) || ('Could not start checkout (' + orderRes.status + ').'));
      }
      var order = await orderRes.json();
      await loadSdk();

      var rzp = new window.Razorpay({
        key: order.key_id,
        amount: order.amount,
        currency: order.currency,
        order_id: order.order_id,
        name: 'AuctionScope',
        description: 'Pro access',
        prefill: { email: user.email || '', name: user.name || '' },
        theme: { color: '#2563eb' },
        handler: function (resp) { onPaid(resp); },
      });
      rzp.on('payment.failed', function (resp) {
        var msg = (resp && resp.error && resp.error.description) || 'Please try again.';
        showResult('error', 'Payment failed', msg);
      });
      if (window.track) window.track('checkout_start', { plan: 'pro' });
      rzp.open();
    } catch (e) {
      console.error('[billing] checkout', e);
      showResult('error', 'Could not start checkout', e.message || 'Please try again.');
    }
  }

  async function onPaid(resp) {
    showResult('pending', 'Confirming payment…', 'This will only take a moment.');
    try {
      var r = await fetchAuth(API + '/billing/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          razorpay_order_id: resp.razorpay_order_id,
          razorpay_payment_id: resp.razorpay_payment_id,
          razorpay_signature: resp.razorpay_signature,
        }),
      });
      var data = await safeJson(r);
      if (r.ok && data && data.status === 'paid') {
        if (window.Auth && window.Auth.me) { try { await window.Auth.me(); } catch (_) {} }
        trackUpgradeSuccess();
        showResult('ok', "You're Pro! ✦", 'Your upgrade is active. Enjoy the higher limits.');
        return;
      }
      // Verify is non-authoritative — the webhook activates the plan. Poll a few
      // times for it to land before telling the user to wait.
      await pollUntilPaid();
    } catch (e) {
      console.error('[billing] verify', e);
      await pollUntilPaid();
    }
  }

  async function pollUntilPaid() {
    showResult('pending', 'Payment received', 'Activating your account…');
    for (var i = 0; i < 6; i++) {
      await sleep(2000);
      var u = null;
      try { u = window.Auth && window.Auth.me ? await window.Auth.me() : null; } catch (_) {}
      if (u && u.tier === 'paid') {
        trackUpgradeSuccess();
        showResult('ok', "You're Pro! ✦", 'Your upgrade is active. Enjoy the higher limits.');
        return;
      }
    }
    showResult('ok', 'Payment received ✓',
      "We're finalising your upgrade — it'll be active within a minute. Refresh the page if it doesn't appear.");
  }

  window.Billing = { openCheckout: openCheckout };
})();
