#!/usr/bin/env bash
set -euo pipefail
note() { echo "[43-tmux] $*"; }
die() { echo "[43-tmux] ОШИБКА: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "нужен root (sudo bash -s)"

CHANGED=0
RELOAD=0
ENVFILE=/etc/agent-system/proxy.env
command -v tmux >/dev/null || die "нет tmux (сначала 42-claude-code.sh)"
[ -d /home/claude/work ] || die "нет ~/work (сначала 42-claude-code.sh)"
[ -f "$ENVFILE" ] || die "нет $ENVFILE"
set -a; . "$ENVFILE"; set +a

put() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [ ! -f "$dest" ] || ! cmp -s "$tmp" "$dest" || [ "$(stat -c '%a %U' "$dest")" != "$mode $owner" ]; then
    install -D -m "$mode" -o "$owner" -g "$owner" "$tmp" "$dest"
    CHANGED=1; RELOAD=1; note "обновлён $dest"
  fi
  rm -f "$tmp"
}

put /home/claude/.tmux.conf 644 claude <<EOT
set -g mouse on
set -sg escape-time 10
set -g history-limit 50000
set -g default-terminal "tmux-256color"
$(if [ -n "${HTTPS_PROXY:-}" ]; then for k in HTTPS_PROXY HTTP_PROXY https_proxy http_proxy NO_PROXY no_proxy; do printf 'set-environment -g %s "%s"\n' "$k" "${!k}"; done; fi)
EOT

put /etc/systemd/system/agent-code.slice 644 root <<'EOT'
[Unit]
Description=Сессии Claude Code

[Slice]
MemoryHigh=2G
MemoryMax=2.5G
EOT

put /etc/systemd/system/agent-tmux.service 644 root <<'EOT'
[Unit]
Description=tmux-сервер с сессией main для Claude Code
After=network-online.target warp-svc.service
Wants=network-online.target

[Service]
User=claude
Group=claude
Type=forking
Slice=agent-code.slice
WorkingDirectory=/home/claude/work
EnvironmentFile=/etc/agent-system/proxy.env
Environment=HOME=/home/claude
Environment=LANG=en_US.UTF-8
Environment=PATH=/home/claude/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/tmux new-session -d -s main -c /home/claude/work
ExecStop=/usr/bin/tmux kill-server
Restart=always
RestartSec=5
OOMScoreAdjust=200

[Install]
WantedBy=multi-user.target
EOT

[ "$RELOAD" = 1 ] && systemctl daemon-reload

systemctl is-enabled --quiet agent-tmux || { systemctl enable agent-tmux >/dev/null 2>&1; CHANGED=1; note "agent-tmux в автозапуске"; }
if ! systemctl is-active --quiet agent-tmux; then
  systemctl start agent-tmux; CHANGED=1; note "agent-tmux запущен"
fi
sleep 1
systemctl is-active --quiet agent-tmux || die "agent-tmux не активен"
sudo -u claude tmux has-session -t main 2>/dev/null || die "сессии main нет"

if [ "$CHANGED" = 0 ]; then note "изменений нет"; else note "готово (запущенный tmux подхватит .tmux.conf после перезапуска службы)"; fi
