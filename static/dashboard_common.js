// OpenClave — shared per-agent dashboard behavior (Backlog.md item #6).
//
// Extracted from 7 separately-maintained dashboard.html files that had
// diverged into near-identical copies of the same chat/conversation/backlog/
// kanban/usage code — a shared fix previously had to be re-applied by hand in
// each one. Each dashboard.html now sets window.OPENCLAVE_AGENT_ID (and
// OPENCLAVE_AGENT_NAME for the composer placeholder / empty-state text)
// before loading this file, keeps its own color variables and any custom
// panels (e.g. Reba's tracking/trend charts), and calls initDashboardCommon()
// plus whichever of its own panel loaders it needs.
//
// Runs inside the sandboxed per-agent iframe (dashboard_shell.js) exactly
// like the inline scripts it replaces did — fetch()/localStorage are already
// shimmed there, this file doesn't need to know that.

const AGENT_ID = window.OPENCLAVE_AGENT_ID;
const AGENT_NAME = window.OPENCLAVE_AGENT_NAME || AGENT_ID;

(function initResizableLayout() {
  const LAYOUT_KEY = 'layout_widths';
  const layoutEl = document.querySelector('.layout');
  let left = 220, right = 300, kbHeight = 120;
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null');
    if (saved && saved.left && saved.right) { left = saved.left; right = saved.right; }
    // 260px was the old default and may have been saved by an unrelated column drag.
    if (saved && saved.kbHeight && saved.kbHeight !== 260) kbHeight = saved.kbHeight;
  } catch (e) {}

  function apply() {
    layoutEl.style.setProperty('--col-left-w', left + 'px');
    layoutEl.style.setProperty('--col-right-w', right + 'px');
    // Also on the root, so the header's model picker can match the column.
    document.documentElement.style.setProperty('--col-right-w', right + 'px');
    layoutEl.style.setProperty('--kb-row-h', kbHeight + 'px');
  }
  apply();

  function save() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify({ left, right, kbHeight })); } catch (e) {}
  }

  function makeDraggable(handle, onDrag) {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startLeft = left, startRight = right;
      handle.classList.add('dragging');
      function onMove(ev) { onDrag(ev.clientX - startX, startLeft, startRight); apply(); }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        handle.classList.remove('dragging');
        save();
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  const resizeLeft = document.getElementById('resize-left');
  const resizeRight = document.getElementById('resize-right');
  if (resizeLeft) makeDraggable(resizeLeft, (dx, startLeft) => {
    left = Math.max(160, Math.min(480, startLeft + dx));
  });
  if (resizeRight) makeDraggable(resizeRight, (dx, _sl, startRight) => {
    right = Math.max(220, Math.min(560, startRight - dx));
  });

  const kbHandle = document.getElementById('resize-kb');
  if (kbHandle) kbHandle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = kbHeight;
    kbHandle.classList.add('dragging');
    function onMove(ev) {
      kbHeight = Math.max(120, Math.min(600, startHeight - (ev.clientY - startY)));
      apply();
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      kbHandle.classList.remove('dragging');
      save();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
})();

let currentConversationId = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const error = new Error(body.detail || `${path} -> ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}

function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso + 'T00:00:00').toLocaleDateString(undefined, {month:'short', day:'numeric'}); }
  catch { return iso; }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// For embedding a JS string literal inside a double-quoted HTML attribute
// (onclick="..."): JSON.stringify gives a valid JS string literal, and
// escaping its own double-quotes as entities keeps the HTML attribute
// boundary intact even when the source text contains " or '.
function escapeAttr(s) {
  return JSON.stringify(s).replace(/"/g, '&quot;');
}

function conversationAsText(detail) {
  const title = detail.title || detail.id;
  const turns = detail.turns.map(t => `${t.role === 'user' ? 'You' : AGENT_NAME}:\n${t.content.trim()}`);
  return [`${title} (${AGENT_NAME})`, ...turns].join('\n\n');
}

async function writeClipboard(text) {
  // This page runs in a sandboxed iframe, where the async clipboard API can be
  // unavailable (no permission, or plain-HTTP origin); fall back to the older
  // execCommand copy, which works from a click inside the frame.
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  }
}

async function copyConversation(id, e) {
  e.stopPropagation();
  const btn = e.currentTarget;
  const original = btn.innerHTML;
  try {
    const detail = await api(`/${AGENT_ID}/conversations/${id}`);
    if (!await writeClipboard(conversationAsText(detail))) throw new Error('copy refused');
    btn.textContent = '✓ Copied';
  } catch (err) {
    btn.textContent = '! Copy failed';
    btn.title = 'Copy failed';
  }
  setTimeout(() => { btn.innerHTML = original; btn.title = 'Copy whole conversation to clipboard'; }, 1500);
}

// Unsent text in a conversation (the shared chat saves drafts per conversation).
function hasDraft(conversationId) {
  try { return Boolean(localStorage.getItem(`openclave.draft.${AGENT_ID}.${conversationId}`)?.trim()); } catch (e) { return false; }
}

// Search box above the conversation list (same search as mobile): 2+
// characters show matching conversations with a snippet instead of the list.
let convoQuery = '';
function injectConversationSearch() {
  const list = document.getElementById('convo-list');
  if (!list || document.getElementById('convo-search')) return;
  list.insertAdjacentHTML('beforebegin', '<input type="search" id="convo-search" class="convo-search" placeholder="Search chats" autocomplete="off">');
  const input = document.getElementById('convo-search');
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { convoQuery = input.value.trim(); loadConversations(); }, 250);
  });
}

// Row actions live behind one ⋯ button at the right of each conversation /
// file row (they used to float over the title on hover and cover it). The
// menu is position:fixed so the scrolling list can't clip it.
const ROW_MENU_BUTTON = '<button class="convo-more" title="Actions" aria-haspopup="menu" aria-expanded="false">⋯</button>';
let openRowMenu = null;

function closeRowMenu() {
  if (!openRowMenu) return;
  openRowMenu.classList.remove('menu-open');
  const more = openRowMenu.querySelector('.convo-more');
  if (more) more.setAttribute('aria-expanded', 'false');
  openRowMenu = null;
}

function attachRowMenu(el) {
  const more = el.querySelector('.convo-more');
  const menu = el.querySelector('.convo-actions');
  if (!more || !menu) return;
  more.onclick = (e) => {
    e.stopPropagation();
    if (openRowMenu === el) { closeRowMenu(); return; }
    closeRowMenu();
    el.classList.add('menu-open');
    more.setAttribute('aria-expanded', 'true');
    openRowMenu = el;
    const r = more.getBoundingClientRect();
    const h = menu.offsetHeight, w = menu.offsetWidth;
    const below = r.bottom + 4 + h <= window.innerHeight;
    menu.style.top = `${below ? r.bottom + 4 : Math.max(4, r.top - 4 - h)}px`;
    menu.style.left = `${Math.max(4, Math.min(r.right - w, window.innerWidth - w - 4))}px`;
  };
  // Capture phase: the action handlers stop propagation. Copy stays open a
  // moment so its "✓ Copied" is visible.
  menu.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (btn.classList.contains('convo-copy')) setTimeout(() => { if (openRowMenu === el) closeRowMenu(); }, 900);
    else closeRowMenu();
  }, true);
}

document.addEventListener('click', (e) => {
  if (openRowMenu && !openRowMenu.querySelector('.convo-actions').contains(e.target)) closeRowMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeRowMenu(); });
window.addEventListener('resize', closeRowMenu);
document.addEventListener('scroll', closeRowMenu, true);

async function loadConversations() {
  const list = document.getElementById('convo-list');
  if (convoQuery.length >= 2) {
    try {
      const hits = await api(`/${AGENT_ID}/conversation-search?q=${encodeURIComponent(convoQuery)}`);
      list.innerHTML = hits.length ? '' : '<div class="empty">No matches.</div>';
      for (const h of hits) {
        const el = document.createElement('div');
        el.className = 'convo-item' + (h.id === currentConversationId ? ' active' : '');
        el.innerHTML = `<div class="convo-text"><div class="t">${escapeHtml(h.title)}</div><div class="d convo-snippet">${escapeHtml(h.snippet || fmtDate(h.date))}</div></div>`;
        el.onclick = () => openConversation(h.id);
        list.appendChild(el);
      }
    } catch (e) { list.innerHTML = '<div class="empty">Search failed.</div>'; }
    return;
  }
  try {
    // Folders (folders.py): plain buckets; drag a chat onto a folder to put it there.
    const [convos, folderList] = await Promise.all([
      api(`/${AGENT_ID}/conversations`),
      api(`/${AGENT_ID}/folders`).catch(() => []),
    ]);
    list.innerHTML = '';
    if (!folderList.length && !convos.length) list.innerHTML = '<div class="empty">No chats yet.</div>';
    for (const f of folderList) list.appendChild(folderGroup(f.name, convos.filter(c => c.folder === f.name)));
    const names = new Set(folderList.map(f => f.name));
    const loose = convos.filter(c => !c.folder || !names.has(c.folder));
    if (folderList.length) list.appendChild(folderGroup(null, loose));
    else for (const c of loose) list.appendChild(convoRow(c));
    if (!document.getElementById('new-topic')) list.appendChild(newFolderButton());  // pages without the "+ new chat" row
  } catch (e) {
    list.innerHTML = '<div class="empty">Failed to load conversations.</div>';
  }
}

function foldedFolders() {
  try { return new Set(JSON.parse(localStorage.getItem(`openclave.folders.folded.${AGENT_ID}`) || '[]')); } catch (e) { return new Set(); }
}

function setFolderFolded(name, folded) {
  const set = foldedFolders();
  if (folded) set.add(name); else set.delete(name);
  try { localStorage.setItem(`openclave.folders.folded.${AGENT_ID}`, JSON.stringify([...set])); } catch (e) {}
}

async function moveToFolder(conversationId, folder) {
  try {
    await api(`/${AGENT_ID}/conversations/${conversationId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder: folder || '' }) });
  } catch (e) { alert(e.message || 'Could not move the chat.'); }
  loadConversations();
}

// A folder (or, with name null, the chats in no folder): a header you can
// fold and drop chats on, then its chats.
function folderGroup(name, convos) {
  const key = name ?? '';
  const group = document.createElement('div');
  group.className = 'folder-group' + (foldedFolders().has(key) ? ' folded' : '') + (name ? '' : ' folder-none');
  group.innerHTML = `<div class="folder-head"><button type="button" class="folder-fold" title="${escapeAttr(name || 'No topic')}"><span class="folder-caret">▾</span><span class="folder-name">${escapeHtml(name || 'No topic')}</span></button>${name ? `<button type="button" class="folder-new" title="New chat in ${escapeAttr(name)}">+</button><span class="folder-spacer"></span>` : ''}${name ? '<button type="button" class="folder-btn folder-rename" title="Rename topic">✎</button><button type="button" class="folder-btn folder-delete" title="Delete topic (its chats stay)">✕</button>' : ''}</div><div class="folder-chats"></div>`;
  const body = group.querySelector('.folder-chats');
  for (const c of convos) body.appendChild(convoRow(c));
  group.querySelector('.folder-fold').onclick = (e) => {
    e.stopPropagation();
    setFolderFolded(key, group.classList.toggle('folded'));
  };
  if (name) {
    group.querySelector('.folder-new').onclick = async (e) => {
      e.stopPropagation();
      if (group.classList.contains('folded')) { group.classList.remove('folded'); setFolderFolded(key, false); }
      try {
        const made = await api(`/${AGENT_ID}/folders/${encodeURIComponent(name)}/conversations`, { method: 'POST' });
        openConversation(made.id);
      } catch (err) { alert(err.message || 'Could not start a chat.'); }
    };
    group.querySelector('.folder-rename').onclick = async (e) => {
      e.stopPropagation();
      const to = prompt('Topic name', name);
      if (!to || !to.trim() || to.trim() === name) return;
      try { await api(`/${AGENT_ID}/folders/${encodeURIComponent(name)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: to.trim() }) }); }
      catch (err) { alert(err.message || 'Could not rename.'); }
      loadConversations();
    };
    group.querySelector('.folder-delete').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete the topic "${name}"? Its chats stay, just without a topic.`)) return;
      try { await api(`/${AGENT_ID}/folders/${encodeURIComponent(name)}`, { method: 'DELETE' }); } catch (err) { alert(err.message); }
      loadConversations();
    };
  }
  // Drop target: the whole group.
  group.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes('text/x-openclave-chat')) { e.preventDefault(); group.classList.add('drop-here'); } });
  group.addEventListener('dragleave', (e) => { if (!group.contains(e.relatedTarget)) group.classList.remove('drop-here'); });
  group.addEventListener('drop', (e) => {
    const id = e.dataTransfer.getData('text/x-openclave-chat');
    group.classList.remove('drop-here');
    if (!id) return;
    e.preventDefault();
    const from = e.dataTransfer.getData('text/x-openclave-folder');
    if (from !== key) moveToFolder(id, name);
  });
  return group;
}

// Topics are the chat folders (folders.py); the button sits next to "+ new chat".
function newFolderButton(className = 'folder-add') {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = className;
  b.textContent = '+ new topic';
  b.onclick = async (e) => {
    e.stopPropagation();
    const name = prompt('Topic name');
    if (!name || !name.trim()) return;
    try { await api(`/${AGENT_ID}/folders`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }) }); }
    catch (err) { alert(err.message || 'Could not make the folder.'); }
    loadConversations();
  };
  return b;
}

function convoRow(c) {
  const el = document.createElement('div');
  el.className = 'convo-item' + (c.id === currentConversationId ? ' active' : '');
  el.innerHTML = `
    <div class="convo-row">
      <div class="convo-text">
        <div class="t">${escapeHtml(c.title)}${hasDraft(c.id) ? '<span class="draft-tag">draft</span>' : ''}</div>
        <div class="d">${fmtDate(c.date)}</div>
      </div>
      ${ROW_MENU_BUTTON}
      <div class="convo-actions" role="menu">
        <button class="convo-copy" title="Copy whole conversation to clipboard">${COPY_ICON}<span>Copy</span></button>
        <button class="convo-handover" title="Move the chat to another agent, with any related files"><svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 5h9M8 2l3 3-3 3"/><path d="M14 11H5M8 8l-3 3 3 3"/></svg><span>Move / hand over</span></button>
        <button class="convo-rename" title="Rename conversation">✎<span>Rename</span></button>
        <button class="convo-autoname" title="Auto-rename: a short name from the conversation">✨<span>Auto-rename</span></button>
        <button class="convo-archive" title="Archive: out of the list, into Recaps as a short card">▤<span>Archive</span></button>
        <button class="convo-purge" title="Purge: keep the chat and a short summary, drop the transcript (undo in the chat)">⌫<span>Purge…</span></button>
        <button class="convo-delete" title="Delete conversation">✕<span>Delete</span></button>
      </div>
    </div>`;
  el.onclick = () => openConversation(c.id);
  el.querySelector('.convo-copy').onclick = (e) => copyConversation(c.id, e);
  el.draggable = true;
  el.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/x-openclave-chat', c.id);
    e.dataTransfer.setData('text/x-openclave-folder', c.folder || '');
    e.dataTransfer.effectAllowed = 'move';
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => el.classList.remove('dragging'));
  el.querySelector('.convo-handover').onclick = async (e) => {
    // Move and hand over are one flow (2026-11-05): pick the agent by its
    // tab, as Move did, then tick any files to send along.
    e.stopPropagation();
    const target = await pickTargetAgent('this conversation', e.currentTarget, true);
    if (target) handoverConversation(c.id, target.id);
  };
  el.querySelector('.convo-rename').onclick = (e) => startRename(el, c, e);
  el.querySelector('.convo-autoname').onclick = (e) => autoRename(c.id, e);
  el.querySelector('.convo-archive').onclick = (e) => archiveConversation(c.id, e);
  el.querySelector('.convo-purge').onclick = async (e) => {
    e.stopPropagation();
    if (!await OpenClaveChat.openPurge({ agent: { id: AGENT_ID, name: AGENT_NAME }, conversationId: c.id, api })) return;
    if (c.id === currentConversationId) mountChat(c.id);
    loadConversations();
  };
  el.querySelector('.convo-delete').onclick = (e) => deleteConversation(c.id, e);
  attachRowMenu(el);
  return el;
}

function startRename(el, c, evt) {
  evt.stopPropagation();
  const textDiv = el.querySelector('.t');
  const input = document.createElement('input');
  input.className = 'convo-title-input';
  input.value = c.title;
  textDiv.replaceWith(input);
  input.focus();
  input.select();
  input.onclick = (e) => e.stopPropagation();
  const finish = (save) => {
    input.onblur = null;
    const value = input.value.trim();
    if (save && value && value !== c.title) {
      renameConversation(c.id, value);
    } else {
      loadConversations();
    }
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
}

async function renameConversation(id, title) {
  try {
    await api(`/${AGENT_ID}/conversations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
  } catch (e) {
    alert('Failed to rename conversation.');
  }
  loadConversations();
}

// One-click Auto-rename (Rick's backlog): the server names it from its first
// exchange and keeps the file's time, so it doesn't turn up as new.
async function autoRename(id, evt) {
  evt.stopPropagation();
  const btn = evt.currentTarget;
  btn.disabled = true;
  try {
    await api(`/${AGENT_ID}/conversations/${id}/auto-name`, { method: 'POST' });
  } catch (e) {
    alert(e.message || 'Auto-rename failed.');
  }
  loadConversations();
}

let moveRequestSeq = 0;
const pendingMoves = new Map();
window.addEventListener('message', event => {
  if (event.source !== parent) return;
  const msg = event.data;
  if (!msg || msg.__openclaveBridge !== true || msg.kind !== 'move-result') return;
  const resolve = pendingMoves.get(msg.id);
  if (!resolve) return;
  pendingMoves.delete(msg.id);
  resolve(msg);
});

// Choosing where to move a conversation or file (2026-09-28: no more
// typing the agent's name). The header's agent tabs light up and a hint bar
// says to click the one that should receive it; a click there picks it, Esc
// or a click anywhere else cancels. Without tabs (they failed to load) a small
// list opens next to the button instead. Resolves to {id, name} or null.
// The shell still asks the owner to confirm the move (the sandbox can't move alone).
// allowList: offer "Other agent…" (resolves to {id: ''}) for agents whose
// tabs aren't shown, e.g. in another clave; the caller then lets you pick.
async function pickTargetAgent(what, anchor, allowList = false) {
  const targets = await api(`/${AGENT_ID}/move-targets`);
  const byId = new Map(targets.map(a => [a.id, a]));
  const tabs = [...document.querySelectorAll('#agent-tabs .agent-tab')].filter(t => byId.has(t.dataset.agent));
  return new Promise(resolve => {
    const cleanup = [];
    const done = agent => { cleanup.forEach(fn => fn()); resolve(agent); };
    const on = (target, type, fn, opts) => { target.addEventListener(type, fn, opts); cleanup.push(() => target.removeEventListener(type, fn, opts)); };
    let picker;
    if (tabs.length) {
      document.body.classList.add('picking-agent');
      cleanup.push(() => document.body.classList.remove('picking-agent'));
      picker = document.getElementById('agent-tabs');
      const hint = document.createElement('div');
      hint.className = 'pick-agent-hint';
      hint.innerHTML = `Click an agent tab to move <b>${escapeHtml(what)}</b> there${allowList ? ' <button type="button" class="link-btn" data-pick-list>Other agent…</button>' : ''} <span>· Esc to cancel</span>`;
      document.body.appendChild(hint);
      cleanup.push(() => hint.remove());
    } else {
      picker = document.createElement('div');
      picker.className = 'view-pop pick-agent-list';
      picker.innerHTML = `<div class="pick-agent-title">Move to…</div>` + targets.map(a =>
        `<button type="button" class="view-item" data-agent="${escapeHtml(a.id)}">${escapeHtml(a.name)}</button>`).join('');
      const r = anchor.getBoundingClientRect();
      Object.assign(picker.style, { position: 'fixed', top: `${r.bottom + 4}px`, left: `${Math.max(8, r.left - 120)}px`, right: 'auto' });
      document.body.appendChild(picker);
      cleanup.push(() => picker.remove());
    }
    // Capture phase, so a tab (a real link) never navigates while picking.
    on(document, 'click', e => {
      if (e.target.closest?.('[data-pick-list]')) { e.preventDefault(); e.stopPropagation(); done({id: ''}); return; }
      const hit = e.target.closest?.('[data-agent]');
      if (hit && picker.contains(hit) && byId.has(hit.dataset.agent)) {
        e.preventDefault(); e.stopPropagation();
        done(byId.get(hit.dataset.agent));
      } else if (!e.target.closest?.('.agent-tabs-track, .pick-agent-list')) {
        e.preventDefault(); e.stopPropagation();
        done(null);
      }
    }, true);
    on(document, 'keydown', e => { if (e.key === 'Escape') done(null); });
  });
}

// Handover (shared dialog in chat_core.js), from the list button or
// "/handover <agent>" in the chat. The move itself goes through the shell,
// which asks the owner to confirm.
async function handoverConversation(id, hint = '') {
  try {
    await OpenClaveChat.openHandover({
      agent: { id: AGENT_ID, name: AGENT_NAME }, conversationId: id, api, targetHint: hint,
      run: async (targetAgent, files) => {
        const requestId = ++moveRequestSeq;
        const result = new Promise(resolve => pendingMoves.set(requestId, resolve));
        parent.postMessage({ __openclaveBridge: true, kind: 'handover', id: requestId, conversationId: id, targetAgent, files }, '*');
        const response = await result;
        if (response.cancelled) return null;
        if (response.error) throw new Error(response.error);
        return response.result;
      },
      onDone: () => {
        if (id === currentConversationId) mountChat(null);
        loadConversations();
      },
    });
  } catch (e) {
    alert('Handover failed: ' + e.message);
  }
}

// Archive (recaps.py): the chat leaves the list and shows up in the Recaps
// tab as a short card; Restore there brings it back whole.
async function archiveConversation(id, evt) {
  evt.stopPropagation();
  try {
    await api(`/${AGENT_ID}/conversations/${id}/archive`, { method: 'POST' });
    if (id === currentConversationId) mountChat(null);
    loadConversations();
  } catch (e) {
    alert(e.message || 'Failed to archive conversation.');
  }
}

async function deleteConversation(id, evt) {
  evt.stopPropagation();
  if (!confirm('Delete this conversation? This cannot be undone.')) return;
  try {
    await api(`/${AGENT_ID}/conversations/${id}`, { method: 'DELETE' });
    if (id === currentConversationId) mountChat(null);
    loadConversations();
  } catch (e) {
    alert('Failed to delete conversation.');
  }
}

// ── Chat: the shared implementation in /static/chat_core.js, the same code
// the mobile app runs (2026-09-27: desktop and mobile chat must stay in
// sync). This page only supplies its elements and refresh hooks. The mic and
// speaker go through the trusted shell (dashboard_shell.js), because this
// sandboxed iframe can't open the microphone itself.
let chat = null;
let chatCanAttach = true;  // every model takes photos; documents go in as text
let chatAudio = null;

function loadChatCore() {
  if (window.OpenClaveChat) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/static/chat_core.js';
    script.onload = resolve;
    script.onerror = () => reject(new Error('chat_core.js failed to load'));
    document.head.appendChild(script);
  });
}

function mountChat(conversationId) {
  chat?.destroy();
  currentConversationId = conversationId;
  chatAudio ??= OpenClaveChat.createBridgeAudio();
  chat = OpenClaveChat.mountChat({
    agent: {id: AGENT_ID, name: AGENT_NAME}, conversationId, api,
    form: document.querySelector('.composer'),
    messages: document.getElementById('chat-window'),
    markdown: text => DOMPurify.sanitize(marked.parse(text)),
    externalLinkTarget: '_top',
    canAttach: chatCanAttach, canTalk: true, audio: chatAudio,
    emptyClass: 'empty', enterSends: true,
    onConversationChange: id => { currentConversationId = id; loadConversations(); },
    // The shell (trusted) opens the other agent's page with the text as draft.
    onForward: (targetAgent, text) => parent.postMessage({__openclaveBridge: true, kind: 'forward', targetAgent, text}, '*'),
    onHandover: (id, hint) => handoverConversation(id, hint),
    // The shell (trusted) opens that agent's page, which shows its latest chat.
    onAutojump: agentId => parent.postMessage({__openclaveBridge: true, kind: 'open-agent', targetAgent: agentId}, '*'),
    onReply: () => {
      loadConversations();
      if (!document.getElementById('recaps-pane')?.hidden) loadRecaps();  // a chat picked up again leaves Recaps
      loadBacklog();
      refreshModelResponseTimes();
      if (typeof onAfterSend === 'function') onAfterSend();
    },
  });
}

function openConversation(id) {
  mountChat(id);
  loadConversations();
}

marked.setOptions({ breaks: true, gfm: true });

async function loadPlan() {
  const panel = document.getElementById('plan-panel');
  try {
    const plan = await api(`/${AGENT_ID}/plan`);
    if (!plan.phases.length) { panel.innerHTML = '<div class="empty">No plan yet.</div>'; }
    else {
      panel.innerHTML = plan.phases.map(p => `
        <div class="phase ${p.status}">
          <div class="row"><span class="name">${escapeHtml(p.name)}</span><span class="pct">${p.percent}%</span></div>
          <div class="bar"><div style="width:${p.percent}%"></div></div>
        </div>`).join('');
    }
  } catch (e) {
    panel.innerHTML = '<div class="empty">Failed to load plan.</div>';
  }
}

let backlogItems = [];
let kanbanStages = {};

const KANBAN_COLS = [
  { stage: 'backlog', title: 'Backlog' },
  { stage: 'in-progress', title: 'In Progress' },
  { stage: 'done', title: 'Done' },
];

async function loadBacklog() {
  try {
    const [backlog, kanban] = await Promise.all([api(`/${AGENT_ID}/backlog`), api(`/${AGENT_ID}/kanban`)]);
    backlogItems = backlog.items;
    kanbanStages = {};
    kanban.items.forEach(i => { kanbanStages[i.text] = i.stage; });
    renderBacklog();
    renderKanban();
  } catch (e) {
    document.getElementById('backlog-panel').innerHTML = '<div class="empty">Failed to load backlog.</div>';
  }
}

function renderBacklog() {
  const panel = document.getElementById('backlog-panel');
  if (!panel) return;
  if (!backlogItems.length) { panel.innerHTML = '<div class="empty">Backlog empty.</div>'; return; }
  panel.innerHTML = backlogItems.map(i => {
    const flagged = !!kanbanStages[i.text];
    const title = flagged ? 'In Kanban — click to remove' : 'Click to flag into Kanban';
    return `
      <div class="backlog-item ${i.done ? 'done' : ''}">
        <div class="checkbox ${i.done ? 'done' : ''} ${flagged ? 'flagged' : ''}" title="${title}" onclick="toggleFlag(${escapeAttr(i.text)})"></div>
        <div>${escapeHtml(i.text)}</div>
      </div>`;
  }).join('');
}

function renderKanban() {
  const board = document.getElementById('kanban-board');
  board.innerHTML = KANBAN_COLS.map((col, idx) => {
    const items = Object.entries(kanbanStages).filter(([, s]) => s === col.stage).map(([t]) => t);
    const prevStage = idx > 0 ? KANBAN_COLS[idx - 1].stage : null;
    const nextStage = idx < KANBAN_COLS.length - 1 ? KANBAN_COLS[idx + 1].stage : null;
    const cards = items.length ? items.map(text => `
      <div class="kanban-card ${col.stage === 'done' ? 'done' : ''}">
        <div class="kanban-card-title">${escapeHtml(text)}</div>
        <div class="kanban-card-actions">
          ${prevStage ? `<button class="kanban-card-btn" onclick="setKanbanStage(${escapeAttr(text)}, ${escapeAttr(prevStage)})">&larr;</button>` : ''}
          ${nextStage ? `<button class="kanban-card-btn" onclick="setKanbanStage(${escapeAttr(text)}, ${escapeAttr(nextStage)})">&rarr;</button>` : ''}
          <button class="kanban-card-btn" onclick="setKanbanStage(${escapeAttr(text)}, null)">&times;</button>
        </div>
      </div>`).join('') : '<div class="kanban-empty">Nothing here.</div>';
    return `
      <div class="kanban-col">
        <div class="kanban-col-title">${col.title}</div>
        ${cards}
      </div>`;
  }).join('<div class="resize-handle"></div>');
  applyKanbanWidths();
  initKanbanResize();
}

const KB_LAYOUT_KEY = 'kanban_widths';
let kbWidths = [1, 1, 1];
try {
  const savedKb = JSON.parse(localStorage.getItem(KB_LAYOUT_KEY) || 'null');
  if (Array.isArray(savedKb) && savedKb.length === 3) kbWidths = savedKb;
} catch (e) {}

function applyKanbanWidths() {
  const board = document.getElementById('kanban-board');
  board.style.gridTemplateColumns = `${kbWidths[0]}fr 6px ${kbWidths[1]}fr 6px ${kbWidths[2]}fr`;
}

function initKanbanResize() {
  const board = document.getElementById('kanban-board');
  board.querySelectorAll('.resize-handle').forEach((handle, i) => {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidths = kbWidths.slice();
      const totalFr = startWidths[0] + startWidths[1] + startWidths[2];
      const pxPerFr = (board.getBoundingClientRect().width - 12) / totalFr;
      const a = i, b = i + 1;
      const minFr = 0.3;
      handle.classList.add('dragging');
      function onMove(ev) {
        const deltaFr = (ev.clientX - startX) / pxPerFr;
        let newA = startWidths[a] + deltaFr;
        let newB = startWidths[b] - deltaFr;
        if (newA < minFr) { newB -= (minFr - newA); newA = minFr; }
        if (newB < minFr) { newA -= (minFr - newB); newB = minFr; }
        kbWidths[a] = Math.max(minFr, newA);
        kbWidths[b] = Math.max(minFr, newB);
        applyKanbanWidths();
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        handle.classList.remove('dragging');
        try { localStorage.setItem(KB_LAYOUT_KEY, JSON.stringify(kbWidths)); } catch (e) {}
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

async function toggleFlag(itemText) {
  await setKanbanStage(itemText, kanbanStages[itemText] ? null : 'backlog');
}

async function setKanbanStage(itemText, stage) {
  try {
    await api(`/${AGENT_ID}/kanban`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item: itemText, stage }),
    });
    loadBacklog();
  } catch (e) {}
}

// ── Image attachments (Backlog.md "paste images into chat") ──────────────
// Injected into the DOM at runtime rather than requiring markup in every
// agent's own dashboard.html — same reasoning as the rest of this shared
// file: one fix/feature, not seven. Only injected when this agent's
// currently-selected model actually supports vision (checked once in
// initDashboardCommon), so the button never appears somewhere it would just
// get rejected.

const modelDisplayNames = new Map();
async function refreshModelResponseTimes() {
  const select = document.getElementById('model-select');
  if (!select) return;
  try {
    const responseTimes = await api(`/${AGENT_ID}/model-response-times`);
    for (const option of select.options) {
      const name = modelDisplayNames.get(option.value) || option.value;
      const timing = responseTimes[option.value];
      option.textContent = name + costSuffix(option.value) + (timing ? ` · ~${(timing.average_ms / 1000).toFixed(1)}s avg (${timing.samples})` : '') + supportsSuffix(option.value);
    }
  } catch (e) { /* Timing is optional; chat remains usable. */ }
}

// Models that support this agent's connections (connections.py) are tagged
// in the picker, e.g. "Claude Sonnet 4.6 — HA supported".
const modelSupports = new Map();
const supportsSuffix = id => (modelSupports.get(id) || []).length ? ` — ${modelSupports.get(id).join(', ')} supported` : '';

// Cost tier (llm_models.cost_tier, 2026-10-12): "$$" in each option and
// a dot in the tier's colour left of the picker, following the chosen model.
const modelCosts = new Map();
const costSuffix = id => modelCosts.get(id)?.cost_label ? ` · ${modelCosts.get(id).cost_label}` : '';
function drawCostDot(select) {
  let dot = document.getElementById('model-cost-dot');
  if (!dot) { select.insertAdjacentHTML('beforebegin', '<span class="cost-dot" id="model-cost-dot"></span>'); dot = document.getElementById('model-cost-dot'); }
  const m = modelCosts.get(select.value);
  dot.classList.toggle('unknown', !m?.cost_color);
  dot.style.background = m?.cost_color || '';
  dot.title = m?.cost_color ? `${m.cost_label} · API $${m.api_output_usd}/M output` : 'Price unknown';
}

function injectModelSelector(models, current, responseTimes = {}) {
  models.forEach(m => { modelDisplayNames.set(m.id, m.display_name); modelSupports.set(m.id, m.supports || []); modelCosts.set(m.id, m); });
  const headerRight = document.querySelector('.header-right');
  if (!headerRight || document.getElementById('model-select')) return;
  const option = m => {
    const timing = responseTimes[m.id];
    const average = timing ? ` · ~${(timing.average_ms / 1000).toFixed(1)}s avg (${timing.samples})` : '';
    return `<option value="${m.id}" ${m.id === current.id ? 'selected' : ''}>${escapeHtml(m.display_name + costSuffix(m.id) + average + supportsSuffix(m.id))}</option>`;
  };
  // Grouped API fast / API slow / CLI fast / CLI slow, in the order /models returns them.
  const groups = new Map();
  for (const m of models) {
    const key = m.group || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }
  const order = ['API · fast', 'API · slow', 'CLI · fast', 'CLI · slow'];
  const options = [...groups.entries()]
    .sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
    .map(([g, ms]) => g ? `<optgroup label="${escapeHtml(g)}">${ms.map(option).join('')}</optgroup>` : ms.map(option).join(''))
    .join('');
  const picker = `<select class="model-select" id="model-select" title="Change model">${options}</select>`;
  const viewMenu = document.getElementById('view-menu');
  if (viewMenu) viewMenu.insertAdjacentHTML('afterend', picker);
  else headerRight.insertAdjacentHTML('afterbegin', picker);
  let currentModel = current.id;
  drawCostDot(document.getElementById('model-select'));
  // The table picker (static/model_picker.js): every model with Vendor, Route,
  // Speed, Latency, Price and Released columns, sortable. It sits over the
  // select, so the change handler below is unchanged.
  const withPicker = cb => {
    if (window.OpenClaveModelPicker) { cb(); return; }
    const s = document.createElement('script');
    s.src = '/static/model_picker.js';
    s.onload = cb;
    document.head.appendChild(s);
  };
  withPicker(() => window.OpenClaveModelPicker.enhance(document.getElementById('model-select'), models));
  document.getElementById('model-select').addEventListener('change', async e => {
    const modelId = e.target.value;
    drawCostDot(e.target);
    try {
      const chosen = await api(`/${AGENT_ID}/model`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ model_id: modelId }) });
      currentModel = modelId;
      // Photo button follows the new model's vision support.
      const canAttach = true;  // every model takes photos, and documents go in as text
      if (canAttach !== chatCanAttach && window.OpenClaveChat) { chatCanAttach = canAttach; mountChat(currentConversationId); }
    } catch (err) {
      e.target.value = currentModel;
      drawCostDot(e.target);
      window.OpenClaveModelPicker?.refresh(e.target);
      alert('Failed to change model.');
    }
  });
}

async function initConversations() {
  let convos = [];
  try { convos = await api(`/${AGENT_ID}/conversations`); } catch (e) {}
  // "#new" on the page URL (e.g. a message forwarded from another agent):
  // start a new conversation, where the forwarded text waits as the draft.
  if (convos.length && !window.OPENCLAVE_START_NEW) {
    openConversation(convos[0].id);
  } else {
    mountChat(null);
    loadConversations();
  }
}

// ── Files tab: sits next to "Conversations" in the left column and edits
// this agent's workspace files in place of the chat (same idea as the
// Control Panel's mission/persona editor). Injected here rather than in each
// agent's own dashboard.html so every agent gets it without rewriting those.
let openFile = null;  // { path, version, saved } while the editor is showing

function fileDirty() {
  const area = document.getElementById('file-editor-text');
  return !!(openFile && area && area.value !== openFile.saved);
}

function confirmDiscardFile() {
  return !fileDirty() || confirm(`Discard unsaved changes to ${openFile.path}?`);
}

function closeFileEditor() {
  openFile = null;
  document.querySelector('.col-mid')?.classList.remove('editing-file');
  document.querySelectorAll('.file-item.active').forEach(el => el.classList.remove('active'));
}

function showLeftTab(tab) {
  document.querySelectorAll('.left-tab').forEach(b => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('active', on);
    // Many tabs in a narrow column: bring the chosen one into view.
    if (on) b.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'smooth'});
  });
  document.getElementById('convo-pane').hidden = tab !== 'convos';
  document.getElementById('files-pane').hidden = tab !== 'files';
  document.getElementById('recaps-pane').hidden = tab !== 'recaps';
  if (tab === 'files') loadFiles();
  if (tab === 'recaps') loadRecaps();
  showEmbed(EMBED_VIEWS[tab] || null);
}

// The tab row scrolls sideways when the column is too narrow for every tab;
// ‹ › appear at whichever end has more, and the mouse wheel scrolls it too.
function wireTabArrows(bar) {
  const row = bar.querySelector('.left-tabs');
  const [back, fwd] = bar.querySelectorAll('.tabs-arrow');
  const update = () => {
    back.hidden = row.scrollLeft <= 1;
    fwd.hidden = row.scrollLeft + row.clientWidth >= row.scrollWidth - 1;
  };
  const step = dir => row.scrollBy({left: dir * Math.max(80, row.clientWidth * 0.6), behavior: 'smooth'});
  back.onclick = () => step(-1);
  fwd.onclick = () => step(1);
  row.addEventListener('scroll', update, {passive: true});
  row.addEventListener('wheel', e => {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && row.scrollWidth > row.clientWidth) { row.scrollLeft += e.deltaY; e.preventDefault(); }
  }, {passive: false});
  new ResizeObserver(update).observe(row);
  update();
}

// Recaps (recaps.py): archived chats as a compact list: name, how it ended,
// last active. A click opens it in the chat on the right, with its recap on
// top and the whole transcript below; writing a message brings it back.
const RECAP_LABEL = { concluded: 'Concluded', open: 'Left open', dropped: 'Dropped' };
async function loadRecaps() {
  const list = document.getElementById('recaps-list');
  if (!list) return;
  try {
    const { recaps } = await api(`/${AGENT_ID}/recaps`);
    if (!recaps.length) { list.innerHTML = '<div class="empty">Nothing archived yet. Archive a chat from its ⋯ menu.</div>'; return; }
    list.innerHTML = '';
    for (const r of recaps) {
      const el = document.createElement('div');
      el.className = 'convo-item recap-item' + (r.id === currentConversationId ? ' active' : '');
      el.title = r.about || '';
      el.innerHTML = `<div class="t">${escapeHtml(r.title)}</div>
        <div class="d"><span class="recap-dot ${escapeHtml(r.status || 'none')}"></span>${escapeHtml(r.pending ? 'Writing recap…' : (RECAP_LABEL[r.status] || 'No recap yet'))} · ${fmtDate(r.last_active)}</div>`;
      el.onclick = () => {
        mountChat(r.id);
        list.querySelectorAll('.recap-item').forEach(x => x.classList.toggle('active', x === el));
      };
      list.appendChild(el);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">Failed to load recaps.</div>';
  }
}

// ── Dash and Tools tabs (2026-10-16): the phone app's screens for this
// agent (its own page, its tool ticks) in this same left pane, like Chat and
// Files. This sandboxed frame can't load them, so the trusted shell
// (dashboard_shell.js) lays the screen exactly over #embed-pane and follows
// its size. The pane is at least EMBED_MIN_W wide (the screens are built for
// a phone) and widens further when the screen says it needs more, up to
// EMBED_MAX_SHARE of the window. Your own column width is kept for Chat and Files.
const EMBED_VIEWS = { dash: 'console', tools: 'tools', skills: 'skills' };
const EMBED_MIN_W = 420, EMBED_MAX_SHARE = 0.6;
let embedView = null, embedObserver = null, embedWant = 0;

function embedWidth() {
  return Math.round(Math.min(Math.max(EMBED_MIN_W, embedWant), window.innerWidth * EMBED_MAX_SHARE));
}

function postEmbed() {
  const pane = document.getElementById('embed-pane');
  const r = embedView && pane && !pane.hidden ? pane.getBoundingClientRect() : null;
  parent.postMessage({ __openclaveBridge: true, kind: 'embed-pane',
    view: r && r.width > 0 && r.height > 0 ? embedView : null,
    rect: r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null }, '*');
}

function showEmbed(view) {
  const pane = document.getElementById('embed-pane');
  const layout = document.querySelector('.layout');
  if (!pane) return;
  if (view !== embedView) embedWant = 0;
  embedView = view;
  pane.hidden = !view;
  pane.parentElement.classList.toggle('embed-mode', !!view);
  if (layout) {
    layout.classList.toggle('embed-open', !!view);
    layout.style.setProperty('--embed-w', embedWidth() + 'px');
  }
  if (view && !embedObserver) {
    embedObserver = new ResizeObserver(postEmbed);
    embedObserver.observe(pane);
    window.addEventListener('resize', () => {
      document.querySelector('.layout')?.style.setProperty('--embed-w', embedWidth() + 'px');
      postEmbed();
    });
  }
  postEmbed();
}

// The shell passes on how wide the screen's content wants to be.
window.addEventListener('message', e => {
  const m = e.data;
  if (e.source !== window.parent || !m || m.__openclaveBridge !== true || m.kind !== 'embed-want') return;
  const want = Number(m.width);
  if (!embedView || m.view !== embedView || !Number.isFinite(want) || want <= embedWant) return;
  embedWant = want;
  document.querySelector('.layout')?.style.setProperty('--embed-w', embedWidth() + 'px');
});

function fmtSize(n) {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
}

async function loadFiles() {
  const list = document.getElementById('files-list');
  try {
    const files = await api(`/${AGENT_ID}/files`);
    if (!files.length) { list.innerHTML = '<div class="empty">No editable files.</div>'; return; }
    list.innerHTML = '';
    for (const f of files) {
      const el = document.createElement('div');
      el.className = 'convo-item file-item' + (openFile && openFile.path === f.path ? ' active' : '');
      el.title = f.path;
      // Core files (plan, backlog, mission…) can be copied and edited, but
      // not renamed, moved or deleted — the agent's tools need them by name.
      const manage = f.protected ? '' : `
            <button class="convo-move" title="Move file to another agent">⇢<span>Move to agent</span></button>
            <button class="convo-rename" title="Rename file">✎<span>Rename</span></button>
            <button class="convo-delete" title="Delete file">✕<span>Delete</span></button>`;
      el.innerHTML = `
        <div class="convo-row">
          <div class="convo-text">
            <div class="t">${escapeHtml(f.path)}</div>
            <div class="d">${escapeHtml(f.modified)} · ${fmtSize(f.size)}</div>
          </div>
          ${ROW_MENU_BUTTON}
          <div class="convo-actions" role="menu">
            <button class="convo-copy" title="Copy file contents to clipboard">${COPY_ICON}<span>Copy</span></button>${manage}
          </div>
        </div>`;
      el.onclick = () => openFileEditor(f.path);
      el.querySelector('.convo-copy').onclick = (e) => copyFile(f.path, e);
      if (!f.protected) {
        el.querySelector('.convo-move').onclick = (e) => moveFile(f.path, e);
        el.querySelector('.convo-rename').onclick = (e) => startFileRename(el, f.path, e);
        el.querySelector('.convo-delete').onclick = (e) => deleteFile(f.path, e);
      }
      attachRowMenu(el);
      list.appendChild(el);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">Failed to load files.</div>';
  }
}

const COPY_ICON = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H3a1.5 1.5 0 0 0-1.5 1.5v6A1.5 1.5 0 0 0 3 10.5h.5"/></svg>';

// Before renaming/moving/deleting the file that's open: unsaved edits get a
// chance to be kept, and the editor closes since its file is going away.
function releaseOpenFile(path) {
  if (!openFile || openFile.path !== path) return true;
  if (!confirmDiscardFile()) return false;
  closeFileEditor();
  return true;
}

async function copyFile(path, e) {
  e.stopPropagation();
  const btn = e.currentTarget;
  const original = btn.innerHTML;
  try {
    const f = await api(`/${AGENT_ID}/files/content?path=${encodeURIComponent(path)}`);
    if (!await writeClipboard(f.content)) throw new Error('copy refused');
    btn.textContent = '✓ Copied';
  } catch (err) {
    btn.textContent = '! Copy failed';
    btn.title = 'Copy failed';
  }
  setTimeout(() => { btn.innerHTML = original; btn.title = 'Copy file contents to clipboard'; }, 1500);
}

function startFileRename(el, path, evt) {
  evt.stopPropagation();
  const textDiv = el.querySelector('.t');
  const input = document.createElement('input');
  input.className = 'convo-title-input';
  input.value = path;
  textDiv.replaceWith(input);
  input.focus();
  // Select the name without the extension, like a file manager.
  const dot = path.lastIndexOf('.');
  input.setSelectionRange(path.lastIndexOf('/') + 1, dot > path.lastIndexOf('/') ? dot : path.length);
  input.onclick = (e) => e.stopPropagation();
  const finish = (save) => {
    input.onblur = null;
    const value = input.value.trim();
    if (save && value && value !== path) renameFile(path, value);
    else loadFiles();
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
}

async function renameFile(path, newPath) {
  const wasOpen = openFile && openFile.path === path;
  if (!releaseOpenFile(path)) { loadFiles(); return; }
  try {
    const res = await api(`/${AGENT_ID}/files/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, new_path: newPath }),
    });
    await loadFiles();
    if (wasOpen) openFileEditor(res.path);
  } catch (e) {
    alert(e.message || 'Failed to rename file.');
    loadFiles();
  }
}

async function moveFile(path, evt) {
  evt.stopPropagation();
  try {
    const target = await pickTargetAgent(path, evt.currentTarget);
    if (!target) return;
    if (!releaseOpenFile(path)) return;
    const requestId = ++moveRequestSeq;
    const result = new Promise(resolve => pendingMoves.set(requestId, resolve));
    parent.postMessage({ __openclaveBridge: true, kind: 'move-file', id: requestId, path, targetAgent: target.id }, '*');
    const response = await result;
    if (response.error) { alert(response.error); return; }
    loadFiles();
  } catch (e) {
    alert('Failed to move file: ' + e.message);
  }
}

async function deleteFile(path, evt) {
  evt.stopPropagation();
  if (!confirm(`Delete ${path}? This cannot be undone.`)) return;
  if (openFile && openFile.path === path) closeFileEditor();  // deleting anyway, no need to ask about edits
  try {
    await api(`/${AGENT_ID}/files/content?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
  } catch (e) {
    alert(e.message || 'Failed to delete file.');
  }
  loadFiles();
}

// Markdown files open formatted (Rick's backlog: render mission, plan,
// notes as markdown instead of raw text); Edit shows the text to change.
const isMarkdownPath = path => /\.md$/i.test(path);

function setFileMode(mode) {
  const view = document.getElementById('file-editor-view');
  const area = document.getElementById('file-editor-text');
  const btn = document.getElementById('file-editor-mode');
  const markdownFile = !!openFile && isMarkdownPath(openFile.path);
  btn.hidden = !markdownFile;
  const showView = markdownFile && mode === 'view';
  if (showView) view.innerHTML = DOMPurify.sanitize(marked.parse(stripFrontmatter(area.value)));
  view.hidden = !showView;
  area.hidden = showView;
  document.getElementById('file-editor-save').hidden = showView;
  btn.textContent = showView ? 'Edit' : 'Preview';
  if (!showView && !area.disabled) area.focus();
}

function stripFrontmatter(text) {
  return text.replace(/^---\n[\s\S]*?\n---\n?/, '');
}

async function openFileEditor(path) {
  if (openFile && openFile.path === path) return;
  if (!confirmDiscardFile()) return;
  const mid = document.querySelector('.col-mid');
  const area = document.getElementById('file-editor-text');
  const status = document.getElementById('file-editor-status');
  document.getElementById('file-editor-path').textContent = path;
  status.textContent = 'loading…';
  area.value = '';
  area.disabled = true;
  mid.classList.add('editing-file');
  openFile = { path, version: null, saved: '' };
  document.querySelectorAll('.file-item').forEach(el => el.classList.toggle('active', el.title === path));
  try {
    const f = await api(`/${AGENT_ID}/files/content?path=${encodeURIComponent(path)}`);
    if (!openFile || openFile.path !== path) return;
    openFile = { path, version: f.version, saved: f.content };
    area.value = f.content;
    area.disabled = false;
    status.textContent = '';
    setFileMode(isMarkdownPath(path) ? 'view' : 'edit');
  } catch (e) {
    status.textContent = e.message || 'Failed to load';
  }
}

async function saveFile() {
  if (!openFile || openFile.version == null) return;
  const area = document.getElementById('file-editor-text');
  const status = document.getElementById('file-editor-status');
  const { path } = openFile;
  status.textContent = 'saving…';
  try {
    const f = await api(`/${AGENT_ID}/files/content`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content: area.value, version: openFile.version }),
    });
    if (!openFile || openFile.path !== path) return;
    openFile.version = f.version;
    openFile.saved = f.content;
    status.textContent = 'Saved';
    loadFiles();
    if (path === 'plan.md' && typeof loadPlan === 'function') loadPlan();
    if (path === 'backlog.md' || path === 'kanban.json') loadBacklog();
  } catch (e) {
    status.textContent = e.message || 'Failed to save';
  }
}

async function createFile() {
  if (!confirmDiscardFile()) return;
  const name = prompt('New file name (e.g. ideas.md or memory/notes.md):');
  if (!name || !name.trim()) return;
  try {
    const f = await api(`/${AGENT_ID}/files`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: name.trim() }),
    });
    openFile = null;  // already confirmed above
    await loadFiles();
    openFileEditor(f.path);
  } catch (e) {
    alert(e.message || 'Could not create file.');
  }
}

function initFilesTab() {
  const convoList = document.getElementById('convo-list');
  const mid = document.querySelector('.col-mid');
  if (!convoList || !mid) return;
  const title = convoList.previousElementSibling;
  const newConvo = document.getElementById('new-convo');
  // Agent-written pages still say "+ new conversation"; the tab is "Chat".
  if (newConvo) newConvo.textContent = '+ new chat';

  const tabBar = document.createElement('div');
  tabBar.className = 'left-tabs-bar';
  tabBar.innerHTML = '<button type="button" class="tabs-arrow" aria-label="More tabs to the left" hidden>‹</button><div class="left-tabs"></div><button type="button" class="tabs-arrow" aria-label="More tabs to the right" hidden>›</button>';
  const tabs = tabBar.querySelector('.left-tabs');
  // Dash (the agent's own page) and Tools show the phone app's screens in this
  // pane; see showEmbed.
  tabs.innerHTML = `<button class="left-tab active" data-tab="convos">Chat</button><button class="left-tab" data-tab="files">Files</button><button class="left-tab" data-tab="dash" title="This agent's own page, as on the phone">Dash</button><button class="left-tab" data-tab="tools" title="Tick this agent's tools on/off">Tools</button><button class="left-tab" data-tab="skills" title="Know-how this agent loads when needed">Skills</button><button class="left-tab" data-tab="recaps" title="Archived chats as short cards: what each was about and how it ended">Recaps</button>`;
  const convoPane = document.createElement('div');
  convoPane.id = 'convo-pane';
  const filesPane = document.createElement('div');
  filesPane.id = 'files-pane';
  filesPane.hidden = true;
  filesPane.innerHTML = '<button class="new-convo" id="new-file">+ new file</button><div id="files-list"><div class="empty">loading…</div></div>';

  const first = newConvo && newConvo.parentElement === convoList.parentElement ? newConvo : convoList;
  first.before(tabBar);
  tabBar.after(convoPane);
  if (newConvo && newConvo.parentElement === convoList.parentElement) {
    const row = document.createElement('div');
    row.className = 'new-row';
    const topic = newFolderButton('new-convo');
    topic.id = 'new-topic';
    row.append(newConvo, topic);
    convoPane.appendChild(row);
  }
  if (title && title.classList.contains('section-title')) title.remove();
  convoPane.appendChild(convoList);
  convoPane.after(filesPane);
  const recapsPane = document.createElement('div');
  recapsPane.id = 'recaps-pane';
  recapsPane.hidden = true;
  recapsPane.innerHTML = '<div id="recaps-list"><div class="empty">loading…</div></div>';
  filesPane.after(recapsPane);
  const embedPane = document.createElement('div');
  embedPane.id = 'embed-pane';
  embedPane.hidden = true;
  recapsPane.after(embedPane);
  tabs.querySelectorAll('.left-tab').forEach(b => { b.onclick = () => showLeftTab(b.dataset.tab); });
  wireTabArrows(tabBar);
  document.getElementById('new-file').onclick = createFile;

  const editor = document.createElement('div');
  editor.id = 'file-editor';
  editor.innerHTML = `
    <div class="file-editor-head">
      <span id="file-editor-path"></span>
      <span id="file-editor-status" class="usage-line"></span>
      <button id="file-editor-mode" hidden>Edit</button>
      <button id="file-editor-save">Save</button>
      <button id="file-editor-close" title="Back to chat">✕</button>
    </div>
    <div id="file-editor-view" class="md-view" hidden></div>
    <textarea id="file-editor-text" spellcheck="false"></textarea>`;
  mid.appendChild(editor);
  document.getElementById('file-editor-save').onclick = saveFile;
  document.getElementById('file-editor-mode').onclick = () => setFileMode(!document.getElementById('file-editor-view').hidden ? 'edit' : 'view');
  document.getElementById('file-editor-close').onclick = () => { if (confirmDiscardFile()) closeFileEditor(); };
  const area = document.getElementById('file-editor-text');
  area.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveFile(); }
  });
  area.addEventListener('input', () => {
    document.getElementById('file-editor-status').textContent = fileDirty() ? 'unsaved' : '';
  });

  // Going back to a conversation closes the editor (asking first if unsaved).
  convoList.addEventListener('click', e => {
    if (!openFile) return;
    if (!confirmDiscardFile()) { e.stopPropagation(); return; }
    closeFileEditor();
  }, true);
  if (newConvo) newConvo.addEventListener('click', e => {
    if (!openFile) return;
    if (!confirmDiscardFile()) { e.stopImmediatePropagation(); return; }
    closeFileEditor();
  }, true);
}

// ── Agent tabs (desktop): every agent as a browser-style tab in the header,
// between the name and the model picker (2026-09-28). This agent's tab
// is there too, highlighted, so the tabs never reshuffle when switching. A
// tab is a plain link that navigates the real browser tab (target=_top; the
// sandbox allows top navigation on a click). Drag a tab to rearrange; the
// order is saved on the server, the same order as the dashboard tiles. If the
// tabs don't fit, hovering near the right or left edge scrolls them; the
// mouse wheel does too.
// The header circle starts as initials (templates/agent_dashboard.html);
// swap in the avatar the dashboard tile shows, when the agent has one.
async function injectHeaderAvatar() {
  const icon = document.querySelector('header .icon');
  if (!icon) return;
  let data;
  try { data = await api(`/${AGENT_ID}/avatar-data`); } catch (e) { return; }
  if (!data?.data_url) return;
  const img = document.createElement('img');
  img.src = data.data_url;
  img.alt = '';
  img.style.cssText = 'width:100%;height:100%;border-radius:50%;display:block';
  icon.replaceChildren(img);
}

async function injectAgentTabs() {
  const header = document.querySelector('header');
  const title = header?.querySelector('h1');
  if (!header || !title || document.getElementById('agent-tabs')) return;
  let tabs;
  try { tabs = await api(`/${AGENT_ID}/agent-tabs`); } catch (e) { return; }
  if (!tabs.length) return;
  const strip = document.createElement('nav');
  strip.id = 'agent-tabs';
  strip.className = 'agent-tabs';
  strip.setAttribute('aria-label', 'Other agents');
  // Control Panel → Sorting → "latest message first": no dragging then.
  const byRecent = tabs[0].sorted === 'recent';
  strip.innerHTML = `<div class="agent-tabs-track">${tabs.map(t =>
    `<a class="agent-tab${t.current ? ' current' : ''}" data-agent="${escapeHtml(t.id)}" href="/${encodeURIComponent(t.id)}" target="_top" draggable="${byRecent ? 'false' : 'true'}"${t.current ? ' aria-current="page"' : ''} title="${t.current ? escapeHtml(t.name) + ' (this page)' : 'Go to ' + escapeHtml(t.name)}${byRecent ? ' · latest message first (Control Panel → Sorting)' : ' · drag to rearrange'}"><span class="agent-tab-dot" style="background:${escapeHtml(t.color || 'var(--accent)')}"></span>${escapeHtml(t.name)}</a>`
  ).join('')}</div>`;
  title.after(strip);
  const track = strip.querySelector('.agent-tabs-track');
  // Clicking this agent's own tab does nothing (it's already open).
  track.addEventListener('click', e => { if (e.target.closest('.agent-tab.current')) e.preventDefault(); });

  // Drag to rearrange: the dragged tab moves live under the pointer, and the
  // new order is saved on drop.
  let dragged = null;
  track.addEventListener('dragstart', e => {
    dragged = e.target.closest?.('.agent-tab');
    if (!dragged || document.body.classList.contains('picking-agent')) { e.preventDefault(); dragged = null; return; }
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragged.dataset.agent);
    requestAnimationFrame(() => dragged?.classList.add('dragging'));
  });
  track.addEventListener('dragover', e => {
    if (!dragged) return;
    e.preventDefault();
    const over = e.target.closest?.('.agent-tab');
    if (over && over !== dragged) {
      const r = over.getBoundingClientRect();
      if (e.clientX < r.left + r.width / 2) over.before(dragged); else over.after(dragged);
    }
    edgeScroll(e.clientX);
  });
  track.addEventListener('drop', e => { if (dragged) e.preventDefault(); });
  track.addEventListener('dragend', async () => {
    if (!dragged) return;
    dragged.classList.remove('dragging');
    dragged = null;
    speed = 0;
    const order = [...track.querySelectorAll('.agent-tab')].map(t => t.dataset.agent);
    if (order.join() === tabs.map(t => t.id).join()) return;
    try {
      await api(`/${AGENT_ID}/agent-tabs/order`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ order }) });
      tabs.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
    } catch (err) { console.error('saving tab order failed:', err); }
  });

  const EDGE = 70;        // px from either edge that starts scrolling
  const MAX_SPEED = 9;    // px per frame at the very edge
  let speed = 0, frame = null;
  const updateFades = () => {
    strip.classList.toggle('more-left', track.scrollLeft > 1);
    strip.classList.toggle('more-right', track.scrollLeft + track.clientWidth < track.scrollWidth - 1);
  };
  const step = () => {
    if (!speed) { frame = null; return; }
    track.scrollLeft += speed;
    updateFades();
    frame = requestAnimationFrame(step);
  };
  function edgeScroll(x) {
    const rect = track.getBoundingClientRect();
    const fromRight = rect.right - x, fromLeft = x - rect.left;
    if (fromRight < EDGE && track.scrollLeft + track.clientWidth < track.scrollWidth) speed = MAX_SPEED * (1 - fromRight / EDGE) + 1;
    else if (fromLeft < EDGE && track.scrollLeft > 0) speed = -(MAX_SPEED * (1 - fromLeft / EDGE) + 1);
    else speed = 0;
    if (speed && !frame) frame = requestAnimationFrame(step);
  }
  track.addEventListener('mousemove', e => edgeScroll(e.clientX));
  track.addEventListener('mouseleave', () => { speed = 0; });
  track.addEventListener('dragleave', e => { if (!track.contains(e.relatedTarget)) speed = 0; });
  track.addEventListener('wheel', e => {
    if (track.scrollWidth <= track.clientWidth) return;
    e.preventDefault();
    track.scrollLeft += Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    updateFades();
  }, {passive: false});
  track.addEventListener('scroll', updateFades);
  window.addEventListener('resize', updateFades);
  track.querySelector('.agent-tab.current')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  updateFades();
}

// ── View menu (desktop): a "View" button left of the model picker with a
// checkbox per pane, so the owner can hide the ones they don't need (2026-09-28).
// Panes are found from the page's standard structure, since every agent's
// dashboard.html is agent-editable: Chat (the conversation list,
// .col-left), the right column's top section (Current Plan, or Reba's
// Progress), Backlog (.col-right .backlog) and Kanban. The chat itself
// (.col-mid) always stays (the owner didn't want it hideable) and takes the space
// the hidden panes leave. The choice is shared by every agent page
// (SHARED_KEYS in dashboard_shell.js).
const PANES_KEY = 'openclave.panes';

function viewPanes() {
  const layout = document.querySelector('.layout');
  if (!layout) return [];
  const right = layout.querySelector('.col-right');
  const backlog = right?.querySelector(':scope > .backlog');
  const top = right ? [...right.children].filter(el => el !== backlog) : [];
  const kanban = layout.querySelector('.kanban-panel');
  const panes = [
    { id: 'conversations', label: 'Chat', els: [layout.querySelector('.col-left')] },
    { id: 'plan', label: top.find(el => el.classList.contains('section-title'))?.textContent.trim() || 'Current Plan', els: top },
    { id: 'backlog', label: 'Backlog', els: [backlog] },
    // Reba's kanban sits inside a .bottom-row; hide that whole grid row.
    { id: 'kanban', label: 'Kanban', els: [kanban?.closest('.layout > *')] },
  ];
  return panes.filter(p => (p.els = p.els.filter(Boolean)).length);
}

function loadHiddenPanes() {
  try { return new Set(JSON.parse(localStorage.getItem(PANES_KEY) || '[]')); } catch (e) { return new Set(); }
}

function applyPanes(panes, hidden) {
  const layout = document.querySelector('.layout');
  const on = id => panes.some(p => p.id === id) && !hidden.has(id);
  panes.forEach(p => p.els.forEach(el => el.classList.toggle('pane-off', hidden.has(p.id))));
  const right = layout.querySelector('.col-right');
  const rightOn = on('plan') || on('backlog');
  right?.classList.toggle('pane-off', !rightOn);
  right?.classList.toggle('plan-off', !on('plan'));

  // Columns: fixed widths for the side columns, the chat takes the rest.
  const cols = [];
  if (on('conversations')) cols.push({ id: 'left', w: 'var(--col-left-w, 220px)' });
  cols.push({ id: 'mid', w: '1fr' });
  if (rightOn) cols.push({ id: 'right', w: 'var(--col-right-w, 300px)' });
  // One drag handle between neighbours: resize-left sizes the left column,
  // resize-right the right one.
  const handles = new Set();
  for (let i = 1; i < cols.length; i++) handles.add(cols[i - 1].id === 'left' ? 'resize-left' : 'resize-right');
  ['resize-left', 'resize-right'].forEach(id => document.getElementById(id)?.classList.toggle('pane-off', !handles.has(id)));
  const kanbanOn = on('kanban');
  document.getElementById('resize-kb')?.classList.toggle('pane-off', !kanbanOn);

  const allOn = panes.every(p => !hidden.has(p.id));
  layout.style.gridTemplateColumns = allOn ? '' : cols.flatMap((c, i) => i ? ['6px', c.w] : [c.w]).join(' ');
  layout.style.gridTemplateRows = allOn || kanbanOn ? '' : 'minmax(0, 1fr)';
}

function injectViewMenu() {
  const headerRight = document.querySelector('.header-right');
  const panes = viewPanes();
  if (!headerRight || !panes.length || document.getElementById('view-menu')) return;
  // Only known panes (an old "chat" entry from when the chat was hideable is dropped).
  const hidden = new Set([...loadHiddenPanes()].filter(id => panes.some(p => p.id === id)));
  const wrap = document.createElement('div');
  wrap.id = 'view-menu';
  wrap.className = 'view-menu';
  wrap.innerHTML = `<button type="button" class="view-btn" aria-haspopup="true" aria-expanded="false" title="Show or hide panes">View ▾</button>
    <div class="view-pop" role="menu" hidden>${panes.map(p =>
      `<label class="view-item"><input type="checkbox" data-pane="${p.id}" ${hidden.has(p.id) ? '' : 'checked'}>${escapeHtml(p.label)}</label>`).join('')}
      <label class="view-item view-item-sep" title="When another agent's reply is ready, open it (only while you're waiting, never mid-typing)"><input type="checkbox" data-autojump ${localStorage.getItem('openclave.autojump') === 'on' ? 'checked' : ''}>Autojump to replies</label></div>`;
  headerRight.prepend(wrap);
  const btn = wrap.querySelector('.view-btn');
  const pop = wrap.querySelector('.view-pop');
  const setOpen = open => { pop.hidden = !open; btn.setAttribute('aria-expanded', String(open)); };
  btn.onclick = () => setOpen(pop.hidden);
  document.addEventListener('mousedown', e => { if (!wrap.contains(e.target)) setOpen(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false); });
  pop.addEventListener('change', e => {
    if ('autojump' in e.target.dataset) {
      try {
        if (e.target.checked) localStorage.setItem('openclave.autojump', 'on');
        else { localStorage.removeItem('openclave.autojump'); localStorage.removeItem('openclave.autojump.queue'); }
      } catch (err) {}
      return;
    }
    const id = e.target.dataset.pane;
    if (e.target.checked) hidden.delete(id); else hidden.add(id);
    try { localStorage.setItem(PANES_KEY, JSON.stringify([...hidden])); } catch (err) {}
    applyPanes(panes, hidden);
  });
  applyPanes(panes, hidden);
}

// ── Notifications bell (desktop), left of "View". The real toggle lives in
// the trusted shell (reply_notifications.js); this sandboxed page can't ask
// for notification permission, so it only mirrors the state and asks the
// shell to flip it (dashboard_shell.js, "notify-toggle").
// Drawn in the text colour: the 🔔/🔕 emoji was too faint on the dark header.
const BELL_SVG = on => `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>${on
  ? '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>'
  : '<path d="M8.7 3A6 6 0 0 1 18 8a21.3 21.3 0 0 0 .6 5"/><path d="M17 17H3s3-2 3-9a4.67 4.67 0 0 1 .3-1.7"/><path d="m2 2 20 20"/>'}</svg>`;

function injectNotifyButton() {
  const headerRight = document.querySelector('.header-right');
  if (!headerRight || document.getElementById('notify-btn') || window.parent === window) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'notify-btn';
  btn.className = 'notify-btn';
  btn.innerHTML = BELL_SVG(false);
  btn.hidden = true;  // until the shell reports the state
  btn.onclick = () => parent.postMessage({ __openclaveBridge: true, kind: 'notify-toggle' }, '*');
  window.addEventListener('message', e => {
    const m = e.data;
    if (e.source !== window.parent || !m || m.__openclaveBridge !== true || m.kind !== 'notify-state') return;
    btn.hidden = false;
    btn.innerHTML = BELL_SVG(!!m.on);
    btn.classList.toggle('on', !!m.on);
    btn.title = m.title;
    btn.setAttribute('aria-label', m.title);
    btn.setAttribute('aria-pressed', String(!!m.on));
    btn.disabled = !m.supported;
  });
  headerRight.prepend(btn);
  parent.postMessage({ __openclaveBridge: true, kind: 'notify-state-request' }, '*');
}

// ── Clave picker (claves.py), left of the bell: the same choice as the chips
// next to the Claves button on the home page. Picking one redraws the agent
// tabs with that clave's agents (this agent's tab always stays).
async function injectClavePicker() {
  const headerRight = document.querySelector('.header-right');
  if (!headerRight || document.getElementById('clave-pick')) return;
  let data;
  try { data = await api(`/${AGENT_ID}/claves`); } catch (e) { return; }
  if (!data.enabled || data.claves.length < 2 || document.getElementById('clave-pick')) return;
  const box = document.createElement('div');
  box.id = 'clave-pick';
  box.className = 'clave-pick';
  box.setAttribute('role', 'group');
  box.setAttribute('aria-label', 'Active clave');
  const draw = () => {
    box.innerHTML = data.claves.map(c => `<button type="button" class="clave-chip" data-clave="${escapeHtml(c.id)}" aria-pressed="${c.id === data.active}" title="Show ${escapeHtml(c.name)}'s agents in the tabs">${escapeHtml(c.name)}</button>`).join('');
  };
  draw();
  box.addEventListener('click', async e => {
    const chip = e.target.closest('.clave-chip');
    if (!chip || chip.getAttribute('aria-pressed') === 'true') return;
    try {
      data = await api(`/${AGENT_ID}/claves/active`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ clave: chip.dataset.clave }) });
    } catch (err) { return; }
    draw();
    document.getElementById('agent-tabs')?.remove();
    injectAgentTabs();
  });
  headerRight.prepend(box);
}

async function initDashboardCommon() {
  // The "<- OpenClave" link has no target, so by default it navigates only
  // this iframe's own browsing context (loading the root page nested and
  // broken inside it) rather than the real browser tab — even with the
  // sandbox's allow-top-navigation-by-user-activation permission, nothing
  // asks for top navigation unless something targets it explicitly.
  const back = document.querySelector('.back');
  if (back) back.target = '_top';
  injectHeaderAvatar();
  injectAgentTabs();
  injectViewMenu();
  injectNotifyButton();
  injectClavePicker();

  document.getElementById('new-convo').onclick = () => {
    mountChat(null);
    loadConversations();
  };
  initFilesTab();  // restructures the left column; search goes in after it
  injectConversationSearch();
  loadBacklog();
  let model = null;
  try {
    // Agent-scoped, not /api/agents + /api/models: this runs inside the
    // sandboxed iframe, whose fetch bridge only allows this agent's own
    // "/<agent>/" prefix — those two global endpoints were rejected by that
    // bridge every time, silently, which is why the attach button never
    // appeared for anyone until this was found. The model selector below
    // needs the same agent-scoped /models list for the same reason.
    const [current, models, responseTimes] = await Promise.all([
      api(`/${AGENT_ID}/model`), api(`/${AGENT_ID}/models`),
      api(`/${AGENT_ID}/model-response-times`).catch(() => ({})),
    ]);
    model = current;
    injectModelSelector(models, model, responseTimes);
  } catch (e) { console.error('model check failed, attach button/selector will not appear:', e); }
  try { await loadChatCore(); } catch (e) {
    document.getElementById('chat-window').innerHTML = '<div class="empty">Chat failed to load — reload the page.</div>';
    return;
  }
  initConversations();
}
