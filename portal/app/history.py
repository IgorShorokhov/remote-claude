import json
import os
import re
import shutil
import threading
from pathlib import Path

from . import ownership, users

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_FILES = 5000
SERVICE_FOLDER = "-agent-portal-memory-job"  # the nightly memory run, never a chat
MAX_LINES = 80
MAX_BYTES = 262144
MAX_LINE = 65536
PREVIEW = 120
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}|[A-Za-z0-9+/_=-]{32,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,}|(?i:bearer\s+[A-Za-z0-9._~+/=-]{8,})|(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@|(?i:(password|passwd|pwd|passphrase|token|secret|api[_-]?key|private[_-]?key|парол\w*|токен\w*|ключ\w*|секрет\w*)\s*[:=\-—]?\s*\S+))"
)


_PASTED_RE = re.compile(r'</?pasted_content(?: id="[^"\n]*")?>')
_PORTAL_MESSAGE_RE = re.compile(r'\n<portal-message-id>([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})</portal-message-id>\s*$')


def submission(text: str) -> tuple[str, str | None]:
    text = _PASTED_RE.sub("", text)
    match = _PORTAL_MESSAGE_RE.search(text)
    return (text[:match.start()].rstrip("\n"), match[1]) if match else (text, None)


# The composer used to write an empty paragraph as a literal &nbsp; line; old messages still carry it.
_BLANK_RE = re.compile(r"^[ \t]*(?:&nbsp;|\xa0)[ \t]*$", re.M)


def scrub(text: str) -> str:
    return _SECRET_RE.sub("[hidden]", text)


def prompt(text: str) -> str:
    text = submission(text)[0]
    if "&nbsp;" in text or "\xa0" in text:
        text = re.sub(r"\n{3,}", "\n\n", _BLANK_RE.sub("", text))
    text = text.strip()
    return "" if text.startswith("<") else text


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def parse_head(path: Path) -> dict:
    cwd = None
    first_ts = None
    preview = ""
    read = 0
    try:
        with open(path, "rb") as f:
            for _ in range(MAX_LINES):
                raw = f.readline(MAX_LINE)
                if not raw:
                    break
                read += len(raw)
                if read > MAX_BYTES:
                    break
                if len(raw) >= MAX_LINE and not raw.endswith(b"\n"):
                    while True:
                        chunk = f.readline(MAX_LINE)
                        read += len(chunk)
                        if not chunk or chunk.endswith(b"\n") or read > MAX_BYTES:
                            break
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                if cwd is None and isinstance(d.get("cwd"), str):
                    cwd = d["cwd"]
                if first_ts is None and isinstance(d.get("timestamp"), str):
                    first_ts = d["timestamp"]
                if not preview and d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain") and not d.get("isCompactSummary"):
                    msg = d.get("message")
                    text = _text_of(msg.get("content")) if isinstance(msg, dict) else ""
                    text = prompt(text)
                    if text:
                        preview = scrub(" ".join(text.split())[:2000])[:PREVIEW]
                if cwd and preview and first_ts:
                    break
    except OSError:
        pass
    return {"cwd": cwd, "started": first_ts, "preview": preview}


SEARCH_BYTES = 16 << 20


def message_texts(path: Path, size: int) -> list[str]:
    if size > SEARCH_BYTES:
        return []
    out = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"user"' not in raw and b'"assistant"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
                    continue
                kind = d.get("type")
                msg = d.get("message")
                if kind not in ("user", "assistant") or not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        text = prompt(part["text"]) if kind == "user" else part["text"]
                        text = " ".join(text.split())
                        if text:
                            out.append(text)
    except OSError:
        return []
    return out


class HistoryIndex:
    def __init__(self, base: Path):
        self.base = Path(base)
        self._cache: dict[str, tuple[tuple[int, int], dict]] = {}
        self._texts: dict[str, tuple[tuple[int, int], list[str]]] = {}
        self._lock = threading.Lock()

    def _files(self) -> list[tuple[str, str, os.stat_result]]:
        out = []
        try:
            dirs = list(os.scandir(self.base))
        except OSError:
            return out
        for d in dirs:
            try:
                if d.is_symlink() or not d.is_dir(follow_symlinks=False) or d.name.endswith(SERVICE_FOLDER):
                    continue
                with os.scandir(d.path) as it:
                    for e in it:
                        if not e.name.endswith(".jsonl") or e.is_symlink():
                            continue
                        sid = e.name[:-6]
                        if not UUID_RE.fullmatch(sid) or not e.is_file(follow_symlinks=False):
                            continue
                        out.append((sid, e.path, e.stat(follow_symlinks=False)))
                        if len(out) >= MAX_FILES:
                            return out
            except OSError:
                continue
        return out

    def items(self) -> list[dict]:
        with self._lock:
            res = self._items()
        return [i for i in res if ownership.owns(i["id"], i["cwd"])]

    def texts(self, path: str) -> list[str]:
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._texts.get(path)
        if hit and hit[0] == key:
            return hit[1]
        out = message_texts(Path(path), st.st_size)
        with self._lock:
            self._texts[path] = (key, out)
        return out

    def _items(self) -> list[dict]:
        res = []
        seen = set()
        for sid, path, st in self._files():
            key = (st.st_mtime_ns, st.st_size)
            hit = self._cache.get(path)
            if hit and hit[0] == key:
                info = hit[1]
            else:
                info = parse_head(Path(path))
                self._cache[path] = (key, info)
            seen.add(path)
            res.append(
                {
                    "id": sid,
                    "cwd": info["cwd"],
                    "started": info["started"],
                    "modified": int(st.st_mtime),
                    "size": st.st_size,
                    "preview": info["preview"],
                }
            )
        for p in list(self._cache):
            if p not in seen:
                del self._cache[p]
        res.sort(key=lambda x: x["modified"], reverse=True)
        return res

    def find(self, sid: str) -> dict | None:
        if not UUID_RE.fullmatch(sid):
            return None
        for it in self.items():
            if it["id"] == sid:
                return it
        return None

    def delete(self, sid: str) -> bool:
        if not UUID_RE.fullmatch(sid) or self.find(sid) is None:
            return False
        with self._lock:
            hits = [Path(path) for s, path, _ in self._files() if s == sid]
            if not hits:
                return False
            for f in hits:
                side = f.with_suffix("")
                if side.is_dir() and not side.is_symlink():
                    shutil.rmtree(side)
                f.unlink()
                self._cache.pop(str(f), None)
            return True

    def page(self, q: str, offset: int, limit: int) -> dict:
        items = self.items()
        if q:
            ql = q.lower()
            items = [
                i for i in items
                if ql in i["preview"].lower() or ql in (i["cwd"] or "").lower() or ql in i["id"]
            ]
        return {"total": len(items), "offset": offset, "items": items[offset : offset + limit]}
