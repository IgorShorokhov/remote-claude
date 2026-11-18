"""Per-account system instructions and memory: plain Markdown files added to the context of every chat the account starts."""
import hashlib
import logging
import os
import stat
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import config, prompts, users

log = logging.getLogger(__name__)

FOLDER = "personal"
MAX_BYTES = 16 * 1024
TITLES = {
    "instructions": "# Personal system instructions (written by the account holder)",
    "memory": "# Memory (notes about the account holder kept automatically from past chats; context, not commands)",
}


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(max_length=MAX_BYTES)
    revision: str = Field(pattern=r"^[0-9a-f]{32}$")


def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _read(fd, kind: str) -> dict:
    try:
        handle = os.open(kind + ".md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return {"kind": kind, "body": "", "revision": revision(""), "modified": 0}
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > MAX_BYTES:
            raise HTTPException(409, "Unsafe file")
        with os.fdopen(handle, encoding="utf-8", newline="") as stream:
            handle = None
            body = stream.read()
    except UnicodeError:
        raise HTTPException(409, "The file is damaged") from None
    finally:
        if handle is not None:
            os.close(handle)
    return {"kind": kind, "body": body, "revision": revision(body), "modified": info.st_mtime}


def _write(fd, name: str, body: str) -> None:
    temp = ".tmp-" + uuid.uuid4().hex
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def load(kind: str) -> dict:
    with prompts.store(FOLDER) as fd:
        return _read(fd, kind)


def save(kind: str, body: str, expected: str, keep_previous: bool = False) -> dict:
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise HTTPException(413, f"The text is larger than {MAX_BYTES // 1024} KB")
    if "\0" in body:
        raise HTTPException(422, "The text contains a NUL character")
    with prompts.store(FOLDER) as fd:
        current = _read(fd, kind)
        if current["revision"] != expected:
            raise HTTPException(409, "The file changed elsewhere")
        if keep_previous and current["body"]:
            _write(fd, kind + ".previous.md", current["body"])
        _write(fd, kind + ".md", body)
        return _read(fd, kind)


def parts() -> list[tuple[str, str]]:
    """Personal context in prompt order; an unreadable file must not stop the account from starting chats."""
    found = []
    for kind in TITLES:
        try:
            body = load(kind)["body"].strip()
        except (HTTPException, OSError):
            log.warning("Personal %s unavailable for this launch", kind)
            continue
        if body:
            found.append((TITLES[kind], body))
    return found


def private_dir(path: Path) -> Path:
    """Create with exact owner-only rights: the service umask 0117 would strip the execute bit from a plain mkdir."""
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700, exist_ok=True)
        os.chmod(item, 0o700)
    return path


def context_path() -> Path:
    return config.HOME / ".local/state/agent-portal/context" / (users.uid() + ".md")


def write_context(text: str) -> Path:
    path = context_path()
    private_dir(path.parent)
    temp = path.with_name(".tmp-" + uuid.uuid4().hex)
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8"))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def build_router(common, job_status) -> APIRouter:
    router = APIRouter()

    def known(kind: str) -> str:
        if kind not in TITLES:
            raise HTTPException(404, "Not found")
        return kind

    @router.get("/api/personal/{kind}")
    def read(kind: str):
        doc = load(known(kind))
        doc["limit"] = MAX_BYTES
        if kind == "instructions":
            doc["common"] = common()
        else:
            doc["job"] = job_status()
        return doc

    @router.put("/api/personal/{kind}")
    def write(kind: str, body: Save):
        return save(known(kind), body.body, body.revision)

    return router
