"use strict";
// The portal renders VERSION and PRECACHE into this file, so it changes only when a static file does.
var VERSION = "__VERSION__";
var PRECACHE = "__PRECACHE__";
var ASSETS = "portal-assets";
var SHELL = "portal-shell";
var SHELL_KEY = "/";
var SIGN_IN = "/oauth2/start?rd=%2F";
var WAIT = 1800;
var PARALLEL = 6;
var DAY = 24 * 60 * 60 * 1000;

function ready() { return Array.isArray(PRECACHE); }
function absolute(url) { return new URL(url, self.location.origin).href; }
function storable(res) { return res.ok && res.type === "basic" && !res.redirected; }

// A saved copy remembers when it was fetched, so files without a version in the address are re-checked at most once a day.
function stamped(res) {
  return res.blob().then(function (body) {
    var headers = new Headers(res.headers);
    headers.set("x-saved-at", String(Date.now()));
    return new Response(body, { status: res.status, statusText: res.statusText, headers: headers });
  });
}

function save(cache, request, res) {
  return stamped(res).then(function (copy) { return cache.put(request, copy); });
}

function saveShell() {
  return Promise.all([caches.open(SHELL), fetch(SHELL_KEY, { credentials: "same-origin" })]).then(function (done) {
    if (storable(done[1])) return save(done[0], SHELL_KEY, done[1]);
  }).catch(function () {});
}

function precache() {
  if (!ready()) return Promise.resolve();
  return saveShell().then(function () { return caches.open(ASSETS); }).then(function (cache) {
    var queue = PRECACHE.slice();
    function next() {
      var url = queue.shift();
      if (!url) return Promise.resolve();
      return cache.match(url).then(function (hit) {
        if (hit) return;
        return fetch(url, { credentials: "same-origin" }).then(function (res) {
          if (storable(res)) return save(cache, url, res);
        }).catch(function () {});
      }).then(next);
    }
    var workers = [];
    for (var i = 0; i < PARALLEL; i++) workers.push(next());
    return Promise.all(workers);
  });
}

function prune() {
  return caches.keys().then(function (names) {
    return Promise.all(names.filter(function (n) { return n !== ASSETS && n !== SHELL; }).map(function (n) { return caches.delete(n); }));
  }).then(function () {
    if (!ready()) return;
    var keep = {};
    PRECACHE.forEach(function (u) { keep[absolute(u)] = true; });
    return caches.open(ASSETS).then(function (cache) {
      return cache.keys().then(function (requests) {
        return Promise.all(requests.filter(function (r) { return !keep[r.url]; }).map(function (r) { return cache.delete(r); }));
      });
    });
  });
}

function old(hit) {
  var at = Number(hit.headers.get("x-saved-at"));
  return !(at > 0) || Date.now() - at > DAY;
}

function refresh(cache, request) {
  return fetch(request, { cache: "no-cache", credentials: "same-origin" }).then(function (res) {
    if (storable(res)) return save(cache, request, res);
  }).catch(function () {});
}

function asset(event, url) {
  var key = new Request(url.href);
  return caches.open(ASSETS).then(function (cache) {
    return cache.match(key).then(function (hit) {
      if (hit) {
        if (!url.searchParams.has("v") && old(hit)) event.waitUntil(refresh(cache, key));
        return hit;
      }
      return fetch(event.request).then(function (res) {
        if (storable(res)) event.waitUntil(save(cache, key, res.clone()));
        return res;
      });
    });
  });
}

function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms, null); }); }

// The page itself always asks the server first (the login may have expired); the saved copy only covers a slow or missing network.
function shell(event) {
  return caches.open(SHELL).then(function (cache) {
    var network = Promise.resolve(event.preloadResponse).then(function (early) {
      return early || fetch(event.request);
    }).then(function (res) {
      if (res.status === 401) return Response.redirect(SIGN_IN, 302);
      if (storable(res)) event.waitUntil(save(cache, SHELL_KEY, res.clone()));
      return res;
    });
    event.waitUntil(network.catch(function () {}));
    return cache.match(SHELL_KEY).then(function (saved) {
      if (!saved) return network;
      return Promise.race([network.catch(function () { return null; }), pause(WAIT)]).then(function (res) {
        return res && (res.ok || res.type === "opaqueredirect" || res.type === "opaque" || res.status < 500) ? res : saved;
      });
    });
  });
}

self.addEventListener("install", function (event) {
  event.waitUntil(precache().then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(Promise.all([
    prune(),
    self.registration.navigationPreload ? self.registration.navigationPreload.enable().catch(function () {}) : null
  ]).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (!ready() || request.method !== "GET" || request.headers.has("range")) return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    if (url.pathname === "/") event.respondWith(shell(event));
    return;
  }
  if (url.pathname.indexOf("/static/") === 0 && url.pathname !== "/static/sw.js") event.respondWith(asset(event, url));
});
