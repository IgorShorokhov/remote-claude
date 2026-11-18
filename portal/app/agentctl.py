"""Agent mode: chats are Claude Code processes spoken to in stream-json (the way the VS Code extension does), not tmux terminals.

The processes live in the agent host service (agent_host.py) so a portal restart does not stop them. This module is the portal's side:
it keeps a Chat per process, rebuilds its state from the host's numbered line log, and offers what the tmux backend offered
(list, kill, interrupt, model switch, compact, permission answers) without reading a screen.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import deque

from . import config, models, users
from .errors import TmuxError

log = logging.getLogger(__name__)

LINE_LIMIT = 1 << 26
CALL_TIMEOUT = 15.0
RECONNECT_EVERY = 3.0
# The host answers an attach by queueing every line it still keeps, and it drops a connection whose queue (4096 messages) overflows.
# So only the newest lines of a long log are asked for; the rest of a chat's state comes from its history file.
REPLAY_LINES = 2000
MESSAGE_ID = re.compile(r"<portal-message-id>([0-9a-fA-F-]{8,64})</portal-message-id>\s*$")
INPUT_SHOWN = 6000
RATE_KINDS = {"five_hour": "five_hour", "seven_day": "seven_day"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def mode() -> str:
    return config.CHAT_MODE


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Sub:
    """One listener of a chat (a WebSocket bridge). Discrete events queue up; the live preview is coalesced by the listener."""

    def __init__(self):
        self.events: deque = deque(maxlen=512)
        self.wake = asyncio.Event()

    def push(self, event):
        self.events.append(event)
        self.wake.set()


class Chat:
    def __init__(self, hub, name, cwd, meta, pid=None):
        self.hub = hub
        self.name = name
        self.cwd = cwd
        self.meta = dict(meta)
        self.pid = pid
        self.alive = True
        self.code = None
        self.seq = 0
        self.ready = False
        self.busy = False
        self.compacting = False
        self.requests: dict[str, dict] = {}
        self.futures: dict[str, asyncio.Future] = {}
        self.models: list[dict] = []
        self.current_model = self.meta.get("model")
        self.session_id = self.meta.get("sid")
        self.subs: set[Sub] = set()
        self.turn_id = None
        self.stream = uuid.uuid4().hex
        self.part = 0
        self.blocks: dict[int, str] = {}
        self.preview = None
        self.preview_version = 0
        self.errors: deque = deque(maxlen=20)
        self.last_activity = float(self.meta.get("started") or time.time())
        self.asked: list[tuple[str, str]] = []
        self.loading = False

    # --- reading the line log -------------------------------------------------------------------------------------------------
    def apply(self, seq, d, line):
        if seq <= self.seq:
            return
        self.seq = seq
        if d == "err":
            self.errors.append(line[:300])
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if d == "in":
            self.apply_in(kind, message)
        else:
            self.apply_out(kind, message)

    def apply_in(self, kind, message):
        if kind == "user":
            self.busy = True
            self.last_activity = time.time()
            self.blocks.clear()
            self.part += 1
            content = (message.get("message") or {}).get("content")
            text = content if isinstance(content, str) else ""
            found = MESSAGE_ID.search(text)
            self.turn_id = found[1] if found else uuid.uuid4().hex
        elif kind == "control_response":
            answered = (message.get("response") or {}).get("request_id")
            if self.requests.pop(answered, None) is not None:
                self.asked = []
                self.emit({"type": "agent_state", "sid": self.session_id})

    def apply_out(self, kind, message):
        if kind == "control_response":
            response = message.get("response") or {}
            future = self.futures.pop(response.get("request_id"), None)
            if future is None and isinstance(response.get("response"), dict) and isinstance(response["response"].get("models"), list):
                self.models = [m for m in response["response"]["models"] if isinstance(m, dict)]
                self.ready = True
            if future and not future.done():
                if response.get("subtype") == "error":
                    future.set_exception(TmuxError(409, str(response.get("error") or "Claude refused the request")[:200]))
                else:
                    future.set_result(response.get("response") or {})
        elif kind == "control_request":
            request = message.get("request") or {}
            if request.get("subtype") == "can_use_tool" and isinstance(message.get("request_id"), str):
                self.requests[message["request_id"]] = request
                self.asked = []
                self.emit({"type": "agent_state", "sid": self.session_id})
        elif kind == "system":
            sub = message.get("subtype")
            if sub == "init":
                model = message.get("model")
                if isinstance(model, str):
                    self.current_model = model
                if isinstance(message.get("session_id"), str):
                    self.session_id = message["session_id"]
                self.ready = True
            elif sub == "status":
                status = message.get("status")
                self.compacting = status == "compacting"
                if status in ("requesting", "compacting"):
                    self.busy = True
                    # Claude writes the user's message to the history just before it asks the model: tell the page to look.
                    self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": self.turn_id})
            elif sub == "compact_boundary":
                self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": self.turn_id})
        elif kind == "stream_event":
            self.apply_stream(message.get("event") or {})
        elif kind == "assistant":
            model = (message.get("message") or {}).get("model")
            if isinstance(model, str) and model != "<synthetic>":
                self.current_model = model
            self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": self.turn_id})
        elif kind == "user":
            self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": self.turn_id})
        elif kind == "result":
            self.busy = False
            self.compacting = False
            self.blocks.clear()
            self.last_activity = time.time()
            self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": self.turn_id})
            self.emit({"type": "agent_state", "sid": self.session_id})
        elif kind == "rate_limit_event":
            self.hub.note_rate(message.get("rate_limit_info"))

    def apply_stream(self, event):
        what = event.get("type")
        if what == "message_start":
            self.busy = True
            self.blocks.clear()
            self.part += 1
        elif what == "content_block_delta":
            delta = event.get("delta") or {}
            index = event.get("index")
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and isinstance(index, int):
                self.blocks[index] = self.blocks.get(index, "") + delta["text"]
                text = "\n\n".join(self.blocks[i] for i in sorted(self.blocks))
                self.preview = {"type": "terminal_preview", "sid": self.session_id, "turn_id": self.turn_id, "stream_id": self.stream,
                                "part_id": f"part-{self.part}", "split": False, "text": text, "cols": 400, "source": "agent"}
                self.preview_version += 1
                self.wake()

    def ended(self, code):
        self.alive = False
        self.code = code
        self.busy = False
        self.compacting = False
        self.requests.clear()
        for future in self.futures.values():
            if not future.done():
                future.set_exception(TmuxError(409, "Claude has stopped"))
        self.futures.clear()
        self.emit({"type": "agent_state", "sid": self.session_id})

    # --- listeners ------------------------------------------------------------------------------------------------------------
    def emit(self, event):
        for sub in list(self.subs):
            sub.push(event)

    def wake(self):
        for sub in list(self.subs):
            sub.wake.set()

    def subscribe(self) -> Sub:
        sub = Sub()
        self.subs.add(sub)
        return sub

    def unsubscribe(self, sub):
        self.subs.discard(sub)

    # --- state seen by the HTTP layer -----------------------------------------------------------------------------------------
    @property
    def working(self):
        return self.alive and (self.busy or self.compacting or bool(self.requests))

    def status(self):
        if not self.alive:
            return "stopped"
        if self.requests:
            return "running"
        if self.compacting or self.busy:
            return "thinking"
        return "idle"

    def item(self):
        return {"name": self.name, "path": self.cwd, "activity": int(self.last_activity), "attached": 0,
                "created": int(self.meta.get("started") or self.last_activity), "login": self.meta.get("login", ""),
                "started": float(self.meta.get("started") or self.last_activity)}

    def option(self, key):
        table = {"@resume_sid": "sid", "@portal_title": "title", "@portal_model": "model", "@portal_effort": "effort",
                 "@portal_model_state": "model_state", "@portal_user": "owner", "@portal_sender": "sender", "@portal_login": "login",
                 "@portal_last_used": "last_used", "@portal_started_at": "started"}
        value = self.meta.get(table.get(key, ""))
        return None if value in (None, "") else str(value)

    # --- talking to the process -----------------------------------------------------------------------------------------------
    async def send_line(self, payload):
        if not self.alive:
            raise TmuxError(409, "Claude has stopped; wake the chat")
        reply = await self.hub.call({"op": "write", "name": self.name, "line": json.dumps(payload, ensure_ascii=False)})
        if not reply.get("ok"):
            raise TmuxError(409, "Could not reach Claude: " + str(reply.get("error")))

    async def request(self, subtype, timeout=CALL_TIMEOUT, **fields):
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.futures[rid] = future
        try:
            await self.send_line({"type": "control_request", "request_id": rid, "request": {"subtype": subtype, **fields}})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise TmuxError(504, "Claude did not answer in time")
        finally:
            self.futures.pop(rid, None)

    async def say(self, text):
        if self.requests:
            raise TmuxError(409, "Claude is waiting for your answer to a question")
        self.busy = True
        self.last_activity = time.time()
        try:
            await self.send_line({"type": "user", "message": {"role": "user", "content": text}})
        except TmuxError:
            self.busy = False
            raise
        await self.touch()
        # The page confirms delivery by finding the message in the saved history; do not leave that to the first reply.
        # Claude saves it a few seconds later (2.8 s measured, longer on a big context), so keep telling the page for half a minute.
        asyncio.create_task(self.nudge(self.turn_id))

    async def nudge(self, turn):
        await asyncio.sleep(0.4)
        for _ in range(30):
            if self.turn_id != turn or not self.alive:
                return
            self.emit({"type": "transcript_ready", "sid": self.session_id, "turn_id": turn})
            await asyncio.sleep(1.0)

    async def touch(self):
        self.meta["last_used"] = str(time.time())

    async def interrupt(self):
        if self.requests:
            await self.answer_raw(next(iter(self.requests)), allow=False, message="The user stopped this request")
        await self.request("interrupt")

    # --- questions to the user (tool permissions, AskUserQuestion) ------------------------------------------------------------
    def prompt(self):
        if not self.requests:
            return None
        rid = next(iter(self.requests))
        request = self.requests[rid]
        tool = request.get("tool_name") or "tool"
        data = request.get("input") if isinstance(request.get("input"), dict) else {}
        if tool == "AskUserQuestion":
            questions = [q for q in data.get("questions", []) if isinstance(q, dict)]
            index = len(self.asked)
            if index >= len(questions):
                return None
            q = questions[index]
            options = [{"n": n, "label": str(o.get("label", ""))[:200], "hint": str(o.get("description", ""))[:300]}
                       for n, o in enumerate((o for o in q.get("options", []) if isinstance(o, dict)), 1)]
            options.append({"n": len(options) + 1, "label": "Type something else", "hint": "", "free": True})
            note = " (pick one)" if q.get("multiSelect") else ""
            return {"signature": digest(rid + str(index)), "title": str(q.get("header") or "Question")[:80], "detail": "",
                    "question": str(q.get("question", ""))[:600] + note, "options": options, "selected": 1, "permission": False, "multiple": False}
        detail = json.dumps(data, ensure_ascii=False, indent=2)[:INPUT_SHOWN] if data else ""
        for key in ("command", "file_path", "url", "pattern"):
            if isinstance(data.get(key), str):
                detail = data[key][:INPUT_SHOWN] + (("\n\n" + str(request.get("description") or "")) if request.get("description") else "")
                break
        options = [{"n": 1, "label": "Yes", "hint": ""}]
        if request.get("permission_suggestions"):
            options.append({"n": 2, "label": "Yes, and don't ask again for this kind of action in this chat", "hint": ""})
        options.append({"n": len(options) + 1, "label": "No", "hint": ""})
        return {"signature": digest(rid), "title": f"Claude wants to use {request.get('display_name') or tool}", "detail": detail,
                "question": "Do you want to proceed?", "options": options, "selected": 1, "permission": True, "multiple": False}

    async def answer_raw(self, rid, allow, message="", updated=None, permissions=None):
        request = self.requests.get(rid)
        if request is None:
            raise TmuxError(409, "This question is no longer open")
        if allow:
            body = {"behavior": "allow", "updatedInput": updated if updated is not None else request.get("input") or {}}
            if permissions:
                body["updatedPermissions"] = permissions
        else:
            body = {"behavior": "deny", "message": message or "The user declined"}
        await self.send_line({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": body}})
        if allow is False or updated is not None or not self.is_ask(request):
            self.requests.pop(rid, None)
            self.asked = []
            self.emit({"type": "agent_state", "sid": self.session_id})

    @staticmethod
    def is_ask(request):
        return request.get("tool_name") == "AskUserQuestion"

    async def answer(self, signature, option=None, text=None):
        current = self.prompt()
        if current is None or current["signature"] != signature:
            raise TmuxError(409, "This question is no longer open")
        rid = next(iter(self.requests))
        request = self.requests[rid]
        if option is None:
            await self.answer_raw(rid, allow=False, message="The user dismissed the question")
            return
        chosen = next((o for o in current["options"] if o["n"] == option), None)
        if chosen is None or bool(text) != bool(chosen.get("free")):
            raise TmuxError(400, "This answer does not fit the question")
        if self.is_ask(request):
            data = request.get("input") or {}
            questions = [q for q in data.get("questions", []) if isinstance(q, dict)]
            question = questions[len(self.asked)]
            self.asked.append((question.get("question", ""), (text or chosen["label"]).strip()))
            if len(self.asked) < len(questions):
                self.emit({"type": "agent_state", "sid": self.session_id})
                return
            answers = dict(self.asked)
            await self.answer_raw(rid, allow=True, updated={**data, "answers": answers})
            return
        if option == len(current["options"]):
            await self.answer_raw(rid, allow=False, message="The user denied this action")
        elif option == 2 and len(current["options"]) == 3:
            await self.answer_raw(rid, allow=True, permissions=request.get("permission_suggestions"))
        else:
            await self.answer_raw(rid, allow=True)

    # --- model, effort, compact -----------------------------------------------------------------------------------------------
    def menu(self):
        rows, seen = [], set()
        for entry in self.models:
            value = entry.get("value")
            ident = value if value in ("default", "opusplan") else (entry.get("resolvedModel") or value)
            if not isinstance(ident, str) or ident in seen:
                continue
            seen.add(ident)
            rows.append({"id": ident, "label": entry.get("displayName") or ident, "position": len(rows) + 1, "enabled": True})
        chosen = self.meta.get("model") or self.current_model or "default"
        effort = self.meta.get("effort") or None
        return {"models": rows, "selected": chosen, "checked": chosen, "effort": effort, "total": len(rows), "source": "claude_agent"}

    async def operate(self, model=None, effort=None):
        if self.working:
            raise TmuxError(409, "Claude must be idle")
        if model is not None:
            await self.request("set_model", model=model)
            self.meta["model"] = model
            self.meta["model_state"] = "cli_confirmed"
            self.current_model = model
        if effort is not None:
            await self.request("apply_flag_settings", settings={"effortLevel": effort})
            self.meta["effort"] = effort
        else:
            self.meta["effort"] = self.meta.get("effort", "")
        await self.hub.call({"op": "meta", "name": self.name, "meta": {k: self.meta.get(k, "") for k in ("model", "effort", "model_state")}})
        data = self.menu()
        label = next((r["label"] for r in data["models"] if r["id"] == data["selected"]), data["selected"])
        return {**data, "model": model or data["selected"], "model_state": "cli_confirmed", "cli_model": label}

    async def compact(self, instructions=""):
        if self.working:
            raise TmuxError(409, "Claude must be idle")
        await self.say("/compact" + (" " + instructions if instructions else ""))

    async def load_models(self):
        if self.loading:
            return
        self.loading = True
        try:
            reply = await self.request("initialize")
        except TmuxError as error:
            log.warning("agent initialize failed: %s", error.detail)
            return
        finally:
            self.loading = False
        self.models = [m for m in reply.get("models", []) if isinstance(m, dict)]
        self.ready = True


class Hub:
    def __init__(self):
        self.chats: dict[str, Chat] = {}
        self.reader = None
        self.writer = None
        self.connected = False
        self.waiting: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.lock = asyncio.Lock()
        self.rate: dict[str, dict] = {}
        self.task = None
        self.keeper = None
        self.tried = 0.0

    # --- connection ------------------------------------------------------------------------------------------------------------
    async def ensure(self):
        if self.connected:
            return
        async with self.lock:
            if self.connected:
                return
            # One attempt at a time and not more often than every few seconds: a connection that keeps breaking must not turn
            # every waiting request into a reconnect of its own.
            if time.monotonic() - self.tried < RECONNECT_EVERY:
                raise TmuxError(503, "The agent host is not answering; try again in a few seconds")
            self.tried = time.monotonic()
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(config.AGENT_HOST_SOCKET, limit=LINE_LIMIT), 3)
                hello = json.loads(await asyncio.wait_for(reader.readline(), 3))
            except (OSError, asyncio.TimeoutError, ValueError):
                if writer is not None:
                    writer.close()
                raise TmuxError(503, "The agent host is not running")
            if not isinstance(hello, dict) or hello.get("ev") != "hello" or hello.get("protocol") != 1:
                writer.close()
                raise TmuxError(503, "The agent host speaks another protocol version")
            self.reader, self.writer, self.connected = reader, writer, True
            self.task = asyncio.create_task(self.read(reader, writer))
            if self.keeper is None:
                self.keeper = asyncio.create_task(self.keep())
            # Still under the lock, and through send() rather than call(): a connection lost here ends the attempt instead of starting another.
            reply = await self.send({"op": "list"})
            if not reply.get("ok"):
                return
            procs = [info for info in reply.get("procs", []) if isinstance(info, dict) and isinstance(info.get("name"), str)]
            # A restarted host has lost its processes: chats it no longer lists are gone, not running.
            listed = {info["name"] for info in procs}
            for name in [n for n in self.chats if n not in listed]:
                self.chats.pop(name).ended(None)
            for info in procs:
                if not self.connected:
                    return
                await self.adopt(info)

    async def keep(self):
        while True:
            await asyncio.sleep(5)
            if not self.connected:
                try:
                    await self.ensure()
                except TmuxError:
                    pass

    async def adopt(self, info):
        name = info["name"]
        chat = self.chats.get(name)
        if chat is not None and chat.pid != info.get("pid"):
            chat.ended(None)
            chat = None
        if chat is None:
            chat = Chat(self, name, info.get("cwd") or "", info.get("meta") or {}, info.get("pid"))
            self.chats[name] = chat
            chat.ready = True
        chat.alive = bool(info.get("alive"))
        chat.pid = info.get("pid")
        newest = info.get("seq") if isinstance(info.get("seq"), int) else 0
        reply = await self.send({"op": "attach", "name": name, "after": max(chat.seq, newest - REPLAY_LINES)})
        if reply.get("ok") and chat.alive and not chat.models and not chat.loading:
            asyncio.create_task(chat.load_models())

    async def read(self, reader, writer):
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    future = self.waiting.pop(message["id"], None)
                    if future and not future.done():
                        future.set_result(message)
                    continue
                chat = self.chats.get(message.get("name"))
                if chat is None:
                    continue
                if message.get("ev") == "line":
                    chat.apply(message["seq"], message.get("d", "out"), message.get("line", ""))
                elif message.get("ev") == "exit":
                    chat.ended(message.get("code"))
                elif message.get("ev") == "gap":
                    chat.errors.append("some earlier output was no longer available")
        except (OSError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()
            if self.reader is reader:
                self.connected = False
                for future in self.waiting.values():
                    if not future.done():
                        future.set_result({"ok": False, "error": "host connection lost"})
                self.waiting.clear()

    async def call(self, payload):
        await self.ensure_connected()
        return await self.send(payload)

    async def send(self, payload):
        if not self.connected:
            raise TmuxError(503, "The agent host did not answer")
        self.counter += 1
        rid = self.counter
        future = asyncio.get_running_loop().create_future()
        self.waiting[rid] = future
        try:
            self.writer.write((json.dumps({**payload, "id": rid}, ensure_ascii=False) + "\n").encode())
            await self.writer.drain()
            return await asyncio.wait_for(future, CALL_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            raise TmuxError(503, "The agent host did not answer")
        finally:
            self.waiting.pop(rid, None)

    async def ensure_connected(self):
        if not self.connected:
            await self.ensure()

    # --- usage limits seen in rate_limit_event ---------------------------------------------------------------------------------
    def note_rate(self, info):
        if not isinstance(info, dict):
            return
        kind = info.get("rateLimitType")
        if kind in RATE_KINDS and isinstance(info.get("utilization"), (int, float)):
            self.rate[kind] = {"used_percentage": info["utilization"] * (100 if info["utilization"] <= 1 else 1),
                               "resets_at": info.get("resetsAt"), "seen": time.time()}

    # --- the operations the rest of the portal uses ---------------------------------------------------------------------------
    def visible(self, all_users=False):
        return [c for c in self.chats.values() if all_users or users.owns_session(c.meta.get("owner", ""))]

    async def start(self, name, cwd, argv, env, meta):
        reply = await self.call({"op": "spawn", "name": name, "argv": argv, "cwd": cwd, "env": env, "meta": meta})
        if not reply.get("ok"):
            raise TmuxError(500 if reply.get("error") != "already running" else 409, "Claude could not be started: " + str(reply.get("error")))
        chat = Chat(self, name, cwd, meta, reply.get("pid"))
        chat.ready = False
        self.chats[name] = chat
        await self.call({"op": "attach", "name": name, "after": 0})
        asyncio.create_task(chat.load_models())
        return chat

    def get(self, name):
        return self.chats.get(name)

    async def kill(self, name):
        chat = self.chats.get(name)
        if chat is None:
            raise TmuxError(404, "no such session")
        await self.call({"op": "kill", "name": name})
        deadline = time.monotonic() + 12
        while chat.alive and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        reply = await self.call({"op": "forget", "name": name})
        if not reply.get("ok") and reply.get("error") == "still running":
            raise TmuxError(504, "Claude did not stop in time; try again")
        self.chats.pop(name, None)


hub = Hub()


def has(name):
    return name in hub.chats


async def refresh():
    """Connect to the host when agent mode is on; the tmux code paths keep working when it is down."""
    if config.CHAT_MODE != "agent" and not hub.chats:
        return
    try:
        await hub.ensure()
    except TmuxError:
        pass


def chat_for(name) -> Chat:
    chat = hub.chats.get(name)
    if chat is None or not users.owns_session(chat.meta.get("owner", "")):
        raise TmuxError(404, "no such session")
    return chat


def usage_now():
    """Limits from the newest rate_limit_event of any chat, in the shape usage.py serves."""
    return dict(hub.rate)
