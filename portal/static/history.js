(function () {
  var P = window.Portal;
  var el = P.el;
  var ui = {};
  var st = { q: "", offset: 0, total: 0 };
  var loadEpoch = 0;

  function fmtIso(s) {
    if (!s) return "";
    var d = new Date(s);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function load() {
    var epoch = ++loadEpoch;
    var url = "/api/history?limit=50&offset=" + st.offset + "&q=" + encodeURIComponent(st.q);
    ui.msg.textContent = "";
    P.api("GET", url).then(function (r) {
      if (epoch !== loadEpoch) return;
      st.total = r.total;
      ui.list.textContent = "";
      r.items.forEach(function (it) {
        var btn = el("button", { type: "button", class: "btn", text: "Resume" });
        btn.addEventListener("click", function () {
          btn.disabled = true;
          P.api("POST", "/api/history/" + it.id + "/resume", {})
            .then(function (res) {
              try { sessionStorage.setItem("portal-open-session", res.name); } catch (e) {}
              P.goto("code");
              window.dispatchEvent(new CustomEvent("portal-open-session", { detail: res.name }));
            })
            .catch(function (e) { ui.msg.textContent = e.message; })
            .then(function () { btn.disabled = false; });
        });
        var del = el("button", { type: "button", class: "btn chat-delete", text: "Delete", "aria-label": "Delete chat" });
        del.addEventListener("click", function () {
          P.ask("Delete chat?", { ok: "Delete" }).then(function (yes) {
            if (!yes) return;
            del.disabled = true;
            return P.api("DELETE", "/api/history/" + it.id, { confirm: it.id }).then(load);
          }).catch(function (e) { ui.msg.textContent = e.message; del.disabled = false; });
        });
        ui.list.appendChild(el("li", { class: "hist-item" }, [
          el("div", { class: "hist-main" }, [
            el("div", { class: "hist-prev", text: it.preview || "(no text)" }),
            el("div", { class: "muted small", text: (it.cwd || "?") + " · " + (fmtIso(it.started) || P.fmtTime(it.modified)) + " · " + P.fmtBytes(it.size) + " · " + it.id.slice(0, 8) })
          ]),
          btn,
          del
        ]));
      });
      if (!r.items.length) ui.list.appendChild(el("li", { class: "muted", text: "Nothing found" }));
      var from = r.total ? st.offset + 1 : 0;
      ui.page.textContent = from + "-" + (st.offset + r.items.length) + " of " + r.total;
      ui.prev.disabled = st.offset === 0;
      ui.next.disabled = st.offset + 50 >= r.total;
    }).catch(function (e) { if (epoch === loadEpoch) ui.msg.textContent = "Error: " + e.message; });
  }

  P.tabs.history = {
    init: function (section) {
      section.classList.add("hist");
      var q = el("input", { type: "search", class: "hist-search", placeholder: "Search text or folder", autocomplete: "off" });
      var go = el("button", { type: "button", class: "btn", text: "Search" });
      function search() { st.q = q.value.trim(); st.offset = 0; load(); }
      go.addEventListener("click", search);
      q.addEventListener("keydown", function (ev) { if (ev.key === "Enter") search(); });
      ui.list = el("ul", { class: "hist-list" });
      ui.msg = el("p", { class: "err" });
      ui.page = el("span", { class: "muted small" });
      ui.prev = el("button", { type: "button", class: "btn", text: "Back" });
      ui.next = el("button", { type: "button", class: "btn", text: "Next" });
      ui.prev.addEventListener("click", function () { st.offset = Math.max(0, st.offset - 50); load(); });
      ui.next.addEventListener("click", function () { st.offset += 50; load(); });
      section.appendChild(el("div", { class: "row" }, [q, go]));
      section.appendChild(ui.msg);
      section.appendChild(ui.list);
      section.appendChild(el("div", { class: "row" }, [ui.prev, ui.page, ui.next]));
      load();
    },
    onShow: function () {}
  };
})();
