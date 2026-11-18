(function () {
  var P = window.Portal, el = P.el;
  var ui = {}, current = null, epoch = 0, dialogue, pending = Object.create(null);
  var chats = [], lastModel = null;
  var search = { q: '', inside: false, seq: 0, timer: null, found: null };

  function api(method, path, body) { return P.api(method, '/api/backup' + path, body); }
  function status(text) { if (ui.status) ui.status.textContent = text; }

  function markActive() {
    if (!ui.list) return;
    Array.prototype.forEach.call(ui.list.children, function (li) {
      var on = current && li.dataset && li.dataset.id === current.id;
      li.classList.toggle('active', !!on);
      var opener = li.querySelector && li.querySelector('.sess-open');
      if (opener) {
        if (on) opener.setAttribute('aria-current', 'true');
        else if (opener.removeAttribute) opener.removeAttribute('aria-current');
      }
    });
  }

  function open(chat) {
    current = chat;
    epoch++;
    ui.title.textContent = chat.title;
    var hasConsent = (chat.model_selected === 1 || chat.model_selected === true) || (chat.model_selected === undefined && !!chat.model && chat.model !== 'backup');
    var savedModel = hasConsent ? chat.model : '';
    ui.model.value = savedModel;
    if (!ui.model.value) ui.model.value = '';
    ui.effort.value = /^(low|medium|high)$/.test(chat.effort) ? chat.effort : 'default';
    dialogue.select(chat.id, true, chat.id);
    dialogue.connection(!ui.model.disabled && !!ui.model.value && hasConsent);
    // An older chat without a saved model takes the one picked last, as a new chat does (the owner's own choice, not an automatic one).
    if (!hasConsent && lastModel && lastModel.model && !ui.model.disabled && Array.prototype.some.call(ui.model.options, function (o) { return o.value === lastModel.model; })) {
      ui.model.value = lastModel.model;
      ui.effort.value = /^(low|medium|high)$/.test(lastModel.effort) ? lastModel.effort : 'default';
      if (ui.saveModel) ui.saveModel();
    }
    ui.section.classList.remove('side-open');
    showModel();
    markActive();
    watch(chat.id);
  }

  // The label of the model button, like Code Web's: the model's name and the effort when one is chosen.
  function showModel() {
    if (!ui.modelToggle) return;
    var option = ui.model.selectedOptions && ui.model.selectedOptions[0];
    var name = ui.model.value && option ? option.textContent.split(' · ')[0] : 'Choose a model';
    var text = name + (ui.model.value && ui.effort.value !== 'default' ? ' · ' + ui.effort.value.charAt(0).toUpperCase() + ui.effort.value.slice(1) : '');
    ui.modelToggle.replaceChildren(el('span', { class: 'model-label', text: text }));
    ui.modelToggle.title = ui.model.value ? text + ' (' + ui.model.value + ')' : text;
    ui.modelToggle.classList.toggle('model-missing', !ui.model.value);
  }

  // While a turn runs, the reply is read as it is being written and handed to the dialogue the way Code Web's live preview is.
  var watching = null;
  function watch(sid) {
    if (watching && watching.sid === sid) return;
    if (watching) clearTimeout(watching.timer);
    var me = watching = { sid: sid, timer: null, events: -1, quiet: 0 };
    function tick() {
      if (watching !== me) return;
      api('GET', '/chats/' + sid + '/live').then(function (live) {
        if (watching !== me) return;
        if (live.events !== me.events) { me.events = live.events; dialogue.event({ type: 'transcript_ready', sid: sid }); }
        if (live.running && live.text) dialogue.event({ type: 'terminal_preview', sid: sid, turn_id: live.turn_id, stream_id: 's' + live.step, part_id: 0, text: live.text });
        ui.section.classList.toggle('agent-running', !!live.running);
        var row = chats.find(function (c) { return c.id === sid; });
        if (row && !!row.running !== !!live.running) { refresh(); }
        ui.modelStatus.textContent = live.usage && live.usage.context
          ? 'Context ' + kilo(live.usage.context) + ' tokens · this chat: ' + kilo(live.usage.input) + ' in, ' + kilo(live.usage.output) + ' out'
          : ui.providerText || '';
        me.quiet = live.running ? 0 : me.quiet + 1;
        me.timer = setTimeout(tick, live.running ? 350 : document.hidden ? 15000 : me.quiet > 3 ? 4000 : 1000);
      }).catch(function () { if (watching === me) me.timer = setTimeout(tick, 3000); });
    }
    tick();
  }
  function kilo(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n); }
  function wakeWatch() { if (watching) { clearTimeout(watching.timer); watching.quiet = 0; var sid = watching.sid; watching = null; watch(sid); } }

  function snippet(text, q) {
    var span = el('span', { class: 'chat-snippet' });
    var lower = text.toLowerCase(), pos = lower.indexOf(q.toLowerCase());
    if (pos < 0) { span.textContent = text; return span; }
    function txt(s) { return (typeof document !== 'undefined' && document.createTextNode) ? document.createTextNode(s) : el('span', { text: s }); }
    span.appendChild(txt(text.slice(0, pos)));
    span.appendChild(el('mark', { text: text.slice(pos, pos + q.length) }));
    span.appendChild(txt(text.slice(pos + q.length)));
    return span;
  }

  function editChat(c, li, label) {
    if (ui.editing) return;
    var input = el('input', { class: 'chat-edit', type: 'text', 'aria-label': 'Chat name' });
    input.value = c.title;
    ui.editing = input;
    li.classList.add('editing');
    li.insertBefore(input, label);
    label.hidden = true;
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      ui.editing = null;
      var title = input.value.trim();
      input.remove();
      label.hidden = false;
      li.classList.remove('editing');
      if (!save || !title || title === c.title) return;
      label.querySelector('.sess-name').textContent = title;
      api('PATCH', '/chats/' + c.id, { title: title }).then(function (res) {
        c.title = res.title;
        if (current && current.id === c.id) ui.title.textContent = res.title;
        return refresh();
      }).catch(function (e) { status(e.message); renderList(); });
    }
    input.addEventListener('click', function (ev) { ev.stopPropagation(); });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      else if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(false); }
    });
    input.addEventListener('blur', function () { finish(true); });
    input.focus();
    input.select();
  }

  function deleteChat(c) {
    P.ask('Delete “' + c.title + '”? Its history is removed.', { ok: 'Delete' }).then(function (yes) {
      if (!yes) return;
      api('DELETE', '/chats/' + c.id, {}).then(function () {
        if (current && current.id === c.id) {
          current = null;
          ui.title.textContent = 'API Agent';
          ui.model.value = '';
          dialogue.connection(false);
          dialogue.select(null, false, null);
        }
        return refresh();
      }).catch(function (e) { status(e.message); });
    });
  }

  // The same row as a Code Web chat: status dot, name, then a pencil and a bin that show on hover (always on touch screens).
  var ICON_PEN = ['M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z', 'M13.5 8.5l3 3'];
  var ICON_BIN = ['M4 7h16', 'M9 7V4.5h6V7', 'M6.5 7l1 12.5h9l1-12.5', 'M10 11v5M14 11v5'];
  function svgIcon(paths) {
    var NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
    var attrs = { viewBox: '0 0 24 24', width: '15', height: '15', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' };
    Object.keys(attrs).forEach(function (k) { svg.setAttribute(k, attrs[k]); });
    paths.forEach(function (d) { var p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); svg.appendChild(p); });
    return svg;
  }

  function chatRow(c) {
    var label = el('button', { class: 'sess-open chat-open', type: 'button', title: c.title }, [
      el('span', { class: 'sess-name', text: c.title })
    ]);
    label.addEventListener('click', function () { open(c); });
    var pen = el('button', { class: 'chat-act chat-rename', type: 'button', title: 'Rename', 'aria-label': 'Rename ' + c.title });
    pen.appendChild(svgIcon(ICON_PEN));
    var bin = el('button', { class: 'chat-act chat-delete', type: 'button', title: 'Delete', 'aria-label': 'Delete ' + c.title });
    bin.appendChild(svgIcon(ICON_BIN));
    var li = el('li', { class: 'chat-item' + (c.running ? ' live starting' : ' sleeping'), 'data-id': c.id, title: c.running ? 'Working' : '' }, [
      el('span', { class: 'chat-dot', 'aria-hidden': 'true' }),
      label,
      el('span', { class: 'chat-acts' }, [pen, bin])
    ]);
    pen.addEventListener('click', function (ev) { ev.stopPropagation(); editChat(c, li, label); });
    bin.addEventListener('click', function (ev) { ev.stopPropagation(); deleteChat(c); });
    return li;
  }

  function renderList() {
    ui.list.textContent = '';
    var q = search.q.trim().toLowerCase();
    var shown = !q ? chats : chats.filter(function (c) {
      return search.found ? !!search.found[c.id] : c.title.toLowerCase().indexOf(q) >= 0;
    });
    shown.forEach(function (c) {
      var li = chatRow(c);
      var hits = q && search.found && search.found[c.id];
      if (hits && hits.length) li.querySelector('.chat-open').appendChild(snippet(hits[0], q));
      ui.list.appendChild(li);
    });
    if (!chats.length) {
      ui.list.appendChild(el('li', { class: 'muted small chat-none', text: 'No API Agent chats' }));
    } else if (!shown.length) {
      ui.list.appendChild(el('li', { class: 'muted small chat-none chat-nothing' }, [
        el('strong', { text: 'Nothing found' }),
        el('span', { text: search.inside ? 'Try another word.' : 'Turn on “Search inside” to search the conversation text.' })
      ]));
    }
    markActive();
  }

  function runSearch() {
    clearTimeout(search.timer);
    search.found = null;
    var q = search.q.trim();
    renderList();
    var seq = ++search.seq;
    if (!q || !search.inside) return;
    search.timer = setTimeout(function () {
      api('GET', '/chats/search?q=' + encodeURIComponent(q) + '&inside=1').then(function (r) {
        if (seq !== search.seq) return;
        var found = {};
        (r.chats || []).forEach(function (c) { found[c.id] = c.matches || []; });
        search.found = found;
        if (!ui.editing) renderList();
      }).catch(function (e) { if (seq === search.seq) status(e.message); });
    }, 250);
  }

  function searchBox() {
    var input = el('input', { class: 'chat-search-input', type: 'search', placeholder: 'Search chats', 'aria-label': 'Search chats', maxlength: '120' });
    var clear = el('button', { class: 'chat-search-clear', type: 'button', title: 'Clear', 'aria-label': 'Clear search', text: '×', hidden: 'hidden' });
    var box = el('input', { type: 'checkbox', class: 'chat-search-inside' });
    function set(v) { input.value = v; search.q = v; clear.hidden = !v; runSearch(); }
    input.addEventListener('input', function () { set(input.value); });
    input.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && input.value) { ev.preventDefault(); ev.stopPropagation(); set(''); } });
    clear.addEventListener('click', function () { set(''); input.focus(); });
    box.addEventListener('change', function () { search.inside = box.checked; runSearch(); });
    return el('div', { class: 'chat-search' }, [
      el('div', { class: 'chat-search-field' }, [input, clear]),
      el('label', { class: 'chat-search-toggle small' }, [box, el('span', { text: 'Search inside' })])
    ]);
  }

  function refresh() {
    return api('GET', '/chats').then(function (r) {
      chats = r.chats || [];
      lastModel = r.last || null;
      renderList();
      if (!current && chats.length) {
        open(chats[0]);
      } else if (current) {
        var found = chats.find(function (c) { return c.id === current.id; });
        if (found) {
          current = found;
          ui.title.textContent = found.title;
        }
        markActive();
      }
    });
  }

  function create() {
    return api('POST', '/chats', {title: 'New chat'}).then(function (chat) {
      chats.unshift(chat);
      open(chat);
      return refresh();
    }).catch(function (e) { status(e.message); });
  }

  P.tabs.backup = {
    init: function (section) {
      ui.section = section;
      section.classList.add('code', 'backup-agent');
      P.loadCss('/static/code-management.css');
      P.loadCss('/static/code-dialogue.css');
      ui.list = el('ul', {class: 'sess-list chat-list', 'aria-label': 'API Agent chats'});
      var add = el('button', {type: 'button', class: 'btn primary chat-new', text: 'New chat', disabled: 'disabled'});
      add.addEventListener('click', create);
      ui.title = el('strong', {class: 'term-title', text: 'API Agent'});
      ui.status = el('span', {class: 'muted small backup-status', role: 'status'});
      ui.model = el('select', {class: 'active-model', 'aria-label': 'Model', disabled: 'disabled'}, [el('option', {value: '', text: 'Choose a model'})]);
      ui.effort = el('select', {class: 'active-effort', 'aria-label': 'Effort'}, [['default', 'Effort: default'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High']].map(function (v) { return el('option', {value: v[0], text: v[1]}); }));
      ui.effort.value = 'default';
      ui.modelStatus = el('span', {class: 'model-status muted small'});
      ui.effortNote = el('span', {class: 'effort-note muted small', text: 'Effort is sent to models that think before answering; others ignore it.'});

      var configKey = el('input', {
        type: 'password',
        class: 'backup-config-key',
        placeholder: 'API key',
        autocomplete: 'off',
        spellcheck: 'false',
        'aria-label': 'API key'
      });
      var configBase = el('input', {
        type: 'url',
        class: 'backup-config-base',
        placeholder: 'Provider base URL, https://…  (empty = keep current)',
        autocomplete: 'off',
        spellcheck: 'false',
        'aria-label': 'Provider base URL'
      });
      var providerReady = false;
      var keyLine = el('span', {class: 'muted small backup-config-keyline', text: 'Key: no key set'});
      var configSave = el('button', {
        type: 'button',
        class: 'btn backup-config-save',
        text: 'Save key'
      });
      var decisionLine = el('span', {class: 'muted small backup-config-decision-state', text: 'Decision key: …'});
      var decisionKey = el('input', {
        type: 'password',
        class: 'backup-config-key backup-config-decision-key',
        placeholder: 'Google AI Studio API key',
        autocomplete: 'off',
        spellcheck: 'false',
        'aria-label': 'Google AI Studio API key'
      });
      var decisionSave = el('button', {type: 'button', class: 'btn backup-config-save', text: 'Save'});
      var decisionRemove = el('button', {type: 'button', class: 'btn backup-config-save', text: 'Remove', disabled: 'disabled'});
      var configBox = el('div', {
        class: 'backup-config-bar',
        role: 'region',
        'aria-label': 'API Agent settings'
      }, [
        el('div', {class: 'backup-config-section'}, [
          el('strong', {class: 'small', text: 'Chat provider'}),
          keyLine,
          configKey,
          configBase,
          configSave
        ]),
        el('div', {class: 'backup-config-section'}, [
          el('strong', {class: 'small', text: 'Auto model for Code Web'}),
          el('span', {class: 'muted small backup-config-note', text: 'New Code Web chats start as Auto. The first message of a chat (any account\'s) is sent to Google\'s Decision model, which picks the Claude model and effort once; the chat keeps them. Remove the key to turn Auto off.'}),
          decisionLine,
          decisionKey,
          decisionSave,
          decisionRemove
        ])
      ]);
      configBox.hidden = true;

      var settingsBtn = el('button', {
        type: 'button',
        class: 'btn backup-settings-btn',
        text: 'Provider settings',
        title: 'API key and provider settings'
      });
      settingsBtn.addEventListener('click', function () {
        configBox.hidden = !configBox.hidden;
        if (!configBox.hidden) {
          loadDecisionState();
          configKey.focus();
        }
      });

      function saveConfig() {
        var key = configKey.value ? configKey.value.trim() : '';
        if (!key) {
          status('Enter the API key before saving');
          return;
        }
        configSave.disabled = true;
        status('Saving the key…');
        var base = configBase.value ? configBase.value.trim() : '';
        var body = {api_key: key, replace: providerReady};
        if (base) body.base_url = base;
        api('POST', '/config', body).then(function () {
          configKey.value = '';
          configBase.value = '';
          configSave.disabled = false;
          configBox.hidden = true;
          status('Key saved');
          return loadStatusAndModels();
        }).catch(function (e) {
          configKey.value = '';
          configSave.disabled = false;
          var msg = (e && e.message) ? String(e.message) : 'Could not save the configuration';
          if (msg.indexOf(key) >= 0) msg = 'Could not save the configuration';
          status(msg);
        });
      }
      configSave.addEventListener('click', saveConfig);
      configKey.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          saveConfig();
        }
      });

      function loadDecisionState() {
        return api('GET', '/decision-key').then(function (r) {
          var on = !!(r && r.configured);
          decisionLine.textContent = 'Decision key: ' + (on ? 'set' : 'not set');
          decisionRemove.disabled = !on;
        }).catch(function () { decisionLine.textContent = 'Decision key: state unknown'; });
      }

      function saveDecision() {
        var key = decisionKey.value ? decisionKey.value.trim() : '';
        if (!key) {
          status('Enter the Google AI Studio key before saving');
          return;
        }
        decisionSave.disabled = true;
        status('Saving and testing the key…');
        api('PUT', '/decision-key', {api_key: key}).then(function (r) {
          decisionKey.value = '';
          decisionSave.disabled = false;
          var msg;
          if (r && r.checked && r.sample) {
            msg = 'Key saved. Test: a simple request gets ' + r.sample.model.choice + ' / ' + r.sample.effort.choice + '.';
          } else {
            msg = 'Key saved, but the test request failed (' + ((r && r.problem) || 'unknown') + '). Auto will use the default model until it works.';
          }
          status(msg);
          return loadDecisionState();
        }).catch(function (e) {
          decisionKey.value = '';
          decisionSave.disabled = false;
          var msg = (e && e.message) ? String(e.message) : 'Could not save the key';
          if (msg.indexOf(key) >= 0) msg = 'Could not save the key';
          status(msg);
        });
      }
      decisionSave.addEventListener('click', saveDecision);
      decisionKey.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          saveDecision();
        }
      });
      decisionRemove.addEventListener('click', function () {
        P.ask('Turn Auto off and remove the key?', {ok: 'Remove'}).then(function (yes) {
          if (!yes) return;
          return api('DELETE', '/decision-key').then(function () {
            status('Decision key removed. Auto is off.');
            return loadDecisionState();
          }).catch(function (e) { status((e && e.message) ? e.message : 'Could not remove the key'); });
        });
      });

      function saveModel() {
        if (!current) {
          dialogue.connection(false);
          return;
        }
        var model = ui.model.value;
        if (!model) {
          current.model = '';
          current.model_selected = 0;
          dialogue.connection(false);
          status('Choose a model from the list');
          return;
        }
        var sid = current.id;
        var my = epoch;
        dialogue.connection(false);
        showModel();
        return api('PUT', '/chats/' + sid + '/model', {model: model, effort: ui.effort.value}).then(function (res) {
          if (my === epoch && current && current.id === sid) {
            current.model = res.model;
            current.effort = res.effort;
            current.model_selected = 1;
            dialogue.connection(true);
            status('');
          }
          return res;
        }).catch(function (e) {
          if (my === epoch && current && current.id === sid) {
            ui.model.value = (current.model_selected && current.model) ? current.model : '';
            dialogue.connection(!ui.model.disabled && !!ui.model.value && !!current.model_selected);
            status((e && e.message) ? e.message : 'Could not save the model');
          }
        });
      }
      ui.saveModel = saveModel;
      ui.model.addEventListener('change', saveModel);
      ui.effort.addEventListener('change', saveModel);

      var menu = el('button', {class: 'btn', type: 'button', text: 'Chat history'});
      menu.addEventListener('click', function () {
        var navigation = document.getElementById('navigation-open');
        if (navigation && navigation.getAttribute('aria-expanded') === 'false') navigation.click();
        else section.classList.toggle('side-open');
      });
      var host = el('div', {class: 'code-dialogue'});
      section.appendChild(el('aside', {class: 'code-side'}, [
        el('div', {class: 'row'}, [add]),
        searchBox(),
        ui.list
      ]));
      ui.modelToggle = el('button', {type: 'button', class: 'model-toggle', 'aria-haspopup': 'true', 'aria-expanded': 'false'});
      ui.modelPanel = el('div', {class: 'model-panel viewport-menu', role: 'group', 'aria-label': 'Model and effort', hidden: 'hidden'}, [ui.model, ui.effort, ui.effortNote, ui.modelStatus, settingsBtn]);
      function placeMenu() {
        if (ui.modelPanel.hidden) return;
        var view = window.visualViewport, width = view ? view.width : innerWidth, height = view ? view.height : innerHeight;
        var left = (view ? view.offsetLeft : 0) + 12, top = (view ? view.offsetTop : 0) + 12, room = Math.max(1, width - 24);
        ui.modelPanel.style.width = Math.min(300, room) + 'px';
        ui.modelPanel.style.maxHeight = Math.max(1, height - 24) + 'px';
        var anchor = ui.modelToggle.getBoundingClientRect(), h = ui.modelPanel.offsetHeight, w = ui.modelPanel.offsetWidth;
        var x = Math.max(left, Math.min(anchor.right - w, left + room - w)), y = anchor.top - h - 8;
        if (y < top && anchor.bottom + 8 + h <= top + height - 24) y = anchor.bottom + 8;
        ui.modelPanel.style.left = x + 'px';
        ui.modelPanel.style.top = Math.max(top, Math.min(y, top + Math.max(0, height - 24 - h))) + 'px';
      }
      function modelMenu(open) {
        ui.modelPanel.hidden = !open;
        ui.modelToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) placeMenu();
      }
      ui.modelToggle.addEventListener('click', function () { modelMenu(ui.modelPanel.hidden); });
      document.addEventListener('click', function (ev) { if (!ui.modelPanel.hidden && !bar.contains(ev.target) && !ev.target.closest('.modal.ask')) modelMenu(false); });
      document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && !ui.modelPanel.hidden) { modelMenu(false); ui.modelToggle.focus(); } });
      window.addEventListener('resize', placeMenu);
      if (window.visualViewport) window.visualViewport.addEventListener('resize', placeMenu);
      var bar = el('div', {class: 'model-bar'}, [ui.modelToggle, ui.modelPanel]);
      showModel();
      section.appendChild(el('div', {class: 'code-main'}, [
        el('p', {class: 'code-primary-note muted small', text: 'API Agent · models through the API · separate history'}),
        el('div', {class: 'term-head'}, [menu, ui.title, ui.status]),
        configBox,
        host
      ]));

      function loadStatusAndModels() {
        return api('GET', '/status').then(function (r) {
          var keyInfo = (r && r.key_status) || 'no key set';
          add.disabled = !r.ready;
          providerReady = !!r.ready;
          keyLine.textContent = 'Key: ' + keyInfo;
          if (!r.ready) {
            configBox.hidden = false;
            loadDecisionState();
            ui.model.disabled = true;
            while (ui.model.options && ui.model.options.length > 1) {
              ui.model.remove(1);
            }
            ui.model.value = '';
            dialogue.connection(false);
            status('Connect the provider: enter the API key below');
            return refresh();
          }
          configBox.hidden = true;
          return api('GET', '/models').then(function (catalog) {
            while (ui.model.options && ui.model.options.length > 1) {
              ui.model.remove(1);
            }
            var models = (catalog && catalog.models) || [], groups = {};
            models.forEach(function (m) {
              var name = m.group || 'Models';
              if (!groups[name]) { groups[name] = el('optgroup', {label: name}); ui.model.appendChild(groups[name]); }
              groups[name].appendChild(el('option', {value: m.id, text: m.label || m.id}));
            });
            ui.providerText = ui.modelStatus.textContent = 'Provider: ' + ((r && r.provider) || 'API') + ' · ' + models.length + ' models';
            ui.model.disabled = !models.length;
            if (!models.length) {
              ui.model.value = '';
              dialogue.connection(false);
              status('The provider lists no models');
            } else {
              if (current && current.model && current.model_selected) {
                ui.model.value = current.model;
              } else {
                ui.model.value = '';
              }
              var canSend = !ui.model.disabled && !!ui.model.value && !!(current && current.model_selected);
              dialogue.connection(canSend);
              status(ui.model.value ? '' : 'Choose a model to start');
              showModel();
            }
            return refresh();
          }).catch(function (e) {
            ui.model.disabled = true;
            ui.model.value = '';
            dialogue.connection(false);
            var err = (e && e.message) ? e.message : 'catalog error';
            status('The model list did not load: ' + err);
            return refresh();
          });
        }).catch(function (e) {
          add.disabled = true;
          ui.model.disabled = true;
          dialogue.connection(false);
          status((e && e.message) ? e.message : 'Connection error');
        });
      }

      (P.touchOnly && P.touchOnly() ? Promise.resolve() : P.loadPromptEditor()).then(function () {
        return P.loadScript('/static/code-voice.js').catch(function () { return null; });
      }).then(function () {
        return P.loadScript('/static/code-dialogue.js');
      }).then(function () {
        dialogue = P.CodeDialogue(host, {
          api: '/api/backup',
          brand: 'API Agent',
          attachments: true,
          maxUpload: 50 * 1024 * 1024,
          usage: false,
          send: function (text) {
            var hasConsent = current && ((current.model_selected === 1 || current.model_selected === true) || (current.model_selected === undefined && !!current.model && current.model !== 'backup'));
            if (ui.model.disabled || !ui.model.value || !current || !hasConsent || current.model !== ui.model.value || pending[current.id]) return false;
            var id = current.id, my = epoch, model = ui.model.value;
            pending[id] = true;
            api('POST', '/chats/' + id + '/messages', {text: text, model: ui.model.value, effort: ui.effort.value}).then(function () {
              if (my === epoch) { current.model = model; status(''); wakeWatch(); }
            }).catch(function (e) { if (my === epoch) status(e.message); }).then(function () { delete pending[id]; });
            return true;
          },
          enter: function () { return false; },
          liveLabel: 'live',
          waiting: function () {
            if (!current) return 'Create a chat to start';
            if (ui.model.disabled) return 'Connect the provider: Provider settings in the model menu';
            if (!ui.model.value) return 'Choose a model to send a message';
            if (!(current.model_selected === 1 || current.model_selected === true)) return 'Saving the model choice…';
            return null;
          },
          stop: function () {
            if (!current) return false;
            api('POST', '/chats/' + current.id + '/stop', {}).then(wakeWatch).catch(function (e) { status(e.message); });
            return true;
          },
          forceClose: function () {
            if (!current) return Promise.resolve(false);
            return api('POST', '/chats/' + current.id + '/stop', {}).then(function (r) { wakeWatch(); return true; }).catch(function () { return false; });
          },
          cwd: function () { return current ? current.id : ''; },
          uploadUrl: function (f) { return '/api/backup/chats/' + encodeURIComponent(f.dir) + '/uploads/raw?name=' + encodeURIComponent(f.file); },
          resume: function () { return Promise.resolve(false); }
        });
        var clip = host.querySelector('.dialogue-attach');
        if (clip) clip.after(bar); else host.querySelector('.dialogue-composer').prepend(bar);
        return loadStatusAndModels();
      }).catch(function (e) { status(e.message); });
    }
  };
})();
