(function () {
  var P = window.Portal;
  var el = P.el;
  var box = null;
  var timer = null;

  var STATES = {
    connected: ["ok", "connected"],
    disconnected: ["bad", "offline"],
    error: ["bad", "offline"],
    unknown: ["wait", "checking"],
    ro: ["warn", "read-only"]
  };

  function note(text) {
    box.textContent = "";
    box.appendChild(el("div", { class: "stub", text: text }));
  }

  function fmt(sec) {
    return sec ? P.fmtTime(sec) : "never";
  }

  function startSession(f, btn) {
    var base = ("pc-" + f.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-")).slice(0, 24).replace(/-+$/, "");
    var tries = 0;
    btn.disabled = true;
    function attempt(name) {
      return P.api("POST", "/api/sessions", { name: name, path: f.session_path, start_claude: true })
        .catch(function (e) {
          if (e.status === 409 && e.message === "session exists" && tries < 4) {
            tries += 1;
            return attempt(base.slice(0, 22) + "-" + (tries + 1));
          }
          throw e;
        });
    }
    attempt(base)
      .then(function (r) {
        P.goto("code");
        window.dispatchEvent(new CustomEvent("portal-open-session", { detail: r.name }));
      })
      .catch(function (e) { window.alert("Could not create session: " + e.message); })
      .then(function () { btn.disabled = false; });
  }

  function render(r) {
    box.textContent = "";
    if (r.state === "watcher_not_running") {
      note("PC watcher not running.");
      return;
    }
    var head = el("div", { class: "row" }, [
      el("span", { class: "pcdot " + (r.reachable ? "ok" : "bad") }),
      el("strong", { text: r.reachable ? "PC online" : "PC offline" }),
      el("span", { class: "sess-meta", text: "checked: " + fmt(r.checked) + (r.state === "stale" ? " (stale)" : "") }),
      el("span", { class: "sess-meta", text: "last online: " + fmt(r.last_ok) })
    ]);
    box.appendChild(head);
    box.appendChild(el("div", { class: "warn", text: "Use trusted PC folders only." }));
    if (!r.folders.length) {
      box.appendChild(el("div", { class: "stub", text: "No PC folders." }));
      return;
    }
    var list = el("div", { class: "pclist" });
    r.folders.forEach(function (f) {
      var st = STATES[f.state] || STATES.unknown;
      var usable = f.state === "connected" && r.state === "ok" && r.reachable;
      var btn = el("button", { class: "btn primary", type: "button", text: "New session" });
      btn.disabled = !usable;
      btn.addEventListener("click", function () { startSession(f, btn); });
      list.appendChild(el("div", { class: "pcrow" }, [
        el("span", { class: "pcdot " + st[0] }),
        el("span", { class: "pcname", text: f.name }),
        el("span", { class: "sess-meta", text: st[1] }),
        el("span", { class: "sess-meta", text: f.last_ok ? "last seen: " + P.fmtTime(f.last_ok) : "" }),
        btn
      ]));
    });
    box.appendChild(list);
  }

  function load() {
    return P.api("GET", "/api/pc").then(render).catch(function (e) {
      note("PC status failed (" + e.message + ").");
    });
  }

  P.tabs.pc = {
    init: function (section) {
      box = el("div", { class: "pcbox" });
      section.appendChild(box);
      note("Loading...");
    },
    onShow: function () {
      load();
      if (timer) clearInterval(timer);
      timer = setInterval(function () {
        var s = document.querySelector('[data-tab="pc"]');
        if (s && !s.hidden) load();
      }, 15000);
    }
  };
})();
