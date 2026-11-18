#!/usr/bin/python3 -I
"""Root side of the Access page (decision 229), installed by infra/59-access-bridge.sh as /usr/local/sbin/agent-access-apply.

The portal leaves request.json in its state folder; agent-access.path starts this script. It reads the request as the portal's user
(so a link planted in that folder cannot make root read or write another file), allows only one change against the current
roles.txt: add one member with role user and the id derived from the email, or remove one member who is not the owner. Every other
row must stay as it is. Then it backs up roles.txt, feeds the new list to agent-apply-roles, and answers in result.json.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

ROLES = os.environ.get("ACCESS_ROLES", "/etc/agent-system/roles.txt")
STATE = os.environ.get("ACCESS_STATE", "/home/claude/.local/state/agent-portal/access")
HELPER = os.environ.get("ACCESS_HELPER", "/usr/local/sbin/agent-apply-roles")
BACKUPS = os.environ.get("ACCESS_BACKUPS", "/etc/agent-system/access-backups")
AS_USER = os.environ.get("ACCESS_AS_USER", "claude")  # empty in tests: the files are handled by the current user
def _owner():
    try:  # servers installed from the kit name their owner here
        with open("/etc/agent-system/owner-email", encoding="utf-8") as f:
            return f.read().strip().lower() or "owner@example.invalid"
    except OSError:
        return "owner@example.invalid"


OWNER = os.environ.get("ACCESS_OWNER") or _owner()
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
MAX = 64 * 1024
KEEP = 30

READ = r"""
import os, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
except FileNotFoundError:
    sys.exit(3)
with os.fdopen(fd, "rb") as f:
    sys.stdout.buffer.write(f.read(%d))
""" % (MAX + 1)
WRITE = r"""
import os, sys
folder, data = sys.argv[1], sys.stdin.buffer.read()
temp = os.path.join(folder, ".result.json.%d" % os.getpid())
fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "wb") as f:
    f.write(data)
os.replace(temp, os.path.join(folder, "result.json"))
try:
    os.unlink(os.path.join(folder, "request.json"))
except FileNotFoundError:
    pass
"""


def as_user(code, *args, data=None):
    extra = {"user": AS_USER, "group": AS_USER, "extra_groups": []} if AS_USER else {}
    return subprocess.run([sys.executable, "-I", "-c", code, *args], input=data, capture_output=True, timeout=20, **extra)


def member_id(email):
    return "u-" + hashlib.sha256(email.encode()).hexdigest()[:20]


def rows_of(text):
    rows = []
    for line in text.splitlines():
        parts = line.strip().split()
        if parts and not parts[0].startswith("#"):
            rows.append(tuple(p.lower() for p in parts))
    return rows


def check(current, wanted, change):
    """The one change the Access page may ask for; returns a short description or raises ValueError."""
    kind, email = change.get("kind"), str(change.get("email", "")).lower()
    if not EMAIL.match(email) or email == OWNER:
        raise ValueError("this email cannot be changed from the portal")
    before = {r[0]: r for r in current}
    after = {r[0]: r for r in wanted}
    if len(after) != len(wanted):
        raise ValueError("the same email twice")
    if kind == "add":
        if email in before:
            raise ValueError("this email can already sign in")
        if after.get(email) != (email, "user", member_id(email)):
            raise ValueError("a new member gets role user and its own account id")
        if member_id(email) in {r[2] for r in current if len(r) > 2}:
            raise ValueError("this account id is already taken")
        rest = {k: v for k, v in after.items() if k != email}
    elif kind == "remove":
        if email not in before or email in after:
            raise ValueError("this email is not in the list")
        rest = after
        before = {k: v for k, v in before.items() if k != email}
    else:
        raise ValueError("unknown change")
    if rest != before:
        raise ValueError("other accounts would change too")
    return "%s %s" % ("added" if kind == "add" else "removed", email)


def answer(request_id, ok, message, change=None):
    data = json.dumps({"id": request_id, "ok": ok, "message": message, "at": int(time.time()), "change": change}, ensure_ascii=False)
    done = as_user(WRITE, STATE, data=data.encode())
    print(("applied: " if ok else "refused: ") + message)
    if done.returncode != 0:
        print("could not write result.json: " + done.stderr.decode("utf-8", "replace")[-300:], file=sys.stderr)


def backup(data):
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    name = os.path.join(BACKUPS, time.strftime("roles-%Y%m%d-%H%M%S.txt"))
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    old = sorted(n for n in os.listdir(BACKUPS) if n.startswith("roles-"))
    for n in old[:-KEEP]:
        os.unlink(os.path.join(BACKUPS, n))


def main():
    got = as_user(READ, os.path.join(STATE, "request.json"))
    if got.returncode == 3:
        return 0
    if got.returncode != 0 or len(got.stdout) > MAX:
        answer(None, False, "the request could not be read")
        return 1
    try:
        request = json.loads(got.stdout.decode("utf-8"))
        request_id, text, change = request["id"], request["roles"], request["change"]
        if not (isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{32}", request_id) and isinstance(text, str) and isinstance(change, dict)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        answer(None, False, "the request is malformed")
        return 1
    with open(ROLES, "rb") as f:
        old = f.read()
    try:
        what = check(rows_of(old.decode("utf-8")), rows_of(text), change)
    except ValueError as error:
        answer(request_id, False, str(error), change)
        return 1
    backup(old)
    run = subprocess.run([HELPER], input=text.encode(), capture_output=True, timeout=120)
    if run.returncode != 0:
        answer(request_id, False, (run.stderr or run.stdout).decode("utf-8", "replace").strip()[-400:] or "agent-apply-roles failed", change)
        return 1
    answer(request_id, True, what, change)
    return 0


if __name__ == "__main__":
    sys.exit(main())
