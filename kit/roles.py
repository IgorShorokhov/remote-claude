#!/usr/bin/env python3
"""Prints the list of accounts for agent-apply-roles: the current /etc/agent-system/roles.txt plus the owner and the MEMBERS of kit.conf.

Nobody is removed here: emails added or removed on the Access page stay as they are. The owner is the only admin.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import configure  # noqa: E402

ROLES = Path("/etc/agent-system/roles.txt")


def main():
    conf = configure.check(configure.read())
    owner = conf["OWNER_EMAIL"]
    rows = {}
    try:
        text = ROLES.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) in (2, 3) and not parts[0].startswith("#"):
            email = parts[0].lower()
            rows[email] = [email, parts[1].lower(), parts[2].lower() if len(parts) == 3 else configure.member_id(email)]
    for email in list(rows):
        if rows[email][1] == "admin" and email != owner:
            rows[email] = [email, "user", configure.member_id(email)]  # the owner changed in kit.conf: the old one stays a member
    rows[owner] = [owner, "admin", "owner"]
    for email in conf["MEMBERS"].split():
        rows.setdefault(email, [email, "user", configure.member_id(email)])
    ordered = [rows.pop(owner)] + [rows[k] for k in sorted(rows)]
    sys.stdout.write("".join(" ".join(r) + "\n" for r in ordered))


if __name__ == "__main__":
    main()
