/**
 * Monika AI & Jarvis Core — Service Worker (sw.js)
 * High-performance PWA service worker with offline App Shell caching,
 * Stale-While-Revalidate for static assets, and strict Network-Only for dynamic APIs.
 */

const CACHE_NAME = 'monika-pwa-v4.0-clean';

// Listen for direct purge messages from the UI
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  if (event.data && event.data.type === 'PURGE_ALL') {
    const purgePromise = caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith('monika-')).map((k) => caches.delete(k)))
    ).then(() => {
      if (event.ports && event.ports[0]) event.ports[0].postMessage({ success: true });
    });
    event.waitUntil(purgePromise);
  }
});

// Essential App Shell resources cached on installation
const APP_SHELL_RESOURCES = [
  '/',
  '/index.html',
  '/manifest.json',
  '/favicon.ico',
  '/monika_avatar.jpg',
  '/icons/icon-192x192.png',
  '/icons/icon-512x512.png',
  '/icons/icon-maskable-192x192.png',
  '/icons/icon-maskable-512x512.png',
  '/icons/apple-touch-icon.png'
];

// External CDN dependencies for offline UI rendering
const CDN_RESOURCES = [
  'https://cdn.tailwindcss.com',
  'https://cdn.jsdelivr.net/npm/marked/marked.min.js',
  'https://telegram.org/js/telegram-web-app.js'
];

// Dynamic API patterns that must NEVER be cached (strictly Network-Only)
const NETWORK_ONLY_PATTERNS = [
  '/api/',
  '/v1/',
  '/screenshot',
  '/camera',
  '/webcam',
  '/stream/',
  '/touchpad/',
  '/ws'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      // Pre-cache core app shell
      try {
        await cache.addAll(APP_SHELL_RESOURCES);
      } catch (err) {
        console.warn('[SW] Some local shell resources failed to precache:', err);
      }
      // Optionally cache CDNs without blocking activation if offline during install
      for (const url of CDN_RESOURCES) {
        try {
          await cache.add(new Request(url, { mode: 'cors' }));
        } catch (_) {}
      }
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key.startsWith('monika-') && key !== CACHE_NAME).map((key) => {
          console.log('[SW] Purging outdated cache:', key);
          return caches.delete(key);
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // 1. Non-GET requests (POST, PUT, DELETE) -> Network Only
  if (request.method !== 'GET') {
    return;
  }

  // 2. Dynamic Realtime APIs & Feeds -> Network Only
  const isNetworkOnly = NETWORK_ONLY_PATTERNS.some((pattern) => url.pathname.includes(pattern));
  if (isNetworkOnly) {
    event.respondWith(
      fetch(request).catch(() => {
        return new Response(
          JSON.stringify({
            status: 'offline',
            error: 'Tarmoq mavjud emas. Kompyuter bilan jonli aloqa oflayn holatda.',
            offline: true
          }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json; charset=utf-8' }
          }
        );
      })
    );
    return;
  }

  // 3. Navigation Requests (HTML document) -> Network-First with Cache Fallback
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            const responseClone = response.clone();
            event.waitUntil(
              caches.open(CACHE_NAME)
                .then((cache) => cache.put(request, responseClone))
                .catch((err) => console.warn('[SW] Cache write failed:', err))
            );
          }
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(request);
          if (cached) return cached;
          const fallback = await caches.match('/');
          if (fallback) return fallback;
          return caches.match('/index.html');
        })
    );
    return;
  }

  // 4. Static Assets (Icons, Images, Scripts, Styles) -> Stale-While-Revalidate
  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      const fetchPromise = fetch(request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const clone = networkResponse.clone();
            event.waitUntil(
              caches.open(CACHE_NAME)
                .then((cache) => cache.put(request, clone))
                .catch((err) => console.warn('[SW] Cache write failed:', err))
            );
          }
          return networkResponse;
        })
        .catch(() => cachedResponse);

      return cachedResponse || fetchPromise;
    })
  );
});
