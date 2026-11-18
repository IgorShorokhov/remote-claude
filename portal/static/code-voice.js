(function () {
  var P = window.Portal;
  P.VoiceInput = function (options) {
    var button = options.button, status = options.status, label = options.label, cancelButton = options.cancel;
    var state = 'idle', token = 0, stream = null, recorder = null, timer = null, chunks = [], bytes = 0, started = 0;
    var base = '', selection = null, pending = null, live = null;
    function change(next, text) {
      state = next; status.hidden = next === 'idle'; label.textContent = text || '';
      button.classList.toggle('is-recording', next === 'recording');
      button.classList.toggle('is-recognizing', next === 'recognizing');
      button.setAttribute('aria-pressed', next === 'recording' ? 'true' : 'false');
      button.setAttribute('aria-label', next === 'recording' ? 'Finish dictation' : 'Voice input');
      button.title = next === 'recording' ? 'Finish dictation' : 'Voice input';
      button.disabled = next === 'permission' || next === 'recognizing' || next === 'retry';
      options.retry.hidden = next !== 'retry';
      cancelButton.textContent = next === 'recognizing' ? 'Don’t insert' : 'Cancel';
      options.state(next !== 'idle');
    }
    function release() {
      clearInterval(timer); timer = null;
      if (stream) stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    // Dictation through the chat's own Claude Code: the audio is streamed to the server, which feeds Anthropic's speech service.
    function liveClose(send) {
      var session = live; live = null;
      clearInterval(timer); timer = null;
      if (!session) return;
      try { if (send) session.ws.send(JSON.stringify({type: send})); } catch (e) {}
      try { session.node.disconnect(); session.ctx.close(); } catch (e) {}
      session.media.getTracks().forEach(function (track) { track.stop(); });
      if (send !== 'stop') { try { session.ws.close(); } catch (e) {} }
    }
    function liveStop() {
      if (!live) return;
      var session = live;
      clearInterval(timer); timer = null;
      change('recognizing', 'Transcribing…');
      try { session.node.disconnect(); } catch (e) {}
      session.media.getTracks().forEach(function (track) { track.stop(); });
      try { session.ws.send(JSON.stringify({type: 'stop'})); } catch (e) {}
    }
    function liveStart(name, my, media) {
      return new Promise(function (resolve) {
        var done = false;
        var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/sessions/' + encodeURIComponent(name) + '/dictate' + (P.account ? '?account=' + encodeURIComponent(P.account) : ''));
        ws.binaryType = 'arraybuffer';
        var give = setTimeout(function () { finish(false, ''); }, 8000);
        function finish(ok, reason) {
          if (done) return;
          done = true; clearTimeout(give);
          if (!ok) { try { ws.close(); } catch (e) {} if (reason) options.warn(reason); }
          resolve(ok);
        }
        ws.onopen = function () { ws.send(JSON.stringify({type: 'start'})); };
        ws.onerror = function () { finish(false, ''); };
        ws.onclose = function () {
          if (!done) finish(false, '');
          else if (live && live.ws === ws && my === token) { liveClose(); if (state !== 'idle') { change('idle'); options.warn('Dictation was interrupted'); } }
        };
        ws.onmessage = function (event) {
          var message; try { message = JSON.parse(event.data); } catch (e) { return; }
          if (my !== token) { try { ws.close(); } catch (e) {} return; }
          if (message.type === 'ready' && !done) {
            var AudioContextClass = window.AudioContext || window.webkitAudioContext;
            var ctx = new AudioContextClass({sampleRate: 16000});
            ctx.audioWorklet.addModule(P.asset('/static/dictation-worklet.js')).then(function () {
              // Cancelled while starting: cancel() already reset the UI, so only release what was opened.
              if (my !== token) {
                try { ctx.close(); } catch (e) {}
                try { ws.close(); } catch (e) {}
                try { media.getTracks().forEach(function (track) { track.stop(); }); } catch (e) {}
                finish(false, '');
                return;
              }
              var node = new AudioWorkletNode(ctx, 'dictation-down', {processorOptions: {rate: ctx.sampleRate}});
              node.port.onmessage = function (block) { if (ws.readyState === 1) ws.send(block.data); };
              ctx.createMediaStreamSource(media).connect(node);
              live = {ws: ws, ctx: ctx, node: node, media: media};
              started = Date.now(); change('recording', 'Listening… · 00:00');
              timer = setInterval(function () {
                var seconds = Math.floor((Date.now() - started) / 1000);
                if (state !== 'recording') return;
                label.textContent = 'Listening… · ' + String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
                if (seconds >= 175) liveStop();
              }, 1000);
              finish(true);
            }, function () { try { ctx.close(); } catch (e) {} finish(false, ''); });
          } else if (message.type === 'unavailable' || message.type === 'busy') {
            finish(false, message.type === 'busy' ? 'Another dictation is running. Try again in a moment.' : '');
          } else if (message.type === 'final' && live && live.ws === ws) {
            liveClose(); var text = String(message.text || '').trim(); change('idle');
            if (text) insert(text); else options.warn('No speech recognized. Try speaking closer to the microphone.');
          } else if (message.type === 'error' && live && live.ws === ws) {
            liveClose(); change('idle'); options.warn(message.reason || 'Dictation failed');
          }
        };
      });
    }
    function cancel() {
      token += 1; liveClose('cancel'); release();
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      recorder = null; chunks = []; bytes = 0; pending = null; change('idle');
    }
    function anchor() {
      base = options.input.value;
      selection = options.input.promptEditor ? options.input.promptEditor.state.selection.getBookmark() : {from:options.input.selectionStart,to:options.input.selectionEnd};
    }
    function insert(text) {
      var input = options.input, editor = input.promptEditor;
      input.focus();
      if (input.value === base && selection) {
        if (editor) {
          try { editor.view.dispatch(editor.state.tr.setSelection(selection.resolve(editor.state.doc))); } catch (e) {}
        } else input.setSelectionRange(selection.from, selection.to);
      } else if (editor && !editor.state.selection.empty) editor.commands.setTextSelection(editor.state.selection.to);
      else if (!editor && input.selectionStart !== input.selectionEnd) input.setSelectionRange(input.selectionEnd, input.selectionEnd);
      if (editor) {
        var before = editor.state.doc.textBetween(Math.max(0, editor.state.selection.from - 1), editor.state.selection.from);
        var after = editor.state.doc.textBetween(editor.state.selection.to, Math.min(editor.state.doc.content.size, editor.state.selection.to + 1));
        var value = (before && !/\s/.test(before) ? ' ' : '') + text + (after && !/^[\s.,!?;:)]/.test(after) ? ' ' : '');
        editor.commands.insertContent({type:'text',text:value});
      } else {
        var start = input.selectionStart, end = input.selectionEnd;
        var prefix = start && !/\s/.test(input.value[start-1]) ? ' ' : '';
        var suffix = input.value[end] && !/[\s.,!?;:)]/.test(input.value[end]) ? ' ' : '';
        input.setRangeText(prefix + text + suffix, start, end, 'end');
      }
      input.dispatchEvent(new Event('input', {bubbles:true})); options.changed();
    }
    function recognize(data, format, my, path) {
      if (my !== token) return;
      change('recognizing', 'Transcribing…');
      pending = {data:data,format:format,path:path};
      var payload = {format:format,language:'auto'};
      if (path) payload.path = path; else payload.audio = data;
      return P.api('POST', '/api/speech', payload).then(function (result) {
        if (my !== token) return;
        var text = result && typeof result.text === 'string' ? result.text.trim() : '';
        pending = null; change('idle');
        if (text) insert(text); else options.warn('No speech recognized. Try speaking closer to the microphone.');
      }, function (error) { if (my === token) { change('retry', 'You can retry transcription'); options.warn(error.message || 'Could not transcribe speech'); } });
    }
    function encode(blob) {
      return new Promise(function (resolve, reject) {
        var reader = new FileReader(); reader.onerror = reject;
        reader.onload = function () { resolve(String(reader.result).split(',')[1]); };
        reader.readAsDataURL(blob);
      });
    }
    function stop() {
      if (live) { liveStop(); return; }
      if (!recorder || recorder.state === 'inactive') return;
      clearInterval(timer); timer = null; change('recognizing', 'Preparing the recording…'); recorder.stop(); release();
    }
    async function start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
        options.warn('This browser cannot record the microphone. Attach an MP3 and press “Transcribe”.'); return;
      }
      var my = ++token; anchor(); options.warn(''); change('permission', 'Allow microphone access…');
      try {
        var media = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
        if (my !== token) { media.getTracks().forEach(function (track) { track.stop(); }); return; }
        var name = options.session && options.session();
        if (name && window.WebSocket && (window.AudioContext || window.webkitAudioContext) && window.AudioWorkletNode) {
          change('permission', 'Starting dictation…');
          if (await liveStart(name, my, media)) return;
          if (my !== token) { media.getTracks().forEach(function (track) { track.stop(); }); return; }
        }
        stream = media; chunks = []; bytes = 0;
        var mime = ['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(function (type) { return typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported(type); });
        recorder = new MediaRecorder(media, mime ? {mimeType:mime,audioBitsPerSecond:64000} : {});
        var current = recorder;
        current.ondataavailable = function (event) {
          if (my !== token || !event.data.size) return;
          bytes += event.data.size;
          if (bytes > 5 * 1024 * 1024) { cancel(); options.warn('The recording is over 5 MiB. Record a shorter message.'); return; }
          chunks.push(event.data);
        };
        current.onerror = function () { if (my === token) { cancel(); options.warn('Could not record the microphone. Try again.'); } };
        current.onstop = function () {
          if (my !== token) return;
          var blob = new Blob(chunks, {type:current.mimeType || mime || 'audio/webm'}); chunks = []; release(); recorder = null;
          if (!blob.size) { change('idle'); options.warn('The recording is empty'); return; }
          var format = /mp4/.test(blob.type) ? 'mp4' : /ogg/.test(blob.type) ? 'ogg' : 'webm';
          encode(blob).then(function (data) { return recognize(data, format, my); }, function () { if (my === token) { change('idle'); options.warn('Could not read the recording'); } });
        };
        current.start(1000); started = Date.now(); change('recording', 'Recording · 00:00');
        timer = setInterval(function () {
          var seconds = Math.floor((Date.now() - started) / 1000);
          label.textContent = 'Recording · ' + String(Math.floor(seconds/60)).padStart(2,'0') + ':' + String(seconds%60).padStart(2,'0');
          if (seconds >= 179) stop();
        }, 1000);
      } catch (error) {
        if (my !== token) return;
        cancel();
        options.warn(error.name === 'NotAllowedError' ? 'Microphone access is blocked. Allow it in the site settings.' : error.name === 'NotFoundError' ? 'No microphone found.' : 'Could not start the microphone. Check that no other app is using it.');
      }
    }
    button.addEventListener('click', function () { if (state === 'recording') stop(); else if (state === 'idle') start(); });
    cancelButton.addEventListener('click', cancel);
    options.retry.addEventListener('click',function () { if (state === 'retry' && pending) { options.warn(''); recognize(pending.data,pending.format,++token,pending.path); } });
    document.addEventListener('keydown', function (event) { if (state !== 'idle' && event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); cancel(); } }, true);
    window.addEventListener('pagehide', cancel);
    return {cancel:cancel, busy:function () { return state !== 'idle'; }, file:function (path) {
      if (state !== 'idle') { options.warn('Finish the current dictation first'); return; }
      anchor(); options.warn(''); return recognize(null, 'mp3', ++token, path);
    }};
  };
})();
