// Opt-in notifications on this browser: Web Push for agent replies
// (chat_jobs.py), reminders (reminders.py) and the digest, which arrives even
// when OpenClave is closed. On an iPhone, push needs the Home Screen app and a tap on this toggle.
(function () {
  const KEY = 'openclave.reply-notifications';
  const SW_URL = '/mobile/sw.js';
  const supported = 'Notification' in window && window.isSecureContext;
  const pushSupported = supported && 'serviceWorker' in navigator && 'PushManager' in window;
  // Icon only. Mobile puts it in the top bar (.notify-slot, left of the model
  // badge); the desktop agent page has none of its own: its sandboxed iframe
  // draws one left of "View" and toggles this one through dashboard_shell.js.
  // Anywhere else (the main dashboard) it sits fixed top-right.
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'notify-btn';
  const FIXED_STYLE = 'position:fixed;right:14px;top:14px;z-index:10000;padding:6px 9px;border:1px solid #666;border-radius:8px;background:#1a1e22;font:16px sans-serif;line-height:1;cursor:pointer';
  const listeners = [];
  function enabled() { return supported && localStorage.getItem(KEY) === 'on' && Notification.permission === 'granted'; }
  function state() {
    const on = enabled();
    return {
      on, supported,
      icon: on ? '🔔' : '🔕',
      title: !supported
        ? (/iPhone|iPad/.test(navigator.userAgent) ? 'Add OpenClave to the Home Screen to get notifications' : 'Notifications need a secure browser connection')
        : `Notifications ${on ? 'on' : 'off'}: agent replies and reminders on this device`,
    };
  }
  function update() {
    const st = state();
    button.textContent = st.icon;
    button.title = st.title;
    button.setAttribute('aria-label', st.title);
    button.disabled = !supported;
    button.setAttribute('aria-pressed', String(st.on));
    listeners.forEach(fn => fn(st));
  }

  function deviceLabel() {
    const ua = navigator.userAgent;
    const device = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
      : /Windows/.test(ua) ? 'Windows' : /Mac/.test(ua) ? 'Mac' : 'Browser';
    return `${device}${location.pathname.startsWith('/mobile/') ? ' app' : ' browser'}`;
  }

  async function registration() {
    return navigator.serviceWorker.register(SW_URL, {scope: '/mobile/'});
  }

  function keyBytes(b64) {
    const raw = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - b64.length % 4) % 4));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }

  // Idempotent: re-sends the current subscription so the server always has it.
  async function subscribePush() {
    if (!pushSupported) return;
    try {
      const reg = await registration();
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        const {key} = await (await fetch('/api/push/key')).json();
        sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(key)});
      }
      await fetch('/api/push/subscribe', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subscription: sub.toJSON(), label: deviceLabel()}),
      });
    } catch (err) { console.warn('push subscription failed; reply notifications still work while open:', err); }
  }

  async function unsubscribePush() {
    if (!pushSupported) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration('/mobile/');
      const sub = await reg?.pushManager.getSubscription();
      if (!sub) return;
      await fetch('/api/push/unsubscribe', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({endpoint: sub.endpoint}),
      });
      await sub.unsubscribe();
    } catch { /* nothing to undo */ }
  }

  async function toggle() {
    if (enabled()) { localStorage.removeItem(KEY); unsubscribePush(); }
    else if (supported) {
      const permission = await Notification.requestPermission();
      if (permission === 'granted') { localStorage.setItem(KEY, 'on'); subscribePush(); }
    }
    update();
  }
  button.onclick = toggle;
  window.OpenClaveNotifications = {
    toggle, state,
    onChange(fn) { listeners.push(fn); },
    // Mobile re-renders its top bar on every route; it hands us the new slot.
    mountIn(slot) { if (slot) { button.style.cssText = ''; slot.appendChild(button); } },
  };
  function mount() {
    const slot = document.querySelector('.notify-slot');
    if (slot) window.OpenClaveNotifications.mountIn(slot);
    else if (!window.OPENCLAVE_AGENT_ID && !location.pathname.startsWith('/mobile/')) {
      button.style.cssText = FIXED_STYLE;
      document.body.appendChild(button);
    }
    update();
    if (enabled()) subscribePush();  // devices that turned this on before push existed
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
  async function pushSubscribed() {
    try {
      const reg = pushSupported && await navigator.serviceWorker.getRegistration('/mobile/');
      return Boolean(reg && await reg.pushManager.getSubscription());
    } catch { return false; }
  }
  // Opening an agent: its "replied" notifications are stale now.
  window.OpenClaveNotifications.clearReply = async function (agentId) {
    try {
      const reg = 'serviceWorker' in navigator && await navigator.serviceWorker.getRegistration('/mobile/');
      (await reg?.getNotifications({tag: `openclave-reply-${agentId}`}) || []).forEach(n => n.close());
    } catch { /* not supported here */ }
  };
  // Replies are pushed by the server when they finish (chat_jobs._push_reply),
  // whatever is open. This local notice is only the fallback for a device
  // without a push subscription, and only when the page isn't on screen.
  window.openclaveNotifyReply = async function (agentName) {
    if (!enabled() || !document.hidden || await pushSubscribed()) return;
    const title = `${agentName} replied`;
    const options = {body: 'OpenClave is ready for you.', tag: `openclave-reply-${agentName}`};
    try {
      if (location.pathname.startsWith('/mobile/') && 'serviceWorker' in navigator) {
        await (await registration()).showNotification(title, options);
      } else new Notification(title, options);
    } catch { /* Notification support differs between browsers; chat still works. */ }
  };
})();
