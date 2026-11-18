import json
import os
from pathlib import Path
import re
import uuid

from . import config, personal, users

UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def root():
    return config.HOME / '.local/state/agent-portal/chat-owners'


def recorded(sid):
    if not UUID.fullmatch(sid):
        return ''
    try:
        path = root() / (sid + '.json')
        if path.is_symlink():
            return ''
        data = json.loads(path.read_text())
        value = data.get('user')
        return value if isinstance(value, str) and users.ID_RE.fullmatch(value) else ''
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError):
        return ''


def owns(sid, cwd=None):
    value = recorded(sid)
    if value is not None:
        return value == users.uid()
    if cwd:
        path = Path(os.path.realpath(cwd))
        base = users.users_dir()
        if users._inside(path, base) and path != base:
            return path.relative_to(base).parts[0] == users.uid()
    return users.is_primary()


def claim(sid):
    if not UUID.fullmatch(sid):
        raise ValueError('Invalid chat ID')
    directory = root()
    personal.private_dir(directory)
    path = directory / (sid + '.json')
    data = json.dumps({'user': users.uid(), 'login': users.login.get()}).encode()
    temp = directory / ('.tmp-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            if recorded(sid) != users.uid():
                raise ValueError('Chat already owned')
    finally:
        temp.unlink(missing_ok=True)
