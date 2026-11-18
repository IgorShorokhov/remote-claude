#!/usr/bin/env bash
# Root bridge for the Access page (decision 229): the portal leaves a sign-in change in its state folder, a path unit hands it to
# agent-apply-roles. Run once as root from the repository: sudo bash infra/59-access-bridge.sh [install|status|remove]
set -euo pipefail
MODE=${1:-install}
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=/usr/local/sbin/agent-access-apply
STATE=/home/claude/.local/state/agent-portal/access
UNITS=/etc/systemd/system

[ "$(id -u)" = 0 ] || { echo "нужен root: sudo bash $0 $MODE" >&2; exit 1; }

case "$MODE" in
  status)
    systemctl is-enabled agent-access.path 2>/dev/null || true
    systemctl is-active agent-access.path 2>/dev/null || true
    journalctl -u agent-access.service -n 10 --no-pager -o cat 2>/dev/null || true
    exit 0 ;;
  remove)
    systemctl disable --now agent-access.path 2>/dev/null || true
    rm -f "$UNITS/agent-access.path" "$UNITS/agent-access.service" "$BIN"
    systemctl daemon-reload
    echo "мост снят; копии roles.txt остались в /etc/agent-system/access-backups"
    exit 0 ;;
  install) ;;
  *) echo "режимы: install, status, remove" >&2; exit 2 ;;
esac

[ -x /usr/local/sbin/agent-apply-roles ] || { echo "нет agent-apply-roles: сначала infra/54-portal-base.sh" >&2; exit 1; }
/usr/bin/python3 -I -m py_compile "$HERE/access_bridge.py"
install -m 750 -o root -g root "$HERE/access_bridge.py" "$BIN"
install -d -m 700 -o root -g root /etc/agent-system/access-backups
runuser -u claude -- install -d -m 700 "$STATE"

cat > "$UNITS/agent-access.service" <<EOT
[Unit]
Description=Apply a sign-in change from the portal's Access page
ConditionPathExists=$STATE/request.json

[Service]
Type=oneshot
ExecStart=$BIN
TimeoutStartSec=150
EOT

cat > "$UNITS/agent-access.path" <<EOT
[Unit]
Description=Watch the portal's Access page for sign-in changes

[Path]
PathExists=$STATE/request.json
PathModified=$STATE/request.json
Unit=agent-access.service
TriggerLimitIntervalSec=60
TriggerLimitBurst=20

[Install]
WantedBy=multi-user.target
EOT

systemctl daemon-reload
systemctl enable --now agent-access.path
systemctl is-active --quiet agent-access.path || { echo "agent-access.path не запустился" >&2; exit 1; }
echo "мост установлен: $BIN, agent-access.path активен"
