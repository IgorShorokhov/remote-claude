import json
import os
import re
import stat
import threading
import time

from . import config, models, ownership, users
from .history import UUID_RE

MAX_BYTES = 1048576
MAX_LINES = 200
MAX_FILES = 5000
TTL = 5.0


def project_dir(cwd):
    return re.sub(r'[^A-Za-z0-9]', '-', str(cwd))


def latest_model(cwd):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root = os.open(config.HISTORY_DIR, flags | os.O_DIRECTORY)
        try:
            project = os.open(project_dir(cwd), flags | os.O_DIRECTORY, dir_fd=root)
        finally:
            os.close(root)
        try:
            newest = None
            with os.scandir(project) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_FILES:
                        return None
                    if not entry.name.endswith('.jsonl') or not UUID_RE.fullmatch(entry.name[:-6]):
                        continue
                    if not ownership.owns(entry.name[:-6], cwd):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                        continue
                    candidate = (st.st_mtime_ns, entry.name)
                    if newest is None or candidate > newest:
                        newest = candidate
            if newest is None:
                return None
            fd = os.open(newest[1], flags, dir_fd=project)
            with os.fdopen(fd, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    return None
                offset = max(0, st.st_size - MAX_BYTES)
                stream.seek(offset)
                raw = stream.read(MAX_BYTES)
        finally:
            os.close(project)
        lines = raw.split(b'\n')
        if offset:
            lines = lines[1:]
        for line in reversed(lines[:-1][-MAX_LINES:]):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(record, dict) or record.get('type') != 'assistant' or record.get('isSidechain'):
                continue
            message = record.get('message')
            model = message.get('model') if isinstance(message, dict) else record.get('model')
            if models.valid(model) and model != '<synthetic>':
                return model
    except (OSError, ValueError, UnicodeError):
        return None
    return None


class ModelObserver:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def snapshot(self, session):
        key = (users.uid(), str(config.HISTORY_DIR), str(config.HOME), session['name'], session['path'], session['created'])
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and now - cached[0] < TTL:
                return dict(cached[1])
            requested = models.settings(config.HOME / '.claude/settings.json').get('model')
            result = {'actual': latest_model(session['path']),
                      'requested': requested if models.valid(requested) else None,
                      'source': 'latest_project_transcript'}
            if len(self._cache) >= 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), result)
            return dict(result)
