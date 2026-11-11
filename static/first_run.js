// First steps on a new install (setup_check.first_run via /api/setup): the
// same card on the desktop home page and the phone's agent list, so the
// wording is written once. Each step ticks off when it's done; the card goes
// away once a model can answer, the manager exists and there is at least one
// more agent. It only routes into the existing forms (Control Panel → Setup,
// the ready-made agents), it doesn't rebuild them.
(function () {
  const esc = s => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };

  const STYLE = `
  .first-run{border:1px solid #2b3137;border-radius:12px;background:#1a1e22;padding:18px 20px;margin:0 0 18px;line-height:1.5;max-width:760px;color:#f0ede7}
  .first-run h2{margin:0 0 10px;font-size:18px;display:flex;align-items:center;gap:10px}
  .fr-layers{display:flex;flex-wrap:wrap;align-items:stretch;gap:6px;margin:0 0 14px;font-size:13px;color:#969ca3}
  .fr-layers span{flex:1 1 150px;border:1px solid #2b3137;border-radius:8px;padding:6px 10px}
  .fr-layers b{color:#f0ede7;display:block;font-size:13.5px}
  .fr-steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px}
  .fr-step{display:flex;gap:10px;align-items:flex-start}
  .fr-num{flex:0 0 24px;height:24px;border-radius:50%;border:1px solid #969ca3;display:grid;place-items:center;font-size:12.5px;font-weight:600;color:#969ca3}
  .fr-step.done .fr-num{background:#7fd1b0;border-color:#7fd1b0;color:#102019}
  .fr-step.done .fr-text{color:#969ca3}
  .fr-step.todo .fr-num{border-color:#f0ede7;color:#f0ede7}
  .fr-text{flex:1;min-width:0}
  .fr-text p{margin:2px 0 0;color:#969ca3;font-size:13.5px}
  .fr-btn{display:inline-block;margin-top:8px;background:#7fd1b0;color:#102019;border-radius:9px;padding:8px 16px;font-weight:600;font-size:14px;text-decoration:none}
  .fr-btn.quiet{background:none;color:#7fd1b0;border:1px solid #2b3137}
  .fr-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .fr-chips a{border:1px solid #2b3137;border-radius:999px;padding:4px 12px;color:#f0ede7;text-decoration:none;font-size:13.5px}
  .fr-chips a small{color:#969ca3}
  .fr-chips a:hover,.fr-btn.quiet:hover{border-color:#969ca3}
  .first-run a.fr-link{color:#7fd1b0}
  .fr-foot{margin:14px 0 0;font-size:12.5px;color:#969ca3;overflow-wrap:anywhere}`;

  // Where each step leads: the Control Panel on desktop, the phone's own screens.
  const LINKS = {
    desktop: {setup: '/control/#setup', signIn: '/control/#claude-login', manager: '/control/#manager',
              add: id => `/control/#add-${encodeURIComponent(id)}`, all: '/control/#new-agent',
              review: id => `/mobile/#/agents/${encodeURIComponent(id)}/console`},
    phone: {setup: '/control/?from=mobile#setup', signIn: '/control/?from=mobile#claude-login', manager: '#/create-agent/manager',
            add: id => `#/create-agent/${encodeURIComponent(id)}`, all: '#/create-agent',
            review: id => `#/agents/${encodeURIComponent(id)}/console`},
  };

  function needed(d) {
    return !d.ready || !d.manager || d.agents < 2;
  }

  function html(d, links) {
    const signIn = d.routes.find(r => r.needs_sign_in);
    // Only the first step still to do gets the big button.
    const current = !d.ready ? 'model' : !d.manager ? 'manager' : 'more';
    const btn = s => `fr-btn${s === current ? '' : ' quiet'}`;
    const step = (done, title, body) => `<li class="fr-step ${done ? 'done' : 'todo'}"><span class="fr-num">${done ? '✓' : ''}</span><div class="fr-text">${title}${body}</div></li>`;
    const model = d.ready
      ? step(true, '<b>A model is set up.</b>', '')
      : step(false, '<b>Add a model.</b>', `<p>None can answer yet. The quickest: paste an Anthropic or xAI API key.${signIn ? ' Or sign in to Claude: Claude Code is installed here.' : ' A Claude or ChatGPT subscription works too (see the README).'}</p>
          <a class="fr-btn" href="${links.setup}" data-first-run="model">Add an API key →</a>${signIn ? ` <a class="fr-btn quiet" href="${links.signIn}">Sign in to Claude</a>` : ''}`);
    const manager = d.manager
      ? step(true, `<b>Your manager, ${esc(d.manager.name)}, is set up.</b>`, `<p>See what it does: <a class="fr-link" href="${links.review(d.manager.id)}">run a review now</a>.</p>`)
      : step(false, '<b>Name your manager.</b>', `<p>It keeps the other agents tidy. Every install has one; you pick its name and personality.</p>
          <a class="${btn('manager')}" href="${links.manager}" data-first-run="manager">Name your manager →</a>`);
    const more = d.agents >= 2 && d.manager
      ? step(true, `<b>You have ${d.agents} agents.</b>`, '')
      : step(false, '<b>Add two to four agents</b> for things you care about.', `<p>Each asks you a few questions first, to fit itself to you.</p>
          ${d.suggested.length ? `<div class="fr-chips">${d.suggested.map(t => `<a href="${links.add(t.id)}" title="${esc(t.tagline)}">${esc(t.name)} <small>${esc(t.tagline)}</small></a>`).join('')}</div>` : ''}
          <a class="${btn('more')}" href="${links.all}" data-first-run="more">All ready-made agents</a>`);
    return `<h2><img src="/static/mobile/icons/keeper-${d.ready ? 'done' : 'thinking'}.svg" alt="" width="40" height="40">Welcome to OpenClave</h2>
      <div class="fr-layers" aria-label="How OpenClave sorts your chats">
        <span><b>Agents</b>One per area of your life</span>
        <span><b>Topics</b>Each agent's chats, grouped</span>
        <span><b>Weekly review</b>Your manager suggests what to tidy</span>
      </div>
      <ol class="fr-steps">${model}${manager}${more}</ol>
      <p class="fr-foot">Everything your agents learn is kept as plain Markdown files in <code>${esc(d.agents_folder)}</code>.</p>`;
  }

  // The card for this install, or null when it's set up. `phone`: link to the
  // phone's screens instead of the Control Panel.
  async function card({phone = false} = {}) {
    let d;
    try {
      const res = await fetch('/api/setup');
      if (!res.ok) return null;
      d = await res.json();
    } catch { return null; }
    if (!needed(d)) return null;
    if (!document.getElementById('first-run-style')) {
      const style = document.createElement('style');
      style.id = 'first-run-style';
      style.textContent = STYLE;
      document.head.appendChild(style);
    }
    const el = document.createElement('section');
    el.className = 'first-run';
    el.id = 'welcome';
    el.innerHTML = html(d, LINKS[phone ? 'phone' : 'desktop']);
    return el;
  }

  window.OpenClaveFirstRun = {card};
})();
