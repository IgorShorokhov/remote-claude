(function () {
  var P = window.Portal;
  P.input.mobileDesktop = function (id, frame, canvas, section) {
    var input = null;
    var composing = false;
    var committed = null;
    var status = P.el("p", { class: "desk-hint", role: "status" });
    function ready() {
      try { return !section.hidden && frame.contentDocument.documentElement.classList.contains("noVNC_connected") && canvas(); }
      catch (e) { return false; }
    }
    function key(key, code, shift) {
      var target = ready();
      if (!target) return false;
      var win = target.ownerDocument.defaultView;
      function emit(type, value, name) {
        target.dispatchEvent(new win.KeyboardEvent(type, { key: value, code: name, shiftKey: !!shift, bubbles: true, cancelable: true }));
      }
      if (shift) emit("keydown", "Shift", "ShiftLeft");
      try { emit("keydown", key, code || "Unidentified"); emit("keyup", key, code || "Unidentified"); }
      finally { if (shift) emit("keyup", "Shift", "ShiftLeft"); }
      return true;
    }
    function valid(value) {
      return value.length <= 4096 && !/[\u0000-\u0008\u000b-\u001f\u007f\uD800-\uDFFF]/.test(value);
    }
    function type(value) {
      value = value.replace(/\r\n?/g, "\n");
      if (!valid(value)) { status.textContent = "Max 4096 chars, no emoji. Nothing sent."; return false; }
      for (var char of value) {
        if (!key(char === "\n" ? "Enter" : char === "\t" ? "Tab" : char, char === "\n" ? "Enter" : char === "\t" ? "Tab" : "Unidentified", char === "\n")) return false;
      }
      return true;
    }
    function bind() {
      input = null;
      composing = false;
      try {
        var doc = frame.contentDocument;
        var container = doc.querySelector("#noVNC_container");
        if (!container) return;
        input = doc.createElement("textarea");
        input.className = "portal-keyboard-input";
        input.setAttribute("aria-label", "Remote keyboard");
        input.setAttribute("autocapitalize", "off");
        input.setAttribute("autocomplete", "off");
        input.setAttribute("autocorrect", "off");
        input.setAttribute("inputmode", "text");
        input.setAttribute("enterkeyhint", "enter");
        input.spellcheck = false;
        input.tabIndex = -1;
        container.appendChild(input);
        input.addEventListener("keydown", function (ev) {
          ev.stopImmediatePropagation();
          if (ev.isComposing || composing || ev.keyCode === 229) return;
          if (/^(Backspace|Delete|Enter|Tab|Escape|ArrowLeft|ArrowRight|ArrowUp|ArrowDown)$/.test(ev.key)) {
            ev.preventDefault();
            key(ev.key, ev.code || ev.key, ev.shiftKey);
          }
        }, true);
        input.addEventListener("keyup", function (ev) { ev.stopImmediatePropagation(); }, true);
        input.addEventListener("compositionstart", function () { composing = true; committed = null; });
        input.addEventListener("compositionend", function () {
          composing = false;
          committed = input.value;
          type(committed);
          input.value = "";
          setTimeout(function () { committed = null; }, 0);
        });
        input.addEventListener("beforeinput", function (ev) {
          if (ev.isComposing || composing) return;
          if (ev.inputType === "deleteContentBackward" || ev.inputType === "deleteContentForward" || ev.inputType === "insertLineBreak" || ev.inputType === "insertParagraph") {
            ev.preventDefault();
            key(ev.inputType === "deleteContentBackward" ? "Backspace" : ev.inputType === "deleteContentForward" ? "Delete" : "Enter", ev.inputType === "deleteContentBackward" ? "Backspace" : ev.inputType === "deleteContentForward" ? "Delete" : "Enter");
          }
        });
        input.addEventListener("input", function (ev) {
          if (composing || ev.isComposing) return;
          if (committed !== null && input.value === committed) {
            committed = null;
            input.value = "";
            return;
          }
          committed = null;
          type(input.value);
          input.value = "";
        });
      } catch (e) { status.textContent = "Keyboard unavailable. Reconnect."; }
    }
    frame.addEventListener("load", bind);
    var keyboard = P.el("button", { class: "btn", type: "button", text: "Keyboard" });
    var outgoing = P.el("textarea", { id: id + "-mobile-text", rows: "3", class: "mobile-text-field", spellcheck: "false", autocomplete: "off", autocapitalize: "off", maxlength: "4096" });
    var send = P.el("button", { class: "btn", type: "button", text: "Send text" });
    var stop = P.el("button", { class: "btn", type: "button", text: "Stop typing", disabled: "disabled" });
    var sending = false;
    var generation = 0;
    function cancelSend() {
      generation++;
      if (sending) status.textContent = "Stopped. Some text may have arrived.";
      sending = false;
      send.disabled = false;
      keyboard.disabled = false;
      stop.disabled = true;
      outgoing.readOnly = false;
    }
    frame.addEventListener("load", cancelSend);
    new MutationObserver(function () {
      if (section.hidden) { cancelSend(); if (input) input.blur(); }
    }).observe(section, { attributes: true, attributeFilter: ["hidden"] });
    document.addEventListener("visibilitychange", function () { if (document.hidden) cancelSend(); });
    stop.addEventListener("click", cancelSend);
    send.addEventListener("click", async function () {
      if (sending) return;
      var value = outgoing.value.replace(/\r\n?/g, "\n");
      if (!value) { status.textContent = "Enter text first."; return; }
      if (!valid(value) || value.includes("\t")) { status.textContent = "Max 4096 chars, no emoji or tabs. Nothing sent."; return; }
      var target = ready();
      if (!target) { status.textContent = "Not connected. Reconnect."; return; }
      var current = ++generation;
      var doc = target.ownerDocument;
      sending = true;
      send.disabled = true;
      keyboard.disabled = true;
      stop.disabled = false;
      outgoing.readOnly = true;
      target.focus({ preventScroll: true });
      status.textContent = "Typing… don't touch the remote field.";
      try {
        for (var char of value) {
          if (current !== generation) return;
          if (document.hidden || !ready() || canvas().ownerDocument !== doc || !type(char)) {
            cancelSend();
            return;
          }
          await new Promise(function (resolve) { setTimeout(resolve, 30); });
        }
        if (current !== generation) return;
        status.textContent = "Sent: " + value.length + " chars. No Enter sent.";
      } catch (e) {
        status.textContent = "Typing error. Some text may have arrived.";
      } finally {
        if (current === generation) { sending = false; send.disabled = false; keyboard.disabled = false; stop.disabled = true; outgoing.readOnly = false; }
      }
    });
    keyboard.addEventListener("click", function () {
      var target = ready();
      if (!target || !input) { status.textContent = "Not connected. Reconnect."; return; }
      target.focus({ preventScroll: true });
      input.focus({ preventScroll: true });
      status.textContent = "Typing on.";
    });
    status.classList.add("mobile-text-status");
    var body = P.el("div", { class: "mobile-text-body" }, [
      P.el("div", { class: "field-label" }, [P.el("label", { for: id + "-mobile-text", text: "Send text" }), P.tip("Pick the remote field in Mouse mode first. Up to 4096 chars.")]), outgoing,
      P.el("div", { class: "row" }, [send, stop]), status
    ]);
    var details = P.el("details", { class: "mobile-text-panel" });
    var summary = P.el("summary", { class: "mobile-text-summary" });
    summary.appendChild(P.el("span", { text: "✍ Text input" }));
    var stateKey = "portal.textPanel.open";
    var saved = null;
    try { saved = window.localStorage.getItem(stateKey); } catch (e) { saved = null; }
    if (saved === "1" || saved === "0") details.open = saved === "1";
    else details.open = window.innerWidth < 900;
    details.addEventListener("toggle", function () {
      try { window.localStorage.setItem(stateKey, details.open ? "1" : "0"); } catch (e) { }
      if (!details.open && input) input.blur();
    });
    details.appendChild(summary);
    details.appendChild(body);
    return { keyboard: keyboard, panel: details };
  };
})();
