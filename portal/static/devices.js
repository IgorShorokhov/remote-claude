(function () {
  var P = window.Portal;
  var el = P.el;
  var box = null;
  var listBox = null;
  var timer = null;
  var loaded = false, drawn = "", seen = {};

  function ago(sec) {
    if (!sec) return "never";
    var d = Math.max(0, Math.round(Date.now() / 1000 - sec));
    if (d < 90) return "just now";
    if (d < 5400) return Math.round(d / 60) + " min ago";
    if (d < 129600) return Math.round(d / 3600) + " h ago";
    return Math.round(d / 86400) + " days ago";
  }

  function download(href, text, primary) {
    return el("a", { class: "btn" + (primary ? " primary" : ""), href: href, download: "", text: text });
  }

  function steps(items) {
    return el("ol", { class: "dev-steps" }, items.map(function (t) { return el("li", { text: t }); }));
  }

  function card(title, lead, body) {
    return el("section", { class: "dev-card" }, [el("h3", { text: title }), lead ? el("p", { class: "dev-lead", text: lead }) : null].concat(body));
  }

  function row(d) {
    var meta = [];
    if (d.kind === "pc") {
      if (d.os || d.host) meta.push([d.os, d.host].filter(Boolean).join(" · "));
      if (d.version) meta.push("agent " + d.version);
    } else if (d.version) {
      meta.push("extension " + d.version);
    }
    meta.push(d.online ? "connected" : "last seen " + ago(d.used));
    var paused = d.state && d.state.paused;
    var out = el("div", { class: "dev-row" }, [
      el("span", { class: "pcdot " + (d.online ? (paused ? "warn" : "ok") : "bad"), "aria-hidden": "true" }),
      el("div", { class: "dev-main" }, [
        el("div", { class: "dev-name" }, [
          el("strong", { text: d.label || (d.kind === "pc" ? "Computer" : "Chrome") }),
          el("span", { class: "badge", text: d.kind === "pc" ? "Computer" : "Chrome" }),
          paused ? el("span", { class: "badge warn-badge", text: "paused on the computer" }) : null,
          d.kind === "pc" && d.online && d.state && d.state.screen === false ? el("span", { class: "badge", text: "screen control off" }) : null,
          d.kind === "pc" && d.online && d.state && d.state.shell === false ? el("span", { class: "badge", text: "commands off" }) : null
        ]),
        el("div", { class: "sess-meta", text: meta.join(" · ") })
      ])
    ]);
    var drop = el("button", { class: "btn", type: "button", text: "Disconnect" });
    drop.addEventListener("click", function () {
      P.ask("Disconnect " + (d.label || "this device") + "? Claude can no longer reach it until you connect it again.", { ok: "Disconnect" }).then(function (yes) {
        if (!yes) return;
        drop.disabled = true;
        P.api("DELETE", "/api/devices/" + encodeURIComponent(d.id)).then(load).catch(function (e) {
          drop.disabled = false;
          window.alert("Could not disconnect: " + e.message);
        });
      });
    });
    out.appendChild(drop);
    return out;
  }

  // The list is polled every 10 s: an unchanged answer leaves the rows alone, and only a newly connected device slides in.
  function render(r) {
    var key = JSON.stringify(r.devices);
    if (key === drawn) return;
    drawn = key;
    listBox.textContent = "";
    if (!r.devices.length) {
      listBox.appendChild(el("div", { class: "stub", text: "Nothing is connected yet. Connect a computer or Chrome below." }));
      return;
    }
    var before = seen;
    seen = {};
    r.devices.forEach(function (d) {
      var line = row(d);
      if (!before[d.id]) line.classList.add("motion-row");
      seen[d.id] = true;
      listBox.appendChild(line);
    });
  }

  function load() {
    return P.api("GET", "/api/devices").then(function (r) {
      loaded = true;
      render(r);
    }).catch(function (e) {
      if (!loaded) {
        listBox.textContent = "";
        listBox.appendChild(el("div", { class: "stub", text: "The list could not be loaded (" + e.message + ")." }));
      }
    });
  }

  function build(section) {
    box = el("div", { class: "dev-box" });
    section.appendChild(box);
    box.appendChild(el("h2", { text: "Your computers and browsers" }));
    box.appendChild(el("p", { class: "dev-lead", text: "Connect your own computer or Chrome and Claude, in your chats, can work on them. Only your chats can reach what you connect: other accounts never see it." }));
    listBox = el("div", { class: "dev-list" });
    box.appendChild(listBox);
    listBox.appendChild(el("div", { class: "stub", text: "Loading..." }));

    var command = "sh ~/Downloads/claude-portal-setup.sh";
    var copy = el("button", { class: "btn", type: "button", text: "Copy" });
    copy.addEventListener("click", function () {
      P.copyText(command);
      copy.textContent = "Copied";
      setTimeout(function () { copy.textContent = "Copy"; }, 1500);
    });
    box.appendChild(card("Connect a Windows computer", "Claude sees the screen and uses the mouse and keyboard there, runs commands and edits files, with your Windows user's rights. Installs for your user only; no administrator rights needed.", [
      el("div", { class: "row" }, [download("/api/devices/download/windows", "Download for Windows", true)]),
      steps([
        "Open the downloaded ClaudePortal-Setup.cmd (double-click). If Windows asks, choose Run anyway.",
        "A browser page opens with a short code. Check it matches the one in the black window, then press Approve.",
        "Done. The Claude Portal settings open with switches for the screen, commands and autostart. Later: the Claude icon in the tray (by the clock) or Claude Portal in the Start menu."
      ])
    ]));
    box.appendChild(card("Connect Linux or macOS", "The same app: screen, mouse and keyboard, commands and files. It needs python3 (3.9 or newer; on a Mac it comes with the Command Line Tools) and starts with your session.", [
      el("div", { class: "row" }, [download("/api/devices/download/unix", "Download the script", true), el("code", { class: "dev-code", text: command }), copy]),
      steps([
        "Run the command above in a terminal, as yourself (no sudo).",
        "Approve the code in the browser page that opens. The Claude Portal settings open next.",
        "macOS: in the settings press “Open macOS permissions” and allow Screen Recording and Accessibility for Python.",
        "Linux: screen control needs an X11 session (Ubuntu: “Ubuntu on Xorg” at login); on Wayland only commands and files work.",
        "Later: Claude Portal in your applications (Launchpad or the app menu)."
      ])
    ]));
    box.appendChild(card("Connect Chrome", "Claude opens tabs, reads pages and clicks in your own Chrome, with your signed-in sites. It works only in a tab group named Claude and in tabs you allow.", [
      el("div", { class: "row" }, [download("/api/devices/download/extension", "Download the extension", true)]),
      steps([
        "Unzip it into a folder you will keep, for example Documents.",
        "In Chrome open chrome://extensions, turn on Developer mode, press Load unpacked and choose the folder claude-browser-bridge.",
        "Pin the extension, click its icon, press Connect, then Approve in the page that opens."
      ])
    ]));
    box.appendChild(card("What you are allowing", null, [
      el("ul", { class: "dev-notes" }, [
        el("li", { text: "A connected computer lets Claude see its screen, click and type, run commands and read, create, change and delete files there, as the user who started the agent. Connect only computers you are fine with that on." }),
        el("li", { text: "Each part can be switched off in the Claude Portal settings on the computer. The tray icon turns blue while Claude uses the screen; Pause stops everything at once." }),
        el("li", { text: "In your chats Claude works on your computers without asking before each command, like over SSH. Only running JavaScript in a Chrome page still asks." }),
        el("li", { text: "Every command is written to activity.log on the computer (file contents are not). Pause Claude there at any time, or press Disconnect here." }),
        el("li", { text: "Text from web pages and files is not trusted by Claude, but a hostile page can still try to steer it. Do not point Claude at pages you do not trust while a sensitive computer is connected." })
      ])
    ]));
  }

  P.tabs.devices = {
    init: function (section) {
      build(section);
    },
    onShow: function () {
      load();
      if (timer) clearInterval(timer);
      timer = setInterval(function () {
        var s = document.querySelector('[data-tab="devices"]');
        if (s && !s.hidden) load();
      }, 10000);
    },
    onHide: function () {
      if (timer) clearInterval(timer);
      timer = null;
    }
  };
})();
