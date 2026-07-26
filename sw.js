/* Service worker: caches the app shell so it opens instantly and works offline.
   data.json is always fetched from the network first, falling back to cache,
   so you never see a stale ranking when a fresh one is available.

   CACHE VERSION: bump this string any time index.html, this file, or any shell
   asset changes. Bumping it is what makes an already-installed phone actually
   pick up the update — without it, a phone that installed an old version can
   keep serving that old index.html indefinitely, because "cache first" means
   exactly that. (This is what happened on 2026-07-26 — v1 shipped with
   pure cache-first for the shell and no version bump, so an already-installed
   phone never saw the new Movers tab.) */
const CACHE = 'shortlist-v3';
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

  // Shell (index.html, manifest, icons): network first, so an update to the
  // app itself shows up the very next time it's opened with a connection,
  // instead of waiting for a cache-version bump. Falls back to cache when
  // offline, which is the only time the cached copy is actually used.
  e.respondWith(
    fetch(req).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return r;
    }).catch(() => caches.match(req))
  );
});
