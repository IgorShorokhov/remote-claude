#!/usr/bin/env bash
# Без домена: сайт открывается через Tailscale Funnel по адресу вида имя.tailXXXX.ts.net (сертификат даёт Tailscale).
#   bash kit/funnel.sh up        поставить Tailscale, войти (человек открывает ссылку), запомнить адрес
#   bash kit/funnel.sh publish   открыть 127.0.0.1:8080 в интернет через Funnel
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "нужен root" >&2; exit 1; }
HOSTFILE=/etc/agent-system/kit-host

name() { tailscale status --json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'; }

up() {
  if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh -o /tmp/tailscale-install.sh
    sh /tmp/tailscale-install.sh
    rm -f /tmp/tailscale-install.sh
  fi
  systemctl enable --now tailscaled >/dev/null 2>&1
  if ! tailscale status >/dev/null 2>&1; then
    echo ">>> ЧЕЛОВЕКУ: ниже появится ссылка login.tailscale.com. Откройте её и войдите в свой аккаунт Tailscale."
    tailscale up --hostname=remote-claude --timeout=15m
  fi
  local host
  host=$(name)
  case "$host" in *.ts.net) ;; *) echo "у сервера нет имени *.ts.net: в админке Tailscale включите MagicDNS (DNS -> Enable MagicDNS)" >&2; exit 1 ;; esac
  install -d -m 755 /etc/agent-system
  echo "$host" > "$HOSTFILE"
  chmod 644 "$HOSTFILE"
  echo "адрес сайта будет https://$host"
}

publish() {
  echo ">>> Если ниже появится ссылка login.tailscale.com/f/funnel или /f/serve, ЧЕЛОВЕК открывает её и нажимает Enable."
  timeout 900 tailscale funnel --bg 8080
  tailscale funnel status
  local host i code
  host=$(cat "$HOSTFILE")
  for i in $(seq 1 24); do
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$host/" || true)
    case "$code" in 302|401|200) echo "сайт https://$host отвечает (код $code)"; return 0 ;; esac
    sleep 5
  done
  echo "https://$host не ответил за 2 минуты (код $code). Проверьте: tailscale funnel status" >&2
  return 1
}

case "${1:-}" in
  up) up ;;
  publish) publish ;;
  *) sed -n '2,4p' "$0"; exit 2 ;;
esac
