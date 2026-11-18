import contextvars
import hashlib
import os
import re
import threading
from pathlib import Path

from . import config
from .roles import normalize_email

ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")
DEFAULT_ID = "owner"
OWNER_EMAIL = config.OWNER_EMAIL

current: contextvars.ContextVar[str | None] = contextvars.ContextVar("portal_user", default=None)
login: contextvars.ContextVar[str] = contextvars.ContextVar("portal_login", default="")
email: contextvars.ContextVar[str] = contextvars.ContextVar("portal_email", default="")


def device_uid() -> str | None:
    """The account whose computers and browsers this chat may use: the verified account of the request that starts it."""
    value = current.get()
    if value:
        return value
    mail = email.get()
    return account_id(mail) if mail else None


def account_id(value: str) -> str:
    value = normalize_email(value)
    return DEFAULT_ID if value == OWNER_EMAIL else "u-" + hashlib.sha256(value.encode()).hexdigest()[:20]


def login_tag(email: str | None) -> str:
    if not email:
        return ""
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:16]


def parse_users(text: str) -> tuple[dict[str, str], str | None]:
    table: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        email, uid = normalize_email(parts[0]), parts[2].strip().lower()
        if not ID_RE.fullmatch(uid) or email in table:
            continue
        table[email] = uid
    return table, DEFAULT_ID


class UsersStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp = None
        self._table: dict[str, str] = {}
        self._primary: str | None = None
        self._present = False

    def _load(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self._stamp, self._table, self._primary, self._present = None, {}, None, False
            return
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp != self._stamp:
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._stamp, self._table, self._primary, self._present = None, {}, None, True
                return
            self._table, self._primary = parse_users(text)
            self._stamp, self._present = stamp, any(len(x.split()) == 3 for x in text.splitlines() if x.strip() and not x.lstrip().startswith('#'))

    def user_for(self, email: str) -> str | None:
        with self._lock:
            self._load()
            email = normalize_email(email)
            if not self._present:
                return account_id(email)
            value = self._table.get(email)
            if value == DEFAULT_ID and email != OWNER_EMAIL:
                return None
            if email == OWNER_EMAIL and value != DEFAULT_ID:
                return None
            if value and sum(v == value for v in self._table.values()) > 1:
                return None
            return value

    def primary(self) -> str:
        with self._lock:
            self._load()
            return DEFAULT_ID


_store: UsersStore | None = None


def install(store: UsersStore) -> None:
    global _store
    _store = store


def primary_id() -> str:
    return _store.primary() if _store else DEFAULT_ID


def uid() -> str:
    return current.get() or primary_id()


def is_primary(user: str | None = None) -> bool:
    return (user or uid()) == primary_id()


def users_dir() -> Path:
    return Path(os.path.realpath(config.PORTAL_ROOTS[0] / "users"))


def home() -> Path:
    if is_primary():
        return config.HOME.resolve()
    return users_dir() / uid()


def ensure_home() -> Path:
    path = home()
    if not is_primary():
        for d in (users_dir(), path):
            d.mkdir(mode=0o700, exist_ok=True)
            st = d.stat()
            if st.st_uid == os.getuid() and (st.st_mode & 0o700) != 0o700:
                os.chmod(d, 0o700)
    return path


def chat_root() -> Path:
    if is_primary():
        raw = os.environ.get("PORTAL_CHAT_ROOT")
        return Path(raw) if raw else config.PORTAL_ROOTS[0] / "chats"
    return users_dir() / uid() / "chats"


def titles_file() -> Path:
    raw = os.environ.get("PORTAL_CHAT_TITLES")
    base = Path(raw) if raw else config.HOME / ".local" / "state" / "agent-portal" / "chat-titles.json"
    if is_primary():
        return base
    return base.with_name(f"chat-titles-{uid()}.json")


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def path_allowed(value) -> bool:
    if value is None:
        return is_primary()
    try:
        real = Path(os.path.realpath(value))
    except (OSError, ValueError):
        return False
    base = users_dir()
    if _inside(real, base) and real != base:
        return real.relative_to(base).parts[0] == uid()
    owner_chats = Path(os.path.realpath(os.environ.get("PORTAL_CHAT_ROOT", config.PORTAL_ROOTS[0] / "chats")))
    if _inside(real, owner_chats):
        return is_primary()
    return True


def session_owner(tag: str | None) -> str:
    return tag if tag and ID_RE.fullmatch(tag) else primary_id()


def owns_session(tag: str | None) -> bool:
    return session_owner(tag) == uid()
