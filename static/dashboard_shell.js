// OpenClave — trusted per-agent dashboard shell.
//
// Runs at the real, unsandboxed origin (this file is app-owned, never
// LLM-editable). It loads the agent's own dashboard.html — written by that
// agent via write_own_dashboard, so untrusted — into an iframe sandboxed with
// no allow-same-origin. That gives the iframe's content a browser-enforced
// opaque origin: it cannot read this page's DOM or storage, and it has no
// way to make the browser send or read a same-origin network request at all.
// The only channel out is postMessage, and this script is the sole listener
// on the other end — it enforces that every request the iframe sends starts
// with this agent's own "/<agent_id>/" prefix before ever touching the real
// network. That's what contains agent-editable dashboards (a bad or buggy
// dashboard could otherwise call any other agent's API, including R2's
// Home-Assistant action tools, on this shared origin).
//
// The injected SHIM below overrides fetch() and localStorage/sessionStorage
// inside the sandboxed iframe so existing agent-authored dashboards (built
// against plain fetch('/<agent>/...') calls) keep working unmodified — they
// don't need to know they're sandboxed. localStorage becomes in-memory only
// (opaque origins have no real storage) — layout preferences some dashboards
// save there (pane widths etc.) don't persist across a reload; everything
// else is unaffected.
(function () {
  const AGENT_ID = window.OPENCLAVE_AGENT_ID;
  const ALLOWED_PREFIX = `/${AGENT_ID}/`;

  const iframe = document.createElement("iframe");
  // allow-top-navigation-by-user-activation (not the unrestricted
  // allow-top-navigation) lets a real click on a link like "<- OpenClave"
  // navigate the actual browser tab, while still blocking a script from
  // silently redirecting the whole tab on its own — the exact same
  // trusted-vs-untrusted-input distinction the rest of this sandbox exists
  // to enforce for network requests.
  iframe.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-modals allow-top-navigation-by-user-activation");
  // Lets the "copy conversation" button use the clipboard from inside the sandbox.
  iframe.setAttribute("allow", "clipboard-write");
  iframe.style.cssText = "border:0;width:100%;height:100vh;display:block";
  document.body.appendChild(iframe);

  function injectShim(html, shimSrc) {
    const tag = `<script>${shimSrc}<\/script>`;
    const headIdx = html.search(/<head[^>]*>/i);
    if (headIdx !== -1) {
      const end = html.indexOf(">", headIdx) + 1;
      return html.slice(0, end) + tag + html.slice(end);
    }
    const htmlIdx = html.search(/<html[^>]*>/i);
    if (htmlIdx !== -1) {
      const end = html.indexOf(">", htmlIdx) + 1;
      return html.slice(0, end) + tag + html.slice(end);
    }
    return tag + html;
  }

  // The iframe's localStorage is in-memory (opaque origin), which would
  // lose chat drafts, queued messages and layout on every reload. So the
  // shell keeps a copy in its own storage under a per-agent prefix, seeds the
  // iframe with it and saves every change the iframe reports.
  // The iframe can only ever touch keys under its own prefix.
  const FRAME_PREFIX = `openclave.frame.${AGENT_ID}.`;
  // Same EN/NO and the same View-menu panes on every agent page.
  // Plus Autojump (on/off and its queue of unopened replies) for every page.
  const SHARED_CHECKS = {
    "openclave.voiceLang": (v) => /^[a-z]{2,3}$/.test(v),  // en or [voice] second_language
    "openclave.panes": (v) => v.length <= 2000,
    "openclave.autojump": (v) => v === "on",
    "openclave.autojump.queue": (v) => v.length <= 20000,
  };
  const SHARED_KEYS = new Set(Object.keys(SHARED_CHECKS));
  const storageKey = (k) => (SHARED_KEYS.has(k) ? k : FRAME_PREFIX + k);
  function frameSeed() {
    const seed = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith(FRAME_PREFIX)) seed[k.slice(FRAME_PREFIX.length)] = localStorage.getItem(k);
      }
      for (const k of SHARED_KEYS) { const v = localStorage.getItem(k); if (v !== null) seed[k] = v; }
    } catch (e) {}
    return JSON.stringify(seed).replace(/</g, "\\u003c");
  }

  const SHIM = `
(function () {
  function memoryStorage(seed, persist) {
    const store = Object.assign(Object.create(null), seed || {});
    const save = (op, k, v) => { if (persist) parent.postMessage({ __openclaveBridge: true, kind: "storage", op, key: k, value: v }, "*"); };
    return {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); save("set", String(k), String(v)); },
      removeItem: (k) => { delete store[k]; save("remove", String(k)); },
      clear: () => { for (const k in store) delete store[k]; },
      key: (i) => Object.keys(store)[i] || null,
      get length() { return Object.keys(store).length; },
    };
  }
  try {
    Object.defineProperty(window, "localStorage", { value: memoryStorage(__SEED__, true), configurable: true });
    window.OPENCLAVE_START_NEW = __START_NEW__;
    Object.defineProperty(window, "sessionStorage", { value: memoryStorage(), configurable: true });
  } catch (e) {}

  let seq = 0;
  const pending = new Map();
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || msg.__openclaveBridge !== true || msg.kind !== "response") return;
    const cb = pending.get(msg.id);
    if (!cb) return;
    pending.delete(msg.id);
    cb(msg);
  });
  window.fetch = function (input, init) {
    const path = typeof input === "string" ? input : (input && input.url) || "";
    const id = ++seq;
    return new Promise((resolve, reject) => {
      pending.set(id, (msg) => {
        if (msg.error) { reject(new Error(msg.error)); return; }
        resolve({
          ok: msg.status >= 200 && msg.status < 300,
          status: msg.status,
          json: async () => JSON.parse(msg.body || "null"),
          text: async () => msg.body || "",
        });
      });
      parent.postMessage({
        __openclaveBridge: true, kind: "request", id,
        method: (init && init.method) || "GET",
        path,
        body: init && init.body,
      }, "*");
    });
  };
})();
`;

  fetch(`${ALLOWED_PREFIX}dashboard-src`)
    .then((r) => r.text())
    .then((html) => {
      const shim = SHIM.replace("__SEED__", () => frameSeed()).replace("__START_NEW__", location.hash === "#new" ? "true" : "false");
      iframe.srcdoc = injectShim(html, shim);
      if (location.hash === "#new") history.replaceState(null, "", location.pathname);
    })
    .catch((err) => {
      document.body.textContent = "Failed to load dashboard: " + err;
    });

  // Reply notifications: the chat sends through /chat-requests and polls
  // each request, so "done" is the first poll that reports completed.
  const notified = new Set();
  function isReplyDone(method, path, text) {
    if (method === "POST" && (path === `${ALLOWED_PREFIX}chat` || path === `${ALLOWED_PREFIX}voice/reply`)) return true;
    const m = method === "GET" && path.match(/\/chat-requests\/([^/?]+)$/);
    if (!m || notified.has(m[1])) return false;
    try { if (JSON.parse(text).status !== "completed") return false; } catch (e) { return false; }
    notified.add(m[1]);
    return true;
  }

  function sendNotifyState(st) {
    iframe.contentWindow?.postMessage(Object.assign({ __openclaveBridge: true, kind: "notify-state" }, st), "*");
  }
  window.OpenClaveNotifications?.onChange(sendNotifyState);

  // Dash and Tools tabs: the phone app's screens for
  // this agent (its own page with every block, Reba's sliders, forms, Rick's
  // cleanup; its tool ticks) shown in the agent page's left pane, like Chat
  // and Files. The sandboxed frame can't load them, so it reports where its
  // empty #embed-pane is and this lays the phone app in embed mode, loaded at
  // the real origin, exactly over it. Each screen stays loaded once opened,
  // so switching tabs is instant; it redraws whenever it's shown again.
  const EMBED_VIEWS = new Set(["console", "tools", "skills"]);
  const embeds = {};  // view -> iframe
  function embedFrame(view) {
    if (embeds[view]) return embeds[view];
    const f = document.createElement("iframe");
    f.title = view === "tools" ? "Agent tools" : view === "skills" ? "Agent skills" : "Agent page";
    f.dataset.view = view;
    f.style.cssText = "position:fixed;border:0;z-index:5;display:none;background:transparent";
    f.src = `/mobile/?embed=page#/agents/${encodeURIComponent(AGENT_ID)}/${view}`;
    document.body.appendChild(f);
    embeds[view] = f;
    return f;
  }
  function placeEmbed(view, rect) {
    const base = iframe.getBoundingClientRect();
    for (const [v, f] of Object.entries(embeds)) if (v !== view) f.style.display = "none";
    if (!view) return;
    const f = embedFrame(view);
    const wasHidden = f.style.display === "none";
    Object.assign(f.style, { left: `${base.left + rect.x}px`, top: `${base.top + rect.y}px`,
                             width: `${rect.w}px`, height: `${rect.h}px`, display: "block" });
    if (wasHidden && f.contentWindow) {
      try { f.contentWindow.dispatchEvent(new HashChangeEvent("hashchange")); } catch (e) {}
    }
  }
  // An embedded screen asks for room (wider content) or for another screen
  // (e.g. a chat with a draft: the full phone app in a new tab).
  window.addEventListener("message", (event) => {
    const f = Object.values(embeds).find(x => x.contentWindow === event.source);
    if (!f || event.origin !== location.origin) return;
    const m = event.data;
    if (!m || m.__openclaveEmbed !== true) return;
    if (m.kind === "want" && Number.isFinite(m.width)) {
      iframe.contentWindow.postMessage({ __openclaveBridge: true, kind: "embed-want", view: f.dataset.view, width: m.width }, "*");
    } else if (m.kind === "leave") {
      if (typeof m.hash === "string" && /^#\/agents\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_%-]+)*$/.test(m.hash)) {
        window.open(`/mobile/${m.hash}`, "_blank", "noopener");
      }
      f.contentWindow.location.hash = `#/agents/${encodeURIComponent(AGENT_ID)}/${f.dataset.view}`;
    }
  });

  // Mic and speaker for the iframe's chat (it can't open the mic itself).
  OpenClaveChat.serveAudioBridge(iframe);

  window.addEventListener("message", async (event) => {
    if (event.source !== iframe.contentWindow) return;
    const msg = event.data;
    if (!msg || msg.__openclaveBridge !== true) return;

    if (msg.kind === "move-conversation") {
      const respondMove = (payload) => iframe.contentWindow.postMessage(
        Object.assign({ __openclaveBridge: true, kind: "move-result", id: msg.id }, payload), "*"
      );
      try {
        if (typeof msg.conversationId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(msg.conversationId)) {
          respondMove({ error: "Invalid conversation id." }); return;
        }
        const agents = await (await fetch("/api/agents")).json();
        const target = agents.find(a => a.id === msg.targetAgent && a.id !== AGENT_ID);
        if (!target) { respondMove({ error: "Invalid target agent." }); return; }
        // The sandboxed dashboard can request a move, but only the owner can approve it here.
        if (!window.confirm(`Move this conversation from ${AGENT_ID} to ${target.name}?`)) {
          respondMove({ error: "Move cancelled." }); return;
        }
        const res = await fetch("/api/conversations/move", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_agent: AGENT_ID, target_agent: target.id, conversation_id: msg.conversationId }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          respondMove({ error: body.detail || "Move failed." }); return;
        }
        respondMove({ success: true });
      } catch (err) { respondMove({ error: String(err) }); }
      return;
    }
    if (msg.kind === "move-file") {
      const respondMove = (payload) => iframe.contentWindow.postMessage(
        Object.assign({ __openclaveBridge: true, kind: "move-result", id: msg.id }, payload), "*"
      );
      try {
        if (typeof msg.path !== "string" || !msg.path || msg.path.length > 512) {
          respondMove({ error: "Invalid file path." }); return;
        }
        const agents = await (await fetch("/api/agents")).json();
        const target = agents.find(a => a.id === msg.targetAgent && a.id !== AGENT_ID);
        if (!target) { respondMove({ error: "Invalid target agent." }); return; }
        // Same as conversations: the sandboxed dashboard can ask, only the owner approves here.
        if (!window.confirm(`Move ${msg.path} from ${AGENT_ID} to ${target.name}?`)) {
          respondMove({ error: "Move cancelled." }); return;
        }
        const res = await fetch("/api/files/move", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_agent: AGENT_ID, target_agent: target.id, path: msg.path }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          respondMove({ error: body.detail || "Move failed." }); return;
        }
        respondMove({ success: true });
      } catch (err) { respondMove({ error: String(err) }); }
      return;
    }
    if (msg.kind === "handover") {
      // Conversation plus files to another agent (handover.py). As with the
      // moves above, the sandboxed page can only ask; the owner confirms here.
      const respond = (payload) => iframe.contentWindow.postMessage(
        Object.assign({ __openclaveBridge: true, kind: "move-result", id: msg.id }, payload), "*"
      );
      try {
        if (typeof msg.conversationId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(msg.conversationId)) {
          respond({ error: "Invalid conversation id." }); return;
        }
        const files = Array.isArray(msg.files) ? msg.files : [];
        if (files.length > 100 || !files.every(f => typeof f === "string" && f && f.length <= 512)) {
          respond({ error: "Invalid file list." }); return;
        }
        const agents = await (await fetch("/api/agents")).json();
        const target = agents.find(a => a.id === msg.targetAgent && a.id !== AGENT_ID);
        if (!target) { respond({ error: "Invalid target agent." }); return; }
        const what = files.length ? ` with ${files.length} file${files.length > 1 ? "s" : ""} (${files.join(", ")})` : "";
        if (!window.confirm(`Hand this conversation over from ${AGENT_ID} to ${target.name}${what}?`)) {
          respond({ cancelled: true }); return;
        }
        const res = await fetch("/api/conversations/handover", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_agent: AGENT_ID, target_agent: target.id, conversation_id: msg.conversationId, files }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) { respond({ error: body.detail || "Handover failed." }); return; }
        respond({ success: true, result: body });
      } catch (err) { respond({ error: String(err) }); }
      return;
    }
    if (msg.kind === "embed-pane") {
      const r = msg.rect;
      const ok = r && ["x", "y", "w", "h"].every(k => Number.isFinite(r[k]) && Math.abs(r[k]) < 100000);
      placeEmbed(EMBED_VIEWS.has(msg.view) && ok ? msg.view : null, r);
      return;
    }

    if (msg.kind === "open-agent") {
      // Autojump: open another agent's page (it shows that agent's latest chat).
      try {
        const agents = await (await fetch("/api/agents")).json();
        const target = agents.find(a => a.id === msg.targetAgent && a.id !== AGENT_ID);
        if (target) window.location.href = `/${encodeURIComponent(target.id)}`;
      } catch (e) {}
      return;
    }
    if (msg.kind === "forward") {
      // "Send to another agent" from a message: open that agent's page on a
      // new conversation with the text as its draft (nothing is sent).
      try {
        if (typeof msg.text !== "string" || !msg.text || msg.text.length > 20000) return;
        const agents = await (await fetch("/api/agents")).json();
        const target = agents.find(a => a.id === msg.targetAgent && a.id !== AGENT_ID);
        if (target) OpenClaveChat.forwardToAgentPage(target.id, msg.text);
      } catch (e) {}
      return;
    }
    // Notifications bell drawn by the iframe left of "View": the toggle and
    // permission prompt happen here (reply_notifications.js).
    if (msg.kind === "notify-toggle" || msg.kind === "notify-state-request") {
      const n = window.OpenClaveNotifications;
      if (!n) return;
      if (msg.kind === "notify-toggle") await n.toggle();
      else sendNotifyState(n.state());
      return;
    }
    if (msg.kind === "storage") {
      if (typeof msg.key !== "string" || msg.key.length > 200) return;
      try {
        if (msg.op === "remove") localStorage.removeItem(storageKey(msg.key));
        else if (msg.op === "set" && typeof msg.value === "string" && msg.value.length <= 200000) {
          if (SHARED_KEYS.has(msg.key) && !SHARED_CHECKS[msg.key](msg.value)) return;
          localStorage.setItem(storageKey(msg.key), msg.value);
        }
      } catch (e) {}
      return;
    }
    if (msg.kind !== "request") return;

    const respond = (payload) => {
      iframe.contentWindow.postMessage(
        Object.assign({ __openclaveBridge: true, kind: "response", id: msg.id }, payload),
        "*"
      );
    };

    // One read-only exception for Autojump: which agents just finished a reply
    // (names and conversation ids, no content).
    const autojumpPoll = (msg.method || "GET") === "GET" && typeof msg.path === "string" && /^\/api\/replies\?after=\d+$/.test(msg.path);
    if (!autojumpPoll && (typeof msg.path !== "string" || !msg.path.startsWith(ALLOWED_PREFIX))) {
      respond({ error: "blocked: this dashboard can only reach its own agent's endpoints" });
      return;
    }

    const method = msg.method || "GET";
    const init = { method };
    if (msg.body != null && method !== "GET" && method !== "HEAD") {
      init.body = msg.body;
      init.headers = { "Content-Type": "application/json" };
    }

    try {
      const res = await fetch(msg.path, init);
      const text = await res.text();
      respond({ status: res.status, body: text });
      if (res.ok && isReplyDone(method, msg.path, text)) window.openclaveNotifyReply?.(AGENT_ID);
    } catch (err) {
      respond({ error: String(err) });
    }
  });
})();
