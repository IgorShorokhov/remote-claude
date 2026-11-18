(function () {
  var P = window.Portal;
  var id = crypto.randomUUID();
  try { sessionStorage.setItem("portal-presence-client", id); } catch (e) {}
  var lastInput = performance.now();
  var lastSent = lastInput;
  var screens = {};
  var seq = 0;
  function tab() {
    var button = document.querySelector('#tabs [aria-selected="true"]');
    var value = button && button.dataset.id;
    return value === "console" ? "code" : value;
  }
  function payload(leaving) {
    return { client_id: id, tab: tab(), visible: !leaving && document.visibilityState === "visible",
      focused: !leaving && document.visibilityState === "visible" && document.hasFocus(),
      last_input_ago_s: Math.min(86400, Math.max(0, (performance.now() - lastInput) / 1000)) };
  }
  function send(leaving) {
    var body = payload(leaving);
    if (!body.tab) return;
    lastSent = performance.now();
    if (leaving) {
      navigator.sendBeacon("/api/presence", new Blob([JSON.stringify(body)], { type: "application/json" }));
      return;
    }
    var order = ++seq;
    P.api("POST", "/api/presence", body)
      .then(function (data) { if (order !== seq) return; Object.keys(screens).forEach(function (key) { if (data.tabs && data.tabs[key]) screens[key](data.tabs[key].state); }); })
      .catch(function () {});
  }
  function input() {
    lastInput = performance.now();
    if (lastInput - lastSent >= 5000) send(false);
  }
  function watchInput(doc) {
    ["keydown", "pointerdown", "wheel", "input", "touchstart"].forEach(function (name) { doc.addEventListener(name, input, { capture: true, passive: true }); });
  }
  watchInput(document);
  document.addEventListener("visibilitychange", function () { send(false); });
  ["focus", "blur", "pageshow"].forEach(function (name) { window.addEventListener(name, function () { send(false); }); });
  window.addEventListener("pagehide", function () { send(true); });
  new MutationObserver(function () { send(false); }).observe(document.getElementById("tabs"), { subtree: true, childList: true, attributes: true, attributeFilter: ["aria-selected"] });
  setInterval(function () { send(false); }, 15000);
  P.presence = {
    attach: function (key, frame, section, warn, reconnect) {
      var waiting = false, state = "unknown", reloading = false, poll = null;
      // The usual report goes out every 15 seconds; while the screen is starting, ask every 2 seconds so it opens as soon as it is up.
      function hurry() {
        if (poll || !waiting) return;
        poll = setInterval(function () {
          if (!waiting || section.hidden || document.visibilityState !== "visible") { clearInterval(poll); poll = null; return; }
          if (state !== "running") send(false);
        }, 2000);
      }
      function curtain() {
        if (!waiting) return;
        warn.textContent = "Screen starting. It will reconnect automatically.";
        warn.hidden = false;
        frame.style.visibility = "hidden";
      }
      screens[key] = function (value) {
        if (value === "stopped" || value === "starting") { waiting = true; reloading = false; }
        if (value === "running" && waiting && state !== "running" && !reloading) { reloading = true; reconnect(); }
        state = value;
        curtain();
        hurry();
      };
      frame.addEventListener("load", function () {
        try {
          var doc = frame.contentDocument;
          watchInput(doc);
          ["focus", "blur"].forEach(function (name) { frame.contentWindow.addEventListener(name, function () { send(false); }); });
          var proto = frame.contentWindow.CanvasRenderingContext2D.prototype;
          ["drawImage", "putImageData", "fillRect"].forEach(function (name) {
            var original = proto[name];
            proto[name] = function () {
              var result = original.apply(this, arguments);
              if (waiting && state === "running" && this.canvas.width > 0 && this.canvas === doc.querySelector("#noVNC_container canvas") && doc.documentElement.classList.contains("noVNC_connected")) {
                waiting = false; reloading = false;
                frame.contentWindow.requestAnimationFrame(function () { warn.hidden = true; frame.style.visibility = ""; });
              }
              return result;
            };
          });
          curtain();
        } catch (e) { curtain(); }
      });
    }
  };
  send(false);
})();
