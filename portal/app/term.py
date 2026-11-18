import asyncio
import fcntl
import json
import os
import signal
import struct
import subprocess
import sys
import termios

from fastapi import WebSocket, WebSocketDisconnect

from . import agent_ws, agentctl, chat_runtime, cli_controls, config, identity, ownership, tmuxctl, transcript
from .code_events import OutputParser
from .cli_preview import CliPreview
from .history import UUID_RE, submission

MAX_FRAME = 16384
_active = 0


def _winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


CTTY_HELPER = (
    "import fcntl,os,sys,termios;"
    "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
    "os.execv(sys.argv[1],sys.argv[1:])"
)


def _parse_resize(text: str):
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "resize":
        return None
    c, r = d.get("cols"), d.get("rows")
    if not (isinstance(c, int) and isinstance(r, int)) or isinstance(c, bool) or isinstance(r, bool):
        return None
    if not (10 <= c <= 500 and 10 <= r <= 500):
        return None
    return c, r


def _parse_paste(text: str) -> bytes | None:
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("type") != "paste":
        return None
    value = message.get("text")
    if set(message) != {"type", "text"} or not isinstance(value, str):
        raise ValueError("invalid paste")
    value = identity.neutralize(value.replace("\r\n", "\n").replace("\r", "\n"))
    if any((ord(ch) < 32 and ch not in "\n\t") or 127 <= ord(ch) <= 159 for ch in value):
        raise ValueError("control character in paste")
    data = value.encode("utf-8")
    if len(data) > MAX_FRAME - 12:
        raise ValueError("paste too large")
    return b"\x1b[200~" + data + b"\x1b[201~"


async def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except OSError:
            pass
        for _ in range(20):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    await asyncio.to_thread(proc.wait)


async def bridge(ws: WebSocket, name: str) -> None:
    global _active
    if not tmuxctl.NAME_ANY_RE.fullmatch(name):
        await ws.close(code=1008)
        return
    if agentctl.has(name):
        await agent_ws.bridge(ws, name)
        return
    try:
        items = await tmuxctl.require_server()
    except tmuxctl.TmuxError:
        await ws.close(code=1011)
        return
    if not any(i["name"] == name for i in items):
        await ws.close(code=1008)
        return
    if _active >= config.MAX_TERM_CLIENTS:
        await ws.close(code=1013)
        return
    _active += 1
    master = slave = -1
    proc = None
    loop = asyncio.get_running_loop()
    try:
        master, slave = os.openpty()
        _winsize(master, 80, 24)
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", CTTY_HELPER, *tmuxctl.base_cmd(), "attach-session", "-t", f"={name}"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=tmuxctl.child_env(),
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        state = {"reading": False}

        def on_readable():
            if q.qsize() >= 60:
                loop.remove_reader(master)
                state["reading"] = False
                return
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(master)
                state["reading"] = False
                q.put_nowait(None)
                return
            q.put_nowait(data)
            if q.qsize() >= 32:
                loop.remove_reader(master)
                state["reading"] = False

        def start_reading():
            if not state["reading"]:
                loop.add_reader(master, on_readable)
                state["reading"] = True

        start_reading()

        parser = OutputParser(cols=80, rows=24) if ws.query_params.get("events") == "1" else None

        preview = None
        pending_preview = None
        if ws.query_params.get("preview") == "1":
            sid = await tmuxctl.option(name, '@resume_sid')
            item = next(item for item in items if item['name'] == name)
            if sid and UUID_RE.fullmatch(sid) and await asyncio.to_thread(ownership.owns, sid, item['path']):
                preview = CliPreview(sid, root=config.HISTORY_DIR)

        async def send_preview():
            if preview:
                await asyncio.to_thread(preview.reader.refresh)
                for event in preview.flush(refresh=False):
                    await ws.send_json({"v": 1, "type": "code_events", "events": [event]})

        async def send_events(events):
            if events:
                await ws.send_json({"v": 1, "type": "code_events", "events": events})

        async def pump_out():
            last_flush = loop.time()
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), 0.15) if parser or preview else await q.get()
                except asyncio.TimeoutError:
                    if parser is not None:
                        await send_events(parser.flush())
                    await send_preview()
                    continue
                if data is None:
                    if parser:
                        await send_events(parser.flush())
                    await send_preview()
                    return
                await ws.send_bytes(data)
                if parser:
                    await send_events(parser.feed(data))
                    if loop.time() - last_flush >= 0.5:
                        await send_events(parser.flush())
                        last_flush = loop.time()
                if preview:
                    preview.feed(data)
                    if loop.time() - last_flush >= .08:
                        await send_preview()
                        last_flush = loop.time()
                if not state["reading"] and q.qsize() < 8:
                    start_reading()

        async def pump_in():
            nonlocal pending_preview
            while True:
                msg = await ws.receive()
                if not ws.scope.get('portal_valid', lambda: True)():
                    return
                if msg["type"] == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                text = msg.get("text")
                if text is not None:
                    try:
                        if len(text.encode("utf-8")) > MAX_FRAME:
                            return
                        data = _parse_paste(text)
                        if preview and data is not None:
                            pending_preview = submission(json.loads(text)["text"])[1]
                    except (ValueError, UnicodeError):
                        return
                    size = _parse_resize(text) if len(text) < 200 else None
                    if size:
                        try:
                            _winsize(master, *size)
                            if parser:
                                parser.screen.resize(*size)
                            if preview:
                                preview.resize(*size)
                        except OSError:
                            return
                if data is not None:
                    if cli_controls.busy(name):
                        await ws.close(code=1013)
                        return
                    if len(data) > MAX_FRAME:
                        return
                    async with tmuxctl._lock:
                        await tmuxctl.require_session(name)
                        await chat_runtime.touch(name)
                        view = memoryview(data)
                        tries = 0
                        while view:
                            try:
                                n = os.write(master, view)
                                view = view[n:]
                            except BlockingIOError:
                                tries += 1
                                if tries > 200:
                                    return
                                await asyncio.sleep(0.01)
                            except OSError:
                                return
                    if preview and pending_preview and data in (b"\r", b"\n"):
                        preview.arm(pending_preview); pending_preview = None
                    if data == b"\x03":
                        pending_preview = None
                    if parser and data == b"\x03":
                        await send_events([{"v": 1, "type": "control", "source": "terminal_bridge",
                                            "signal": "ctrl_c", "status": "written"}])
                    continue

        async def validate_connection():
            while ws.scope.get('portal_valid', lambda: True)():
                await asyncio.sleep(2)

        t_guard = asyncio.create_task(validate_connection())
        t_out = asyncio.create_task(pump_out())
        t_in = asyncio.create_task(pump_in())
        try:
            await asyncio.wait({t_out, t_in, t_guard}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t_out, t_in, t_guard):
                t.cancel()
            await asyncio.gather(t_out, t_in, t_guard, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        _active -= 1
        if master >= 0:
            try:
                loop.remove_reader(master)
            except Exception:
                pass
        if proc is not None:
            await _stop(proc)
        for fd in (master, slave):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            await ws.close()
        except Exception:
            pass
