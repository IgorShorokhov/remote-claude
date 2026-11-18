"""VPN section: Tailscale devices, exit node, WARP switch for VPN clients (docs/specs/vpn-warp.md).

The portal runs with NoNewPrivileges, so it cannot sudo. Root side (infra/92-vpn-warp.sh):
- publishes STATUS (switch state, Tailscale devices, last request result) every 30 s and after each request;
- a systemd path unit runs `agent-vpn-warp request` when REQUEST appears; the file holds one word on|off|status.
"""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from . import config

STATUS = Path(os.environ.get("PORTAL_VPN_STATUS", "/run/agent-status/vpn/vpn.json"))
REQUEST = Path(os.environ.get("PORTAL_VPN_REQUEST", str(config.HOME / ".local" / "state" / "agent-portal" / "vpn-request")))
JOURNAL = Path(os.environ.get("PORTAL_VPN_JOURNAL", str(config.HOME / ".local" / "state" / "agent-portal" / "vpn-journal.jsonl")))
ACTIONS = ("on", "off")
CONFIRM = "route-vpn"
STALE_SECONDS = 120
WAIT_SECONDS = 25.0
POLL = 0.5
JOURNAL_SHOW = 20
JOURNAL_TAIL = 65536
NO_HELPER = "VPN WARP status unavailable (run infra/92-vpn-warp.sh apply on the VPS)"

_switch = asyncio.Lock()


class VpnError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail


def read_status():
    try:
        raw = STATUS.read_bytes()[:262144]
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _switch_view(sw):
    if not isinstance(sw, dict) or sw.get("saved") not in ACTIONS or sw.get("rules") not in ACTIONS:
        return None
    return {"saved": sw["saved"], "rules": sw["rules"], "warp_ok": sw.get("warp_proxy_ok") is True,
            "tun_ok": sw.get("tun_ok") is True, "warp_ip": str(sw.get("warp_ip") or "")[:64]}


def _device(d):
    return {"name": str(d.get("name") or "?")[:64], "ip": str(d.get("ip") or "")[:64], "os": str(d.get("os") or "")[:32],
            "online": d.get("online") is True, "last_seen": d.get("last_seen") if isinstance(d.get("last_seen"), str) else None,
            "active": d.get("active") is True, "self": d.get("self") is True}


def snapshot(now=None):
    now = time.time() if now is None else now
    data = read_status()
    out = {"devices": [], "exit_node": None, "switch": None, "checked": None, "errors": [], "journal": read_journal()}
    if data is None:
        out["errors"].append(NO_HELPER)
        return out
    checked = data.get("checked")
    out["checked"] = checked if isinstance(checked, int) else None
    if out["checked"] is None or now - out["checked"] > STALE_SECONDS:
        out["errors"].append("VPN status is stale (watchdog timer not running?)")
    out["switch"] = _switch_view(data.get("switch"))
    if out["switch"] is None:
        out["errors"].append(NO_HELPER)
    out["devices"] = [_device(d) for d in data.get("devices") or []
                      if isinstance(d, dict) and d.get("name") != "funnel-ingress-node"]
    en = data.get("exit_node")
    out["exit_node"] = {"approved": en.get("approved") is True} if isinstance(en, dict) else None
    return out


def request(action):
    if action not in ACTIONS + ("status",):
        raise VpnError(400, "bad action")
    REQUEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = REQUEST.with_name(f"{REQUEST.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(action + "\n", encoding="utf-8")
        os.replace(tmp, REQUEST)
    finally:
        tmp.unlink(missing_ok=True)


def write_journal(email, action, result):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": int(time.time()), "email": email, "action": action, "result": result})
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_journal(limit=JOURNAL_SHOW):
    try:
        with open(JOURNAL, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - JOURNAL_TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append({k: item.get(k) for k in ("ts", "email", "action", "result")})
    return out


async def switch(email, action, wait=None):
    if action not in ACTIONS:
        raise VpnError(400, "action must be on or off")
    wait = WAIT_SECONDS if wait is None else wait
    async with _switch:
        if REQUEST.exists():
            raise VpnError(409, "another VPN request is pending")
        start = int(time.time())
        request(action)
        result = "no answer from the VPS helper (request still pending)"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL)
            last = (read_status() or {}).get("last")
            if isinstance(last, dict) and last.get("action") == action and isinstance(last.get("ts"), int) and last["ts"] >= start:
                result = str(last.get("result") or "unknown")[:200]
                break
        write_journal(email, action, result)
    return {"result": result, **await asyncio.to_thread(snapshot)}


def build_router():
    from fastapi import APIRouter, HTTPException, Request
    from pydantic import BaseModel, ConfigDict

    class SwitchIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: str
        confirm: str

    r = APIRouter()

    @r.get("/api/vpn")
    async def vpn_info():
        return await asyncio.to_thread(snapshot)

    @r.post("/api/vpn/refresh")
    async def vpn_refresh():
        # Under the same lock as a switch: a status request written between its check and its write would replace "on" or "off".
        async with _switch:
            if not REQUEST.exists():
                await asyncio.to_thread(request, "status")
        return {"ok": True}

    @r.post("/api/vpn/warp")
    async def vpn_switch(body: SwitchIn, request: Request):
        if body.confirm != CONFIRM:
            raise HTTPException(400, "confirmation required")
        try:
            return await switch(request.scope["portal"]["email"], body.action)
        except VpnError as e:
            raise HTTPException(e.status, e.detail)

    return r
