import base64
import binascii
import os
import re
import stat
import time
import uuid
from pathlib import Path

from . import fsroots

MAX_BYTES = 5 * 1024 * 1024
RAW_MAX_BYTES = 100 * 1024 * 1024
# Large attachments are kept for a week, then removed; smaller ones stay with the chat.
PRUNE_BYTES = 20 * 1024 * 1024
PRUNE_AGE = 7 * 24 * 3600
PART_AGE = 24 * 3600
PART_PREFIX = ".upload-"
FOLDER = "attachments"
NAME_RE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


class UploadError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clean_name(name):
    if not isinstance(name, str) or not name.strip() or "\x00" in name or len(name) > 200:
        raise UploadError(400, "Bad file name")
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise UploadError(400, "Bad file name")
    # Any type is accepted (Excel, Word, archives, files without an extension): the file only lands in the chat's attachments folder
    # under a cleaned name and is never run by the portal; Claude reads it with its own tools.
    return NAME_RE.sub("_", name.strip())


def split_name(safe):
    """(stem, dot, ext) for numbering a duplicate: 'a.xlsx' -> a-2.xlsx, 'Makefile' -> Makefile-2."""
    stem, dot, ext = safe.rpartition(".")
    return (stem, dot, ext) if dot else (safe, "", "")


def decode(data):
    if not isinstance(data, str) or len(data) > (MAX_BYTES * 4) // 3 + 8:
        raise UploadError(413, "File is too large (max 5 MB)")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise UploadError(400, "Bad file data")
    if len(raw) > MAX_BYTES:
        raise UploadError(413, "File is too large (max 5 MB)")
    return raw


def folder(directory, roots):
    try:
        base = fsroots.resolve_dir(directory, roots)
    except fsroots.FsError:
        raise UploadError(403, "Folder is outside your files")
    target_dir = base / FOLDER
    if target_dir.is_symlink():
        raise UploadError(403, "Folder is outside your files")
    target_dir.mkdir(mode=0o700, exist_ok=True)
    st = target_dir.stat()
    if st.st_uid == os.getuid() and (st.st_mode & 0o700) != 0o700:
        os.chmod(target_dir, 0o700)
    if not fsroots.inside(Path(os.path.realpath(target_dir)), base):
        raise UploadError(403, "Folder is outside your files")
    return base, target_dir


def begin(directory, name, roots):
    """Validate the target and open a hidden part file; the caller streams bytes into it and calls finish()."""
    safe = clean_name(name)
    _, target_dir = folder(directory, roots)
    part = target_dir / (PART_PREFIX + uuid.uuid4().hex + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return part, safe, fd


def finish(part, safe, size):
    """Publish the part file under the first free name without ever replacing an existing file."""
    target_dir = part.parent
    stem, dot, ext = split_name(safe)
    path, n = target_dir / safe, 1
    while True:
        try:
            os.link(part, path, follow_symlinks=False)
            break
        except FileExistsError:
            n += 1
            path = target_dir / f"{stem}-{n}{dot}{ext}"
        except OSError:
            # No hard links here (the laptop folder is mounted over SSH): fall back to a rename onto a free name.
            if os.path.lexists(path):
                n += 1
                path = target_dir / f"{stem}-{n}{dot}{ext}"
                continue
            os.rename(part, path)
            return {"path": str(path), "relative": f"{FOLDER}/{path.name}", "name": path.name, "size": size}
    os.unlink(part)
    return {"path": str(path), "relative": f"{FOLDER}/{path.name}", "name": path.name, "size": size}


def prune(roots, now=None):
    """Remove attachments over PRUNE_BYTES older than PRUNE_AGE and abandoned part files; returns the number removed."""
    now = time.time() if now is None else now
    removed = 0
    for root in roots:
        for top, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__")]
            if os.path.basename(top) != FOLDER:
                continue
            for name in files:
                path = os.path.join(top, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                stale_part = name.startswith(PART_PREFIX) and now - st.st_mtime > PART_AGE
                too_old = st.st_size > PRUNE_BYTES and now - st.st_mtime > PRUNE_AGE
                if stale_part or too_old:
                    try:
                        os.unlink(path)
                        removed += 1
                    except OSError:
                        pass
    return removed


def save(directory, name, data, roots):
    safe = clean_name(name)
    raw = decode(data)
    base, target_dir = folder(directory, roots)
    stem, dot, ext = split_name(safe)
    path = target_dir / safe
    if re.fullmatch(r"pasted-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.txt", safe):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(MAX_BYTES + 1) != raw:
                        raise UploadError(409, "Text attachment conflicts with an existing upload; original text is retained")
            except OSError:
                raise UploadError(409, "Text attachment is unavailable; original text is retained")
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        return {"path": str(path), "relative": f"{FOLDER}/{path.name}", "name": path.name, "size": len(raw)}
    n = 1
    while path.exists() or path.is_symlink():
        n += 1
        path = target_dir / f"{stem}-{n}{dot}{ext}"
    if not fsroots.inside(Path(os.path.realpath(path.parent)), base):
        raise UploadError(403, "Folder is outside your files")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return {"path": str(path), "relative": f"{FOLDER}/{path.name}", "name": path.name, "size": len(raw)}
