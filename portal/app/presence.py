import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .security import TABS_BY_ROLE

PRESENCE_PATH = Path('/run/agent-portal/presence.json')
CONTROLLER_PATH = Path('/var/lib/agent-chrome-idle/state.json')
MAX_BODY = 1024
FIELDS = {'client_id', 'tab', 'visible', 'focused', 'last_input_ago_s'}


class PresenceStore:
    def __init__(self):
        self.clients = {}
        self.last_seen_by_tab = {}
        self.overloaded_until = 0

    def flush(self, now):
        self.clients = {k: v for k, v in self.clients.items() if now - v['last_seen'] <= 300}
        data = {'version': 1, 'updated_at': now, 'overloaded_until': self.overloaded_until, 'clients': list(self.clients.values()),
                'last_seen_by_tab': {k: v for k, v in self.last_seen_by_tab.items() if now - v <= 600}}
        fd, name = tempfile.mkstemp(prefix='.presence-', dir=PRESENCE_PATH.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump(data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, PRESENCE_PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return data

    def update(self, payload, info, now):
        if not isinstance(payload, dict) or set(payload) != FIELDS:
            raise HTTPException(400, 'Invalid presence')
        cid, tab, ago = payload['client_id'], payload['tab'], payload['last_input_ago_s']
        if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', cid):
            raise HTTPException(400, 'Invalid client')
        if not isinstance(tab, str) or tab not in TABS_BY_ROLE['admin']:
            raise HTTPException(400, 'Unknown tab')
        if tab not in TABS_BY_ROLE[info['role']]:
            raise HTTPException(403, 'Tab not allowed')
        if any(type(payload[k]) is not bool for k in ('visible', 'focused')) or type(ago) not in (int, float) or not 0 <= ago <= 86400 or not math.isfinite(ago):
            raise HTTPException(400, 'Invalid presence')
        cid = hashlib.sha256((info['email'] + '\0' + cid).encode()).hexdigest()
        self.clients = {k: v for k, v in self.clients.items() if now - v['last_seen'] <= 300}
        if cid not in {v['client_id'] for v in self.clients.values()} and len({v['client_id'] for v in self.clients.values()}) >= 32:
            self.overloaded_until = now + 300
            self.flush(now)
            raise HTTPException(429, 'Too many clients')
        for (key_id, key_tab), old in self.clients.items():
            if key_id == cid and key_tab != tab and old['visible']:
                old.update(visible=False, focused=False, hidden_since=now, last_seen=now)
                self.last_seen_by_tab[key_tab] = now
        old = self.clients.get((cid, tab))
        visible = payload['visible']
        last_input = now if old is None or ago < max(5, now - old['last_seen']) else old['last_input']
        hidden = None if visible else old['hidden_since'] if old and not old['visible'] else now
        self.clients[cid, tab] = {'client_id': cid, 'tab': tab, 'visible': visible,
                                 'focused': payload['focused'] and visible, 'last_seen': now,
                                 'last_input': last_input, 'hidden_since': hidden}
        self.last_seen_by_tab[tab] = now
        self.flush(now)


async def refresh(store):
    while True:
        try:
            store.flush(time.time())
        except OSError:
            pass
        await asyncio.sleep(15)


@contextlib.asynccontextmanager
async def lifespan(app):
    from . import chat_runtime, memory_job
    from . import features
    tasks = [asyncio.create_task(refresh(app.state.presence)), asyncio.create_task(chat_runtime.refresh())]
    if features.on("memory"):
        tasks.append(asyncio.create_task(memory_job.loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def controller_tabs(diagnostic=False):
    try:
        if CONTROLLER_PATH.stat().st_size > 16384:
            return {}
        data = json.loads(CONTROLLER_PATH.read_bytes())
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or not isinstance(data.get('tabs'), dict):
            return {}
        age = time.time() - data['updated_at']
        if not 0 <= age <= 60:
            return {}
        result = {}
        for tab, value in data['tabs'].items():
            if tab not in ('chat', 'browser') or not isinstance(value, dict) or value.get('state') not in ('running', 'stopped', 'starting', 'unknown'):
                return {}
            item = {'state': value['state']}
            if diagnostic:
                if value.get('action') in ('start', 'stop', 'keep'):
                    item['action'] = value['action']
                reason = value.get('reason')
                if isinstance(reason, str) and re.fullmatch(r'[a-z-]{1,64}', reason):
                    item['reason'] = reason
            result[tab] = item
        return result
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {}


def build_router(store):
    router = APIRouter()

    @router.post('/api/presence')
    async def post(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, 'Presence too large')
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'Invalid JSON')
        try:
            store.update(payload, request.scope['portal'], time.time())
        except OSError:
            raise HTTPException(503, 'Presence unavailable')
        tabs = controller_tabs()
        allowed = TABS_BY_ROLE[request.scope['portal']['role']]
        return JSONResponse({'ok': True, 'tabs': {k: v for k, v in tabs.items() if k in allowed}}, headers={'Cache-Control': 'no-store'})

    @router.get('/api/presence/state')
    async def state():
        try:
            data = json.loads(PRESENCE_PATH.read_bytes()) if PRESENCE_PATH.stat().st_size <= 131072 else None
        except (OSError, ValueError):
            data = None
        return JSONResponse({'presence': data, 'tabs': controller_tabs(diagnostic=True)}, headers={'Cache-Control': 'no-store'})

    return router
