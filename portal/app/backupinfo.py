import json
import math
import os
import stat
import time
from pathlib import Path

from . import config, syncinfo

NO_DATA = "no backup data yet"
BAD_DATA = "backup status corrupted"
MAX_COUNT = 2 ** 63 - 1
NO_SUCCESS = "no successful backup yet"
ERRORS = {
    "IO": "backup I/O error",
    "LOW_SPACE": "not enough space for backup",
    "INVALID_ARCHIVE": "archive failed integrity check",
    "STATUS_WRITE": "could not write backup status",
    "COLLISION": "new backup conflicts with an existing copy",
    "LIMIT_EXCEEDED": "space limit exceeded; last good copy kept",
}


class BackupError(Exception):
    def __init__(self, detail):
        self.status = 503
        self.detail = detail


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackupError(BAD_DATA)
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise BackupError(BAD_DATA)
    if not 0 <= value <= MAX_COUNT:
        raise BackupError(BAD_DATA)
    return int(value)


def _load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackupError(BAD_DATA)
            raw = stream.read(16385)
    except OSError:
        raise BackupError(NO_DATA) from None
    if len(raw) > 16384:
        raise BackupError(BAD_DATA)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError(BAD_DATA) from None
    if not isinstance(data, dict):
        raise BackupError(BAD_DATA)
    return data


def _pc_copy(last, error, now):
    if last is None:
        return "not_confirmed"
    try:
        data = _load(config.SYNC_STATUS_FILE)
        syncinfo.validate(data)
        checked = _count(data.get("checked"))
    except (syncinfo.SyncError, BackupError, TypeError, ValueError, OverflowError, RecursionError):
        return "unknown"
    if not 0 <= now - checked <= config.SYNC_STALE_SECONDS:
        return "unknown"
    if error or checked < last or data.get("connected") is not True:
        return "not_confirmed"
    matches = [f for f in data.get("folders") or [] if f.get("id") == "backups"]
    if len(matches) != 1:
        return "not_confirmed"
    folder = matches[0]
    complete = (folder.get("type") == "sendonly" and folder.get("state") == "idle"
                and all(folder.get(k) == 0 for k in ("errors", "need_files", "need_bytes"))
                and folder.get("peer_completion") == 100)
    return "synced" if complete else "not_confirmed"


def snapshot(now=None, path=None):
    stamp = int(time.time()) if now is None else int(now)
    source = Path(config.BACKUP_STATUS_FILE if path is None else path)
    data = _load(source)
    try:
        if not {"checked", "last_success", "last_size", "copies", "total_bytes", "error"} <= data.keys():
            raise BackupError(BAD_DATA)
        checked = _count(data.get("checked"))
        last = None if data.get("last_success") is None else _count(data["last_success"])
        size = None if data.get("last_size") is None else _count(data["last_size"])
        copies = _count(data.get("copies"))
        total = _count(data.get("total_bytes"))
        if not 0 < checked <= stamp:
            raise BackupError(BAD_DATA)
        if last is not None and (not 0 < last <= checked or not size or not copies or total < size):
            raise BackupError(BAD_DATA)
        if last is None and size is not None:
            raise BackupError(BAD_DATA)
        code = data.get("error")
        if code is not None and (not isinstance(code, str) or code not in ERRORS):
            raise BackupError(BAD_DATA)
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError(BAD_DATA) from None
    age = None if last is None else stamp - last
    error = ERRORS[code] if code else (NO_SUCCESS if last is None else None)
    state = "error" if error else ("stale" if age is not None and age >= config.BACKUP_STALE_SECONDS else "ok")
    return {"state": state, "last_success": last, "age": age,
            "last_size": size, "copies": copies, "total_bytes": total,
            "error": error, "pc_copy": _pc_copy(last, error, stamp)}
