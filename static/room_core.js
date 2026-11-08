// OpenClave — the group chat (room.py), shared by the desktop chat view
// (dashboard.html) and mobile (#/room), so both behave the same. Each UI
// keeps its own layout and CSS.
//   - messages from the owner and every agent, newest at the bottom, polled
//   - "<name> is answering…" while agents work on a room message
//   - @mention picker: type @ for the agent list; @all asks everyone
//   - a draft that survives reloads
// Classic script; exposes window.OpenClaveRoom. Needs chat_core.js first.
(function () {
  'use strict';
  const POLL_MS = 2000;
  const DRAFT_KEY = 'openclave.room.draft';
  const {escapeHtml, storage} = window.OpenClaveChat;

  function time(ms) {
    const d = new Date(ms), now = new Date();
    const hm = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    return d.toDateString() === now.toDateString() ? hm : `${d.toLocaleDateString([], {month: 'short', day: 'numeric'})} ${hm}`;
  }

  // o: {messages, form, api, agents: [{id,name,color}], md(text) -> html,
  //     scroller?, onOpenAgent?(id), ownerName?, clave?, claveName?}
  // clave (claves.py): which room; "everyone" (default) is every agent, a
  // clave's room has only its members (the server says who: data.members).

  function mount(o) {
    const {messages, form, api} = o;
    const scroller = o.scroller || messages;
    const store = o.store || storage();
    const md = o.md || (text => `<p>${escapeHtml(text)}</p>`);
    const clave = o.clave || 'everyone';
    const q = clave === 'everyone' ? '' : `&clave=${encodeURIComponent(clave)}`;
    const draftKey = clave === 'everyone' ? DRAFT_KEY : `${DRAFT_KEY}.${clave}`;
    const byId = Object.fromEntries((o.agents || []).map(a => [a.id, a]));
    let members = null;  // agent ids in this room, from the server
    // A channel (channels.py): anyone can be @tagged in, and tagging adds them.
    const isChannel = clave.startsWith('ch-');
    const roomAgents = () => (o.agents || []).filter(a => isChannel || !members || members.includes(a.id));
    const who = clave === 'everyone' ? 'everyone' : (o.claveName || clave);
    let lastSeq = 0, timer = null, destroyed = false, pending = [];
    const shown = [];

    form.innerHTML = `<div class="room-picker" hidden role="listbox" aria-label="Mention an agent"></div>
      <div class="room-hint" role="status"></div>
      <div class="composer-row"><div class="input-col"><textarea rows="1" placeholder="Message ${escapeHtml(who)} — @name to ask an agent, @all for everyone here" aria-label="Message the group chat"></textarea></div>
      <button type="button" class="send">Send</button></div>`;
    const textarea = form.querySelector('textarea');
    const send = form.querySelector('.send');
    const hint = form.querySelector('.room-hint');
    const picker = form.querySelector('.room-picker');
    textarea.value = store.get(draftKey) || '';

    function avatarHtml(m) {
      if (m.from === 'user') return `<div class="room-avatar you">${escapeHtml((m.name || 'You').slice(0, 1))}</div>`;
      const a = byId[m.from];
      return `<button type="button" class="room-avatar" data-open="${escapeHtml(m.from)}" style="background:${escapeHtml(a?.color || '#555')}" title="Open ${escapeHtml(m.name)}"><img src="/api/agents/${encodeURIComponent(m.from)}/avatar" alt=""></button>`;
    }
    function messageHtml(m) {
      const name = m.from === 'user'
        ? `<span class="room-name">${escapeHtml(m.name)}</span>`
        : `<button type="button" class="room-name link" data-open="${escapeHtml(m.from)}">${escapeHtml(m.name)}</button>`;
      const body = m.from === 'user' ? `<p>${escapeHtml(m.text)}</p>` : md(m.text);
      const asked = (m.asked || []).map(id => byId[id]?.name || id);
      const note = m.from === 'user' && !(m.asked || []).length
        ? '<div class="room-note">Nobody asked: mention an agent with @name.</div>'
        : asked.length && m.from !== 'user' ? `<div class="room-note">Asked ${asked.map(escapeHtml).join(', ')}</div>` : '';
      const joined = (m.joined || []).map(id => byId[id]?.name || id);
      const joinNote = joined.length ? `<div class="room-note">Added ${joined.map(escapeHtml).join(', ')} to this channel</div>` : '';
      return `<div class="room-msg${m.error ? ' error' : ''}${m.from === 'user' ? ' mine' : ''}" data-seq="${m.seq}">${avatarHtml(m)}
        <div class="room-main"><div class="room-head">${name}<span class="room-time">${escapeHtml(time(m.ts_ms))}</span></div>
        <div class="room-text">${body}</div>${note}${joinNote}</div></div>`;
    }
    function pendingHtml() {
      const names = [...new Set(pending.map(p => byId[p.agent_id]?.name || p.agent_id))];
      if (!names.length) return '';
      const verb = names.length === 1 ? 'is' : 'are';
      return `<div class="room-pending">${names.map(escapeHtml).join(', ')} ${verb} answering…</div>`;
    }
    function render() {
      const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
      messages.innerHTML = shown.length
        ? shown.map(messageHtml).join('') + pendingHtml()
        : `<div class="room-empty">${clave === 'everyone' ? 'The group chat: everyone can read it.' : isChannel ? `The ${escapeHtml(who)} channel: only its agents are here. Tag anyone else with @name to bring them in.` : `The ${escapeHtml(who)} clave's group chat: only its agents are here.`} Mention agents with @name to get their answer, or @all for everyone here.</div>${pendingHtml()}`;
      messages.querySelectorAll('.room-text a[href]').forEach(link => { link.target = '_blank'; link.rel = 'noopener noreferrer'; });
      if (atBottom) scroller.scrollTop = scroller.scrollHeight;
    }

    async function poll() {
      if (destroyed) return;
      try {
        const data = await api(`/api/room?after=${lastSeq}${q}`);
        if (Array.isArray(data.members)) members = data.members;
        if (destroyed) return;
        const fresh = data.messages || [];
        const changed = fresh.length || JSON.stringify(pending) !== JSON.stringify(data.pending || []);
        for (const m of fresh) { shown.push(m); lastSeq = Math.max(lastSeq, m.seq); }
        if (fresh.length) o.onMessages?.(fresh);
        pending = data.pending || [];
        if (changed || !messages.children.length) render();
      } catch { /* next poll */ }
      if (!destroyed) timer = setTimeout(poll, POLL_MS);
    }

    async function submit() {
      const text = textarea.value.trim();
      if (!text || send.disabled) return;
      send.disabled = true;
      hint.textContent = '';
      try {
        const posted = await api('/api/room', {method: 'POST', body: JSON.stringify({text, clave})});
        textarea.value = '';
        store.remove(draftKey);
        if (posted && posted.seq > lastSeq) { shown.push(posted); lastSeq = posted.seq; }
        if (posted && posted.asked?.length) pending = pending.concat(posted.asked.map(id => ({agent_id: id, seq: posted.seq})));
        scroller.scrollTop = scroller.scrollHeight;
        render();
        scroller.scrollTop = scroller.scrollHeight;
      } catch (err) {
        hint.textContent = err.message || 'Could not send.';
      } finally {
        send.disabled = false;
        textarea.focus();
      }
    }

    // @mention picker
    let matches = [], active = 0;
    function mentionAt() {
      const upto = textarea.value.slice(0, textarea.selectionStart);
      const m = upto.match(/(?:^|\s)@([\w-]*)$/);
      return m ? {query: m[1].toLowerCase(), start: upto.length - m[1].length - 1} : null;
    }
    function drawPicker() {
      const at = mentionAt();
      const options = [{id: 'all', name: 'all', label: 'Everyone here'}, ...roomAgents().map(a => ({id: a.id, name: a.name.replace(/\s+/g, ''),
        label: isChannel && members && !members.includes(a.id) ? `${a.name} · adds them` : a.name}))];
      matches = at ? options.filter(x => x.name.toLowerCase().startsWith(at.query) || x.id.startsWith(at.query)) : [];
      if (!matches.length) { picker.hidden = true; return; }
      active = Math.min(active, matches.length - 1);
      picker.innerHTML = matches.map((x, i) => `<button type="button" role="option" class="room-pick${i === active ? ' active' : ''}" data-i="${i}" aria-selected="${i === active}">@${escapeHtml(x.name)}<span>${escapeHtml(x.label)}</span></button>`).join('');
      picker.hidden = false;
    }
    function choose(i) {
      const at = mentionAt(), pick = matches[i];
      if (!at || !pick) return;
      const before = textarea.value.slice(0, at.start), after = textarea.value.slice(textarea.selectionStart);
      textarea.value = `${before}@${pick.name} ${after}`;
      const caret = before.length + pick.name.length + 2;
      textarea.setSelectionRange(caret, caret);
      store.set(draftKey, textarea.value);
      picker.hidden = true;
      textarea.focus();
    }
    picker.addEventListener('mousedown', e => {
      const b = e.target.closest('.room-pick');
      if (b) { e.preventDefault(); choose(Number(b.dataset.i)); }
    });
    textarea.addEventListener('input', () => {
      store.set(draftKey, textarea.value);
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
      active = 0;
      drawPicker();
    });
    textarea.addEventListener('keydown', e => {
      if (!picker.hidden && matches.length) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          active = (active + (e.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
          drawPicker();
          return;
        }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); choose(active); return; }
        if (e.key === 'Escape') { picker.hidden = true; return; }
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && o.enterSends !== false) { e.preventDefault(); submit(); }
    });
    textarea.addEventListener('blur', () => setTimeout(() => { picker.hidden = true; }, 150));
    send.onclick = submit;
    messages.addEventListener('click', e => {
      const open = e.target.closest('[data-open]');
      if (open && o.onOpenAgent) o.onOpenAgent(open.dataset.open);
    });

    render();
    poll();
    return {
      submit,
      destroy() { destroyed = true; clearTimeout(timer); },
    };
  }

  // Add or edit a channel (channels.py): name and members, or delete it.
  // o: {api, agents: [{id,name}], channel?: {id,name,members}} -> resolves to
  // the saved channel, {deleted: id}, or null when cancelled. Both UIs use it.
  function editChannel(o) {
    const ch = o.channel || {name: '', members: []};
    const overlay = document.createElement('div');
    overlay.className = 'handover-overlay';
    overlay.innerHTML = `<div class="handover-dialog" role="dialog" aria-modal="true" aria-label="${ch.id ? 'Edit channel' : 'New channel'}">
      <h2>${ch.id ? `# ${escapeHtml(ch.name)}` : 'New channel'}</h2>
      <label class="handover-to">Name <input class="channel-name" maxlength="40" value="${escapeHtml(ch.name)}" placeholder="e.g. garage"></label>
      <div class="handover-label">Who's in it</div>
      <div class="handover-files">${(o.agents || []).map(a => `<label class="handover-file"><input type="checkbox" value="${escapeHtml(a.id)}"${ch.members.includes(a.id) ? ' checked' : ''}><span>${escapeHtml(a.name)}</span></label>`).join('')}</div>
      <p class="handover-hint">You can also bring an agent in later by tagging them with @name in the channel.</p>
      <div class="handover-error" role="alert"></div>
      <div class="handover-actions">${ch.id ? '<button type="button" class="handover-cancel channel-delete">Delete channel</button>' : ''}<button type="button" class="handover-cancel channel-cancel">Cancel</button><button type="button" class="handover-go channel-save">${ch.id ? 'Save' : 'Create'}</button></div>
    </div>`;
    document.body.appendChild(overlay);
    const q = s => overlay.querySelector(s);
    q('.channel-name').focus();
    return new Promise(resolve => {
      const close = value => { overlay.remove(); resolve(value); };
      overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
      overlay.addEventListener('keydown', e => { if (e.key === 'Escape') close(null); });
      q('.channel-cancel').onclick = () => close(null);
      const del = q('.channel-delete');
      if (del) del.onclick = async () => {
        if (!window.confirm(`Delete # ${ch.name}? The agents keep their own copies of the conversation.`)) return;
        try { await o.api(`/api/channels/${encodeURIComponent(ch.id)}`, {method: 'DELETE'}); close({deleted: ch.id}); }
        catch (err) { q('.handover-error').textContent = err.message || 'Could not delete.'; }
      };
      q('.channel-save').onclick = async () => {
        const body = {name: q('.channel-name').value, members: [...overlay.querySelectorAll('.handover-file input:checked')].map(i => i.value)};
        try {
          const saved = await o.api(ch.id ? `/api/channels/${encodeURIComponent(ch.id)}` : '/api/channels',
            {method: ch.id ? 'PATCH' : 'POST', body: JSON.stringify(body)});
          close(saved);
        } catch (err) { q('.handover-error').textContent = err.message || 'Could not save.'; }
      };
    });
  }

  window.OpenClaveRoom = {mount, editChannel};
})();
