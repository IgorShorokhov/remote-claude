(function () {
  function createPolicy(budget) {
    budget = budget || { lastReload: null, streak: 0, stopped: false };
    var lowSince = null;
    var healthySince = null;
    return {
      resetWindow: function () { lowSince = null; healthySince = null; },
      sample: function (now, s) {
        if (budget.stopped) return null;
        if (!s.visible || !s.active || s.typing || !s.connected || !Number.isFinite(s.fps) || s.fps < 0) {
          lowSince = null; healthySince = null; return null;
        }
        if (s.fps >= 5) {
          lowSince = null;
          if (healthySince === null) healthySince = now;
          if (now - healthySince > 8000) budget.streak = 0;
          return null;
        }
        healthySince = null;
        if (lowSince === null) lowSince = now;
        if (now - lowSince <= 8000) return null;
        if (budget.streak >= 3) { budget.stopped = true; return "unstable"; }
        if (budget.lastReload !== null && now - budget.lastReload < 60000) return null;
        budget.lastReload = now;
        budget.streak++;
        lowSince = null;
        return "reload";
      }
    };
  }
  var budget = { lastReload: null, streak: 0, stopped: false };
  var records = [];
  function record(id, event, fps) {
    var entry = { at: Date.now(), desktop: id, event: event, fps: Math.round(fps * 100) / 100 };
    records.push(entry);
    records = records.slice(-50);
    try { window.sessionStorage.setItem("portal.frameWatchdog.events", JSON.stringify(records)); } catch (e) {}
    window.dispatchEvent(new CustomEvent("portal-frame-watchdog", { detail: entry }));
  }
  function observeCanvas(canvas) {
    var win = canvas.ownerDocument.defaultView;
    var context = canvas.getContext("2d");
    var methods = ["drawImage", "putImageData", "fillRect", "clearRect"];
    if (!context) {
      context = canvas.getContext("webgl2") || canvas.getContext("webgl");
      methods = ["drawArrays", "drawElements", "drawArraysInstanced", "drawElementsInstanced", "blitFramebuffer", "clear"];
    }
    if (!context) return null;
    var count = 0;
    var pending = null;
    var originals = [];
    methods.forEach(function (name) {
      var original = context[name];
      if (typeof original !== "function") return;
      function wrapped() {
        var result = original.apply(this, arguments);
        if (pending === null) pending = win.requestAnimationFrame(function () { count++; pending = null; });
        return result;
      }
      context[name] = wrapped;
      originals.push([name, original, wrapped]);
    });
    return {
      count: function () { return count; },
      stop: function () {
        if (pending !== null) win.cancelAnimationFrame(pending);
        originals.forEach(function (item) { if (context[item[0]] === item[2]) context[item[0]] = item[1]; });
      }
    };
  }
  function textField(node) {
    return !!(node && (node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName)));
  }
  function attach(id, frame, section, reconnect) {
    var policy = createPolicy(budget);
    var warning = window.Portal.el("p", { class: "warn frame-watchdog-warning", role: "status", hidden: "hidden", text: "Unstable connection. Press Reconnect." });
    section.appendChild(warning);
    var lastActivity = -Infinity;
    var lastTyping = -Infinity;
    var composing = false;
    var canvas = null;
    var counter = null;
    var doc = null;
    var prevCount = 0;
    var prevAt = performance.now();
    var restore = null;
    var cleanups = [];
    function input(ev) {
      var now = performance.now();
      if (ev.isTrusted) lastActivity = now;
      if (/^(keydown|beforeinput|input|compositionstart|compositionend)$/.test(ev.type)) lastTyping = now;
      if (ev.type === "compositionstart") composing = true;
      if (ev.type === "compositionend") composing = false;
    }
    function listen(target) {
      ["pointerdown", "pointermove", "touchmove", "wheel", "keydown", "beforeinput", "input", "compositionstart", "compositionend"].forEach(function (type) {
        target.addEventListener(type, input, { capture: true, passive: true });
        cleanups.push(function () { target.removeEventListener(type, input, true); });
      });
    }
    function clearClient() {
      if (counter) counter.stop();
      cleanups.forEach(function (f) { f(); });
      cleanups = [];
      counter = null; canvas = null; doc = null;
      composing = false;
      policy.resetWindow();
      prevAt = performance.now(); prevCount = 0;
    }
    function bind() {
      clearClient();
      try { doc = frame.contentDocument; if (doc) listen(doc); } catch (e) { doc = null; }
    }
    frame.addEventListener("load", bind);
    section.addEventListener("pointerdown", input, { capture: true, passive: true });
    section.addEventListener("wheel", input, { capture: true, passive: true });
    section.addEventListener("keydown", input, true);
    section.addEventListener("input", input, true);
    document.addEventListener("visibilitychange", function () { policy.resetWindow(); prevAt = performance.now(); prevCount = counter ? counter.count() : 0; });
    function sample() {
      var now = performance.now();
      warning.hidden = !budget.stopped;
      if (budget.stopped) { clearInterval(timer); clearClient(); return; }
      try {
        var current = doc && doc.querySelector("#noVNC_container canvas");
        if (current !== canvas) {
          if (counter) counter.stop();
          canvas = current;
          counter = canvas ? observeCanvas(canvas) : null;
          prevCount = 0; prevAt = now; policy.resetWindow();
          return;
        }
        var count = counter ? counter.count() : 0;
        var elapsed = now - prevAt;
        var fps = counter && elapsed > 0 && elapsed <= 2500 ? (count - prevCount) * 1000 / elapsed : NaN;
        prevAt = now; prevCount = count;
        if (restore && canvas) {
          canvas.parentElement.scrollLeft = restore.x;
          canvas.parentElement.scrollTop = restore.y;
          if (now - restore.at > 3000) restore = null;
        }
        var action = policy.sample(now, {
          fps: fps,
          visible: document.visibilityState === "visible" && !section.hidden && frame.style.visibility !== "hidden",
          active: now - lastActivity <= 10000,
          typing: composing || now - lastTyping <= 10000 || textField(document.activeElement) || textField(doc && doc.activeElement),
          connected: !!(doc && doc.documentElement.classList.contains("noVNC_connected"))
        });
        if (!action) return;
        record(id, action, fps);
        if (action === "unstable") { warning.hidden = false; return; }
        restore = { x: canvas.parentElement.scrollLeft, y: canvas.parentElement.scrollTop, at: now };
        clearClient();
        reconnect();
      } catch (e) { policy.resetWindow(); }
    }
    var timer = setInterval(sample, 1000);
    window.addEventListener("pagehide", function () { clearInterval(timer); clearClient(); });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && !budget.stopped) { bind(); clearInterval(timer); timer = setInterval(sample, 1000); }
    });
  }
  var api = { createPolicy: createPolicy, attach: attach, observeCanvas: observeCanvas, events: function () { return records.slice(); } };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.Portal.frameWatchdog = api;
})();
