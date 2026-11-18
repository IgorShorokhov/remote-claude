"""Sections a server offers (PORTAL_FEATURES). The owner's server runs all of them; the install kit for other servers turns some off.

A section that is off disappears from every account's menu and its API answers 403. Code Web and its chat history are always on.
"""
from . import config

ALL = ("chat", "browser", "console", "backup", "prompts", "skills", "instructions", "memory", "devices", "pc", "sync", "vpn", "access", "voice")
PREFIXES = {
    "backup": ("/api/backup",),
    "prompts": ("/api/prompts",),
    "skills": ("/api/skills",),
    "instructions": ("/api/personal/instructions",),
    "memory": ("/api/personal/memory",),
    "devices": ("/api/devices",),
    "pc": ("/api/pc",),
    "sync": ("/api/sync",),
    "vpn": ("/api/vpn",),
    "access": ("/api/access",),
    "voice": ("/api/speech",),
}


def enabled() -> frozenset[str]:
    raw = (config.FEATURES or "all").strip().lower()
    if raw == "all":
        return frozenset(ALL)
    return frozenset(x.strip() for x in raw.split(",") if x.strip() in ALL)


def on(name: str) -> bool:
    return name in enabled()


def blocked(path: str) -> bool:
    live = enabled()
    return any(name not in live and any(path == p or path.startswith(p + "/") for p in prefixes) for name, prefixes in PREFIXES.items())


def tabs(ids: list[str]) -> list[str]:
    live = enabled()
    return [t for t in ids if t not in ALL or t in live]
