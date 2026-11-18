import http.client
import json
import math
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

NO_DATA = "no sync data yet"
BAD_DATA = "sync status file corrupted"
NO_GUI = "Syncthing socket not responding"
NO_SYNC_TIME = "last sync time unknown"
NO_STATS = "Syncthing transfer history unavailable"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
STATE_RE = re.compile(r"[a-z][a-z-]{0,31}")
LINK_RE = re.compile(r"(tcp|quic|relay)-(client|server)")
VERSION_RE = re.compile(r"v?\d{1,3}(?:\.\d{1,3}){1,3}")
ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
GO_ZERO_EPOCH = int(datetime(1, 1, 1, tzinfo=timezone.utc).timestamp())
MAX_COUNT = 2 ** 63 - 1


class SyncError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


def parse_time(value) -> int | None:
    if not isinstance(value, str):
        return None
    m = ISO_RE.match(value.strip())
    if m is None:
        return None
    try:
        dt = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    off = m.group(7)
    if off and off != "Z":
        digits = off[1:].replace(":", "")
        sign = -1 if off[0] == "-" else 1
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return int(dt.timestamp())


def _event_time(value) -> int | None:
    stamp = parse_time(value)
    if stamp is None or stamp in (0, GO_ZERO_EPOCH):
        return None
    return stamp


def _strict_count(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SyncError(503, BAD_DATA)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SyncError(503, BAD_DATA)
    if value < 0 or value > MAX_COUNT:
        raise SyncError(503, BAD_DATA)
    return int(value)


def _count(value) -> int | None:
    try:
        return _strict_count(value)
    except SyncError:
        return None


def _strict_percent(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SyncError(503, BAD_DATA)
    if isinstance(value, float) and not math.isfinite(value):
        raise SyncError(503, BAD_DATA)
    if value < 0 or value > MAX_COUNT:
        raise SyncError(503, BAD_DATA)
    number = int(value)
    return 100 if number > 100 else number


def _percent(value) -> int | None:
    try:
        return _strict_percent(value)
    except SyncError:
        return None


def _token(value) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if STATE_RE.fullmatch(s) else None


def _note(notes: list, text: str) -> None:
    if text not in notes:
        notes.append(text)


def validate(data: dict) -> None:
    for field in ("checked", "conflicts"):
        _strict_count(data.get(field))
    connected = data.get("connected")
    if connected is not None and not isinstance(connected, bool):
        raise SyncError(503, BAD_DATA)
    seen = data.get("peer_last_seen")
    if seen is not None and not isinstance(seen, str):
        raise SyncError(503, BAD_DATA)
    raw = data.get("folders")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise SyncError(503, BAD_DATA)
    for item in raw:
        if not isinstance(item, dict):
            raise SyncError(503, BAD_DATA)
        if "id" in item and not isinstance(item.get("id"), str):
            raise SyncError(503, BAD_DATA)
        for field in ("errors", "need_files", "need_bytes", "global_bytes"):
            _strict_count(item.get(field))
        _strict_percent(item.get("peer_completion"))
        scan = item.get("last_scan")
        if scan is not None and not isinstance(scan, str):
            raise SyncError(503, BAD_DATA)


def load(path=None) -> dict:
    p = Path(path) if path is not None else Path(config.SYNC_STATUS_FILE)
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SyncError(503, NO_DATA)
    try:
        data = json.loads(raw)
    except ValueError:
        raise SyncError(503, BAD_DATA)
    if not isinstance(data, dict):
        raise SyncError(503, BAD_DATA)
    validate(data)
    return data


def folders(data: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    raw = data.get("folders")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("id")
        if isinstance(name, str) and NAME_RE.fullmatch(name) and name not in out:
            out[name] = item
    return out


def _complete(state: str, errors, need_files, need_bytes, completion) -> bool:
    if state != "idle":
        return False
    if errors != 0 or need_files != 0 or need_bytes != 0:
        return False
    if completion is None or completion < 100:
        return False
    return True


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _get(key: str, path: str, timeout: float):
    c = _UnixConnection(str(config.SYNC_GUI_SOCK), timeout)
    try:
        c.request("GET", path, headers={"X-API-Key": key})
        r = c.getresponse()
        body = r.read(262144)
        if r.status != 200:
            raise OSError("gui http %d" % r.status)
        return json.loads(body)
    finally:
        c.close()


_key_cache: dict = {"stamp": None, "key": None}


def _api_key() -> str | None:
    p = Path(config.SYNCTHING_CONFIG)
    try:
        st = os.stat(p)
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    if _key_cache["stamp"] == stamp:
        return _key_cache["key"]
    key = None
    try:
        node = ET.parse(p).getroot().find("gui")
        raw = node.findtext("apikey") if node is not None else None
        key = raw.strip() if isinstance(raw, str) and raw.strip() else None
    except (OSError, ET.ParseError):
        key = None
    _key_cache["stamp"] = stamp if key else None
    _key_cache["key"] = key
    return key


_GUI_FAIL = (OSError, ValueError, KeyError, AttributeError, http.client.HTTPException)


def _enrich(ids) -> dict:
    out: dict = {"link": None, "version": None, "folders": {}, "files_at": {}, "notes": []}
    key = _api_key()
    if not key:
        out["notes"].append(NO_GUI)
        return out
    timeout = config.SYNC_TIMEOUT
    try:
        me = _get(key, "/rest/system/status", timeout).get("myID")
        version = _get(key, "/rest/system/version", timeout).get("version")
        if isinstance(version, str) and VERSION_RE.fullmatch(version.strip()):
            out["version"] = version.strip()
        conns = _get(key, "/rest/system/connections", timeout).get("connections") or {}
        for device, info in conns.items():
            if device == me or not isinstance(info, dict):
                continue
            link = info.get("type")
            if isinstance(link, str) and LINK_RE.fullmatch(link):
                out["link"] = link
            peer_version = info.get("clientVersion")
            if isinstance(peer_version, str) and VERSION_RE.fullmatch(peer_version.strip()):
                out["version"] = peer_version.strip()
            break
    except _GUI_FAIL:
        _note(out["notes"], NO_GUI)
    for name in ids:
        try:
            st = _get(key, "/rest/db/status?folder=" + name, timeout)
        except _GUI_FAIL:
            continue
        if isinstance(st, dict):
            out["folders"][name] = {
                "files_local": _count(st.get("localFiles")),
                "files_global": _count(st.get("globalFiles")),
                "bytes_local": _count(st.get("localBytes")),
                "bytes_global": _count(st.get("globalBytes")),
                "need_files": _count(st.get("needFiles")),
                "need_bytes": _count(st.get("needBytes")),
            }
    stats = None
    try:
        stats = _get(key, "/rest/stats/folder", timeout)
    except _GUI_FAIL:
        stats = None
    if isinstance(stats, dict):
        for name in ids:
            item = stats.get(name)
            last = item.get("lastFile") if isinstance(item, dict) else None
            at = last.get("at") if isinstance(last, dict) else None
            stamp = _event_time(at)
            if stamp is not None:
                out["files_at"][name] = stamp
    elif NO_GUI not in out["notes"]:
        _note(out["notes"], NO_STATS)
    return out


def _normalize(data: dict, extra: dict, now: int) -> dict:
    checked = _count(data.get("checked"))
    age = now - checked if checked is not None else None
    stale = age is None or age > config.SYNC_STALE_SECONDS
    seen = _event_time(data.get("peer_last_seen"))
    by_name = folders(data)
    live_folders = extra.get("folders") or {}
    files_at = extra.get("files_at") or {}
    order = [n for n in config.SYNC_FOLDERS] + [n for n in by_name if n not in config.SYNC_FOLDERS]
    items = []
    scans = []
    synced = []
    for name in order:
        raw = by_name.get(name, {})
        live = live_folders.get(name) or {}
        state = _token(raw.get("state")) or "unknown"
        need = _count(raw.get("need_files"))
        if need is None:
            need = live.get("need_files")
        need_bytes = _count(raw.get("need_bytes"))
        if need_bytes is None:
            need_bytes = live.get("need_bytes")
        errors = _count(raw.get("errors"))
        completion = _percent(raw.get("peer_completion"))
        scanned = _event_time(raw.get("last_scan"))
        if scanned is not None:
            scans.append(scanned)
        last_file = files_at.get(name)
        if last_file is not None:
            synced.append(last_file)
        items.append(
            {
                "name": name,
                "type": _token(raw.get("type")) or "unknown",
                "state": state,
                "up_to_date": _complete(state, errors, need, need_bytes, completion),
                "files_local": live.get("files_local"),
                "files_global": live.get("files_global"),
                "bytes_local": live.get("bytes_local"),
                "bytes_global": _count(raw.get("global_bytes")),
                "need_files": need,
                "need_bytes": need_bytes,
                "errors": errors,
                "completion": completion,
                "last_sync": last_file,
                "last_scan": scanned,
            }
        )
    conflicts = _count(data.get("conflicts"))
    if conflicts is None:
        names = data.get("conflict_names")
        conflicts = len(names) if isinstance(names, list) else None
    last_sync = max(synced) if synced else None
    last_scan = max(scans) if scans else None
    notes = list(extra.get("notes") or [])
    if last_sync is None and NO_STATS not in notes:
        _note(notes, NO_SYNC_TIME)
    if conflicts:
        notes.append("sync conflicts: %d" % conflicts)
    return {
        "state": "stale" if stale else "ok",
        "stale": stale,
        "age": age,
        "checked": checked,
        "connected": bool(data.get("connected")),
        "peer_last_seen": seen,
        "peer_seen_age": now - seen if seen is not None else None,
        "link": extra.get("link"),
        "version": extra.get("version"),
        "last_sync": last_sync,
        "last_sync_age": now - last_sync if last_sync is not None else None,
        "last_scan": last_scan,
        "last_scan_age": now - last_scan if last_scan is not None else None,
        "conflicts": conflicts,
        "folders": items,
        "notes": notes,
    }


def snapshot(now=None, path=None) -> dict:
    data = load(path)
    stamp = int(time.time()) if now is None else int(now)
    extra = _enrich(list(folders(data)))
    return _normalize(data, extra, stamp)
