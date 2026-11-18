(function () {
  var P = window.Portal;
  var el = P.el;
  var STATES = {
    thinking: "Thinking…",
    running: "Running",
    idle: "Ready",
    stopped: "Stopped",
    unknown: "Waiting"
  };
  var STILL_MS = 30000;
  var GREETINGS = ["Let’s get started", "Where should we start?", "What needs doing?", "Tell me about the task", "What shall we work on?", "Write the first message", "What’s on your mind today?", "How can I help?", "Got a task? Let’s go", "Let’s figure it out"];
  var greetingIndex = Math.floor(Math.random() * GREETINGS.length);
  function toolLabel(name) {
    var n = String(name || "").toLowerCase();
    if (/bash|shell|command|exec/.test(n)) return "Running a command…";
    if (/search|grep|glob|find/.test(n)) return "Searching…";
    if (/fetch|web|browse/.test(n)) return "Reading a web page…";
    if (/edit|write|notebook/.test(n)) return "Editing a file…";
    if (/read|ls|view/.test(n)) return "Reading a file…";
    if (/task|agent/.test(n)) return "Delegating a task…";
    return "Running a tool…";
  }
  function delayFor(preview, state, hidden) {
    if (hidden) return preview ? 15000 : 10000;
    if (preview) return 2500;
    return state === "thinking" || state === "running" ? 1000 : 2000;
  }
  P.CodeDialogue = function (host, actions) {
    var touch = window.matchMedia("(pointer:coarse)").matches && !window.matchMedia("(pointer:fine)").matches;
    host.classList.add(touch ? "dlg-touch" : "dlg-desk");
    host.classList.add("dialogue-history");
    var base = actions && actions.api ? actions.api : "";
    var brand = actions && actions.brand ? actions.brand : "Claude";
    function sessionUrl(id, tail) { return (base ? base + "/sessions/" : "/api/sessions/") + encodeURIComponent(id) + tail; }
    function chatUrl(id, tail) { return (base ? base + "/chats/" : "/api/chats/") + encodeURIComponent(id) + tail; }
    var connected = false, allowed = false, selected = null, following = true;
    var bulk = false, liveBatch = false, catching = false, fresh = false, gliding = 0;
    var scrollDownIntent = 0, touchY = null, lastScrollTop = 0;
    var still = window.matchMedia("(prefers-reduced-motion: reduce)");
    var busy = null, again = false;
    var cursor = null, state = "unknown", tools = new Map(), seen = new Set(), lastTool = "";
    // History opens at its newest page; `head` is the byte offset where the shown part starts (0 once everything is loaded).
    var head = null, olderBusy = false, olderTimer = null, sink = null, pendingResults = new Map();
    var pollCall = null, pollStarted = 0, pollSlow = 0, loadTimer = null;
    var loadNote = el("div", { class: "dialogue-older dialogue-load-note", role: "status" });
    var timer = null, epoch = 0, bound = null, total = 0, held = false;
    var drafts = new Map();
    var previewId = null, starting = false;
    var loaded = false;
    var liveTurn = null, liveAliases = new Set(), liveParts = new Map(), liveClosed = false, liveKnown = [];
    var queuedCli = null, cliFrame = null, expectedClient = null, handoffs = new Set();
    var cliPainters = new Set(), retired = new Set(), glideFrame = null, glideAt = 0, glideExpect = null;
    var greeting = el("p", { class: "dialogue-greeting", hidden: "hidden" });
    function chooseGreeting() { greeting.textContent = GREETINGS[greetingIndex++ % GREETINGS.length]; }
    // Another chat's history fades in once its first answer arrives instead of popping into the emptied feed.
    function arrive() {
      feed.classList.remove('feed-arrive'); void feed.offsetWidth; feed.classList.add('feed-arrive');
    }
    function showGreeting() {
      var show = loaded && !starting && !total && !!(selected || previewId);
      greeting.hidden = !show;
      if (show && greeting.parentNode !== feed) feed.prepend(greeting);
    }
    var waitSince = 0, watch = null, tick = null, since = Date.now();
    var historyViewport = el("div", { class: "dialogue-history-viewport" });
    var feed = el("div", { class: "dialogue-feed", role: "log", "aria-label": "Chat", tabindex: "0" });
    var turnRail = el("nav", { class: "dialogue-turns", "aria-label": "Request navigation", hidden: "hidden" });
    var requestMarks = [], activeMark = null, hoveredMark = null, railFrame = null;
    function railLayout() {
      scheduleRail();
    }
    function revealMark(button) {
      var rect = button.getBoundingClientRect(), parent = turnRail.getBoundingClientRect();
      if (rect.top < parent.top) turnRail.scrollTop -= parent.top - rect.top;
      else if (rect.bottom > parent.bottom) turnRail.scrollTop += rect.bottom - parent.bottom;
    }
    function highlightMark(mark) {
      if (!mark || activeMark === mark) return;
      if (activeMark) { activeMark.button.setAttribute("aria-current", "false"); activeMark.button.tabIndex = -1; }
      activeMark = mark;
      mark.button.setAttribute("aria-current", "true"); mark.button.tabIndex = 0;
      revealMark(mark.button);
    }
    function scheduleRail() {
      if (railFrame !== null) return;
      railFrame = requestAnimationFrame(function () {
        railFrame = null;
        if (!requestMarks.length || !feed.getClientRects().length) return;
        var rect = feed.getBoundingClientRect(), parent = historyViewport.getBoundingClientRect();
        var room = Math.max(1, rect.height - 72), pitch = touch ? 24 : 13;
        var height = Math.min(420, room, requestMarks.length * pitch);
        turnRail.style.top = (rect.top - parent.top + room / 2 + 4) + "px";
        turnRail.style.height = height + "px";
        turnRail.style.setProperty("--turn-thickness", Math.min(2, height / requestMarks.length * .6) + "px");
        var edge = rect.top + 24, lo = 0, hi = requestMarks.length - 1, found = 0;
        while (lo <= hi) {
          var mid = (lo + hi) >> 1;
          if (requestMarks[mid].node.getBoundingClientRect().top <= edge) { found = mid; lo = mid + 1; }
          else hi = mid - 1;
        }
        highlightMark(requestMarks[found]);
      });
    }
    function clearTurnRail() {
      requestMarks = []; activeMark = null; hoveredMark = null; turnRail.removeAttribute("title"); turnRail.textContent = ""; turnRail.hidden = true;
    }
    function makeMark(node, item, number) {
      var button = el("button", { class: "dialogue-turn", type: "button", tabindex: "-1",
        "aria-label": "Go to request " + number, title: String(item.text || "").slice(0, 500).replace(/\s+/g, " ").slice(0, 160) });
      var mark = { node: node, button: button };
      button.addEventListener("click", function (event) {
        if (event.detail === 0) jumpToMark(mark);
      });
      return mark;
    }
    function markRequest(node, item) {
      var mark = makeMark(node, item, requestMarks.length + 1);
      requestMarks.push(mark); turnRail.appendChild(mark.button); turnRail.hidden = false;
      railLayout();
    }
    function prependMarks(list) {
      var marks = list.map(function (m, i) { return makeMark(m.node, m.item, i + 1); }), first = turnRail.firstChild;
      marks.forEach(function (mark) { turnRail.insertBefore(mark.button, first); });
      requestMarks = marks.concat(requestMarks);
      for (var i = marks.length; i < requestMarks.length; i++) requestMarks[i].button.setAttribute("aria-label", "Go to request " + (i + 1));
      turnRail.hidden = false; railLayout();
    }
    function jumpToMark(mark) {
      if (!mark) return;
      following = false; fresh = false; gliding = Date.now() + 500; down.hidden = false;
      highlightMark(mark);
      var top = mark.node.getBoundingClientRect().top - feed.getBoundingClientRect().top + feed.scrollTop - 12;
      feed.scrollTo({ top: Math.max(0, top), behavior: still.matches ? "auto" : "smooth" });
    }
    function pointedMark(event) {
      var rect = turnRail.getBoundingClientRect();
      if (!rect.height || !requestMarks.length) return null;
      var index = Math.max(0, Math.min(requestMarks.length - 1, Math.floor((event.clientY - rect.top) / rect.height * requestMarks.length)));
      return requestMarks[index];
    }
    turnRail.addEventListener("click", function (event) {
      if (event.detail !== 0) jumpToMark(pointedMark(event));
    });
    turnRail.addEventListener("pointermove", function (event) {
      var mark = pointedMark(event);
      if (hoveredMark === mark) return;
      if (hoveredMark) hoveredMark.button.classList.remove("is-hovered");
      hoveredMark = mark;
      if (mark) { mark.button.classList.add("is-hovered"); turnRail.title = mark.button.title; }
    });
    turnRail.addEventListener("pointerleave", function () {
      if (hoveredMark) hoveredMark.button.classList.remove("is-hovered");
      hoveredMark = null; turnRail.removeAttribute("title");
    });
    turnRail.addEventListener("keydown", function (event) {
      var index = requestMarks.findIndex(function (mark) { return mark.button === event.target; });
      if (index < 0 || !/^(ArrowUp|ArrowDown|Home|End)$/.test(event.key)) return;
      event.preventDefault();
      index = event.key === "Home" ? 0 : event.key === "End" ? requestMarks.length - 1 : Math.max(0, Math.min(requestMarks.length - 1, index + (event.key === "ArrowUp" ? -1 : 1)));
      var mark = requestMarks[index]; highlightMark(mark); mark.button.focus({ preventScroll: true });
    });
    var activity = el("div", { class: "dialogue-activity", role: "status" });
    var moved = Date.now(), wrote = false, sentAt = 0;
    var thinking = el("div", { class: "dialogue-thinking", hidden: "hidden" }, [
      el("div", { class: "message-author" }, [el("img", { class: "message-avatar avatar-assistant", src: "/static/claude.svg", alt: "", "aria-hidden": "true" }), el("strong", { text: brand })]),
      el("div", { class: "thinking-line" }, [el("span", { class: "thinking-dots", "aria-hidden": "true" }, [el("i"), el("i"), el("i")]), activity])
    ]);
    function keep() { if (feed.lastElementChild !== thinking) feed.appendChild(thinking); }
    var input = P.createPromptInput && !touch ? P.createPromptInput() : el("textarea", { class: "dialogue-input", rows: "1", placeholder: touch ? "Message " + brand : "Message " + brand + "…", "aria-label": "Message Claude", autocomplete: "off", enterkeyhint: touch ? "enter" : "send" });
    function icon(button, box, attrs, shapes) {
      var NS = "http://www.w3.org/2000/svg";
      var svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", box);
      svg.setAttribute("height", box);
      svg.setAttribute("aria-hidden", "true");
      Object.keys(attrs).forEach(function (k) { svg.setAttribute(k, attrs[k]); });
      shapes.forEach(function (s) {
        var node = document.createElementNS(NS, s[0]);
        Object.keys(s[1]).forEach(function (k) { node.setAttribute(k, s[1][k]); });
        svg.appendChild(node);
      });
      button.appendChild(svg);
      return button;
    }
    var send = icon(el("button", { class: "btn primary dialogue-send icon-btn", type: "button", "aria-label": "Send", title: touch ? "" : "Send (Enter)" }),
      18, { fill: "currentColor" }, [["path", { d: "M3.4 20.4l17.4-7.5a1 1 0 0 0 0-1.8L3.4 3.6a.9.9 0 0 0-1.3 1L4 11l9 1-9 1-1.9 6.4a.9.9 0 0 0 1.3 1z" }]]);
    var stop = icon(el("button", { class: "btn dialogue-stop icon-btn", type: "button", "aria-label": "Stop", title: touch ? "" : "Stop (Esc)", hidden: "hidden" }),
      14, { fill: "currentColor" }, [["rect", { x: "5", y: "5", width: "14", height: "14", rx: "2.5" }]]);
    var editor = el("div", { class: "prompt-editor" }, [input]);
    input.placeholder = touch ? "Message " + brand : "Message " + brand + "…";
    input.setAttribute("aria-description", "Markdown is formatted as you type. Shift+Enter adds a new line.");
    var down = el("button", { class: "btn dialogue-down", type: "button", text: "↓", "aria-label": "Jump to latest", hidden: "hidden" });
    var menu = el("div", { class: "dialogue-menu", role: "menu", hidden: "hidden" });
    var picker = el("input", { type: "file", multiple: "multiple", hidden: "hidden", "aria-hidden": "true" });
    var clip = icon(el("button", { class: "btn dialogue-attach icon-btn", type: "button", "aria-label": "Attach files", title: touch ? "" : "Attach files" }),
      18, { fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" },
      [["path", { d: "M20.5 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9" }]]);
    var mic = icon(el('button', {class:'btn dialogue-mic icon-btn',type:'button','aria-label':'Voice input','aria-pressed':'false',title:'Voice input'}),
      19, {fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'},
      [['rect',{x:'9',y:'2',width:'6',height:'12',rx:'3'}],['path',{d:'M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M9 22h6'}]]);
    var voiceLabel = el('span'), voiceCancel = el('button',{type:'button',class:'dialogue-voice-cancel',text:'Cancel'});
    var voiceRetry = el('button',{type:'button',class:'dialogue-voice-retry',text:'Retry',hidden:'hidden'});
    var voiceStatus = el('div',{class:'dialogue-voice-status',hidden:'hidden'},[voiceLabel,voiceRetry,voiceCancel]);
    var voice = null, voiceBusy = false;
    var chips = el("div", { class: "dialogue-files", hidden: "hidden" });
    var attachError = el("div", { class: "dialogue-attach-error", role: "alert", hidden: "hidden" });
    var files = [];
    var usageBtn = el("button", { class: "btn dialogue-usage-btn", type: "button", text: "Usage", "aria-expanded": "false" });
    if (actions && actions.attachments === false) clip.hidden = true;
    if (actions && actions.usage === false) usageBtn.hidden = true;
    var usagePanel = el("div", { class: "dialogue-usage", role: "dialog", "aria-label": "Usage limits", hidden: "hidden" });
    function usageTime(sec) { return sec ? new Date(sec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "never"; }
    function usageRender(d) {
      usagePanel.textContent = "";
      if (!d || !d.ok) {
        usagePanel.appendChild(el("div", { class: "usage-off", text: "Limits unavailable" }));
        usagePanel.appendChild(el("div", { class: "usage-time", text: "Checked " + usageTime(d && d.checked_at) + " · last success " + usageTime(d && d.last_ok_at) }));
        return;
      }
      d.rows.forEach(function (row) {
        var fill = el("span", { class: "usage-fill" });
        fill.style.width = row.used + "%";
        if (row.used >= 80) fill.classList.add("hot");
        usagePanel.appendChild(el("div", { class: "usage-row" }, [
          el("div", { class: "usage-head" }, [el("b", { text: row.label }), el("span", { text: row.used + "% used · " + row.left + "% left" })]),
          el("div", { class: "usage-bar" }, [fill]),
          row.resets_at ? el("div", { class: "usage-reset", text: "Resets " + usageReset(row.resets_at) }) : row.resets ? el("div", { class: "usage-reset", text: "Resets " + row.resets }) : null
        ]));
      });
      var fast = d.source === "claude_statusline" || d.source === "claude_agent";
      usagePanel.appendChild(el("div", { class: "usage-time", text: (fast ? "Updated " : "Checked ") + usageTime(d.checked_at) + (d.source === "claude_agent" ? " from the running chat" : fast ? " from your chats" : " via Claude CLI") }));
      if (d.source === "claude_statusline") {
        var slow = el("button", { class: "btn usage-check", type: "button", text: "Check with Claude CLI" });
        slow.addEventListener("click", function () { usageLoad(true); });
        usagePanel.appendChild(slow);
      }
    }
    function usageReset(sec) {
      var date = new Date(sec * 1000), day = date.toDateString() === new Date().toDateString();
      return day ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : date.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
    }
    function usageLoad(slow) {
      usagePanel.textContent = "";
      usagePanel.appendChild(el("div", { class: "usage-time", text: slow ? "Checking with Claude CLI…" : "Checking…" }));
      usageBtn.disabled = true;
      usagePlace();
      P.api("GET", "/api/usage" + (slow ? "?refresh=true" : "")).then(usageRender, function () { usageRender(null); }).then(function () { usageBtn.disabled = false; usagePlace(); });
    }
    usageBtn.addEventListener("click", function () {
      if (!usagePanel.hidden) { usagePanel.hidden = true; usageBtn.setAttribute("aria-expanded", "false"); return; }
      usagePanel.hidden = false;
      usageBtn.setAttribute("aria-expanded", "true");
      usageLoad(false);
    });
    function usagePlace() {
      if (usagePanel.hidden) return;
      if (!usageBtn.isConnected) { usagePanel.hidden = true; return; }
      var vv = window.visualViewport, vt = vv ? vv.offsetTop : 0, vl = vv ? vv.offsetLeft : 0;
      var vw = vv ? vv.width : window.innerWidth, vh = vv ? vv.height : window.innerHeight;
      var b = usageBtn.getBoundingClientRect(), m = 8;
      usagePanel.style.maxHeight = "";
      var w = usagePanel.offsetWidth, h = usagePanel.offsetHeight;
      var minTop = vt + m, head = document.querySelector("header, .topbar, .app-header");
      if (head) { var hb = head.getBoundingClientRect(); if (hb.bottom > minTop && hb.bottom < b.top) minTop = hb.bottom + m; }
      var top = b.top - m - h;
      if (top < minTop) { top = minTop; usagePanel.style.maxHeight = Math.max(80, b.top - m - minTop) + "px"; }
      var left = Math.min(Math.max(b.left, vl + m), vl + vw - w - m);
      usagePanel.style.left = left + "px";
      usagePanel.style.top = top + "px";
    }
    window.addEventListener("resize", usagePlace);
    window.addEventListener("scroll", usagePlace, true);
    if (window.visualViewport) { window.visualViewport.addEventListener("resize", usagePlace); window.visualViewport.addEventListener("scroll", usagePlace); }
    document.addEventListener("click", function (ev) {
      if (!usagePanel.hidden && !usagePanel.contains(ev.target) && ev.target !== usageBtn) { usagePanel.hidden = true; usageBtn.setAttribute("aria-expanded", "false"); }
    });
    if (P.features && P.features.indexOf('voice') < 0) mic.hidden = true;  // the server has no speech recognition
    var actionsRow = [usageBtn, clip, mic, stop, send];
    // A dialogue without the Usage button (API Agent) must not take away the panel of the one that has it.
    if (!(actions && actions.usage === false)) {
      Array.prototype.forEach.call(document.querySelectorAll("body > .dialogue-usage"), function (n) { n.remove(); });
      document.body.appendChild(usagePanel);
    }
    if (!touch) actionsRow.splice(1, 0, el("span", { class: "dialogue-hint" }, [P.tip("Enter to send · Shift+Enter for a new line · Esc to stop")]));
    historyViewport.appendChild(feed);
    historyViewport.appendChild(turnRail);
    historyViewport.appendChild(down);
    host.appendChild(historyViewport);
    var retry = el("button", { class: "btn dialogue-retry", type: "button", text: "Check delivery" });
    var force = el("button", { class: "btn dialogue-retry", type: "button", text: "Force close" });
    var stuckBox = el("div", { class: "dialogue-stuck", role: "alert", hidden: "hidden" }, [el("span", { text: "No response yet — session may be stuck" }), retry, force]);
    force.addEventListener("click", function () { if (actions.forceClose) actions.forceClose().then(function (ok) { if (ok) stuckBox.hidden = true; }); });
    host.appendChild(stuckBox);
    var ask = el("div", { class: "dialogue-ask", role: "group", "aria-label": "Claude is asking", hidden: "hidden" });
    host.appendChild(ask);
    var composer = el("div", { class: "dialogue-composer" }, [chips, attachError, voiceStatus, editor, el("div", { class: "dialogue-actions" }, actionsRow), picker]);
    host.appendChild(composer);
    if (P.VoiceInput) voice = P.VoiceInput({button:mic,status:voiceStatus,label:voiceLabel,cancel:voiceCancel,retry:voiceRetry,input:input,warn:warn,session:function () { return actions.session ? actions.session() : null; },
      changed:function () { rawDraft = null; persist(); fit(); }, state:function (busy) { voiceBusy = busy; controls(); }});
    else mic.addEventListener('click',function () { warn('Voice input did not load. Reload the page.'); });
    if (typeof ResizeObserver === "function") new ResizeObserver(railLayout).observe(feed);
    window.addEventListener("resize", railLayout);
    if (!touch) document.body.appendChild(menu);

    function showState() { paint(); stop.hidden = stop.disabled; }
    function paint() {
      clearTimeout(tick);
      keep();
      var show = false;
      if (starting) { activity.textContent = "Starting Claude…"; activity.dataset.state = "thinking"; stop.disabled = true; show = true; }
      else if (asking && !previewId) { activity.textContent = "Waiting for your answer"; activity.dataset.state = "thinking"; stop.disabled = true; show = true; }
      else if (previewId) { activity.textContent = "Reply to continue"; activity.dataset.state = "idle"; stop.disabled = true; }
      else if (!connected && actions.waiting && actions.waiting()) {
        // An agent without a session to start (API Agent) says what it is waiting for instead of counting seconds.
        activity.textContent = actions.waiting(); activity.dataset.state = "idle"; stop.disabled = true; show = true;
      } else if (!connected) {
        var secs = Math.floor((Date.now() - since) / 1000);
        activity.textContent = selected ? "Starting session… " + secs + " s" : "Waiting for the session…";
        activity.dataset.state = "thinking";
        stop.disabled = true;
        show = !!selected;
        tick = setTimeout(showState, 1000);
      } else if (state === "thinking" || state === "running") {
        var quiet = Date.now() - moved, limit = P.CodeDialogue.STILL_MS || STILL_MS;
        if (quiet >= limit) activity.textContent = "Still working…";
        else {
          activity.textContent = state === "running" && lastTool ? toolLabel(lastTool) : wrote ? "Writing…" : STATES.thinking;
          tick = setTimeout(showState, limit - quiet + 50);
        }
        activity.dataset.state = state;
        stop.disabled = held;
        show = true;
      } else {
        activity.textContent = STATES[state] || STATES.unknown;
        activity.dataset.state = state;
        stop.disabled = true;
      }
      show = show && liveParts.size === 0;
      if (thinking.hidden === show) { thinking.hidden = !show; if (show) follow(); }
    }
    function wait(on) {
      clearTimeout(watch);
      waitSince = on ? Date.now() : 0;
      stuckBox.hidden = true;
      if (on) watch = setTimeout(function () { if (waitSince) stuckBox.hidden = false; }, P.CodeDialogue.STUCK_MS || 45000);
    }
    retry.addEventListener("click", function () {
      poll();
      warn("Delivery is not confirmed yet. Checking the history; the text and Enter are not sent again so the request does not run twice. The text stays in the bubble and survives a reload.");
    });
    function controls() { send.disabled = voiceBusy || starting || held || pending() || (previewId ? false : (!connected || !allowed || bound === false)); showState(); showGreeting(); }
    function pending() { return files.some(function (f) { return !f.path; }); }
    var MAX = 100 * 1024 * 1024;
    function warn(text) {
      attachError.textContent = text; attachError.hidden = !text;
    }
    function drawFiles() {
      chips.textContent = "";
      chips.hidden = !files.length;
      files.forEach(function (f) {
        var x = el("button", { type: "button", class: "dialogue-file-x", text: "×", "aria-label": "Remove " + f.name });
        x.addEventListener("click", function () { if (f.xhr) f.xhr.abort(); files = files.filter(function (o) { return o !== f; }); persist(); drawFiles(); controls(); });
        var kids = [fileTile("dialogue-file-icon", fileKind(f.text !== undefined ? "text.txt" : f.name), 14), el("span", { class: "dialogue-file-name", text: f.text !== undefined ? "Text · " + sizeText(new TextEncoder().encode(f.text).length) : f.name })];
        if (f.text !== undefined) {
          var view = el("button", { type: "button", text: "View", "aria-label": "View text" });
          view.addEventListener("click", function () {
            var dialog = el("dialog", { class: "dialogue-text-preview" });
            var full = el("textarea", { "aria-label": "Full text", readonly: "", rows: "18" });
            full.value = f.text;
            var close = el("button", { type: "button", text: "Close" });
            close.addEventListener("click", function () { dialog.close(); dialog.remove(); });
            dialog.addEventListener("cancel", function () { dialog.remove(); });
            dialog.append(full, close); host.appendChild(dialog); dialog.showModal();
          });
          var edit = el("button", { type: "button", text: "Edit", "aria-label": "Move the text back into the editor" });
          edit.addEventListener("click", function () {
            if (f.xhr) f.xhr.abort();
            setDraft(draftText() + f.text);
            files = files.filter(function (o) { return o !== f; });
            persist(); drawFiles(); controls(); fit(); input.focus();
          });
          kids.push(view, edit);
        }
        if (f.failed && f.lost) {
          kids.push(el("span", { class: "dialogue-file-failed", text: "Attach the file again" }));
        } else if (f.failed) {
          var again = el("button", { type: "button", class: "dialogue-file-retry", text: "Retry", "aria-label": "Retry upload " + f.name });
          again.addEventListener("click", function () { upload(f); });
          kids.push(el("span", { class: "dialogue-file-failed", text: "Upload failed" }), again);
        } else if (!f.path) {
          kids.push(el("span", { class: "dialogue-file-pct", text: f.pct + "%" + (f.rate ? " · " + f.rate : "") }));
        }
        if (/\.mp3$/i.test(f.file || f.name) && voice) {
          var transcribe = el('button',{type:'button',class:'dialogue-file-transcribe',text:'Transcribe','aria-label':'Transcribe '+f.name});
          transcribe.disabled = !f.path;
          transcribe.addEventListener('click',function () { voice.file(f.path); }); kids.push(transcribe);
        }
        kids.push(x);
        var chip = el("span", { class: "dialogue-file" + (f.path ? "" : f.failed ? " failed" : " uploading") + (f.shown ? "" : " motion-chip") }, kids);
        f.shown = true;
        if (!f.path && !f.failed) {
          var fill = el("span", { class: "dialogue-file-fill" });
          fill.style.width = f.pct + "%";
          chip.insertBefore(el("span", { class: "dialogue-file-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(f.pct) }, [fill]), chip.firstChild);
        }
        chips.appendChild(chip);
      });
    }
    function speed(bytes, ms) {
      if (ms < 1000 || bytes < 1048576) return "";
      var bps = bytes / (ms / 1000);
      return bps >= 1048576 ? (bps / 1048576).toFixed(1) + " MB/s" : Math.round(bps / 1024) + " KB/s";
    }
    function textData(text) {
      var bytes = new TextEncoder().encode(text), binary = "";
      for (var i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      return btoa(binary);
    }
    // Files go up as raw bytes (up to 100 MB) and are never copied into page storage; pasted text keeps the JSON route.
    function upload(f) {
      // An agent with its own upload address (actions.uploadUrl) takes every file as raw bytes, pasted text included.
      var custom = actions && actions.uploadUrl;
      var blob = f.blob || (custom && f.text !== undefined ? new Blob([f.text], { type: "text/plain" }) : null);
      if (!blob && f.text === undefined) { f.failed = true; f.lost = true; drawFiles(); controls(); return; }
      f.failed = false; f.lost = false; f.pct = 0; f.rate = ""; drawFiles(); controls();
      var xhr = new XMLHttpRequest(), t0 = Date.now();
      f.xhr = xhr;
      xhr.open("POST", blob ? (custom ? actions.uploadUrl(f) : "/api/uploads/raw?dir=" + encodeURIComponent(f.dir) + "&name=" + encodeURIComponent(f.file)) : "/api/uploads");
      xhr.setRequestHeader("X-Portal", "1");
      if (P.account) xhr.setRequestHeader("X-Portal-Account", P.account);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("Content-Type", blob ? "application/octet-stream" : "application/json");
      xhr.upload.onprogress = function (ev) {
        if (!ev.lengthComputable || files.indexOf(f) < 0) return;
        f.pct = Math.min(99, Math.floor(ev.loaded * 100 / ev.total));
        f.rate = speed(ev.loaded, Date.now() - t0);
        drawFiles();
      };
      var bad = function () {
        f.xhr = null;
        if (files.indexOf(f) < 0) return;
        f.failed = true; drawFiles(); controls();
      };
      xhr.onerror = bad;
      xhr.onload = function () {
        var r = null;
        try { r = JSON.parse(xhr.responseText); } catch (e) { r = null; }
        if (xhr.status < 200 || xhr.status >= 300 || !r || !r.path) return bad();
        f.xhr = null;
        if (files.indexOf(f) < 0) return;
        f.pct = 100; f.path = r.path; f.name = r.name; persist(); drawFiles(); controls();
        if (f.sendAfter && !pending() && !input.value) { f.sendAfter = false; submit(); }
      };
      if (blob) xhr.send(blob);
      else xhr.send(JSON.stringify({ dir: f.dir, name: f.file, data: textData(f.text) }));
    }
    function addFiles(list) {
      warn("");
      if (actions && actions.attachments === false) { warn("Attachments are not available for this agent."); return; }
      var dir = actions.cwd ? actions.cwd() : "";
      Array.prototype.forEach.call(list || [], function (file) {
        if (!dir) { warn("Open a chat first"); return; }
        var limit = actions && actions.maxUpload || MAX;
        if (file.size > limit) { warn(file.name + ": file is larger than " + Math.round(limit / 1048576) + " MB"); return; }
        var f = { name: file.name, file: file.name, dir: dir, path: null, pct: 0, rate: "", blob: file };
        files.push(f); drawFiles(); controls();
        if (!persist()) { f.failed = true; drawFiles(); controls(); return; }
        upload(f);
      });
    }
    function foldText(text, sendAfter) {
      var info = P.CodeDialogue.packText(text), dir = actions.cwd ? actions.cwd() : "";
      if (info.tooBig || !dir) { warn(info.tooBig ? "The text is over 5 MiB. It stays in the editor; shorten it before sending." : "Open a chat first"); return false; }
      var name = "pasted-" + crypto.randomUUID() + ".txt";
      var f = { name: name, file: name, dir: dir, text: text, path: null, pct: 0, rate: "", sendAfter: !!sendAfter };
      files.push(f);
      if (!persist()) { files.pop(); return false; }
      upload(f);
      return true;
    }
    function pastedName(file, i) {
      var d = new Date(), p = function (n) { return String(n).padStart(2, "0"); };
      var ext = (String(file.type).split("/")[1] || "png").replace("jpeg", "jpg").replace(/\+.*$/, "");
      return "screenshot-" + d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "-" + p(d.getHours()) + "-" + p(d.getMinutes()) + (i ? "-" + (i + 1) : "") + "." + ext;
    }
    input.addEventListener("paste", function (ev) {
      var cd = ev.clipboardData;
      if (!cd) return;
      var imgs = [];
      Array.prototype.forEach.call(cd.items || [], function (it) {
        if (it.kind === "file" && /^image\//.test(it.type)) { var f = it.getAsFile(); if (f) imgs.push(f); }
      });
      var pasted = cd.getData("text/plain");
      if (pasted && P.CodeDialogue.packText(pasted).fold) {
        ev.preventDefault();
        if (!foldText(pasted)) { input.setRangeText(pasted, input.selectionStart, input.selectionEnd, "end"); persist(); fit(); }
      }
      if (!imgs.length) return;
      ev.preventDefault();
      if (pasted && !P.CodeDialogue.packText(pasted).fold) {
        input.setRangeText(pasted, input.selectionStart, input.selectionEnd, "end"); persist(); fit();
      }
      addFiles(imgs.map(function (f, i) {
        var named = /^image\.\w+$/i.test(f.name) || !f.name || !/\.\w+$/.test(f.name);
        return named ? new File([f], pastedName(f, i), { type: f.type }) : f;
      }));
    }, true);
    clip.addEventListener("click", function () { picker.value = ""; picker.click(); });
    picker.addEventListener("change", function () { addFiles(picker.files); });
    host.addEventListener("dragover", function (ev) { if (ev.dataTransfer && Array.prototype.indexOf.call(ev.dataTransfer.types || [], "Files") >= 0) { ev.preventDefault(); composer.classList.add("dragging"); } });
    host.addEventListener("dragleave", function (ev) { if (!host.contains(ev.relatedTarget)) composer.classList.remove("dragging"); });
    host.addEventListener("drop", function (ev) {
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault(); composer.classList.remove("dragging"); addFiles(ev.dataTransfer.files);
    });
    function withFiles(text) {
      if (!files.length) return text;
      var lines = files.map(function (f) { return f.path; });
      var complete = files.some(function (f) { return f.text !== undefined; }) ? "\nRead text attachments completely, continuing in chunks to EOF." : "";
      return "Attached files (read them first): " + lines.join(", ") + complete + (text ? "\n\n" + text : "");
    }
    function clearFiles() {
      files.forEach(function (f) { if (f.xhr) f.xhr.abort(); });
      files = []; rawDraft = null; drawFiles(); warn("");
      host.querySelectorAll(".dialogue-text-preview,.file-viewer").forEach(function (dialog) { dialog.remove(); });
    }
    // One follower chases the bottom with an exponential ease, so repeated calls retarget instead of restarting or snapping.
    function stopGlide() { if (glideFrame !== null) cancelAnimationFrame(glideFrame); glideFrame = null; glideExpect = null; }
    // Someone else (scrollbar, keyboard, script) moved the feed up while it was following: hand control back to the reader.
    function glideInterrupted() { return glideExpect !== null && feed.scrollTop < glideExpect - 2; }
    function glideStep(now) {
      glideFrame = null;
      if (!following) { gliding = 0; glideExpect = null; return; }
      var gap = feed.scrollHeight - feed.clientHeight - feed.scrollTop;
      if (glideInterrupted()) { stopGlide(); gliding = 0; if (gap > 70) { following = false; down.hidden = false; } return; }
      if (gap <= 1) { gliding = 0; glideExpect = null; return; }
      var dt = Math.min(48, Math.max(1, now - glideAt)); glideAt = now;
      gliding = Date.now() + 400;
      feed.scrollTop = feed.scrollTop + Math.max(1, gap * (1 - Math.exp(-dt / 65)), gap - feed.clientHeight * 0.75);
      glideExpect = feed.scrollTop;
      glideFrame = requestAnimationFrame(glideStep);
    }
    function glide() {
      if (still.matches || bulk || !feed.getClientRects().length) { stopGlide(); gliding = 0; feed.scrollTop = feed.scrollHeight; return; }
      if (glideInterrupted()) return;
      gliding = Date.now() + 400;
      if (glideFrame === null) { glideAt = performance.now(); glideExpect = feed.scrollTop; glideFrame = requestAnimationFrame(glideStep); }
    }
    function follow(smooth) {
      var smoothly = (smooth || fresh || glideFrame !== null) && !still.matches;
      fresh = false;
      if (following) {
        if (smoothly) glide();
        else { stopGlide(); gliding = 0; feed.scrollTop = feed.scrollHeight; }
      }
      down.hidden = following;
    }
    feed.addEventListener("scroll", function () {
      scheduleRail();
      var movedDown = feed.scrollTop > lastScrollTop + 1; lastScrollTop = feed.scrollTop;
      if (glideInterrupted()) { stopGlide(); gliding = 0; }
      if (Date.now() < gliding) return;
      if (head && !olderBusy && nearTop()) { clearTimeout(olderTimer); olderTimer = null; loadOlder(); }
      var near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70;
      if (!near) following = false;
      else if (movedDown && Date.now() < scrollDownIntent) following = true;
      down.hidden = following;
    });
    feed.addEventListener("scrollend", function () { if (glideFrame === null) gliding = 0; });
    function scrollIntent(dy) {
      scrollDownIntent = dy > 0 ? Date.now() + 300 : 0;
      if (dy >= 0) return;
      following = false; gliding = 0; down.hidden = false; stopGlide();
      feed.scrollTo({top:feed.scrollTop, behavior:'instant'});
    }
    feed.addEventListener('wheel', function (ev) { if (!ev.ctrlKey) scrollIntent(ev.deltaY); }, {passive:true});
    feed.addEventListener('touchstart', function (ev) { touchY = ev.touches.length === 1 ? ev.touches[0].clientY : null; scrollDownIntent = 0; }, {passive:true});
    feed.addEventListener('touchmove', function (ev) {
      if (touchY === null || ev.touches.length !== 1) return;
      var y = ev.touches[0].clientY; scrollIntent(touchY - y); touchY = y;
    }, {passive:true});
    feed.addEventListener('keydown', function (ev) {
      if (/^(ArrowUp|PageUp|Home)$/.test(ev.key)) scrollIntent(-1);
      else if (/^(ArrowDown|PageDown|End| )$/.test(ev.key)) scrollIntent(1);
    });
    down.addEventListener("click", function () { following = true; follow(true); });
    function scrollsItself(node, dy) {
      for (; node && node !== document.documentElement && node.nodeType === 1; node = node.parentElement) {
        if (node === feed) return true;
        var oy = getComputedStyle(node).overflowY;
        if ((oy === "auto" || oy === "scroll") && node.scrollHeight > node.clientHeight + 1) {
          if (dy < 0 ? node.scrollTop > 0 : node.scrollTop + node.clientHeight < node.scrollHeight - 1) return true;
        }
      }
      return false;
    }
    document.addEventListener("wheel", function (ev) {
      if (ev.defaultPrevented || ev.ctrlKey || !ev.deltaY || host.hidden || !host.getClientRects().length) return;
      var t = ev.target;
      if (t && t.closest && t.closest(".desk,.term-box,.modal,iframe")) return;
      if (scrollsItself(t, ev.deltaY)) return;
      var dy = ev.deltaY * (ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? feed.clientHeight : 1);
      scrollIntent(dy);
      var before = feed.scrollTop;
      feed.scrollTop = before + dy;
      if (feed.scrollTop !== before) ev.preventDefault();
    }, { passive: false });
    function append(node) {
      greeting.hidden = true;
      if (sink) { sink.appendChild(node); total += 1; return; }
      if (!bulk) { node.classList.add("motion-in"); fresh = true; }
      feed.appendChild(node);
      keep();
      total += 1;
    }
    // mode: nothing for a saved reply, "live" while it is being written, "static" for a file preview (no form can be answered there).
    function render(body, text, mode) {
      if (P.CodeMarkdown) P.CodeMarkdown(body, text, mode === "static" ? null : { answer: sendAnswer, live: mode === "live" });
      else body.appendChild(el("div", { class: "message-text", text: text }));
    }
    // The answer of a form block goes out as an ordinary message; the draft and the attachments being prepared stay as they were.
    function sendAnswer(text) {
      if (send.disabled) return false;
      var draft = draftText(), held = files;
      files = [];
      try { setDraft(text); submit(); }
      finally { var sentOut = input.value === ""; files = held; setDraft(draft); fit(); persist(); }
      return sentOut;
    }
    // A form answers the last question only: once a message of yours comes after it, the form is closed.
    function closeStaleForms() {
      if (!feed.querySelector(".md-form-open")) return;
      var later = false, kids = feed.children;
      for (var i = kids.length - 1; i >= 0; i--) {
        if (kids[i].classList.contains("message-user")) { later = true; continue; }
        if (later) Array.prototype.forEach.call(kids[i].querySelectorAll(".md-form-open"), function (form) { if (form._close) form._close(); });
      }
    }
    var formSweep = 0;
    new MutationObserver(function () {
      if (formSweep) return;
      formSweep = requestAnimationFrame(function () { formSweep = 0; closeStaleForms(); });
    }).observe(feed, { childList: true, subtree: true });
    function copy(text, button) {
      P.copyText(text).then(function () { if (button) { button.textContent = "✓"; setTimeout(function () { button.textContent = "⧉"; }, 1200); } }, function () {});
    }
    function renderUser(body, text) {
      body.textContent = "";
      var parsed = parseAttachments(text);
      if (parsed.files && parsed.files.length) {
        var chips = el("div", { class: "message-attachments" });
        parsed.files.forEach(function (f, index) {
          var kind = fileKind(f.name, f.type);
          var chip = el("span", { class: "message-attachment", title: f.path || f.name }, [
            fileTile("message-attachment-icon", kind, 18),
            el("span", { class: "message-attachment-text" }, [
              el("span", { class: "message-attachment-name", text: f.name }),
              el("span", { class: "message-attachment-type", text: kind.label })
            ])
          ]);
          chip.hidden = index >= ATTACHMENTS_SHOWN;
          chips.appendChild(chip);
        });
        var extra = parsed.files.length - ATTACHMENTS_SHOWN;
        if (extra > 0) {
          var more = el("button", { type: "button", class: "message-attachment-more", text: "Show " + extra + " more" });
          more.addEventListener("click", function () {
            chips.querySelectorAll(".message-attachment[hidden]").forEach(function (chip) { chip.hidden = false; });
            more.remove();
          });
          chips.appendChild(more);
        }
        body.appendChild(chips);
      }
      body.appendChild(el("div", { class: "message-text", text: parsed.text }));
    }
    function stamp(ts) {
      var d = ts ? new Date(ts) : new Date();
      if (isNaN(d)) d = new Date();
      return el("time", { datetime: d.toISOString(), text: d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) });
    }
    function avatar(role) {
      if (role !== "user") return el("img", { class: "message-avatar avatar-" + role, src: "/static/claude.svg", alt: "", "aria-hidden": "true" });
      var who = document.getElementById("who-email");
      var src = (who && who.textContent) || "U";
      return el("span", { class: "message-avatar avatar-" + role, "aria-hidden": "true", text: src.trim().charAt(0).toUpperCase() || "U" });
    }
    function message(role, text, ts) {
      var body = el("div", { class: "message-body" });
      if (role === "user") renderUser(body, text);
      else render(body, text);
      var copyBtn = el("button", { class: "message-copy", type: "button", text: "⧉", "aria-label": "Copy message", title: touch ? "" : "Copy" });
      copyBtn.addEventListener("click", function () { copy(node._raw, copyBtn); });
      var node = el("article", { class: "dialogue-message message-" + role }, [
        el("div", { class: "message-author" }, [avatar(role), el("strong", { text: role === "user" ? "You" : brand }), stamp(ts), copyBtn]), body
      ]);
      node._raw = text;
      append(node);
      return node;
    }
    function cliKey(text) { return String(text || '').toLowerCase().replace(/[^\p{L}\p{N}]/gu, ''); }
    function textMap(root) {
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [], key = '', node;
      while ((node = walker.nextNode())) {
        if (node.parentElement.closest('button,.cli-old-layer')) continue;
        for (var i = 0; i < node.length;) {
          var char = String.fromCodePoint(node.data.codePointAt(i)), normalized = cliKey(char);
          if (normalized) { for (var j = 0; j < normalized.length; j++) nodes.push({node:node, offset:i}); key += normalized; }
          i += char.length;
        }
      }
      return {key:key, nodes:nodes};
    }
    function pinScroll(semantic) {
      var rect = feed.getBoundingClientRect(), target = document.elementFromPoint(rect.left + Math.min(80, rect.width / 2), rect.top + 12);
      var node = target && target.closest('.dialogue-message,.tool-card');
      var pin = {node:node && feed.contains(node) ? node : null, top:node ? node.getBoundingClientRect().top : 0, scroll:feed.scrollTop};
      if (!semantic || following || !pin.node) return pin;
      var body = pin.node.querySelector('.message-body');
      if (!body) return pin;
      var map = textMap(body), range = document.createRange();
      for (var i = 0; i < map.nodes.length; i++) {
        var point = map.nodes[i]; range.setStart(point.node, point.offset); range.setEnd(point.node, point.offset + 1);
        var box = range.getBoundingClientRect();
        if (box.height && box.bottom > rect.top + 12 && box.top < rect.bottom) {
          pin.quote = map.key.slice(i, i + 80); pin.ratio = i / map.key.length; pin.textTop = box.top; pin.body = body; break;
        }
      }
      return pin;
    }
    function restoreScroll(pin) {
      if (following) glide();
      else if (pin.quote && pin.body.isConnected) {
        var map = textMap(pin.body), at = map.key.indexOf(pin.quote), best = -1;
        while (at >= 0) {
          if (best < 0 || Math.abs(at / map.key.length - pin.ratio) < Math.abs(best / map.key.length - pin.ratio)) best = at;
          at = map.key.indexOf(pin.quote, at + 1);
        }
        if (best >= 0) {
          var point = map.nodes[best], range = document.createRange();
          range.setStart(point.node, point.offset); range.setEnd(point.node, point.offset + 1);
          feed.scrollTop += range.getBoundingClientRect().top - pin.textTop;
        } else feed.scrollTop += pin.node.getBoundingClientRect().top - pin.top;
      } else if (pin.node && pin.node.isConnected) feed.scrollTop += pin.node.getBoundingClientRect().top - pin.top;
      else feed.scrollTop = pin.scroll;
    }
    function resetLive() {
      cliPainters.forEach(function (cancel) { cancel(); }); cliPainters.clear();
      handoffs.forEach(function (finish) { finish(); }); handoffs.clear();
      retired.forEach(function (node) { clearTimeout(node._retireTimer); if (node.isConnected) { node.remove(); total -= 1; } }); retired.clear(); stopGlide();
      if (cliFrame !== null) cancelAnimationFrame(cliFrame);
      cliFrame = null; queuedCli = null; expectedClient = null;
      liveTurn = null; liveAliases.clear(); liveParts.clear(); liveKnown = []; liveClosed = false;
    }
    function beginLive(id, aliases) {
      if (liveAliases.has(id)) { (aliases || []).forEach(function (alias) { if (alias) liveAliases.add(alias); }); return; }
      liveParts.forEach(function (node) { node.classList.add('cli-incomplete'); });
      liveParts.clear(); liveTurn = id; liveAliases = new Set([id].concat(aliases || []).filter(Boolean));
      liveClosed = false; liveKnown = [];
    }
    function beforeLive(node) {
      var first = Array.from(liveParts.values()).find(function (part) { return part.isConnected && part !== node; });
      if (first && node.isConnected) feed.insertBefore(node, first);
      return node;
    }
    function finishCli(node, text, ts) {
      if (node._cliPaint) {
        node._cliPaint();
        var flow = node.querySelector('.cli-flow');
        if (flow && node._cliTarget) drawCli(flow, node._cliTarget, node._cliCols);
      }
      var body = node.querySelector('.message-body'), pin = pinScroll(true), oldHeight = body.getBoundingClientRect().height;
      var old = el('div', {class:'cli-old-layer','aria-hidden':'true'}), next = el('div', {class:'cli-final-layer'});
      while (body.firstChild) old.appendChild(body.firstChild);
      render(next, text); body.append(old, next);
      node._raw = text; node._cliPreview = false; node.classList.remove('message-cli-preview');
      node.querySelector('time').replaceWith(stamp(ts));
      if (!following || still.matches || !node.getClientRects().length) { body.replaceChildren(next); restoreScroll(pin); return node; }
      node._handoff = true; body.classList.add('cli-handoff'); body.style.height = oldHeight + 'px';
      var height = next.getBoundingClientRect().height, start = Math.max(performance.now(), (node._cliStart || 0) + (P.CodeDialogue.CLI_HOLD_MS == null ? 500 : P.CodeDialogue.CLI_HOLD_MS)), frame = null;
      function finish() {
        if (frame !== null) cancelAnimationFrame(frame);
        handoffs.delete(finish); node._handoff = false;
        var anchor = pinScroll(true); body.replaceChildren(next); body.classList.remove('cli-handoff'); body.style.height = ''; next.style.opacity = '';
        if (node.isConnected) restoreScroll(anchor);
        if (node._rerenderAfter && node.isConnected) { node._rerenderAfter = false; finishCli(node, node._raw, ts); }
      }
      handoffs.add(finish);
      function animate(now) {
        if (!node.isConnected || !following) { finish(); return; }
        if (now < start) { frame = requestAnimationFrame(animate); return; }
        var anchor = pinScroll(), progress = Math.min(1, (now - start) / 180), ease = 1 - Math.pow(1 - progress, 3);
        body.style.height = (oldHeight + (height - oldHeight) * ease) + 'px';
        old.style.opacity = String(1 - ease); next.style.opacity = String(ease);
        restoreScroll(anchor);
        if (progress < 1) frame = requestAnimationFrame(animate);
        else finish();
      }
      restoreScroll(pin); frame = requestAnimationFrame(animate);
      return node;
    }
    function authoritative(it) {
      var text = cliKey(it.text), matches = Array.from(liveParts.entries()).filter(function (entry) {
        var raw = cliKey(entry[1]._raw);
        return raw && (raw === text || Math.min(raw.length, text.length) >= 24 && (text.startsWith(raw) || raw.startsWith(text)));
      });
      if (!matches.length && it.final && liveParts.size) matches = [Array.from(liveParts.entries()).pop()];
      var node, back = matches.length ? null : Array.from(retired).find(function (part) {
        var raw = cliKey(part._raw);
        return part.isConnected && raw && (raw === text || Math.min(raw.length, text.length) >= 24 && (text.startsWith(raw) || raw.startsWith(text)));
      });
      if (back) {
        node = finishCli(adopt(back), it.text, it.ts);
      } else if (matches.length) {
        var match = matches[0]; liveParts.delete(match[0]); node = finishCli(match[1], it.text, it.ts);
      } else if (it.final && liveBatch && !previewId && connected) {
        var pin = pinScroll();
        node = beforeLive(message('assistant', '', it.ts)); node.classList.remove('motion-in'); fresh = false;
        var plain = el('div', { class: 'cli-flow' }); node.querySelector('.message-body').appendChild(plain); drawCli(plain, it.text, 80);
        node._cliStart = performance.now();
        finishCli(node, it.text, it.ts); restoreScroll(pin);
      } else node = beforeLive(message('assistant', it.text, it.ts));
      liveKnown.push(text); liveKnown = liveKnown.slice(-64);
      if (it.final) {
        var pin = pinScroll(); liveParts.forEach(function (part) { part.remove(); total -= 1; }); liveParts.clear(); liveClosed = true; restoreScroll(pin);
        retired.forEach(function (part) { dropRetired(part); });
      }
      return node;
    }
    // Terminal frames arrive in bursts; one painter per bubble chases the newest frame at a rate that drains any backlog in ~170 ms,
    // so text streams continuously instead of jumping by whole lines between bursts.
    function paintCliText(node, text, cols) {
      var body = node.querySelector('.message-body'), flow = body.firstChild;
      if (!flow || !flow.classList || !flow.classList.contains('cli-flow')) {
        flow = el('div', { class: 'cli-flow' }); node._cliShown = body.textContent; body.replaceChildren(flow); drawCli(flow, node._cliShown, cols);
      }
      node._cliTarget = text; node._cliCols = cols;
      function show(value) { node._cliShown = value; drawCli(flow, value, node._cliCols); }
      if (still.matches || !text.startsWith(node._cliShown)) { if (node._cliPaint) node._cliPaint(); show(text); return; }
      var backlog = text.length - node._cliShown.length;
      if (backlog <= 0) return;
      if (!node._cliPaint && backlog < 40) { show(text); return; }
      node._cliRate = Math.max(node._cliPaint ? node._cliRate || 0 : 0, backlog / 170, 0.06);
      if (node._cliPaint) return;
      var last = performance.now(), carry = 0, frame = null;
      function cancel() { if (frame !== null) cancelAnimationFrame(frame); frame = null; cliPainters.delete(cancel); node._cliPaint = null; node._cliRate = 0; }
      node._cliPaint = cancel; cliPainters.add(cancel);
      function animate(now) {
        frame = null;
        if (!node.isConnected || !node._cliPreview) { cancel(); return; }
        var target = node._cliTarget, shown = node._cliShown;
        if (!target.startsWith(shown)) { show(target); cancel(); return; }
        carry += node._cliRate * Math.min(48, Math.max(0, now - last)); last = now;
        var step = Math.floor(carry);
        if (step > 0) {
          carry -= step;
          var length = Math.min(target.length, shown.length + step);
          if (length < target.length && /[\uD800-\uDBFF]/.test(target.charAt(length - 1))) length += 1;
          var pin = pinScroll(); show(target.slice(0, length)); restoreScroll(pin);
        }
        if (node._cliShown.length < target.length) frame = requestAnimationFrame(animate); else cancel();
      }
      frame = requestAnimationFrame(animate);
    }
    // A withdrawn frame waits briefly for its saved JSONL text so the bubble can turn into it in place instead of vanishing and reappearing.
    function retire(node) {
      if (node._cliPaint) node._cliPaint();
      node._cliPreview = false; node.classList.remove('message-cli-preview', 'message-assistant'); node.classList.add('message-retiring');
      retired.add(node); node._retireTimer = setTimeout(function () { dropRetired(node); }, 1200);
    }
    function adopt(node) {
      clearTimeout(node._retireTimer); retired.delete(node);
      node.classList.remove('message-retiring'); node.classList.add('message-assistant');
      return node;
    }
    function dropRetired(node, now) {
      clearTimeout(node._retireTimer); retired.delete(node);
      if (!node.isConnected) return;
      function gone() { if (!node.isConnected) return; var pin = pinScroll(); node.remove(); total -= 1; paint(); restoreScroll(pin); }
      if (now || still.matches || !node.animate || !node.getClientRects().length) { gone(); return; }
      var height = node.getBoundingClientRect().height, gap = parseFloat(getComputedStyle(feed).rowGap) || 0;
      node.style.overflow = 'hidden';
      node.animate([{ opacity: getComputedStyle(node).opacity, height: height + 'px', marginBottom: '0px' }, { opacity: 0, height: '0px', marginBottom: -gap + 'px' }],
        { duration: 200, easing: 'cubic-bezier(.23,1,.32,1)', fill: 'forwards' }).onfinish = gone;
    }
    function showCli(event) {
      if (!loaded || catching || !selected || !connected || !allowed || event.sid !== chatSid) return false;
      if (!liveAliases.has(event.turn_id)) {
        var own = outbox.find(function (node) { return node._clientId === event.turn_id; });
        if (!own) return false;
        beginLive(event.turn_id); expectedClient = event.turn_id;
      }
      if (liveClosed || typeof event.text !== 'string' || !event.text.trim()) return true;
      var text = cliKey(event.text), id = event.stream_id + ':' + event.part_id;
      var extended = liveKnown.find(function (known) { return known.length >= 12 && text.length > known.length && text.startsWith(known); });
      if (extended) {
        var duplicate = liveParts.get(id);
        if (duplicate && cliKey(duplicate._raw).startsWith(extended)) {
          var anchor = pinScroll(); if (duplicate._cliPaint) duplicate._cliPaint(); liveParts.delete(id); duplicate.remove(); total -= 1; paint(); restoreScroll(anchor);
        }
        return true;
      }
      if (liveKnown.some(function (known) { return known === text || text.length >= 12 && known.startsWith(text); })) return true;
      var node = liveParts.get(id);
      if (!node && liveParts.size && !event.split) {
        node = liveParts.values().next().value; liveParts.clear();
      }
      var pin = pinScroll();
      if (!node) {
        node = Array.from(retired).find(function (part) { var raw = cliKey(part._raw); return part.isConnected && raw && (text === raw || text.startsWith(raw)); });
        if (node) { adopt(node); node._cliPreview = true; node.classList.add('message-cli-preview'); }
      }
      if (!node) {
        node = message('assistant', ''); node._cliPreview = true; node._cliStart = performance.now(); node.classList.add('message-cli-preview'); node.classList.remove('motion-in');
        var time = node.querySelector('time'); time.textContent = actions.liveLabel || 'CLI'; time.title = actions.liveLabel ? 'Being written; formatting appears once the reply is saved' : 'Preview from the terminal; formatting appears once the reply is saved';
      }
      liveParts.set(id, node);
      if (event.source === 'agent') {
        // Agent mode streams the reply as markdown already: draw it like a saved reply, not as terminal text.
        var shown = node.querySelector('.message-body'), clock = node.querySelector('time');
        if (clock && clock.textContent === 'CLI') { clock.textContent = 'Live'; clock.title = 'Streaming; saved once the reply is complete'; }
        shown.replaceChildren(); render(shown, event.text, "live");
      } else paintCliText(node, event.text, event.cols);
      node._raw = event.text; wrote = true; moved = Date.now(); fresh = false; wait(false); thinking.hidden = true;
      restoreScroll(pin);
      return true;
    }
    var asking = null, askTimer = null, askCall = null;
    function drawAsk(prompt, problem) {
      if (!prompt) {
        if (asking) { asking = null; ask.textContent = ""; ask.hidden = true; paint(); }
        return;
      }
      if (asking && asking.signature === prompt.signature && !problem) return;
      asking = prompt;
      ask.textContent = "";
      if (prompt.title) ask.appendChild(el("div", { class: "dialogue-ask-title", text: prompt.title }));
      if (prompt.detail) ask.appendChild(el("pre", { class: "dialogue-ask-detail", text: prompt.detail }));
      if (prompt.question) ask.appendChild(el("div", { class: "dialogue-ask-question", text: prompt.question }));
      if (problem) ask.appendChild(el("div", { class: "dialogue-ask-problem", role: "alert", text: problem }));
      var buttons = el("div", { class: "dialogue-ask-options" });
      function reply(body) {
        Array.prototype.forEach.call(ask.querySelectorAll("button,input"), function (node) { node.disabled = true; });
        var at = selected;
        body.signature = prompt.signature;
        P.api("POST", sessionUrl(at, "/prompt"), body).then(function () {
          if (selected === at) { moved = Date.now(); drawAsk(null); clearTimeout(askTimer); askTimer = setTimeout(askPoll, 600); }
        }, function (e) { if (selected === at) { drawAsk(prompt, e.message || "Could not deliver the answer."); } });
      }
      if (prompt.multiple) buttons.appendChild(el("div", { class: "dialogue-ask-note", text: "This question allows several answers. Open Console to answer it." }));
      else prompt.options.forEach(function (option) {
        if (option.free) {
          var field = el("input", { class: "dialogue-ask-input", type: "text", maxlength: "4000", placeholder: "Type your answer", "aria-label": "Type your answer" });
          var go = el("button", { class: "btn", type: "button", text: "Answer" });
          function typed() { if (field.value.trim()) reply({ option: option.n, text: field.value.trim() }); }
          go.addEventListener("click", typed);
          field.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); typed(); } });
          buttons.appendChild(el("div", { class: "dialogue-ask-free" }, [field, go]));
          return;
        }
        var button = el("button", { class: "btn dialogue-ask-option" + (option.n === 1 ? " primary" : ""), type: "button" }, [el("span", { text: option.label })].concat(option.hint ? [el("small", { text: option.hint })] : []));
        button.addEventListener("click", function () { reply({ option: option.n }); });
        buttons.appendChild(button);
      });
      if (!prompt.permission) {
        var cancel = el("button", { class: "btn dialogue-ask-cancel", type: "button", text: "Cancel" });
        cancel.addEventListener("click", function () { reply({}); });
        buttons.appendChild(cancel);
      }
      ask.appendChild(buttons);
      ask.hidden = false;
      paint();
    }
    function askPoll() {
      clearTimeout(askTimer);
      var at = selected;
      function again() { clearTimeout(askTimer); askTimer = setTimeout(askPoll, P.CodeDialogue.ASK_MS || 2000); }
      if (!at || previewId) { drawAsk(null); return again(); }
      if (document.hidden || !host.getClientRects().length) return again();
      if (ask.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return again();
      if (askCall) askCall.abort();
      var call = askCall = new AbortController(), deadline = setTimeout(function () { call.abort(); }, P.CodeDialogue.REQUEST_MS || 15000);
      P.peek(sessionUrl(at, "/prompt"), call.signal).then(function (data) {
        if (askCall === call && selected === at) drawAsk(data && data.prompt);
      }).finally(function () { clearTimeout(deadline); if (askCall === call) { askCall = null; again(); } });
    }
    function terminalEvent(event) {
      if (!event || event.sid !== chatSid || previewId || !selected) return;
      if (event.type === 'transcript_ready') { clearTimeout(timer); timer = setTimeout(poll, 0); return; }
      if (event.type === 'agent_state') { if (event.prompt) { clearTimeout(askTimer); askTimer = setTimeout(askPoll, 0); } return; }
      if (event.type === 'agent_error') { warn(event.detail || 'Could not reach Claude.'); return; }
      if (event.type === 'terminal_preview_clear') {
        if (queuedCli && queuedCli.turn_id === event.turn_id && queuedCli.stream_id === event.stream_id && queuedCli.part_id === event.part_id) queuedCli = null;
        if (!liveAliases.has(event.turn_id)) return;
        var id = event.stream_id + ':' + event.part_id, node = liveParts.get(id);
        if (node) { liveParts.delete(id); retire(node); paint(); }
        return;
      }
      if (event.type !== 'terminal_preview') return;
      queuedCli = event;
      if (cliFrame !== null) return;
      cliFrame = requestAnimationFrame(function () { cliFrame = null; var candidate = queuedCli; queuedCli = null; if (candidate && !showCli(candidate)) queuedCli = candidate; });
    }
    var outbox = [];
    var rawDraft = null;
    function draftText() { return rawDraft && rawDraft.view === input.value ? rawDraft.raw : input.value; }
    function setDraft(text) { input.value = text; rawDraft = { raw: text, view: input.value }; }
    function storageKey() {
      var who = document.getElementById("who-email");
      return "portal-code-outbox:" + JSON.stringify([who && (who.title || who.textContent), P.account || "", chatSid || previewId || selected]);
    }
    function persist() {
      if (P.identityLocked) return false;
      if (!chatSid && !previewId && !selected) return true;
      var savedFiles = files.map(function (f) { return { name: f.name, file: f.file, dir: f.dir, text: f.text, path: f.path, pct: f.pct }; });
      try {
        sessionStorage.setItem(storageKey(), JSON.stringify({ draft: draftText(), attachments: savedFiles, outbox: outbox.map(function (n) { return { text: n._raw, id: n._clientId, files: n._files, sources: n._sources, attempted: !!n._attempted }; }) }));
        return true;
      } catch (e) { warn("Could not save the text in this browser. Sending is paused; copy your text."); return false; }
    }
    function restore() {
      try {
        var saved = JSON.parse(sessionStorage.getItem(storageKey()) || "null");
        if (!saved) return;
        setDraft(saved.draft || "");
        files = (saved.attachments || []).filter(function (f) { return !/\.mp3$/i.test(f.file || f.name) || !!f.path; });
        files.forEach(function (f) { if (!f.path) { f.failed = true; f.lost = f.text === undefined; } });
        drawFiles(); controls();
        (saved.outbox || []).forEach(function (s) {
          var n = message("user", s.text);
          n._clientId = s.id; n._files = s.files; n._sources = s.sources; n._attempted = s.attempted;
          outbox.push(n);
          settle(n, s.attempted ? null : false);
        });
      } catch (e) { warn("Could not restore the saved text."); }
    }
    window.addEventListener("portal-identity-reset", function (event) {
      if (voice) voice.cancel();
      if (!event.detail || !event.detail.navigation) {
        try { Object.keys(sessionStorage).forEach(function (key) { if (key.indexOf("portal-code-outbox:") === 0) sessionStorage.removeItem(key); }); } catch (e) {}
      }
      files.forEach(function (f) { if (f.xhr) f.xhr.abort(); });
      clearFiles();
      host.querySelectorAll(".dialogue-text-preview,.file-viewer").forEach(function (dialog) { dialog.remove(); });
      resetLive(); outbox = []; input.value = ""; drafts.clear();
      epoch += 1; clearTimeout(timer); resetLive(); feed.textContent = ""; clearTurnRail(); total = 0; seen.clear(); tools.clear();
      head = null; pendingResults.clear(); hideLoadNote(); if (pollCall) pollCall.control.abort();
    });
    window.addEventListener("portal-save-before-navigation", persist);
    function wire(node) {
      if (/^\s*\//.test(node._raw)) return node._raw;
      if (!node._clientId) node._clientId = crypto.randomUUID();
      return node._raw + "\n\n<portal-message-id>" + node._clientId + "</portal-message-id>";
    }
    function settle(node, ok) {
      var old = node.querySelector(".message-status");
      if (old) old.remove();
      node.classList.toggle("message-pending", ok === null);
      node.classList.toggle("message-failed", ok === false);
      if (ok === true) return;
      var status = el("div", { class: "message-status", role: ok === false ? "alert" : "status" }, [el("span", { text: ok === false ? "Not sent" : "Checking delivery…" })]);
      if (ok === false) {
        var again = el("button", { type: "button", class: "btn message-retry", text: "Retry" });
        again.addEventListener("click", function () { deliver(node); });
        status.appendChild(again);
      }
      node.appendChild(status);
    }
    function deliver(node) {
      if (node._attempted) { poll(); return false; }
      var text = wire(node);
      if (node._clientId) { beginLive(node._clientId); expectedClient = node._clientId; }
      node._attempted = true;
      if (!persist()) { node._attempted = false; settle(node, false); return false; }
      settle(node, null);
      if (previewId && actions.resume) {
        var id = previewId;
        actions.resume(text).then(function (ok) {
          if (chatSid !== id) return;
          if (!ok) { node._attempted = false; settle(node, false); persist(); return; }
          node.classList.add("message-pending");
          wait(true); state = "thinking"; lastTool = ""; moved = Date.now(); wrote = false;
          if (node._clientId) { beginLive(node._clientId); expectedClient = node._clientId; }
          sentAt = /^\s*\//.test(node._raw || "") ? 0 : Date.now();
          showState(); setTimeout(poll, 300);
        }, function () {
          if (chatSid !== id) return;
          warn("Delivery not confirmed. The text is kept; check the history before retrying.");
          node._attempted = false; settle(node, false); persist();
        });
        return true;
      }
      try {
        if (!actions.send(text, node._files)) { node._attempted = false; settle(node, false); persist(); return false; }
      } catch (e) { warn("Delivery not confirmed. The text is kept; check the history before retrying."); return false; }
      wait(true);
      state = "thinking"; lastTool = ""; moved = Date.now(); wrote = false; sentAt = /^\s*\//.test(node._raw || "") ? 0 : Date.now();
      showState();
      setTimeout(poll, 300);
      return true;
    }
    function claim(it) {
      if (!it.client_id) return false;
      for (var i = 0; i < outbox.length; i++) {
        if (outbox[i]._clientId === it.client_id) {
          var node = outbox.splice(i, 1)[0];
          settle(node, true);
          node._raw = it.text;
          var body = node.querySelector(".message-body");
          if (body) renderUser(body, it.text);
          else {
            var txt = node.querySelector(".message-text");
            if (txt) txt.textContent = it.text;
          }
          node.querySelector("time").replaceWith(stamp(it.ts));
          persist();
          if (node.isConnected) { feed.insertBefore(node, thinking); beforeLive(node); }
          return node.isConnected ? node : false;
        }
      }
      return false;
    }
    function tool(it) {
      var result = el("pre", { text: "…" });
      var badge = el("span", { class: "tool-status", text: "running" });
      var node = el("details", { class: "tool-card" }, [
        el("summary", {}, [el("strong", { text: it.name }), el("span", { class: "tool-args", text: String(it.args || "").split("\n")[0].slice(0, touch ? 60 : 160) }), badge]),
        el("div", { class: "tool-details" }, [el("pre", { text: it.args || "" }), result])
      ]);
      append(node);
      tools.set(it.id, { result: result, badge: badge, node: node });
      lastTool = it.name;
      var early = pendingResults.get(it.id);
      if (early) { pendingResults.delete(it.id); toolResult(early); }
    }
    function thought(it) {
      var node = el("details", { class: "dialogue-thought" }, [
        el("summary", {}, [el("strong", { text: "Thinking" }), el("span", { class: "thought-preview", text: String(it.text || "").trim().split("\n")[0].slice(0, touch ? 60 : 160) })]),
        el("div", { class: "thought-body", text: it.text })
      ]);
      append(node);
      return node;
    }
    function toolResult(it) {
      var t = tools.get(it.tool_id);
      if (!t) { if (head) pendingResults.set(it.tool_id, it); return; }
      t.result.textContent = it.text || "(empty)";
      t.badge.textContent = it.error ? "error" : "done";
      t.node.classList.toggle("tool-error", !!it.error);
    }
    function stopMarker(kind) {
      return el("div", { class: "dialogue-stop-marker", role: "status", text: kind === "interrupted" ? "Stopped by user"
        : kind === "turn_error" ? "Claude could not complete this request. Check Console for details." : "The previous request was not completed. Send a new message to continue." });
    }
    var chatSid = null, turnStart = NaN, turns = [], boxTimer = null, boxEpoch = 0;
    function sizeText(n) {
      if (n < 1024) return n + " B";
      if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
      return (n / 1048576).toFixed(1) + " MB";
    }
    function outboxUrl(sid, f, view, scripts) {
      return chatUrl(sid, "/outbox/" + encodeURIComponent(f.name)) + (view ? "?view=1" + (scripts ? "&scripts=1" : "") : "");
    }
    function canView(f) { return !!f.preview && !f.too_big; }
    function openViewer(sid, siblings, first) {
      var items = siblings.filter(canView), at = Math.max(0, items.indexOf(first));
      var epoch = 0, ctrl = null, scripts = false, source = false, raw = "";
      host.querySelectorAll(".file-viewer").forEach(function (old) { old.remove(); });
      var dlg = el("dialog", { class: "file-viewer", "aria-label": "File preview" });
      function button(text, label, onclick) {
        var b = el("button", { type: "button", class: "btn", text: text, "aria-label": label, title: label });
        b.addEventListener("click", onclick);
        return b;
      }
      var name = el("strong", { class: "fv-name" }), meta = el("span", { class: "fv-meta" });
      var prev = button("‹", "Previous file", function () { show(at - 1); });
      var next = button("›", "Next file", function () { show(at + 1); });
      var nav = el("div", { class: "fv-nav" }, [prev, next]);
      var rendered = button("Rendered", "Show rendered Markdown", function () { source = false; drawText(); });
      var plain = button("Source", "Show Markdown source", function () { source = true; drawText(); });
      var modes = el("div", { class: "fv-modes", role: "group", "aria-label": "Markdown view" }, [rendered, plain]);
      var run = button("Run scripts", "Allow scripts in this page", function () { scripts = !scripts; drawFrame(); });
      var copyBtn = button("⧉", "Copy text", function () { copy(raw, copyBtn); });
      var tab = el("a", { class: "btn", target: "_blank", rel: "noopener", text: "Open in tab" });
      var save = el("a", { class: "btn", text: "Download" });
      var body = el("div", { class: "fv-body" }), note = el("p", { class: "fv-note", role: "status" });
      var close = button("×", "Close preview", function () { dlg.close(); });
      dlg.append(
        el("div", { class: "fv-head" }, [
          el("div", { class: "fv-title" }, [name, meta]),
          nav, modes, run, copyBtn, tab, save, close
        ]),
        note, body
      );
      function current() { return items[at]; }
      function fail(text) {
        body.textContent = "";
        body.className = "fv-body";
        body.appendChild(el("p", { class: "fv-error", text: text }));
      }
      function drawText() {
        var f = current();
        if (!f || f.preview !== "text") return;
        body.textContent = "";
        body.className = "fv-body";
        var md = /\.md$/i.test(f.name);
        rendered.setAttribute("aria-pressed", String(md && !source));
        plain.setAttribute("aria-pressed", String(md && source));
        if (md && !source) {
          var doc = el("div", { class: "message-body fv-md" });
          render(doc, raw, "static");
          body.appendChild(doc);
        } else {
          body.appendChild(el("pre", { class: "fv-text", tabindex: "0", text: raw }));
        }
      }
      function drawFrame() {
        var f = current();
        body.textContent = "";
        body.className = "fv-body";
        run.setAttribute("aria-pressed", String(scripts));
        note.textContent = scripts
          ? "Scripts are running in an isolated sandbox: no access to your account, the portal or the network."
          : "Scripts are off and external resources are blocked, so some pages look plainer than in a browser.";
        var frame = el("iframe", { class: "fv-frame", title: f.name, referrerpolicy: "no-referrer" });
        frame.setAttribute("sandbox", scripts ? "allow-scripts" : "");
        frame.setAttribute("src", outboxUrl(sid, f, true, scripts));
        body.appendChild(frame);
      }
      function show(i) {
        if (i < 0 || i >= items.length) return;
        at = i;
        var f = current(), mine = ++epoch;
        if (ctrl) { ctrl.abort(); ctrl = null; }
        scripts = false; source = false; raw = "";
        name.textContent = f.name; name.title = f.name;
        meta.textContent = (items.length > 1 ? (at + 1) + " of " + items.length + " · " : "") + sizeText(f.size);
        save.href = outboxUrl(sid, f); save.setAttribute("download", f.name);
        tab.href = outboxUrl(sid, f, true);
        prev.disabled = at === 0; next.disabled = at === items.length - 1;
        nav.hidden = items.length < 2;
        modes.hidden = !(f.preview === "text" && /\.md$/i.test(f.name));
        run.hidden = f.preview !== "html";
        copyBtn.hidden = f.preview !== "text";
        note.textContent = "";
        note.hidden = f.preview !== "html";
        body.textContent = "";
        var focus = document.activeElement;
        if (!focus || !dlg.contains(focus) || focus.offsetParent === null) close.focus();
        if (f.preview === "image") {
          body.className = "fv-body fv-image";
          var img = el("img", { class: "fv-img", alt: f.name, src: outboxUrl(sid, f, true) });
          img.addEventListener("click", function () { img.classList.toggle("actual"); });
          img.addEventListener("error", function () { if (mine === epoch) fail("This image could not be loaded."); });
          body.appendChild(img);
        } else if (f.preview === "html") {
          drawFrame();
        } else {
          body.appendChild(el("p", { class: "fv-note", text: "Loading…" }));
          ctrl = new AbortController();
          P.text(outboxUrl(sid, f, true), ctrl.signal).then(function (t) {
            if (mine !== epoch) return;
            raw = t; drawText();
          }).catch(function (e) {
            if (e.name === "AbortError" || mine !== epoch) return;
            fail(e.message || "The preview could not be loaded.");
          });
        }
      }
      dlg.addEventListener("keydown", function (e) {
        if (e.altKey || e.ctrlKey || e.metaKey) return;
        if (e.key === "ArrowLeft") { show(at - 1); e.preventDefault(); }
        else if (e.key === "ArrowRight") { show(at + 1); e.preventDefault(); }
      });
      dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
      dlg.addEventListener("close", function () { epoch++; if (ctrl) ctrl.abort(); dlg.remove(); });
      host.appendChild(dlg);
      dlg.showModal();
      show(at);
      close.focus();
    }
    function fileCard(sid, f, siblings) {
      var viewable = canView(f);
      var action = f.too_big
        ? el("span", { class: "outbox-note", text: "Over 50 MB" })
        : el("a", { class: "btn outbox-download", href: outboxUrl(sid, f), download: f.name, text: "Download" });
      var tile = fileTile("outbox-type", fileKind(f.name), 20);
      if (viewable && f.preview === "image" && f.size <= 4 * 1048576) {
        var thumb = el("img", { class: "outbox-thumb", alt: "", loading: "lazy", decoding: "async", src: outboxUrl(sid, f, true) });
        thumb.addEventListener("error", function () { thumb.remove(); tile.classList.remove("has-thumb"); });
        tile.classList.add("has-thumb");
        tile.appendChild(thumb);
      }
      var meta = el("span", { class: "outbox-meta" }, [el("strong", { class: "outbox-name", text: f.name, title: f.name }), el("span", { class: "outbox-size", text: fileKind(f.name).label + " · " + sizeText(f.size) })]);
      var actions = [action];
      if (viewable) {
        var open = el("button", { type: "button", class: "btn outbox-preview", text: "Preview", "aria-label": "Preview " + f.name });
        var show = function () { openViewer(sid, siblings || [f], f); };
        open.addEventListener("click", show);
        [tile, meta].forEach(function (n) { n.classList.add("outbox-open"); n.addEventListener("click", show); });
        actions.unshift(open);
      }
      return el("div", { class: "outbox-card" }, [tile, meta, el("span", { class: "outbox-actions" }, actions)]);
    }
    function drawOutbox(sid, list) {
      turns = turns.filter(function (t) { return t.node.isConnected; });
      var groups = new Map();
      list.forEach(function (f) {
        var m = f.mtime * 1000;
        for (var i = 0; i < turns.length; i++) {
          var t = turns[i];
          if ((isNaN(t.start) || m >= t.start - 2000) && m <= t.end + 5000) {
            if (!groups.has(t)) groups.set(t, []);
            groups.get(t).push(f);
            return;
          }
        }
      });
      turns.forEach(function (t) {
        var files = groups.get(t) || [];
        var key = files.map(function (f) { return f.name + ":" + f.mtime + ":" + f.size; }).join("|");
        if (t.key === key) return;
        t.key = key;
        if (t.box) { t.box.remove(); t.box = null; }
        if (!files.length) return;
        t.box = el("div", { class: "outbox-files", "aria-label": "Files from Claude" }, files.map(function (f) { return fileCard(sid, f, files); }));
        t.node.appendChild(t.box);
      });
    }
    function refreshOutbox() {
      clearTimeout(boxTimer);
      boxTimer = setTimeout(function () {
        var sid = chatSid, my = ++boxEpoch;
        if (!sid || !turns.length) return;
        P.api("GET", chatUrl(sid, "/outbox")).then(function (r) {
          if (my === boxEpoch && sid === chatSid) drawOutbox(sid, r.files || []);
        }, function () {});
      }, 400);
    }
    // A request can hang without ever failing (a stalled connection). It must not hold the chat forever: past its deadline it is cancelled and sent again.
    function timedGet(url, slow) {
      var control = new AbortController(), late = false;
      var limit = Math.min((P.CodeDialogue.REQUEST_MS || 15000) * Math.pow(2, slow), 120000);
      var deadline = setTimeout(function () { late = true; control.abort(); }, limit);
      var promise = P.api("GET", url, undefined, { signal: control.signal }).catch(function (e) {
        if (late) { var error = new Error("The server did not answer in time."); error.kind = "timeout"; throw error; }
        throw e;
      }).finally(function () { clearTimeout(deadline); });
      return { control: control, promise: promise };
    }
    function cancelled(e) { return e.kind === "timeout" || e.name === "AbortError"; }
    function hideLoadNote() { clearTimeout(loadTimer); loadTimer = null; loadNote.remove(); }
    function showLoading() {
      clearTimeout(loadTimer);
      loadTimer = setTimeout(function () {
        if (loaded || total) return;
        loadNote.textContent = "Loading messages…"; loadNote.classList.remove("load-failed");
        feed.prepend(loadNote);
      }, 400);
    }
    function showLoadProblem(e) {
      clearTimeout(loadTimer);
      if (loaded || total) return;
      var again = el("button", { type: "button", class: "btn dialogue-load-retry", text: "Try again" });
      again.addEventListener("click", retryNow);
      loadNote.textContent = (cancelled(e) ? "The server did not answer in time." : e.message) + " Retrying… ";
      loadNote.appendChild(again); loadNote.classList.add("load-failed");
      feed.prepend(loadNote);
    }
    function retryNow() {
      if (busy === epoch && pollCall) pollCall.control.abort();
      else poll();
    }
    function safely(run, it) {
      try { run(it); } catch (e) {
        if (window.console) console.error("Could not show a chat item", it.kind, e);
        append(el("div", { class: "dialogue-stop-marker", role: "status", text: "A message could not be shown: " + e.message }));
      }
    }
    function apply(it) {
      var key = it.kind + ":" + (it.id || it.tool_id);
      if (seen.has(key)) return;
      seen.add(key);
      moved = Date.now();
      if (it.kind === "user") {
        var id = it.client_id || String(it.id || '').split(':')[0];
        if (!expectedClient || it.client_id === expectedClient || !bulk && !catching) {
          beginLive(id, [it.client_id, String(it.id || '').split(':')[0]]);
          if (it.client_id === expectedClient) expectedClient = null;
        }
        wrote = false; sentAt = 0; turnStart = Date.parse(it.ts); markRequest(beforeLive(claim(it) || message("user", it.text, it.ts)), it);
      }
      else if (it.kind === "assistant") {
        wrote = true; lastTool = "";
        var answer = authoritative(it);
        if (it.final) { turns.push({ start: turnStart, end: Date.parse(it.ts) || Date.now(), node: answer }); refreshOutbox(); }
        wait(false);
      }
      else if (it.kind === "thinking") beforeLive(thought(it));
      else if (it.kind === "tool") { tool(it); var currentTool = tools.get(it.id); if (currentTool) beforeLive(currentTool.node); wait(false); }
      else if (it.kind === "tool_result") toolResult(it);
      else if (it.kind === "turn_end") { liveClosed = true; wait(false); }
      else if (it.kind === "turn_unanswered" || it.kind === "turn_error") {
        liveClosed = true; sentAt = 0; wait(false);
        liveParts.forEach(function (node) { node.classList.add("cli-incomplete"); });
        append(stopMarker(it.kind));
      }
      else if (it.kind === "interrupted") { liveClosed = true; liveParts.forEach(function (node) { node.classList.add("cli-incomplete"); }); append(stopMarker(it.kind)); }
      else if (it.kind === "compact") {
        // A compacted chat reads like a new one: the old messages go, a note says why.
        var boundary = key;
        resetLive(); feed.textContent = ""; clearTurnRail(); total = 0; head = 0; pendingResults.clear(); tools.clear(); seen.clear(); seen.add(boundary);
        append(compactMarker());
      }
    }
    function compactMarker() { return el("div", { class: "dialogue-stop-marker dialogue-compact", role: "status", text: "This chat was compacted. Earlier messages are hidden; Claude keeps a summary." }); }
    // Earlier pages are plain history: rendered off-screen, inserted above, with the reader's view held in place.
    var olderHint = el("div", { class: "dialogue-older", role: "status", text: "Loading earlier messages…" });
    function firstEntry() {
      for (var node = feed.firstChild; node; node = node.nextSibling) if (node !== greeting && node !== olderHint) return node;
      return null;
    }
    function nearTop() { return feed.scrollTop < feed.clientHeight * 1.5; }
    function scheduleOlder() {
      clearTimeout(olderTimer); olderTimer = null;
      if (head && !olderBusy) olderTimer = setTimeout(loadOlder, nearTop() ? 0 : 150);
    }
    function loadOlder() {
      olderTimer = null;
      if (!head || olderBusy) return;
      // Background backfill waits while the reader is in the middle of the history, so a moving finger is never nudged.
      if (document.hidden || !feed.getClientRects().length || !nearTop() && feed.scrollHeight - feed.scrollTop - feed.clientHeight > feed.clientHeight) { olderTimer = setTimeout(loadOlder, 800); return; }
      var my = epoch, from = head, url = previewId ? chatUrl(previewId, "/transcript?before=" + from)
        : sessionUrl(selected, "/transcript?before=" + from);
      olderBusy = true;
      timedGet(url, 0).promise.then(function (r) {
        olderBusy = false;
        if (my !== epoch || head !== from) { scheduleOlder(); return; }
        if (!r.file || r.size < from) return;
        prependOlder(r.items || [], typeof r.start === "number" && r.start < from ? r.start : 0);
        scheduleOlder();
      }, function () {
        olderBusy = false;
        if (my === epoch && head === from) olderTimer = setTimeout(loadOlder, 4000); else scheduleOlder();
      });
    }
    function applyOlder(items) {
      var marks = [], done = [], start = NaN;
      items.forEach(function (one) {
        safely(function (it) {
          var key = it.kind + ":" + (it.id || it.tool_id);
          if (seen.has(key)) return;
          seen.add(key);
          if (it.kind === "user") { start = Date.parse(it.ts); marks.push({ node: message("user", it.text, it.ts), item: it }); }
          else if (it.kind === "assistant") { var answer = message("assistant", it.text, it.ts); if (it.final) done.push({ start: start, end: Date.parse(it.ts) || 0, node: answer }); }
          else if (it.kind === "thinking") thought(it);
          else if (it.kind === "tool") tool(it);
          else if (it.kind === "tool_result") toolResult(it);
          else if (it.kind === "interrupted" || it.kind === "turn_unanswered" || it.kind === "turn_error") append(stopMarker(it.kind));
          else if (it.kind === "compact") append(compactMarker());
        }, one);
      });
      return { marks: marks, turns: done };
    }
    function prependOlder(items, start) {
      var ref = firstEntry(), anchor = ref;
      while (anchor && !anchor.getClientRects().length) anchor = anchor.nextSibling;
      var top = anchor ? anchor.getBoundingClientRect().top : 0, frag = document.createDocumentFragment();
      var wasBulk = bulk, wasTool = lastTool, got;
      sink = frag; bulk = true;
      try { got = applyOlder(items); } finally { sink = null; bulk = wasBulk; lastTool = wasTool; }
      head = start;
      feed.insertBefore(frag, ref);
      if (!head) { olderHint.remove(); pendingResults.clear(); }
      if (anchor) {
        var delta = anchor.getBoundingClientRect().top - top;
        if (delta) { feed.scrollTop += delta; lastScrollTop = feed.scrollTop; if (glideExpect !== null) glideExpect = feed.scrollTop; }
      }
      if (got.marks.length) prependMarks(got.marks);
      if (got.turns.length) { turns = got.turns.concat(turns); refreshOutbox(); }
    }
    function notice(text) {
      resetLive(); feed.textContent = ""; clearTurnRail();
      total = 0; head = null;
      append(el("p", { class: "dialogue-note", text: text }));
    }
    function afterSend(next, runtimeIdle) {
      if (!sentAt || Date.now() - sentAt > (P.CodeDialogue.STUCK_MS || 45000)) { sentAt = 0; return next; }
      return next === "idle" || next === "unknown" || runtimeIdle ? "thinking" : next;
    }
    function poll() {
      clearTimeout(timer);
      if (!selected && !previewId) return;
      if (busy === epoch) { again = true; return; }
      again = false;
      var my = epoch, from = cursor, url;
      if (previewId) url = chatUrl(previewId, "/transcript" + (from === null ? "?tail=1" : "?after=" + from));
      else url = sessionUrl(selected, "/transcript?state=" + state + (from === null ? "&tail=1" : "&after=" + from));
      if (pollCall) pollCall.control.abort();
      if (!loaded && !loadTimer && !loadNote.isConnected) showLoading();
      busy = my;
      pollStarted = Date.now();
      var done = function (delay) {
        if (busy === my) busy = null;
        if (my !== epoch) return;
        timer = setTimeout(poll, again ? 0 : delay);
      };
      pollCall = timedGet(url, pollSlow);
      pollCall.promise.then(function (r) {
        if (my !== epoch) { done(0); return; }
        pollSlow = 0;
        var sid = previewId || r.session_id || null;
        if (sid !== chatSid) { chatSid = sid; turns = []; }
        if (!previewId) {
          if (!r.bound) {
            if (bound !== false) notice("Legacy session. Use the Console.");
            bound = false; controls();
            done(document.hidden ? 15000 : 5000);
            return;
          }
          bound = true;
        }
        if (!loaded) arrive();
        loaded = true;
        hideLoadNote();
        var more = false;
        if (r.file) {
          if (starting && total && from !== null && r.size < from) { done(250); return; }
          if (from !== null && r.size < from) { cursor = null; head = null; seen.clear(); tools.clear(); pendingResults.clear(); resetLive(); feed.textContent = ""; clearTurnRail(); total = 0; more = true; }
          else { if (from === null) head = typeof r.start === "number" && r.start > 0 ? r.start : 0; liveBatch = from !== null && !catching || !!expectedClient; bulk = from === null || catching || (r.items || []).length > 3; try { (r.items || []).forEach(function (it) { safely(apply, it); }); } finally { bulk = false; liveBatch = false; } cursor = r.next; if (!previewId) state = afterSend(r.status || state, r.runtime_idle); more = r.next < r.size && r.next > (from || 0); }
          catching = more;
        }
        if (r.file && from === null && head) feed.insertBefore(olderHint, firstEntry());
        controls(); follow();
        if (r.file && from === null) scheduleOlder();
        if (!more && queuedCli) { var pending = queuedCli; queuedCli = null; terminalEvent(pending); }
        done(more ? 0 : delayFor(!!previewId, state, document.hidden));
      }).catch(function (e) {
        if (my === epoch && e.kind === "timeout") pollSlow = Math.min(pollSlow + 1, 3);
        if (my === epoch && cursor === null) showLoadProblem(e);
        if (my === epoch && !previewId && e.status === 404) {
          sentAt = 0; state = "stopped"; wait(false); controls();
          var key = "session-ended:" + selected;
          if (!seen.has(key)) {
            seen.add(key);
            append(el("div", { class: "dialogue-stop-marker", role: "status", text: "The session ended. Reopen this chat to continue with its history." }));
          }
        }
        done(cancelled(e) ? 300 : 4000);
      });
    }
    function fit() {
      if (!input.getClientRects().length) { input.style.height = ""; return; }
      var stick = following && feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70, top = feed.scrollTop;
      input.style.height = "auto";
      var max = parseFloat(getComputedStyle(input).maxHeight) || 200;
      var full = input.scrollHeight;
      input.style.height = Math.min(full, max) + "px";
      input.style.overflowY = full > max ? "auto" : "hidden";
      if (stick) { if (Date.now() >= gliding) feed.scrollTop = feed.scrollHeight; }
      else if (feed.scrollTop !== top) feed.scrollTop = top;
    }
    function submit() {
      if (send.disabled || (!input.value && !files.length)) return;
      if (P.CodeDialogue.packText(draftText()).fold) {
        var original = draftText();
        if (foldText(original, true)) { input.value = ""; persist(); fit(); }
        return;
      }
      var typed = draftText().replace(/^(?:[ \t]*\r?\n)+/, ""), text = withFiles(typed), sent = files;
      if (!typed && !files.length) return;
      var candidate = { _raw: text };
      var error = actions.validate && actions.validate(wire(candidate));
      if (error) {
        if (typed && foldText(typed, true)) { input.value = ""; persist(); fit(); }
        else if (persist()) warn(error);
        return;
      }
      if (!persist()) return;
      warn("");
      if (previewId) {
        var id = previewId;
        input.value = ""; fit(); clearFiles();
        var resumed = message("user", text);
        resumed._clientId = candidate._clientId;
        resumed._sources = sent;
        resumed._attempted = true;
        outbox.push(resumed);
        if (!persist()) { resumed._attempted = false; input.value = typed; fit(); settle(resumed, false); return; }
        settle(resumed, null);
        following = true; follow();
        actions.resume(wire(resumed)).then(function (ok) {
          if (chatSid !== id) return;
          if (!ok) { resumed._attempted = false; settle(resumed, false); persist(); return; }
          resumed.classList.add("message-pending");
          wait(true); state = "thinking"; lastTool = ""; moved = Date.now(); wrote = false;
          if (resumed._clientId) { beginLive(resumed._clientId); expectedClient = resumed._clientId; }
          sentAt = /^\s*\//.test(resumed._raw || "") ? 0 : Date.now();
          showState(); setTimeout(poll, 300);
        }, function () {
          if (chatSid !== id) return;
          warn("Delivery not confirmed. The text is kept; check the history before retrying.");
          resumed._attempted = false; settle(resumed, false); persist();
        });
        return id;
      }
      var node = message("user", text);
      node._clientId = candidate._clientId;
      node._files = sent.length > 0;
      node._sources = sent;
      outbox.push(node);
      input.value = "";
      clearFiles();
      fit();
      following = true; follow();
      deliver(node);
    }
    function doStop() {
      if (stop.disabled) return;
      if (actions.stop()) {
        activity.textContent = "Stopping…";
        setTimeout(poll, 400);
        setTimeout(function () { if (state === "thinking" || state === "running") stuckBox.hidden = false; }, P.CodeDialogue.STOP_MS || 8000);
      }
    }
    input.addEventListener("keydown", function (ev) {
      if (ev.isComposing || ev.keyCode === 229) return;
      if (touch) return;
      if (ev.key === "Enter" && ev.shiftKey && input.paragraphBreak) { ev.preventDefault(); input.paragraphBreak(); }
      else if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit(); }
      else if (ev.key === "Escape") { ev.preventDefault(); doStop(); }
    }, true);
    input.addEventListener("input", function () { fit(); persist(); });
    input.addEventListener("focus", fit);
    window.addEventListener("resize", fit);
    if (!touch) {
      host.addEventListener("keydown", function (ev) {
        if (ev.target === input) return;
        if (ev.key === "/" && !ev.ctrlKey && !ev.metaKey) { ev.preventDefault(); input.focus(); }
        else if (ev.key === "Escape") doStop();
        else if (ev.key === "End") { following = true; follow(); }
      });
      feed.addEventListener("contextmenu", function (ev) {
        var node = ev.target.closest && ev.target.closest(".dialogue-message");
        if (!node || String(window.getSelection())) return;
        ev.preventDefault();
        menu.textContent = "";
        [["Copy text", function () { copy(node.querySelector(".message-body").innerText); }],
         ["Copy as Markdown", function () { copy(node._raw); }],
         ["Quote", function () { input.value = node._raw.split("\n").map(function (l) { return "> " + l; }).join("\n") + "\n\n" + input.value; fit(); input.focus(); }]
        ].forEach(function (item) {
          var b = el("button", { type: "button", role: "menuitem", text: item[0] });
          b.addEventListener("click", function () { menu.hidden = true; item[1](); });
          menu.appendChild(b);
        });
        menu.style.left = Math.min(ev.clientX, window.innerWidth - 220) + "px";
        menu.style.top = Math.min(ev.clientY, window.innerHeight - 130) + "px";
        menu.hidden = false;
        menu.firstChild.focus();
      });
      document.addEventListener("click", function () { menu.hidden = true; });
      document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") menu.hidden = true; });
      window.addEventListener("blur", function () { menu.hidden = true; });
    }
    send.addEventListener("click", submit);
    stop.addEventListener("click", doStop);
    var wake = function () {
      if (document.hidden || !selected && !previewId) return;
      if (busy === epoch && pollCall && Date.now() - pollStarted > (P.CodeDialogue.WAKE_MS || 5000)) pollCall.control.abort();
      else poll();
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    window.addEventListener("pageshow", wake);
    window.addEventListener("online", wake);
    controls();
    return {
      renderMarkdown: function () {
        var top = feed.scrollTop;
        feed.querySelectorAll(".message-assistant").forEach(function (node) {
          if (node._cliPreview) return;
          if (node._handoff) { node._rerenderAfter = true; return; }
          var body = node.querySelector(".message-body");
          body.textContent = "";
          render(body, node._raw);
        });
        if (following) follow();
        else feed.scrollTop = top;
      },
      select: function (name, isClaude, id) {
        allowed = !!isClaude;
        if (selected !== name || previewId) {
          drawAsk(null);
          clearTimeout(askTimer); askTimer = setTimeout(askPoll, 0);
          var preserve = !!id && (chatSid === id || previewId === id);
          if (!preserve && voice) voice.cancel();
          if (!preserve) persist();
          if (selected) drafts.set(selected, input.value);
          var carried = previewId ? input.value : null;
          previewId = null;
          input.placeholder = touch ? "Message " + brand : "Message " + brand + "…";
          selected = name; epoch += 1; if (!connected) since = Date.now();
          if (!preserve) {
            starting = false;
            clearFiles(); wait(false); cursor = null; head = null; pollSlow = 0; hideLoadNote(); state = "unknown"; lastTool = ""; bound = null; sentAt = 0;
            seen.clear(); tools.clear(); pendingResults.clear(); outbox = []; resetLive(); feed.textContent = ""; clearTurnRail(); total = 0; following = true;
            chatSid = id || null; loaded = false; chooseGreeting();
            input.value = carried || drafts.get(name) || "";
            restore();
          }
          fit();
          poll();
        }
        controls();
      },
      preview: function (id) {
        clearTimeout(timer);
        var preserve = id === chatSid || id === previewId;
        if (!preserve && voice) voice.cancel();
        if (!preserve) persist();
        if (selected) drafts.set(selected, input.value);
        selected = null; previewId = id; epoch += 1; allowed = true;
        if (!preserve) {
          starting = false;
          clearFiles(); wait(false); cursor = null; head = null; pollSlow = 0; hideLoadNote(); state = "idle"; lastTool = ""; bound = null; sentAt = 0; outbox = [];
          seen.clear(); tools.clear(); pendingResults.clear(); resetLive(); feed.textContent = ""; clearTurnRail(); total = 0; following = true;
          input.value = ""; chatSid = id; loaded = false; chooseGreeting();
          restore();
        }
        input.placeholder = touch ? "Reply" : "Reply…";
        fit();
        poll();
        controls();
      },
      starting: function (on) { starting = !!on; controls(); },
      connection: function (on) {
        if (!on && connected) since = Date.now();
        connected = on;
        controls();
        if (on && selected) poll();
      },
      hold: function (on) { held = on; controls(); },
      event: terminalEvent
    };
  };
  P.CodeDialogue.packText = function (text) {
    var size = new TextEncoder().encode(text).length;
    return { text: text, fold: size >= 4096 || new TextEncoder().encode(JSON.stringify(text)).length >= 8192, tooBig: size > 5 * 1024 * 1024 };
  };
  // The CLI prints formatted Markdown: no **, # or fences, but it keeps "- " and "1. " markers and hard-wraps at the terminal width.
  // This rebuilds the structure that survives, so the preview already looks like the saved answer.
  var CLI_ITEM = /^(\s*)([-•*]|\d{1,3}[.)])(?:\s+(.*))?$/;
  function softWrapped(line, next, cols) {
    return !!cols && line.length + 1 + next.trim().split(/\s+/)[0].length > cols - 3;
  }
  // Formulas reach the screen without the backslash before punctuation: \{ \\ \, \[ \( become { \ , [ ( and have to be restored.
  var CLI_MATH = /\$\$(?!\s)([^$]{1,2000}?)(?<!\s)\$\$|(?<![\p{L}\p{N}$\\])\$(?![\s$])((?:[^$\\]|\\[^]){1,2000}?)(?<![\s\\])\$(?![\p{L}\p{N}$(\/{\[])/gu;
  var CLI_PAREN = /\(((?:[^()\n]|\([^()\n]*\))+)\)/g;
  var CLI_TEX = /\\(?:d?frac|binom|sqrt|sum|prod|int|oint|lim|log|ln|sin|cos|tan|alpha|beta|gamma|delta|epsilon|varepsilon|theta|lambda|mu|nu|pi|rho|sigma|tau|phi|varphi|psi|omega|Gamma|Delta|Theta|Lambda|Sigma|Phi|Psi|Omega|partial|nabla|infty|cdot|times|pm|leq?|geq?|neq?|approx|equiv|in|to|rightarrow|Rightarrow|vec|hat|bar|dot|overline|mathbb|mathbf|mathrm|text|begin)(?![a-zA-Z])/;
  function cliBraces(tex) {
    var out = "", stack = [], command = false;
    for (var i = 0; i < tex.length; i++) {
      var char = tex.charAt(i);
      if (char === "\\") {
        var name = /^\\(?:[a-zA-Z]+|[^])?/.exec(tex.slice(i))[0];
        out += name; i += name.length - 1; command = /[a-zA-Z]$/.test(name);
        continue;
      }
      if (char === "{") {
        var bare = !command && !/[_^}\]]$/.test(out);
        stack.push(bare); out += bare ? "\\{" : "{";
      } else if (char === "}") out += stack.pop() ? "\\}" : "}";
      else out += char;
      command = false;
    }
    return out;
  }
  function cliTex(tex) {
    return cliBraces(tex.replace(/\s*\n\s*/g, " ")
      .replace(/\\(left|right|[bB]igg?[lrm]?)\s*([{}])/g, "\\$1\\$2")
      .replace(/(^|[^\\])([%#])/g, "$1\\$2")
      .replace(/\s,\s/g, " \\, ")
      .replace(/(\\begin\{[a-zA-Z]+\*?\})([^]*?)(\\end\{)/g, function (all, open, body, close) {
        return open + body.replace(/(?<!\\)\\(?=\s|$)/g, "\\\\") + close;
      }));
  }
  function cliMath(tex, display, raw) {
    return P.CodeMarkdown && P.CodeMarkdown.math ? P.CodeMarkdown.math(cliTex(tex), display, raw, true) : document.createTextNode(raw);
  }
  function cliParens(host, text) {
    var at = 0;
    Array.from(text.matchAll(CLI_PAREN)).forEach(function (match) {
      if (!CLI_TEX.test(match[1]) || /[а-яё]/i.test(match[1].replace(/\\text\{[^}]*\}/g, ""))) return;
      host.append(text.slice(at, match.index), cliMath(match[1], false, match[0]));
      at = match.index + match[0].length;
    });
    host.append(text.slice(at));
  }
  function cliProse(node, text) {
    var at = 0;
    Array.from(text.matchAll(CLI_MATH)).forEach(function (match) {
      cliParens(node, text.slice(at, match.index));
      node.append(cliMath(match[1] || match[2], !!match[1], match[0]));
      at = match.index + match[0].length;
    });
    cliParens(node, text.slice(at));
    return node;
  }
  function openDollar(text) {
    return (text.match(/(?<!\\)\$/g) || []).length % 2 === 1;
  }
  function cliTable(lines) {
    var rows = [], row = null, rules = lines.filter(function (line) { return line.charAt(0) === "├"; }).length;
    lines.forEach(function (line) {
      if (line.charAt(0) !== "│") { row = null; return; }
      var cells = line.slice(1, line.lastIndexOf("│")).split("│").map(function (cell) { return cell.trim(); });
      if (row && (rules > 1 || rows.length === 1)) cells.forEach(function (cell, index) { if (cell) row[index] = (row[index] ? row[index] + " " : "") + cell; });
      else { row = cells; rows.push(row); }
    });
    if (rows.length < 2 || rows.some(function (one) { return one.length !== rows[0].length; })) return null;
    function line(tag, cells) { return el("tr", {}, cells.map(function (cell) { return cliProse(el(tag), cell); })); }
    return el("div", { class: "markdown-table", tabindex: "0" }, [el("table", {}, [
      el("thead", {}, [line("th", rows[0])]),
      el("tbody", {}, rows.slice(1).map(function (one) { return line("td", one); }))
    ])]);
  }
  function cliBlocks(text, cols) {
    var blocks = [], open = null;
    function start(type, extra) { open = Object.assign({ type: type }, extra); blocks.push(open); return open; }
    text.split("\n").forEach(function (line) {
      var plain = line.trim(), item = CLI_ITEM.exec(line), single;
      if (open && open.close) {
        if (plain === open.close) {
          var body = open.lines.slice(1).join("\n");
          open.raw = open.lines.concat(plain).join("\n");
          open.type = open.close === "$$" || CLI_TEX.test(body) ? "math" : "text";
          if (open.type === "math") open.tex = body; else open.lines.push(line);
          delete open.close; open = null;
        } else if (!plain && open.close !== "$$") { open.type = "text"; delete open.close; open = null; }
        else if (plain) open.lines.push(line);
        return;
      }
      if (!plain) { open = null; return; }
      if (plain === "$$" || plain === "[" || plain === "\\[") {
        start("fence", { close: plain === "$$" ? "$$" : plain === "[" ? "]" : "\\]", lines: [line] });
        return;
      }
      if ((single = /^\$\$(.+)\$\$$/.exec(plain) || /^\\?\[\s*(.+?)\s*\\?\]$/.exec(plain)) && (plain.charAt(0) === "$" || CLI_TEX.test(single[1]))) {
        start("math", { tex: single[1], raw: plain }); open = null;
        return;
      }
      if (open && open.type === "text" && openDollar(open.lines[open.lines.length - 1]) && softWrapped(open.lines[open.lines.length - 1], line, cols)) {
        open.lines[open.lines.length - 1] += " " + plain;
        return;
      }
      if (open && open.type === "list" && openDollar(open.items[open.items.length - 1].text) && line.search(/\S/) >= open.items[open.items.length - 1].hang) {
        open.items[open.items.length - 1].text += " " + plain;
        return;
      }
      if (/^[┌│├└╭╰]/.test(plain)) {
        (open && open.type === "box" ? open : start("box", { lines: [] })).lines.push(plain);
      } else if (plain.charAt(0) === "▎") {
        var quote = plain.slice(1).trim();
        if (open && open.type === "quote") open.text += (softWrapped(open.last, quote, cols - 2) ? " " : "\n") + quote;
        else start("quote", { text: quote });
        open.last = quote;
      } else if (/^-{3,}$/.test(plain)) {
        start("rule"); open = null;
      } else if (item) {
        var ordered = /\d/.test(item[2]), indent = item[1].length;
        if (!open || open.type !== "list" || indent === open.indent && ordered !== open.ordered) start("list", { indent: indent, ordered: ordered, first: parseInt(item[2], 10) || 1, items: [] });
        open.items.push({ indent: indent, ordered: ordered, number: parseInt(item[2], 10) || 1, text: item[3] || "", hang: indent + item[2].length + 1 });
      } else if (open && open.type === "list" && line.search(/\S/) >= open.items[open.items.length - 1].hang) {
        var last = open.items[open.items.length - 1];
        last.text += (last.text ? " " : "") + plain;
      } else if (open && open.type === "text") {
        var tail = open.lines[open.lines.length - 1];
        if (softWrapped(tail, line, cols)) open.lines[open.lines.length - 1] = tail + " " + plain;
        else open.lines.push(line);
      } else start("text", { lines: [line] });
    });
    blocks.forEach(function (block, index) {
      if (block.type === "text" && block.lines.length === 1 && index < blocks.length - 1 && block.lines[0].length <= 70 && !/^\s|[.,:;!?)…]$|^\$.*\$$/.test(block.lines[0])) block.heading = true;
    });
    return blocks;
  }
  function cliList(block) {
    var root = el(block.ordered ? "ol" : "ul"), stack = [{ list: root, indent: block.indent }];
    if (block.ordered && block.first > 1) root.setAttribute("start", block.first);
    block.items.forEach(function (item) {
      while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) stack.pop();
      var top = stack[stack.length - 1];
      if (item.indent > top.indent && top.list.lastElementChild) {
        var inner = el(item.ordered ? "ol" : "ul");
        if (item.ordered && item.number > 1) inner.setAttribute("start", item.number);
        top.list.lastElementChild.appendChild(inner);
        top = { list: inner, indent: item.indent }; stack.push(top);
      }
      top.list.appendChild(cliProse(el("li"), item.text));
    });
    return root;
  }
  function cliNode(block) {
    if (block.type === "list") return cliList(block);
    if (block.type === "box") return block.lines.join("").indexOf("$") >= 0 && cliTable(block.lines) || el("pre", { class: "cli-box", text: block.lines.join("\n") });
    if (block.type === "math") return cliMath(block.tex, true, block.raw);
    if (block.type === "quote") return cliProse(el("blockquote"), block.text);
    if (block.type === "rule") return el("hr");
    if (block.heading) return cliProse(el("p", { class: "cli-heading" }), block.lines[0]);
    return cliProse(el("p"), block.lines.join("\n"));
  }
  // Only the block that changed is rebuilt: frames arrive many times a second and everything above the last block is already final.
  function drawCli(flow, text, cols) {
    var blocks = cliBlocks(text, cols);
    blocks.forEach(function (block, index) {
      var key = JSON.stringify(block), node = flow.children[index];
      if (node && node._key === key) return;
      var fresh = cliNode(block);
      fresh._key = key;
      if (node) node.replaceWith(fresh); else flow.appendChild(fresh);
    });
    while (flow.children.length > blocks.length) flow.lastElementChild.remove();
  }
  P.CodeDialogue.cliBlocks = cliBlocks;
  var ATTACHMENTS_SHOWN = 6;
  var DOC_SHAPE = [["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }], ["path", { d: "M14 2v6h6" }]];
  var FILE_KINDS = {
    image: { types: "png jpg jpeg gif webp svg bmp heic heif tif tiff avif", label: "Image", shape: [["rect", { x: "3", y: "3", width: "18", height: "18", rx: "3" }], ["circle", { cx: "8.5", cy: "8.5", r: "1.5" }], ["path", { d: "m21 15-5-5L5 21" }]] },
    pdf: { types: "pdf", label: "PDF", shape: DOC_SHAPE.concat([["path", { d: "M8 13h8M8 17h5" }]]) },
    doc: { types: "doc docx odt rtf pages", label: "Document", shape: DOC_SHAPE.concat([["path", { d: "m8 12 1.5 6 2.5-4 2.5 4 1.5-6" }]]) },
    sheet: { types: "xls xlsx ods csv tsv numbers", label: "Spreadsheet", shape: DOC_SHAPE.concat([["path", { d: "M8 12h8v6H8zM12 12v6M8 15h8" }]]) },
    slides: { types: "ppt pptx odp key", label: "Slides", shape: [["rect", { x: "2", y: "4", width: "20", height: "13", rx: "2" }], ["path", { d: "M12 17v4M8 21h8" }]] },
    archive: { types: "zip rar 7z tar gz tgz bz2 xz", label: "Archive", shape: DOC_SHAPE.concat([["path", { d: "M11 5h.01M11 8h.01M11 11h.01M11 14h.01M11 17h.01" }]]) },
    audio: { types: "mp3 wav ogg oga m4a flac aac opus", label: "Audio", shape: [["path", { d: "M9 18V6l11-2v12" }], ["circle", { cx: "6", cy: "18", r: "3" }], ["circle", { cx: "17", cy: "16", r: "3" }]] },
    video: { types: "mp4 mov mkv webm avi m4v", label: "Video", shape: [["rect", { x: "2", y: "5", width: "14", height: "14", rx: "2" }], ["path", { d: "m16 10 6-3v10l-6-3" }]] },
    code: { types: "py js mjs cjs ts tsx jsx c h cpp hpp cc cs java kt rs go rb php sh bash fish sql html css scss json yaml yml toml xml ipynb tex lua m r", label: "Code", shape: DOC_SHAPE.concat([["path", { d: "m10 13-2 2 2 2M14 13l2 2-2 2" }]]) },
    text: { types: "txt md markdown log ini cfg conf", label: "Text", shape: DOC_SHAPE.concat([["path", { d: "M8 13h8M8 17h8" }]]) }
  };
  function fileKind(name, type) {
    var ext = /\.([a-z0-9]+)$/i.exec(name || "");
    var id = ext && Object.keys(FILE_KINDS).find(function (key) { return (" " + FILE_KINDS[key].types + " ").indexOf(" " + ext[1].toLowerCase() + " ") >= 0; });
    if (!id && type === "image") id = "image";
    return { id: id || "file", shape: id ? FILE_KINDS[id].shape : DOC_SHAPE, label: id && id !== "code" && id !== "text" && id !== "image" ? FILE_KINDS[id].label : ext ? ext[1].toUpperCase() : id ? FILE_KINDS[id].label : "File" };
  }
  function fileTile(className, kind, size) {
    var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    [["viewBox", "0 0 24 24"], ["width", size], ["height", size], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.7"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"]].forEach(function (pair) { svg.setAttribute(pair[0], pair[1]); });
    kind.shape.forEach(function (part) {
      var node = document.createElementNS(NS, part[0]);
      Object.keys(part[1]).forEach(function (key) { node.setAttribute(key, part[1][key]); });
      svg.appendChild(node);
    });
    var tile = el("span", { class: "file-tile " + className, "data-kind": kind.id, "aria-hidden": "true" });
    tile.appendChild(svg);
    return tile;
  }
  function parseAttachments(raw) {
    var str = raw || "";
    var images = [];
    var imgMatch = str.match(/^((?:\s*\[Image #\d+\])+)/);
    if (imgMatch) {
      var tagMatch;
      var tagRe = /\[Image #(\d+)\]/g;
      while ((tagMatch = tagRe.exec(imgMatch[1])) !== null) {
        images.push("Image #" + tagMatch[1]);
      }
      str = str.slice(imgMatch[0].length);
    }
    var list = [];
    var attachedMatch = str.match(/^\s*Attached files \(read them first[^)]*\):[ \t]*([^\r\n]*)/i);
    if (attachedMatch) {
      var pathsLine = attachedMatch[1].trim();
      str = str.slice(attachedMatch[0].length);
      var compMatch = str.match(/^\s*\r?\n\s*Read text attachments completely, continuing in chunks to EOF\.?/i);
      if (compMatch) {
        str = str.slice(compMatch[0].length);
      }
      if (pathsLine) {
        pathsLine.split(/,\s*/).forEach(function (p) {
          var path = p.trim();
          if (!path) return;
          var name = path.split("/").pop() || path;
          var isImg = /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(name);
          list.push({ name: name, path: path, type: isImg ? "image" : "file" });
        });
      }
    }
    if (!list.length && images.length) {
      images.forEach(function (img) {
        list.push({ name: img, path: null, type: "image" });
      });
    } else if (images.length && list.length) {
      var imgFilesCount = list.filter(function (f) { return f.type === "image"; }).length;
      if (images.length > imgFilesCount) {
        images.slice(imgFilesCount).forEach(function (img) {
          list.push({ name: img, path: null, type: "image" });
        });
      }
    }
    if (list.length) {
      str = str.replace(/^[ \t]*\r?\n+/, "");
      if (!attachedMatch) str = str.replace(/^[ \t]+/, "");
    }
    var cleanText = list.length ? str : raw;
    return { files: list, text: cleanText };
  }
  P.CodeDialogue.parseAttachments = parseAttachments;
})();
