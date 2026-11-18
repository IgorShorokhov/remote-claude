(function () {
  var P = window.Portal;
  var root = document.documentElement;
  var nav = document.getElementById('navigation');
  var open = document.getElementById('navigation-open');
  var close = document.getElementById('navigation-close');
  var backdrop = document.getElementById('navigation-backdrop');
  var host = document.getElementById('session-host');
  var title = document.getElementById('page-title');
  var mobile = matchMedia('(max-width: 768px)');
  var content = document.getElementById('panel');
  var header = open.closest('.mode-header');
  var expanded = !mobile.matches;
  var current = 'code';
  var section;
  var label = document.querySelector('#navigation-sessions .navigation-label');
  var actions = P.el('ul', { id: 'mode-actions', class: 'mode-actions', 'aria-label': 'Actions' });
  var icons = {
    code: ['Code Web', 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16'],
    prompts: ['Prompts', 'M6 3h9l4 4v14H6V3m8 0v5h5M9 12h7m-7 4h7'],
    skills: ['Skills', 'M12 3l2.4 5.4L20 9.2l-4.2 3.9 1.1 5.9L12 16.1 7.1 19l1.1-5.9L4 9.2l5.6-.8L12 3'],
    console: ['Console', 'm5 6 6 6-6 6m8 0h6'],
    instructions: ['System instructions', 'M4 6h10m4 0h2M4 12h2m4 0h10M4 18h8m4 0h4M14 4v4m-4 2v4m6 2v4'],
    memory: ['Memory', 'M12 5a3 3 0 0 0-5.8-1A3 3 0 0 0 4 9a3 3 0 0 0 .5 5.5A3 3 0 0 0 8 19a3 3 0 0 0 4 1V5m0 0a3 3 0 0 1 5.8-1A3 3 0 0 1 20 9a3 3 0 0 1-.5 5.5A3 3 0 0 1 16 19a3 3 0 0 1-4 1'],
    browser: ['VPS browser', 'M3 8h18M7 5h.01M10 5h.01M5 2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2'],
    chat: ['Claude chat', 'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2v-9.5A8.5 8.5 0 0 1 10.5 4h2a8.5 8.5 0 0 1 8.5 7.5M7 11h10M7 15h6'],
    devices: ['Computers', 'M5 5h14a1 1 0 0 1 1 1v9H4V6a1 1 0 0 1 1-1m-3 12h20l-2 3H4l-2-3'],
    pc: ['Remote PC', 'M4 3h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2m8 14v4m-4 0h8'],
    sync: ['File sync', 'M20 7a8 8 0 0 0-13-3L3 8m0-5v5h5m-4 9a8 8 0 0 0 13 3l4-4m0 5v-5h-5'],
    vpn: ['VPN', 'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4m-4 10 3 3 5-6'],
    access: ['Access', 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8m10-3v6m3-3h-6'],
    history: ['Chat history', 'M3 11a9 9 0 1 1 2 7M3 4v7h7m2-5v6l4 2'],
    backup: ['API Agent', 'M9 3h6m-3 0v4M5 7h14a2 2 0 0 1 2 2v10H3V9a2 2 0 0 1 2-2m4 5h.01M15 12h.01M8 16h8']
  };
  function decorate() {
    var tabs = document.getElementById('tabs');
    // Always in view; the rest sits under Other. Computers is here because people come to the portal to connect a computer (17 Nov 2026).
    var MAIN = ['code', 'backup', 'console', 'prompts', 'skills', 'devices'];
    var other = document.getElementById('navigation-other');
    if (!other) {
      tabs.removeAttribute('role');
      tabs.removeAttribute('aria-orientation');
      other = P.el('details', { id: 'navigation-other' });
      other.appendChild(P.el('summary', { text: 'Other' }));
      tabs.appendChild(other);
    }
    document.querySelectorAll('#tabs .tab').forEach(function (button) {
      if (MAIN.indexOf(button.dataset.id) < 0) other.appendChild(button);
      button.removeAttribute('role');
      button.setAttribute('aria-current', button.dataset.id === current ? 'page' : 'false');
      if (button.dataset.id === current && other.contains(button)) other.open = true;
      if (button.querySelector('.navigation-symbol')) return;
      var entry = icons[button.dataset.id];
      var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'navigation-symbol');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', (entry || icons.backup)[1]);
      svg.appendChild(path);
      var text = P.el('span', { class: 'navigation-text', text: entry ? entry[0] : button.textContent });
      button.replaceChildren(svg, text);
      button.setAttribute('aria-label', text.textContent);
    });
  }
  host.after(actions);
  function action(text, run) {
    var b = P.el('button', { type: 'button', class: 'btn', text: text });
    b.addEventListener('click', function () { run(); selected(); });
    actions.appendChild(P.el('li', {}, [b]));
  }
  function proxy(node, text, pattern) {
    var b = Array.from(node.querySelectorAll('button')).find(function (x) { return pattern.test(x.textContent); });
    if (b) action(text, function () { b.click(); });
  }
  function create(id) {
    P.show(id);
    var add = host.querySelector('.code-side > .row .primary') || document.querySelector('.tools-code-actions .primary');
    if (add) add.click();
  }
  function sideOf(id) { return id === 'backup' || id === 'prompts' || id === 'skills' ? id : 'code'; }
  function modeActions(id, node) {
    var own = id === 'code' || id === 'console' || id === 'backup' || id === 'prompts' || id === 'skills';
    decorate();
    host.querySelectorAll('.code-side,.prompt-side').forEach(function (side) { side.hidden = !own || side.dataset.portalTab !== sideOf(id); });
    label.textContent = id === 'skills' ? 'Skills' : id === 'prompts' ? 'Prompts' : id === 'backup' ? 'API Agent chats' : id === 'code' ? 'Chats' : id === 'console' ? 'Consoles' : 'Actions';
    actions.textContent = '';
    actions.hidden = own;
    if (own) return;
    proxy(node, 'Reconnect', /Reconnect/);
    proxy(node, 'Fullscreen', /Full screen|Fullscreen/);
    action('New chat', function () { create('code'); });
    if (P.role === 'admin') action('New console', function () { create('console'); });
  }
  function display(value, focus) {
    expanded = value;
    root.classList.toggle('navigation-expanded', value);
    open.setAttribute('aria-expanded', String(value));
    nav.inert = !value;
    backdrop.hidden = !value || !mobile.matches;
    content.inert = value && mobile.matches;
    header.inert = value && mobile.matches;
    if (focus) (value ? nav.querySelector('#tabs .tab:not(:disabled)') || close : open).focus({ preventScroll: true });
  }
  function selected() {
    if (mobile.matches) display(false, nav.contains(document.activeElement));
  }
  open.addEventListener('click', function () { display(!expanded, true); });
  close.addEventListener('click', function () { display(false, true); });
  backdrop.addEventListener('click', function () { display(false, true); });
  nav.addEventListener('click', function (ev) {
    var side = ev.target.closest('.code-side');
    if (side && side.dataset.portalTab === 'backup') return;
    if (side && current !== 'code' && current !== 'console') P.show('code');
  }, true);
  nav.addEventListener('click', function (ev) {
    if (ev.target.closest('.code-side > .row .primary')) selected();
    if (ev.target.closest('.skill-open')) { P.show('skills'); selected(); return; }
    if (ev.target.closest('.prompt-open')) { P.show('prompts'); selected(); return; }
    if (ev.target.closest('.sess-open')) {
      var side = ev.target.closest('.code-side');
      P.show(side && side.dataset.portalTab === 'backup' ? 'backup' : current === 'console' ? 'console' : 'code');
    }
    if (ev.target.closest('.tab, .sess-open, .prompt-open, .prompt-new')) selected();
  });
  document.addEventListener('keydown', function (ev) {
    if (!expanded) return;
    if (ev.key === 'Escape' && (mobile.matches || nav.contains(document.activeElement))) { display(false, true); ev.preventDefault(); }
    if (ev.key === 'Tab' && mobile.matches) {
      var focusable = Array.from(nav.querySelectorAll('button,a,input,select,textarea,summary')).filter(function (e) { return !e.disabled && e.getClientRects().length; });
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (ev.shiftKey && document.activeElement === first) { last.focus(); ev.preventDefault(); }
      else if (!ev.shiftKey && document.activeElement === last) { first.focus(); ev.preventDefault(); }
    }
  });
  mobile.addEventListener('change', function () { display(!mobile.matches, false); });
  function heading() {
    var button = document.querySelector('#tabs [data-id="' + current + '"]');
    var sessionTitle = section && section.querySelector('.term-title');
    title.textContent = (current === 'code' || current === 'console') && sessionTitle && sessionTitle.textContent ? sessionTitle.textContent : button ? button.textContent : 'Remote Claude';
  }
  var profile = P.shell.profile;
  P.shell.profile = function (me) {
    profile(me);
    document.getElementById('navigation-sessions').hidden = false;
  };
  var activate = P.shell.activate;
  P.shell.activate = function (id, node) {
    current = id;
    section = node;
    var side = node.querySelector('.code-side,.prompt-side');
    if (side) {
      side.dataset.portalTab = sideOf(id);
      host.appendChild(side);
      var titleNode = node.querySelector('.term-title');
      if (titleNode) new MutationObserver(heading).observe(titleNode, { childList: true, subtree: true, characterData: true });
      new MutationObserver(function () { if (mobile.matches && node.querySelector('.modal')) display(false, false); }).observe(node, { childList: true });
    }
    host.querySelectorAll('.code-side,.prompt-side').forEach(function (side) { side.hidden = side.dataset.portalTab !== sideOf(id); });
    activate(id, node);
    modeActions(id, node);
    document.querySelectorAll('.tools-code-actions button').forEach(function (button) {
      if (button.textContent !== 'Chat history' || button.dataset.navigationBound) return;
      button.dataset.navigationBound = 'true';
      button.addEventListener('click', function () { display(true, false); var first = host.querySelector('.sess-open'); if (first) first.focus(); });
    });
    heading();
    selected();
  };
  function viewport() {
    if (window.visualViewport) root.style.setProperty('--portal-height', window.visualViewport.height + 'px');
  }
  if (window.visualViewport) window.visualViewport.addEventListener('resize', viewport);
  viewport();
  display(expanded, false);
})();
