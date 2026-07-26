/* Service worker: caches the app shell so it opens instantly and works offline.
   data.json is always fetched from the network first, falling back to cache,
   so you never see a stale ranking when a fresh one is available. */
const CACHE = 'shortlist-v1';
const SHELL = ['index.html', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Data: network first, cache as fallback.
  if (url.pathname.endsWith('data.json')) {
    e.respondWith(
      fetch(req).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return r;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Shell: cache first.
  e.respondWith(caches.match(req).then(r => r || fetch(req)));
});
