from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import contextlib
import functools
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs

from .config import PORTAL_HOST, ROLES_FILE, STATIC_DIR, USERS_FILE
from . import accounts, browser_bridge, features, chats, config, devices, ext, identity, memory_job, personal, presence, prompts, skills, speech, users, vpn
from .api import build_router
from .history import HistoryIndex
from .roles import RolesStore
from .security import TABS, TABS_BY_ROLE, AuthMiddleware, BACKUP_OWNER


IMMUTABLE = "private, max-age=31536000, immutable"


class PortalStatic(StaticFiles):
    # A file addressed with ?v=<hash of its bytes> never changes, so the browser may keep it for good; the rest is revalidated.
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("ascii", "replace"))
        response.headers["Cache-Control"] = IMMUTABLE if versioned and response.status_code in (200, 304) else "no-cache"
        if path == "sw.js":
            response.headers["Service-Worker-Allowed"] = "/"
        if path.endswith(".webmanifest"):
            response.headers["Content-Type"] = "application/manifest+json"
        return response


NoCacheStatic = PortalStatic
_ASSET_RE = re.compile(r'(/static/[A-Za-z0-9_./-]+\.(?:js|css))"')
VERSIONED = (".js", ".css")
NOT_PRECACHED = frozenset({"sw.js", "index.html"})
PRECACHED = (".js", ".css", ".woff2", ".png", ".svg", ".webmanifest")


@functools.lru_cache(maxsize=1024)
def _file_hash(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:10]


def static_files() -> list[Path]:
    return [p for p in sorted(STATIC_DIR.rglob("*")) if p.is_file()]


def asset_hashes() -> dict[str, str]:
    out = {}
    for p in static_files():
        rel = p.relative_to(STATIC_DIR).as_posix()
        if p.suffix in VERSIONED and rel not in NOT_PRECACHED:
            st = p.stat()
            out[rel] = _file_hash(str(p), st.st_mtime_ns, st.st_size)
    return out


@functools.lru_cache(maxsize=1)
def asset_version() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file() and p.suffix in (".js", ".css", ".html"):
            h.update(str(p.relative_to(STATIC_DIR)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def render_index() -> bytes:
    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    v = asset_version()
    hashes = asset_hashes()
    used = set()

    def versioned(match):
        rel = match.group(1)[len("/static/"):]
        used.add(rel)
        return f'{match.group(1)}?v={hashes.get(rel, v)}"'

    text = text.replace('<meta name="portal-version" content="dev">', f'<meta name="portal-version" content="{v}">')
    text = _ASSET_RE.sub(versioned, text)
    lazy = {rel: h for rel, h in hashes.items() if rel not in used}
    meta = html.escape(json.dumps(lazy, separators=(",", ":")), quote=True)
    return text.replace("</head>", f'<meta name="portal-assets" content="{meta}">\n</head>', 1).encode("utf-8")


def precache_list() -> list[str]:
    hashes = asset_hashes()
    out = []
    for p in static_files():
        rel = p.relative_to(STATIC_DIR).as_posix()
        if rel in NOT_PRECACHED or p.suffix not in PRECACHED:
            continue
        out.append(f"/static/{rel}" + (f"?v={hashes[rel]}" if rel in hashes else ""))
    return out


def render_sw() -> bytes:
    text = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    text = text.replace('"__VERSION__"', json.dumps(asset_version()))
    return text.replace('"__PRECACHE__"', json.dumps(precache_list(), separators=(",", ":"))).encode("utf-8")


@contextlib.asynccontextmanager
async def lifespan(app):
    async with presence.lifespan(app), browser_bridge.lifespan(app):
        yield


def create_app(roles_file=None, host=None, users_file=None) -> FastAPI:
    store = RolesStore(roles_file or ROLES_FILE)
    user_store = users.UsersStore(users_file or USERS_FILE)
    users.install(user_store)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.presence = presence.PresenceStore()
    app.state.browser = browser_bridge.Hub()
    app.state.roles = store
    app.state.users = user_store
    app.include_router(presence.build_router(app.state.presence))

    history = HistoryIndex(config.HISTORY_DIR)
    app.include_router(build_router(history))
    app.include_router(chats.build_router(history))
    app.include_router(vpn.build_router())
    app.include_router(ext.build_router())
    app.include_router(browser_bridge.build_router())
    app.include_router(devices.build_router())
    app.include_router(prompts.build_router())
    app.include_router(skills.build_router())
    app.include_router(accounts.build_router(store))
    app.include_router(personal.build_router(identity.common, memory_job.status))
    app.include_router(speech.build_router())
    from . import backup_agent
    app.include_router(backup_agent.build_router())

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        info = request.scope["portal"]
        allowed = TABS_BY_ROLE[info["role"]]
        allowed = [i for i in allowed if i != 'backup' or info['email'] == BACKUP_OWNER]
        allowed = [i for i in allowed if i != 'access' or info['email'] == accounts.owner()]
        allowed = accounts.allowed_tabs(info['email'], features.tabs(allowed))
        return {
            "email": info["email"],
            "role": info["role"],
            "user": info.get("user"),
            "tabs": [{"id": i, "title": t} for i, t in TABS if i in allowed],
            "features": sorted(features.enabled()),
        }

    @app.get("/", include_in_schema=False)
    async def index():
        return Response(
            render_index(),
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/static/sw.js", include_in_schema=False)
    async def service_worker():
        return Response(
            render_sw(),
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    app.mount("/static", PortalStatic(directory=STATIC_DIR), name="static")
    app.add_middleware(
        AuthMiddleware, roles=store, users=user_store, host=PORTAL_HOST if host is None else host
    )
    return app


app = create_app()
