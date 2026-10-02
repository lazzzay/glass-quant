// Cockpit V7 · Service-Worker (PWA)
// Minimal Offline-Shell: cached index.html + system-explorer.html + letzte
// state.json / heartbeat.json / trades_snapshot.json.
// Für API-Calls (127.0.0.1:5757) wird nie gecached: immer Network-first.
// Bei Offline liefert der SW den letzten bekannten Snapshot aus.

const CACHE = "cockpit-v7-shell-v2"; // X-1 Tag 2 · Block I Tax-Loss-Tile (2026-04-21)
const SHELL = [
  "./",
  "./index.html",
  "./system-explorer.html",
  "./manifest.webmanifest",
  "./state.json",
  "./heartbeat.json",
  "./trades_snapshot.json"
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL).catch(() => {
        // Best-effort: einzelne Dateien können fehlen, kein Hard-Fail
        return Promise.all(SHELL.map(u => c.add(u).catch(() => null)));
      }))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // Nie cachen: API (Flask), SSE-Stream
  if(url.port === "5757" || url.pathname.endsWith("/stream")){
    return; // lass den Browser das normal fetchen
  }

  // Network-first mit Cache-Fallback für JSON-State-Dateien
  if(url.pathname.endsWith("state.json") ||
     url.pathname.endsWith("heartbeat.json") ||
     url.pathname.endsWith("trades_snapshot.json")){
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first für Shell (HTML, manifest)
  if(url.pathname.endsWith(".html") || url.pathname.endsWith(".webmanifest")){
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      }))
    );
    return;
  }
  // Default: pass-through
});
