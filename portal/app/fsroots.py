import functools
import os
import re
from pathlib import Path

from . import config, pcguard, users

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class FsError(pcguard.PcError):
    pass


@functools.lru_cache(maxsize=32)
def _resolved(roots: tuple[str, ...]) -> tuple[Path, ...]:
    out = []
    for r in roots:
        try:
            out.append(Path(os.path.realpath(r)))
        except OSError:
            continue
    return tuple(out)


def real_roots(roots) -> list[Path]:
    result = list(_resolved(tuple(str(r) for r in roots)))
    if not users.is_primary():
        result.append(users.ensure_home())
    return result


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def resolve_dir(value: str, roots) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise FsError(400, "bad path")
    if ".." in Path(value).parts:
        raise FsError(403, "outside roots")
    try:
        real = Path(os.path.realpath(value))
    except OSError:
        raise FsError(400, "bad path")
    if not any(inside(real, r) for r in real_roots(roots)) or not users.path_allowed(real):
        raise FsError(403, "outside roots")
    if not real.is_dir():
        raise FsError(404, "not a directory")
    return real


def list_subdirs(path: Path, roots) -> list[str]:
    rr = real_roots(roots)
    rel = pcguard.pc_relative(path)
    if rel is not None and not rel:
        return sorted(pcguard.connected_names(pcguard.read_status()), key=str.lower)[:500]
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=True):
                        continue
                    real = Path(os.path.realpath(e.path))
                except OSError:
                    continue
                if any(inside(real, r) for r in rr) and users.path_allowed(real):
                    out.append(e.name)
    except OSError:
        raise FsError(403, "cannot read")
    out.sort(key=str.lower)
    return out[:500]


def make_subdir(parent: str, name: str, roots) -> Path:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.endswith("."):
        raise FsError(400, "bad name")
    if pcguard.is_pc_path(parent):
        raise FsError(403, "create the folder via Claude Code")
    base = resolve_dir(parent, roots)
    if pcguard.is_pc_path(base):
        raise FsError(403, "create the folder via Claude Code")
    target = base / name
    if os.path.lexists(target):
        raise FsError(409, "exists")
    try:
        os.mkdir(target, 0o700)
        os.chmod(target, 0o700)
    except OSError as e:
        raise FsError(503, "cannot create")
    real = Path(os.path.realpath(target))
    if not any(inside(real, r) for r in real_roots(roots)):
        os.rmdir(target)
        raise FsError(403, "outside roots")
    return real


def expand_user(value):
    if not isinstance(value, str):
        raise FsError(400, "bad path")
    value = value.strip()
    if value == "~":
        value = str(config.HOME)
    elif value.startswith("~/"):
        value = str(config.HOME) + value[1:]
    if not value.startswith("/"):
        raise FsError(400, "full path required (/ or ~/)")
    return value


def remove_dir(value, confirm, roots) -> None:
    value = expand_user(value)
    if len(value) > 4096 or "\x00" in value or ".." in Path(value).parts:
        raise FsError(400, "bad path")
    if pcguard.is_pc_path(value):
        raise FsError(403, "delete PC folders via the console")
    lexical = os.path.abspath(value)
    if os.path.islink(lexical):
        raise FsError(403, "symlink")
    real = resolve_dir(lexical, roots)
    if pcguard.is_pc_path(real):
        raise FsError(403, "delete PC folders via the console")
    rr = real_roots(roots)
    if real in rr:
        raise FsError(403, "cannot delete root")
    if not isinstance(confirm, str) or confirm != real.name:
        raise FsError(400, "confirm mismatch")
    try:
        os.rmdir(real)
    except OSError as e:
        import errno

        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise FsError(409, "folder not empty")
        raise FsError(503, "cannot remove")


PATH_MESSAGE = "Path must be relative to home, e.g. work/project; no . or .."
PC_CREATE_MESSAGE = "Cannot create laptop folders; pick an existing one"


def normalize_home_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\x00\r\n"):
        raise FsError(400, PATH_MESSAGE)
    if value == "~":
        return "~"
    if value.startswith("~/"):
        value = value[2:]
    elif value.startswith(("/", "~")):
        raise FsError(400, PATH_MESSAGE)
    parts = [p for p in value.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise FsError(400, PATH_MESSAGE)
    return "~/" + "/".join(parts)


def home_path(value):
    normalized = normalize_home_path(value)
    home = config.HOME.resolve()
    lexical = config.HOME / normalized.removeprefix("~/") if normalized != "~" else config.HOME
    try:
        real = lexical.resolve()
        pc = lexical == config.PC_LINK or inside(lexical, config.PC_LINK)
        trusted_pc = config.PC_LINK == config.HOME / "pc" and config.PC_LINK.resolve() == config.PC_ROOT
        if pc:
            if not trusted_pc or not inside(real, config.PC_ROOT):
                raise FsError(400, "Link leaves the allowed laptop folder")
        elif not inside(real, home):
            raise FsError(400, "Link leaves home folder")
        if not users.path_allowed(real):
            raise FsError(403, "Folder is outside your files")
        if real.exists() and not real.is_dir():
            raise FsError(400, "Path is a file, need a folder")
        work = home / "work"
        can_create = not pc and inside(lexical, config.HOME / "work") and inside(real, work) and work.resolve() == work
        return {"path": normalized, "absolute_path": str(real), "exists": real.is_dir(), "can_create": can_create,
                "message": PC_CREATE_MESSAGE if pc else "New folders only inside ~/work"}
    except (OSError, RuntimeError):
        raise FsError(400, "Could not check folder path")


def make_home_dir(value):
    info = home_path(value)
    if not info["can_create"]:
        raise FsError(400, info["message"])
    real = Path(info["absolute_path"])
    root = config.HOME.resolve() / "work"
    fd = None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in real.relative_to(root).parts:
            created = False
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if created:
                os.fchmod(fd, 0o755)
    except OSError:
        raise FsError(400, "Could not create folder: check path and permissions")
    finally:
        if fd is not None:
            os.close(fd)
    return home_path(value)


async def ahome_path(value):
    return await pcguard.call(home_path, value, pc=True)


def roots_list(roots) -> list[str]:
    out = []
    for r in real_roots(roots):
        if pcguard.is_pc_path(r):
            if pcguard.connected_names(pcguard.read_status()):
                out.append(str(r))
        elif r.is_dir():
            out.append(str(r))
    return out


async def aresolve_dir(value, roots) -> Path:
    value = expand_user(value)
    pc = pcguard.is_pc_path(value)
    if pc:
        pcguard.precheck(value)
    return await pcguard.call(resolve_dir, value, roots, pc=pc)


async def alist_subdirs(path: Path, roots) -> list[str]:
    return await pcguard.call(list_subdirs, path, roots, pc=pcguard.is_pc_path(path))


async def amake_subdir(parent: str, name: str, roots) -> Path:
    return await pcguard.call(make_subdir, expand_user(parent), name, roots, pc=False)


async def aremove_dir(value, confirm, roots) -> None:
    return await pcguard.call(remove_dir, value, confirm, roots, pc=False)
