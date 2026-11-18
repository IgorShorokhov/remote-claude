import asyncio
import logging
from datetime import datetime
import os
import re
import shutil
import time

from . import agentctl, config, statusline, tmuxctl
from .errors import TmuxError

SESSION = 'portal@usage'
TTL = 60.0

log = logging.getLogger('portal.usage')
_lock = asyncio.Lock()
_state: dict = {'data': None, 'checked': None, 'ok_at': None, 'error': None}

BLOCK = re.compile(
    r'^[ \t]*(Current session|Current week \(all models\))[ \t]*\n'
    r'[ \t\u2580-\u259f]*(\d{1,3})%[ \t]+used[ \t]*(?=\n|$)'
    r'(?:\n[ \t]*Resets[ \t]+([^\n]+?)[ \t]*(?=\n|$))?', re.M)


def parse(screen):
    screen = tmuxctl_plain(screen)
    found = {m[1]: (int(m[2]), m[3]) for m in BLOCK.finditer(screen)}
    if 'Current session' not in found or 'Current week (all models)' not in found:
        raise ValueError('usage screen not recognized')
    rows = []
    for key, label in (('Current session', '5h'), ('Current week (all models)', 'Weekly')):
        used, resets = found[key]
        if used > 100 or used != 0 and not resets:
            raise ValueError('invalid or incomplete usage block')
        rows.append({'label': label, 'used': used, 'left': 100 - used, 'resets': resets})
    return rows


def tmuxctl_plain(screen):
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', screen).replace('\xa0', ' ').replace('\r', '')


def claude_bin():
    found = shutil.which(config.CLAUDE_BIN)
    if found:
        return found
    local = config.HOME / '.local' / 'bin' / 'claude'
    if os.access(local, os.X_OK):
        return str(local)
    raise ValueError(f'claude CLI not found: {config.CLAUDE_BIN} (set PORTAL_CLAUDE_BIN)')


async def _tmux(*args, timeout=5.0):
    try:
        base = tmuxctl.base_cmd()
    except tmuxctl.TmuxError as e:
        raise ValueError(e.detail)
    proc = await asyncio.create_subprocess_exec(
        *base, *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=tmuxctl.child_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, ''
    return proc.returncode, out.decode('utf-8', 'replace')


async def _screen():
    _, out = await _tmux('capture-pane', '-p', '-t', f'={SESSION}:')
    return out


async def read_cli():
    binary = claude_bin()
    await _tmux('kill-session', '-t', f'={SESSION}')
    cwd = str(config.PORTAL_ROOTS[0]) if config.PORTAL_ROOTS else str(config.HOME)
    rc, _ = await _tmux('new-session', '-d', '-s', SESSION, '-x', '120', '-y', '50', '-c', cwd, '--', binary)
    if rc:
        raise ValueError('could not start service session')
    try:
        for _ in range(60):
            await asyncio.sleep(.5)
            screen = await _screen()
            if 'trust this folder' in screen:
                raise ValueError('Claude asks to trust the folder')
            if re.search(r'^❯', screen, re.M):
                break
        else:
            raise ValueError('Claude did not start')
        await _tmux('send-keys', '-t', f'={SESSION}:', '-l', '/usage')
        await asyncio.sleep(.6)
        await _tmux('send-keys', '-t', f'={SESSION}:', 'Enter')
        last = ''
        for _ in range(40):
            await asyncio.sleep(.5)
            last = await _screen()
            try:
                return parse(last)
            except ValueError:
                continue
        return parse(last)
    finally:
        await _tmux('kill-session', '-t', f'={SESSION}')


def agent_rows(limits):
    """Rows for the usage panel from get_usage (ISO reset times) or from rate_limit_event numbers."""
    rows = []
    for key, label in (('five_hour', '5h'), ('seven_day', 'Weekly')):
        window = limits.get(key)
        if not isinstance(window, dict):
            return None
        try:
            used = max(0, min(100, round(float(window.get('utilization', window.get('used_percentage'))))))
            at = window.get('resets_at')
            resets = int(datetime.fromisoformat(at).timestamp()) if isinstance(at, str) else int(at)
        except (TypeError, ValueError):
            return None
        rows.append({'label': label, 'used': used, 'left': 100 - used, 'resets': None, 'resets_at': resets})
    return rows


async def agent_get():
    live = [c for c in agentctl.hub.chats.values() if c.alive and c.ready]
    for chat in live:
        try:
            reply = await chat.request('get_usage', timeout=10)
        except TmuxError:
            continue
        rows = agent_rows(reply.get('rate_limits') or {})
        if rows:
            now = time.time()
            return {'ok': True, 'rows': rows, 'checked_at': now, 'last_ok_at': now, 'error': None, 'source': 'claude_agent'}
    rows = agent_rows(agentctl.usage_now())
    if rows and rows[0]['resets_at'] > time.time():
        now = time.time()
        return {'ok': True, 'rows': rows, 'checked_at': now, 'last_ok_at': now, 'error': None, 'source': 'claude_agent'}
    return {'ok': False, 'rows': [], 'checked_at': time.time(), 'last_ok_at': None, 'error': 'Open a chat to see the limits', 'source': 'claude_agent'}


async def get(refresh=False):
    if config.CHAT_MODE == 'agent':
        return await agent_get()
    if not refresh:
        fast = await asyncio.to_thread(statusline.read)
        if fast:
            return {'ok': True, 'rows': fast[1], 'checked_at': fast[0], 'last_ok_at': fast[0], 'error': None, 'source': 'claude_statusline'}
    async with _lock:
        now = time.time()
        if refresh or _state['checked'] is None or now - _state['checked'] > TTL:
            _state['checked'] = now
            try:
                _state['data'] = await asyncio.wait_for(read_cli(), 60)
                _state['ok_at'] = now
                _state['error'] = None
            except (ValueError, asyncio.TimeoutError, OSError) as e:
                _state['data'] = None
                _state['error'] = str(e) or 'timeout'
                log.warning('usage check failed: %s', _state['error'])
        return {
            'ok': _state['data'] is not None,
            'rows': _state['data'] or [],
            'checked_at': _state['checked'],
            'last_ok_at': _state['ok_at'],
            'error': _state['error'],
            'source': 'claude_cli_usage',
        }
