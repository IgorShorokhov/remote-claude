(function () {
  var P = window.Portal, el = P.el;
  var section, side, list, title, editor, status, error, copyVersion, empty, documentBox, createButton;
  var files = [], current = null, dirty = false, conflict = false, loading = false, saving = null, timer = null;
  var epoch = 0, edits = 0, shown = false;
  function key() { return P.account ? 'portal-prompts-draft:' + P.account : null; }
  function backup(clear) {
    var name = key();
    if (!name || P.identityLocked) return;
    try {
      if (clear) sessionStorage.removeItem(name);
      else if (current && dirty) sessionStorage.setItem(name, JSON.stringify({id:current.id, revision:current.revision, title:title.value, body:editor.value}));
    } catch (e) { if (!clear) fail('The local copy is unavailable. Keep this page open until it is saved on the server.'); }
  }
  function localDraft() {
    try {
      var item = key() && JSON.parse(sessionStorage.getItem(key()));
      return item && /^[0-9a-f]{32}$/.test(item.id) && /^[0-9a-f]{32}$/.test(item.revision) && typeof item.title === 'string' && typeof item.body === 'string' ? item : null;
    } catch (e) { return null; }
  }
  function fail(message) { error.textContent = message; error.hidden = !message; copyVersion.hidden = !conflict; }
  function busy(on) {
    loading = on; createButton.disabled = on;
    if (editor) editor.promptEditor.setEditable(!on);
    title.disabled = on;
  }
  function paint() {
    list.textContent = '';
    files.forEach(function (file) {
      var button = el('button', {type:'button', class:'prompt-open sess-open', 'data-id':file.id, title:file.title}, [el('span', {class:'sess-name', text:file.title})]);
      button.addEventListener('click', function () { select(file.id); });
      list.appendChild(el('li', {class:'prompt-item' + (current && file.id === current.id ? ' active' : '')}, [button]));
    });
    empty.hidden = !!current;
    documentBox.hidden = !current;
  }
  function updateFile(file) {
    files = files.filter(function (item) { return item.id !== file.id; });
    files.unshift({id:file.id,title:file.title,revision:file.revision,modified:file.modified});
    files.sort(function (a,b) { return b.modified-a.modified; });
    paint();
  }
  function change() {
    if (!current || loading || P.identityLocked) return;
    dirty = true; edits += 1; status.textContent = 'Not saved'; backup(false);
    clearTimeout(timer);
    if (!conflict) timer = setTimeout(save, 700);
  }
  function save() {
    clearTimeout(timer);
    if (P.identityLocked) return Promise.resolve(false);
    if (saving) return saving.then(function (ok) { return ok && dirty ? save() : ok; });
    if (!current || !dirty) return Promise.resolve(true);
    if (conflict) return Promise.resolve(false);
    var stamp = epoch, version = edits, id = current.id;
    var body = {title:title.value.trim() || 'New prompt',body:editor.value,revision:current.revision};
    status.textContent = 'Saving…';
    saving = P.api('PUT', '/api/prompts/' + id, body).then(function (file) {
      if (stamp !== epoch || !current || current.id !== id) return false;
      current = file; dirty = edits !== version;
      if (!dirty) title.value = file.title;
      status.textContent = dirty ? 'Not saved' : 'Saved'; fail('');
      updateFile(file); backup(!dirty);
      return true;
    }, function (err) {
      if (stamp !== epoch) return false;
      conflict = err.status === 409;
      status.textContent = 'Not saved';
      fail(err.message + ' Your text stays in the editor.'); backup(false);
      return false;
    }).finally(function () { saving = null; });
    return saving.then(function (ok) { return ok && dirty ? save() : ok; });
  }
  function display(file, draft, missing) {
    current = file; title.value = draft ? draft.title : file.title; editor.value = draft ? draft.body : file.body;
    dirty = !!draft; edits += 1; conflict = !!draft && (missing || file.revision !== draft.revision);
    status.textContent = draft ? 'Restored an unsaved draft' : 'Saved';
    fail(conflict ? 'The file changed in another window. Save your draft as a separate copy.' : '');
    paint();
    if (draft && !conflict) timer = setTimeout(save, 700);
  }
  async function select(id, draft) {
    if (loading || (current && current.id === id && !draft)) return;
    busy(true);
    var stamp = epoch;
    try {
      if (!await save() || stamp !== epoch) return;
      var file = await P.api('GET', '/api/prompts/' + id);
      if (stamp === epoch) display(file, draft);
    } catch (err) { if (stamp === epoch) fail(err.message); }
    finally { if (stamp === epoch) busy(false); }
  }
  async function create(asCopy) {
    if (loading) return;
    busy(true);
    var stamp = epoch;
    try {
      if (!asCopy && !await save() || stamp !== epoch) return;
      var body = asCopy ? {title:((title.value.trim() || 'New prompt').slice(0, 90) + ' (copy)'),body:editor.value} : {};
      var file = await P.api('POST', '/api/prompts', body);
      if (stamp !== epoch) return;
      backup(true); conflict = false; display(file); updateFile(file); title.focus(); title.select();
    } catch (err) { if (stamp === epoch) fail(err.message); }
    finally { if (stamp === epoch) busy(false); }
  }
  async function remove() {
    if (!current || loading || !await save()) return;
    var id = current.id, stamp = epoch;
    if (!await P.ask('Delete prompt “' + current.title + '”?', {ok:'Delete'}) || stamp !== epoch) return;
    busy(true);
    try {
      await P.api('DELETE', '/api/prompts/' + id, {confirm:id,revision:current.revision});
      if (stamp !== epoch) return;
      backup(true); files = files.filter(function (item) { return item.id !== id; });
      current = null; dirty = false; conflict = false; editor.value = ''; title.value = ''; fail(''); paint();
    } catch (err) { if (stamp === epoch) fail(err.message); }
    finally { if (stamp === epoch) busy(false); }
  }
  async function refresh() {
    if (!editor || loading || P.identityLocked) return;
    var stamp = epoch;
    try {
      var response = await P.api('GET', '/api/prompts');
      if (stamp !== epoch) return;
      files = response.prompts; paint();
      if (current) return;
      var draft = localDraft();
      if (draft && !files.some(function (file) { return file.id === draft.id; })) {
        display({id:draft.id,revision:draft.revision,title:draft.title,body:draft.body}, draft, true);
      } else if (draft || files.length) select(draft ? draft.id : files[0].id, draft);
    } catch (err) { if (stamp === epoch) fail(err.message); }
  }
  P.tabs.prompts = {
    init: function (node) {
      section = node; section.classList.add('prompts');
      P.loadCss('/static/code-dialogue.css'); P.loadCss('/static/prompts.css');
      createButton = el('button', {type:'button',class:'btn primary prompt-new',text:'New prompt'});
      createButton.disabled = true;
      createButton.addEventListener('click', function () { create(false); });
      list = el('ul', {class:'sess-list prompt-list','aria-label':'Saved prompts'});
      side = el('aside', {class:'prompt-side'}, [el('div', {class:'row'}, [createButton]),list]);
      title = el('input', {class:'prompt-title','aria-label':'Prompt title',maxlength:'100',placeholder:'Prompt title'});
      title.addEventListener('input', change);
      status = el('span', {class:'prompt-status',role:'status'});
      error = el('p', {class:'prompt-error',role:'alert',hidden:'hidden'});
      copyVersion = el('button', {type:'button',class:'btn',text:'Save a copy',hidden:'hidden'});
      copyVersion.addEventListener('click', function () { create(true); });
      var saveButton = el('button', {type:'button',class:'btn',text:'Save'});
      saveButton.addEventListener('click', save);
      var copyButton = el('button', {type:'button',class:'btn',text:'Copy'});
      copyButton.addEventListener('click', function () {
        if (editor) P.copyText(editor.value).then(function () { status.textContent = 'Copied'; }, function () { fail('Could not copy the text'); });
      });
      var download = el('button', {type:'button',class:'btn',text:'Download .md'});
      download.addEventListener('click', async function () {
        if (!current || !await save()) return;
        var link = el('a', {href:'/api/prompts/' + current.id + '/download',download:current.title + '.md'});
        document.body.appendChild(link); link.click(); link.remove();
      });
      var del = el('button', {type:'button',class:'btn',text:'Delete'}); del.addEventListener('click', remove);
      var header = el('div', {class:'prompt-header'}, [title,status]);
      var bar = el('div', {class:'prompt-actions'}, [saveButton,copyButton,download,del,copyVersion]);
      documentBox = el('div', {class:'prompt-file',hidden:'hidden'}, [header,bar]);
      empty = el('div', {class:'prompt-empty'}, [el('p', {text:'Keep your prompts here'})]);
      section.append(side,error,empty,documentBox);
      P.loadPromptEditor().then(function () {
        if (P.identityLocked) return;
        createButton.disabled = false;
        editor = P.createPromptInput(); editor.setAttribute('aria-label','Prompt text'); editor.placeholder = 'Write a prompt…';
        editor.addEventListener('input', change);
        documentBox.appendChild(el('div', {class:'code-dialogue prompt-document'}, [editor]));
        if (shown) refresh();
      }, function () { empty.textContent = 'The editor did not load. Reload the page.'; });
      section.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
      }, true);
      window.addEventListener('beforeunload', function (event) {
        if (!P.identityLocked && (dirty || saving)) { backup(false); event.preventDefault(); event.returnValue = ''; }
      });
      window.addEventListener('portal-identity-reset', function (event) {
        var name = key();
        if (name && !(event.detail && event.detail.navigation)) try { sessionStorage.removeItem(name); } catch (e) {}
        epoch += 1; clearTimeout(timer); current = null; files = []; dirty = false; conflict = false;
        if (editor) editor.value = ''; title.value = ''; fail(''); paint();
      });
    },
    onShow: function () { shown = true; if (section) refresh(); },
    onHide: function () { shown = false; save(); },
  };
})();
