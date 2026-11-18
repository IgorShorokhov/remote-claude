#!/usr/bin/env python3
"""Claude Portal agent: lets Claude, through your portal, run commands and edit files on this computer.

One file, standard library only (Python 3.9 or newer), the same on Windows, Linux and macOS.
The agent dials out to the portal over HTTPS and keeps one WebSocket open; nothing listens on this computer. Commands arrive only from your
portal, for the account that approved this computer, and every one is written to activity.log next to the settings (file contents are not).

    portal_agent.py setup --portal https://HOST [--label NAME]   pair with the portal, start at every sign-in, start now
    portal_agent.py run                                          keep the connection open (what the autostart runs)
    portal_agent.py settings                                     open the settings window (starts the agent if needed)
    portal_agent.py pause | resume                               stop or allow Claude here without disconnecting
    portal_agent.py status                                       connection, pause state and the last log lines
    portal_agent.py uninstall                                    disconnect from the portal and remove the autostart
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import platform
import random
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import datetime
from pathlib import Path

VERSION = "0.3.5"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
CHUNK = 12000  # characters per reply piece: the portal refuses WebSocket messages over 64 KB
MAX_MESSAGE = 64 * 1024 * 1024
PING_EVERY = 20.0
DEAD_AFTER = 50.0
MAX_ACTIVE = 12
HEAD = 100_000
TAIL = 100_000
READ_DEFAULT = 200_000
READ_MAX = 6 * 1024 * 1024
WRITE_MAX = 16 * 1024 * 1024
LIST_MAX = 1000
COMMAND_MAX = 8000
LOG_MAX = 1_000_000


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- settings

def data_dir() -> Path:
    override = os.environ.get("CLAUDE_PORTAL_AGENT_HOME")
    if override:
        return Path(override)
    if WIN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ClaudePortal"
    if MAC:
        return Path.home() / "Library" / "Application Support" / "ClaudePortal"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-portal"


def private_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not WIN:
        os.chmod(path, 0o700)
    return path


def read_config() -> dict:
    try:
        value = json.loads((data_dir() / "config.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(value: dict) -> None:
    path = private_dir() / "config.json"
    temp = path.with_name("config.json.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
    os.replace(temp, path)


def paused() -> bool:
    return (data_dir() / "paused").exists()


def log(message: str) -> None:
    line = f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        path = private_dir() / "activity.log"
        if path.exists() and path.stat().st_size > LOG_MAX:
            os.replace(path, path.with_name("activity.log.1"))
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(line, flush=True)


def set_status(state: str, detail: str = "") -> None:
    try:
        (private_dir() / "status.json").write_text(json.dumps({"state": state, "detail": detail, "at": time.time(), "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def portal_origin(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not re.match(r"^[a-z]+://", text, re.I):
        text = "https://" + text
    url = urllib.parse.urlsplit(text)
    local = url.hostname in ("127.0.0.1", "localhost", "::1")
    if not url.hostname or url.scheme not in ("https", "http") or (url.scheme == "http" and not local):
        raise SystemExit("The portal address must start with https://")
    return f"{url.scheme}://{url.netloc}"


# ---------------------------------------------------------------- WebSocket client (RFC 6455)

class Closed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 20.0):
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme == "wss"
        raw = socket.create_connection((parts.hostname, parts.port or (443 if secure else 80)), timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buffer = bytearray()
        self.send_lock = threading.Lock()
        self.closed = False
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname) if secure else raw
            self._handshake(parts, timeout)
        except BaseException:
            raw.close()
            raise

    def _handshake(self, parts, timeout: float) -> None:
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        self.sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: claude-portal-agent/{VERSION}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk or len(head) > 65536:
                raise Closed("The portal closed the connection during the handshake.")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        wanted = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if lines[0].split(" ")[1:2] != ["101"] or headers.get("sec-websocket-accept") != wanted:
            raise Closed("The portal did not accept the WebSocket: " + lines[0][:80])
        self.buffer.extend(rest)

    def _need(self, count: int, wait: float | None) -> bool:
        while len(self.buffer) < count:
            if wait is not None and not self.buffer:
                if not select.select([self.sock], [], [], wait)[0] and not (hasattr(self.sock, "pending") and self.sock.pending()):
                    return False
                wait = None
            self.sock.settimeout(30.0)
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                raise Closed("The connection stalled in the middle of a message.")
            if not chunk:
                raise Closed("The portal closed the connection.")
            self.buffer.extend(chunk)
        return True

    def _take(self, count: int) -> bytes:
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def _frame(self, wait: float | None):
        if not self._need(2, wait):
            return None
        first, second = self.buffer[0], self.buffer[1]
        size = second & 0x7F
        offset = 2
        if size == 126:
            self._need(4, None)
            size, offset = struct.unpack(">H", bytes(self.buffer[2:4]))[0], 4
        elif size == 127:
            self._need(10, None)
            size, offset = struct.unpack(">Q", bytes(self.buffer[2:10]))[0], 10
        if size > MAX_MESSAGE:
            raise Closed("A message from the portal is too large.")
        masked = bool(second & 0x80)
        self._need(offset + (4 if masked else 0) + size, None)
        self._take(offset)
        mask = self._take(4) if masked else b""
        payload = self._take(size)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def receive(self, wait: float):
        """The next text message, or None when nothing arrives within wait seconds."""
        message = bytearray()
        kind = None
        while True:
            frame = self._frame(wait if kind is None else None)
            if frame is None:
                return None
            final, opcode, payload = frame
            if opcode == 0x8:
                self.closed = True
                raise Closed("The portal closed the connection.")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, message = opcode, bytearray(payload)
            elif opcode == 0x0 and kind is not None:
                message.extend(payload)
            else:
                raise Closed("Unexpected WebSocket frame.")
            if len(message) > MAX_MESSAGE:
                raise Closed("A message from the portal is too large.")
            if final:
                return message.decode("utf-8", "replace") if kind == 0x1 else None

    def _send(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if size < 4096 else _xor(payload, mask)
        with self.send_lock:
            self.sock.settimeout(30.0)
            self.sock.sendall(bytes(header) + mask + body)

    def send_json(self, value: dict) -> None:
        self._send(0x1, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def close(self) -> None:
        try:
            if not self.closed:
                self._send(0x8, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _xor(payload: bytes, mask: bytes) -> bytes:
    size = len(payload)
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(size, "big")


# ---------------------------------------------------------------- tools

def resolve(value) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ToolError("path must be a non-empty string")
    path = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    return path if path.is_absolute() else Path.home() / path


def number(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(min(max(value, low), high))


def oem_encoding() -> str:
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "utf-8"


def shell_command(kind: str, command: str):
    """(argv, encoding of its output)."""
    if len(command) > COMMAND_MAX:
        raise ToolError(f"The command is longer than {COMMAND_MAX} characters: write it to a script file with pc_write and run the file.")
    if WIN:
        if kind in ("default", "powershell"):
            script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;"
                      "$ProgressPreference='SilentlyContinue';" + command +
                      "\nexit $(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 })")
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded], "utf-8"
        if kind == "cmd":
            return ["cmd.exe", "/d", "/s", "/c", command], oem_encoding()
        found = shutil.which("bash" if kind == "bash" else "sh")
        if not found:
            raise ToolError(f"{kind} is not installed on this computer.")
        return [found, "-c", command], "utf-8"
    if kind == "powershell":
        found = shutil.which("pwsh")
        if not found:
            raise ToolError("PowerShell (pwsh) is not installed on this computer.")
        return [found, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], "utf-8"
    if kind == "cmd":
        raise ToolError("cmd exists only on Windows.")
    found = shutil.which("bash") if kind == "bash" else "/bin/sh"
    if not found:
        raise ToolError("bash is not installed on this computer.")
    return [found, "-c", command], "utf-8"


class Capture(threading.Thread):
    """Reads a pipe to the end, keeping the first HEAD and the last TAIL bytes so a runaway command cannot fill the memory."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self):
        while True:
            chunk = self.stream.read1(65536)
            if not chunk:
                break
            self.total += len(chunk)
            room = HEAD - len(self.head)
            if room > 0:
                self.head.extend(chunk[:room])
                chunk = chunk[room:]
            if chunk:
                self.tail.extend(chunk)
                del self.tail[:-TAIL]

    def text(self, encoding: str):
        cut = self.total - len(self.head) - len(self.tail)
        data = bytes(self.head) + (f"\n...[{cut} bytes cut]...\n".encode() if cut > 0 else b"") + bytes(self.tail)
        return data.decode(encoding, "replace"), cut > 0


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15, creationflags=0x08000000)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def tool_run(args: dict) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")
    kind = args.get("shell") if args.get("shell") in ("default", "powershell", "cmd", "bash", "sh") else "default"
    argv, encoding = shell_command(kind, command)
    cwd = resolve(args.get("cwd") or "~")
    if not cwd.is_dir():
        raise ToolError(f"{cwd} is not a folder")
    timeout = number(args.get("timeout_s"), 120, 1, 900)
    options = {"creationflags": 0x08000000 | 0x00000200} if WIN else {"start_new_session": True}
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    except OSError as error:
        raise ToolError(f"Could not start the shell: {error}")
    out, err = Capture(proc.stdout), Capture(proc.stderr)
    out.start()
    err.start()
    timed_out = False
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            pass
    out.join(5)
    err.join(5)
    stdout, cut_out = out.text(encoding)
    stderr, cut_err = err.text(encoding)
    return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr, "timed_out": timed_out, "truncated": cut_out or cut_err,
            "duration_ms": int((time.monotonic() - started) * 1000), "cwd": str(cwd)}


def tool_read(args: dict) -> dict:
    path = resolve(args.get("path"))
    if not path.is_file():
        raise ToolError(f"{path} is not a file" if path.exists() else f"{path} does not exist")
    size = path.stat().st_size
    offset = number(args.get("offset"), 0, 0, 1 << 60)
    limit = number(args.get("limit"), READ_DEFAULT, 1, READ_MAX)
    with open(path, "rb") as stream:
        stream.seek(offset)
        data = stream.read(limit)
    if args.get("encoding") == "base64":
        return {"path": str(path), "size": size, "offset": offset, "length": len(data), "eof": offset + len(data) >= size,
                "encoding": "base64", "data": base64.b64encode(data).decode()}
    if b"\0" in data[:8192]:
        raise ToolError("This looks like a binary file; read it with encoding=base64.")
    text = None
    if offset + len(data) < size:  # more follows: end the piece on a character boundary so the next offset starts on one
        for cut in range(4):
            try:
                text = data[:len(data) - cut].decode("utf-8")
                data = data[:len(data) - cut]
                break
            except UnicodeDecodeError:
                continue
    if text is None:
        text = data.decode("utf-8", "replace")
    return {"path": str(path), "size": size, "offset": offset, "length": len(data), "eof": offset + len(data) >= size, "encoding": "utf-8", "text": text}


def tool_write(args: dict) -> dict:
    path = resolve(args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if args.get("encoding") == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except ValueError:
            raise ToolError("content is not valid base64")
    else:
        data = content.encode("utf-8")
    if len(data) > WRITE_MAX:
        raise ToolError("One write is limited to 16 MB; send larger files in pieces with append.")
    if path.is_dir():
        raise ToolError(f"{path} is a folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    if args.get("append"):
        with open(path, "ab") as stream:
            stream.write(data)
    else:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            with open(temp, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if existed:
                try:
                    shutil.copymode(path, temp)
                except OSError:
                    pass
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
    return {"path": str(path), "bytes": len(data), "created": not existed, "appended": bool(args.get("append"))}


def tool_edit(args: dict) -> dict:
    path = resolve(args.get("path"))
    old, new = args.get("old"), args.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ToolError("old must be a non-empty string and new a string")
    if not path.is_file():
        raise ToolError(f"{path} does not exist")
    raw = path.read_bytes()
    if len(raw) > WRITE_MAX:
        raise ToolError("The file is too large to edit this way.")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("The file is not UTF-8 text.")
    crlf = "\r\n" in text
    if crlf:
        old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(old)
    if count == 0:
        raise ToolError("old text not found in the file")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old text occurs {count} times; make it longer so it is unique, or set replace_all")
    text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_bytes(data)
        shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return {"path": str(path), "replaced": count if args.get("replace_all") else 1}


def tool_list(args: dict) -> dict:
    root = resolve(args.get("path"))
    if not root.is_dir():
        raise ToolError(f"{root} is not a folder" if root.exists() else f"{root} does not exist")
    depth = number(args.get("depth"), 1, 1, 3)
    entries = []
    truncated = False

    def walk(folder: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as error:
            entries.append({"name": str(folder.relative_to(root)), "type": "error", "error": error.strerror or str(error)})
            return
        for child in children:
            if len(entries) >= LIST_MAX:
                truncated = True
                return
            try:
                info = child.lstat()
            except OSError:
                continue
            link = child.is_symlink()
            kind = "link" if link else "dir" if child.is_dir() else "file"
            entries.append({"name": str(child.relative_to(root)), "type": kind, "size": None if kind == "dir" else info.st_size,
                            "mtime": datetime.fromtimestamp(info.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M")})
            if kind == "dir" and level < depth:
                walk(child, level + 1)

    walk(root, 1)
    return {"path": str(root), "entries": entries, "truncated": truncated}


def tool_info(args: dict) -> dict:
    if WIN:
        shell = "powershell"
    else:
        shell = os.path.basename(os.environ.get("SHELL") or "sh")
    return {"os": platform.platform(), "platform": sys.platform, "arch": platform.machine(), "host": socket.gethostname(),
            "user": getpass.getuser(), "home": str(Path.home()), "default_shell": shell, "python": platform.python_version(),
            "agent": VERSION, "paused": paused(), "time": datetime.now().astimezone().isoformat(timespec="seconds")}


# ---------------------------------------------------------------- preferences (the switches of the settings window)

PREF_DEFAULTS = {"screen": True, "shell": True, "update": True}
PREF_LOCK = threading.Lock()


def prefs() -> dict:
    stored = read_config().get("prefs")
    chosen = {k: bool(v) for k, v in stored.items() if k in PREF_DEFAULTS} if isinstance(stored, dict) else {}
    return {**PREF_DEFAULTS, **chosen}


def set_pref(name: str, value: bool) -> None:
    if name not in PREF_DEFAULTS:
        raise ToolError(f"unknown setting {name}")
    with PREF_LOCK:
        config = read_config()
        config["prefs"] = {**prefs(), name: bool(value)}
        write_config(config)
    log(f"setting: {name} {'on' if value else 'off'}")


def set_paused(on: bool) -> None:
    if on:
        private_dir()
        (data_dir() / "paused").write_text("paused\n")
    else:
        (data_dir() / "paused").unlink(missing_ok=True)
    log("paused by the user" if on else "resumed by the user")


# ---------------------------------------------------------------- computer use: screenshots, mouse and keyboard

TYPE_PAUSE = 0.012  # seconds between typed characters
SCREEN_EDGE = 1366  # long edge of a screenshot sent to Claude; coordinates of every action are in this space
SHOT = {"scale": None, "origin": (0, 0)}  # the latest full screenshot
LAST_ACTION = [0.0]  # when Claude last touched the screen; the tray icon turns blue for a few seconds
_WIN = {}


def png_rgb(raw: bytes, width: int, height: int, bgra: bool = True) -> bytes:
    """A truecolour PNG from 32-bit BGRA (or 24-bit RGB) pixels, top row first."""
    if bgra:
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    else:
        rgb = raw
    stride = width * 3
    rows = b"".join(b"\x00" + rgb[i * stride:(i + 1) * stride] for i in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def dpi_aware() -> None:
    """Real pixels instead of the scaled ones (a 150% display reports 1707x1067 instead of 2560x1600). Before any window exists."""
    if not WIN or _WIN.get("dpi"):
        return
    import ctypes
    _WIN["dpi"] = True
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def win_api():
    if "api" in _WIN:
        return _WIN["api"]
    import ctypes
    from ctypes import wintypes as w
    dpi_aware()
    user32, gdi32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                    ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                    ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hCursor", w.HANDLE), ("ptScreenPos", w.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD), ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

    class EVENT(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", w.DWORD), ("event", EVENT)]

    user32.GetDC.argtypes, user32.GetDC.restype = [w.HWND], w.HDC
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    user32.GetIconInfo.argtypes = [w.HANDLE, ctypes.POINTER(ICONINFO)]
    user32.DrawIconEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.HANDLE, ctypes.c_int, ctypes.c_int, w.UINT, w.HANDLE, w.UINT]
    user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.VkKeyScanW.argtypes, user32.VkKeyScanW.restype = [w.WCHAR], ctypes.c_short
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [w.HDC], w.HDC
    gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFOHEADER), w.UINT, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
    gdi32.CreateDIBSection.restype = w.HBITMAP
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [w.HDC, w.HGDIOBJ], w.HGDIOBJ
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [w.HDC]
    gdi32.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, w.DWORD]
    api = {"ct": ctypes, "w": w, "user32": user32, "gdi32": gdi32, "BIH": BITMAPINFOHEADER, "CURSORINFO": CURSORINFO, "ICONINFO": ICONINFO,
           "INPUT": INPUT, "MOUSEINPUT": MOUSEINPUT, "KEYBDINPUT": KEYBDINPUT}
    _WIN["api"] = api
    return api


def screen_size() -> tuple:
    a = win_api()
    return a["user32"].GetSystemMetrics(0), a["user32"].GetSystemMetrics(1)


def win_capture(region=None, edge: int = SCREEN_EDGE):
    """(png, width, height, scale, origin) of the primary display, or of a region of it, scaled so the long edge is at most `edge`."""
    a = win_api()
    ct, user32, gdi32 = a["ct"], a["user32"], a["gdi32"]
    width, height = screen_size()
    x0, y0, x1, y1 = region or (0, 0, width, height)
    x0, y0 = max(0, min(x0, width - 1)), max(0, min(y0, height - 1))
    x1, y1 = max(x0 + 1, min(x1, width)), max(y0 + 1, min(y1, height))
    sw, sh = x1 - x0, y1 - y0
    scale = min(1.0, edge / max(sw, sh)) if region is None else min(4.0, edge / max(sw, sh))
    w_, h_ = max(1, round(sw * scale)), max(1, round(sh * scale))
    screen = user32.GetDC(None)
    if not screen:
        raise ToolError("Windows gave no access to the screen (is the session locked?)")
    memory = gdi32.CreateCompatibleDC(screen)
    header = a["BIH"](40, w_, -h_, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ct.c_void_p()
    bitmap = gdi32.CreateDIBSection(screen, ct.byref(header), 0, ct.byref(bits), None, 0)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        gdi32.SetStretchBltMode(memory, 4)
        gdi32.SetBrushOrgEx(memory, 0, 0, None)
        if not gdi32.StretchBlt(memory, 0, 0, w_, h_, screen, x0, y0, sw, sh, 0x00CC0020 | 0x40000000):
            raise ToolError("The screen could not be captured (the lock screen or a security prompt may be in front)")
        cursor = a["CURSORINFO"]()
        cursor.cbSize = ct.sizeof(cursor)
        if user32.GetCursorInfo(ct.byref(cursor)) and cursor.flags & 1:
            info = a["ICONINFO"]()
            hot_x = hot_y = 0
            if user32.GetIconInfo(cursor.hCursor, ct.byref(info)):
                hot_x, hot_y = info.xHotspot, info.yHotspot
                for handle in (info.hbmMask, info.hbmColor):
                    if handle:
                        gdi32.DeleteObject(handle)
            cx = round((cursor.ptScreenPos.x - x0) * scale) - hot_x
            cy = round((cursor.ptScreenPos.y - y0) * scale) - hot_y
            user32.DrawIconEx(memory, cx, cy, cursor.hCursor, 0, 0, 0, None, 3)
        raw = ct.string_at(bits, w_ * h_ * 4)
    finally:
        gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    return png_rgb(raw, w_, h_), w_, h_, scale, (x0, y0)


def send_inputs(events: list) -> None:
    a = win_api()
    if not events:
        return
    array = (a["INPUT"] * len(events))(*events)
    sent = a["user32"].SendInput(len(events), array, a["ct"].sizeof(a["INPUT"]))
    if sent != len(events):
        raise ToolError("Windows blocked the input. An administrator window, a security prompt or the lock screen is probably in front.")
    LAST_ACTION[0] = time.monotonic()


def mouse_event(flags: int, x: int = 0, y: int = 0, data: int = 0):
    a = win_api()
    event = a["INPUT"](type=0)
    event.event.mi = a["MOUSEINPUT"](x, y, data & 0xFFFFFFFF, flags, 0, 0)
    return event


def key_event(vk: int = 0, scan: int = 0, flags: int = 0):
    a = win_api()
    event = a["INPUT"](type=1)
    event.event.ki = a["KEYBDINPUT"](vk, scan, flags, 0, 0)
    return event


def move_event(x: int, y: int):
    """An absolute move over the whole virtual desktop (all monitors), in real pixels."""
    user32 = win_api()["user32"]
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    nx = round((x - vx) * 65535 / max(1, vw - 1))
    ny = round((y - vy) * 65535 / max(1, vh - 1))
    return mouse_event(0x0001 | 0x8000 | 0x4000, nx, ny)


VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "super": 0x5B, "cmd": 0x5B, "meta": 0x5B,
      "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
      "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "page_up": 0x21, "pagedown": 0x22, "page_down": 0x22,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "space": 0x20, "printscreen": 0x2C, "print": 0x2C,
      "capslock": 0x14, "menu": 0x5D, "apps": 0x5D, "pause": 0x13, "numlock": 0x90, "scrolllock": 0x91,
      "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3,
      **{f"f{n}": 0x6F + n for n in range(1, 25)}}
EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D, 0x2C, 0x90, 0xAF, 0xAE, 0xAD, 0xB3}


def vk_of(name: str) -> int:
    name = name.strip()
    low = name.lower().replace(" ", "")
    if low in VK:
        return VK[low]
    if len(name) == 1:
        if name.isascii() and name.isalnum():
            return ord(name.upper())
        code = win_api()["user32"].VkKeyScanW(name)
        if code != -1:
            return code & 0xFF
    raise ToolError(f"Unknown key {name!r}. Use names like ctrl, alt, shift, win, enter, tab, escape, backspace, delete, home, end, "
                    "pageup, pagedown, up, down, left, right, space, f1-f24, or one character.")


def press(vk: int, up: bool) -> object:
    return key_event(vk, 0, (0x0001 if vk in EXTENDED else 0) | (0x0002 if up else 0))


def png_size(data: bytes) -> tuple:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ToolError("The screenshot tool did not return a PNG image")
    return struct.unpack(">II", data[16:24])


def mark_pointer(rgb: bytearray, width: int, height: int, x: int, y: int) -> None:
    """A small arrow where the pointer is, for systems whose capture leaves it out (X11). RGB, top row first."""
    shape = ["X", "XX", "XWX", "XWWX", "XWWWX", "XWWWWX", "XWWWWWX", "XWWWWWWX", "XWWWWXXXX", "XWWXWX", "XWX XWX", "XX  XWX", "X    XX"]
    for dy, row in enumerate(shape):
        for dx, cell in enumerate(row):
            px, py = x + dx, y + dy
            if cell != " " and 0 <= px < width and 0 <= py < height:
                i = (py * width + px) * 3
                rgb[i:i + 3] = b"\x00\x00\x00" if cell == "X" else b"\xff\xff\xff"


class Desktop:
    """One system's screen: size and capture in its own units, and input at positions in those units."""
    name = "screen"

    def problem(self):
        """None when screen control can work here, otherwise a sentence for the user (shown in the settings window and to Claude)."""
        return None

    def size(self) -> tuple:
        raise NotImplementedError

    def capture(self, region, edge: int):
        raise NotImplementedError

    def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    def button(self, name: str, down: bool, clicks: int = 1) -> None:
        raise NotImplementedError

    def click(self, name: str, clicks: int) -> None:
        for n in range(1, clicks + 1):
            self.button(name, True, n)
            self.button(name, False, n)

    def drag_to(self, x: int, y: int) -> None:
        self.move(x, y)

    def scroll(self, direction: str, amount: int) -> None:
        raise NotImplementedError

    def key(self, name: str, down: bool) -> None:
        raise NotImplementedError

    def type_char(self, char: str) -> None:
        raise NotImplementedError

    def cursor(self) -> tuple:
        raise NotImplementedError


class WinDesktop(Desktop):
    name = "Windows"

    def size(self):
        return screen_size()

    def capture(self, region, edge):
        return win_capture(region, edge)

    def move(self, x, y):
        send_inputs([move_event(x, y)])

    def button(self, name, down, clicks=1):
        send_inputs([mouse_event(WIN_BUTTONS[name][0 if down else 1])])

    def click(self, name, clicks):
        down, up = WIN_BUTTONS[name]
        send_inputs([mouse_event(flag) for _ in range(clicks) for flag in (down, up)])

    def scroll(self, direction, amount):
        delta = 120 * amount * (1 if direction in ("up", "right") else -1)
        send_inputs([mouse_event(0x01000 if direction in ("left", "right") else 0x0800, 0, 0, delta)])

    def key(self, name, down):
        send_inputs([press(vk_of(name), not down)])

    def type_char(self, char):
        if char == "\n":
            send_inputs([press(0x0D, False), press(0x0D, True)])
        elif char == "\t":
            send_inputs([press(0x09, False), press(0x09, True)])
        else:
            raw = char.encode("utf-16-le")
            units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
            send_inputs([key_event(0, u, 0x0004) for u in units] + [key_event(0, u, 0x0004 | 0x0002) for u in units])

    def cursor(self):
        a = win_api()
        point = a["w"].POINT()
        a["user32"].GetCursorPos(a["ct"].byref(point))
        return point.x, point.y


WIN_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


# -------- Linux, X11: Xlib for the picture, XTest for input (both come with every X desktop)

X_KEYS = {"ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L", "option": "Alt_L", "shift": "Shift_L", "win": "Super_L",
          "super": "Super_L", "cmd": "Super_L", "meta": "Super_L", "enter": "Return", "return": "Return", "tab": "Tab", "escape": "Escape",
          "esc": "Escape", "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "insert": "Insert", "home": "Home", "end": "End",
          "pageup": "Prior", "page_up": "Prior", "pagedown": "Next", "page_down": "Next", "up": "Up", "down": "Down", "left": "Left",
          "right": "Right", "space": "space", "printscreen": "Print", "print": "Print", "capslock": "Caps_Lock", "menu": "Menu",
          "apps": "Menu", "pause": "Pause", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock", "volumeup": "XF86AudioRaiseVolume",
          "volumedown": "XF86AudioLowerVolume", "volumemute": "XF86AudioMute", "playpause": "XF86AudioPlay",
          **{f"f{n}": f"F{n}" for n in range(1, 25)}}


class X11Desktop(Desktop):
    name = "Linux (X11)"

    def __init__(self):
        self.x = None
        self.lock = threading.Lock()

    def problem(self):
        if not os.environ.get("DISPLAY"):
            if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
                return WAYLAND_PROBLEM
            return "No graphical session was found (DISPLAY is not set): start the app from your desktop session."
        if (os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("CLAUDE_PORTAL_X11"):
            return WAYLAND_PROBLEM
        try:
            self.api()
        except ToolError as error:
            return str(error)
        return None

    def api(self):
        if self.x:
            return self.x
        import ctypes
        import ctypes.util
        names = {"X11": ctypes.util.find_library("X11") or "libX11.so.6", "Xtst": ctypes.util.find_library("Xtst") or "libXtst.so.6"}
        try:
            xlib, xtst = ctypes.CDLL(names["X11"]), ctypes.CDLL(names["Xtst"])
        except OSError:
            raise ToolError("The X11 libraries libX11 and libXtst are missing; install them (Debian/Ubuntu: sudo apt install libxtst6).")
        c = ctypes
        ul = c.c_ulong

        class XImage(c.Structure):
            _fields_ = [("width", c.c_int), ("height", c.c_int), ("xoffset", c.c_int), ("format", c.c_int), ("data", c.POINTER(c.c_ubyte)),
                        ("byte_order", c.c_int), ("bitmap_unit", c.c_int), ("bitmap_bit_order", c.c_int), ("bitmap_pad", c.c_int),
                        ("depth", c.c_int), ("bytes_per_line", c.c_int), ("bits_per_pixel", c.c_int), ("red_mask", ul), ("green_mask", ul),
                        ("blue_mask", ul), ("obdata", c.c_void_p), ("create_image", c.c_void_p),
                        ("destroy_image", c.CFUNCTYPE(c.c_int, c.c_void_p))]

        xlib.XOpenDisplay.argtypes, xlib.XOpenDisplay.restype = [c.c_char_p], c.c_void_p
        xlib.XDefaultRootWindow.argtypes, xlib.XDefaultRootWindow.restype = [c.c_void_p], ul
        xlib.XDefaultScreen.argtypes = [c.c_void_p]
        xlib.XDisplayWidth.argtypes = xlib.XDisplayHeight.argtypes = [c.c_void_p, c.c_int]
        xlib.XGetImage.argtypes = [c.c_void_p, ul, c.c_int, c.c_int, c.c_uint, c.c_uint, ul, c.c_int]
        xlib.XGetImage.restype = c.POINTER(XImage)
        xlib.XQueryPointer.argtypes = [c.c_void_p, ul, c.POINTER(ul), c.POINTER(ul), c.POINTER(c.c_int), c.POINTER(c.c_int),
                                       c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_uint)]
        xlib.XStringToKeysym.argtypes, xlib.XStringToKeysym.restype = [c.c_char_p], ul
        xlib.XKeysymToKeycode.argtypes, xlib.XKeysymToKeycode.restype = [c.c_void_p, ul], c.c_ubyte
        xlib.XGetKeyboardMapping.argtypes = [c.c_void_p, c.c_ubyte, c.c_int, c.POINTER(c.c_int)]
        xlib.XGetKeyboardMapping.restype = c.POINTER(ul)
        xlib.XChangeKeyboardMapping.argtypes = [c.c_void_p, c.c_int, c.c_int, c.POINTER(ul), c.c_int]
        xlib.XDisplayKeycodes.argtypes = [c.c_void_p, c.POINTER(c.c_int), c.POINTER(c.c_int)]
        xlib.XFree.argtypes = [c.c_void_p]
        xlib.XSync.argtypes = [c.c_void_p, c.c_int]
        xlib.XFlush.argtypes = [c.c_void_p]
        xtst.XTestFakeMotionEvent.argtypes = [c.c_void_p, c.c_int, c.c_int, c.c_int, ul]
        xtst.XTestFakeButtonEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, ul]
        xtst.XTestFakeKeyEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, ul]
        display = xlib.XOpenDisplay(None)
        if not display:
            raise ToolError(f"Could not open the X display {os.environ.get('DISPLAY')!r}.")
        self.x = {"c": c, "xlib": xlib, "xtst": xtst, "d": display, "root": xlib.XDefaultRootWindow(display),
                  "screen": xlib.XDefaultScreen(display), "spare": None}
        return self.x

    def size(self):
        x = self.api()
        return x["xlib"].XDisplayWidth(x["d"], x["screen"]), x["xlib"].XDisplayHeight(x["d"], x["screen"])

    def capture(self, region, edge):
        x = self.api()
        width, height = self.size()
        x0, y0, x1, y1 = region or (0, 0, width, height)
        x0, y0 = max(0, min(x0, width - 1)), max(0, min(y0, height - 1))
        x1, y1 = max(x0 + 1, min(x1, width)), max(y0 + 1, min(y1, height))
        sw, sh = x1 - x0, y1 - y0
        with self.lock:
            image = x["xlib"].XGetImage(x["d"], x["root"], x0, y0, sw, sh, 0xFFFFFFFF, 2)
            if not image:
                raise ToolError("The screen could not be captured.")
            try:
                im = image.contents
                if im.bits_per_pixel != 32:
                    raise ToolError(f"Unsupported screen depth: {im.bits_per_pixel} bits per pixel.")
                raw = x["c"].string_at(im.data, im.bytes_per_line * im.height)
                stride = im.bytes_per_line
            finally:
                image.contents.destroy_image(x["c"].cast(image, x["c"].c_void_p))
        scale = min(1.0, edge / max(sw, sh)) if region is None else min(4.0, edge / max(sw, sh))
        w_, h_ = max(1, round(sw * scale)), max(1, round(sh * scale))
        from operator import itemgetter
        xs = [min(sw - 1, int(i / scale)) * 4 for i in range(w_)]
        pick = [itemgetter(*[i + k for i in xs]) if w_ > 1 else (lambda row, k=k: (row[xs[0] + k],)) for k in (2, 1, 0)]
        view = memoryview(raw)
        rgb = bytearray(w_ * h_ * 3)
        line = bytearray(w_ * 3)
        for j in range(h_):
            start = min(sh - 1, int(j / scale)) * stride
            row = view[start:start + stride]
            line[0::3], line[1::3], line[2::3] = bytes(pick[0](row)), bytes(pick[1](row)), bytes(pick[2](row))
            rgb[j * w_ * 3:(j + 1) * w_ * 3] = line
        px, py = self.cursor()
        mark_pointer(rgb, w_, h_, round((px - x0) * scale), round((py - y0) * scale))
        return png_rgb(bytes(rgb), w_, h_, bgra=False), w_, h_, scale, (x0, y0)

    def sync(self):
        x = self.x
        x["xlib"].XSync(x["d"], 0)
        LAST_ACTION[0] = time.monotonic()

    def move(self, px, py):
        x = self.api()
        with self.lock:
            x["xtst"].XTestFakeMotionEvent(x["d"], -1, px, py, 0)
            self.sync()

    def button(self, name, down, clicks=1):
        x = self.api()
        with self.lock:
            x["xtst"].XTestFakeButtonEvent(x["d"], {"left": 1, "middle": 2, "right": 3}[name], 1 if down else 0, 0)
            self.sync()

    def click(self, name, clicks):
        for _ in range(clicks):
            self.button(name, True)
            self.button(name, False)
            time.sleep(0.03)

    def scroll(self, direction, amount):
        x = self.api()
        code = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
        with self.lock:
            for _ in range(amount):
                x["xtst"].XTestFakeButtonEvent(x["d"], code, 1, 0)
                x["xtst"].XTestFakeButtonEvent(x["d"], code, 0, 0)
            self.sync()

    def keycode(self, keysym: int):
        """(keycode, shift) of a keysym on the current layout, or None."""
        x = self.api()
        code = x["xlib"].XKeysymToKeycode(x["d"], keysym)
        if not code:
            return None
        per = x["c"].c_int()
        syms = x["xlib"].XGetKeyboardMapping(x["d"], code, 1, x["c"].byref(per))
        try:
            levels = [syms[i] for i in range(min(per.value, 2))]
        finally:
            x["xlib"].XFree(syms)
        if keysym == levels[0]:
            return code, False
        if len(levels) > 1 and keysym == levels[1]:
            return code, True
        return None

    def key(self, name, down):
        x = self.api()
        low = name.strip().lower().replace(" ", "")
        if low in X_KEYS:
            keysym = x["xlib"].XStringToKeysym(X_KEYS[low].encode())
        elif len(name) == 1:
            keysym = char_keysym(name.lower() if name.isalpha() and name.isascii() else name)
        else:
            raise ToolError(f"Unknown key {name!r}.")
        found = self.keycode(keysym)
        if not found:
            raise ToolError(f"The key {name!r} is not on this keyboard layout.")
        with self.lock:
            x["xtst"].XTestFakeKeyEvent(x["d"], found[0], 1 if down else 0, 0)
            self.sync()

    def spare_keycode(self):
        """A keycode with no symbols, borrowed to type characters the layout does not have (as xdotool does)."""
        x = self.api()
        if x["spare"] is None:
            low, high = x["c"].c_int(), x["c"].c_int()
            x["xlib"].XDisplayKeycodes(x["d"], x["c"].byref(low), x["c"].byref(high))
            per = x["c"].c_int()
            count = high.value - low.value + 1
            syms = x["xlib"].XGetKeyboardMapping(x["d"], low.value, count, x["c"].byref(per))
            try:
                for i in range(count - 1, -1, -1):
                    if all(syms[i * per.value + k] == 0 for k in range(per.value)):
                        x["spare"] = low.value + i
                        break
            finally:
                x["xlib"].XFree(syms)
            if x["spare"] is None:
                raise ToolError("No free key to type this character with.")
        return x["spare"]

    def type_char(self, char):
        if char == "\n":
            return self.tap("enter")
        if char == "\t":
            return self.tap("tab")
        x = self.api()
        keysym = char_keysym(char)
        found = self.keycode(keysym)
        with self.lock:
            if found:
                code, shift = found
                shift_code = self.keycode(x["xlib"].XStringToKeysym(b"Shift_L"))[0] if shift else None
                if shift:
                    x["xtst"].XTestFakeKeyEvent(x["d"], shift_code, 1, 0)
                x["xtst"].XTestFakeKeyEvent(x["d"], code, 1, 0)
                x["xtst"].XTestFakeKeyEvent(x["d"], code, 0, 0)
                if shift:
                    x["xtst"].XTestFakeKeyEvent(x["d"], shift_code, 0, 0)
                self.sync()
                return
            spare = self.spare_keycode()
            pair = (x["c"].c_ulong * 2)(keysym, keysym)
            x["xlib"].XChangeKeyboardMapping(x["d"], spare, 2, pair, 1)
            x["xlib"].XSync(x["d"], 0)
            time.sleep(0.02)
            x["xtst"].XTestFakeKeyEvent(x["d"], spare, 1, 0)
            x["xtst"].XTestFakeKeyEvent(x["d"], spare, 0, 0)
            x["xlib"].XSync(x["d"], 0)
            time.sleep(0.02)
            empty = (x["c"].c_ulong * 2)(0, 0)
            x["xlib"].XChangeKeyboardMapping(x["d"], spare, 2, empty, 1)
            self.sync()

    def tap(self, name):
        self.key(name, True)
        self.key(name, False)

    def cursor(self):
        x = self.api()
        c = x["c"]
        root, child = c.c_ulong(), c.c_ulong()
        rx, ry, wx, wy, mask = c.c_int(), c.c_int(), c.c_int(), c.c_int(), c.c_uint()
        with self.lock:
            x["xlib"].XQueryPointer(x["d"], x["root"], c.byref(root), c.byref(child), c.byref(rx), c.byref(ry), c.byref(wx), c.byref(wy),
                                    c.byref(mask))
        return rx.value, ry.value


def char_keysym(char: str) -> int:
    code = ord(char)
    return code if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF else 0x01000000 | code


WAYLAND_PROBLEM = ("This desktop runs Wayland, which lets no app see or control other windows. Commands and files work; for screen control "
                   "choose an X11 session at login (Ubuntu: the gear on the login screen, “Ubuntu on Xorg”; Fedora/KDE: “Plasma (X11)”).")


# -------- macOS: screencapture and sips (built in) for the picture, CoreGraphics events for input

MAC_KEYS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
            "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
            "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
            "n": 45, "m": 46, ".": 47, "`": 50,
            "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "esc": 53, "cmd": 55, "win": 55, "super": 55,
            "meta": 55, "shift": 56, "capslock": 57, "alt": 58, "option": 58, "ctrl": 59, "control": 59, "delete": 117, "del": 117,
            "home": 115, "end": 119, "pageup": 116, "page_up": 116, "pagedown": 121, "page_down": 121, "left": 123, "right": 124,
            "down": 125, "up": 126, "volumeup": 72, "volumedown": 73, "volumemute": 74,
            "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103,
            "f12": 111}
MAC_FLAGS = {55: 0x100000, 56: 0x20000, 58: 0x80000, 59: 0x40000}


class MacDesktop(Desktop):
    name = "macOS"

    def __init__(self):
        self.q = None
        self.flags = 0
        self.position = None

    def api(self):
        if self.q:
            return self.q
        import ctypes
        c = ctypes

        class CGPoint(c.Structure):
            _fields_ = [("x", c.c_double), ("y", c.c_double)]

        class CGSize(c.Structure):
            _fields_ = [("width", c.c_double), ("height", c.c_double)]

        class CGRect(c.Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        cg = c.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ax = c.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cg.CGMainDisplayID.restype = c.c_uint32
        cg.CGDisplayBounds.argtypes, cg.CGDisplayBounds.restype = [c.c_uint32], CGRect
        cg.CGEventCreateMouseEvent.argtypes = [c.c_void_p, c.c_uint32, CGPoint, c.c_uint32]
        cg.CGEventCreateMouseEvent.restype = c.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes, cg.CGEventCreateKeyboardEvent.restype = [c.c_void_p, c.c_uint16, c.c_bool], c.c_void_p
        cg.CGEventCreateScrollWheelEvent2.argtypes = [c.c_void_p, c.c_uint32, c.c_uint32, c.c_int32, c.c_int32, c.c_int32]
        cg.CGEventCreateScrollWheelEvent2.restype = c.c_void_p
        cg.CGEventKeyboardSetUnicodeString.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_uint16)]
        cg.CGEventSetIntegerValueField.argtypes = [c.c_void_p, c.c_uint32, c.c_int64]
        cg.CGEventSetFlags.argtypes = [c.c_void_p, c.c_uint64]
        cg.CGEventPost.argtypes = [c.c_uint32, c.c_void_p]
        cg.CGEventCreate.argtypes, cg.CGEventCreate.restype = [c.c_void_p], c.c_void_p
        cg.CGEventGetLocation.argtypes, cg.CGEventGetLocation.restype = [c.c_void_p], CGPoint
        cf.CFRelease.argtypes = [c.c_void_p]
        ax.AXIsProcessTrusted.restype = c.c_bool
        self.q = {"c": c, "cg": cg, "cf": cf, "ax": ax, "CGPoint": CGPoint}
        return self.q

    def screen_access(self) -> bool:
        q = self.api()
        try:
            q["cg"].CGPreflightScreenCaptureAccess.restype = q["c"].c_bool
            return bool(q["cg"].CGPreflightScreenCaptureAccess())
        except AttributeError:
            return True

    def problem(self):
        try:
            q = self.api()
        except OSError as error:
            return f"macOS frameworks could not be loaded: {error}"
        missing = []
        if not self.screen_access():
            missing.append("“Screen Recording”")
        if not q["ax"].AXIsProcessTrusted():
            missing.append("“Accessibility”")
        if missing:
            # macOS checks the real interpreter file, not the python3 stub or Python.app (owner's Mac, 17 Nov 2026: only this path worked).
            real = os.path.realpath(sys.executable)
            return ("macOS needs your permission: System Settings → Privacy & Security → " + " and ".join(missing) +
                    f" → “+”, press Cmd+Shift+G, paste {real} and switch it on; then quit and restart Claude Portal. "
                    "The settings window of Claude Portal has a button that opens these pages.")
        return None

    def request(self) -> None:
        """Ask macOS for both permissions (it shows its own prompts) and open the right page of System Settings."""
        q = self.api()
        c = q["c"]
        try:
            q["cg"].CGRequestScreenCaptureAccess.restype = c.c_bool
            q["cg"].CGRequestScreenCaptureAccess()
        except AttributeError:
            pass
        cf = q["cf"]
        cf.CFStringCreateWithCString.argtypes, cf.CFStringCreateWithCString.restype = [c.c_void_p, c.c_char_p, c.c_uint32], c.c_void_p
        cf.CFDictionaryCreate.argtypes = [c.c_void_p, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.c_long, c.c_void_p, c.c_void_p]
        cf.CFDictionaryCreate.restype = c.c_void_p
        key = cf.CFStringCreateWithCString(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
        true = c.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys, values = (c.c_void_p * 1)(key), (c.c_void_p * 1)(true.value)
        options = cf.CFDictionaryCreate(None, keys, values, 1, None, None)
        q["ax"].AXIsProcessTrustedWithOptions.argtypes, q["ax"].AXIsProcessTrustedWithOptions.restype = [c.c_void_p], c.c_bool
        q["ax"].AXIsProcessTrustedWithOptions(options)
        cf.CFRelease(options)
        cf.CFRelease(key)
        page = "Privacy_ScreenCapture" if not self.screen_access() else "Privacy_Accessibility"
        subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{page}"], capture_output=True)

    def size(self):
        q = self.api()
        bounds = q["cg"].CGDisplayBounds(q["cg"].CGMainDisplayID())
        return round(bounds.size.width), round(bounds.size.height)

    def capture(self, region, edge):
        width, height = self.size()
        x0, y0, x1, y1 = region or (0, 0, width, height)
        x0, y0 = max(0, min(x0, width - 1)), max(0, min(y0, height - 1))
        x1, y1 = max(x0 + 1, min(x1, width)), max(y0 + 1, min(y1, height))
        sw, sh = x1 - x0, y1 - y0
        import tempfile
        with tempfile.TemporaryDirectory(prefix="claude-portal-") as folder:
            shot, small = os.path.join(folder, "shot.png"), os.path.join(folder, "small.png")
            command = ["screencapture", "-x", "-C", "-t", "png", "-m"]
            if region is not None:
                command += ["-R", f"{x0},{y0},{sw},{sh}"]
            done = subprocess.run(command + [shot], capture_output=True, timeout=30)
            if done.returncode != 0 or not os.path.exists(shot):
                raise ToolError("The screen could not be captured. " + (self.problem() or done.stderr.decode(errors="replace")[:200]))
            target = edge if region is None else min(4 * max(sw, sh), edge)
            subprocess.run(["sips", "-Z", str(target), shot, "--out", small], capture_output=True, timeout=30)
            data = open(small if os.path.exists(small) else shot, "rb").read()
        w_, h_ = png_size(data)
        return data, w_, h_, w_ / sw, (x0, y0)

    def post(self, event):
        q = self.api()
        if not event:
            raise ToolError("macOS refused to create the input event.")
        q["cg"].CGEventPost(0, event)
        q["cf"].CFRelease(event)
        LAST_ACTION[0] = time.monotonic()

    def mouse(self, kind: int, x, y, button: int = 0, clicks: int = 0):
        q = self.api()
        event = q["cg"].CGEventCreateMouseEvent(None, kind, q["CGPoint"](x, y), button)
        if clicks:
            q["cg"].CGEventSetIntegerValueField(event, 1, clicks)
        if self.flags:
            q["cg"].CGEventSetFlags(event, self.flags)
        self.post(event)

    def move(self, x, y):
        self.position = (x, y)
        self.mouse(5, x, y)

    def drag_to(self, x, y):
        self.position = (x, y)
        self.mouse(6, x, y)

    def button(self, name, down, clicks=1):
        x, y = self.position if self.position and time.monotonic() - LAST_ACTION[0] < 2 else self.cursor()
        kinds = {"left": (1, 2, 0), "right": (3, 4, 1), "middle": (25, 26, 2)}[name]
        self.mouse(kinds[0] if down else kinds[1], x, y, kinds[2], clicks)

    def scroll(self, direction, amount):
        q = self.api()
        dy = amount if direction == "up" else -amount if direction == "down" else 0
        dx = amount if direction == "left" else -amount if direction == "right" else 0
        self.post(q["cg"].CGEventCreateScrollWheelEvent2(None, 1, 2, dy, dx, 0))

    def key(self, name, down):
        q = self.api()
        low = name.strip().lower().replace(" ", "")
        code = MAC_KEYS.get(low)
        if code is None:
            raise ToolError(f"Unknown key {name!r} on macOS. Use names like cmd, ctrl, alt, shift, enter, tab, escape, backspace, "
                            "delete, arrows, f1-f12 or one Latin letter or digit.")
        if code in MAC_FLAGS:
            self.flags = (self.flags | MAC_FLAGS[code]) if down else (self.flags & ~MAC_FLAGS[code])
        event = q["cg"].CGEventCreateKeyboardEvent(None, code, down)
        if self.flags:
            q["cg"].CGEventSetFlags(event, self.flags)
        self.post(event)

    def type_char(self, char):
        if char == "\n":
            return self.key("enter", True) or self.key("enter", False)
        if char == "\t":
            return self.key("tab", True) or self.key("tab", False)
        q = self.api()
        c = q["c"]
        raw = char.encode("utf-16-le")
        units = (c.c_uint16 * (len(raw) // 2))(*[int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)])
        for down in (True, False):
            event = q["cg"].CGEventCreateKeyboardEvent(None, 0, down)
            q["cg"].CGEventKeyboardSetUnicodeString(event, len(units), units)
            self.post(event)

    def cursor(self):
        q = self.api()
        event = q["cg"].CGEventCreate(None)
        point = q["cg"].CGEventGetLocation(event)
        q["cf"].CFRelease(event)
        return round(point.x), round(point.y)


_DESKTOP = []


def desktop() -> Desktop:
    if not _DESKTOP:
        _DESKTOP.append(WinDesktop() if WIN else MacDesktop() if MAC else X11Desktop())
    return _DESKTOP[0]


def screen_problem():
    """Why screen control cannot work here right now, or None."""
    try:
        return desktop().problem()
    except Exception as error:  # a broken system library must not break the settings window
        return f"{type(error).__name__}: {error}"


def need_screen() -> Desktop:
    if not prefs()["screen"]:
        raise ToolError("Screen control is switched off on this computer. Its owner can switch it on in the Claude Portal settings.")
    problem = screen_problem()
    if problem:
        raise ToolError(problem)
    return desktop()


def to_screen(x, y) -> tuple:
    """Pixels of the latest screenshot to the system's own screen units (pixels on Windows and X11, points on macOS)."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
        raise ToolError("x and y must be numbers (pixels of the latest screenshot)")
    scale = SHOT["scale"]
    if scale is None:
        width, height = desktop().size()
        scale = min(1.0, SCREEN_EDGE / max(width, height))
    ox, oy = SHOT["origin"]
    return round(x / scale) + ox, round(y / scale) + oy


def from_screen(x, y) -> tuple:
    scale = SHOT["scale"] or 1.0
    return round((x - SHOT["origin"][0]) * scale), round((y - SHOT["origin"][1]) * scale)


MODIFIERS = {"ctrl", "control", "alt", "option", "shift", "win", "super", "cmd", "meta"}


def parse_modifiers(value) -> list:
    if not value:
        return []
    names = value.split("+") if isinstance(value, str) else list(value)
    bad = [n for n in names if str(n).strip().lower() not in MODIFIERS]
    if bad:
        raise ToolError(f"modifiers may only be ctrl, alt, shift, win (cmd on macOS); got {bad}")
    return [str(n).strip().lower() for n in names]


def settle(args: dict, result: dict) -> dict:
    """Optionally a fresh screenshot after the action, so Claude sees the effect without another call."""
    if args.get("screenshot"):
        time.sleep(min(max(float(args.get("wait_ms") or 600), 0), 5000) / 1000)
        shot = tool_screenshot({})
        result = {**result, **shot}
    return result


def keep_png(args: dict, data: bytes) -> dict:
    """save_to: the picture also becomes a file on this computer (to attach it to a web page, for example)."""
    if not args.get("save_to"):
        return {}
    path = resolve(args["save_to"])
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")
    tool_write({"path": str(path), "content": base64.b64encode(data).decode(), "encoding": "base64"})
    return {"saved_to": str(path)}


def tool_screenshot(args: dict) -> dict:
    d = need_screen()
    data, width, height, scale, origin = d.capture(None, SCREEN_EDGE)
    SHOT["scale"], SHOT["origin"] = scale, origin
    return {"width": width, "height": height, "screen": list(d.size()), "system": d.name, **keep_png(args, data),
            "note": "Coordinates for click, move, drag and scroll are pixels of this image.",
            "image": {"data": base64.b64encode(data).decode(), "mimeType": "image/png"}}


def tool_zoom(args: dict) -> dict:
    d = need_screen()
    region = args.get("region")
    if not (isinstance(region, list) and len(region) == 4):
        raise ToolError("region must be [x0, y0, x1, y1] in pixels of the latest screenshot")
    x0, y0 = to_screen(region[0], region[1])
    x1, y1 = to_screen(region[2], region[3])
    if x1 <= x0 or y1 <= y0:
        raise ToolError("region must have x1 > x0 and y1 > y0")
    data, width, height, _, _ = d.capture((x0, y0, x1, y1), SCREEN_EDGE)
    return {"width": width, "height": height, "region": region, **keep_png(args, data),
            "note": "A close-up only: keep using the coordinates of the full screenshot for actions.",
            "image": {"data": base64.b64encode(data).decode(), "mimeType": "image/png"}}


def with_modifiers(d: Desktop, mods: list, action) -> None:
    pressed = []
    try:
        for name in mods:
            d.key(name, True)
            pressed.append(name)
        action()
    finally:
        for name in reversed(pressed):
            d.key(name, False)


def tool_click(args: dict) -> dict:
    d = need_screen()
    button = args.get("button") or "left"
    if button not in ("left", "right", "middle"):
        raise ToolError("button must be left, right or middle")
    clicks = number(args.get("clicks"), 1, 1, 3)
    mods = parse_modifiers(args.get("modifiers"))
    if args.get("x") is not None or args.get("y") is not None:
        d.move(*to_screen(args.get("x"), args.get("y")))
        time.sleep(0.03)
    with_modifiers(d, mods, lambda: d.click(button, clicks))
    return settle(args, {"done": f"{button} click x{clicks}"})


def tool_move(args: dict) -> dict:
    d = need_screen()
    d.move(*to_screen(args.get("x"), args.get("y")))
    return settle(args, {"done": "moved"})


def tool_drag(args: dict) -> dict:
    d = need_screen()
    x0, y0 = to_screen(args.get("x"), args.get("y"))
    x1, y1 = to_screen(args.get("to_x"), args.get("to_y"))
    d.move(x0, y0)
    time.sleep(0.03)
    d.button("left", True)
    try:
        steps = 20
        for i in range(1, steps + 1):
            time.sleep(0.012)
            d.drag_to(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
        time.sleep(0.05)
    finally:
        d.button("left", False)
    return settle(args, {"done": "dragged"})


def tool_scroll(args: dict) -> dict:
    d = need_screen()
    direction = args.get("direction") or "down"
    if direction not in ("up", "down", "left", "right"):
        raise ToolError("direction must be up, down, left or right")
    amount = number(args.get("amount"), 3, 1, 30)
    if args.get("x") is not None or args.get("y") is not None:
        d.move(*to_screen(args.get("x"), args.get("y")))
        time.sleep(0.03)
    d.scroll(direction, amount)
    return settle(args, {"done": f"scrolled {direction} {amount}"})


def tool_type(args: dict) -> dict:
    d = need_screen()
    text = args.get("text")
    if not isinstance(text, str) or not text:
        raise ToolError("text must be a non-empty string")
    if len(text) > 5000:
        raise ToolError("At most 5000 characters at a time; for long text write a file with pc_write and open it")
    # One character at a time with a short pause, like a fast typist: modern apps (WinUI, Electron) drop characters from long bursts.
    for char in text.replace("\r\n", "\n"):
        d.type_char(char)
        time.sleep(TYPE_PAUSE)
    return settle(args, {"done": f"typed {len(text)} characters"})


def tool_key(args: dict) -> dict:
    d = need_screen()
    combo = args.get("keys")
    if not isinstance(combo, str) or not combo.strip():
        raise ToolError("keys must be a key or a combination such as ctrl+s, alt+tab, enter")
    names = [n for n in combo.replace(" ", "").split("+") if n] if combo.strip() != "+" else ["+"]
    for _ in range(number(args.get("repeat"), 1, 1, 100)):
        with_modifiers(d, names[:-1], lambda: (d.key(names[-1], True), d.key(names[-1], False)))
    return settle(args, {"done": f"pressed {combo}"})


def tool_cursor(args: dict) -> dict:
    d = need_screen()
    x, y = from_screen(*d.cursor())
    return {"x": x, "y": y}


CU_TOOLS = {"cu_screenshot": tool_screenshot, "cu_zoom": tool_zoom, "cu_click": tool_click, "cu_move": tool_move, "cu_drag": tool_drag,
            "cu_scroll": tool_scroll, "cu_type": tool_type, "cu_key": tool_key, "cu_cursor": tool_cursor}


TOOLS = {"pc_info": tool_info, "pc_run": tool_run, "pc_read": tool_read, "pc_write": tool_write, "pc_edit": tool_edit, "pc_list": tool_list,
         **CU_TOOLS}


def describe(tool: str, args: dict) -> str:
    """One line for the activity log: what was asked, never file contents or typed text."""
    if tool == "cu_type":
        return f"screen: type {len(str(args.get('text', '')))} characters"
    if tool in ("cu_screenshot", "cu_zoom") and args.get("save_to"):
        return f"screen: {tool.removeprefix('cu_')}, saved to {str(args['save_to'])[:200]}"
    if tool.startswith("cu_"):
        where = f" at {args.get('x')},{args.get('y')}" if args.get("x") is not None else ""
        extra = args.get("keys") or args.get("direction") or args.get("button") or ""
        return f"screen: {tool.removeprefix('cu_')} {extra}{where}".replace("  ", " ").strip()
    if tool == "pc_run":
        return "run: " + " ".join(str(args.get("command", "")).split())[:300]
    path = str(args.get("path", ""))[:200]
    if tool == "pc_write":
        return f"write: {path} ({len(str(args.get('content', '')))} chars{', append' if args.get('append') else ''})"
    return f"{tool.removeprefix('pc_')}: {path}"


def execute(tool: str, args: dict) -> dict:
    function = TOOLS.get(tool)
    if function is None:
        raise ToolError(f"Unknown tool {tool}")
    if paused() and tool != "pc_info":
        raise ToolError("Claude is paused on this computer by its owner. They can allow it again in the Claude Portal settings (tray icon).")
    if tool.startswith("pc_") and tool != "pc_info" and not prefs()["shell"]:
        raise ToolError("Commands and files are switched off on this computer. Its owner can switch them on in the Claude Portal settings.")
    log(describe(tool, args))
    return function(args if isinstance(args, dict) else {})


# ---------------------------------------------------------------- connection to the portal

def send_result(ws: WebSocket, cid: str, ok: bool, payload) -> None:
    body = {"type": "res", "id": cid, "ok": ok, ("result" if ok else "error"): payload}
    whole = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(whole) <= CHUNK:
        ws.send_json(body)
        return
    pieces = [whole[i:i + CHUNK] for i in range(0, len(whole), CHUNK)]
    for number_, piece in enumerate(pieces):
        ws.send_json({"type": "res_part", "id": cid, "i": number_, "n": len(pieces), "data": piece})


def answer(ws: WebSocket, command: dict, slots: threading.BoundedSemaphore) -> None:
    cid = command.get("id")
    try:
        try:
            result = execute(str(command.get("tool")), command.get("args") if isinstance(command.get("args"), dict) else {})
        except ToolError as error:
            send_result(ws, cid, False, str(error))
        except Exception as error:  # one bad command must not take the connection down
            log(f"error: {type(error).__name__}: {error}")
            send_result(ws, cid, False, f"{type(error).__name__}: {error}")
        else:
            send_result(ws, cid, True, result)
    except Exception:
        pass
    finally:
        slots.release()


def session(portal: str, token: str, stop: threading.Event) -> str:
    """One connection, from hello to the end. Returns why it ended: 'auth' (refused), 'removed' (settings deleted), 'reload' (paired again), 'stopped' or 'lost'."""
    parts = urllib.parse.urlsplit(portal)
    ws = WebSocket(("wss://" if parts.scheme == "https" else "ws://") + parts.netloc + "/ext/bridge")
    slots = threading.BoundedSemaphore(MAX_ACTIVE)
    try:
        ws.send_json({"type": "hello", "token": token, "info": {"version": VERSION, "os": platform.system(), "host": socket.gethostname(),
                                                               "screen": screen_problem() is None}})
        greeting = ws.receive(15)
        greeting = json.loads(greeting) if greeting else {}
        if greeting.get("type") == "error":
            return "auth" if greeting.get("code") == "auth" else "lost"
        if greeting.get("type") != "hello_ok":
            return "lost"
        log(f"connected to {portal} as {greeting.get('label')}")
        set_status("connected", portal)
        last_state, last_ping, last_seen, last_check = None, time.monotonic(), time.monotonic(), time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if now - last_check >= 5:
                last_check = now
                current = read_config().get("token")
                if not current:
                    return "removed"
                if current != token:
                    return "reload"
            state = {"paused": paused(), **prefs()}
            if state != last_state:
                ws.send_json({"type": "state", "state": state})
                last_state = state
            if now - last_ping >= PING_EVERY:
                ws.send_json({"type": "ping"})
                last_ping = now
            if now - last_seen > DEAD_AFTER:
                raise Closed("The portal stopped answering.")
            text = ws.receive(1.0)
            if text is None:
                continue
            last_seen = time.monotonic()
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "error":
                return "auth" if message.get("code") == "auth" else "lost"
            if kind == "update" and install_update(message):
                return "update"
            if kind == "cmd" and isinstance(message.get("id"), str):
                if not slots.acquire(blocking=False):
                    send_result(ws, message["id"], False, "This computer is busy with too many commands.")
                    continue
                threading.Thread(target=answer, args=(ws, message, slots), daemon=True).start()
        return "stopped"
    finally:
        ws.close()


def single_instance(wait: float = 0.0):
    """Holds a lock for the life of the process; None when another agent already runs. After an update the old process may still be closing."""
    handle = open(private_dir() / "agent.lock", "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            if WIN:
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.5)


def install_update(message: dict) -> bool:
    """A newer agent sent by the portal: the checksum, the version and the syntax are checked before it replaces this file (decision 226)."""
    version, digest = str(message.get("version", "")), str(message.get("sha256", ""))
    if not prefs()["update"]:
        log(f"update {version} skipped: updates are off in the settings")
        return False
    try:
        newer = tuple(int(p) for p in version.split(".")) > tuple(int(p) for p in VERSION.split("."))
        source = base64.b64decode(str(message.get("source", "")), validate=True)
    except ValueError:
        return False
    if not newer or hashlib.sha256(source).hexdigest() != digest or f'VERSION = "{version}"'.encode() not in source:
        log(f"update {version} rejected: it does not match its checksum or version")
        return False
    here = Path(__file__).resolve()
    try:
        compile(source, str(here), "exec")
        fresh = here.with_name(here.name + ".new")
        fresh.write_bytes(source)
        shutil.copy2(here, here.with_name(here.name + ".bak"))
        os.replace(fresh, here)
    except (SyntaxError, ValueError, OSError) as error:
        log(f"update {version} failed: {str(error)[:200]}")
        return False
    log(f"updated {VERSION} -> {version}; restarting")
    return True


def restart_updated(stop: threading.Event, lock) -> None:
    """Starts the new file in place of this process. On Windows the tray icon is removed first, then a new process takes the lock."""
    here = str(Path(__file__).resolve())
    if not WIN:
        lock.close()
        os.execv(sys.executable, [sys.executable, here, "run"])
    stop.set()
    time.sleep(1.5)
    lock.close()
    subprocess.Popen([sys.executable, here, "run", "--wait-lock"], creationflags=0x00000008 | 0x08000000, close_fds=True)


def cmd_run(args) -> int:
    lock = single_instance(10.0 if getattr(args, "wait_lock", False) else 0.0)
    if lock is None:
        print("The agent is already running on this computer.")
        return 0
    config = read_config()
    if not config.get("token") or not config.get("portal"):
        print("This computer is not paired yet. Run: portal_agent.py setup --portal https://YOUR-PORTAL")
        return 1
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    dpi_aware()
    try:
        start_panel()
    except OSError as error:
        log(f"settings window unavailable: {error}")
    start_tray(stop)
    delay = 1.0
    log(f"agent {VERSION} started")
    while not stop.is_set():
        began = time.monotonic()
        try:
            why = session(config["portal"], config["token"], stop)
        except (OSError, Closed, ssl.SSLError, ValueError) as error:
            why = "lost"
            set_status("offline", str(error)[:200])
            log(f"connection failed: {str(error)[:200]}")
        if why == "removed":
            log("settings removed: the agent stops")
            break
        if why == "update":
            restart_updated(stop, lock)
            return 0
        if why == "reload":
            log("new settings: reconnecting")
            config = read_config()
            delay = 1.0
            continue
        if why == "auth":
            set_status("signed_out", "The portal no longer accepts this computer. Run setup again.")
            log("the portal refused the token: this computer was disconnected or the token expired; run setup again")
            stop.wait(600)
            config = read_config()
            continue
        if stop.is_set():
            break
        delay = 1.0 if time.monotonic() - began > 60 else min(delay * 2, 60.0)
        set_status("offline", "reconnecting")
        stop.wait(delay + random.random())
    set_status("stopped")
    return 0


# ---------------------------------------------------------------- the settings window: a local page, opened from the tray or the Start menu

PANEL = {}  # {"url": ..., "server": ...} while the agent runs


def autostart_on() -> bool:
    if WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "ClaudePortalAgent")
            return True
        except OSError:
            return False
    if MAC:
        return (Path.home() / "Library" / "LaunchAgents" / "app.claude-portal.agent.plist").exists()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (base / "systemd" / "user" / "claude-portal-agent.service").exists() or (base / "autostart" / "claude-portal-agent.desktop").exists()


def set_autostart(on: bool) -> None:
    if WIN:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if on:
                winreg.SetValueEx(key, "ClaudePortalAgent", 0, winreg.REG_SZ, f'"{interpreter()}" "{script_path()}" run')
            else:
                try:
                    winreg.DeleteValue(key, "ClaudePortalAgent")
                except OSError:
                    pass
    elif on:
        (install_mac if MAC else install_linux)(interpreter(), script_path(), start=False)
    else:
        remove_autostart()
    log(f"setting: autostart {'on' if on else 'off'}")


def log_tail(lines: int = 40) -> list:
    try:
        with open(data_dir() / "activity.log", encoding="utf-8", errors="replace") as stream:
            return [line.rstrip("\n") for line in stream.readlines()[-lines:]][::-1]
    except OSError:
        return []


def panel_state() -> dict:
    config = read_config()
    try:
        status = json.loads((data_dir() / "status.json").read_text())
    except (OSError, ValueError):
        status = {}
    return {"version": VERSION, "portal": config.get("portal", ""), "email": config.get("email", ""), "label": config.get("label", ""),
            "paired": bool(config.get("token")), "status": status.get("state", "stopped"), "detail": status.get("detail", ""),
            "paused": paused(), "prefs": prefs(), "autostart": autostart_on(), "screen_problem": screen_problem(), "can_grant": MAC,
            "acting": time.monotonic() - LAST_ACTION[0] < 5, "system": platform.system(), "log": log_tail()}


PANEL_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Portal</title><style>
:root{color-scheme:light dark;--bg:#f6f5f2;--card:#fff;--text:#1f1e1c;--muted:#6b6862;--line:#e3e0da;--accent:#d97757;--ok:#2f9e6b;--bad:#c94a3c;--warn:#c99a2e}
@media(prefers-color-scheme:dark){:root{--bg:#1c1b19;--card:#262522;--text:#ecebe7;--muted:#a19d95;--line:#3a3834}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
main{max-width:620px;margin:0 auto;padding:24px 16px 48px;display:flex;flex-direction:column;gap:16px}
h1{font-size:22px;margin:0;font-weight:600}h2{font-size:15px;margin:0 0 4px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.status{display:flex;align-items:center;gap:12px}.dot{width:12px;height:12px;border-radius:50%;background:var(--muted);flex:none}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}.dot.warn{background:var(--warn)}.dot.act{background:#3b82f6}
.muted{color:var(--muted);font-size:13px}.row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.row p{margin:2px 0 0;color:var(--muted);font-size:13px}
.switch{position:relative;width:48px;height:28px;flex:none}.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--line);border-radius:28px;transition:.2s;cursor:pointer}
.slider:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 3px #0003}
input:checked+.slider{background:var(--accent)}input:checked+.slider:before{transform:translateX(20px)}input:disabled+.slider{opacity:.45;cursor:default}
input:focus-visible+.slider{outline:2px solid var(--accent);outline-offset:2px}
.stop{width:100%;padding:14px;border:0;border-radius:12px;background:var(--bad);color:#fff;font:600 16px inherit;cursor:pointer}
.stop.go{background:var(--ok)}button.link{background:none;border:0;color:var(--bad);font:inherit;cursor:pointer;padding:0;text-decoration:underline}
ol{margin:0;padding-left:0;list-style:none;max-height:260px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace;color:var(--muted)}
.banner{background:#3b82f61a;border:1px solid #3b82f655;border-radius:12px;padding:10px 14px}
</style></head><body><main>
<div class="status"><span class="dot" id="dot"></span><div><h1>Claude Portal</h1><div class="muted" id="state">…</div></div></div>
<div class="banner" id="acting" hidden>Claude сейчас управляет экраном. Остановить: кнопка «Стоп» ниже.</div>
<button class="stop" id="stop" type="button">Стоп: запретить Claude всё на этом компьютере</button>
<div class="card">
<div class="row"><div><h2>Claude может работать на этом компьютере</h2><p>Главный выключатель. Когда он выключен, Claude ничего здесь не делает.</p></div>
<label class="switch"><input type="checkbox" id="work"><span class="slider"></span></label></div>
<div class="row"><div><h2>Управление экраном</h2><p id="screen-note">Снимки экрана, мышь и клавиатура. Claude видит то же, что вы, и может нажимать и печатать.</p></div>
<label class="switch"><input type="checkbox" id="screen"><span class="slider"></span></label></div>
<div class="banner" id="screen-problem" hidden><span id="screen-problem-text"></span> <button class="link" id="grant" type="button" hidden style="color:inherit">Открыть разрешения macOS</button></div>
<div class="row"><div><h2>Команды и файлы</h2><p>Запуск команд, чтение, создание и изменение файлов с правами вашего пользователя.</p></div>
<label class="switch"><input type="checkbox" id="shell"><span class="slider"></span></label></div>
<div class="row"><div><h2>Запускать при входе в систему</h2><p>Иначе Claude будет доступен, только пока вы сами не запустите приложение.</p></div>
<label class="switch"><input type="checkbox" id="autostart"><span class="slider"></span></label></div>
<div class="row"><div><h2>Обновляться автоматически</h2><p>Новая версия приходит с портала при подключении; файл проверяется по контрольной сумме, старый сохраняется рядом (.bak).</p></div>
<label class="switch"><input type="checkbox" id="update"><span class="slider"></span></label></div>
</div>
<div class="card"><h2>VPN через сервер</h2><p class="muted">Скоро: интернет через сервер портала (Tailscale) одной кнопкой.</p></div>
<div class="card"><h2>Что делал Claude</h2><p class="muted">Последние действия. Текст, который Claude печатает, и содержимое файлов сюда не пишутся.</p><ol id="log"></ol></div>
<p class="muted" id="who"></p>
<p class="muted"><button class="link" id="forget" type="button">Отключить этот компьютер от портала</button></p>
</main><script>
var busy=false;
function q(id){return document.getElementById(id)}
function post(path,body){return fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Panel":"1"},body:JSON.stringify(body||{})}).then(function(r){if(!r.ok)throw new Error(r.status);return r.json()}).then(draw)}
function draw(s){
 var on=!s.paused,state={connected:"Подключено к порталу",offline:"Нет связи с порталом, пробую снова",signed_out:"Компьютер отключён от портала. Установите заново из вкладки Computers.",stopped:"Приложение не запущено"}[s.status]||s.status;
 q("dot").className="dot "+(s.acting?"act":s.status!=="connected"?"bad":s.paused?"warn":"ok");
 q("state").textContent=(s.paused&&s.status==="connected"?"На паузе: Claude здесь ничего не делает":state);
 q("acting").hidden=!s.acting;
 q("work").checked=on;q("screen").checked=s.prefs.screen;q("shell").checked=s.prefs.shell;q("autostart").checked=s.autostart;q("update").checked=s.prefs.update;
 q("screen-problem").hidden=!s.screen_problem;q("screen-problem-text").textContent=s.screen_problem||"";q("grant").hidden=!(s.can_grant&&s.screen_problem);
 q("stop").textContent=on?"Стоп: запретить Claude всё на этом компьютере":"Разрешить Claude снова работать здесь";q("stop").className="stop"+(on?"":" go");
 q("who").textContent=(s.email?"Аккаунт: "+s.email+" · ":"")+(s.label?"Компьютер: "+s.label+" · ":"")+"Версия "+s.version;
 var log=q("log");log.textContent="";s.log.forEach(function(l){var li=document.createElement("li");li.textContent=l;log.appendChild(li)});
}
function refresh(){if(busy)return;fetch("/api/state").then(function(r){return r.json()}).then(draw).catch(function(){q("state").textContent="Приложение закрыто";q("dot").className="dot bad"})}
["work","screen","shell","autostart","update"].forEach(function(id){q(id).addEventListener("change",function(){busy=true;post("/api/set",{name:id,value:q(id).checked}).catch(function(e){alert("Не получилось: "+e.message)}).then(function(){busy=false;refresh()})})});
q("stop").addEventListener("click",function(){post("/api/set",{name:"work",value:q("stop").className.indexOf("go")>=0})});
q("grant").addEventListener("click",function(){post("/api/permissions")});
q("forget").addEventListener("click",function(){if(confirm("Отключить этот компьютер? Claude больше не сможет здесь работать, пока вы не установите приложение снова."))post("/api/forget").then(function(){q("state").textContent="Отключено. Окно можно закрыть."})});
refresh();setInterval(refresh,2000);
</script></body></html>"""


def panel_handler():
    from http.server import BaseHTTPRequestHandler
    from http.cookies import SimpleCookie
    import hmac

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudePortal"

        def log_message(self, *args):
            pass

        def send(self, code: int, body: bytes, kind: str = "application/json", extra=None) -> None:
            self.send_response(code)
            for key, value in {"Content-Type": kind, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                               "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                               "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                                          "connect-src 'self'; frame-ancestors 'none'", **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self) -> bool:
            return self.headers.get("Host") == self.server.host  # a foreign page pointing a DNS name at 127.0.0.1 gets nothing

        def authed(self) -> bool:
            try:
                jar = SimpleCookie(self.headers.get("Cookie") or "")
                return hmac.compare_digest(jar["cp_panel"].value, self.server.token)
            except (KeyError, Exception):
                return False

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, b"forbidden", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                given = urllib.parse.parse_qs(url.query).get("t", [""])[0]
                if given and hmac.compare_digest(given, self.server.token):
                    return self.send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": f"cp_panel={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
                if self.authed():
                    return self.send(200, PANEL_HTML.encode(), "text/html; charset=utf-8")
                return self.send(403, "Откройте настройки через значок Claude в трее или через меню «Пуск».".encode(), "text/plain; charset=utf-8")
            if url.path == "/api/state" and self.authed():
                return self.send(200, json.dumps(panel_state(), ensure_ascii=False).encode())
            return self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if (not self.host_ok() or not self.authed() or self.headers.get("X-Panel") != "1"
                    or self.headers.get("Origin") not in (None, "http://" + self.server.host)):
                return self.send(403, b"forbidden", "text/plain")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            except ValueError:
                return self.send(400, b"bad json", "text/plain")
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/api/set":
                    name, value = body.get("name"), bool(body.get("value"))
                    if name == "work":
                        set_paused(not value)
                    elif name == "autostart":
                        set_autostart(value)
                    else:
                        set_pref(name, value)
                elif path == "/api/forget":
                    forget()
                elif path == "/api/permissions" and MAC:
                    desktop().request()
                else:
                    return self.send(404, b"not found", "text/plain")
            except (ToolError, OSError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode())
            return self.send(200, json.dumps(panel_state(), ensure_ascii=False).encode())

    return Handler


def start_panel() -> str:
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), panel_handler())
    server.daemon_threads = True
    server.host = f"127.0.0.1:{server.server_address[1]}"
    server.token = secrets.token_urlsafe(24)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{server.host}/?t={server.token}"
    PANEL.update(url=url, server=server)
    path = private_dir() / "panel.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"url": url, "pid": os.getpid()}, stream)
    return url


def open_panel() -> None:
    if PANEL.get("url"):
        webbrowser.open(PANEL["url"])


def forget() -> None:
    """Disconnect from the portal from the settings window: the token is revoked, autostart removed, the agent stops."""
    config = read_config()
    if config.get("token") and config.get("portal"):
        try:
            http_json(config["portal"] + "/ext/revoke", {"token": config["token"]})
        except SystemExit:
            pass
    remove_autostart()
    (data_dir() / "config.json").unlink(missing_ok=True)  # the running agent sees this within seconds and stops
    log("disconnected from the portal by the user")


MAC_JOB = "app.claude-portal.agent"


def start_agent() -> None:
    """On a Mac the agent must run as the launchd job, not as a child of Terminal: macOS ties the screen permissions to the program that
    started it, and a copy started from Terminal borrows Terminal's, which are gone after a reboot (owner's Mac, 17 Nov 2026)."""
    if MAC and (Path.home() / "Library" / "LaunchAgents" / f"{MAC_JOB}.plist").exists():
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{MAC_JOB}"], capture_output=True, timeout=30)
        return
    options = {"creationflags": 0x00000008 | 0x08000000} if WIN else {"start_new_session": True}
    subprocess.Popen([interpreter(), str(script_path()), "run"], close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def cmd_settings(args) -> int:
    """Open the settings window of the running agent; start the agent first when it is not running."""
    path = data_dir() / "panel.json"
    for attempt in range(2):
        try:
            info = json.loads(path.read_text())
            with DIRECT.open(info["url"].split("?")[0] + "api/state", timeout=2):
                pass
        except urllib.error.HTTPError:
            webbrowser.open(info["url"])  # 403 without the cookie means the agent answers
            return 0
        except (OSError, ValueError, KeyError):
            if attempt == 0:
                path.unlink(missing_ok=True)
                start_agent()
                for _ in range(60):
                    time.sleep(0.25)
                    if path.exists():
                        break
            continue
        webbrowser.open(info["url"])
        return 0
    print("The agent did not start. Run: portal_agent.py run")
    return 1


# ---------------------------------------------------------------- the tray icon (Windows)

APP_ID = "ClaudePortal.Agent"


def app_identity() -> None:
    """Windows names a balloon by the process's app id: without one the notifications say "Python". The name is registered per user."""
    import ctypes
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Claude Portal")
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except OSError:
        pass


def tray_loop(stop: threading.Event) -> None:
    import ctypes
    from ctypes import wintypes as w
    app_identity()
    user32, shell32, gdi32, kernel32 = (ctypes.WinDLL(n, use_last_error=True) for n in ("user32", "shell32", "gdi32", "kernel32"))
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                    ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD), ("guidItem", ctypes.c_ubyte * 16),
                    ("hBalloonIcon", w.HICON)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD), ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                    ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                    ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

    user32.DefWindowProcW.argtypes, user32.DefWindowProcW.restype = [w.HWND, w.UINT, w.WPARAM, w.LPARAM], LRESULT
    user32.RegisterClassW.argtypes, user32.RegisterClassW.restype = [ctypes.POINTER(WNDCLASSW)], w.ATOM
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.GetMessageW.argtypes, user32.GetMessageW.restype = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes, user32.DispatchMessageW.restype = [ctypes.POINTER(w.MSG)], LRESULT
    user32.SetTimer.argtypes, user32.SetTimer.restype = [w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p], ctypes.c_size_t
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.CreatePopupMenu.restype = w.HMENU
    user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p]
    user32.DestroyMenu.argtypes = [w.HMENU]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    user32.CreateIconIndirect.argtypes, user32.CreateIconIndirect.restype = [ctypes.POINTER(ICONINFO)], w.HICON
    user32.DestroyIcon.argtypes = [w.HICON]
    user32.DestroyWindow.argtypes = [w.HWND]
    user32.RegisterWindowMessageW.argtypes, user32.RegisterWindowMessageW.restype = [w.LPCWSTR], w.UINT
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BIH), w.UINT, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
    gdi32.CreateDIBSection.restype = w.HBITMAP
    gdi32.CreateBitmap.argtypes, gdi32.CreateBitmap.restype = [ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, ctypes.c_void_p], w.HBITMAP
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    kernel32.GetModuleHandleW.argtypes, kernel32.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE

    def make_icon(rgb, ring=False):
        size = 32
        bits = ctypes.c_void_p()
        color = gdi32.CreateDIBSection(None, ctypes.byref(BIH(40, size, -size, 1, 32, 0, 0, 0, 0, 0, 0)), 0, ctypes.byref(bits), None, 0)
        pixels = bytearray(size * size * 4)
        for y in range(size):
            for x in range(size):
                d = ((x - 15.5) ** 2 + (y - 15.5) ** 2) ** 0.5
                alpha = max(0.0, min(1.0, 14.5 - d))
                if ring:
                    alpha *= max(0.0, min(1.0, d - 9.0))
                i = (y * size + x) * 4
                pixels[i:i + 4] = bytes((rgb[2], rgb[1], rgb[0], round(alpha * 255)))
        ctypes.memmove(bits, bytes(pixels), len(pixels))
        mask = gdi32.CreateBitmap(size, size, 1, 1, bytes(size * size // 8))
        handle = user32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
        gdi32.DeleteObject(mask)
        gdi32.DeleteObject(color)
        return handle

    icons = {"ok": make_icon((217, 119, 87)), "paused": make_icon((150, 146, 140)), "acting": make_icon((59, 130, 246)),
             "offline": make_icon((201, 74, 60), ring=True)}
    CALLBACK, TIMER, MENU_OPEN, MENU_PAUSE, MENU_SCREEN, MENU_QUIT = 0x8001, 1, 1, 2, 3, 4
    recreated = user32.RegisterWindowMessageW("TaskbarCreated")
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    data.uID = 1
    data.uCallbackMessage = CALLBACK
    seen = {"key": None, "busy_since": 0.0}

    def look():
        try:
            state = json.loads((data_dir() / "status.json").read_text()).get("state")
        except (OSError, ValueError):
            state = None
        if time.monotonic() - LAST_ACTION[0] < 5:
            return "acting", "Claude управляет экраном"
        if state != "connected":
            return "offline", "Claude Portal: нет связи с порталом"
        if paused():
            return "paused", "Claude Portal: на паузе"
        return "ok", "Claude Portal: Claude может работать здесь"

    def show(add=False, balloon=None):
        key, tip = look()
        data.uFlags = 0x1 | 0x2 | 0x4 | (0x10 if balloon else 0)
        data.hIcon = icons[key]
        data.szTip = tip[:127]
        if balloon:
            data.szInfoTitle, data.szInfo, data.dwInfoFlags = balloon[0][:63], balloon[1][:255], 0x1
        shell32.Shell_NotifyIconW(0 if add else 1, ctypes.byref(data))
        seen["key"] = key

    def menu(hwnd):
        popup = user32.CreatePopupMenu()
        user32.AppendMenuW(popup, 0, MENU_OPEN, "Открыть настройки")
        user32.AppendMenuW(popup, 0x800, 0, None)
        user32.AppendMenuW(popup, 0x8 if paused() else 0, MENU_PAUSE, "Пауза: Claude здесь ничего не делает")
        user32.AppendMenuW(popup, (0x8 if prefs()["screen"] else 0), MENU_SCREEN, "Разрешить управление экраном")
        user32.AppendMenuW(popup, 0x800, 0, None)
        user32.AppendMenuW(popup, 0, MENU_QUIT, "Закрыть до следующего входа")
        point = w.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        choice = user32.TrackPopupMenu(popup, 0x100 | 0x2, point.x, point.y, 0, hwnd, None)
        user32.DestroyMenu(popup)
        if choice == MENU_OPEN:
            open_panel()
        elif choice == MENU_PAUSE:
            set_paused(not paused())
        elif choice == MENU_SCREEN:
            set_pref("screen", not prefs()["screen"])
        elif choice == MENU_QUIT:
            log("closed from the tray")
            stop.set()
        show()

    @WNDPROC
    def proc(hwnd, message, wparam, lparam):
        try:
            if message == CALLBACK:
                if lparam == 0x0202:
                    open_panel()
                elif lparam in (0x0205, 0x007B):
                    menu(hwnd)
                return 0
            if message == 0x0113:  # WM_TIMER
                if stop.is_set():
                    user32.DestroyWindow(hwnd)
                    return 0
                key, _ = look()
                if key == "acting" and seen["key"] != "acting" and time.monotonic() - seen["busy_since"] > 60:
                    show(balloon=("Claude управляет экраном", "Остановить: значок Claude в трее, затем «Пауза»."))
                elif key != seen["key"]:
                    show()
                if key == "acting":
                    seen["busy_since"] = time.monotonic()
                return 0
            if message == recreated:
                show(add=True)
                return 0
            if message == 0x0002:  # WM_DESTROY
                shell32.Shell_NotifyIconW(2, ctypes.byref(data))
                user32.PostQuitMessage(0)
                return 0
        except Exception as error:  # a broken tray must not take the agent down
            log(f"tray error: {type(error).__name__}: {error}")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    klass = WNDCLASSW(0, proc, 0, 0, instance, None, None, None, None, "ClaudePortalTray")
    user32.RegisterClassW(ctypes.byref(klass))
    hwnd = user32.CreateWindowExW(0, "ClaudePortalTray", "Claude Portal", 0, 0, 0, 0, 0, None, None, instance, None)
    if not hwnd:
        log("tray: the window could not be created")
        return
    data.hWnd = hwnd
    show(add=True)
    user32.SetTimer(hwnd, TIMER, 700, None)
    message = w.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    for handle in icons.values():
        user32.DestroyIcon(handle)
    _WIN["tray_proc"] = proc


def notify(title: str, text: str) -> None:
    """A system notification on Linux and macOS (Windows uses the tray balloon)."""
    try:
        if MAC:
            script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Claude Portal", title, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def start_watcher(stop: threading.Event) -> None:
    """Linux and macOS have no tray here: a notification tells the user when Claude starts using the screen."""
    def run():
        idle_since = time.monotonic()
        while not stop.wait(0.7):
            if time.monotonic() - LAST_ACTION[0] < 5:
                if time.monotonic() - idle_since > 60:
                    notify("Claude управляет экраном", "Остановить: откройте Claude Portal и нажмите «Стоп».")
                idle_since = time.monotonic()

    threading.Thread(target=run, name="watcher", daemon=True).start()


def start_tray(stop: threading.Event) -> None:
    if not WIN:
        start_watcher(stop)
        return

    def run():
        try:
            tray_loop(stop)
        except Exception as error:
            log(f"tray error: {type(error).__name__}: {error}")

    threading.Thread(target=run, name="tray", daemon=True).start()


# ---------------------------------------------------------------- pairing, autostart

# Straight to the portal, like the WebSocket: a VPN on macOS or Windows often sets a system proxy that urllib would follow and fail on
# ("nodename nor servname provided" on a Mac whose own DNS finds the portal fine, 17 Nov 2026).
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": f"claude-portal-agent/{VERSION}"})
    try:
        with DIRECT.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise SystemExit(f"The portal answered {error.code} for {url}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SystemExit(f"Could not reach the portal ({getattr(error, 'reason', error)}). Check the internet connection and the address.")


def default_label() -> str:
    system = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(platform.system(), platform.system())
    return f"{system} {socket.gethostname()}"[:60]


def cmd_pair(args) -> dict:
    portal = portal_origin(args.portal)
    label = (args.label or default_label()).strip()[:60]
    info = http_json(portal + "/ext/device", {"label": label, "kind": "pc"})
    link = info["verification_uri"] + "?" + urllib.parse.urlencode({"code": info["user_code"]})
    print("\n  Approve this computer in your browser.")
    print(f"  Code: {info['user_code']}")
    print(f"  Page: {link}\n")
    if not args.no_browser and (WIN or MAC or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        try:
            webbrowser.open(link)
        except Exception:
            pass
    deadline = time.time() + info.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(max(2, int(info.get("interval", 3))))
        result = http_json(portal + "/ext/poll", {"device_code": info["device_code"]})
        if result.get("status") == "ok":
            config = {"portal": portal, "token": result["token"], "email": result.get("email", ""), "label": label}
            write_config(config)
            log(f"paired with {portal} as {config['email']}")
            print(f"  Approved. This computer is connected as {label}.")
            return config
        if result.get("status") == "expired":
            break
    raise SystemExit("The code expired before it was approved. Run the setup again.")


def interpreter() -> str:
    exe = sys.executable
    if WIN and exe.lower().endswith("python.exe"):
        windowless = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(windowless):
            return windowless
    return exe


def script_path() -> Path:
    here = Path(__file__).resolve()
    target = private_dir() / "portal_agent.py"
    if not target.exists() or here != target.resolve():
        shutil.copyfile(here, target)
    return target


def start_menu() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Claude Portal"


def install_windows(python: str, script: Path) -> None:
    import winreg
    command = f'"{python}" "{script}" run'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        winreg.SetValueEx(key, "ClaudePortalAgent", 0, winreg.REG_SZ, command)
    shown = python[:-len("pythonw.exe")] + "python.exe" if python.lower().endswith("pythonw.exe") else python
    folder = start_menu()
    folder.mkdir(parents=True, exist_ok=True)
    for old in ("Pause Claude", "Resume Claude", "Claude agent status"):
        (folder / f"{old}.cmd").unlink(missing_ok=True)
    (folder / "Remove Claude agent.cmd").write_text(f'@echo off\r\n"{shown}" "{script}" uninstall\r\npause\r\n', encoding="ascii", errors="replace")
    link = str(folder / "Claude Portal.lnk").replace("'", "''")
    shortcut = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');$s.TargetPath='{python.replace(chr(39), chr(39) * 2)}';"
                f"$s.Arguments='\"{str(script).replace(chr(39), chr(39) * 2)}\" settings';$s.Description='Claude Portal: settings';$s.Save()")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", shortcut], capture_output=True, timeout=60,
                   creationflags=0x08000000)
    if not paired_agent_running():
        subprocess.Popen([python, str(script), "run"], creationflags=0x00000008 | 0x08000000, close_fds=True)


def paired_agent_running() -> bool:
    try:
        info = json.loads((data_dir() / "panel.json").read_text())
        DIRECT.open(info["url"].split("?")[0] + "api/state", timeout=2)
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError, KeyError):
        return False
    return True


def mac_app() -> Path:
    return Path.home() / "Applications" / "Claude Portal.app"


def linux_launcher() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications" / "claude-portal.desktop"


def install_launcher(python: str, script: Path) -> None:
    """Claude Portal in the app list (Launchpad and Spotlight on macOS, the applications menu on Linux): it opens the settings."""
    if MAC:
        contents = mac_app() / "Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
            '<key>CFBundleName</key><string>Claude Portal</string><key>CFBundleDisplayName</key><string>Claude Portal</string>'
            '<key>CFBundleIdentifier</key><string>app.claude-portal.settings</string><key>CFBundleExecutable</key><string>claude-portal</string>'
            '<key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>' + VERSION + '</string>'
            '</dict></plist>\n', encoding="utf-8")
        launcher = contents / "MacOS" / "claude-portal"
        launcher.write_text(f'#!/bin/sh\nexec "{python}" "{script}" settings\n', encoding="utf-8")
        os.chmod(launcher, 0o755)
    elif not WIN:
        path = linux_launcher()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[Desktop Entry]\nType=Application\nName=Claude Portal\nComment=Settings of Claude on this computer\n'
                        f'Exec="{python}" "{script}" settings\nIcon=preferences-system\nTerminal=false\nCategories=Utility;Settings;\n',
                        encoding="utf-8")


def install_mac(python: str, script: Path, start: bool = True) -> None:
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / "app.claude-portal.agent.plist"
    plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                     f'<plist version="1.0"><dict><key>Label</key><string>app.claude-portal.agent</string><key>ProgramArguments</key><array>'
                     f'<string>{python}</string><string>{script}</string><string>run</string></array><key>RunAtLoad</key><true/>'
                     '<key>KeepAlive</key><true/></dict></plist>\n', encoding="utf-8")
    if start:
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)


def systemd_user() -> bool:
    try:
        return shutil.which("systemctl") is not None and subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_linux(python: str, script: Path, start: bool = True) -> None:
    # In a desktop session the agent starts with the session (it needs DISPLAY for the screen); on a machine without one, systemd.
    if systemd_user() and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
        unit.mkdir(parents=True, exist_ok=True)
        (unit / "claude-portal-agent.service").write_text(
            f"[Unit]\nDescription=Claude Portal agent\nAfter=network-online.target\n\n[Service]\nExecStart=\"{python}\" \"{script}\" run\nRestart=always\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable"] + (["--now"] if start else []) + ["claude-portal-agent.service"], capture_output=True)
        return
    autostart = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
    autostart.mkdir(parents=True, exist_ok=True)
    (autostart / "claude-portal-agent.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Claude Portal agent\nExec=\"{python}\" \"{script}\" run\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    if start:
        subprocess.Popen([python, str(script), "run"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_install(args) -> None:
    script = script_path()
    python = interpreter()
    if WIN:
        install_windows(python, script)
    elif MAC:
        install_mac(python, script)
    else:
        install_linux(python, script)
    install_launcher(python, script)
    log("autostart installed")
    print("  Claude will start with this computer and is running now.")
    print(f"  Activity log: {data_dir() / 'activity.log'}")


def cmd_setup(args) -> int:
    config = read_config()
    if config.get("token") and config.get("portal") == portal_origin(args.portal) and not args.repair:
        print(f"  This computer is already connected as {config.get('label') or 'this computer'}; updating the app.")
    else:
        cmd_pair(args)
    cmd_install(args)
    if not args.no_browser:
        cmd_settings(args)
    return 0


def remove_autostart(stop_running: bool = False) -> None:
    if WIN:
        set_autostart(False)
    elif MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / "app.claude-portal.agent.plist"
        if stop_running:
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if systemd_user():
            subprocess.run(["systemctl", "--user", "disable"] + (["--now"] if stop_running else []) + ["claude-portal-agent.service"],
                           capture_output=True)
        (base / "systemd" / "user" / "claude-portal-agent.service").unlink(missing_ok=True)
        (base / "autostart" / "claude-portal-agent.desktop").unlink(missing_ok=True)


def cmd_uninstall(args) -> int:
    config = read_config()
    if config.get("token") and config.get("portal"):
        try:
            http_json(config["portal"] + "/ext/revoke", {"token": config["token"]})
        except SystemExit as error:
            print(f"  Could not tell the portal ({error}); disconnect this computer in the Computers tab of the portal.")
    remove_autostart(stop_running=True)
    if WIN:
        shutil.rmtree(start_menu(), ignore_errors=True)
    elif MAC:
        shutil.rmtree(mac_app(), ignore_errors=True)
    else:
        linux_launcher().unlink(missing_ok=True)
    (data_dir() / "config.json").unlink(missing_ok=True)  # a running agent sees this within seconds and exits
    log("uninstalled")
    print(f"  Removed. You can delete the folder {data_dir()}.")
    return 0


def cmd_pause(args) -> int:
    private_dir()
    (data_dir() / "paused").write_text("paused\n")
    log("paused by the user")
    print("  Claude is paused on this computer.")
    return 0


def cmd_resume(args) -> int:
    (data_dir() / "paused").unlink(missing_ok=True)
    log("resumed by the user")
    print("  Claude may work on this computer again.")
    return 0


def cmd_status(args) -> int:
    config = read_config()
    try:
        state = json.loads((data_dir() / "status.json").read_text())
    except (OSError, ValueError):
        state = {}
    print(f"  Portal: {config.get('portal') or 'not paired'}  Account: {config.get('email') or '-'}")
    print(f"  Connection: {state.get('state', 'not running')} {state.get('detail', '')}".rstrip())
    print(f"  Paused: {'yes' if paused() else 'no'}  Agent {VERSION}")
    try:
        print("  Last activity:")
        for line in (data_dir() / "activity.log").read_text(encoding="utf-8").splitlines()[-8:]:
            print("   ", line)
    except OSError:
        pass
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # the pairing code must appear at once, also when the output is a pipe or a file
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True, errors="replace")
    parser = argparse.ArgumentParser(prog="portal_agent.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "pair"):
        item = sub.add_parser(name)
        item.add_argument("--portal", required=True)
        item.add_argument("--label", default="")
        item.add_argument("--no-browser", action="store_true")
        item.add_argument("--repair", action="store_true", help="pair again even when this computer is already connected")
    sub.add_parser("run").add_argument("--wait-lock", action="store_true", help="wait for an agent that is closing after an update")
    for name in ("pause", "resume", "status", "uninstall", "install", "settings"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    handlers = {"setup": cmd_setup, "pair": lambda a: (cmd_pair(a), 0)[1], "install": lambda a: (cmd_install(a), 0)[1], "run": cmd_run,
                "pause": cmd_pause, "resume": cmd_resume, "status": cmd_status, "uninstall": cmd_uninstall, "settings": cmd_settings}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
