"""Claude drives a person's own Chrome and their own computer.

Three parts meet here:
1. a device keeps one WebSocket open to /ext/bridge and runs the commands it receives: the Chrome extension (browser-extension/, kind "browser")
   or the computer agent (pc-agent/, kind "pc": shell and files);
2. this module keeps the list of connected devices (the hub) and matches each command to its answer;
3. browser_mcp.py and pc_mcp.py, started by Claude Code on this server, send commands over a private Unix socket.

The WebSocket has its own door, like the VS Code sign-in: a device proves itself with a long token (kind "browser" or "pc", approved by its
account on /ext/authorize) sent in its first message, because a Chrome extension or a background program cannot carry the portal's Google
cookie. A token belongs to one account and a command only reaches the devices of the account that owns the chat. A device token cannot be
traded for a portal session.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from . import ext, users

BROWSER_TOOLS = frozenset({
    "tabs_list", "tab_open", "tab_close", "tab_focus", "tab_navigate", "tab_allow",
    "page_snapshot", "page_screenshot", "mouse_move", "click", "type", "key", "scroll", "select_option", "drag",
    "eval", "wait", "file_upload",
})
PC_TOOLS = frozenset({"pc_info", "pc_run", "pc_read", "pc_write", "pc_edit", "pc_list",
                      "cu_screenshot", "cu_zoom", "cu_click", "cu_move", "cu_drag", "cu_scroll", "cu_type", "cu_key", "cu_cursor"})
TOOLS = BROWSER_TOOLS | PC_TOOLS
KINDS = ("browser", "pc")
NO_DEVICE = {
    "browser": "No browser is connected. Open Chrome with the Claude Browser extension and make sure it is signed in.",
    "pc": "No computer is connected. Install the Claude agent on it (Computers tab of the portal) and make sure it is online.",
}
HELLO_TIMEOUT = 10
MAX_GREETING = 16  # sockets that have connected but not proved themselves yet
IDLE_TIMEOUT = 75
MAX_DEVICES = 6  # per account
MAX_TOTAL = 200
STATE_KEYS = ("enabled", "scope", "tabs", "controlled", "paused", "screen", "shell", "update")
MAX_PARTS = 2000  # a reply arrives in pieces: the server limits one WebSocket message to 64 KB
MAX_REPLY = 24 * 1024 * 1024
CALL_TIMEOUT = 60
LOCAL_LIMIT = 48 * 1024 * 1024
DEFAULT_SOCKET = "/run/agent-portal/browser.sock"
AGENT_FILE = Path(__file__).with_name("pc_agent") / "portal_agent.py"


def version_tuple(text) -> tuple:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return ()


def agent_update(version: str) -> dict | None:
    """The current agent for a computer that reported an older one; the computer checks it and installs it unless its user turned updates off."""
    try:
        source = AGENT_FILE.read_bytes()
    except OSError:
        return None
    match = re.search(rb'^VERSION = "([0-9.]+)"', source, re.M)
    if match is None or not version_tuple(version) or version_tuple(version) >= version_tuple(match.group(1).decode()):
        return None
    return {"type": "update", "version": match.group(1).decode(), "sha256": hashlib.sha256(source).hexdigest(),
            "source": base64.b64encode(source).decode()}


def socket_path() -> str:
    return os.environ.get("PORTAL_BROWSER_SOCK", DEFAULT_SOCKET)


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def kind_of(tool: str) -> str:
    return "pc" if tool in PC_TOOLS else "browser"


class Device:
    def __init__(self, ws: WebSocket, tid: str, email: str, uid: str, kind: str, label: str, info: dict):
        self.ws = ws
        self.tid = tid
        self.email = email
        self.uid = uid
        self.kind = kind
        self.label = label
        self.info = info
        self.state: dict = {}
        self.connected = time.time()
        self.seen = self.connected
        self.pending: dict[str, asyncio.Future] = {}
        self.parts: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self.lock:
            await self.ws.send_json(message)

    def public(self) -> dict:
        return {"id": self.tid, "kind": self.kind, "label": self.label, "connected_at": self.connected, "seen_at": self.seen,
                "version": self.info.get("version"), "os": self.info.get("os"), "host": self.info.get("host"),
                "screen_control": bool(self.info.get("screen")), "state": self.state}

    def fail_all(self, error: BridgeError) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.pending.clear()


class Hub:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.greeting = 0

    def mine(self, uid: str, kind: str | None = None) -> list[Device]:
        return [d for d in self.devices.values() if d.uid == uid and (kind is None or d.kind == kind)]

    def pick(self, uid: str, kind: str, wanted: str | None = None) -> Device:
        found = self.mine(uid, kind)
        if not found:
            raise BridgeError("no_device", NO_DEVICE[kind])
        if wanted:
            for device in found:
                if wanted in (device.tid, device.label):
                    return device
            raise BridgeError("no_device", f"No connected {'browser' if kind == 'browser' else 'computer'} matches {wanted!r}.")
        return max(found, key=lambda d: d.seen)

    def status(self, uid: str, kind: str | None = None) -> list[dict]:
        return [d.public() for d in self.mine(uid, kind)]

    def drop_token(self, tid: str) -> None:
        device = self.devices.get(tid)
        if device is not None:
            self.drop(device)
            asyncio.ensure_future(_close(device.ws, 1008))

    async def call(self, tool: str, args: dict | None = None, timeout: float = CALL_TIMEOUT, device: str | None = None, uid: str | None = None):
        if tool not in TOOLS:
            raise BridgeError("bad_tool", f"Unknown device tool {tool!r}.")
        target = self.pick(uid or users.primary_id(), kind_of(tool), device)
        cid = secrets.token_hex(6)
        future = asyncio.get_running_loop().create_future()
        target.pending[cid] = future
        try:
            try:
                await target.send({"type": "cmd", "id": cid, "tool": tool, "args": args or {}})
            except Exception:
                raise BridgeError("disconnected", "The device disconnected before the command was sent.")
            try:
                reply = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise BridgeError("timeout", f"The device did not answer {tool} within {int(timeout)} seconds.")
        finally:
            target.pending.pop(cid, None)
            target.parts.pop(cid, None)
        if not reply.get("ok"):
            raise BridgeError("device_error", str(reply.get("error") or "The device refused the command."))
        return reply.get("result")

    def add(self, device: Device) -> None:
        old = self.devices.get(device.tid)
        self.devices[device.tid] = device
        if old is not None:
            old.fail_all(BridgeError("disconnected", "The device reconnected."))
            asyncio.ensure_future(_close(old.ws, 1000))

    def drop(self, device: Device) -> None:
        if self.devices.get(device.tid) is device:
            del self.devices[device.tid]
        device.fail_all(BridgeError("disconnected", "The device disconnected."))


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code)


async def serve_extension(ws: WebSocket, hub: Hub, roles, accounts=None) -> None:
    await ws.accept()

    async def refuse(code: str) -> None:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "code": code})
        await _close(ws, 1008)

    if hub.greeting >= MAX_GREETING:
        await _close(ws, 1013)
        return
    hub.greeting += 1
    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
        await _close(ws, 1008)
        return
    finally:
        hub.greeting -= 1
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await refuse("protocol")
        return
    found = ext.tokens.find(hello.get("token"))
    uid = accounts.user_for(found[1]["email"]) if found is not None and accounts is not None else None
    if (found is None or found[1].get("kind") not in KINDS or roles is None or roles.role_for(found[1]["email"]) not in ("admin", "user")
            or uid is None):
        await refuse("auth")
        return
    tid, row = found
    if tid not in hub.devices and (len(hub.devices) >= MAX_TOTAL or len(hub.mine(uid)) >= MAX_DEVICES):
        await refuse("busy")
        return
    ext.tokens.touch(tid)
    info = hello.get("info") if isinstance(hello.get("info"), dict) else {}
    kind = row["kind"]
    label = str(row.get("label") or ("Chrome" if kind == "browser" else "Computer"))[:60]
    device = Device(ws, tid, row["email"], uid, kind, label,
                    {"version": str(info.get("version", ""))[:20], "os": str(info.get("os", ""))[:40], "host": str(info.get("host", ""))[:60],
                     "screen": info.get("screen") is True})
    hub.add(device)
    try:
        await device.send({"type": "hello_ok", "id": tid, "kind": kind, "label": device.label, "email": device.email})
        update = agent_update(device.info["version"]) if kind == "pc" else None
        if update is not None:
            await device.send(update)
        while True:
            try:
                message = await asyncio.wait_for(ws.receive_json(), IDLE_TIMEOUT)
            except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
                break
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind in ("res", "res_part") and not isinstance(message.get("id"), str):
                continue
            device.seen = time.time()
            if kind == "ping":
                if ext.tokens.row(tid) is None or roles.role_for(device.email) not in ("admin", "user"):
                    await refuse("auth")
                    break
                await device.send({"type": "pong"})
            elif kind == "state" and isinstance(message.get("state"), dict):
                device.state = {k: message["state"][k] for k in STATE_KEYS if k in message["state"]}
            elif kind == "res":
                future = device.pending.get(message.get("id"))
                if future is not None and not future.done():
                    future.set_result(message)
            elif kind == "res_part":
                whole = take_part(device, message)
                future = device.pending.get(message.get("id"))
                if whole is not None and future is not None and not future.done():
                    future.set_result(whole)
    finally:
        hub.drop(device)
        await _close(ws, 1000)


def take_part(device: Device, message: dict):
    """Collect the pieces of one reply; returns the reply when the last piece arrives."""
    cid, i, n, data = message.get("id"), message.get("i"), message.get("n"), message.get("data")
    if cid not in device.pending or not isinstance(data, str) or type(i) is not int or type(n) is not int or not 0 <= i < n <= MAX_PARTS:
        return None
    buf = device.parts.setdefault(cid, {"n": n, "got": {}, "size": 0})
    if buf["n"] != n or i in buf["got"]:
        device.parts.pop(cid, None)
        return None
    buf["got"][i] = data
    buf["size"] += len(data)
    if buf["size"] > MAX_REPLY:
        device.parts.pop(cid, None)
        return None
    if len(buf["got"]) < n:
        return None
    del device.parts[cid]
    try:
        whole = json.loads("".join(buf["got"][k] for k in range(n)))
    except ValueError:
        return None
    return whole if isinstance(whole, dict) and whole.get("type") == "res" and whole.get("id") == cid else None


async def handle_local(hub: Hub, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()

    async def reply(payload: dict) -> None:
        async with lock:
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()

    async def run(request: dict) -> None:
        rid = request.get("id")
        try:
            op = request.get("op")
            # A chat's MCP server names its account in PORTAL_DEVICE_USER. Chats started before this variable existed carry none: they are the
            # owner's (only the owner's chats ever had these tools), so a missing name means the primary account. All chats run as one Linux
            # user, so the name is a routing key and not a lock: separate Linux users per account would be the real wall.
            uid = request.get("uid")
            uid = uid if isinstance(uid, str) and users.ID_RE.fullmatch(uid) else users.primary_id()
            if op == "status":
                kind = request.get("kind") if request.get("kind") in KINDS else None
                await reply({"id": rid, "ok": True, "result": {"devices": hub.status(uid, kind)}})
            elif op == "call":
                timeout = min(float(request.get("timeout") or CALL_TIMEOUT), 120.0)
                result = await hub.call(str(request.get("tool")), request.get("args") if isinstance(request.get("args"), dict) else {},
                                        timeout, request.get("device"), uid)
                await reply({"id": rid, "ok": True, "result": result})
            else:
                await reply({"id": rid, "ok": False, "code": "bad_op", "error": "Unknown operation."})
        except BridgeError as e:
            await reply({"id": rid, "ok": False, "code": e.code, "error": e.message})
        except Exception as e:  # keep the socket usable whatever one request does
            with contextlib.suppress(Exception):
                await reply({"id": rid, "ok": False, "code": "internal", "error": f"{type(e).__name__}: {e}"})

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if isinstance(request, dict):
                task = asyncio.create_task(run(request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except (ConnectionError, asyncio.LimitOverrunError, ValueError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            writer.close()


def socket_alive(target: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1)
    try:
        probe.connect(str(target))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def start_local(hub: Hub, path: str | None = None):
    """A second portal process (a test server started with the default path) must not take over or delete the socket of the running one:
    on 16 Nov 2026 a browser test removed it and Claude lost every computer until a restart."""
    target = Path(path or socket_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and socket_alive(target):
        raise OSError(f"another portal already serves {target}")
    target.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(lambda r, w: handle_local(hub, r, w), path=str(target), limit=LOCAL_LIMIT)
    os.chmod(target, 0o600)
    server.portal_inode = target.stat().st_ino
    return server


@contextlib.asynccontextmanager
async def lifespan(app):
    hub = app.state.browser
    try:
        server = await start_local(hub)
    except OSError:
        server = None
    try:
        yield
    finally:
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            target = Path(socket_path())
            with contextlib.suppress(OSError):
                if target.stat().st_ino == server.portal_inode:
                    target.unlink()


def build_router() -> APIRouter:
    r = APIRouter()

    @r.websocket("/ext/bridge")
    async def bridge(ws: WebSocket):
        await serve_extension(ws, ws.app.state.browser, getattr(ws.app.state, "roles", None), getattr(ws.app.state, "users", None))

    @r.get("/api/browser/status")
    async def status(request: Request):
        return {"devices": request.app.state.browser.status(request.scope["portal"]["user"], "browser")}

    return r
