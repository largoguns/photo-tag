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

  if (url.pathname.startsWith(STATIC_PREFIX)) {
    // Hashed names (production, e.g. app.3f2a9c1b7d4e.css) never change: cache first.
    // Unhashed names (development) may change under the same URL: network first.
    const hashed = /\.[0-9a-f]{12}\.[a-z0-9]+$/i.test(url.pathname);
    const store = (response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    };
    event.respondWith(
      hashed
        ? caches.match(request).then((cached) => cached || fetch(request).then(store))
        : fetch(request).then(store).catch(() => caches.match(request))
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
// The scan screen already shows the result: skip the scan notification while it is on view.
async function scanScreenVisible() {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return windows.some((client) => client.visibilityState === "visible"
    && new URL(client.url).pathname.startsWith("/admin/scan/"));
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil((async () => {
    if (data.tag === "phototag-scan" && await scanScreenVisible()) return;
    await self.registration.showNotification(data.title || "PhotoTag", {
      body: data.body || "",
      icon: "{{ icon_url|escapejs }}",
      badge: "{{ badge_url|escapejs }}",
      tag: data.tag || "phototag",
      renotify: true,
      data: { url: data.url || "/" },
    });
  })());
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
