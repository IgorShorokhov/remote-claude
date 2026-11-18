(function () {
  var P = window.Portal;
  function cancel(ev) { ev.preventDefault(); }
  P.input = {
    guard: function (root) {
      ["contextmenu", "selectstart", "dragstart", "gesturestart", "gesturechange", "gestureend"].forEach(function (name) {
        root.addEventListener(name, cancel, true);
      });
      root.addEventListener("keydown", function (ev) {
        if (P.input.shortcut(ev)) ev.preventDefault();
      }, true);
      root.addEventListener("wheel", function (ev) {
        if (ev.ctrlKey || ev.metaKey) ev.preventDefault();
      }, { capture: true, passive: false });
    },
    shortcut: function (ev) {
      var k = ev.key.toLowerCase();
      return ((ev.ctrlKey || ev.metaKey) && /^(w|t|l|r|n|h|j|d|f|p|s|o|u|tab|[0-9]|[+\-=])$/.test(k)) ||
        (ev.altKey && /^(arrowleft|arrowright|home|d)$/.test(k)) ||
        /^(f5|f11|contextmenu)$/.test(k) || (ev.shiftKey && k === "f10");
    },
    touch: function (root, options) {
      var finger = null;
      function handle(ev) {
        var target = options.target(ev.target);
        if (!target || (options.mode() === "mouse" && !options.tap)) return;
        if (options.mode() !== "mouse") {
          ev.preventDefault();
          ev.stopImmediatePropagation();
        }
        if (ev.type === "touchstart") {
          finger = ev.touches.length === 1 ? { id: ev.touches[0].identifier, x: ev.touches[0].clientX, y: ev.touches[0].clientY, target: target, moved: false } : null;
        } else if (ev.type === "touchmove") {
          if (!finger || ev.touches.length !== 1 || ev.touches[0].identifier !== finger.id) { finger = null; return; }
          var t = ev.touches[0];
          var dx = finger.x - t.clientX;
          var dy = finger.y - t.clientY;
          if (Math.abs(dx) + Math.abs(dy) < 4) return;
          if (!finger.moved && options.drag) options.drag();
          finger.moved = true;
          if (options.mode() === "pan") options.pan(finger.target, dx, dy);
          else if (options.mode() !== "mouse") options.wheel(finger.target, finger.x, finger.y, Math.max(-120, Math.min(120, dy)));
          finger.x = t.clientX;
          finger.y = t.clientY;
        } else {
          if (ev.type === "touchend" && !ev.touches.length && finger && !finger.moved && options.tap) options.tap(finger.target);
          finger = null;
        }
      }
      ["touchstart", "touchmove", "touchend", "touchcancel"].forEach(function (name) {
        root.addEventListener(name, handle, { capture: true, passive: false });
      });
    },
    scroller: function (root, active) {
      var pending = null;
      var timer = null;
      var emitted = new WeakSet();
      function cancelPending() {
        if (timer !== null) clearTimeout(timer);
        timer = null;
        pending = null;
      }
      function queue(target, x, y, dy, dx) {
        if (!active() || root.hidden || !target.isConnected) return;
        if (pending && pending.target !== target) cancelPending();
        if (!pending) pending = { target: target, dx: 0, dy: 0 };
        pending.x = x;
        pending.y = y;
        pending.dx += dx || 0;
        pending.dy += dy || 0;
        if (timer !== null) return;
        timer = setTimeout(function () {
          var p = pending;
          pending = null;
          timer = null;
          if (!p || !active() || root.hidden || !p.target.isConnected || (!p.dx && !p.dy)) return;
          var win = p.target.ownerDocument.defaultView;
          var event = new win.WheelEvent("wheel", { bubbles: true, cancelable: true, clientX: p.x, clientY: p.y, deltaX: p.dx, deltaY: p.dy, deltaMode: 0 });
          emitted.add(event);
          p.target.dispatchEvent(event);
        }, 30);
      }
      root.addEventListener("wheel", function (ev) {
        if (emitted.has(ev)) return;
        var target = ev.target.closest && ev.target.closest("#noVNC_container canvas");
        if (!target) return;
        ev.preventDefault();
        ev.stopImmediatePropagation();
        if (ev.ctrlKey || ev.metaKey) { cancelPending(); return; }
        var unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? target.clientHeight : 1;
        queue(target, ev.clientX, ev.clientY, ev.deltaY * unit, ev.deltaX * unit);
      }, { capture: true, passive: false });
      root.addEventListener("visibilitychange", cancelPending);
      root.defaultView.addEventListener("pagehide", cancelPending);
      return { wheel: queue, cancel: cancelPending };
    },
    wheel: function (target, x, y, dy) {
      var win = target.ownerDocument.defaultView;
      target.dispatchEvent(new win.WheelEvent("wheel", { bubbles: true, cancelable: true, clientX: x, clientY: y, deltaY: dy, deltaMode: 0 }));
    },
    fullscreen: function (button, surface, focus, status, keys) {
      var active = false;
      button.addEventListener("click", function () {
        if (document.fullscreenElement === surface) { document.exitFullscreen().catch(function () {}); return; }
        if (!surface.requestFullscreen) { status.textContent = "Fullscreen not supported. Use on-screen keys."; return; }
        surface.requestFullscreen().then(function () {
          focus();
          if (!navigator.keyboard || !navigator.keyboard.lock) {
            status.textContent = "Keyboard lock not supported. Use on-screen keys.";
            return;
          }
          return (keys ? navigator.keyboard.lock(keys) : navigator.keyboard.lock()).then(function () {
            if (document.fullscreenElement !== surface) { navigator.keyboard.unlock(); return; }
            status.textContent = "Keyboard lock on. Exit: button or long Esc.";
            focus();
          }).catch(function () {
            if (document.fullscreenElement === surface) status.textContent = "Keyboard lock denied. Use on-screen keys.";
          });
        }).catch(function () { status.textContent = "Fullscreen unavailable. Use on-screen keys."; });
      });
      document.addEventListener("fullscreenchange", function () {
        var now = document.fullscreenElement === surface;
        button.textContent = now ? "Exit fullscreen" : "Fullscreen";
        button.title = button.textContent;
        button.setAttribute("aria-label", button.textContent);
        if (active && !now) {
          if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
          status.textContent = "Keyboard lock off. Use on-screen keys.";
        }
        active = now;
      });
    },
    chord: function (target, key, code, modifier) {
      if (!target) return;
      var win = target.ownerDocument.defaultView;
      target.focus();
      var ctrl = modifier === "Control";
      var modCode = modifier + "Left";
      function emit(type, k, c, on) {
        target.dispatchEvent(new win.KeyboardEvent(type, { key: k, code: c, ctrlKey: ctrl && on, altKey: !ctrl && on, bubbles: true, cancelable: true }));
      }
      emit("keydown", modifier, modCode, true);
      try { emit("keydown", key, code, true); emit("keyup", key, code, true); }
      finally { emit("keyup", modifier, modCode, false); }
    },
    mode: function () {
      return window.matchMedia && window.matchMedia("(pointer:coarse)").matches ? "scroll" : "mouse";
    }
  };
})();
