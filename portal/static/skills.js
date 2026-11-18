(function () {
  // Skills (decision 228): one set for every account, in ~/.claude/skills. Everyone reads them; only an admin changes them.
  var P = window.Portal, el = P.el;
  var section, side, list, search, main, empty, view, heading, about, problem, chips, editor, status, error, actions, picker, uploader;
  var skills = [], current = null, file = null, dirty = false, busy = false, epoch = 0, shown = false;

  function admin() { return P.role === 'admin'; }
  function api(method, path, body) { return P.api(method, '/api/skills' + path, body); }
  function kb(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B'; }
  function fail(text) { error.textContent = text || ''; error.hidden = !text; }
  function state(text) { status.textContent = text; }

  function paintList() {
    list.textContent = '';
    var q = search.value.trim().toLowerCase();
    var shown = skills.filter(function (s) { return !q || (s.name + ' ' + s.description).toLowerCase().indexOf(q) >= 0; });
    shown.forEach(function (s) {
      var open = el('button', { type: 'button', class: 'skill-open prompt-open sess-open', title: s.description || s.name }, [
        el('span', { class: 'sess-name', text: s.name }),
        el('span', { class: 'skill-line', text: s.problem ? '⚠ ' + s.problem : s.description })
      ]);
      open.addEventListener('click', function () { choose(s.name); });
      list.appendChild(el('li', { class: 'prompt-item skill-item' + (current && current.name === s.name ? ' active' : '') + (s.problem ? ' broken' : '') }, [open]));
    });
    if (!shown.length) list.appendChild(el('li', { class: 'muted small chat-none', text: skills.length ? 'Nothing found' : 'No skills yet' }));
  }

  function paintSkill() {
    empty.hidden = !!current;
    view.hidden = !current;
    if (!current) return;
    heading.textContent = current.name;
    about.textContent = (current.description || 'No description') + ' · ' + current.files + (current.files === 1 ? ' file' : ' files') + ' · ' + kb(current.size);
    problem.textContent = current.problem ? 'Claude will not use this skill as it is: ' + current.problem + '.' : '';
    problem.hidden = !current.problem;
    chips.textContent = '';
    current.list.forEach(function (f) {
      var chip = el('button', { type: 'button', class: 'skill-file' + (file && file.path === f.path ? ' active' : ''), title: f.path + ' · ' + kb(f.size), text: f.path });
      chip.addEventListener('click', function () { openFile(f.path); });
      chips.appendChild(chip);
    });
  }

  async function guard() {
    if (!dirty) return true;
    return !!await P.ask('The file has unsaved changes. Leave without saving?', { ok: 'Leave' });
  }

  async function refresh(pick) {
    var stamp = epoch;
    try {
      var r = await api('GET', '');
      if (stamp !== epoch) return;
      skills = r.skills;
      paintList();
      if (pick) await choose(pick, true);
      else if (!current && skills.length) await choose(skills[0].name, true);
    } catch (e) { if (stamp === epoch) fail(e.message); }
  }

  async function choose(name, force) {
    if (busy || (!force && current && current.name === name && !dirty)) return;
    if (!await guard()) return;
    var stamp = ++epoch;
    try {
      var skill = await api('GET', '/' + encodeURIComponent(name));
      if (stamp !== epoch) return;
      current = skill; file = null; dirty = false; fail('');
      paintList(); paintSkill();
      await openFile('SKILL.md', true);
    } catch (e) { if (stamp === epoch) fail(e.message); }
  }

  async function openFile(path, force) {
    if (!current || (!force && file && file.path === path)) return;
    if (!force && !await guard()) return;
    var stamp = epoch, name = current.name;
    var meta = current.list.find(function (f) { return f.path === path; });
    file = { path: path, revision: null, text: false };
    dirty = false;
    editor.value = '';
    editor.readOnly = true;
    paintSkill();
    if (!meta) { state(''); return; }
    state('Opening…');
    try {
      var r = await api('GET', '/' + encodeURIComponent(name) + '/file?path=' + encodeURIComponent(path));
      if (stamp !== epoch || !file || file.path !== path) return;
      file = { path: path, revision: r.revision, text: true };
      editor.value = r.text;
      editor.readOnly = !admin();
      state(admin() ? 'Saved' : 'Read only: only an admin changes skills');
    } catch (e) {
      if (stamp !== epoch) return;
      editor.value = '';
      state(e.status === 415 || e.status === 413 ? 'Not a text file (' + kb(meta.size) + '): download the skill to get it' : e.message);
    }
    actionsState();
  }

  async function save() {
    if (!admin() || !current || !file || !file.text || !dirty) return true;
    var stamp = epoch, name = current.name, path = file.path, text = editor.value;
    state('Saving…');
    try {
      var r = await api('PUT', '/' + encodeURIComponent(name) + '/file', { path: path, text: text, revision: file.revision });
      if (stamp !== epoch || !file || file.path !== path) return false;
      file.revision = r.revision;
      dirty = editor.value !== text;
      state(dirty ? 'Not saved' : 'Saved');
      current = Object.assign({}, current, r.skill, { list: current.list });
      skills = skills.map(function (s) { return s.name === name ? Object.assign({}, s, r.skill) : s; });
      paintList(); paintSkill(); fail('');
      return true;
    } catch (e) {
      if (stamp === epoch) { state('Not saved'); fail(e.message + ' Your text stays in the editor.'); }
      return false;
    }
  }

  async function newSkill() {
    if (!admin() || !await guard()) return;
    var name = await P.ask('Name of the new skill (lowercase letters, digits and hyphens):', { value: '', ok: 'Next', max: 64 });
    if (!name) return;
    name = name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
    var description = await P.ask('When should Claude use it? One or two sentences:', { value: '', ok: 'Create', max: 1000 });
    if (!description) return;
    try {
      var made = await api('POST', '', { name: name, description: description });
      dirty = false; await refresh(made.name);
    } catch (e) { fail(e.message); }
  }

  async function importArchive(blob) {
    if (!admin() || !blob) return;
    var replace = false;
    state('Importing ' + blob.name + '…');
    for (;;) {
      try {
        var made = await P.sendBytes('/api/skills/import' + (replace ? '?replace=true' : ''), blob);
        dirty = false; await refresh(made.name); state('Imported ' + made.name);
        return;
      } catch (e) {
        if (e.status === 409 && !replace && await P.ask(e.message + '. Replace it? The old one goes to the trash.', { ok: 'Replace' })) { replace = true; continue; }
        state(''); fail(e.message); return;
      }
    }
  }

  async function newFile() {
    if (!admin() || !current || !await guard()) return;
    var path = await P.ask('File path inside the skill, for example references/notes.md:', { value: 'references/', ok: 'Create', max: 300 });
    if (!path) return;
    try {
      await api('PUT', '/' + encodeURIComponent(current.name) + '/file', { path: path.trim(), text: '' });
      var skill = await api('GET', '/' + encodeURIComponent(current.name));
      current = skill; paintSkill(); await openFile(path.trim().replace(/^\.\//, ''), true);
    } catch (e) { fail(e.message); }
  }

  async function uploadFile(blob) {
    if (!admin() || !current || !blob) return;
    var path = await P.ask('Put the file at this path inside the skill:', { value: blob.name, ok: 'Upload', max: 300 });
    if (!path) return;
    state('Uploading…');
    try {
      await P.sendBytes('/api/skills/' + encodeURIComponent(current.name) + '/upload?path=' + encodeURIComponent(path.trim()), blob);
      var skill = await api('GET', '/' + encodeURIComponent(current.name));
      current = skill; paintSkill(); await openFile(path.trim(), true);
    } catch (e) { state(''); fail(e.message); }
  }

  async function removeFile() {
    if (!admin() || !current || !file || file.path === 'SKILL.md') return;
    if (!await P.ask('Delete ' + file.path + ' from ' + current.name + '? It goes to the trash.', { ok: 'Delete' })) return;
    try {
      await api('DELETE', '/' + encodeURIComponent(current.name) + '/file', { confirm: file.path, path: file.path });
      dirty = false; await choose(current.name, true);
    } catch (e) { fail(e.message); }
  }

  async function rename() {
    if (!admin() || !current || !await save()) return;
    var to = await P.ask('New name for ' + current.name + ':', { value: current.name, ok: 'Rename', max: 64 });
    if (!to || to === current.name) return;
    try {
      var r = await api('POST', '/' + encodeURIComponent(current.name) + '/rename', { to: to.trim() });
      await refresh(r.name);
    } catch (e) { fail(e.message); }
  }

  async function remove() {
    if (!admin() || !current) return;
    if (!await P.ask('Delete the skill ' + current.name + ' for every account? It goes to the trash and can be restored from there.', { ok: 'Delete' })) return;
    try {
      await api('DELETE', '/' + encodeURIComponent(current.name), { confirm: current.name });
      current = null; file = null; dirty = false; await refresh();
      paintSkill();
    } catch (e) { fail(e.message); }
  }

  function download() {
    if (!current) return;
    var link = el('a', { href: '/api/skills/' + encodeURIComponent(current.name) + '/download', download: current.name + '.skill' });
    document.body.appendChild(link); link.click(); link.remove();
  }

  var buttons = {};
  function button(id, text, handler, adminOnly) {
    var b = el('button', { type: 'button', class: 'btn skill-' + id, text: text });
    b.addEventListener('click', handler);
    b.dataset.admin = adminOnly ? '1' : '';
    buttons[id] = b;
    return b;
  }
  function actionsState() {
    Object.keys(buttons).forEach(function (id) { buttons[id].hidden = buttons[id].dataset.admin === '1' && !admin(); });
    if (buttons.save) buttons.save.disabled = !file || !file.text || !dirty;
    if (buttons.delfile) buttons.delfile.disabled = !file || file.path === 'SKILL.md';
  }

  P.tabs.skills = {
    init: function (node) {
      section = node; section.classList.add('prompts', 'skills');
      P.loadCss('/static/prompts.css'); P.loadCss('/static/skills.css');
      var create = el('button', { type: 'button', class: 'btn primary prompt-new skill-new', text: 'New skill' });
      create.addEventListener('click', newSkill);
      picker = el('input', { type: 'file', accept: '.skill,.zip', hidden: 'hidden' });
      picker.addEventListener('change', function () { var f = picker.files[0]; picker.value = ''; importArchive(f); });
      var importButton = el('button', { type: 'button', class: 'btn skill-import', text: 'Import .skill / .zip' });
      importButton.addEventListener('click', function () { picker.click(); });
      search = el('input', { type: 'search', class: 'chat-search-input', placeholder: 'Search skills', 'aria-label': 'Search skills', maxlength: '80' });
      search.addEventListener('input', paintList);
      list = el('ul', { class: 'sess-list prompt-list skill-list', 'aria-label': 'Skills' });
      side = el('aside', { class: 'prompt-side skill-side' }, [el('div', { class: 'row' }, [create]), el('div', { class: 'row' }, [importButton]), el('div', { class: 'chat-search' }, [el('div', { class: 'chat-search-field' }, [search])]), list, picker]);
      side.querySelectorAll('.row').forEach(function (row) { row.dataset.admin = '1'; });

      heading = el('h2', { class: 'skill-name' });
      about = el('p', { class: 'skill-about' });
      about.title = 'Show the whole description';
      about.addEventListener('click', function () { about.classList.toggle('open'); });
      problem = el('p', { class: 'skill-problem', role: 'alert', hidden: 'hidden' });
      chips = el('div', { class: 'skill-files', role: 'list', 'aria-label': 'Files of the skill' });
      status = el('span', { class: 'prompt-status', role: 'status' });
      error = el('p', { class: 'prompt-error', role: 'alert', hidden: 'hidden' });
      uploader = el('input', { type: 'file', hidden: 'hidden' });
      uploader.addEventListener('change', function () { var f = uploader.files[0]; uploader.value = ''; uploadFile(f); });
      actions = el('div', { class: 'prompt-actions skill-actions' }, [
        button('save', 'Save', save, true), button('newfile', 'New file', newFile, true),
        button('upload', 'Upload file', function () { uploader.click(); }, true), button('delfile', 'Delete file', removeFile, true),
        button('download', 'Download .skill', download, false), button('rename', 'Rename', rename, true), button('delete', 'Delete skill', remove, true), uploader
      ]);
      editor = el('textarea', { class: 'skill-editor', spellcheck: 'false', 'aria-label': 'File text', readonly: 'readonly' });
      editor.addEventListener('input', function () { if (!file || !file.text) return; dirty = true; state('Not saved'); actionsState(); });
      editor.addEventListener('keydown', function (ev) {
        if (ev.key === 'Tab' && !ev.shiftKey && !editor.readOnly) { ev.preventDefault(); editor.setRangeText('  ', editor.selectionStart, editor.selectionEnd, 'end'); editor.dispatchEvent(new Event('input')); }
      });
      view = el('div', { class: 'prompt-file skill-view', hidden: 'hidden' }, [
        el('div', { class: 'skill-head' }, [heading, status]), about, problem, actions, chips, el('div', { class: 'skill-document' }, [editor]),
        el('p', { class: 'skill-hint', text: 'Shared by every account. Changes reach new chats and API Agent at once; a running Code Web chat keeps the skills it started with.' })
      ]);
      empty = el('div', { class: 'prompt-empty' }, [el('p', { text: 'Skills are ready instructions Claude loads for a kind of task. They are the same for every account.' })]);
      section.append(side, error, empty, view);
      section.addEventListener('keydown', function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') { ev.preventDefault(); save(); }
      }, true);
      window.addEventListener('beforeunload', function (ev) { if (dirty) { ev.preventDefault(); ev.returnValue = ''; } });
      window.addEventListener('portal-identity-reset', function () { epoch += 1; skills = []; current = null; file = null; dirty = false; editor.value = ''; paintList(); paintSkill(); });
      actionsState();
    },
    onShow: function () {
      shown = true;
      side.querySelectorAll('[data-admin="1"]').forEach(function (row) { row.hidden = !admin(); });
      actionsState();
      refresh();
    },
    onHide: function () { shown = false; }
  };
})();
