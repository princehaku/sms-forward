self.addEventListener('push', event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {body: event.data ? event.data.text() : ''};
  }
  const title = payload.title || 'SMS Center';
  const options = {
    body: payload.body || '收到新消息',
    tag: payload.tag || 'sms-center-message',
    renotify: true,
    data: {
      url: payload.url || './',
      eventType: payload.event_type || '',
      messageId: payload.message_id || null,
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || './', self.registration.scope).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const windowClient of windows) {
      if (new URL(windowClient.url).origin === self.location.origin) {
        await windowClient.navigate(targetUrl);
        return windowClient.focus();
      }
    }
    return clients.openWindow(targetUrl);
  })());
});
