import json
import os
import stat
from pathlib import Path

from .history import UUID_RE, prompt, submission

MAX_READ = 1 << 20
# Newest-first pages: enough items to fill a screen, read in blocks so huge lines (base64 images) stay whole.
PAGE_ITEMS = 80
BACK_BLOCK = 256 << 10
INTERRUPT = "[Request interrupted by user"


def locate(base: Path, sid: str) -> Path | None:
    if not UUID_RE.fullmatch(sid):
        return None
    try:
        dirs = list(os.scandir(base))
    except OSError:
        return None
    for d in dirs:
        if d.is_symlink() or not d.is_dir(follow_symlinks=False):
            continue
        p = Path(d.path) / f"{sid}.jsonl"
        try:
            if p.is_file() and not p.is_symlink():
                return p
        except OSError:
            continue
    return None


def _tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _args(name: str, data) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt"):
        if isinstance(data.get(key), str):
            return data[key]
    return json.dumps(data, ensure_ascii=False)


def queued_prompt(d: dict, ts: str | None) -> list[dict]:
    # A message sent while Claude is working is recorded as a queued_command attachment, not as a user entry.
    att = d.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command" or att.get("commandMode") != "prompt":
        return []
    # Claude Code in the terminal marks the sender (origin.kind); the stream-json process used by agent chats leaves origin out.
    origin = att.get("origin")
    if origin is not None and (not isinstance(origin, dict) or origin.get("kind") != "human"):
        return []
    if not isinstance(att.get("prompt"), str):
        return []
    text = prompt(att["prompt"])
    if not text:
        return []
    item = {"kind": "user", "id": f"{d.get('uuid') or ''}:queued", "text": text,
            "ts": att.get("timestamp") if isinstance(att.get("timestamp"), str) else ts}
    _, client_id = submission(att["prompt"])
    if client_id:
        item["client_id"] = client_id
    return [item]


def entries(d: dict) -> list[dict]:
    if not isinstance(d, dict) or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return []
    kind = d.get("type")
    ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else None
    if kind == "attachment":
        return queued_prompt(d, ts)
    if kind == "system" and d.get("subtype") == "compact_boundary":
        return [{"kind": "compact", "id": str(d.get("uuid") or ""), "ts": ts}]
    if kind == "system" and d.get("subtype") == "turn_duration":
        return [{"kind": "turn_end", "id": str(d.get("uuid") or ""), "ts": ts}]
    msg = d.get("message")
    if kind not in ("user", "assistant") or not isinstance(msg, dict):
        return []
    uid = str(d.get("uuid") or "")
    content = msg.get("content")
    if kind == "assistant" and d.get("isApiErrorMessage"):
        return [{"kind": "turn_error", "id": uid, "ts": ts}]
    if (kind == "assistant" and msg.get("model") == "<synthetic>"
            and content == [{"type": "text", "text": "No response requested."}]):
        return [{"kind": "turn_unanswered", "id": uid, "ts": ts}]
    out = []
    if kind == "user":
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return []
        user = None
        raw_text = "\n".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
        _, client_id = submission(raw_text)
        for i, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text = part["text"]
                if text.startswith(INTERRUPT):
                    out.append({"kind": "interrupted", "id": f"{uid}:{i}", "ts": ts})
                elif prompt(text):
                    if user is None:
                        user = {"kind": "user", "id": f"{uid}:{i}", "text": "", "ts": ts}
                        if client_id:
                            user["client_id"] = client_id
                        out.append(user)
                    user["text"] = (user["text"] + "\n" + prompt(text)).strip()
                elif text.lstrip().startswith("<task-notification>"):
                    out.append({"kind": "wake", "id": f"{uid}:{i}", "ts": ts})
            elif part.get("type") == "tool_result":
                out.append({"kind": "tool_result", "tool_id": str(part.get("tool_use_id") or ""),
                            "text": _tool_text(part.get("content")),
                            "error": bool(part.get("is_error")), "ts": ts})
        return out
    if not isinstance(content, list):
        return []
    for i, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            out.append({"kind": "assistant", "id": f"{uid}:{i}", "text": part["text"], "ts": ts})
        elif part.get("type") == "thinking" and isinstance(part.get("thinking"), str) and part["thinking"].strip():
            out.append({"kind": "thinking", "id": f"{uid}:{i}", "text": part["thinking"].strip(), "ts": ts})
        elif part.get("type") == "tool_use":
            name = str(part.get("name") or "Tool")[:80]
            out.append({"kind": "tool", "id": str(part.get("id") or f"{uid}:{i}"), "name": name,
                        "args": _args(name, part.get("input")), "ts": ts})
    if msg.get("stop_reason") == "end_turn" and out and out[-1]["kind"] != "thinking":
        out[-1]["final"] = True
    return out


def status(items: list[dict], prev: str = "unknown") -> str:
    state = prev
    pending: set[str] = set()
    for it in items:
        k = it["kind"]
        if k in ("user", "wake"):
            state = "thinking"
        elif k == "tool":
            pending.add(it["id"])
            state = "running"
        elif k == "tool_result":
            pending.discard(it["tool_id"])
            state = "running" if pending else "thinking"
        elif k == "turn_end":
            pending.clear()
            if state != "stopped":
                state = "idle"
        elif k in ("interrupted", "turn_unanswered", "turn_error"):
            pending.clear()
            state = "stopped"
        elif k == "assistant":
            state = "idle" if it.get("final") else ("running" if pending else "thinking")
    return state


def _open(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        return None, None
    return os.fdopen(fd, 'rb'), info


def read_back(path: Path, before: int | None = None) -> dict:
    """Whole records ending at `before` (default: end of file), read backwards until a page is full.

    `start` is where the page begins: pass it as the next `before`; 0 means the first record is included.
    `next` is where forward reading continues, so a record still being written stays with the forward reader.
    """
    f, info = _open(path)
    if f is None:
        return {"items": [], "start": 0, "next": before or 0, "size": 0}
    with f:
        size = info.st_size
        end = size if before is None or not 0 <= before <= size else before
        items: list[dict] = []
        buf, lo, start, nxt = b'', end, end, end
        while start > 0 and len(items) < PAGE_ITEMS:
            cut = buf.rfind(b'\n', 0, max(0, len(buf) - 1))
            if cut < 0 and lo > 0:
                lo, prev = max(0, lo - max(BACK_BLOCK, len(buf))), lo
                f.seek(lo)
                buf = f.read(prev - lo) + buf
                continue
            line, buf = buf[cut + 1:], buf[:cut + 1]
            start -= len(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                if start + len(line) == size and not line.endswith(b'\n'):
                    nxt = start
                continue
            items[:0] = entries(record)
            if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "compact_boundary":
                start = 0  # a compacted chat starts here: what came before is not shown
                break
        return {"items": items, "start": start, "next": nxt, "size": size}


def read(path: Path, after: int | None) -> dict:
    f, info = _open(path)
    if f is None:
        return {"items": [], "next": after or 0, "size": 0}
    with f:
        size = info.st_size
        start = after if after is not None and 0 <= after <= size else 0
        f.seek(start)
        items = []
        while f.tell() < size and f.tell() - start < MAX_READ:
            position = f.tell()
            raw = f.readline(size - position)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                if not raw.endswith(b'\n'):
                    f.seek(position)
                    break
                continue
            items.extend(entries(record))
        return {"items": items, "next": f.tell(), "size": size}
