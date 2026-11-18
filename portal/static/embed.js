(function () {
  var P = window.Portal;
  P.embedded = window.top !== window && /(?:^|[?&])embed=vscode(?:&|$)/.test(location.search);
  if (!P.embedded) return;
  document.documentElement.classList.add("embed");
  var host = window.parent;
  var context = null;
  var sent = {};

  function tell(message) { try { host.postMessage(message, "*"); } catch (e) {} }

  // The open folder comes from the VS Code extension; the server hands it to the agent with every prompt of this chat.
  function push(force) {
    var chat = P.codeChat && P.codeChat();
    if (!context || !chat) return;
    var body = JSON.stringify(context);
    var last = sent[chat];
    if (!force && last && last.body === body && Date.now() - last.at < 240000) return;
    sent[chat] = { body: body, at: Date.now() };
    P.api("PUT", "/api/chats/" + encodeURIComponent(chat) + "/context", context).catch(function () { delete sent[chat]; });
  }

  // A frame inside a webview may not touch the clipboard, so copy and paste go through VS Code itself.
  var reads = {}, readCount = 0;
  try {
    var board = navigator.clipboard || {};
    Object.defineProperty(board, "writeText", { configurable: true, value: function (text) {
      text = String(text).slice(0, 1000000);
      return new Promise(function (resolve, reject) {
        var id = ++readCount;
        reads[id] = function () { resolve(); };
        tell({ type: "rc-copy", id: id, text: text });
        // Copied only when VS Code confirms it; otherwise try the page's own way and say so if that fails too.
        setTimeout(function () {
          if (!reads[id]) return;
          delete reads[id];
          var area = document.createElement("textarea");
          area.value = text;
          area.className = "embed-copy";
          document.body.appendChild(area);
          area.select();
          var ok = false;
          try { ok = document.execCommand("copy"); } catch (e) {}
          area.remove();
          if (ok) resolve(); else reject(new Error("Clipboard blocked"));
        }, 900);
      });
    } });
    Object.defineProperty(board, "readText", { configurable: true, value: function () {
      return new Promise(function (resolve, reject) {
        var id = ++readCount;
        reads[id] = resolve;
        tell({ type: "rc-read", id: id });
        setTimeout(function () { if (reads[id]) { delete reads[id]; reject(new Error("Clipboard did not answer")); } }, 3000);
      });
    } });
    if (!navigator.clipboard) Object.defineProperty(navigator, "clipboard", { configurable: true, value: board });
  } catch (e) {}

  window.addEventListener("message", function (ev) {
    var data = ev.data;
    if (ev.source === host && data && (data.type === "rc-clipboard" || data.type === "rc-copied") && reads[data.id]) {
      reads[data.id](typeof data.text === "string" ? data.text : "");
      delete reads[data.id];
      return;
    }
    if (ev.source !== host || !data || typeof data !== "object" || data.type !== "rc-context" || !Array.isArray(data.folders)) return;
    context = {
      folders: data.folders.slice(0, 5).filter(function (f) { return f && typeof f.path === "string" && f.path; })
        .map(function (f) { return { path: f.path.slice(0, 1024), name: typeof f.name === "string" ? f.name.slice(0, 120) : "" }; }),
      host: typeof data.host === "string" ? data.host.slice(0, 60) : ""
    };
    push(true);
  });

  setInterval(push, 2000);
  window.addEventListener("message", function (ev) {
    if (ev.origin === location.origin && ev.data && ev.data.type === "portal-auth-expired") tell({ type: "rc-auth-expired" });
  });
  tell({ type: "rc-ready" });
})();
