"""The computers and browsers a person has connected to the portal: the list, disconnecting one, and the files that install them.

Everything here is about the signed-in account only. A device is a token of kind "browser" (Chrome extension) or "pc" (computer agent) made by
the device-code sign-in in ext.py; the hub in browser_bridge.py knows which of them are connected right now.
"""
import base64
import io
import json
import re
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from . import browser_bridge, config, ext

AGENT_DIR = Path(__file__).with_name("pc_agent")
DIST_DIR = Path(__file__).with_name("downloads")
EXTENSION_ZIP = DIST_DIR / "claude-browser-bridge.zip"
TID_RE = re.compile(r"[0-9a-f]{8}")


def agent_source() -> bytes:
    return (AGENT_DIR / "portal_agent.py").read_bytes()


def agent_version() -> str:
    match = re.search(rb'^VERSION = "([0-9.]+)"', agent_source(), re.M)
    return match.group(1).decode() if match else ""


def portal_origin(request: Request | None = None) -> str:
    if config.PORTAL_HOST:
        return f"https://{config.PORTAL_HOST}"
    return str(request.base_url).rstrip("/") if request is not None else ""


EXTENSION_DEFAULT = re.compile(r'(\n\s*portal: )"https://[^"]*"')


def extension_zip(portal: str) -> bytes:
    """The Chrome extension with this portal as its default address (the zip in git carries the owner's)."""
    source = zipfile.ZipFile(io.BytesIO(EXTENSION_ZIP.read_bytes()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for item in source.infolist():
            data = source.read(item)
            if portal and item.filename.endswith("lib/store.js"):
                data = EXTENSION_DEFAULT.sub(lambda m: m.group(1) + json.dumps(portal), data.decode("utf-8"), count=1).encode("utf-8")
            dest.writestr(item, data)
    return out.getvalue()


def render(template: str, portal: str) -> str:
    """The installer text for one portal: the agent, its address and the pinned Python build are written into it."""
    python = json.loads((AGENT_DIR / "python-embed.json").read_text())
    text = (AGENT_DIR / template).read_text()
    values = {"@PORTAL@": portal, "@AGENT_B64@": base64.b64encode(agent_source()).decode(),
              "@PYTHON_AMD64_URL@": python["amd64"]["url"], "@PYTHON_AMD64_SHA@": python["amd64"]["sha256"],
              "@PYTHON_ARM64_URL@": python["arm64"]["url"], "@PYTHON_ARM64_SHA@": python["arm64"]["sha256"]}
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def windows_setup(portal: str) -> bytes:
    return render("setup_windows.cmd", portal).replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def unix_setup(portal: str) -> bytes:
    return render("setup_unix.sh", portal).encode("ascii")


def attachment(body: bytes, name: str, kind: str = "application/octet-stream") -> Response:
    return Response(body, media_type=kind, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
                                                    "X-Content-Type-Options": "nosniff"})


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/devices")
    async def listing(request: Request):
        who = request.scope["portal"]
        live = {d.tid: d.public() for d in request.app.state.browser.mine(who["user"])}
        now = time.time()
        rows = []
        for tid, row in ext.tokens.load().items():
            if row.get("email") != who["email"] or row.get("kind") not in browser_bridge.KINDS or row.get("expires", 0) <= now:
                continue
            item = {"id": tid, "kind": row["kind"], "label": row.get("label") or "", "created": row.get("created"), "used": row.get("used"),
                    "online": tid in live}
            if tid in live:
                item.update({k: live[tid].get(k) for k in ("os", "host", "version", "state", "connected_at")})
            rows.append(item)
        rows.sort(key=lambda d: (not d["online"], d["kind"], d["label"].lower()))
        return {"devices": rows, "portal": portal_origin(request), "agent_version": agent_version(), "extension": EXTENSION_ZIP.exists()}

    @r.delete("/api/devices/{tid}")
    async def disconnect(tid: str, request: Request):
        who = request.scope["portal"]
        row = ext.tokens.row(tid) if TID_RE.fullmatch(tid) else None
        if row is None or row.get("email") != who["email"] or row.get("kind") not in browser_bridge.KINDS:
            raise HTTPException(404, "no such device")
        ext.tokens.revoke(tid)
        request.app.state.browser.drop_token(tid)
        return {"revoked": True}

    @r.get("/api/devices/download/{what}")
    async def download(what: str, request: Request):
        origin = portal_origin(request)
        if what == "windows":
            return attachment(windows_setup(origin), "ClaudePortal-Setup.cmd")
        if what == "unix":
            return attachment(unix_setup(origin), "claude-portal-setup.sh", "text/x-shellscript")
        if what == "agent":
            return attachment(agent_source(), "portal_agent.py", "text/x-python")
        if what == "extension" and EXTENSION_ZIP.exists():
            return attachment(extension_zip(origin), "claude-browser-bridge.zip", "application/zip")
        raise HTTPException(404, "no such file")

    return r
