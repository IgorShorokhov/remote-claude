import contextlib
import itertools
import os
import stat
from pathlib import PurePosixPath

from . import fsroots, pcguard, users

MAX_BYTES = 131072
MAX_ENTRIES = 500
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def visible(parts):
    for part in parts:
        name = part.lower()
        if (not part or part in ('.', '..') or name.startswith('.') or name in ('private', 'secrets', 'credentials', 'node_modules', '__pycache__')
                or name.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa')) or name.endswith(('.env', '.pem', '.key', '.p12', '.pfx'))
                or any(ord(c) < 32 or ord(c) == 127 for c in part)):
            return False
    return True


@contextlib.contextmanager
def opened(base, relative, roots, directory):
    if '\x00' in relative or '\\' in relative or relative.startswith('/') or len(relative) > 4096:
        raise fsroots.FsError(403, 'Path outside session folder')
    parts = relative.split('/') if relative else []
    if not visible(parts):
        raise fsroots.FsError(403, 'Path not viewable')
    if not users.path_allowed(base.joinpath(*parts)):
        raise fsroots.FsError(403, 'Path outside your chat files')
    rr = [r for r in fsroots.real_roots(roots) if fsroots.inside(base, r)]
    if not rr:
        raise fsroots.FsError(403, 'Path outside allowed folders')
    root = max(rr, key=lambda r: len(r.parts))
    prefix = list(base.relative_to(root).parts)
    if not visible(prefix):
        raise fsroots.FsError(403, 'Folder not viewable')
    fd = None
    try:
        fd = os.open(root, FLAGS | os.O_DIRECTORY)
        components = prefix + parts
        for index, part in enumerate(components):
            is_dir = directory or index < len(components) - 1
            new = os.open(part, FLAGS | (os.O_DIRECTORY if is_dir else 0), dir_fd=fd)
            os.close(fd)
            fd = new
        yield fd
    except OSError:
        raise fsroots.FsError(403, 'Could not open path safely')
    finally:
        if fd is not None:
            os.close(fd)


def listing(base, relative, roots):
    entries = []
    with opened(base, relative, roots, True) as fd:
        with os.scandir(fd) as iterator:
            raw = list(itertools.islice(iterator, MAX_ENTRIES + 1))
            truncated = len(raw) > MAX_ENTRIES
            for e in raw[:MAX_ENTRIES]:
                if not visible([e.name]):
                    continue
                if not users.path_allowed(base / relative / e.name):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    kind = 'directory'
                elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    kind = 'file'
                else:
                    continue
                entries.append({'name': e.name, 'path': str(PurePosixPath(relative) / e.name), 'kind': kind})
    entries.sort(key=lambda e: (e['kind'] != 'directory', e['name'].lower()))
    return {'path': relative, 'entries': entries, 'truncated': truncated}


def preview(base, relative, roots):
    if not relative:
        raise fsroots.FsError(400, 'Select a file')
    with opened(base, relative, roots, False) as fd:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise fsroots.FsError(403, 'Regular files only, no links')
        if st.st_size > MAX_BYTES:
            raise fsroots.FsError(413, 'File over 128 KiB')
        with os.fdopen(os.dup(fd), 'rb') as f:
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise fsroots.FsError(413, 'File over 128 KiB')
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise fsroots.FsError(415, 'UTF-8 text files only')
        if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
            raise fsroots.FsError(415, 'Binary file')
        return {'path': relative, 'text': text, 'size': len(data)}


async def read(base, relative, roots, directory):
    await pcguard.ensure_usable(base)
    return await pcguard.call(listing if directory else preview, base, relative, roots, pc=pcguard.is_pc_path(base))
