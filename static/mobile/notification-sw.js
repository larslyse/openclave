// OpenClave mobile service worker, served at /mobile/sw.js (scope /mobile/).
// Shows Web Push notifications (replies from chat_jobs.py, reminders.py, the
// digest; all via web_push.py) and the local reply fallback, and opens the right screen when one is tapped.
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {body: event.data?.text() || ''}; }
  event.waitUntil(self.registration.showNotification(data.title || 'OpenClave', {
    body: data.body || '', tag: data.tag || undefined, data: {url: data.url || '/mobile/'},
    icon: '/static/mobile/icons/app-icon-192.png?v=3',
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/mobile/', self.location.origin).href;
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(windows => {
    const open = windows.find(client => client.url.includes('/mobile/'));
    if (open) return open.navigate(url).then(c => (c || open).focus()).catch(() => open.focus());
    return clients.openWindow(url);
  }));
});
