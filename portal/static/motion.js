(function () {
  var root = document.documentElement;
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var fade = null;
  function ready() {
    requestAnimationFrame(function () { requestAnimationFrame(function () { root.classList.add("motion-ready"); }); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
  else ready();
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest && ev.target.closest(".tab[data-id], .theme-toggle");
    if (!t) return;
    if (t.classList.contains("tab")) { root.classList.add("motion-pane"); return; }
    if (still.matches) return;
    root.classList.add("theme-fade");
    clearTimeout(fade);
    fade = setTimeout(function () { root.classList.remove("theme-fade"); }, 260);
  }, true);
})();
