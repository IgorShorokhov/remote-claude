(function () {
  // Access (decision 229): the owner decides which emails may sign in and which sections each account may open.
  var P = window.Portal, el = P.el;
  var box, notice, stats, head, rows, locked, logList, search, error;
  var data = null, epoch = 0, timer = null, shown = false, busy = {};
  var LOCK = 'M7 11V8a5 5 0 0 1 10 0v3M6 11h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1';
  var DISMISSED = 'portal-access-result';

  function api(method, path, body) { return P.api(method, '/api/access' + path, body); }
  function icon(d, cls) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('class', cls || 'acc-icon');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d); svg.appendChild(path);
    return svg;
  }
  function fail(text) { error.textContent = text || ''; error.hidden = !text; }
  function initial(email) { return (email[0] || '?').toUpperCase(); }
  function hue(email) { var h = 0; for (var i = 0; i < email.length; i++) h = (h * 31 + email.charCodeAt(i)) % 360; return h; }
  function ago(sec) {
    var d = Math.max(0, Date.now() / 1000 - sec);
    if (d < 60) return 'just now';
    if (d < 3600) return Math.round(d / 60) + ' min ago';
    if (d < 86400) return Math.round(d / 3600) + ' h ago';
    return P.fmtTime(sec);
  }
  function dismissed() { try { return sessionStorage.getItem(DISMISSED); } catch (e) { return null; } }

  function paintNotice() {
    notice.textContent = '';
    var b = data.bridge, wait = b.pending, result = b.result;
    if (wait) {
      var who = wait.change.email, adding = wait.change.kind === 'add';
      var lines = [el('strong', { text: (adding ? 'Adding ' : 'Removing ') + who }),
        el('span', { text: b.installed ? 'The server is applying the change; this takes a few seconds.' : 'Sign-in changes need a one-time setup on the server. Until then this request waits.' })];
      if (!b.installed) {
        var cmd = 'cd /home/claude/agent-system && sudo bash infra/59-access-bridge.sh';
        var copy = el('button', { type: 'button', class: 'btn acc-copy', text: 'Copy command' });
        copy.addEventListener('click', function () { P.copyText ? P.copyText(cmd) : navigator.clipboard.writeText(cmd); copy.textContent = 'Copied'; });
        lines.push(el('code', { class: 'acc-cmd', text: cmd }), copy);
      }
      var cancel = el('button', { type: 'button', class: 'btn acc-cancel', text: 'Cancel request' });
      cancel.addEventListener('click', cancelRequest);
      notice.appendChild(el('div', { class: 'acc-note ' + (b.installed ? 'acc-note-busy' : 'acc-note-warn'), role: 'status' }, [el('div', { class: 'acc-note-text' }, lines), cancel]));
    } else if (result && result.id && result.id !== dismissed() && Date.now() / 1000 - result.at < 600) {
      var change = result.change || {};
      var text = result.ok ? (change.kind === 'remove' ? change.email + ' can no longer sign in.' : change.email + ' can sign in now.') : 'The server refused the change: ' + result.message;
      var close = el('button', { type: 'button', class: 'btn acc-dismiss', 'aria-label': 'Hide', text: 'OK' });
      close.addEventListener('click', function () { try { sessionStorage.setItem(DISMISSED, result.id); } catch (e) {} paintNotice(); });
      notice.appendChild(el('div', { class: 'acc-note ' + (result.ok ? 'acc-note-ok' : 'acc-note-bad'), role: result.ok ? 'status' : 'alert' }, [el('div', { class: 'acc-note-text' }, [el('span', { text: text })]), close]));
    }
  }

  function paintHead() {
    head.textContent = '';
    head.appendChild(el('div', { class: 'acc-col-who', text: 'Account' }));
    data.sections.forEach(function (s) {
      var open = data.accounts.filter(function (a) { return !a.owner && a.sections[s.id]; }).length;
      var members = data.accounts.filter(function (a) { return !a.owner; }).length;
      head.appendChild(el('div', { class: 'acc-col', title: s.about, 'data-tip': s.about }, [
        el('span', { class: 'acc-col-name', text: s.title }), el('span', { class: 'acc-col-count', text: members ? open + '/' + members : '' })
      ]));
    });
    head.appendChild(el('div', { class: 'acc-col-more' }));
  }

  function switchFor(account, section) {
    var on = !!account.sections[section.id];
    var sw = el('button', { type: 'button', role: 'switch', class: 'acc-switch', 'aria-checked': on ? 'true' : 'false',
      'aria-label': section.title + ' for ' + account.email, 'data-section': section.id });
    sw.appendChild(el('span', { class: 'acc-knob' }));
    if (account.owner) { sw.disabled = true; sw.title = 'You always have every section'; }
    else if (account.pending && account.pending.kind === 'remove') sw.disabled = true;
    sw.addEventListener('click', function () { change(account, (function () { var o = {}; o[section.id] = !on; return o; })()); });
    return el('label', { class: 'acc-cell' }, [el('span', { class: 'acc-cell-name', text: section.title }), sw]);
  }

  function rowFor(a) {
    var badges = [];
    if (a.owner) badges.push(el('span', { class: 'acc-badge owner', text: 'Owner' }));
    else badges.push(el('span', { class: 'acc-badge', text: 'Member' }));
    if (a.pending) badges.push(el('span', { class: 'acc-badge wait', text: a.pending.kind === 'add' ? 'Sign-in pending' : 'Removing' }));
    var count = Object.keys(a.sections).filter(function (k) { return a.sections[k]; }).length;
    var who = el('div', { class: 'acc-who' }, [
      avatar(a.email),
      el('div', { class: 'acc-id' }, [el('span', { class: 'acc-email', text: a.email, title: a.email }),
        el('span', { class: 'acc-meta' }, badges.concat([el('span', { class: 'acc-count', text: a.owner ? 'everything' : count + ' of ' + data.sections.length + ' sections' })]))])
    ]);
    var cells = el('div', { class: 'acc-cells' }, data.sections.map(function (s) { return switchFor(a, s); }));
    if (a.owner) cells.classList.add('acc-cells-owner');
    var more = el('div', { class: 'acc-more-cell' });
    if (!a.owner && !(a.pending && a.pending.kind === 'remove')) {
      var menu = el('details', { class: 'acc-more' }, [el('summary', { 'aria-label': 'More for ' + a.email, title: 'More' }, [el('span', { text: '⋯', 'aria-hidden': 'true' })])]);
      var list = el('div', { class: 'acc-menu', role: 'menu' });
      [['Open every section', function () { change(a, all(true)); }],
       ['Close every section', function () { change(a, all(false)); }],
       a.pending ? null : ['Remove sign-in…', function () { remove(a); }, 'danger']].forEach(function (item) {
        if (!item) return;
        var b = el('button', { type: 'button', role: 'menuitem', class: 'acc-menu-item' + (item[2] ? ' ' + item[2] : ''), text: item[0] });
        b.addEventListener('click', function () { menu.open = false; item[1](); });
        list.appendChild(b);
      });
      menu.appendChild(list);
      more.appendChild(menu);
    } else if (a.owner) {
      more.appendChild(el('span', { class: 'acc-lock', title: 'Your own access cannot be changed here' }, [icon(LOCK)]));
    }
    return el('li', { class: 'acc-row' + (a.owner ? ' owner' : '') + (a.pending ? ' pending' : '') + (busy[a.email] ? ' saving' : ''), 'data-email': a.email }, [who, cells, more]);
  }

  function avatar(email) {
    var node = el('span', { class: 'acc-avatar', text: initial(email), 'aria-hidden': 'true' });
    node.style.setProperty('--h', hue(email));  // the page policy forbids style attributes, not CSSOM
    return node;
  }

  function all(on) { var o = {}; data.sections.forEach(function (s) { o[s.id] = on; }); return o; }

  function paintRows() {
    rows.textContent = '';
    var q = search.value.trim().toLowerCase();
    var list = data.accounts.filter(function (a) { return !q || a.email.indexOf(q) >= 0; });
    list.forEach(function (a) { rows.appendChild(rowFor(a)); });
    if (!list.length) rows.appendChild(el('li', { class: 'acc-none', text: q ? 'No email matches' : 'Nobody else can sign in yet' }));
    var members = data.accounts.filter(function (a) { return !a.owner; }).length;
    var waiting = data.accounts.filter(function (a) { return a.pending; }).length;
    stats.textContent = members + (members === 1 ? ' member' : ' members') + ' besides you' + (waiting ? ' · ' + waiting + ' waiting' : '');
  }

  function paintLocked() {
    locked.textContent = '';
    locked.appendChild(el('div', { class: 'acc-locked-head' }, [icon(LOCK), el('strong', { text: 'Only you' }),
      el('span', { text: 'These sections act as the server itself, so they cannot be given to another account.' })]));
    locked.appendChild(el('ul', { class: 'acc-chips' }, data.owner_only.map(function (s) {
      return el('li', { class: 'acc-chip', title: s.about }, [el('span', { text: s.title }), el('small', { text: s.about })]);
    })));
  }

  function paintLog() {
    logList.textContent = '';
    data.log.forEach(function (item) {
      logList.appendChild(el('li', {}, [el('span', { class: 'acc-log-when', text: ago(item.at) }),
        el('span', { class: 'acc-log-what' }, [el('b', { text: item.email }), document.createTextNode(': ' + item.what)])]));
    });
    if (!data.log.length) logList.appendChild(el('li', { class: 'acc-none', text: 'No changes yet' }));
  }

  function paint() {
    if (!data) return;
    box.style.setProperty('--n', data.sections.length);
    paintNotice(); paintHead(); paintRows(); paintLocked(); paintLog();
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (shown && data && data.bridge.pending) timer = setTimeout(refresh, data.bridge.installed ? 1500 : 6000);
  }

  async function refresh() {
    var stamp = ++epoch;
    try {
      var r = await api('GET', '');
      if (stamp !== epoch) return;
      data = r; fail(''); paint();
    } catch (e) { if (stamp === epoch) { fail(e.message); schedule(); } }
  }

  async function change(account, wanted) {
    if (account.owner || busy[account.email]) return;
    var before = Object.assign({}, account.sections);
    Object.assign(account.sections, wanted);
    busy[account.email] = true; paintRows(); paintHead();
    try {
      var r = await api('PUT', '/' + encodeURIComponent(account.email) + '/sections', { sections: wanted });
      account.sections = r.sections; fail('');
    } catch (e) {
      account.sections = before; fail(e.message);
    }
    delete busy[account.email];
    await refresh();
  }

  async function remove(account) {
    var typed = await P.ask('Remove sign-in for ' + account.email + '? The account loses the portal at once, open pages included. Its chats and files stay on the server. Type the email to confirm:', { value: '', ok: 'Remove', max: 254 });
    if (typed === null) return;
    if (typed.trim().toLowerCase() !== account.email) { fail('The email did not match, nothing was removed.'); return; }
    try { await api('DELETE', '/accounts/' + encodeURIComponent(account.email), { confirm: account.email }); fail(''); }
    catch (e) { fail(e.message); }
    await refresh();
  }

  async function cancelRequest() {
    if (!await P.ask('Cancel the waiting sign-in change?', { ok: 'Cancel change' })) return;
    try { await api('DELETE', '/request'); fail(''); } catch (e) { fail(e.message); }
    await refresh();
  }

  function addDialog() {
    if (!data) return;
    var back = document.activeElement;
    var field = el('input', { type: 'email', class: 'acc-new-email', placeholder: 'name@example.com', autocomplete: 'off', maxlength: '254', 'aria-label': 'Email' });
    var picks = {};
    var presets = [['all', 'Everything a member can have', function () { return true; }],
      ['code', 'Code Web only', function (id) { return id === 'code'; }],
      ['custom', 'Choose sections', null]];
    var choice = 'all';
    var boxes = el('div', { class: 'acc-new-sections' }, data.sections.map(function (s) {
      var c = el('input', { type: 'checkbox', 'data-section': s.id });
      c.checked = true; picks[s.id] = c;
      c.addEventListener('change', function () { setPreset('custom', true); });
      return el('label', { class: 'acc-new-section' }, [c, el('span', {}, [el('b', { text: s.title }), el('small', { text: s.about })])]);
    }));
    var radios = el('div', { class: 'acc-presets', role: 'radiogroup', 'aria-label': 'Sections' }, presets.map(function (p) {
      var b = el('button', { type: 'button', role: 'radio', class: 'acc-preset', 'data-preset': p[0], 'aria-checked': p[0] === choice ? 'true' : 'false', text: p[1] });
      b.addEventListener('click', function () { setPreset(p[0]); });
      return b;
    }));
    function setPreset(id, keep) {
      choice = id;
      radios.querySelectorAll('.acc-preset').forEach(function (b) { b.setAttribute('aria-checked', b.dataset.preset === id ? 'true' : 'false'); });
      var rule = presets.filter(function (p) { return p[0] === id; })[0][2];
      if (rule && !keep) data.sections.forEach(function (s) { picks[s.id].checked = rule(s.id); });
      boxes.classList.toggle('open', id === 'custom');
    }
    var problem = el('p', { class: 'acc-new-error', role: 'alert', hidden: 'hidden' });
    var ok = el('button', { type: 'button', class: 'btn primary acc-new-ok', text: 'Give access' });
    var cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
    var modal = el('div', { class: 'modal acc-new', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Add an email' }, [
      el('div', { class: 'modal-box acc-new-box' }, [
        el('h3', { text: 'Add an email' }),
        el('p', { class: 'acc-new-lead', text: 'The person signs in with this Google account and gets their own chats, prompts, memory and computers.' }),
        field, el('p', { class: 'acc-new-label', text: 'What can they open?' }), radios, boxes,
        el('p', { class: 'acc-new-hint', text: 'Server, console, browser, PC, sync and VPN stay yours. Sections can be changed later.' }),
        problem, el('div', { class: 'row end' }, [cancel, ok])
      ])
    ]);
    function close() { modal.remove(); if (back && back.isConnected && back.focus) back.focus({ preventScroll: true }); }
    async function submit() {
      var email = field.value.trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { problem.textContent = 'Enter an email address.'; problem.hidden = false; field.focus(); return; }
      var sections = {};
      data.sections.forEach(function (s) { sections[s.id] = picks[s.id].checked; });
      ok.disabled = true; ok.textContent = 'Adding…';
      try {
        await api('POST', '/accounts', { email: email, sections: sections });
        close(); await refresh();
      } catch (e) { problem.textContent = e.message; problem.hidden = false; ok.disabled = false; ok.textContent = 'Give access'; }
    }
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', close);
    field.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); submit(); } });
    modal.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(); } });
    modal.addEventListener('click', function (ev) { if (ev.target === modal) close(); });
    document.body.appendChild(modal);
    field.focus();
  }

  P.tabs.access = {
    init: function (node) {
      P.loadCss('/static/access.css');
      node.classList.add('access');
      search = el('input', { type: 'search', class: 'acc-search', placeholder: 'Find an email', 'aria-label': 'Find an email', maxlength: '254' });
      search.addEventListener('input', function () { if (data) paintRows(); });
      var add = el('button', { type: 'button', class: 'btn primary acc-add', text: 'Add email' });
      add.addEventListener('click', addDialog);
      notice = el('div', { class: 'acc-notice' });
      error = el('p', { class: 'acc-error', role: 'alert', hidden: 'hidden' });
      stats = el('span', { class: 'acc-stats' });
      head = el('div', { class: 'acc-cols', 'aria-hidden': 'true' });
      rows = el('ul', { class: 'acc-rows', 'aria-label': 'Accounts' });
      locked = el('section', { class: 'acc-locked', 'aria-label': 'Owner-only sections' });
      logList = el('ol', { class: 'acc-log-list' });
      box = el('div', { class: 'acc-box' }, [
        el('header', { class: 'acc-head' }, [
          el('div', { class: 'acc-title' }, [el('h2', { text: 'Access' }),
            el('p', { class: 'acc-lead', text: 'Who can sign in to the portal, and which sections each account sees. Changes to sections apply at once.' })]),
          el('div', { class: 'acc-tools' }, [search, add])
        ]),
        notice, error,
        el('div', { class: 'acc-table' }, [el('div', { class: 'acc-table-top' }, [stats]), head, rows]),
        locked,
        el('details', { class: 'acc-log' }, [el('summary', { text: 'Recent changes' }), logList])
      ]);
      node.appendChild(box);
      function closeMenus(except) { box.querySelectorAll('.acc-more[open]').forEach(function (m) { if (m !== except) m.open = false; }); }
      document.addEventListener('click', function (ev) { closeMenus(ev.target.closest && ev.target.closest('.acc-more')); });
      box.addEventListener('keydown', function (ev) {
        var menu = ev.target.closest && ev.target.closest('.acc-more[open]');
        if (ev.key === 'Escape' && menu) { ev.preventDefault(); menu.open = false; menu.querySelector('summary').focus(); }
      });
      window.addEventListener('portal-identity-reset', function () { epoch += 1; data = null; rows.textContent = ''; });
    },
    onShow: function () { shown = true; refresh(); },
    onHide: function () { shown = false; clearTimeout(timer); }
  };
})();
