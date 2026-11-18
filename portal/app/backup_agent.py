import asyncio
import base64
import fcntl

import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
import io
import json
from pydantic import BaseModel, Field
import shlex
from urllib.parse import quote

from . import api_agent_run, api_agent_tools, config, outbox, uploads


class ChatInput(BaseModel):
    title: str = Field(default='New chat', min_length=1, max_length=120)


class MessageModel(BaseModel):
    model: str = Field(default='', max_length=256)
    effort: str | None = Field(default=None, max_length=16)


class MessageInput(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    model: str = Field(default='', max_length=256)
    effort: str = 'medium'


def transport_key():
    path = key_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o777 != 0o600 or mode.st_uid != os.geteuid():
                raise ValueError('permissions')
            key = stream.read(4097).strip()
        if not key or len(key) > 4096 or not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError('invalid key')
        return key
    except (OSError, ValueError):
        raise HTTPException(503, 'The API Agent key is unavailable') from None


def transport_client():
    return httpx.Client(timeout=300, trust_env=False, follow_redirects=False)


def state_dir():
    path = Path(os.environ.get('PORTAL_BACKUP_STATE', str(config.HOME / '.local/state/agent-portal/backup-agent')))
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError('unsafe backup state directory')
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise OSError('backup state directory changed')
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except OSError:
        raise HTTPException(503, 'API Agent local storage is unavailable') from None
    return path


@contextmanager
def database():
    path = state_dir() / 'chats.sqlite3'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL)')
        if 'model_selected' not in {r[1] for r in db.execute('PRAGMA table_info(chats)')}:
            try:
                db.execute('ALTER TABLE chats ADD COLUMN model_selected INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE chats SET model=''")
            except sqlite3.OperationalError:
                pass  # another request added the column first
        db.execute('CREATE TABLE IF NOT EXISTS events (n INTEGER PRIMARY KEY, sid TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, ts TEXT NOT NULL, client_id TEXT)')
        if 'extra' not in {r[1] for r in db.execute('PRAGMA table_info(events)')}:
            try:
                db.execute("ALTER TABLE events ADD COLUMN extra TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        yield db
        db.commit()
    finally:
        db.close()


def get_chat(db, sid):
    if not re.fullmatch('[0-9a-f]{32}', sid):
        raise HTTPException(404, 'Chat not found')
    row = db.execute('SELECT * FROM chats WHERE id=?', (sid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Chat not found')
    return dict(row)


@contextmanager
def chat_lock(sid):
    with database() as db:
        get_chat(db, sid)
    if api_agent_run.running(sid):
        raise HTTPException(409, 'This API Agent chat is already running a task')
    fd = os.open(state_dir() / (sid + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, 'This API Agent chat is already running a task') from None
        yield
    finally:
        os.close(fd)


def event(db, sid, kind, text, client_id=None, extra=None):
    extra_str = json.dumps(extra) if extra else ''
    db.execute('INSERT INTO events(sid,kind,text,ts,client_id,extra) VALUES (?,?,?,?,?,?)',
               (sid, kind, text, datetime.now(timezone.utc).isoformat(), client_id, extra_str))


PAGE_ITEMS = 200
OUTBOX_ROOT = 'work/api-agent'  # under ~/work: the portal service may write there (decision 227)
ATTACH_FOLDER = 'attachments'
UPLOAD_MAX = 50 * 1024 * 1024


def outbox_path(sid):
    return f'{OUTBOX_ROOT}/{sid}/outbox'


def shell_host():
    """Where API Agent's commands run: this server by default (the owner's Linux laptop, the old target, was wiped on 15 Nov 2026),
    or an SSH host named in PORTAL_BACKUP_SSH_HOST."""
    return os.environ.get('PORTAL_BACKUP_SSH_HOST', 'local')


def shell_argv(command):
    """argv that runs one shell command string on the agent's machine."""
    host = shell_host()
    if host == 'local':
        return ['bash', '-c', command]
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command]


def shell_env():
    return None if shell_host() != 'local' else {**os.environ, 'HOME': str(config.HOME)}


def where():
    return "this server (the portal's VPS)" if shell_host() == 'local' else f"the computer `{shell_host()}` over SSH"


def workspace(sid):
    """The chat's folder: its outbox, attachments and the starting folder of its commands, like a Code Web chat folder."""
    return config.HOME / OUTBOX_ROOT / sid


def api_agent_note(sid):
    from . import skills
    listed = "\n".join(f"- {item['name']}: {item['description']}" for item in skills.catalog())
    return (
        "\n\nYou are API Agent in the web portal: a chat with its own history, on the model the owner picked in the portal. "
        f"Your tools run on {where()}, starting in this chat's folder {workspace(sid)}: bash, read_file, write_file, edit_file, glob, grep, "
        "web_fetch, web_search and skill. Read a file before you edit it; prefer edit_file to rewriting whole files. "
        "Files the owner attaches are in ./attachments; their paths are listed in the first line of the message "
        "(\"Attached files (read them first): ...\"); pictures among them are shown to you."
        + ("\nSkills (load the matching one with the skill tool before you start):\n" + listed if listed else "")
    )


def system_prompt(sid):
    from . import identity, personal
    sections = [identity.common(identity.OWNER, outbox=True, skills=False)] + [title + "\n\n" + identity.neutralize(body) for title, body in personal.parts()]
    return "\n\n".join(sections) + "\n" + api_agent_note(sid)


def pc_script(script, timeout=120):
    cmd = shell_argv('bash -s')
    try:
        proc = subprocess.run(cmd, input=script.encode(), capture_output=True, timeout=timeout, env=shell_env())
        return proc.returncode, proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 124, b''


def attachment_script(sid, name):
    """Shell script run where the agent's commands run: the file arrives on stdin and is kept under the first free name in the chat's attachments folder."""
    root = f'$HOME/{OUTBOX_ROOT}/{sid}'
    return (
        'set -e\n'
        f'd="{root}/{ATTACH_FOLDER}"\n'
        'mkdir -p "$d"\n'
        f'chmod 700 "$HOME/{OUTBOX_ROOT}" "{root}" "$d" 2>/dev/null || true\n'
        't=$(mktemp "$d/.upload-XXXXXX")\n'
        "trap 'rm -f \"$t\"' EXIT\n"
        f'head -c {UPLOAD_MAX + 1} > "$t"\n'
        'size=$(wc -c < "$t")\n'
        f'[ "$size" -le {UPLOAD_MAX} ] || exit 3\n'
        f'n={shlex.quote(name)}\n'
        'case "$n" in *.*) stem=${n%.*}; ext=.${n##*.} ;; *) stem=$n; ext= ;; esac\n'
        'target="$d/$n"; i=1\n'
        'while ! ln "$t" "$target" 2>/dev/null; do i=$((i+1)); [ "$i" -lt 1000 ] || exit 4; target="$d/$stem-$i$ext"; done\n'
        'echo "$target"\n'
    )


def outbox_listing(sid):
    script = f'd="$HOME/{outbox_path(sid)}"\n[ -d "$d" ] || exit 0\nfind "$d" -mindepth 1 -maxdepth 1 -type f -printf "%f\\t%s\\t%T@\\n"\n'
    code, out = pc_script(script)
    if code != 0:
        raise HTTPException(502, 'The agent machine is not reachable; the outbox is unavailable')
    files = []
    for line in out.decode('utf-8', 'replace').split('\n'):
        parts = line.split('\t')
        if len(parts) != 3 or not outbox.valid_name(parts[0]) or not parts[1].isdigit():
            continue
        try:
            mtime = float(parts[2])
        except ValueError:
            mtime = 0.0
        size = int(parts[1])
        files.append({'name': parts[0], 'size': size, 'mtime': mtime, 'type': outbox.kind(parts[0]), 'too_big': size > outbox.MAX_BYTES})
    files.sort(key=lambda f: (f['mtime'], f['name']))
    return files[:outbox.MAX_FILES]

OUTPUT_BYTES = 200_000
OUTPUT_SHOWN = 20_000


def run_pc_command(command: str, timeout: int = 60) -> tuple[str, str, int]:
    # The output is cut where it runs: a command that prints without end must not fill the portal's memory for a minute.
    capped = 'bash -c ' + shlex.quote('set -o pipefail; { ' + command + '\n} 2>&1 | head -c %d' % OUTPUT_BYTES)
    cmd = shell_argv(capped)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=timeout, env=shell_env(),
                              cwd=str(config.HOME) if shell_host() == 'local' else None)
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return '', 'Command timed out after 60 seconds', 124
    except Exception as exc:
        return '', str(exc), 1


DEFAULT_PROVIDER_URL = 'https://plusvibeapi.ru/v1'


def validate_base_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('base_url')
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('base_url')
    try:
        parts.port
    except ValueError:
        raise ValueError('base_url') from None
    return value.rstrip('/')


def provider_path():
    return state_dir() / 'provider.json'


def provider_config():
    path = provider_path()
    if not path.exists():
        return {'base_url': DEFAULT_PROVIDER_URL, 'name': 'plus_vibe_api'}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o777 != 0o600 or mode.st_uid != os.geteuid():
                raise ValueError('permissions')
            data = json.load(stream)
        name = data.get('name')
        if name not in ('plus_vibe_api', 'custom'):
            raise ValueError('name')
        return {'base_url': validate_base_url(data.get('base_url')), 'name': name}
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, 'The API Agent provider settings are unavailable') from None


def write_provider(base_url):
    name = 'plus_vibe_api' if base_url == DEFAULT_PROVIDER_URL else 'custom'
    return json.dumps({'name': name, 'base_url': base_url})


def write_private(target, text):
    temp = target.with_name(f'{target.name}.{uuid.uuid4().hex}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as dest:
        os.fchmod(dest.fileno(), 0o600)
        dest.write(text)
        dest.flush()
        os.fsync(dest.fileno())
    os.replace(temp, target)


def key_path():
    return Path(os.environ.get('PORTAL_BACKUP_KEY_FILE', str(state_dir() / 'plusvibe.key')))


def provider_status():
    ready = False
    key_status = 'no key set'
    if key_path().exists():
        try:
            transport_key()
            ready = True
            key_status = 'key set; checked through the model list'
        except HTTPException:
            key_status = 'key unavailable or unsafe permissions'
    return {'provider': provider_config()['name'], 'ready': ready,
            'provider_status': 'provider configured' if ready else 'provider not connected',
            'key_status': key_status}


def provider_request(method, endpoint, payload=None):
    key = transport_key()
    base_url = provider_config()['base_url']
    try:
        with transport_client() as client:
            response = client.request(method, base_url + endpoint,
                                      headers={'Authorization': 'Bearer ' + key}, json=payload)
            if response.status_code != 200:
                # Never pass the provider's own status on: the page reads 401/403 and redirects from the portal as a lost sign-in.
                raise HTTPException(502, f'Provider HTTP {response.status_code}; request refused, no automatic retry')
            return response.json(), key
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Provider: connection or response format error; request not retried') from None


def available_models():
    data, key = provider_request('GET', '/models')
    try:
        models = list(dict.fromkeys(item['id'] for item in data['data']))
        if len(models) > 10000 or any(not isinstance(m, str) or not m or len(m) > 256 or key in m or any(ord(c) < 32 for c in m) for m in models):
            raise ValueError()
        return models
    except (KeyError, TypeError, ValueError):
        raise HTTPException(502, 'Provider: invalid model list') from None


def rows_of(db, sid):
    out = []
    for r in db.execute('SELECT * FROM events WHERE sid=? ORDER BY n', (sid,)):
        try:
            extra = json.loads(r['extra']) if r['extra'] else {}
        except ValueError:
            extra = {}
        out.append({'n': r['n'], 'kind': r['kind'], 'text': r['text'], 'ts': r['ts'], 'client_id': r['client_id'],
                    'extra': extra if isinstance(extra, dict) else {}})
    return out


SHOWN_KINDS = ('user', 'assistant', 'thinking', 'tool', 'tool_result', 'interrupted')


def page_items(rows, busy):
    """Items in the format of Code Web's transcript (transcript.py), so the same dialogue draws both."""
    items = []
    for r in rows:
        if r['kind'] not in SHOWN_KINDS:
            continue
        extra = r['extra']
        item = {'id': str(r['n']), 'kind': r['kind'], 'text': r['text'], 'ts': r['ts']}
        if r['client_id']:
            item['client_id'] = r['client_id']
        if r['kind'] == 'assistant':
            item['final'] = bool(extra.get('final', 'step' not in extra))
        elif r['kind'] == 'tool':
            item.update({'id': extra.get('id') or item['id'], 'name': extra.get('name') or 'tool',
                         'args': extra.get('args') if 'args' in extra else r['text']})
            item.pop('text')
        elif r['kind'] == 'tool_result':
            item.update({'tool_id': extra.get('tool_id', ''), 'error': bool(extra.get('error'))})
        items.append(item)
    # A turn cut by a portal restart: its question has no final answer and no turn is running.
    if not busy:
        last_user = max((i for i, it in enumerate(items) if it['kind'] == 'user'), default=None)
        if last_user is not None and not any(it['kind'] == 'interrupted' or it.get('final') for it in items[last_user:]):
            items.append({'id': 'cut', 'kind': 'interrupted', 'ts': items[-1]['ts']})
    return items


def transcript_status(items):
    from .transcript import status
    return status([it for it in items if it['kind'] != 'interrupted'], 'thinking')


PICTURE = re.compile(r'(/\S+\.(?:png|jpe?g|gif|webp))(?=[\s,]|$)', re.IGNORECASE)


def attached_pictures(text):
    """Pictures the owner attached to this message go to the model as images (vision models read them; others ignore them)."""
    first = text.split('\n', 1)[0]
    if not first.startswith('Attached files'):
        return []
    out = []
    for path in PICTURE.findall(first)[:8]:
        target = Path(path)
        kind = api_agent_tools.IMAGE_TYPES.get(target.suffix.lower())
        try:
            if kind and target.is_file() and target.stat().st_size <= api_agent_tools.IMAGE_MAX:
                out.append(f'data:{kind};base64,' + base64.b64encode(target.read_bytes()).decode())
        except OSError:
            pass
    return out


@contextmanager
def provider_stream(payload):
    key = transport_key()
    with transport_client() as client, \
            client.stream('POST', provider_config()['base_url'] + '/chat/completions', headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
        yield response


def provider_ask(payload):
    data, _ = provider_request('POST', '/chat/completions', payload)
    return data


VENDORS = (
    ('Anthropic', ('claude', 'anthropic/')), ('OpenAI', ('gpt', 'openai/')), ('Google', ('gemini', 'gemma', 'google/')),
    ('DeepSeek', ('deepseek',)), ('Qwen', ('qwen',)), ('xAI', ('grok', 'x-ai/')), ('Moonshot', ('kimi', 'moonshot')),
    ('Zhipu', ('glm', 'z-ai/')), ('MiniMax', ('minimax',)), ('Xiaomi', ('mimo', 'xiaomi/')), ('Perplexity', ('perplexity/', 'sonar')),
    ('Meta', ('llama',)), ('NVIDIA', ('nemotron',)), ('ByteDance', ('doubao',)), ('Tencent', ('hy3', 'hy4', 'tencent/')),
)
WORDS = {'gpt': 'GPT', 'glm': 'GLM', 'deepseek': 'DeepSeek', 'minimax': 'MiniMax', 'mimo': 'MiMo', 'hy3': 'HY3', 'hy4': 'HY4',
         'llama': 'Llama', 'qwen3.8': 'Qwen3.8', 'qwen3.7': 'Qwen3.7', 'qwen3.6': 'Qwen3.6', 'ai': 'AI'}


def vendor(model):
    low = model.lower()
    return next((name for name, marks in VENDORS if any(m in low for m in marks)), 'Other')


def model_label(model):
    """'anthropic/claude-opus-5' -> 'Claude Opus 5', 'openai/gpt-5.6-luna' -> 'GPT-5.6 Luna', 'deepseek-v4.1-flash' -> 'DeepSeek V4.1 Flash'."""
    name = model.split('/', 1)[-1].split(':', 1)[0]
    words = name.split('-')
    out = []
    for word in words:
        low = word.lower()
        if low in WORDS:
            out.append(WORDS[low])
        elif re.fullmatch(r'v?\d[\d.]*[a-z]?', low):
            out.append(word.upper() if low.startswith('v') else word)
        else:
            out.append(word[:1].upper() + word[1:])
    text = ' '.join(out)
    text = re.sub(r'^GPT (\d)', r'GPT-\1', text)
    return text + (' (free)' if model.endswith(':free') else '')


def catalog(ids):
    """The provider's chat models for the picker: grouped by maker, readable names, without embedding models."""
    rows = [{'id': m, 'label': model_label(m), 'group': vendor(m)} for m in ids if 'embedding' not in m.lower()]
    seen = {}
    for row in rows:
        seen.setdefault(row['label'], []).append(row)
    for same in seen.values():
        if len(same) > 1:
            for row in same:
                row['label'] += ' · ' + row['id']
    order = [name for name, _ in VENDORS] + ['Other']
    return sorted(rows, key=lambda r: (order.index(r['group']), r['label'].lower()))


def last_model_path():
    return state_dir() / 'last-model.json'


def remember_model(model, effort):
    write_private(last_model_path(), json.dumps({'model': model, 'effort': effort}))


def last_model():
    try:
        data = json.loads(last_model_path().read_text())
        return data if isinstance(data, dict) and isinstance(data.get('model'), str) else None
    except (OSError, ValueError):
        return None


DEFAULT_TITLES = ('New chat', 'New API Agent chat')


def short_title(text):
    """A chat is named after its first message, like a Code Web chat: the first line that is not the attachment list."""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('Attached files')]
    title = re.sub(r'\s+', ' ', lines[0] if lines else 'Files')
    return title if len(title) <= 60 else title[:57].rsplit(' ', 1)[0] + '…'


def build_router():
    router = APIRouter(prefix='/api/backup')
    router.add_api_route('/status', provider_status, methods=['GET'])

    @router.get('/decision-key')
    def decision_key_status():
        from . import decision
        return {'configured': decision.configured()}

    @router.put('/decision-key')
    async def decision_key_set(request: Request):
        # The body is read by hand: a validation error of the framework would repeat the submitted key back.
        from . import decision
        try:
            declared = request.headers.get('content-length')
            if declared is not None and (not declared.isdigit() or int(declared) > 8192):
                raise ValueError()
            raw = await request.body()
            body = json.loads(raw)
            if len(raw) > 8192 or not isinstance(body, dict) or set(body) != {'api_key'} or not isinstance(body['api_key'], str):
                raise ValueError()
            key = body['api_key'].strip()
            if not decision.KEY_RE.fullmatch(key):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Invalid key format') from None
        sample, problem = None, None
        try:
            sample = await asyncio.to_thread(decision.check_key, key)
        except decision.DecisionError as error:
            if error.kind == 'rejected':
                raise HTTPException(422, 'Google did not accept this key') from None
            problem = error.kind
        try:
            await asyncio.to_thread(decision.store_key, key)
        except (OSError, HTTPException):
            raise HTTPException(503, 'Configuration storage unavailable') from None
        return {'ok': True, 'checked': problem is None, 'problem': problem, 'sample': sample}

    @router.delete('/decision-key')
    def decision_key_remove():
        from . import decision
        try:
            decision.remove_key()
        except OSError:
            raise HTTPException(503, 'Configuration storage unavailable') from None
        return {'ok': True}

    @router.post('/chats/{sid}/uploads/raw', status_code=201)
    async def upload(sid: str, request: Request, name: str = Query(..., max_length=200)):
        def known():
            with database() as db:
                get_chat(db, sid)
        await asyncio.to_thread(known)
        try:
            safe = uploads.clean_name(name)
        except uploads.UploadError as error:
            raise HTTPException(error.status, error.detail) from None
        declared = request.headers.get('content-length')
        if declared is not None and (not declared.isdigit() or int(declared) > UPLOAD_MAX):
            raise HTTPException(413, 'File is too large (max 50 MB)')
        proc = await asyncio.create_subprocess_exec(
            *shell_argv('bash -c ' + shlex.quote(attachment_script(sid, safe))), env=shell_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > UPLOAD_MAX:
                    raise HTTPException(413, 'File is too large (max 50 MB)')
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            proc.stdin.close()
            out = await asyncio.wait_for(proc.stdout.read(), 60)
            code = await asyncio.wait_for(proc.wait(), 60)
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
            raise HTTPException(502, 'The agent machine is not reachable; the file was not saved') from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        path = out.decode('utf-8', 'replace').strip()
        if code == 3:
            raise HTTPException(413, 'File is too large (max 50 MB)')
        if code != 0 or not path.startswith('/') or '\n' in path or not outbox.valid_name(path.rsplit('/', 1)[-1]):
            raise HTTPException(502, 'The agent machine did not save the file')
        return {'path': path, 'name': path.rsplit('/', 1)[-1], 'size': size}

    @router.post('/config')
    async def configure(request: Request):
        from .backup_config import install
        try:
            raw = await request.body()
            if len(raw) > 8192:
                raise ValueError()
            body = json.loads(raw)
            if not isinstance(body, dict) or 'api_key' not in body or not set(body) <= {'api_key', 'base_url', 'replace'}:
                raise ValueError()
            key = body['api_key']
            if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError()
            replace = body.get('replace', False)
            if not isinstance(replace, bool):
                raise ValueError()
            base_url = validate_base_url(body['base_url']) if 'base_url' in body else None
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Invalid configuration format') from None
        try:
            install(io.StringIO(key), replace=replace, base_url=base_url)
        except ValueError as error:
            if 'подтверждения replace' in str(error):
                raise HTTPException(409, 'A key or provider is already set; tick Replace to change it') from None
            raise HTTPException(409, 'Another key is already set; overwriting is not allowed') from None
        except OSError:
            raise HTTPException(503, 'Configuration storage unavailable') from None
        return {'ok': True}

    @router.put('/chats/{sid}/model')
    def select_model(sid: str, body: MessageModel):
        with chat_lock(sid):
            if not body.model or body.model not in available_models():
                raise HTTPException(422, 'The model is not in the provider list')
            effort = body.effort if body.effort in api_agent_run.EFFORTS + ('default',) else None
            with database() as db:
                if effort is None:
                    effort = get_chat(db, sid)['effort']
                db.execute('UPDATE chats SET model=?,model_selected=1,effort=? WHERE id=?', (body.model, effort, sid))
            remember_model(body.model, effort)
        return {'id': sid, 'model': body.model, 'effort': effort}

    @router.get('/chats')
    def chats():
        with database() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM chats ORDER BY rowid DESC')]
        return {'chats': [{**row, 'running': api_agent_run.running(row['id'])} for row in rows], 'last': last_model()}

    @router.post('/chats', status_code=201)
    def create(body: ChatInput):
        # A new chat starts on the model the owner picked last (the owner's own choice; API Agent has no automatic model choice).
        sid = uuid.uuid4().hex
        last = last_model() or {}
        model, effort = last.get('model') or '', last.get('effort') or 'default'
        with database() as db:
            db.execute('INSERT INTO chats(id,title,model,effort,model_selected) VALUES (?,?,?,?,?)', (sid, body.title, model, effort, 1 if model else 0))
            return get_chat(db, sid)

    @router.patch('/chats/{sid}')
    def rename(sid: str, body: ChatInput):
        with database() as db:
            db.execute('UPDATE chats SET title=? WHERE id=?', (body.title, sid))
            return get_chat(db, sid)

    @router.delete('/chats/{sid}')
    def delete(sid: str):
        with chat_lock(sid), database() as db:
            db.execute('DELETE FROM events WHERE sid=?', (sid,))
            db.execute('DELETE FROM chats WHERE id=?', (sid,))
            Path(state_dir() / (sid + '.lock')).unlink(missing_ok=True)
        return {'ok': True}

    @router.get('/models')
    def models():
        return {'models': catalog(available_models()) if provider_status()['ready'] else [], 'efforts': list(api_agent_run.EFFORTS)}

    @router.get('/sessions/{sid}/prompt')
    def session_prompt(sid: str):
        with database() as db:
            get_chat(db, sid)
        return {'prompt': None}

    @router.get('/sessions/{sid}/transcript')
    def transcript(sid: str, after: int | None = None, before: int | None = None, tail: int = 0, state: str = ''):
        with database() as db:
            get_chat(db, sid)
            rows = rows_of(db, sid)
        items = page_items(rows, api_agent_run.running(sid))
        total = len(items)
        if before is not None:
            end = max(0, min(before, total))
            start = max(0, end - PAGE_ITEMS)
        elif after is not None:
            start = min(max(0, after), total)
            end = total
        else:
            start = max(0, total - PAGE_ITEMS)
            end = total
        busy = api_agent_run.running(sid)
        status = transcript_status(items) if busy else 'idle'
        return {'session_id': sid, 'bound': True, 'file': True, 'size': total, 'next': total, 'start': start, 'items': items[start:end], 'status': status}

    @router.post('/chats/{sid}/messages', status_code=202)
    def send(sid: str, body: MessageInput):
        if not provider_status()['ready']:
            raise HTTPException(503, 'provider not connected · no key set')
        if not body.model:
            raise HTTPException(422, 'Choose a model before sending')
        with chat_lock(sid):
            with database() as db:
                saved = get_chat(db, sid)
                if not saved['model_selected'] or not saved['model'] or saved['model'] != body.model:
                    raise HTTPException(422, 'Save an explicit model choice first')
            if body.model not in available_models():
                raise HTTPException(422, 'The model is not in the provider list')
            match = re.search(r'\n\n<portal-message-id>([0-9a-f-]{36})</portal-message-id>$', body.text)
            text = body.text[:match.start()] if match else body.text
            client_id = match[1] if match else None
            effort = body.effort if body.effort in api_agent_run.EFFORTS else 'default'
            with database() as db:
                if client_id and db.execute('SELECT 1 FROM events WHERE sid=? AND client_id=?', (sid, client_id)).fetchone():
                    raise HTTPException(409, 'The message was already sent; check the history, no automatic retry')
                event(db, sid, 'user', text, client_id)
                db.execute('UPDATE chats SET model=?,effort=? WHERE id=?', (body.model, effort, sid))
                if saved['title'] in DEFAULT_TITLES:
                    db.execute('UPDATE chats SET title=? WHERE id=?', (short_title(text), sid))
                rows = rows_of(db, sid)
            # The prompt reads the account's instructions and memory now, in the request: the turn's thread has no account context.
            prompt = system_prompt(sid)
            request = {'model': body.model, 'effort': effort, 'messages': api_agent_run.history(rows), 'images': attached_pictures(text),
                       'post': provider_stream}

            secret = transport_key()

            def save(kind, text, extra):
                # A model can be tricked into printing the provider key (it is never in its context, but a tool could read it): never store it.
                with database() as db:
                    event(db, sid, kind, text.replace(secret, '[provider key hidden]'), extra=json.loads(json.dumps(extra or {}).replace(secret, '[provider key hidden]')) or None)

            if api_agent_run.start(sid, client_id, request, save, prompt, workspace(sid), provider_ask) is None:
                raise HTTPException(409, 'This API Agent chat is already running a task')
        return {'ok': True, 'started': True}

    @router.post('/chats/{sid}/stop')
    def stop(sid: str):
        with database() as db:
            get_chat(db, sid)
        return {'stopped': api_agent_run.stop(sid)}

    @router.get('/chats/{sid}/live')
    def live(sid: str):
        with database() as db:
            get_chat(db, sid)
            count = db.execute('SELECT COUNT(*) FROM events WHERE sid=?', (sid,)).fetchone()[0]
            spent = [json.loads(r['extra']).get('usage') or {} for r in db.execute("SELECT extra FROM events WHERE sid=? AND kind='usage' ORDER BY n", (sid,))]
        run = api_agent_run.RUNS.get(sid)
        state = run.live() if run else {'running': False}
        state['usage'] = {'context': spent[-1].get('prompt_tokens', 0) if spent else 0,
                          'output': sum(u.get('completion_tokens') or 0 for u in spent), 'input': sum(u.get('prompt_tokens') or 0 for u in spent)}
        if state.get('text'):
            state['text'] = state['text'].replace(transport_key(), '[provider key hidden]')
        return {**state, 'events': count}

    @router.get('/chats/{sid}/outbox')
    def outbox_list(sid: str):
        with database() as db:
            get_chat(db, sid)
        return {'files': outbox_listing(sid)}

    @router.get('/chats/{sid}/outbox/{name}')
    def outbox_download(sid: str, name: str):
        with database() as db:
            get_chat(db, sid)
        if not outbox.valid_name(name):
            raise HTTPException(404, 'File not found or not allowed')
        script = (f'd="$HOME/{outbox_path(sid)}"\n'
                  f'f="$d/"{shlex.quote(name)}\n'
                  '[ -f "$f" ] || exit 3\n'
                  'size=$(stat -c %s -- "$f") || exit 4\n'
                  f'[ "$size" -le {outbox.MAX_BYTES} ] || exit 5\n'
                  'cat -- "$f"\n')
        code, data = pc_script(script, timeout=300)
        if code == 3:
            raise HTTPException(404, 'File not found or not allowed')
        if code == 5 or len(data) > outbox.MAX_BYTES:
            raise HTTPException(413, 'File is over 50 MB and cannot be downloaded here')
        if code != 0:
            raise HTTPException(502, 'The agent machine could not return the file')
        return Response(data, media_type='application/octet-stream', headers=outbox.headers(name, len(data)))

    @router.get('/usage')
    def usage():
        return {'available': False}

    return router
