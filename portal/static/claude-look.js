(function () {
  var root = document.documentElement;
  var media = window.matchMedia('(prefers-color-scheme: dark)');
  var saved;
  try { saved = localStorage.getItem('portal-theme'); } catch (e) {}
  var explicit = saved === 'light' || saved === 'dark';
  function apply(theme) {
    root.dataset.theme = theme;
    if (button) {
      button.textContent = theme === 'dark' ? 'Light theme' : 'Dark theme';
      button.setAttribute('aria-label', button.textContent);
    }
  }
  var button;
  apply(explicit ? saved : media.matches ? 'dark' : 'light');
  document.addEventListener('DOMContentLoaded', function () {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'theme-toggle';
    button.addEventListener('click', function () {
      explicit = true;
      apply(root.dataset.theme === 'dark' ? 'light' : 'dark');
      try { localStorage.setItem('portal-theme', root.dataset.theme); } catch (e) {}
    });
    document.querySelector('.account').appendChild(button);
    apply(root.dataset.theme);
  });
  media.addEventListener('change', function (event) {
    if (!explicit) apply(event.matches ? 'dark' : 'light');
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Tab') root.classList.add('kbd-nav');
  }, true);
  ['pointerdown', 'touchstart', 'mousedown'].forEach(function (type) {
    document.addEventListener(type, function () { root.classList.remove('kbd-nav'); }, { capture: true, passive: true });
  });
})();
