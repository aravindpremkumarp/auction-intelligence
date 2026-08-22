/* ====== CONFIG ====== */
const API_BASE = window.API_BASE || '';
// Every API call gets a timeout so a down/hung backend fails visibly instead
// of leaving the UI waiting forever. /chat is generous (agent turns can take
// a while); everything else should answer fast. A caller-supplied signal
// (e.g. the browse abort) still wins where AbortSignal.any is available.
const FETCH_TIMEOUT_MS = 20000;
// Blocking /chat only. Agent turns legitimately run for minutes when the
// upstream model is slow (a single LLM round-trip can sit 30-80s), so the
// old 120s cap was killing turns that were still working. /chat/stream is
// excluded from this hard timeout entirely — apiChatStream manages its own
// idle-based timeout, which can tell a slow-but-alive stream (the server
// heartbeats every ~15s) from a dead connection.
const CHAT_FETCH_TIMEOUT_MS = 300000;
// Matches /chat/stream AND /chat/v2/stream. Testing for the literal
// '/chat/stream' would miss v2 and hand its turns the 300s hard cap, killing
// long-but-alive streams the server was still working on.
const _isChatStream = (url) => /\/chat(\/v\d+)?\/stream$/.test(String(url).split('?')[0]);
const authFetch = (url, opts = {}) => {
  if (_isChatStream(url)) {
    return (window.Auth && window.Auth.fetchWithAuth ? window.Auth.fetchWithAuth(url, opts) : fetch(url, opts));
  }
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    const ms = String(url).includes('/chat') ? CHAT_FETCH_TIMEOUT_MS : FETCH_TIMEOUT_MS;
    const timeoutSignal = AbortSignal.timeout(ms);
    let signal = timeoutSignal;
    if (opts.signal) signal = AbortSignal.any ? AbortSignal.any([opts.signal, timeoutSignal]) : opts.signal;
    opts = { ...opts, signal };
  }
  return (window.Auth && window.Auth.fetchWithAuth ? window.Auth.fetchWithAuth(url, opts) : fetch(url, opts));
};
const GATED_MODES = new Set(['deep-research']);
function _requireAuthForMode(mode) {
  if (!GATED_MODES.has(mode)) return true;
  const u = window.Auth && window.Auth.getUser && window.Auth.getUser();
  if (u && u.email_verified) return true;
  if (window.Auth && window.Auth.openLoginModal) window.Auth.openLoginModal();
  return false;
}
// `select_properties` is the agent re-presenting a subset of earlier results
// ("top three of those") — for the matches panel it counts as a search, and
// the LAST such call in a turn wins. `semantic_property_search` is the
// pre-rename name of `semantic_search`, kept for stored conversations.
const SEARCH_TOOLS = ['search_auctions', 'semantic_search', 'semantic_property_search', 'select_properties'];
const LIST_TOOLS = [...SEARCH_TOOLS, 'upcoming_auctions', 'price_comparison', 'find_similar_properties', 'borrower_lookup', 'survey_search'];
const DETAIL_TOOLS = ['get_auction_detail'];
const WATCHLIST_KEY = 'bankauction.watchlist.v1';
const SESSION_KEY = 'bankauction.session_id';
const ICON = {
  flat:'icon-flat', house:'icon-house', land:'icon-land', commercial:'icon-commercial',
  plot:'icon-plot', 'land-building':'icon-land-building', 'commercial-building':'icon-commercial-building',
  'industrial-land-building':'icon-industrial-lb', factory:'icon-factory',
  'cold-storage':'icon-cold-storage', 'commercial-property':'icon-commercial-property',
  'industrial-land':'icon-industrial-land', godown:'icon-godown', villa:'icon-villa'
};

/* ====== STATE ====== */
let currentScreen = 'landing';
let detailReturnScreen = 'results';
let currentDetailId = null;
let currentDetailTitle = '';
let currentDetailLoc = '';
let saved = loadWatchlist();
let watchlistCache = {};
let chatHistory = [];
let apiMessageHistory = null;
let lastQuery = null;
let currentResults = [];
let currentTotalCount = null;
let currentSort = 'date_asc';
// True when the panel is showing an AI-produced ranking (e.g. the agent
// answered "which has the largest land?" and re-ordered the matches). The
// backend ships those rows already ordered via a synthetic select_properties
// artifact; this flag tells the panel to preserve that order instead of
// re-sorting by date/price, and unlocks the "Best match" sort option.
let panelIsRanked = false;
// The sort the user last chose explicitly for a non-ranked panel — restored
// when a later turn drops back out of a ranking so we don't strand them on a
// now-meaningless "relevance" order.
let userSort = 'date_asc';
// Index into chatHistory of the AI message whose matches the panel is
// currently showing; null = following the live (most recent) result set.
// Each turn's matches are re-derived from the artifacts already stored on
// its AI message, so no extra per-message snapshot is persisted.
let panelSnapshotIndex = null;
let detailChatHistory = [];
let detailApiMessageHistory = null;   // server-format message_history for the active property chat — must thread across turns or follow-ups lose context
let currentPropertyChatId = null;     // UUID of the active chat in the right-side property panel
let propertyChatList = [];            // [{id, title, property_id, updated_at}] for the panel history list
let _pendingPropertyChatId = null;    // one-shot: when a sidebar click routes to a specific chat id, honored on the next loadDetailChat

function sessionId() {
  let s = sessionStorage.getItem(SESSION_KEY);
  if (!s) {
    s = (crypto.randomUUID && crypto.randomUUID()) || ('s-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
    sessionStorage.setItem(SESSION_KEY, s);
  }
  return s;
}

function loadWatchlist() {
  try {
    const raw = localStorage.getItem(WATCHLIST_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch(e) { return new Set(); }
}
function persistWatchlist() {
  // Signed-in users' watchlist lives on the server (see syncWatchlistFromServer /
  // toggleSaved). Only anonymous guests use localStorage as a scratchpad.
  if (window.Auth && window.Auth.getUser && window.Auth.getUser()) return;
  try { localStorage.setItem(WATCHLIST_KEY, JSON.stringify([...saved])); } catch(e) {}
}

async function syncWatchlistFromServer() {
  try {
    const r = await authFetch(`${API_BASE}/watchlist`);
    if (!r.ok) return;
    const data = await r.json();
    saved = new Set(Array.isArray(data.ids) ? data.ids : []);
    watchlistCache = {};
    updateSavedCount();
    loadAlerts();
    if (currentScreen === 'watchlist') renderWatchlist();
    else if (currentScreen === 'results') renderResultsList();
    else if (currentScreen === 'detail') updateDetailSaveButton();
  } catch(e) { console.error('[watchlist] load failed', e); }
}

(function wireWatchlistToAuth() {
  if (!(window.Auth && window.Auth.onAuthChange)) return;
  let prevUser = null;
  window.Auth.onAuthChange((user) => {
    const wasSignedIn = !!prevUser;
    const isSignedIn = !!user;
    prevUser = user;
    if (isSignedIn && !wasSignedIn) {
      syncWatchlistFromServer();
    } else if (!isSignedIn && wasSignedIn) {
      saved = new Set();
      watchlistCache = {};
      try { localStorage.removeItem(WATCHLIST_KEY); } catch(e) {}
      updateSavedCount();
      loadAlerts();
      if (currentScreen === 'watchlist') renderWatchlist();
      else if (currentScreen === 'results') renderResultsList();
      else if (currentScreen === 'detail') updateDetailSaveButton();
    }
  });
})();

/* ====== HELPERS ====== */
function escapeHtml(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Convert a raw legal-style "Property Description" paragraph into structured
// markdown (headers, bullets, bold labels, direction emojis) so the existing
// renderMarkdown pipeline can format it the same way it formats AI chat.
// Returns the input untouched when it already looks structured (has blank
// lines, headers, or list bullets).
function formatDescription(text) {
  if (text == null) return '';
  let s = String(text).trim();
  if (!s) return '';

  const alreadyStructured =
    /\n\s*\n/.test(s) ||
    /^\s*#{1,6}\s+/m.test(s) ||
    /^\s*[-*+]\s+/m.test(s);
  if (alreadyStructured) return s;

  s = s.replace(/^\s*(?:property\s+)?description(?:\s+of\s+propert(?:y\/ies|ies|y))?\s*[:\-–]\s*/i, '');
  s = s.replace(/\s+/g, ' ').trim();

  const itemRegex = /\bItem\s+No\.?\s*[:\-]?\s*(\d+)\b/gi;
  const segments = [];
  let lastIdx = 0;
  let lastNum = null;
  let m;
  while ((m = itemRegex.exec(s)) !== null) {
    segments.push({ num: lastNum, body: s.slice(lastIdx, m.index).trim() });
    lastNum = m[1];
    lastIdx = m.index + m[0].length;
  }
  segments.push({ num: lastNum, body: s.slice(lastIdx).trim() });

  const dirEmoji = { north: '↑', south: '↓', east: '→', west: '←' };

  const formatBody = (body) => {
    if (!body) return '';
    const lines = [];

    // Pull out a "Bounded On The North By X, South By Y, East By Z, West By W"
    // clause and rebuild it as a labelled bullet list.
    let boundaryLines = [];
    const bm = body.match(
      /\bBounded\s+(?:on\s+the\s+)?(?:plot\s+no\.?[^,]*,?\s*)?(.+?)(?=(?:\s+And\s+Situated\b|\s+And\s+Within\b|\s+Within\s+The\b|\s*$))/i,
    );
    if (bm) {
      // The boundary clause often runs straight into a trailing
      // "Measuring; … feet" dimensions clause. Split that off first so the
      // dimensions survive in the body instead of being dropped along with
      // the boundary text.
      const measureAt = bm[1].search(/\bMeasuring\b/i);
      const inner = measureAt >= 0 ? bm[1].slice(0, measureAt) : bm[1];
      const trailing = measureAt >= 0 ? bm[1].slice(measureAt) : '';
      // Walk every direction keyword and slice the value as everything up
      // to the next direction. This preserves commas inside a single value
      // (e.g. "Plot No. 156/2,3 And 155/4").
      const dirRe = /\b(north|south|east|west)\b/gi;
      const positions = [];
      let dm;
      while ((dm = dirRe.exec(inner)) !== null) {
        positions.push({ idx: dm.index, end: dm.index + dm[0].length, dir: dm[1].toLowerCase() });
      }
      const seen = {};
      const parts = [];
      for (let k = 0; k < positions.length; k++) {
        const start = positions[k].end;
        const finish = (k + 1 < positions.length) ? positions[k + 1].idx : inner.length;
        let val = inner.slice(start, finish);
        // Drop the connective that follows the direction word ("by", "of",
        // and a leading dash/colon) so the value reads as just the neighbour.
        val = val.replace(/^\s*(?:by|of)\b\s*/i, '').replace(/^\s*[-–:]\s*/, '');
        // Stop at a sentence terminator or any next-field keyword that bled
        // in from the source paragraph, otherwise the value can read
        // "Plot No. 12. Measuring: …".
        val = val.split(/\.\s+(?=[A-Z])/)[0];
        val = val.split(/\b(?:Measuring|Comprised\s+In|Situated\s+Within|As\s+Per\s+Patta|Together\s+With)\b/i)[0];
        val = val.trim().replace(/^[-–:\s]+/, '').replace(/[,;.\s]+$/, '');
        const dir = positions[k].dir;
        if (val && !seen[dir]) { seen[dir] = true; parts.push({ dir, val }); }
      }
      if (parts.length >= 2) {
        const order = ['north', 'south', 'east', 'west'];
        parts.sort((a, b) => order.indexOf(a.dir) - order.indexOf(b.dir));
        boundaryLines = parts.map(p =>
          `- ${dirEmoji[p.dir] || '•'} **${p.dir.charAt(0).toUpperCase() + p.dir.slice(1)}:** ${p.val}`
        );
        // Remove only the boundary prose, splicing any dimensions back in.
        body = (body.slice(0, bm.index) + ' ' + trailing).replace(/\s{2,}/g, ' ').trim();
      }
    }

    // Bold a few well-known field labels in whatever text remains so the eye
    // can scan for the data points fast. Use a single alternation regex with
    // longest labels first so overlapping phrases (e.g. "Measuring An Extent
    // Of" vs "Measuring") get bolded once at the longest match.
    const labels = [
      'Together With An Undivided Share Of',
      'As Per Patta New Survey No',
      'Comprised In Old Survey No',
      'Measuring An Extent Of',
      'Measuring About',
      'Building Called',
      'Old Survey No', 'New Survey No',
      'Comprised In', 'Situated Within',
      'As Per Patta',
      'Plot Nos', 'Plot No', 'Survey No', 'Flat No', 'Door No', 'Bearing No',
      'Measuring', 'Patta',
    ];
    const labelRe = new RegExp(
      '\\b(' + labels.map(l => l.replace(/\s+/g, '\\s+')).join('|') + ')\\b',
      'gi',
    );
    body = body.replace(labelRe, '**$1**');

    // Tidy up leftover scaffolding from where the boundary clause was cut out.
    body = body
      .replace(/\bAnd\s+And\b/gi, 'And')
      .replace(/,\s*And\s*(?=[.,])/gi, '')
      .replace(/\bAnd\s*$/i, '')
      .replace(/\bSchedule\s+([A-Z])\s+Schedule\s+\1\b/gi, 'Schedule $1')
      .replace(/\s{2,}/g, ' ')
      .trim();

    // Keep the description as one flowing paragraph. The previous version
    // split on every "period + capital", which shattered abbreviations like
    // "Mr. H. Dawood Khan" into separate lines and read as broken text; the
    // bold labels above already give the eye scan-points inline.
    const paragraph = body
      .replace(/\s+/g, ' ')
      .replace(/^[\s,;:.]+|[\s,;:.]+$/g, '')
      .trim();

    if (paragraph) lines.push(paragraph);
    if (boundaryLines.length) {
      lines.push('');
      lines.push('**Boundaries**');
      lines.push(boundaryLines.join('\n'));
    }
    return lines.join('\n');
  };

  const blocks = [];
  segments.forEach((seg, i) => {
    if (!seg.body && seg.num == null) return;
    if (seg.num == null) {
      const formatted = formatBody(seg.body);
      if (formatted) blocks.push(formatted);
    } else {
      const header = `### 🏠 Item ${seg.num}`;
      const formatted = formatBody(seg.body);
      blocks.push(formatted ? `${header}\n\n${formatted}` : header);
    }
  });

  const result = blocks.join('\n\n').trim();
  return result || s;
}

// Render a small, safe subset of Markdown to HTML for AI chat bubbles.
// Supports: fenced + inline code, tables, headers (#..######), bold (** **),
// italic (* *), links [text](http(s)://…), and unordered/ordered lists.
// All raw input is HTML-escaped before any tags are introduced, so user-
// supplied text can never inject markup.

// MinerU OCR stores notice tables as raw HTML (<table><tr><td>… with no
// inter-cell whitespace), so property descriptions can carry literal table
// markup. renderMarkdown escapes all HTML, which made those tables display
// as raw tags. Convert each <table> block to a GFM pipe table; the cell text
// still flows through renderMarkdown's escaping, so nothing in the scraped
// data can inject markup (DOMParser parses inert, never executes scripts).
function convertHtmlTables(text) {
  const s = String(text);
  if (!/<table[\s>]/i.test(s) || typeof DOMParser === 'undefined') return s;
  return s.replace(/<table[\s\S]*?<\/table>/gi, (block) => {
    let doc;
    try { doc = new DOMParser().parseFromString(block, 'text/html'); } catch (_) { return block; }
    const rows = Array.from(doc.querySelectorAll('tr')).map(tr =>
      Array.from(tr.querySelectorAll('th,td')).map(cell =>
        // `|` is the one character that would break the pipe-table shape.
        cell.textContent.replace(/\s+/g, ' ').replace(/\|/g, '/').trim()
      )
    ).filter(r => r.length);
    if (!rows.length) return block;
    const width = Math.max(...rows.map(r => r.length));
    const pad = (r) => r.concat(Array(width - r.length).fill(''));
    const line = (r) => '| ' + pad(r).join(' | ') + ' |';
    const sep = '| ' + Array(width).fill('---').join(' | ') + ' |';
    return '\n\n' + [line(rows[0]), sep, ...rows.slice(1).map(line)].join('\n') + '\n\n';
  });
}

function renderMarkdown(text) {
  if (text == null || text === '') return '';
  const escapeChars = (str) => String(str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  let s = String(text).replace(/\r\n?/g, '\n');

  // 1. Stash fenced code blocks so their contents survive every transform.
  const fences = [];
  s = s.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = fences.push({ lang: (lang || '').trim(), code: code.replace(/\n$/, '') }) - 1;
    return ` F${idx} `;
  });

  // 2. Stash inline code so * / _ inside it isn't treated as emphasis.
  const inlines = [];
  s = s.replace(/`([^`\n]+)`/g, (_, code) => {
    const idx = inlines.push(code) - 1;
    return ` I${idx} `;
  });

  // 2.5. Turn raw HTML tables (OCR'd notice data) into pipe tables so the
  // table renderer below picks them up instead of escaping them to literal
  // markup. Runs after code stashing so tables inside code blocks survive.
  s = convertHtmlTables(s);

  // 3. Escape HTML in everything that's left.
  s = escapeChars(s);

  // 4. Block-level parsing (line oriented): tables, headers, lists.
  const splitRow = (line) => {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|')) row = row.slice(0, -1);
    return row.split('|').map(c => c.trim());
  };
  const isTableSeparator = (line) =>
    line.includes('|') && /^\s*\|?\s*:?-{2,}:?(\s*\|\s*:?-{2,}:?)*\s*\|?\s*$/.test(line);

  const lines = s.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Tables: header row followed by a |---|---| separator row.
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const header = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(splitRow(lines[i]));
        i++;
      }
      let html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
      for (const h of header) html += `<th>${h}</th>`;
      html += '</tr></thead><tbody>';
      for (const r of rows) {
        html += '<tr>';
        for (let j = 0; j < header.length; j++) html += `<td>${r[j] != null ? r[j] : ''}</td>`;
        html += '</tr>';
      }
      html += '</tbody></table></div>';
      out.push(html);
      continue;
    }

    // Headers (#, ##, ..., ######)
    const hMatch = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (hMatch) {
      const level = hMatch[1].length;
      out.push(`<div class="md-h md-h${level}">${hMatch[2]}</div>`);
      i++;
      continue;
    }

    // Horizontal rule: a line of 3+ dashes / asterisks / underscores.
    if (/^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$/.test(line)) {
      out.push('<hr class="md-hr">');
      i++;
      continue;
    }

    // Unordered list block
    if (/^\s*[-*+]\s+\S/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+\S/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i++;
      }
      out.push('<ul class="md-list">' + items.map(x => `<li>${x}</li>`).join('') + '</ul>');
      continue;
    }

    // Ordered list block
    if (/^\s*\d+\.\s+\S/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+\S/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i++;
      }
      out.push('<ol class="md-list">' + items.map(x => `<li>${x}</li>`).join('') + '</ol>');
      continue;
    }

    out.push(line);
    i++;
  }
  s = out.join('\n');

  // 5. Inline formatting on what's left.
  // Bold (**text**) — non-greedy, no surrounding whitespace, no embedded *.
  s = s.replace(/\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*/g, '<strong>$1</strong>');
  // Italic (*text*) — single asterisks, not part of **.
  s = s.replace(/(^|[^*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)/g, '$1<em>$2</em>');
  // Underscore bold (__text__).
  s = s.replace(/__(?!\s)([^_\n]+?)(?<!\s)__/g, '<strong>$1</strong>');
  // Links [text](http(s)://...) — only http(s) URLs are linkified.
  s = s.replace(/\[([^\]\n]+?)\]\((https?:\/\/[^\s)]+?)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // 6. Newlines: drop those adjacent to block-level tags, then preserve
  // paragraph breaks (blank lines) as a double <br> and single newlines as <br>.
  s = s.replace(/\n*(<\/?(?:div|table|thead|tbody|tr|th|td|ul|ol|li|pre|blockquote|hr)\b[^>]*>)\n*/g, '$1');
  s = s.replace(/\n{2,}/g, '<br><br>');
  s = s.replace(/\n/g, '<br>');

  // 7. Restore inline + fenced code with proper escaping.
  s = s.replace(/ I(\d+) /g, (_, n) => `<code class="md-code">${escapeChars(inlines[+n])}</code>`);
  s = s.replace(/ F(\d+) /g, (_, n) => {
    const f = fences[+n];
    return `<pre class="md-pre"><code>${escapeChars(f.code)}</code></pre>`;
  });

  return s;
}
function thumbSvg(type) {
  return `<svg viewBox="0 0 160 120" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg"><use href="#${ICON[type]||'icon-flat'}"/></svg>`;
}
function inferType(row) {
  const hay = [row.asset_category, ...(Array.isArray(row.property_types) ? row.property_types : []), row.title].filter(Boolean).join(' ').toLowerCase();
  if (/villa|bungalow/.test(hay)) return 'villa';
  if (/godown|warehouse/.test(hay)) return 'godown';
  if (/cold\s*storage/.test(hay)) return 'cold-storage';
  if (/factory/.test(hay)) return 'factory';
  if (/industrial\s+land\s+(?:and|&)\s+building|industrial\s+(?:land\s+)?(?:and\s+)?building/.test(hay)) return 'industrial-land-building';
  if (/industrial\s+land/.test(hay)) return 'industrial-land';
  if (/commercial\s+building|office/.test(hay)) return 'commercial-building';
  if (/commercial\s+property|shop|retail/.test(hay)) return 'commercial-property';
  if (/commerc/.test(hay)) return 'commercial';
  if (/land\s+(?:and|&)\s+building/.test(hay)) return 'land-building';
  if (/plot/.test(hay)) return 'plot';
  if (/land|agri|vacant/.test(hay)) return 'land';
  if (/house|independent/.test(hay)) return 'house';
  if (/flat|apartment|bhk|residential/.test(hay)) return 'flat';
  return 'flat';
}
function formatINR(n) {
  if (n == null || isNaN(n)) return '—';
  const num = Number(n);
  if (num >= 1e7) return `₹ ${(num/1e7).toFixed(2).replace(/\.?0+$/, '')} Cr`;
  if (num >= 1e5) return `₹ ${(num/1e5).toFixed(2).replace(/\.?0+$/, '')} L`;
  return `₹ ${num.toLocaleString('en-IN')}`;
}
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function parseDate(v) {
  if (!v) return null;
  if (v.year && v.month) {
    const d = new Date(Date.UTC(v.year, v.month - 1, v.day || 1, v.hour || 0, v.minute || 0));
    return isNaN(d) ? null : d;
  }
  const d = new Date(v);
  return isNaN(d) ? null : d;
}
function formatAuctionDate(v) {
  const d = parseDate(v);
  if (!d) return '—';
  const day = d.getDate().toString().padStart(2, '0');
  const mon = MONTHS[d.getMonth()];
  const hh = d.getHours().toString().padStart(2, '0');
  const mm = d.getMinutes().toString().padStart(2, '0');
  return `${day} ${mon} · ${hh}:${mm}`;
}
function daysUntil(v) {
  const d = parseDate(v);
  if (!d) return null;
  return Math.ceil((d - new Date()) / 86400e3);
}
function joinLoc(area, city) {
  const parts = [area, city].filter(Boolean);
  return parts.length ? parts.join(', ') : '—';
}
// Human-readable labels for the keys returned by inferType().
const TYPE_LABEL = {
  'flat': 'Residential flat',
  'house': 'Independent house',
  'villa': 'Villa',
  'plot': 'Plot',
  'land': 'Land',
  'land-building': 'Land & building',
  'commercial': 'Commercial property',
  'commercial-property': 'Commercial property',
  'commercial-building': 'Commercial building',
  'godown': 'Godown / warehouse',
  'cold-storage': 'Cold storage',
  'factory': 'Factory',
  'industrial-land': 'Industrial land',
  'industrial-land-building': 'Industrial land & building',
};
function _normEntity(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }
// The raw `title` is frequently the lender/borrower entity (e.g. "… Finance Ltd.")
// rather than a property description, and it duplicates the bank tag. When it looks
// like an entity name, fall back to a descriptive property-type label so cards lead
// with *what* the property is; the location and bank are shown on their own lines.
function deriveDisplayTitle(row, type) {
  const rawTitle = (row.title || '').trim();
  const bankNorm = _normEntity(row.bank);
  const titleNorm = _normEntity(rawTitle);
  const looksLikeEntity = !rawTitle
    || (bankNorm && (titleNorm === bankNorm || titleNorm.includes(bankNorm) || bankNorm.includes(titleNorm)))
    || /\b(ltd|limited|pvt|llp)\b/i.test(rawTitle);
  if (looksLikeEntity) return TYPE_LABEL[type] || 'Property';
  return rawTitle;
}
function toCard(row) {
  const current = Number(row.reserve_price);
  const previous = row.previous_reserve_price != null ? Number(row.previous_reserve_price) : null;
  let drop = null;
  if (previous != null && !isNaN(previous) && !isNaN(current) && previous > current && previous > 0) {
    drop = {
      pct: Math.round(((previous - current) / previous) * 100),
      previousRaw: previous,
      previous: formatINR(previous),
    };
  }
  const type = inferType(row);
  const startD = parseDate(row.auction_start || row.deadline);
  return {
    id: row.auction_id,
    type,
    title: deriveDisplayTitle(row, type),
    loc: joinLoc(row.area, row.city),
    bank: row.bank || null,
    bankShort: row.bank_short || row.bank || null,
    price: formatINR(row.reserve_price),
    emd: formatINR(row.emd),
    date: formatAuctionDate(row.auction_start || row.deadline),
    dateRaw: row.auction_start || row.deadline || null,
    ended: !!(startD && startD < new Date()),
    url: row.url || null,
    drop,
  };
}

/* ====== NAV ====== */
const URL_ID_PARAM = 'id';
function propertyShareURL(id) {
  const u = new URL(window.location.href);
  u.search = '';
  u.hash = '';
  u.pathname = '/property/' + encodeURIComponent(id);
  return u.toString();
}
function pathForScreen(screen) {
  if (screen === 'detail' && currentDetailId) {
    return '/property/' + encodeURIComponent(currentDetailId);
  }
  if (screen === 'results') {
    // Deep-link the open conversation so a refresh restores it. _restoreChatId
    // covers the boot window after refresh, before the thread has loaded and
    // set activeChatId — without it the boot go('results') would rewrite the
    // URL back to bare /chat and lose the id.
    const cid = activeChatId || _restoreChatId;
    return cid ? '/chat/' + encodeURIComponent(cid) : '/chat';
  }
  if (screen === 'watchlist') return '/watchlist';
  if (screen === 'dossiers') return '/dossiers';
  return '/';
}
function syncURLForScreen(screen, replace) {
  // /lab keeps its own URL for the whole session. Without this the boot
  // go('landing') rewrites it to '/', and lab.js — which loads after app.js
  // and gates itself on location.pathname — finds itself on the wrong page
  // and the inspector never mounts.
  if (IS_LAB) return;
  const target = pathForScreen(screen);
  // Supabase OAuth/magic-link callback markers — leave the URL alone until the
  // SDK has consumed them, otherwise pushState strips the code and the user
  // round-trips through Google but stays signed out.
  const search = new URLSearchParams(window.location.search);
  if (search.has('code') || search.has('error_code') || window.location.hash.includes('access_token')) return;
  if (window.location.pathname === target && !window.location.search) return;
  const method = replace ? 'replaceState' : 'pushState';
  history[method]({ screen, id: currentDetailId }, '', target);
}
function go(screen) {
  // Dossiers ships dark for the public release (see the index.html bootstrap +
  // backend DOSSIERS_ENABLED). When it's off, treat any route to it — a tab
  // click, a /dossiers deep link, or the detail-page button — as a no-op that
  // lands on home, so the hidden feature is never reachable.
  if (screen === 'dossiers' && !window.DOSSIERS_ENABLED) screen = 'landing';
  // 'browse' is a virtual nav target (used by the "browse all properties" CTA): it
  // shows the home screen scrolled to the property listing. There's no separate
  // browse tab — it shares the home screen — so the home tab stays highlighted.
  const screenName = screen === 'browse' ? 'landing' : screen;
  if (screenName === 'detail' && currentScreen && currentScreen !== 'detail') {
    detailReturnScreen = currentScreen;
    const labels = { landing: '← back to all properties', browse: '← back to all properties', results: '← back to results', watchlist: '← back to watchlist' };
    const btn = document.getElementById('detail-back');
    if (btn) btn.textContent = labels[detailReturnScreen] || '← back';
  }
  currentScreen = screenName;
  document.querySelectorAll('.screen').forEach(s => s.classList.toggle('on', s.dataset.screen === screenName));
  document.querySelectorAll('.top-nav button').forEach(b => {
    const active = b.dataset.nav === screenName;
    b.classList.toggle('on', active);
    if (active) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.bottom-tabs .bt').forEach(b => {
    const active = b.dataset.nav === screenName;
    b.classList.toggle('on', active);
    if (active) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (screen === 'browse') {
    const bs = document.getElementById('browse-section');
    if (bs) bs.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  if (screenName === 'results') { renderResults(); setMobileTab('chat'); }
  if (screenName === 'watchlist') renderWatchlist();
  if (screenName === 'dossiers' && window.Dossiers) window.Dossiers.render();
  if (screenName === 'detail') {
    const empty = document.getElementById('detail-empty');
    const content = document.getElementById('detail-content');
    const backWrap = document.getElementById('detail-back-wrap');
    if (currentDetailId) {
      if (empty) empty.style.display = 'none';
      if (content) content.style.display = '';
      if (backWrap) backWrap.style.display = '';
      loadAndRenderDetail(currentDetailId);
    } else {
      if (empty) empty.style.display = '';
      if (content) content.style.display = 'none';
      if (backWrap) backWrap.style.display = 'none';
    }
  }
  syncURLForScreen(screenName, false);
}
document.querySelectorAll('.top-nav button').forEach(b => {
  b.addEventListener('click', () => go(b.dataset.nav));
});
document.querySelectorAll('.bottom-tabs .bt').forEach(b => {
  b.addEventListener('click', () => go(b.dataset.nav));
});

/* ====== Auto-hide bottom navigation (mobile) ======
   The bottom tab bar stays tucked away and only slides up when the user
   interacts with the page — a scroll, a touch, a tap, or a wheel — then
   retracts again after a short pause of inactivity, keeping the limited
   mobile viewport focused on content. Desktop never renders the bar
   (display:none), so this is a no-op there. */
(function () {
  const nav = document.querySelector('.bottom-tabs');
  if (!nav) return;
  const mq = window.matchMedia('(max-width: 640px)');
  const HIDE_DELAY = 2600;
  let hideTimer = null;

  function hide() {
    if (mq.matches) nav.classList.add('nav-hidden');
  }
  function reveal() {
    clearTimeout(hideTimer);
    nav.classList.remove('nav-hidden');
    if (mq.matches) hideTimer = setTimeout(hide, HIDE_DELAY);
  }
  function reset() {
    clearTimeout(hideTimer);
    // Start (and stay, on resize) tucked away on mobile; always visible on desktop.
    nav.classList.toggle('nav-hidden', mq.matches);
  }

  ['scroll', 'touchstart', 'touchmove', 'pointerdown', 'wheel'].forEach(ev =>
    window.addEventListener(ev, reveal, { passive: true, capture: true })
  );
  // Keep it up while the pointer rests on the bar (mid-tap); re-arm on leave.
  nav.addEventListener('pointerenter', () => clearTimeout(hideTimer));
  nav.addEventListener('pointerleave', reveal);

  if (mq.addEventListener) mq.addEventListener('change', reset);
  reset();
})();

/* ====== Chat sidebar drawer (mobile slide-in) ====== */
function openSidebar() {
  const sb = document.getElementById('chat-sidebar');
  const bd = document.getElementById('sb-backdrop');
  if (sb) sb.classList.add('mobile-on');
  if (bd) bd.classList.add('on');
}
function closeSidebar() {
  const sb = document.getElementById('chat-sidebar');
  const bd = document.getElementById('sb-backdrop');
  if (sb) sb.classList.remove('mobile-on');
  if (bd) bd.classList.remove('on');
}
{
  const bd = document.getElementById('sb-backdrop');
  if (bd) bd.addEventListener('click', closeSidebar);
}

/* ====== Mobile tabs (results screen panels) ====== */
function setMobileTab(panel) {
  // panel: 'history' | 'chat' | 'results'
  const tabs = document.querySelectorAll('#results-mobile-tabs button');
  if (!tabs.length) return;
  const resultsPane = document.querySelector('.results .results-pane');
  if (panel === 'history') {
    // History slides in over the results area like the matches panel; tapping the
    // clock again toggles it shut. (No backdrop — close via the tabs or by
    // picking a chat.)
    const sb = document.getElementById('chat-sidebar');
    if (sb && sb.classList.contains('mobile-on')) closeSidebar();
    else openSidebar();
    return;
  }
  // Switching to conversation/matches dismisses the history overlay if open.
  closeSidebar();
  tabs.forEach(b => {
    const active = b.dataset.mtab === panel;
    b.classList.toggle('on', active);
    if (b.dataset.mtab !== 'history') b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  // The conversation (transcript) is the always-present base layer; the matches
  // panel is an overlay that slides in from the right when 'results' is active.
  if (panel === 'chat') {
    resultsPane && resultsPane.classList.remove('show');
  } else if (panel === 'results') {
    resultsPane && resultsPane.classList.add('show');
  }
}
document.querySelectorAll('#results-mobile-tabs button').forEach(b => {
  b.addEventListener('click', () => setMobileTab(b.dataset.mtab));
});

/* ====== answer-mode picker (shares the model/effort picker format) ====== */
// Mode lives in window.currentMode (read by _chatRequestBody). The picker mirrors
// the model picker exactly: a pill that opens a popover of modes (label +
// description + check), so both composer controls read as one component. Seeded
// with a default list + friendlier copy than the raw /modes text, then refreshed
// by hydrateModes() so the control works even before /modes responds.
// /chat/v2 — the tiered loop (plan once, run the queries in parallel,
// answer once). Off by default; flip with ?chatv2=1 or
// localStorage.chat_v2='1'. The response is shaped like v1's apart from the
// conversation-state channel: v2 echoes a small `scope` object where v1
// round-trips the whole message_history.
const IS_LAB = (() => {
  try { return /^\/lab\/?$/.test(location.pathname); } catch (_) { return false; }
})();
const CHAT_V2 = (() => {
  // /lab IS the v2 surface, so the flag is implied there and cannot be turned
  // off from the URL — otherwise the page would silently show v1 and the
  // inspector would sit empty with nothing explaining why.
  if (IS_LAB) return true;
  try {
    const q = new URLSearchParams(location.search).get('chatv2');
    if (q === '1' || q === '0') { localStorage.setItem('chat_v2', q); return q === '1'; }
    return localStorage.getItem('chat_v2') === '1';
  } catch (_) { return false; }
})();
// Which loop answers a turn. Three exist and they differ in where the
// conversation lives, which is the whole subject of the A/B:
//   'v1'     /chat       pydantic-ai ReAct; client round-trips the transcript
//   'tiered' /chat/v2    plan-execute-synthesize; client round-trips a summary
//   'deep'   /chat/deep  Deep Agents ReAct; the server owns the transcript
// `?loop=tiered` (or localStorage.chat_loop) switches back.
//
// The flagged surface DEFAULTS TO 'tiered', and it briefly did not. The deep
// loop was made the default on the argument that a transcript answers
// referring questions a summary cannot — which the A/B confirmed: it resolves
// every one in the catalogue, including an off-topic aside and back again.
// But it takes 149 s at the median against the tiered loop's 25 s, and the
// idle guard below gives up at 75 s, so most of its correct answers never
// reach the person who asked. A default that loses the measurement it was
// set on has to move back; the picker is still there for anyone comparing.
// See docs/chat-loop-ab-2026-08.md.
//
// Both endpoints are admin-only, so this only decides what an ADMIN sees on
// /lab. A signed-in user gets 'v1' — CHAT_V2 above is false off /lab.
//   'agent3' /chat/agent3  the auction-specialised agent: six graph tools that
//            reach into the sale notice, on-demand skills, and the answer /
//            intent gates. Server owns the transcript, same as 'deep'.
//            See docs/auction-deep-agent-2026-08.md.
const CHAT_LOOPS = ['tiered', 'deep', 'agent3'];
const CHAT_LOOP = (() => {
  if (!CHAT_V2) return 'v1';
  try {
    const q = new URLSearchParams(location.search).get('loop');
    if (CHAT_LOOPS.includes(q)) { localStorage.setItem('chat_loop', q); return q; }
    const saved = localStorage.getItem('chat_loop');
    if (CHAT_LOOPS.includes(saved)) return saved;
  } catch (_) { /* private mode — fall through to the default */ }
  return 'tiered';
})();
const CHAT_DEEP = CHAT_LOOP === 'deep';
const CHAT_AGENT3 = CHAT_LOOP === 'agent3';
// The property that actually matters at every call site below: the server
// owns the transcript under a thread key, so there is no scope object and no
// message history to round-trip. Both 'deep' and 'agent3' work this way, and
// writing `CHAT_DEEP || CHAT_AGENT3` in five places is how the third one gets
// missed.
const CHAT_THREADED = CHAT_DEEP || CHAT_AGENT3;
//: Base path per loop, in ONE place. The thread DELETE below used to hardcode
//: `/chat/deep/`, which is exactly the kind of second copy that survives a
//: rename and silently stops forgetting anything.
const LOOP_BASE = { deep: '/chat/deep', agent3: '/chat/agent3' };
// Published for the /lab inspector's picker. It must show the loop that is
// ACTUALLY answering, and it cannot re-derive that: the resolution above
// reads a query param, then localStorage, then a default, and lab.js
// duplicating those three steps is a second source of truth that drifts. It
// drifted the first time the default moved.
try { window.__chatLoop = CHAT_LOOP; } catch (_) { /* non-browser host */ }
// The /lab inspector renders one gate shape: `{ ok, reason }`. v2 and deep
// return exactly that. agent3 returns `{ repairs, repaired, advisory }` —
// a different mechanism, because its gate REWRITES a bad draft rather than
// flagging a shipped one, so "ok" means "nothing had to be repaired".
//
// Normalised here rather than in lab.js: the inspector is a diagnostic that
// must not need to know how many loops exist. Without this, `gate.ok` is
// undefined on every agent3 turn and the inspector shows a gate warning on
// all of them — a false alarm on the one surface built to spot real ones.
function normalizeGate(gate) {
  if (!gate) return null;
  if (gate.ok !== undefined) return gate;
  const repairs = gate.repairs || 0;
  return {
    ok: repairs === 0,
    // The advisory list is deliberately NOT surfaced as a problem: measured
    // across five runs it is 34 findings and zero true positives.
    reason: repairs
      ? `${repairs} repair(s): ${(gate.repaired || []).join('; ')}`
      : '',
  };
}

const chatPath = (suffix) => {
  if (CHAT_THREADED) return `${API_BASE}${LOOP_BASE[CHAT_LOOP]}${suffix}`;
  return `${API_BASE}/chat${CHAT_V2 ? '/v2' : ''}${suffix}`;
};
// v2's conversation state: one small dict, echoed back each turn in place of
// the growing transcript apiMessageHistory holds for v1.
let apiChatScope = null;
// The deep loop's entire client-side state. The transcript itself lives in
// Neo4j under this key — which is the point: state the server depends on is
// state the server owns.
let apiChatThreadId = null;

// Forget the current conversation's agent state — ALL of it, whichever loop
// produced it. It exists because the clearing was open-coded at four sites:
// `apiMessageHistory` was reset at all four and `apiChatScope`, added later,
// at none, so starting a new chat carried the previous thread's city, price
// band and area names into the next conversation's first question. A third
// channel (`apiChatThreadId`) would have repeated it, so there is now one
// owner and every reset calls it.
function resetAgentState() {
  apiMessageHistory = null;
  apiChatScope = null;
  const staleThread = apiChatThreadId;
  apiChatThreadId = null;
  // A threaded loop's transcript lives server-side, so nulling a local
  // variable forgets nothing — the next turn on the same key would resume the
  // old conversation. Best-effort: a failed cleanup must never block starting
  // a new chat, and the checkpointer prunes the thread on its own bound anyway.
  if (staleThread && CHAT_THREADED) {
    try {
      authFetch(`${API_BASE}${LOOP_BASE[CHAT_LOOP]}/${encodeURIComponent(staleThread)}`,
                { method: 'DELETE' }).catch(() => {});
    } catch (_) { /* never block a new chat on cleanup */ }
  }
}

window.currentMode = 'ask';
const MODE_DESC = {
  'ask': 'Search and ask questions across every auction.',
  'deep-research': 'Full due diligence on a single property.',
};
let chatModeOpts = [
  { id: 'ask', label: 'Ask', description: MODE_DESC['ask'] },
  { id: 'deep-research', label: 'Deep research', description: MODE_DESC['deep-research'] },
];
// (Re)render every mode picker from the current option list + selection.
function renderModePickers() {
  const show = chatModeOpts.length >= 2;   // a single mode isn't a choice
  document.querySelectorAll('.mode-picker').forEach(picker => {
    if (!show) { picker.hidden = true; return; }
    picker.hidden = false;
    const cur = window.currentMode || 'ask';
    const parts = ['<div class="mp-group-label">Answer mode</div>'];
    chatModeOpts.forEach(o => parts.push(_mpRow('mode', o, o.id === cur)));
    const pop = picker.querySelector('.mode-picker-pop');
    if (pop) pop.innerHTML = parts.join('');
    const lbl = picker.querySelector('.mpt-mode');
    if (lbl) lbl.textContent = _mpLabel(chatModeOpts, cur);
  });
}

/* ====== model + thinking-effort picker ====== */
// State + persistence contract (unchanged from the original toggles):
// window.currentModel / window.currentReasoningEffort hold the chosen ids (or
// null = "let the server pick the tier default"); both are mirrored to
// localStorage and read by _chatRequestBody. The server re-gates every turn, so
// these are advisory UI state only — a free user can't unlock Pro by tampering.
window.currentModel = null;
window.currentReasoningEffort = null;
const CHAT_MODEL_KEY = 'chat_model';
const CHAT_EFFORT_KEY = 'chat_effort';
let chatModelOpts = [];   // [{id,label,description,locked}] from /chat/models
let chatEffortOpts = [];  // [{id,label,description}] from /chat/models

const _MP_CHECK = '<svg class="mp-item-check" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>';

function _mpLabel(list, id) {
  const hit = list.find(o => o.id === id);
  return hit ? hit.label : (id || '');
}
function _mpRow(kind, opt, selected) {
  // Server marks paid-only options with locked: true — models AND reasoning
  // efforts above the free-tier ceiling. Render both disabled with the badge.
  const locked = !!opt.locked;
  return (
    `<button type="button" class="mp-item" role="menuitemradio" aria-checked="${selected ? 'true' : 'false'}"` +
    ` data-kind="${kind}" data-id="${escapeHtml(opt.id)}"${locked ? ' disabled aria-disabled="true"' : ''}>` +
      `<span class="mp-item-main">` +
        `<span class="mp-item-label">${escapeHtml(opt.label)}${locked ? '<span class="mp-lock">Paid</span>' : ''}</span>` +
        (opt.description ? `<span class="mp-item-desc">${escapeHtml(opt.description)}</span>` : '') +
      `</span>` + _MP_CHECK +
    `</button>`
  );
}
// (Re)render every picker instance from the current option lists + selection.
// Rebuilds the popover rows and trigger labels in place; never touches the
// open/closed state, so it's safe to call after each pick.
function renderModelPickers() {
  const showModels = chatModelOpts.length >= 2;   // a single model isn't a choice
  const showEfforts = chatEffortOpts.length >= 1;
  document.querySelectorAll('.model-picker').forEach(picker => {
    if (!showModels && !showEfforts) { picker.hidden = true; return; }
    picker.hidden = false;
    const parts = [];
    if (showModels) {
      parts.push('<div class="mp-group-label">Model</div>');
      chatModelOpts.forEach(o => parts.push(_mpRow('model', o, o.id === window.currentModel)));
    }
    if (showModels && showEfforts) parts.push('<div class="mp-divider"></div>');
    if (showEfforts) {
      parts.push('<div class="mp-group-label">Thinking effort</div>');
      chatEffortOpts.forEach(o => parts.push(_mpRow('effort', o, o.id === window.currentReasoningEffort)));
    }
    const pop = picker.querySelector('.model-picker-pop');
    if (pop) pop.innerHTML = parts.join('');
    const mEl = picker.querySelector('.mpt-model');
    const eEl = picker.querySelector('.mpt-effort');
    const dot = picker.querySelector('.mpt-dot');
    if (mEl) { mEl.textContent = showModels ? _mpLabel(chatModelOpts, window.currentModel) : ''; mEl.hidden = !showModels; }
    if (eEl) { eEl.textContent = showEfforts ? _mpLabel(chatEffortOpts, window.currentReasoningEffort) : ''; eEl.hidden = !showEfforts; }
    if (dot) dot.hidden = !(showModels && showEfforts);
  });
}
// Both composer controls — model/effort and answer-mode — are the same pill +
// popover component, so the open/close/position/keyboard plumbing drives them
// together via these shared selectors.
const _PICKER_SEL = '.model-picker, .mode-picker';
const _PICKER_TRIGGER_SEL = '.model-picker-trigger, .mode-picker-trigger';
const _PICKER_POP_SEL = '.model-picker-pop, .mode-picker-pop';
const _PICKER_OPEN_SEL = '.model-picker-trigger[aria-expanded="true"], .mode-picker-trigger[aria-expanded="true"]';
function _closeModelPickers(except) {
  document.querySelectorAll(_PICKER_SEL).forEach(p => {
    if (p === except) return;
    const t = p.querySelector(_PICKER_TRIGGER_SEL);
    const pop = p.querySelector(_PICKER_POP_SEL);
    if (t) t.setAttribute('aria-expanded', 'false');
    if (pop) pop.hidden = true;
  });
}
// Place the popover so it never clips behind the header. The panel normally
// drops UP from the trigger, but on the landing composer (which sits high, just
// under the sticky topbar) there isn't room above — so flip it DOWN and cap its
// height to the available space when up-room is short. Measured on each open.
function _positionModelPop(picker) {
  if (!picker) return;
  const trigger = picker.querySelector(_PICKER_TRIGGER_SEL);
  const pop = picker.querySelector(_PICKER_POP_SEL);
  if (!trigger || !pop) return;
  const GAP = 8, MARGIN = 8, HARD_MAX = 380;
  const rect = trigger.getBoundingClientRect();
  // Top boundary: keep the panel below the header. The desktop topbar paints
  // above the screen (its own z-index), so honour it whenever it currently sits
  // above the trigger — whether sticky or a relative bar at the top of the page.
  // Once scrolled out of view (bottom <= 0) it stops counting.
  let topBound = MARGIN;
  const topbar = document.querySelector('.topbar');
  if (topbar) {
    const tb = topbar.getBoundingClientRect();
    if (tb.bottom > topBound && tb.bottom <= rect.top) topBound = tb.bottom + MARGIN;
  }
  const spaceAbove = rect.top - GAP - topBound;
  const spaceBelow = window.innerHeight - rect.bottom - GAP - MARGIN;
  // Keep the established drop-up when it comfortably fits; otherwise use whichever
  // side has more room so the panel is fully visible and scrolls internally.
  const dropUp = spaceAbove >= Math.min(HARD_MAX, 240) || spaceAbove >= spaceBelow;
  pop.classList.toggle('drop-down', !dropUp);
  pop.style.maxHeight = Math.max(180, Math.min(HARD_MAX, dropUp ? spaceAbove : spaceBelow)) + 'px';
}
// Delegated interactions: toggle the popover, pick a model/effort, click-away to
// close. Selecting keeps the popover open (it's a combined settings panel), so
// the user can set model and effort in one visit; the checkmark + trigger labels
// update live across every chat bar.
document.addEventListener('click', (e) => {
  if (!e.target.closest) return;
  const trigger = e.target.closest(_PICKER_TRIGGER_SEL);
  if (trigger) {
    const picker = trigger.closest(_PICKER_SEL);
    const pop = picker && picker.querySelector(_PICKER_POP_SEL);
    const willOpen = trigger.getAttribute('aria-expanded') !== 'true';
    _closeModelPickers(picker);
    trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    if (pop) pop.hidden = !willOpen;
    if (pop && willOpen) _positionModelPop(picker);
    return;
  }
  const item = e.target.closest('.mp-item');
  if (item) {
    if (item.hasAttribute('disabled')) return;   // locked model — server gate also enforces
    const kind = item.dataset.kind, id = item.dataset.id;
    if (kind === 'mode') {
      // A single choice: set it, refresh label + check, then close the popover.
      window.currentMode = id || 'ask';
      renderModePickers();
      _closeModelPickers(null);
      return;
    }
    if (kind === 'model') {
      window.currentModel = id || null;
      try { localStorage.setItem(CHAT_MODEL_KEY, id); } catch(_) {}
    } else if (kind === 'effort') {
      window.currentReasoningEffort = id || null;
      try { localStorage.setItem(CHAT_EFFORT_KEY, id); } catch(_) {}
    }
    renderModelPickers();
    return;
  }
  if (!e.target.closest(_PICKER_SEL)) _closeModelPickers(null);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const open = document.querySelector(_PICKER_OPEN_SEL);
  if (!open) return;
  _closeModelPickers(null);
  open.focus();
});
// Re-fit an open popover when the viewport changes (e.g. mobile rotate / resize).
window.addEventListener('resize', () => {
  const open = document.querySelector(_PICKER_OPEN_SEL);
  if (open) _positionModelPop(open.closest(_PICKER_SEL));
});

/* ====== theme toggle ====== */
(function () {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  // Stroke icons (not emoji) so the toggle matches the rest of the icon set.
  const MOON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  const SUN = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.6"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/></svg>';
  const sync = () => {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    btn.innerHTML = dark ? SUN : MOON;
    const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
    btn.setAttribute('aria-label', label);
    btn.title = label;
  };
  sync();
  btn.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    sync();
  });
})();

/* ====== API ====== */
// auction_ids currently shown in the matches panel, in the order on screen.
// Sent with each chat turn as `panel_auction_ids` so the agent can resolve
// "compare these" / "the matches" / "all of them" to concrete ids. The panel
// is browser-only state (it can come from browse filters or a restored chat,
// not just the live history), so without this the agent has nothing to point
// at. We send ids only — cheap — and let the agent re-fetch detail on demand.
function _currentPanelAuctionIds() {
  if (!Array.isArray(currentResults) || !currentResults.length) return [];
  // Mirror what the user sees: the panel renders sortResults(currentResults).
  const ordered = sortResults(currentResults, currentSort);
  const ids = [];
  const seen = new Set();
  for (const row of ordered) {
    const raw = row && (row.auction_id != null ? row.auction_id : row.id);
    if (raw == null || raw === '') continue;
    const id = String(raw);
    if (seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
    if (ids.length >= 50) break;  // matches the backend's _MAX_PANEL_IDS cap
  }
  return ids;
}
function _chatRequestBody(message) {
  const mode = window.currentMode || 'ask';
  if (!_requireAuthForMode(mode)) throw new Error('login required for this mode');
  // pendingChatScope is set by the "chat about these" button on the browse
  // panel — passed once on the next turn, then cleared so it doesn't cling
  // to follow-up questions outside the original filter context.
  const activeFilters = window.pendingChatScope || null;
  window.pendingChatScope = null;
  const panelIds = _currentPanelAuctionIds();
  let body;
  if (CHAT_THREADED) {
    // No scope and no transcript — just the thread key. On the first turn it
    // is null and the server mints one, which the response hands back.
    body = { message, mode, thread_id: apiChatThreadId || undefined,
             panel: panelIds.length ? { matches: panelIds } : undefined };
  } else if (CHAT_V2) {
    body = { message, mode, scope: apiChatScope || undefined,
             panel: panelIds.length ? { matches: panelIds } : undefined };
  } else {
    body = { message, message_history: apiMessageHistory, mode };
  }
  if (activeFilters) body.active_filters = activeFilters;
  if (!CHAT_V2 && panelIds.length) body.panel_auction_ids = panelIds;
  // Model + thinking-effort toggles (omitted when null so the server applies
  // the tier default). The server re-gates these, so a free user can't unlock
  // Pro by tampering with the body.
  if (window.currentModel) body.model = window.currentModel;
  if (window.currentReasoningEffort) body.reasoning_effort = window.currentReasoningEffort;
  return body;
}
function _chatHttpError(res) {
  if (res.status === 401) { if (window.Auth) window.Auth.openLoginModal(); return new Error('login required'); }
  if (res.status === 429) {
    // The daily-quota 429 is today's de-facto paywall/upgrade gate (plan §8).
    if (window.track) window.track('paywall_view', { reason: 'rate_limit', surface: 'chat' });
    return new Error('rate limit reached — please sign in or try again later');
  }
  return new Error(`chat ${res.status}`);
}
// `prebuilt` lets askAI reuse the exact body from a failed streaming attempt
// so the one-shot pendingChatScope isn't lost on fallback.
async function apiChat(message, prebuilt) {
  const body = prebuilt || _chatRequestBody(message);
  const res = await authFetch(chatPath(''), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw _chatHttpError(res);
  return res.json();
}
// Streaming twin of apiChat against /chat/stream (SSE over POST — fetch +
// ReadableStream because EventSource can't POST). Calls onEvent(name, data)
// for `status` and `delta` frames; resolves with the `final` payload, which
// is shaped exactly like blocking /chat's JSON. Errors carry .useFallback
// when the blocking endpoint should be retried (old backend / transport
// failure before the server started working) — never after the model run
// has started, so a failed turn isn't silently billed twice.
// Idle + total guards for the chat stream, replacing the old hard 120s
// fetch timeout (which aborted turns the server was still working on —
// the "BodyStreamBuffer was aborted" failure). The server emits an SSE
// keepalive comment every ~15s while the agent thinks, so 75s of wire
// silence means the connection is genuinely dead, not just a slow model.
// The total cap is a last-resort backstop for a stream that keeps ticking
// but never finishes.
const CHAT_STREAM_IDLE_TIMEOUT_MS = 75000;
const CHAT_STREAM_MAX_MS = 600000;
async function apiChatStream(body, onEvent) {
  const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
  let idleTimer = null, totalTimer = null, timedOut = false;
  const armIdle = () => {
    if (!ctrl) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { timedOut = true; ctrl.abort(); }, CHAT_STREAM_IDLE_TIMEOUT_MS);
  };
  if (ctrl) {
    totalTimer = setTimeout(() => { timedOut = true; ctrl.abort(); }, CHAT_STREAM_MAX_MS);
    armIdle();
  }
  try {
    let res;
    try {
      res = await authFetch(chatPath('/stream'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        ...(ctrl ? { signal: ctrl.signal } : {}),
      });
    } catch (e) {
      if (e && e.name === 'AbortError') {
        // Our own idle/total guard fired — the model may still be running
        // server-side, so no fallback retry (it would bill the turn twice).
        throw timedOut ? new Error('the server stopped responding — please retry') : e;
      }
      const err = new Error('network error'); err.useFallback = true; throw err;
    }
    if (res.status === 404 || res.status === 405) {
      // Backend without /chat/stream (older deploy) — use blocking /chat.
      const err = new Error(`chat ${res.status}`); err.useFallback = true; throw err;
    }
    if (!res.ok) throw _chatHttpError(res);
    if (!res.body || !res.body.getReader) {
      const err = new Error('streaming unsupported'); err.useFallback = true; throw err;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let final = null;
    while (true) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        if (timedOut) throw new Error('the answer stalled — please retry');
        throw e;
      }
      const { done, value } = chunk;
      if (done) break;
      armIdle(); // any bytes (keepalives included) prove the server is alive
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, sep); buf = buf.slice(sep + 2);
        let ev = 'message', data = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        let payload;
        try { payload = JSON.parse(data); } catch (e) { continue; }
        if (ev === 'final') final = payload;
        else if (ev === 'error') throw new Error(payload.detail || 'chat failed');
        else if (onEvent) onEvent(ev, payload);
      }
    }
    if (!final) throw new Error('stream ended unexpectedly — please retry');
    return final;
  } finally {
    clearTimeout(idleTimer);
    clearTimeout(totalTimer);
  }
}
async function hydrateModes() {
  try {
    const res = await authFetch(`${API_BASE}/modes`);
    if (res.ok) {
      const data = await res.json();
      const modes = Array.isArray(data?.modes) ? data.modes : [];
      if (modes.length) {
        // Prefer our friendlier copy for known modes; fall back to the API text.
        chatModeOpts = modes.map(m => ({
          id: m.id,
          label: m.label || m.id,
          description: MODE_DESC[m.id] || m.description || '',
        }));
      }
    }
  } catch(e) { /* keep the seeded default mode list */ }
  renderModePickers();
}
// Swap the hardcoded starter chips for live, data-driven ones from
// GET /suggestions (built from the current graph, so they can't point at zero
// matches — see api/chat/suggestions.py). Best-effort: on any failure or an
// empty payload the markup's hardcoded fallback chips stay in place. Both
// `.landing-suggest` rows (landing screen + chat panel) get the same set.
async function hydrateSuggestions() {
  try {
    const res = await authFetch(`${API_BASE}/suggestions`);
    if (!res.ok) return;
    const data = await res.json();
    const items = Array.isArray(data?.suggestions) ? data.suggestions : [];
    if (!items.length) return;  // keep the fallback chips
    const html = items
      .filter(s => s && s.q && s.label)
      .map(s => `<span class="chip" data-q="${escapeHtml(s.q)}">${escapeHtml(s.label)}</span>`)
      .join('');
    if (!html) return;
    document.querySelectorAll('.landing-suggest').forEach(row => { row.innerHTML = html; });
  } catch (_) { /* best-effort; hardcoded chips remain */ }
}
// Populate the model + thinking-effort picker from the tier-aware
// GET /chat/models. Re-run on auth change so upgrading to paid unlocks Pro
// without a refresh. The picker shows its Model section only when 2+ models
// exist; locked models render disabled with a "Paid" badge, and the server
// enforces the gate regardless.
async function hydrateChatModels() {
  try {
    const res = await authFetch(`${API_BASE}/chat/models`);
    if (!res.ok) return;
    const data = await res.json();
    const models = Array.isArray(data?.models) ? data.models : [];
    const efforts = Array.isArray(data?.reasoning_efforts) ? data.reasoning_efforts : [];
    const defModel = (data?.defaults && data.defaults.model) || 'flash';
    const defEffort = (data?.defaults && data.defaults.reasoning_effort) || 'high';
    let savedModel = null, savedEffort = null;
    try { savedModel = localStorage.getItem(CHAT_MODEL_KEY); savedEffort = localStorage.getItem(CHAT_EFFORT_KEY); } catch(_) {}
    // Never let a locked (or unknown) saved choice stick — fall back to default.
    const unlocked = new Set(models.filter(m => !m.locked).map(m => m.id));
    const model = unlocked.has(savedModel) ? savedModel : defModel;
    const unlockedEfforts = new Set(efforts.filter(e => !e.locked).map(e => e.id));
    const effort = unlockedEfforts.has(savedEffort) ? savedEffort : defEffort;
    window.currentModel = model;
    window.currentReasoningEffort = effort;
    chatModelOpts = models;
    chatEffortOpts = efforts;
    renderModelPickers();
  } catch(e) { /* leave the picker hidden; the server still applies tier defaults */ }
}
async function apiAuctionDetail(auctionId) {
  const res = await authFetch(`${API_BASE}/auction/${encodeURIComponent(auctionId)}`);
  if (!res.ok) throw new Error(`detail ${res.status}`);
  return res.json();
}
async function apiFeedback(payload) {
  const res = await authFetch(`${API_BASE}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`feedback ${res.status}`);
  return res.json();
}

/* ====== CHAT ====== */
// One detail artifact may carry several records now (get_auction_detail
// accepts a list of ids), so callers get an array. Stored single-record
// artifacts still yield exactly one row.
function detailArtifactToRows(art) {
  const res = art && art.result;
  if (!res) return [];
  if (Array.isArray(res.results)) return res.results.map(detailRecordToRow).filter(Boolean);
  const row = detailRecordToRow(res);
  return row ? [row] : [];
}
function detailRecordToRow(res) {
  if (!res || !res.auction_id) return null;
  const f = res.fields || {};
  const rel = res.relationships || {};
  const prior = (res.price_history || [])
    .filter(p => !p.is_current && p.reserve_price_num != null)
    .map(p => Number(p.reserve_price_num))
    .filter(n => !Number.isNaN(n));
  const previous = prior.length ? Math.max.apply(null, prior) : null;
  return {
    auction_id: res.auction_id,
    title: f.title,
    url: f.url,
    reserve_price: f.reserve_price_num,
    emd: f.emd_num,
    auction_start: f.auction_start_dt,
    city: rel.city && rel.city.name,
    area: rel.area && rel.area.name,
    bank: rel.bank && rel.bank.name,
    asset_category: rel.asset_category && rel.asset_category.name,
    property_types: rel.property_types || [],
    previous_reserve_price: previous,
  };
}
function extractWebSources(artifacts) {
  if (!Array.isArray(artifacts)) return [];
  const seen = new Set();
  const out = [];
  for (const a of artifacts) {
    if (!a || a.tool !== 'internet_search') continue;
    const srcs = (a.result && Array.isArray(a.result.sources)) ? a.result.sources : [];
    for (const s of srcs) {
      if (!s || !s.url || seen.has(s.url)) continue;
      seen.add(s.url);
      out.push(s);
    }
  }
  return out;
}
// The auction IDs this turn's tool calls actually returned. We only linkify a
// bare ID the answer mentions (e.g. "Equitas SFB Auction ( 765494 )") when the
// backend really surfaced it, so an arbitrary number that merely looks like an
// ID is never turned into a dead link.
function collectAuctionIds(artifacts) {
  const ids = new Set();
  try {
    const { rows } = extractResultsFromArtifacts(artifacts);
    for (const r of (rows || [])) {
      if (r && r.auction_id != null && r.auction_id !== '') ids.add(String(r.auction_id));
    }
  } catch (_) { /* artifacts may be malformed mid-stream — no IDs is fine */ }
  return ids;
}

// Apply `fn` only to text that sits between tags, never inside a tag or its
// attributes, so we can inject anchors into rendered-markdown HTML without ever
// corrupting an existing href/class/title.
function mapHtmlText(html, fn) {
  return String(html).replace(/(<[^>]+>)|([^<]+)/g, (_, tag, text) => tag != null ? tag : fn(text));
}

// Turn [1][2][3] citations into links to their matching web source, and bare
// auction IDs into links that open that property. Operates on the already-
// rendered, already-escaped markdown HTML for an AI answer.
function linkifyAnswerHtml(html, m) {
  if (!html) return html;
  const sources = extractWebSources(m && m.artifacts);
  const ids = collectAuctionIds(m && m.artifacts);

  if (sources.length) {
    html = mapHtmlText(html, (text) => text.replace(/\[(\d{1,3})\]/g, (full, n) => {
      const src = sources[+n - 1];
      if (!src || !src.url) return full;
      return `<a class="citation" href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(src.title || src.url)}">[${n}]</a>`;
    }));
  }

  if (ids.size) {
    const pattern = Array.from(ids).map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
    const re = new RegExp('\\b(' + pattern + ')\\b', 'g');
    html = mapHtmlText(html, (text) => text.replace(re, (full) =>
      `<a class="prop-id-link" role="button" tabindex="0" data-property-id="${escapeHtml(full)}" title="open this property">${full}</a>`));
  }

  return html;
}

function extractResultsFromArtifacts(artifacts) {
  if (!Array.isArray(artifacts)) return { rows: [], total: null, tool: null };
  const pick = (names) => artifacts.filter(a => names.includes(a.tool)).pop();
  // Prefer list-shaped results when present; a follow-up detail call should
  // still swap the panel to the cards the user just asked about. When the
  // user gives multiple parcel ids ("show me 708365 & 701641") the agent
  // calls get_auction_detail once per id — render every detail artifact,
  // not just the last one.
  const listArt = pick(SEARCH_TOOLS) || pick(LIST_TOOLS);
  const detailArts = artifacts.filter(a => DETAIL_TOOLS.includes(a.tool));
  const listIdx = listArt ? artifacts.indexOf(listArt) : -1;
  const lastDetailIdx = detailArts.length ? artifacts.indexOf(detailArts[detailArts.length - 1]) : -1;
  if (detailArts.length && lastDetailIdx > listIdx) {
    const rows = detailArts.flatMap(detailArtifactToRows);
    return { rows, total: rows.length, tool: detailArts[0].tool };
  }
  if (!listArt) return { rows: [], total: null, tool: null };
  const primary = listArt;
  const r = primary.result;
  let rows = [];
  let total = null;
  // Prefer ui_rows when the backend attached it — this carries the full
  // match set (up to the UI cap) even when the LLM only saw a 20-row sample.
  if (Array.isArray(primary.ui_rows) && primary.ui_rows.length) {
    rows = primary.ui_rows;
  } else if (Array.isArray(r)) {
    rows = r;
  } else if (r && Array.isArray(r.results)) {
    rows = r.results;
  }
  if (r && typeof r.total_count === 'number') total = r.total_count;
  return { rows, total, tool: primary.tool };
}

// The matches a chat message put on the panel, re-derived from the
// artifacts stored on it — {rows, total} or null for messages that didn't
// touch the panel. Powers the per-turn "N matches" chips, including on
// conversations saved before the chips existed.
function _msgMatches(m) {
  if (!m || m.role !== 'ai' || !Array.isArray(m.artifacts)) return null;
  const ext = extractResultsFromArtifacts(m.artifacts);
  if (!ext.rows.length && !ext.tool) return null;
  return { rows: ext.rows, total: (ext.total != null) ? ext.total : ext.rows.length, tool: ext.tool };
}

// A synthetic select_properties artifact means the agent re-ranked the panel
// to match its answer (see api/chat/panel.py). Those rows arrive pre-ordered,
// so the panel must preserve that order rather than fall back to date/price.
function _isRankedTool(tool) { return tool === 'select_properties'; }

// Point the panel's sort at the source that just populated it. A ranking turn
// forces the "relevance" (as-answered) order; anything else restores the sort
// the user last picked, so switching away from a ranking doesn't strand them.
function setPanelSource(tool) {
  // Reasons belong to the turn that produced them. A panel repopulated by
  // anything other than a chat turn must not keep showing them.
  if (!tool) setRecommendation(null);
  panelIsRanked = _isRankedTool(tool);
  if (panelIsRanked) {
    currentSort = 'relevance';
  } else if (currentSort === 'relevance') {
    currentSort = userSort;
  }
}

async function askAI(userText, opts = {}) {
  userText = (userText || '').trim();
  if (!userText) return;
  if (opts.fromLanding) {
    chatHistory = [];
    resetAgentState();
    currentResults = [];
    currentTotalCount = null;
    panelSnapshotIndex = null;
    setPanelSource(null);
    activeChatId = null;
    go('results');
  }
  chatHistory.push({ role: 'user', text: userText });
  chatHistory.push({ role: 'ai thinking', text: 'thinking' });
  lastQuery = userText;
  renderChat();

  const startedAt = performance.now();
  const dropTransient = () => {
    chatHistory = chatHistory.filter(m => m.role !== 'ai thinking' && m.role !== 'ai streaming');
  };
  try {
    let resp;
    // Build the body once: _chatRequestBody consumes the one-shot
    // pendingChatScope, so the same object must serve stream AND fallback.
    // (Throws for a gated mode when signed out — so the events below only fire
    // for turns that actually run, never for a blocked attempt.)
    const reqBody = _chatRequestBody(userText);
    if (window.track) {
      const _src = opts.fromLanding ? 'landing' : 'chat';
      const _mode = window.currentMode || 'ask';
      window.track('search', { source: _src, mode: _mode });
      if (_mode === 'deep-research') window.track('deep_research_open', { source: _src });
    }
    try {
      resp = await apiChatStream(reqBody, (ev, data) => {
        const cur = chatHistory[chatHistory.length - 1];
        if (!cur) return;
        if (ev === 'status' && cur.role === 'ai thinking') {
          cur.text = data.label || 'thinking';
          scheduleChatRender();
        } else if (ev === 'delta' && data.text) {
          if (cur.role === 'ai thinking') {
            chatHistory[chatHistory.length - 1] = { role: 'ai streaming', text: data.text };
          } else if (cur.role === 'ai streaming') {
            cur.text += data.text;
          }
          scheduleChatRender();
        }
      });
    } catch (streamErr) {
      if (!streamErr || !streamErr.useFallback) throw streamErr;
      // Stream transport unavailable (old deploy / proxy) — same turn over
      // blocking /chat. The model never started, so no double-billing.
      console.warn('chat stream unavailable, falling back to /chat:', streamErr.message);
      resp = await apiChat(userText, reqBody);
    }
    // Each loop carries its own conversation channel, and exactly one of the
    // three is ever present in a response: the deep loop returns a thread key,
    // v2 a scope summary, v1 the whole transcript.
    if (CHAT_THREADED) apiChatThreadId = resp.thread_id || apiChatThreadId;
    else if (CHAT_V2) apiChatScope = resp.scope || apiChatScope;
    else apiMessageHistory = resp.message_history || apiMessageHistory;
    setRecommendation(resp.recommendation);
    // Publish the turn for the /lab inspector. An event rather than a direct
    // call so app.js stays unaware of whether a lab page is loaded at all.
    if (CHAT_V2) {
      try {
        window.dispatchEvent(new CustomEvent('chatv2:turn', { detail: {
          question: userText, plan: resp.plan, usage: resp.usage,
          gate: normalizeGate(resp.gate), scope: resp.scope, loop: CHAT_LOOP,
          threadId: resp.thread_id || null, steps: resp.steps || 0,
          recommendation: resp.recommendation,
          elapsedMs: performance.now() - startedAt,
        } }));
      } catch (_) { /* the inspector is a diagnostic; never break a turn */ }
    }
    const extracted = extractResultsFromArtifacts(resp.artifacts);
    if (extracted.rows.length || extracted.tool) {
      currentResults = extracted.rows;
      currentTotalCount = extracted.total;
      panelSnapshotIndex = null; // a new turn puts the panel back on "live"
      setPanelSource(extracted.tool);
    }
    dropTransient();
    chatHistory.push({ role: 'ai', text: (resp.answer || '').trim(), artifacts: resp.artifacts || [], elapsedMs: performance.now() - startedAt });
  } catch(e) {
    console.error(e);
    dropTransient();
    chatHistory.push({ role: 'ai', text: `Sorry — I couldn't reach the server. (${e.message})`, elapsedMs: performance.now() - startedAt });
  }
  renderChat();
  renderResultsList();
  // Mint a conversation id on the first turn so subsequent saves PUT to a
  // stable key. Saving is a no-op for anonymous users.
  if (isSignedIn() && !activeChatId) {
    activeChatId = (crypto.randomUUID && crypto.randomUUID()) || ('c-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
    // Reflect the new thread id in the URL (replace, not push — same turn) so
    // a refresh mid-conversation restores this chat instead of a blank one.
    if (currentScreen === 'results') syncURLForScreen('results', true);
  }
  saveActiveConversation();
}

// Coalesce per-delta re-renders into one paint per frame — renderChat
// rebuilds the whole log's innerHTML, so rendering on every SSE chunk
// would thrash layout during fast streams.
let _chatRenderQueued = false;
function scheduleChatRender() {
  if (_chatRenderQueued) return;
  _chatRenderQueued = true;
  requestAnimationFrame(() => { _chatRenderQueued = false; renderChat(); });
}

// Human-friendly response time: sub-second as "420ms", otherwise "3.4s".
function formatDuration(ms) {
  if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function renderChat(history, logEl, opts) {
  history = history || chatHistory;
  logEl   = logEl   || document.getElementById('chat-log');
  opts    = opts    || {};
  if (!logEl) return;
  const inputId  = opts.inputId  || 'results-input';
  // Default callbacks preserve the main-chat behavior: clear pydantic-ai
  // history (can't be cleanly partial-edited) and persist the conversation.
  const onChange = opts.onChange || (() => {
    resetAgentState();
    panelSnapshotIndex = null; saveActiveConversation();
  });
  const onRetry  = opts.onRetry  || ((q) => askAI(q));
  const scope    = opts.scope || null;

  // Which message's matches the panel reflects, for chip highlighting.
  // A null panelSnapshotIndex means "live" — the last panel-touching turn.
  let activeSnapIdx = panelSnapshotIndex;
  if (!scope && activeSnapIdx == null) {
    for (let k = history.length - 1; k >= 0; k--) {
      if (_msgMatches(history[k])) { activeSnapIdx = k; break; }
    }
  }

  logEl.innerHTML = history.map((m, i) => {
    // Streaming statuses ("Searching auctions…") ride in m.text; the CSS
    // ::after dots keep animating whatever the label says.
    if (m.role === 'ai thinking') return `<div class="bubble ai thinking">${escapeHtml(m.text || 'thinking')}</div>`;
    // Partial streamed answer: markdown but no action buttons/sources yet —
    // those attach when the final payload replaces this message.
    if (m.role === 'ai streaming') {
      return `<div class="bubble-wrap ai"><div class="bubble ai md">${renderMarkdown(m.text || '')}</div></div>`;
    }
    if (m.role === 'user') {
      return `<div class="bubble-wrap user">
        <div class="bubble user">${escapeHtml(m.text)}</div>
        <div class="bubble-actions">
          <button class="b-act" data-act="copy" data-i="${i}" title="copy" aria-label="copy">
            <svg viewBox="0 0 16 16" width="12" height="12"><rect x="4" y="4" width="9" height="10" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="2" y="2" width="9" height="10" fill="currentColor" opacity="0.15" stroke="currentColor" stroke-width="1.5"/></svg>
          </button>
          <button class="b-act" data-act="edit" data-i="${i}" title="edit & re-run" aria-label="edit">
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M2 14 L2 11 L11 2 L14 5 L5 14 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><line x1="9" y1="4" x2="12" y2="7" stroke="currentColor" stroke-width="1.5"/></svg>
          </button>
          <button class="b-act" data-act="delete" data-i="${i}" title="delete" aria-label="delete">
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 5 L13 5 M5 5 L5 14 Q5 15 6 15 L10 15 Q11 15 11 14 L11 5 M6 5 L6 3 Q6 2 7 2 L9 2 Q10 2 10 3 L10 5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>
          </button>
        </div>
      </div>`;
    }
    if (m.role === 'ai') {
      // Per-turn matches chip (main chat only) — click to flip the matches
      // panel back to what this answer found.
      const snap = !scope ? _msgMatches(m) : null;
      const snapTotal = snap ? snap.total : 0;
      const matchesHtml = snap ? `
        <button class="matches-chip${i === activeSnapIdx ? ' active' : ''}" data-i="${i}" title="show this answer's matches in the panel" aria-label="show this answer's ${snapTotal} matches in the panel">
          <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><line x1="6.5" y1="2.75" x2="6.5" y2="13.25" stroke="currentColor" stroke-width="1.5"/></svg>
          <span>${snapTotal.toLocaleString('en-IN')} match${snapTotal === 1 ? '' : 'es'}</span>
        </button>` : '';
      const sources = extractWebSources(m.artifacts);
      const sourcesHtml = sources.length ? `
        <div class="sources-row">
          <span class="sources-label">sources</span>
          ${sources.map((s, n) => `
            <a class="source-chip" href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(s.title || s.url)}">
              <img class="source-favicon" src="https://www.google.com/s2/favicons?domain=${encodeURIComponent(s.domain || '')}&sz=32" alt="" loading="lazy" onerror="this.style.display='none'">
              <span class="source-num">[${n + 1}]</span>
              <span class="source-domain">${escapeHtml(s.domain || '')}</span>
            </a>`).join('')}
        </div>` : '';
      const timeHtml = (typeof m.elapsedMs === 'number' && isFinite(m.elapsedMs)) ? `
        <div class="resp-time" title="time taken for this response">
          <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.5 L8 8 L10.5 9.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>${escapeHtml(formatDuration(m.elapsedMs))}</span>
        </div>` : '';
      // Matches chip and response time share one row so they sit side by side
      // under the answer instead of stacking.
      const metaHtml = (matchesHtml || timeHtml) ? `
        <div class="ai-meta">${matchesHtml}${timeHtml}</div>` : '';
      return `<div class="bubble-wrap ai">
        <div class="bubble ai md">${linkifyAnswerHtml(renderMarkdown(m.text), m)}</div>
        ${sourcesHtml}
        ${metaHtml}
        <div class="bubble-actions">
          <button class="b-act" data-act="copy-ai" data-i="${i}" title="copy" aria-label="copy">
            <svg viewBox="0 0 16 16" width="12" height="12"><rect x="4" y="4" width="9" height="10" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="2" y="2" width="9" height="10" fill="currentColor" opacity="0.15" stroke="currentColor" stroke-width="1.5"/></svg>
          </button>
          <button class="b-act" data-act="up" data-i="${i}" title="helpful" aria-label="helpful">
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M3 14 L3 7 L5 7 L8 2 Q9 2 9 4 L9 6 L13 6 Q14.5 6 14 7.5 L12.5 13 Q12 14 11 14 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
          </button>
          <button class="b-act" data-act="down" data-i="${i}" title="not helpful" aria-label="not helpful">
            <svg viewBox="0 0 16 16" width="12" height="12" style="transform:rotate(180deg)"><path d="M3 14 L3 7 L5 7 L8 2 Q9 2 9 4 L9 6 L13 6 Q14.5 6 14 7.5 L12.5 13 Q12 14 11 14 Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
          </button>
          <button class="b-act" data-act="retry" data-i="${i}" title="try again" aria-label="try again">
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M13 8 A5 5 0 1 1 8 3 L11 3 M11 1 L11 5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>`;
    }
    return `<div class="bubble ${m.role}">${escapeHtml(m.text)}</div>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;
  logEl.querySelectorAll('.b-act').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const i = +btn.dataset.i;
      const msg = history[i];
      if (!msg) return;
      const act = btn.dataset.act;
      if (act === 'copy' || act === 'copy-ai') {
        navigator.clipboard?.writeText(msg.text);
        flashBtn(btn);
      } else if (act === 'edit') {
        const inp = document.getElementById(inputId);
        inp.value = msg.text;
        inp.focus();
        inp.setSelectionRange(msg.text.length, msg.text.length);
        history.length = i;
        onChange();
        renderChat(history, logEl, opts);
      } else if (act === 'delete') {
        // Drop this user message and its immediate AI reply (if any) so we
        // don't leave an orphan response.
        const next = history[i + 1];
        const removeCount = (next && (next.role === 'ai' || next.role === 'ai thinking' || next.role === 'ai streaming')) ? 2 : 1;
        history.splice(i, removeCount);
        onChange();
        renderChat(history, logEl, opts);
      } else if (act === 'up') {
        btn.classList.add('active');
        sendMessageFeedback(i, 'up', { history, propertyId: scope && scope.propertyId });
        flashBtn(btn, true);
      } else if (act === 'down') {
        btn.classList.add('active');
        openMessageFeedback(i, { history, propertyId: scope && scope.propertyId });
      } else if (act === 'retry') {
        let j = i - 1;
        while (j >= 0 && history[j].role !== 'user') j--;
        if (j >= 0) {
          const q = history[j].text;
          history.length = j;
          onRetry(q);
        }
      }
    });
  });
  logEl.querySelectorAll('.matches-chip').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      showMatchSnapshot(+btn.dataset.i);
    });
  });
  // Bare auction IDs inside an answer ("… Auction ( 765494 )") open that
  // property's detail page. Works the same in the main chat and the per-
  // property panel, since both render through here.
  logEl.querySelectorAll('.prop-id-link').forEach(el => {
    const open = () => { currentDetailId = el.dataset.propertyId; go('detail'); };
    el.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); open(); });
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  });
}

// Flip the matches panel to what the turn at chatHistory[i] found — lets
// the user click an earlier answer's chip and see that turn's matches.
function showMatchSnapshot(i) {
  const snap = _msgMatches(chatHistory[i]);
  if (!snap) return;
  currentResults = snap.rows;
  currentTotalCount = snap.total;
  panelSnapshotIndex = i;
  setPanelSource(snap.tool);
  // Toggle the chip highlight in place — a full renderChat would yank the
  // log's scroll position to the bottom.
  document.querySelectorAll('#chat-log .matches-chip').forEach(b => {
    b.classList.toggle('active', +b.dataset.i === i);
  });
  renderResultsList();
  // On mobile the panel is a separate tab — bring it on screen so the click
  // visibly does something.
  if (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) {
    setMobileTab('results');
  }
}

function flashBtn(btn) {
  btn.classList.add('flash');
  setTimeout(() => btn.classList.remove('flash'), 800);
}

/* ====== RESULTS ====== */
const SORT_LABEL = {
  relevance:  'ranked by your question',
  date_asc:   'sorted by auction date (oldest first)',
  date_desc:  'sorted by auction date (newest first)',
  price_asc:  'sorted by price (low → high)',
  price_desc: 'sorted by price (high → low)',
};
function sortResults(rows, mode) {
  const copy = rows.slice();
  // "relevance" is the order the backend already ranked the rows in (the
  // agent's answer ordering) — keep it as-is, don't re-sort.
  if (mode === 'relevance') return copy;
  const endIfMissing = (v) => v == null || v === '' || Number.isNaN(v);
  if (mode === 'price_asc' || mode === 'price_desc') {
    const dir = mode === 'price_asc' ? 1 : -1;
    copy.sort((a, b) => {
      const av = Number(a.reserve_price);
      const bv = Number(b.reserve_price);
      const am = endIfMissing(av), bm = endIfMissing(bv);
      if (am && bm) return 0;
      if (am) return 1;
      if (bm) return -1;
      return (av - bv) * dir;
    });
  } else {
    const dir = mode === 'date_desc' ? -1 : 1;
    copy.sort((a, b) => {
      const av = a.auction_start || a.deadline || '';
      const bv = b.auction_start || b.deadline || '';
      const am = endIfMissing(av), bm = endIfMissing(bv);
      if (am && bm) return 0;
      if (am) return 1;
      if (bm) return -1;
      return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
    });
  }
  return copy;
}
function renderResults() {
  renderChat();
  renderResultsList();
}
function renderResultsList() {
  const list = document.getElementById('results-list');
  const countEl = document.getElementById('results-count');
  const subEl = document.getElementById('results-subcount');
  const sortEl = document.getElementById('results-sort');
  if (sortEl) sortEl.style.display = (currentResults && currentResults.length > 1) ? '' : 'none';
  if (!currentResults || currentResults.length === 0) {
    if (chatHistory.length === 0) {
      list.innerHTML = `<div class="results-empty">ask a question to see matching properties here.</div>`;
      countEl.textContent = '—';
      subEl.textContent = 'waiting for query';
    } else {
      list.innerHTML = `<div class="results-empty">no properties matched this query yet.</div>`;
      countEl.textContent = '0 matches';
      subEl.textContent = 'try a broader query';
    }
    _setMtabCount(0);
    return;
  }
  _syncSortOptions();
  const sorted = sortResults(currentResults, currentSort);
  const cards = sorted.map(row => toCard(row)).filter(c => c.id);
  const shown = cards.length;
  const total = (currentTotalCount != null) ? currentTotalCount : shown;
  countEl.textContent = `${total.toLocaleString('en-IN')} match${total === 1 ? '' : 'es'}`;
  const sortLabel = SORT_LABEL[currentSort] || SORT_LABEL.date_asc;
  subEl.textContent = total > shown ? `showing ${shown} · ${sortLabel}` : sortLabel;
  list.innerHTML = cards.map(c => propCardHtml(c, false, '', true)).join('');
  wireCardClicks();
  _setMtabCount(total);
}
// The "Best match" option only makes sense while the panel holds an AI
// ranking, so it's injected/removed on demand rather than living statically in
// the markup. Keeps the dropdown's value in sync with currentSort either way.
function _syncSortOptions() {
  const sel = document.getElementById('results-sort');
  if (!sel) return;
  let opt = sel.querySelector('option[value="relevance"]');
  if (panelIsRanked) {
    if (!opt) {
      opt = document.createElement('option');
      opt.value = 'relevance';
      opt.textContent = 'Best match (your question)';
      sel.insertBefore(opt, sel.firstChild);
    }
  } else if (opt) {
    opt.remove();
  }
  sel.value = currentSort;
}
function _setMtabCount(n) {
  const el = document.getElementById('mtab-count');
  if (el) el.textContent = String(n || 0);
}
(function wireSortControl() {
  const sel = document.getElementById('results-sort');
  if (!sel) return;
  sel.value = currentSort;
  sel.addEventListener('change', () => {
    currentSort = sel.value;
    // Remember an explicit date/price pick so we can restore it after a
    // ranking turn borrows the sort for "relevance".
    if (currentSort !== 'relevance') userSort = currentSort;
    renderResultsList();
  });
})();
// Compact inline metadata icons (lucide-style, currentColor — never emoji).
const _CARD_ICO_PIN = '<svg class="ci" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 14.5s5-4.2 5-8a5 5 0 0 0-10 0c0 3.8 5 8 5 8Z"/><circle cx="8" cy="6.5" r="1.7"/></svg>';
const _CARD_ICO_BANK = '<svg class="ci" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.2 6.2 8 2.6l5.8 3.6M3 6.9v5.4M13 6.9v5.4M6 6.9v5.4M10 6.9v5.4M2.2 12.7h11.6"/></svg>';
const _CARD_ICO_CAL = '<svg class="ci" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="1.6"/><path d="M5 2.2v2.6M11 2.2v2.6M2.5 6.8h11"/></svg>';
const _CARD_ICO_CLOCK = '<svg class="ci" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.4 1.4"/></svg>';

// Reason-first cards. The v2 synthesis call returns a typed recommendation
// object; its picks are keyed by auction_id here so the card template can bind
// a one-line reason and a few badges. Cards render exactly as before when the
// turn produced no recommendation, which is every v1 turn.
let currentPicks = new Map();
function setRecommendation(rec) {
  currentPicks = new Map();
  if (!rec || !Array.isArray(rec.picks)) return;
  rec.picks.forEach(p => { if (p && p.auction_id) currentPicks.set(String(p.auction_id), p); });
}
const _BADGE_TONES = { good: 'badge-good', warn: 'badge-warn', neutral: 'badge-neutral' };
// `withReason` is opt-in and defaults OFF. propCardHtml also renders the
// browse grid and the saved-deadline timeline, and a reason written for "the
// cheapest flats in Chennai" is nonsense on a watchlist card for a different
// city — it reads as if the agent recommended it for the view you are in.
// Only the matches panel, which IS the chat's result set, passes true.
function _pickHtml(id, withReason) {
  if (!withReason) return '';
  const pick = currentPicks.get(String(id));
  if (!pick) return '';
  const badges = Array.isArray(pick.badges) ? pick.badges.slice(0, 3) : [];
  return `
        ${pick.reason ? `<div class="card-reason">${escapeHtml(pick.reason)}</div>` : ''}
        ${badges.length ? `<div class="card-badges">${badges.map(b =>
          `<span class="card-badge ${_BADGE_TONES[b && b.tone] || _BADGE_TONES.neutral}">${escapeHtml((b && b.text) || '')}</span>`
        ).join('')}</div>` : ''}`;
}

function propCardHtml(c, urgent, countdown, withReason) {
  const isSaved = saved.has(c.id);
  const dropBadge = c.drop
    ? `<div class="price-drop" title="Reserve price previously ${escapeHtml(c.drop.previous)}">${c.drop.pct}% drop from ${escapeHtml(c.drop.previous)}</div>`
    : '';
  // Don't repeat the bank when it merely echoes the (entity-derived) title.
  const showBank = c.bank && _normEntity(c.bank) !== _normEntity(c.title);
  return `
    <div class="prop${urgent ? ' urgent' : ''}" data-id="${escapeHtml(c.id)}">
      <div class="thumb">${thumbSvg(c.type)}</div>
      <div class="meta">
        <div class="title" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
        ${c.loc ? `<div class="loc">${_CARD_ICO_PIN}<span>${escapeHtml(c.loc)}</span></div>` : ''}
        <div class="prop-foot">
          ${showBank ? `<span class="card-bank" title="${escapeHtml(c.bank)}">${_CARD_ICO_BANK}<span class="card-bank-name">${escapeHtml(c.bankShort)}</span></span>` : ''}
          ${c.date ? `<span class="card-date">${_CARD_ICO_CAL}<span>${escapeHtml(c.date)}</span></span>` : ''}
          ${c.ended ? '<span class="ended-tag">auction ended</span>' : ''}
        </div>
        <div class="price">${escapeHtml(c.price)}</div>
        ${dropBadge}
        ${_pickHtml(c.id, withReason)}
        ${urgent && countdown ? `<div class="countdown">${_CARD_ICO_CLOCK}<span>auction ${escapeHtml(countdown)}</span></div>` : ''}
      </div>
      <button class="card-save ${isSaved ? 'saved' : ''}" data-save-id="${escapeHtml(c.id)}" title="${isSaved ? 'saved' : 'save to watchlist'}" aria-label="save">
        <svg viewBox="0 0 18 18" width="18" height="18"><path d="M9 2 L11.3 6.6 L16.5 7.3 L12.7 11 L13.7 16.2 L9 13.7 L4.3 16.2 L5.3 11 L1.5 7.3 L6.7 6.6 Z" fill="${isSaved ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
      </button>
    </div>`;
}
function wireCardClicks() {
  document.querySelectorAll('.prop[data-id]:not([data-wired])').forEach(el => {
    el.dataset.wired = '1';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.card-save')) return;
      currentDetailId = el.dataset.id;
      go('detail');
    });
  });
  document.querySelectorAll('.card-save:not([data-wired])').forEach(btn => {
    btn.dataset.wired = '1';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = btn.dataset.saveId;
      toggleSaved(id);
    });
  });
}
function toggleSaved(id) {
  const willSave = !saved.has(id);
  if (willSave) {
    saved.add(id);
    const row = currentResults.find(r => r.auction_id === id);
    if (row) watchlistCache[id] = toCard(row);
    if (window.track) window.track('watchlist_add', { source: currentScreen });
  } else {
    saved.delete(id);
  }
  persistWatchlist();
  updateSavedCount();
  loadAlerts();
  if (currentScreen === 'results') renderResultsList();
  else if (currentScreen === 'watchlist') renderWatchlist();
  else if (currentScreen === 'detail') updateDetailSaveButton();

  const user = window.Auth && window.Auth.getUser && window.Auth.getUser();
  if (!user) return;
  const method = willSave ? 'POST' : 'DELETE';
  authFetch(`${API_BASE}/watchlist/${encodeURIComponent(id)}`, { method })
    .then(r => { if (!r.ok) throw new Error(`${method} ${r.status}`); })
    .catch(e => {
      console.error('[watchlist] sync failed', e);
      if (willSave) saved.delete(id); else saved.add(id);
      updateSavedCount();
      if (currentScreen === 'results') renderResultsList();
      else if (currentScreen === 'watchlist') renderWatchlist();
      else if (currentScreen === 'detail') updateDetailSaveButton();
    });
}

/* ====== DETAIL ====== */
async function loadAndRenderDetail(id) {
  document.getElementById('detail-title').textContent = 'loading…';
  document.getElementById('detail-sub').textContent = '';
  document.getElementById('detail-photo').innerHTML = '';
  document.getElementById('detail-facts').innerHTML = '';
  document.getElementById('detail-history-wrap').style.display = 'none';
  document.getElementById('detail-history').innerHTML = '';
  document.getElementById('detail-desc-wrap').style.display = 'none';
  document.getElementById('detail-chat-log').innerHTML = '';
  loadDetailChat(id);
  try {
    const detail = await apiAuctionDetail(id);
    renderDetail(detail);
  } catch(e) {
    document.getElementById('detail-title').textContent = 'Not found';
    document.getElementById('detail-sub').textContent = e.message;
  }
}
function renderPriceHistory(history) {
  const wrap = document.getElementById('detail-history-wrap');
  const host = document.getElementById('detail-history');
  if (!Array.isArray(history) || history.length < 2) {
    wrap.style.display = 'none';
    host.innerHTML = '';
    return;
  }
  const rows = history.map((h, i) => {
    const price = h.reserve_price_num != null ? formatINR(h.reserve_price_num) : '—';
    const when = formatAuctionDate(h.auction_start_dt);
    const prev = i > 0 ? history[i - 1] : null;
    let deltaHtml = '';
    if (prev && prev.reserve_price_num && h.reserve_price_num) {
      const diff = prev.reserve_price_num - h.reserve_price_num;
      if (diff > 0) {
        const pct = Math.round((diff / prev.reserve_price_num) * 100);
        deltaHtml = `<span class="history-delta down">▼ ${pct}%</span>`;
      } else if (diff < 0) {
        const pct = Math.round((-diff / prev.reserve_price_num) * 100);
        deltaHtml = `<span class="history-delta up">▲ ${pct}%</span>`;
      }
    }
    const isCurrent = h.is_current ? ' current' : '';
    const label = h.is_current ? 'this auction' : 'previous round';
    const linkAttrs = h.is_current ? '' : `data-jump-id="${escapeHtml(h.auction_id)}" role="link" tabindex="0"`;
    return `
      <div class="history-row${isCurrent}" ${linkAttrs}>
        <div class="history-when">${escapeHtml(when)}</div>
        <div class="history-price">${escapeHtml(price)} ${deltaHtml}</div>
        <div class="history-tag">${escapeHtml(label)}</div>
      </div>`;
  }).join('');
  host.innerHTML = rows;
  wrap.style.display = '';
  host.querySelectorAll('[data-jump-id]').forEach(el => {
    const jump = () => {
      currentDetailId = el.dataset.jumpId;
      loadAndRenderDetail(currentDetailId);
      syncURLForScreen('detail', false);
    };
    el.addEventListener('click', jump);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
    });
  });
}

// Minimal inline lucide icons (stroke = currentColor), inlined rather than a
// sprite to match the app's existing inline-SVG style. Used by the detail
// page's "Briefing" data panels.
const _LUCIDE = {
  wallet:   '<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>',
  clock:    '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  gavel:    '<path d="m14.5 12.5-8 8a2.1 2.1 0 1 1-3-3l8-8"/><path d="m16 16 6-6"/><path d="m8 8 6-6"/><path d="m9 7 8 8"/><path d="m21 11-8-8"/>',
  landmark: '<line x1="3" x2="21" y1="22" y2="22"/><line x1="6" x2="6" y1="18" y2="11"/><line x1="10" x2="10" y1="18" y2="11"/><line x1="14" x2="14" y1="18" y2="11"/><line x1="18" x2="18" y1="18" y2="11"/><polygon points="12 2 20 7 4 7"/>',
  building: '<path d="M3 21h18"/><path d="M5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16"/><path d="M9 7h6M9 11h6M9 15h6"/>',
  user:     '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  pin:      '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
  phone:    '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92Z"/>',
  tag:      '<path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.41l8.7 8.7a2.43 2.43 0 0 0 3.42 0l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".75"/>',
};
function licon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${_LUCIDE[name] || ''}</svg>`;
}

function renderDetail(detail) {
  const f = detail.fields || {};
  const r = detail.relationships || {};
  const title = f.title || 'Untitled';
  const city = r.city?.name || '';
  const area = r.area?.name || '';
  const bank = r.bank?.name || '';
  const types = r.property_types || [];
  const assetCategory = r.asset_category?.name || '';
  const loc = joinLoc(area, city);
  currentDetailTitle = title;
  currentDetailLoc = loc;
  document.getElementById('detail-title').textContent =
    deriveDisplayTitle({ title, bank }, inferType({ asset_category: assetCategory, property_types: types, title }));
  const subBits = [loc, assetCategory, types.length ? types.join(' / ') : ''].filter(Boolean);
  document.getElementById('detail-sub').textContent = subBits.join(' · ') || '—';

  const typeIcon = inferType({ asset_category: assetCategory, property_types: types, title });
  document.getElementById('detail-photo').innerHTML = thumbSvg(typeIcon);

  const auctionStart = f.auction_start_dt;
  const auctionEnd = f.auction_end_dt;
  const deadline = f.application_deadline_dt;
  const _startD = parseDate(auctionStart);
  const auctionEnded = !!(_startD && _startD < new Date());
  const state = r.state?.name || '';
  const branch = r.branch?.name || '';
  const borrower = r.borrower?.name || '';
  const auctionType = r.auction_type?.name || '';
  const fullLoc = [area, city, state].filter(Boolean).join(' · ');
  // Drop a redundant "Contact No:" prefix so the value reads as just the number.
  const contact = String(f.contact_details || '')
    .replace(/^\s*contact\s*(?:no\.?|nos\.?|details?|number)?\s*[:\-.]*\s*/i, '')
    .trim();
  // ── "Briefing" presentation: Pricing / Timeline / Property & parties ──
  // Grouped by the questions a bidder asks (how much, by when, what/who) so
  // the money leads and related facts read together instead of as a flat grid.
  const now = new Date();
  const reservePrice = formatINR(f.reserve_price_num);
  const emd = formatINR(f.emd_num);

  // Derived insight: EMD as a clean % of reserve (SARFAESI EMD is often 10%).
  let emdNote = '';
  const _rp = Number(f.reserve_price_num), _em = Number(f.emd_num);
  if (_rp > 0 && _em > 0 && _em < _rp) {
    const pct = (_em / _rp) * 100, rounded = Math.round(pct);
    if (rounded >= 1 && rounded <= 50 && Math.abs(pct - rounded) <= 0.4) {
      emdNote = `EMD is ${rounded}% of the reserve price`;
    }
  }

  // Timeline — only steps with a real date; "done" once the date is past.
  const _endPast = !!(parseDate(auctionEnd) && parseDate(auctionEnd) < now);
  const stepDefs = [
    { lbl: 'Apply by', v: deadline },
    { lbl: 'Auction starts', v: auctionStart },
    { lbl: (auctionEnded || _endPast) ? 'Auction ended' : 'Auction ends', v: auctionEnd },
  ].filter(s => s.v && formatAuctionDate(s.v) !== '—');
  const stepsHtml = stepDefs.map((s, i) => {
    const d = parseDate(s.v), done = !!(d && d < now), last = i === stepDefs.length - 1;
    return `
      <div class="pb-step${done ? ' done' : ''}">
        <div class="gut"><span class="node"></span>${last ? '' : '<span class="line"></span>'}</div>
        <div class="body"><div class="lbl">${escapeHtml(s.lbl)}</div><div class="val">${escapeHtml(formatAuctionDate(s.v))}</div></div>
      </div>`;
  }).join('');

  // Property & parties — icon + label + value, only for fields we actually have.
  const typeVal = [assetCategory, types.join(', ')].filter(Boolean).join(' · ');
  const metaHtml = [
    { ic: 'landmark', lbl: 'Bank', val: bank },
    { ic: 'building', lbl: 'Branch', val: branch },
    { ic: 'user', lbl: 'Borrower', val: borrower },
    { ic: 'tag', lbl: 'Type', val: typeVal },
    { ic: 'gavel', lbl: 'Auction type', val: auctionType },
    { ic: 'pin', lbl: 'Location', val: fullLoc },
    { ic: 'phone', lbl: 'Contact', val: contact, tel: true },
  ].filter(m => m.val && m.val !== '—').map(m => {
    const valHtml = m.tel
      ? `<a href="tel:${escapeHtml(String(m.val).replace(/[^0-9+]/g, ''))}">${escapeHtml(m.val)}</a>`
      : escapeHtml(m.val);
    return `<div class="pb-item">${licon(m.ic)}<div><div class="lbl">${escapeHtml(m.lbl)}</div><div class="val">${valHtml}</div></div></div>`;
  }).join('');

  const pricingHtml = `
    <section class="pb-panel pb-pricing">
      <div class="pb-eyebrow">${licon('wallet')}Pricing</div>
      <div class="pb-price">${escapeHtml(reservePrice)}</div>
      ${emd !== '—' ? `<div class="pb-price-sub"><span class="amt">${escapeHtml(emd)}</span><span class="lbl">EMD</span></div>` : ''}
      ${emdNote ? `<div class="pb-note">${escapeHtml(emdNote)}</div>` : ''}
    </section>`;
  const timelineHtml = stepsHtml ? `
    <section class="pb-panel">
      <div class="pb-eyebrow">${licon('clock')}Timeline</div>
      <div class="pb-steps">${stepsHtml}</div>
    </section>` : '';
  // Pricing spans full width when there's no timeline to sit beside it.
  const topHtml = timelineHtml ? `<div class="pb-row">${pricingHtml}${timelineHtml}</div>` : pricingHtml;

  document.getElementById('detail-facts').innerHTML = `
    <div class="pb">
      ${topHtml}
      ${metaHtml ? `<section class="pb-panel"><div class="pb-eyebrow">${licon('tag')}Property &amp; parties</div><div class="pb-kv">${metaHtml}</div></section>` : ''}
    </div>`;

  renderPriceHistory(detail.price_history);

  const desc = f.description;
  if (desc && String(desc).trim()) {
    const cleaned = String(desc).trim()
      .replace(/^#{1,6}\s*description of propert(?:y\/ies|ies|y)\s*:?\s*/i, '')
      // Drop source-portal identifiers we don't surface (Asset / Security ID).
      .replace(/[,;.]?\s*\b(?:Asset|Security)\s*ID\s*[-:]?\s*[A-Za-z0-9/]+/gi, '')
      .replace(/\s{2,}/g, ' ')
      .trim();
    const descEl = document.getElementById('detail-desc');
    const descToggle = document.getElementById('detail-desc-toggle');
    const formattedHtml = renderMarkdown(formatDescription(cleaned));
    const originalHtml = `<div class="detail-desc-raw">${escapeHtml(cleaned)}</div>`;
    descEl.dataset.formatted = formattedHtml;
    descEl.dataset.original = originalHtml;
    descEl.classList.add('formatted');
    descEl.innerHTML = formattedHtml;
    descToggle.dataset.mode = 'formatted';
    descToggle.textContent = 'View original';
    descToggle.setAttribute('aria-pressed', 'false');
    document.getElementById('detail-desc-wrap').style.display = '';
  }

  // also cache for watchlist
  watchlistCache[detail.auction_id] = {
    id: detail.auction_id,
    type: typeIcon,
    title,
    loc,
    bank: bank || null,
    price: formatINR(f.reserve_price_num),
    emd: formatINR(f.emd_num),
    date: formatAuctionDate(auctionStart),
    dateRaw: auctionStart,
    url: f.url || null,
  };

  updateDetailSaveButton();
}
function updateDetailSaveButton() {
  const sb = document.getElementById('detail-save');
  if (!currentDetailId) return;
  const isSaved = saved.has(currentDetailId);
  sb.textContent = isSaved ? '★ saved to watchlist' : '☆ save to watchlist';
  sb.classList.toggle('saved', isSaved);
  sb.onclick = () => toggleSaved(currentDetailId);
  const db = document.getElementById('detail-dossier');
  if (db) db.onclick = () => {
    if (window.Dossiers && window.Dossiers.openForAuction) {
      window.Dossiers.openForAuction(currentDetailId, currentDetailTitle);
    }
  };
}

function renderDetailChat() {
  renderChat(
    detailChatHistory,
    document.getElementById('detail-chat-log'),
    {
      inputId: 'detail-input',
      onChange: () => saveDetailChat(),
      onRetry:  (q) => askAboutProperty(q),
      scope:    { propertyId: currentDetailId },
    },
  );
}

function _mintChatId() {
  return (crypto.randomUUID && crypto.randomUUID()) || ('c-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
}

function _propertyChatTitle() {
  const firstUser = detailChatHistory.find(m => m.role === 'user');
  const msg = (firstUser ? firstUser.text : '').trim().slice(0, 80);
  const propPart = (currentDetailTitle || '').trim().slice(0, 60);
  if (!propPart && !msg) return 'property chat';
  if (!propPart) return msg;
  if (!msg) return propPart;
  return `${propPart} — ${msg}`;
}

function _relTime(iso) {
  const t = Date.parse(iso);
  if (!isFinite(t)) return '';
  const diff = Date.now() - t;
  if (diff < 60e3) return 'just now';
  if (diff < 3600e3) return `${Math.floor(diff/60e3)}m ago`;
  if (diff < 86400e3) return `${Math.floor(diff/3600e3)}h ago`;
  if (diff < 7*86400e3) return `${Math.floor(diff/86400e3)}d ago`;
  return new Date(t).toLocaleDateString();
}

async function loadDetailChat(propertyId) {
  detailChatHistory = [];
  detailApiMessageHistory = null;
  propertyChatList = [];
  // Honor a one-shot pending chat id from the sidebar; otherwise pick the most
  // recent chat for this property.
  const explicitChatId = _pendingPropertyChatId;
  _pendingPropertyChatId = null;
  // Anonymous visitors have no saved chats; skip the calls instead of firing
  // a guaranteed 401 (console noise on every detail view while signed out).
  // Gate on the persisted Supabase session — the same source authFetch reads —
  // NOT on Auth.getUser(), which hydrates async and is still null during a
  // cold deep-link load for signed-in users.
  let _hasSession = false;
  try {
    const _sb = window.Auth && window.Auth._supabase;
    if (_sb) {
      const _res = await _sb.auth.getSession();
      _hasSession = !!(_res && _res.data && _res.data.session);
    }
  } catch (_) { /* treat as anon */ }
  if (!_hasSession) {
    currentPropertyChatId = _mintChatId();
    renderDetailChat();
    renderPropertyChatHistory();
    return;
  }
  try {
    const r = await authFetch(`${API_BASE}/conversations?property_id=${encodeURIComponent(propertyId)}`);
    if (r && r.ok) {
      const j = await r.json();
      propertyChatList = Array.isArray(j.conversations) ? j.conversations : [];
    }
  } catch(_) { /* anon / 401 — stay empty */ }
  let targetId = explicitChatId || (propertyChatList[0] && propertyChatList[0].id);
  if (targetId) {
    try {
      const r2 = await authFetch(`${API_BASE}/conversations/${encodeURIComponent(targetId)}`);
      if (r2 && r2.ok) {
        const data = await r2.json();
        detailChatHistory = Array.isArray(data.messages) ? data.messages : [];
        detailApiMessageHistory = data.api_history || null;
        currentPropertyChatId = data.id;
      } else {
        currentPropertyChatId = _mintChatId();
      }
    } catch(_) { currentPropertyChatId = _mintChatId(); }
  } else {
    currentPropertyChatId = _mintChatId();
  }
  renderDetailChat();
  renderPropertyChatHistory();
}

async function saveDetailChat() {
  if (!currentDetailId || !currentPropertyChatId) return;
  // Skip empty drafts so the panel + sidebar history don't fill with blanks.
  const messages = detailChatHistory.filter(m => m.role !== 'ai thinking');
  if (!messages.length) return;
  const pid = currentDetailId;
  const cid = currentPropertyChatId;
  const payload = {
    title: _propertyChatTitle(),
    messages,
    api_history: detailApiMessageHistory,
    results: [],
    total_count: null,
    property_id: pid,
  };
  try {
    const r = await authFetch(`${API_BASE}/conversations/${encodeURIComponent(cid)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r || !r.ok) return;
    // Mirror the new state into both lists so the panel history + main sidebar
    // re-render without a round-trip. updated_at is approximate (server clock
    // wins on next sync) but good enough for ordering.
    const nowIso = new Date().toISOString();
    const pIdx = propertyChatList.findIndex(c => c.id === cid);
    const pEntry = { id: cid, title: payload.title, property_id: pid, updated_at: nowIso };
    if (pIdx >= 0) propertyChatList.splice(pIdx, 1);
    propertyChatList.unshift(pEntry);
    renderPropertyChatHistory();
    const sIdx = recentChats.findIndex(c => c.id === cid);
    const sEntry = { id: cid, title: payload.title, ts: Date.now(), property_id: pid };
    if (sIdx >= 0) recentChats.splice(sIdx, 1);
    recentChats.unshift(sEntry);
    renderSidebar();
  } catch(e) { console.warn('[property-chat] save failed', e); }
}

function clearDetailChat() {
  if (!currentDetailId) return;
  // Mint a fresh chat id; the previous chat stays on the server because
  // saveDetailChat already persisted each turn.
  currentPropertyChatId = _mintChatId();
  detailChatHistory = [];
  detailApiMessageHistory = null;
  renderDetailChat();
  renderPropertyChatHistory();
}

async function loadPropertyChat(chatId) {
  if (!chatId || !isSignedIn()) return;
  try {
    const r = await authFetch(`${API_BASE}/conversations/${encodeURIComponent(chatId)}`);
    if (!r || !r.ok) return;
    const data = await r.json();
    currentPropertyChatId = data.id;
    detailChatHistory = Array.isArray(data.messages) ? data.messages : [];
    detailApiMessageHistory = data.api_history || null;
    renderDetailChat();
    renderPropertyChatHistory();
  } catch(e) { console.warn('[property-chat] load failed', e); }
}

function renderPropertyChatHistory() {
  const host = document.getElementById('detail-chat-history');
  if (!host) return;
  if (!isSignedIn() || !propertyChatList.length) {
    host.style.display = 'none';
    host.innerHTML = '';
    return;
  }
  const items = propertyChatList.slice(0, 20);
  const itemRows = items.map(c => {
    const active = c.id === currentPropertyChatId ? ' active' : '';
    const ts = c.updated_at ? _relTime(c.updated_at) : '';
    const title = c.title || 'untitled';
    return `<div class="pch-item${active}" data-chat-id="${escapeHtml(c.id)}" title="${escapeHtml(title)}">
      <span class="pch-title">${escapeHtml(title)}</span>
      <span class="pch-ts">${escapeHtml(ts)}</span>
    </div>`;
  }).join('');
  host.style.display = '';
  host.innerHTML = `<div class="lbl" style="margin-bottom:6px;">past chats about this property</div>${itemRows}`;
  host.querySelectorAll('.pch-item').forEach(el => {
    el.addEventListener('click', () => loadPropertyChat(el.dataset.chatId));
  });
}

function _removeDetailThinking() {
  for (let k = detailChatHistory.length - 1; k >= 0; k--) {
    if (detailChatHistory[k].role === 'ai thinking') detailChatHistory.splice(k, 1);
  }
}

async function askAboutProperty(text) {
  text = (text || '').trim(); if (!text) return;
  const auctionId = currentDetailId;
  if (!auctionId) return;
  // Per-property "ask about this property" agent turn — the funnel's
  // web-enriched-question step (plan §13). No message text sent (no PII).
  if (window.track) window.track('chat_send', { surface: 'property' });
  detailChatHistory.push({ role: 'user', text });
  detailChatHistory.push({ role: 'ai thinking', text: '' });
  renderDetailChat();
  // Snapshot the api history we send with this turn — if the user switches
  // property mid-flight, the response is for the snapshot, not the new state.
  const apiHistorySnapshot = detailApiMessageHistory;
  const preamble = `Regarding auction ${auctionId} (${currentDetailTitle} in ${currentDetailLoc}): ${text}`;
  let aiEntry;
  let nextApiHistory = apiHistorySnapshot;
  const startedAt = performance.now();
  try {
    // The property-detail chat stays on v1 in Phase 1: it threads
    // `message_history`, which v2 neither accepts nor returns. It moves over
    // with the rest of the v2 work, not as a side effect of this flag.
    const res = await authFetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: preamble, message_history: apiHistorySnapshot }),
    });
    const resp = await res.json();
    nextApiHistory = resp.message_history || apiHistorySnapshot;
    const answer = (resp.answer || '').trim();
    const elapsedMs = performance.now() - startedAt;
    aiEntry = answer
      ? { role: 'ai', text: answer, artifacts: resp.artifacts || [], elapsedMs }
      : { role: 'ai', text: "_The agent didn't return an answer. Try rephrasing — e.g. ask about the EMD, schedule, reserve price, or borrower._", artifacts: resp.artifacts || [], elapsedMs };
  } catch(e) {
    aiEntry = { role: 'ai', text: `Server unavailable: ${e.message}`, elapsedMs: performance.now() - startedAt };
  }
  // If the user navigated to a different property while /chat was in flight,
  // the response no longer applies to what's on screen — drop it silently
  // rather than corrupt the new property's chat.
  if (currentDetailId !== auctionId) return;
  detailApiMessageHistory = nextApiHistory;
  _removeDetailThinking();
  detailChatHistory.push(aiEntry);
  renderDetailChat();
  saveDetailChat();
}

/* ====== WATCHLIST ====== */
/* ====== DEADLINE ALERTS ====== */
// In-app alerts for saved properties whose auction deadline is within 7 days.
// Computed on-read by GET /alerts (signed in) or POST /alerts with the
// localStorage watchlist ids (anonymous). No price/status alerts — deadline
// timing only. See docs/design/2026-06-13-property-deadline-alerts.md.
let alertsCache = [];
const _ALERT_TONES = { urgent: 'urgent', soon: 'soon', upcoming: 'upcoming' };

async function loadAlerts() {
  try {
    const user = window.Auth && window.Auth.getUser && window.Auth.getUser();
    let res;
    if (user) {
      res = await authFetch(`${API_BASE}/alerts`);
    } else {
      const ids = [...saved];
      if (!ids.length) { alertsCache = []; updateAlertsBadge(); renderWatchlistAlerts(); return; }
      res = await authFetch(`${API_BASE}/alerts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auction_ids: ids }),
      });
    }
    if (!res || !res.ok) return;
    const data = await res.json();
    alertsCache = Array.isArray(data.alerts) ? data.alerts : [];
  } catch (e) {
    console.error('[alerts] load failed', e);
    return;
  }
  updateAlertsBadge();
  if (currentScreen === 'watchlist') renderWatchlistAlerts();
}

function updateAlertsBadge() {
  const badge = document.getElementById('alerts-count');
  if (!badge) return;
  const n = alertsCache.length;
  badge.textContent = n > 0 ? String(n) : '';
  badge.dataset.empty = n > 0 ? '0' : '1';
}

function openAlertDetail(id) {
  currentDetailId = id;
  go('detail');
}

function renderWatchlistAlerts() {
  const host = document.getElementById('watchlist-alerts');
  if (!host) return;
  if (!alertsCache.length) { host.innerHTML = ''; return; }
  const rows = alertsCache.map(a => {
    const tone = _ALERT_TONES[a.severity] || 'upcoming';
    const id = escapeHtml(a.auction_id);
    const title = escapeHtml(a.title || a.auction_id);
    const city = a.city ? ` · ${escapeHtml(a.city)}` : '';
    const msg = escapeHtml(a.message || '');
    return `<button class="wl-alert" data-tone="${tone}" onclick="openAlertDetail('${id}')">
      <span class="wl-alert-dot"></span>
      <span class="wl-alert-body">
        <span class="wl-alert-title">${title}${city}</span>
        <span class="wl-alert-msg">${msg}</span>
      </span>
    </button>`;
  }).join('');
  const n = alertsCache.length;
  host.innerHTML = `<div class="wl-alerts-head">⏰ ${n} deadline${n === 1 ? '' : 's'} coming up</div>${rows}`;
}

function updateSavedCount() {
  const countEl = document.getElementById('save-count');
  if (countEl) countEl.textContent = saved.size;
  const btBadge = document.getElementById('bt-save-count');
  if (btBadge) {
    btBadge.textContent = saved.size > 0 ? String(saved.size) : '';
    btBadge.dataset.empty = saved.size > 0 ? '0' : '1';
  }
}
function renderWatchlist() {
  renderWatchlistAlerts();
  const body = document.getElementById('watchlist-body');
  const ids = [...saved];
  document.getElementById('watchlist-count').textContent = `${ids.length} saved${ids.length ? ' · sorted by auction date' : ''}`;
  if (ids.length === 0) {
    body.innerHTML = `<div style="text-align:center; padding:60px 20px;">
      <div class="hand" style="font-size:26px; color:var(--muted);">nothing saved yet.</div>
      <div class="lbl" style="margin-top:10px;">save properties with ☆ to see them here</div>
      <button class="btn" onclick="go('landing')" style="margin: 24px auto 0; max-width: 320px;">browse properties</button>
    </div>`;
    return;
  }
  // Ensure we have card data for each saved id — fetch any missing in parallel.
  const missing = ids.filter(id => !watchlistCache[id]);
  if (missing.length) {
    body.innerHTML = `<div style="text-align:center; padding:40px 20px;" class="hand">loading…</div>`;
    Promise.all(missing.map(id => apiAuctionDetail(id).then(d => {
      const types = d.relationships?.property_types || [];
      const assetCategory = d.relationships?.asset_category?.name || '';
      const typeIcon = inferType({ asset_category: assetCategory, property_types: types, title: d.fields?.title });
      watchlistCache[id] = {
        id,
        type: typeIcon,
        title: d.fields?.title || 'Untitled',
        loc: joinLoc(d.relationships?.area?.name, d.relationships?.city?.name),
        bank: d.relationships?.bank?.name || null,
        price: formatINR(d.fields?.reserve_price_num),
        emd: formatINR(d.fields?.emd_num),
        date: formatAuctionDate(d.fields?.auction_start_dt),
        dateRaw: d.fields?.auction_start_dt,
        url: d.fields?.url || null,
      };
    }).catch(e => {
      watchlistCache[id] = { id, type: 'flat', title: `Unavailable (${id})`, loc: '—', bank: null, price: '—', emd: '—', date: '—', dateRaw: null, url: null };
    }))).then(() => renderWatchlist());
    return;
  }

  const items = ids.map(id => watchlistCache[id]).filter(Boolean);
  items.sort((a, b) => {
    const da = parseDate(a.dateRaw)?.getTime() ?? Infinity;
    const db = parseDate(b.dateRaw)?.getTime() ?? Infinity;
    return da - db;
  });
  const now = Date.now();
  const weekCutoff = now + 7 * 86400e3;
  const weekItems = [];
  const laterItems = [];
  items.forEach(c => {
    const t = parseDate(c.dateRaw)?.getTime();
    if (t != null && t <= weekCutoff && t >= now) weekItems.push(c);
    else laterItems.push(c);
  });

  let html = '';
  if (weekItems.length) {
    html += `<div class="timeline-group">
      <div class="timeline-label urgent"><span class="dot"></span>this week<span class="rule"></span></div>
      ${weekItems.map(c => {
        const days = daysUntil(c.dateRaw);
        const cd = days == null ? '' : (days <= 0 ? 'today' : `in ${days} day${days === 1 ? '' : 's'}`);
        return propCardHtml(c, true, cd);
      }).join('')}
    </div>`;
  }
  if (laterItems.length) {
    html += `<div class="timeline-group">
      <div class="timeline-label"><span class="dot"></span>later<span class="rule"></span></div>
      ${laterItems.map(c => propCardHtml(c, false)).join('')}
    </div>`;
  }
  body.innerHTML = html;
  wireCardClicks();
}

/* ====== CHATS SIDEBAR ====== */
// Persisted per-user via /conversations (api/conversations). Anonymous
// users see only the in-memory thread they're currently typing — no
// sidebar history, no save attempts, no 401s.
let recentChats = [];
let activeChatId = null;
let sbFilter = '';
// A conversation id parsed from /chat/{id} on refresh, held until auth resolves
// so loadConversation (which needs a signed-in user) can reopen the thread.
let _restoreChatId = null;

function isSignedIn() {
  return !!(window.Auth && window.Auth.getUser && window.Auth.getUser());
}

// Reopen the thread named in the URL once we're signed in. No-ops (keeping
// _restoreChatId pending) until auth is ready; the auth-change hook retries.
// Drops the pending restore if the user has navigated away from the chat
// screen so a late auth event can't yank them back.
function maybeRestoreActiveChat() {
  if (!_restoreChatId || !isSignedIn()) return;
  if (currentScreen !== 'results') { _restoreChatId = null; return; }
  if (_restoreChatId === activeChatId) { _restoreChatId = null; return; }
  const id = _restoreChatId;
  _restoreChatId = null;
  loadConversation(id);
}

function _conversationTitle() {
  const firstUser = chatHistory.find(m => m.role === 'user');
  const t = (firstUser ? firstUser.text : '').trim().slice(0, 80);
  return t || 'untitled';
}

async function syncConversationsFromServer() {
  if (!isSignedIn()) return;
  try {
    const r = await authFetch(`${API_BASE}/conversations`);
    if (!r.ok) return;
    const data = await r.json();
    const list = Array.isArray(data.conversations) ? data.conversations : [];
    recentChats = list.map(c => ({
      id: c.id,
      title: c.title || 'untitled',
      ts: c.updated_at ? (Date.parse(c.updated_at) || Date.now()) : Date.now(),
      property_id: c.property_id || null,
    }));
    renderSidebar();
  } catch (e) { console.error('[chats] sync failed', e); }
}

async function loadConversation(id) {
  if (!isSignedIn()) return;
  try {
    const r = await authFetch(`${API_BASE}/conversations/${encodeURIComponent(id)}`);
    if (!r.ok) return;
    const data = await r.json();
    activeChatId = data.id;
    chatHistory = Array.isArray(data.messages) ? data.messages : [];
    apiMessageHistory = data.api_history || null;
    // v2 has no transcript, so this IS the restored agent state: without it a
    // reopened conversation answers the next follow-up with nothing carried.
    apiChatScope = data.agent_scope || null;
    // The deep loop needs nothing persisted here: its thread key IS the
    // conversation id, and the transcript is already in the graph under it.
    // Reopening a saved chat therefore resumes the real conversation rather
    // than a summary of it — the one thing neither other loop can do.
    apiChatThreadId = data.id || null;
    currentResults = Array.isArray(data.results) ? data.results : [];
    currentTotalCount = (typeof data.total_count === 'number') ? data.total_count : null;
    panelSnapshotIndex = null; // saved conversations restore the live set
    // Re-derive whether the restored panel is an AI ranking so "Best match"
    // order survives a reload. Match the panel to the message that actually
    // produced it (by auction_id order), not just the newest matches-bearing
    // turn — a restored panel can be an older snapshot, and adopting a later
    // ranking turn's tool would mislabel a plain search as "Best match" and
    // freeze the wrong order. Only meaningful when there are rows to order.
    const _panelKey = (rows) => (rows || [])
      .map(r => String((r && (r.auction_id != null ? r.auction_id : r.id)) || '')).join(',');
    let liveTool = null;
    if (currentResults.length) {
      const panelIds = _panelKey(currentResults);
      for (let k = chatHistory.length - 1; k >= 0; k--) {
        const snap = _msgMatches(chatHistory[k]);
        if (snap && _panelKey(snap.rows) === panelIds) { liveTool = snap.tool; break; }
      }
    }
    setPanelSource(liveTool);
    // go('results') syncs the URL to /chat/{id} via pathForScreen; when already
    // on the chat screen go() is skipped, so push the deep link explicitly.
    if (currentScreen !== 'results') go('results');
    else syncURLForScreen('results', false);
    renderSidebar();
    renderChat();
    renderResultsList();
  } catch (e) { console.error('[chats] load failed', e); }
}

async function saveActiveConversation() {
  if (!isSignedIn() || !activeChatId) return;
  // Snapshot id + payload now — caller's state may change before fetch resolves.
  const id = activeChatId;
  const payload = {
    title: _conversationTitle(),
    messages: chatHistory.filter(m => m.role !== 'ai thinking'),
    api_history: apiMessageHistory,
    agent_scope: apiChatScope,
    results: currentResults || [],
    total_count: currentTotalCount,
  };
  try {
    const r = await authFetch(`${API_BASE}/conversations/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) return;
    const idx = recentChats.findIndex(c => c.id === id);
    const entry = { id, title: payload.title, ts: Date.now(), total_count: currentTotalCount };
    if (idx >= 0) recentChats.splice(idx, 1);
    recentChats.unshift(entry);
    renderSidebar();
  } catch (e) { console.error('[chats] save failed', e); }
}

async function deleteConversation(id) {
  if (!isSignedIn()) return;
  if (!confirm('delete this chat?')) return;
  try {
    await authFetch(`${API_BASE}/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (e) { console.error('[chats] delete failed', e); }
  recentChats = recentChats.filter(c => c.id !== id);
  if (activeChatId === id) {
    chatHistory = [];
    resetAgentState();
    currentResults = [];
    currentTotalCount = null;
    panelSnapshotIndex = null;
    setPanelSource(null);
    activeChatId = null;
    renderChat();
    renderResultsList();
  }
  renderSidebar();
}

function newThread() {
  chatHistory = [];
  resetAgentState();
  currentResults = [];
  currentTotalCount = null;
  panelSnapshotIndex = null;
  setPanelSource(null);
  activeChatId = null;
  renderSidebar();
  renderChat();
  renderResultsList();
  go('landing');
}

function relTime(ts) {
  if (!ts) return '';
  const d = Math.max(0, Date.now() - ts);
  if (d < 60e3) return 'just now';
  if (d < 3.6e6) return `${Math.floor(d / 60e3)}m ago`;
  if (d < 86.4e6) return `${Math.floor(d / 3.6e6)}h ago`;
  if (d < 6.05e8) return `${Math.floor(d / 86.4e6)}d ago`;
  return `${Math.floor(d / 6.05e8)}w ago`;
}

function renderSidebar() {
  const body = document.getElementById('sb-body');
  if (!body) return;
  if (!isSignedIn()) {
    body.innerHTML = `<div class="lbl" style="padding:10px 4px;">sign in to save chats</div>`;
    return;
  }
  const q = sbFilter.toLowerCase();
  const now = Date.now();
  const filtered = recentChats.filter(c => (c.title || '').toLowerCase().includes(q));
  const today = filtered.filter(c => now - c.ts < 86400e3);
  const week = filtered.filter(c => now - c.ts >= 86400e3 && now - c.ts < 7*86400e3);
  const older = filtered.filter(c => now - c.ts >= 7*86400e3);
  const itemHtml = c => {
    const isActive = (c.id === activeChatId) || (c.id === currentPropertyChatId && currentScreen === 'detail');
    const propAttr = c.property_id ? ` data-property-id="${escapeHtml(c.property_id)}"` : '';
    const prefix = c.property_id ? '<span class="sb-item-pin" aria-hidden="true">⌂</span>' : '';
    const preview = c.property_id ? 'reopen this property thread' : 'resume this search';
    const matches = (typeof c.total_count === 'number' && c.total_count > 0)
      ? `<span class="matches-count">${c.total_count.toLocaleString('en-IN')} match${c.total_count === 1 ? '' : 'es'}</span>`
      : '';
    return `<div class="sb-item ${isActive ? 'active' : ''}" data-chat-id="${c.id}"${propAttr} title="${escapeHtml(c.title)}">
       <span class="sb-item-title">${prefix}${escapeHtml(c.title)}</span>
       <span class="sb-item-preview">${preview}</span>
       <div class="sb-item-meta">
         <span class="sb-item-ts">${relTime(c.ts)}</span>
         ${matches}
       </div>
       <button class="sb-item-del" data-del-id="${c.id}" title="delete" aria-label="delete">×</button>
     </div>`;
  };
  const grp = (label, items) => items.length ? `<div class="sb-group-label">${label}</div>` + items.map(itemHtml).join('') : '';
  body.innerHTML = grp('today', today) + grp('this week', week) + grp('earlier', older) || `<div class="lbl" style="padding:10px 4px;">no past chats yet</div>`;
  body.querySelectorAll('.sb-item').forEach(el => el.addEventListener('click', (e) => {
    if (e.target.closest('.sb-item-del')) return;
    const propertyId = el.dataset.propertyId;
    const chatId = el.dataset.chatId;
    closeSidebar();
    if (propertyId) {
      // Route to the property detail page with this specific chat preselected.
      currentDetailId = propertyId;
      _pendingPropertyChatId = chatId;
      go('detail');
    } else {
      loadConversation(chatId);
      // On mobile, switching to a chat means the user wants the conversation
      // pane visible — not the matches pane that may have been on screen.
      if (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) {
        setMobileTab('chat');
      }
    }
  }));
  body.querySelectorAll('.sb-item-del').forEach(btn => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    deleteConversation(btn.dataset.delId);
  }));
}

(function wireConversationsToAuth() {
  if (!(window.Auth && window.Auth.onAuthChange)) return;
  let prevUser = null;
  let didInitialSync = false;
  window.Auth.onAuthChange((user) => {
    const wasSignedIn = !!prevUser;
    const nowSignedIn = !!user;
    prevUser = user;
    if (nowSignedIn && (!wasSignedIn || !didInitialSync)) {
      didInitialSync = true;
      syncConversationsFromServer();
      // A refresh on /chat/{id} stashed _restoreChatId before auth was ready;
      // now that we're signed in, reopen that thread.
      maybeRestoreActiveChat();
    } else if (!nowSignedIn && wasSignedIn) {
      recentChats = [];
      activeChatId = null;
      _restoreChatId = null;
      didInitialSync = false;
      renderSidebar();
    }
  });
})();

document.getElementById('sb-toggle').addEventListener('click', () => {
  const collapsed = document.getElementById('results-grid').classList.toggle('sb-collapsed');
  const btn = document.getElementById('sb-toggle');
  btn.setAttribute('aria-label', collapsed ? 'Show chat history' : 'Hide chat history');
  btn.setAttribute('aria-expanded', String(!collapsed));
});
// Reopen the collapsed history from the conversation header (Option C).
(() => {
  const reopen = document.getElementById('tp-reopen');
  if (!reopen) return;
  reopen.addEventListener('click', () => {
    document.getElementById('results-grid').classList.remove('sb-collapsed');
    const sbt = document.getElementById('sb-toggle');
    if (sbt) { sbt.setAttribute('aria-label', 'Hide chat history'); sbt.setAttribute('aria-expanded', 'true'); }
  });
})();
document.getElementById('sb-search').addEventListener('input', (e) => {
  sbFilter = e.target.value; renderSidebar();
});

(() => {
  const handle = document.getElementById('tp-resize');
  let dragging = false, startX = 0, startW = 360;
  handle.addEventListener('pointerdown', (e) => {
    dragging = true; startX = e.clientX;
    startW = parseInt(getComputedStyle(document.getElementById('results-grid')).gridTemplateColumns.split(' ')[1]) || 360;
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const w = Math.max(240, Math.min(600, startW + (e.clientX - startX)));
    document.getElementById('results-grid').style.setProperty('--transcript-w', w + 'px');
  });
  handle.addEventListener('pointerup', (e) => { dragging = false; handle.releasePointerCapture(e.pointerId); });
})();

/* ====== FEEDBACK ====== */
let fbKind = 'message';
let fbRating = null;
let fbTargetIndex = null;
let fbTags = new Set();
// Scope set when opening message feedback: which history array to read and
// (optionally) which property the feedback is about. Resets on close.
let fbHistory = null;
let fbPropertyId = null;

function openModal() {
  fbTags.clear();
  fbRating = null;
  document.getElementById('fb-text').value = '';
  document.querySelectorAll('#fb-tags .chip').forEach(c => c.classList.remove('filled'));
  document.querySelectorAll('#fb-rating button').forEach(b => b.classList.remove('on'));
  document.getElementById('fb-modal').classList.add('on');
}
function openMessageFeedback(i, scope) {
  fbKind = 'message';
  fbTargetIndex = i;
  fbRating = 'down';
  fbHistory = (scope && scope.history) || chatHistory;
  fbPropertyId = (scope && scope.propertyId) || null;
  document.getElementById('fb-title').textContent = 'What went wrong?';
  document.getElementById('fb-subtitle').textContent = 'help us improve the answers';
  document.getElementById('fb-rating').style.display = 'none';
  document.getElementById('fb-tags').style.display = 'flex';
  openModal();
}
function openGeneralFeedback() {
  fbKind = 'general';
  fbTargetIndex = -1;
  fbRating = null;
  document.getElementById('fb-title').textContent = 'Send us feedback';
  document.getElementById('fb-subtitle').textContent = 'how is the app doing?';
  document.getElementById('fb-rating').style.display = 'flex';
  document.getElementById('fb-tags').style.display = 'none';
  openModal();
}
function closeFeedback() {
  document.getElementById('fb-modal').classList.remove('on');
  fbHistory = null;
  fbPropertyId = null;
}
async function sendMessageFeedback(msgIndex, rating, scope) {
  const history = (scope && scope.history) || chatHistory;
  const propertyId = (scope && scope.propertyId) || null;
  const ai = history[msgIndex];
  if (!ai || ai.role !== 'ai') return;
  let userQ = '';
  for (let k = msgIndex - 1; k >= 0; k--) {
    if (history[k].role === 'user') { userQ = history[k].text; break; }
  }
  const artifacts = (ai.artifacts || []).map(a => ({ tool: a.tool, args: a.args }));
  const contextTurns = history.slice(Math.max(0, msgIndex - 6), msgIndex + 1).map(m => {
    if (m.role === 'user') return { role: 'user', content: m.text };
    if (m.role === 'ai') return { role: 'assistant', content: m.text, tool_calls: (m.artifacts || []).map(a => ({ tool: a.tool, args: a.args })) };
    return null;
  }).filter(Boolean);
  try {
    await apiFeedback({
      kind: 'message', rating, text: null,
      session_id: sessionId(),
      message_index: msgIndex,
      question: userQ, answer: ai.text || '',
      artifacts, context_turns: contextTurns,
      user_agent: navigator.userAgent, page_url: location.href,
      property_id: propertyId,
    });
  } catch(e) { console.warn('feedback failed', e); }
}
async function submitFeedback() {
  const text = document.getElementById('fb-text').value.trim();
  const tagText = [...fbTags].join(', ');
  const combined = [tagText, text].filter(Boolean).join(' — ');
  try {
    if (fbKind === 'message') {
      const i = fbTargetIndex;
      const history = fbHistory || chatHistory;
      const ai = history[i];
      let userQ = '';
      for (let k = i - 1; k >= 0; k--) {
        if (history[k].role === 'user') { userQ = history[k].text; break; }
      }
      const artifacts = (ai?.artifacts || []).map(a => ({ tool: a.tool, args: a.args }));
      const contextTurns = history.slice(Math.max(0, i - 6), i + 1).map(m => {
        if (m.role === 'user') return { role: 'user', content: m.text };
        if (m.role === 'ai') return { role: 'assistant', content: m.text, tool_calls: (m.artifacts || []).map(a => ({ tool: a.tool, args: a.args })) };
        return null;
      }).filter(Boolean);
      await apiFeedback({
        kind: 'message', rating: 'down', text: combined || null,
        session_id: sessionId(),
        message_index: i,
        question: userQ, answer: ai?.text || '',
        artifacts, context_turns: contextTurns,
        user_agent: navigator.userAgent, page_url: location.href,
        property_id: fbPropertyId,
      });
    } else {
      await apiFeedback({
        kind: 'general', rating: fbRating, text: text || null,
        session_id: sessionId(),
        message_index: -1,
        question: '', answer: '',
        artifacts: [], context_turns: [],
        user_agent: navigator.userAgent, page_url: location.href,
      });
    }
  } catch(e) { console.warn('feedback failed', e); }
  closeFeedback();
}
document.querySelectorAll('#fb-tags .chip').forEach(c => {
  c.addEventListener('click', () => {
    const t = c.dataset.tag;
    if (fbTags.has(t)) { fbTags.delete(t); c.classList.remove('filled'); }
    else { fbTags.add(t); c.classList.add('filled'); }
  });
});
document.querySelectorAll('#fb-rating button').forEach(b => {
  b.addEventListener('click', () => {
    fbRating = b.dataset.r;
    document.querySelectorAll('#fb-rating button').forEach(x => x.classList.toggle('on', x === b));
  });
});
document.getElementById('fb-modal').addEventListener('click', (e) => {
  if (e.target.id === 'fb-modal') closeFeedback();
});

/* ====== BROWSE-ALL-PROPERTIES (landing) ====== */
// `category` -> backend `type` (AssetCategory: Residential / Commercial / Land...)
// `propertyType` -> backend `property_type` (PropertyType: Apartment / Plot / Villa...)
// Two real, non-overlapping filter dimensions backed by separate graph edges.
// `category`, `propertyType`, `bank`, `state`, `district`, `village` are
// arrays — each filter can hold multiple selected values, sent to the API
// as repeated query params (e.g., `?bank=A&bank=B`) and OR'd within that
// dimension server-side.
const browseState = {
  q: '',
  category: [], propertyType: [], bank: [],
  state: [], district: [], village: [],
  price: '', priceMin: '', priceMax: '',
  dateFrom: '', dateTo: '',
  // 'upcoming' = live auctions soonest-first, ended ones after. Plain
  // date_asc led with months-old ended auctions on first load.
  sort: 'upcoming',
  offset: 0,
};
const MULTI_FILTER_KEYS = ['category', 'propertyType', 'bank', 'state', 'district', 'village'];
const BROWSE_LIMIT = 60;
let browseAbort = null;
let browseDebounce = null;

function _priceLabel(key) {
  return ({ '0-50': 'under 50L', '50-100': '50L–1Cr', '100-200': '1Cr–2Cr', '200-': '2Cr+' })[key] || key;
}

function _browseQueryParams() {
  const p = new URLSearchParams();
  if (browseState.q) p.set('q', browseState.q);
  // Multi-value filters: append once per selected value. FastAPI parses
  // repeated params into a list[str], the cypher helper OR's within-dim.
  const _appendAll = (key, values) => {
    for (const v of (values || [])) if (v) p.append(key, v);
  };
  _appendAll('type',          browseState.category);
  _appendAll('property_type', browseState.propertyType);
  _appendAll('bank',          browseState.bank);
  _appendAll('state',         browseState.state);
  _appendAll('district',      browseState.district);
  _appendAll('village',       browseState.village);
  // Price comes in as either a preset bucket (e.g. "50-100", in lakhs) or
  // custom min/max (also lakhs from the UI). Convert to rupees for the API.
  let lo = null, hi = null;
  if (browseState.price === 'custom') {
    if (browseState.priceMin !== '') lo = +browseState.priceMin;
    if (browseState.priceMax !== '') hi = +browseState.priceMax;
  } else if (browseState.price) {
    const [a, b] = browseState.price.split('-');
    if (a !== '') lo = +a;
    if (b !== '' && b != null) hi = +b;
  }
  if (lo != null && !isNaN(lo)) p.set('min_price', String(lo * 1e5));
  if (hi != null && !isNaN(hi)) p.set('max_price', String(hi * 1e5));
  if (browseState.dateFrom) p.set('date_from', browseState.dateFrom);
  if (browseState.dateTo) p.set('date_to', new Date(browseState.dateTo + 'T23:59:59').toISOString());
  p.set('sort', browseState.sort || 'upcoming');
  p.set('limit', String(BROWSE_LIMIT));
  p.set('offset', String(browseState.offset || 0));
  return p;
}

function _hasAnyBrowseFilter() {
  if (browseState.q || browseState.price || browseState.priceMin || browseState.priceMax
    || browseState.dateFrom || browseState.dateTo) return true;
  return MULTI_FILTER_KEYS.some(k => (browseState[k] || []).length > 0);
}

// Names of the currently-active filter dimensions (e.g. "bank,state,price") for
// the browse_filter analytics event. Dimension keys only — never the selected
// values (a free-text query could carry PII), so this is safe to send.
function _activeBrowseDims() {
  const dims = [];
  if (browseState.q) dims.push('q');
  if (browseState.price || browseState.priceMin || browseState.priceMax) dims.push('price');
  if (browseState.dateFrom || browseState.dateTo) dims.push('date');
  MULTI_FILTER_KEYS.forEach(k => { if ((browseState[k] || []).length) dims.push(k); });
  return dims.join(',');
}

function _browseFiltersToScope() {
  // Translate browseState into the agent's search_auctions arg names so the
  // model carries them on its first tool call. Lists with one item are sent
  // as a scalar to match the tool's str | list[str] union exactly.
  const f = {};
  const oneOrMany = (vals) => (vals && vals.length === 1 ? vals[0] : vals.slice());
  if (browseState.category?.length)     f.asset_category = oneOrMany(browseState.category);
  if (browseState.propertyType?.length) f.property_type  = oneOrMany(browseState.propertyType);
  if (browseState.bank?.length)         f.bank           = oneOrMany(browseState.bank);
  if (browseState.district?.length)     f.city           = oneOrMany(browseState.district);
  if (browseState.village?.length)      f.area           = oneOrMany(browseState.village);
  if (browseState.price === 'custom') {
    if (browseState.priceMin !== '') f.min_price = +browseState.priceMin * 1e5;
    if (browseState.priceMax !== '') f.max_price = +browseState.priceMax * 1e5;
  } else if (browseState.price) {
    const [a, b] = browseState.price.split('-');
    if (a !== '') f.min_price = +a * 1e5;
    if (b !== '' && b !== undefined) f.max_price = +b * 1e5;
  }
  if (browseState.dateFrom) f.starts_after  = browseState.dateFrom;
  if (browseState.dateTo)   f.starts_before = new Date(browseState.dateTo + 'T23:59:59').toISOString();
  return f;
}

function _updateFilterMultiLabel(wrapId) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  const key = wrap.dataset.key;
  const selected = browseState[key] || [];
  const labelEl = wrap.querySelector('.filter-multi-label');
  if (!labelEl) return;
  if (selected.length === 0) {
    labelEl.textContent = 'all';
  } else if (selected.length === 1) {
    labelEl.textContent = selected[0];
  } else {
    labelEl.textContent = `${selected.length} selected`;
  }
}

function _filterMultiOptions(panel) {
  const input = panel.querySelector('.filter-multi-search-input');
  const optsWrap = panel.querySelector('.filter-multi-options');
  if (!input || !optsWrap) return;
  const q = input.value.trim().toLowerCase();
  const opts = optsWrap.querySelectorAll('.filter-multi-opt');
  let visible = 0;
  for (const opt of opts) {
    const name = (opt.dataset.name || '').toLowerCase();
    const matches = !q || name.includes(q);
    opt.style.display = matches ? '' : 'none';
    if (matches) visible++;
  }
  let noMatch = optsWrap.querySelector('.filter-multi-no-match');
  if (q && !visible && opts.length) {
    if (!noMatch) {
      noMatch = document.createElement('div');
      noMatch.className = 'filter-multi-no-match';
      noMatch.textContent = 'no matches';
      optsWrap.appendChild(noMatch);
    }
  } else if (noMatch) {
    noMatch.remove();
  }
}

function _populateFacetMulti(wrapId, facetRows, selectedValues) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  const panel = wrap.querySelector('.filter-multi-panel');
  if (!panel) return;

  // Build the search + options scaffold once; subsequent calls only refresh
  // the options list, so the user's typed search query and input focus
  // survive across applyBrowse re-renders.
  let optsWrap = panel.querySelector('.filter-multi-options');
  if (!optsWrap) {
    panel.innerHTML = ''
      + '<div class="filter-multi-search">'
      +   '<input type="text" class="filter-multi-search-input" placeholder="search…" aria-label="search options" autocomplete="off">'
      + '</div>'
      + '<div class="filter-multi-options"></div>';
    optsWrap = panel.querySelector('.filter-multi-options');
    const search = panel.querySelector('.filter-multi-search-input');
    search.addEventListener('input', () => _filterMultiOptions(panel));
  }

  const selected = new Set((selectedValues || []).filter(Boolean));
  const seen = new Set();
  const rows = [];
  for (const r of (facetRows || [])) {
    if (!r.value) continue;
    seen.add(r.value);
    rows.push({ value: r.value, count: r.count || 0 });
  }
  // Preserve any selected values missing from the current facet so the
  // user's choice doesn't silently disappear when filters combine.
  for (const v of selected) {
    if (!seen.has(v)) rows.push({ value: v, count: 0 });
  }
  rows.sort((a, b) => a.value.toLowerCase().localeCompare(b.value.toLowerCase()));

  if (!rows.length) {
    optsWrap.innerHTML = `<div class="filter-multi-empty">no options</div>`;
  } else {
    optsWrap.innerHTML = rows.map(r => {
      const checked = selected.has(r.value) ? 'checked' : '';
      return `<label class="filter-multi-opt" data-name="${escapeHtml(r.value)}"><input type="checkbox" value="${escapeHtml(r.value)}" ${checked}><span class="name">${escapeHtml(r.value)}</span><span class="count">(${r.count})</span></label>`;
    }).join('');
  }
  _filterMultiOptions(panel);
  _updateFilterMultiLabel(wrapId);
}

function renderActiveChips() {
  const wrap = document.getElementById('active-chips');
  if (!wrap) return;
  const chips = [];
  if (browseState.q) chips.push({ k: 'q', label: `"${browseState.q}"` });
  // One chip per selected value in each multi-select dimension. The chip's
  // `data-clear="key"` plus `data-value` lets the click handler remove just
  // that single value from browseState[key].
  for (const key of MULTI_FILTER_KEYS) {
    for (const v of (browseState[key] || [])) {
      if (v) chips.push({ k: key, value: v, label: v });
    }
  }
  if (browseState.price === 'custom' && (browseState.priceMin !== '' || browseState.priceMax !== '')) {
    const lo = browseState.priceMin === '' ? '0' : browseState.priceMin;
    const hi = browseState.priceMax === '' ? '∞' : browseState.priceMax;
    chips.push({ k: 'price', label: `₹${lo}L – ₹${hi}L` });
  } else if (browseState.price) {
    chips.push({ k: 'price', label: _priceLabel(browseState.price) });
  }
  if (browseState.dateFrom || browseState.dateTo) {
    chips.push({ k: 'date', label: `${browseState.dateFrom || 'any'} → ${browseState.dateTo || 'any'}` });
  }
  wrap.innerHTML = chips.map(c => {
    const valAttr = c.value != null ? ` data-value="${escapeHtml(c.value)}"` : '';
    return `<span class="active-chip" data-clear="${c.k}"${valAttr} title="remove filter">${escapeHtml(c.label)} <span class="x">×</span></span>`;
  }).join('');

  // Badge on the collapsed "Filters" toggle = number of active advanced filters
  // (the free-text query lives in the always-visible search box, so exclude it).
  const advCount = chips.filter(c => c.k !== 'q').length;
  const badge = document.getElementById('f-active-count');
  if (badge) {
    badge.textContent = advCount ? String(advCount) : '';
    badge.hidden = advCount === 0;
  }
}

// Multi-filter clearers receive the specific value to remove from the
// dimension's array (one chip = one value); cascading dimensions
// (state→district→village) reset downstream selections to avoid stale
// combinations the user can't see.
const _BROWSE_CLEARERS = {
  q:            () => { browseState.q = ''; document.getElementById('browse-q').value = ''; },
  category:     (v) => { browseState.category     = (browseState.category     || []).filter(x => x !== v); },
  propertyType: (v) => { browseState.propertyType = (browseState.propertyType || []).filter(x => x !== v); },
  bank:         (v) => { browseState.bank         = (browseState.bank         || []).filter(x => x !== v); },
  state:        (v) => { browseState.state        = (browseState.state        || []).filter(x => x !== v);
                         browseState.district = []; browseState.village = []; },
  district:     (v) => { browseState.district     = (browseState.district     || []).filter(x => x !== v);
                         browseState.village = []; },
  village:      (v) => { browseState.village      = (browseState.village      || []).filter(x => x !== v); },
  price:        () => { browseState.price = ''; browseState.priceMin = ''; browseState.priceMax = '';
                        document.getElementById('f-price').value = '';
                        document.getElementById('f-price-min').value = '';
                        document.getElementById('f-price-max').value = '';
                        document.getElementById('price-custom').style.display = 'none'; },
  date:         () => { browseState.dateFrom = ''; browseState.dateTo = '';
                        document.getElementById('f-date-from').value = '';
                        document.getElementById('f-date-to').value = ''; },
};

function clearAllBrowseFilters() {
  Object.assign(browseState, {
    q: '',
    category: [], propertyType: [], bank: [],
    state: [], district: [], village: [],
    price: '', priceMin: '', priceMax: '',
    dateFrom: '', dateTo: '',
  });
  document.getElementById('browse-q').value = '';
  document.getElementById('f-price').value = '';
  document.getElementById('f-price-min').value = '';
  document.getElementById('f-price-max').value = '';
  document.getElementById('f-date-from').value = '';
  document.getElementById('f-date-to').value = '';
  document.getElementById('price-custom').style.display = 'none';
  document.querySelectorAll('.filter-multi-search-input').forEach(i => { i.value = ''; });
  applyBrowse();
}

async function applyBrowse({ append = false } = {}) {
  const grid = document.getElementById('browse-grid');
  const countEl = document.getElementById('browse-count');
  const subEl = document.getElementById('browse-sublabel');
  const clearBtn = document.getElementById('f-clear');
  const pagerEl = document.getElementById('browse-pager');
  const loadMoreBtn = document.getElementById('browse-loadmore');
  const pagerProgress = document.getElementById('browse-pager-progress');
  if (!grid) return;

  if (browseAbort) browseAbort.abort();
  browseAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;

  if (!append) browseState.offset = 0;

  if (loadMoreBtn) loadMoreBtn.disabled = true;
  if (pagerProgress && append) pagerProgress.textContent = 'loading…';

  grid.setAttribute('aria-busy', 'true');
  const anyFilter = _hasAnyBrowseFilter();
  // Log a browse_filter only for a fresh, user-initiated application that has at
  // least one active filter — excludes the initial unfiltered load and the
  // "load more" pagination (append), and is naturally debounced by applyBrowse.
  if (!append && anyFilter && window.track) {
    window.track('browse_filter', { filters: _activeBrowseDims(), sort: browseState.sort });
  }
  clearBtn.disabled = !anyFilter;
  const chatBtn = document.getElementById('f-chat');
  if (chatBtn) {
    chatBtn.disabled = !anyFilter;
    chatBtn.hidden = !anyFilter;
  }

  const url = `${API_BASE}/properties?${_browseQueryParams().toString()}`;
  let payload;
  try {
    const r = await authFetch(url, browseAbort ? { signal: browseAbort.signal } : undefined);
    // Deploy-window guard: an API that predates the 'upcoming' sort 400s it.
    // Fall back to date_asc (supported forever) instead of an empty grid.
    if (r.status === 400 && browseState.sort === 'upcoming') {
      browseState.sort = 'date_asc';
      const sortSel = document.getElementById('f-sort');
      if (sortSel) sortSel.value = 'date_asc';
      return applyBrowse({ append });
    }
    if (!r.ok) throw new Error(`browse ${r.status}`);
    payload = await r.json();
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    grid.setAttribute('aria-busy', 'false');
    if (!append) {
      grid.innerHTML = `<div class="browse-empty">couldn't load properties<span class="lbl">${escapeHtml(String(e.message || e))}</span></div>`;
      countEl.textContent = '— properties';
      subEl.textContent = 'try again';
      if (pagerEl) pagerEl.hidden = true;
    } else if (loadMoreBtn) {
      loadMoreBtn.disabled = false;
      if (pagerProgress) pagerProgress.textContent = `couldn't load more — try again`;
    }
    return;
  }

  const facets = payload.facets || {};
  // f-category populates from facets.type (the AssetCategory facet — the
  // backend's `type` query param has historically meant AssetCategory).
  // f-type populates from the new facets.property_type (PropertyType node).
  // Don't repopulate on append — it would clobber the user's current selection
  // with options keyed off the same filters they're already viewing.
  if (!append) {
    _populateFacetMulti('f-category', facets.type,          browseState.category);
    _populateFacetMulti('f-type',     facets.property_type, browseState.propertyType);
    _populateFacetMulti('f-bank',     facets.bank,          browseState.bank);
    _populateFacetMulti('f-state',    facets.state,         browseState.state);
    _populateFacetMulti('f-district', facets.district,      browseState.district);
    _populateFacetMulti('f-village',  facets.village,       browseState.village);
  }

  const total = payload.total || 0;
  const newRows = payload.results || [];
  const newCount = newRows.length;
  const shownAll = (browseState.offset || 0) + newCount;

  const newCards = newRows.map(toCard).filter(c => c.id);
  // Cache rows so toggling save (which reads currentResults to fill the
  // watchlist preview) still works for cards not in the chat-driven list.
  for (const row of newRows) {
    if (row && row.auction_id) watchlistCache[row.auction_id] = toCard(row);
  }
  const newHtml = newCards.map(c => propCardHtml(c, false)).join('');

  if (!append) {
    if (!newCount) {
      grid.innerHTML = `<div class="browse-empty">no properties match these filters<span class="lbl">try clearing one or two</span></div>`;
    } else {
      grid.innerHTML = newHtml;
    }
  } else if (newCount) {
    grid.insertAdjacentHTML('beforeend', newHtml);
  }
  if (newCount) wireCardClicks();

  countEl.textContent = `${total.toLocaleString('en-IN')} propert${total === 1 ? 'y' : 'ies'}`;
  const banks = browseState.bank || [];
  if (banks.length === 1) {
    subEl.textContent = `from ${banks[0]}`;
  } else if (banks.length > 1) {
    subEl.textContent = `from ${banks.length} banks`;
  } else if (total > shownAll) {
    subEl.textContent = `showing ${shownAll.toLocaleString('en-IN')} · across all banks`;
  } else {
    subEl.textContent = 'across all banks';
  }

  if (pagerEl) {
    const hasMore = shownAll < total;
    pagerEl.hidden = total === 0;
    if (loadMoreBtn) {
      loadMoreBtn.hidden = !hasMore;
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = 'load more';
    }
    if (pagerProgress) {
      if (total === 0) {
        pagerProgress.textContent = '';
      } else if (hasMore) {
        pagerProgress.textContent = `${shownAll.toLocaleString('en-IN')} of ${total.toLocaleString('en-IN')} shown`;
      } else {
        pagerProgress.textContent = `all ${total.toLocaleString('en-IN')} shown — that's everything`;
      }
    }
  }

  grid.setAttribute('aria-busy', 'false');
  renderActiveChips();
}

function _scheduleBrowse(delay = 250) {
  if (browseDebounce) clearTimeout(browseDebounce);
  browseDebounce = setTimeout(applyBrowse, delay);
}

function _closeAllFilterMultis(except) {
  document.querySelectorAll('.filter-multi.open').forEach(w => {
    if (w === except) return;
    w.classList.remove('open');
    const p = w.querySelector('.filter-multi-panel');
    const b = w.querySelector('.filter-multi-btn');
    if (p) p.hidden = true;
    if (b) b.setAttribute('aria-expanded', 'false');
  });
}

function _wireFilterMulti(wrapId) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  const key = wrap.dataset.key;
  const btn = wrap.querySelector('.filter-multi-btn');
  const panel = wrap.querySelector('.filter-multi-panel');
  if (!btn || !panel) return;

  btn.addEventListener('click', e => {
    e.stopPropagation();
    const willOpen = panel.hidden;
    _closeAllFilterMultis(willOpen ? wrap : null);
    panel.hidden = !willOpen;
    wrap.classList.toggle('open', willOpen);
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  });

  panel.addEventListener('click', e => { e.stopPropagation(); });

  panel.addEventListener('change', e => {
    const cb = e.target;
    if (!cb || cb.type !== 'checkbox') return;
    const value = cb.value;
    const arr = Array.isArray(browseState[key]) ? browseState[key].slice() : [];
    const idx = arr.indexOf(value);
    if (cb.checked) {
      if (idx < 0) arr.push(value);
    } else if (idx >= 0) {
      arr.splice(idx, 1);
    }
    browseState[key] = arr;
    if (key === 'state') { browseState.district = []; browseState.village = []; }
    else if (key === 'district') { browseState.village = []; }
    _updateFilterMultiLabel(wrapId);
    applyBrowse();
  });
}

(function wireBrowseControls() {
  const $ = (id) => document.getElementById(id);
  if (!$('browse-grid')) return;

  $('browse-q').addEventListener('input', e => { browseState.q = e.target.value; _scheduleBrowse(); });
  ['f-category', 'f-type', 'f-bank', 'f-state', 'f-district', 'f-village'].forEach(_wireFilterMulti);
  document.addEventListener('click', e => {
    if (e.target.closest('.filter-multi')) return;
    _closeAllFilterMultis();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') _closeAllFilterMultis();
  });
  $('f-sort').addEventListener('change', e => { browseState.sort = e.target.value; applyBrowse(); });
  $('f-price').addEventListener('change', e => {
    browseState.price = e.target.value;
    const isCustom = e.target.value === 'custom';
    $('price-custom').style.display = isCustom ? '' : 'none';
    if (!isCustom) {
      browseState.priceMin = ''; browseState.priceMax = '';
      $('f-price-min').value = ''; $('f-price-max').value = '';
    }
    applyBrowse();
  });
  $('f-price-min').addEventListener('input', e => { browseState.priceMin = e.target.value; _scheduleBrowse(); });
  $('f-price-max').addEventListener('input', e => { browseState.priceMax = e.target.value; _scheduleBrowse(); });
  $('f-date-from').addEventListener('change', e => { browseState.dateFrom = e.target.value; applyBrowse(); });
  $('f-date-to').addEventListener('change', e => { browseState.dateTo = e.target.value; applyBrowse(); });
  $('f-clear').addEventListener('click', clearAllBrowseFilters);

  // Collapse / expand the advanced filter rows; remember the choice so the
  // grid isn't pushed down on every visit. Search + active chips stay visible.
  const filtersBox = document.querySelector('.browse-filters');
  const fToggle = $('f-toggle');
  if (filtersBox && fToggle) {
    const COLLAPSE_KEY = 'browse_filters_collapsed';
    let collapsed = false;
    try { collapsed = localStorage.getItem(COLLAPSE_KEY) === '1'; } catch (_) {}
    const applyCollapsed = () => {
      filtersBox.classList.toggle('collapsed', collapsed);
      fToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    };
    applyCollapsed();
    fToggle.addEventListener('click', () => {
      collapsed = !collapsed;
      try { localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0'); } catch (_) {}
      applyCollapsed();
    });
  }

  $('f-chat').addEventListener('click', () => {
    if (!_hasAnyBrowseFilter()) return;
    window.pendingChatScope = _browseFiltersToScope();
    // Mirror the browse search box into the chat input so the model sees
    // the user's free-text intent alongside the structured scope.
    const inp = document.getElementById('landing-input');
    if (inp) {
      if (browseState.q) inp.value = browseState.q;
      inp.focus();
      inp.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });
  $('active-chips').addEventListener('click', e => {
    const chip = e.target.closest('.active-chip');
    if (!chip) return;
    const k = chip.dataset.clear;
    const v = chip.dataset.value;
    const fn = _BROWSE_CLEARERS[k];
    if (fn) { fn(v); applyBrowse(); }
  });
  const loadMore = $('browse-loadmore');
  if (loadMore) {
    loadMore.addEventListener('click', () => {
      browseState.offset = (browseState.offset || 0) + BROWSE_LIMIT;
      applyBrowse({ append: true });
    });
  }
})();

/* ====== INPUT WIRING ====== */
function bindInput(inputId, sendId, handler) {
  const inp = document.getElementById(inputId);
  const send = document.getElementById(sendId);
  const isTextarea = inp.tagName === 'TEXTAREA';
  const autoGrow = () => {
    if (!isTextarea) return;
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 160) + 'px';
  };
  const trigger = () => {
    const v = inp.value.trim();
    if (!v) return;
    inp.value = '';
    autoGrow();
    handler(v);
  };
  send.addEventListener('click', trigger);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      trigger();
    }
  });
  if (isTextarea) {
    inp.addEventListener('input', autoGrow);
    autoGrow();
  }
}
bindInput('landing-input', 'landing-send', (v) => askAI(v, { fromLanding: true }));
bindInput('results-input', 'results-send', (v) => askAI(v));
bindInput('detail-input', 'detail-send', (v) => askAboutProperty(v));
document.getElementById('detail-chat-new').addEventListener('click', () => clearDetailChat());

document.getElementById('detail-desc-toggle').addEventListener('click', (e) => {
  const btn = e.currentTarget;
  const descEl = document.getElementById('detail-desc');
  const next = btn.dataset.mode === 'formatted' ? 'original' : 'formatted';
  descEl.classList.toggle('formatted', next === 'formatted');
  descEl.innerHTML = descEl.dataset[next] || '';
  btn.dataset.mode = next;
  btn.textContent = next === 'formatted' ? 'View original' : 'View formatted';
  btn.setAttribute('aria-pressed', next === 'original' ? 'true' : 'false');
});

document.getElementById('detail-share').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  if (!currentDetailId) return;
  const url = propertyShareURL(currentDetailId);
  const title = currentDetailTitle || 'Bank Auction Property';
  try {
    if (navigator.share && /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
      await navigator.share({ title, url });
    } else {
      await navigator.clipboard.writeText(url);
    }
    flashBtn(btn);
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    try { await navigator.clipboard.writeText(url); flashBtn(btn); } catch {}
  }
});

// Delegated so it also fires for chips swapped in later by hydrateSuggestions()
// (feedback chips use data-tag, not data-q, so they're unaffected).
document.addEventListener('click', (e) => {
  const chip = e.target.closest('.chip[data-q]');
  if (chip) askAI(chip.dataset.q, { fromLanding: true });
});

/* ====== DATA FRESHNESS ====== */
// Best-effort "data updated …" indicator. Reads /stats and fills #data-freshness
// only on success — any failure leaves the element empty so the UI never breaks.
function _relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso);
  if (isNaN(then.getTime())) return null;
  const secs = Math.floor((Date.now() - then.getTime()) / 1000);
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  const days = Math.floor(hrs / 24);
  if (days < 30) return days + 'd ago';
  return then.toLocaleDateString();
}
async function loadDataFreshness() {
  const el = document.getElementById('data-freshness');
  if (!el) return;
  try {
    const r = await authFetch(`${API_BASE}/stats`);
    if (!r.ok) return;
    const s = await r.json();
    const rel = _relativeTime(s.last_enriched);
    el.textContent = rel ? `· data updated ${rel}` : '';
    if (rel) el.title = `Enrichment last refreshed ${new Date(s.last_enriched).toLocaleString()}`;
  } catch (_) { /* freshness is best-effort; never block the UI */ }
}

/* ====== INIT ====== */
sessionId();
updateSavedCount();
loadAlerts();
renderSidebar();
renderResultsList();
hydrateModes();
hydrateSuggestions();
hydrateChatModels();
// Re-hydrate the model/effort toggles on auth change so a fresh login (or an
// upgrade to paid) unlocks Pro without a page refresh.
if (window.Auth && window.Auth.onAuthChange) {
  window.Auth.onAuthChange(() => { hydrateChatModels(); });
}
applyBrowse();
loadDataFreshness();

function applyURLState(replace) {
  const path = window.location.pathname;
  const propMatch = path.match(/^\/property\/([^/]+)\/?$/);
  const chatMatch = path.match(/^\/chat\/([^/]+)\/?$/);
  const legacyId = new URLSearchParams(window.location.search).get(URL_ID_PARAM);

  if (propMatch) {
    currentDetailId = decodeURIComponent(propMatch[1]);
    detailReturnScreen = 'landing';
    go('detail');
    if (replace) syncURLForScreen('detail', true);
  } else if (legacyId) {
    currentDetailId = legacyId;
    detailReturnScreen = 'landing';
    go('detail');
    syncURLForScreen('detail', true);
  } else if (chatMatch) {
    // Deep link to a saved conversation. Stash the id (pathForScreen keeps it
    // in the URL via _restoreChatId), show the chat screen, then reopen the
    // thread once auth resolves.
    _restoreChatId = decodeURIComponent(chatMatch[1]);
    go('results');
    maybeRestoreActiveChat();
  } else if (IS_LAB || path === '/chat') {
    // /lab is the chat surface with v2 forced on (see CHAT_V2), so it boots
    // straight into the chat screen rather than the landing page.
    go('results');
  } else if (path === '/watchlist') {
    go('watchlist');
  } else if (path === '/dossiers') {
    go('dossiers');
  } else {
    go('landing');
  }
}
// Prerendered property pages (scripts/prerender_properties.py) ship a static,
// crawlable content block for bots/no-JS — real users get it too, briefly,
// as a fast-first-paint bonus, then the live app takes over here.
var _ssrProperty = document.getElementById('ssr-property');
if (_ssrProperty) _ssrProperty.remove();
applyURLState(true);
window.addEventListener('popstate', () => {
  const path = window.location.pathname;
  const propMatch = path.match(/^\/property\/([^/]+)\/?$/);
  const chatMatch = path.match(/^\/chat\/([^/]+)\/?$/);

  if (propMatch) {
    const id = decodeURIComponent(propMatch[1]);
    currentDetailId = id;
    if (currentScreen !== 'detail') {
      detailReturnScreen = currentScreen || 'landing';
      go('detail');
    } else {
      loadAndRenderDetail(id);
    }
  } else if (chatMatch) {
    const cid = decodeURIComponent(chatMatch[1]);
    if (cid !== activeChatId) {
      _restoreChatId = cid;
      if (currentScreen !== 'results') go('results');
      maybeRestoreActiveChat();
    } else if (currentScreen !== 'results') {
      go('results');
    }
  } else if (path === '/chat') {
    if (currentScreen !== 'results') go('results');
  } else if (path === '/watchlist') {
    if (currentScreen !== 'watchlist') go('watchlist');
  } else if (path === '/dossiers') {
    if (currentScreen !== 'dossiers') go('dossiers');
  } else {
    if (currentScreen === 'detail') currentDetailId = null;
    if (currentScreen !== 'landing') go('landing');
  }
});
