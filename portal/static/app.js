(function () {
  var P = window.Portal;
  var ALL = [
    ["code", "Code Web"],
    ["backup", "API Agent"],
    ["console", "Console"],
    ["prompts", "Prompts"],
    ["skills", "Skills"],
    ["instructions", "System instructions"],
    ["memory", "Memory"],
    ["browser", "Browser"],
    ["chat", "Chat"],
    ["devices", "Computers"],
    ["pc", "PC"],
    ["sync", "Sync"],
    ["vpn", "VPN"],
    ["access", "Access"]
  ];
  var tabsEl = document.getElementById("tabs");
  var panel = document.getElementById("panel");
  var allowed = [];
  var sections = {};
  var current = null;

  function show(id) {
    if (allowed.indexOf(id) < 0) return;
    var key = id === "console" ? "code" : id;
    var old = current === "console" ? "code" : current;
    if (old && old !== key && P.tabs[old] && P.tabs[old].onHide) P.tabs[old].onHide();
    if (key === "code" && P.tabs.code.setView) P.tabs.code.setView(id);
    if (!sections[key]) {
      var s = P.el("section", { class: "tabpane", "data-tab": key });
      panel.appendChild(s);
      sections[key] = s;
      if (P.tabs[key]) P.tabs[key].init(s);
    }
    Object.keys(sections).forEach(function (k) { sections[k].hidden = k !== key; });
    Array.prototype.forEach.call(document.querySelectorAll(".tab[data-id]"), function (b) {
      b.setAttribute("aria-selected", b.dataset.id === id ? "true" : "false");
    });
    current = id;
    if (P.shell) P.shell.activate(id, sections[key]);
    if (P.tabs[key] && P.tabs[key].onShow) P.tabs[key].onShow();
    try { history.replaceState(null, "", "#" + id); } catch (e) {}
  }
  P.show = show;

  function render(me) {
    document.getElementById("who-email").textContent = me.email;
    P.role = me.role;
    document.getElementById("who-role").textContent = me.role === "admin" ? "Full access" : "Access granted";
    if (P.shell) P.shell.profile(me);
    allowed = me.tabs.map(function (t) { return t.id; });
    P.features = me.features || null;
    if (allowed.indexOf("code") >= 0 && me.role === "admin" && (!P.features || P.features.indexOf("console") >= 0)) allowed.push("console");
    if (P.embedded) allowed = allowed.filter(function (id) { return id === "code"; });
    ALL.forEach(function (t) {
      if (allowed.indexOf(t[0]) < 0) return;
      var b = P.el("button", { class: "tab", type: "button", role: "tab", "aria-label": t[1], "data-id": t[0], text: t[1] });
      b.addEventListener("click", function () { show(t[0]); });
      tabsEl.appendChild(b);
    });
    var want = location.hash.slice(1);
    show(allowed.indexOf("code") >= 0 ? "code" : allowed.indexOf(want) >= 0 ? want : allowed[0]);
  }

  window.addEventListener("hashchange", function () {
    var want = location.hash.slice(1);
    if (want !== current && allowed.indexOf(want) >= 0) show(want);
  });

  // The first profile request can fail while the server restarts or the network blinks; the page then keeps trying instead of
  // staying on an error until someone reloads it (16 Nov 2026: the chats never loaded after a failed first request).
  var profileWait = 2500;
  function loadProfile() {
    P.api("GET", "/api/me").then(render).catch(function (e) {
      if (P.identityLocked || e.kind === "auth" || e.kind === "forbidden") {
        panel.textContent = "Profile failed: " + e.message;
        return;
      }
      panel.textContent = "Connecting to the server…";
      profileWait = Math.min(profileWait * 2, 15000);
      setTimeout(loadProfile, Math.max(profileWait, 5200));
    });
  }
  loadProfile();
  function checkIdentity() {
    if (!P.identityLocked && document.visibilityState === "visible") P.api("GET", "/api/me").catch(function () {});
  }
  window.addEventListener("focus", checkIdentity);
  document.addEventListener("visibilitychange", checkIdentity);
  setInterval(checkIdentity, 5000);
  // Registered late and from a file: the page CSP blocks inline scripts, and the first visit should not compete with its own worker for the network.
  if ("serviceWorker" in navigator && !P.embedded) {
    window.addEventListener("load", function () {
      setTimeout(function () { navigator.serviceWorker.register("/static/sw.js", { scope: "/" }).catch(function () {}); }, 1200);
    });
  }
})();
