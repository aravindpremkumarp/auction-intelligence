/*
 * web/auth.js
 * -----------
 * Supabase-backed auth module for the static Vercel-hosted frontend.
 * Exposes `window.Auth` with the original surface (fetchWithAuth, login,
 * signup, logout, me, forgot, openLoginModal, openSignupModal,
 * openForgotModal, onAuthChange, getUser, getRole) plus loginMagicLink and
 * loginGoogle so index.html can stay untouched.
 *
 * The Supabase JS SDK (loaded from CDN in index.html) owns session storage
 * and silent refresh; `me()` still hits FastAPI /auth/me so Neo4j-mirrored
 * role/enabled/name flow through.
 */
(function () {
  'use strict';

  var API = (typeof window !== 'undefined' && window.API_BASE) || '';
  var SB_URL = (typeof window !== 'undefined' && window.SUPABASE_URL) || '';
  var SB_ANON = (typeof window !== 'undefined' && window.SUPABASE_ANON_KEY) || '';

  // ── Analytics: signup / login funnel event ──────────────────────────────
  // Detected synchronously, BEFORE the Supabase client consumes (and strips)
  // the auth params from the URL, so we can tell an OAuth / magic-link redirect
  // completion apart from a plain reload of a persisted session.
  var _authRedirectInbound = false;
  try {
    var _q = new URLSearchParams(window.location.search);
    _authRedirectInbound = _q.has('code') || _q.has('error_code') || /access_token=/.test(window.location.hash);
  } catch (_) {}
  var _authEventSent = false;
  function _pendingAuthMethod(set) {
    try {
      if (set) { sessionStorage.setItem('ascope_auth_method', set); return set; }
      return sessionStorage.getItem('ascope_auth_method') || '';
    } catch (_) { return ''; }
  }
  // Fire the GA4 `signup` event on genuine auth success — deduped once per page
  // load. NO PII: method + new-account flag only, never email/name.
  function trackAuthSuccess(method, isNew) {
    if (_authEventSent) return;
    _authEventSent = true;
    if (window.track) {
      var p = { method: method || 'unknown' };
      if (typeof isNew === 'boolean') p.new_account = isNew;
      window.track('signup', p);
    }
    try { sessionStorage.removeItem('ascope_auth_method'); } catch (_) {}
  }

  if (!window.supabase || !SB_URL || !SB_ANON) {
    console.error('[auth] Supabase client or env vars missing; auth disabled');
    return;
  }

  var sb = window.supabase.createClient(SB_URL, SB_ANON, {
    auth: {
      flowType: 'pkce',
      detectSessionInUrl: true,
      persistSession: true,
      autoRefreshToken: true,
    },
  });

  var listeners = [];
  var currentUser = null;

  function emit() { listeners.forEach(function (cb) { try { cb(currentUser); } catch (_) {} }); }
  function onAuthChange(cb) { listeners.push(cb); try { cb(currentUser); } catch (_) {} }

  async function _accessToken() {
    var res = await sb.auth.getSession();
    return res && res.data && res.data.session ? res.data.session.access_token : '';
  }

  async function fetchWithAuth(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {});
    var token = await _accessToken();
    if (token) opts.headers['Authorization'] = 'Bearer ' + token;
    var res = await fetch(url, opts);
    if (res.status !== 401) return res;
    // One retry after forcing a refresh — SDK normally handles this silently.
    var r = await sb.auth.refreshSession();
    if (r.error || !r.data || !r.data.session) return res;
    opts.headers['Authorization'] = 'Bearer ' + r.data.session.access_token;
    return fetch(url, opts);
  }

  async function me() {
    var token = await _accessToken();
    if (!token) { currentUser = null; emit(); return null; }
    var r = await fetchWithAuth(API + '/auth/me');
    if (!r.ok) { currentUser = null; emit(); return null; }
    currentUser = await r.json();
    emit();
    return currentUser;
  }

  async function login(email, password) {
    var r = await sb.auth.signInWithPassword({ email: email, password: password });
    if (r.error) throw new Error(r.error.message || 'login failed');
    trackAuthSuccess('password', false);
    return await me();
  }

  async function signup(email, password, name) {
    var r = await sb.auth.signUp({
      email: email,
      password: password,
      options: {
        data: { name: name || '' },
        emailRedirectTo: window.location.origin,
      },
    });
    if (r.error) throw new Error(r.error.message || 'signup failed');
    trackAuthSuccess('password', true);
    return r.data;
  }

  async function loginMagicLink(email) {
    _pendingAuthMethod('magic_link');
    var r = await sb.auth.signInWithOtp({
      email: email,
      options: { emailRedirectTo: window.location.origin },
    });
    if (r.error) throw new Error(r.error.message || 'magic link failed');
    return r.data;
  }

  async function loginGoogle() {
    _pendingAuthMethod('google');
    var r = await sb.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    });
    if (r.error) throw new Error(r.error.message || 'google sign-in failed');
    return r.data;
  }

  async function forgot(email) {
    var r = await sb.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
    if (r && r.error) throw new Error(r.error.message || 'reset failed');
    return r && r.data;
  }

  async function logout() {
    try { await sb.auth.signOut(); } catch (_) {}
    try { localStorage.removeItem('bankauction.watchlist.v1'); } catch (_) {}
    currentUser = null;
    emit();
  }

  // Useful for debugging magic-link / recovery redirects — see whether the
  // SDK consumed the URL fragment and which event fired.
  console.debug('[auth] init URL', typeof window !== 'undefined' ? window.location.href : '');

  // React to Supabase session changes (PKCE redirects, silent refresh, logout,
  // password recovery).
  sb.auth.onAuthStateChange(function (event, session) {
    console.debug('[auth] state change', event, { hasSession: !!session });
    if (event === 'PASSWORD_RECOVERY') {
      // User clicked the reset-password email link. Supabase has applied a
      // short-lived recovery session; prompt for the new password.
      openSetPasswordModal();
      return;
    }
    if (session) {
      // Count a signup/login conversion only when this page load is the
      // completion of an OAuth / magic-link redirect — never on a plain reload
      // of a persisted session (which also emits a signed-in event). Deduped in
      // trackAuthSuccess so it can't stack with the explicit password paths.
      if (_authRedirectInbound && (event === 'SIGNED_IN' || event === 'INITIAL_SESSION')) {
        trackAuthSuccess(_pendingAuthMethod() || 'email_link');
      }
      me();
    } else {
      currentUser = null;
      emit();
    }
  });

  // ── Modals ────────────────────────────────────────────────────────────
  function ensureStyles() {
    if (document.getElementById('auth-modal-styles')) return;
    var css = '' +
      '.auth-modal-backdrop{position:fixed;inset:0;background:rgba(15,23,42,0.45);' +
      'backdrop-filter:saturate(180%) blur(2px);display:grid;place-items:center;z-index:9999;}' +
      '.auth-modal{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);' +
      'box-shadow:var(--shadow-lg);padding:24px 26px;max-width:380px;width:92%;' +
      'font-family:var(--font-body);color:var(--ink);position:relative;}' +
      '.auth-modal .x-close{position:absolute;top:10px;right:12px;background:transparent;' +
      'border:none;box-shadow:none;font-size:20px;line-height:1;cursor:pointer;padding:4px 8px;' +
      'margin:0;color:var(--muted);font-weight:500;border-radius:var(--radius-xs);}' +
      '.auth-modal .x-close:hover{color:var(--ink);background:var(--paper-2);}' +
      '.auth-modal h2{font-family:var(--font-body);font-size:22px;font-weight:700;' +
      'letter-spacing:-0.01em;margin:0 0 14px;}' +
      '.auth-modal h2 em{background:transparent;color:var(--accent);padding:0;font-style:normal;}' +
      '.auth-modal label{display:block;font-family:var(--font-body);font-size:12px;' +
      'font-weight:600;color:var(--ink-soft);margin-top:12px;}' +
      '.auth-modal input{width:100%;padding:9px 12px;border:1px solid var(--border-strong);' +
      'border-radius:var(--radius-sm);font-family:var(--font-body);font-size:14px;' +
      'background:var(--card);color:var(--ink);margin-top:5px;' +
      'transition:border-color 150ms ease, box-shadow 150ms ease;}' +
      '.auth-modal input:focus{outline:none;border-color:var(--accent);box-shadow:var(--shadow-focus);}' +
      '.auth-modal button{margin-top:14px;padding:9px 16px;background:var(--accent);' +
      'border:1px solid transparent;border-radius:var(--radius-sm);color:var(--on-accent);' +
      'font-family:var(--font-body);font-size:14px;font-weight:600;cursor:pointer;' +
      'transition:background 150ms ease;}' +
      '.auth-modal button:hover{background:var(--accent-hover);}' +
      '.auth-modal .sec{margin-left:8px;background:var(--card);color:var(--ink-soft);' +
      'border:1px solid var(--border-strong);}' +
      '.auth-modal .sec:hover{background:var(--paper-2);color:var(--ink);}' +
      '.auth-modal .alt{display:flex;flex-direction:column;gap:8px;margin:14px 0;}' +
      '.auth-modal .alt button{margin:0;width:100%;background:var(--card);color:var(--ink);' +
      'border:1px solid var(--border-strong);font-weight:500;}' +
      '.auth-modal .alt button:hover{background:var(--paper-2);}' +
      '.auth-modal .alt button.g{background:var(--card);}' +
      '.auth-modal .sep{display:flex;align-items:center;gap:10px;margin:14px 0;' +
      'font-family:var(--font-body);font-size:11px;font-weight:500;color:var(--muted);' +
      'text-transform:uppercase;letter-spacing:0.04em;}' +
      '.auth-modal .sep::before,.auth-modal .sep::after{content:"";flex:1;height:1px;background:var(--border);}' +
      '.auth-modal .msg{font-family:var(--font-body);font-size:12.5px;margin:12px 0 0;min-height:1em;}' +
      '.auth-modal .msg.err{color:var(--danger);font-weight:600;}' +
      '.auth-modal .msg.ok{color:var(--good);font-weight:600;}' +
      '.auth-modal .link{font-family:var(--font-body);font-size:12px;color:var(--accent);' +
      'cursor:pointer;text-decoration:none;}' +
      '.auth-modal .link:hover{text-decoration:underline;}' +
      '.auth-slot .user-menu{display:flex;align-items:center;gap:8px;' +
      'font-family:var(--font-body);font-size:13px;}' +
      '.auth-slot .user-menu .avatar{background:var(--accent);color:var(--on-accent);border:0;' +
      'border-radius:50%;width:32px;height:32px;display:grid;place-items:center;' +
      'font-weight:600;font-size:13px;cursor:pointer;}' +
      '.auth-slot .user-menu details[open] summary~div{display:block;}' +
      '.auth-slot details{position:relative;}' +
      '.auth-slot details summary{list-style:none;cursor:pointer;}' +
      '.auth-slot details summary::-webkit-details-marker{display:none;}' +
      '.auth-slot details .dropdown{position:absolute;right:0;top:42px;background:var(--card);' +
      'border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-lg);' +
      'min-width:170px;z-index:50;overflow:hidden;padding:4px;}' +
      '.auth-slot details .dropdown a{display:block;padding:8px 12px;color:var(--ink);' +
      'text-decoration:none;font-family:var(--font-body);font-size:13px;' +
      'border-radius:var(--radius-xs);cursor:pointer;}' +
      '.auth-slot details .dropdown a:hover{background:var(--paper-2);}' +
      '.auth-slot .sign-in{background:var(--accent);border:1px solid transparent;' +
      'border-radius:var(--radius-sm);padding:7px 14px;font-family:var(--font-body);' +
      'font-size:13px;font-weight:600;cursor:pointer;color:var(--on-accent);' +
      'transition:background 150ms ease;}' +
      '.auth-slot .sign-in:hover{background:var(--accent-hover);}' +
      '.auth-slot .user-menu .avatar.guest{background:var(--paper-2);color:var(--ink-soft);' +
      'border:1px solid var(--border);}' +
      '.auth-slot details .dropdown a.primary{color:var(--accent);font-weight:600;}' +
      '.auth-slot details .dropdown a.plan-pro{color:var(--good,#16a34a);font-weight:600;cursor:default;}' +
      '.auth-slot details .dropdown a.plan-pro:hover{background:transparent;}';
    var style = document.createElement('style');
    style.id = 'auth-modal-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  var _escHandler = null;

  function closeModal() {
    var b = document.getElementById('auth-modal-backdrop');
    if (b) b.remove();
    if (_escHandler) {
      document.removeEventListener('keydown', _escHandler);
      _escHandler = null;
    }
  }

  function openModal(builder) {
    ensureStyles();
    closeModal();
    var bd = document.createElement('div');
    bd.id = 'auth-modal-backdrop';
    bd.className = 'auth-modal-backdrop';
    // Outside-click dismiss intentionally disabled — only ✕ button, Cancel
    // button, or Escape key closes a modal, so an accidental background
    // click does not wipe a half-typed credential form.
    var box = document.createElement('div');
    box.className = 'auth-modal';
    bd.appendChild(box);
    builder(box);
    document.body.appendChild(bd);
    _escHandler = function (e) { if (e.key === 'Escape') closeModal(); };
    document.addEventListener('keydown', _escHandler);
  }

  var X_CLOSE = '<button class="x-close" aria-label="Close" title="Close">\u00d7</button>';

  function openLoginModal() {
    openModal(function (box) {
      box.innerHTML = '' + X_CLOSE +
        '<h2><em>Sign in</em></h2>' +
        '<div class="alt">' +
          '<button class="g" id="m-google">Continue with Google</button>' +
          '<button class="g" id="m-magic">Email me a magic link</button>' +
        '</div>' +
        '<div class="sep">or with password</div>' +
        '<form autocomplete="off" onsubmit="return false">' +
          // readonly + onfocus is the reliable cross-browser way to suppress
          // saved-credential autofill — Chrome ignores autocomplete="off" on
          // login-shaped forms, which misleads Google/magic-link users into
          // submitting stale saved passwords from other sites.
          '<label>Email<input id="m-email" type="email" autocomplete="off" required readonly onfocus="this.removeAttribute(\'readonly\')"></label>' +
          '<label>Password<input id="m-pw" type="password" autocomplete="off" required readonly onfocus="this.removeAttribute(\'readonly\')"></label>' +
        '</form>' +
        '<div><button id="m-submit">Sign in</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>' +
        '<p><span class="link" id="to-signup">Create account</span> · <span class="link" id="to-forgot">Forgot password?</span></p>';
      box.querySelector('.x-close').onclick = closeModal;
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try {
          await login(box.querySelector('#m-email').value, box.querySelector('#m-pw').value);
          closeModal();
        } catch (e) {
          console.error('[auth] login', e);
          var txt = e.message || '';
          if (/invalid login credentials/i.test(txt)) {
            txt = "No password is set for this email. If you signed up with Google, click 'Continue with Google' above. Otherwise, use 'Forgot password?' to set one.";
          }
          msg.textContent = txt; msg.className = 'msg err';
        }
      };
      box.querySelector('#m-google').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try { await loginGoogle(); } catch (e) {
          console.error('[auth] google', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
      box.querySelector('#m-magic').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        var email = box.querySelector('#m-email').value;
        if (!email) { msg.textContent = 'Enter your email first'; msg.className = 'msg err'; return; }
        try {
          await loginMagicLink(email);
          msg.textContent = 'Check your email for a sign-in link.';
          msg.className = 'msg ok';
        } catch (e) {
          console.error('[auth] magic', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
      box.querySelector('#to-signup').onclick = openSignupModal;
      box.querySelector('#to-forgot').onclick = openForgotModal;
    });
  }

  function openSignupModal() {
    openModal(function (box) {
      box.innerHTML = '' + X_CLOSE +
        '<h2><em>Create</em> account</h2>' +
        '<div class="alt">' +
          '<button class="g" id="m-google">Continue with Google</button>' +
        '</div>' +
        '<div class="sep">or with email + password</div>' +
        '<form autocomplete="off" onsubmit="return false">' +
          '<label>Name<input id="m-name" autocomplete="off" required></label>' +
          '<label>Email<input id="m-email" type="email" autocomplete="off" required></label>' +
          '<label>Password (8+ chars)<input id="m-pw" type="password" autocomplete="new-password" required minlength="8"></label>' +
        '</form>' +
        '<div><button id="m-submit">Sign up</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>' +
        '<p><span class="link" id="to-login">Have an account? Sign in</span></p>';
      box.querySelector('#m-name').value = '';
      box.querySelector('#m-email').value = '';
      box.querySelector('#m-pw').value = '';
      box.querySelector('.x-close').onclick = closeModal;
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-google').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try { await loginGoogle(); } catch (e) {
          console.error('[auth] google', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try {
          await signup(
            box.querySelector('#m-email').value,
            box.querySelector('#m-pw').value,
            box.querySelector('#m-name').value,
          );
          msg.textContent = 'Check your email to confirm your account.';
          msg.className = 'msg ok';
        } catch (e) {
          console.error('[auth] signup', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
      box.querySelector('#to-login').onclick = openLoginModal;
    });
  }

  function openForgotModal() {
    openModal(function (box) {
      box.innerHTML = '' + X_CLOSE +
        '<h2><em>Reset</em> password</h2>' +
        '<form autocomplete="off" onsubmit="return false">' +
          '<label>Email<input id="m-email" type="email" autocomplete="off" required></label>' +
        '</form>' +
        '<div><button id="m-submit">Send reset link</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>';
      box.querySelector('#m-email').value = '';
      box.querySelector('.x-close').onclick = closeModal;
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try {
          await forgot(box.querySelector('#m-email').value);
          msg.textContent = 'If that email exists, a reset link has been sent.';
          msg.className = 'msg ok';
        } catch (e) {
          console.error('[auth] forgot', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
    });
  }

  function openSetPasswordModal() {
    openModal(function (box) {
      box.innerHTML = '' + X_CLOSE +
        '<h2><em>Set</em> new password</h2>' +
        '<form autocomplete="off" onsubmit="return false">' +
          '<label>New password (8+ chars)<input id="m-pw" type="password" autocomplete="new-password" required minlength="8"></label>' +
          '<label>Confirm password<input id="m-pw2" type="password" autocomplete="new-password" required minlength="8"></label>' +
        '</form>' +
        '<div><button id="m-submit">Save password</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>';
      box.querySelector('#m-pw').value = '';
      box.querySelector('#m-pw2').value = '';
      box.querySelector('.x-close').onclick = closeModal;
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        var pw = box.querySelector('#m-pw').value;
        var pw2 = box.querySelector('#m-pw2').value;
        if (pw.length < 8) { msg.textContent = 'Password must be at least 8 characters.'; msg.className = 'msg err'; return; }
        if (pw !== pw2) { msg.textContent = 'Passwords do not match.'; msg.className = 'msg err'; return; }
        try {
          var r = await sb.auth.updateUser({ password: pw });
          if (r && r.error) throw new Error(r.error.message || 'update failed');
          msg.textContent = 'Password updated. You are now signed in.';
          msg.className = 'msg ok';
          await me();
          setTimeout(closeModal, 1200);
        } catch (e) {
          console.error('[auth] updateUser', e);
          msg.textContent = e.message; msg.className = 'msg err';
        }
      };
    });
  }

  // ── Header slot hydration ─────────────────────────────────────────────
  function hydrateSlot() {
    ensureStyles();
    var slot = document.getElementById('auth-slot');
    if (!slot) return;
    slot.className = 'auth-slot';
    slot.innerHTML = '';
    var closeMenu = function () { var d = slot.querySelector('details'); if (d) d.open = false; };
    var openFeedback = function (e) { e.preventDefault(); closeMenu(); if (window.openGeneralFeedback) window.openGeneralFeedback(); };
    if (!currentUser) {
      var personIcon = '<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="7" r="3.2"/><path d="M3.8 16.6c0-3 2.8-4.9 6.2-4.9s6.2 1.9 6.2 4.9"/></svg>';
      slot.innerHTML =
        '<details>' +
          '<summary><div class="user-menu"><div class="avatar guest" title="Account">' + personIcon + '</div></div></summary>' +
          '<div class="dropdown">' +
            '<a data-act="signin" class="primary">Sign in</a>' +
            '<a data-act="feedback">Send feedback</a>' +
          '</div>' +
        '</details>';
      slot.querySelector('[data-act="signin"]').onclick = function (e) { e.preventDefault(); closeMenu(); openLoginModal(); };
      slot.querySelector('[data-act="feedback"]').onclick = openFeedback;
      return;
    }
    var initials = (currentUser.name || currentUser.email || '?').trim().slice(0, 1).toUpperCase();
    var isAdmin = currentUser.role === 'admin';
    var isPaid = currentUser.tier === 'paid';
    var planLine = isPaid
      ? '<a data-act="plan" class="plan-pro">✦ Pro' +
          (currentUser.plan_expires_at ? ' · until ' + escapeHtml(fmtPlanDate(currentUser.plan_expires_at)) : '') + '</a>'
      : '<a data-act="upgrade" class="primary">Upgrade to Pro ✦</a>';
    slot.innerHTML =
      '<details>' +
        '<summary><div class="user-menu"><div class="avatar" title="' + escapeAttr(currentUser.email) + '">' + initials + '</div></div></summary>' +
        '<div class="dropdown">' +
          '<a data-act="who">' + escapeHtml(currentUser.email) + '</a>' +
          planLine +
          (isAdmin ? '<a href="/admin">Admin</a>' : '') +
          (isAdmin ? '<a href="/review">Review</a>' : '') +
          '<a data-act="feedback">Send feedback</a>' +
          '<a data-act="logout">Sign out</a>' +
        '</div>' +
      '</details>';
    if (!isPaid) {
      slot.querySelector('[data-act="upgrade"]').onclick = function (e) {
        e.preventDefault(); closeMenu();
        if (window.Billing && window.Billing.openCheckout) window.Billing.openCheckout();
      };
    }
    slot.querySelector('[data-act="feedback"]').onclick = openFeedback;
    slot.querySelector('[data-act="logout"]').onclick = async function (e) {
      e.preventDefault(); await logout();
    };
  }

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }

  function fmtPlanDate(iso) {
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return '';
      return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
    } catch (_) { return ''; }
  }

  onAuthChange(hydrateSlot);

  // ── Public API ────────────────────────────────────────────────────────
  window.Auth = {
    fetchWithAuth: fetchWithAuth,
    login: login,
    signup: signup,
    loginMagicLink: loginMagicLink,
    loginGoogle: loginGoogle,
    logout: logout,
    me: me,
    forgot: forgot,
    openLoginModal: openLoginModal,
    openSignupModal: openSignupModal,
    openForgotModal: openForgotModal,
    onAuthChange: onAuthChange,
    getUser: function () { return currentUser; },
    getRole: function () { return currentUser ? currentUser.role : null; },
    _supabase: sb,
  };

  // Auto-hydrate on load. The onAuthStateChange subscription above will fire
  // for any existing persisted session and call me() on our behalf, but we
  // still kick an initial me() here for pages that open without a session
  // event (e.g. nothing in storage, anon).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { me(); });
  } else {
    me();
  }
})();
