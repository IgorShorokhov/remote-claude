#!/usr/bin/env bash
# Пользователь claude: от него работают Claude Code, портал и чаты. Без пароля и без sudo: сервером управляет ваш собственный пользователь.
set -euo pipefail
note() { echo "[user] $*"; }
[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }

if ! id claude >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" --shell /bin/bash claude >/dev/null
  note "создан пользователь claude"
fi
[ "$(passwd -S claude | awk '{print $2}')" = L ] || { passwd -l claude >/dev/null; note "пароль claude заблокирован"; }
if [ -e /etc/sudoers.d/claude ]; then
  note "внимание: у claude есть /etc/sudoers.d/claude (sudo); установщик его не создаёт и не трогает"
fi
for d in /home/claude/work /home/claude/.claude /home/claude/.claude/projects /home/claude/.claude/skills /home/claude/.local /home/claude/.local/state; do
  [ -d "$d" ] || { install -d -m 700 -o claude -g claude "$d"; note "создана папка $d"; }
done
chmod 755 /home/claude/.claude/skills
note "готово"
