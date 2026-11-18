import asyncio
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from . import agentctl, config, fsroots, identity, ownership, users
from .errors import TmuxError

NAME_NEW_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
NAME_ANY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,31}")
FMT = "#{session_name}\t#{session_path}\t#{session_activity}\t#{session_attached}\t#{session_created}\t#{?@portal_user,#{@portal_user},#{PORTAL_USER}}\t#{@portal_login}\t#{@portal_started_at}"


_lock = asyncio.Lock()
_recent_claude: dict[str, float] = {}
RECENT_TTL = 60.0


def esc(value: str) -> str:
    return value.replace("#", "##")


def child_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(config.HOME)),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }


def tmux_bin() -> str:
    found = shutil.which(config.TMUX_BIN)
    if not found:
        raise TmuxError(503, "tmux not found")
    return found


def base_cmd() -> list[str]:
    return [tmux_bin(), "-N", "-S", config.TMUX_SOCKET]


async def run(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *base_cmd(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TmuxError(504, "tmux not responding")
    return proc.returncode, out.decode("utf-8", "replace")


async def list_sessions(all_users: bool = False) -> list[dict] | None:
    await agentctl.refresh()
    agents = [c.item() for c in agentctl.hub.visible(all_users)]
    items = await _tmux_sessions(all_users)
    if items is None:
        return agents if agentctl.hub.connected else None
    return items + agents


async def _tmux_sessions(all_users: bool = False) -> list[dict] | None:
    if not os.path.exists(config.TMUX_SOCKET):
        return None
    rc, out = await run("list-sessions", "-F", FMT)
    if rc != 0:
        return None
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        while 5 <= len(parts) < 8:
            parts.append("")
        if len(parts) != 8 or not NAME_ANY_RE.fullmatch(parts[0]):
            continue
        if not all_users and not users.owns_session(parts[5]):
            continue
        try:
            items.append(
                {
                    "name": parts[0],
                    "path": parts[1],
                    "activity": int(parts[2]),
                    "attached": int(parts[3]),
                    "created": int(parts[4]),
                    "login": parts[6],
                    "started": float(parts[7]) if parts[7] else 0,
                }
            )
        except ValueError:
            continue
    return items


async def pane_pids() -> dict[str, list[int]]:
    rc, out = await run("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    res: dict[str, list[int]] = {}
    if rc != 0:
        return res
    for line in out.splitlines():
        name, _, pid = line.partition("\t")
        if pid.isdigit():
            res.setdefault(name, []).append(int(pid))
    return res


def _proc_table(proc_root: Path) -> dict[int, tuple[int, str]]:
    table = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return table
    for e in entries:
        if not e.isdigit():
            continue
        try:
            raw = (proc_root / e / "stat").read_text()
        except OSError:
            continue
        i, j = raw.find("("), raw.rfind(")")
        if i < 0 or j < 0:
            continue
        comm = raw[i + 1 : j]
        rest = raw[j + 2 :].split()
        if len(rest) < 2 or not rest[1].isdigit():
            continue
        table[int(e)] = (int(rest[1]), comm)
    return table


_VER_RE = re.compile(r"^\d+\.\d+\.\d+")


def claude_sessions(panes: dict[str, list[int]], proc_root: Path = Path("/proc")) -> set[str]:
    table = _proc_table(proc_root)
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found = set()
    for name, pids in panes.items():
        stack = list(pids)
        seen = set()
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            comm = table.get(p, (0, ""))[1]
            if comm == "claude" or _VER_RE.match(comm):
                found.add(name)
                break
            stack.extend(children.get(p, []))
    return found


async def require_server(all_users: bool = False) -> list[dict]:
    items = await list_sessions(all_users)
    if items is None:
        raise TmuxError(503, "tmux not running")
    return items


async def claude_count(proc_root: Path = Path("/proc")) -> tuple[int, set[str]]:
    names = claude_sessions(await pane_pids(), proc_root) | {c.name for c in agentctl.hub.chats.values() if c.alive}
    return len(names), names


async def shell_only(name: str) -> bool:
    pids = (await pane_pids()).get(name, [])
    table = await asyncio.to_thread(_proc_table, Path('/proc'))
    return (len(pids) == 1 and table.get(pids[0], (0, ''))[1] in ('bash', 'sh', 'zsh', 'fish')
            and not any(parent == pids[0] for parent, _ in table.values()))


def claude_starting(name: str) -> bool:
    started = _recent_claude.get(name)
    return started is not None and time.monotonic() - started <= RECENT_TTL


async def option(name: str, key: str) -> str | None:
    if not NAME_ANY_RE.fullmatch(name):
        raise TmuxError(400, "bad name")
    if agentctl.has(name):
        return agentctl.hub.get(name).option(key)
    rc, out = await run("show-options", "-v", "-t", f"={name}:", key)
    out = out.strip()
    if rc != 0 or not out:
        return None
    return out


async def occupied_names(items: list[dict]) -> set[str]:
    _, names = await claude_count()
    now = time.monotonic()
    existing = {i["name"] for i in items}
    for n, t in list(_recent_claude.items()):
        if now - t > RECENT_TTL or n not in existing:
            del _recent_claude[n]
    starting = {i['name'] for i in items if 0 <= time.time() - i.get('started', 0) <= RECENT_TTL}
    return (names | set(_recent_claude) | starting) & existing


async def _claude_total(items: list[dict]) -> int:
    return len(await occupied_names(items))


def session_limit():
    return config.MAX_CLAUDE_SESSIONS_OWNER if identity.sender() == identity.OWNER else config.MAX_CLAUDE_SESSIONS


async def create(
    name: str,
    cwd: str,
    start_claude: bool,
    command: list[str] | None = None,
    tag: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    who = identity.sender()

    async with _lock:
        items = await require_server(all_users=True)
        if any(i["name"] == name for i in items):
            raise TmuxError(409, "session exists")
        if len(items) >= config.MAX_TOTAL_SESSIONS:
            raise TmuxError(409, f"{len(items)} sessions already, limit {config.MAX_TOTAL_SESSIONS}")
        if start_claude:
            running = await occupied_names(items)
            mine = running & {i["name"] for i in await list_sessions() or []}
            limit = session_limit()
            if len(mine) >= limit:
                raise TmuxError(409, f"{len(mine)} of {limit} chats are already running for your account. "
                                     "Put one to sleep and try again.")
            if len(running) >= config.MAX_CLAUDE_TOTAL:
                raise TmuxError(409, f"The server is already running {len(running)} of {config.MAX_CLAUDE_TOTAL} Claude sessions. "
                                     "Try again when one goes to sleep.")
        if start_claude and config.CHAT_MODE == "agent":
            await _create_agent(name, cwd, command or [config.CLAUDE_BIN], tag, model, effort, who)
            return
        rc, out = await run(
            "new-session", "-d", "-s", esc(name), "-c", esc(cwd),
            "-e", "TERM=xterm-256color", "-e", "COLORTERM=truecolor",
            "-e", "PORTAL_USER=" + users.uid(),
            "-e", "PULSE_SERVER=unix:" + config.PULSE_SOCKET,
        )
        if rc != 0:
            raise TmuxError(500, "could not create session")
        rc, _ = await run("set-option", "-t", f"={name}:", "mouse", "off")
        if rc != 0:
            await run("kill-session", "-t", f"={name}")
            raise TmuxError(500, "could not configure session")
        rc, _ = await run("set-option", "-t", f"={name}:", "@portal_user", users.uid())
        if rc == 0:
            rc, _ = await run("set-option", "-t", f"={name}:", "@portal_sender", who)
        if rc == 0 and users.login.get():
            rc, _ = await run("set-option", "-t", f"={name}:", "@portal_login", users.login.get())
        if rc != 0:
            await run("kill-session", "-t", f"={name}")
            raise TmuxError(500, "could not tag session")
        rc, out = await run("display-message", "-p", "-t", f"={name}:", "#{pane_current_path}")
        actual = out.strip()
        ok = False
        if rc == 0 and actual and users.path_allowed(actual):
            real = Path(os.path.realpath(actual))
            ok = any(fsroots.inside(real, r) for r in fsroots.real_roots(config.PORTAL_ROOTS))
            if not ok:
                ok = real == Path(cwd) and fsroots.inside(real, config.HOME.resolve())
        if not ok:
            await run("kill-session", "-t", f"={name}")
            raise TmuxError(403, "session folder not allowed")
        if tag:
            try:
                await asyncio.to_thread(ownership.claim, tag)
            except (OSError, ValueError):
                await run("kill-session", "-t", f"={name}")
                raise TmuxError(503, "could not save chat ownership")
            rc, _ = await run("set-option", "-t", f"={name}:", "@resume_sid", tag)
            if rc != 0:
                await run("kill-session", "-t", f"={name}")
                raise TmuxError(500, "could not tag session")
        if start_claude:
            for key in ('@portal_started_at', '@portal_last_used'):
                rc, _ = await run('set-option', '-t', f'={name}:', key, str(time.time()))
                if rc:
                    await run('kill-session', '-t', f'={name}')
                    raise TmuxError(500, 'Could not save runtime activity')
            _recent_claude[name] = time.monotonic()
            if model is not None:
                choices = [("@portal_model", model), ("@portal_model_state", "launch_requested")]
                if effort is not None:
                    choices.append(("@portal_effort", effort))
                for key, value in choices:
                    rc, _ = await run("set-option", "-t", f"={name}:", key, value)
                    if rc != 0:
                        await run("kill-session", "-t", f"={name}")
                        _recent_claude.pop(name, None)
                        raise TmuxError(500, "could not save model choice")
            try:
                text = shlex.join(identity.with_identity(command or [config.CLAUDE_BIN], who))
            except OSError:
                _recent_claude.pop(name, None)
                await run("kill-session", "-t", f"={name}")
                raise TmuxError(503, "Could not prepare the chat instructions; slot released")

            rc, _ = await run("send-keys", "-t", f"={name}:", "-l", text)
            if rc == 0:
                rc, _ = await run("send-keys", "-t", f"={name}:", "Enter")
            if rc != 0:
                _recent_claude.pop(name, None)
                await run("kill-session", "-t", f"={name}")
                raise TmuxError(500, "Claude Code not started; slot released")


async def _create_agent(name, cwd, command, tag, model, effort, who) -> None:
    real = Path(os.path.realpath(cwd))
    inside = any(fsroots.inside(real, r) for r in fsroots.real_roots(config.PORTAL_ROOTS))
    if not (users.path_allowed(str(real)) and (inside or (real == Path(cwd) and fsroots.inside(real, config.HOME.resolve())))):
        raise TmuxError(403, "session folder not allowed")
    if tag:
        try:
            await asyncio.to_thread(ownership.claim, tag)
        except (OSError, ValueError):
            raise TmuxError(503, "could not save chat ownership")
    try:
        argv = identity.with_agent_identity(command, who)
    except OSError:
        raise TmuxError(503, "Could not prepare the chat instructions; slot released")
    now = time.time()
    meta = {"sid": tag or "", "owner": users.uid(), "sender": who, "login": users.login.get() or "", "started": now, "last_used": now,
            "model": model or "", "effort": effort or "", "model_state": "launch_requested" if model else ""}
    _recent_claude[name] = time.monotonic()
    try:
        await agentctl.hub.ensure()
        await agentctl.hub.start(name, str(real), argv, {"PORTAL_USER": users.uid(), "CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}, meta)
    except TmuxError:
        _recent_claude.pop(name, None)
        raise


async def require_session(name: str) -> dict:
    if not NAME_ANY_RE.fullmatch(name):
        raise TmuxError(400, "bad name")
    items = await require_server()
    for item in items:
        if item["name"] == name:
            return item
    raise TmuxError(404, "no such session")


async def set_title(name: str, title: str) -> None:
    async with _lock:
        await require_session(name)
        if agentctl.has(name):
            agentctl.hub.get(name).meta["title"] = title
            await agentctl.hub.call({"op": "meta", "name": name, "meta": {"title": title}})
            return
        rc, _ = await run("set-option", "-t", f"={name}:", "@portal_title", title)
        if rc != 0:
            raise TmuxError(500, "could not rename session")


async def request_model(name: str, model: str) -> None:
    async with _lock:
        await require_session(name)
        if agentctl.has(name):
            await agentctl.chat_for(name).operate(model, None)
            return
        panes = await pane_pids()
        _, names = await claude_count()
        if name not in names or len(panes.get(name, [])) != 1:
            raise TmuxError(409, "Needs a single-pane Claude session; use the console")
        for key, value in (("@portal_model", model), ("@portal_model_state", "switch_requested")):
            rc, _ = await run("set-option", "-t", f"={name}:", key, value)
            if rc != 0:
                raise TmuxError(500, "could not save model request")
        rc, _ = await run("send-keys", "-t", f"={name}:", "-l", f"/model {model}")
        if rc == 0:
            rc, _ = await run("send-keys", "-t", f"={name}:", "Enter")
        if rc != 0:
            raise TmuxError(500, "could not send request; check the console")


async def interrupt(name: str) -> None:
    await require_session(name)
    if agentctl.has(name):
        await agentctl.chat_for(name).interrupt()
        return
    rc, _ = await run("send-keys", "-t", f"={name}:", "Escape")
    if rc != 0:
        raise TmuxError(500, "could not send Esc")


async def kill(name: str) -> None:
    items = await require_server()
    if not any(i["name"] == name for i in items):
        raise TmuxError(404, "no such session")
    if agentctl.has(name):
        await agentctl.hub.kill(name)
        return
    rc, _ = await run("kill-session", "-t", f"={name}")
    if rc != 0:
        raise TmuxError(500, "could not close session")
