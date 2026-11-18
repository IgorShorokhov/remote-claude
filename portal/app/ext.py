"""Sign-in for the VS Code extension, and the folder it reports to the agent.

Google sign-in cannot run inside a VS Code webview, so the extension gets its own door:
1. the extension asks for a device code and shows its short user code;
2. the owner opens /ext/authorize in a browser that is already signed in (Google, as for the site) and approves it;
3. the extension receives a long token and keeps it in VS Code's secret storage;
4. for each view it trades the token for a one-time ticket, the embedded page opens /ext/enter?ticket=... and gets a signed cookie;
5. Caddy asks /ext/verify about that cookie instead of oauth2-proxy and then applies the usual roles.
Everything after the cookie is the normal portal, for the same whitelisted account.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import config, ownership
from .history import UUID_RE
from .roles import normalize_email

COOKIE = "portal_ext"
PENDING_TTL = 600
PENDING_MAX = 20
TICKET_TTL = 60
TICKET_MAX = 200
TOKEN_TTL = 90 * 86400
COOKIE_TTL = 30 * 86400
TOUCH_EVERY = 3600
CONTEXT_TTL = 30 * 60
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_RE = re.compile(r"rcx_([0-9a-f]{8})\.([A-Za-z0-9_-]{43})")
CODE_RE = re.compile(r"[A-Z2-9]{4}-[A-Z2-9]{4}")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
FOLDERS_MAX = 5
PATH_MAX = 1024


def directory() -> Path:
    return config.HOME / ".local/state/agent-portal/ext"


def context_directory() -> Path:
    return Path(os.environ.get("PORTAL_EXT_CONTEXT", "/run/agent-portal/vscode"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def private_directory(path: Path) -> None:
    # The service runs with UMask=0117, which strips the search bit from mkdir(mode=0o700): set the mode explicitly.
    missing = []
    for parent in (path, *path.parents):
        if parent.exists():
            break
        missing.append(parent)
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)
    os.chmod(path, 0o700)


def write_private(path: Path, data: bytes) -> None:
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


class Tokens:
    """Long tokens kept as hashes in one file; the signing key lives next to it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stamp = None
        self.table = {}

    def file(self) -> Path:
        return directory() / "tokens.json"

    def load(self) -> dict:
        path = self.file()
        try:
            try:
                st = path.stat()
            except PermissionError:
                os.chmod(path.parent, 0o700)  # a directory left without its search bit would hide every token
                st = path.stat()
        except OSError:
            self.stamp, self.table = None, {}
            return self.table
        stamp = (str(path), st.st_mtime_ns, st.st_size)
        if stamp != self.stamp:
            try:
                data = json.loads(path.read_text())
                self.table = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.table = {}
            self.stamp = stamp
        return self.table

    def save(self, table: dict) -> None:
        write_private(self.file(), json.dumps(table).encode())
        self.stamp = None

    def key(self) -> bytes:
        path = directory() / "key"
        try:
            raw = path.read_bytes()
            if len(raw) == 32:
                return raw
        except FileNotFoundError:
            pass
        except PermissionError:
            # A directory left without its search bit must not make the portal replace the key and sign every session out.
            os.chmod(path.parent, 0o700)
            raw = path.read_bytes()
            if len(raw) == 32:
                return raw
        raw = secrets.token_bytes(32)
        write_private(path, raw)
        return raw

    def add(self, email: str, label: str, kind: str = "vscode") -> str:
        with self.lock:
            table = dict(self.load())
            now = time.time()
            table = {k: v for k, v in table.items() if v.get("expires", 0) > now}
            tid = secrets.token_hex(4)
            while tid in table:
                tid = secrets.token_hex(4)
            secret = b64(secrets.token_bytes(32))
            table[tid] = {"hash": digest(secret), "email": normalize_email(email), "label": label, "kind": kind, "created": now, "used": now, "expires": now + TOKEN_TTL}
            self.save(table)
            return f"rcx_{tid}.{secret}"

    def find(self, token: str):
        match = TOKEN_RE.fullmatch(token) if isinstance(token, str) else None
        if not match:
            return None
        tid, secret = match.groups()
        row = self.load().get(tid)
        if not row or row.get("expires", 0) <= time.time() or not hmac.compare_digest(row.get("hash", ""), digest(secret)):
            return None
        return tid, row

    def row(self, tid: str):
        row = self.load().get(tid)
        return row if row and row.get("expires", 0) > time.time() else None

    def touch(self, tid: str) -> None:
        with self.lock:
            table = dict(self.load())
            row = table.get(tid)
            now = time.time()
            if not row or now - row.get("used", 0) < TOUCH_EVERY:
                return
            table[tid] = {**row, "used": now, "expires": now + TOKEN_TTL}
            self.save(table)

    def revoke(self, tid: str) -> None:
        with self.lock:
            table = dict(self.load())
            if table.pop(tid, None) is not None:
                self.save(table)


class Grants:
    """Device codes waiting for approval and one-time tickets: short-lived, so only in memory."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.tickets = {}

    def sweep(self, now: float) -> None:
        self.pending = {k: v for k, v in self.pending.items() if v["created"] + PENDING_TTL > now}
        self.tickets = {k: v for k, v in self.tickets.items() if v["exp"] > now}

    def start(self, label: str, kind: str = "vscode"):
        now = time.time()
        with self.lock:
            self.sweep(now)
            if len(self.pending) >= PENDING_MAX:
                raise HTTPException(429, "Too many sign-in requests; try again in a few minutes")
            device = b64(secrets.token_bytes(32))
            user = "".join(secrets.choice(ALPHABET) for _ in range(8))
            user = user[:4] + "-" + user[4:]
            self.pending[digest(device)] = {"user": user, "label": label, "kind": kind, "created": now, "email": None, "token": None}
            return device, user

    def by_user_code(self, code: str):
        now = time.time()
        with self.lock:
            self.sweep(now)
            return next((v for v in self.pending.values() if v["user"] == code and v["email"] is None), None)

    def approve(self, code: str, email: str, tokens: Tokens) -> bool:
        now = time.time()
        with self.lock:
            self.sweep(now)
            row = next((v for v in self.pending.values() if v["user"] == code and v["email"] is None), None)
            if row is None:
                return False
            row["token"] = tokens.add(email, row["label"], row.get("kind", "vscode"))
            row["email"] = email
            return True

    def poll(self, device: str):
        now = time.time()
        with self.lock:
            self.sweep(now)
            key = digest(device)
            row = self.pending.get(key)
            if row is None:
                return {"status": "expired"}
            if row["token"] is None:
                return {"status": "pending"}
            del self.pending[key]
            return {"status": "ok", "token": row["token"], "email": row["email"]}

    def ticket(self, tid: str) -> str:
        now = time.time()
        with self.lock:
            self.sweep(now)
            if len(self.tickets) >= TICKET_MAX:
                raise HTTPException(429, "Too many tickets")
            value = b64(secrets.token_bytes(32))
            self.tickets[digest(value)] = {"tid": tid, "exp": now + TICKET_TTL}
            return value

    def redeem(self, ticket: str):
        now = time.time()
        with self.lock:
            self.sweep(now)
            row = self.tickets.pop(digest(ticket), None)
            return row["tid"] if row else None


tokens = Tokens()
grants = Grants()


def sign(tid: str, exp: int) -> str:
    return b64(hmac.new(tokens.key(), f"{tid}.{exp}".encode(), hashlib.sha256).digest())


def session_cookie(tid: str) -> str:
    exp = int(time.time() + COOKIE_TTL)
    return f"{tid}.{exp}.{sign(tid, exp)}"


def session_email(header: str):
    try:
        jar = SimpleCookie()
        jar.load(header or "")
        value = jar[COOKIE].value
    except Exception:
        return None
    parts = value.split(".")
    if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{8}", parts[0]) or not parts[1].isdigit():
        return None
    tid, exp = parts[0], int(parts[1])
    if exp <= time.time() or not hmac.compare_digest(parts[2], sign(tid, exp)):
        return None
    row = tokens.row(tid)
    if row is None:
        return None
    tokens.touch(tid)
    return row["email"]


def check(internal, cookie):
    """Caddy's question for one request: (status, email). 403 when the caller is not Caddy."""
    if internal != "1":
        return 403, None
    email = session_email(cookie)
    return (202, email) if email else (401, None)


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="VS Code", max_length=60)
    kind: Literal["vscode", "browser", "pc"] = "vscode"


class PollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_code: str = Field(max_length=64)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=16)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=128)


class FolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=PATH_MAX)
    name: str = Field(default="", max_length=120)


class ContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folders: list[FolderIn] = Field(default_factory=list, max_length=FOLDERS_MAX)
    host: str = Field(default="", max_length=60)


def page(title: str, body: str, script: str = "") -> HTMLResponse:
    text = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<script src="/static/claude-look.js"></script><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/claude-look.css"><link rel="stylesheet" href="/static/ext-authorize.css"></head>'
        f'<body><main class="ext-card">{body}</main>{script}</body></html>'
    )
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


DEFAULT_LABELS = {"vscode": "VS Code", "browser": "Chrome", "pc": "Computer"}


def clean_label(value: str, kind: str = "vscode") -> str:
    value = CONTROL.sub("", value or "").strip()
    return value or DEFAULT_LABELS.get(kind, "VS Code")


def build_router() -> APIRouter:
    r = APIRouter()

    @r.post("/ext/device")
    async def device(body: DeviceIn):
        code, user = grants.start(clean_label(body.label, body.kind), body.kind)
        base = f"https://{config.PORTAL_HOST}" if config.PORTAL_HOST else ""
        return {"device_code": code, "user_code": user, "verification_uri": f"{base}/ext/authorize",
                "expires_in": PENDING_TTL, "interval": 3}

    @r.post("/ext/poll")
    async def poll(body: PollIn):
        if not SECRET_RE.fullmatch(body.device_code):
            return {"status": "expired"}
        return grants.poll(body.device_code)

    @r.get("/ext/authorize")
    async def authorize(request: Request, code: str = ""):
        info = request.scope["portal"]
        wanted = code.strip().upper()
        row = grants.by_user_code(wanted) if CODE_RE.fullmatch(wanted) else None
        if row is None:
            body = ('<h1>Sign in to VS Code</h1><p>Enter the code shown in VS Code.</p>'
                    '<input id="ext-code" maxlength="9" placeholder="XXXX-XXXX" autocomplete="off" autocapitalize="characters" spellcheck="false">'
                    '<button id="ext-go" class="btn primary" type="button">Continue</button>')
        elif row.get("kind") == "pc":
            body = (f'<h1>Let Claude work on this computer</h1><p>This lets Claude, signed in as <strong>{html.escape(info["email"])}</strong>, '
                    f'run commands and read, create, change and delete files on <strong>{html.escape(row["label"])}</strong>, with the rights of the '
                    f'user who started the agent there.</p>'
                    f'<p class="ext-code">{html.escape(wanted)}</p>'
                    f'<p class="muted">Approve only if the same code is shown in the installer you just ran. You can pause Claude on that computer '
                    f'or disconnect it here at any time.</p>'
                    f'<button id="ext-approve" class="btn primary" type="button" data-code="{html.escape(wanted)}">Approve</button>')
        elif row.get("kind") == "browser":
            body = (f'<h1>Let Claude control Chrome</h1><p>This lets Claude, signed in as <strong>{html.escape(info["email"])}</strong>, '
                    f'open, read and click pages in <strong>{html.escape(row["label"])}</strong>, including sites where you are signed in.</p>'
                    f'<p class="ext-code">{html.escape(wanted)}</p>'
                    f'<p class="muted">Approve only if the same code is shown in the extension you just opened. You can pause or sign out in the extension at any time.</p>'
                    f'<button id="ext-approve" class="btn primary" type="button" data-code="{html.escape(wanted)}">Approve</button>')
        else:
            body = (f'<h1>Sign in to VS Code</h1><p>This gives <strong>{html.escape(row["label"])}</strong> access to Code Web as '
                    f'<strong>{html.escape(info["email"])}</strong>.</p><p class="ext-code">{html.escape(wanted)}</p>'
                    f'<p class="muted">Approve only if the same code is shown in VS Code.</p>'
                    f'<button id="ext-approve" class="btn primary" type="button" data-code="{html.escape(wanted)}">Approve</button>')
        body += '<p id="ext-status" class="muted" role="status"></p>'
        return page("Sign in to VS Code", body, '<script src="/static/ext-authorize.js"></script>')

    @r.post("/ext/approve")
    async def approve(request: Request, body: ApproveIn):
        code = body.code.strip().upper()
        if not CODE_RE.fullmatch(code) or not grants.approve(code, request.scope["portal"]["email"], tokens):
            raise HTTPException(404, "Code not found or expired")
        return {"approved": True}

    @r.post("/ext/ticket")
    async def ticket(body: TokenIn):
        found = tokens.find(body.token)
        if found is None:
            raise HTTPException(401, "unauthorized")
        tid, row = found
        if row.get("kind", "vscode") != "vscode":
            raise HTTPException(401, "unauthorized")
        tokens.touch(tid)
        return {"ticket": grants.ticket(tid), "email": row["email"]}

    @r.get("/ext/enter")
    async def enter(ticket: str = ""):
        tid = grants.redeem(ticket) if SECRET_RE.fullmatch(ticket) else None
        if tid is None or tokens.row(tid) is None:
            return Response("Sign-in link expired. Reopen the view in VS Code.", status_code=401, media_type="text/plain", headers={"Cache-Control": "no-store"})
        response = RedirectResponse("/?embed=vscode", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers.append("Set-Cookie", f"{COOKIE}={session_cookie(tid)}; Max-Age={COOKIE_TTL}; Path=/; Secure; HttpOnly; SameSite=None; Partitioned")
        return response

    @r.post("/ext/revoke")
    async def revoke(body: TokenIn):
        found = tokens.find(body.token)
        if found is not None:
            tokens.revoke(found[0])
        return {"revoked": found is not None}

    @r.get("/ext/verify")
    async def verify(request: Request):
        status, email = check(request.headers.get("x-ext-internal"), request.headers.get("cookie", ""))
        if status == 403:
            raise HTTPException(403, "forbidden")
        return Response(status_code=status, headers={"Cache-Control": "no-store", **({"X-Auth-Request-Email": email} if email else {})})

    @r.put("/api/chats/{sid}/context")
    async def put_context(sid: str, body: ContextIn):
        if not UUID_RE.fullmatch(sid) or not ownership.owns(sid):
            raise HTTPException(404, "no such chat")
        folders = []
        for folder in body.folders:
            path, name = CONTROL.sub("", folder.path).strip(), CONTROL.sub("", folder.name).strip()
            if path:
                folders.append({"path": path, "name": name})
        if not folders:
            context_path(sid).unlink(missing_ok=True)
            return {"folders": []}
        write_private(context_path(sid), json.dumps({"folders": folders, "host": CONTROL.sub("", body.host).strip(), "at": time.time()}).encode())
        return {"folders": folders}

    @r.delete("/api/chats/{sid}/context")
    async def delete_context(sid: str):
        if not UUID_RE.fullmatch(sid) or not ownership.owns(sid):
            raise HTTPException(404, "no such chat")
        context_path(sid).unlink(missing_ok=True)
        return {"folders": []}

    return r


def context_path(sid: str) -> Path:
    return context_directory() / (sid + ".json")


def hook_command() -> str:
    """Claude Code runs this before every prompt; what it prints is added to the prompt as context while the visible message stays as typed."""
    return (
        "python3 -I -c \"import json,sys,time;d=json.load(sys.stdin);c=json.load(open('" + str(context_directory()) + "/'+d['session_id']+'.json'));"
        "assert time.time()-c['at']<" + str(CONTEXT_TTL) + ";print('The user has VS Code open on their computer with: '"
        "+', '.join(f['path'] for f in c['folders'])+'. Treat it as the project they mean.')\" 2>/dev/null||true"
    )
