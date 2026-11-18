#!/usr/bin/env bash
# Ночные резервные копии на этом же сервере: чаты и настройки Claude, папка work, состояние портала и список почт.
# Хранятся 7 дней в /var/backups/remote-claude (только root). Вход в Claude (.credentials.json) не копируется.
#   bash kit/backup.sh install | remove | run | list
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }
DEST=/var/backups/remote-claude
BIN=/usr/local/sbin/remote-claude-backup

script() {
  cat <<'EOT'
#!/usr/bin/env bash
set -euo pipefail
DEST=/var/backups/remote-claude
install -d -m 700 -o root -g root "$DEST"
name="$DEST/backup-$(date +%Y%m%d-%H%M%S).tar.zst"
tmp="$name.part"
tar --zstd -cf "$tmp" --warning=no-file-changed --ignore-failed-read \
  --exclude='home/claude/.claude/.credentials.json' --exclude='home/claude/.claude/shell-snapshots' \
  --exclude='home/claude/work/**/node_modules' --exclude='home/claude/work/**/.venv' \
  -C / home/claude/work home/claude/.claude home/claude/.local/state/agent-portal etc/agent-system/roles.txt etc/agent-system/portal.env \
  || [ $? = 1 ]
chmod 600 "$tmp"
mv "$tmp" "$name"
find "$DEST" -name 'backup-*.tar.zst' -mtime +6 -delete
echo "копия $name, $(du -h "$name" | cut -f1)"
EOT
}

case "${1:-}" in
  install)
    command -v zstd >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd >/dev/null
    tmp=$(mktemp); script > "$tmp"
    cmp -s "$tmp" "$BIN" || install -m 750 -o root -g root "$tmp" "$BIN"
    rm -f "$tmp"
    cat > /etc/systemd/system/remote-claude-backup.service <<EOT
[Unit]
Description=Remote Claude: nightly backup

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart=$BIN
EOT
    cat > /etc/systemd/system/remote-claude-backup.timer <<'EOT'
[Unit]
Description=Remote Claude: nightly backup at 04:30

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=20m
Persistent=true

[Install]
WantedBy=timers.target
EOT
    systemctl daemon-reload
    systemctl enable --now remote-claude-backup.timer >/dev/null 2>&1
    echo "резервные копии: каждую ночь в $DEST, 7 дней"
    ;;
  remove)
    if [ -e /etc/systemd/system/remote-claude-backup.timer ]; then
      systemctl disable --now remote-claude-backup.timer >/dev/null 2>&1 || true
      rm -f /etc/systemd/system/remote-claude-backup.timer /etc/systemd/system/remote-claude-backup.service
      systemctl daemon-reload
      echo "ночные копии выключены; старые копии остались в $DEST"
    fi
    ;;
  run) "$BIN" ;;
  list) ls -lh "$DEST" 2>/dev/null || echo "копий ещё нет" ;;
  *) sed -n '2,4p' "$0"; exit 2 ;;
esac
