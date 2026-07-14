// Auction-alert email capture (the landing-screen signup bar).
//
// Posts an email to POST /alerts/subscribe — the lead-capture hook from the
// marketing plan (§5 Activation / §6 Retention). Anonymous, no auth. This
// only builds the subscriber list; sending the alerts is a separate, later
// piece. Fires the GA4 `alert_subscribe` event on success so the capture is
// measurable in the funnel (index.html wires window.track).
(function () {
  'use strict';

  var form = document.getElementById('alert-capture-form');
  if (!form) return;

  var API = (typeof window !== 'undefined' && window.API_BASE) || '';
  var email = document.getElementById('alert-capture-email');
  var btn = document.getElementById('alert-capture-btn');
  var msg = document.getElementById('alert-capture-msg');
  var done = false;

  function show(text, ok) {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = false;
    msg.className = 'ac-msg' + (ok ? ' ok' : ' err');
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (done) return;
    var value = (email && email.value || '').trim();
    // Light client check; the server (EmailStr) is the real validator.
    if (!value || value.indexOf('@') < 1 || value.indexOf('.') < 0) {
      show('enter a valid email', false);
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = 'adding…'; }

    fetch(API + '/alerts/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: value, source: 'home' }),
    }).then(function (res) {
      if (!res.ok) throw new Error('subscribe failed');
      done = true;
      form.hidden = true;
      show("you're on the list — we'll email you when new auctions match.", true);
      if (window.track) window.track('alert_subscribe', { source: 'home' });
    }).catch(function () {
      if (btn) { btn.disabled = false; btn.textContent = 'notify me'; }
      show('could not add you just now — please try again.', false);
    });
  });
})();
