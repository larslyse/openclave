const app = document.querySelector('#app');
const LAST_ROUTE = 'openclave.mobile.last-route';
// Embedded in the desktop agent page's Dash/Tools tab (dashboard_shell.js,
// 2026-10-14/16): only the agent's page or tools, no top bar or
// chat/files buttons (desktop has its own), and it never stays on another screen.
const EMBED = /[?&]embed=page(&|$)/.test(globalThis.location?.search || '');
if (EMBED) document.documentElement.classList.add('embed');
// In the desktop pane: when content is wider than the pane (a wide table or
// html block), say how wide it wants to be; the pane grows to fit.
if (EMBED && typeof ResizeObserver !== 'undefined') {
  let queued = false;
  const reportWidth = () => {
    queued = false;
    const doc = document.documentElement, view = doc.clientWidth;
    let want = doc.scrollWidth;
    for (const el of app.querySelectorAll('section, table, iframe, pre, .page-card, .content')) {
      if (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible') {
        want = Math.max(want, view + el.scrollWidth - el.clientWidth);
      }
    }
    if (want > view + 1) window.parent.postMessage({__openclaveEmbed: true, kind: 'want', width: want + 8}, location.origin);
  };
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(reportWidth); } };
  new ResizeObserver(queue).observe(document.documentElement);
  new MutationObserver(queue).observe(app, {childList: true, subtree: true});
}
const PENDING_KEY = 'openclave.mobile.pending';
let agents = [];
// Claves (claves.py): [{id, name, members}] when "Multiple claves" is on, else [].
let claveList = [], channelList = [];
let renderToken = 0;
// One mic/speaker for the app's lifetime, so the iOS audio unlock sticks.
// The server supplies the public HTTPS address; deployments keep that value in
// OPENCLAVE_HOME/openclave.toml rather than baking a host into the client.
const mobileNetwork = fetch('/api/network').then(response => response.ok ? response.json() : {})
  .catch(() => ({}));
const mobileAudioPromise = mobileNetwork.then(network => {
  const secureUrl = network.secure_mobile_url;
  const insecureHint = secureUrl
    ? `Voice needs the secure address: ${secureUrl} — if you opened this from an old Home Screen icon, remove it and re-add it from that URL.`
    : 'Voice needs the configured HTTPS address of OpenClave (microphone access is blocked on plain HTTP).';
  return {audio: OpenClaveChat.createLocalAudio({insecureHint}), voiceAvailable: network.voice_available === true};
});
const escapeHtml = value => {
  const node = document.createElement('div');
  node.textContent = value ?? '';
  return node.innerHTML;
};
const byId = id => agents.find(agent => agent.id === id);

// Open instantly (Rick's backlog, 2026-10-05): the agent list and each
// agent's conversation list are kept on the phone and drawn straight away,
// then replaced in place by the server's. "new" markers only show once the
// server's copy is in (agentsFresh / fromCache), and queued requests always
// come from the pending store, never from these copies.
const AGENTS_CACHE = 'openclave.mobile.cache.agents';
const CONVOS_CACHE = 'openclave.mobile.cache.convos';
const CACHE_CONVOS_PER_AGENT = 40;
let agentsFresh = false;
const readJson = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const writeJson = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* full or blocked: no cache */ } };
function setAgents(list) {
  agents = list;
  agentsFresh = true;
  writeJson(AGENTS_CACHE, list);
}
const cachedConversations = agentId => readJson(CONVOS_CACHE, {})[agentId] || null;
function cacheConversations(agentId, list) {
  const all = readJson(CONVOS_CACHE, {});
  all[agentId] = list.slice(0, CACHE_CONVOS_PER_AGENT);
  for (const id of Object.keys(all)) if (!byId(id) && id !== agentId) delete all[id];
  writeJson(CONVOS_CACHE, all);
}
const draftKey = (agent, conversation) => `openclave.draft.${agent}.${conversation || 'new'}`;
const getPending = () => {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY) || '[]');
    return Array.isArray(saved) ? saved : saved ? [{...saved, accepted:true}] : [];
  } catch { return []; }
};
const setPending = request => {
  const requests = getPending().filter(r => r.requestId !== request.requestId);
  requests.push(request);
  localStorage.setItem(PENDING_KEY, JSON.stringify(requests));
};
const removePending = request => localStorage.setItem(PENDING_KEY,
  JSON.stringify(getPending().filter(r => r.requestId !== request.requestId)));


async function api(path, options = {}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  let body = null;
  try { body = await response.json(); } catch {}
  if (!response.ok) {
    const error = new Error(body?.detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function go(hash) { location.hash = hash; }
function routeParts() { return location.hash.replace(/^#\/?/, '').split('/').filter(Boolean); }

// `agent` (an agent's own screens): its avatar before the name, and left of
// it the button to the agent's feature page (Reba's sliders, ...); on that
// page the button is lit and leads back to the chats (2026-10-05).
// `subtitle` (chat screens): the conversation's name on a second line under
// the agent's, so the owner sees which chat they're in (Rick's backlog, 2026-10-14).
function shell(title, backHash, body, right = '<span></span>', agent = null, subtitle = null) {
  const onFeature = Boolean(agent) && routeParts()[2] === 'console';
  const feature = agent ? `<button type="button" class="feature-btn${onFeature ? ' active' : ''}" id="feature-btn" aria-label="${onFeature ? 'Back to chats' : `${escapeHtml(agent.name)}'s page`}" aria-pressed="${onFeature}"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/></svg></button>` : '';
  app.innerHTML = `<main class="shell">
    <header class="topbar">
      <div class="topbar-left">${backHash ? `<button class="icon-btn" id="back" aria-label="Back">‹</button>` : ''}${feature}</div>
      <h1>${agent ? `${avatar(agent, 'small')}${subtitle === null ? `<span class="title-text">${escapeHtml(title)}</span>` : `<span class="title-stack"><span class="title-text">${escapeHtml(title)}</span><span class="title-sub">${escapeHtml(subtitle)}</span></span>`}` : escapeHtml(title)}</h1><div class="topbar-right"><span class="notify-slot"></span>${right}</div>
    </header>${body}</main>`;
  // Notifications bell, left of the model badge (reply_notifications.js).
  window.OpenClaveNotifications?.mountIn(document.querySelector('.notify-slot'));
  if (backHash) document.querySelector('#back').onclick = () => go(backHash);
  if (agent) document.querySelector('#feature-btn').onclick = () => go(onFeature ? `#/agents/${agent.id}/conversations` : `#/agents/${agent.id}/console`);
}

// Short badge for the topbar — current model, tap to change. Falls back to
// an empty slot when the agent's model isn't known yet (still loading).
// Compact model label (2026-10-12): "Opus 5.5 · 16s", not "Claude
// Opus 5.5 (subscription)". The seconds are that model's measured average
// reply time (/api/model-response-times), kept on the phone between visits.
const TIMES_CACHE = 'openclave.mobile.cache.times';
let responseTimes = readJson(TIMES_CACHE, {});
const shortModelName = name => String(name || '').replace(/^Claude /, '').replace(/\s*\((subscription|CLI)\)$/i, '');
function modelSeconds(modelId) {
  const t = responseTimes[modelId];
  return t && t.average_ms ? `${Math.round(t.average_ms / 1000)}s` : '';
}
function modelBadgeText(agent) {
  const secs = modelSeconds(agent.model_id);
  return shortModelName(agent.model_name) + (secs ? ` · ${secs}` : '');
}
function loadResponseTimes() {
  api('/api/model-response-times').then(times => {
    responseTimes = times || {};
    writeJson(TIMES_CACHE, responseTimes);
    const badge = document.querySelector('[data-model-pick]');
    const agent = badge && byId(badge.dataset.modelPick);
    if (agent) badge.textContent = modelBadgeText(agent);
  }).catch(() => {});
}
function modelBadge(agent) {
  if (!agent.model_name) return '<span></span>';
  return `<button class="icon-btn model-badge" data-model-pick="${agent.id}" aria-label="Change model (${escapeHtml(agent.model_name)})">${escapeHtml(modelBadgeText(agent))}</button>`;
}
function wireModelBadge(agent) {
  document.querySelector('[data-model-pick]')?.addEventListener('click', () => go(`#/agents/${agent.id}/model`));
}

// Quick jump lands in the agent's latest chat with that agent's conversation
// list right under it in history, so Back, our swipe and Safari's own edge
// swipe all open that list instead of returning to the previous agent
// (Rick's backlog, 2026-10-02).
async function jumpToLatestConversation(agentId) {
  const listHash = `#/agents/${agentId}/conversations`;
  let target = listHash;
  try {
    const conversations = await api(`/${agentId}/conversations`);
    target = conversations.length ? `${listHash}/${conversations[0].id}` : `#/agents/${agentId}/new`;
  } catch {}
  if (target === listHash) { go(listHash); return; }
  openOverList(agentId, target);
}

// Open a chat with the agent's conversation list under it in history, so
// Back and the swipe land on that list (Quick jump, a newly created agent,
// Autojump). The list is drawn for a moment first: iOS's swipe-back shows a
// screenshot of the entry below, taken when we leave it, and replaceState
// alone kept the previous agent's picture there, so the swipe showed the old
// agent before the list appeared (2026-10-04).
async function openOverList(agentId, target) {
  const listHash = `#/agents/${agentId}/conversations`;
  history.replaceState(null, '', listHash);
  await render();
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));  // painted
  if (location.hash !== listHash) return;  // The owner already went somewhere else
  history.pushState({upTo: listHash}, '', target);
  render();
}

// "Quick jump" button, full width under the message box (see renderChat). It
// used to float above Send and got hit by accident, so it was removed on
// 2026-09-25; the owner asked for it back under the message box instead. Tap and
// hold both open the quick-switch menu above the button (2026-09-28:
// tap used to go to the overview). Hold: drag onto another agent and release
// to jump straight into their latest conversation. Tap, or a hold released
// on no agent: the menu stays open; tap an agent to jump, tap elsewhere or
// the button again to close. Pointer capture keeps drag tracking alive once
// the finger leaves the button.
function wireHomeButton() {
  document.querySelectorAll('[data-home]').forEach(b => b.onclick = () => go('#/agents'));
}

// pick(agentId): what choosing an agent does (chat: its latest chat; the
// conversation list: that agent's list).
function wireSwitchButton(currentId, pick = jumpToLatestConversation) {
  const btn = document.querySelector('#switch-btn');
  if (!btn) return;
  const HOLD_MS = 350;
  let holdTimer = null;
  let menuEl = null;
  let menuOpen = false;
  let activeAgentId = null;
  let closeOnUp = false;
  let anchorWatch = null;
  let placeMenu = null;

  function openMenu() {
    menuOpen = true;
    btn.classList.add('holding');
    menuEl = document.createElement('div');
    menuEl.className = 'quick-switch';
    drawItems();
    // Right side of the screen, but names left-aligned inside the menu.
    // Above the whole composer, so it never covers the text box or Send, and
    // it follows the composer while open (a status line or a growing text
    // box pushes it up; the menu used to stay put and cover it).
    const anchor = btn.closest('.composer, .list-footer') || btn;
    const place = () => { if (menuEl) menuEl.style.bottom = `${window.innerHeight - anchor.getBoundingClientRect().top + 8}px`; };
    place();
    anchorWatch = new ResizeObserver(place);
    anchorWatch.observe(anchor);
    placeMenu = place;
    window.addEventListener('resize', place);
    menuEl.onclick = event => {
      const toggle = event.target.closest('[data-autojump]');
      if (toggle) {
        const on = chatStore.get(OpenClaveChat.AUTOJUMP_KEY) !== 'on';
        if (on) chatStore.set(OpenClaveChat.AUTOJUMP_KEY, 'on');
        else { chatStore.remove(OpenClaveChat.AUTOJUMP_KEY); chatStore.remove(OpenClaveChat.AUTOJUMP_QUEUE_KEY); }
        toggle.setAttribute('aria-checked', String(on));
        toggle.querySelector('.toggle-state').textContent = on ? 'On' : 'Off';
        return;
      }
      const item = event.target.closest('.quick-switch-item');
      if (!item) return;
      closeMenu();
      pick(item.dataset.agent);
    };
    document.body.appendChild(menuEl);
    document.addEventListener('pointerdown', onOutside, true);
    window.addEventListener('hashchange', closeMenu);
  }

  function drawItems() {
    // Same priority as the overview (unread replies, then most recent), but
    // upside down: the menu opens above the button, so the most likely pick
    // sits at the bottom, nearest the thumb (2026-10-04).
    const seen = loadSeen() || {};
    const others = agents.filter(a => a.id !== currentId).sort((a, b) =>
      (hasUnread(b, seen) - hasUnread(a, seen)) || ((b.last_activity_ms ?? -1) - (a.last_activity_ms ?? -1))).reverse();
    // Autojump on/off sits at the top of the same menu (opt-in, per phone).
    const autojumpOn = chatStore.get(OpenClaveChat.AUTOJUMP_KEY) === 'on';
    menuEl.innerHTML = `<div class="quick-switch-toggle" data-autojump role="switch" aria-checked="${autojumpOn}">Autojump to replies<span class="toggle-state">${autojumpOn ? 'On' : 'Off'}</span></div>` + others.map(a =>
      `<div class="quick-switch-item" data-agent="${a.id}"><span class="dot" style="background:${a.color || 'var(--accent)'}"></span>${escapeHtml(a.name)}${hasUnread(a, seen) ? NEW_LIGHT : ''}</div>`
    ).join('');
  }

  // `agents` is only as fresh as the last /api/agents fetch, and replies that
  // landed while the owner sat in a chat were missing from the menu. Fetch on
  // every press; if the menu is already open and no finger is on an item,
  // redraw it with the fresh "new" markers and order.
  function refreshAgents() {
    api('/api/agents').then(list => {
      setAgents(list);
      if (menuOpen && menuEl && !activeAgentId) drawItems();
    }).catch(() => { /* keep the menu as drawn */ });
  }

  function closeMenu() {
    menuOpen = false;
    activeAgentId = null;
    btn.classList.remove('holding');
    menuEl?.remove();
    menuEl = null;
    anchorWatch?.disconnect();
    anchorWatch = null;
    if (placeMenu) window.removeEventListener('resize', placeMenu);
    placeMenu = null;
    document.removeEventListener('pointerdown', onOutside, true);
    window.removeEventListener('hashchange', closeMenu);
  }

  function onOutside(event) {
    if (!menuEl?.contains(event.target) && !btn.contains(event.target)) closeMenu();
  }

  function updateHover(x, y) {
    const hit = document.elementFromPoint(x, y)?.closest('.quick-switch-item');
    const nextId = hit?.dataset.agent || null;
    if (nextId === activeAgentId) return;
    menuEl.querySelectorAll('.quick-switch-item').forEach(item => item.classList.toggle('active', item.dataset.agent === nextId));
    activeAgentId = nextId;
  }

  btn.onpointerdown = event => {
    event.preventDefault();
    // Menu already open from a tap: this press closes it.
    if (menuOpen) { closeOnUp = true; return; }
    refreshAgents();
    btn.setPointerCapture(event.pointerId);
    holdTimer = setTimeout(openMenu, HOLD_MS);
  };
  btn.onpointermove = event => { if (menuOpen && !closeOnUp) updateHover(event.clientX, event.clientY); };
  btn.onpointerup = () => {
    clearTimeout(holdTimer);
    if (closeOnUp) { closeOnUp = false; closeMenu(); return; }
    if (!menuOpen) { openMenu(); return; }
    const chosen = activeAgentId;
    if (chosen) {
      closeMenu();
      pick(chosen);
    } else {
      // Released on no agent: keep the menu open for a tap.
      btn.classList.remove('holding');
    }
  };
  btn.onpointercancel = () => { clearTimeout(holdTimer); closeOnUp = false; closeMenu(); };
  btn.oncontextmenu = event => event.preventDefault();
}

// Swipe right across a chat -> this agent's conversation list, same as the
// Back button (was: all agents; changed 2026-10-02 because after a Quick jump
// The owner expects the jumped-to agent's list). Only on the message area, never
// the composer, so dragging a text cursor can't trigger it. The chat follows
// the finger for feedback and snaps back unless the swipe is long, fast and
// mostly horizontal.
function wireSwipeBack(agentId) {
  const listHash = `#/agents/${agentId}/conversations`;
  const area = document.querySelector('#chat');
  if (!area) return;
  let startX = 0, startY = 0, startT = 0, tracking = false, horizontal = null;
  const reset = () => { area.style.transition = 'transform .18s ease-out'; area.style.transform = ''; };
  area.addEventListener('touchstart', e => {
    // Code blocks and tables scroll sideways themselves.
    if (e.touches.length !== 1 || e.target.closest?.('pre, table')) { tracking = false; return; }
    // Dragging selection handles is not a swipe.
    if (!(window.getSelection?.().isCollapsed ?? true)) { tracking = false; return; }
    ({clientX: startX, clientY: startY} = e.touches[0]);
    startT = Date.now(); tracking = true; horizontal = null;
    area.style.transition = '';
  }, {passive: true});
  area.addEventListener('touchmove', e => {
    if (!tracking) return;
    if (!(window.getSelection?.().isCollapsed ?? true)) { tracking = false; reset(); return; }
    const dx = e.touches[0].clientX - startX, dy = e.touches[0].clientY - startY;
    if (horizontal === null && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) horizontal = dx > 0 && Math.abs(dx) > Math.abs(dy) * 1.5;
    if (horizontal === false) { tracking = false; return; }
    if (horizontal) area.style.transform = `translateX(${Math.max(0, dx) * 0.6}px)`;
  }, {passive: true});
  area.addEventListener('touchend', e => {
    if (!tracking || !horizontal) { tracking = false; return; }
    tracking = false;
    const dx = e.changedTouches[0].clientX - startX, dy = e.changedTouches[0].clientY - startY;
    if (dx > 90 && Math.abs(dy) < dx * 0.6 && Date.now() - startT < 700) {
      // The list is already the previous entry after a Quick jump: step back
      // to it rather than stacking another copy on top.
      if (history.state?.upTo === listHash) history.back(); else go(listHash);
    }
    else reset();
  });
  area.addEventListener('touchcancel', () => { tracking = false; reset(); });
}

// Cost tier (llm_models.cost_tier, 2026-10-12): a dot in the tier's
// colour plus $/$$/$$$, so it reads without colour too.
const costDot = m => m?.cost_color
  ? `<span class="cost-dot" style="background:${escapeHtml(m.cost_color)}" aria-hidden="true"></span>`
  : '<span class="cost-dot unknown" title="Price unknown" aria-hidden="true"></span>';
const costLabel = m => m?.cost_label ? ` <span class="cost-label" style="color:${escapeHtml(m.cost_color)}">${escapeHtml(m.cost_label)}</span>` : '';

async function renderModelPicker(agent, token) {
  shell(agent.name, `#/agents/${agent.id}/conversations`, '<div class="center-state">Loading models…</div>', undefined, agent);
  let models;
  try { models = await api(`/api/models?agent=${encodeURIComponent(agent.id)}`); }
  catch (error) { if (token === renderToken) state(agent.name, error.message, true); return; }
  if (token !== renderToken) return;
  // One row per model with its columns, sortable by tapping a header
  // (static/model_picker.js, same as the desktop pickers).
  shell(agent.name, `#/agents/${agent.id}/conversations`, `<section class="content model-table"><p class="intro">Model for ${escapeHtml(agent.name)} · tap a column to sort</p><div id="model-table"></div><p class="form-error" id="model-error"></p></section>`, undefined, agent);
  let busy = false;
  document.querySelector('#model-table').appendChild(window.OpenClaveModelPicker.table(models, agent.model_id, async modelId => {
    if (busy) return;
    if (modelId === agent.model_id) { go(`#/agents/${agent.id}/conversations`); return; }
    busy = true;
    try {
      await api(`/api/agents/${agent.id}/model`, {method: 'POST', body: JSON.stringify({model_id: modelId})});
      const picked = models.find(m => m.id === modelId);
      agent.model_id = modelId;
      agent.model_name = picked?.display_name || modelId;
      go(`#/agents/${agent.id}/conversations`);
    } catch (error) {
      busy = false;
      document.querySelector('#model-error').textContent = error.message;
    }
  }));
}

// New agent from the phone, same as Control Panel → New agent (Rick's
// backlog, 2026-10-02): name, purpose, model. The agent then runs its setup
// interview (mission, persona, tools, avatar) in its Onboarding chat, driven
// by its system prompt, so the phone gets every onboarding option desktop has.
// The manager has no default personality or job: the user picks or describes
// one and can change the job (catalog.install persona / mission_extra), the
// same choices as Control Panel → New agent.
function managerChoices(t) {
  return `<div class="mgr-choices">
    ${t.job.length ? `<p class="intro">Its job:</p><ul class="mgr-job-list">${t.job.map(j => `<li>${escapeHtml(j)}</li>`).join('')}</ul>` : ''}
    <label>Personality<select class="mgr-persona"><option value="">Choose…</option>${t.personas.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`).join('')}<option value="own">In my own words…</option></select></label>
    <p class="intro mgr-persona-hint"></p>
    <textarea class="mgr-own" rows="2" maxlength="1000" placeholder="Describe its personality, e.g. dry humour, patient, never uses jargon" hidden></textarea>
    ${t.extras.length ? `<p class="intro">Extra jobs (optional):</p>${t.extras.map(x => `<label class="mgr-extra"><input type="checkbox" value="${escapeHtml(x.id)}"><span><b>${escapeHtml(x.label)}</b> ${escapeHtml(x.summary)}</span></label>`).join('')}` : ''}
    <label>Change or add to its job (optional)<textarea class="mgr-job" rows="2" maxlength="300" placeholder="In your own words"></textarea></label></div>`;
}
// Name ideas for the manager: tapping one fills the name box (no default name).
function nameIdeas(t) {
  return t.name_ideas.length ? `<p class="intro mgr-ideas">Name ideas: ${t.name_ideas.map(n => `<button type="button" class="chip" data-name="${escapeHtml(n)}">${escapeHtml(n)}</button>`).join(' ')}</p>` : '';
}
function managerFields(el) {
  const sel = el.querySelector('.mgr-persona');
  if (!sel) return {};
  return {persona: sel.value === 'own' ? el.querySelector('.mgr-own').value : sel.value,
          mission_extra: el.querySelector('.mgr-job').value,
          extras: [...el.querySelectorAll('.mgr-extra input:checked')].map(c => c.value)};
}

async function renderCreateAgent(token) {
  shell('New agent', '#/agents', '<div class="center-state">Loading models…</div>');
  let models;
  try { models = await api('/api/models'); }
  catch (error) { if (token === renderToken) state('New agent', error.message, true); return; }
  if (token !== renderToken) return;
  const order = ['API · fast', 'API · slow', 'CLI · fast', 'CLI · slow'];
  const groups = new Map(order.map(g => [g, []]));
  for (const m of models) { if (!groups.has(m.group)) groups.set(m.group, []); groups.get(m.group).push(m); }
  const options = [...groups].filter(([, ms]) => ms.length).map(([g, ms]) =>
    `<optgroup label="${escapeHtml(g || 'Other')}">${ms.map(m => `<option value="${escapeHtml(m.id)}"${m.id === 'claude-sonnet-5' ? ' selected' : ''}>${escapeHtml(m.display_name)}${m.cost_label ? ` · ${escapeHtml(m.cost_label)}` : ''}</option>`).join('')}</optgroup>`).join('');
  let templates = [];
  try { ({templates} = await api('/api/catalog')); } catch { /* the blank form still works */ }
  if (token !== renderToken) return;
  const card = t => `<div class="template" data-id="${escapeHtml(t.id)}">
    <div class="template-head">${t.has_avatar ? `<img class="template-av" src="/api/catalog/${encodeURIComponent(t.id)}/avatar" alt="">` : `<span class="template-av" style="background:${escapeHtml(t.color)}">${t.manager ? 'M' : escapeHtml(t.name.slice(0, 2).toUpperCase())}</span>`}
      <span><b>${t.manager ? 'Manager' : escapeHtml(t.name)}</b>${t.installed.length ? '<span class="draft-tag">added</span>' : ''}<small>${escapeHtml(t.tagline)}</small></span></div>
    <p>${escapeHtml(t.summary)}</p>${t.needs ? `<p class="warn">Needs: ${escapeHtml(t.needs)}</p>` : ''}
    ${t.manager ? `<div class="template-add"><input value="" placeholder="Its name" maxlength="24" aria-label="Name"></div>${nameIdeas(t)}${managerChoices(t)}<div class="template-add"><button type="button">Add</button></div>`
      : `<div class="template-add"><input value="${escapeHtml(t.name)}" maxlength="24" aria-label="Name"><button type="button">Add</button></div>`}
    <p class="form-error"></p></div>`;
  const categories = [...new Set(templates.map(t => t.category))];
  // Past about ROSTER_WARN agents, lanes overlap and routing gets harder (Rick's backlog).
  const ROSTER_WARN = 10;
  const rosterWarn = agents.length >= ROSTER_WARN
    ? `<p class="warn roster-warn">You already have ${agents.length} agents. Beyond about ${ROSTER_WARN}, their domains start to overlap and it gets harder to know who handles what. Consider extending or merging an existing agent instead.</p>` : '';
  shell('New agent', '#/agents', `<section class="content"><form class="create-agent" id="create-agent">${rosterWarn}
    <label><span>Model <span id="create-cost"></span></span><select name="model_id">${options}</select></label>
    ${templates.length ? `<h2 class="create-h">Ready-made agents</h2>
    <p class="intro">Each comes with a job and a personality. It then asks you a few questions to fit itself to you.</p>
    ${categories.map(c => `<h3 class="create-cat">${escapeHtml(c)}</h3>${templates.filter(t => t.category === c).map(card).join('')}`).join('')}
    <h2 class="create-h">Or start blank</h2>` : ''}
    <label>Name<input name="name" maxlength="24" autocomplete="off" placeholder="e.g. Chef"></label>
    <label>What is it for?<textarea name="purpose" maxlength="200" rows="3" placeholder="e.g. Weekly meal planning and grocery lists"></textarea></label>
    <p class="intro">It then interviews you in its first chat to set up its mission, personality, tools and avatar.</p>
    <p class="form-error" id="create-error"></p>
    <button type="submit" class="primary">Create</button>
  </form></section>`);
  const opened = created => openOverList(created.id, created.conversation_id
    ? `#/agents/${created.id}/conversations/${created.conversation_id}` : `#/agents/${created.id}/conversations`);
  document.querySelectorAll('.template').forEach(el => {
    const button = el.querySelector('.template-add button'), error = el.querySelector('.form-error');
    const persona = el.querySelector('.mgr-persona');
    el.querySelectorAll('.mgr-ideas [data-name]').forEach(chip => chip.onclick = () => { el.querySelector('.template-add input').value = chip.dataset.name; });
    if (persona) {
      const t = templates.find(x => x.id === el.dataset.id);
      persona.onchange = () => {
        el.querySelector('.mgr-own').hidden = persona.value !== 'own';
        el.querySelector('.mgr-persona-hint').textContent = t.personas.find(p => p.id === persona.value)?.summary || '';
      };
    }
    button.onclick = async () => {
      button.disabled = true; button.textContent = 'Adding…'; error.textContent = '';
      try {
        const created = await api(`/api/catalog/${encodeURIComponent(el.dataset.id)}/install`, {method: 'POST',
          body: JSON.stringify({model_id: document.querySelector('#create-agent').model_id.value, name: el.querySelector('.template-add input').value, ...managerFields(el)})});
        setAgents(await api('/api/agents'));
        opened(created);
      } catch (e) {
        error.textContent = e.message + (/taken/.test(e.message) ? ' Pick another name above.' : '');
        button.disabled = false; button.textContent = 'Add';
      }
    };
  });
  const form = document.querySelector('#create-agent');
  const showCost = () => { const m = models.find(x => x.id === form.model_id.value); document.querySelector('#create-cost').innerHTML = costDot(m) + costLabel(m); };
  form.model_id.onchange = showCost; showCost();
  window.OpenClaveModelPicker?.enhance(form.model_id, models);  // the table picker, as a sheet on the phone
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const error = document.querySelector('#create-error');
    button.disabled = true; button.textContent = 'Creating…'; error.textContent = '';
    try {
      const body = Object.fromEntries(new FormData(form));
      if (!body.name.trim() || !body.purpose.trim()) throw new Error('Give the new agent a name and say what it is for.');
      const created = await api('/api/agents', {method: 'POST', body: JSON.stringify(body)});
      setAgents(await api('/api/agents'));
      // Open the onboarding chat the server already started with the agent's
      // first question (opening /new made a second interview, Ray 2026-10-04).
      opened(created);
    } catch (e) {
      error.textContent = e.message;
      button.disabled = false; button.textContent = 'Create';
    }
  };
}

function state(title, message, retry) {
  shell(title, '#/agents', `<div class="center-state ${retry ? 'error' : ''}">${escapeHtml(message)}${retry ? '<br><button class="retry">Try again</button>' : ''}</div>`);
  if (retry) document.querySelector('.retry').onclick = render;
}

function avatar(agent, size = '') {
  return `<span class="avatar${size ? ` ${size}` : ''}" style="background:${agent.color}"><img src="/api/agents/${agent.id}/avatar${agent.avatar_v ? `?v=${agent.avatar_v}` : ''}" alt=""></span>`;
}

// Unread replies: an agent has one when its latest conversation ends with
// the agent's turn and that happened after the owner last left the agent's
// screens on this phone. "Seen" is stamped on leaving (not entering), so a
// reply that lands after you walk away from a chat still counts as new.
const SEEN_KEY = 'openclave.mobile.seen';
const VISITING_KEY = 'openclave.mobile.visiting';

function loadSeen() {
  try { return JSON.parse(localStorage.getItem(SEEN_KEY) || 'null'); } catch { return null; }
}

function markSeen(agentId) {
  const seen = loadSeen() || {};
  seen[agentId] = Date.now();
  localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
}

// Called on every route change with the agent the new route belongs to (or null).
function trackVisit(agentId) {
  const previous = localStorage.getItem(VISITING_KEY);
  if (previous && previous !== agentId) markSeen(previous);
  if (agentId) {
    localStorage.setItem(VISITING_KEY, agentId);
    window.OpenClaveNotifications?.clearReply(agentId);  // read now: drop its stale "replied" alert
  } else localStorage.removeItem(VISITING_KEY);
}

function seedSeen() {
  // First run with this feature: start with nothing unread rather than every agent.
  if (loadSeen()) return;
  localStorage.setItem(SEEN_KEY, JSON.stringify(Object.fromEntries(agents.map(a => [a.id, Date.now()]))));
}

// An agent is "new" when its latest conversation is: the same rule as the
// conversation list, so the agent list, Quick jump and the chats agree, and
// the light stays until that conversation is opened (2026-10-04).
function hasUnread(agent, seen) {
  if (!agentsFresh) return false;  // drawn from the phone's copy: wait for the server
  if (agent.last_conversation_id) {
    return conversationUnread(agent.id, {id: agent.last_conversation_id, updated_ms: agent.last_activity_ms, last_is_reply: agent.last_is_reply});
  }
  return agent.last_is_reply && agent.last_activity_ms != null && agent.last_activity_ms > (seen[agent.id] ?? 0);
}

// Per conversation: stamped on leaving a chat, so a reply that lands after
// you walk away still counts. A conversation not opened since this started
// on this phone counts from that moment, so old chats don't all light up.
const CONVO_SEEN_KEY = 'openclave.mobile.seen.convo';
const CONVO_BASE_KEY = 'openclave.mobile.seen.convo.base';
function convoBase() {
  let base = Number(localStorage.getItem(CONVO_BASE_KEY));
  if (!base) { base = Date.now(); localStorage.setItem(CONVO_BASE_KEY, String(base)); }
  return base;
}
const VISITING_CONVO_KEY = 'openclave.mobile.visiting.convo';
const loadConvoSeen = () => { try { return JSON.parse(localStorage.getItem(CONVO_SEEN_KEY) || '{}'); } catch { return {}; } };

function trackConversation(agentId, conversationId) {
  const key = agentId && conversationId ? `${agentId}/${conversationId}` : null;
  const previous = localStorage.getItem(VISITING_CONVO_KEY);
  if (previous && previous !== key) {
    const seen = loadConvoSeen();
    seen[previous] = Date.now();
    // Keep it small: the 300 most recent.
    localStorage.setItem(CONVO_SEEN_KEY, JSON.stringify(Object.fromEntries(Object.entries(seen).sort((a, b) => b[1] - a[1]).slice(0, 300))));
  }
  if (key) localStorage.setItem(VISITING_CONVO_KEY, key); else localStorage.removeItem(VISITING_CONVO_KEY);
}

function conversationUnread(agentId, item, convoSeen = loadConvoSeen()) {
  // The chat on screen right now is being read.
  if (localStorage.getItem(VISITING_CONVO_KEY) === `${agentId}/${item.id}`) return false;
  const since = convoSeen[`${agentId}/${item.id}`] ?? convoBase();
  return Boolean(item.last_is_reply && item.updated_ms != null && item.updated_ms > since);
}

const NEW_LIGHT = '<span class="new-tag">new</span>';

// Amber dot after the name of agents with something that needs the owner
// (attention.needs_you: quiet measurement log, open inbox, connection).
// Fetched after the list renders so it never slows the overview down.
async function markAttention(token) {
  try {
    const data = await api('/api/attention');
    if (token !== renderToken) return;
    for (const [id, items] of Object.entries(data.needs_you || data.agents || {})) {
      const title = items.length && document.querySelector(`.row[data-agent="${id}"] .row-title`);
      if (title && !title.querySelector('.attn-dot')) {
        title.insertAdjacentHTML('beforeend', `<span class="attn-dot" role="img" aria-label="Needs attention: ${escapeHtml(items.map(i => i.text).join('; ')).replace(/"/g, '&quot;')}"></span>`);
      }
    }
  } catch { /* optional */ }
}

function renderAgents(token, fresh = false) {
  const seen = loadSeen() || {};
  // Unread first, then most recent conversation; agents with no
  // conversations keep their Control Panel order at the bottom (stable sort).
  const sorted = [...agents].sort((a, b) =>
    (hasUnread(b, seen) - hasUnread(a, seen)) || ((b.last_activity_ms ?? -1) - (a.last_activity_ms ?? -1)));
  const rows = sorted.map(agent => {
    const unread = hasUnread(agent, seen);
    return `<button type="button" class="row" data-agent="${agent.id}" aria-label="Chat with ${escapeHtml(agent.name)}${unread ? ', new message' : ''}; hold for console">
    ${avatar(agent)}<span class="row-main"><span class="row-title">${escapeHtml(agent.name)}${unread ? '<span class="new-tag">new msg</span>' : ''}</span><span class="row-sub">${escapeHtml(agent.tagline)}</span></span><span class="chev">›</span>
  </button>`;
  }).join('');
  const addRow = `<button type="button" class="row add-agent-row" id="add-agent"><span class="avatar add-avatar">+</span><span class="row-main"><span class="row-title">New agent</span><span class="row-sub">Set one up from here</span></span><span class="chev">›</span></button>`;
  const roomRow = `<button type="button" class="row room-row" id="open-room" aria-label="Group chat with all agents"><span class="avatar room-avatar-hash">#</span><span class="row-main"><span class="row-title">everyone</span><span class="row-sub">Group chat with all agents</span></span><span class="chev">›</span></button>`;
  shell('OpenClave', null, `<section class="content"><p class="intro">Who do you want to talk to?</p><div id="clave-switch" class="clave-switch">${claveSwitch()}</div><div class="list">${roomRow}<div id="clave-rows" style="display:contents">${claveRows()}</div>${rows}${addRow}</div></section>`);
  document.querySelector('#open-room').onclick = () => go('#/room');
  Promise.all([api('/api/claves').catch(() => ({enabled: false})), api('/api/channels').catch(() => [])]).then(([data, chans]) => {
    const fresh = data.enabled ? data.claves : [];
    claveOverview = data;
    if (token === renderToken) { mountClaveSwitch(); applyClaveFilter(); }
    if (JSON.stringify(fresh) === JSON.stringify(claveList) && JSON.stringify(chans) === JSON.stringify(channelList)) return;
    claveList = fresh;
    channelList = chans;
    const box = token === renderToken && document.querySelector('#clave-rows');
    if (box) box.innerHTML = claveRows();
  }).catch(() => {});
  // Control Panel button, top left of the overview (2026-10-04): the
  // same /control/ page as desktop, which has a phone layout.
  document.querySelector('.topbar-left').insertAdjacentHTML('beforeend',
    '<a class="icon-btn control-btn" href="/control/?from=mobile" aria-label="Control Panel" title="Control Panel"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg></a>');
  document.querySelector('#add-agent').onclick = () => go('#/create-agent');
  markAttention(token);
  mountClaveSwitch();
  applyClaveFilter();

  document.querySelectorAll('[data-agent]').forEach(row => {
    const agent = byId(row.dataset.agent);
    let holdTimer, held = false, startX = 0, startY = 0;
    row.onpointerdown = event => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      startX = event.clientX; startY = event.clientY;
      held = false;
      holdTimer = setTimeout(() => { held = true; go(`#/agents/${agent.id}/console`); }, 350);
    };
    row.onpointermove = event => {
      if (Math.abs(event.clientX - startX) > 10 || Math.abs(event.clientY - startY) > 10) clearTimeout(holdTimer);
    };
    row.onpointerup = row.onpointercancel = () => clearTimeout(holdTimer);
    row.oncontextmenu = event => { event.preventDefault(); if (!held) { held = true; go(`#/agents/${agent.id}/console`); } };
    row.onclick = () => { if (held) { held = false; return; } go(`#/agents/${agent.id}/conversations`); };
  });

  // Draw from what we have straight away, then once more with fresh
  // activity so replies that arrived since the last fetch show up.
  if (!fresh) {
    api('/api/agents').then(list => {
      if (token !== renderToken) return;
      setAgents(list);
      renderAgents(token, true);
    }).catch(() => { /* keep the list as drawn */ });
  }
}

// Sliders configured by an agent's page.json.
async function drawSliders(agent, container, block, token) {
  const metrics = block.metrics;
  const n = container.dataset.block || 's';
  container.insertAdjacentHTML('beforeend', `${block.title ? `<h2>${escapeHtml(block.title)}</h2>` : ''}<form class="sliders-log">${metrics.map(m=>`<label class="metric-control">${escapeHtml(m.label)} <output data-out="${m.id}">${m.min}</output>${escapeHtml(m.unit||'')}<input type="range" name="${m.id}" min="${m.min}" max="${m.max}" step="${m.step}" value="${m.min}"></label>`).join('')}<button class="primary">${escapeHtml(block.button || 'Log measurements')}</button><p class="log-status" id="log-status-${n}" role="status"></p></form>`);
  const form = container.querySelector('form.sliders-log:last-of-type');
  const status = form.querySelector('.log-status');
  for (const metric of metrics) {
    const slider = form.elements[metric.id];
    slider.oninput = () => {form.querySelector(`[data-out="${metric.id}"]`).textContent = slider.value;};
  }
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('button'); button.disabled = true;
    status.textContent = 'Saving…';
    try {
      await api(`/${agent.id}/tracking/batch`, {method:'POST',body:JSON.stringify({section: block.section || 'Progress', entries:metrics.map(m=>({metric:m.id,value:String(form.elements[m.id].value),unit:m.unit}))})});
      status.textContent = 'Logged.';
    } catch(error) {status.textContent = error.message;}
    finally {button.disabled = false;}
  };
  try {
    const tracking = await api(`/${agent.id}/tracking?section=${encodeURIComponent(block.section || 'Progress')}`);
    if (token !== renderToken) return;
    for (const metric of metrics) {
      const latest = tracking.entries.find(e=>e.metric===metric.id);
      if (latest?.value != null) {form.elements[metric.id].value=parseFloat(latest.value);form.elements[metric.id].oninput();}
    }
  } catch { /* defaults remain editable */ }
}

// The agent changed its page since the owner last kept one (page_history.py):
// it shows, with Keep / Revert on top, until he decides.
function drawPageChanged(agent, changed, body, token) {
  if (!changed) return;
  body.insertAdjacentHTML('afterbegin', `<div class="page-changed" role="status"><span>${escapeHtml(agent.name)} changed this page.</span><button type="button" class="link-btn" data-page-keep>Keep</button><button type="button" class="link-btn" data-page-revert>Revert</button></div>`);
  const bar = body.querySelector('.page-changed');
  bar.querySelector('[data-page-keep]').onclick = async () => {
    try { await api(`/${agent.id}/page/keep`, {method:'POST'}); bar.remove(); }
    catch (error) { window.alert(error.message); }
  };
  bar.querySelector('[data-page-revert]').onclick = async () => {
    if (!window.confirm(`Go back to the page you last kept?`)) return;
    try { await api(`/${agent.id}/page/revert`, {method:'POST'}); if (token === renderToken) renderConsole(agent, ++renderToken); }
    catch (error) { window.alert(error.message); }
  };
}

// The maintainer's weekly cleanup (cleanup.py) lives on their page (2026-10-14;
// it started in the Control Panel): the same Archive / Delete / Rename / Keep
// buttons, editable names and restore. Nothing changes until a button here.
const CLEANUP_AGENT = 'maintainer';
const CLEANUP_KIND = {archive: 'Archive', delete: 'Delete', rename: 'Rename'};

async function drawCleanup(body, token) {
  const box = document.createElement('section');
  box.className = 'cleanup-box';
  box.id = 'cleanup';
  box.innerHTML = '<h2>Weekly cleanup</h2><p class="muted">Loading…</p>';
  body.prepend(box);
  const post = (path, payload) => api(path, {method: 'POST', body: JSON.stringify(payload)});
  const draw = data => {
    if (token !== renderToken) return;
    const items = [...data.suggestions].sort((a, b) => a.agent_name.localeCompare(b.agent_name) || a.kind.localeCompare(b.kind));
    const when = data.generated_at ? `Checked ${escapeHtml(data.generated_at.replace('T', ' ').slice(0, 16))}` : 'Not checked yet';
    box.innerHTML = `<h2>Weekly cleanup</h2>
      <p class="muted cleanup-when">${when} · <button type="button" class="link-btn" data-check>Check now</button></p>
      ${data.agents.map(n => `<p class="cleanup-note"><b>${escapeHtml(n.agent_name)}</b>: ${escapeHtml(n.text)}</p>`).join('')}
      <div class="cleanup-list">${items.map(c => `<div class="page-card cleanup-item" data-id="${escapeHtml(c.id)}">
        <div class="cleanup-head"><span class="cleanup-kind ${escapeHtml(c.kind)}">${CLEANUP_KIND[c.kind] || escapeHtml(c.kind)}</span><span class="cleanup-agent">${escapeHtml(c.agent_name)}</span></div>
        <button type="button" class="page-card-title cleanup-peek" data-peek="${escapeHtml(c.agent_id)}/${escapeHtml(c.conversation_id)}" data-name="${escapeHtml(c.agent_name)}" aria-expanded="false" title="Read the chat before deciding">${escapeHtml(c.title)} <span class="muted">▸ read</span></button>
        <div class="cleanup-transcript" hidden></div>
        <div class="page-card-meta">${escapeHtml(c.reason)}</div>
        ${c.kind === 'rename' ? `<input class="page-form-input cleanup-name" type="text" maxlength="120" aria-label="New name" value="${escapeHtml(c.suggested_title || '')}">` : ''}
        <div class="cleanup-actions"><button type="button" class="cleanup-apply ${escapeHtml(c.kind)}" data-apply>${CLEANUP_KIND[c.kind] || 'Apply'}</button><button type="button" class="cleanup-keep" data-keep>Keep</button></div>
        <p class="cleanup-error" role="status"></p></div>`).join('') || '<p class="muted">Nothing to tidy.</p>'}</div>
      <details class="cleanup-archived"><summary>Archived conversations (${data.archived.length})</summary>${data.archived.map(c => `<div class="cleanup-archived-row"><span><b>${escapeHtml(c.agent_name)}</b> ${escapeHtml(c.title)} <span class="muted">${escapeHtml(c.date)}</span></span><button type="button" class="link-btn" data-restore="${escapeHtml(c.agent_id)}/${escapeHtml(c.id)}">Restore</button></div>`).join('') || '<p class="muted">None.</p>'}</details>`;
    // Read a suggested chat in place before Archive/Delete/Rename: the
    // report and any half-typed name stay as they are (backlog 2026-10-20).
    box.querySelectorAll('[data-peek]').forEach(btn => btn.onclick = async () => {
      const panel = btn.nextElementSibling, open = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', String(!open));
      btn.querySelector('.muted').textContent = open ? '▸ read' : '▾ hide';
      panel.hidden = open;
      if (open || panel.dataset.loaded) return;
      panel.textContent = 'Loading…';
      const [agentId, conversationId] = btn.dataset.peek.split('/');
      try {
        const convo = await api(`/${encodeURIComponent(agentId)}/conversations/${encodeURIComponent(conversationId)}`);
        const turns = convo.turns || [];
        panel.innerHTML = (turns.length > 12 ? `<p class="muted">… ${turns.length - 12} earlier messages</p>` : '')
          + turns.slice(-12).map(t => `<div class="cleanup-turn ${t.role === 'user' ? 'user' : 'agent'}"><b>${t.role === 'user' ? 'You' : escapeHtml(btn.dataset.name)}</b> ${markdown(t.content)}</div>`).join('')
          || '<p class="muted">Empty chat.</p>';
        panel.dataset.loaded = '1';
      } catch (error) { panel.textContent = error.message || 'Could not load the chat.'; }
    });
    box.querySelector('[data-check]').onclick = async event => {
      event.target.disabled = true; event.target.textContent = 'Checking…';
      try { draw(await post('/api/cleanup/check', {})); } catch (error) { event.target.textContent = error.message; }
    };
    box.querySelectorAll('.cleanup-item').forEach(card => {
      const item = items.find(c => c.id === card.dataset.id), error = card.querySelector('.cleanup-error');
      const act = async (button, path, payload) => {
        button.disabled = true; error.textContent = '';
        try { await post(path, payload); draw(await api('/api/cleanup')); }
        catch (err) { error.textContent = err.message || 'Failed.'; button.disabled = false; }
      };
      card.querySelector('[data-apply]').onclick = event => {
        if (item.kind === 'delete' && !window.confirm(`Delete "${item.title}" for good?`)) return;
        const name = card.querySelector('.cleanup-name');
        if (name && !name.value.trim()) { error.textContent = 'Type a name first.'; name.focus(); return; }
        act(event.target, '/api/cleanup/apply', {id: item.id, title: name ? name.value.trim() : null});
      };
      card.querySelector('[data-keep]').onclick = event => act(event.target, '/api/cleanup/dismiss', {id: item.id});
    });
    box.querySelectorAll('[data-restore]').forEach(button => button.onclick = async () => {
      const [agent_id, conversation_id] = button.dataset.restore.split('/');
      button.disabled = true;
      try { await post('/api/cleanup/unarchive', {agent_id, conversation_id}); draw(await api('/api/cleanup')); }
      catch (error) { window.alert(error.message); button.disabled = false; }
    });
  };
  try { draw(await api('/api/cleanup')); }
  catch (error) { if (token === renderToken) box.querySelector('p').textContent = error.message || 'Could not load the cleanup.'; }
}

// The manager's proposals (proposals.py): what it wants to do, mostly from
// the weekly audit (audit.py): notes worth keeping in the main vault, nudges
// for other agents, tasks. Yes does it, No drops it (and it won't come back
// soon), Discuss opens the Audit chat with a draft. A note can be edited
// before Yes. Shown right under the cleanup on the manager's page.
const PROPOSAL_KIND = {vault_note: 'To your vault', message_agent: 'Message', task: 'Task', model: 'Model'};
async function drawProposals(agent, body, token) {
  const box = document.createElement('section');
  box.className = 'cleanup-box proposals-box';
  box.innerHTML = `<h2>${escapeHtml(agent.name)} proposes</h2><p class="muted">Loading…</p>`;
  // Right under the cleanup, which stays at the top of the page (layout rule).
  const cleanupBox = body.querySelector('#cleanup');
  if (cleanupBox) cleanupBox.after(box); else body.prepend(box);
  const post = (path, payload) => api(path, {method: 'POST', body: JSON.stringify(payload || {})});
  const target = p => p.kind === 'vault_note' ? `${p.mode === 'create' ? 'New note' : 'Add to'} <b>${escapeHtml(p.target)}</b>`
    : p.kind === 'message_agent' ? `To <b>${escapeHtml(p.agent_name || p.agent)}</b>` : '';
  const draw = data => {
    if (token !== renderToken) return;
    const items = data.proposals;
    box.innerHTML = `<h2>${escapeHtml(agent.name)} proposes</h2>
      <p class="muted cleanup-when">${items.length ? `${items.length} waiting for you` : 'Nothing waiting.'} · <button type="button" class="link-btn" data-audit>Audit now</button></p>
      <div class="cleanup-list">${items.map(p => `<div class="page-card cleanup-item proposal" data-id="${escapeHtml(p.id)}">
        <div class="cleanup-head"><span class="cleanup-kind ${escapeHtml(p.kind)}">${PROPOSAL_KIND[p.kind] || escapeHtml(p.kind)}</span><span class="cleanup-agent">${escapeHtml(p.created || '')}</span></div>
        <div class="page-card-title">${escapeHtml(p.title)}</div>
        ${p.why ? `<div class="page-card-meta">${escapeHtml(p.why)}</div>` : ''}
        ${target(p) ? `<div class="page-card-meta">${target(p)}</div>` : ''}
        ${p.kind === 'model' ? `<div class="proposal-text">${markdown(p.content)}</div>` : `<details class="proposal-content"><summary>${p.kind === 'task' ? 'What exactly' : 'Show the text'}</summary>
          ${p.kind === 'task' ? `<div class="proposal-text">${markdown(p.content)}</div>` : `<textarea class="page-form-input proposal-edit" rows="8" aria-label="Text">${escapeHtml(p.content)}</textarea><p class="muted">You can edit it before Yes.</p>`}
          ${p.source ? `<p class="muted">From ${escapeHtml(p.source)}</p>` : ''}</details>`}
        <div class="cleanup-actions"><button type="button" class="cleanup-apply" data-yes>Yes</button><button type="button" class="cleanup-keep" data-no>No</button><button type="button" class="cleanup-keep" data-discuss>Discuss</button></div>
        <p class="cleanup-error" role="status"></p></div>`).join('')}</div>`;
    box.querySelector('[data-audit]').onclick = async event => {
      event.target.disabled = true; event.target.textContent = 'Starting…';
      try { await post('/api/audit/run'); event.target.textContent = `Started: ${agent.name} replies in the Audit chat`; }
      catch (error) { event.target.textContent = error.message; }
    };
    box.querySelectorAll('.proposal').forEach(card => {
      const p = items.find(x => x.id === card.dataset.id), error = card.querySelector('.cleanup-error');
      const act = async (button, path, payload) => {
        button.disabled = true; error.textContent = '';
        try { await post(path, payload); draw(await api('/api/proposals')); }
        catch (err) { error.textContent = err.message || 'Failed.'; button.disabled = false; }
      };
      card.querySelector('[data-yes]').onclick = event => {
        const edit = card.querySelector('.proposal-edit');
        act(event.target, `/api/proposals/${p.id}/approve`, edit && edit.value !== p.content ? {content: edit.value} : {});
      };
      card.querySelector('[data-no]').onclick = event => {
        const note = window.prompt('No. Want to say why? (optional; it helps next time)');
        if (note === null) return;
        act(event.target, `/api/proposals/${p.id}/decline`, {note: note.trim() || null});
      };
      card.querySelector('[data-discuss]').onclick = async () => {
        try {
          const where = await post(`/api/proposals/${p.id}/discuss`);
          const key = draftKey(where.agent_id, where.conversation_id);
          if (!localStorage.getItem(key)?.trim()) localStorage.setItem(key, where.draft);
          go(`#/agents/${where.agent_id}/conversations/${where.conversation_id}`);
        } catch (err) { error.textContent = err.message; }
      };
    });
  };
  try { draw(await api('/api/proposals')); }
  catch (error) { if (token === renderToken) box.querySelector('p').textContent = error.message || 'Could not load the proposals.'; }
}

async function renderConsole(agent, token) {
  await drawConsoleBody(agent, token);
  const body = document.querySelector('#console-body');
  if (agent.roles?.includes(CLEANUP_AGENT) && token === renderToken && body) {
    if (body.classList.contains('center-state')) body.className = '';
    await drawCleanup(body, token);
    if (token === renderToken) await drawProposals(agent, body, token);
  }
}

async function drawConsoleBody(agent, token) {
  const header = `<div class="console-actions"><button id="console-chat">Open chat</button><button id="console-files">Files</button><button id="console-tools">Tools</button><button id="console-skills">Skills</button><button id="console-recaps">Recaps</button></div>`;
  shell(agent.name, '#/agents', `<section class="content">${header}<div id="console-body" class="center-state">Loading…</div></section>`, undefined, agent);
  document.querySelector('#console-chat').onclick = () => go(`#/agents/${agent.id}/conversations`);
  document.querySelector('#console-files').onclick = () => go(`#/agents/${agent.id}/files`);
  document.querySelector('#console-tools').onclick = () => go(`#/agents/${agent.id}/tools`);
  document.querySelector('#console-skills').onclick = () => go(`#/agents/${agent.id}/skills`);
  document.querySelector('#console-recaps').onclick = () => go(`#/agents/${agent.id}/recaps`);
  // Claves button only while "Multiple claves" is on.
  api(`/api/agents/${agent.id}/claves`).then(data => {
    const row = token === renderToken && data.enabled && document.querySelector('.console-actions');
    if (!row || row.querySelector('#console-claves')) return;
    row.insertAdjacentHTML('beforeend', '<button id="console-claves">Claves</button>');
    row.querySelector('#console-claves').onclick = () => go(`#/agents/${agent.id}/claves`);
  }).catch(() => {});
  const body = document.querySelector('#console-body');
  // The agent's own page (page.json, custom_pages.py) wins over the built-in ones below.
  let page = null, changed = null;
  try { ({page, changed} = await api(`/${agent.id}/page`)); } catch { /* fall back to the built-in page */ }
  if (token !== renderToken) return;
  if (page) { drawCustomPage(agent, page, body, token); drawPageChanged(agent, changed, body, token); return; }
  body.innerHTML = `<p>Nothing here.</p><button class="primary" id="setup-console">Create new custom page?</button>`;
  body.querySelector('#setup-console').onclick = () => askForPage(agent, 'create');
  drawPageChanged(agent, changed, body, token);
}

// Tools (2026-10-16): tick this agent's optional tools on/off, the same
// list and API as the Control Panel. Every tick saves at once and applies to
// the agent's next message. Desktop shows this screen in its Tools panel.
let toolCatalog = null;
async function renderTools(agent, token) {
  const back = `#/agents/${agent.id}/console`;
  shell(`${agent.name} · Tools`, back, '<div class="center-state">Loading tools…</div>', undefined, agent);
  let state;
  try {
    toolCatalog ??= await api('/api/tools');
    state = await api(`/api/agents/${agent.id}/tools`);
  } catch (error) {
    if (token === renderToken) document.querySelector('.center-state').textContent = error.message || 'Could not load the tools.';
    return;
  }
  if (token !== renderToken) return;
  const summaries = Object.fromEntries(toolCatalog.optional.map(o => [o.name, o.summary]));
  const label = name => name.replace(/_/g, ' ');
  const conn = (state.connections || []).map(c => !c.ready ? `${escapeHtml(c.name)}: not set up yet`
    : c.available ? `${escapeHtml(c.name)} ✓` : `<span class="warn">${escapeHtml(c.name)}: not on this model. ${escapeHtml(state.connection_hint || '')}</span>`).join(' · ');
  const rows = state.available.map(n => `<label class="tool-row"><input type="checkbox" value="${escapeHtml(n)}"${state.selected.includes(n) ? ' checked' : ''}><span><b>${escapeHtml(label(n))}</b><small>${escapeHtml(summaries[n] || '')}</small></span></label>`).join('');
  shell(`${agent.name} · Tools`, back, `<section class="content tools-screen">
    ${state.onboarding ? `<p class="intro">${escapeHtml(agent.name)} is still onboarding and will pick its own tools; you can change them here afterwards.</p>` : ''}
    <p class="intro">Ticked tools apply from ${escapeHtml(agent.name)}'s next message. <span id="tools-status" role="status"></span></p>
    <div class="tool-list">${rows}</div>
    <p class="tools-note">Writes: ${escapeHtml(state.writes || '')}${conn ? `<br>Connections: ${conn}` : ''}</p>
    <details class="tools-note"><summary>Always on (${toolCatalog.core.length})</summary>${toolCatalog.core.map(t => escapeHtml(label(t))).join(', ')}</details>
  </section>`, undefined, agent);
  const status = document.querySelector('#tools-status');
  const boxes = [...document.querySelectorAll('.tool-row input')];
  let saving = Promise.resolve();
  boxes.forEach(box => box.onchange = () => {
    // Saves run one after another, so quick ticks can't overwrite each other.
    saving = saving.then(async () => {
      status.textContent = 'Saving…';
      try {
        const {selected} = await api(`/api/agents/${agent.id}/tools`, {method: 'POST', body: JSON.stringify({tools: boxes.filter(b => b.checked).map(b => b.value)})});
        boxes.forEach(b => { b.checked = selected.includes(b.value); });
        status.textContent = 'Saved';
      } catch (error) {
        box.checked = !box.checked;
        status.textContent = error.message || 'Could not save';
      }
    });
  });
}

// Claves (claves.py): which clave this agent is in (one; the manager is in
// all). The phone's stand-in for the desktop overview's drag and drop.
async function renderAgentClaves(agent, token) {
  const back = `#/agents/${agent.id}/console`;
  shell(`${agent.name} · Claves`, back, '<div class="center-state">Loading…</div>', undefined, agent);
  let data;
  try { data = await api(`/api/agents/${agent.id}/claves`); } catch (error) {
    if (token === renderToken) document.querySelector('.center-state').textContent = error.message || 'Could not load the claves.';
    return;
  }
  if (token !== renderToken) return;
  const rows = data.manager ? '' : data.claves.map(c => `<label class="tool-row"><input type="radio" name="clave" value="${escapeHtml(c.id)}"${data.selected === c.id ? ' checked' : ''}><span><b>${escapeHtml(c.name)}</b></span></label>`).join('');
  shell(`${agent.name} · Claves`, back, `<section class="content tools-screen">
    <p class="intro">${!data.enabled ? 'Multiple claves is off. Turn it on in the Claves view on the desktop home page.'
      : data.manager ? `${escapeHtml(agent.name)} is the manager and is in every clave.`
      : `The clave ${escapeHtml(agent.name)} is in. Picking another moves it there. <span id="claves-status" role="status"></span>`}</p>
    <div class="tool-list">${rows || (data.manager ? '' : '<p class="intro">No claves yet.</p>')}</div>
    <p class="tools-note">Create and rename claves, set leads and clone agents into another clave in the Claves view on the desktop home page.</p>
  </section>`, undefined, agent);
  const status = document.querySelector('#claves-status');
  const boxes = [...document.querySelectorAll('.tool-row input')];
  let saving = Promise.resolve();
  boxes.forEach(box => box.onchange = () => {
    saving = saving.then(async () => {
      if (status) status.textContent = 'Saving…';
      try {
        const {selected} = await api(`/api/agents/${agent.id}/claves`, {method: 'POST', body: JSON.stringify({clave: box.value})});
        boxes.forEach(b => { b.checked = b.value === selected; });
        data.selected = selected;
        claveList = [];  // members changed; the list reloads them
        if (status) status.textContent = 'Saved';
      } catch (error) {
        boxes.forEach(b => { b.checked = b.value === data.selected; });
        if (status) status.textContent = error.message || 'Could not save';
      }
    });
  });
}

// Skills (skills.py): the know-how this agent can load. Its own skills and
// the shared ones, each switched on/off for this agent; imported skills stay
// off until enabled after a look at what they expect. Same API on desktop,
// which shows this screen in its Skills tab.
async function renderSkills(agent, token) {
  const back = `#/agents/${agent.id}/console`;
  const title = `${agent.name} · Skills`;
  shell(title, back, '<div class="center-state">Loading skills…</div>', undefined, agent);
  let list;
  try { ({skills: list} = await api(`/api/agents/${agent.id}/skills`)); }
  catch (error) { if (token === renderToken) document.querySelector('.center-state').textContent = error.message || 'Could not load the skills.'; return; }
  if (token !== renderToken) return;
  const tag = s => s.scope === 'own' ? `${escapeHtml(agent.name)}'s own` : 'shared';
  const row = s => `<div class="skill" data-key="${escapeHtml(s.key)}">
    <label class="tool-row"><input type="checkbox"${s.enabled ? ' checked' : ''}${s.approved ? '' : ' disabled'}><span><b>${escapeHtml(s.name)}<span class="draft-tag">${tag(s)}</span></b><small>${escapeHtml(s.description || 'No description')}</small>
    ${s.warnings.length ? `<small class="warn">Expects: ${escapeHtml(s.warnings.join('; '))}</small>` : ''}
    ${s.source ? `<small>Imported ${escapeHtml(s.imported || '')} from ${escapeHtml(s.source)}</small>` : ''}</span></label>
    <div class="skill-actions">${s.approved ? '' : '<button type="button" data-act="approve">Review &amp; enable</button>'}<button type="button" data-act="view">Read</button><button type="button" data-act="delete">Delete</button></div>
    <pre class="skill-text" hidden></pre></div>`;
  shell(title, back, `<section class="content tools-screen">
    <p class="intro">${escapeHtml(agent.name)} sees the names and descriptions of ticked skills, and loads one when a request matches. <span id="tools-status" role="status"></span></p>
    <div class="tool-list">${list.map(row).join('') || '<p class="tools-note">No skills yet. Ask ' + escapeHtml(agent.name) + ' to save one after a job, or import one below.</p>'}</div>
    <details class="tools-note skill-import"><summary>Import a skill</summary>
      <p>From OpenClaw, Claude or anywhere with a <code>SKILL.md</code>: a link to the file or a GitHub folder, a folder on this server, or pasted text. Skills that need a shell, scripts or other tools are flagged, and every import starts switched off.</p>
      <input id="skill-source" placeholder="https://github.com/…/skills/name  or  /path/to/skill">
      <textarea id="skill-text" rows="5" placeholder="…or paste SKILL.md here"></textarea>
      <select id="skill-target"><option value="${escapeHtml(agent.id)}">For ${escapeHtml(agent.name)} only</option><option value="shared">For all agents</option></select>
      <button type="button" class="primary" id="skill-check">Check it</button>
      <div id="skill-report"></div>
    </details>
  </section>`, undefined, agent);
  const status = document.querySelector('#tools-status');
  const path = key => `/api/agents/${agent.id}/skills/${key}`;
  document.querySelectorAll('.skill').forEach(el => {
    const key = el.dataset.key, s = list.find(x => x.key === key);
    const box = el.querySelector('input');
    box.onchange = async () => {
      status.textContent = 'Saving…';
      try { await api(path(key), {method: 'POST', body: JSON.stringify({on: box.checked})}); status.textContent = 'Saved'; }
      catch (error) { box.checked = !box.checked; status.textContent = error.message || 'Could not save'; }
    };
    const showText = async () => {
      const pre = el.querySelector('.skill-text');
      if (pre.hidden && !pre.textContent) pre.textContent = (await api(path(key))).text;
      pre.hidden = false;
    };
    el.querySelector('[data-act="view"]').onclick = async () => {
      const pre = el.querySelector('.skill-text');
      if (!pre.hidden) { pre.hidden = true; return; }
      try { await showText(); } catch (error) { status.textContent = error.message; }
    };
    const approve = el.querySelector('[data-act="approve"]');
    if (approve) approve.onclick = async () => {
      try { await showText(); } catch {}
      const warn = s.warnings.length ? `\n\nIt expects: ${s.warnings.join('; ')}.\n${agent.name} will use only what it can do with its own tools.` : '';
      if (!confirm(`Enable "${s.name}"? Its text becomes instructions ${agent.name} follows; read it first (shown below).${warn}`)) return;
      try { await api(path(key), {method: 'POST', body: JSON.stringify({approve: true})}); renderSkills(agent, renderToken); }
      catch (error) { status.textContent = error.message; }
    };
    el.querySelector('[data-act="delete"]').onclick = async () => {
      if (!confirm(`Delete the skill "${s.name}"${s.scope === 'shared' ? ' for all agents' : ''}?`)) return;
      try { await api(path(key), {method: 'DELETE'}); renderSkills(agent, renderToken); }
      catch (error) { status.textContent = error.message; }
    };
  });
  const report = document.querySelector('#skill-report');
  const payload = extra => ({source: document.querySelector('#skill-source').value, text: document.querySelector('#skill-text').value,
    target: document.querySelector('#skill-target').value, ...extra});
  document.querySelector('#skill-check').onclick = async () => {
    report.textContent = 'Checking…';
    try {
      const r = await api('/api/skills/import', {method: 'POST', body: JSON.stringify(payload({preview: true}))});
      report.innerHTML = `<p><b>${escapeHtml(r.name)}</b>: ${escapeHtml(r.description || 'no description')}</p>
        ${r.warnings.length ? `<p class="warn">Expects things ${escapeHtml(agent.name)} may not have: ${escapeHtml(r.warnings.join('; '))}. Know-how still transfers; steps that need these won't.</p>` : '<p>Plain know-how: nothing it needs is missing.</p>'}
        ${r.files.length ? `<p>Files: ${escapeHtml(r.files.join(', '))}</p>` : ''}
        ${r.exists ? '<p class="warn">A skill with this name exists there; importing replaces it.</p>' : ''}
        <button type="button" class="primary" id="skill-import">Import (switched off)</button>`;
      document.querySelector('#skill-import').onclick = async () => {
        try { await api('/api/skills/import', {method: 'POST', body: JSON.stringify(payload({replace: r.exists}))}); renderSkills(agent, renderToken); }
        catch (error) { report.textContent = error.message; }
      };
    } catch (error) { report.textContent = error.message; }
  };
}

// Recaps (recaps.py): archived chats as a compact list: name, how it ended,
// last active. A tap opens it in the chat view (chat_core shows its recap on
// top and the transcript below); writing a message brings it back. Desktop
// has the same list in its sidebar (dashboard_common loadRecaps).
const RECAP_STATUS = {concluded: ['Concluded', 'done'], open: ['Left open', 'open'], dropped: ['Dropped', 'dropped']};
async function renderRecaps(agent, token) {
  const back = `#/agents/${agent.id}/console`;
  const title = `${agent.name} · Recaps`;
  shell(title, back, '<div class="center-state">Loading recaps…</div>', undefined, agent);
  let data;
  try { data = await api(`/api/agents/${agent.id}/recaps`); }
  catch (error) { if (token === renderToken) document.querySelector('.center-state').textContent = error.message || 'Could not load the recaps.'; return; }
  if (token !== renderToken) return;
  const row = r => {
    const [label, cls] = RECAP_STATUS[r.status] || ['No recap yet', 'none'];
    return `<button class="row recap-row" data-id="${escapeHtml(r.id)}"><span class="row-main"><span class="row-title">${escapeHtml(r.title)}</span><span class="row-sub"><span class="recap-status ${cls}">${r.pending ? 'Writing…' : label}</span> ${escapeHtml(r.last_active)}</span></span><span class="chev">›</span></button>`;
  };
  shell(title, back, `<section class="content recaps-screen">
    ${data.recaps.map(row).join('') || '<p class="tools-note">Nothing archived yet. Archive a chat from its actions (long-press or swipe it in the list).</p>'}
  </section>`, undefined, agent);
  document.querySelectorAll('.recap-row').forEach(el => el.onclick = () => go(`#/agents/${agent.id}/conversations/${el.dataset.id}`));
  if (data.recaps.some(r => r.pending)) setTimeout(() => { if (token === renderToken) renderRecaps(agent, ++renderToken); }, 5000);
}

// Files (2026-10-12): this agent's own folder only, like the desktop
// Files tab and through the same /<agent>/files API, which checks every path
// against the folder and refuses a save if the file changed meanwhile.
const CONFIRM_SAVE = new Set(['mission.md', 'persona.md']);
const fileHash = (agent, path) => `#/agents/${agent.id}/files/${encodeURIComponent(path)}`;

async function renderFiles(agent, token) {
  shell(`${agent.name} · Files`, `#/agents/${agent.id}/console`, '<div class="center-state">Loading files…</div>', undefined, agent);
  let files;
  try { files = await api(`/${agent.id}/files`); }
  catch (error) { if (token === renderToken) state(agent.name, error.message, true); return; }
  if (token !== renderToken) return;
  const rows = files.map(f => `<button type="button" class="row file-row" data-path="${escapeHtml(f.path)}"><span class="row-main"><span class="row-title">${escapeHtml(f.path)}${f.protected ? '<span class="draft-tag">core</span>' : ''}</span><span class="row-sub">${escapeHtml(f.modified)} · ${f.size < 1024 ? `${f.size} B` : `${(f.size / 1024).toFixed(1)} KB`}</span></span><span class="chev">›</span></button>`).join('');
  shell(`${agent.name} · Files`, `#/agents/${agent.id}/console`, `<section class="content"><p class="intro">${escapeHtml(agent.name)}'s folder</p><div class="list">${rows || '<div class="center-state">No files yet.</div>'}</div></section>`, undefined, agent);
  document.querySelectorAll('.file-row').forEach(row => row.onclick = () => go(fileHash(agent, row.dataset.path)));
}

async function renderFileEditor(agent, path, token) {
  const back = `#/agents/${agent.id}/files`;
  shell(path.split('/').pop(), back, '<div class="center-state">Loading…</div>', undefined, agent);
  let file;
  try { file = await api(`/${agent.id}/files/content?path=${encodeURIComponent(path)}`); }
  catch (error) { if (token === renderToken) state(path, error.message, false); return; }
  if (token !== renderToken) return;
  // Markdown files open formatted (Rick's backlog); Edit shows the text.
  const isMarkdown = /\.md$/i.test(path);
  shell(path.split('/').pop(), back, `<section class="content file-editor"><p class="intro">${escapeHtml(path)}</p>
    <div class="md-view file-view" hidden></div>
    <textarea class="file-text" spellcheck="false" autocapitalize="off" aria-label="${escapeHtml(path)}"></textarea>
    <div class="file-bar">${isMarkdown ? '<button type="button" class="file-reload" id="file-mode">Edit</button>' : ''}<button type="button" class="primary" id="file-save">Save</button><button type="button" class="file-reload" id="file-reload">Reload</button></div>
    <p class="file-status" role="status"></p></section>`, undefined, agent);
  const text = document.querySelector('.file-text'), status = document.querySelector('.file-status');
  const save = document.querySelector('#file-save'), view = document.querySelector('.file-view');
  const modeBtn = document.querySelector('#file-mode');
  const setMode = mode => {
    const showView = isMarkdown && mode === 'view';
    if (showView) view.innerHTML = markdown(text.value.replace(/^---\n[\s\S]*?\n---\n?/, ''));
    view.hidden = !showView; text.hidden = showView; save.hidden = showView;
    if (modeBtn) modeBtn.textContent = showView ? 'Edit' : 'Preview';
  };
  if (modeBtn) modeBtn.onclick = () => setMode(view.hidden ? 'view' : 'edit');
  text.value = file.content;
  setMode('view');
  let version = file.version, saved = file.content;
  const say = (msg, kind = '') => { status.className = `file-status ${kind}`; status.textContent = msg; };
  text.oninput = () => { if (status.classList.contains('ok')) say(''); };
  save.onclick = async () => {
    if (text.value === saved) { say('No changes.'); return; }
    if (!path.includes('/') && CONFIRM_SAVE.has(path.toLowerCase())
        && !window.confirm(`Save ${path}? It changes how ${agent.name} works.`)) return;
    save.disabled = true; say('Saving…');
    try {
      const result = await api(`/${agent.id}/files/content`, {method: 'PUT', body: JSON.stringify({path, content: text.value, version})});
      version = result.version; saved = result.content;
      say('Saved ✓', 'ok');
    } catch (error) {
      say(error.status === 409 ? `${agent.name} changed this file since you opened it. Your text is still here: copy what you need, then Reload.` : error.message, 'error');
    } finally { save.disabled = false; }
  };
  document.querySelector('#file-reload').onclick = async () => {
    if (text.value !== saved && !window.confirm('Drop your unsaved changes and load the file again?')) return;
    try {
      const fresh = await api(`/${agent.id}/files/content?path=${encodeURIComponent(path)}`);
      text.value = saved = fresh.content; version = fresh.version; say('Reloaded.');
      if (!view.hidden) setMode('view');
    } catch (error) { say(error.message, 'error'); }
  };
}

// The agent builds its page itself (check_page / page.json), so the request
// goes to it as a draft in a new chat; nothing is sent until the owner taps Send.
function askForPage(agent, mode) {
  const request = window.prompt(mode === 'create' ? `What should ${agent.name}'s page show?` : `What should change on ${agent.name}'s page?`);
  if (!request?.trim()) return;
  localStorage.setItem(draftKey(agent.id, null), mode === 'create'
    // The owner's words first: the chat's title comes from the start of the message.
    ? `${request.trim()}\n\n(Build this as your page in the phone app; check_page shows how.)`
    : `${request.trim()}\n\n(Change your page in the phone app to do this; check_page shows it now.)`);
  go(`#/agents/${agent.id}/new`);
}

const formItems = items => (items || []).map(i => `<li class="${i.done ? 'done' : ''}">${escapeHtml(i.text)}</li>`).join('');

function drawCustomPage(agent, page, body, token = renderToken) {
  body.className = 'custom-page';
  const cell = value => (value && !/^[-—–]$/.test(value.trim()) ? value : '');
  const blocks = page.blocks.map((b, n) => {
    const title = b.title ? `<h3>${escapeHtml(b.title)}</h3>` : '';
    if (b.type === 'error') return `<div class="page-error">${escapeHtml(b.message)}</div>`;
    if (b.type === 'text') return `<p class="page-text">${escapeHtml(b.text)}</p>`;
    if (b.type === 'markdown') return `<section class="page-md">${title}${markdown(b.text)}</section>`;
    // form: one text box + button; the server appends "- [ ] <text>" to the
    // note this block names (custom_pages.add_item) and returns its items.
    if (b.type === 'sliders') return `<section class="page-sliders" data-block="${n}"></section>`;
    if (b.type === 'form') return `<section class="page-form" data-block="${n}">${title}<form class="page-form-row" autocomplete="off">
        <input type="text" name="text" class="page-form-input" maxlength="300" placeholder="${escapeHtml(b.placeholder || '')}" aria-label="${escapeHtml(b.title || b.placeholder || 'New item')}" enterkeyhint="done">
        <button type="submit" class="primary page-form-btn">${escapeHtml(b.button || 'Add')}</button>
      </form><p class="page-form-status" role="status"></p><ul class="page-form-items">${formItems(b.items)}</ul></section>`;
    if (b.type === 'html') return `<section>${title}<iframe class="page-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" style="height:${Number(b.height) || 600}px" src="/${agent.id}/page/html/${b.file.split('/').map(encodeURIComponent).join('/')}"></iframe></section>`;
    // table: one card per row, first column bold, the rest as label + value.
    const sections = b.sections.map(sec => {
      const cards = sec.rows.map(row => {
        const rest = row.slice(1).map((v, i) => cell(v) ? `<span class="kv"><b>${escapeHtml(sec.columns[i + 1])}</b> ${escapeHtml(v)}</span>` : '').join('');
        return `<div class="page-card"><div class="page-card-title">${escapeHtml(cell(row[0]) || '—')}</div>${rest ? `<div class="page-card-meta">${rest}</div>` : ''}</div>`;
      }).join('');
      return `<div class="page-section">${sec.heading ? `<h4>${escapeHtml(sec.heading)} <span class="count">${sec.rows.length}</span></h4>` : ''}${cards || '<p class="muted">Empty.</p>'}</div>`;
    }).join('');
    const search = b.search ? `<input type="search" class="search-input page-search" data-block="${n}" placeholder="Search ${escapeHtml(b.title || 'items')}" aria-label="Search" autocomplete="off">` : '';
    return `<section class="page-table" data-block="${n}">${title}${search}${sections}<p class="muted page-nomatch" hidden>No matches.</p></section>`;
  }).join('');
  body.innerHTML = `${page.title ? `<h2>${escapeHtml(page.title)}</h2>` : ''}${blocks}<button type="button" class="link-btn page-change" id="page-change">Change this page…</button>`;
  body.querySelector('#page-change').onclick = () => askForPage(agent, 'change');
  body.querySelectorAll('.page-sliders').forEach(section => drawSliders(agent, section, page.blocks[Number(section.dataset.block)], token));
  body.querySelectorAll('.page-form').forEach(section => {
    const form = section.querySelector('form'), input = form.text, button = form.querySelector('button');
    const status = section.querySelector('.page-form-status');
    form.onsubmit = async event => {
      event.preventDefault();
      if (!input.value.trim()) { status.className = 'page-form-status error'; status.textContent = 'Type something first.'; input.focus(); return; }
      button.disabled = true; status.className = 'page-form-status'; status.textContent = '';
      try {
        const result = await api(`/${agent.id}/page/add`, {method: 'POST', body: JSON.stringify({block: Number(section.dataset.block), text: input.value})});
        input.value = '';
        status.className = 'page-form-status ok'; status.textContent = 'Added ✓';
        section.querySelector('.page-form-items').innerHTML = formItems(result.items);
      } catch (error) {
        status.className = 'page-form-status error'; status.textContent = error.message || 'Could not add it.';
      } finally { button.disabled = false; }
    };
    input.oninput = () => { if (status.classList.contains('ok')) status.textContent = ''; };
  });
  // Every word must match somewhere in the card (same rule as conversation search).
  body.querySelectorAll('.page-search').forEach(input => input.oninput = () => {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    const table = input.closest('.page-table');
    let shown = 0;
    table.querySelectorAll('.page-section').forEach(sec => {
      let here = 0;
      sec.querySelectorAll('.page-card').forEach(card => {
        const hit = words.every(w => card.textContent.toLowerCase().includes(w));
        card.hidden = !hit; here += hit;
      });
      sec.hidden = Boolean(words.length) && !here;
      shown += here;
    });
    table.querySelector('.page-nomatch').hidden = shown > 0 || !words.length;
  });
}

// Handover (shared dialog in chat_core.js): the conversation and the files
// The owner ticks go to another agent, which replies "Handover registered".
// Resolves to the result, or null if cancelled.
function startHandover(agent, conversationId, hint = '') {
  return OpenClaveChat.openHandover({
    agent, conversationId, api, targetHint: hint,
    run: (targetId, files) => api('/api/conversations/handover', {method:'POST', body:JSON.stringify({
      source_agent:agent.id, target_agent:targetId, conversation_id:conversationId, files,
    })}),
    onDone: result => {
      const oldDraft = draftKey(agent.id, conversationId);
      const draft = localStorage.getItem(oldDraft);
      if (draft) localStorage.setItem(draftKey(result.target_agent, conversationId), draft);
      localStorage.removeItem(oldDraft);
    },
    openTarget: targetId => go(`#/agents/${targetId}/conversations/${conversationId}`),
  }).catch(error => { window.alert(error.message); return null; });
}

// Conversation actions (Hand over/Move/Rename/Archive/Delete) stay hidden until a long-press or
// a swipe left on the row; swipe right or opening another row hides them.
const LONG_PRESS_MS = 500;
const SWIPE_PX = 40;

function setConversationOpen(item, open) {
  if (open) document.querySelectorAll('.conversation-item.open').forEach(other => other !== item && other.classList.remove('open'));
  item.classList.toggle('open', open);
}

function wireConversationReveal(item) {
  const row = item.querySelector('.row');
  let startX = 0, startY = 0, timer = null, moved = false, handled = false;
  const cancel = () => { clearTimeout(timer); timer = null; };
  // Each press toggles at most once, whichever fires first: our timer, or the
  // browser's own long-press contextmenu (Android fires both around 500ms).
  const toggleOnce = () => {
    if (handled) return;
    handled = true;
    cancel();
    row.dataset.suppressClick = '1';  // the release after a long-press must not open the chat
    setConversationOpen(item, !item.classList.contains('open'));
    navigator.vibrate?.(10);
  };
  row.addEventListener('pointerdown', e => {
    startX = e.clientX; startY = e.clientY; moved = false; handled = false;
    delete row.dataset.suppressClick;
    cancel();
    timer = setTimeout(toggleOnce, LONG_PRESS_MS);
  });
  row.addEventListener('pointermove', e => {
    if (Math.abs(e.clientX - startX) > 10 || Math.abs(e.clientY - startY) > 10) { moved = true; cancel(); }
  });
  row.addEventListener('pointerup', e => {
    cancel();
    const dx = e.clientX - startX, dy = e.clientY - startY;
    if (!handled && moved && Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy) * 1.5) {
      handled = true;
      row.dataset.suppressClick = '1';
      setConversationOpen(item, dx < 0);
    }
  });
  row.addEventListener('pointercancel', cancel);
  row.addEventListener('contextmenu', e => { e.preventDefault(); toggleOnce(); });
}

// Search box above the conversation list: 2+ characters search titles and
// text (every word must match) and show hits with a snippet instead of the
// list. Same search as the desktop agent page.
function wireConversationSearch(agent, token) {
  const input = document.querySelector('#convo-search');
  if (!input) return;
  const results = document.querySelector('#search-results');
  const list = document.querySelector('#convo-list');
  let timer = null, seq = 0;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { results.hidden = true; list.hidden = false; return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const hits = await api(`/${agent.id}/conversation-search?q=${encodeURIComponent(q)}`);
        if (mine !== seq || token !== renderToken) return;
        results.innerHTML = hits.length
          ? hits.map(h => `<button class="row" data-open="${escapeHtml(h.id)}"><span class="row-main"><span class="row-title">${escapeHtml(h.title)}</span><span class="row-sub">${escapeHtml(h.snippet || h.date || '')}</span></span><span class="chev">›</span></button>`).join('')
          : '<div class="center-state">No matches.</div>';
        results.querySelectorAll('[data-open]').forEach(el => el.onclick = () => go(`#/agents/${agent.id}/conversations/${el.dataset.open}`));
        results.hidden = false; list.hidden = true;
      } catch { /* keep the list */ }
    }, 250);
  });
}

async function renderConversations(agent, token, quiet = false) {
  const cached = !quiet && cachedConversations(agent.id);
  if (cached) drawConversations(agent, token, cached, true);
  else if (!quiet) shell(agent.name, '#/agents', '<div class="center-state">Loading conversations…</div>', undefined, agent);
  let conversations;
  try {
    // Folders (folders.py) group the list; an older server without them just gives a flat list.
    const [list, folderList] = await Promise.all([api(`/${agent.id}/conversations`), api(`/${agent.id}/folders`).catch(() => null)]);
    conversations = list;
    if (folderList) cacheFolders(agent.id, folderList);
  }
  catch (error) { if (token === renderToken && !cached) state(agent.name, error.message, true); return; }
  if (token !== renderToken) return;
  cacheConversations(agent.id, conversations);
  // Swap in the server's list without losing a search typed meanwhile.
  const search = document.querySelector('#convo-search');
  const typed = search?.value, focused = search && document.activeElement === search;
  drawConversations(agent, token, conversations, false);
  const again = document.querySelector('#convo-search');
  if (typed && again) { again.value = typed; again.dispatchEvent(new Event('input')); }
  if (focused) again?.focus();
}

const FOLDERS_CACHE = 'openclave.folders.v1';
function cachedFolders(agentId) { return readJson(FOLDERS_CACHE, {})[agentId] || []; }
function cacheFolders(agentId, list) {
  const all = readJson(FOLDERS_CACHE, {});
  all[agentId] = list;
  writeJson(FOLDERS_CACHE, all);
}
function foldedFolders(agentId) { return new Set(readJson(`openclave.folders.folded.${agentId}`, [])); }

// Folders (folders.py): plain buckets. Each folder is a header you can fold,
// then its chats; chats in no folder come last.
function groupedConversationRows(agent, conversations, rowHtml) {
  const folderList = cachedFolders(agent.id);
  if (!folderList.length) return conversations.map(rowHtml).join('');
  const names = new Set(folderList.map(f => f.name));
  const folded = foldedFolders(agent.id);
  const group = (key, label, items) => `<div class="folder-group${folded.has(key) ? ' folded' : ''}${key ? '' : ' folder-none'}"><div class="folder-head"><button class="folder-fold" data-fold="${escapeHtml(key)}"><span class="folder-caret">▾</span><span class="folder-name">${escapeHtml(label)}</span></button>${key ? `<button class="folder-new" data-folder-new="${escapeHtml(key)}" aria-label="New chat in ${escapeHtml(label)}">+</button>` : ''}</div><div class="folder-chats">${items.map(rowHtml).join('')}</div></div>`;
  const out = folderList.map(f => group(f.name, f.name, conversations.filter(c => c.folder === f.name)));
  out.push(group('', 'No topic', conversations.filter(c => !c.folder || !names.has(c.folder))));
  // Folder names in the agent's own colour.
  const color = /^#[0-9a-f]{3,8}$/i.test(agent.color || '') ? agent.color : '';
  return color ? `<div class="folder-list" style="--folder:${color}">${out.join('')}</div>` : out.join('');
}

function wireFolders(agent, token, conversations) {
  document.querySelectorAll('[data-fold]').forEach(el => el.onclick = () => {
    const folded = el.closest('.folder-group').classList.toggle('folded');
    const set = foldedFolders(agent.id);
    if (folded) set.add(el.dataset.fold); else set.delete(el.dataset.fold);
    writeJson(`openclave.folders.folded.${agent.id}`, [...set]);
  });
  document.querySelectorAll('[data-folder-new]').forEach(el => el.onclick = async () => {
    el.disabled = true;
    try {
      const made = await api(`/${agent.id}/folders/${encodeURIComponent(el.dataset.folderNew)}/conversations`, {method:'POST'});
      go(`#/agents/${agent.id}/conversations/${made.id}`);
    } catch (error) { window.alert(error.message); el.disabled = false; }
  });
  const add = document.querySelector('#new-folder');
  if (add) add.onclick = async () => {
    const name = window.prompt('Topic name');
    if (!name || !name.trim()) return;
    try {
      await api(`/${agent.id}/folders`, {method:'POST', body:JSON.stringify({name:name.trim()})});
      if (token === renderToken) await renderConversations(agent, token);
    } catch (error) { window.alert(error.message); }
  };
  document.querySelectorAll('[data-folder-move]').forEach(el => el.onclick = async () => {
    const item = conversations.find(c => c.id === el.dataset.folderMove);
    const names = cachedFolders(agent.id).map(f => f.name);
    const entered = window.prompt(`Move "${item.title}" to which topic?\n${names.map((n, i) => `${i + 1}. ${n}`).join('\n')}${names.length ? '\n' : ''}0. No topic\nOr type a new topic name.`);
    if (entered === null || !entered.trim()) return;
    const v = entered.trim();
    const folder = v === '0' ? '' : (/^\d+$/.test(v) && names[Number(v) - 1]) || v;
    el.disabled = true;
    try {
      await api(`/${agent.id}/conversations/${item.id}`, {method:'PATCH', body:JSON.stringify({folder})});
      if (token === renderToken) await renderConversations(agent, token);
    } catch (error) { window.alert(error.message); el.disabled = false; }
  });
}

function drawConversations(agent, token, conversations, fromCache) {
  {
    const convoSeen = loadConvoSeen();
    const rowHtml = item => `<div class="conversation-item"><button class="row" data-conversation="${escapeHtml(item.id)}"><span class="row-main"><span class="row-title has-tags"><span class="title-text">${escapeHtml(item.title)}</span>${!fromCache && conversationUnread(agent.id, item, convoSeen) ? NEW_LIGHT : ''}${localStorage.getItem(draftKey(agent.id, item.id))?.trim() ? '<span class="draft-tag">draft</span>' : ''}</span><span class="row-sub">${escapeHtml(item.date || '')}</span></span><span class="chev">›</span></button><div class="conversation-actions"><button data-handover="${escapeHtml(item.id)}" title="Move the chat to another agent, with any related files">Move / hand over</button><button data-folder-move="${escapeHtml(item.id)}">Topic</button><button data-rename="${escapeHtml(item.id)}">Rename</button><button data-autoname="${escapeHtml(item.id)}" title="A short name from the conversation">Auto-name</button><button data-archive="${escapeHtml(item.id)}" title="Out of the list, into Recaps as a short card">Archive</button><button data-purge="${escapeHtml(item.id)}" title="Keep the chat and a short summary, drop the transcript">Purge…</button><button class="danger" data-delete="${escapeHtml(item.id)}">Delete</button></div></div>`;
    const rows = groupedConversationRows(agent, conversations, rowHtml);
    shell(agent.name, '#/agents', `<section class="content"><div class="new-row"><button class="primary" id="new-chat">New chat</button><button type="button" class="primary" id="new-folder">New topic</button></div>${conversations.length ? '<input type="search" class="search-input" id="convo-search" placeholder="Search conversations" aria-label="Search conversations" autocomplete="off">' : ''}<div class="list" id="search-results" hidden></div><div class="list" id="convo-list">${rows || '<div class="center-state">No conversations yet.</div>'}</div></section>
      <div class="list-footer"><button type="button" class="switch-btn quick-jump" id="switch-btn" aria-label="Quick jump: tap or hold to switch agent">Quick jump</button><button type="button" class="switch-btn home-btn" data-home aria-label="Conclave: back to all agents">Conclave</button></div>`, modelBadge(agent), agent);
    wireModelBadge(agent);
    // Quick jump here too (Rick's backlog, 2026-10-04): same menu, unread
    // markers and order as in chat, but an agent opens its conversation list.
    // Replacing this list keeps Back and the edge swipe going where ‹ goes.
    wireSwitchButton(agent.id, id => location.replace(`#/agents/${id}/conversations`));
    wireHomeButton();
    if (!fromCache) api('/api/agents').then(list => { if (token === renderToken) setAgents(list); }).catch(() => {});
    document.querySelector('#new-chat').onclick = () => go(`#/agents/${agent.id}/new`);
    wireConversationSearch(agent, token);
    wireFolders(agent, token, conversations);
    document.querySelectorAll('.conversation-item').forEach(wireConversationReveal);
    document.querySelectorAll('[data-conversation]').forEach(el => el.onclick = () => {
      if (el.dataset.suppressClick) { delete el.dataset.suppressClick; return; }
      go(`#/agents/${agent.id}/conversations/${el.dataset.conversation}`);
    });
    document.querySelectorAll('[data-rename]').forEach(el => el.onclick = async () => {
      const item = conversations.find(c => c.id === el.dataset.rename);
      const title = window.prompt('Conversation name', item.title);
      if (title === null || !title.trim() || title.trim() === item.title) return;
      el.disabled = true;
      try {
        await api(`/${agent.id}/conversations/${item.id}`, {method:'PATCH', body:JSON.stringify({title:title.trim()})});
        if (token === renderToken) await renderConversations(agent, token);
      } catch (error) { window.alert(error.message); el.disabled = false; }
    });
    // One-click Auto-rename: named from its first exchange, without showing as new.
    document.querySelectorAll('[data-autoname]').forEach(el => el.onclick = async () => {
      el.disabled = true;
      try {
        await api(`/${agent.id}/conversations/${el.dataset.autoname}/auto-name`, {method:'POST'});
        if (token === renderToken) await renderConversations(agent, token);
      } catch (error) { window.alert(error.message); el.disabled = false; }
    });
    // Archive: out of the list and into the Recaps tab as a short card.
    document.querySelectorAll('[data-archive]').forEach(el => el.onclick = async () => {
      el.disabled = true;
      try {
        await api(`/${agent.id}/conversations/${el.dataset.archive}/archive`, {method:'POST'});
        if (token === renderToken) await renderConversations(agent, token);
      } catch (error) { window.alert(error.message); el.disabled = false; }
    });
    document.querySelectorAll('[data-purge]').forEach(el => el.onclick = async () => {
      if (await OpenClaveChat.openPurge({agent: {id: agent.id, name: agent.name}, conversationId: el.dataset.purge, api})
          && token === renderToken) await renderConversations(agent, token);
    });
    document.querySelectorAll('[data-handover]').forEach(el => el.onclick = async () => {
      const result = await startHandover(agent, el.dataset.handover);
      if (result && token === renderToken) await renderConversations(agent, token);
    });
    document.querySelectorAll('[data-delete]').forEach(el => el.onclick = async () => {
      const item = conversations.find(c => c.id === el.dataset.delete);
      if (!window.confirm(`Delete "${item.title}" and its photos? This cannot be undone.`)) return;
      el.disabled = true;
      try {
        await api(`/${agent.id}/conversations/${item.id}`, {method:'DELETE'});
        localStorage.removeItem(draftKey(agent.id, item.id));
        getPending().filter(r => r.agentId === agent.id && r.conversationId === item.id).forEach(removePending);
        if (token === renderToken) await renderConversations(agent, token);
      } catch (error) { window.alert(error.message); el.disabled = false; }
    });
  }
}

function markdown(text) {
  if (!window.marked || !window.DOMPurify) return `<p>${escapeHtml(text)}</p>`;
  marked.setOptions({breaks:true, gfm:true});
  return DOMPurify.sanitize(marked.parse(text));
}

// The chat itself is the shared implementation in /static/chat_core.js, the
// same code the desktop agent pages run (2026-09-27: desktop and
// mobile chat must stay in sync). This only adds the mobile frame around it:
// top bar with model badge, swipe right for the conversation list, Quick jump.
const CONCLAVE_ICON = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.5"/><path d="M3 19c0-3 2.7-5 6-5s6 2 6 5"/><path d="M15 14.5c2.8-.4 5.5 1.2 6 4"/></svg>';
let currentChat = null;
const chatStore = OpenClaveChat.storage();

async function renderChat(agent, conversationId, token) {
  const {audio: mobileAudio, voiceAvailable} = await mobileAudioPromise;
  if (token !== renderToken) return;
  shell(agent.name, `#/agents/${agent.id}/conversations`, `<section class="chat-content" id="chat"><div class="messages"></div></section><form class="composer" id="composer"></form>`, modelBadge(agent), agent, conversationId ? '' : 'New chat');
  const showTitle = title => {
    const sub = document.querySelector('.topbar .title-sub');
    if (sub && token === renderToken) { sub.textContent = title || 'New chat'; sub.title = sub.textContent; }
  };
  wireModelBadge(agent);
  wireSwipeBack(agent.id);
  currentChat = OpenClaveChat.mountChat({
    agent, conversationId, api,
    form: document.querySelector('#composer'),
    messages: document.querySelector('.messages'),
    scroller: document.querySelector('#chat'),
    markdown,
    canAttach: true,  // every model takes photos, and documents go in as text
    canTalk: voiceAvailable,
    audio: mobileAudio,
    store: chatStore,
    pendingKey: PENDING_KEY,
    // "Conclave" (back to the agent list) just left of Quick jump on the same
    // row; compact, so Quick jump stays a long bar (2026-10-04).
    // Quick jump is the core way to multitask (2026-10-12): a big
    // orange bar taking the rest of the row; Conclave shrinks to an icon.
    subHtml: `<button type="button" class="switch-btn home-btn" data-home aria-label="Conclave: back to all agents" title="Conclave">${CONCLAVE_ICON}</button><button type="button" class="switch-btn quick-jump" id="switch-btn" aria-label="Quick jump: tap or hold to switch agent">Quick jump</button>`,
    isCurrent: () => token === renderToken,
    notify: name => window.openclaveNotifyReply?.(name),
    onConversationChange: id => history.replaceState(null, '', `#/agents/${agent.id}/conversations/${id}`),
    onTitle: showTitle,
    onAutojump: (agentId, conversationId) => openOverList(agentId, `#/agents/${agentId}/conversations/${conversationId}`),
    onForward: (targetId, text) => { localStorage.setItem(draftKey(targetId, null), text); go(`#/agents/${targetId}/new`); },
    // "/handover stash": afterwards this chat is gone from here, so back to
    // the list (unless "Open in <agent>" already went there).
    onHandover: async (id, hint) => {
      const here = location.hash;
      const result = await startHandover(agent, id, hint);
      if (result && location.hash === here) go(`#/agents/${agent.id}/conversations`);
    },
  });
  wireSwitchButton(agent.id);
  wireHomeButton();
  // Fresh activity for the Quick jump menu's unread markers and order.
  api('/api/agents').then(list => { if (token === renderToken) setAgents(list); }).catch(() => {});
  await currentChat.loaded;
}

// One row per clave's group chat, under "everyone" (claves.py). Clicks are
// handled on the list (data-clave-room), so redrawing the rows needs no rebinding.
// Then the owner's own channels (channels.py) and a row to add one.
// Clave switcher on the home list (2026-11-05), the phone twin of the
// desktop chips next to the Claves button: picks the active clave
// (service_settings.active_clave) and shows only its agents. With claves
// off or none set up it is a single "Claves" button that says where to set
// them up, since creating claves is desktop-only.
let claveOverview = null;
function claveSwitch() {
  const data = claveOverview;
  if (!data) return '';
  if (!data.enabled || !data.claves.length) {
    return '<button type="button" class="clave-chip" id="claves-info" aria-expanded="false">Claves</button>'
      + '<p class="clave-note" id="claves-note" hidden>Claves are set up in the desktop version: open the Claves view on the desktop home page.</p>';
  }
  return data.claves.map(c => `<button type="button" class="clave-chip" data-clave-pick="${escapeHtml(c.id)}" aria-pressed="${c.id === data.active}">${escapeHtml(c.name)}</button>`).join('');
}
function mountClaveSwitch() {
  const box = document.querySelector('#clave-switch');
  if (!box) return;
  box.innerHTML = claveSwitch();
  const info = box.querySelector('#claves-info');
  if (info) info.onclick = () => {
    const note = box.querySelector('#claves-note');
    note.hidden = !note.hidden;
    info.setAttribute('aria-expanded', String(!note.hidden));
  };
  box.querySelectorAll('[data-clave-pick]').forEach(chip => chip.onclick = async () => {
    if (chip.getAttribute('aria-pressed') === 'true') return;
    try {
      claveOverview = await api('/api/claves/active', {method: 'POST', body: JSON.stringify({clave: chip.dataset.clavePick})});
    } catch { return; }
    mountClaveSwitch();
    applyClaveFilter();
  });
}
function applyClaveFilter() {
  const data = claveOverview;
  const clave = data?.enabled && data.claves.find(c => c.id === data.active);
  const shown = clave ? new Set([...clave.members, data.manager].filter(Boolean)) : null;
  document.querySelectorAll('.row[data-agent]').forEach(row => { row.hidden = Boolean(shown) && !shown.has(row.dataset.agent); });
}
function claveRows() {
  return claveList.map(c => `<button type="button" class="row room-row" data-clave-room="${escapeHtml(c.id)}" aria-label="Group chat of the ${escapeHtml(c.name)} clave"><span class="avatar room-avatar-hash">#</span><span class="row-main"><span class="row-title">${escapeHtml(c.name)}</span><span class="row-sub">Clave · ${c.members.length} agents</span></span><span class="chev">›</span></button>`).join('')
    + channelList.map(c => `<button type="button" class="row room-row" data-clave-room="${escapeHtml(c.id)}" aria-label="Channel ${escapeHtml(c.name)}"><span class="avatar room-avatar-hash">#</span><span class="row-main"><span class="row-title">${escapeHtml(c.name)}</span><span class="row-sub">Channel · ${c.members.length} agents</span></span><span class="chev">›</span></button>`).join('')
    + `<button type="button" class="row room-row" data-channel-new aria-label="Add a channel"><span class="avatar room-avatar-hash">+</span><span class="row-main"><span class="row-title">Add channel</span><span class="row-sub">A group chat with the agents you pick</span></span><span class="chev">›</span></button>`
    + `<p class="list-label">Agents – private chat</p>`;
}
async function editChannelMobile(channel) {
  const result = await OpenClaveRoom.editChannel({api, agents, channel});
  if (!result) return;
  channelList = await api('/api/channels').catch(() => channelList);
  go(result.deleted ? '#/agents' : `#/room/${encodeURIComponent(result.id)}`);
}
document.addEventListener('click', event => {
  if (event.target.closest('[data-channel-new]')) { editChannelMobile(null); return; }
  const edit = event.target.closest('[data-channel-edit]');
  if (edit) { editChannelMobile(channelList.find(c => c.id === edit.dataset.channelEdit)); return; }
  const row = event.target.closest('[data-clave-room]');
  if (row) go(`#/room/${encodeURIComponent(row.dataset.claveRoom)}`);
});

// The group chat (room.py): the shared room code in /static/room_core.js,
// the same as the desktop's chat view.
let currentRoom = null;
function renderRoom(token, claveId = null) {
  const isChannel = !!claveId && claveId.startsWith('ch-');
  const clave = claveId ? (isChannel ? channelList : claveList).find(c => c.id === claveId) : null;
  if (isChannel && !clave) {
    api('/api/channels').then(chans => {
      channelList = chans;
      if (token !== renderToken) return;
      if (chans.some(c => c.id === claveId)) renderRoom(token, claveId); else go('#/room');
    }).catch(() => { if (token === renderToken) go('#/room'); });
    return;
  }
  if (claveId && !clave) {
    // Opened straight from a link: learn the clave's name first.
    api('/api/claves').then(data => {
      claveList = data.enabled ? data.claves : [];
      if (token !== renderToken) return;
      if (claveList.some(c => c.id === claveId)) renderRoom(token, claveId); else go('#/room');
    }).catch(() => { if (token === renderToken) go('#/room'); });
    return;
  }
  const channelBar = isChannel ? `<div class="room-channel-bar"><span class="muted">${clave.members.map(id => escapeHtml(agents.find(a => a.id === id)?.name || id)).join(', ') || 'Nobody yet: tag agents with @name'}</span><button type="button" class="link-btn" data-channel-edit="${escapeHtml(clave.id)}">Members</button></div>` : '';
  shell(clave ? `# ${clave.name}` : '# everyone', '#/agents', `${channelBar}<section class="chat-content" id="chat"><div class="messages room-messages"></div></section><form class="composer room-composer" id="composer"></form>`);
  currentRoom = OpenClaveRoom.mount({
    clave: clave?.id, claveName: clave?.name,
    messages: document.querySelector('.messages'),
    form: document.querySelector('#composer'),
    scroller: document.querySelector('#chat'),
    api, agents, md: markdown, store: chatStore, enterSends: false,
    onOpenAgent: id => go(`#/agents/${id}/conversations`),
  });
  document.querySelector('#composer').onsubmit = event => event.preventDefault();
}

async function render() {
  const token = ++renderToken;
  currentChat?.destroy();
  currentChat = null;
  currentRoom?.destroy();
  currentRoom = null;
  const parts = routeParts();
  if (EMBED) {
    // Only the agent's page; anything else (e.g. "Create new custom page?",
    // which opens a chat with a draft) goes to the shell, which opens it in
    // the full phone app in a new tab.
    if (parts[0] !== 'agents' || !parts[1] || !['console', 'tools', 'skills'].includes(parts[2])) {
      const agentId = parts[1] || '';
      window.parent.postMessage({__openclaveEmbed: true, kind: 'leave', agentId, hash: location.hash}, location.origin);
      return;
    }
  } else {
    if (!parts.length) { go(localStorage.getItem(LAST_ROUTE) || '#/agents'); return; }
    localStorage.setItem(LAST_ROUTE, location.hash);
  }
  if (parts[0] === 'create-agent') { trackVisit(null); renderCreateAgent(token); return; }
  if (parts[0] === 'room') { trackVisit(null); renderRoom(token, parts[1] ? decodeURIComponent(parts[1]) : null); return; }
  if (parts[0] !== 'agents') { go('#/agents'); return; }
  trackVisit(parts.length > 1 ? parts[1] : null);
  trackConversation(parts[1], parts[2] === 'conversations' ? parts[3] : null);
  if (parts.length === 1) { renderAgents(token); return; }
  const agent = byId(parts[1]);
  if (!agent) { state('OpenClave', 'Agent not found.', false); return; }
  if (parts[2] === 'conversations' && parts.length === 3) return renderConversations(agent, token);
  if (parts[2] === 'console') return renderConsole(agent, token);
  if (parts[2] === 'files' && parts[3]) return renderFileEditor(agent, decodeURIComponent(parts[3]), token);
  if (parts[2] === 'files') return renderFiles(agent, token);
  if (parts[2] === 'tools') return renderTools(agent, token);
  if (parts[2] === 'claves') return renderAgentClaves(agent, token);
  if (parts[2] === 'skills') return renderSkills(agent, token);
  if (parts[2] === 'recaps') return renderRecaps(agent, token);
  if (parts[2] === 'new') return renderChat(agent, null, token);
  if (parts[2] === 'model') return renderModelPicker(agent, token);
  if (parts[2] === 'conversations' && parts[3]) return renderChat(agent, parts[3], token);
  go(`#/agents/${agent.id}/conversations`);
}

window.addEventListener('hashchange', render);
// Back in the foreground: update what's on screen in place. A full render()
// here blanked the screen and refetched everything one call after another,
// so every switch back to the app felt like a cold start.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshView(); });
function refreshView() {
  const parts = routeParts();
  if (parts[0] !== 'agents') return;  // e.g. the new-agent form keeps what's typed
  trackVisit(parts[1] || null);
  if (currentChat) {
    currentChat.refresh();
    const token = renderToken;  // fresh "new" markers for Quick jump
    api('/api/agents').then(list => { if (token === renderToken) setAgents(list); }).catch(() => {});
    return;
  }
  if (parts.length === 1) { renderAgents(++renderToken); return; }
  const agent = byId(parts[1]);
  if (agent && parts[2] === 'conversations' && parts.length === 3 && !document.querySelector('#convo-search')?.value) {
    renderConversations(agent, ++renderToken, true);
  }
}
loadResponseTimes();
const bootAgents = readJson(AGENTS_CACHE, null);
if (Array.isArray(bootAgents) && bootAgents.length) {
  // Last time's agents: draw the last screen now, server copy right after.
  agents = bootAgents;
  convoBase();
  render();
  api('/api/agents').then(list => {
    const missing = document.querySelector('.center-state')?.textContent === 'Agent not found.';
    setAgents(list);
    seedSeen();
    if (missing) render();  // an agent created since: now it's known
  }).catch(() => { /* keep the copy; each screen retries on its own */ });
} else {
  try {
    setAgents(await api('/api/agents'));
    seedSeen();
    convoBase();  // per-conversation "new" counts from here on this phone
    await render();
  } catch (error) { state('OpenClave', error.message, true); }
}
