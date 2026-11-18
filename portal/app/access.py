from fastapi import HTTPException
from starlette.requests import HTTPConnection

from . import tmuxctl

SESSION_PREFIXES = ("/api/sessions/", "/api/term/")


def is_admin(conn: HTTPConnection) -> bool:
    return (conn.scope.get("portal") or {}).get("role") == "admin"


async def console_guard(conn: HTTPConnection) -> None:
    name = conn.path_params.get("name")
    if not name or is_admin(conn) or not conn.scope.get("path", "").startswith(SESSION_PREFIXES):
        return
    try:
        sid = await tmuxctl.option(name, "@resume_sid")
    except tmuxctl.TmuxError:
        sid = None
    if not sid:
        raise HTTPException(403, "Consoles are available to the owner only")
