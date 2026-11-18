import asyncio
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .access import console_guard, is_admin
from . import agentctl, backupinfo, chat_lifecycle, cli_controls, cli_prompt, config, decision, dictation, fileview, fsroots, models, pcguard, syncinfo, term, tmuxctl, transcript, uploads, usage
from .history import UUID_RE, HistoryIndex
from .model_observation import ModelObserver

CGROUP = Path("/sys/fs/cgroup")
SLICES = ("agent-code.slice", "agent-desktop.slice", "agent-chat.slice")


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    start_claude: bool = True
    model: str | None = None
    continue_chat: bool = False


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


class TitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    confirm: str
    confirm_idle: bool = False


class CompactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str
    confirm_idle: bool = False
    instructions: str = Field(default="", max_length=200)


class ControlsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    effort: str | None = None
    confirm: str
    confirm_idle: bool = False


class PromptAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: str = Field(max_length=64)
    option: int | None = Field(default=None, ge=1, le=99)
    text: str | None = Field(default=None, max_length=4000)


class DirIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class RmDirIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    confirm: str


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


class EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dir: str
    name: str
    data: str


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def memory_info(cgroup: Path = CGROUP, meminfo: Path = Path("/proc/meminfo")) -> dict:
    slices = {}
    for s in SLICES:
        cur = _read_int(cgroup / s / "memory.current")
        mx_raw = None
        try:
            mx_raw = (cgroup / s / "memory.max").read_text().strip()
        except OSError:
            pass
        slices[s.removesuffix(".slice")] = {
            "used": cur,
            "max": int(mx_raw) if mx_raw and mx_raw.isdigit() else None,
        }
    avail = total = None
    try:
        for line in meminfo.read_text().splitlines():
            k, _, v = line.partition(":")
            parts = v.split()
            if k == "MemAvailable" and parts:
                avail = int(parts[0]) * 1024
            elif k == "MemTotal" and parts:
                total = int(parts[0]) * 1024
    except (OSError, ValueError):
        pass
    return {"slices": slices, "available": avail, "total": total}


def _fs(e: pcguard.PcError):
    return HTTPException(e.status, e.detail)


def _tm(e: tmuxctl.TmuxError):
    return HTTPException(e.status, e.detail)


def build_router(history: HistoryIndex) -> APIRouter:
    r = APIRouter(dependencies=[Depends(console_guard)])

    @r.get("/api/models")
    async def model_list():
        data = await asyncio.to_thread(models.catalog)
        if decision.available():
            auto = {'id': decision.AUTO, 'label': 'Auto', 'source': 'auto', 'note': 'Automatic: model and effort are chosen for you'}
            data = {**data, 'models': [auto, *data['models']], 'preferred': decision.AUTO}
        return data

    @r.get("/api/sessions")
    async def sessions(request: Request):
        try:
            items = await tmuxctl.list_sessions()
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        mem = memory_info()
        roots = {"roots": fsroots.roots_list(config.PORTAL_ROOTS), "pc_root": str(config.PC_ROOT)}
        if items is None:
            return {"running": False, "sessions": [], "limit": tmuxctl.session_limit(), "claude": 0, "memory": mem, **roots}
        try:
            all_items = await tmuxctl.list_sessions(all_users=True) or []
            names = await tmuxctl.occupied_names(all_items)
            count = len(names)
            _, detected = await tmuxctl.claude_count()
            records = await asyncio.to_thread(chat_lifecycle.load)
            chat_names = set()
            for i in items:
                i["claude"] = i["name"] in detected
                i["agent"] = agentctl.has(i["name"])
                sid = await tmuxctl.option(i["name"], "@resume_sid")
                if sid:
                    chat_names.add(i["name"])
                i["awake"] = chat_lifecycle.awake(sid, True, records) if sid and UUID_RE.fullmatch(sid) else None
                i["starting"] = i["name"] in names and not i["claude"]
                i["title"] = await tmuxctl.option(i["name"], "@portal_title") or i["name"]
                i["model"] = await tmuxctl.option(i["name"], "@portal_model")
                i["effort"] = await tmuxctl.option(i["name"], "@portal_effort")
                i["model_state"] = await tmuxctl.option(i["name"], "@portal_model_state") or "unknown"
                i["status"] = "claude_detected" if i["claude"] else "process_not_detected"
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        if not is_admin(request):
            items = [i for i in items if i["name"] in chat_names]
        items.sort(key=lambda i: i["name"])
        mine = {item['name'] for item in items} & names
        own_chats = 0
        for name in mine:
            own_chats += bool(await tmuxctl.option(name, '@resume_sid'))
        capacity = {'used': count, 'mine': len(mine), 'other': count - len(mine),
                    'chats': own_chats, 'consoles': len(mine) - own_chats, 'server_limit': config.MAX_CLAUDE_TOTAL}
        return {"running": True, "sessions": items, "limit": tmuxctl.session_limit(), "claude": count, 'capacity': capacity, "memory": mem,
                "mode": config.CHAT_MODE, **roots}

    @r.post("/api/sessions", status_code=201)
    async def create_session(body: SessionIn, request: Request):
        if not body.start_claude and not is_admin(request):
            raise HTTPException(403, "Consoles are available to the owner only")
        if not tmuxctl.NAME_NEW_RE.fullmatch(body.name):
            raise HTTPException(400, "Name: a-z, 0-9, - or _, up to 24 chars.")
        try:
            choice = (body.model if body.model not in (None, 'default') else models.LATEST['sonnet']) if body.start_claude else body.model
            model = await asyncio.to_thread(models.validate, choice)
            info = await fsroots.ahome_path(body.path)
            if not info["exists"]:
                return JSONResponse(status_code=409, content={"error": "missing_path", **info})
            cwd = Path(info["absolute_path"])
            await pcguard.ensure_usable(cwd)
            if not body.start_claude and (model is not None or body.continue_chat):
                raise HTTPException(400, "Model and continue need Claude Code")
            command = [config.CLAUDE_BIN]
            sid = None
            if model is not None:
                command.extend(["--model", model])
            if body.continue_chat:
                own = [i for i in await asyncio.to_thread(history.items) if i['cwd'] and Path(i['cwd']).resolve() == cwd]
                if not own:
                    raise HTTPException(404, "no own chat in this folder")
                sid = own[0]['id']
                command.extend(["--resume", sid])
            elif body.start_claude:
                sid = str(uuid.uuid4())
                command.extend(["--session-id", sid])
            await tmuxctl.create(body.name, str(cwd), body.start_claude, command=command, tag=sid, model=model)
        except pcguard.PcError as e:
            raise _fs(e)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"name": body.name, "path": info["path"], "absolute_path": str(cwd), "session_id": sid}

    @r.delete("/api/sessions/{name}")
    async def delete_session(name: str, body: DeleteIn):
        if not tmuxctl.NAME_ANY_RE.fullmatch(name):
            raise HTTPException(400, "bad name")
        if body.confirm != name:
            raise HTTPException(400, "confirm mismatch")
        try:
            await tmuxctl.kill(name)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"ok": True}

    @r.post("/api/sessions/{name}/interrupt")
    async def interrupt_session(name: str):
        if not tmuxctl.NAME_ANY_RE.fullmatch(name):
            raise HTTPException(400, "bad name")
        try:
            await tmuxctl.interrupt(name)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"ok": True}

    @r.get("/api/sessions/{name}/prompt")
    async def session_prompt(name: str):
        if not tmuxctl.NAME_ANY_RE.fullmatch(name):
            raise HTTPException(400, "bad name")
        if agentctl.has(name):
            chat = agentctl.hub.get(name)
            return {"prompt": chat.prompt() if chat in agentctl.hub.visible() else None}
        try:
            return {"prompt": await cli_prompt.current(name)}
        except tmuxctl.TmuxError:
            return {"prompt": None}

    @r.post("/api/sessions/{name}/prompt")
    async def answer_prompt(name: str, body: PromptAnswerIn):
        if not tmuxctl.NAME_ANY_RE.fullmatch(name):
            raise HTTPException(400, "bad name")
        try:
            if agentctl.has(name):
                await agentctl.chat_for(name).answer(body.signature, body.option, body.text)
            else:
                await cli_prompt.answer(name, body.signature, body.option, body.text)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"ok": True}

    @r.patch("/api/sessions/{name}")
    async def rename_session(name: str, body: TitleIn):
        title = body.title.strip()
        if not title or len(title) > 80 or any(unicodedata.category(c).startswith("C") for c in body.title):
            raise HTTPException(400, "Name: 1-80 chars, no control characters")
        try:
            await tmuxctl.set_title(name, title)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"name": name, "title": title}

    observer = ModelObserver()

    @r.get("/api/sessions/{name}/model")
    async def observed_model(name: str):
        try:
            item = await tmuxctl.require_session(name)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return await asyncio.to_thread(observer.snapshot, item)

    @r.post("/api/sessions/{name}/model")
    async def switch_model(name: str, body: ModelIn):
        if body.confirm != name or not body.confirm_idle:
            raise HTTPException(400, "Confirm Claude is idle with an empty prompt")
        try:
            model = await asyncio.to_thread(models.validate, body.model)
            await tmuxctl.request_model(name, model)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"name": name, "model": model, "model_state": "switch_requested"}

    @r.get("/api/usage")
    async def usage_limits(refresh: bool = False):
        return await usage.get(refresh)

    @r.post("/api/sessions/{name}/compact")
    async def compact(name: str, body: CompactIn):
        if body.confirm != name or not body.confirm_idle:
            raise HTTPException(400, "Confirm Claude is idle with an empty prompt")
        instructions = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", body.instructions).strip()
        try:
            if agentctl.has(name):
                await agentctl.chat_for(name).compact(instructions)
            else:
                await cli_controls.compact(name, instructions)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"name": name, "started": True}

    @r.post("/api/sessions/{name}/controls")
    async def controls(name: str, body: ControlsIn):
        if body.confirm != name or not body.confirm_idle:
            raise HTTPException(400, "Confirm Claude is idle with an empty prompt")
        if body.model is not None:
            body.model = models.canonical(body.model)
        if body.model is not None and ((body.model not in ('default', models.OPUSPLAN) and not models.FULL_RE.fullmatch(body.model)) or body.effort not in (None, 'low', 'medium', 'high', 'xhigh', 'max')):
            raise HTTPException(400, "Pick a CLI model and effort low/medium/high/xhigh/max")
        if body.model is None and body.effort is not None:
            raise HTTPException(400, "Model required")
        try:
            if agentctl.has(name):
                return await agentctl.chat_for(name).operate(body.model, body.effort)
            return await cli_controls.operate(name, body.model, body.effort)
        except tmuxctl.TmuxError as e:
            if getattr(e, 'model_unchanged', False):
                return JSONResponse(status_code=e.status, content={'detail': e.detail, 'model_unchanged': True})
            raise _tm(e)

    async def session_files(name, path, directory):
        try:
            item = await tmuxctl.require_session(name)
            base = await fsroots.aresolve_dir(item["path"], config.PORTAL_ROOTS)
            return await fileview.read(base, path, config.PORTAL_ROOTS, directory)
        except pcguard.PcError as e:
            raise _fs(e)
        except tmuxctl.TmuxError as e:
            raise _tm(e)

    @r.post("/api/uploads", status_code=201)
    async def upload(body: UploadIn):
        try:
            return await asyncio.to_thread(uploads.save, body.dir, body.name, body.data, config.PORTAL_ROOTS)
        except uploads.UploadError as e:
            raise HTTPException(e.status, e.detail)

    @r.post("/api/uploads/raw", status_code=201)
    async def upload_raw(request: Request, dir: str = Query(..., max_length=4096), name: str = Query(..., max_length=200)):
        limit = uploads.RAW_MAX_BYTES
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            raise HTTPException(413, "File is too large (max 100 MB)")
        try:
            part, safe, fd = await asyncio.to_thread(uploads.begin, dir, name, config.PORTAL_ROOTS)
        except uploads.UploadError as e:
            raise HTTPException(e.status, e.detail)
        size, done = 0, False
        try:
            with os.fdopen(fd, "wb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "File is too large (max 100 MB)")
                    await asyncio.to_thread(stream.write, chunk)
            if declared is not None and size != int(declared):
                raise HTTPException(400, "Upload was interrupted")
            try:
                result = await asyncio.to_thread(uploads.finish, part, safe, size)
            except OSError:
                raise HTTPException(503, "Could not save the file")
            done = True
            return result
        finally:
            if not done:
                try:
                    os.unlink(part)
                except OSError:
                    pass

    @r.get("/api/sessions/{name}/files")
    async def file_tree(name: str, path: str = Query("", max_length=4096)):
        return await session_files(name, path, True)

    @r.get("/api/sessions/{name}/file")
    async def file_preview(name: str, path: str = Query(..., max_length=4096)):
        return await session_files(name, path, False)

    @r.get("/api/pc")
    async def pc_info():
        status = pcguard.read_status()
        roots = fsroots.roots_list(config.PORTAL_ROOTS)
        base = {"roots": roots, "pc_root": str(config.PC_ROOT)}
        if status is None:
            return {"state": "watcher_not_running", **base}
        folders = []
        for f in status.get("folders") or []:
            if isinstance(f, dict) and isinstance(f.get("name"), str):
                folders.append({**f, "path": str(config.PC_ROOT / f["name"]), "session_path": "pc/" + f["name"]})
        return {
            "state": "stale" if status["stale"] else "ok",
            "reachable": bool(status.get("reachable")),
            "checked": status.get("checked"),
            "last_ok": status.get("last_ok"),
            "age": status["age"],
            "folders": folders,
            **base,
        }

    @r.get("/api/backup")
    async def backup_info():
        try:
            return await asyncio.to_thread(backupinfo.snapshot)
        except backupinfo.BackupError as e:
            raise HTTPException(e.status, e.detail)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            raise HTTPException(503, backupinfo.BAD_DATA)

    @r.get("/api/sync")
    async def sync_info():
        try:
            return await asyncio.to_thread(syncinfo.snapshot)
        except syncinfo.SyncError as e:
            raise HTTPException(e.status, e.detail)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            raise HTTPException(503, syncinfo.BAD_DATA)

    @r.get("/api/dirs")
    async def dirs(path: str = Query("", max_length=4096)):
        if not path:
            return {"path": None, "parent": None, "roots": fsroots.roots_list(config.PORTAL_ROOTS), "dirs": []}
        try:
            real = await fsroots.aresolve_dir(path, config.PORTAL_ROOTS)
            subs = await fsroots.alist_subdirs(real, config.PORTAL_ROOTS)
        except pcguard.PcError as e:
            raise _fs(e)
        roots = fsroots.real_roots(config.PORTAL_ROOTS)
        parent = None if real in roots else str(real.parent)
        return {"path": str(real), "parent": parent, "roots": [str(x) for x in roots], "dirs": subs}

    @r.post("/api/dirs", status_code=201)
    async def make_dir(body: DirIn):
        try:
            info = await pcguard.call(fsroots.make_home_dir, body.path, pc=False)
        except pcguard.PcError as e:
            raise _fs(e)
        return info

    @r.delete("/api/dirs")
    async def remove_dir(body: RmDirIn):
        try:
            await fsroots.aremove_dir(body.path, body.confirm, config.PORTAL_ROOTS)
        except pcguard.PcError as e:
            raise _fs(e)
        return {"ok": True}

    @r.get("/api/history")
    async def history_list(
        q: str = Query("", max_length=100),
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(50, ge=1, le=50),
    ):
        return await asyncio.to_thread(history.page, q.strip(), offset, limit)

    @r.delete("/api/history/{sid}")
    async def history_delete(sid: str, body: ConfirmIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        if body.confirm != sid:
            raise HTTPException(400, "confirmation does not match")
        try:
            items = await tmuxctl.list_sessions()
            for i in items or []:
                if await tmuxctl.option(i["name"], "@resume_sid") == sid:
                    raise HTTPException(409, "Chat is open in a session; close it first")
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        if not await asyncio.to_thread(history.delete, sid):
            raise HTTPException(404, "no such session")
        return {"ok": True}

    @r.post("/api/history/{sid}/resume", status_code=201)
    async def history_resume(sid: str, body: EmptyIn):
        if not UUID_RE.fullmatch(sid):
            raise HTTPException(400, "bad id")
        item = await asyncio.to_thread(history.find, sid)
        if item is None:
            raise HTTPException(404, "no such session")
        base = f"r-{sid.replace('-', '')[:12]}"
        try:
            items = await tmuxctl.require_server()
            existing = {i["name"] for i in items}
            name = base
            for n in range(1, 6):
                name = base if n == 1 else f"{base}-{n}"
                if name not in existing:
                    break
                if await tmuxctl.option(name, "@resume_sid") == sid:
                    return {"name": name, "existing": True}
            else:
                raise HTTPException(409, "session name taken")
            if not item["cwd"]:
                raise HTTPException(409, "session folder unknown")
            cwd = await fsroots.aresolve_dir(item["cwd"], config.PORTAL_ROOTS)
            await pcguard.ensure_usable(cwd)
            await tmuxctl.create(name, str(cwd), True, [config.CLAUDE_BIN, "--resume", sid], tag=sid)
        except pcguard.PcError as e:
            raise _fs(e)
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        return {"name": name, "existing": False}

    @r.get("/api/sessions/{name}/transcript")
    async def session_transcript(
        name: str,
        after: int | None = Query(None, ge=0),
        state: str = Query("unknown", pattern="^(unknown|thinking|running|idle|stopped)$"),
        tail: bool = False,
        before: int | None = Query(None, ge=0),
    ):
        if not tmuxctl.NAME_ANY_RE.fullmatch(name):
            raise HTTPException(400, "bad name")
        try:
            await tmuxctl.require_session(name)
            sid = await tmuxctl.option(name, "@resume_sid")
        except tmuxctl.TmuxError as e:
            raise _tm(e)
        if not sid or not UUID_RE.fullmatch(sid):
            return {"bound": False}
        path = await asyncio.to_thread(transcript.locate, config.HISTORY_DIR, sid)
        if path is None:
            return {"bound": True, "session_id": sid, "file": False, "items": [], "next": 0, "start": 0, "status": state}
        if before is not None:
            return {"bound": True, "session_id": sid, "file": True, **await asyncio.to_thread(transcript.read_back, path, before)}
        if tail and after is None:
            res = await asyncio.to_thread(transcript.read_back, path)
        else:
            res = await asyncio.to_thread(transcript.read, path, after)
        observed = transcript.status(res["items"], state)
        runtime_idle = False
        if agentctl.has(name):
            chat = agentctl.chat_for(name)
            if chat.working:
                observed = observed if observed in ("thinking", "running") else "thinking"
            elif observed in ("thinking", "running") and res["next"] == res["size"]:
                runtime_idle = True
            return {"bound": True, "session_id": sid, "file": True,
                    "status": "stopped" if runtime_idle else observed, "runtime_idle": runtime_idle, **res}
        if observed in ("thinking", "running") and res["next"] == res["size"]:
            try:
                before = path.stat()
                if before.st_size == res["size"] and time.time() - before.st_mtime > 5:
                    screen = await cli_controls.capture(name)
                    process_ended = False
                    if not cli_controls.idle(screen) and not tmuxctl.claude_starting(name):
                        _, processes = await tmuxctl.claude_count()
                        process_ended = name not in processes
                    current = path.stat()
                    runtime_idle = ((cli_controls.idle(screen) or process_ended) and not cli_controls.busy(name)
                                    and not any(marker in screen for marker in ("Select model", "Enter to confirm", "Do you want"))
                                    and before.st_mtime_ns == current.st_mtime_ns
                                    and before.st_size == current.st_size)
            except (OSError, tmuxctl.TmuxError):
                pass
        return {"bound": True, "session_id": sid, "file": True,
                "status": "stopped" if runtime_idle else observed, "runtime_idle": runtime_idle, **res}

    @r.websocket("/api/sessions/{name}/dictate")
    async def dictate(ws: WebSocket, name: str):
        await dictation.serve(ws, name)

    @r.websocket("/api/term/{name}")
    async def terminal(ws: WebSocket, name: str):
        await term.bridge(ws, name)

    return r
