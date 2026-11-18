import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime

from fastapi import HTTPException

from . import agentctl, chat_lifecycle, chat_models, cli_controls, config, pcguard, tmuxctl, transcript, uploads, users
from .history import UUID_RE

log = logging.getLogger(__name__)


def timestamp(value, fallback):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else fallback
    except (TypeError, ValueError):
        return fallback


async def touch(name):
    if agentctl.has(name):
        await agentctl.chat_for(name).touch()
        return
    rc, _ = await tmuxctl.run('set-option', '-t', f'={name}:', '@portal_last_used', str(time.time()))
    if rc:
        raise tmuxctl.TmuxError(503, 'Could not record chat activity')


TAIL = 256 * 1024


def last_event(path, fallback):
    """Time of the newest timestamped record: Claude rewrites untimed metadata (titles, modes) while idle, so mtime is not activity."""
    try:
        with open(path, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - TAIL))
            lines = stream.read().splitlines()
    except OSError:
        return fallback
    for raw in reversed(lines):
        if b'"timestamp"' not in raw:
            continue
        try:
            value = json.loads(raw).get('timestamp')
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, AttributeError, TypeError, RecursionError):
            continue
    return fallback


def file_stamp(sid):
    path = transcript.locate(config.HISTORY_DIR, sid)
    if path is None:
        return None
    st = path.stat()
    return st.st_mtime_ns, st.st_size, last_event(path, st.st_mtime)


async def sweep():
    async with tmuxctl._lock:
        items = await tmuxctl.list_sessions(all_users=True) or []
        occupied = await tmuxctl.occupied_names(items)
        for item in items:
            name = item['name']
            try:
                sid = await tmuxctl.option(name, '@resume_sid')
                owner = await tmuxctl.option(name, '@portal_user')
                if not sid or not UUID_RE.fullmatch(sid) or not owner or not users.ID_RE.fullmatch(owner):
                    continue
                token = users.current.set(owner)
                try:
                    if config.CHAT_MODE == 'agent' and config.MIGRATE_LEGACY and not agentctl.has(name):
                        if await migrate(item, sid):
                            continue
                    await expire(item, sid, name in occupied)
                finally:
                    users.current.reset(token)
            except (OSError, HTTPException, tmuxctl.TmuxError):
                log.warning('Chat runtime cleanup deferred; state or runtime unavailable')


async def migrate(item, sid):
    """Agent mode: a chat still running as a tmux terminal goes to sleep once it is idle with an empty prompt (history and model are kept)."""
    name = item['name']
    if cli_controls.busy(name) or tmuxctl.claude_starting(name):
        return False
    try:
        if not cli_controls.idle(await cli_controls.capture(name)):
            return False
    except tmuxctl.TmuxError:
        return False
    before = await asyncio.to_thread(file_stamp, sid)
    await asyncio.to_thread(chat_lifecycle.check_writable)
    await chat_models.remember_session(name)
    await asyncio.to_thread(chat_lifecycle.save, sid, False, item['path'], before[2] if before else item['created'])
    await tmuxctl.kill(name)
    tmuxctl._recent_claude.pop(name, None)
    log.info('Chat moved from the terminal to agent mode (put to sleep)')
    return True


async def expire(item, sid, occupied):
    """Put a chat to sleep after CHAT_IDLE_SECONDS without input, output or transcript events, whatever its screen shows."""
    name = item['name']
    now = time.time()
    before = await asyncio.to_thread(file_stamp, sid)
    last = timestamp(await tmuxctl.option(name, '@portal_last_used'), item['created'])
    last = max(last, item['activity'], item['created'], before[2] if before else 0)
    if cli_controls.busy(name) or tmuxctl.claude_starting(name) or (agentctl.has(name) and agentctl.chat_for(name).working):
        return
    if now - last <= config.CHAT_IDLE_SECONDS:
        return
    if before != await asyncio.to_thread(file_stamp, sid):
        return
    await asyncio.to_thread(chat_lifecycle.check_writable)
    await chat_models.remember_session(name)
    await asyncio.to_thread(chat_lifecycle.save, sid, False, item['path'], before[2] if before else item['created'])
    await tmuxctl.kill(name)
    tmuxctl._recent_claude.pop(name, None)
    log.info('Chat runtime put an idle chat to sleep')


async def refresh():
    pruned = 0.0
    while True:
        try:
            await sweep()
        except (OSError, HTTPException, tmuxctl.TmuxError):
            log.warning('Chat runtime sweep unavailable; will retry')
        if time.monotonic() - pruned >= config.PRUNE_INTERVAL or not pruned:
            pruned = time.monotonic()
            # The laptop folder is a network mount: walking it can hang, and a hung cleanup must not stop chats from going to sleep.
            roots = [root for root in config.PORTAL_ROOTS if not pcguard.is_pc_path(root)]
            try:
                await asyncio.wait_for(asyncio.to_thread(uploads.prune, roots), 900)
            except (OSError, asyncio.TimeoutError):
                log.warning('Attachment cleanup unavailable; will retry')
        await asyncio.sleep(30)
