// Service Worker — caches OpenCV.js and WASM so they load instantly after first visit
// These two files are 14MB total and the biggest cause of slow scanner startup on mobile.
//
// v1 -> v2 (creator-frontend-hardening wave): the QR-generation CDN script and the Tailwind
// CDN script were removed from the creator page and replaced with local files
// (static/vendor/qrcode/qrcode.min.js, static/css/tailwind.build.css). This SW never cached
// those CDN scripts itself (it only ever intercepts /static/js/opencv* below), so there is
// nothing stale to migrate in the fetch handler - but the cache name is still bumped so any
// previously-installed SW runs its `activate` cleanup and starts fresh rather than keep an
// old install alive indefinitely. The new local assets are small (~90KB combined) and are not
// added to OPENCV_FILES/precached here: they are not the 14MB-class problem this SW exists to
// solve, and the plain browser HTTP cache already covers them without adding fetch-intercept
// logic to this file (kept intentionally unchanged otherwise, per scanner-cache ownership).
const CACHE_NAME = 'scanstory-opencv-v2';
const OPENCV_FILES = [
  '/static/js/opencv.js',
  '/static/js/opencv_js.wasm'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[SW] Pre-caching OpenCV files');
      return cache.addAll(OPENCV_FILES);
    }).catch(err => {
      // Non-fatal: if pre-cache fails, network will be used
      console.warn('[SW] Pre-cache failed (will cache on first use):', err);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  // Remove old caches when a new SW version is deployed
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Only intercept OpenCV static files
  if (!url.pathname.startsWith('/static/js/opencv')) return;

  event.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(event.request).then(cached => {
        if (cached) {
          console.log('[SW] Serving from cache:', url.pathname);
          return cached;
        }
        // Not cached yet — fetch from network and cache for next time
        return fetch(event.request).then(response => {
          if (response.ok) {
            cache.put(event.request, response.clone());
            console.log('[SW] Cached:', url.pathname);
          }
          return response;
        });
      })
    )
  );
});
