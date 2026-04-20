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
    return r.data;
  }

  async function loginMagicLink(email) {
    var r = await sb.auth.signInWithOtp({
      email: email,
      options: { emailRedirectTo: window.location.origin },
    });
    if (r.error) throw new Error(r.error.message || 'magic link failed');
    return r.data;
  }

  async function loginGoogle() {
    var r = await sb.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    });
    if (r.error) throw new Error(r.error.message || 'google sign-in failed');
    return r.data;
  }

  async function forgot(email) {
    await sb.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
  }

  async function logout() {
    try { await sb.auth.signOut(); } catch (_) {}
    currentUser = null;
    emit();
  }

  // React to Supabase session changes (PKCE redirects, silent refresh, logout).
  sb.auth.onAuthStateChange(function (_event, session) {
    if (session) {
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
      '.auth-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.45);' +
      'display:grid;place-items:center;z-index:9999;}' +
      '.auth-modal{background:#fff;border:2px solid #1a1a1a;box-shadow:2px 3px 0 rgba(0,0,0,0.9);' +
      'padding:22px 24px;max-width:380px;width:92%;font-family:\'Kalam\',cursive;color:#1a1a1a;}' +
      '.auth-modal h2{font-family:\'Caveat\',cursive;font-size:28px;margin:0 0 10px;}' +
      '.auth-modal h2 em{background:#ffd84d;padding:0 6px;font-style:normal;}' +
      '.auth-modal label{display:block;font-family:\'IBM Plex Mono\',monospace;font-size:12px;margin-top:10px;}' +
      '.auth-modal input{width:100%;padding:8px 10px;border:2px solid #1a1a1a;' +
      'font-family:\'IBM Plex Mono\',monospace;font-size:13px;background:#fff;color:#1a1a1a;}' +
      '.auth-modal button{margin-top:12px;padding:8px 14px;background:#ffd84d;border:2px solid #1a1a1a;' +
      'color:#1a1a1a;font-family:\'IBM Plex Mono\',monospace;font-size:13px;font-weight:600;' +
      'box-shadow:2px 3px 0 rgba(0,0,0,0.9);cursor:pointer;}' +
      '.auth-modal .sec{margin-left:8px;background:#fff;}' +
      '.auth-modal .alt{display:flex;flex-direction:column;gap:8px;margin:12px 0;}' +
      '.auth-modal .alt button{margin:0;width:100%;background:#fff;}' +
      '.auth-modal .alt button.g{background:#fff;}' +
      '.auth-modal .sep{display:flex;align-items:center;gap:8px;margin:10px 0;' +
      'font-family:\'IBM Plex Mono\',monospace;font-size:11px;color:#666;}' +
      '.auth-modal .sep::before,.auth-modal .sep::after{content:"";flex:1;height:1px;background:#ddd;}' +
      '.auth-modal .msg{font-family:\'IBM Plex Mono\',monospace;font-size:12px;margin:10px 0 0;min-height:1em;}' +
      '.auth-modal .msg.err{color:#d64a2e;font-weight:600;}' +
      '.auth-modal .msg.ok{color:#2e8b57;font-weight:600;}' +
      '.auth-modal .link{font-family:\'IBM Plex Mono\',monospace;font-size:11px;cursor:pointer;text-decoration:underline;}' +
      '.auth-slot .user-menu{display:flex;align-items:center;gap:8px;font-family:\'IBM Plex Mono\',monospace;font-size:12px;}' +
      '.auth-slot .user-menu .avatar{background:#ffd84d;border:2px solid #1a1a1a;width:32px;height:32px;' +
      'display:grid;place-items:center;font-weight:700;cursor:pointer;}' +
      '.auth-slot .user-menu details[open] summary~div{display:block;}' +
      '.auth-slot details{position:relative;}' +
      '.auth-slot details summary{list-style:none;cursor:pointer;}' +
      '.auth-slot details summary::-webkit-details-marker{display:none;}' +
      '.auth-slot details .dropdown{position:absolute;right:0;top:38px;background:#fff;border:2px solid #1a1a1a;' +
      'box-shadow:2px 3px 0 rgba(0,0,0,0.9);min-width:160px;z-index:50;}' +
      '.auth-slot details .dropdown a{display:block;padding:8px 12px;color:#1a1a1a;text-decoration:none;' +
      'font-family:\'IBM Plex Mono\',monospace;font-size:12px;border-bottom:1px dashed rgba(0,0,0,0.15);cursor:pointer;}' +
      '.auth-slot details .dropdown a:hover{background:#faf7f0;}' +
      '.auth-slot .sign-in{background:#ffd84d;border:2px solid #1a1a1a;padding:6px 12px;' +
      'font-family:\'IBM Plex Mono\',monospace;font-size:12px;font-weight:600;cursor:pointer;' +
      'box-shadow:2px 3px 0 rgba(0,0,0,0.9);color:#1a1a1a;}';
    var style = document.createElement('style');
    style.id = 'auth-modal-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function closeModal() {
    var b = document.getElementById('auth-modal-backdrop');
    if (b) b.remove();
  }

  function openModal(builder) {
    ensureStyles();
    closeModal();
    var bd = document.createElement('div');
    bd.id = 'auth-modal-backdrop';
    bd.className = 'auth-modal-backdrop';
    bd.addEventListener('click', function (e) { if (e.target === bd) closeModal(); });
    var box = document.createElement('div');
    box.className = 'auth-modal';
    bd.appendChild(box);
    builder(box);
    document.body.appendChild(bd);
  }

  function openLoginModal() {
    openModal(function (box) {
      box.innerHTML = '' +
        '<h2><em>Sign in</em></h2>' +
        '<div class="alt">' +
          '<button class="g" id="m-google">Continue with Google</button>' +
          '<button class="g" id="m-magic">Email me a magic link</button>' +
        '</div>' +
        '<div class="sep">or with password</div>' +
        '<label>Email<input id="m-email" type="email" autocomplete="email" required></label>' +
        '<label>Password<input id="m-pw" type="password" autocomplete="current-password" required></label>' +
        '<div><button id="m-submit">Sign in</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>' +
        '<p><span class="link" id="to-signup">Create account</span> · <span class="link" id="to-forgot">Forgot password?</span></p>';
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try {
          await login(box.querySelector('#m-email').value, box.querySelector('#m-pw').value);
          closeModal();
        } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
      };
      box.querySelector('#m-google').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try { await loginGoogle(); } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
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
        } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
      };
      box.querySelector('#to-signup').onclick = openSignupModal;
      box.querySelector('#to-forgot').onclick = openForgotModal;
    });
  }

  function openSignupModal() {
    openModal(function (box) {
      box.innerHTML = '' +
        '<h2><em>Create</em> account</h2>' +
        '<div class="alt">' +
          '<button class="g" id="m-google">Continue with Google</button>' +
        '</div>' +
        '<div class="sep">or with email + password</div>' +
        '<label>Name<input id="m-name" required></label>' +
        '<label>Email<input id="m-email" type="email" required></label>' +
        '<label>Password (8+ chars)<input id="m-pw" type="password" required minlength="8"></label>' +
        '<div><button id="m-submit">Sign up</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>' +
        '<p><span class="link" id="to-login">Have an account? Sign in</span></p>';
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-google').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        msg.textContent = ''; msg.className = 'msg';
        try { await loginGoogle(); } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
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
        } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
      };
      box.querySelector('#to-login').onclick = openLoginModal;
    });
  }

  function openForgotModal() {
    openModal(function (box) {
      box.innerHTML = '' +
        '<h2><em>Reset</em> password</h2>' +
        '<label>Email<input id="m-email" type="email" required></label>' +
        '<div><button id="m-submit">Send reset link</button><button class="sec" id="m-cancel">Cancel</button></div>' +
        '<p class="msg" id="m-msg"></p>';
      box.querySelector('#m-cancel').onclick = closeModal;
      box.querySelector('#m-submit').onclick = async function () {
        var msg = box.querySelector('#m-msg');
        await forgot(box.querySelector('#m-email').value);
        msg.textContent = 'If that email exists, a reset link has been sent.';
        msg.className = 'msg ok';
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
    if (!currentUser) {
      var btn = document.createElement('button');
      btn.className = 'sign-in';
      btn.textContent = 'Sign in';
      btn.onclick = openLoginModal;
      slot.appendChild(btn);
      return;
    }
    var initials = (currentUser.name || currentUser.email || '?').trim().slice(0, 1).toUpperCase();
    var isAdmin = currentUser.role === 'admin';
    slot.innerHTML =
      '<details>' +
        '<summary><div class="user-menu"><div class="avatar" title="' + escapeAttr(currentUser.email) + '">' + initials + '</div></div></summary>' +
        '<div class="dropdown">' +
          '<a data-act="who">' + escapeHtml(currentUser.email) + '</a>' +
          (isAdmin ? '<a href="/admin">Admin</a>' : '') +
          '<a data-act="logout">Sign out</a>' +
        '</div>' +
      '</details>';
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
