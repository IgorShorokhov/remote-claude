import json
import fcntl
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, users

ID = re.compile(r"[0-9a-f]{32}")
PREFIX = "<!-- portal-prompt: "
MAX_BODY = 5 * 1024 * 1024
MAX_FILES = 1000
_locks = {}
_guard = threading.Lock()


class NewPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="New prompt", min_length=1, max_length=100)
    body: str = Field(default="", max_length=MAX_BODY)


class SavePrompt(NewPrompt):
    revision: str = Field(pattern=r"^[0-9a-f]{32}$")


class DeletePrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str
    revision: str = Field(pattern=r"^[0-9a-f]{32}$")


@contextmanager
def store(leaf="prompts"):
    uid = users.uid()
    if not users.ID_RE.fullmatch(uid):
        raise HTTPException(403, "Account not allowed")
    with _guard:
        lock = _locks.setdefault(uid, threading.RLock())
    with lock:
        fd = os.open(config.PORTAL_ROOTS[0].resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in ("users", uid, leaf):
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_uid != os.getuid():
                    raise HTTPException(403, "Storage unavailable")
                os.fchmod(fd, 0o700)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        except OSError:
            raise HTTPException(503, "Storage unavailable") from None
        finally:
            os.close(fd)


def filename(pid):
    if not ID.fullmatch(pid):
        raise HTTPException(404, "Prompt not found")
    return pid + ".md"


def read(fd, pid, full=True):
    try:
        handle = os.open(filename(pid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        raise HTTPException(404, "Prompt not found") from None
    except OSError:
        raise HTTPException(409, "Prompt file unavailable") from None
    try:
        st = os.fstat(handle)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_size > MAX_BODY + 4096:
            raise HTTPException(409, "Unsafe prompt file")
        with os.fdopen(handle, "r", encoding="utf-8", newline="") as stream:
            handle = None
            header = stream.readline(4096)
            if not header.startswith(PREFIX) or not header.endswith(" -->\n"):
                raise ValueError()
            meta = json.loads(header[len(PREFIX):-5])
            if (not isinstance(meta, dict) or not isinstance(meta.get("title"), str)
                    or not 1 <= len(meta["title"]) <= 100 or not ID.fullmatch(meta.get("revision", ""))
                    or not isinstance(meta.get("modified"), (int, float)) or not math.isfinite(meta["modified"])):
                raise ValueError()
            result = {"id": pid, "title": meta["title"], "revision": meta["revision"], "modified": meta["modified"]}
            if full:
                result["body"] = stream.read(MAX_BODY + 1)
                if len(result["body"].encode("utf-8")) > MAX_BODY:
                    raise ValueError()
            return result
    except (ValueError, UnicodeError, TypeError):
        raise HTTPException(409, "Prompt file is damaged") from None
    finally:
        if handle is not None:
            os.close(handle)


def write(fd, pid, title, body):
    title = title.strip()
    if not title or any(ord(c) < 32 for c in title):
        raise HTTPException(422, "Use a title without line breaks")
    if len(body.encode("utf-8")) > MAX_BODY:
        raise HTTPException(413, "The prompt is larger than 5 MB")
    meta = {"title": title, "revision": uuid.uuid4().hex, "modified": time.time()}
    raw = (PREFIX + json.dumps(meta, ensure_ascii=False) + " -->\n" + body).encode("utf-8")
    temp = ".tmp-" + uuid.uuid4().hex
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, filename(pid), src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
    return {"id": pid, **meta, "body": body}


def build_router():
    router = APIRouter()

    @router.get("/api/prompts")
    def listing():
        with store() as fd:
            items = [read(fd, name[:-3], False) for name in os.listdir(fd) if name.endswith(".md") and ID.fullmatch(name[:-3])]
        return {"prompts": sorted(items, key=lambda item: (-item["modified"], item["id"]))}

    @router.post("/api/prompts", status_code=201)
    def create(body: NewPrompt):
        with store() as fd:
            if sum(1 for name in os.listdir(fd) if name.endswith(".md") and ID.fullmatch(name[:-3])) >= MAX_FILES:
                raise HTTPException(409, "Storage already holds 1000 prompts")
            return write(fd, uuid.uuid4().hex, body.title, body.body)

    @router.get("/api/prompts/{pid}")
    def get(pid: str):
        with store() as fd:
            return read(fd, pid)

    @router.put("/api/prompts/{pid}")
    def save(pid: str, body: SavePrompt):
        with store() as fd:
            old = read(fd, pid, False)
            if old["revision"] != body.revision:
                raise HTTPException(409, "The prompt changed in another window. Save your text as a separate copy.")
            return write(fd, pid, body.title, body.body)

    @router.delete("/api/prompts/{pid}")
    def delete(pid: str, body: DeletePrompt):
        with store() as fd:
            old = read(fd, pid, False)
            if body.confirm != pid or old["revision"] != body.revision:
                raise HTTPException(409, "Deletion not confirmed, or the prompt changed in another window")
            os.unlink(filename(pid), dir_fd=fd)
            os.fsync(fd)
        return {"ok": True}

    @router.get("/api/prompts/{pid}/download")
    def download(pid: str):
        with store() as fd:
            item = read(fd, pid)
        name = quote(item["title"] + ".md", safe="")
        return Response(item["body"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename=prompt.md; filename*=UTF-8''" + name,
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        })

    return router
