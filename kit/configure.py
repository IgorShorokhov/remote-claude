#!/usr/bin/env python3
"""Settings of a Remote Claude server: asks the questions (or checks kit.conf filled by hand) and writes kit.conf.

    python3 kit/configure.py            questions in the terminal, then kit.conf is written
    python3 kit/configure.py --check    checks kit.conf and prints what will be installed (exit 1 on a mistake)
    python3 kit/configure.py --env      prints the checked settings as shell variables (install.sh reads them)
"""
import hashlib
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "kit.conf"
EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
YES, NO = "yes", "no"

# key, portal section (None: not a portal section), title, what it gives, cost
FEATURES = (
    ("FEATURE_CONSOLE", "console", "Console", "терминал сервера в браузере (только владелец)", "ничего"),
    ("FEATURE_PROMPTS", "prompts", "Prompts", "сохранённые промпты", "ничего"),
    ("FEATURE_SKILLS", "skills", "Skills", "общие скиллы Claude, редактор", "ничего"),
    ("FEATURE_INSTRUCTIONS", "instructions", "System instructions", "свои инструкции для Claude у каждого аккаунта", "ничего"),
    ("FEATURE_MEMORY", "memory", "Memory", "память о пользователе; ночью обновляется моделью Haiku", "немного лимита подписки"),
    ("FEATURE_ACCESS", "access", "Access", "страница владельца: кто входит и какие разделы видит", "ничего"),
    ("FEATURE_DEVICES", "devices", "Computers", "подключить свой компьютер и Chrome к Claude", "ничего"),
    ("FEATURE_API_AGENT", "backup", "API Agent", "агент на моделях OpenAI-совместимого API (свой ключ)", "оплата API"),
    ("FEATURE_VOICE", "voice", "Voice input", "голосовой ввод, распознавание на сервере (Whisper small)", "+1 ГБ RAM, 0,5 ГБ диска"),
)
PRESETS = {
    "minimal": {"FEATURE_PROMPTS", "FEATURE_SKILLS", "FEATURE_INSTRUCTIONS"},
    "standard": {"FEATURE_CONSOLE", "FEATURE_PROMPTS", "FEATURE_SKILLS", "FEATURE_INSTRUCTIONS", "FEATURE_MEMORY", "FEATURE_ACCESS"},
    "full": {k for k, *_ in FEATURES},
}
DEFAULTS = {"OWNER_EMAIL": "", "MEMBERS": "", "SITE_MODE": "domain", "DOMAIN": "", "PROXY": "none", "BACKUPS": YES, "TITLE": "Remote Claude"}


class Mistake(Exception):
    pass


def member_id(email):
    return "u-" + hashlib.sha256(email.encode()).hexdigest()[:20]


def read(path=None):
    path = path or CONF
    if not path.exists():
        raise Mistake(f"нет файла {path.name}: скопируйте kit.conf.example в kit.conf или запустите ./install.sh configure")
    values = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise Mistake(f"kit.conf, строка {n}: нужно КЛЮЧ=значение")
        key, value = line.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise Mistake(f"kit.conf, строка {n}: незакрытая кавычка") from None
        values[key] = " ".join(parts)
    return values


def check(values):
    out = dict(DEFAULTS)
    known = set(DEFAULTS) | {k for k, *_ in FEATURES}
    unknown = sorted(set(values) - known)
    if unknown:
        raise Mistake("неизвестные ключи в kit.conf: " + ", ".join(unknown))
    out.update(values)
    owner = out["OWNER_EMAIL"].strip().lower()
    if not EMAIL.fullmatch(owner):
        raise Mistake("OWNER_EMAIL: нужна почта Google владельца, например name@gmail.com")
    members = []
    for m in out["MEMBERS"].replace(",", " ").split():
        m = m.lower()
        if not EMAIL.fullmatch(m):
            raise Mistake(f"MEMBERS: «{m}» не похоже на почту")
        if m != owner and m not in members:
            members.append(m)
    if out["SITE_MODE"] not in ("domain", "tailscale"):
        raise Mistake("SITE_MODE: domain или tailscale")
    domain = out["DOMAIN"].strip().lower().rstrip(".")
    if out["SITE_MODE"] == "domain":
        if not HOST.fullmatch(domain) or domain.endswith((".ts.net", ".local")):
            raise Mistake("DOMAIN: нужно имя сайта, например claude.example.com (его A-запись указывает на IP этого сервера)")
    else:
        domain = ""
    if out["PROXY"] not in ("none", "warp"):
        raise Mistake("PROXY: none или warp")
    flags = {}
    for key, *_ in FEATURES:
        value = out.get(key, YES if key in PRESETS["standard"] else NO).lower()
        if value not in (YES, NO):
            raise Mistake(f"{key}: yes или no")
        flags[key] = value
    if out["BACKUPS"] not in (YES, NO):
        raise Mistake("BACKUPS: yes или no")
    if members and flags["FEATURE_ACCESS"] == NO:
        pass  # other accounts can sign in; the owner just has no page to change it
    title = out["TITLE"].strip() or "Remote Claude"
    if not re.fullmatch(r"[\w .,+-]{1,40}", title):
        raise Mistake("TITLE: до 40 букв, цифр и пробелов")
    sections = ["code"] + [section for key, section, *_ in FEATURES if flags[key] == YES]
    return {"OWNER_EMAIL": owner, "MEMBERS": " ".join(members), "SITE_MODE": out["SITE_MODE"], "DOMAIN": domain, "PROXY": out["PROXY"],
            "BACKUPS": out["BACKUPS"], "TITLE": title, **flags,
            "PORTAL_FEATURES": ",".join(s for s in sections if s != "code") or "none", "SECTIONS": sections}


def summary(c):
    lines = [f"Владелец: {c['OWNER_EMAIL']}",
             "Другие почты: " + (c["MEMBERS"] or "нет"),
             "Адрес: " + (f"https://{c['DOMAIN']}" if c["SITE_MODE"] == "domain" else "имя *.ts.net от Tailscale (узнаем при установке)"),
             "Прокси к Anthropic: " + ("Cloudflare WARP" if c["PROXY"] == "warp" else "нет, напрямую"),
             "Резервные копии: " + ("каждую ночь, 7 дней" if c["BACKUPS"] == YES else "нет"),
             "Разделы:", "  [x] Code Web: чаты с Claude Code (всегда)"]
    for key, _, title, about, cost in FEATURES:
        lines.append(f"  [{'x' if c[key] == YES else ' '}] {title}: {about}")
    return "\n".join(lines)


def render(c):
    out = ["# Remote Claude: настройки сервера. Поменяли — запустите снова: sudo ./install.sh", ""]
    out += ["# Почта Google владельца: только он администратор", f'OWNER_EMAIL="{c["OWNER_EMAIL"]}"',
            "# Другие почты, которым можно входить (через пробел); потом их меняют на странице Access", f'MEMBERS="{c["MEMBERS"]}"', "",
            "# Как сайт виден из интернета: domain (свой домен, сертификат Let's Encrypt) или tailscale (Tailscale Funnel, имя *.ts.net)",
            f'SITE_MODE="{c["SITE_MODE"]}"', "# Для SITE_MODE=domain: имя сайта, A-запись которого указывает на IP сервера", f'DOMAIN="{c["DOMAIN"]}"', "",
            "# none: сервер ходит к Anthropic напрямую; warp: через Cloudflare WARP (если Anthropic недоступен из страны сервера)",
            f'PROXY="{c["PROXY"]}"', "",
            "# Разделы: yes или no. Code Web есть всегда. Готовые наборы:",
            "#   минимум   PROMPTS SKILLS INSTRUCTIONS",
            "#   обычный   минимум + CONSOLE MEMORY ACCESS (по умолчанию)",
            "#   всё       обычный + DEVICES API_AGENT VOICE"]
    for key, _, title, about, cost in FEATURES:
        out.append(f'{key}="{c[key]}"  # {title}: {about}; цена: {cost}')
    out += ["", "# Ночные резервные копии чатов и настроек (7 дней, /var/backups/remote-claude)", f'BACKUPS="{c["BACKUPS"]}"',
            "# Название на странице входа", f'TITLE="{c["TITLE"]}"', ""]
    return "\n".join(out)


def ask(text, default="", check_fn=None):
    while True:
        hint = f" [{default}]" if default else ""
        try:
            value = input(f"{text}{hint}: ").strip() or default
        except EOFError:
            raise SystemExit("\nвопросы прерваны, kit.conf не записан")
        if check_fn is None:
            return value
        problem = check_fn(value)
        if not problem:
            return value
        print("  " + problem)


def choose(text, options, default):
    print(text)
    for i, (value, label) in enumerate(options, 1):
        print(f"  {i}. {label}" + ("  (по умолчанию)" if value == default else ""))
    while True:
        raw = ask("Номер", str([v for v, _ in options].index(default) + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  нужен номер из списка")


def interview():
    try:
        old = check(read())
    except Mistake:
        old = dict(DEFAULTS)
    print("Настройка Remote Claude. Enter оставляет значение в скобках.\n")
    v = {}
    v["OWNER_EMAIL"] = ask("Почта Google владельца", old.get("OWNER_EMAIL", ""),
                           lambda x: None if EMAIL.fullmatch(x.lower()) else "нужна почта, например name@gmail.com").lower()
    v["MEMBERS"] = ask("Почты друзей, которым можно входить (через пробел, можно пусто)", old.get("MEMBERS", ""),
                       lambda x: None if all(EMAIL.fullmatch(m.lower()) for m in x.replace(",", " ").split()) else "одна из почт написана неверно")
    v["SITE_MODE"] = choose("\nКак открывать сайт из интернета?", [
        ("domain", "свой домен (нужна A-запись домена на IP сервера; сертификат сделается сам)"),
        ("tailscale", "Tailscale Funnel (без домена, нужен аккаунт Tailscale; адрес вида имя.tailXXXX.ts.net)")], old.get("SITE_MODE", "domain"))
    v["DOMAIN"] = ""
    if v["SITE_MODE"] == "domain":
        v["DOMAIN"] = ask("Имя сайта, например claude.example.com", old.get("DOMAIN", ""),
                          lambda x: None if HOST.fullmatch(x.lower().rstrip(".")) else "нужно имя вида claude.example.com").lower().rstrip(".")
    v["PROXY"] = choose("\nСервер в стране, откуда Anthropic недоступен (например, Россия)?", [
        ("none", "нет, сервер в другой стране: ходить напрямую"),
        ("warp", "да: ходить к Anthropic через Cloudflare WARP")], old.get("PROXY", "none"))
    preset = choose("\nКакие разделы нужны?", [
        ("minimal", "минимум: Code Web, Prompts, Skills, System instructions"),
        ("standard", "обычный: минимум + Console, Memory, Access"),
        ("full", "всё: обычный + Computers, API Agent, голосовой ввод"),
        ("custom", "выбрать каждый раздел")], "standard")
    for key, *_ in FEATURES:
        v[key] = YES if preset != "custom" and key in PRESETS[preset] else old.get(key, NO)
    if preset == "custom":
        for key, _, title, about, cost in FEATURES:
            default = old.get(key, YES if key in PRESETS["standard"] else NO)
            answer = ask(f"  {title} — {about} (цена: {cost}). Включить? yes/no", default,
                         lambda x: None if x.lower() in (YES, NO) else "yes или no")
            v[key] = answer.lower()
    v["BACKUPS"] = ask("\nНочные резервные копии? yes/no", old.get("BACKUPS", YES), lambda x: None if x.lower() in (YES, NO) else "yes или no").lower()
    v["TITLE"] = ask("Название на странице входа", old.get("TITLE", "Remote Claude"))
    final = check(v)
    print("\n" + summary(final) + "\n")
    if ask("Записать kit.conf? yes/no", YES).lower() != YES:
        raise SystemExit("ничего не записано")
    CONF.write_text(render(final), encoding="utf-8")
    owner = ROOT.stat()
    if os.geteuid() == 0 and owner.st_uid != 0:
        os.chown(CONF, owner.st_uid, owner.st_gid)  # run with sudo: the file stays editable by the one who cloned the kit
    print(f"записан {CONF}")


def main(argv):
    try:
        if argv[1:] == ["--check"]:
            print(summary(check(read())))
        elif argv[1:] == ["--env"]:
            c = check(read())
            c["MEMBER_ROWS"] = "".join(f"{m} user {member_id(m)}\n" for m in c["MEMBERS"].split())
            c["SECTIONS"] = " ".join(c["SECTIONS"])
            for key, value in c.items():
                print(f"KIT_{key}={shlex.quote(value)}")
        elif len(argv) == 1:
            interview()
        else:
            print(__doc__)
            return 2
    except Mistake as error:
        print("ОШИБКА в настройках: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
