"""The skills of this server: folders with a SKILL.md in ~/.claude/skills (decision 225), and the portal's Skills section (decision 228).

Claude Code in the chats loads them itself. API Agent has no Skill tool, so its prompt lists them and it reads a SKILL.md with its
shell before a task that fits. The folder `synced` (what Claude Code downloads from claude.ai) is not used: the skills are kept here.
"""
import hashlib
import io
import os
import re
import shutil
import stat as statmod
import threading
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import config

FIELD = re.compile(r'^(name|description):\s*(.*)$')
SHORT = 140


def folder() -> Path:
    return config.HOME / ".claude" / "skills"


def front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    found, key = {}, None
    for line in text[3:end if end > 0 else 2000].splitlines():
        match = FIELD.match(line)
        if match:
            key = match.group(1)
            found[key] = match.group(2).strip()
        elif key and line.startswith((" ", "\t")):
            found[key] += " " + line.strip()
    return {k: v.strip().strip('"').strip("'").replace("\\n", " ") for k, v in found.items()}


def catalog() -> list[dict]:
    items = []
    try:
        entries = sorted(folder().iterdir())
    except OSError:
        return items
    for entry in entries:
        skill = entry / "SKILL.md"
        if entry.name == "synced" or entry.name.startswith(".") or not skill.is_file():
            continue
        try:
            meta = front_matter(skill.read_text(encoding="utf-8", errors="replace")[:6000])
        except OSError:
            continue
        description = " ".join(meta.get("description", "").split())
        if len(description) > SHORT:
            description = description[:SHORT].rsplit(" ", 1)[0] + "…"
        items.append({"name": meta.get("name") or entry.name, "path": str(skill), "description": description})
    return items


def catalog_note() -> str:
    items = catalog()
    if not items:
        return ""
    lines = "\n".join(f"- {item['name']}: {item['description']}" for item in items)
    return ("\n\nSkills: ready instructions for kinds of tasks, in ~/.claude/skills/<name>/SKILL.md. Before a task that fits one, read its "
            "SKILL.md with `cat` and follow it; a skill marked for every reply always applies.\n" + lines)


# ---------------------------------------------------------------- the Skills section of the portal (decision 228)
# One set of skills for every account: everyone can read them, only an admin can change them, since a change alters Claude for all.
# A deleted skill or replaced file goes to a trash folder, never straight to nothing.


NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
TEXT_MAX = 1024 * 1024
FILE_MAX = 10 * 1024 * 1024
ARCHIVE_MAX = 30 * 1024 * 1024
UNPACKED_MAX = 60 * 1024 * 1024
FILES_MAX = 800
HIDDEN = {"synced"}
LOCK = threading.Lock()
TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

Describe when to use this skill and the steps Claude should follow.
"""


def trash() -> Path:
    # Next to the skills on purpose: in the portal's sandbox ~/.claude and ~/.local/state are different mounts, a move between them fails.
    return config.HOME / ".claude" / "skills-trash"


def skill_dir(name: str) -> Path:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in HIDDEN:
        raise HTTPException(404, "No such skill")
    path = folder() / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise HTTPException(404, "No such skill") from None
    if not statmod.S_ISDIR(info.st_mode):
        raise HTTPException(404, "No such skill")
    return path


def inside(base: Path, relative: str) -> Path:
    """A file of a skill: a relative path without .., no symlink on the way."""
    if not isinstance(relative, str) or not relative or len(relative) > 300 or relative.startswith("/") or "\\" in relative or "\0" in relative:
        raise HTTPException(400, "Bad file path")
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or len(p) > 120 for p in parts):
        raise HTTPException(400, "Bad file path")
    path = base
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise HTTPException(400, "Symbolic links are not edited here")
    return path


def is_text(data: bytes) -> bool:
    if b"\0" in data[:8000]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def files_of(base: Path) -> list[dict]:
    out = []
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(root) / d).is_symlink())
        for name in sorted(names):
            path = Path(root) / name
            if name.startswith(".") or path.is_symlink():
                continue
            info = path.stat()
            out.append({"path": str(path.relative_to(base)), "size": info.st_size, "modified": int(info.st_mtime)})
            if len(out) >= FILES_MAX:
                return out
    out.sort(key=lambda f: (f["path"] != "SKILL.md", f["path"].count("/"), f["path"]))
    return out


def summary(base: Path) -> dict:
    skill = base / "SKILL.md"
    problem, meta = None, {}
    try:
        meta = front_matter(skill.read_text(encoding="utf-8", errors="replace")[:6000])
    except OSError:
        problem = "SKILL.md is missing"
    if not problem and not meta.get("name"):
        problem = "SKILL.md has no name: in its header"
    elif not problem and not meta.get("description"):
        problem = "SKILL.md has no description: in its header"
    files = files_of(base)
    return {"name": base.name, "title": meta.get("name") or base.name, "description": " ".join(meta.get("description", "").split()),
            "files": len(files), "size": sum(f["size"] for f in files),
            "modified": max([f["modified"] for f in files] or [int(base.stat().st_mtime)]), "problem": problem}


def to_trash(path: Path, label: str) -> str:
    target = trash() / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    make_dirs(target.parent)
    while target.exists():
        target = target.with_name(target.name + "-1")
    os.rename(path, target)
    return str(target)


def make_dirs(path: Path) -> None:
    """mkdir -p with modes set explicitly: the portal service runs with umask 0117, which leaves new folders without the x bit."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for part in reversed(missing):
        part.mkdir()
        os.chmod(part, 0o755)


def write_file(path: Path, data: bytes) -> None:
    make_dirs(path.parent)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(data)
    os.chmod(temp, 0o644)
    os.replace(temp, path)


class NewSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)


class SaveFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    text: str = Field(max_length=TEXT_MAX)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str
    path: str | None = None


class Rename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


def unpack(data: bytes, wanted: str | None) -> tuple[str, dict[str, bytes]]:
    """A .skill or .zip archive: SKILL.md at its root or in one top folder. Returns the skill's folder name and its files."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "This is not a .skill or .zip archive") from None
    entries = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/") and not i.filename.split("/")[-1].startswith("._")]
    if len(entries) > FILES_MAX or sum(i.file_size for i in entries) > UNPACKED_MAX:
        raise HTTPException(413, "The archive is too big once unpacked")
    names = [i.filename for i in entries]
    roots = {n.split("/", 1)[0] for n in names}
    prefix = ""
    if "SKILL.md" not in names:
        if len(roots) == 1 and f"{next(iter(roots))}/SKILL.md" in names:
            prefix = next(iter(roots)) + "/"
        else:
            raise HTTPException(400, "The archive has no SKILL.md at its top")
    files = {}
    for info in entries:
        rel = info.filename[len(prefix):]
        if not rel or rel.startswith("/") or ".." in rel.split("/") or "\\" in rel:
            raise HTTPException(400, f"Unsafe path in the archive: {info.filename[:80]}")
        if statmod.S_ISLNK(info.external_attr >> 16):
            raise HTTPException(400, "The archive contains a symbolic link")
        files[rel] = archive.read(info)
    meta = front_matter(files["SKILL.md"].decode("utf-8", "replace")[:6000])
    name = wanted or meta.get("name") or prefix.rstrip("/")
    name = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")[:64]
    if not NAME.fullmatch(name or "") or name in HIDDEN:
        raise HTTPException(400, "Could not tell the skill's name; give it one")
    return name, files


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/skills")

    @router.get("")
    def listing():
        base = folder()
        items = []
        if base.is_dir():
            for entry in sorted(base.iterdir()):
                if entry.name in HIDDEN or entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                    continue
                items.append(summary(entry))
        return {"skills": items, "folder": str(base)}

    @router.post("", status_code=201)
    def create(body: NewSkill):
        with LOCK:
            path = folder() / body.name
            if body.name in HIDDEN or path.exists():
                raise HTTPException(409, "A skill with this name already exists")
            text = TEMPLATE.format(name=body.name, description=" ".join(body.description.split()), title=body.name.replace("-", " ").capitalize())
            write_file(path / "SKILL.md", text.encode())
        return detail(body.name)

    @router.get("/{name}")
    def detail(name: str):
        base = skill_dir(name)
        return {**summary(base), "list": files_of(base)}

    @router.get("/{name}/file")
    def read_file(name: str, path: str = Query(..., max_length=300)):
        target = inside(skill_dir(name), path)
        if not target.is_file():
            raise HTTPException(404, "No such file")
        if target.stat().st_size > TEXT_MAX:
            raise HTTPException(413, "The file is over 1 MB; download it instead")
        data = target.read_bytes()
        if not is_text(data):
            raise HTTPException(415, "Not a text file; download it instead")
        return {"path": path, "text": data.decode("utf-8"), "revision": revision(data)}

    @router.put("/{name}/file")
    def save_file(name: str, body: SaveFile):
        with LOCK:
            base = skill_dir(name)
            target = inside(base, body.path)
            if target.exists():
                if not target.is_file():
                    raise HTTPException(409, "A folder has this name")
                if body.revision != revision(target.read_bytes()):
                    raise HTTPException(409, "The file changed since you opened it. Copy your text and reload.")
            elif body.revision is not None:
                raise HTTPException(409, "The file was deleted since you opened it")
            data = body.text.encode("utf-8")
            write_file(target, data)
        return {"path": body.path, "revision": revision(data), "skill": summary(base)}

    @router.post("/{name}/upload", status_code=201)
    async def upload(name: str, request: Request, path: str = Query(..., max_length=300)):
        base = skill_dir(name)
        target = inside(base, path)
        data = await request.body()
        if len(data) > FILE_MAX:
            raise HTTPException(413, "The file is over 10 MB")
        with LOCK:
            if target.exists():
                if not target.is_file():
                    raise HTTPException(409, "A folder has this name")
                to_trash(target, f"{name}-{target.name}")
            write_file(target, data)
        return {"path": path, "size": len(data)}

    @router.delete("/{name}/file")
    def delete_file(name: str, body: Confirm):
        with LOCK:
            base = skill_dir(name)
            if not body.path or body.confirm != body.path:
                raise HTTPException(409, "Deletion not confirmed")
            if body.path == "SKILL.md":
                raise HTTPException(409, "SKILL.md is the skill itself; delete the whole skill instead")
            target = inside(base, body.path)
            if not target.is_file():
                raise HTTPException(404, "No such file")
            return {"trash": to_trash(target, f"{name}-{target.name}")}

    @router.post("/{name}/rename")
    def rename(name: str, body: Rename):
        with LOCK:
            base = skill_dir(name)
            target = folder() / body.to
            if body.to in HIDDEN or target.exists():
                raise HTTPException(409, "A skill with this name already exists")
            base.rename(target)
            skill = target / "SKILL.md"
            if skill.is_file():
                text = skill.read_text(encoding="utf-8", errors="surrogateescape")
                if text.startswith("---"):
                    text = re.sub(r"^name:.*$", f"name: {body.to}", text, count=1, flags=re.M)
                    write_file(skill, text.encode("utf-8", "surrogateescape"))
        return detail(body.to)

    @router.delete("/{name}")
    def delete(name: str, body: Confirm):
        with LOCK:
            base = skill_dir(name)
            if body.confirm != name:
                raise HTTPException(409, "Deletion not confirmed")
            return {"trash": to_trash(base, name)}

    @router.post("/import", status_code=201)
    async def import_archive(request: Request, name: str | None = Query(None, max_length=64), replace: bool = False):
        data = await request.body()
        if len(data) > ARCHIVE_MAX:
            raise HTTPException(413, "The archive is over 30 MB")
        skill, files = unpack(data, name)
        with LOCK:
            target = folder() / skill
            if target.exists():
                if not replace:
                    raise HTTPException(409, f"The skill {skill} already exists")
                to_trash(target, skill)
            staging = folder() / f".import-{skill}-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            try:
                for rel, content in files.items():
                    write_file(staging / rel, content)
                staging.rename(target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return detail(skill)

    @router.get("/{name}/download")
    def download(name: str):
        base = skill_dir(name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in files_of(base):
                archive.write(base / item["path"], f"{name}/{item['path']}")
        data = buffer.getvalue()
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}.skill"', "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    return router
