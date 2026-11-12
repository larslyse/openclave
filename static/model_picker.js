// Model picker: every model as a row, with columns
//   Vendor · Model · Route · Speed · Latency · Price · Released
// and sortable by any of them (click a header; again to reverse). The sort
// is remembered in this browser.
//
//   OpenClaveModelPicker.enhance(select, models)  // desktop: a button + popover over a <select>
//   OpenClaveModelPicker.table(models, currentId, onPick)  // phone: an inline table element
//
// enhance keeps the <select> (hidden) as the source of truth: picking a row
// sets its value and fires "change", so the code that already listens to the
// select keeps working. Options the select has that aren't models (e.g. "Off"
// for the fallback) show as plain rows above the table.
// models: /api/models rows (main._model_option): vendor, short_name, route,
// speed, latency_ms, latency_samples, cost_label, cost_color, api_*_usd,
// released, released_is_added, old, supports, display_name, tier.
(function () {
  'use strict';

  const SORT_KEY = 'openclave.modelSort';
  const VENDOR_SHORT = {Anthropic: 'ANT', OpenAI: 'OAI', xAI: 'xAI'};
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const isApi = m => m.route === 'API';
  const price = m => isApi(m) ? (m.api_output_usd ?? Infinity) : -1;  // a subscription costs nothing extra per reply

  const COLS = [
    {key: 'vendor', label: 'Vendor', value: m => m.vendor || ''},
    {key: 'name', label: 'Model', value: m => (m.short_name || m.display_name || '').toLowerCase()},
    {key: 'route', label: 'Route', value: m => m.route || ''},
    {key: 'speed', label: 'Speed', value: m => (m.speed === 'fast' ? 0 : 1)},
    {key: 'latency', label: 'Latency', value: m => m.latency_ms ?? Infinity},
    {key: 'price', label: 'Price', value: price},
    {key: 'released', label: 'Released', value: m => m.released || ''},
  ];

  const CSS = `
  .mp-button{display:inline-flex;align-items:center;gap:6px;cursor:pointer;text-align:left;max-width:100%}
  .mp-button .mp-caret{opacity:.6;font-size:.8em}
  .mp-pop{position:fixed;z-index:2000;background:#1a1e22;color:#e7e4de;border:1px solid #30373a;border-radius:10px;box-shadow:0 10px 36px rgba(0,0,0,.5);max-height:min(70vh,560px);overflow:auto;font:13px/1.35 system-ui,sans-serif}
  .mp-pop.sheet{left:0!important;right:0;bottom:0;top:auto!important;width:auto!important;max-height:75vh;border-radius:14px 14px 0 0}
  .mp-table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
  .mp-table th{position:sticky;top:0;background:#15191c;color:#a4aaa9;font-weight:500;font-size:12px;text-align:left;padding:7px 9px;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid #30373a}
  .mp-table th:hover{color:#e7e4de}
  .mp-table th .mp-dir{font-size:10px;margin-left:3px;color:var(--accent,#9dd6b9)}
  .mp-table td{padding:7px 9px;border-bottom:1px solid #262b2e;white-space:nowrap}
  .mp-row{cursor:pointer}
  .mp-row:hover td{background:#22282c}
  .mp-row.selected td{background:#243029}
  .mp-row.selected td:first-child{box-shadow:inset 3px 0 0 var(--accent,#9dd6b9)}
  .mp-row.old td{color:#8b9090}
  .mp-name{font-weight:600;color:#e7e4de}
  .mp-row.old .mp-name{font-weight:500;color:#a4aaa9}
  .mp-muted{color:#7d8484}
  .mp-tag{display:inline-block;margin-left:5px;padding:0 5px;border-radius:4px;font-size:10.5px;font-weight:600;background:#2f3a33;color:#9dd6b9;vertical-align:1px}
  .mp-old{display:inline-block;margin-left:5px;padding:0 5px;border-radius:4px;font-size:10.5px;background:#2b2f33;color:#a4aaa9}
  .mp-short{display:none}
  .mp-extra td{color:#a4aaa9;font-style:italic}
  @media (max-width:640px){
    .mp-table th,.mp-table td{padding:8px 4px}
    .mp-table th:first-child,.mp-table td:first-child{padding-left:8px}
    .mp-table th:last-child,.mp-table td:last-child{padding-right:8px}
    .mp-long{display:none}.mp-short{display:inline}
    .mp-col-released{display:none}
  }`;

  function injectCss() {
    if (document.getElementById('mp-css')) return;
    const style = document.createElement('style');
    style.id = 'mp-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function loadSort() {
    try {
      const s = JSON.parse(localStorage.getItem(SORT_KEY) || 'null');
      if (s && COLS.some(c => c.key === s.key) && (s.dir === 1 || s.dir === -1)) return s;
    } catch { /* storage may be blocked */ }
    return null;  // default: vendor, then newest first
  }
  function saveSort(s) { try { localStorage.setItem(SORT_KEY, JSON.stringify(s)); } catch { /* fine */ } }

  function sorted(models, sort) {
    const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
    const byReleasedDesc = (a, b) => cmp(b.released || '', a.released || '');
    const byName = (a, b) => cmp((a.short_name || '').toLowerCase(), (b.short_name || '').toLowerCase());
    return [...models].sort((a, b) => {
      if (!sort) return cmp(a.vendor || '', b.vendor || '') || byReleasedDesc(a, b) || byName(a, b) || cmp(a.route, b.route);
      const col = COLS.find(c => c.key === sort.key);
      return sort.dir * cmp(col.value(a), col.value(b)) || byReleasedDesc(a, b) || byName(a, b) || cmp(a.route, b.route);
    });
  }

  function cells(m) {
    const secs = m.latency_ms != null ? `${(m.latency_ms / 1000).toFixed(1)}s` : '—';
    const latencyTitle = m.latency_ms != null ? `average of ${m.latency_samples} replies` : 'not measured yet';
    const priceCell = isApi(m)
      ? (m.cost_label ? `<span style="color:${esc(m.cost_color)}" title="$${esc(m.api_input_usd)} / $${esc(m.api_output_usd)} per M tokens (in / out)">${esc(m.cost_label)}</span>` : '<span class="mp-muted">?</span>')
      : `<span class="mp-muted" title="On your subscription${m.api_output_usd != null ? ` (API list price $${esc(m.api_input_usd)} / $${esc(m.api_output_usd)} per M)` : ''}">sub</span>`;
    const released = m.released
      ? `<span${m.released_is_added ? ' title="Added to OpenClave this month (OpenAI doesn\'t publish dates)"' : ''}>${esc(m.released)}${m.released_is_added ? '*' : ''}</span>`
      : '<span class="mp-muted">—</span>';
    const tags = (m.supports || []).map(s => `<span class="mp-tag" title="Supports this agent's ${esc(s)} connection">${esc(s)}</span>`).join('');
    return [
      `<span class="mp-long">${esc(m.vendor)}</span><span class="mp-short">${esc(VENDOR_SHORT[m.vendor] || m.vendor)}</span>`,
      `<span class="mp-name" title="${esc(m.display_name)}${m.tier ? ` · ${esc(m.tier)}` : ''}">${esc(m.short_name || m.display_name)}</span>${tags}`,
      esc(m.route),
      esc(m.speed),
      `<span title="${latencyTitle}"${m.latency_ms == null ? ' class="mp-muted"' : ''}>${secs}</span>`,
      priceCell,
      released + (m.old ? `<span class="mp-old" title="${esc(m.old)}">old</span>` : '')
        + (m.ready === false ? '<span class="mp-old" title="This route isn\'t set up on this computer yet: see Control Panel → Setup">not set up</span>' : ''),
    ];
  }

  // The table: an element that redraws itself when a header is clicked.
  function table(models, currentId, onPick, extras = []) {
    injectCss();
    const wrap = document.createElement('div');
    wrap.className = 'mp-wrap';
    let sort = loadSort();
    const draw = () => {
      const head = COLS.map(c => `<th data-key="${c.key}" class="mp-col-${c.key}" scope="col">${c.label}${sort && sort.key === c.key ? `<span class="mp-dir">${sort.dir === 1 ? '▲' : '▼'}</span>` : ''}</th>`).join('');
      const extraRows = extras.map(x => `<tr class="mp-row mp-extra${x.value === currentId ? ' selected' : ''}" data-id="${esc(x.value)}"><td colspan="${COLS.length}">${esc(x.label)}</td></tr>`).join('');
      const rows = sorted(models, sort).map(m => `<tr class="mp-row${m.id === currentId ? ' selected' : ''}${m.old || m.ready === false ? ' old' : ''}" data-id="${esc(m.id)}">${cells(m).map((c, i) => `<td class="mp-col-${COLS[i].key}">${c}</td>`).join('')}</tr>`).join('');
      wrap.innerHTML = `<table class="mp-table"><thead><tr>${head}</tr></thead><tbody>${extraRows}${rows}</tbody></table>`;
      wrap.querySelectorAll('th').forEach(th => th.onclick = e => {
        e.stopPropagation();
        const key = th.dataset.key;
        sort = sort && sort.key === key ? {key, dir: -sort.dir} : {key, dir: key === 'released' ? -1 : 1};
        saveSort(sort);
        draw();
      });
      wrap.querySelectorAll('.mp-row').forEach(tr => tr.onclick = () => onPick(tr.dataset.id));
    };
    draw();
    return wrap;
  }

  let openPop = null;
  function closePop() {
    if (!openPop) return;
    openPop.el.remove();
    document.removeEventListener('pointerdown', openPop.outside, true);
    document.removeEventListener('keydown', openPop.key, true);
    window.removeEventListener('resize', closePop);
    openPop = null;
  }

  function label(select, byId) {
    const m = byId.get(select.value);
    if (!m) return esc(select.options[select.selectedIndex]?.textContent?.trim() || 'Choose a model');
    const p = isApi(m) ? (m.cost_label || '') : 'sub';
    return `${esc(m.short_name || m.display_name)} <span class="mp-muted">· ${esc(m.route)}${p ? ` · ${esc(p)}` : ''}</span>`;
  }

  function enhance(select, models) {
    if (!select) return null;
    injectCss();
    const byId = new Map(models.map(m => [m.id, m]));
    let button = select._mpButton;
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = `${select.className} mp-button`.trim();
      button.title = select.title || 'Change model';
      select.after(button);
      select.hidden = true;
      select.style.display = 'none';
      select._mpButton = button;
      select.addEventListener('change', () => { button.innerHTML = `${label(select, select._mpById)}<span class="mp-caret">▾</span>`; });
    }
    select._mpById = byId;
    button.innerHTML = `${label(select, byId)}<span class="mp-caret">▾</span>`;
    button.onclick = e => {
      e.stopPropagation();
      if (openPop && openPop.button === button) { closePop(); return; }
      closePop();
      const extras = [...select.options].filter(o => !byId.has(o.value)).map(o => ({value: o.value, label: o.textContent.trim()}));
      const pop = document.createElement('div');
      pop.className = 'mp-pop';
      pop.setAttribute('role', 'dialog');
      pop.setAttribute('aria-label', 'Choose a model');
      pop.appendChild(table(models.filter(m => [...select.options].some(o => o.value === m.id)), select.value, id => {
        closePop();
        if (id === select.value) return;
        select.value = id;
        select.dispatchEvent(new Event('change', {bubbles: true}));
      }, extras));
      document.body.appendChild(pop);
      const vw = document.documentElement.clientWidth, vh = window.innerHeight;
      if (vw < 640) pop.classList.add('sheet');
      else {
        const r = button.getBoundingClientRect();
        pop.style.width = 'max-content';
        pop.style.maxWidth = `${vw - 16}px`;
        const width = pop.offsetWidth + (pop.offsetWidth - pop.clientWidth);  // room for the scrollbar
        pop.style.width = `${Math.min(width, vw - 16)}px`;
        pop.style.left = `${Math.max(8, Math.min(r.left, vw - width - 8))}px`;
        const below = vh - r.bottom - 8;
        if (below >= 240 || below >= r.top) { pop.style.top = `${r.bottom + 4}px`; pop.style.maxHeight = `${Math.min(560, below - 4)}px`; }
        else { pop.style.top = `${Math.max(8, r.top - 4 - Math.min(560, pop.scrollHeight, r.top - 12))}px`; pop.style.maxHeight = `${Math.min(560, r.top - 12)}px`; }
      }
      pop.querySelector('.mp-row.selected')?.scrollIntoView({block: 'nearest'});
      const outside = ev => { if (!pop.contains(ev.target) && ev.target !== button && !button.contains(ev.target)) closePop(); };
      const key = ev => { if (ev.key === 'Escape') { ev.stopPropagation(); closePop(); button.focus(); } };
      document.addEventListener('pointerdown', outside, true);
      document.addEventListener('keydown', key, true);
      window.addEventListener('resize', closePop);
      openPop = {el: pop, button, outside, key};
    };
    return button;
  }

  // After code sets select.value itself (e.g. undoing a failed save).
  const refresh = select => { if (select?._mpButton) select._mpButton.innerHTML = `${label(select, select._mpById)}<span class="mp-caret">▾</span>`; };

  window.OpenClaveModelPicker = {enhance, table, refresh, close: closePop};
})();
