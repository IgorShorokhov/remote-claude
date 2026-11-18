#!/usr/bin/env bash
# Служба, которая держит процессы Claude Code чатов нового режима портала (portal/app/agent_host.py). Режимы: apply (по умолчанию), check.
# Живёт отдельно от портала, как agent-tmux: перезапуск портала не останавливает чаты. Перезапуск ЭТОЙ службы останавливает все чаты
# нового режима (они просыпаются по --resume), поэтому apply не трогает работающую службу, если юнит и код не менялись.
set -euo pipefail
MODE=${1:-apply}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SRC="$HERE/portal/app/agent_host.py"
DEST=/usr/local/lib/agent-host/agent_host.py
UNIT=/etc/systemd/system/agent-host.service
SOCK=/run/agent-host/host.sock
ENVFILE=/etc/agent-system/proxy.env
changed=0

unit() {
  cat <<'EOT'
[Unit]
Description=Agent host: holds the Claude Code processes of portal chats
After=network-online.target warp-svc.service
Wants=network-online.target

[Service]
Type=simple
User=claude
Group=claude
Slice=agent-code.slice
RuntimeDirectory=agent-host
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
WorkingDirectory=/home/claude/work
EnvironmentFile=/etc/agent-system/proxy.env
Environment=HOME=/home/claude
Environment=LANG=en_US.UTF-8
Environment=PATH=/home/claude/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 -I /usr/local/lib/agent-host/agent_host.py
Restart=always
RestartSec=3
OOMScoreAdjust=200
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOT
}

hello() {
  sudo -u claude python3 -I - "$SOCK" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(sys.argv[1])
line = s.makefile().readline()
sys.exit(0 if json.loads(line).get("ev") == "hello" else 1)
PY
}

check() {
  local bad=0
  [ -f "$DEST" ] && cmp -s "$SRC" "$DEST" && echo "код службы совпадает с репозиторием" || { echo "ПРОВАЛ: $DEST отличается от репозитория"; bad=1; }
  [ -f "$UNIT" ] && [ "$(unit)" = "$(cat "$UNIT")" ] && echo "юнит совпадает" || { echo "ПРОВАЛ: юнит отличается"; bad=1; }
  systemctl is-active --quiet agent-host && echo "agent-host активен" || { echo "ПРОВАЛ: agent-host не активен"; bad=1; }
  hello && echo "сокет отвечает" || { echo "ПРОВАЛ: сокет $SOCK не отвечает"; bad=1; }
  exit $bad
}

[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }
case "$MODE" in check) check ;; apply) ;; *) echo "режимы: apply, check" >&2; exit 2 ;; esac
[ -f "$SRC" ] || { echo "нет $SRC" >&2; exit 1; }
[ -f "$ENVFILE" ] || { echo "нет $ENVFILE" >&2; exit 1; }
python3 -I -c 'import sys; compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")' "$SRC"

install -d -m 755 -o root -g root /usr/local/lib/agent-host
if [ ! -f "$DEST" ] || ! cmp -s "$SRC" "$DEST"; then install -m 644 -o root -g root "$SRC" "$DEST"; changed=1; fi
tmp=$(mktemp); unit > "$tmp"
if [ ! -f "$UNIT" ] || ! cmp -s "$tmp" "$UNIT"; then install -m 644 -o root -g root "$tmp" "$UNIT"; systemctl daemon-reload; changed=1; fi
rm -f "$tmp"
systemctl enable agent-host >/dev/null 2>&1
if ! systemctl is-active --quiet agent-host; then
  systemctl start agent-host
elif [ "$changed" = 1 ]; then
  echo "код или юнит изменились: перезапуск остановит работающие чаты нового режима"
  systemctl restart agent-host
fi
sleep 1
bash "$0" check
