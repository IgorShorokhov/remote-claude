#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
MODE=${1:-apply}
changed=0
LASTPUT=0
restart_portal=0
ROOT=/opt/agent-portal
ENVF=/etc/agent-system/portal.env
SOCK=/run/agent-portal/portal.sock

[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }

put() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  LASTPUT=0
  if [ ! -f "$dest" ] || ! cmp -s "$tmp" "$dest" || [ "$(stat -c %a "$dest")" != "$mode" ] || [ "$(stat -c %U:%G "$dest")" != "$owner" ]; then
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    changed=1
    LASTPUT=1
    case "$dest" in
      */agent-portal.service|*/agent-portal.slice|*/agent-portal-sockperm) restart_portal=1 ;;
    esac
  fi
  rm -f "$tmp"
}

ENVBAK=0
backup_env() {
  if [ "$ENVBAK" = 0 ] && [ -s "$ENVF" ]; then
    cp -p "$ENVF" "$ENVF.bak-$(date +%Y%m%d-%H%M%S)"
    ENVBAK=1
  fi
}

setenvkey() {
  local k=$1 v=$2
  if grep -q "^$k=" "$ENVF" 2>/dev/null; then
    if [ "$(grep "^$k=" "$ENVF")" != "$k=$v" ]; then
      backup_env
      sed -i "s|^$k=.*|$k=$v|" "$ENVF"
      changed=1
      restart_portal=1
    fi
  else
    backup_env
    echo "$k=$v" >> "$ENVF"
    changed=1
    restart_portal=1
  fi
}

SANDBOX=$(cat <<'EOT'
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/run/agent-portal /home/claude/work -/home/claude/.claude -/home/claude/.claude.json -/home/claude/.local/state/agent-portal
PrivateTmp=no
PrivateDevices=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictRealtime=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
EOT
)

SANDBOX_TEST=$(cat <<'PYT'
import fcntl, os, subprocess, termios
def line(k, ok):
    print(("ок: " if ok else "ПРОВАЛ: ") + k)
try:
    m, sl = os.openpty()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            fcntl.ioctl(sl, termios.TIOCSCTTY, 0)
            os.execvp("tmux", ["tmux", "-V"])
        except Exception:
            os._exit(3)
    _, st = os.waitpid(pid, 0)
    line("в песочнице службы: pty, setsid, TIOCSCTTY и запуск tmux", st == 0)
except Exception as e:
    line("в песочнице службы: pty (%s)" % e, False)
r = subprocess.run(["tmux", "list-sessions"], capture_output=True)
line("в песочнице службы виден сокет tmux (сервер agent-tmux)", r.returncode == 0)
try:
    os.listdir("/home/claude/.claude/projects"); ok = True
except Exception:
    ok = False
line("чтение ~/.claude/projects", ok)
f = "/home/claude/.claude/projects/.portal-check"
try:
    open(f, "w").close(); os.unlink(f); ok = True
except Exception:
    ok = False
line("запись в ~/.claude/projects (удаление и переименование чатов)", ok)
d = "/home/claude/work/.portal-check"
cwd = os.getcwd()
try:
    os.mkdir(d)
    os.chmod(d, 0o700)
    os.chdir(d)
    ok = (os.stat(".").st_mode & 0o777) == 0o700
except Exception:
    ok = False
finally:
    try:
        os.chdir(cwd)
        os.rmdir(d)
    except Exception:
        pass
line("папка в ~/work: создать, chmod 700, войти (cd) при UMask=0117", ok)
for f in ("/home/claude/.ssh/.portal-check", "/home/claude/.bashrc.portal-check", "/home/claude/.portal-check"):
    try:
        open(f, "w").close(); os.unlink(f); ok = False
    except Exception:
        ok = True
    line("запись в %s запрещена" % f.replace("/.portal-check", "").replace(".bashrc.portal-check", ".bashrc").replace("/home/claude", "~"), ok)
if os.path.isdir("/run/agent-status"):
    try:
        os.listdir("/run/agent-status"); ok = True
    except Exception:
        ok = False
    line("видна папка состояния /run/agent-status (читается)", ok)
else:
    print("заметка: /run/agent-status ещё нет (его создаёт 62 apply)")
if os.path.isdir("/srv/pc"):
    try:
        os.listdir("/srv/pc"); ok = True
    except Exception:
        ok = False
    line("видна и читается /srv/pc (корень папок ПК; если провал — 62 apply: root:agent-portal 750 и claude в группе agent-portal)", ok)
    try:
        open("/srv/pc/.portal-check", "w").close(); os.unlink("/srv/pc/.portal-check"); ok = False
    except Exception:
        ok = True
    line("запись в /srv/pc запрещена", ok)
else:
    print("заметка: /srv/pc ещё нет (его создаёт 62 apply)")
PYT
)

check() {
  local bad=0
  say() { echo "$1"; }
  fail() { echo "ПРОВАЛ: $1"; bad=1; }
  getent group agent-portal >/dev/null && say "группа agent-portal есть" || fail "нет группы agent-portal"
  id -nG caddy 2>/dev/null | tr ' ' '\n' | grep -qx agent-portal && say "caddy в группе agent-portal" || say "caddy получает группу через SupplementaryGroups в службе"
  [ "$(stat -c %a:%U $ROOT)" = "755:root" ] && say "$ROOT 755 root" || fail "права $ROOT"
  python3 -c 'import ensurepip' 2>/dev/null && say "python3-venv на месте" || fail "нет ensurepip (python3-venv)"
  if [ -L $ROOT/app ]; then say "app -> $(readlink $ROOT/app)"; fi
  if [ -L $ROOT/venv ]; then say "venv -> $(readlink $ROOT/venv)"; fi
  grep -qE '^PORTAL_HOST=[a-z0-9.-]+\.[a-z]{2,}$' $ENVF && say "PORTAL_HOST задан" || fail "PORTAL_HOST не задан"
  grep -qx 'PORTAL_CLAUDE_BIN=/home/claude/.local/bin/claude' $ENVF && say "PORTAL_CLAUDE_BIN задан" || fail "PORTAL_CLAUDE_BIN не задан"
  runuser -u claude -- test -x /home/claude/.local/bin/claude && say "claude CLI исполняем" || fail "нет /home/claude/.local/bin/claude"
  if [ -f /etc/agent-system/roles.txt ]; then
    [ "$(stat -c %a:%U:%G /etc/agent-system/roles.txt)" = "640:root:agent-portal" ] && say "roles.txt 640 root:agent-portal" || fail "права roles.txt"
    runuser -u claude -g agent-portal -- test -r /etc/agent-system/roles.txt && say "служба (claude, группа agent-portal) читает roles.txt" || fail "claude с группой agent-portal не читает roles.txt"
  else
    say "roles.txt ещё нет (создаст 52)"
  fi
  if systemctl is-active --quiet agent-portal; then
    [ "$(stat -c %a:%U:%G $(dirname $SOCK))" = "750:claude:agent-portal" ] && say "папка сокета 750 claude:agent-portal" || fail "права папки сокета $(stat -c %a:%U:%G $(dirname $SOCK))"
    if ! id caddy >/dev/null 2>&1; then say "caddy ещё не установлен (50-caddy.sh)"
    else runuser -u caddy -g agent-portal -- test -w $SOCK && say "caddy с группой agent-portal может писать в сокет" || fail "caddy с группой agent-portal не может писать в сокет"
    fi
    runuser -u nobody -- test -w $SOCK 2>/dev/null && fail "посторонний пользователь (nobody) может писать в сокет" || say "посторонние пользователи в сокет не пишут"
    [ "$(stat -c %a:%U:%G $SOCK)" = "660:claude:agent-portal" ] && say "сокет 660 claude:agent-portal" || fail "права сокета $(stat -c %a:%U:%G $SOCK)"
    local props=() l out
    while IFS= read -r l; do [ -n "$l" ] && props+=(-p "$l"); done <<< "$SANDBOX"
    out=$(systemd-run --quiet --wait --collect --pipe -p User=claude -p Group=agent-portal -p UMask=0117 -p Environment=HOME=/home/claude "${props[@]}" /usr/bin/python3 -I -c "$SANDBOX_TEST" 2>&1 || true)
    echo "$out" | sed 's/^/  /'
    echo "$out" | grep -q 'ПРОВАЛ' && bad=1
    [ "$(echo "$out" | grep -c '^ок')" -ge 8 ] || { echo "ПРОВАЛ: проверка песочницы службы не выполнилась целиком"; bad=1; }
    [ "$(curl -s --max-time 5 --unix-socket $SOCK -o /dev/null -w '%{http_code}' http://portal/api/health)" = 200 ] && say "портал отвечает на /api/health (сокет)" || fail "портал не отвечает"
    [ "$(curl -s --max-time 5 --unix-socket $SOCK -o /dev/null -w '%{http_code}' http://portal/api/me)" = 401 ] && say "без заголовков /api/me даёт 401" || fail "/api/me без заголовков не 401"
    ss -ltnH | awk '{print $4}' | grep -q ':8000$' && fail "портал слушает TCP 8000" || say "портал не слушает TCP"
  else
    say "служба agent-portal не запущена (нет кода: ждёт 55)"
  fi
  [ "$bad" = 0 ] && echo "check: порядок" || { echo "check: есть проблемы"; return 1; }
}

if [ "$MODE" = check ]; then
  check
  exit $?
fi
[ "$MODE" = apply ] || { echo "режимы: apply, check" >&2; exit 2; }

if ! getent group agent-portal >/dev/null; then
  groupadd --system agent-portal
  changed=1
fi

if [ "$(dpkg-query -W -f='${Status}' python3-venv 2>/dev/null || true)" != "install ok installed" ]; then
  apt-get update -qq
  apt-get install -y --no-install-recommends python3-venv
  changed=1
fi

if [ -n "${KIT_PORTAL_HOST:-}" ]; then
  # Servers installed from the kit (kit/) name their address in kit.conf instead of Tailscale.
  [[ "$KIT_PORTAL_HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "KIT_PORTAL_HOST: неверное имя" >&2; exit 1; }
  HOST=$KIT_PORTAL_HOST
else
HOST=$(tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
case "$HOST" in *.ts.net) ;; *) echo "нет имени *.ts.net" >&2; exit 1 ;; esac
fi

install -d -m 755 /etc/agent-system
[ -f "$ENVF" ] || { : > "$ENVF"; changed=1; }
setenvkey PORTAL_HOST "$HOST"
setenvkey PORTAL_ROLES_FILE /etc/agent-system/roles.txt
setenvkey PORTAL_ROOTS "${KIT_PORTAL_ROOTS:-/home/claude/work:/home/claude/pc}"
if [ -n "${KIT_OWNER_EMAIL:-}" ]; then
  setenvkey PORTAL_OWNER_EMAIL "$KIT_OWNER_EMAIL"
  setenvkey PORTAL_FEATURES "${KIT_FEATURES:-all}"
  printf '%s\n' "$KIT_OWNER_EMAIL" > /etc/agent-system/owner-email.new
  if ! cmp -s /etc/agent-system/owner-email.new /etc/agent-system/owner-email 2>/dev/null; then changed=1; fi
  chmod 644 /etc/agent-system/owner-email.new
  mv /etc/agent-system/owner-email.new /etc/agent-system/owner-email
fi
setenvkey PORTAL_MAX_CLAUDE 4
setenvkey PORTAL_CLAUDE_BIN /home/claude/.local/bin/claude
chown root:root "$ENVF"
chmod 644 "$ENVF"

if [ "$(stat -c %a:%U $ROOT 2>/dev/null || true)" != "755:root" ]; then
  install -d -m 755 -o root -g root $ROOT
  changed=1
fi
[ -d /home/claude/work ] || install -d -m 700 -o claude -g claude /home/claude/work
for d in /home/claude/.local /home/claude/.local/state /home/claude/.local/state/agent-portal; do
  if [ ! -d "$d" ]; then
    install -d -m 700 -o claude -g claude "$d"
    changed=1
    restart_portal=1
  fi
done
for d in releases venvs; do
  if [ "$(stat -c %a:%U $ROOT/$d 2>/dev/null || true)" != "755:root" ]; then
    install -d -m 755 -o root -g root $ROOT/$d
    changed=1
  fi
done

put /etc/systemd/system/agent-portal.slice 644 root:root <<'EOT'
[Unit]
Description=Портал (лимит памяти)

[Slice]
MemoryHigh=300M
MemoryMax=384M
EOT

put /usr/local/lib/agent-portal-sockperm 755 root:root <<'EOT'
#!/bin/sh
S=/run/agent-portal/portal.sock
i=0
while [ "$i" -lt 100 ]; do
  if [ -S "$S" ]; then
    if [ "$(stat -c %a "$S")" = 666 ] || [ "$i" -ge 50 ]; then
      chmod 660 "$S" && exit 0
    fi
  fi
  i=$((i+1))
  sleep 0.1
done
echo "сокет $S не появился или не удалось сменить права" >&2
exit 1
EOT

put /etc/systemd/system/agent-portal.service 644 root:root <<EOT
[Unit]
Description=Портал Claude-станции (FastAPI на unix-сокете)
After=network.target agent-tmux.service
ConditionPathExists=/opt/agent-portal/app/app/main.py

[Service]
User=claude
Group=agent-portal
UMask=0117
Slice=agent-portal.slice
RuntimeDirectory=agent-portal
RuntimeDirectoryMode=0750
WorkingDirectory=/opt/agent-portal/app
EnvironmentFile=/etc/agent-system/portal.env
Environment=HOME=/home/claude
ExecStart=/opt/agent-portal/venv/bin/uvicorn app.main:app --uds /run/agent-portal/portal.sock --no-server-header --no-access-log --ws-max-size 65536
ExecStartPost=/usr/local/lib/agent-portal-sockperm
Restart=always
RestartSec=3
$SANDBOX

[Install]
WantedBy=multi-user.target
EOT
systemctl daemon-reload

put /usr/local/sbin/agent-apply-roles 750 root:root <<'PYEOF'
#!/usr/bin/python3 -I
import fcntl, grp, hashlib, os, pwd, re, subprocess, sys

ROLES = "/etc/agent-system/roles.txt"
ALLOWED = "/etc/agent-system/allowed-emails.txt"
CADDY = "/etc/caddy/roles.caddy"
SPEC = (
    (ROLES, 0o640, "root", "agent-portal"),
    (ALLOWED, 0o644, "root", "root"),
    (CADDY, 0o640, "root", "caddy"),
)
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
USER = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
OWNER = "owner@example.invalid"
try:  # servers installed from the kit name their owner here (infra/54-portal-base.sh, KIT_OWNER_EMAIL)
    with open("/etc/agent-system/owner-email", encoding="utf-8") as _f:
        OWNER = _f.read().strip().lower() or OWNER
except OSError:
    pass


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse(text, strict=True):
    rows, seen = [], set()
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        p = s.split()
        if len(p) not in (2, 3):
            die("строка %d: нужно «почта роль [пользователь]»" % n)
        email, role = p[0].strip().lower(), p[1].strip().lower()
        if len(p) == 3 and not USER.match(p[2].lower()):
            die("строка %d: пользователь: a-z, 0-9, - или _, до 24 знаков" % n)
        if not EMAIL.match(email):
            die("строка %d: почта в неверном формате" % n)
        if role not in ("admin", "user") and not (not strict and role == "chat"):
            die("строка %d: роль только admin или user" % n)
        if strict and (role == "admin") != (email == OWNER):
            die("строка %d: роль admin только у владельца" % n)
        if email in seen:
            die("строка %d: повтор почты" % n)
        seen.add(email)
        rows.append((email, "user" if role == "chat" else role) + ((p[2].lower(),) if len(p) == 3 else ()))
    if not any(r[1] == "admin" for r in rows):
        die("нужен владелец с ролью admin")
    return rows


def render(rows):
    canon = "".join(" ".join(r) + "\n" for r in rows).encode()
    allowed = "".join("%s\n" % r[0] for r in rows).encode()
    caddy = (
        "map {http.request.header.X-Forwarded-Email} {portal_role} {\n"
        + "".join("\t~(?i)^%s$ %s\n" % (re.escape(r[0]), r[1]) for r in rows)
        + "\tdefault none\n}\n"
    ).encode()
    return {ROLES: canon, ALLOWED: allowed, CADDY: caddy}


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def in_place(path, data, mode, owner, group):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (read(path) == data and (st.st_mode & 0o777) == mode
            and st.st_uid == pwd.getpwnam(owner).pw_uid and st.st_gid == grp.getgrnam(group).gr_gid)


def write(path, data, mode, owner, group):
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chown(tmp, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
    os.chmod(tmp, mode)
    os.rename(tmp, path)


def counts(rows):
    return sum(1 for r in rows if r[1] == "admin"), sum(1 for r in rows if r[1] == "user")


def status():
    data = read(ROLES)
    if data is None:
        print("hash=none consistent=no admin=0 user=0")
        return
    try:
        rows = parse(data.decode("utf-8"), strict=False)
    except SystemExit:
        print("hash=%s consistent=no admin=0 user=0" % hashlib.sha256(data).hexdigest())
        return
    want = render(rows)
    ok = all(in_place(p, want[p], m, o, g) for p, m, o, g in SPEC)
    a, c = counts(rows)
    print("hash=%s consistent=%s admin=%d user=%d" % (hashlib.sha256(data).hexdigest(), "yes" if ok else "no", a, c))


def main():
    if os.geteuid() != 0:
        die("запускать от root")
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status()
        return
    if len(sys.argv) > 1:
        die("режимы: без аргумента (читает stdin) или status")
    rows = parse(sys.stdin.buffer.read(65536).decode("utf-8"))
    want = render(rows)

    lock = os.open("/run/agent-apply-roles.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)

    old = {p: read(p) for p, _, _, _ in SPEC}
    todo = [(p, m, o, g) for p, m, o, g in SPEC if not in_place(p, want[p], m, o, g)]
    a, c = counts(rows)
    if not todo:
        print("admin: %d, user: %d" % (a, c))
        print("изменений нет")
        return

    def rollback():
        for p, m, o, g in SPEC:
            if old[p] is None:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            else:
                write(p, old[p], m, o, g)

    try:
        for p, m, o, g in todo:
            write(p, want[p], m, o, g)
        r = subprocess.run(["/usr/bin/caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError("caddy validate не прошёл")
    except Exception as e:
        rollback()
        die("%s: все три файла возвращены как были" % e)

    paths = [p for p, _, _, _ in todo]
    if CADDY in paths:
        if subprocess.run(["systemctl", "reload", "caddy"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.run(["systemctl", "restart", "caddy"], check=True)
    if ALLOWED in paths and subprocess.run(["systemctl", "is-active", "--quiet", "oauth2-proxy"]).returncode == 0:
        subprocess.run(["systemctl", "restart", "oauth2-proxy"], check=True)
    print("admin: %d, user: %d" % (a, c))
    print("обновлено")


main()
PYEOF

put /usr/local/sbin/agent-portal-install 750 root:root <<'EOT'
#!/usr/bin/env bash
set -euo pipefail
ROOT=/opt/agent-portal
SOCK=/run/agent-portal/portal.sock
[ "$(id -u)" = 0 ] || { echo "запускать от root" >&2; exit 1; }
python3 -c 'import ensurepip' 2>/dev/null || { echo "нет python3-venv (сначала 54-portal-base.sh)" >&2; exit 1; }
install -d -m 755 $ROOT/releases $ROOT/venvs
exec 9>/run/agent-portal-install.lock
flock 9
S=$(mktemp -d $ROOT/.stage.XXXXXX)
trap 'rm -rf "$S"' EXIT

head -c 52428800 > "$S/in.tgz"
tar -tzf "$S/in.tgz" > "$S/names.txt"
tar -tzvf "$S/in.tgz" > "$S/verbose.txt"
if grep -qE '(^/|(^|/)\.\.(/|$))' "$S/names.txt"; then
  echo "в архиве абсолютные пути или .., отказ" >&2
  exit 1
fi
if cut -c1 "$S/verbose.txt" | grep -qvE '^[-d]$'; then
  echo "в архиве ссылки или особые файлы, отказ" >&2
  exit 1
fi
mkdir "$S/new"
tar -xzf "$S/in.tgz" -C "$S/new" --no-same-owner --no-same-permissions
for f in app/main.py app/security.py static/index.html requirements.txt; do
  [ -f "$S/new/$f" ] || { echo "в архиве нет $f, отказ" >&2; exit 1; }
done
chown -R root:root "$S/new"
chmod -R u=rwX,go=rX "$S/new"

newhash=$(cd "$S/new" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
rel=${newhash:0:16}
reqhash=$(sha256sum "$S/new/requirements.txt" | cut -d' ' -f1)
vdir=$ROOT/venvs/${reqhash:0:16}

cur_app=$(readlink -f $ROOT/app 2>/dev/null || true)
cur_venv=$(readlink -f $ROOT/venv 2>/dev/null || true)

migrate_legacy() {
  local name=$1 link=$ROOT/$1 dir
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    case $name in app) dir=$ROOT/releases/legacy ;; venv) dir=$ROOT/venvs/legacy ;; esac
    rm -rf "$dir"
    mv "$link" "$dir"
    ln -s "$dir" "$ROOT/.tmp-$name"
    mv -T "$ROOT/.tmp-$name" "$link"
  fi
}

swap_link() {
  ln -sfn "$2" "$ROOT/.tmp-$1"
  mv -T "$ROOT/.tmp-$1" "$ROOT/$1"
}

relpath=$ROOT/releases/$rel
if [ -d "$relpath" ] && [ "$(cat $relpath/.tree.sha256 2>/dev/null || true)" = "$newhash" ]; then
  :
else
  rm -rf "$relpath"
  mv "$S/new" "$relpath"
  echo "$newhash" > "$relpath/.tree.sha256"
fi

if [ ! -x "$vdir/bin/python" ] || [ ! -f "$vdir/.ok" ]; then
  rm -rf "$vdir"
  python3 -m venv "$vdir"
  if ! PIP_DISABLE_PIP_VERSION_CHECK=1 "$vdir/bin/pip" install -q --require-hashes --only-binary=:all: -r "$relpath/requirements.txt"; then
    rm -rf "$vdir"
    echo "pip не поставил зависимости, ничего не переключено" >&2
    exit 1
  fi
  touch "$vdir/.ok"
fi

if ! (cd "$relpath" && PORTAL_HOST=check.invalid "$vdir/bin/python" -c 'import sys; sys.path.insert(0, "."); import app.main') >/dev/null 2>"$S/import.err"; then
  tail -n 5 "$S/import.err" | cut -c1-200 >&2
  echo "новая версия не импортируется, ничего не переключено" >&2
  exit 1
fi

if [ "$cur_app" = "$relpath" ] && [ "$cur_venv" = "$vdir" ] && systemctl is-active --quiet agent-portal; then
  echo "изменений нет"
  exit 0
fi

migrate_legacy app
migrate_legacy venv
prev_app=$(readlink -f $ROOT/app 2>/dev/null || true)
prev_venv=$(readlink -f $ROOT/venv 2>/dev/null || true)
swap_link app "$relpath"
swap_link venv "$vdir"
systemctl daemon-reload

healthy() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if [ "$(curl -s --max-time 3 --unix-socket $SOCK -o /dev/null -w '%{http_code}' http://portal/api/health || true)" = 200 ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

systemctl enable agent-portal >/dev/null 2>&1 || true
systemctl restart agent-portal || true
if ! healthy; then
  echo "новая версия не отвечает, откат на предыдущую" >&2
  if [ -n "$prev_app" ] && [ -n "$prev_venv" ] && [ -d "$prev_app" ] && [ -d "$prev_venv" ]; then
    swap_link app "$prev_app"
    swap_link venv "$prev_venv"
    systemctl restart agent-portal || true
    healthy && echo "предыдущая версия снова работает" >&2 || echo "предыдущая версия тоже не отвечает: journalctl -u agent-portal -n 30" >&2
  else
    echo "предыдущей версии не было: journalctl -u agent-portal -n 30" >&2
  fi
  exit 1
fi

for d in $ROOT/releases/* $ROOT/venvs/*; do
  [ -e "$d" ] || continue
  case "$(readlink -f "$d")" in
    "$relpath"|"$vdir"|"$prev_app"|"$prev_venv") ;;
    *) rm -rf "$d" ;;
  esac
done
echo "портал обновлён и отвечает"
EOT

systemctl enable agent-portal >/dev/null 2>&1 || true
if [ "$restart_portal" = 1 ] && systemctl is-active --quiet agent-portal; then
  systemctl restart agent-portal
  sleep 3
  systemctl is-active --quiet agent-portal || { echo "портал не поднялся после перезапуска: journalctl -u agent-portal -n 20" >&2; exit 1; }
fi

check
[ "$changed" = 1 ] && echo "готово" || echo "изменений нет"
