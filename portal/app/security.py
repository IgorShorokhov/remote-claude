import json
import re
from urllib.parse import parse_qs

from . import accounts, config, features
from . import ext as ext_mod
from . import users as users_mod
from .roles import RolesStore, normalize_email

ANY = frozenset({"admin", "user"})
ADMIN = frozenset({"admin"})
READ = frozenset({"GET", "HEAD"})
BACKUP_OWNER = config.OWNER_EMAIL

TABS = (
    ("chat", "Chat"),
    ("browser", "Browser"),
    ("code", "Code"),
    ("backup", "API Agent"),
    ("prompts", "Prompts"),
    ("skills", "Skills"),
    ("instructions", "System instructions"),
    ("memory", "Memory"),
    ("history", "History"),
    ("devices", "Computers"),
    ("pc", "PC"),
    ("sync", "Sync"),
    ("vpn", "VPN"),
    ("access", "Access"),
)
OWNER_TABS = frozenset({"browser", "backup", "pc", "sync", "vpn", "access"})
TABS_BY_ROLE = {
    "admin": [t[0] for t in TABS],
    "user": [t[0] for t in TABS if t[0] not in OWNER_TABS],
}
USER_API = ("/api/models", "/api/usage", "/api/sessions", "/api/chats", "/api/history",
            "/api/prompts", "/api/speech", "/api/uploads", "/api/dirs", "/api/devices")
USER_API_SUBPATHS = ("/api/personal", "/api/term")

PUBLIC_PATHS = frozenset({"/api/health"})
# The extension's sign-in carries its own secrets (device code, token, one-time ticket, signed cookie); /ext/verify only answers Caddy.
PUBLIC_EXT = frozenset({("POST", "/ext/device"), ("POST", "/ext/poll"), ("POST", "/ext/ticket"), ("POST", "/ext/revoke"),
                        ("GET", "/ext/enter"), ("GET", "/ext/verify")})
# The browser extension's socket proves itself with a browser-kind token in its first message (browser_bridge.py).
PUBLIC_WS = frozenset({"/ext/bridge"})
RAW_UPLOAD = "/api/uploads/raw"
RAW_UPLOAD_BACKUP = re.compile(r"/api/backup/chats/[0-9a-f]{32}/uploads/raw")
RAW_UPLOAD_SKILLS = re.compile(r"/api/skills/(import|[a-z0-9][a-z0-9-]{0,63}/upload)")  # archives and skill files as raw bytes


def allowed_roles(method: str, path: str) -> frozenset[str]:
    if method == "POST" and path == "/api/presence":
        return ANY
    if (method, path) in (("GET", "/ext/authorize"), ("POST", "/ext/approve")):
        return ANY
    if method in READ:
        if path == "/" or path.startswith("/static/"):
            return ANY
        if path == "/api/me":
            return ANY
    if path.startswith("/api/"):
        # One set of skills for everyone: every account reads them, only an admin changes them (decision 228).
        if path == "/api/skills" or path.startswith("/api/skills/"):
            return ANY if method in READ else ADMIN
        # Caddy asks whether this account may open the shared Chat desktop (decision 229).
        if path == accounts.GATE and method in READ:
            return ANY
        if any(path == prefix or path.startswith(prefix + "/") for prefix in USER_API) or any(path.startswith(prefix + "/") for prefix in USER_API_SUBPATHS):
            return ANY
        return ADMIN
    return frozenset()


def _headers(scope) -> dict[bytes, list[bytes]]:
    out: dict[bytes, list[bytes]] = {}
    for k, v in scope.get("headers", []):
        out.setdefault(k.lower(), []).append(v)
    return out


def _single(headers, name: bytes) -> str | None:
    vals = headers.get(name)
    if not vals or len(vals) != 1:
        return None
    try:
        s = vals[0].decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not s or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in s):
        return None
    return s


def _origin_ok(headers, host: str) -> bool:
    if not host:
        return False
    return headers.get(b"origin") == [f"https://{host}".encode()]


def _json_ct(headers, path: str = "") -> bool:
    vals = headers.get(b"content-type")
    if not vals or len(vals) != 1:
        return False
    kind = vals[0].split(b";")[0].strip().lower()
    # Raw attachment bytes are the only non-JSON write; X-Portal and Origin still guard it.
    return kind == b"application/json" or (path == RAW_UPLOAD or RAW_UPLOAD_BACKUP.fullmatch(path) is not None or RAW_UPLOAD_SKILLS.fullmatch(path) is not None) and kind == b"application/octet-stream"


class AuthMiddleware:
    def __init__(self, app, roles: RolesStore, host: str, users=None):
        self.app = app
        self.roles = roles
        self.users = users
        self.host = host

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        method = "GET" if kind == "websocket" else scope["method"].upper()
        if kind == "http" and path in PUBLIC_PATHS and method in READ:
            await self.app(scope, receive, send)
            return
        if kind == "websocket" and path == "/ext/verify":
            await self._verify_upgrade(scope, send)
            return
        if kind == "websocket" and path in PUBLIC_WS:
            await self.app(scope, receive, send)
            return
        if kind == "http" and (method, path) in PUBLIC_EXT:
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        email = _single(headers, b"x-forwarded-email")
        if email is None:
            await self._deny(kind, send, 401)
            return
        email = normalize_email(email)
        if path.startswith('/api/backup/') and email != BACKUP_OWNER:
            await self._deny(kind, send, 403)
            return
        role = self.roles.role_for(email)
        if role is None:
            await self._deny(kind, send, 403)
            return
        if _single(headers, b"x-portal-role") != role:
            await self._deny(kind, send, 403)
            return
        if role not in allowed_roles(method, path):
            await self._deny(kind, send, 403)
            return
        # Access management is the owner's alone; a section the owner closed for this account is refused (decision 229).
        if (path.startswith("/api/access") and path != accounts.GATE and email != accounts.owner()) or accounts.denied(email, path) or features.blocked(path):
            await self._deny(kind, send, 403)
            return
        if kind == "websocket" or method not in READ:
            if not _origin_ok(headers, self.host):
                await self._deny(kind, send, 403)
                return
            if kind == "http" and (
                (headers.get(b"x-portal") != [b"1"] and path != "/api/presence") or not _json_ct(headers, path)
            ):
                await self._deny(kind, send, 403)
                return
        user = self.users.user_for(email) if self.users else users_mod.DEFAULT_ID
        if user is None:
            await self._deny(kind, send, 403)
            return
        tag = users_mod.login_tag(email)
        binding = headers.get(b'x-portal-account')
        query_binding = parse_qs(scope.get('query_string', b'').decode('ascii', 'replace')).get('account') if kind == 'websocket' else None
        if (binding is not None and binding != [tag.encode()]) or (query_binding is not None and query_binding != [tag]):
            await self._deny(kind, send, 403)
            return
        scope["portal"] = {"email": email, "role": role, "user": user}
        scope['portal_valid'] = lambda: self.roles.role_for(email) == role and (self.users is None or self.users.user_for(email) == user)
        token = users_mod.current.set(user)
        mark = users_mod.login.set(users_mod.login_tag(email))
        sender = users_mod.email.set(email)
        async def private_send(message):
            if message['type'] == 'http.response.start' and path.startswith('/api/'):
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in (b'cache-control', b'x-portal-account')]
                message['headers'] += [(b'cache-control', b'no-store'), (b'x-portal-account', users_mod.login_tag(email).encode())]
            await send(message)
        try:
            await self.app(scope, receive, private_send)
        finally:
            users_mod.email.reset(sender)
            users_mod.login.reset(mark)
            users_mod.current.reset(token)

    async def _verify_upgrade(self, scope, send):
        # Caddy forwards the browser's "Upgrade: websocket" headers when it asks about a socket request; answer that question as plain HTTP.
        h = _headers(scope)
        status, email = ext_mod.check((h.get(b"x-ext-internal") or [b""])[0].decode("latin1"), (h.get(b"cookie") or [b""])[0].decode("latin1"))
        if "websocket.http.response" not in scope.get("extensions", {}):
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [(b"content-length", b"0"), (b"cache-control", b"no-store")] + ([(b"x-auth-request-email", email.encode())] if email else [])
        await send({"type": "websocket.http.response.start", "status": status, "headers": headers})
        await send({"type": "websocket.http.response.body", "body": b""})

    async def _deny(self, kind, send, status: int):
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "unauthorized" if status == 401 else "forbidden"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
