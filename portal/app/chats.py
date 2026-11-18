import asyncio
import json
import os
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .access import console_guard
from . import agentctl, chat_lifecycle, chat_models, cli_controls, config, decision, fsroots, models, outbox, ownership, pcguard, tmuxctl, transcript, users
from .history import UUID_RE, HistoryIndex, scrub

TITLE_LEN = 60
MAX_LIST = 200
SNIPPET = 120
SEARCH_PER_CHAT = 20
SEARCH_TOTAL = 400


class OpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    model: str | None = None
    text: str | None = Field(default=None, max_length=200000)


class NewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    draft: bool = False


class ChatModelIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    effort: str | None = None


class TitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


def chat_root() -> Path:
    return users.chat_root()


def ensure_root(root: Path) -> None:
    users.ensure_home()
    root.mkdir(mode=0o700, exist_ok=True)
    st = root.stat()
    if st.st_uid == os.getuid() and (st.st_mode & 0o700) != 0o700:
        os.chmod(root, 0o700)


def titles_file() -> Path:
    return users.titles_file()


def load_titles() -> dict:
    try:
        data = json.loads(titles_file().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if UUID_RE.fullmatch(k) and isinstance(v, str)} if isinstance(data, dict) else {}


_titles_lock = threading.Lock()


def save_title(sid: str, title: str | None) -> None:
    with _titles_lock:
        _save_title(sid, title)


def _save_title(sid: str, title: str | None) -> None:
    data = load_titles()
    if title is None:
        data.pop(sid, None)
    else:
        data[sid] = title
    path = titles_file()
    for d in reversed([path.parent, *path.parent.parents][:3]):
        if not d.exists():
            d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= TITLE_LEN else text[: TITLE_LEN - 1].rstrip() + "…"


async def live_sessions() -> tuple[dict[str, dict], list[dict]]:
    items = await tmuxctl.list_sessions()
    if items is None:
        return {}, []
    _, names = await tmuxctl.claude_count()
    occupied = await tmuxctl.occupied_names(await tmuxctl.list_sessions(all_users=True) or [])
    chats: dict[str, dict] = {}
    consoles = []
    for i in items:
        sid = await tmuxctl.option(i["name"], "@resume_sid")
        i["claude"] = i["name"] in names
        i["starting"] = i["name"] in occupied and not i["claude"]
        i["title"] = await tmuxctl.option(i["name"], "@portal_title") or i["name"]
        if sid and UUID_RE.fullmatch(sid):
            chats[sid] = i
        else:
            consoles.append(i)
    return chats, consoles


def fragment(text: str, pos: int, length: int) -> str:
    start = max(0, pos - (SNIPPET - length) // 2)
    end = min(len(text), start + SNIPPET)
    start = max(0, end - SNIPPET)
    return ("…" if start else "") + scrub(text[start:end]) + ("…" if end < len(text) else "")


def session_name(sid: str) -> str:
    return "c-" + sid.replace("-", "")[:12]


def _tm(e: tmuxctl.TmuxError):
    return HTTPException(e.status, e.detail)


def build_router(history: HistoryIndex) -> APIRouter:
    r = APIRouter(dependencies=[Depends(console_guard)])
    lock = asyncio.Lock()

    @r.get("/api/chats")
    async def chat_list():
        async with lock:
            return await list_chats()

    async def list_chats():
        try:
            live, consoles = await live_sessions()
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        titles = await asyncio.to_thread(load_titles)
        records = await asyncio.to_thread(chat_lifecycle.load)
        out = []
        seen = set()
        for it in (await asyncio.to_thread(history.items))[:MAX_LIST]:
            s = live.get(it["id"])
            seen.add(it["id"])
            out.append({
                "id": it["id"],
                "title": titles.get(it["id"]) or short(it["preview"]) or "New chat",
                "cwd": it["cwd"],
                "modified": it["modified"],
                "active": s is not None,
                "awake": chat_lifecycle.awake(it["id"], s is not None, records),
                "name": s["name"] if s else None,
                "claude": bool(s and s["claude"]),
                "starting": bool(s and s["starting"]),
                "empty": False,
            })
        for sid, s in live.items():
            if sid in seen:
                continue
            title = titles.get(sid) or (s["title"] if s["title"] != s["name"] else "New chat")
            seen.add(sid)
            out.append({"id": sid, "title": title, "cwd": s["path"], "modified": s["created"], "active": True, "awake": chat_lifecycle.awake(sid, True, records), "name": s["name"], "claude": s["claude"], "starting": s["starting"], "empty": True})
        for sid, record in records.items():
            if sid not in seen:
                out.append({"id": sid, "title": titles.get(sid) or "New chat", "cwd": record["cwd"], "modified": record["modified"], "active": False, "awake": False, "name": None, "claude": False, "empty": True})
        for item in out:
            if item.get('name'):
                await chat_models.remember_session(item['name'])
        records = await asyncio.to_thread(chat_lifecycle.load)
        for item in out:
            remembered = await asyncio.to_thread(chat_models.snapshot, item['id'], records)
            item.update(remembered)
            if remembered.get('model') and remembered != {key: records.get(item['id'], {}).get(key) for key in remembered}:
                await asyncio.to_thread(chat_lifecycle.save, item['id'], item['awake'], item['cwd'], item['modified'], **remembered)
        out.sort(key=lambda c: c["modified"], reverse=True)
        return {
            "chats": out,
            "consoles": [{"name": c["name"], "title": c["title"], "path": c["path"], "claude": c["claude"]} for c in consoles],
        }

    @r.get("/api/chats/search")
    async def chat_search(q: str = Query("", max_length=120), inside: int = Query(0, ge=0, le=1)):
        needle = q.strip().casefold()

        def search():
            titles = load_titles()
            items = history.items()[:MAX_LIST]
            seen = {it["id"] for it in items}
            for sid, record in chat_lifecycle.load().items():
                if sid not in seen:
                    items.append({"id": sid, "cwd": record["cwd"], "preview": "", "modified": record["modified"]})
            items.sort(key=lambda it: it["modified"], reverse=True)
            out = []
            total = 0
            for it in items:
                title = titles.get(it["id"]) or short(it["preview"]) or "New chat"
                if needle in title.casefold() or needle in it["preview"].casefold():
                    out.append({"id": it["id"], "title": title, "matches": []})
                    continue
                if not inside or total >= SEARCH_TOTAL:
                    continue
                path = transcript.locate(config.HISTORY_DIR, it["id"])
                if path is None:
                    continue
                matches = []
                for text in history.texts(str(path)):
                    pos = text.casefold().find(needle)
                    if pos >= 0:
                        matches.append(fragment(text, pos, len(needle)))
                        if len(matches) >= SEARCH_PER_CHAT or total + len(matches) >= SEARCH_TOTAL:
                            break
                if matches:
                    total += len(matches)
                    out.append({"id": it["id"], "title": title, "matches": matches})
            return {"chats": out, "truncated": total >= SEARCH_TOTAL}

        if not needle:
            return {"chats": [], "truncated": False}
        return await asyncio.to_thread(search)

    @r.post("/api/chats", status_code=201)
    async def chat_new(body: NewIn):
        async with lock:
            await asyncio.to_thread(chat_lifecycle.check_writable)
            try:
                root = chat_root()
                await asyncio.to_thread(ensure_root, root)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                folder = root / stamp
                n = 2
                while folder.exists():
                    folder = root / f"{stamp}-{n}"
                    n += 1
                real_root = Path(os.path.realpath(root.parent))
                if not any(fsroots.inside(real_root, x) for x in fsroots.real_roots(config.PORTAL_ROOTS)):
                    raise HTTPException(403, "chat folder not allowed")
                await asyncio.to_thread(folder.mkdir, 0o700, False, False)
                await asyncio.to_thread(os.chmod, folder, 0o700)
                await asyncio.to_thread(outbox.ensure, folder)
                sid = str(uuid.uuid4())
                name = session_name(sid)
                # A new chat waits for its first message to get a model: Auto picks it then (owner only, while a key is set).
                auto = body.draft and decision.available()
                model = decision.AUTO if auto else await asyncio.to_thread(models.validate, models.LATEST['sonnet'])
                if body.draft:
                    await asyncio.to_thread(ownership.claim, sid)
                else:
                    await tmuxctl.create(name, str(folder), True, [config.CLAUDE_BIN, '--model', model, "--session-id", sid], tag=sid, model=model)
                await asyncio.to_thread(chat_lifecycle.save, sid, not body.draft, str(folder), time.time(),
                                        model=model, model_source='auto' if auto else 'selected', model_saved_at=time.time())
            except tmuxctl.TmuxError as e:
                raise _tm(e)
        return {"id": sid, "name": None if body.draft else name, "cwd": str(folder), 'model': model}

    @r.patch('/api/chats/{sid}/model')
    async def remember_chat_model(sid: str, body: ChatModelIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, 'bad id')
        async with lock:
            records = await asyncio.to_thread(chat_lifecycle.load)
            item = records.get(sid) or await asyncio.to_thread(history.find, sid)
            if item is None:
                raise HTTPException(404, 'no such chat')
            live, _ = await live_sessions()
            if sid in live and (live[sid]['claude'] or live[sid]['starting']):
                raise HTTPException(409, 'Change the running model through Claude controls')
            try:
                if body.model == decision.AUTO:
                    if not decision.available():
                        raise HTTPException(400, 'Auto is not available: no decision key is set')
                    model = decision.AUTO
                else:
                    model = await asyncio.to_thread(models.validate, body.model)
                previous = await asyncio.to_thread(chat_models.snapshot, sid, records)
                # Effort belongs to the chat, not to a model family: it stays when the model changes, and Auto chooses its own.
                if model == decision.AUTO:
                    effort = None
                elif body.effort is not None:
                    if body.effort not in chat_models.EFFORTS:
                        raise HTTPException(400, 'bad effort')
                    effort = body.effort
                else:
                    effort = previous.get('effort')
                await asyncio.to_thread(chat_lifecycle.save, sid, False, item['cwd'], item['modified'],
                                        model=model, model_source='selected', model_saved_at=time.time(),
                                        effort=effort, effort_model=model, effort_source='selected')
            except tmuxctl.TmuxError as e:
                raise _tm(e)
        return {'id': sid, 'model': model, 'effort': effort}

    @r.post("/api/consoles", status_code=201)
    async def console_new():
        async with lock:
            try:
                items = await tmuxctl.list_sessions(all_users=True) or []
                taken = {i["name"] for i in items}
                mine = {i["name"] for i in await tmuxctl.list_sessions() or []}
                titles = {await tmuxctl.option(x, "@portal_title") for x in mine}
                k = 1
                while f"Console {k}" in titles:
                    k += 1
                n = 1
                while f"console-{n}" in taken:
                    n += 1
                name = f"console-{n}"
                home = config.HOME.resolve()
                await tmuxctl.create(name, str(home), False)
                await tmuxctl.set_title(name, f"Console {k}")
            except tmuxctl.TmuxError as e:
                raise _tm(e)
        return {"name": name, "title": f"Console {k}"}

    @r.get("/api/chats/{sid}/transcript")
    async def chat_transcript(sid: str, after: int | None = Query(None, ge=0), tail: bool = False,
                              before: int | None = Query(None, ge=0)):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        try:
            live, _ = await live_sessions()
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        records = await asyncio.to_thread(chat_lifecycle.load)
        if sid not in live and sid not in records and await asyncio.to_thread(history.find, sid) is None:
            raise HTTPException(404, "no such chat")
        path = await asyncio.to_thread(transcript.locate, config.HISTORY_DIR, sid)
        if path is None:
            return {"id": sid, "file": False, "items": [], "next": 0, "start": 0}
        if before is not None:
            res = await asyncio.to_thread(transcript.read_back, path, before)
        elif tail and after is None:
            res = await asyncio.to_thread(transcript.read_back, path)
        else:
            res = await asyncio.to_thread(transcript.read, path, after)
        return {"id": sid, "file": True, **res}

    async def chat_outbox_folder(sid: str):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        try:
            live, _ = await live_sessions()
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        item = await asyncio.to_thread(history.find, sid)
        records = await asyncio.to_thread(chat_lifecycle.load)
        item = item or records.get(sid)
        if sid not in live and item is None:
            raise HTTPException(404, "no such chat")
        return outbox.chat_folder((live.get(sid) or {}).get("path") or (item or {}).get("cwd"))

    @r.get("/api/chats/{sid}/outbox")
    async def chat_outbox(sid: str):
        folder = await chat_outbox_folder(sid)
        return {"id": sid, "limit": outbox.MAX_BYTES, "files": await asyncio.to_thread(outbox.listing, folder)}

    @r.get("/api/chats/{sid}/outbox/{name}")
    async def chat_outbox_file(request: Request, sid: str, name: str, view: str | None = None, scripts: str | None = None):
        # Caddy keeps this route's own sandbox policy only when it sees exactly view=1. A repeated parameter reads differently there
        # and here, and would let a file from the chat open under the portal's policy: refuse it.
        if len(request.query_params.getlist("view")) > 1 or len(request.query_params.getlist("scripts")) > 1:
            raise HTTPException(400, "Repeated query parameter")
        folder = await chat_outbox_folder(sid)
        previewing = view == "1"
        if previewing and not outbox.preview_kind(name):
            raise HTTPException(415, "No preview for this file type")
        try:
            fd, size = await asyncio.to_thread(outbox.open_file, folder, name)
        except outbox.OutboxError as e:
            raise HTTPException(e.status, e.detail)
        if not previewing:
            return StreamingResponse(outbox.stream(fd, size), media_type=outbox.media_type(name), headers=outbox.headers(name, size))
        if outbox.preview_kind(name) == "text" and size > outbox.PREVIEW_TEXT_MAX:
            os.close(fd)
            raise HTTPException(413, "File is too large to preview; download it instead")
        return StreamingResponse(outbox.stream(fd, size), media_type=outbox.preview_media_type(name),
                                 headers=outbox.preview_headers(name, size, scripts == "1"))

    @r.post("/api/chats/{sid}/open")
    async def chat_open(sid: str, body: OpenIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        # The question to Google takes a second or more and must not hold the lock: every client's chat list waits on it.
        early = None
        if decision.available() and body.model in (None, 'default', decision.AUTO):
            known = await asyncio.to_thread(chat_lifecycle.load)
            kept = await asyncio.to_thread(chat_models.snapshot, sid, known)
            if (body.model if body.model not in (None, 'default') else kept.get('model')) == decision.AUTO:
                early = await asyncio.to_thread(decision.pick, body.text or '', True)
        async with lock:
            await asyncio.to_thread(chat_lifecycle.check_writable)
            try:
                live, _ = await live_sessions()
                if sid in live and (live[sid]["claude"] or live[sid]["starting"]):
                    await asyncio.to_thread(chat_lifecycle.save, sid, True, live[sid]["path"], live[sid]["created"])
                    return {"id": sid, "name": live[sid]["name"], "started": False}
                item = await asyncio.to_thread(history.find, sid)
                records = await asyncio.to_thread(chat_lifecycle.load)
                item = item or records.get(sid) or live.get(sid)
                if item is None:
                    raise HTTPException(404, "no such chat")
                folder = item.get("cwd") or item.get("path")
                if not folder:
                    raise HTTPException(409, "chat folder unknown")
                cwd = await fsroots.aresolve_dir(folder, config.PORTAL_ROOTS)
                await pcguard.ensure_usable(cwd)
                own = outbox.chat_folder(str(cwd))
                if own is not None:
                    await asyncio.to_thread(outbox.ensure, own)
                remembered = await asyncio.to_thread(chat_models.snapshot, sid, records)
                choice = body.model if body.model not in (None, 'default') else remembered.get('model') or models.LATEST['sonnet']
                picked = None
                if choice == decision.AUTO:
                    # Decided once, from the first message; the answer is saved in the chat and the chat keeps it.
                    picked = early or await asyncio.to_thread(decision.pick, body.text or '', decision.available())
                    model = picked['model']
                else:
                    model = await asyncio.to_thread(models.validate, choice)
                if sid in live:
                    await tmuxctl.kill(live[sid]["name"])
                name = session_name(sid)
                path = await asyncio.to_thread(transcript.locate, config.HISTORY_DIR, sid)
                command = [config.CLAUDE_BIN] + (["--model", model] if model else []) + (["--resume", sid] if path else ["--session-id", sid])
                effort = picked['effort'] if picked else remembered.get('effort') if remembered.get('model') == model else None
                if effort in chat_models.EFFORTS:
                    command += ['--effort', effort]
                else:
                    effort = None
                await tmuxctl.create(name, str(cwd), True, command, tag=sid, model=model, effort=effort)
                await asyncio.to_thread(chat_lifecycle.save, sid, True, str(cwd), item.get("modified", time.time()),
                                        model=model, model_source='auto' if picked else 'launch_requested', model_saved_at=time.time(),
                                        effort=effort, effort_model=model, **({'auto': picked['source']} if picked else {}))
            except pcguard.PcError as e:
                raise HTTPException(e.status, e.detail)
            except tmuxctl.TmuxError as e:
                raise _tm(e)
        return {"id": sid, "name": name, "started": True, "model": model, "effort": effort, **({'auto': picked['source']} if picked else {})}

    @r.post("/api/chats/{sid}/sleep")
    async def chat_sleep(sid: str, body: NewIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        async with lock:
            await asyncio.to_thread(chat_lifecycle.check_writable)
            try:
                live, _ = await live_sessions()
                records = await asyncio.to_thread(chat_lifecycle.load)
                item = await asyncio.to_thread(history.find, sid)
                session = live.get(sid)
                record = records.get(sid)
                if session is None and item is None and record is None:
                    raise HTTPException(404, "no such chat")
                source = item or record or session
                cwd = source.get("cwd") or source.get("path")
                modified = source.get("modified", source.get("created", time.time()))
                if session:
                    await chat_models.remember_session(session['name'])
                    await tmuxctl.kill(session["name"])
                await asyncio.to_thread(chat_lifecycle.save, sid, False, cwd, modified)
            except tmuxctl.TmuxError as e:
                raise _tm(e)
        return {"id": sid, "awake": False}

    @r.get("/api/sessions/{name}/ready")
    async def session_ready(name: str):
        try:
            if agentctl.has(name):
                chat = agentctl.chat_for(name)
                return {"ready": chat.alive and chat.ready and not chat.working, "reason": None}
            await tmuxctl.require_session(name)
            screen = await cli_controls.capture(name)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"ready": cli_controls.idle(screen), "reason": cli_controls.stuck(screen)}

    @r.patch("/api/chats/{sid}")
    async def chat_rename(sid: str, body: TitleIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        title = body.title.strip()
        if not title or len(title) > 80 or any(unicodedata.category(c).startswith("C") for c in body.title):
            raise HTTPException(400, "Name: 1-80 chars, no control characters")
        try:
            live, _ = await live_sessions()
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        records = await asyncio.to_thread(chat_lifecycle.load)
        if sid not in live and sid not in records and await asyncio.to_thread(history.find, sid) is None:
            raise HTTPException(404, "no such chat")
        await asyncio.to_thread(save_title, sid, title)
        if sid in live:
            try:
                await tmuxctl.set_title(live[sid]["name"], title)
            except tmuxctl.TmuxError:
                pass
        return {"id": sid, "title": title}

    @r.delete("/api/chats/{sid}")
    async def chat_delete(sid: str, body: ConfirmIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        if body.confirm != sid:
            raise HTTPException(400, "confirmation does not match")
        async with lock:
            await asyncio.to_thread(chat_lifecycle.check_writable)
            try:
                live, _ = await live_sessions()
                session = live.get(sid)
                item = await asyncio.to_thread(history.find, sid)
                records = await asyncio.to_thread(chat_lifecycle.load)
                record = records.get(sid)
                if session is None and item is None and record is None:
                    raise HTTPException(404, "no such chat")
                if session is not None:
                    await tmuxctl.kill(session["name"])
            except tmuxctl.TmuxError as e:
                raise _tm(e)
            folder = (item or record or {}).get("cwd") or (session or {}).get("path")
            try:
                await asyncio.to_thread(history.delete, sid)
            except OSError:
                raise HTTPException(500, "Could not delete the chat file on the server")
            try:
                await asyncio.to_thread(save_title, sid, None)
            except OSError:
                pass
            await asyncio.to_thread(chat_lifecycle.remove, sid)
            await asyncio.to_thread(remove_empty, folder)
        return {"ok": True}

    return r


def remove_empty(folder: str | None) -> None:
    if not folder:
        return
    try:
        path = Path(os.path.realpath(folder))
        root = Path(os.path.realpath(chat_root()))
        if path.parent != root or not path.is_dir():
            return
        box = path / outbox.NAME
        if [p.name for p in path.iterdir()] == [outbox.NAME] and box.is_dir() and not box.is_symlink() and not any(box.iterdir()):
            box.rmdir()
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass
