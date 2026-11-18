(function () {
  var P = window.Portal;
  var handle = P.el("button", { id: "tools-handle", type: "button", "aria-label": "Tools", title: "Tools", "aria-controls": "tools-drawer", "aria-expanded": "false" });
  var nav = P.el("nav", { id: "tool-tabs", role: "tablist", "aria-label": "Other sections" });
  var role = P.el("span", { class: "badge" });
  var drawer = P.el("div", { id: "tools-drawer", hidden: "hidden", role: "region", "aria-label": "Mode tools" }, [
    P.el("div", { class: "tools-common" }, [nav, role, P.el("a", { class: "btn", href: "/oauth2/sign_out?rd=/", text: "Sign out" })])
  ]);
  var shell = P.el("div", { class: "tools-shell" }, [handle, drawer]);
  var panel = document.getElementById("panel");
  panel.before(shell);
  var groups = {};
  var active = null;
  var start = null;
  var suppressClick = false;

  function close(focus) {
    var inside = drawer.contains(document.activeElement);
    drawer.hidden = true;
    handle.setAttribute("aria-expanded", "false");
    if (focus || inside) handle.focus({ preventScroll: true });
  }
  function toggle() {
    if (drawer.hidden) {
      drawer.hidden = false;
      handle.setAttribute("aria-expanded", "true");
    } else close(false);
  }
  handle.addEventListener("click", function () {
    if (suppressClick) { suppressClick = false; return; }
    toggle();
  });
  handle.addEventListener("pointerdown", function (ev) {
    if (!ev.isPrimary || ev.button !== 0) return;
    suppressClick = false;
    start = { id: ev.pointerId, x: ev.clientX, y: ev.clientY };
    handle.setPointerCapture(ev.pointerId);
  });
  handle.addEventListener("pointerup", function (ev) {
    if (!start || start.id !== ev.pointerId) return;
    var dy = ev.clientY - start.y;
    if (Math.abs(dy) >= 28 && Math.abs(dy) > Math.abs(ev.clientX - start.x)) {
      toggle();
      suppressClick = true;
    }
    start = null;
  });
  handle.addEventListener("pointercancel", function () { start = null; });
  handle.addEventListener("lostpointercapture", function () { start = null; });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !drawer.hidden) { close(true); ev.preventDefault(); }
  });
  document.addEventListener("pointerdown", function (ev) {
    if (!drawer.hidden && !shell.contains(ev.target)) close(false);
  });
  window.addEventListener("blur", function () { start = null; });
  document.addEventListener("visibilitychange", function () { if (document.hidden) { start = null; close(false); } });
  document.addEventListener("fullscreenchange", function () {
    close(false);
    if (document.fullscreenElement && active && document.fullscreenElement === active) active.prepend(shell);
    else panel.before(shell);
  });

  function icon(button, symbol, label) {
    button.classList.add("desk-icon");
    button.setAttribute("data-icon", symbol);
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  }
  function codeTools(section, group) {
    var touch = window.matchMedia("(pointer:coarse)").matches;
    var actions = P.el("div", { class: "tools-code-actions" });
    section.querySelectorAll(".term-head > button.console-fullscreen" + (touch ? ", .term-head > button[aria-pressed]:not(.console-copy)" : "")).forEach(function (button) {
      if (button.textContent === "Chat" || button.textContent === "Console") return;
      if (button.textContent === "Scroll") icon(button, "↕", "Scroll");
      if (button.classList.contains("console-fullscreen")) icon(button, "⛶", "Fullscreen");
      actions.appendChild(button);
    });
    if (touch) {
      var keyboard = P.el("button", { type: "button", class: "btn", text: "Keyboard" });
      icon(keyboard, "⌨", "Keyboard");
      keyboard.addEventListener("click", function () {
        close(false);
        if (P.tabs.code.focusInput) P.tabs.code.focusInput();
      });
      actions.appendChild(keyboard);
    }
    if (!actions.querySelector(".console-fullscreen")) {
      var full = P.el("button", { type: "button", class: "btn", text: "Fullscreen" });
      icon(full, "⛶", "Fullscreen");
      P.input.fullscreen(full, section, function () {}, P.el("p", { class: "desk-hint", role: "status" }));
      actions.appendChild(full);
    }
    group.appendChild(actions);
    var keybar = section.querySelector(".keybar");
    if (keybar) group.appendChild(keybar);
    actions.addEventListener("click", function (ev) {
      if (ev.target.closest("button:not(.desk-icon)")) close(false);
    });
  }
  var codeChat = false;
  function visible() {
    var key = active && active.dataset.tab;
    shell.hidden = !key || !groups[key] || !groups[key].childElementCount || (key === "code" && codeChat);
    if (shell.hidden) close(false);
  }
  P.shell = {
    nav: nav,
    codeChat: function (on) { codeChat = !!on; visible(); },
    profile: function (me) {
      role.textContent = me.tabs.length > 1 ? "Full access" : "Access granted";
      document.getElementById("who-email").title = me.email;
    },
    activate: function (id, section) {
      close(false);
      active = section;
      Object.keys(groups).forEach(function (key) { groups[key].hidden = true; });
      var key = section.dataset.tab;
      if (!groups[key]) {
        var group = groups[key] = P.el("div", { class: "tools-group", "data-tools": key });
        var bar = section.querySelector(".desk-bar");
        if (bar) {
          bar.querySelectorAll("details").forEach(function (d) { d.open = true; });
          group.appendChild(bar);
        } else if (key === "code") codeTools(section, group);
        drawer.appendChild(group);
      }
      groups[key].hidden = false;
      visible();
    }
  };
})();
