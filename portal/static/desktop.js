(function () {
  var P = window.Portal;
  var MODES = [["remote", "HD, 1280×720"], ["scale", "Fit screen"], ["off", "1:1 sharp text"]];
  var KEY = "portal-view-smooth";
  var QUALITY = "&stream_mode=-1025&video_quality=1&quality=8&compression=2" +
    "&dynamic_quality_min=6&dynamic_quality_max=7&treat_lossless=8" +
    "&jpeg_video_quality=4&webp_video_quality=4" +
    "&framerate=30&framerate_image_mode=30&framerate_streaming_mode=30" +
    "&video_time=0&video_area=10&video_out_time=1&video_scaling=2" +
    "&enable_webp=false&enable_hidpi=false&enable_webrtc=false" +
    "&enable_perf_stats=false&clipboard_up=false&clipboard_down=false";

  function savedMode() {
    if (!P.config || !P.config.enableResizeModes) return "remote";
    var v = null;
    try { v = localStorage.getItem(KEY); } catch (e) {}
    if (P.input && P.input.mode() === "scroll") return "remote";
    return v === "off" || v === "scale" ? v : "remote";
  }

  function makeDesktop(id, base, hint) {
    var syncVisible = function () {};
    return {
      init: function (section) {
        var resizeEnabled = !!(P.config && P.config.enableResizeModes);
        var mode = savedMode();
        var scrollControl = null;
        function cancelScroll() { if (scrollControl) scrollControl.cancel(); }
        function icon(button, symbol, label) {
          button.setAttribute("class", "btn desk-icon");
          button.setAttribute("data-icon", symbol);
          button.setAttribute("title", label);
          button.setAttribute("aria-label", label);
          return button;
        }
        var inputMode = P.input ? P.input.mode() : "mouse";
        if (!resizeEnabled && inputMode === "pan") inputMode = "scroll";
        var inputBtns = [["mouse", "Mouse"], ["scroll", "Scroll"], ["pan", "Pan 1:1"]].map(function (m) {
          var b = P.el("button", { class: "btn", type: "button", text: m[1], "data-input-mode": m[0], "aria-pressed": inputMode === m[0] ? "true" : "false" });
          b.addEventListener("click", function () {
            cancelScroll();
            if (m[0] === "pan" && !resizeEnabled) return;
            inputMode = m[0];
            if (inputMode === "pan" && resizeEnabled) setMode("off");
            inputBtns.forEach(function (n) { n.setAttribute("aria-pressed", n === b ? "true" : "false"); });
          });
          if (m[0] === "pan" && !resizeEnabled) b.setAttribute("disabled", "disabled");
          return icon(b, { mouse: "↖", scroll: "↕", pan: "✥" }[m[0]], m[1]);
        });
        var src = base + "vnc.html?autoconnect=1&reconnect=1&view_clip=false&resize=" + mode +
          "&path=" + base.slice(1) + "websockify" + QUALITY;
        var frame = P.el("iframe", {
          class: "desk-frame" + (resizeEnabled && mode === "remote" ? " desk-frame-smooth" : ""),
          title: id,
          src: src,
          allow: "clipboard-read; clipboard-write; fullscreen",
          referrerpolicy: "no-referrer"
        });
        if (P.input) P.input.guard(frame);
        function setMode(m) {
          if (!resizeEnabled) m = "remote";
          cancelScroll();
          mode = m;
          frame.setAttribute("class", "desk-frame" + (resizeEnabled && m === "remote" ? " desk-frame-smooth" : ""));
          if (m !== "off" && inputMode === "pan") {
            inputMode = "scroll";
            inputBtns.forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.inputMode === inputMode ? "true" : "false"); });
          }
          try { localStorage.setItem(KEY, m); } catch (e) {}
          try {
            frame.contentWindow.postMessage({ action: "resize", value: m }, location.origin);
          } catch (e) {}
          btns.forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.mode === m ? "true" : "false"); });
        }
        var btns = MODES.map(function (m) {
          var b = P.el("button", { class: "btn", type: "button", text: m[1], "data-mode": m[0], "aria-pressed": m[0] === mode ? "true" : "false" });
          if (!resizeEnabled) b.setAttribute("hidden", "hidden");
          b.addEventListener("click", function () { setMode(m[0]); });
          return b;
        });
        var full = P.el("button", { class: "btn", type: "button", text: "Full screen" });
        var inputStatus = P.el("p", { class: "input-status desk-hint", role: "status" });
        function clientCanvas() {
          try { return frame.contentDocument.querySelector("#noVNC_container canvas"); } catch (e) { return null; }
        }
        var mobileText = P.input.mobileDesktop(id, frame, clientCanvas, section);
        if (P.input) P.input.fullscreen(full, section, function () { var c = clientCanvas(); if (c) c.focus(); }, inputStatus);
        var keys = P.el("div", { class: "remote-keys", "aria-label": "Remote screen keys" }, [
          ["Ctrl+W", "w", "KeyW", "Control"], ["Ctrl+T", "t", "KeyT", "Control"],
          ["Ctrl+L", "l", "KeyL", "Control"], ["Ctrl+R", "r", "KeyR", "Control"],
          ["Alt+←", "ArrowLeft", "ArrowLeft", "Alt"], ["Alt+→", "ArrowRight", "ArrowRight", "Alt"]
        ].map(function (k) {
          var b = P.el("button", { class: "btn", type: "button", text: k[0] });
          b.addEventListener("click", function () { P.input.chord(clientCanvas(), k[1], k[2], k[3]); });
          return b;
        }));
        var warn = P.el("p", { class: "warn", text: "Screen did not load. Press Reconnect or reload the page (Ctrl+F5).", hidden: "hidden" });
        var loadTimer = null;
        var resizeTimers = [];
        var clientObserver = null;
        syncVisible = function () {
          if (section.hidden) return;
          try {
            if (frame.contentDocument.documentElement.classList.contains("noVNC_connected")) frame.contentWindow.postMessage({ action: "resize", value: mode }, location.origin);
          } catch (e) {}
        };
        function watchLoad() {
          warn.hidden = true;
          if (loadTimer) clearTimeout(loadTimer);
          loadTimer = setTimeout(function () { warn.hidden = false; }, 10000);
        }
        frame.addEventListener("load", function () {
          cancelScroll();
          resizeTimers.forEach(clearTimeout);
          resizeTimers = [];
          if (clientObserver) clientObserver.disconnect();
          if (loadTimer) clearTimeout(loadTimer);
          warn.hidden = true;
          try {
            var doc = frame.contentDocument;
            var wasConnected = false;
            function connectionChanged() {
              var connected = doc.documentElement.classList.contains("noVNC_connected");
              if (connected && !wasConnected) [400, 1200].forEach(function (delay) { resizeTimers.push(setTimeout(syncVisible, delay)); });
              wasConnected = connected;
            }
            clientObserver = new frame.contentWindow.MutationObserver(connectionChanged);
            clientObserver.observe(doc.documentElement, { attributes: true, attributeFilter: ["class"] });
            connectionChanged();
            P.input.guard(doc);
            scrollControl = P.input.scroller(doc, function () { return !section.hidden; });
            var css = doc.createElement("link");
            css.rel = "stylesheet";
            css.href = "/static/remote-input.css";
            doc.head.appendChild(css);
            P.input.touch(doc, {
              mode: function () { return inputMode; },
              target: function (n) { return n.closest && n.closest("#noVNC_container canvas"); },
              wheel: scrollControl.wheel,
              pan: function (canvas, dx, dy) { canvas.parentElement.scrollLeft += dx; canvas.parentElement.scrollTop += dy; }
            });
          } catch (e) {
            warn.textContent = "Input control unavailable. Reconnect after signing in.";
            warn.hidden = false;
          }
        });
        var reload = P.el("button", { class: "btn", type: "button", text: "Reconnect" });
        reload.addEventListener("click", function () {
          cancelScroll();
          watchLoad();
          frame.src = src.replace(/resize=\w+/, "resize=" + mode);
        });
        if (P.presence) P.presence.attach(id, frame, section, warn, function () { reload.click(); });
        if (P.frameWatchdog) P.frameWatchdog.attach(id, frame, section, function () { reload.click(); });
        var showStats = false;
        var stats = P.el("button", { class: "btn", type: "button", text: "Show FPS", "aria-pressed": "false" });
        stats.addEventListener("click", function () {
          cancelScroll();
          showStats = !showStats;
          stats.setAttribute("aria-pressed", showStats ? "true" : "false");
          stats.textContent = showStats ? "Hide FPS" : "Show FPS";
          src = src.replace(/enable_perf_stats=(true|false)/, "enable_perf_stats=" + showStats);
          watchLoad();
          frame.src = src.replace(/resize=\w+/, "resize=" + mode);
        });

        var viewTip = resizeEnabled ? "Pan 1:1 moves the visible part of the screen. Smooth is 1280×720 on PC and fits the window on phones." : "The screen always fits the window. Pan 1:1 is off while scale modes are disabled in config.";
        function helpLine(text, tip) { return P.el("p", { class: "desk-hint" }, [P.el("span", { text: text + " " }), P.tip(tip)]); }
        var note = P.el("div", { class: "desk-help-lines" }, [
          helpLine("Touch: Scroll or Mouse.", "Scroll: swipe to scroll chat or terminal. Mouse: tap to click, two fingers scroll, long press is right click."),
          helpLine("View modes.", viewTip),
          helpLine("Up to 30 FPS.", "Motion looks softer; check sharpness after it stops. Measure FPS while scrolling, not on still text."),
          helpLine("Shared screen.", hint || "Everyone who opens this tab sees the same screen.")
        ]);
        var tools = P.el("details", { class: "desk-tools" }, [
          P.el("summary", { class: "btn desk-icon", "data-icon": "⋯", title: "Screen controls", "aria-label": "Screen controls", text: "Screen controls" }),
          P.el("div", { class: "desk-tools-body" }, [P.el("div", { class: "desk-options" }, btns.concat([reload, stats])), mobileText.panel, keys])
        ]);
        var help = P.el("details", { class: "desk-help" }, [
          P.el("summary", { class: "btn desk-icon", "data-icon": "?", title: "Help", "aria-label": "Help", text: "Help" }),
          P.el("div", { class: "desk-help-body" }, [note, helpLine("Keys.", "Reserved shortcuts are in Screen controls. Full keyboard capture depends on the browser. Ctrl/⌘ + wheel is not sent to the remote screen.")])
        ]);
        tools.open = false;
        help.open = false;
        tools.addEventListener("toggle", function () { if (!P.shell && tools.open) help.open = false; });
        help.addEventListener("toggle", function () { if (!P.shell && help.open) tools.open = false; });
        var bar = P.el("div", { class: "desk-bar" }, inputBtns.concat([icon(mobileText.keyboard, "⌨", "Keyboard"), icon(full, "⛶", "Full screen"), tools, help]));
        section.addEventListener("keydown", function (ev) { if (!P.shell && ev.key === "Escape") { tools.open = false; help.open = false; } });
        section.classList.add("desk");
        if (!resizeEnabled) section.setAttribute("data-fit-window", "true");
        watchLoad();
        [bar, inputStatus, warn, frame].forEach(function (n) { if (n) section.appendChild(n); });
      },
      onShow: function () { syncVisible(); }
    };
  }

  P.tabs.chat = makeDesktop(
    "chat",
    "/desktop/chat/",
    "Everyone who opens Chat sees and moves the same screen. To sign in to claude.ai on the server, enter the link or code here; signing in on another device does not authorize this screen."
  );
  P.tabs.browser = makeDesktop("browser", "/desktop/admin/", "Agent desktop: the agent's Chrome and terminal. The screen is shared across devices.");
})();
