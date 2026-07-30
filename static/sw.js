// Service Worker — caches OpenCV.js and WASM so they load instantly after first visit
// These two files are 14MB total and the biggest cause of slow scanner startup on mobile.

const CACHE_NAME = 'scanstory-opencv-v1';
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
