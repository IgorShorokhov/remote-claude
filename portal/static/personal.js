(function () {
  var P = window.Portal, el = P.el;
  function when(seconds) {
    return new Date(seconds * 1000).toLocaleString('en-GB', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
  }
  var RESULTS = {updated:'memory updated', unchanged:'nothing new to keep', no_changes:'no new messages', failed:'failed, will retry next night'};
  function page(kind, heading, about, placeholder) {
    var section, editor, status, error, reload, details, common, current = null, dirty = false, saving = null, timer = null;
    var epoch = 0, edits = 0, shown = false, loading = false;
    function fail(message, canReload) { error.textContent = message; error.hidden = !message; reload.hidden = !canReload; }
    function detail(doc) {
      if (kind === 'instructions') { common.textContent = doc.common || ''; return; }
      details.textContent = doc.job && doc.job.last_run ? 'Last nightly run: ' + when(doc.job.last_run) + ', ' + (RESULTS[doc.job.result] || doc.job.result) : 'The first nightly run has not happened yet.';
    }
    function change() {
      if (!current || loading || P.identityLocked) return;
      dirty = true; edits += 1; status.textContent = 'Not saved';
      clearTimeout(timer); timer = setTimeout(save, 700);
    }
    function save() {
      clearTimeout(timer);
      if (P.identityLocked) return Promise.resolve(false);
      if (saving) return saving.then(function (ok) { return ok && dirty ? save() : ok; });
      if (!current || !dirty) return Promise.resolve(true);
      var stamp = epoch, version = edits;
      status.textContent = 'Saving…';
      saving = P.api('PUT', '/api/personal/' + kind, {body:editor.value,revision:current.revision}).then(function (doc) {
        if (stamp !== epoch) return false;
        current = doc; dirty = edits !== version;
        status.textContent = dirty ? 'Not saved' : 'Saved'; fail('');
        return true;
      }, function (err) {
        if (stamp !== epoch) return false;
        status.textContent = 'Not saved';
        fail(err.message + ' Your text stays in the editor.', err.status === 409);
        return false;
      }).finally(function () { saving = null; });
      return saving;
    }
    function show(doc) {
      loading = true;
      current = doc; editor.value = doc.body; dirty = false; status.textContent = doc.modified ? 'Saved ' + when(doc.modified) : '';
      loading = false;
      detail(doc); fail('');
    }
    function refresh() {
      if (!editor || dirty || saving || P.identityLocked) return;
      var stamp = epoch;
      P.api('GET', '/api/personal/' + kind).then(function (doc) {
        if (stamp !== epoch || dirty || saving) return;
        if (!current || current.revision !== doc.revision) show(doc); else detail(doc);
      }, function (err) { if (stamp === epoch) fail(err.message); });
    }
    return {
      init: function (node) {
        section = node; section.classList.add('prompts', 'personal');
        P.loadCss('/static/code-dialogue.css'); P.loadCss('/static/prompts.css');
        status = el('span', {class:'prompt-status',role:'status'});
        error = el('p', {class:'prompt-error',role:'alert',hidden:'hidden'});
        reload = el('button', {type:'button',class:'btn',text:'Load the saved version',hidden:'hidden'});
        reload.addEventListener('click', function () { dirty = false; current = null; fail(''); refresh(); });
        var head = el('div', {class:'personal-head'}, [el('h2', {class:'personal-title',text:heading}), status]);
        var box = el('div', {class:'prompt-file'}, [head, el('p', {class:'personal-about',text:about})]);
        if (kind === 'instructions') {
          common = el('pre', {class:'personal-common-text'});
          box.appendChild(el('details', {class:'personal-common'}, [el('summary', {text:'Common part, the same rules for every account (read only)'}), common]));
        } else {
          details = el('p', {class:'personal-about personal-job'});
          box.appendChild(details);
        }
        box.append(error, reload);
        section.appendChild(box);
        P.loadPromptEditor().then(function () {
          if (P.identityLocked) return;
          editor = P.createPromptInput(); editor.setAttribute('aria-label', heading); editor.placeholder = placeholder;
          editor.addEventListener('input', change);
          box.appendChild(el('div', {class:'code-dialogue prompt-document'}, [editor]));
          if (shown) refresh();
        }, function () { fail('The editor did not load. Reload the page.'); });
        section.addEventListener('keydown', function (event) {
          if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
        }, true);
        window.addEventListener('beforeunload', function (event) {
          if (!P.identityLocked && (dirty || saving)) { event.preventDefault(); event.returnValue = ''; }
        });
        window.addEventListener('portal-identity-reset', function () {
          epoch += 1; clearTimeout(timer); current = null; dirty = false;
          if (editor) editor.value = '';
          if (common) common.textContent = '';
          if (details) details.textContent = '';
          fail('');
        });
      },
      onShow: function () { shown = true; if (section) refresh(); },
      onHide: function () { shown = false; save(); }
    };
  }
  P.tabs.instructions = page('instructions', 'System instructions', 'Your personal part. It goes right after the common part in every chat you start. Chats that are already running pick up changes after they restart.', 'Who you are, how Claude should explain and format things…');
  P.tabs.memory = page('memory', 'Memory', 'What Claude remembers about you, added after the system instructions. Every night Claude Haiku reads what changed in your chats and edits this file. You can edit it too.', 'Nothing here yet. It fills up after the first nightly run.');
})();
