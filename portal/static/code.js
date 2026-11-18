(function () {
  var P = window.Portal;
  var el = P.el;
  var VENDOR = "/static/vendor/xterm/";
  var state = { name: null, term: null, fit: null, ws: null, timer: null, closing: false, ctrl: false, libs: null };
  var ui = {};
  var sessions = {};
  var modelChoices = [];
  var AUTO_HELP = "Auto chooses the model (Haiku, Sonnet or Opus) and the effort (Low, Medium, High or Xhigh) for you. " +
    "When you send the first message of a new chat, it is passed to Google's Decision model, which rates how demanding the request is. " +
    "The chat starts with that choice and keeps it: later messages never change it, but you can still pick another model or effort by hand. " +
    "If Google does not answer in time, the chat starts with Sonnet at Medium effort. The first message is sent to Google for this.";
  var preferredModel = "claude-sonnet-5-5";
  var modelReads = Object.create(null);
  var modelEpoch = 0;
  var observedModel = null;
  var chatIntent = {};
  var enc = new TextEncoder();

  function setStatus(t) { ui.status.textContent = t; }

  function modelName(id) {
    if (!id || id === "default") return "Model not saved";
    if (id === "opusplan") return "Opus Plan Mode";
    if (id === "auto") return "Auto";
    id = { opus: "claude-opus-5-5", sonnet: "claude-sonnet-5-5", haiku: "claude-haiku-5-5", fable: "claude-fable-5-2" }[id] || id;
    var m = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2})(?!\d))?/.exec(id);
    return m ? m[1][0].toUpperCase() + m[1].slice(1) + " " + m[2] + (m[3] ? "." + m[3] : "") : id;
  }

  function capWord(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

  function selectedText(doc) {
    if (doc === ui.termBox.ownerDocument) return state.term ? state.term.getSelection() : "";
    var selection = doc.getSelection();
    return selection ? selection.toString() : "";
  }

  function copySelection(doc) {
    doc = doc || ui.termBox.ownerDocument;
    var text = selectedText(doc);
    if (!text) { ui.copyStatus.textContent = "nothing to copy"; return; }
    function fallback() {
      var doc = ui.termBox.ownerDocument;
      var active = doc.activeElement;
      var selection = doc.getSelection();
      var ranges = [];
      for (var i = 0; selection && i < selection.rangeCount; i++) ranges.push(selection.getRangeAt(i).cloneRange());
      var area = doc.createElement("textarea");
      area.value = text;
      area.setAttribute("aria-hidden", "true");
      area.className = "console-copy-fallback";
      (doc.fullscreenElement || doc.body).appendChild(area);
      try {
        area.select();
        if (!doc.execCommand("copy")) throw new Error("copy denied");
      } finally {
        area.remove();
        if (active && active.isConnected) active.focus({ preventScroll: true });
        if (selection) { selection.removeAllRanges(); ranges.forEach(function (range) { selection.addRange(range); }); }
      }
    }
    var clipboard = doc.defaultView.navigator.clipboard;
    Promise.resolve().then(function () {
      if (!clipboard || !clipboard.writeText) return fallback();
      return clipboard.writeText(text).catch(fallback);
    }).then(function () { ui.copyStatus.textContent = "Copied"; }, function () {
      ui.copyStatus.textContent = "Copy failed. Allow clipboard or copy manually.";
    });
  }

  function copyKey(ev) {
    if (!ev.ctrlKey || ev.altKey || ev.metaKey || ev.isComposing) return;
    if (!(ev.shiftKey && (ev.code === "KeyC" || ev.key.toLowerCase() === "c")) && !(!ev.shiftKey && ev.key === "Insert")) return;
    if (ui.termBox.hidden || ui.section.hidden) return;
    var doc = ev.target.ownerDocument;
    if (!selectedText(doc)) { ui.copyStatus.textContent = "nothing to copy"; return; }
    ev.preventDefault();
    ev.stopPropagation();
    ev.stopImmediatePropagation();
    copySelection(doc);
  }

  function installCopyCapture() {
    var frames = new WeakSet();
    var docs = new WeakSet();
    var handled = new WeakSet();
    function capture(ev) {
      if (handled.has(ev)) return;
      handled.add(ev);
      copyKey(ev);
    }
    function bind(doc, frame) {
      if (!doc || docs.has(doc)) return;
      docs.add(doc);
      doc.addEventListener("keydown", function (ev) {
        if (frame ? ui.termBox.contains(frame) && frame.contentDocument === doc : ui.termBox.contains(ev.target)) capture(ev);
      }, true);
    }
    function scan() {
      ui.termBox.querySelectorAll("iframe").forEach(function (frame) {
        function loaded() { try { bind(frame.contentDocument, frame); } catch (e) {} }
        if (!frames.has(frame)) { frames.add(frame); frame.addEventListener("load", loaded); }
        loaded();
      });
    }
    ui.termBox.addEventListener("keydown", capture, true);
    bind(ui.termBox.ownerDocument, null);
    new MutationObserver(scan).observe(ui.termBox, { childList: true, subtree: true });
    scan();
  }

  function loadDialogue() {
    if (state.dialogueLib) return state.dialogueLib;
    P.loadCss("/static/code-dialogue.css");
    state.dialogueLib = Promise.all([P.touchOnly && P.touchOnly() ? Promise.resolve() : P.loadPromptEditor(), P.loadScript("/static/code-voice.js").catch(function () { return null; }), P.loadScript("/static/code-dialogue.js")]).then(makeDialogue, function () {
      ui.warn.textContent = "Chat view failed. Use the console.";
      ui.warn.hidden = false;
    });
    return state.dialogueLib;
  }

  function loadMarkdown() {
    if (state.markdownLib) return state.markdownLib;
    state.markdownLib = Promise.all([
      P.loadScript("/static/vendor/marked/marked.umd.js"),
      P.loadScript("/static/vendor/highlight/highlight.min.js")
    ]).then(function () {
      P.loadCss("/static/vendor/katex/katex.min.css");
      return P.loadScript("/static/vendor/katex/katex.min.js").catch(function () {});
    }).then(function () { return P.loadScript("/static/code-widgets.js").catch(function () {}); })
      .then(function () { return P.loadScript("/static/code-markdown.js"); })
      .then(function () { if (ui.dialogue && ui.dialogue.renderMarkdown) ui.dialogue.renderMarkdown(); })
      .catch(function () { ui.warn.textContent = "Markdown unavailable; plain text shown."; ui.warn.hidden = false; });
    return state.markdownLib;
  }

  function loadLibs() {
    if (state.libs) return state.libs;
    P.loadCss(VENDOR + "xterm.css");
    var term = P.loadScript(VENDOR + "xterm.js").then(function () {
      return Promise.all([P.loadScript(VENDOR + "addon-fit.js"), P.loadScript(VENDOR + "addon-webgl.js")]);
    });
    state.libs = Promise.all([term, loadDialogue()]);
    return state.libs;
  }

  P.codeChat = function () { return state.chat; };

  function makeDialogue() {
    if (ui.dialogue || !P.CodeDialogue) return;
    ui.dialogue = P.CodeDialogue(ui.dialogueBox, {
      validate: pasteLimit,
      send: function (text, slow) { return pasteEnter(text, slow); },
      session: function () { return state.name && (sessions[state.name] || {}).claude ? state.name : null; },
      enter: function () { return sendBytes(enc.encode("\r")); },
      stop: function () {
        var name = state.name;
        if (!name) return false;
        P.api("POST", "/api/sessions/" + encodeURIComponent(name) + "/interrupt", {}).catch(function (e) { setStatus(e.message); });
        return true;
      },
      forceClose: function () {
        var name = state.name;
        if (!name) return Promise.resolve(false);
        return P.ask("Force close this chat's session? Claude stops now; the chat history stays and you can reopen it.", { ok: "Force close" }).then(function (yes) {
          if (!yes) return false;
          return P.api("DELETE", "/api/sessions/" + encodeURIComponent(name), { confirm: name }).then(function () {
            if (state.name === name) { clearView(); setStatus("session closed"); }
            refresh();
            return true;
          });
        }).catch(function (e) { setStatus(e.message); return false; });
      },
      cwd: function () { return ui.path && ui.path.textContent.charAt(0) === "/" ? ui.path.textContent : ""; },
      resume: function (text) { return state.chat ? resumeChat(state.chat, text).catch(function (e) { setStatus(e.message); throw e; }) : Promise.resolve(false); }
    });
    if (state.view !== "console") dialogueMode(true);
    setTimeout(loadMarkdown, 0);
  }

  function createTerm() {
    var t = new window.Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, "DejaVu Sans Mono", Menlo, Consolas, monospace',
      fontSize: window.matchMedia("(max-width:700px)").matches ? 12 : 14,
      scrollback: 5000,
      allowProposedApi: false,
      linkHandler: { activate: function () {}, hover: function () {}, leave: function () {}, allowNonHttpProtocols: false },
      theme: { background: "#0b0d11", foreground: "#e6e8ee" }
    });
    var fit = new window.FitAddon.FitAddon();
    t.loadAddon(fit);
    // Loaded before open, the WebGL renderer replaces the DOM one, whose inline <style> elements the page CSP (style-src 'self') blocks.
    // Without WebGL2 (Firefox with the GPU off, some phones) the addon would throw inside open(), so it is not loaded at all.
    var webgl = false;
    try { webgl = !!document.createElement("canvas").getContext("webgl2"); } catch (e) { webgl = false; }
    try {
      if (!webgl) throw new Error("no WebGL2");
      var gl = new window.WebglAddon.WebglAddon();
      gl.onContextLoss(function () { gl.dispose(); });
      t.loadAddon(gl);
    } catch (e) {
      ui.warn.textContent = "WebGL unavailable: console colors may be missing.";
      ui.warn.hidden = false;
    }
    t.open(ui.termBox);
    var touchDevice = P.input.mode() === "scroll";
    var allowTouchFocus = false;
    ui.termBox.addEventListener("focus", function (ev) {
      if (touchDevice && ev.target === t.textarea && !allowTouchFocus) t.blur();
    }, true);
    function tapInput() {
      allowTouchFocus = true;
      try { t.focus(); } finally { allowTouchFocus = false; }
    }
    var scrollRemainder = 0;
    P.input.touch(ui.termBox, {
      mode: function () { return ui.inputMode; },
      target: function (n) { return n.closest && n.closest(".xterm"); },
      tap: tapInput,
      drag: function () { if (touchDevice) t.blur(); },
      wheel: function (target, x, y, dy) {
        if (t.modes.mouseTrackingMode === "none") {
          scrollRemainder += dy;
          var lines = Math.trunc(scrollRemainder / 20);
          if (lines) { t.scrollLines(lines); scrollRemainder -= lines * 20; }
        } else {
          P.input.wheel(target.querySelector(".xterm-screen"), x, y, dy);
        }
      }
    });
    ui.termBox.addEventListener("paste", function (ev) {
      if (!ev.clipboardData) return;
      ev.preventDefault();
      ev.stopImmediatePropagation();
      sendPaste(ev.clipboardData.getData("text/plain"));
    }, true);
    t.onData(function (d) { sendInput(d); });
    t.onBinary(function (d) {
      var bytes = new Uint8Array(d.length);
      for (var i = 0; i < d.length; i++) bytes[i] = d.charCodeAt(i) & 255;
      sendBytes(bytes);
    });
    t.onResize(function () { sendResize(); });
    state.term = t;
    state.focusInput = tapInput;
    state.fit = fit;
    new ResizeObserver(function () { doFit(); }).observe(ui.termBox);
  }

  function doFit() {
    if (!state.fit || ui.section.hidden || !ui.termBox.clientWidth) return;
    try { state.fit.fit(); } catch (e) {}
  }

  function sendBytes(bytes) {
    if (ui.modelBusy || !state.ws || state.ws.readyState !== 1) return false;
    state.ws.send(bytes);
    return true;
  }

  // A chat run as a process (agent mode) has no terminal: its messages go as one frame, and there is no CLI pane or dictation.
  function isAgent() { return !!(state.name && (sessions[state.name] || {}).agent); }

  function sendMessage(text) {
    if (ui.modelBusy || !state.ws || state.ws.readyState !== 1 || !text) return false;
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(text) || /[\ud800-\udfff]/u.test(text)) {
      setStatus("message has unsupported control characters");
      return false;
    }
    var error = pasteLimit(text);
    if (error) { setStatus(error); return false; }
    state.ws.send(JSON.stringify({ type: "message", text: text }));
    return true;
  }

  function pasteEnter(text, slow) {
    if (isAgent()) return sendMessage(text);
    if (!sendPaste(text)) return false;
    var socket = state.ws;
    var go = function () { if (state.ws === socket) sendBytes(enc.encode("\r")); };
    if (slow) setTimeout(go, P.ENTER_DELAY_MS == null ? 1200 : P.ENTER_DELAY_MS); else go();
    return true;
  }

  function pasteLimit(text) {
    var bytes = enc.encode(JSON.stringify({ type: "paste", text: text })).length;
    var normalized = text.replace(/\r\n?/g, "\n").replace(/\[(\s*)(portal\s+)?sender\s*:/gi, "[quoted sender:");
    if (bytes > 16384 || enc.encode(normalized).length > 16372) return "The message is over the 16 KiB limit (16384 UTF-8 bytes including service data). The text is kept; shorten it or attach it as a text file.";
    return "";
  }

  function sendPaste(text) {
    if (ui.modelBusy || !state.ws || state.ws.readyState !== 1 || !text) return false;
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(text) || /[\ud800-\udfff]/u.test(text)) {
      setStatus("paste has unsupported control characters");
      return false;
    }
    var frame = JSON.stringify({ type: "paste", text: text });
    var error = pasteLimit(text);
    if (error) { setStatus(error); return false; }
    state.ws.send(frame);
    return true;
  }

  function sendInput(d) {
    if (state.ctrl && d.length === 1) {
      var c = d.toUpperCase().charCodeAt(0);
      if (c >= 64 && c <= 95) d = String.fromCharCode(c - 64);
      setCtrl(false);
    }
    sendBytes(enc.encode(d));
  }

  function sendResize() {
    var t = state.term;
    if (!t || !state.ws || state.ws.readyState !== 1) return;
    state.ws.send(JSON.stringify({ type: "resize", cols: Math.max(10, Math.min(500, t.cols)), rows: Math.max(10, Math.min(500, t.rows)) }));
  }

  function setCtrl(on) {
    state.ctrl = on;
    ui.ctrlBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function disconnect() {
    state.closing = true;
    clearTimeout(state.timer);
    if (state.ws) { try { state.ws.close(); } catch (e) {} }
    state.ws = null;
  }
  window.addEventListener("portal-identity-reset", function () {
    disconnect();
    state.name = null;
    state.chat = null;
    sessions = {};
    if (state.term) state.term.dispose();
    state.term = null;
  });

  function dialogueMode(on) {
    ui.dialogueMode = !!on && !!ui.dialogue;
    ui.dialogueBox.hidden = !ui.dialogueMode;
    ui.termBox.hidden = ui.dialogueMode;
    ui.keybar.hidden = ui.dialogueMode;
    if (P.shell && P.shell.codeChat) P.shell.codeChat(ui.dialogueMode);
    var clip = ui.dialogueBox.querySelector(".dialogue-attach");
    if (ui.modelbar && clip && ui.dialogueMode) clip.after(ui.modelbar);
    else if (ui.modelbar && ui.modelHome && !ui.dialogueMode) ui.modelHome.after(ui.modelbar);
    doFit();
  }

  // One open chat has two panes, Code Web and its terminal. Both use the same connection, so switching never reconnects.
  function chatOpen() {
    return !!state.chat || !!(state.name && (sessions[state.name] || {}).claude);
  }

  function connect(name) {
    if (P.identityLocked) return;
    var same = state.name === name;
    disconnect();
    state.name = name;
    state.closing = false;
    var session = sessions[name] || {};
    var chat = state.chat && chatById(state.chat);
    var isClaude = !!session.claude || !!state.chat;
    ui.title.textContent = chat ? chat.title : session.title || name;
    ui.path.textContent = session.path || "unknown folder";
    if (!same) {
      if (state.term) state.term.reset();
      modelEpoch++; observedModel = null; ui.liveCatalog = null;
      cliModels();
      ui.controlResult.textContent = "";
    }
    ui.dialogueBox.classList.toggle("agent-chat", !!session.agent);
    if (session.agent && ui.cliPane) { ui.cliPane = false; }
    updateModel();
    pollModel();
    if (ui.dialogue) {
      ui.dialogue.select(name, isClaude, state.chat);
      if (!same) dialogueMode(state.view !== "console" && isClaude && !ui.cliPane);
    }
    markActive();
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var ws = new WebSocket(proto + "//" + location.host + "/api/term/" + encodeURIComponent(name) + "?preview=1" + (P.account ? "&account=" + encodeURIComponent(P.account) : ""));
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    setStatus("connecting...");
    ws.onopen = function () {
      if (state.ws !== ws) return;
      state.retry = 0;
      setStatus("connected");
      if (ui.dialogue) ui.dialogue.connection(true);
      state.term.reset();
      doFit();
      sendResize();
      if (!ui.dialogueMode && P.input.mode() !== "scroll") state.term.focus();
    };
    ws.onmessage = function (ev) {
      if (state.ws !== ws) return;
      if (typeof ev.data !== "string") state.term.write(new Uint8Array(ev.data));
      else if (ui.dialogue) {
        try {
          var envelope = JSON.parse(ev.data);
          if (envelope.v === 1 && envelope.type === "code_events" && Array.isArray(envelope.events)) envelope.events.forEach(ui.dialogue.event);
        } catch (e) { setStatus("bad event; open the console"); }
      }
    };
    ws.onclose = function () {
      if (state.ws !== ws) return;
      if (ui.dialogue) ui.dialogue.connection(false);
      // A hidden page (a phone app in the background) waits: wake() connects the moment it is shown again.
      if (document.hidden) { setStatus("disconnected"); return; }
      // The first retry is almost immediate (a network blink); then longer after each failure in a row (3, 6, 12 ... up to 30 seconds).
      var delay = !state.retry ? 400 : Math.min(30000, 3000 * Math.pow(2, state.retry - 1));
      state.retry = (state.retry || 0) + 1;
      setStatus(delay < 1000 ? "reconnecting…" : "disconnected, retrying in " + Math.round(delay / 1000) + " s");
      state.timer = setTimeout(function () {
        if (state.closing || state.name !== name) return;
        refresh(true).then(function (names) {
          if (state.closing || state.name !== name || state.ws !== ws) return;
          if (names.indexOf(name) >= 0) connect(name);
          else if (state.chat && chatById(state.chat)) { state.name = null; selectChat(state.chat); }
          else { setStatus("session closed"); state.name = null; ui.title.textContent = ""; if (ui.dialogue) ui.dialogue.select(null, false); }
        }).catch(function () { if (!state.closing && state.name === name && state.ws === ws) connect(name); });
      }, delay);
    };
  }

  // A phone freezes a page in the background and its socket dies quietly. When the page is shown again, connect at once instead of
  // waiting for the retry timer, and do not trust a socket that sat through a long pause.
  var hiddenAt = 0;
  function wake() {
    if (P.identityLocked || document.hidden || !state.name) return;
    var ws = state.ws, long = hiddenAt && Date.now() - hiddenAt > 3000;
    hiddenAt = 0;
    if (ws && ws.readyState === 1 && !long) { if (ui.dialogue) ui.dialogue.connection(true); return; }
    clearTimeout(state.timer);
    state.retry = 0;
    connect(state.name);
  }
  document.addEventListener("visibilitychange", function () { if (document.hidden) hiddenAt = Date.now(); else wake(); });
  document.addEventListener("resume", wake);
  ["online", "focus", "pageshow", "portal-resume"].forEach(function (name) { window.addEventListener(name, wake); });

  function markActive() {
    if (state.chat) { try { sessionStorage.setItem("portal-open-chat", state.chat); } catch (e) {} }
    Array.prototype.forEach.call(ui.list.children, function (li) {
      var on = li.dataset.id ? li.dataset.id === state.chat : (!!li.dataset.name && li.dataset.name === state.name && !state.chat);
      li.classList.toggle("active", on);
      var opener = li.querySelector && li.querySelector(".sess-open");
      if (opener) { if (on) opener.setAttribute("aria-current", "true"); else opener.removeAttribute("aria-current"); }
    });
    if (ui.empty) {
      var empty = state.view !== "console" && !state.chat && !state.name && !chats.length;
      ui.empty.hidden = !empty;
      if (empty) ui.dialogueBox.hidden = ui.termBox.hidden = ui.keybar.hidden = true;
      else if (ui.termBox.hidden && ui.dialogueBox.hidden) dialogueMode(ui.dialogueMode);
    }
  }

  var chats = [];
  var consoles = [];
  var chatsError = null;

  function chatRow(c) {
    var awake = c.awake === undefined ? c.active : c.awake;
    var running = awake && (c.claude === undefined || c.claude || c.starting);
    var meta = el("span", { class: "sess-meta" }, [document.createTextNode(P.fmtTime(c.modified))]);
    var starting = running && c.starting && !c.claude, ended = awake && !running;
    var state = starting ? "Starting" : running ? "Running" : awake ? "Process ended" : "Asleep";
    if (awake) meta.appendChild(el("span", { class: "chat-live", text: state }));
    var label = el("button", { class: "sess-open chat-open", type: "button", title: c.title }, [el("span", { class: "sess-name", text: c.title }), meta]);
    label.addEventListener("click", function () { selectChat(c.id); });
    var pen = el("button", { class: "chat-act chat-rename", type: "button", title: "Rename", "aria-label": "Rename " + c.title });
    pen.appendChild(svgIcon(ICON_PEN));
    var bin = el("button", { class: "chat-act chat-delete", type: "button", title: "Delete", "aria-label": "Delete " + c.title });
    bin.appendChild(svgIcon(ICON_BIN));
    // The status dot is the sleep switch: green (running) puts the chat to sleep, grey (asleep) wakes it.
    var dot = el("button", { class: "chat-dot chat-wake", type: "button", title: state + (awake ? " · tap to put to sleep" : " · tap to wake"), "aria-label": (awake ? "Put to sleep: " : "Wake: ") + c.title, "aria-pressed": awake ? "true" : "false" });
    dot.disabled = !!(ui.chatChanging && ui.chatChanging[c.id]);
    dot.addEventListener("click", function (ev) { ev.stopPropagation(); toggleChat(c, awake); });
    var li = el("li", { class: "chat-item" + (running ? " live" : " sleeping") + (starting ? " starting" : "") + (ended ? " ended" : ""), title: state + (ended ? " · not using a slot" : ""), "data-id": c.id, "data-name": c.name || "" }, [dot, label, el("span", { class: "chat-acts" }, [pen, bin])]);
    pen.addEventListener("click", function (ev) { ev.stopPropagation(); editChat(c, li, label); });
    bin.addEventListener("click", function (ev) { ev.stopPropagation(); deleteChat(c); });
    return li;
  }

  function consoleRow(s) {
    var label = el("button", { class: "sess-open", type: "button" }, [
      el("span", { class: "sess-name", text: s.title || s.name })
    ]);
    label.addEventListener("click", function () { state.chat = null; open(s.name); });
    var rename = el("button", { class: "sess-rename", type: "button", title: "Rename", text: "✎", "aria-label": "Rename " + s.name });
    rename.addEventListener("click", function () { renameSession(s); });
    var del = el("button", { class: "sess-del", type: "button", title: "Close console", text: "×" });
    del.addEventListener("click", function () { removeSession(s.name); });
    return el("li", { "data-name": s.name }, [label, rename, del]);
  }

  var ICON_PEN = ["M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z", "M13.5 8.5l3 3"];
  var ICON_BIN = ["M4 7h16", "M9 7V4.5h6V7", "M6.5 7l1 12.5h9l1-12.5", "M10 11v5M14 11v5"];
  // A separate label element lets long model names end in an ellipsis before the chevron.
  function toggleLabel(text) {
    ui.modelToggle.replaceChildren(el("span", { class: "model-label", text: text }));
    ui.modelToggle.title = text;
  }

  function svgIcon(paths) {
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    var attrs = { viewBox: "0 0 24 24", width: "15", height: "15", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };
    Object.keys(attrs).forEach(function (k) { svg.setAttribute(k, attrs[k]); });
    paths.forEach(function (d) { var p = document.createElementNS(NS, "path"); p.setAttribute("d", d); svg.appendChild(p); });
    return svg;
  }

  function editChat(c, li, label) {
    if (ui.editing) return;
    var input = el("input", { class: "chat-edit", type: "text", "aria-label": "Chat name" });
    input.value = c.title;
    ui.editing = input;
    li.classList.add("editing");
    li.insertBefore(input, label);
    label.hidden = true;
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      ui.editing = null;
      var title = input.value.trim();
      input.remove(); label.hidden = false; li.classList.remove("editing");
      if (!save || !title || title === c.title) return;
      label.querySelector(".sess-name").textContent = title;
      P.api("PATCH", "/api/chats/" + c.id, { title: title }).then(function () { return refresh(); }).catch(function (e) { setStatus(e.message); renderList(); });
    }
    input.addEventListener("click", function (ev) { ev.stopPropagation(); });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    input.focus(); input.select();
  }

  function renderList() {
    ui.list.textContent = "";
    ui.list.setAttribute("aria-label", state.view === "console" ? "Consoles" : "Chats");
    if (state.view === "console") {
      consoles.forEach(function (s) { ui.list.appendChild(consoleRow(s)); });
      if (!consoles.length) {
        var make = el("button", { type: "button", class: "btn primary", text: "New console" });
        make.addEventListener("click", newConsole);
        ui.list.appendChild(el("li", { class: "chat-none console-none" }, [make]));
      }
      markActive();
      return;
    }
    var q = search.q.trim().toLowerCase();
    var leaving = ui.deleting || {};
    var shown = (!q ? chats : chats.filter(function (c) {
      return search.found ? !!search.found[c.id] : c.title.toLowerCase().indexOf(q) >= 0;
    })).filter(function (c) { return !leaving[c.id]; });
    var known = ui.knownRows, rows = {};
    shown.forEach(function (c) {
      var li = chatRow(c);
      var hits = q && search.found && search.found[c.id];
      if (hits && hits.length) li.querySelector(".chat-open").appendChild(snippet(hits[0], q));
      if (known && !known[c.id]) li.classList.add("motion-row");
      rows[c.id] = true;
      ui.list.appendChild(li);
    });
    if (!q) ui.knownRows = rows;
    if (chatsError) ui.list.appendChild(el("li", { class: "muted small chat-none", text: "Chats unavailable: " + chatsError }));
    else if (!chats.length) ui.list.appendChild(el("li", { class: "muted small chat-none", text: "No chats yet" }));
    else if (!shown.length) ui.list.appendChild(el("li", { class: "muted small chat-none chat-nothing" }, [
      el("strong", { text: "Nothing found" }),
      el("span", { text: search.inside ? "Try another word." : "Turn on “Search inside” to search the conversation text." })
    ]));
    markActive();
  }

  var search = { q: "", inside: false, found: null, timer: 0, seq: 0 };

  function snippet(text, q) {
    var low = text.toLowerCase(), at = low.indexOf(q);
    var box = el("span", { class: "chat-snippet" });
    if (at < 0) { box.textContent = text; return box; }
    box.appendChild(document.createTextNode(text.slice(0, at)));
    box.appendChild(el("mark", { text: text.slice(at, at + q.length) }));
    box.appendChild(document.createTextNode(text.slice(at + q.length)));
    return box;
  }

  function runSearch() {
    clearTimeout(search.timer);
    search.found = null;
    var q = search.q.trim();
    renderList();
    if (!q) return;
    var seq = ++search.seq;
    search.timer = setTimeout(function () {
      P.api("GET", "/api/chats/search?q=" + encodeURIComponent(q) + "&inside=" + (search.inside ? 1 : 0)).then(function (r) {
        if (seq !== search.seq) return;
        var found = {};
        (r.chats || []).forEach(function (c) { found[c.id] = c.matches || []; });
        search.found = found;
        if (!ui.editing) renderList();
      }).catch(function (e) { if (seq === search.seq) setStatus(e.message); });
    }, 250);
  }

  function searchBox() {
    var input = el("input", { class: "chat-search-input", type: "search", placeholder: "Search chats", "aria-label": "Search chats", maxlength: "120" });
    var clear = el("button", { class: "chat-search-clear", type: "button", title: "Clear", "aria-label": "Clear search", text: "×", hidden: "hidden" });
    var box = el("input", { type: "checkbox", class: "chat-search-inside" });
    function set(v) { input.value = v; search.q = v; clear.hidden = !v; runSearch(); }
    input.addEventListener("input", function () { set(input.value); });
    input.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && input.value) { ev.preventDefault(); ev.stopPropagation(); set(""); } });
    clear.addEventListener("click", function () { set(""); input.focus(); });
    box.addEventListener("change", function () { search.inside = box.checked; runSearch(); });
    return el("div", { class: "chat-search" }, [
      el("div", { class: "chat-search-field" }, [input, clear]),
      el("label", { class: "chat-search-toggle small" }, [box, document.createTextNode("Search inside")])
    ]);
  }

  function chatById(id) {
    for (var i = 0; i < chats.length; i++) if (chats[i].id === id) return chats[i];
    return null;
  }

  // Each account may keep r.limit chats running; sleeping chats do not count.
  function drawSlots(r) {
    var limit = r.limit || 3, cap = r.capacity || { mine: 0, chats: 0, consoles: 0 }, mine = cap.mine || 0;
    var dots = el("span", { class: "chat-slot-dots", "aria-hidden": "true" });
    for (var i = 0; i < limit; i++) dots.appendChild(el("span", { class: "chat-slot" + (i < mine ? " mine" : "") }));
    ui.slots.replaceChildren(dots, el("span", { text: "Running " + mine + " of " + limit }));
    ui.slots.title = "Each account can keep " + limit + " chats running (" + cap.chats + " chats, " + cap.consoles + " consoles with Claude now). Sleeping chats don't count; a chat idle for an hour falls asleep on its own.";
    ui.slots.classList.toggle("full", mine >= limit);
  }

  var refreshSeq = 0;
  function refresh(strict) {
    var seq = ++refreshSeq;
    return Promise.all([P.api("GET", "/api/sessions"), P.api("GET", "/api/chats").catch(function (e) { return { error: e.message }; })]).then(function (res) {
      var r = res[0];
      // An older answer that arrives after a newer one must not bring back a chat that was just deleted or renamed.
      if (seq !== refreshSeq) return r.sessions.map(function (s) { return s.name; });
      if (ui.slots) drawSlots(r);
      if (ui.capacity) {
        var cap = r.capacity;
        ui.capacity.textContent = (cap ? cap.mine : r.claude) + " / " + r.limit + " running";
        ui.capacity.title = "Running Claude chats for this account";
      }
      sessions = {};
      var names = [];
      r.sessions.forEach(function (s) { sessions[s.name] = s; names.push(s.name); });
      chatsError = res[1].error || null;
      chats = res[1].chats || [];
      consoles = res[1].consoles || [];
      if (!ui.editing) renderList();
      if (ui.dialogue && state.name) ui.dialogue.select(state.name, !!state.chat || !!(sessions[state.name] && sessions[state.name].claude), state.chat);
      var chat = state.chat && chatById(state.chat);
      if (chat) ui.title.textContent = chat.title;
      else if (state.name && sessions[state.name]) ui.title.textContent = sessions[state.name].title || state.name;
      if (state.name && sessions[state.name]) ui.path.textContent = sessions[state.name].path;
      updateModel();
      return names;
    }).catch(function (e) {
      ui.list.textContent = "";
      ui.list.appendChild(el("li", { class: "muted", text: "Error: " + e.message }));
      if (strict) throw e;
      return [];
    });
  }

  function busyRetry(call) {
    return call(false).catch(function (e) {
      var d = e.data && e.data.detail;
      if (e.status !== 409 || !d || d.error !== "busy") throw e;
      return P.ask("Claude is still answering in “" + d.title + "”. Stop it and switch?", { ok: "Switch" }).then(function (yes) {
        if (!yes) return null;
        return call(true);
      });
    });
  }

  function waitReady(name) {
    var started = Date.now();
    if (ui.dialogue) ui.dialogue.starting(true);
    return new Promise(function (resolve) {
      function tick() {
        if (state.name !== name) return resolve(false);
        P.api("GET", "/api/sessions/" + encodeURIComponent(name) + "/ready").then(function (r) {
          if (r.reason) { setStatus(r.reason); return resolve(null); }
          if (r.ready && state.ws && state.ws.readyState === 1) return resolve(true);
          if (Date.now() - started > 45000) return resolve(false);
          setTimeout(tick, 700);
        }).catch(function () { if (Date.now() - started > 45000) resolve(false); else setTimeout(tick, 1000); });
      }
      tick();
    }).then(function (ok) {
      if (ui.dialogue && state.name === name) ui.dialogue.starting(false);
      if (ok === false && state.name === name) setStatus("Claude is slow to start. Check the console.");
      return !!ok;
    });
  }

  var creating = false;
  function newChat(automatic) {
    if (state.view === "console") { newConsole(); return; }
    if (creating) return;
    creating = true;
    ui.add.disabled = true;
    var epoch = modelEpoch;
    setStatus("starting a new chat…");
    return busyRetry(function (force) { return P.api("POST", "/api/chats", { draft: true, force: force }); }).then(function (r) {
      if (!r) return;
      if (automatic === true && epoch !== modelEpoch) return refresh();
      state.chat = r.id;
      return refresh().then(function () { return r.name ? open(r.name, r.id).then(function () { return waitReady(r.name); }) : selectChat(r.id); });
    }).catch(function (e) {
      if (automatic === true && epoch !== modelEpoch) return;
      setStatus(e.message); if (automatic !== true) window.alert(e.message);
    }).then(function () { creating = false; ui.add.disabled = false; });
  }

  // On load an untouched chat is reused instead of piling up new empty ones. A tab that the phone unloaded in the background comes back to
  // the chat it showed: the browser restores the tab's sessionStorage with it, while a fresh launch starts without one.
  function startChat() {
    if (ui.bootChatRequested || state.want) return;
    ui.bootChatRequested = true;
    var last = null;
    try { last = sessionStorage.getItem("portal-open-chat"); } catch (e) {}
    if (last && chatById(last)) return selectChat(last);
    var blank = chats.filter(function (c) { return c.empty; }).sort(function (a, b) { return b.modified - a.modified; })[0];
    if (blank) return selectChat(blank.id);
    return newChat(true);
  }

  function newConsole() {
    ui.add.disabled = true;
    P.api("POST", "/api/consoles", {}).then(function (r) {
      state.chat = null;
      return refresh().then(function () { return open(r.name); });
    }).catch(function (e) { setStatus(e.message); window.alert(e.message); }).then(function () { ui.add.disabled = false; });
  }

  function firstConsole() {
    return consoles.length ? consoles[0].name : null;
  }

  function selectChat(id) {
    var c = chatById(id);
    if (!c) return;
    if (c.name && c.awake !== false && c.claude) { open(c.name, c.id); return; }
    state.want = null;
    ui.section.classList.remove("side-open");
    return loadDialogue().then(function () {
      disconnect();
      state.name = null;
      state.chat = c.id;
      ui.title.textContent = c.title;
      ui.path.textContent = c.cwd || "";
      setStatus("not running");
      modelEpoch++; observedModel = null; ui.pendingModel = null; ui.controlResult.textContent = ""; intentModels(); updateModel();
      if (ui.dialogue) { ui.dialogue.preview(c.id); dialogueMode(true); }
      markActive();
    }).catch(function (e) { setStatus(e.message); });
  }

  function resumeChat(id, text) {
    if (ui.dialogue) ui.dialogue.starting(true);
    var intent = chatIntent[id];
    return busyRetry(function (force) {
      var body = force ? { force: true } : {};
      if (intent && intent !== "default") body.model = intent;
      // Auto picks the model from the first message; the "Attached files" line stays in, because it tells the choice which files come with it.
      if (text) body.text = text.slice(0, 4000);
      return P.api("POST", "/api/chats/" + id + "/open", body);
    }).then(function (r) {
      if (r && r.started) delete chatIntent[id];
      if (!r || state.chat !== id) return false;
      return refresh().then(function () {
        if (state.chat !== id) return false;
        return open(r.name, id);
      }).then(function () {
        if (state.chat !== id || state.name !== r.name) return false;
        return waitReady(r.name);
      }).then(function (ok) {
        if (!ok || state.name !== r.name) return false;
        return text ? pasteEnter(text, /^Attached files/.test(text)) : true;
      });
    }).finally(function () { if (ui.dialogue && state.chat === id) ui.dialogue.starting(false); });
  }

  function toggleChat(c, awake) {
    ui.chatChanging = ui.chatChanging || {};
    if (ui.chatChanging[c.id]) return;
    ui.chatChanging[c.id] = true; renderList();
    var action = awake ? P.api("POST", "/api/chats/" + c.id + "/sleep", {}).then(function () {
      if (state.chat === c.id) {
        disconnect(); state.name = null; state.want = null;
        if (ui.dialogue) { ui.dialogue.connection(false); ui.dialogue.preview(c.id); }
      }
      return refresh();
    }) : state.chat === c.id ? resumeChat(c.id) : P.api("POST", "/api/chats/" + c.id + "/open", {}).then(refresh);
    action.catch(function (e) { setStatus(e.message); }).finally(function () {
      delete ui.chatChanging[c.id]; renderList();
    });
  }

  // The row leaves at once and the server finishes in the background (it stops tmux and rescans history, which can take seconds);
  // on failure the row comes back.
  function deleteChat(c) {
    ui.deleting = ui.deleting || {};
    if (ui.deleting[c.id]) return;
    ui.deleting[c.id] = true;
    var li = ui.list.querySelector('li[data-id="' + c.id + '"]');
    var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (li && li.animate && !still && li.getClientRects().length) {
      var height = li.getBoundingClientRect().height;
      li.classList.add("chat-leaving"); li.style.overflow = "hidden";
      li.animate([{ opacity: 1, transform: "none", height: height + "px" }, { opacity: 0, transform: "translateX(-12px)", height: height + "px", offset: .55 }, { opacity: 0, transform: "translateX(-12px)", height: "0px" }],
        { duration: 260, easing: "cubic-bezier(.23,1,.32,1)", fill: "forwards" }).onfinish = function () { if (ui.deleting[c.id] && !ui.editing) renderList(); };
    } else if (!ui.editing) renderList();
    if (state.chat === c.id) clearView();
    P.api("DELETE", "/api/chats/" + c.id, { confirm: c.id }).then(function () {
      chats = chats.filter(function (x) { return x.id !== c.id; });
      delete ui.deleting[c.id];
      return refresh().then(pickDefault);
    }).catch(function (e) {
      delete ui.deleting[c.id];
      if (ui.knownRows) ui.knownRows[c.id] = false;
      setStatus(e.message);
      if (!ui.editing) renderList();
    });
  }

  function clearView() {
    disconnect(); state.name = null; state.chat = null;
    ui.title.textContent = ""; ui.path.textContent = ""; updateModel(); setStatus("");
    if (ui.dialogue) { ui.dialogue.connection(false); ui.dialogue.select(null, false); }
  }

  function pickDefault() {
    if (state.chat || state.name) return;
    var live = chats.filter(function (c) { return c.active; })[0];
    if (live) open(live.name, live.id);
    else if (chats.length) selectChat(chats[0].id);
    else markActive();
  }

  function removeSession(name) {
    P.ask("Close session " + name + "? Everything running in it will stop.", { ok: "Close" }).then(function (yes) {
      if (!yes) return null;
      return name === "main" ? P.ask("This is the main session. Close anyway?", { ok: "Close" }) : true;
    }).then(function (yes) {
      if (!yes) return;
      return P.api("DELETE", "/api/sessions/" + encodeURIComponent(name), { confirm: name }).then(function () {
        if (state.name === name) { clearView(); setStatus("session closed"); }
        return refresh();
      });
    }).catch(function (e) { setStatus(e.message); });
  }

  function open(name, chatId) {
    state.want = name;
    state.chat = chatId || null;
    ui.section.classList.remove("side-open");
    return loadLibs().then(function () {
      if (state.want !== name) return;
      if (!state.term) createTerm();
      connect(name);
    }).catch(function (e) { setStatus(e.message); });
  }

  function renameSession(session) {
    P.ask("Chat name", { value: session.title || session.name, ok: "Save" }).then(function (title) {
      if (title === null || !title.trim()) return;
      return P.api("PATCH", "/api/sessions/" + encodeURIComponent(session.name), { title: title }).then(function () { return refresh(); });
    }).catch(function (e) { setStatus(e.message); });
  }

  function fillModels(select) {
    var previous = select.value;
    select.textContent = "";
    modelChoices.forEach(function (m) {
      if (m.id !== "default") select.appendChild(el("option", { value: m.id, text: (m.label || modelName(m.id)) + (m.note ? " · " + m.note : "") }));
    });
    select.value = modelChoices.some(function (m) { return m.id === previous; }) ? previous : preferredModel;
  }

  function pollModel() {
    var name = state.name;
    if (!name || !ui.section || ui.section.hidden || document.hidden || !sessions[name]) return;
    var epoch = modelEpoch;
    var identity = sessions[name].path + ":" + sessions[name].created;
    var entry = modelReads[name];
    if (entry && entry.identity === identity && (entry.busy || performance.now() - entry.at < 5000)) {
      if (entry.data) { observedModel = { name: name, data: entry.data }; updateModel(); }
      return;
    }
    if (Object.keys(modelReads).length >= 64) delete modelReads[Object.keys(modelReads)[0]];
    entry = { identity: identity, at: performance.now(), busy: true, data: null };
    modelReads[name] = entry;
    function finish(data) {
      entry.busy = false;
      entry.at = performance.now();
      entry.data = data;
      if (state.name !== name || modelEpoch !== epoch || ui.section.hidden || document.hidden) return;
      observedModel = { name: name, data: data };
      updateModel();
    }
    P.api("GET", "/api/sessions/" + encodeURIComponent(name) + "/model")
      .then(finish).catch(function () { finish({ actual: null, requested: null }); });
  }

  function idleChat() { return !!state.chat && !state.name; }

  function intentModels() {
    if (!ui.modelSelect || !idleChat()) return;
    fillModels(ui.modelSelect);
    var want = chatIntent[state.chat] || (chatById(state.chat) || {}).model || preferredModel;
    if (want !== "default" && !Array.from(ui.modelSelect.options).some(function (option) { return option.value === want; })) {
      ui.modelSelect.appendChild(el("option", { value: want, text: modelName(want), disabled: "disabled" }));
    }
    ui.modelSelect.value = want === "default" ? preferredModel : want;
  }

  function savedEffort(chat, model) {
    if (!chat || chat.model !== model || (chat.effort_model && chat.effort_model !== model)) return null;
    return /^(low|medium|high|xhigh|max)$/.test(chat.effort) ? chat.effort : null;
  }

  function updateIdle() {
    var chat = chatById(state.chat) || {};
    var want = chatIntent[state.chat] || chat.model;
    var named = want && want !== "default";
    var effort = savedEffort(chat, want);
    toggleLabel((named ? modelName(want) : "Model not saved") + (effort ? " · " + capWord(effort) : ""));
    ui.modelStatus.textContent = want === "auto" ? "Model: Auto (automatic choice). The first message picks the model and effort, and the chat keeps them."
      : named ? "Saved model: " + modelName(want) + ". The chat is not running now." : "This older chat has no saved model. Pick one before sending.";
    if (named && want !== "auto" && chat.auto) ui.modelStatus.textContent += chat.auto === "decision" ? " Chosen by Auto." : " Auto used the default.";
    if (effort) ui.modelStatus.textContent += " Saved effort: " + capWord(effort) + ".";
    ui.modelToggle.title = ui.modelStatus.textContent;
    ui.modelToggle.disabled = false;
    ui.modelSelect.disabled = !modelChoices.length;
    ui.catalogBtn.disabled = ui.compactBtn.disabled = true;
    // Effort can be set before the chat starts, for every model; Auto chooses it itself.
    var auto = want === "auto";
    ui.effortSelect.disabled = auto || !modelChoices.length;
    ui.effortSelect.hidden = auto;
    selectValue(ui.effortSelect, effort, "Effort: default");
    ui.effortNote.hidden = !auto;
    ui.effortNote.textContent = "Effort is chosen automatically together with the model.";
    if (ui.dialogue) ui.dialogue.hold(false);
  }

  function updateModel() {
    if (!ui.modelStatus) return;
    if (idleChat()) return updateIdle();
    var session = sessions[state.name] || {};
    var key = state.name + ":" + (session.model || "default") + ":" + session.effort + ":" + session.model_state;
    var changed = ui.modelKey !== key;
    ui.modelKey = key;
    var data = observedModel && observedModel.name === state.name ? observedModel.data : {};
    var confirmed = session.model_state === "cli_confirmed";
    var pend = ui.pendingModel && ui.pendingModel.name === state.name ? ui.pendingModel : null;
    if (pend && confirmed && !ui.modelBusy && (session.model !== pend.previousModel || session.effort !== pend.previousEffort)) {
      ui.pendingModel = null; pend = null; ui.liveCatalog = null; cliModels();
      ui.controlResult.textContent = "Confirmed from Claude";
    }
    var live = ui.liveCatalog && ui.liveCatalog.name === state.name;
    if (confirmed && live && (session.model !== (ui.liveCatalog.data.model || ui.liveCatalog.data.checked || ui.liveCatalog.data.selected) || session.effort !== ui.liveCatalog.data.effort)) {
      ui.liveCatalog = null; live = false; cliModels();
    }
    if (changed && confirmed && !live && !pend && !ui.modelBusy) cliModels();
    var active = live ? ui.liveCatalog.data.model || ui.liveCatalog.data.checked : confirmed && !pend ? session.model : null;
    var text = active ? "Model: " + active + " (confirmed)" : "Model: " + (pend ? "unknown" : data.actual || "unknown");
    if (active && data.actual) text += " · latest reply: " + data.actual;
    if (data.requested && data.requested !== "default" && data.requested !== data.actual) text += " · requested: " + data.requested;
    if (!active && session.model && session.model !== "default" && session.model !== data.requested) text += " · session: " + session.model;
    if (session.effort && !pend) text += " · effort: " + session.effort;
    var autoChat = chatById(state.chat) || {};
    if (autoChat.auto) text += autoChat.auto === "decision" ? " · chosen by Auto" : " · Auto used the default";
    ui.modelStatus.textContent = text;
    ui.modelStatus.title = active ? "Active model confirmed by the Claude menu; latest reply may use a previous model." : "Model of the latest reply, not confirmation of the next reply. Load models to check Claude.";
    var current = confirmed ? session.model : live ? (ui.liveCatalog.data.model || ui.liveCatalog.data.checked || ui.liveCatalog.data.selected) : session.model;
    var remembered = (chatById(state.chat) || {}).model;
    var shown = current && current !== "default" ? modelName(current) : data.actual ? modelName(data.actual) : remembered ? modelName(remembered) : "Model not saved";
    var known = savedEffort(chatById(state.chat), current && current !== "default" ? current : remembered);
    var effort = live ? ui.liveCatalog.data.effort : !pend ? session.effort || (!confirmed ? known : null) : null;
    if (!live && !pend && !confirmed && !session.effort && known) {
      text += " · saved effort: " + capWord(known); ui.modelStatus.textContent = text;
    }
    if (!live && !pend) selectValue(ui.effortSelect, effort, "Effort not saved");
    var label = shown + (effort ? " · " + capWord(effort) : "");
    if (pend && pend.unknown) label = "Model: unknown";
    if (ui.modelBusy) label = "Switching…";
    toggleLabel(label);
    ui.modelToggle.title = text;
    ui.modelToggle.disabled = !session.claude;
    ui.catalogBtn.disabled = !session.claude || !!ui.modelBusy;
    ui.compactBtn.disabled = ui.catalogBtn.disabled;
    ui.modelSelect.disabled = ui.effortSelect.disabled = ui.catalogBtn.disabled;
    ui.effortSelect.hidden = (!!live || confirmed) && !effort && !session.agent;
    ui.effortNote.hidden = !ui.effortSelect.hidden;
    ui.effortNote.textContent = "This Claude menu does not offer effort for the selected model.";
    if (ui.dialogue) ui.dialogue.hold(!!ui.modelBusy);
  }

  function switchModel() {
    if (!idleChat()) return requestControls(true);
    chatIntent[state.chat] = ui.modelSelect.value;
    var chosen = modelName(ui.modelSelect.value);
    ui.controlResult.textContent = "Will start with " + chosen;
    var id = state.chat;
    var body = { model: ui.modelSelect.value };
    if (/^(low|medium|high|xhigh)$/.test(ui.effortSelect.value)) body.effort = ui.effortSelect.value;
    P.api("PATCH", "/api/chats/" + id + "/model", body).then(function (data) {
      var chat = chatById(id); if (chat) { chat.model = data.model; chat.effort = data.effort || null; chat.effort_model = data.model; }
      if (state.chat === id) updateModel();
    }).catch(function (e) { delete chatIntent[id]; if (state.chat === id) { ui.controlResult.textContent = e.message; intentModels(); updateModel(); } });
    updateModel();
  }

  function requestControls(change) {
    var name = state.name;
    if (!name || ui.modelBusy || !(sessions[name] || {}).claude) return;
    if (change) return sendControls(name, true);
    P.ask("Load models from Claude? Claude must be idle with an empty prompt. This session only.", { ok: "Load" }).then(function (yes) {
      if (yes && state.name === name && !ui.modelBusy) sendControls(name, change);
    });
  }

  function compactChat() {
    var name = state.name;
    if (!name || ui.modelBusy || !(sessions[name] || {}).claude) return;
    P.ask("Compact this chat? Claude summarises the conversation and the old messages are hidden, so new instructions get more attention. Claude must be idle with an empty prompt. Optionally say what the summary should keep:", { ok: "Compact", value: "", max: 200 }).then(function (answer) {
      if (answer === false || answer === null || answer === undefined) return;
      var instructions = typeof answer === "string" ? answer : "";
      ui.controlResult.textContent = "Compacting…";
      P.api("POST", "/api/sessions/" + encodeURIComponent(name) + "/compact", { confirm: name, confirm_idle: true, instructions: instructions }).then(function () {
        ui.controlResult.textContent = "Compaction started. The chat will show a note when it is done.";
      }).catch(function (e) { ui.controlResult.textContent = "Not compacted: " + switchReason(e.message); });
    });
  }

  function cliModels() {
    ui.modelSelect.textContent = "";
    var ids = (modelChoices.length ? modelChoices.map(function (m) { return m.id; }).filter(function (id) { return id !== "default"; }) : ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]);
    ids.forEach(function (id) {
      ui.modelSelect.appendChild(el("option", { value: id, text: modelName(id) }));
    });
    var session = sessions[state.name] || {};
    ui.modelSelect.value = ids.indexOf(session.model) >= 0 ? session.model : preferredModel;
    selectValue(ui.effortSelect, session.effort || savedEffort(chatById(state.chat), session.model), "Effort not saved");
  }

  function sendControls(name, change) {
    var body = { confirm: name, confirm_idle: true };
    if (change) {
      body.model = ui.modelSelect.value;
      if (/^(low|medium|high|xhigh|max)$/.test(ui.effortSelect.value)) body.effort = ui.effortSelect.value;
    }
    var epoch = modelEpoch;
    ui.modelBusy = true;
    ui.controlResult.textContent = "Checking…";
    updateModel();
    P.api("POST", "/api/sessions/" + encodeURIComponent(name) + "/controls", body).then(function (data) {
      if (state.name !== name || epoch !== modelEpoch) return;
      showControls(name, data);
      ui.controlResult.textContent = change ? "Confirmed · " + data.cli_model + (data.effort ? " · " + data.effort : " · effort not available") : "Models loaded · effort: " + (data.effort || "not available");
    }).catch(function (e) {
      if (state.name === name && epoch === modelEpoch) {
        var reason = switchReason(e.message);
        ui.controlResult.textContent = "Not switched: " + reason;
        if (!change) return;
        if (e.data && e.data.model_unchanged) {
          var saved = ui.liveCatalog && ui.liveCatalog.name === name ? ui.liveCatalog.data : null;
          if (saved) { showControls(name, saved); return; }
          if (ui.pendingModel && ui.pendingModel.name === name && ui.pendingModel.unknown) {
            selectValue(ui.modelSelect, "", "Model not confirmed");
            selectValue(ui.effortSelect, "", "Effort not confirmed");
            return;
          }
          cliModels(); ui.pendingModel = null; return;
        }
        ui.pendingModel = { name: name, reason: reason, unknown: true, previousModel: (sessions[name] || {}).model, previousEffort: (sessions[name] || {}).effort };
        ui.liveCatalog = null;
        selectValue(ui.modelSelect, "", "Model not confirmed");
        selectValue(ui.effortSelect, "", "Effort not confirmed");
        return P.api("POST", "/api/sessions/" + encodeURIComponent(name) + "/controls", { confirm: name, confirm_idle: true }).then(function (data) {
          if (state.name === name && epoch === modelEpoch) showControls(name, data);
        }).catch(function () {});
      }
    }).then(function () { ui.modelBusy = false; updateModel(); });
  }

  function showControls(name, data) {
    ui.liveCatalog = { name: name, data: data };
    ui.modelSelect.textContent = "";
    data.models.filter(function (m) { return m.id !== "default"; }).forEach(function (m) {
      var option = el("option", { value: m.id, text: m.label });
      option.disabled = !m.enabled;
      ui.modelSelect.appendChild(option);
    });
    if (modelChoices.some(function (m) { return m.id === "opusplan"; }) && !data.models.some(function (m) { return m.id === "opusplan"; })) {
      ui.modelSelect.appendChild(el("option", { value: "opusplan", text: modelName("opusplan") }));
    }
    var model = data.model || data.checked || data.selected;
    selectValue(ui.modelSelect, model, "Model not confirmed");
    selectValue(ui.effortSelect, data.effort, "Effort not available");
    sessions[name].model = model;
    if (data.model_state) sessions[name].model_state = data.model_state;
    sessions[name].effort = data.effort;
    sessions[name].modelLabel = model === "default" ? "" : data.cli_model;
    ui.pendingModel = null;
  }

  function selectValue(select, value, placeholder) {
    Array.from(select.options).filter(function (o) { return !o.value; }).forEach(function (o) { o.remove(); });
    if (!value || !Array.from(select.options).some(function (o) { return o.value === value; })) {
      var option = el("option", { value: "", text: placeholder });
      option.disabled = true;
      select.insertBefore(option, select.firstChild);
      value = "";
    }
    select.value = value;
  }

  function switchReason(message) {
    var m = String(message || "");
    if (/idle|empty prompt/i.test(m)) return "Finish the reply, then switch";
    if (/cut off|enlarge/i.test(m)) return "Enlarge the console, then retry";
    if (/single-pane/i.test(m)) return "Needs a single Claude pane";
    return m.replace(/;.*$|\.(\s.*)?$/, "") || "Claude did not confirm";
  }

  function newSessionDialog() {
    var pathRules = "Path must be relative to home, e.g. work/project; no . or ..";
    var nameRules = "Name: a-z, 0-9, - or _, up to 24 chars.";
    var nameIn = el("input", { type: "text", maxlength: "24", placeholder: "a-z, 0-9, - or _", autocomplete: "off", autocapitalize: "none", "aria-describedby": "session-name-rules" });
    var claudeIn = el("input", { type: "checkbox", checked: "checked" });
    var modelIn = el("select", { class: "new-model", "aria-label": "Model" });
    fillModels(modelIn);
    var continueIn = el("input", { type: "checkbox" });
    claudeIn.addEventListener("change", function () { modelIn.disabled = continueIn.disabled = !claudeIn.checked; });
    var err = el("p", { class: "err", role: "alert" });
    var pathIn = el("input", { id: "session-path", type: "text", maxlength: "4096", placeholder: "work/project or pc/vps-pc", autocomplete: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Path" });
    pathIn.value = "work";
    var titleIn = el("input", { type: "text", maxlength: "80", placeholder: "Optional", "aria-label": "Chat name" });
    var question = el("p");
    var yes = el("button", { type: "button", class: "btn primary", text: "Yes" });
    var no = el("button", { type: "button", class: "btn", text: "No" });
    var confirmation = el("div", { hidden: "hidden" }, [question, el("div", { class: "row" }, [no, yes])]);
    var pending = null;
    var closed = false;

    var ok = el("button", { type: "button", class: "btn primary", text: "Create" });
    var cancel = el("button", { type: "button", class: "btn", text: "Cancel" });
    var overlay = el("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "New session" }, [
      el("div", { class: "modal-box" }, [
        el("h2", { text: "New session" }),
        el("div", { class: "field-label" }, [el("label", { text: "Name" }), P.tip(nameRules, "session-name-rules")]), nameIn,
        el("div", { class: "field-label" }, [el("label", { for: "session-path", text: "Path" }), P.tip("Folder under ~, e.g. work/project. New folders only inside work. pc: = laptop folder (needs Tailscale).")]), pathIn,
        el("label", { text: "Chat name" }), titleIn,
        el("label", { class: "check" }, [claudeIn, el("span", { text: "Start Claude" })]),
        el("div", { class: "field-label" }, [el("label", { text: "Model" }), P.tip("Model choice may change usage.")]), modelIn,
        el("label", { class: "check" }, [continueIn, el("span", { text: "Continue last chat" })]),
        err, confirmation,
        el("div", { class: "row end" }, [cancel, ok])
      ])
    ]);
    cancel.addEventListener("click", function () { closed = true; overlay.remove(); });
    function resetConfirmation() { if (!pending && ok.disabled) return; pending = null; confirmation.hidden = true; ok.disabled = false; }
    no.addEventListener("click", resetConfirmation);
    pathIn.addEventListener("input", resetConfirmation);
    function finish(r, title) {
      var rename = title ? P.api("PATCH", "/api/sessions/" + encodeURIComponent(r.name), { title: title }) : Promise.resolve();
      return rename.catch(function (e) { window.alert("Created, name not saved: " + e.message); }).then(function () {
        if (closed) return;
        closed = true; overlay.remove(); return refresh().then(function () { open(r.name); });
      });
    }
    function submit(body, title) {
      return P.api("POST", "/api/sessions", body).then(function (r) { return finish(r, title); }).catch(function (e) {
        if (closed) return;
        var data = e.data;
        if (e.status === 409 && data && data.error === "missing_path") {
          if (!data.can_create) { err.textContent = data.message; ok.disabled = false; return; }
          pending = { body: body, title: title };
          question.textContent = "Folder “" + data.path.replace(/^~\//, "") + "” not found. Create?";
          confirmation.hidden = false; yes.disabled = false; yes.focus(); return;
        }
        err.textContent = e.message; ok.disabled = false;
      });
    }
    yes.addEventListener("click", function () {
      if (!pending || yes.disabled || closed) return;
      var request = pending; pending = null; yes.disabled = true; confirmation.hidden = true;
      P.api("POST", "/api/dirs", { path: request.body.path }).then(function () {
        if (!closed) return submit(request.body, request.title);
      }).catch(function (e) { if (!closed) { err.textContent = e.message; ok.disabled = false; } });
    });
    function create() {
      err.textContent = "";
      if (!/^[a-z0-9][a-z0-9_-]{0,23}$/.test(nameIn.value) || /[\r\n]/.test(nameIn.value)) {
        err.textContent = nameRules;
        return;
      }
      if (ok.disabled) return;
      ok.disabled = true;
      var value = pathIn.value;
      if (!value || /[\0\r\n]/.test(value) || value[0] === "/" || (value[0] === "~" && value !== "~" && value.indexOf("~/") !== 0)) { err.textContent = pathRules; ok.disabled = false; return; }
      var parts = value.replace(/^~\//, "").split("/").filter(Boolean);
      if (parts.some(function (p) { return p === "." || p === ".."; }) || !parts.length) { err.textContent = pathRules; ok.disabled = false; return; }
      var normalized = value === "~" ? "~" : "~/" + parts.join("/");
      var title = titleIn.value.trim();
      if (/[\x00-\x1f\x7f]/.test(titleIn.value)) { err.textContent = "Name: no control characters"; ok.disabled = false; return; }
      var body = { name: nameIn.value, path: normalized, start_claude: claudeIn.checked };
      if (claudeIn.checked) { if (modelIn.value !== "default") body.model = modelIn.value; body.continue_chat = continueIn.checked; }
      submit(body, title);
    }
    ok.addEventListener("click", create);
    pathIn.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); create(); } });
    overlay.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); cancel.click(); } });
    ui.section.appendChild(overlay);

    nameIn.focus();
    setTimeout(function () { if (!closed && !overlay.contains(document.activeElement)) nameIn.focus(); }, 0);
  }

  function keyBar() {
    function mk(label, seq, cls) {
      var b = el("button", { type: "button", class: "key" + (cls ? " " + cls : ""), text: label });
      b.addEventListener("pointerdown", function (ev) { ev.preventDefault(); });
      b.addEventListener("click", function () {
        sendBytes(enc.encode(typeof seq === "function" ? seq() : seq));
        if (state.term) state.term.focus();
      });
      return b;
    }
    function arrow(ch) {
      return function () {
        return (state.term && state.term.modes.applicationCursorKeysMode ? "\x1bO" : "\x1b[") + ch;
      };
    }
    ui.ctrlBtn = el("button", { type: "button", class: "key", text: "Ctrl", "aria-pressed": "false" });
    ui.ctrlBtn.addEventListener("pointerdown", function (ev) { ev.preventDefault(); });
    ui.ctrlBtn.addEventListener("click", function () { setCtrl(!state.ctrl); if (state.term) state.term.focus(); });
    var paste = el("button", { type: "button", class: "key", text: "Paste" });
    paste.addEventListener("click", function () {
      if (!navigator.clipboard || !navigator.clipboard.readText) return;
      var socket = state.ws;
      navigator.clipboard.readText().then(function (t) { if (state.ws === socket) sendPaste(t); }).catch(function () {});
    });
    var line = el("input", { type: "text", class: "send-line", placeholder: "send text", autocomplete: "off", autocapitalize: "none", enterkeyhint: "send" });
    function sendLine() {
      if (!line.value || !sendPaste(line.value)) return;
      sendBytes(enc.encode("\r"));
      line.value = "";
    }
    line.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); sendLine(); } });
    var sendBtn = el("button", { type: "button", class: "key", text: "Send" });
    sendBtn.addEventListener("click", sendLine);
    return el("div", { class: "keybar" }, [
      el("div", { class: "keys" }, [
        mk("Esc", "\x1b"), mk("Tab", "\t"), mk("Shift+Tab", "\x1b[Z"), ui.ctrlBtn, mk("Ctrl+C", "\x03"),
        mk("←", arrow("D")), mk("↑", arrow("A")), mk("↓", arrow("B")), mk("→", arrow("C")),
        mk("Enter", "\r"), paste
      ]),
      el("div", { class: "sendrow" }, [line, sendBtn])
    ]);
  }

  window.addEventListener("portal-open-session", function (ev) {
    state.want = ev.detail;
    if (!ui.section) return;
    refresh().then(function () { var c = chats.filter(function (x) { return x.name === state.want; })[0]; open(state.want, c ? c.id : null); });
  });

  P.tabs.code = {
    focusInput: function () {
      if (ui.dialogueMode) {
        var field = ui.dialogueBox.querySelector(".dialogue-input");
        if (field) field.focus();
      } else if (state.focusInput) state.focusInput();
    },
    setView: function (view) {
      state.view = view;
      if (P.shell && P.shell.codeChat) P.shell.codeChat(view !== "console");
      if (ui.add) ui.add.textContent = view === "console" ? "New console" : "New chat";
      if (ui.list) renderList();
      if (ui.searchBox) ui.searchBox.hidden = view === "console";
      var claudeShown = state.chat || (state.name && (sessions[state.name] || {}).claude);
      if (ui.list && view === "console" && (!state.name || claudeShown)) {
        disconnect(); state.name = null; state.chat = null;
        if (state.term) state.term.reset();
        if (firstConsole()) open(firstConsole());
        else refresh().then(function () { if (state.name || state.view !== "console") return; if (firstConsole()) open(firstConsole()); else newConsole(); });
      }
      else if (ui.list && view !== "console" && !state.chat && state.name) { disconnect(); state.name = null; pickDefault(); }
      if (ui.dialogue) dialogueMode(view !== "console" && (!!state.chat || !!(sessions[state.name] || {}).claude));
      },
    init: function (section) {
      P.loadCss("/static/code-management.css");
      ui.section = section;
      section.classList.add("code");
      ui.list = el("ul", { class: "sess-list chat-list", "aria-label": "Chats" });
      ui.add = el("button", { type: "button", class: "btn primary chat-new", text: state.view === "console" ? "New console" : "New chat" });
      ui.add.addEventListener("click", newChat);
      var side = el("aside", { class: "code-side" }, [
        el("div", { class: "row" }, [ui.add]), ui.searchBox = searchBox(), ui.slots = el("p", { class: "chat-slots", "aria-live": "polite" }), ui.list
      ]);
      ui.title = el("strong", { class: "term-title" });
      ui.path = el("span", { class: "dialogue-path muted small", title: "Start folder" });
      ui.status = el("span", { class: "muted small" });
      ui.warn = el("div", { class: "warn", hidden: "hidden" });
      ui.copyStatus = el("span", { class: "console-copy-status muted small", role: "status", "aria-live": "polite" });
      ui.termBox = el("div", { class: "term-box" });
      if (state.view !== "console") ui.termBox.hidden = true;
      installCopyCapture();
      P.input.guard(ui.termBox);
      ui.inputMode = P.input.mode();
      var scroll = el("button", { type: "button", class: "btn", text: "Scroll", "aria-pressed": ui.inputMode === "scroll" ? "true" : "false", title: "Touch scrolls history" });
      scroll.addEventListener("click", function () {
        ui.inputMode = ui.inputMode === "scroll" ? "mouse" : "scroll";
        scroll.setAttribute("aria-pressed", ui.inputMode === "scroll" ? "true" : "false");
      });
      ui.dialogueBox = el("div", { class: "code-dialogue", hidden: "hidden" });
      ui.keybar = keyBar();
      var historyButton = el("button", { type: "button", class: "btn", text: "Chat history" });
      historyButton.addEventListener("click", function () {
        section.classList.add("side-open"); var first = ui.list.querySelector(".sess-open"); if (first) first.focus({ preventScroll: true });
      });
      ui.copyBtn = el("button", { type: "button", class: "btn console-copy", text: "Copy", title: "Ctrl+Shift+C" });
      ui.copyBtn.addEventListener("pointerdown", function (ev) { ev.preventDefault(); });
      ui.copyBtn.addEventListener("click", function () { copySelection(); });
      var full = el("button", { type: "button", class: "btn console-fullscreen", text: "Fullscreen" });
      P.input.fullscreen(full, section, function () { if (state.focusInput) state.focusInput(); }, ui.copyStatus, ["KeyC"]);
      var head = ui.modelHome = el("div", { class: "term-head" }, [historyButton, scroll, ui.copyBtn, full, ui.title, ui.path, ui.status, ui.copyStatus]);
      ui.modelSelect = el("select", { class: "active-model", "aria-label": "Model" });
      ui.catalogBtn = el("button", { type: "button", class: "btn", text: "Load models" });
      ui.catalogBtn.addEventListener("click", function () { requestControls(false); });
      ui.compactBtn = el("button", { type: "button", class: "btn", text: "Compact chat", title: "Summarise the conversation so Claude pays more attention to new instructions" });
      ui.compactBtn.addEventListener("click", compactChat);
      ui.effortSelect = el("select", { class: "active-effort", "aria-label": "Effort" }, [
        el("option", { value: "low", text: "Low" }),
        el("option", { value: "medium", text: "Medium" }),
        el("option", { value: "high", text: "High" }),
        el("option", { value: "xhigh", text: "Xhigh" })
      ]);
      ui.effortNote = el("span", { class: "effort-note muted small" });
      ui.autoHelp = el("div", { class: "model-help muted small", hidden: "hidden" }, [el("span", { text: "Auto: automatic choice " }), P.tip(AUTO_HELP)]);
      cliModels();
      ui.controlResult = el("span", { class: "control-result muted small", role: "status" });
      ui.modelSelect.addEventListener("change", switchModel);
      ui.effortSelect.addEventListener("change", switchModel);
      ui.modelStatus = el("span", { class: "model-status muted small" });
      ui.modelToggle = el("button", { type: "button", class: "model-toggle", "aria-haspopup": "true", "aria-expanded": "false" });
      ui.modelPanel = el("div", { class: "model-panel viewport-menu", role: "group", "aria-label": "Model and effort", hidden: "hidden" }, [ui.catalogBtn, ui.compactBtn, ui.modelSelect, ui.effortSelect, ui.effortNote, ui.autoHelp, ui.modelStatus, ui.controlResult]);
      var menuFrame = null;
      function placeModelMenu() {
        if (ui.modelPanel.hidden) return;
        if (!ui.modelToggle.getClientRects().length) { modelMenu(false); return; }
        var view = window.visualViewport;
        var width = view ? view.width : document.documentElement.clientWidth;
        var height = view ? view.height : window.innerHeight;
        var left = (view ? view.offsetLeft : 0) + 12;
        var top = (view ? view.offsetTop : 0) + 12;
        var available = Math.max(1, width - 24);
        ui.modelPanel.style.width = Math.min(280, available) + "px";
        ui.modelPanel.style.maxHeight = Math.max(1, height - 24) + "px";
        var anchor = ui.modelToggle.getBoundingClientRect();
        var panelHeight = ui.modelPanel.offsetHeight, panelWidth = ui.modelPanel.offsetWidth;
        var x = Math.max(left, Math.min(anchor.right - panelWidth, left + available - panelWidth));
        var y = anchor.top - panelHeight - 8;
        if (y < top && anchor.bottom + 8 + panelHeight <= top + height - 24) y = anchor.bottom + 8;
        y = Math.max(top, Math.min(y, top + Math.max(0, height - 24 - panelHeight)));
        ui.modelPanel.style.left = x + "px";
        ui.modelPanel.style.top = y + "px";
      }
      function queueModelMenu() {
        if (ui.modelPanel.hidden || menuFrame !== null) return;
        menuFrame = requestAnimationFrame(function () { menuFrame = null; placeModelMenu(); });
      }
      function modelMenu(open) {
        ui.modelPanel.hidden = !open;
        ui.modelToggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) placeModelMenu();
      }
      ui.modelToggle.addEventListener("click", function () { modelMenu(ui.modelPanel.hidden); });
      document.addEventListener("click", function (ev) { if (!ui.modelPanel.hidden && !ev.target.closest(".model-bar, .modal.ask")) modelMenu(false); });
      document.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && !ui.modelPanel.hidden) { modelMenu(false); ui.modelToggle.focus(); } });
      window.addEventListener("resize", queueModelMenu);
      document.addEventListener("scroll", queueModelMenu, true);
      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", queueModelMenu);
        window.visualViewport.addEventListener("scroll", queueModelMenu);
      }
      if (typeof ResizeObserver === "function") {
        var menuObserver = new ResizeObserver(queueModelMenu);
        menuObserver.observe(ui.modelPanel); menuObserver.observe(section);
      }
      updateModel();
      var modelbar = ui.modelbar = el("div", { class: "model-bar" }, [ui.modelToggle, ui.modelPanel]);
      ui.capacity = el("span", { class: "code-capacity", "aria-live": "polite" });
      var note = el("p", { class: "code-primary-note muted small" }, [el("span", { text: "Code Web · " }), ui.capacity]);
      ui.emptyNew = el("button", { type: "button", class: "btn primary", text: "New chat" });
      ui.emptyNew.addEventListener("click", newChat);
      ui.empty = el("div", { class: "chat-empty", hidden: "hidden" }, [el("h2", { text: "Start a new chat" }), ui.emptyNew]);
      var main = el("div", { class: "code-main" }, [note, head, modelbar, ui.warn, ui.empty, ui.dialogueBox, ui.termBox, ui.keybar]);
      section.appendChild(side);
      section.appendChild(main);
      loadLibs();
      P.api("GET", "/api/models").then(function (r) {
        modelChoices = Array.isArray(r.models) ? r.models.filter(function (m) { return m.id !== "default"; }) : [];
        ui.autoHelp.hidden = !modelChoices.some(function (m) { return m.id === "auto"; });
        preferredModel = r.preferred || preferredModel;
        ui.modelKey = null;
        if (!idleChat() && !ui.liveCatalog) cliModels();
        intentModels();
        updateModel();
      }).catch(function () { ui.modelStatus.textContent = "Models unavailable; use /model"; });
      refresh().then(function () {
        if (state.want) return;
        if (state.view === "console") { if (firstConsole()) open(firstConsole()); }
        else startChat();
      });
      setInterval(function () { if (!section.hidden && !document.hidden) refresh(); }, 10000);
      setInterval(pollModel, 5000);
      document.addEventListener("visibilitychange", function () { if (!document.hidden) pollModel(); });
    },
    onShow: function () {
      pollModel();
      setTimeout(doFit, 50);
    }
  };
})();
