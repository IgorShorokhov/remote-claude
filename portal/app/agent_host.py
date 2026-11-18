"""Process holder for agent-mode chats.

Runs as its own service so that restarting the portal does not stop running chats (the tmux service does the same for the old mode).
It starts Claude Code processes, keeps a numbered log of the lines they print and the lines sent to them, and serves that log to the portal
over a unix socket. It knows nothing about chats: the meaning of each line is the portal's business (agentctl.py).

Protocol: one JSON object per line in both directions.
  portal -> host  {"id", "op": "spawn|write|kill|forget|meta|list|attach", ...}  answered by {"id", "ok": true, ...} or {"id", "ok": false, "error"}
  host -> portal  {"ev": "line", "name", "seq", "d": "out|in|err", "line"}, {"ev": "exit", "name", "code", "seq"}, {"ev": "gap", "name", "from"}
"""
import asyncio
import json
import os
import re
import signal
import sys
import time
from collections import deque

PROTOCOL = 1
SOCKET = os.environ.get("AGENT_HOST_SOCKET", "/run/agent-host/host.sock")
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,31}")
RING_BYTES = 8 * 1024 * 1024
LINE_LIMIT = 1 << 26
DEAD_KEEP = 3600
KILL_GRACE = 6.0
QUEUE = 65536
ENV_KEYS = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class Proc:
    def __init__(self, name, argv, cwd, meta):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.meta = meta
        self.seq = 0
        self.ring = deque()
        self.size = 0
        self.proc = None
        self.code = None
        self.ended = 0.0
        self.attached = set()
        self.started = time.time()

    def log(self, d, line):
        self.seq += 1
        entry = (self.seq, d, line)
        self.ring.append(entry)
        self.size += len(line) + 32
        while self.size > RING_BYTES and len(self.ring) > 1:
            old = self.ring.popleft()
            self.size -= len(old[2]) + 32
        message = {"ev": "line", "name": self.name, "seq": entry[0], "d": d, "line": line}
        for conn in list(self.attached):
            conn.push(message)
        return entry[0]

    def summary(self):
        return {"name": self.name, "pid": self.proc.pid if self.proc else None, "alive": self.code is None, "code": self.code,
                "seq": self.seq, "first": self.ring[0][0] if self.ring else self.seq + 1, "meta": self.meta,
                "cwd": self.cwd, "started": self.started}


class Conn:
    def __init__(self, host, reader, writer):
        self.host = host
        self.reader = reader
        self.writer = writer
        self.queue = asyncio.Queue(QUEUE)
        self.names = set()
        self.closed = False

    def push(self, message):
        if self.closed:
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for name in self.names:
            proc = self.host.procs.get(name)
            if proc:
                proc.attached.discard(self)
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self.writer.close()

    async def pump(self):
        try:
            while True:
                message = await self.queue.get()
                if message is None:
                    return
                self.writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await self.writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            # Nothing reads the queue from here on: mark the connection closed and free anyone waiting for room in it.
            self.close()
            while not self.queue.empty():
                self.queue.get_nowait()


class Host:
    def __init__(self):
        self.procs: dict[str, Proc] = {}

    async def serve(self, path=SOCKET):
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self.client, path=path, limit=LINE_LIMIT)
        os.chmod(path, 0o600)
        asyncio.create_task(self.sweep())
        return server

    async def sweep(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for name, proc in list(self.procs.items()):
                if proc.code is not None and not proc.attached and now - proc.ended > DEAD_KEEP:
                    del self.procs[name]

    async def client(self, reader, writer):
        conn = Conn(self, reader, writer)
        pump = asyncio.create_task(conn.pump())
        conn.push({"ev": "hello", "protocol": PROTOCOL, "pid": os.getpid()})
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError("not an object")
                except ValueError:
                    conn.push({"ok": False, "error": "bad json"})
                    continue
                try:
                    reply = await self.handle(conn, request)
                except Exception as error:
                    reply = {"ok": False, "error": str(error) or type(error).__name__}
                if "id" in request:
                    reply["id"] = request["id"]
                conn.push(reply)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            conn.close()
            pump.cancel()

    async def handle(self, conn, request):
        op = request.get("op")
        name = request.get("name")
        if op == "list":
            return {"ok": True, "procs": [p.summary() for p in self.procs.values()]}
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            return {"ok": False, "error": "bad name"}
        if op == "spawn":
            return await self.spawn(name, request)
        proc = self.procs.get(name)
        if proc is None:
            return {"ok": False, "error": "no such process"}
        if op == "attach":
            after = request.get("after", 0)
            if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                return {"ok": False, "error": "bad after"}
            first = proc.ring[0][0] if proc.ring else proc.seq + 1
            if after + 1 < first:
                conn.push({"ev": "gap", "name": name, "from": first})
                after = first - 1
            # The log can hold far more lines than the queue: wait for room instead of overflowing it (an overflow drops the connection).
            # New lines may arrive while waiting, so go round until nothing is left, then join the live listeners without a pause between.
            while not conn.closed:
                batch = [entry for entry in proc.ring if entry[0] > after]
                if not batch:
                    break
                for seq, d, line in batch:
                    await conn.queue.put({"ev": "line", "name": name, "seq": seq, "d": d, "line": line})
                    after = seq
                    if conn.closed:
                        break
            if conn.closed:
                return {"ok": False, "error": "connection closed"}
            if proc.code is not None:
                conn.push({"ev": "exit", "name": name, "code": proc.code, "seq": proc.seq})
            proc.attached.add(conn)
            conn.names.add(name)
            return {"ok": True, "seq": proc.seq}
        if op == "write":
            line = request.get("line")
            if not isinstance(line, str) or "\n" in line or proc.code is not None or proc.proc.stdin is None:
                return {"ok": False, "error": "cannot write"}
            proc.proc.stdin.write(line.encode() + b"\n")
            await proc.proc.stdin.drain()
            return {"ok": True, "seq": proc.log("in", line)}
        if op == "meta":
            meta = request.get("meta")
            if not isinstance(meta, dict):
                return {"ok": False, "error": "bad meta"}
            proc.meta.update(meta)
            return {"ok": True}
        if op == "kill":
            asyncio.create_task(self.stop(proc))
            return {"ok": True}
        if op == "forget":
            if proc.code is None:
                return {"ok": False, "error": "still running"}
            del self.procs[name]
            return {"ok": True}
        return {"ok": False, "error": "unknown op"}

    async def spawn(self, name, request):
        old = self.procs.get(name)
        if old and old.code is None:
            return {"ok": False, "error": "already running"}
        argv, cwd, meta = request.get("argv"), request.get("cwd"), request.get("meta") or {}
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) and os.path.basename(argv[0]) == "claude"):
            return {"ok": False, "error": "only claude can be started"}
        if not isinstance(cwd, str) or not os.path.isdir(cwd) or not isinstance(meta, dict):
            return {"ok": False, "error": "bad cwd or meta"}
        env = dict(os.environ)
        for key, value in (request.get("env") or {}).items():
            if isinstance(key, str) and isinstance(value, str) and ENV_KEYS.fullmatch(key):
                env[key] = value
        proc = Proc(name, argv, cwd, meta)
        proc.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT, start_new_session=True)
        self.procs[name] = proc
        asyncio.create_task(self.read(proc, proc.proc.stdout, "out"))
        asyncio.create_task(self.read(proc, proc.proc.stderr, "err"))
        asyncio.create_task(self.wait(proc))
        return {"ok": True, "pid": proc.proc.pid}

    async def read(self, proc, stream, d):
        while True:
            try:
                raw = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # readline has already thrown the oversized chunk away; reading more here would eat the next good line
                proc.log("err", "[line too long, dropped]")
                continue
            if not raw:
                return
            proc.log(d, raw.decode("utf-8", "replace").rstrip("\r\n"))

    async def wait(self, proc):
        code = await proc.proc.wait()
        await asyncio.sleep(0.2)
        proc.code = code
        proc.ended = time.time()
        seq = proc.log("err", f"[process exited with code {code}]")
        for conn in list(proc.attached):
            conn.push({"ev": "exit", "name": proc.name, "code": code, "seq": seq})

    async def stop(self, proc):
        if proc.code is not None:
            return
        for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(proc.proc.pid, sig)
            except OSError:
                return
            try:
                await asyncio.wait_for(proc.proc.wait(), wait)
                return
            except asyncio.TimeoutError:
                continue


async def main():
    host = Host()
    server = await host.serve()
    if os.environ.get("NOTIFY_SOCKET"):
        os.system("systemd-notify --ready 2>/dev/null")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()
    await asyncio.gather(*(host.stop(proc) for proc in list(host.procs.values())), return_exceptions=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
