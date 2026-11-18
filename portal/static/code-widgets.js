(function () {
  // Visual blocks of a chat reply: the agent writes a fenced block (ui-chart, ui-stats, ui-flow ...) and the page draws it.
  // The block holds data only, never code: every text goes in as text and every number is checked, so a bad block cannot run anything.
  var P = window.Portal, el = P.el;
  var SVG = "http://www.w3.org/2000/svg";
  var TONES = ["blue", "purple", "green", "amber", "red", "gray"];
  var TONE_RE = /^\[(blue|purple|green|amber|red|gray)\]\s*(.+)$/i;
  var ICON_RE = /^([^\p{L}\p{N}\s\[|]{1,4})\s+(.+)$/u;
  var NUMBER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  var formCount = 0;

  function svg(tag, attrs, children) {
    var node = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key];
      else node.setAttribute(key, String(attrs[key]));
    });
    (children || []).forEach(function (child) { node.appendChild(child); });
    return node;
  }
  function clean(value, max) { return typeof value === "string" ? value.trim().slice(0, max) : ""; }
  function finite(value) { return typeof value === "number" && isFinite(value); }
  function parse(source) {
    try { var data = JSON.parse(source); return data && typeof data === "object" && !Array.isArray(data) ? data : null; }
    catch (e) { return null; }
  }
  function toneOf(value, fallback) { return TONES.indexOf(value) >= 0 ? value : fallback; }
  function section(kind, title, note, children) {
    var head = [];
    if (title) head.push(el("div", { class: "md-widget-title", text: title }));
    if (note) head.push(el("p", { class: "md-widget-note", text: note }));
    return el("section", { class: "md-widget " + kind }, head.concat(children));
  }
  // Line-based blocks: one item per line, a list marker or a leading [colour] tag is allowed, "|" separates the parts of a line.
  function lines(source, max) {
    var rows = source.split("\n").map(function (row) { return row.trim().replace(/^(?:[-*]|\d+[.)])\s+/, ""); }).filter(Boolean);
    return rows.length >= 1 && rows.length <= max ? rows : null;
  }
  function tagged(row) { var match = TONE_RE.exec(row); return match ? { tone: match[1].toLowerCase(), text: match[2] } : { tone: "", text: row }; }
  function parts(text) { return text.split(/\s*\|\s*/); }
  function number(text) {
    var plain = String(text).replace(/[\s ]/g, "").replace(",", ".");
    return plain && finite(Number(plain)) ? Number(plain) : null;
  }
  // The widths and positions of bars come in as CSS variables set through the style object, so the page needs no inline style text.
  function shown(node, name, value) { node.style.setProperty(name, value); return node; }

  // --- chart ----------------------------------------------------------------------------------------------------------------
  function niceNumber(range, round) {
    var exponent = Math.floor(Math.log10(range)), fraction = range / Math.pow(10, exponent), nice;
    if (round) nice = fraction < 1.5 ? 1 : fraction < 3 ? 2 : fraction < 7 ? 5 : 10;
    else nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return nice * Math.pow(10, exponent);
  }
  function scale(low, high) {
    if (!(high > low)) { var pad = Math.abs(low) || 1; low -= pad / 2; high += pad / 2; }
    var step = niceNumber(niceNumber(high - low, false) / 4, true), ticks = [];
    var start = Math.floor(low / step) * step, end = Math.ceil(high / step) * step;
    for (var value = start; value <= end + step / 2 && ticks.length < 20; value += step) ticks.push(Number(value.toPrecision(12)));
    return { low: ticks[0], high: ticks[ticks.length - 1], ticks: ticks };
  }
  function legend(names) {
    return el("div", { class: "md-legend" }, names.map(function (name, index) {
      return el("span", { class: "md-legend-item" }, [el("span", { class: "md-sw s" + (index % 6) }), el("span", { text: name })]);
    }));
  }
  function pie(info, data) {
    var items = Array.isArray(data.items) ? data.items : null;
    if (!items || items.length < 2 || items.length > 12) return null;
    var slices = [], total = 0;
    for (var i = 0; i < items.length; i++) {
      var item = items[i], name = item && clean(item.name, 40);
      if (!name || !finite(item.value) || !(item.value > 0)) return null;
      slices.push({ name: name, value: item.value });
      total += item.value;
    }
    var size = 320, center = size / 2, outer = 148, inner = data.type === "donut" ? 84 : 0, angle = -Math.PI / 2;
    var draw = svg("svg", { viewBox: "0 0 " + size + " " + size, class: "md-chart-svg md-pie-svg", role: "presentation", focusable: "false" });
    function at(radius, theta) { return (center + radius * Math.cos(theta)).toFixed(2) + " " + (center + radius * Math.sin(theta)).toFixed(2); }
    slices.forEach(function (slice, index) {
      var span = slice.value / total * Math.PI * 2, from = angle, to = angle + span, large = span > Math.PI ? 1 : 0;
      angle = to;
      var path = inner
        ? "M" + at(outer, from) + " A" + outer + " " + outer + " 0 " + large + " 1 " + at(outer, to) + " L" + at(inner, to) + " A" + inner + " " + inner + " 0 " + large + " 0 " + at(inner, from) + " Z"
        : "M" + center + " " + center + " L" + at(outer, from) + " A" + outer + " " + outer + " 0 " + large + " 1 " + at(outer, to) + " Z";
      draw.appendChild(svg("path", { d: path, class: "md-slice s" + (index % 6) }, [svg("title", { text: slice.name + ": " + NUMBER.format(slice.value) + " (" + Math.round(slice.value / total * 100) + "%)" })]));
    });
    var title = clean(data.title, 120) || info, names = slices.map(function (slice) { return slice.name + " " + Math.round(slice.value / total * 100) + "%"; });
    var node = section("md-chart md-pie", title, clean(data.note, 300), [draw, legend(names)]);
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", (title || "Chart") + ": " + slices.map(function (slice) { return slice.name; }).join(", "));
    return node;
  }
  function chart(info, source) {
    var data = parse(source);
    if (!data) return null;
    var type = data.type === undefined ? "line" : data.type;
    if (type === "pie" || type === "donut") return pie(info, data);
    if (type !== "line" && type !== "bar") return null;
    if (!Array.isArray(data.x) || data.x.length < 1 || data.x.length > 60 || !Array.isArray(data.series) || data.series.length < 1 || data.series.length > 6) return null;
    var unit = clean(data.unit, 12);
    function format(value) { return NUMBER.format(value) + (unit ? " " + unit : ""); }
    var labels = data.x.map(function (value) {
      return finite(value) ? NUMBER.format(value) : typeof value === "string" ? value.slice(0, 40) : null;
    });
    if (labels.indexOf(null) >= 0) return null;
    var count = labels.length, rows = [];
    for (var i = 0; i < data.series.length; i++) {
      var one = data.series[i];
      if (!one || !Array.isArray(one.y) || one.y.length !== count || !one.y.every(finite)) return null;
      rows.push({ name: clean(one.name, 40) || "Series " + (i + 1), y: one.y });
    }
    var W = 640, H = 320, R = 16, T = 24, B = 44;
    var all = [].concat.apply([], rows.map(function (row) { return row.y; }));
    var low = Math.min.apply(null, all), high = Math.max.apply(null, all);
    if (type === "bar") { low = Math.min(low, 0); high = Math.max(high, 0); }
    else if (low >= 0 && low <= high * 0.6) low = 0;
    var axis = scale(low, high);
    var L = Math.max(44, 16 + 9.5 * Math.max.apply(null, axis.ticks.map(function (tick) { return format(tick).length; }))), plotW = W - L - R, plotH = H - T - B;
    function py(value) { return T + plotH - (value - axis.low) / (axis.high - axis.low) * plotH; }
    function px(index) { return type === "bar" ? L + plotW / count * (index + 0.5) : count === 1 ? L + plotW / 2 : L + plotW * index / (count - 1); }

    var draw = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "md-chart-svg", role: "presentation", focusable: "false" });
    axis.ticks.forEach(function (tick) {
      var y = py(tick);
      draw.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: tick === 0 ? "md-axis" : "md-grid" }));
      draw.appendChild(svg("text", { x: L - 8, y: y + 4, class: "md-tick", "text-anchor": "end", text: format(tick) }));
    });
    var every = Math.max(1, Math.ceil(count / 10));
    labels.forEach(function (label, index) {
      if (index % every) return;
      draw.appendChild(svg("text", { x: px(index), y: H - B + 18, class: "md-tick", "text-anchor": "middle", text: label.length > 12 ? label.slice(0, 11) + "…" : label }));
    });
    draw.appendChild(svg("line", { x1: L, x2: W - R, y1: py(axis.low), y2: py(axis.low), class: "md-axis" }));
    var yLabel = clean(data.yLabel, 40), xLabel = clean(data.xLabel, 40);
    if (yLabel) draw.appendChild(svg("text", { x: L, y: 12, class: "md-axis-label", "text-anchor": "start", text: yLabel }));
    if (xLabel) draw.appendChild(svg("text", { x: L + plotW / 2, y: H - 6, class: "md-axis-label", "text-anchor": "middle", text: xLabel }));
    rows.forEach(function (row, index) {
      var tip = function (at) { return svg("title", { text: row.name + " · " + labels[at] + ": " + format(row.y[at]) }); };
      if (type === "line") {
        draw.appendChild(svg("polyline", { points: row.y.map(function (value, at) { return px(at) + "," + py(value); }).join(" "), class: "md-line s" + index }));
        row.y.forEach(function (value, at) { draw.appendChild(svg("circle", { cx: px(at), cy: py(value), r: 3.5, class: "md-dot s" + index }, [tip(at)])); });
        return;
      }
      var band = plotW / count, group = band * 0.7, width = group / rows.length;
      row.y.forEach(function (value, at) {
        var top = py(Math.max(value, 0)), height = Math.max(1, Math.abs(py(value) - py(0)));
        draw.appendChild(svg("rect", { x: L + band * at + (band - group) / 2 + width * index, y: top, width: Math.max(1, width - 2), height: height, rx: 3, class: "md-bar s" + index }, [tip(at)]));
      });
    });
    var title = clean(data.title, 120) || info, kids = [draw];
    if (rows.length > 1) kids.push(legend(rows.map(function (row) { return row.name; })));
    var node = section("md-chart", title, clean(data.note, 300), kids);
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", (title || "Chart") + ": " + rows.map(function (row) { return row.name; }).join(", "));
    return node;
  }

  // --- stats: tiles with a big value, a label and a status badge --------------------------------------------------------------
  function stats(info, source) {
    var rows = lines(source, 12), grid = el("div", { class: "md-stats" });
    if (!rows) return null;
    rows.forEach(function (row) {
      var mark = tagged(row), icon = ICON_RE.exec(mark.text), cells;
      if (icon) mark.text = icon[2];
      cells = parts(mark.text);
      if (!cells[0]) return;
      var fg = mark.tone ? " fg-" + mark.tone : "";
      grid.appendChild(el("div", { class: "md-stat" }, [
        icon ? el("div", { class: "md-stat-icon" + fg, text: icon[1] }) : null,
        el("div", { class: "md-stat-value", text: cells[0].slice(0, 40) }),
        cells[1] ? el("div", { class: "md-stat-label", text: cells[1].slice(0, 80) }) : null,
        cells[2] ? el("span", { class: "md-badge tone-" + (mark.tone || "gray") + fg, text: cells[2].slice(0, 40) }) : null
      ]));
    });
    return grid.children.length ? section("md-statsbox", info, "", [grid]) : null;
  }

  // --- progress: labelled bars ----------------------------------------------------------------------------------------------
  function progress(info, source) {
    var rows = lines(source, 15), list = el("div", { class: "md-progress-list" });
    if (!rows) return null;
    for (var i = 0; i < rows.length; i++) {
      var mark = tagged(rows[i]), cells = parts(mark.text), match = /^(-?\d+(?:[.,]\d+)?)\s*(?:\/\s*(-?\d+(?:[.,]\d+)?))?\s*%?$/.exec(cells[1] || "");
      if (!cells[0] || !match) return null;
      var value = number(match[1]), max = match[2] ? number(match[2]) : 100;
      if (value === null || !(max > 0)) return null;
      var percent = Math.max(0, Math.min(100, value / max * 100));
      list.appendChild(el("div", { class: "md-progress" }, [
        el("div", { class: "md-progress-head" }, [
          el("span", { text: cells[0].slice(0, 120) }),
          el("span", { class: "muted", text: match[2] ? NUMBER.format(value) + " / " + NUMBER.format(max) : NUMBER.format(value) + "%" })
        ]),
        el("div", { class: "md-track" }, [shown(el("div", { class: "md-fill" + (mark.tone ? " fg-" + mark.tone : "") }), "--w", percent + "%")])
      ]));
    }
    return section("md-progressbox", info, "", [list]);
  }

  // --- timeline -------------------------------------------------------------------------------------------------------------
  function timeline(info, source) {
    var rows = lines(source, 20), list = el("ol", { class: "md-timeline" });
    if (!rows) return null;
    rows.forEach(function (row, index) {
      var cells = parts(tagged(row).text);
      list.appendChild(el("li", { class: "md-tl-item" }, [
        el("span", { class: "md-tl-n", text: String(index + 1) }),
        el("div", { class: "md-tl-body" }, [
          el("div", { class: "md-tl-title", text: cells[0].slice(0, 120) }),
          cells[1] ? el("div", { class: "md-tl-desc", text: cells[1].slice(0, 200) }) : null
        ])
      ]));
    });
    return section("md-timelinebox", info, "", [list]);
  }

  // --- tiles: coloured boxes with a coloured title ---------------------------------------------------------------------------
  function tiles(info, source) {
    var rows = lines(source, 12), grid = el("div", { class: "md-tiles" });
    if (!rows) return null;
    rows.forEach(function (row, index) {
      var mark = tagged(row), cells = parts(mark.text), tone = mark.tone || TONES[index % 4];
      grid.appendChild(el("div", { class: "md-tile tone-" + tone }, [
        el("div", { class: "md-tile-title fg-" + tone, text: cells[0].slice(0, 80) }),
        cells[1] ? el("div", { class: "md-tile-desc", text: cells[1].slice(0, 200) }) : null
      ]));
    });
    return section("md-tilesbox", info, "", [grid]);
  }

  // --- funnel: bars that narrow with the value --------------------------------------------------------------------------------
  function funnel(info, source) {
    var rows = lines(source, 10), list = el("div", { class: "md-funnel" }), items = [];
    if (!rows) return null;
    for (var i = 0; i < rows.length; i++) {
      var mark = tagged(rows[i]), cells = parts(mark.text), value = number(cells[1] || "");
      if (!cells[0] || value === null || value < 0) return null;
      items.push({ name: cells[0].slice(0, 80), text: cells[1].slice(0, 40), value: value, tone: mark.tone || ["gray", "blue", "purple", "green", "amber", "red"][i % 6] });
    }
    var top = Math.max.apply(null, items.map(function (item) { return item.value; }));
    items.forEach(function (item) {
      list.appendChild(shown(el("div", { class: "md-funnel-bar tone-" + item.tone }, [
        el("div", { class: "md-funnel-name", text: item.name }), el("div", { class: "md-funnel-value", text: item.text })
      ]), "--w", (top > 0 ? Math.max(34, item.value / top * 100) : 100) + "%"));
    });
    return section("md-funnelbox", info, "", [list]);
  }

  // --- flow: boxes joined by arrows; a row may hold several boxes, a box a subtitle --------------------------------------------
  function arrow() {
    return el("li", { class: "md-arrow", "aria-hidden": "true" }, [svg("svg", { viewBox: "0 0 16 28", width: 16, height: 28, focusable: "false" }, [
      svg("line", { x1: 8, y1: 1, x2: 8, y2: 21 }), svg("polygon", { points: "3,18 8,27 13,18" })])]);
  }
  function flow(info, source) {
    var rows = lines(source, 24), list = el("ol", { class: "md-flow-list" });
    if (!rows) return null;
    rows.forEach(function (row, index) {
      var boxes = row.split(/\s*\|\|\s*/).slice(0, 4).map(function (text) {
        var mark = tagged(text), cells = parts(mark.text), tone = mark.tone || TONES[index % 4];
        if (!cells[1]) return el("div", { class: "md-step-box tone-" + tone, text: cells[0].slice(0, 160) });
        return el("div", { class: "md-step-box tone-" + tone }, [
          el("div", { class: "md-step-title", text: cells[0].slice(0, 100) }), el("div", { class: "md-step-sub", text: cells[1].slice(0, 160) })]);
      });
      if (index) list.appendChild(arrow());
      list.appendChild(el("li", { class: "md-step" + (boxes.length > 1 ? " md-step-wide" : "") }, [el("div", { class: "md-step-row" }, boxes)]));
    });
    return section("md-flow", info, "", [list]);
  }

  // --- card -----------------------------------------------------------------------------------------------------------------
  function card(info, source, context) {
    if (!context.markdown || !source.trim()) return null;
    var body = el("div", { class: "md-card-body" });
    context.markdown(body, source);
    return section("md-card", info, "", [body]);
  }

  // --- matrix: a table whose cells are coloured by value (correlations, scores) -------------------------------------------------
  function matrix(info, source) {
    var data = parse(source);
    if (!data || !Array.isArray(data.cols) || data.cols.length < 1 || data.cols.length > 10 || !Array.isArray(data.rows) || data.rows.length < 1 || data.rows.length > 12) return null;
    var cols = data.cols.map(function (col) { return clean(col, 40); }), rows = [], all = [];
    if (cols.some(function (col) { return !col; })) return null;
    for (var i = 0; i < data.rows.length; i++) {
      var row = data.rows[i], label = row && clean(row.label, 40);
      if (!label || !Array.isArray(row.values) || row.values.length !== cols.length || !row.values.every(finite)) return null;
      rows.push({ label: label, values: row.values });
      all = all.concat(row.values);
    }
    var digits = Number.isInteger(data.decimals) && data.decimals >= 0 && data.decimals <= 4 ? data.decimals : 2;
    var fmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
    var low = Math.min.apply(null, all), high = Math.max.apply(null, all), limit = Math.max(Math.abs(low), Math.abs(high)) || 1;
    function heat(value) {
      var t = low < 0 ? value / limit : high > low ? (value - low) / (high - low) : 1;
      return t >= 0.7 ? "heat-g" : t >= 0.2 ? "heat-a" : t > -0.2 ? "heat-n" : t > -0.7 ? "heat-o" : "heat-r";
    }
    var head = el("tr", {}, [el("th", {})].concat(cols.map(function (col) { return el("th", { text: col }); })));
    var body = el("tbody", {}, rows.map(function (row) {
      return el("tr", {}, [el("th", { text: row.label })].concat(row.values.map(function (value) {
        return el("td", {}, [el("span", { class: "md-cell " + heat(value), text: fmt.format(value) })]);
      })));
    }));
    var wrap = el("div", { class: "md-matrix-wrap", tabindex: "0" }, [el("table", { class: "md-matrix" }, [el("thead", {}, [head]), body])]);
    return section("md-matrixbox", clean(data.title, 120) || info, clean(data.note, 300), [wrap]);
  }

  // --- gantt: tasks as bars on a shared axis ---------------------------------------------------------------------------------
  function gantt(info, source) {
    var data = parse(source);
    if (!data || !finite(data.from) || !finite(data.to) || !(data.to > data.from) || !Array.isArray(data.tasks) || data.tasks.length < 1 || data.tasks.length > 15) return null;
    var unit = clean(data.unit, 20), span = data.to - data.from, list = el("div", { class: "md-gantt" });
    function label(value) { return (unit ? unit + " " : "") + NUMBER.format(value); }
    for (var i = 0; i < data.tasks.length; i++) {
      var task = data.tasks[i], name = task && clean(task.name, 80);
      if (!name || !finite(task.start) || !finite(task.end) || task.end < task.start) return null;
      var left = Math.max(0, Math.min(100, (task.start - data.from) / span * 100)), width = Math.max(2, Math.min(100 - left, (task.end - task.start) / span * 100));
      var bar = shown(shown(el("div", { class: "md-gantt-bar fg-" + toneOf(task.tone, "blue"), title: name + ": " + label(task.start) + " → " + label(task.end) }), "--a", left + "%"), "--b", width + "%");
      list.appendChild(el("div", { class: "md-gantt-task" }, [el("div", { class: "md-gantt-name", text: name }), el("div", { class: "md-track" }, [bar])]));
    }
    list.appendChild(el("div", { class: "md-gantt-axis muted" }, [el("span", { text: label(data.from) }), el("span", { text: label(data.to) })]));
    return section("md-ganttbox", clean(data.title, 120) || info, clean(data.note, 300), [list]);
  }

  // --- diagram: free drawing of boxes, circles, lines, arrows, dots and text (coordinate systems, schemes) ----------------------
  function diagram(info, source) {
    var data = parse(source);
    if (!data || !Array.isArray(data.items) || data.items.length < 1 || data.items.length > 80) return null;
    var W = finite(data.width) ? Math.max(200, Math.min(800, data.width)) : 480, H = finite(data.height) ? Math.max(100, Math.min(600, data.height)) : 320;
    var draw = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "md-chart-svg md-diagram-svg", role: "presentation", focusable: "false" });
    function pos(item, key) { var value = item[key]; return finite(value) && value >= -100 && value <= 1000 ? value : null; }
    function ok() { for (var k = 0; k < arguments.length; k++) if (arguments[k] === null) return false; return true; }
    var anchors = { start: "start", middle: "middle", end: "end" };
    for (var i = 0; i < data.items.length; i++) {
      var item = data.items[i], kind = item && item.t, tone = toneOf(item && item.tone, ""), fg = tone ? " fg-" + tone : "";
      if (kind === "box") {
        var x = pos(item, "x"), y = pos(item, "y"), w = pos(item, "w"), h = pos(item, "h");
        if (!ok(x, y, w, h) || w <= 0 || h <= 0) return null;
        draw.appendChild(svg("rect", { x: x, y: y, width: w, height: h, rx: 10, class: "md-d-box tone-" + (tone || "gray") }));
        var text = clean(item.text, 60), sub = clean(item.sub, 60);
        if (text) draw.appendChild(svg("text", { x: x + w / 2, y: y + h / 2 + (sub ? -3 : 5), class: "md-d-text", "text-anchor": "middle", text: text }));
        if (sub) draw.appendChild(svg("text", { x: x + w / 2, y: y + h / 2 + 15, class: "md-d-sub", "text-anchor": "middle", text: sub }));
      } else if (kind === "circle") {
        var cx = pos(item, "x"), cy = pos(item, "y"), r = pos(item, "r");
        if (!ok(cx, cy, r) || r <= 0 || r > 300) return null;
        draw.appendChild(svg("circle", { cx: cx, cy: cy, r: r, class: "md-d-box tone-" + (tone || "gray") }));
        if (clean(item.text, 60)) draw.appendChild(svg("text", { x: cx, y: cy + 5, class: "md-d-text", "text-anchor": "middle", text: clean(item.text, 60) }));
      } else if (kind === "line" || kind === "arrow") {
        var x1 = pos(item, "x1"), y1 = pos(item, "y1"), x2 = pos(item, "x2"), y2 = pos(item, "y2");
        if (!ok(x1, y1, x2, y2)) return null;
        var length = Math.hypot(x2 - x1, y2 - y1) || 1, ux = (x2 - x1) / length, uy = (y2 - y1) / length;
        draw.appendChild(svg("line", { x1: x1, y1: y1, x2: kind === "arrow" ? x2 - ux * 10 : x2, y2: kind === "arrow" ? y2 - uy * 10 : y2, class: "md-d-line" + fg }));
        if (kind === "arrow") {
          var bx = x2 - ux * 14, by = y2 - uy * 14;
          draw.appendChild(svg("polygon", { points: x2 + "," + y2 + " " + (bx - uy * 7) + "," + (by + ux * 7) + " " + (bx + uy * 7) + "," + (by - ux * 7), class: "md-d-head" + fg }));
        }
        if (clean(item.label, 40)) draw.appendChild(svg("text", { x: x2 + ux * 18, y: y2 + uy * 18 + 5, class: "md-d-label" + fg, "text-anchor": "middle", text: clean(item.label, 40) }));
      } else if (kind === "dot") {
        var dx = pos(item, "x"), dy = pos(item, "y");
        if (!ok(dx, dy)) return null;
        draw.appendChild(svg("circle", { cx: dx, cy: dy, r: 7, class: "md-d-dot" + fg }));
        if (clean(item.label, 40)) draw.appendChild(svg("text", { x: dx + 12, y: dy + 24, class: "md-d-label" + fg, "text-anchor": "start", text: clean(item.label, 40) }));
      } else if (kind === "text") {
        var tx = pos(item, "x"), ty = pos(item, "y"), words = clean(item.text, 80);
        if (!ok(tx, ty) || !words) return null;
        draw.appendChild(svg("text", { x: tx, y: ty, class: "md-d-label md-d-" + (item.size === "s" ? "s" : item.size === "l" ? "l" : "m") + fg, "text-anchor": anchors[item.align] || "start", text: words }));
      } else return null;
    }
    return section("md-diagrambox", clean(data.title, 120) || info, clean(data.note, 300), [draw]);
  }

  // --- form -----------------------------------------------------------------------------------------------------------------
  function form(info, source, context) {
    var data = parse(source);
    if (!data || !Array.isArray(data.groups) || data.groups.length < 1 || data.groups.length > 6) return null;
    var groups = [];
    for (var i = 0; i < data.groups.length; i++) {
      var raw = data.groups[i], label = raw && clean(raw.label, 120);
      if (!label) return null;
      if (raw.slider !== undefined) {
        var s = raw.slider;
        if (!s || !finite(s.min) || !finite(s.max) || !(s.max > s.min)) return null;
        var step = finite(s.step) && s.step > 0 ? s.step : 1, start = finite(s.value) ? Math.max(s.min, Math.min(s.max, s.value)) : (s.min + s.max) / 2;
        groups.push({ label: label, slider: { min: s.min, max: s.max, step: step, value: start, unit: clean(s.unit, 12) } });
        continue;
      }
      var options = Array.isArray(raw.options) ? raw.options.map(function (option) { return clean(option, 80); }).filter(Boolean) : [];
      if (options.length < 2 || options.length > 12) return null;
      groups.push({ label: label, options: options, multiple: raw.multiple === true, other: raw.other === true, optional: raw.optional === true });
    }
    var interactive = !!(context.answer && !context.live), done = false, uid = ++formCount;
    var node = section("md-form" + (interactive ? " md-form-open" : ""), clean(data.title, 120) || info, "", []);
    var formEl = el("form", { class: "md-form-el", novalidate: "novalidate" });
    var submit = el("button", { type: "submit", class: "btn md-submit", text: clean(data.submit, 30) || "Continue" });
    var status = el("span", { class: "md-form-status muted small", role: "status" });
    var fields = [];

    function unitOf(slider, value) { return NUMBER.format(value) + (slider.unit ? (slider.unit === "%" ? "%" : " " + slider.unit) : ""); }
    function chosen(field) {
      if (field.range) return [unitOf(field.slider, Number(field.range.value))];
      var values = field.boxes.filter(function (box) { return box.checked; }).map(function (box) { return box.value; });
      if (field.other && field.other.box.checked && field.other.text.value.trim()) values.push(field.other.text.value.trim().slice(0, 200));
      return values;
    }
    function refresh() {
      submit.disabled = !interactive || done || !fields.every(function (field, at) { return groups[at].optional || chosen(field).length > 0; });
    }
    function finish(message) {
      done = true;
      node.classList.remove("md-form-open");
      node.classList.add("md-form-done");
      Array.prototype.forEach.call(formEl.querySelectorAll("input"), function (input) { input.disabled = true; });
      submit.hidden = true;
      submit.disabled = true;
      status.textContent = message;
    }
    node._close = function () { if (!done) finish("Closed"); };

    groups.forEach(function (group, at) {
      var set = el("fieldset", { class: "md-group" }, [el("legend", { class: "md-group-label", text: group.label })]);
      var field;
      if (group.slider) {
        var slider = group.slider, range = el("input", { type: "range", class: "md-range", min: slider.min, max: slider.max, step: slider.step, "aria-label": group.label });
        range.value = slider.value;
        range.disabled = !interactive;
        var readout = el("div", { class: "md-range-value", text: unitOf(slider, slider.value) });
        range.addEventListener("input", function () { readout.textContent = unitOf(slider, Number(range.value)); refresh(); });
        set.appendChild(readout);
        set.appendChild(range);
        field = { range: range, slider: slider };
      } else {
        var type = group.multiple ? "checkbox" : "radio", name = "mdf" + uid + "-" + at;
        var boxes = group.options.map(function (option) {
          var input = el("input", { type: type, name: name });
          input.value = option;
          input.disabled = !interactive;
          input.addEventListener("change", refresh);
          set.appendChild(el("label", { class: "md-option" }, [input, el("span", { text: option })]));
          return input;
        });
        field = { boxes: boxes, other: null };
        if (group.other) {
          var box = el("input", { type: type, name: name }), text = el("input", { type: "text", class: "md-other-text", maxlength: "200", placeholder: "Other…", "aria-label": group.label + ": other" });
          box.disabled = text.disabled = !interactive;
          box.addEventListener("change", function () { if (box.checked) text.focus(); refresh(); });
          text.addEventListener("input", function () { if (text.value && !box.checked) box.checked = true; refresh(); });
          set.appendChild(el("label", { class: "md-option md-option-other" }, [box, text]));
          field.other = { box: box, text: text };
        }
      }
      fields.push(field);
      formEl.appendChild(set);
    });
    formEl.appendChild(el("div", { class: "md-form-actions" }, [submit, status]));
    formEl.addEventListener("submit", function (event) {
      event.preventDefault();
      if (done || !interactive || submit.disabled) return;
      var answers = fields.map(function (field, at) { var values = chosen(field); return values.length ? groups[at].label + ": " + values.join(", ") : ""; }).filter(Boolean);
      if (context.answer(answers.join("\n"))) finish("Answer sent");
      else status.textContent = "Cannot send right now. Try again when the chat is ready.";
    });
    node.appendChild(formEl);
    refresh();
    return node;
  }

  var KINDS = { chart: chart, stats: stats, progress: progress, timeline: timeline, tiles: tiles, funnel: funnel, flow: flow, card: card, matrix: matrix, gantt: gantt, diagram: diagram, form: form };
  // render() returns a node, or null when the block is not valid: the caller then shows it as an ordinary code block.
  P.CodeWidgets = {
    kinds: Object.keys(KINDS),
    render: function (kind, info, source, context) {
      if (!Object.prototype.hasOwnProperty.call(KINDS, kind) || source.length > 20000) return null;
      try { return KINDS[kind](info, source, context || {}); } catch (e) { return null; }
    }
  };
})();
