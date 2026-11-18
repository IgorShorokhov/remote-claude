(function () {
  var P = window.Portal, el = P.el;
  if (window.marked && !window.marked.__portalMath) {
    window.marked.__portalMath = true;
    window.marked.use({ extensions: [{
      name: "mathBlock", level: "block",
      start: function (src) { var i = src.indexOf("$$"); return i < 0 ? undefined : i; },
      tokenizer: function (src) {
        var m = /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n+|$)/.exec(src);
        if (m && m[1].trim()) return { type: "mathBlock", raw: m[0], text: m[1].trim() };
      }
    }, {
      name: "math", level: "inline",
      start: function (src) { var i = src.indexOf("$"); return i < 0 ? undefined : i; },
      tokenizer: function (src) {
        var m = /^\$\$([\s\S]+?)\$\$/.exec(src);
        if (m && m[1].trim()) return { type: "math", raw: m[0], text: m[1].trim(), display: true };
        m = /^\$(?![\s$])((?:\\.|[^\\$\n])+?)(?<![\s\\])\$(?!\d)/.exec(src);
        if (m) return { type: "math", raw: m[0], text: m[1], display: false };
      }
    }] });
  }
  function math(text, display, raw, strict) {
    var host = el(display ? "div" : "span", { class: display ? "math-block" : "math-inline" });
    if (!window.katex) { host.textContent = raw; return host; }
    try {
      window.katex.render(text, host, { displayMode: display, throwOnError: !!strict, trust: false, strict: "ignore", maxSize: 50, maxExpand: 500, output: "htmlAndMathml" });
      if (display) host.setAttribute("tabindex", "0");
    } catch (e) { host.textContent = raw; }
    return host;
  }
  function inline(host, tokens) {
    (tokens || []).forEach(function (t) {
      var node;
      if (t.type === "strong" || t.type === "em" || t.type === "del") node = el(t.type);
      else if (t.type === "codespan") { host.appendChild(el("code", { text: t.text })); return; }
      else if (t.type === "br") { host.appendChild(el("br")); return; }
      else if (t.type === "math") { host.appendChild(math(t.text, t.display, t.raw)); return; }
      else if (t.type === "link" && /^https?:\/\//i.test(t.href) && !/[\x00-\x20\x7f]/.test(t.href)) {
        node = el("a", { href: t.href, target: "_blank", rel: "noopener noreferrer" });
      }
      if (node) { inline(node, t.tokens || [{ type: "text", text: t.text }]); host.appendChild(node); }
      else if (t.tokens && t.type !== "html") inline(host, t.tokens);
      else host.appendChild(document.createTextNode(t.text || t.raw || ""));
    });
  }
  // What the current P.CodeMarkdown call may do: answer is how a form sends its answers, live marks a reply still being written,
  // nested marks the inside of a card: a card may hold charts, tiles and the like, but no second card and no form.
  var context = { answer: null, live: false, nested: false };
  function widgetBlock(host, t, language) {
    var match = /^ui-([a-z]+)$/.exec(language.toLowerCase());
    if (!match || !P.CodeWidgets || P.CodeWidgets.kinds.indexOf(match[1]) < 0) return false;
    if (context.nested && (match[1] === "card" || match[1] === "form")) return false;
    var info = (t.lang || "").trim().slice(language.length).trim().slice(0, 120);
    var node = P.CodeWidgets.render(match[1], info, String(t.text || ""), {
      answer: context.answer, live: context.live,
      markdown: function (body, source) {
        var outer = context.nested;
        context.nested = true;
        try { blocks(body, window.marked.lexer(source, Object.assign({}, window.marked.defaults, { gfm: true })), 1); }
        finally { context.nested = outer; }
      }
    });
    // A block still being written is not complete yet: show a placeholder instead of its half-written data.
    if (!node && context.live) node = el("div", { class: "md-widget md-pending muted small", text: "Drawing…" });
    if (!node) return false;
    host.appendChild(node);
    return true;
  }
  function codeBlock(host, t) {
    var language = (t.lang || "").split(/\s/)[0].slice(0, 40);
    if (widgetBlock(host, t, language)) return;
    var code = el("code", { text: t.text });
    var copy = el("button", { class: "btn code-copy", type: "button", text: "Copy code" });
    copy.addEventListener("click", function () {
      window.Portal.copyText(t.text).then(function () { copy.textContent = "Copied"; }, function () { copy.textContent = "Select code manually"; });
    });
    if (window.hljs && window.hljs.getLanguage(language)) {
      code.className = "language-" + language;
      window.hljs.highlightElement(code);
    }
    host.appendChild(el("div", { class: "message-code" }, [el("div", { class: "code-caption" }, [el("span", { text: language || "text" }), copy]), el("pre", {}, [code])]));
  }
  function blocks(host, tokens, depth) {
    if (depth > 20) { host.appendChild(el("pre", { text: tokens.map(function (t) { return t.raw; }).join("") })); return; }
    tokens.forEach(function (t) {
      var node;
      if (t.type === "space") return;
      if (t.type === "code") { codeBlock(host, t); return; }
      if (t.type === "mathBlock") { host.appendChild(math(t.text, true, t.raw)); return; }
      if (t.type === "heading") { node = el("h" + Math.max(1, Math.min(6, t.depth))); inline(node, t.tokens); }
      else if (t.type === "paragraph" || t.type === "text") { node = el("p"); inline(node, t.tokens || [{ type: "text", text: t.text }]); }
      else if (t.type === "blockquote") { node = el("blockquote"); blocks(node, t.tokens, depth + 1); }
      else if (t.type === "list") {
        node = el(t.ordered ? "ol" : "ul");
        if (t.ordered && Number.isSafeInteger(t.start)) node.setAttribute("start", t.start);
        t.items.forEach(function (item) { var li = el("li"); blocks(li, item.tokens, depth + 1); node.appendChild(li); });
      } else if (t.type === "table") {
        var table = el("table"), head = el("tr"), tbody = el("tbody");
        t.header.forEach(function (cell) { var th = el("th"); inline(th, cell.tokens); head.appendChild(th); });
        t.rows.forEach(function (row) { var tr = el("tr"); row.forEach(function (cell) { var td = el("td"); inline(td, cell.tokens); tr.appendChild(td); }); tbody.appendChild(tr); });
        table.appendChild(el("thead", {}, [head])); table.appendChild(tbody);
        node = el("div", { class: "markdown-table", tabindex: "0" }, [table]);
      } else if (t.type === "hr") node = el("hr");
      else node = el("p", { class: "message-text", text: t.raw || t.text || "" });
      host.appendChild(node);
    });
  }
  P.CodeMarkdown = function (host, text, options) {
    host.textContent = "";
    text = String(text);
    context = { answer: options && options.answer || null, live: !!(options && options.live), nested: false };
    if (text.length > 32768) {
      host.appendChild(el("div", { class: "muted small", text: "Long response shown as plain text." }));
      host.appendChild(el("div", { class: "message-text", text: text }));
      return;
    }
    try { blocks(host, window.marked.lexer(text, Object.assign({}, window.marked.defaults, { gfm: true })), 0); }
    catch (e) { host.textContent = text; }
  };
  P.CodeMarkdown.math = math;
})();
