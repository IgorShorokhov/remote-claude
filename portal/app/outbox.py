import mimetypes
import os
import stat
from pathlib import Path
from urllib.parse import quote

from . import users

NAME = "outbox"
MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 200
PREVIEW_TEXT_MAX = 2 * 1024 * 1024
PREVIEW_TYPES = {
    "jpg": ("image", "image/jpeg"), "jpeg": ("image", "image/jpeg"), "png": ("image", "image/png"),
    "txt": ("text", "text/plain; charset=utf-8"), "md": ("text", "text/plain; charset=utf-8"),
    "html": ("html", "text/html; charset=utf-8"), "htm": ("html", "text/html; charset=utf-8"),
}
CHUNK = 1 << 16
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class OutboxError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def chat_folder(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    try:
        folder = Path(os.path.realpath(cwd))
        root = Path(os.path.realpath(users.chat_root()))
    except (OSError, ValueError):
        return None
    if folder.parent != root or os.path.islink(cwd):
        return None
    return folder


def ensure(folder: Path) -> None:
    path = folder / NAME
    if os.path.islink(path):
        return
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return
    # The portal runs with umask 0117, which strips the execute bit from mkdir(mode=0o700); without it nobody can use the folder.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except OSError:
        pass
    finally:
        os.close(fd)


def valid_name(name: str) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 255 and name not in (".", "..") and not name.startswith(".")
            and "/" not in name and "\\" not in name and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _open_dir(folder: Path) -> int:
    fd = os.open(folder, FLAGS | os.O_DIRECTORY)
    try:
        sub = os.open(NAME, FLAGS | os.O_DIRECTORY, dir_fd=fd)
    finally:
        os.close(fd)
    return sub


def kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].upper() if "." in name else ""
    return ext[:8] if ext else "FILE"


def listing(folder: Path | None) -> list[dict]:
    if folder is None:
        return []
    try:
        fd = _open_dir(folder)
    except OSError:
        return []
    out = []
    try:
        with os.scandir(fd) as it:
            for e in it:
                if len(out) >= MAX_FILES:
                    break
                if not valid_name(e.name):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    continue
                out.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime, "type": kind(e.name),
                            "too_big": st.st_size > MAX_BYTES, "preview": preview_kind(e.name)})
    finally:
        os.close(fd)
    out.sort(key=lambda f: (f["mtime"], f["name"]))
    return out


def open_file(folder: Path | None, name: str):
    if folder is None:
        raise OutboxError(404, "No outbox for this chat")
    if not valid_name(name):
        raise OutboxError(403, "Bad file name")
    try:
        dfd = _open_dir(folder)
    except OSError:
        raise OutboxError(404, "No outbox for this chat")
    try:
        fd = os.open(name, FLAGS, dir_fd=dfd)
    except OSError:
        raise OutboxError(404, "File not found or not allowed")
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OutboxError(403, "Regular files only, no links")
    if st.st_size > MAX_BYTES:
        os.close(fd)
        raise OutboxError(413, "File is over 50 MB and cannot be downloaded here")
    return fd, st.st_size


def stream(fd: int, size: int):
    # The file object owns the descriptor: it is closed even when the response is dropped before the first chunk is asked for.
    source = os.fdopen(fd, "rb", buffering=0)

    def chunks():
        with source:
            left = size
            while left > 0:
                data = source.read(min(CHUNK, left))
                if not data:
                    break
                left -= len(data)
                yield data

    return chunks()


def headers(name: str, size: int) -> dict:
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "file"
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}",
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "no-store",
    }


def media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def preview_kind(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    found = PREVIEW_TYPES.get(ext)
    return found[0] if found else None


def preview_media_type(name: str) -> str:
    return PREVIEW_TYPES[name.rsplit(".", 1)[-1].lower()][1]


def preview_headers(name: str, size: int, scripts: bool = False) -> dict:
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "file"
    if preview_kind(name) == "html":
        policy = ("sandbox allow-scripts; script-src 'unsafe-inline'; " if scripts else "sandbox; ") + (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    else:
        policy = "sandbox; default-src 'none'; frame-ancestors 'self'"
    return {
        "Content-Disposition": f"inline; filename=\"{ascii_name}\"; filename*=UTF-8\'\'{quote(name, safe='')}",
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": policy,
        "Cache-Control": "no-store",
    }
