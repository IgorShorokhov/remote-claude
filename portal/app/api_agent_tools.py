"""The tools of API Agent (decision 227): what Claude Code gives Claude, for models behind an OpenAI-compatible API.

Every tool runs on this server as the portal's user, starting in the chat's own folder (~/work/api-agent/<chat>), the way a Code Web chat
runs in its folder. The portal service itself is sandboxed (most of the disk is read-only to it), so commands and file writes run through
the tmux server, like the Console tab's shells: with the same rights as Claude Code. Reading needs no detour.
A tool returns text for the model; read_file of a picture also returns the picture, which the loop shows to the model.
"""
import base64
import fnmatch
import html
import json
import os
import re
import signal
import subprocess
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import config, skills

OUTPUT_BYTES = 200_000
SHOWN = 30_000
READ_LINES = 2000
LINE_CHARS = 2000
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}
IMAGE_MAX = 5 * 1024 * 1024
FETCH_MAX = 2_000_000
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.cache'}
SEARCH_MODEL = 'perplexity/sonar'


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
                                             'parameters': {'type': 'object', 'properties': properties, 'required': required}}}


TOOLS = [
    schema('bash', 'Run a bash command on this server (Ubuntu VPS) in the chat folder. The working folder carries over between calls. '
                   'Output over 30 000 characters is cut. Prefer read_file/edit_file/glob/grep for files.',
           {'command': {'type': 'string'},
            'timeout': {'type': 'integer', 'description': 'Seconds, default 120, at most 600.'}}, ['command']),
    schema('read_file', 'Read a text file with line numbers (up to 2000 lines per call, use offset for more). A picture (png, jpg, gif, webp) '
                        'is shown to you as an image.',
           {'path': {'type': 'string', 'description': 'Absolute, or relative to the chat folder.'},
            'offset': {'type': 'integer', 'description': 'First line, from 1.'},
            'limit': {'type': 'integer'}}, ['path']),
    schema('write_file', 'Create or overwrite a file with this content. Folders are created.',
           {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
    schema('edit_file', 'Replace an exact piece of a file. old_string must occur exactly once unless replace_all is true; read the file first.',
           {'path': {'type': 'string'}, 'old_string': {'type': 'string'}, 'new_string': {'type': 'string'},
            'replace_all': {'type': 'boolean'}}, ['path', 'old_string', 'new_string']),
    schema('glob', 'Find files by name pattern, for example **/*.py; newest first, at most 200.',
           {'pattern': {'type': 'string'}, 'path': {'type': 'string', 'description': 'Folder to search, default the chat folder.'}}, ['pattern']),
    schema('grep', 'Search file contents with a regular expression; returns path:line:text, at most 200 matches.',
           {'pattern': {'type': 'string'}, 'path': {'type': 'string'}, 'include': {'type': 'string', 'description': 'File name pattern, e.g. *.js'},
            'ignore_case': {'type': 'boolean'}}, ['pattern']),
    schema('web_fetch', 'Download a web page (http or https) and return its text.',
           {'url': {'type': 'string'}}, ['url']),
    schema('web_search', 'Search the web; returns a short answer with source links.',
           {'query': {'type': 'string'}}, ['query']),
    schema('skill', 'Load a skill: ready instructions for a kind of task. Load every skill whose description fits before you start.',
           {'name': {'type': 'string'}}, ['name']),
]


class Stopped(Exception):
    pass


class Result:
    def __init__(self, text, error=False, image=None):
        self.text, self.error, self.image = text, error, image


PATH = '/home/claude/.local/bin:/usr/local/bin:/usr/bin:/bin'
LAUNCH = """cd "$2" 2>/dev/null || cd "$3"
echo $$ > "$1/pid"
export HOME="$4" PATH="$5" LANG=C.UTF-8
umask 022
set -o pipefail
bash "$1/job.sh" < "$1/in" 2>&1 | head -c {limit} > "$1/out"
echo "${{PIPESTATUS[0]}}" > "$1/code.tmp" && mv "$1/code.tmp" "$1/code"
"""


def folder(path: Path) -> Path:
    """mkdir -p with usable modes: the portal service runs with umask 0117, which would leave new folders without the x bit."""
    for part in reversed([path, *path.parents]):
        if not part.exists():
            part.mkdir()
            os.chmod(part, 0o700)
        elif part.is_dir() and not part.stat().st_mode & 0o100 and part.stat().st_uid == os.getuid():
            os.chmod(part, 0o700)  # made earlier under that umask
    return path


class Workspace:
    """One chat's folder, its remembered working folder and the command running in it (so Stop can end it)."""

    def __init__(self, root: Path, cancel: threading.Event, via=None):
        self.root = root
        self.cwd = root
        self.cancel = cancel
        self.via = via or os.environ.get('PORTAL_API_AGENT_EXEC', 'tmux')
        self.job = None
        folder(root)

    def path(self, value):
        target = Path(os.path.expanduser(str(value)))
        return target if target.is_absolute() else self.cwd / target

    def kill(self):
        job = self.job
        if job is None:
            return
        try:
            os.killpg(int((job / 'pid').read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        if self.via == 'tmux':
            subprocess.run([config.TMUX_BIN, '-N', '-S', config.TMUX_SOCKET, 'kill-session', '-t', '=' + job.name],
                           capture_output=True, timeout=5)

    def run(self, script, timeout, data=b''):
        """Runs a bash script outside the portal's sandbox; returns (output bytes, exit code or None on timeout). Raises Stopped."""
        job = folder(self.root.parent / '.jobs' / ('aj-' + uuid.uuid4().hex[:12]))  # next to the chat folders, writable by the portal
        (job / 'job.sh').write_text(script)
        (job / 'in').write_bytes(data)
        (job / 'launch.sh').write_text(LAUNCH.format(limit=OUTPUT_BYTES))
        args = ['bash', str(job / 'launch.sh'), str(job), str(self.cwd), str(self.root), str(config.HOME), PATH]
        self.job = job
        try:
            if self.via == 'tmux':
                done = subprocess.run([config.TMUX_BIN, '-N', '-S', config.TMUX_SOCKET, 'new-session', '-d', '-s', job.name,
                                       ' '.join(quote(a) for a in args)], capture_output=True, timeout=10)
                if done.returncode:
                    raise OSError('could not start the command through tmux: ' + done.stderr.decode('utf-8', 'replace')[:200])
            else:
                subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline, pause = time.monotonic() + timeout, 0.02
            while not (job / 'code').exists():
                if self.cancel.wait(pause):
                    self.kill()
                    raise Stopped()
                if time.monotonic() > deadline:
                    self.kill()
                    time.sleep(0.2)
                    return self.read(job / 'out'), None
                pause = min(pause * 1.5, 0.2)
            code = (job / 'code').read_text().strip()
            return self.read(job / 'out'), int(code) if code.lstrip('-').isdigit() else 1
        finally:
            self.job = None
            if self.via == 'tmux':
                subprocess.run([config.TMUX_BIN, '-N', '-S', config.TMUX_SOCKET, 'kill-session', '-t', '=' + job.name], capture_output=True, timeout=5)
            moved = job / 'cwd'
            try:
                place = Path(moved.read_text().strip()) if moved.exists() else None
                if place and place.is_dir():
                    self.cwd = place
            except OSError:
                pass
            shutil.rmtree(job, ignore_errors=True)

    @staticmethod
    def read(path):
        try:
            return path.read_bytes()
        except OSError:
            return b''


def cut(text, limit=SHOWN):
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n… (cut, {len(text) - limit} more characters)'


def bash(ws, command, timeout=120):
    timeout = max(1, min(int(timeout or 120), 600))
    script = command + '\n__status=$?\npwd > "$(dirname "$0")/cwd" 2>/dev/null\nexit $__status\n'
    out, code = ws.run(script, timeout)
    text = out.decode('utf-8', 'replace').rstrip()
    if code is None:
        return Result(cut(text) + f'\n(stopped after {timeout} s)', error=True)
    if code:
        text = (text + '\n' if text else '') + f'(exit code {code})'
    return Result(cut(text) or '(no output)', error=code != 0)


def quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def read_file(ws, path, offset=1, limit=READ_LINES):
    target = ws.path(path)
    if not target.is_file():
        return Result(f'Not a file: {target}', error=True)
    kind = IMAGE_TYPES.get(target.suffix.lower())
    if kind:
        size = target.stat().st_size
        if size > IMAGE_MAX:
            return Result(f'The picture is {size} bytes, over 5 MB; make a smaller copy first.', error=True)
        data = base64.b64encode(target.read_bytes()).decode()
        return Result(f'Picture {target} ({size} bytes), shown below.', image={'url': f'data:{kind};base64,{data}', 'path': str(target)})
    raw = target.read_bytes()
    if b'\0' in raw[:8000]:
        return Result(f'{target} is a binary file ({len(raw)} bytes); use bash to inspect it.', error=True)
    lines = raw.decode('utf-8', 'replace').splitlines()
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or READ_LINES), READ_LINES))
    part = lines[start - 1:start - 1 + count]
    body = '\n'.join(f'{n:6}\t{line[:LINE_CHARS]}' for n, line in enumerate(part, start))
    more = len(lines) - (start - 1 + len(part))
    if more > 0:
        body += f'\n… {more} more lines; read on with offset={start + len(part)}'
    return Result(cut(body, 120_000) if body else '(empty file)')


def put(ws, target, data: bytes):
    """Writes a file through the runner: the portal itself may not write outside its own folders."""
    out, code = ws.run(f'mkdir -p {quote(str(target.parent))} && cat > {quote(str(target))}\n', 60, data)
    if code != 0:
        raise OSError(out.decode('utf-8', 'replace').strip() or f'could not write {target}')


def write_file(ws, path, content):
    target = ws.path(path)
    put(ws, target, content.encode('utf-8'))
    return Result(f'Wrote {len(content.encode())} bytes to {target}')


def edit_file(ws, path, old_string, new_string, replace_all=False):
    target = ws.path(path)
    if not target.is_file():
        return Result(f'Not a file: {target}', error=True)
    text = target.read_text(encoding='utf-8', errors='surrogateescape')
    found = text.count(old_string) if old_string else 0
    if not found:
        return Result('old_string was not found; read the file and copy the text exactly.', error=True)
    if found > 1 and not replace_all:
        return Result(f'old_string occurs {found} times; give more context or set replace_all.', error=True)
    changed = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    put(ws, target, changed.encode('utf-8', 'surrogateescape'))
    return Result(f'Replaced {found if replace_all else 1} occurrence(s) in {target}')


def walk(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root) / name


def glob(ws, pattern, path=None):
    base = ws.path(path) if path else ws.cwd
    if not base.is_dir():
        return Result(f'Not a folder: {base}', error=True)
    try:
        found = [p for p in base.glob(pattern) if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(base).parts)]
    except (ValueError, NotImplementedError) as error:
        return Result(f'Bad pattern: {error}', error=True)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    lines = [str(p) for p in found[:200]]
    return Result('\n'.join(lines) + (f'\n… {len(found) - 200} more' if len(found) > 200 else '') if lines else 'No files found')


def grep(ws, pattern, path=None, include=None, ignore_case=False):
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as error:
        return Result(f'Bad regular expression: {error}', error=True)
    base = ws.path(path) if path else ws.cwd
    files = [base] if base.is_file() else walk(base) if base.is_dir() else []
    out = []
    for file in files:
        if include and not fnmatch.fnmatch(file.name, include):
            continue
        try:
            if file.stat().st_size > 5_000_000:
                continue
            with open(file, 'rb') as stream:
                head = stream.read(8000)
                if b'\0' in head:
                    continue
                text = (head + stream.read()).decode('utf-8', 'replace')
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                out.append(f'{file}:{n}:{line[:300]}')
                if len(out) >= 200:
                    return Result('\n'.join(out) + '\n… stopped at 200 matches')
    return Result('\n'.join(out) if out else 'No matches')


def page_text(body: str) -> str:
    body = re.sub(r'(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>', ' ', body)
    body = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>', '\n', body)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body))
    return re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t\r\f\v]+', ' ', body)).strip()


def web_fetch(ws, url):
    if not re.match(r'https?://', url or ''):
        return Result('Only http and https addresses.', error=True)
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers={'User-Agent': 'Mozilla/5.0 (portal API Agent)'}) as client, \
                client.stream('GET', url) as response:
            data = b''
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > FETCH_MAX or ws.cancel.is_set():
                    break
            kind = response.headers.get('content-type', '')
            status = response.status_code
    except httpx.HTTPError as error:
        return Result(f'Could not fetch {url}: {type(error).__name__}', error=True)
    text = data.decode('utf-8', 'replace')
    if 'html' in kind or text.lstrip()[:15].lower().startswith(('<!doctype', '<html')):
        text = page_text(text)
    return Result(cut(f'HTTP {status} {url}\n\n{text}'), error=status >= 400)


def web_search(ws, query, ask=None):
    if ask is None:
        return Result('Web search is not available.', error=True)
    try:
        data = ask({'model': SEARCH_MODEL, 'messages': [{'role': 'user', 'content': query}], 'stream': False})
        message = data['choices'][0]['message']
        links = data.get('citations') or message.get('citations') or []
    except Exception:
        return Result('Web search failed; try web_fetch on a known page.', error=True)
    text = message.get('content') or ''
    if links:
        text += '\n\nSources:\n' + '\n'.join(f'[{i}] {u}' for i, u in enumerate(links[:20], 1) if isinstance(u, str))
    return Result(cut(text) or 'No answer')


def skill(ws, name):
    for item in skills.catalog():
        if item['name'] == name or Path(item['path']).parent.name == name:
            path = Path(item['path'])
            return Result(f'Skill folder: {path.parent}\n\n' + cut(path.read_text(encoding='utf-8', errors='replace'), 80_000))
    return Result(f'No skill named {name}. Skills: ' + ', '.join(i['name'] for i in skills.catalog()), error=True)


HANDLERS = {'bash': bash, 'read_file': read_file, 'write_file': write_file, 'edit_file': edit_file, 'glob': glob, 'grep': grep,
            'web_fetch': web_fetch, 'web_search': web_search, 'skill': skill}
SUMMARY_KEYS = ('command', 'path', 'pattern', 'url', 'query', 'name')


def summary(args: dict) -> str:
    """The line shown on the tool card, like Code Web shows a command or a file path."""
    for key in SUMMARY_KEYS:
        if isinstance(args.get(key), str):
            return args[key]
    return json.dumps(args, ensure_ascii=False)


def run(ws, name, raw_args, ask=None):
    if name not in HANDLERS:
        return Result(f'Unknown tool {name}. Tools: ' + ', '.join(HANDLERS), error=True)
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
    except ValueError as error:
        return Result(f'Bad arguments for {name}: {error}', error=True)
    if name == 'web_search':
        args = {**args, 'ask': ask}
    try:
        return HANDLERS[name](ws, **args)
    except Stopped:
        raise
    except TypeError as error:
        return Result(f'Bad arguments for {name}: {error}', error=True)
    except OSError as error:
        return Result(f'{name} failed: {error}', error=True)
