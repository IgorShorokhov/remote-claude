import json
import math
import os
import tempfile
import threading
from functools import wraps
from pathlib import Path

from fastapi import HTTPException

from . import personal, users
from .history import UUID_RE

_lock = threading.RLock()


def directory() -> Path:
    titles = users.titles_file()
    return titles.parent / (titles.stem + '-states')


def prepare_directory() -> Path:
    root = directory()
    personal.private_dir(root)
    if root.is_symlink():
        raise OSError('chat state directory must not be a symlink')
    root.chmod(0o700)
    return root


def storage_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OSError as exc:
            raise HTTPException(503, 'Chat state storage unavailable; operation not confirmed. Retry or contact the administrator.') from exc
    return guarded


@storage_operation
def check_writable() -> None:
    root = prepare_directory()
    fd, temp = tempfile.mkstemp(dir=root, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('{}')
        os.replace(temp, temp + '.probe')
    finally:
        for path in (temp, temp + '.probe'):
            Path(path).unlink(missing_ok=True)


@storage_operation
def load() -> dict:
    result = {}
    root = prepare_directory()
    for path in root.glob('*.json'):
        if not UUID_RE.fullmatch(path.stem) or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(data, dict) and isinstance(data.get('awake'), bool)
                and isinstance(data.get('cwd'), str) and data['cwd']
                and type(data.get('modified')) in (int, float) and math.isfinite(data['modified'])):
            result[path.stem] = data
    return result


@storage_operation
def save(sid: str, awake: bool, cwd: str, modified: float, **metadata) -> None:
    if not UUID_RE.fullmatch(sid):
        raise ValueError('bad id')
    with _lock:
        root = prepare_directory()
        record = load().get(sid, {})
        record.update(awake=awake, cwd=cwd, modified=modified)
        record.update(metadata)
        fd, temp = tempfile.mkstemp(dir=root, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream)
            os.replace(temp, root / (sid + '.json'))
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


@storage_operation
def remove(sid: str) -> None:
    if not UUID_RE.fullmatch(sid):
        raise ValueError('bad id')
    (prepare_directory() / (sid + '.json')).unlink(missing_ok=True)


def awake(sid: str, live: bool, records: dict) -> bool:
    return records.get(sid, {}).get('awake', live)
