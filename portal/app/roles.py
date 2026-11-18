import os
import threading
from pathlib import Path

from .config import ROLE_NAMES


def normalize_email(value: str) -> str:
    return value.strip().lower()


def parse_roles(text: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            continue
        email, role = normalize_email(parts[0]), parts[1].strip().lower()
        if role not in ROLE_NAMES or email in table:
            continue
        table[email] = role
    return table


class RolesStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._table: dict[str, str] = {}

    def _load(self) -> dict[str, str]:
        try:
            st = os.stat(self.path)
        except OSError:
            self._stamp = None
            self._table = {}
            return self._table
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp != self._stamp:
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._stamp = None
                self._table = {}
                return self._table
            self._table = parse_roles(text)
            self._stamp = stamp
        return self._table

    def role_for(self, email: str) -> str | None:
        with self._lock:
            return self._load().get(normalize_email(email))
