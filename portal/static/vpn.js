(function () {
  var P = window.Portal;
  var el = P.el;
  var box = null;
  var timer = null;
  var busy = false;

  function note(text) {
    box.textContent = "";
    box.appendChild(el("h2", { text: "VPN" }));
    box.appendChild(el("div", { class: "stub", text: text }));
  }

  function seen(d) {
    if (d.self) return "this VPS";
    if (d.online) return "online";
    if (!d.last_seen || /^0001-/.test(d.last_seen)) return "offline";
    var t = Date.parse(d.last_seen);
    return isNaN(t) ? "offline" : "offline, last seen " + P.fmtTime(Math.round(t / 1000));
  }

  function toggle(sw) {
    if (busy) return;
    var action = sw.saved === "on" ? "off" : "on";
    var text = action === "on"
      ? "Route VPN client traffic through WARP?"
      : "Stop routing VPN client traffic through WARP (clients exit directly via the VPS IP)?";
    P.ask(text, { ok: "Yes" }).then(function (yes) {
      if (!yes) return;
      busy = true;
      note("Switching " + action + "...");
      P.api("POST", "/api/vpn/warp", { action: action, confirm: "route-vpn" }).then(function (r) {
        busy = false;
        render(r);
      }).catch(function (e) {
        busy = false;
        note("Switch failed: " + e.message);
        setTimeout(load, 3000);
      });
    });
  }

  function render(r) {
    box.textContent = "";
    box.appendChild(el("h2", { text: "VPN" }));
    if (r.result) box.appendChild(el("div", { class: "sess-meta", text: "Last action: " + r.result }));

    var sw = r.switch;
    var warpHead = el("div", { class: "row" });
    if (sw) {
      warpHead.appendChild(el("span", { class: "pcdot " + (sw.warp_ok ? "ok" : "bad") }));
      warpHead.appendChild(el("strong", { text: sw.warp_ok ? "WARP connected" : "WARP not responding" }));
      if (sw.warp_ip) warpHead.appendChild(el("span", { class: "sess-meta", text: "exit IP " + sw.warp_ip }));
    } else {
      warpHead.appendChild(el("span", { class: "pcdot bad" }));
      warpHead.appendChild(el("strong", { text: "WARP switch unavailable" }));
    }
    box.appendChild(warpHead);

    if (sw) {
      var on = sw.saved === "on";
      var state = on ? (sw.rules === "on" ? "On" : "On (paused: WARP or tunnel down, clients exit directly)") : "Off";
      var btn = el("button", { type: "button", class: "btn" + (on ? "" : " primary"), text: on ? "Turn off" : "Turn on" });
      btn.addEventListener("click", function () { toggle(sw); });
      box.appendChild(el("div", { class: "row" }, [
        el("span", { class: "pcdot " + (on ? (sw.rules === "on" ? "ok" : "bad") : "wait") }),
        el("span", { class: "pcname", text: "Route VPN clients through WARP: " + state }),
        btn
      ]));
    }
    box.appendChild(el("div", { class: "sess-meta", text: "Claude Code always uses WARP; this switch affects only VPN clients." }));

    if (r.exit_node) {
      box.appendChild(el("div", { class: "sess-meta", text: "Exit node: " + (r.exit_node.approved ? "advertised and approved" : "not approved") }));
    }

    box.appendChild(el("h3", { text: "Tailscale devices" }));
    var list = el("div", { class: "pclist" });
    (r.devices || []).forEach(function (d) {
      list.appendChild(el("div", { class: "pcrow" }, [
        el("span", { class: "pcdot " + (d.online ? "ok" : "wait") }),
        el("span", { class: "pcname", text: d.name }),
        el("span", { class: "sess-meta", text: d.ip }),
        el("span", { class: "sess-meta", text: d.os }),
        el("span", { class: "sess-meta", text: seen(d) + (d.active && !d.self ? ", active" : "") })
      ]));
    });
    box.appendChild(list);

    (r.errors || []).forEach(function (e) { box.appendChild(el("div", { class: "warn", text: e })); });

    box.appendChild(el("h3", { text: "Switch log" }));
    var j = r.journal || [];
    if (!j.length) box.appendChild(el("div", { class: "sess-meta", text: "No switches yet" }));
    j.forEach(function (i) {
      box.appendChild(el("div", { class: "sess-meta", text: P.fmtTime(i.ts) + "  " + i.email + "  " + i.action + "  " + i.result }));
    });
  }

  function load(refresh) {
    if (busy) return Promise.resolve();
    if (refresh) {
      P.api("POST", "/api/vpn/refresh", {}).then(function () { setTimeout(function () { load(); }, 4000); }, function () {});
    }
    return P.api("GET", "/api/vpn").then(function (r) { if (!busy) render(r); }).catch(function (e) {
      note("VPN status failed: " + e.message);
    });
  }

  P.tabs.vpn = {
    init: function (section) {
      box = el("div", { class: "pcbox" });
      section.appendChild(box);
      note("Loading...");
    },
    onShow: function () {
      load(true);
      if (timer) clearInterval(timer);
      timer = setInterval(function () {
        var s = document.querySelector('[data-tab="vpn"]');
        if (s && !s.hidden) load();
      }, 30000);
    }
  };
})();
