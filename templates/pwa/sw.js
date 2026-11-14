/* PhotoTag service worker. Served from /sw.js so it controls the whole site.
 *
 * Privacy rule: only public static files and the offline page are cached.
 * Photos, faces and every authenticated page always go to the network, so nothing
 * private stays on the device after signing out.
 */
const CACHE = "phototag-{{ cache_version }}";
const OFFLINE_URL = "{{ offline_url }}";
const SHELL = [
  OFFLINE_URL,
{% for url in shell_urls %}  "{{ url|escapejs }}",
{% endfor %}];
const STATIC_PREFIX = "{{ static_prefix|escapejs }}";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Hashed static files never change: cache first.
  if (url.pathname.startsWith(STATIC_PREFIX)) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      }))
    );
    return;
  }

  // Page navigations: always the network; the offline page only when it fails.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
  }
  // Anything else (images, HTMX fragments…) is left to the browser, uncached.
});

// --- Push notifications ----------------------------------------------------
// Payload: {title, body, url, tag}. The text never contains people's names.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "PhotoTag", {
      body: data.body || "",
      icon: "{{ icon_url|escapejs }}",
      badge: "{{ badge_url|escapejs }}",
      tag: data.tag || "phototag",
      renotify: true,
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((client) => client.url.startsWith(self.location.origin));
      if (open) return open.navigate(target).then((client) => (client || open).focus());
      return self.clients.openWindow(target);
    })
  );
});
