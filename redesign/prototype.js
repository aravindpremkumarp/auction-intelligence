/* =====================================================================
   Auctionscope prototype — data + interactions (vanilla JS)
   ===================================================================== */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ---------- mock data ---------- */
  const BASE = new Date(2026, 5, 7); // Jun 7 2026
  const TYPES = {
    flat: 'Flat / Apartment', house: 'Independent House', villa: 'Villa',
    land: 'Land / Plot', commercial: 'Commercial', industrial: 'Industrial', godown: 'Warehouse / Godown'
  };
  const P = [
    { id: 'p1', t: 'flat', title: '3BHK Apartment, Prestige Bella Vista, 1,180 sqft', loc: 'Anna Nagar', dist: 'Chennai', bank: 'State Bank of India', reserve: 68.5, emd: 6.85, days: 2, time: '11:00 AM', drop: 4.2, sqft: 1180 },
    { id: 'p2', t: 'house', title: 'Independent House, 2,400 sqft plot, RS Puram', loc: 'RS Puram', dist: 'Coimbatore', bank: 'HDFC Bank', reserve: 142, emd: 14.2, days: 9, time: '3:00 PM', sqft: 2400 },
    { id: 'p3', t: 'commercial', title: 'Commercial Shop, Ground Floor, 850 sqft', loc: 'Town Hall', dist: 'Madurai', bank: 'Canara Bank', reserve: 32, emd: 3.2, days: 5, time: '12:00 PM', drop: 3, sqft: 850 },
    { id: 'p4', t: 'flat', title: '2BHK Flat, Lakeview Residency, 940 sqft', loc: 'Velachery', dist: 'Chennai', bank: 'Indian Bank', reserve: 47.5, emd: 4.75, days: 14, time: '10:30 AM', sqft: 940 },
    { id: 'p5', t: 'land', title: 'Residential Plot, DTCP approved, 3,600 sqft', loc: 'Thudiyalur', dist: 'Coimbatore', bank: 'Union Bank of India', reserve: 58, emd: 5.8, days: 21, time: '11:00 AM', sqft: 3600 },
    { id: 'p6', t: 'villa', title: 'Luxury Villa, 4BHK, gated community, 3,100 sqft', loc: 'ECR', dist: 'Chennai', bank: 'ICICI Bank', reserve: 285, emd: 28.5, days: 11, time: '2:00 PM', sqft: 3100 },
    { id: 'p7', t: 'house', title: 'Independent House, 1,500 sqft, near bypass', loc: 'Srirangam', dist: 'Tiruchirappalli', bank: 'Bank of Baroda', reserve: 39, emd: 3.9, days: 3, time: '11:30 AM', drop: 5, sqft: 1500 },
    { id: 'p8', t: 'commercial', title: 'Office Space, 2nd Floor, 1,600 sqft, IT corridor', loc: 'OMR', dist: 'Chennai', bank: 'HDFC Bank', reserve: 96, emd: 9.6, days: 7, time: '4:00 PM', sqft: 1600 },
    { id: 'p9', t: 'godown', title: 'Warehouse / Godown, 8,000 sqft, highway access', loc: 'Sankari', dist: 'Salem', bank: 'State Bank of India', reserve: 74, emd: 7.4, days: 18, time: '11:00 AM', sqft: 8000 },
    { id: 'p10', t: 'industrial', title: 'Industrial Shed, 12,000 sqft, SIDCO estate', loc: 'Perundurai', dist: 'Erode', bank: 'Indian Overseas Bank', reserve: 118, emd: 11.8, days: 25, time: '12:30 PM', sqft: 12000 },
    { id: 'p11', t: 'flat', title: '3BHK Apartment, Marina Heights, 1,340 sqft', loc: 'Adyar', dist: 'Chennai', bank: 'Canara Bank', reserve: 89, emd: 8.9, days: 6, time: '10:00 AM', sqft: 1340 },
    { id: 'p12', t: 'land', title: 'Agricultural Land, 1.2 acres, near ring road', loc: 'Othakadai', dist: 'Madurai', bank: 'Indian Bank', reserve: 44, emd: 4.4, days: 30, time: '11:00 AM', sqft: 52000 },
    { id: 'p13', t: 'house', title: 'Independent House, 1,800 sqft, corner plot', loc: 'Palayamkottai', dist: 'Tirunelveli', bank: 'Union Bank of India', reserve: 36.5, emd: 3.65, days: 1, time: '11:00 AM', sqft: 1800 },
    { id: 'p14', t: 'commercial', title: 'Retail Showroom, Main Road frontage, 2,200 sqft', loc: 'Gandhipuram', dist: 'Coimbatore', bank: 'ICICI Bank', reserve: 156, emd: 15.6, days: 13, time: '3:30 PM', drop: 12, sqft: 2200 }
  ];

  /* ---------- formatting ---------- */
  function fmt(lakh) {
    if (lakh >= 100) { let c = lakh / 100; return '₹' + (c % 1 === 0 ? c : c.toFixed(2).replace(/0$/, '')) + 'Cr'; }
    return '₹' + (lakh % 1 === 0 ? lakh : lakh.toFixed(1)) + 'L';
  }
  function dateOf(days) { const d = new Date(BASE); d.setDate(d.getDate() + days); return d; }
  function fmtDate(days) { return dateOf(days).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); }
  function fmtDateFull(days) { return dateOf(days).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }); }
  function statusOf(days) {
    if (days <= 3) return { cls: 'danger', label: days <= 1 ? (days <= 0 ? 'Today' : 'Tomorrow') : days + ' days left' };
    if (days <= 10) return { cls: 'warning', label: 'Closing soon' };
    return { cls: 'success', label: 'Upcoming' };
  }
  const ico = (id) => `<svg><use href="#i-${id}"/></svg>`;

  /* ---------- state ---------- */
  const saved = new Set(JSON.parse(localStorage.getItem('as-saved') || '[]'));
  function persistSaved() { localStorage.setItem('as-saved', JSON.stringify([...saved])); updateSaveCount(); }
  const filters = { q: '', dist: [], t: [], bank: [], price: '', sort: 'date_asc' };
  let detailReturn = 'landing';

  /* ---------- property card (Variant A — list row) ---------- */
  function card(p, opts = {}) {
    const st = statusOf(p.days);
    const isSaved = saved.has(p.id);
    const drop = p.drop ? `<span class="price-drop">▼ ${fmt(p.drop)}</span>` : '';
    const urgent = opts.urgent && st.cls === 'danger' ? ' urgent' : '';
    return `
    <div class="prop${urgent}" data-id="${p.id}" role="button" tabindex="0">
      <button class="card-save${isSaved ? ' saved' : ''}" data-save="${p.id}" aria-label="Save to watchlist">${ico(isSaved ? 'star-fill' : 'star')}</button>
      <div class="thumb">${ico(p.t)}</div>
      <div class="meta">
        <div class="title">${esc(p.title)}</div>
        <div class="loc">${ico('pin')} ${esc(p.loc)}, ${esc(p.dist)}</div>
        <div class="tag-row">
          <span class="tag bank">${esc(p.bank)}</span>
          <span class="status ${st.cls}"><span class="badge-dot"></span> ${st.label}</span>
          ${drop}
        </div>
        <div class="card-foot">
          <span class="auction-when">${ico('cal')} ${fmtDate(p.days)}, ${p.time}</span>
          <span class="price">${fmt(p.reserve)} <small>Reserve</small></span>
        </div>
      </div>
    </div>`;
  }

  /* ---------- navigation ---------- */
  function nav(screen) {
    $$('.screen').forEach(s => s.classList.toggle('on', s.dataset.screen === screen));
    $$('.top-nav button').forEach(b => b.classList.toggle('on', b.dataset.nav === screen));
    $$('.bottom-tabs .bt').forEach(b => b.classList.toggle('on', b.dataset.nav === screen));
    const host = $('.screen-host'); if (host) host.scrollTop = 0;
    window.scrollTo(0, 0);
    if (screen === 'watchlist') renderWatchlist();
  }
  document.addEventListener('click', e => {
    const n = e.target.closest('[data-nav]');
    if (n) { e.preventDefault(); nav(n.dataset.nav); }
  });

  /* ---------- theme ---------- */
  const root = document.documentElement, tbtn = $('#theme-toggle');
  function setTheme(t) {
    root.setAttribute('data-theme', t); localStorage.setItem('as-theme', t);
    tbtn.querySelector('use').setAttribute('href', t === 'dark' ? '#i-sun' : '#i-moon');
  }
  tbtn.addEventListener('click', () => setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'));
  setTheme(localStorage.getItem('as-theme') || 'light');

  /* ---------- save counts ---------- */
  function updateSaveCount() {
    const n = saved.size;
    const sc = $('#save-count'); sc.textContent = n; sc.hidden = n === 0;
    const bt = $('#bt-save-count'); bt.textContent = n; bt.hidden = n === 0;
  }

  /* ====================================================================
     LANDING — suggestions, filters, grid
     ==================================================================== */
  const SUGGEST = ['Flats in Chennai under ₹50L', 'Commercial in Coimbatore', 'Plots near Madurai', 'Houses in Trichy', 'Villas on ECR'];
  $('#landing-suggest').innerHTML = SUGGEST.map(s => `<span class="chip" data-q="${esc(s)}">${esc(s)}</span>`).join('');

  // build filter controls
  const DISTRICTS = [...new Set(P.map(p => p.dist))].sort();
  const BANKS = [...new Set(P.map(p => p.bank))].sort();
  const TYPEKEYS = [...new Set(P.map(p => p.t))];

  function multiSelect(key, label) {
    const opts = key === 'dist' ? DISTRICTS : key === 'bank' ? BANKS : TYPEKEYS;
    const labelOf = (o) => key === 't' ? TYPES[o] : o;
    const counts = {}; P.forEach(p => { const v = p[key]; counts[v] = (counts[v] || 0) + 1; });
    const wrap = document.createElement('div'); wrap.className = 'filter-group';
    wrap.innerHTML = `<span class="lbl-inline">${label}</span>
      <div class="filter-multi">
        <button class="filter-multi-btn" type="button"><span class="filter-multi-label">All</span><svg class="filter-multi-caret"><use href="#i-cal" style="display:none"/></svg><svg class="filter-multi-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>
        <div class="filter-multi-panel" hidden></div>
      </div>`;
    const multi = wrap.querySelector('.filter-multi');
    const btn = wrap.querySelector('.filter-multi-btn');
    const panel = wrap.querySelector('.filter-multi-panel');
    panel.innerHTML = opts.map(o => `<label class="filter-multi-opt"><input type="checkbox" value="${esc(o)}"><span class="name">${esc(labelOf(o))}</span><span class="count">${counts[o]}</span></label>`).join('');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = multi.classList.contains('open');
      $$('.filter-multi.open').forEach(m => { m.classList.remove('open'); m.querySelector('.filter-multi-panel').hidden = true; });
      if (!open) { multi.classList.add('open'); panel.hidden = false; }
    });
    panel.addEventListener('click', e => e.stopPropagation());
    panel.addEventListener('change', () => {
      filters[key] = $$('input:checked', panel).map(i => i.value);
      const lbl = wrap.querySelector('.filter-multi-label');
      const cnt = filters[key].length;
      lbl.textContent = cnt === 0 ? 'All' : cnt === 1 ? labelOf(filters[key][0]) : `${cnt} selected`;
      let pill = btn.querySelector('.count-pill');
      if (cnt > 1 && !pill) { pill = document.createElement('span'); pill.className = 'count-pill'; btn.insertBefore(pill, btn.querySelector('.filter-multi-caret')); }
      if (pill) { if (cnt > 1) pill.textContent = cnt; else pill.remove(); }
      applyFilters();
    });
    return wrap;
  }
  const priceSel = document.createElement('div'); priceSel.className = 'filter-group';
  priceSel.innerHTML = `<span class="lbl-inline">Price</span>
    <select class="filter-select" id="f-price">
      <option value="">Any price</option><option value="0-50">Under ₹50L</option>
      <option value="50-100">₹50L – 1Cr</option><option value="100-200">₹1Cr – 2Cr</option>
      <option value="200-">₹2Cr +</option>
    </select>`;

  const frow = $('#filter-row');
  frow.append(multiSelect('dist', 'District'), multiSelect('t', 'Property type'), multiSelect('bank', 'Bank'), priceSel);

  // collapsible filter panel — hidden until needed
  const fToggle = $('#filter-toggle'), fPanel = $('#filter-panel');
  fToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !fPanel.hidden;
    fPanel.hidden = open;
    fToggle.classList.toggle('on', !open);
    fToggle.setAttribute('aria-expanded', String(!open));
  });

  $('#f-price').addEventListener('change', e => { filters.price = e.target.value; applyFilters(); });
  $('#f-sort').addEventListener('change', e => { filters.sort = e.target.value; applyFilters(); });
  $('#browse-q').addEventListener('input', e => { filters.q = e.target.value.trim().toLowerCase(); applyFilters(); });
  document.addEventListener('click', () => $$('.filter-multi.open').forEach(m => { m.classList.remove('open'); m.querySelector('.filter-multi-panel').hidden = true; }));

  function matchFilters(list) {
    return list.filter(p => {
      if (filters.q) { const hay = (p.title + ' ' + p.loc + ' ' + p.dist + ' ' + p.bank + ' ' + TYPES[p.t]).toLowerCase(); if (!hay.includes(filters.q)) return false; }
      if (filters.dist.length && !filters.dist.includes(p.dist)) return false;
      if (filters.t.length && !filters.t.includes(p.t)) return false;
      if (filters.bank.length && !filters.bank.includes(p.bank)) return false;
      if (filters.price) {
        const [lo, hi] = filters.price.split('-'); const v = p.reserve;
        if (lo && v < +lo) return false; if (hi && v > +hi) return false;
      }
      return true;
    });
  }
  function sortList(list) {
    const s = filters.sort;
    return [...list].sort((a, b) => s === 'price_asc' ? a.reserve - b.reserve : s === 'price_desc' ? b.reserve - a.reserve : a.days - b.days);
  }
  function activeFilterChips() {
    const chips = [];
    filters.dist.forEach(v => chips.push(['dist', v, v]));
    filters.t.forEach(v => chips.push(['t', v, TYPES[v]]));
    filters.bank.forEach(v => chips.push(['bank', v, v]));
    if (filters.price) { const m = { '0-50': 'Under ₹50L', '50-100': '₹50L–1Cr', '100-200': '₹1Cr–2Cr', '200-': '₹2Cr+' }; chips.push(['price', filters.price, m[filters.price]]); }
    const box = $('#active-chips');
    box.style.display = chips.length ? 'flex' : 'none';
    box.innerHTML = chips.map(([k, v, l]) => `<span class="active-chip">${esc(l)} <span class="x" data-rmk="${k}" data-rmv="${esc(v)}">✕</span></span>`).join('');
    $('#filter-clear').disabled = chips.length === 0 && !filters.q;
    const fc = $('#filter-count'); fc.textContent = chips.length; fc.hidden = chips.length === 0;
    $('#filter-toggle').classList.toggle('has-active', chips.length > 0);
  }
  $('#active-chips').addEventListener('click', e => {
    const x = e.target.closest('[data-rmk]'); if (!x) return;
    const k = x.dataset.rmk, v = x.dataset.rmv;
    if (k === 'price') { filters.price = ''; $('#f-price').value = ''; }
    else { filters[k] = filters[k].filter(o => o !== v); syncMulti(k); }
    applyFilters();
  });
  function syncMulti(key) {
    const idx = { dist: 0, t: 1, bank: 2 }[key];
    const wrap = frow.children[idx]; if (!wrap) return;
    $$('.filter-multi-opt input', wrap).forEach(i => { i.checked = filters[key].includes(i.value); });
    const cnt = filters[key].length;
    wrap.querySelector('.filter-multi-label').textContent = cnt === 0 ? 'All' : cnt === 1 ? (key === 't' ? TYPES[filters[key][0]] : filters[key][0]) : `${cnt} selected`;
    const btn = wrap.querySelector('.filter-multi-btn'); const pill = btn.querySelector('.count-pill');
    if (pill && cnt <= 1) pill.remove();
  }
  $('#filter-clear').addEventListener('click', () => {
    filters.q = ''; filters.dist = []; filters.t = []; filters.bank = []; filters.price = '';
    $('#browse-q').value = ''; $('#f-price').value = '';
    ['dist', 't', 'bank'].forEach(syncMulti);
    applyFilters();
  });

  function applyFilters() {
    const list = sortList(matchFilters(P));
    $('#browse-count').textContent = list.length + (list.length === 1 ? ' property' : ' properties');
    const grid = $('#browse-grid');
    grid.innerHTML = list.length ? list.map(p => card(p)).join('')
      : `<div class="empty-state"><div class="ico">${ico('search')}</div><div class="ttl">No properties match these filters</div><div class="desc">Try widening your price range or clearing a filter.</div></div>`;
    activeFilterChips();
  }

  /* ---------- landing search → results ---------- */
  function runSearch(q) {
    if (!q.trim()) return;
    nav('results');
    newThread(q);
    setMobTab('chat');
  }
  $('#landing-send').addEventListener('click', () => runSearch($('#landing-input').value));
  $('#landing-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); runSearch($('#landing-input').value); } });
  $('#landing-suggest').addEventListener('click', e => { const c = e.target.closest('[data-q]'); if (c) runSearch(c.dataset.q); });

  /* ====================================================================
     RESULTS / CHAT
     ==================================================================== */
  let threads = [
    { id: 't1', title: '3BHK in Chennai under ₹1Cr', when: 'Today', msgs: [] },
    { id: 't2', title: 'Commercial spaces in Coimbatore', when: 'Today', msgs: [] },
    { id: 't3', title: 'Plots near Madurai ring road', when: 'Yesterday', msgs: [] },
    { id: 't4', title: 'SBI auctions closing this week', when: 'Previous 7 days', msgs: [] }
  ];
  let activeThread = null;

  function parseQuery(q) {
    q = q.toLowerCase();
    const f = {};
    DISTRICTS.forEach(d => { if (q.includes(d.toLowerCase()) || (d === 'Tiruchirappalli' && q.includes('trichy'))) f.dist = d; });
    if (/flat|apartment|bhk/.test(q)) f.t = 'flat';
    else if (/villa/.test(q)) f.t = 'villa';
    else if (/house/.test(q)) f.t = 'house';
    else if (/plot|land|agri/.test(q)) f.t = 'land';
    else if (/commercial|shop|office|showroom|retail/.test(q)) f.t = 'commercial';
    else if (/godown|warehouse/.test(q)) f.t = 'godown';
    else if (/industr|shed|factory/.test(q)) f.t = 'industrial';
    const m = q.match(/under\s*₹?\s*([\d.]+)\s*(l|lakh|cr|crore)?/);
    if (m) { let v = parseFloat(m[1]); if (/cr/.test(m[2] || '')) v *= 100; f.max = v; }
    return f;
  }
  function matchQuery(q) {
    const f = parseQuery(q);
    let list = P.filter(p => (!f.dist || p.dist === f.dist) && (!f.t || p.t === f.t) && (!f.max || p.reserve <= f.max));
    if (!list.length) list = P.filter(p => !f.dist || p.dist === f.dist);
    if (!list.length) list = P.slice(0, 6);
    return { list: list.sort((a, b) => a.days - b.days), f };
  }
  function aiReply(q, res) {
    const { list, f } = res;
    const where = f.dist ? ` in ${f.dist}` : '';
    const what = f.t ? TYPES[f.t].toLowerCase() + 's' : 'properties';
    const cap = f.max ? ` under ${fmt(f.max)}` : '';
    const banks = [...new Set(list.slice(0, 8).map(p => p.bank))].slice(0, 2).join(' and ');
    return `I found <strong>${list.length} ${what}</strong>${where}${cap}. The soonest auctions are within the next ${list[0] ? list[0].days : 7} days — mostly ${banks} listings. I've sorted them by auction date on the right; ask me to filter by price, EMD, or possession status.`;
  }

  function renderThreads(filter = '') {
    const groups = {};
    threads.filter(t => t.title.toLowerCase().includes(filter.toLowerCase())).forEach(t => { (groups[t.when] = groups[t.when] || []).push(t); });
    $('#sb-body').innerHTML = Object.entries(groups).map(([g, ts]) =>
      `<div class="sb-group-label">${esc(g)}</div>` + ts.map(t =>
        `<div class="sb-item${activeThread === t.id ? ' active' : ''}" data-thread="${t.id}"><span class="sb-item-title">${esc(t.title)}</span></div>`).join('')
    ).join('');
  }
  $('#sb-search').addEventListener('input', e => renderThreads(e.target.value));
  $('#sb-body').addEventListener('click', e => {
    const it = e.target.closest('[data-thread]'); if (!it) return;
    openThread(it.dataset.thread);
    setMobTab('chat');
  });
  $('#new-chat').addEventListener('click', () => { activeThread = null; $('#chat-log').innerHTML = emptyChat(); $('#thread-title').textContent = 'New conversation'; renderResults([]); renderThreads(); setMobTab('chat'); $('#results-input').focus(); });

  function emptyChat() {
    return `<div class="ask-empty" style="text-align:center;padding:32px 16px;font-size:14px">Start by describing what you're looking for —<br>e.g. <em>"ready-to-move 2BHK in Velachery under ₹50L"</em></div>`;
  }
  function openThread(id) {
    const t = threads.find(x => x.id === id); if (!t) return;
    activeThread = id; $('#thread-title').textContent = t.title;
    if (!t.msgs.length) { const res = matchQuery(t.title); t.msgs.push({ role: 'user', text: t.title }); t.msgs.push({ role: 'ai', text: aiReply(t.title, res), list: res.list }); }
    renderChat(t); renderThreads($('#sb-search').value);
    const last = [...t.msgs].reverse().find(m => m.list); renderResults(last ? last.list : []);
  }
  function newThread(q) {
    const id = 't' + Date.now();
    const t = { id, title: q.length > 42 ? q.slice(0, 42) + '…' : q, when: 'Today', msgs: [] };
    threads.unshift(t); activeThread = id;
    $('#thread-title').textContent = t.title;
    const res = matchQuery(q);
    t.msgs.push({ role: 'user', text: q });
    renderChat(t); renderThreads();
    // simulate thinking
    $('#chat-log').insertAdjacentHTML('beforeend', `<div class="bubble ai thinking" id="thinking">Searching auctions</div>`);
    scrollChat();
    setTimeout(() => {
      $('#thinking') && $('#thinking').remove();
      t.msgs.push({ role: 'ai', text: aiReply(q, res), list: res.list });
      renderChat(t); renderResults(res.list);
    }, 850);
  }
  function renderChat(t) {
    $('#chat-log').innerHTML = t.msgs.map(m => {
      if (m.role === 'user') return `<div class="bubble user">${esc(m.text)}</div>`;
      const src = `<div class="sources-row"><span class="sources-label">Sources</span><a class="source-chip" href="#" onclick="return false">eauctionsindia.com</a><a class="source-chip" href="#" onclick="return false">${esc((t.title.match(/sbi|hdfc|canara/i) || ['bank'])[0])}.bank.in</a></div>`;
      return `<div class="bubble ai">${m.text}${src}<div class="resp-time">${ico('clock')} ${(1.6 + Math.random() * 1.8).toFixed(1)}s</div></div>`;
    }).join('');
    scrollChat();
  }
  function scrollChat() { const l = $('#chat-log'); l.scrollTop = l.scrollHeight; }

  let currentMatches = [];
  function renderResults(list) {
    currentMatches = list;
    const s = $('#results-sort').value;
    const sorted = [...list].sort((a, b) => s === 'price_asc' ? a.reserve - b.reserve : s === 'price_desc' ? b.reserve - a.reserve : a.days - b.days);
    $('#results-subcount').textContent = list.length ? `· ${list.length}` : '';
    $('#mtab-count').textContent = list.length;
    $('#results-list').innerHTML = sorted.length ? sorted.map(p => card(p)).join('')
      : `<div class="empty-state"><div class="ico">${ico('msg')}</div><div class="ttl">No matches yet</div><div class="desc">Send a query to see matching auctions.</div></div>`;
  }
  $('#results-sort').addEventListener('change', () => renderResults(currentMatches));
  function sendResults() {
    const v = $('#results-input').value.trim(); if (!v) return;
    $('#results-input').value = '';
    if (!activeThread) { newThread(v); return; }
    const t = threads.find(x => x.id === activeThread);
    t.msgs.push({ role: 'user', text: v }); renderChat(t);
    $('#chat-log').insertAdjacentHTML('beforeend', `<div class="bubble ai thinking" id="thinking">Searching auctions</div>`); scrollChat();
    const res = matchQuery(v);
    setTimeout(() => { $('#thinking') && $('#thinking').remove(); t.msgs.push({ role: 'ai', text: aiReply(v, res), list: res.list }); renderChat(t); renderResults(res.list); }, 850);
  }
  $('#results-send').addEventListener('click', sendResults);
  $('#results-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendResults(); } });
  // mobile segmented tabs: history / conversation / matches
  function setMobTab(t) {
    const g = $('#results-grid');
    g.classList.remove('mob-history', 'mob-chat', 'mob-matches');
    g.classList.add('mob-' + t);
    $$('.mobile-tabs button').forEach(b => {
      const on = b.dataset.mtab === t;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', String(on));
    });
  }
  $$('.mobile-tabs button').forEach(b => b.addEventListener('click', () => setMobTab(b.dataset.mtab)));

  /* ====================================================================
     SAVE + card clicks (delegated across grids/lists)
     ==================================================================== */
  document.addEventListener('click', e => {
    const sv = e.target.closest('[data-save]');
    if (sv) {
      e.stopPropagation();
      const id = sv.dataset.save;
      if (saved.has(id)) saved.delete(id); else saved.add(id);
      persistSaved();
      const on = saved.has(id);
      sv.classList.toggle('saved', on);
      sv.querySelector('use').setAttribute('href', on ? '#i-star-fill' : '#i-star');
      if (root.querySelector('.screen[data-screen="watchlist"].on')) renderWatchlist();
      return;
    }
    const cardEl = e.target.closest('.prop[data-id]');
    if (cardEl) { openDetail(cardEl.dataset.id); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter') { const c = e.target.closest && e.target.closest('.prop[data-id]'); if (c) openDetail(c.dataset.id); }
  });

  /* ====================================================================
     DETAIL
     ==================================================================== */
  function openDetail(id) {
    const p = P.find(x => x.id === id); if (!p) return;
    detailReturn = $('.screen[data-screen="results"].on') ? 'results' : $('.screen[data-screen="watchlist"].on') ? 'watchlist' : 'landing';
    renderDetail(p); nav('detail');
  }
  $('#detail-back').addEventListener('click', () => nav(detailReturn));

  function renderDetail(p) {
    const st = statusOf(p.days), isSaved = saved.has(p.id);
    const history = p.drop ? `
      <div>
        <h3 class="section-title">Price history</h3>
        <div class="price-history">
          <div class="history-row"><span class="when">${fmtDateFull(p.days - 35)}</span><span class="hp">${fmt(p.reserve + p.drop)}</span><span class="tag-mini">First auction</span></div>
          <div class="history-row current"><span class="when">${fmtDateFull(p.days)}</span><span class="hp">${fmt(p.reserve)}</span><span class="tag-mini">Current · ▼ ${fmt(p.drop)}</span></div>
        </div>
      </div>` : '';
    $('#detail-content').innerHTML = `
      <div class="detail-main">
        <div>
          <h1 class="h2 detail-title">${esc(p.title)}</h1>
          <div class="detail-sub">${ico('pin')} ${esc(p.loc)}, ${esc(p.dist)}, Tamil Nadu</div>
          <div class="detail-tags">
            <span class="tag bank">${esc(p.bank)}</span>
            <span class="tag">${esc(TYPES[p.t])}</span>
            <span class="status ${st.cls}"><span class="badge-dot"></span> ${st.label}</span>
            ${p.drop ? `<span class="price-drop">▼ ${fmt(p.drop)} price drop</span>` : ''}
          </div>
        </div>
        <div class="hero-photo">${`<svg class="big"><use href="#i-${p.t}"/></svg>`}<span class="ph-note">Property photo</span></div>
        <div class="fact-grid">
          <div class="fact big"><span class="lbl">Reserve price</span><span class="val">${fmt(p.reserve)}</span></div>
          <div class="fact"><span class="lbl">EMD</span><span class="val">${fmt(p.emd)}</span></div>
          <div class="fact"><span class="lbl">Auction date</span><span class="val">${fmtDateFull(p.days)}</span></div>
          <div class="fact"><span class="lbl">Auction time</span><span class="val">${p.time}</span></div>
          <div class="fact"><span class="lbl">Area</span><span class="val">${p.sqft.toLocaleString('en-IN')} sqft</span></div>
          <div class="fact"><span class="lbl">Type</span><span class="val">${esc(TYPES[p.t])}</span></div>
        </div>
        <div>
          <h3 class="section-title">Description</h3>
          <div class="detail-desc">Bank-auctioned ${esc(TYPES[p.t].toLowerCase())} located in ${esc(p.loc)}, ${esc(p.dist)}. Sale under the SARFAESI Act, 2002 by ${esc(p.bank)}. The property is offered on an "as is where is, as is what is" basis. Intending bidders should verify the encumbrance, measurements and statutory dues independently before submitting the EMD. Inspection is permitted on prior appointment with the authorised officer.</div>
        </div>
        ${history}
        <div>
          <h3 class="section-title">Documents</h3>
          <div class="docs">
            <a class="doc" href="#" onclick="return false"><span class="doc-ico">${ico('doc')}</span><span class="doc-name">Sale notice</span><span class="doc-meta">PDF · 240 KB</span></a>
            <a class="doc" href="#" onclick="return false"><span class="doc-ico">${ico('doc')}</span><span class="doc-name">Valuation report</span><span class="doc-meta">PDF · 1.1 MB</span></a>
          </div>
        </div>
      </div>
      <aside class="detail-aside">
        <div class="aside-card">
          <div class="price-cap">Reserve price</div>
          <div class="price-big">${fmt(p.reserve)}</div>
          <div class="small" style="margin-top:4px">EMD ${fmt(p.emd)} · Auction ${fmtDate(p.days)}, ${p.time}</div>
          <div class="aside-actions">
            <button class="btn block" id="d-save">${isSaved ? ico('star-fill') + ' Saved to watchlist' : ico('star') + ' Save to watchlist'}</button>
            <button class="btn secondary block" id="d-copy">${ico('link')} Copy link</button>
          </div>
        </div>
        <div class="aside-card ask-card">
          <div class="ask-head"><span class="ttl">Ask about this property</span></div>
          <div class="ask-empty">Have a question about EMD, possession, or documents? Ask Auctionscope.</div>
          <div class="ask-log" id="ask-log"></div>
          <div class="chat-input"><textarea id="ask-input" rows="1" placeholder="e.g. What's the EMD amount?"></textarea><button class="send" id="ask-send" aria-label="Ask">${ico('send')}</button></div>
        </div>
      </aside>`;

    // wire detail buttons
    $('#d-save').addEventListener('click', () => {
      if (saved.has(p.id)) saved.delete(p.id); else saved.add(p.id);
      persistSaved();
      $('#d-save').innerHTML = saved.has(p.id) ? ico('star-fill') + ' Saved to watchlist' : ico('star') + ' Save to watchlist';
    });
    $('#d-copy').addEventListener('click', () => {
      const b = $('#d-copy'); b.innerHTML = ico('link') + ' Link copied'; setTimeout(() => b.innerHTML = ico('link') + ' Copy link', 1600);
      try { navigator.clipboard && navigator.clipboard.writeText(location.href + '#' + p.id); } catch (e) {}
    });
    const askLog = $('#ask-log'), askIn = $('#ask-input');
    function ask() {
      const v = askIn.value.trim(); if (!v) return; askIn.value = '';
      $('.ask-empty', $('#detail-content')) && ($('.ask-empty', $('#detail-content')).style.display = 'none');
      askLog.insertAdjacentHTML('beforeend', `<div class="bubble user">${esc(v)}</div>`);
      askLog.insertAdjacentHTML('beforeend', `<div class="bubble ai thinking" id="ask-think">Checking</div>`);
      askLog.scrollTop = askLog.scrollHeight;
      setTimeout(() => {
        $('#ask-think') && $('#ask-think').remove();
        const ans = /emd|deposit/i.test(v) ? `The EMD for this property is <strong>${fmt(p.emd)}</strong>, payable before the auction on ${fmtDateFull(p.days)}.`
          : /possession|move/i.test(v) ? `This is a SARFAESI auction — possession is <strong>symbolic</strong> as listed; confirm physical possession status with ${esc(p.bank)}'s authorised officer.`
          : /document|paper/i.test(v) ? `Available documents are the <strong>sale notice</strong> and <strong>valuation report</strong> (see the Documents section). Title deeds are shared with the highest bidder.`
          : `For this ${esc(TYPES[p.t].toLowerCase())} in ${esc(p.loc)}, the reserve is <strong>${fmt(p.reserve)}</strong> with EMD <strong>${fmt(p.emd)}</strong>. The auction is on ${fmtDateFull(p.days)} at ${p.time}.`;
        askLog.insertAdjacentHTML('beforeend', `<div class="bubble ai">${ans}</div>`); askLog.scrollTop = askLog.scrollHeight;
      }, 750);
    }
    $('#ask-send').addEventListener('click', ask);
    askIn.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } });
  }

  /* ====================================================================
     WATCHLIST
     ==================================================================== */
  function renderWatchlist() {
    const list = P.filter(p => saved.has(p.id)).sort((a, b) => a.days - b.days);
    $('#wl-count').textContent = list.length + ' saved';
    const body = $('#watchlist-body');
    if (!list.length) {
      body.innerHTML = `<div class="empty-state"><div class="ico">${ico('star')}</div><div class="ttl">Nothing saved yet</div><div class="desc">Tap the ☆ on any property to track its auction here.</div><div style="margin-top:18px"><button class="btn" data-nav="landing">Browse properties</button></div></div>`;
      return;
    }
    const soon = list.filter(p => p.days <= 10), later = list.filter(p => p.days > 10);
    let html = '';
    if (soon.length) html += `<div class="timeline-group"><div class="timeline-label urgent"><span class="t-name">Closing soon · next 10 days</span><span class="rule"></span></div><div class="wl-list">${soon.map(p => card(p, { urgent: true })).join('')}</div></div>`;
    if (later.length) html += `<div class="timeline-group"><div class="timeline-label"><span class="t-name">Upcoming auctions</span><span class="rule"></span></div><div class="wl-list">${later.map(p => card(p)).join('')}</div></div>`;
    body.innerHTML = html;
  }

  /* ---------- init ---------- */
  applyFilters();
  updateSaveCount();
  renderThreads();
  $('#chat-log').innerHTML = emptyChat();
})();
