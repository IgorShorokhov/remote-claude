import codecs
from collections import OrderedDict
import re
import unicodedata

from .cli_preview_rules import (background_hint, confirmed_tool_heading, progress_line, running_clock, service_line, shell_body, thinking_summary,
                                tool_heading, tool_summary, without_clock, working_line)


def continued(text, head):
    old = text.split('\n')
    solid = [row for row in head if row.strip()]
    for trim in range(4):
        kept = old[:len(old) - trim]
        base = [row for row in kept if row.strip()]
        for size in range(min(len(base), len(solid)), 0, -1):
            if base[-size:] == solid[:size] and any(len(row) >= 12 for row in solid[:size]):
                last = solid[size - 1]
                skip = next(n for n, row in enumerate(head) if row == last and [r for r in head[:n + 1] if r.strip()] == solid[:size])
                return '\n'.join(kept + head[skip + 1:]).rstrip()[:32768]
    return None


class TerminalScreen:
    def __init__(self, cols=500, rows=500):
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.cols, self.rows = cols, rows
        self.lines = {}
        self.x = self.y = 0
        self.top, self.bottom = 0, rows - 1
        self.saved = (0, 0)
        self.mode = self.escape = ''
        self.active = False
        self.dirty = False
        self.blocks = OrderedDict()
        self.services = OrderedDict()
        self.activity = None
        self.ident = 0
        self.capture_preview = False
        self.preview = None
        self.preview_blocked = False
        self.kept = None
        self.assistants = ()
        self.stream = None
        self.tool_titles = ()
        self.last_prompt = ""
        self.saw_prompt = False

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.top, self.bottom = 0, rows - 1
        self.x, self.y = min(self.x, cols - 1), min(self.y, rows - 1)
        self.lines = {y: text[:cols] for y, text in self.lines.items() if y < rows}

    def scroll(self, direction=1):
        old = self.lines.copy()
        for y in range(self.top, self.bottom + 1):
            source = y + direction
            self.lines[y] = old.get(source, '') if self.top <= source <= self.bottom else ''
        self.dirty = True

    def down(self):
        if self.y == self.bottom:
            self.scroll()
        else:
            self.y = min(self.rows - 1, self.y + 1)

    def csi(self, value):
        command, raw = value[-1], value[:-1]
        if command in 'mhlnt' or raw.startswith(('?', '>', '<', '=')):
            if raw == '?1049' and command == 'h':
                self.lines.clear()
                self.x = self.y = 0
            return
        try:
            args = [int(n or 0) for n in raw.split(';')]
        except ValueError:
            return
        if any(n < 0 for n in args):
            return
        n = args[0] or 1
        if command in 'GHfdr':
            self.active = True
        if command == 'A':
            self.y = max(0, self.y - n)
        elif command in 'Be':
            self.y = min(self.rows - 1, self.y + n)
        elif command == 'C':
            self.x = min(self.cols - 1, self.x + n)
        elif command == 'D':
            self.x = max(0, self.x - n)
        elif command in 'G`':
            self.x = min(self.cols - 1, n - 1)
        elif command in 'Hf':
            self.y = min(self.rows - 1, n - 1)
            self.x = min(self.cols - 1, (args[1] or 1) - 1) if len(args) > 1 else 0
        elif command == 'd':
            self.y = min(self.rows - 1, n - 1)
        elif command == 'K':
            line = self.lines.get(self.y, '')
            self.lines[self.y] = (' ' * (self.x + 1) + line[self.x + 1:]) if args[0] == 1 else '' if args[0] == 2 else line[:self.x]
            self.dirty = True
        elif command in '@PXb':
            line = self.lines.get(self.y, '').ljust(self.x)
            if command == '@':
                line = line[:self.x] + ' ' * n + line[self.x:]
            elif command == 'P':
                line = line[:self.x] + line[self.x + n:]
            elif command == 'X':
                line = line[:self.x] + ' ' * n + line[self.x + n:]
            elif self.x:
                line = line[:self.x] + line[self.x - 1] * n + line[self.x + n:]
                self.x = min(self.cols - 1, self.x + n)
            self.lines[self.y] = line[:self.cols].rstrip(' ') if command != 'b' else line[:self.cols]
            self.dirty = True
        elif command in 'LM':
            if self.top <= self.y <= self.bottom:
                old = self.lines.copy()
                for y in range(self.y, self.bottom + 1):
                    source = y - n if command == 'L' else y + n
                    self.lines[y] = old.get(source, '') if self.y <= source <= self.bottom else ''
                self.x = 0
                self.dirty = True
        elif command in 'EF':
            self.y = min(self.rows - 1, self.y + n) if command == 'E' else max(0, self.y - n)
            self.x = 0
        elif command == 'J':
            if args[0] in (2, 3):
                self.lines.clear()
            else:
                for y in list(self.lines):
                    if y > self.y and args[0] == 0 or y < self.y and args[0] == 1:
                        del self.lines[y]
                self.csi(str(args[0]) + 'K')
            self.dirty = True
        elif command == 'r':
            self.top = min(self.rows - 1, n - 1)
            self.bottom = min(self.rows - 1, (args[1] or self.rows) - 1) if len(args) > 1 else self.rows - 1
            self.x = self.y = 0
        elif command in 'ST':
            for _ in range(min(n, self.rows)):
                self.scroll(1 if command == 'S' else -1)
        elif command == 's':
            self.saved = (self.x, self.y)
        elif command == 'u':
            self.x, self.y = self.saved

    def feed(self, data):
        for ch in self.decoder.decode(data):
            if self.mode == 'string':
                if ch == '\x07' or self.escape == '\x1b' and ch == '\\':
                    self.mode = self.escape = ''
                else:
                    self.escape = '\x1b' if ch == '\x1b' else ''
                continue
            if self.mode == 'charset':
                self.mode = ''
                continue
            if self.mode == 'esc':
                self.escape += ch
                if len(self.escape) == 2:
                    if ch in ']P_^X':
                        self.mode, self.escape = 'string', ''
                    elif ch in '()*+':
                        self.mode, self.escape = 'charset', ''
                    elif ch != '[':
                        if ch == '7':
                            self.saved = (self.x, self.y)
                        elif ch == '8':
                            self.x, self.y = self.saved
                        elif ch == 'M':
                            if self.y == self.top:
                                self.scroll(-1)
                            else:
                                self.y = max(0, self.y - 1)
                        elif ch in 'DE':
                            if ch == 'E':
                                self.x = 0
                            self.down()
                        elif ch == 'c':
                            self.lines.clear()
                            self.x = self.y = 0
                            self.top, self.bottom = 0, self.rows - 1
                            self.dirty = True
                        self.mode = self.escape = ''
                elif '@' <= ch <= '~':
                    self.csi(self.escape[2:])
                    self.mode = self.escape = ''
                elif len(self.escape) >= 64:
                    self.mode = self.escape = ''
                continue
            if ch == '\x1b':
                self.mode, self.escape = 'esc', ch
            elif ch == '\r':
                self.x = 0
            elif ch == '\n':
                self.down()
            elif ch == '\b':
                self.x = max(0, self.x - 1)
            elif ch == '\t':
                self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            elif ch >= ' ' and not 127 <= ord(ch) <= 159:
                line = self.lines.get(self.y, '')
                if unicodedata.combining(ch):
                    continue
                if self.x >= self.cols:
                    self.x = 0
                    self.down()
                    line = self.lines.get(self.y, '')
                width = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
                line = line.ljust(self.x)
                self.lines[self.y] = (line[:self.x] + ch + ('\x00' if width == 2 else '') + line[self.x + width:])[:self.cols]
                self.x += width
                self.dirty = True

    def description(self, rows):
        """First and last row of the description of a running tool at the end of rows; its wrapped rows fill the line up to the edge."""
        last = len(rows) - 1
        while last >= 0 and not rows[last].strip():
            last -= 1
        if last < 0:
            return None
        start = last
        while start > 0 and rows[start - 1].strip() and rows[start].split() and self.wrapped(rows[start - 1], rows[start]):
            start -= 1
        return start, last

    def wrapped(self, above, below):
        # The CLI breaks a line where the next word no longer fits, so a row that could still take it is the end of its paragraph.
        return len(above.rstrip()) + 2 + 1 + len(below.split()[0]) > self.cols

    def _leading(self):
        out = []
        for y in sorted(self.lines):
            line = self.lines[y].replace('\x00', '').replace('\xa0', ' ').rstrip()
            text = line.strip()
            if not text:
                if out:
                    out.append('')
                continue
            if not line.startswith('  ') or text.startswith('⎿') or service_line(text) or tool_summary(text):
                if text.startswith('⎿') and out:
                    out.pop()
                break
            out.append(line[2:])
        return out

    def events(self, event, tool_re, tools):
        if not self.dirty:
            return []
        self.dirty = False
        out, block, prompt = [], None, self.last_prompt if self.capture_preview else ''
        preview = None
        preview_blocked = False
        kept = None
        assistants = []
        opened = False
        head = self._leading() if self.capture_preview else []
        counts = {}
        activity = None

        def finish():
            nonlocal block, preview, preview_blocked, kept, opened
            if not block:
                return
            kind, heading, body, anchor = block
            if kind == 'assistant' and self.capture_preview:
                span = self.description(body)
                if span and running_clock(body[span[1]]):
                    body = body[:span[0]]
                    while body and not body[-1].strip():
                        body = body[:-1]
                    if not body:
                        block = None
                        return
            if kind == 'assistant' and thinking_summary('\n'.join(body)):
                block = None
                return
            opened = True
            if kind == 'assistant' and self.capture_preview and shell_body(body):
                kind, heading = 'tool', ('Bash', body[0])
            text = '\n'.join(body).rstrip()[:32768]
            key = (kind, anchor, heading)
            counts[key] = counts.get(key, 0) + 1
            key += (counts[key],)
            previous = self.blocks.get(key)
            if previous is None:
                previous = next((v for v in reversed(self.blocks.values())
                                 if (not anchor or not v['anchor'] or v['anchor'] != anchor
                                     and (anchor.startswith(v['anchor']) or v['anchor'].startswith(anchor)))
                                 and v['kind'] == kind and v['text'] == text and v['heading'] == heading), None)
            if previous:
                ident = previous['id']
            else:
                self.ident += 1
                ident = 'screen-' + str(self.ident)
            if kind == 'assistant':
                if self.capture_preview:
                    preview = kept = {'id': ident, 'text': text, 'prompt': anchor}
                    preview_blocked = False
                    assistants.append(text)
                if previous is None:
                    out.append(event('assistant', id=ident, text=text))
                elif previous['text'] != text:
                    out.append(event('assistant_replace', message_id=ident, text=text))
            elif kind == 'summary':
                if self.capture_preview:
                    preview = None; preview_blocked = True
                if previous is None:
                    out.append(event('tool', id=ident, name='Claude tools', arguments=heading, summary=True))
            else:
                if self.capture_preview:
                    preview = None; preview_blocked = True
                name, arguments = heading
                if previous is None:
                    out.append(event('tool', id=ident, name=name, arguments=arguments))
                if text and (previous is None or previous['text'] != text):
                    out.append(event('tool_result', tool_id=ident, text=text, status='error_observed' if re.match(r'(?i)(error:|ошибка:)', text) else 'output_received'))
            for old_key, old in list(self.blocks.items()):
                if old['id'] == ident and old_key != key:
                    del self.blocks[old_key]
            self.blocks[key] = {'id': ident, 'text': text, 'kind': kind, 'heading': heading, 'anchor': anchor}
            self.blocks.move_to_end(key)
            while len(self.blocks) > 200:
                self.blocks.popitem(last=False)
            block = None

        for y in sorted(self.lines):
            line = self.lines[y].replace('\x00', '').replace('\xa0', ' ').rstrip()
            text = line.strip()
            if re.match(r'^❯\s*\d{1,2}\.\s', text):
                self.saw_prompt = True
                finish()
                continue
            if text.startswith('❯') or text.startswith('> '):
                self.saw_prompt = True
                finish()
                submitted = text[1:].strip()[:1024]
                if submitted:
                    prompt = submitted
                    if self.capture_preview:
                        self.last_prompt = submitted
                        preview = kept = None
                        preview_blocked = False
                        assistants.clear()
                        opened = True
                elif not self.capture_preview:
                    prompt = ''
                continue
            if not text:
                if block:
                    block[2].append('')
                continue
            if progress_line(text):
                finish()
                continue
            if self.capture_preview and background_hint(text.removeprefix('⎿')):
                continue
            if self.capture_preview and service_line(text.removeprefix('⎿').strip()):
                finish()
                continue
            if re.match(r'^[─━═]{3,}', text) or re.search(r'(auto mode on|shift\+tab to cycle|bypass permissions|ctrl\+o to expand|esc to interrupt|Showing detailed transcript|◐ .* /effort)', text, re.I):
                finish()
                if 'esc to interrupt' in text.lower():
                    activity = 'thinking_observed'
                continue
            if re.match(r'^(?:Ran |Called ).*(?:command|tool|Read|file)', text) or not text.startswith(('● ', '⏺ ')) and tool_summary(text.removeprefix('⎿').strip()):
                finish()
                block = ['summary', text, [], prompt]
                finish()
                continue
            if re.match(r'^[✻✢✶✽✳*·].*(?:…|for \d|done \d)', text) or re.match(r'^\d{1,2}:\d\d [AP]M claude-', text):
                finish()
                if working_line(text):
                    activity = 'thinking_observed'
                continue
            if re.search(r'Interrupted\s*[·—]|interrupted by user', text, re.I):
                finish()
                activity = 'interrupted_observed'
                continue
            match = tool_re.fullmatch(text)
            native_tool = tool_heading(text[2:], tools, self.tool_titles) if self.capture_preview and text.startswith(('● ', '⏺ ')) else None
            if self.capture_preview and not text.startswith(('● ', '⏺ ')) and confirmed_tool_heading(text.removeprefix('⎿').strip(), self.tool_titles):
                native_tool = ('CLI tool', text)
            if text in ('●', '⏺'):
                finish()
                continue
            if native_tool:
                finish()
                block = ['tool', native_tool, [], prompt]
            elif match and (match[1] in tools or match[1].startswith('mcp__')):
                finish()
                block = ['tool', (match[1], match[2]), [], prompt]
            elif text.startswith(('● ', '⏺ ')):
                finish()
                block = ['assistant', '', [text[2:]], prompt]
            elif self.capture_preview and block and block[0] == 'assistant' and text.startswith('⎿'):
                rows = block[2]
                while rows and not rows[-1].strip():
                    rows.pop()
                span = self.description(rows)
                heading = without_clock(' '.join(row.strip() for row in rows[span[0]:]) if span else '')
                if span:
                    del rows[span[0]:]
                if any(row.strip() for row in rows):
                    finish()
                block = ['tool', ('CLI tool', heading), [without_clock(text[1:].strip())], prompt]
            elif block and (line.startswith('  ') or text.startswith('⎿')):
                value = without_clock(text[1:].strip()) if text.startswith('⎿') else line[2:]
                block[2].append(value)
            else:
                finish()
                if text not in self.services:
                    out.append(event('service', text=line))
                    self.services[text] = True
                    while len(self.services) > 100:
                        self.services.popitem(last=False)
        finish()
        if activity and activity != self.activity:
            out.append(event('activity', state=activity, text='From terminal screen'))
        self.activity = activity
        if self.capture_preview:
            grown = continued(self.stream['text'], head) if self.stream and head else None
            if grown:
                stream = dict(self.stream, text=grown)
                assistants.insert(0, grown)
                if not opened:
                    preview = stream
                if preview_blocked and not kept:
                    kept = stream
                self.stream = stream
            if preview:
                self.stream = preview
            self.preview = preview
            self.preview_blocked = preview_blocked
            self.kept = kept
            self.assistants = tuple(assistants)
        return out
