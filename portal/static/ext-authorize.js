(function () {
  var status = document.getElementById("ext-status");
  var approve = document.getElementById("ext-approve");
  var go = document.getElementById("ext-go");
  var input = document.getElementById("ext-code");
  function say(text) { status.textContent = text; }
  if (go) {
    go.addEventListener("click", function () {
      var code = input.value.trim().toUpperCase();
      if (code) location.href = "/ext/authorize?code=" + encodeURIComponent(code);
    });
    input.addEventListener("keydown", function (ev) { if (ev.key === "Enter") go.click(); });
  }
  if (approve) {
    approve.addEventListener("click", function () {
      approve.disabled = true;
      say("Approving…");
      fetch("/ext/approve", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Portal": "1" },
        body: JSON.stringify({ code: approve.dataset.code })
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 404 ? "The code expired. Start again in VS Code." : "Could not approve (" + r.status + ").");
        say("Approved. You can go back to VS Code.");
        approve.hidden = true;
      }).catch(function (e) {
        say(e.message);
        approve.disabled = false;
      });
    });
  }
})();
