import json
import os
import time
import stat
import uuid
from pathlib import PurePosixPath

from . import transcript
from .code_events import OutputParser

TAIL = 2 * 1024 * 1024


def normalized(value):
    return ''.join(char.lower() for char in value if char.isalnum())


def related(one, other):
    return bool(one and other) and (one.startswith(other) or other.startswith(one))


class TurnReader:
    def __init__(self, path=None, root=None, sid=None):
        self.path = path
        self.root = root
        self.sid = sid
        self.located = 0
        self.offset = None
        self.turn = None
        self.closed = True
        self.texts = []
        self.tool_titles = []
        self.signature = None
        self.version = 0

    def refresh(self):
        if self.path is None and self.root is not None and time.monotonic() - self.located > .5:
            self.located = time.monotonic()
            self.path = transcript.locate(self.root, self.sid)
        if self.path is None:
            return
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return
            signature = (info.st_ino, info.st_mtime_ns, info.st_size)
            if self.signature == signature:
                return
            if self.offset is None or info.st_size < self.offset or self.signature and self.signature[0] != info.st_ino:
                self.turn = None; self.closed = True; self.texts = []
                self.tool_titles = []
                self.offset = max(0, info.st_size - TAIL)
                stream.seek(self.offset)
                if self.offset:
                    stream.readline()
                    self.offset = stream.tell()
            stream.seek(self.offset)
            while stream.tell() < info.st_size:
                start = stream.tell()
                raw = stream.readline(min(TAIL + 1, info.st_size - start))
                if len(raw) > TAIL and not raw.endswith(b'\n'):
                    while stream.tell() < info.st_size and not raw.endswith(b'\n'):
                        raw = stream.readline(min(65536, info.st_size - stream.tell()))
                    if raw.endswith(b'\n'):
                        self.offset = stream.tell()
                        continue
                    stream.seek(start)
                    break
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    if not raw.endswith(b'\n'):
                        stream.seek(start)
                        break
                    continue
                entries = transcript.entries(row)
                for item in entries:
                    if item['kind'] == 'user':
                        self.turn = item.get('client_id') or item['id'].split(':')[0]
                        self.closed = False; self.texts = []
                        self.tool_titles = []
                    elif item['kind'] == 'assistant':
                        self.texts.append(normalized(item['text']))
                        self.texts = self.texts[-64:]
                        if item.get('final'):
                            self.closed = True
                    elif item['kind'] in ('turn_end', 'interrupted', 'turn_error', 'turn_unanswered'):
                        self.closed = True
                message = row.get('message') if isinstance(row, dict) else None
                content = message.get('content') if isinstance(message, dict) else None
                if any(item['kind'] == 'tool' for item in entries) and isinstance(content, list):
                    for part in content:
                        data = part.get('input') if isinstance(part, dict) and part.get('type') == 'tool_use' else None
                        title = data.get('description') if isinstance(data, dict) else None
                        if isinstance(title, str) and title.strip():
                            key = normalized(title)[:2048]
                            if len(key) >= 12:
                                self.tool_titles.append(key)
                        if isinstance(data, dict) and part.get('name') == 'Read' and isinstance(data.get('file_path'), str):
                            path = data['file_path'][:4096]
                            parts = PurePosixPath(path).parts
                            variants = [path] + ['/'.join(parts[-count:]) for count in range(1, min(3,len(parts))+1)]
                            self.tool_titles.extend(normalized('Reading '+value)[:2048] for value in variants)
                    self.tool_titles = self.tool_titles[-64:]
                self.offset = stream.tell()
            self.signature = signature
            self.version += 1


class CliPreview:
    def __init__(self, sid, path=None, cols=80, rows=24, root=None):
        self.sid = sid
        self.stream = uuid.uuid4().hex
        self.parser = OutputParser(cols=cols, rows=rows)
        self.parser.screen.capture_preview = True
        self.reader = TurnReader(path, root, sid)
        self.initialized = False
        self.turn = None
        self.armed = None
        self.baseline_prompt = ''
        self.baseline_text = ''
        self.fresh_prompt = False
        self.sent = None
        self.part = None
        self.parts = 0
        self.baseline_blocks = set()
        self.notified = 0
        self.classified = -1

    def arm(self, turn):
        self.armed = turn
        self._begin(turn)

    def _begin(self, turn):
        self.turn = turn
        self.baseline_prompt = self.parser.screen.last_prompt
        self.baseline_text = normalized((self.parser.screen.preview or {}).get('text', ''))
        self.baseline_blocks = {normalized(value) for value in self.parser.screen.assistants}
        self.parser.screen.stream = None
        self.fresh_prompt = False
        self.sent = None
        self.part = None

    def feed(self, data):
        self.parser.feed(data)

    def resize(self, cols, rows):
        self.parser.screen.resize(cols, rows)

    def _withdraw(self, out, turn):
        if self.sent:
            out.append({'type':'terminal_preview_clear','sid':self.sid,'turn_id':turn,
                        'stream_id':self.stream,'part_id':self.part})
            self.sent = None
        return out

    def flush(self, refresh=True):
        if refresh:
            self.reader.refresh()
        if self.classified != self.reader.version:
            self.classified = self.reader.version
            self.parser.screen.tool_titles = tuple(self.reader.tool_titles)
            self.parser.screen.dirty = True
        self.parser.flush()
        if self.reader.turn == self.armed:
            self.armed = None
        turn = self.armed or self.reader.turn
        if not self.initialized:
            if self.parser.screen.saw_prompt:
                self.initialized = True
            self._begin(turn)
            return []
        if turn != self.turn:
            self._begin(turn)
            return []
        out = []
        if self.reader.version != self.notified and self.reader.turn:
            self.notified = self.reader.version
            out.append({'type':'transcript_ready','sid':self.sid,'turn_id':self.reader.turn})
        if not turn or not self.armed and self.reader.closed:
            return out
        screen = self.parser.screen
        candidate = screen.preview
        if screen.preview_blocked:
            shown = normalized(self.sent[1]) if self.sent else ''
            if shown and not any(related(shown, normalized(value)) for value in screen.assistants):
                return self._withdraw(out, turn)
            candidate = screen.kept
        if self.parser.screen.last_prompt and self.parser.screen.last_prompt != self.baseline_prompt:
            self.fresh_prompt = True
        if not candidate or not candidate['text'].strip():
            return out
        text = normalized(candidate['text'])
        growing = self.baseline_text and text.startswith(self.baseline_text) and len(text) > len(self.baseline_text)
        after_empty = self.baseline_prompt and not self.baseline_text and candidate['prompt'] == self.baseline_prompt
        new_block = not self.armed and self.reader.turn == turn and text not in self.baseline_blocks
        if not self.fresh_prompt and not growing and not after_empty and not new_block:
            return out
        if candidate['prompt'] != self.parser.screen.last_prompt:
            return out
        if any(len(saved) >= 12 and len(text) > len(saved) and text.startswith(saved) for saved in self.reader.texts):
            return self._withdraw(out, turn)
        if any(text == saved or len(text) >= 12 and saved.startswith(text) for saved in self.reader.texts):
            return out
        signature = (turn, candidate['text'])
        if signature == self.sent:
            return out
        shown = normalized(self.sent[1]) if self.sent else ''
        split = bool(shown) and not related(shown, text) and any(related(shown, normalized(value)) for value in screen.assistants)
        if split or not self.part:
            self.parts += 1
            self.part = 'part-' + str(self.parts)
        self.sent = signature
        out.append({'type':'terminal_preview','sid':self.sid,'turn_id':turn,'stream_id':self.stream,
                    'part_id':self.part,'split':split,'text':candidate['text'],'cols':self.parser.screen.cols,'source':'terminal_screen'})
        return out
