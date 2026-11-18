import codecs
import re
import time

from .cli_preview_rules import working_line
from .code_screen import TerminalScreen

LIMIT = 4096
TOOLS = {'Bash', 'Read', 'Write', 'Edit', 'MultiEdit', 'Glob', 'Grep', 'Task', 'Agent',
         'WebFetch', 'WebSearch', 'NotebookEdit', 'TodoWrite', 'Skill', 'AskUserQuestion'}
TOOL_RE = re.compile(r'[●⏺] ([A-Za-z][A-Za-z0-9_:-]{0,79})\((.*)\)')


class OutputParser:
    def __init__(self, clock=time.monotonic, cols=500, rows=500):
        self.clock = clock
        self.screen = TerminalScreen(cols, rows)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.line = ''
        self.column = 0
        self.escape = ''
        self.mode = ''
        self.seq = 0
        self.tool = None
        self.context = None
        self.repaint = False

    def event(self, kind, **fields):
        self.seq += 1
        return {'v': 1, 'seq': self.seq, 'type': kind, 'source': 'terminal_heuristic', **fields}

    def parse_line(self, line):
        text = line.strip()
        if self.repaint:
            self.repaint = False
            self.context = None
            self.tool = None
            return [self.event('service', text=line)] if text else []
        if text.startswith('❯') or text.startswith('> '):
            self.context = None
            self.tool = None
            return [self.event('activity', state='unknown', text='Claude state unconfirmed'),
                    self.event('service', text=line)]
        match = TOOL_RE.fullmatch(text)
        if match and (match[1] in TOOLS or match[1].startswith('mcp__')):
            event = self.event('tool', id=str(self.seq + 1), name=match[1], arguments=match[2])
            self.tool = (event['id'], self.clock())
            self.context = 'tool'
            return [event]
        if text.startswith('⎿') and self.tool:
            result = text[1:].strip()
            status = 'error_observed' if re.match(r'(?i)(error:|ошибка:)', result) else 'output_received'
            self.context = 'result'
            return [self.event('tool_result', tool_id=self.tool[0], text=result, status=status,
                               duration_ms=max(0, round((self.clock() - self.tool[1]) * 1000)))]
        if text.startswith(('● ', '⏺ ')):
            self.tool = None
            self.context = 'assistant'
            event = self.event('assistant', id=str(self.seq + 1), text=text[2:])
            self.message_id = event['id']
            return [event]
        if 'esc to interrupt' in text.lower() or working_line(text):
            self.context = None
            return [self.event('activity', state='thinking_observed', text=text)]
        if line.startswith('  ') and self.context == 'assistant':
            return [self.event('assistant_delta', message_id=self.message_id, text=line[2:])]
        if line.startswith('  ') and self.context == 'result' and self.tool:
            return [self.event('tool_delta', tool_id=self.tool[0], text=line[2:])]
        if not text and self.context == 'assistant':
            return [self.event('assistant_delta', message_id=self.message_id, text='')]
        if text:
            self.context = None
            self.tool = None
            return [self.event('service', text=line)]
        return []

    def _csi(self, command):
        params = self.escape[2:-1]
        if command == 'm' or command in 'hl' and params.startswith('?'):
            return
        if command == 'K' and params in ('', '0'):
            self.line = self.line[:self.column]
        elif command == 'K' and params == '2':
            self.line = ''
        else:
            self.repaint = True
            self.context = None
            self.tool = None

    def feed(self, data):
        self.screen.feed(data)
        if self.screen.active:
            return []
        events = []
        for ch in self.decoder.decode(data):
            if self.mode == 'string':
                if ch == '\x07' or self.escape == '\x1b' and ch == '\\':
                    self.mode = self.escape = ''
                else:
                    self.escape = '\x1b' if ch == '\x1b' else ''
                continue
            if self.mode == 'esc':
                self.escape += ch
                if len(self.escape) == 2:
                    if ch in ']P_^X':
                        self.mode = 'string'
                        self.escape = ''
                    elif ch != '[':
                        self.mode = self.escape = ''
                        self.repaint = True
                elif '@' <= ch <= '~':
                    self._csi(ch)
                    self.mode = self.escape = ''
                elif len(self.escape) >= 64:
                    self.mode = self.escape = ''
                    self.repaint = True
                continue
            if ch == '\x1b':
                self.mode = 'esc'
                self.escape = ch
            elif ch == '\n':
                events.extend(self.parse_line(self.line))
                self.line = ''
                self.column = 0
            elif ch == '\r':
                self.column = 0
            elif ch == '\b':
                self.column = max(0, self.column - 1)
            elif ch == '\t' or ch >= ' ' and not 127 <= ord(ch) <= 159:
                value = ' ' if ch == '\t' else ch
                self.line = self.line[:self.column] + value + self.line[self.column + 1:]
                self.column += 1
                if len(self.line) >= LIMIT:
                    events.append(self.event('service', text=self.line, truncated=True))
                    self.line = ''
                    self.column = 0
                    self.repaint = True
                    self.context = None
                    self.tool = None
        return events

    def flush(self):
        if self.screen.active:
            return self.screen.events(self.event, TOOL_RE, TOOLS)
        if not self.line:
            return []
        line, self.line = self.line, ''
        self.column = 0
        self.context = None
        self.tool = None
        return [self.event('service', text=line)]
