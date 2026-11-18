(function () {
  var P = window.Portal;
  var el = P.el;
  var box = null;
  var backupBox = null;
  var timer = null;

  var TYPES = {
    sendreceive: "two-way",
    sendonly: "VPS → PC",
    receiveonly: "PC → VPS"
  };

  var STATES = {
    idle: ["wait", "idle"],
    scanning: ["wait", "scanning"],
    "scan-waiting": ["wait", "waiting to scan"],
    syncing: ["wait", "syncing"],
    "sync-preparing": ["wait", "preparing"],
    "sync-waiting": ["wait", "waiting to sync"],
    cleaning: ["wait", "cleaning"],
    paused: ["warn", "paused"],
    error: ["bad", "error"],
    unknown: ["wait", "no data"]
  };

  var LINKS = {
    "tcp-server": "direct, TCP",
    "tcp-client": "direct, TCP",
    "quic-server": "direct, QUIC",
    "quic-client": "direct, QUIC",
    "relay-server": "via relay",
    "relay-client": "via relay"
  };

  function note(text) {
    box.textContent = "";
    box.appendChild(el("div", { class: "stub", text: text }));
  }

  function plural(n, forms) {
    var k = n % 100;
    if (k >= 11 && k <= 14) return forms[2];
    k = n % 10;
    if (k === 1) return forms[0];
    if (k >= 2 && k <= 4) return forms[1];
    return forms[2];
  }

  function ago(sec) {
    if (sec === null || sec === undefined) return "";
    if (sec < 0) sec = 0;
    if (sec < 60) return sec + " s ago";
    if (sec < 3600) return Math.floor(sec / 60) + " min ago";
    if (sec < 86400) return Math.floor(sec / 3600) + " h ago";
    return Math.floor(sec / 86400) + " d ago";
  }

  function when(sec) {
    if (!sec) return "unknown";
    return P.fmtTime(sec) + " (" + ago(Math.round(Date.now() / 1000) - sec) + ")";
  }

  function files(f) {
    if (f.files_local === null || f.files_global === null) return "files: no data";
    return "files: " + f.files_local + " of " + f.files_global;
  }

  function receive(f) {
    if (f.completion === null || f.completion === undefined) return "on PC: unknown";
    if (f.completion >= 100) return "";
    return "on PC: " + f.completion + "%";
  }

  function folderRow(f) {
    var st = STATES[f.state] || STATES.unknown;
    var done = f.up_to_date === true;
    var dot = f.state === "error" ? "bad" : (done ? "ok" : st[0]);
    var label = done ? "up to date" : (f.state === "idle" ? "unconfirmed" : st[1]);
    var meta = (TYPES[f.type] || f.type) + ", " + label;
    if (!done) {
      var left = [];
      if (f.need_files) left.push("left " + f.need_files + " " + plural(f.need_files, ["file", "files", "files"]));
      if (f.need_bytes) left.push("left " + P.fmtBytes(f.need_bytes));
      if (f.errors) left.push("errors: " + f.errors);
      var got = receive(f);
      if (got) left.push(got);
      if (left.length) meta += " (" + left.join(", ") + ")";
      else if (f.need_files === null || f.need_bytes === null || f.errors === null) meta += " (remaining unknown)";
    }
    return el("div", { class: "pcrow" }, [
      el("span", { class: "pcdot " + dot }),
      el("span", { class: "pcname", text: f.name }),
      el("span", { class: "sess-meta", text: meta }),
      el("span", { class: "sess-meta", text: files(f) }),
      el("span", { class: "sess-meta", text: "size: " + P.fmtBytes(f.bytes_global) })
    ]);
  }

  function render(r) {
    box.textContent = "";
    var conn = r.stale ? ["wait", "Link unknown"] : (r.connected ? ["ok", "PC connected"] : ["bad", "PC offline"]);
    var head = el("div", { class: "row" }, [
      el("span", { class: "pcdot " + conn[0] }),
      el("strong", { text: conn[1] })
    ]);
    if (r.link) head.appendChild(el("span", { class: "sess-meta", text: "link: " + (LINKS[r.link] || r.link) }));
    if (r.version) head.appendChild(el("span", { class: "sess-meta", text: "Syncthing " + r.version }));
    box.appendChild(head);

    if (r.stale) {
      var why = (r.age === null || r.age === undefined) ? "last update unknown" : "updated " + ago(r.age);
      box.appendChild(el("div", { class: "warn", text: "Stale data: " + why + "." }));
    }
    var syncText = (r.last_sync === null || r.last_sync === undefined)
      ? "last sync: unknown"
      : "last sync: " + when(r.last_sync);
    box.appendChild(el("div", { class: "sess-meta", text: syncText }));
    box.appendChild(el("div", { class: "sess-meta", text: "last scan: " + when(r.last_scan) }));
    box.appendChild(el("div", { class: "sess-meta", text: "conflicts: " + (r.conflicts === null ? "unknown" : r.conflicts) }));

    var list = el("div", { class: "pclist" });
    (r.folders || []).forEach(function (f) { list.appendChild(folderRow(f)); });
    box.appendChild(list);

    (r.notes || []).forEach(function (n) {
      box.appendChild(el("div", { class: "sess-meta", text: n }));
    });
    box.appendChild(el("div", { class: "sess-meta", text: "Read-only" }));
  }

  function backupNote(text) {
    backupBox.textContent = "";
    backupBox.appendChild(el("h2", { text: "Backups" }));
    backupBox.appendChild(el("div", { class: "stub", text: text }));
  }

  function renderBackup(r) {
    backupBox.textContent = "";
    backupBox.appendChild(el("h2", { text: "Backups" }));
    var states = {
      ok: ["ok", "Backup verified"],
      stale: ["warn", "Backup stale"],
      error: ["bad", "Backup failing"]
    };
    var state = states[r.state] || states.error;
    backupBox.appendChild(el("div", { class: "row" }, [
      el("span", { class: "pcdot " + state[0] }),
      el("strong", { text: state[1] })
    ]));
    backupBox.appendChild(el("div", { class: "sess-meta", text: "last success: " + (r.last_success ? when(r.last_success) : "never") }));
    backupBox.appendChild(el("div", { class: "sess-meta", text: "last archive: " + P.fmtBytes(r.last_size) }));
    backupBox.appendChild(el("div", { class: "sess-meta", text: "copies: " + r.copies + "; total: " + P.fmtBytes(r.total_bytes) }));
    if (r.error) backupBox.appendChild(el("div", { class: "warn", text: r.error }));
    var pc = {
      synced: "copied to PC",
      not_confirmed: "PC copy unconfirmed",
      unknown: "PC copy unknown"
    };
    backupBox.appendChild(el("div", { class: "sess-meta", text: pc[r.pc_copy] || pc.unknown }));
    backupBox.appendChild(el("div", { class: "sess-meta", text: "Read-only" }));
  }

  function load() {
    var sync = P.api("GET", "/api/sync").then(render).catch(function (e) {
      note("Sync status failed: " + e.message);
    });
    var backup = P.api("GET", "/api/backup").then(renderBackup).catch(function (e) {
      backupNote("Backup status failed: " + e.message);
    });
    return Promise.all([sync, backup]);
  }

  P.tabs.sync = {
    init: function (section) {
      box = el("div", { class: "pcbox" });
      backupBox = el("div", { class: "pcbox" });
      section.appendChild(box);
      section.appendChild(backupBox);
      note("Loading...");
      backupNote("Loading...");
    },
    onShow: function () {
      load();
      if (timer) clearInterval(timer);
      timer = setInterval(function () {
        var s = document.querySelector('[data-tab="sync"]');
        if (s && !s.hidden) load();
      }, 30000);
    }
  };
})();
