"""Limits from Claude Code's status line.

Claude Code runs the status line command after every reply with a JSON document on stdin; for claude.ai subscriptions it carries
rate_limits.five_hour and rate_limits.seven_day (used_percentage, resets_at as epoch seconds). The command below keeps the latest copy per
chat in a file, so the portal can answer from it at once instead of starting a Claude Code session to read /usage.
"""
import json
import os
import time
from pathlib import Path

from . import config

MAX_AGE = 1800
KEEP = 86400


def directory() -> Path:
    return Path(os.environ.get("PORTAL_USAGE_DIR", "/run/agent-portal/usage"))


def command() -> str:
    # Prints nothing, so the status line stays empty and the footer of the CLI keeps its place.
    return (
        "python3 -I -c \"import json,sys,os,time;d=json.load(sys.stdin);r=d.get('rate_limits');s=str(d.get('session_id',''));p='" + str(directory()) + "';"
        "r and s and (os.makedirs(p,exist_ok=True),os.chmod(p,0o700),open(p+'/.'+s,'w').write(json.dumps({'at':time.time(),'rate_limits':r})),os.replace(p+'/.'+s,p+'/'+s+'.json'))\" 2>/dev/null||true"
    )


def read(now=None):
    """(time of the reading, rows) from the freshest chat file, or None when there is nothing recent and complete."""
    now = time.time() if now is None else now
    best = None
    try:
        files = list(directory().glob("*.json"))
    except OSError:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text())
            at = float(data["at"])
            limits = data["rate_limits"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if now - at > KEEP:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if isinstance(limits, dict) and (best is None or at > best[0]):
            best = (at, limits)
    if best is None or now - best[0] > MAX_AGE:
        return None
    rows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "Weekly")):
        window = best[1].get(key)
        try:
            used = max(0, min(100, round(float(window["used_percentage"]))))
            resets = int(window["resets_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if resets <= now:
            return None
        rows.append({"label": label, "used": used, "left": 100 - used, "resets": None, "resets_at": resets})
    return best[0], rows
