import asyncio
import contextvars
import functools
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

POOL_SIZE = 4
BLOCK_SECONDS = 5.0
DOWN = "PC offline"


class PcError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


_pool = ThreadPoolExecutor(POOL_SIZE, thread_name_prefix="pc")
_inflight = 0
_block_until = 0.0


def reset() -> None:
    global _pool, _inflight, _block_until
    _pool = ThreadPoolExecutor(POOL_SIZE, thread_name_prefix="pc")
    _inflight = 0
    _block_until = 0.0


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def pc_relative(value) -> tuple[str, ...] | None:
    if not isinstance(value, (str, Path)) or "\x00" in str(value):
        return None
    p = Path(os.path.abspath(str(value)))
    for root in (config.PC_ROOT, config.PC_LINK):
        if _inside(p, root):
            return p.relative_to(root).parts
    return None


def is_pc_path(value) -> bool:
    return pc_relative(value) is not None


def read_status(now: float | None = None) -> dict | None:
    try:
        raw = config.PC_STATUS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    now = time.time() if now is None else now
    checked = data.get("checked")
    age = int(now - checked) if isinstance(checked, (int, float)) else None
    data["age"] = age
    data["stale"] = age is None or age > config.PC_STALE_SECONDS
    return data


def folder_state(status: dict | None, name: str) -> str:
    if status is None:
        return "watcher_not_running"
    if status.get("stale"):
        return "stale"
    if not status.get("reachable"):
        return "pc_down"
    for f in status.get("folders") or []:
        if isinstance(f, dict) and f.get("name") == name:
            return str(f.get("state"))
    return "unknown"


def connected_names(status: dict | None) -> list[str]:
    if status is None or status.get("stale") or not status.get("reachable"):
        return []
    return [
        f["name"]
        for f in status.get("folders") or []
        if isinstance(f, dict) and f.get("state") == "connected" and isinstance(f.get("name"), str)
    ]


def precheck(value) -> None:
    rel = pc_relative(value)
    if rel is None:
        return
    status = read_status()
    if not rel:
        if not connected_names(status):
            raise PcError(503, DOWN)
        return
    if folder_state(status, rel[0]) != "connected":
        raise PcError(503, DOWN)


async def call(fn, *args, pc: bool, timeout: float | None = None):
    global _inflight, _block_until
    limit = config.PC_TIMEOUT if timeout is None else timeout
    loop = asyncio.get_running_loop()
    if not pc:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), max(limit, 5.0))
        except asyncio.TimeoutError:
            raise PcError(503, "file system not responding")
    if time.monotonic() < _block_until or _inflight >= POOL_SIZE:
        raise PcError(503, DOWN)
    _inflight += 1

    def done(f):
        global _inflight
        _inflight -= 1
        if not f.cancelled():
            f.exception()

    fut = loop.run_in_executor(_pool, functools.partial(contextvars.copy_context().run, fn, *args))
    fut.add_done_callback(done)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), limit)
    except asyncio.TimeoutError:
        _block_until = time.monotonic() + BLOCK_SECONDS
        raise PcError(503, "folder unavailable")


async def ensure_usable(real: Path) -> None:
    rel = pc_relative(real)
    if rel is None:
        return
    if not rel:
        raise PcError(403, "pick a PC folder, not the root")
    precheck(real)

    def check():
        return os.path.isdir(real)

    if not await call(check, pc=True):
        raise PcError(503, "folder unavailable")
