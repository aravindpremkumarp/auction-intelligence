/* Auctionscope social motion kit — JS.
 *
 * Adapted from motion-anything (github.com/nexu-io/motion-anything, Apache-2.0)
 * recipes: count-up, kinetic-headline (splitter). Adjustments for Auctionscope:
 *   - count-up formats Indian currency (₹, en-IN grouping, lakh/crore compact)
 *     and autostarts on load (render/video-safe — the upstream recipe waits on
 *     IntersectionObserver, which never fires in a fixed-viewport capture).
 *   - AS.bind(): fills [data-field] slots from the template's JSON data island,
 *     so the scheduled poster script only ever rewrites the island.
 *   - Signals readiness via [data-render-ready] on <html> once fonts are loaded
 *     and entrance animations have settled — marketing/render_social.py waits
 *     for it before screenshotting.
 *
 * Dependency-free. See marketing/templates/ATTRIBUTION.md.
 */
(function () {
  'use strict';

  var AS = (window.AS = window.AS || {});

  /* ── Indian number formatting ─────────────────────────────────────────── */

  function trimNum(n) {
    // 2 decimals max, trailing zeros stripped: 1.5, 1.25, 12
    return String(Math.round(n * 100) / 100);
  }

  AS.formatINR = function (n, style) {
    n = Number(n) || 0;
    if (style === 'plain') return Math.round(n).toLocaleString('en-IN');
    if (n >= 1e7) return '₹' + trimNum(n / 1e7) + ' Cr';
    if (n >= 1e5) return '₹' + trimNum(n / 1e5) + ' L';
    return '₹' + Math.round(n).toLocaleString('en-IN');
  };

  AS.format = function (value, fmt) {
    if (fmt === 'inr') return AS.formatINR(value);
    if (fmt === 'inr-plain') return '₹' + AS.formatINR(value, 'plain');
    if (fmt === 'number') return Number(value).toLocaleString('en-IN');
    if (fmt === 'pct') return trimNum(Number(value)) + '%';
    return String(value);
  };

  /* ── data island → [data-field] binder ────────────────────────────────── */

  function get(obj, path) {
    return path.split('.').reduce(function (o, k) {
      return o == null ? undefined : o[k];
    }, obj);
  }

  AS.data = null;

  AS.bind = function (root) {
    var island = document.getElementById('data');
    if (!island) return null;
    var data = (AS.data = JSON.parse(island.textContent));
    (root || document).querySelectorAll('[data-field]').forEach(function (el) {
      var value = get(data, el.getAttribute('data-field'));
      if (value === undefined || value === null) return;
      var fmt = el.getAttribute('data-field-format');
      if (el.hasAttribute('data-count')) {
        // numeric slot animated by count-up: set the target, not the text
        el.setAttribute('data-count', value);
        el.setAttribute('data-count-format', fmt || 'number');
      } else {
        el.textContent = AS.format(value, fmt);
      }
    });
    return data;
  };

  /* ── count-up (video-safe autostart) ──────────────────────────────────── */

  function runCount(el) {
    var target = parseFloat(el.getAttribute('data-count')) || 0;
    var fmt = el.getAttribute('data-count-format') || 'number';
    var dur = parseInt(el.getAttribute('data-count-duration'), 10) || 900;
    var reduce =
      window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce || dur <= 0) {
      el.textContent = AS.format(target, fmt);
      return Promise.resolve();
    }
    return new Promise(function (resolve) {
      var start = null;
      function step(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3); // ease-out cubic
        el.textContent = AS.format(target * eased, fmt);
        if (p < 1) requestAnimationFrame(step);
        else resolve();
      }
      requestAnimationFrame(step);
    });
  }

  /* ── kinetic-headline splitter ────────────────────────────────────────── */

  function splitKinetic(el) {
    var byLetter = el.getAttribute('data-kinetic') === 'letters';
    var anim = el.getAttribute('data-kinetic-anim') || 'rise';
    var stagger = parseInt(el.getAttribute('data-kinetic-stagger'), 10) ||
      (byLetter ? 26 : 90);
    el.classList.add('k-anim-' + anim);
    var units = byLetter
      ? el.textContent.split('')
      : el.textContent.split(/\s+/);
    el.textContent = '';
    var i = 0;
    units.forEach(function (u) {
      if (u === ' ' || (!byLetter && u === '')) return;
      if (byLetter && u === ' ') {
        var sp = document.createElement('span');
        sp.className = 'k-space';
        el.appendChild(sp);
        return;
      }
      var s = document.createElement('span');
      s.className = 'k-unit';
      s.style.setProperty('--k-delay', i * stagger + 'ms');
      s.textContent = u;
      el.appendChild(s);
      if (!byLetter) el.appendChild(Object.assign(
        document.createElement('span'), { className: 'k-space' }));
      i += 1;
    });
    return i * stagger + 560; // settle time
  }

  /* ── boot: bind data, split headlines, play entrances, flag readiness ─── */

  function boot() {
    AS.bind();

    var settle = 0;
    document.querySelectorAll('[data-kinetic]').forEach(function (el) {
      settle = Math.max(settle, splitKinetic(el));
    });

    var counts = [];
    document.querySelectorAll('[data-count]').forEach(function (el) {
      counts.push(runCount(el));
    });

    // next frame: trigger CSS entrance transitions
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        document
          .querySelectorAll('[data-kinetic], [data-stagger]')
          .forEach(function (el) { el.classList.add('is-in'); });
      });
    });

    var fonts = document.fonts && document.fonts.ready
      ? document.fonts.ready : Promise.resolve();

    Promise.all([fonts, Promise.all(counts)]).then(function () {
      setTimeout(function () {
        document.documentElement.setAttribute('data-render-ready', '1');
      }, settle + 250);
    });
  }

  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
