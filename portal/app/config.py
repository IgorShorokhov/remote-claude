import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
ROLES_FILE = Path(os.environ.get("PORTAL_ROLES_FILE", "/etc/agent-system/roles.txt"))
USERS_FILE = Path(os.environ.get("PORTAL_USERS_FILE", str(ROLES_FILE)))
PORTAL_HOST = os.environ.get("PORTAL_HOST", "")
ROLE_NAMES = ("admin", "user")
# The account that owns the server: the only admin, the one who gets owner-only sections. Set by the installer on other servers.
OWNER_EMAIL = os.environ.get("PORTAL_OWNER_EMAIL", "owner@example.invalid").strip().lower()
# Sections this server offers, comma separated, or "all" (app/features.py). A friend's server may run only some of them.
FEATURES = os.environ.get("PORTAL_FEATURES", "all")

HOME = Path(os.environ.get("HOME", "/home/claude"))
def _real(value) -> Path:
    return Path(os.path.realpath(value))


PORTAL_ROOTS = tuple(
    _real(p)
    for p in os.environ.get("PORTAL_ROOTS", str(HOME / "work")).split(":")
    if p
)
PC_LINK = Path(os.environ.get("PORTAL_PC_LINK", str(HOME / "pc")))
PC_ROOT = _real(PC_LINK)
PC_STATUS_FILE = Path(os.environ.get("PORTAL_PC_STATUS", "/run/agent-status/pc.json"))
PC_TIMEOUT = 2.0
PC_STALE_SECONDS = 300
SYNC_STATUS_FILE = Path(os.environ.get("PORTAL_SYNC_STATUS", "/run/agent-status/sync/sync.json"))
SYNC_GUI_SOCK = Path(os.environ.get("PORTAL_SYNC_SOCK", "/run/agent-syncthing/gui.sock"))
SYNCTHING_CONFIG = Path(
    os.environ.get("PORTAL_SYNC_CONFIG", str(HOME / ".local" / "state" / "syncthing" / "config.xml"))
)
BACKUP_STATUS_FILE = Path(os.environ.get("PORTAL_BACKUP_STATUS", "/run/agent-status/backup/backup.json"))
BACKUP_STALE_SECONDS = 36 * 3600
SYNC_STALE_SECONDS = 90
SYNC_TIMEOUT = 1.0
SYNC_FOLDERS = ("work", "backups", "project")
TMUX_BIN = os.environ.get("PORTAL_TMUX_BIN", "tmux")
TMUX_SOCKET = os.environ.get("PORTAL_TMUX_SOCKET", f"/tmp/tmux-{os.getuid()}/default")
CLAUDE_BIN = os.environ.get("PORTAL_CLAUDE_BIN", "claude")
HISTORY_DIR = Path(os.environ.get("PORTAL_HISTORY_DIR", str(HOME / ".claude" / "projects")))
MAX_CLAUDE_SESSIONS = 3  # running Claude chats per account
MAX_CLAUDE_SESSIONS_OWNER = 5  # the owner's account: 2 more than the others
MAX_CLAUDE_TOTAL = 20  # safety cap for the whole server
CHAT_IDLE_SECONDS = 60 * 60
PRUNE_INTERVAL = 6 * 60 * 60
MAX_TOTAL_SESSIONS = 12
MAX_TERM_CLIENTS = 8

# Chats run as Claude Code processes spoken to in stream-json ("agent") or as tmux terminals ("tmux", the old way, kept for rollback).
# The processes live in the agent host service (infra/96-agent-host.sh).
CHAT_MODE = os.environ.get("PORTAL_CHAT_MODE", "agent")
# In agent mode, chats still running in tmux from the old mode are put to sleep as soon as they are idle; waking them starts them as processes.
MIGRATE_LEGACY = os.environ.get("PORTAL_MIGRATE_LEGACY", "1") == "1"
AGENT_HOST_SOCKET = os.environ.get("PORTAL_AGENT_HOST_SOCKET", "/run/agent-host/host.sock")

# Virtual microphone for dictation (infra/95-agent-pulse.sh)
PULSE_SOCKET = os.environ.get("PORTAL_PULSE_SOCKET", "/run/agent-pulse/native")
