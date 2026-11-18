"""Access management for the owner (decision 229): which emails may sign in, and which sections each account may use.

Two levels, kept apart on purpose:
- Sign-in (the emails oauth2-proxy lets through, their role and account id) lives in /etc/agent-system and only the root helper
  agent-apply-roles changes it. The portal cannot write there (its service is sandboxed), so it leaves one request in its own state
  folder; the root path unit from infra/59-access-bridge.sh hands it to the helper and allows only "add one member" or "remove one member".
- Sections are the portal's own: a list of sections closed for an account, checked for every request by the auth middleware and,
  for the shared Chat desktop, by Caddy through /api/access/gate/chat.
The owner always has everything; sections that run commands as the server's user stay owner-only and cannot be granted.
"""
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import config, features, users

EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
BRIDGE = Path(os.environ.get("PORTAL_ACCESS_BRIDGE", "/usr/local/sbin/agent-access-apply"))
REQUEST_MAX = 64 * 1024
LOG_KEEP = 300
GATE = "/api/access/gate/chat"

# id, title, what it gives, menu tabs, API prefixes it guards
SECTIONS = (
    ("code", "Code Web", "Chats with Claude Code, files, voice input, chat history", ("code", "history"),
     ("/api/chats", "/api/sessions", "/api/models", "/api/uploads", "/api/dirs", "/api/history", "/api/usage", "/api/term", "/api/speech")),
    ("prompts", "Prompts", "Own saved prompts", ("prompts",), ("/api/prompts",)),
    ("skills", "Skills", "Reads the shared skills", ("skills",), ("/api/skills",)),
    ("instructions", "Instructions", "Own system instructions for Claude", ("instructions",), ("/api/personal/instructions",)),
    ("memory", "Memory", "What Claude remembers about the account", ("memory",), ("/api/personal/memory",)),
    ("devices", "Computers", "Connects own computer and Chrome to Claude", ("devices",), ("/api/devices",)),
    ("chat", "Chat desktop", "The shared remote desktop with Claude's chat", ("chat",), (GATE,)),
)
OWNER_ONLY = (
    ("backup", "API Agent", "runs commands on the server"),
    ("console", "Console", "a shell on the server"),
    ("browser", "Browser", "the server's own Chrome"),
    ("pc", "PC", "the owner's computer"),
    ("sync", "Sync", "the owner's file sync"),
    ("vpn", "VPN", "the server's network"),
    ("access", "Access", "this page"),
)
GRANTABLE = tuple(s[0] for s in SECTIONS)
TAB_SECTION = {tab: s[0] for s in SECTIONS for tab in s[3]}
_lock = threading.Lock()
_cache = {"stamp": None, "off": {}}


def folder() -> Path:
    path = config.HOME / ".local" / "state" / "agent-portal" / "access"
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for part in reversed(missing):
        part.mkdir(exist_ok=True)
        os.chmod(part, 0o700)  # the service's umask 0117 would leave a new folder without the x bit
    return path


def owner() -> str:
    return users.OWNER_EMAIL


def member_id(email: str) -> str:
    """The account id a new member gets; infra/access_bridge.py checks the same formula."""
    return "u-" + hashlib.sha256(email.encode()).hexdigest()[:20]


def write_json(path: Path, data) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def closed() -> dict[str, list[str]]:
    """{email: [closed section ids]}; re-read only when the file changes, since every request asks."""
    path = folder() / "sections.json"
    try:
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
    except OSError:
        _cache["stamp"], _cache["off"] = None, {}
        return {}
    if stamp != _cache["stamp"]:
        data = read_json(path, {})
        accounts = data.get("accounts") if isinstance(data, dict) else None
        _cache["off"] = {e: [s for s in v.get("off", []) if s in GRANTABLE]
                         for e, v in (accounts if isinstance(accounts, dict) else {}).items() if isinstance(v, dict)}
        _cache["stamp"] = stamp
    return _cache["off"]


def section_of(path: str) -> str | None:
    for sid, _, _, _, prefixes in SECTIONS:
        if any(path == p or path.startswith(p + "/") for p in prefixes):
            return sid
    return None


def denied(email: str, path: str) -> bool:
    """True when the request belongs to a section closed for this account; the owner is never closed out."""
    if email == owner():
        return False
    sid = section_of(path)
    return sid is not None and sid in closed().get(email, [])


def allowed_tabs(email: str, tabs: list[str]) -> list[str]:
    if email == owner():
        return tabs
    off = set(closed().get(email, []))
    return [t for t in tabs if TAB_SECTION.get(t) not in off]


def note(by: str, email: str, what: str) -> None:
    with open(folder() / "log.jsonl", "a") as stream:
        stream.write(json.dumps({"at": int(time.time()), "by": by, "email": email, "what": what}, ensure_ascii=False) + "\n")


def journal(limit: int = 60) -> list[dict]:
    try:
        lines = (folder() / "log.jsonl").read_text().splitlines()[-LOG_KEEP:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[:limit]


def roster(path: Path) -> list[list[str]]:
    """Rows of roles.txt as the root helper writes them: email, role, optional account id."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HTTPException(503, "The list of accounts cannot be read") from None
    rows = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) in (2, 3) and not parts[0].startswith("#"):
            rows.append([parts[0].lower(), parts[1].lower()] + [p.lower() for p in parts[2:]])
    return rows


def pending() -> dict | None:
    """The sign-in change waiting for the root bridge (it deletes the request once it has answered)."""
    request = read_json(folder() / "request.json", None)
    result = read_json(folder() / "result.json", None)
    if not isinstance(request, dict) or (isinstance(result, dict) and result.get("id") == request.get("id")):
        return None
    return request


class Sections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: dict[str, bool]


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    sections: dict[str, bool] = Field(default_factory=dict)


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(max_length=254)


def build_router(roles) -> APIRouter:
    router = APIRouter(prefix="/api/access")

    def actor(request: Request) -> str:
        return request.scope["portal"]["email"]

    def entry(email, role, account, off, change):
        is_owner = email == owner()
        return {"email": email, "role": role, "account": account, "owner": is_owner, "pending": change,
                "sections": {s: is_owner or s not in off for s in GRANTABLE}}

    def view():
        rows = roster(roles.path)
        off = closed()
        waiting = pending()
        change = waiting.get("change") if waiting else None
        accounts = [entry(r[0], r[1], r[2] if len(r) > 2 else None, off.get(r[0], []),
                          change if change and change.get("email") == r[0] else None) for r in rows]
        if change and change.get("kind") == "add" and all(a["email"] != change.get("email") for a in accounts):
            accounts.append(entry(change["email"], "user", None, off.get(change["email"], []), change))
        accounts.sort(key=lambda a: (not a["owner"], a["email"]))
        return {"accounts": accounts,
                "sections": [{"id": s[0], "title": s[1], "about": s[2]} for s in SECTIONS if s[0] not in features.ALL or features.on(s[0])],
                "owner_only": [{"id": s[0], "title": s[1], "about": s[2]} for s in OWNER_ONLY if features.on(s[0])],
                "bridge": {"installed": BRIDGE.exists(), "pending": waiting, "result": read_json(folder() / "result.json", None)},
                "log": journal()}

    def store(email: str, wanted: dict[str, bool]) -> list[str]:
        unknown = [s for s in wanted if s not in GRANTABLE]
        if unknown:
            raise HTTPException(400, f"Section {unknown[0]} cannot be granted")
        data = read_json(folder() / "sections.json", {})
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            data = {"accounts": {}}
        off = set(data["accounts"].get(email, {}).get("off", []))
        for sid, on in wanted.items():
            (off.discard if on else off.add)(sid)
        data["accounts"][email] = {"off": sorted(off)}
        write_json(folder() / "sections.json", data)
        return sorted(off)

    def submit(by: str, kind: str, email: str, rows: list[list[str]]) -> dict:
        if pending():
            raise HTTPException(409, "Another sign-in change is still being applied. Wait for it or cancel it")
        text = "".join(" ".join(r) + "\n" for r in rows)
        if len(text) > REQUEST_MAX:
            raise HTTPException(413, "The list of accounts is too long")
        request = {"id": uuid.uuid4().hex, "roles": text, "by": by, "at": int(time.time()), "change": {"kind": kind, "email": email}}
        write_json(folder() / "request.json", request)
        return request

    def describe(wanted: dict[str, bool]) -> str:
        titles = {s[0]: s[1] for s in SECTIONS}
        opened = [titles[s] for s, v in wanted.items() if v]
        shut = [titles[s] for s, v in wanted.items() if not v]
        return "; ".join(x for x in (("opened " + ", ".join(opened)) if opened else "", ("closed " + ", ".join(shut)) if shut else "") if x)

    @router.get("")
    def overview():
        return view()

    @router.get("/gate/chat", status_code=204)
    def gate():
        # Caddy asks here before it serves the Chat desktop; a closed section has already been refused by the middleware.
        return Response(status_code=204)

    @router.put("/{email}/sections")
    def change_sections(email: str, body: Sections, request: Request):
        email = email.strip().lower()
        if email == owner():
            raise HTTPException(409, "The owner always has every section")
        if not body.sections:
            raise HTTPException(400, "Nothing to change")
        with _lock:
            waiting = pending()
            known = [r[0] for r in roster(roles.path)]
            if email not in known and not (waiting and waiting["change"]["email"] == email):
                raise HTTPException(404, "No such account")
            off = store(email, body.sections)
            note(actor(request), email, describe(body.sections))
        return {"email": email, "sections": {s: s not in off for s in GRANTABLE}}

    @router.post("/accounts", status_code=202)
    def add_account(body: NewAccount, request: Request):
        email = body.email.strip().lower()
        if not EMAIL.fullmatch(email):
            raise HTTPException(400, "This does not look like an email address")
        with _lock:
            rows = roster(roles.path)
            if email in [r[0] for r in rows]:
                raise HTTPException(409, "This email can already sign in")
            ids = {r[2] for r in rows if len(r) > 2}
            if member_id(email) in ids:
                raise HTTPException(409, "This account id is already taken")
            request_ = submit(actor(request), "add", email, rows + [[email, "user", member_id(email)]])
            store(email, {s: body.sections.get(s, True) for s in GRANTABLE})
            shut = [s for s in GRANTABLE if not body.sections.get(s, True)]
            note(actor(request), email, "sign-in requested" + (" without " + ", ".join(shut) if shut else " with every section"))
        return {"request": request_["id"]}

    @router.delete("/accounts/{email}", status_code=202)
    def remove_account(email: str, body: Confirm, request: Request):
        email = email.strip().lower()
        if email == owner():
            raise HTTPException(409, "The owner cannot be removed")
        if body.confirm.strip().lower() != email:
            raise HTTPException(409, "Type the email to confirm")
        with _lock:
            rows = roster(roles.path)
            if email not in [r[0] for r in rows]:
                raise HTTPException(404, "No such account")
            request_ = submit(actor(request), "remove", email, [r for r in rows if r[0] != email])
            note(actor(request), email, "removal of sign-in requested")
        return {"request": request_["id"]}

    @router.delete("/request")
    def cancel(request: Request):
        """Drop a request the root bridge has not picked up, for example while it is not installed yet."""
        with _lock:
            waiting = pending()
            if not waiting:
                return {"cancelled": False}
            (folder() / "request.json").unlink(missing_ok=True)
            email = waiting["change"]["email"]
            if waiting["change"]["kind"] == "add" and email not in [r[0] for r in roster(roles.path)]:
                data = read_json(folder() / "sections.json", {})
                if isinstance(data, dict) and isinstance(data.get("accounts"), dict) and data["accounts"].pop(email, None) is not None:
                    write_json(folder() / "sections.json", data)
            note(actor(request), email, "pending sign-in change cancelled")
        return {"cancelled": True}

    return router
