/* FinCore ERP — service worker (PWA app-shell).
 *
 * Strategy
 *   - Navigations (HTML): network-first; on failure serve the cached
 *     offline page. HTML is NEVER cached — ledger screens can carry
 *     sensitive balances and must not be stored on shared devices.
 *   - /static/ assets (versioned ?v= URLs): stale-while-revalidate.
 *   - Font/icon CDNs: stale-while-revalidate, best effort.
 *   - Everything else (/api/, POSTs, ledger data): network only.
 *
 * Bump CACHE when the precache list changes.
 */
const CACHE = 'fincore-shell-v1';
const OFFLINE_PATH = '/static/web/offline.html';
const PRECACHE = [
  OFFLINE_PATH,
  '/static/web/favicon-32.png',
  '/static/web/favicon-180.png',
  '/static/web/icon-192.png',
  '/static/web/icon-512.png',
  '/static/web/site.webmanifest',
];
const CDN_HOSTS = [
  'fonts.googleapis.com',
  'fonts.gstatic.com',
  'cdnjs.cloudflare.com',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  let url;
  try {
    url = new URL(req.url);
  } catch (e) {
    return;
  }

  // 1) Page navigations: network-first, offline fallback.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_PATH))
    );
    return;
  }

  // 2) Same-origin static assets: stale-while-revalidate.
  if (url.origin === self.location.origin && url.pathname.indexOf('/static/') === 0) {
    event.respondWith(swr(req));
    return;
  }

  // 3) Font / icon CDNs: stale-while-revalidate (opaque responses OK).
  if (CDN_HOSTS.indexOf(url.hostname) !== -1) {
    event.respondWith(swr(req));
    return;
  }

  // 4) Everything else (ledger data, APIs): network only.
});

function swr(req) {
  return caches.match(req).then((cached) => {
    const fetched = fetch(req).then((res) => {
      if (res && (res.ok || res.type === 'opaque')) {
        const clone = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, clone));
      }
      return res;
    });
    return cached || fetched;
  });
}
