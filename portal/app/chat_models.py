import datetime
import functools
import json
import math
import os
import stat
import time

from . import chat_lifecycle, config, models, tmuxctl, transcript, users
from .history import UUID_RE

EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max"))


def valid_effort(value):
    return value if isinstance(value, str) and value in EFFORTS else None


@functools.lru_cache(maxsize=512)
def read_tail(uid, path, modified, size):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return {}
            offset = max(0, info.st_size - 1048576)
            stream.seek(offset)
            lines = stream.read(1048576).split(b'\n')
        if offset:
            lines = lines[1:]
        for raw in reversed(lines[-201:]):
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get('type') != 'assistant' or row.get('isSidechain') or row.get('isApiErrorMessage'):
                continue
            message = row.get('message') or {}
            model = message.get('model') if isinstance(message, dict) else None
            if not models.valid(model) or model == 'default':
                continue
            try:
                stamp = datetime.datetime.fromisoformat(row.get('timestamp', '')).timestamp()
            except (ValueError, TypeError):
                stamp = 0
            return {'model': models.canonical(model), 'model_saved_at': stamp, 'model_source': 'last_reply'}
    except OSError:
        pass
    return {}


def snapshot(sid, records):
    saved = records.get(sid, {})
    saved = {key: saved[key] for key in ('model', 'effort', 'model_source', 'model_saved_at', 'effort_model', 'effort_source', 'effort_saved_at', 'auto') if key in saved}
    path = transcript.locate(config.HISTORY_DIR, sid)
    actual = {}
    if path is not None:
        try:
            info = path.stat()
            actual = read_tail(users.uid(), str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            pass
    effort = valid_effort(saved.get('effort'))
    effort_model = saved.get('effort_model') or saved.get('model')
    if effort_model != saved.get('model'):
        effort = None
    saved['effort'] = effort
    if not models.valid(saved.get('model')) or saved.get('model') == 'default':
        return {**actual, 'effort': None} if actual else {}
    stamp = saved.get('model_saved_at', 0)
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        stamp = 0
    if actual.get('model_saved_at', 0) > stamp:
        if saved.get('model') == models.OPUSPLAN and models.runs_under_opusplan(actual.get('model')):
            return saved
        return {**actual, 'effort': effort if actual.get('model') == saved.get('model') else None,
                **{key: saved[key] for key in ('effort_model', 'effort_source', 'effort_saved_at', 'auto') if key in saved}}
    return saved


async def remember_session(name, confirmed=False):
    sid = await tmuxctl.option(name, '@resume_sid')
    model = await tmuxctl.option(name, '@portal_model')
    if not sid or not UUID_RE.fullmatch(sid) or not models.valid(model) or model == 'default':
        return
    try:
        session = await tmuxctl.require_session(name)
    except tmuxctl.TmuxError as error:
        if error.status == 404:
            return
        raise
    model = models.canonical(model)
    effort = await tmuxctl.option(name, '@portal_effort')
    source = await tmuxctl.option(name, '@portal_model_state')
    record = chat_lifecycle.load().get(sid, {})
    effort = valid_effort(effort)
    if not confirmed and models.valid(record.get('model')) and record.get('model') != 'default':
        if model != record['model'] or effort is None or (valid_effort(record.get('effort')) is not None and (record.get('effort_model') or record['model']) == model):
            return
        chat_lifecycle.save(sid, record.get('awake', True), session['path'], record.get('modified', session['created']),
                            effort=effort, effort_model=model, effort_source='session', effort_saved_at=time.time())
        return
    chat_lifecycle.save(sid, record.get('awake', True), session['path'], record.get('modified', session['created']),
                        model=model, effort=effort, model_source=source, model_saved_at=time.time(),
                        effort_model=model, effort_source='cli_confirmed' if confirmed else 'session', effort_saved_at=time.time())
