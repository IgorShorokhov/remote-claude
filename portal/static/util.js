(function () {
  var P = window.Portal = { tabs: {} };
  var el;
  var tipCount = 0;

  el = P.el = function (tag, props, children) {
    var e = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        if (k === "text") e.textContent = props[k];
        else if (k === "class") e.className = props[k];
        else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), props[k]);
        else e.setAttribute(k, props[k]);
      });
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  };

  var pop = null, popFor = null;
  function narrow() { return window.matchMedia("(max-width: 768px), (pointer: coarse)").matches; }
  function hideTip(t) {
    if (t && popFor !== t) return;
    if (pop) pop.hidden = true;
    if (popFor) popFor.classList.remove("open");
    popFor = null;
  }
  function placeTip(t) {
    if (!pop) { pop = el("div", { class: "tip-pop", role: "tooltip" }); pop.hidden = true; document.body.appendChild(pop); }
    popFor = t;
    pop.textContent = t.getAttribute("data-tip");
    pop.hidden = false;
    pop.classList.toggle("sheet", narrow());
    pop.style.left = ""; pop.style.top = "";
    if (narrow()) return;
    var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight, m = 8;
    var r = t.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    var x = Math.max(m, Math.min(r.left + r.width / 2 - w / 2, vw - w - m));
    var y = r.top - h - 6;
    if (y < m) y = r.bottom + 6;
    if (y + h > vh - m) y = Math.max(m, vh - h - m);
    pop.style.left = x + "px";
    pop.style.top = y + "px";
  }
  window.addEventListener("resize", function () { hideTip(); });
  window.addEventListener("scroll", function () { hideTip(); }, true);
  P.tip = function (text, id) {
    var note = el("span", { class: "sr-only", text: text });
    if (id) note.id = id;
    var t = el("button", { type: "button", class: "tip", "aria-label": "Help", "data-tip": text }, [el("span", { "aria-hidden": "true", text: "?" }), note]);
    if (!id) { note.id = "tip-" + (++tipCount); }
    t.setAttribute("aria-describedby", note.id);
    t.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      var on = !t.classList.contains("open");
      hideTip();
      if (on) { t.classList.add("open"); placeTip(t); }
    });
    t.addEventListener("mouseenter", function () { if (!narrow() && !popFor) placeTip(t); });
    t.addEventListener("mouseleave", function () { if (!t.classList.contains("open")) hideTip(t); });
    t.addEventListener("focus", function () { if (!narrow() && !popFor) placeTip(t); });
    t.addEventListener("blur", function () { if (!t.classList.contains("open")) hideTip(t); });
    return t;
  };
  document.addEventListener("click", function () { hideTip(); });

  var MSG = {
    auth: ["Session expired. Click to sign in", "Sign in"],
    forbidden: ["Not allowed for your role", null],
    server: ["Server not responding", "Reload"]
  };
  var bar = null;
  var lastFail = { at: 0, kind: null };
  var RETRY_MS = 5000;
  var account = null;
  var identityChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("portal-identity") : null;
  P.resetIdentity = function (broadcast, navigation) {
    if (P.identityLocked) return;
    if (navigation) window.dispatchEvent(new Event("portal-save-before-navigation"));
    P.identityLocked = true;
    window.dispatchEvent(new CustomEvent("portal-identity-reset", { detail: { navigation: !!navigation } }));
    var panel = document.getElementById("panel");
    if (panel) panel.replaceChildren();
    var tabs = document.getElementById("tabs");
    if (tabs) tabs.replaceChildren();
    var email = document.getElementById("who-email");
    if (email) email.textContent = "";
    // The chat list, the title and the popups live outside #panel: the previous account's names must not stay on the locked page.
    ["session-host", "page-title", "mode-actions"].forEach(function (id) { var node = document.getElementById(id); if (node) node.replaceChildren(); });
    Array.prototype.forEach.call(document.querySelectorAll("body > .dialogue-usage, .tip-pop"), function (node) { node.remove(); });
    try { sessionStorage.removeItem("portal-open-session"); } catch (e) {}
    if (broadcast && identityChannel) identityChannel.postMessage("reset");
  };
  if (identityChannel) identityChannel.onmessage = function () { P.resetIdentity(false); };
  // After a failure every GET waits RETRY_MS. A phone page woken from the background often fails its first request while the radio wakes
  // up; when the page is shown again or the network returns, let requests through at once.
  function clearFailure() { if (!document.hidden) lastFail.kind = null; }
  document.addEventListener("visibilitychange", clearFailure);
  document.addEventListener("resume", clearFailure);
  window.addEventListener("online", clearFailure);
  window.addEventListener("portal-resume", clearFailure);
  document.addEventListener("click", function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="/oauth2/sign_out"]');
    if (link) P.resetIdentity(true);
  }, true);
  window.addEventListener("pagehide", function (ev) {
    // Into the browser's page cache (a phone switching apps): keep the page, hide it until the account is checked on return.
    if (ev.persisted) { window.dispatchEvent(new Event("portal-save-before-navigation")); document.documentElement.classList.add("identity-pending"); return; }
    P.resetIdentity(false, true);
  });
  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;
    // Back from the page cache: the same account keeps its page and reconnects at once; anything else starts over.
    fetch("/api/me", { credentials: "same-origin", cache: "no-store", redirect: "manual", headers: { "X-Portal": "1", "Accept": "application/json" } })
      .then(function (r) {
        var tag = r.headers.get("X-Portal-Account");
        if (!r.ok || !tag || (account && tag !== account) || P.identityLocked) throw new Error("identity");
        document.documentElement.classList.remove("identity-pending");
        window.dispatchEvent(new Event("portal-resume"));
      })
      .catch(function () { location.reload(); });
  });

  P.notify = function (kind) {
    if (!bar) {
      bar = el("div", { class: "authbar", role: "alert", hidden: "hidden" });
      document.body.appendChild(bar);
    }
    if (!kind) {
      bar.hidden = true;
      bar.textContent = "";
      return;
    }
    var m = MSG[kind] || MSG.server;
    bar.textContent = "";
    bar.appendChild(el("span", { text: m[0] }));
    if (m[1]) {
      var b = el("button", { type: "button", class: "btn primary", text: m[1] });
      b.addEventListener("click", function () {
        if (kind === "auth") location.assign("/");
        else location.reload();
      });
      bar.appendChild(b);
    }
    bar.hidden = false;
  };

  function fail(kind, status, text, show) {
    var err = new Error(text || (MSG[kind] || MSG.server)[0]);
    err.status = status || 0;
    err.kind = kind;
    if (show !== false) P.notify(kind);
    return err;
  }

  // A background check: its failures must not raise the portal's connection banner.
  P.peek = function (url, signal) {
    if (P.identityLocked) return Promise.resolve(null);
    var headers = { "X-Portal": "1", "Accept": "application/json" };
    if (account) headers["X-Portal-Account"] = account;
    return fetch(url, { credentials: "same-origin", cache: "no-store", redirect: "manual", headers: headers, signal: signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  };

  // A file's text (the outbox viewer): the same account headers as P.api, but the body is returned as it is.
  P.text = function (url, signal) {
    var headers = { "X-Portal": "1" };
    if (account) headers["X-Portal-Account"] = account;
    return fetch(url, { credentials: "same-origin", cache: "no-store", signal: signal, headers: headers }).then(function (r) {
      if (r.status === 413) throw new Error("This file is too large to preview. Download it instead.");
      if (!r.ok) throw new Error("The preview could not be loaded (" + r.status + ").");
      return r.text();
    });
  };

  // A file or archive as raw bytes (the Skills section); the answer is JSON like P.api's.
  P.sendBytes = function (url, blob) {
    var headers = { "X-Portal": "1", "Accept": "application/json", "Content-Type": "application/octet-stream" };
    if (account) headers["X-Portal-Account"] = account;
    return fetch(url, { method: "POST", credentials: "same-origin", cache: "no-store", redirect: "manual", headers: headers, body: blob }).then(function (r) {
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
        if (!r.ok) { var err = new Error((j && typeof j.detail === "string" && j.detail) || "The server answered " + r.status); err.status = r.status; throw err; }
        return j;
      });
    });
  };

  P.api = function (method, url, body, extra) {
    if (P.identityLocked) return Promise.reject(fail("auth", 401, null, false));
    if (method === "GET" && lastFail.kind && Date.now() - lastFail.at < RETRY_MS) {
      return Promise.reject(fail(lastFail.kind, 0, null, false));
    }
    var opt = {
      method: method,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "manual",
      headers: { "X-Portal": "1", "Accept": "application/json" }
    };
    if (account) opt.headers["X-Portal-Account"] = account;
    if (extra && extra.signal) opt.signal = extra.signal;
    if (method !== "GET") {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body === undefined ? {} : body);
    }
    return fetch(url, opt).then(function (r) {
      var nextAccount = r.headers.get("X-Portal-Account");
      if (nextAccount) {
        if (account && account !== nextAccount) P.resetIdentity(true);
        else { account = nextAccount; P.account = account; }
      }
      if (r.status === 401 || (r.status === 403 && account && !nextAccount) || r.type === "opaqueredirect" || (r.status >= 300 && r.status < 400)) P.resetIdentity(true);
      if (P.identityLocked) throw fail("auth", 401);
      if (r.type === "opaqueredirect" || (r.status >= 300 && r.status < 400)) {
        throw fail("auth", r.status);
      }
      return r.text().then(function (t) {
        if (P.identityLocked) throw fail("auth", 401);
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
        var isObj = j !== null && typeof j === "object";
        var detail = isObj && typeof j.detail === "string" ? j.detail : null;
        if (r.status === 401) throw fail("auth", 401);
        if (r.status === 403 && (!isObj || detail === "forbidden")) throw fail("forbidden", 403);
        if (!isObj) {
          if (r.ok) throw fail("auth", r.status);
          if (r.status >= 500) throw fail("server", r.status);
          throw fail("server", r.status, "HTTP " + r.status);
        }
        if (!r.ok) {
          if (r.status >= 500 && !detail) throw fail("server", r.status);
          var err = new Error(detail || "HTTP " + r.status);
          err.status = r.status;
          err.kind = "app";
          err.data = j;
          throw err;
        }
        lastFail.kind = null;
        P.notify(null);
        return j;
      });
    }, function (e) {
      if (e && e.name === "AbortError") throw e;
      throw fail("server", 0);
    }).catch(function (e) {
      if (e && (e.kind === "server" || e.kind === "auth")) {
        lastFail.kind = e.kind;
        lastFail.at = Date.now();
      }
      throw e;
    });
  };

  window.addEventListener("message", function (ev) {
    if (ev.origin !== location.origin) return;
    var d = ev.data;
    if (d && typeof d === "object" && d.type === "portal-auth-expired") P.notify("auth");
  });

  var versionMeta = document.querySelector('meta[name="portal-version"]');
  P.version = versionMeta && /^[0-9a-f]{12}$/.test(versionMeta.content) ? versionMeta.content : "";
  var assetsMeta = document.querySelector('meta[name="portal-assets"]');
  var fileVersions = {};
  try { fileVersions = JSON.parse(assetsMeta ? assetsMeta.content : "{}") || {}; } catch (e) { fileVersions = {}; }
  // The async clipboard is refused in some browsers (Chrome on Windows when the page lost focus, insecure frames):
  // fall back to copying a hidden text field, which works inside the click itself.
  P.copyText = function (text) {
    function fallback() {
      var active = document.activeElement;
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "readonly");
      area.setAttribute("aria-hidden", "true");
      area.className = "copy-fallback";
      (document.fullscreenElement || document.body).appendChild(area);
      try {
        area.select();
        if (!document.execCommand("copy")) throw new Error("copy denied");
      } finally {
        area.remove();
        if (active && active.isConnected && active.focus) active.focus({ preventScroll: true });
      }
    }
    var board = navigator.clipboard;
    if (!board || !board.writeText) {
      try { fallback(); return Promise.resolve(); } catch (e) { return Promise.reject(e); }
    }
    return board.writeText(text).catch(function () { fallback(); });
  };
  P.asset = function (src) {
    if (!/^\/static\//.test(src) || src.indexOf("?") >= 0) return src;
    var own = fileVersions[src.slice(8)];
    if (typeof own === "string" && /^[0-9a-f]{10}$/.test(own)) return src + "?v=" + own;
    return P.version ? src + "?v=" + P.version : src;
  };

  var updateShown = false, lastCheck = 0;
  function checkUpdate() {
    if (!P.version || updateShown || Date.now() - lastCheck < 60000) return;
    lastCheck = Date.now();
    fetch("/", { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : ""; }).then(function (html) {
      var m = /<meta name="portal-version" content="([0-9a-f]{12})">/.exec(html);
      if (!m || m[1] === P.version || updateShown) return;
      updateShown = true;
      var reload = document.createElement("button");
      reload.type = "button";
      reload.className = "btn";
      reload.textContent = "Reload";
      reload.addEventListener("click", function () { location.reload(); });
      var later = document.createElement("button");
      later.type = "button";
      later.className = "btn";
      later.textContent = "Later";
      var bar = document.createElement("div");
      bar.className = "portal-update";
      bar.setAttribute("role", "status");
      bar.appendChild(document.createTextNode("Remote Claude was updated. Reload to get the new version."));
      bar.appendChild(reload);
      bar.appendChild(later);
      later.addEventListener("click", function () { bar.remove(); });
      document.body.appendChild(bar);
    }, function () {});
  }
  P.checkUpdate = checkUpdate;
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") checkUpdate(); });
  setInterval(checkUpdate, 300000);

  P.loadScript = function (src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = P.asset(src);
      s.onload = resolve;
      s.onerror = function () { reject(new Error("failed to load " + src)); };
      document.head.appendChild(s);
    });
  };

  // A phone or tablet without a mouse: the on-screen keyboard predicts and corrects words by rewriting text it already sent. A plain
  // textarea takes that in natively; the rich editor is a contenteditable, where Android keyboards are known to repeat text and add line breaks.
  P.touchOnly = function () {
    return window.matchMedia("(pointer:coarse)").matches && !window.matchMedia("(pointer:fine)").matches;
  };

  P.loadPromptEditor = function () {
    if (P.createPromptInput) return Promise.resolve();
    if (!P.promptEditorLoading) P.promptEditorLoading = P.loadScript("/static/vendor/prompt-editor/prompt-editor.js").catch(function (error) { P.promptEditorLoading = null; throw error; });
    return P.promptEditorLoading;
  };

  P.loadCss = function (href) {
    var l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = P.asset(href);
    document.head.appendChild(l);
  };

  P.fmtTime = function (sec) {
    if (!sec) return "";
    var d = new Date(sec * 1000);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  };

  P.fmtBytes = function (n) {
    if (n === null || n === undefined) return "?";
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB";
    if (n >= 1048576) return Math.round(n / 1048576) + " MB";
    return Math.round(n / 1024) + " KB";
  };

  P.ask = function (text, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var back = document.activeElement;
      var field = opts.value !== undefined ? el("input", { type: "text", maxlength: String(opts.max || 80), autocomplete: "off", "aria-label": text }) : null;
      if (field) field.value = opts.value;
      var ok = el("button", { type: "button", class: "btn primary", text: opts.ok || "OK" });
      var cancel = el("button", { type: "button", class: "btn", text: "Cancel" });
      var box = el("div", { class: "modal ask", role: "dialog", "aria-modal": "true", "aria-label": text }, [
        el("div", { class: "modal-box" }, [el("p", { text: text }), field, el("div", { class: "row end" }, [cancel, ok])])
      ]);
      function done(value) {
        box.remove();
        if (back && back.isConnected && back.focus) back.focus({ preventScroll: true });
        resolve(value);
      }
      ok.addEventListener("click", function () { done(field ? field.value : true); });
      cancel.addEventListener("click", function () { done(null); });
      box.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); done(null); }
        else if (ev.key === "Tab") {
          var first = field || cancel;
          if (ev.shiftKey && document.activeElement === first) { ok.focus(); ev.preventDefault(); }
          else if (!ev.shiftKey && document.activeElement === ok) { first.focus(); ev.preventDefault(); }
        }
        else if (ev.key === "Enter" && ev.target === field) { ev.preventDefault(); ok.click(); }
      });
      box.addEventListener("click", function (ev) { if (ev.target === box) done(null); });
      document.body.appendChild(box);
      if (field) { field.focus(); field.select(); } else ok.focus();
    });
  };

  P.goto = function (id) {
    if (P.show) P.show(id);
  };
})();
